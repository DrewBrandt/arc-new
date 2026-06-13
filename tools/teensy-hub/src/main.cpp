// ARC Teensy hub -- the nosecone's central router.
//
// Responsibilities:
//   * Route ARC frames by destination. Nosecone spokes (FC-N, ARCH-Mega-N,
//     command radio, pi-5-nose) each have a UART link; the command radio also
//     carries ground-station traffic (GROUND, RADIO_G). Everything else
//     defaults to pi-5-nose, which is the WiFi gateway off the nosecone.
//   * Terminate + transcode frames addressed to the proprietary data radio
//     (RADIO_DATA) via the data_radio module.
//   * Generate its own traffic: a broadcast NETMGMT heartbeat and periodic
//     RADIO/POWER status polls.
//   * Store everything passing through (hub_store) and surface a quick-look
//     status on the OLED.
//
// USB serial is a debug console: press Enter for a one-line status dump.

#include <Arduino.h>

#include "arc_protocol.h"
#include "arc_router.h"
#include "arc_reliable.h"
#include "arc_messages_netmgmt.h"
#include "arc_messages_radio.h"
#include "arc_messages_power.h"

#include "hub_config.h"
#include "hub_links.h"
#include "data_radio.h"
#include "hub_store.h"
#include "hub_oled.h"

// ----------------------------------------------------------------------
// State
// ----------------------------------------------------------------------
static arc_router_t   g_router;
static arc_reliable_t g_reliable;

static uint8_t  g_session = 1;
static uint16_t g_seq = 0;  // seq for unreliable hub-originated frames

static uint32_t g_last_heartbeat_ms = 0;
static uint32_t g_last_poll_ms = 0;
static uint32_t g_last_status_print_ms = 0;

// ----------------------------------------------------------------------
// Router / reliable glue
// ----------------------------------------------------------------------
static void route_via_router(void* user, const arc_frame_t* frame) {
    (void)user;
    arc_router_route(&g_router, frame);
}

// arc_reliable_receive has an incompatible signature for local_fn, so wrap it.
static void local_trampoline(void* user, const arc_frame_t* frame) {
    arc_reliable_receive((arc_reliable_t*)user, frame);
}

// Frames addressed to the hub itself land here (status replies to our polls,
// heartbeats, ACKs). Peer liveness is already updated in the RX path; here we
// cache vitals for the OLED.
static void on_deliver(void* user, const arc_frame_t* f) {
    (void)user;
    if (f->family == ARC_FAMILY_RADIO && f->type == ARC_RADIO_STATUS_REPORT) {
        arc_radio_status_report_t r;
        if (arc_radio_status_report_decode(f->payload, f->payload_len, &r) == ARC_OK) {
            hub_store_set_radio_vitals(r.rssi_dbm, r.snr_db);
        }
    } else if (f->family == ARC_FAMILY_POWER && f->type == ARC_POWER_STATUS_REPORT) {
        arc_power_status_report_t p;
        if (arc_power_status_report_decode(f->payload, f->payload_len, &p) == ARC_OK) {
            hub_store_set_power_vitals(p.bus_voltage_mv);
        }
    }
}

// ----------------------------------------------------------------------
// Hub-originated sends
// ----------------------------------------------------------------------
static void hub_send(uint8_t dst, uint8_t family, uint8_t type,
                     const uint8_t* payload, size_t len) {
    uint8_t built[ARC_MAX_FRAME_SIZE];
    int n = arc_frame_build(built, sizeof(built), HUB_ADDR, dst,
                            0, g_session, g_seq++, family, type, payload, len);
    if (n < 0) return;
    arc_frame_t f;
    if (arc_frame_parse(built, n, &f) == ARC_OK) {
        arc_router_route(&g_router, &f);
    }
}

static void maybe_heartbeat(uint32_t now) {
    if (now - g_last_heartbeat_ms < HUB_HEARTBEAT_MS) return;
    g_last_heartbeat_ms = now;
    uint8_t built[ARC_MAX_FRAME_SIZE];
    int n = arc_frame_build(built, sizeof(built), HUB_ADDR, ARC_ADDR_BROADCAST,
                            0, g_session, g_seq++,
                            ARC_FAMILY_NETMGMT, ARC_NETMGMT_HEARTBEAT, nullptr, 0);
    if (n > 0) hub_links_broadcast(built, n);
}

static void maybe_poll(uint32_t now) {
    if (now - g_last_poll_ms < HUB_POLL_MS) return;
    g_last_poll_ms = now;
    hub_send(ARC_ADDR_RADIO_CMD, ARC_FAMILY_RADIO, ARC_RADIO_GET_STATUS, nullptr, 0);
    hub_send(ARC_ADDR_ARCH_MEGA_N, ARC_FAMILY_POWER, ARC_POWER_GET_STATUS, nullptr, 0);
}

// ----------------------------------------------------------------------
// RX: pull frames off every spoke, record them, route them.
// ----------------------------------------------------------------------
static void pump_links(uint32_t now) {
    uint8_t decoded[ARC_MAX_FRAME_SIZE];
    for (int id = 0; id < HUB_LINK_COUNT; id++) {
        int n;
        while ((n = hub_link_read((HubLinkId)id, decoded, sizeof(decoded), now)) != 0) {
            if (n < 0) {  // COBS error
                hub_store_note_error();
                continue;
            }
            arc_frame_t f;
            if (arc_frame_parse(decoded, n, &f) == ARC_OK) {
                hub_store_record(&f, now);
                arc_router_route(&g_router, &f);
            } else {
                hub_store_note_error();
            }
        }
    }
}

// ----------------------------------------------------------------------
// Tiny USB console
// ----------------------------------------------------------------------
static void print_status(uint32_t now) {
    Serial.print(F("[hub] up="));   Serial.print(now / 1000UL); Serial.print('s');
    Serial.print(F(" frames="));    Serial.print(hub_store_total_frames());
    Serial.print(F(" fps="));       Serial.print(hub_store_frames_per_sec(now));
    Serial.print(F(" err="));       Serial.print(hub_store_error_count());
    Serial.print(F(" peers="));     Serial.print(hub_store_online_count(now, HUB_PEER_TIMEOUT_MS));
    Serial.print(F(" data-tx="));   Serial.print(data_radio_tx_count());
    Serial.print(F(" links:"));
    for (int id = 0; id < HUB_LINK_COUNT; id++) {
        Serial.print(' ');
        Serial.print(hub_links_name((HubLinkId)id));
        Serial.print(hub_links_online((HubLinkId)id, now, HUB_PEER_TIMEOUT_MS) ? '+' : '-');
    }
    Serial.println();
}

static void pump_usb(uint32_t now) {
    while (Serial.available()) {
        int c = Serial.read();
        if (c == '\n' || c == '\r') print_status(now);
    }
}

// ----------------------------------------------------------------------
// Setup / loop
// ----------------------------------------------------------------------
void setup() {
    Serial.begin(HUB_USB_BAUD);
    uint32_t start = millis();
    while (!Serial && millis() - start < 1500) { /* brief wait for USB */ }

    randomSeed(analogRead(A0) ^ micros());
    g_session = (uint8_t)random(1, 256);

    hub_links_begin();
    data_radio_begin();
    hub_store_init(millis());
    bool oled_ok = hub_oled_begin();

    // Reliable endpoint first so the router can point its local handler at it.
    arc_reliable_init(&g_reliable, HUB_ADDR, g_session,
                      /*timeout_ms=*/1000, /*max_retries=*/3, /*first_seq=*/0,
                      route_via_router, on_deliver, /*fail=*/nullptr, nullptr);

    arc_router_init(&g_router, HUB_ADDR, local_trampoline, &g_reliable);

    int idx_pi5   = arc_router_add_link(&g_router, hub_link_send, hub_links_get(HUB_LINK_PI5));
    int idx_fc    = arc_router_add_link(&g_router, hub_link_send, hub_links_get(HUB_LINK_FC));
    int idx_power = arc_router_add_link(&g_router, hub_link_send, hub_links_get(HUB_LINK_POWER));
    int idx_rcmd  = arc_router_add_link(&g_router, hub_link_send, hub_links_get(HUB_LINK_RADIO_CMD));
    int idx_data  = arc_router_add_link(&g_router, data_radio_link_send, nullptr);

    // Nosecone spokes.
    arc_router_add_route(&g_router, ARC_ADDR_FC_N,        idx_fc);
    arc_router_add_route(&g_router, ARC_ADDR_ARCH_MEGA_N, idx_power);
    arc_router_add_route(&g_router, ARC_ADDR_RADIO_CMD,   idx_rcmd);
    arc_router_add_route(&g_router, ARC_ADDR_CONTROLLER,  idx_pi5);
    // Ground station + ground radio reach us over the RF command link.
    arc_router_add_route(&g_router, ARC_ADDR_GROUND,      idx_rcmd);
    arc_router_add_route(&g_router, ARC_ADDR_RADIO_G,     idx_rcmd);
    // Live data downlink: transcoded, not ARC-forwarded.
    arc_router_add_route(&g_router, ARC_ADDR_RADIO_DATA,  idx_data);
    // Everything off-nosecone (senders, other bays) goes to the Pi 5 gateway.
    arc_router_set_default(&g_router, idx_pi5);

    Serial.println();
    Serial.print(F("ARC Teensy hub | addr=0x"));
    if (HUB_ADDR < 0x10) Serial.print('0');
    Serial.print(HUB_ADDR, HEX);
    Serial.print(F(" | session=")); Serial.print(g_session);
    Serial.print(F(" | oled=")); Serial.println(oled_ok ? F("ok") : F("absent"));
    Serial.println(F("press Enter for status"));
}

void loop() {
    uint32_t now = millis();
    pump_links(now);
    arc_reliable_tick(&g_reliable, now);
    maybe_heartbeat(now);
    maybe_poll(now);
    hub_oled_tick(now);
    pump_usb(now);

    if (now - g_last_status_print_ms >= 5000) {
        g_last_status_print_ms = now;
        print_status(now);
    }
}
