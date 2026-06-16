// ARC Teensy hub -- the nosecone's central router.
//
// Responsibilities:
//   * Route ARC frames by destination. Nosecone spokes (FC-N, ARCH-Mega-N,
//     command radio, pi-5-nose) each have a UART link. The hub learns which
//     node is on which UART from heartbeat/source traffic instead of polling
//     fixed ports.
//   * Generate its own traffic: a broadcast NETMGMT heartbeat. It also
//     coalesces node heartbeats so every peer does not have to cross the RF
//     link with a standalone heartbeat.
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
#include "hub_map.h"
#include "hub_links.h"
#include "data_radio.h"
#include "hub_store.h"
#include "hub_oled.h"

// ----------------------------------------------------------------------
// State
// ----------------------------------------------------------------------
static arc_reliable_t g_reliable;

static uint8_t  g_session = 1;
static uint16_t g_seq = 0;  // seq for unreliable hub-originated frames

static uint32_t g_last_heartbeat_ms = 0;
static uint32_t g_last_status_print_ms = 0;
static uint32_t g_last_discovery_ms = 0;

struct LearnedRoute {
    bool         in_use;
    uint8_t      addr;
    HubRouteLink link;
    uint32_t     last_ms;
    uint16_t     moves;
};

static constexpr uint8_t  HUB_LEARNED_ROUTE_MAX = 16;
static constexpr uint32_t HUB_LEARNED_ROUTE_TIMEOUT_MS = 15000;
static constexpr uint8_t  HUB_HEARTBEAT_SEEN_MAX = 24;
static constexpr uint8_t  HUB_DISCOVERY_MAGIC = 0xD1;
static constexpr uint32_t HUB_DISCOVERY_THROTTLE_MS = 500;
static LearnedRoute g_learned[HUB_LEARNED_ROUTE_MAX];
static uint8_t g_heartbeat_seen[HUB_HEARTBEAT_SEEN_MAX];
static uint8_t g_heartbeat_seen_count = 0;

// ----------------------------------------------------------------------
// Router / reliable glue
// ----------------------------------------------------------------------
static int route_frame(const arc_frame_t* frame, uint32_t now, int8_t ingress_link);
static void on_deliver(void* user, const arc_frame_t* f);

static void route_via_router(void* user, const arc_frame_t* frame) {
    (void)user;
    route_frame(frame, millis(), (int8_t)HUB_ROUTE_LINK_LOCAL);
}

// arc_reliable_receive has an incompatible signature for local_fn, so wrap it.
static void local_trampoline(void* user, const arc_frame_t* frame) {
    arc_reliable_receive((arc_reliable_t*)user, frame);
}

static void reset_reliable_endpoint(uint32_t now) {
    (void)now;
    g_session = (uint8_t)random(1, 256);
    g_seq = 0;
    arc_reliable_init(&g_reliable, HUB_ADDR, g_session,
                      /*timeout_ms=*/1000, /*max_retries=*/3, /*first_seq=*/0,
                      route_via_router, on_deliver, /*fail=*/nullptr, nullptr);
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
    } else if (f->family == ARC_FAMILY_NETMGMT && f->type == ARC_NETMGMT_SESSION_RESET) {
        reset_reliable_endpoint(millis());
    }
}

static HubRouteLink link_for_ingress(int8_t ingress_link) {
    if (ingress_link >= 0 && ingress_link < HUB_LINK_COUNT) {
        return (HubRouteLink)ingress_link;
    }
    return HUB_ROUTE_LINK_UNKNOWN;
}

static bool learnable_addr(uint8_t addr) {
    return addr != ARC_ADDR_UNASSIGNED
        && addr != ARC_ADDR_BROADCAST
        && addr != HUB_ADDR;
}

static LearnedRoute* find_learned(uint8_t addr) {
    for (uint8_t i = 0; i < HUB_LEARNED_ROUTE_MAX; i++) {
        if (g_learned[i].in_use && g_learned[i].addr == addr) return &g_learned[i];
    }
    return nullptr;
}

static LearnedRoute* alloc_learned(void) {
    for (uint8_t i = 0; i < HUB_LEARNED_ROUTE_MAX; i++) {
        if (!g_learned[i].in_use) return &g_learned[i];
    }
    return nullptr;
}

static bool learned_active(const LearnedRoute* r, uint32_t now) {
    return r && r->in_use && (now - r->last_ms) < HUB_LEARNED_ROUTE_TIMEOUT_MS;
}

static void expire_learned(uint32_t now) {
    for (uint8_t i = 0; i < HUB_LEARNED_ROUTE_MAX; i++) {
        if (g_learned[i].in_use && !learned_active(&g_learned[i], now)) {
            g_learned[i].in_use = false;
        }
    }
}

static void learn_source_route(const arc_frame_t* frame, int8_t ingress_link, uint32_t now) {
    if (!frame || !learnable_addr(frame->src)) return;
    HubRouteLink learned_link = link_for_ingress(ingress_link);
    if (learned_link < 0) return;

    LearnedRoute* r = find_learned(frame->src);
    if (r) {
        if (r->link != learned_link) {
            r->moves++;
            Serial.print(F("[hub] route move 0x"));
            if (frame->src < 0x10) Serial.print('0');
            Serial.print(frame->src, HEX);
            Serial.print(F(" "));
            Serial.print(hub_route_link_name(r->link));
            Serial.print(F(" -> "));
            Serial.print(hub_route_link_name(learned_link));
            Serial.print(F(" moves="));
            Serial.println(r->moves);
            r->link = learned_link;
        }
        r->last_ms = now;
        return;
    }

    r = alloc_learned();
    if (!r) return;
    r->in_use = true;
    r->addr = frame->src;
    r->link = learned_link;
    r->last_ms = now;
    r->moves = 0;

    Serial.print(F("[hub] learned 0x"));
    if (frame->src < 0x10) Serial.print('0');
    Serial.print(frame->src, HEX);
    Serial.print(F(" "));
    Serial.print(hub_addr_name(frame->src));
    Serial.print(F(" -> "));
    Serial.println(hub_route_link_name(learned_link));
}

static HubRouteLink route_for_dst(uint8_t dst, uint32_t now, bool* learned) {
    expire_learned(now);
    LearnedRoute* r = find_learned(dst);
    if (learned_active(r, now)) {
        if (learned) *learned = true;
        return r->link;
    }

    if (learned) *learned = false;
    return HUB_ROUTE_LINK_UNKNOWN;
}

static bool is_heartbeat(const arc_frame_t* frame) {
    return frame
        && frame->family == ARC_FAMILY_NETMGMT
        && frame->type == ARC_NETMGMT_HEARTBEAT;
}

static void note_heartbeat_seen(uint8_t addr) {
    if (!learnable_addr(addr)) return;
    for (uint8_t i = 0; i < g_heartbeat_seen_count; i++) {
        if (g_heartbeat_seen[i] == addr) return;
    }
    if (g_heartbeat_seen_count < HUB_HEARTBEAT_SEEN_MAX) {
        g_heartbeat_seen[g_heartbeat_seen_count++] = addr;
    }
}

static bool send_route_link(HubRouteLink link, const arc_frame_t* frame) {
    if (link >= 0 && link < HUB_ROUTE_LINK_COUNT) {
        hub_link_send(hub_links_get((HubLinkId)link), frame);
        return true;
    }
    return false;
}

static int route_broadcast(const arc_frame_t* frame, uint32_t now, int8_t ingress_link) {
    uint8_t built[ARC_MAX_FRAME_SIZE];
    int n = arc_frame_build(built, sizeof(built),
                            frame->src, frame->dst,
                            frame->flags, frame->session, frame->seq,
                            frame->family, frame->type,
                            frame->payload, frame->payload_len);
    if (n > 0) {
        HubLinkId except_id = HUB_LINK_COUNT;
        if (ingress_link >= 0 && ingress_link < HUB_LINK_COUNT) {
            except_id = (HubLinkId)ingress_link;
        }
        hub_links_broadcast_except(built, n, except_id);
    }
    hub_store_record(frame, now, ingress_link,
                     (int8_t)HUB_ROUTE_LINK_BROADCAST,
                     ARC_ROUTE_FORWARDED);
    return ARC_ROUTE_FORWARDED;
}

static void send_discovery_ping(uint8_t dst, uint32_t now) {
    if (now - g_last_discovery_ms < HUB_DISCOVERY_THROTTLE_MS) return;
    g_last_discovery_ms = now;

    const uint8_t payload[] = {HUB_DISCOVERY_MAGIC, dst};
    uint8_t built[ARC_MAX_FRAME_SIZE];
    int n = arc_frame_build(built, sizeof(built), HUB_ADDR, ARC_ADDR_BROADCAST,
                            0, g_session, g_seq++,
                            ARC_FAMILY_NETMGMT, ARC_NETMGMT_HEARTBEAT,
                            payload, sizeof(payload));
    if (n <= 0) return;

    hub_links_broadcast(built, n);
    arc_frame_t f;
    if (arc_frame_parse(built, n, &f) == ARC_OK) {
        hub_store_record(&f, now, (int8_t)HUB_ROUTE_LINK_LOCAL,
                         (int8_t)HUB_ROUTE_LINK_BROADCAST,
                         ARC_ROUTE_FORWARDED);
    }
}

static int route_frame(const arc_frame_t* frame, uint32_t now, int8_t ingress_link) {
    if (!frame) return ARC_ERR_BAD_ARG;

    // If our own broadcast comes back from a spoke because of loopback wiring,
    // a bridge echo, or another node reflecting it, do not rebroadcast it. That
    // creates a storm and makes links look busy without learning real peers.
    if (frame->src == HUB_ADDR && ingress_link != (int8_t)HUB_ROUTE_LINK_LOCAL) {
        hub_store_record(frame, now, ingress_link,
                         (int8_t)HUB_ROUTE_LINK_LOCAL,
                         ARC_ROUTE_LOCAL);
        return ARC_ROUTE_LOCAL;
    }

    if (frame->dst == HUB_ADDR) {
        local_trampoline(&g_reliable, frame);
        hub_store_record(frame, now, ingress_link,
                         (int8_t)HUB_ROUTE_LINK_LOCAL,
                         ARC_ROUTE_LOCAL);
        return ARC_ROUTE_LOCAL;
    }

    if (is_heartbeat(frame) && frame->src != HUB_ADDR) {
        note_heartbeat_seen(frame->src);
        hub_store_record(frame, now, ingress_link,
                         (int8_t)HUB_ROUTE_LINK_LOCAL,
                         ARC_ROUTE_LOCAL);
        return ARC_ROUTE_LOCAL;
    }

    if (frame->dst == ARC_ADDR_BROADCAST) {
        return route_broadcast(frame, now, ingress_link);
    }

    bool learned = false;
    HubRouteLink out_link = route_for_dst(frame->dst, now, &learned);
    if (out_link == HUB_ROUTE_LINK_UNKNOWN) {
        send_discovery_ping(frame->dst, now);
        hub_store_record(frame, now, ingress_link,
                         (int8_t)HUB_ROUTE_LINK_UNKNOWN,
                         ARC_ERR_BAD_ARG);
        return ARC_ERR_BAD_ARG;
    }

    int route_result = ARC_ERR_BAD_ARG;
    if (send_route_link(out_link, frame)) {
        route_result = ARC_ROUTE_FORWARDED;
    }
    hub_store_record(frame, now, ingress_link,
                     route_result < 0 ? (int8_t)HUB_ROUTE_LINK_UNKNOWN : (int8_t)out_link,
                     route_result);
    return route_result;
}

static void maybe_heartbeat(uint32_t now) {
    if (now - g_last_heartbeat_ms < HUB_HEARTBEAT_MS) return;
    g_last_heartbeat_ms = now;
    uint8_t built[ARC_MAX_FRAME_SIZE];
    uint8_t payload[HUB_HEARTBEAT_SEEN_MAX + 1];
    size_t payload_len = 0;
    if (g_heartbeat_seen_count > 0) {
        payload[0] = g_heartbeat_seen_count;
        memcpy(&payload[1], g_heartbeat_seen, g_heartbeat_seen_count);
        payload_len = (size_t)g_heartbeat_seen_count + 1;
        g_heartbeat_seen_count = 0;
    }
    int n = arc_frame_build(built, sizeof(built), HUB_ADDR, ARC_ADDR_BROADCAST,
                            0, g_session, g_seq++,
                            ARC_FAMILY_NETMGMT, ARC_NETMGMT_HEARTBEAT,
                            payload_len > 0 ? payload : nullptr, payload_len);
    if (n > 0) {
        hub_links_broadcast(built, n);
        arc_frame_t f;
        if (arc_frame_parse(built, n, &f) == ARC_OK) {
            hub_store_record(&f, now, (int8_t)HUB_ROUTE_LINK_LOCAL,
                             (int8_t)HUB_ROUTE_LINK_BROADCAST,
                             ARC_ROUTE_FORWARDED);
        }
    }
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
                learn_source_route(&f, (int8_t)id, now);
                route_frame(&f, now, (int8_t)id);
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
    expire_learned(now);
    Serial.print(F("[hub] up="));   Serial.print(now / 1000UL); Serial.print('s');
    Serial.print(F(" frames="));    Serial.print(hub_store_total_frames());
    Serial.print(F(" fps="));       Serial.print(hub_store_frames_per_sec(now));
    Serial.print(F(" err="));       Serial.print(hub_store_error_count());
    Serial.print(F(" peers="));     Serial.print(hub_store_online_count(now, HUB_PEER_TIMEOUT_MS));
    uint8_t learned_count = 0;
    for (uint8_t i = 0; i < HUB_LEARNED_ROUTE_MAX; i++) {
        if (g_learned[i].in_use) learned_count++;
    }
    Serial.print(F(" learned="));   Serial.print(learned_count);
    Serial.print(F(" hb-seen="));   Serial.print(g_heartbeat_seen_count);
    Serial.print(F(" data-tx="));   Serial.print(data_radio_tx_count());
    Serial.print(F(" links:"));
    for (int id = 0; id < HUB_LINK_COUNT; id++) {
        Serial.print(' ');
        Serial.print(hub_links_name((HubLinkId)id));
        Serial.print(hub_links_online((HubLinkId)id, now, HUB_PEER_TIMEOUT_MS) ? '+' : '-');
    }
    Serial.print(F(" rx:"));
    for (int id = 0; id < HUB_LINK_COUNT; id++) {
        HubLink* link = hub_links_get((HubLinkId)id);
        Serial.print(' ');
        Serial.print(hub_links_name((HubLinkId)id));
        Serial.print('=');
        Serial.print(link ? link->rx_count : 0);
    }
    const hub_frame_rec_t* last = hub_store_last();
    if (last) {
        Serial.print(F(" last="));
        Serial.print(hub_route_link_name((HubRouteLink)last->in_link));
        Serial.print(F("->"));
        Serial.print(hub_route_link_name((HubRouteLink)last->out_link));
        Serial.print(F(" "));
        Serial.print(hub_addr_name(last->src));
        Serial.print(F(">"));
        Serial.print(hub_addr_name(last->dst));
    }
    Serial.println();
}

static void print_learned_routes(uint32_t now) {
    expire_learned(now);
    Serial.println(F("[hub] learned routes"));
    bool any = false;
    for (uint8_t i = 0; i < HUB_LEARNED_ROUTE_MAX; i++) {
        const LearnedRoute& r = g_learned[i];
        if (!r.in_use) continue;
        any = true;
        Serial.print(F("  0x"));
        if (r.addr < 0x10) Serial.print('0');
        Serial.print(r.addr, HEX);
        Serial.print(F(" "));
        Serial.print(hub_addr_name(r.addr));
        Serial.print(F(" -> "));
        Serial.print(hub_route_link_name(r.link));
        Serial.print(F(" age="));
        Serial.print(now - r.last_ms);
        Serial.print(F("ms moves="));
        Serial.println(r.moves);
    }
    if (!any) Serial.println(F("  (none)"));
}

static void pump_usb(uint32_t now) {
    while (Serial.available()) {
        int c = Serial.read();
        if (c == '\n' || c == '\r') print_status(now);
        if (c == 'm' || c == 'M') hub_map_print(Serial);
        if (c == 'l' || c == 'L') print_learned_routes(now);
        if (c == 'i' || c == 'I') hub_oled_scan_i2c(Serial);
        if (c == 'o' || c == 'O') {
            Serial.println(F("[oled] forcing test pattern for 5s"));
            hub_oled_self_test(5000);
        }
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

    arc_reliable_init(&g_reliable, HUB_ADDR, g_session,
                      /*timeout_ms=*/1000, /*max_retries=*/3, /*first_seq=*/0,
                      route_via_router, on_deliver, /*fail=*/nullptr, nullptr);

    Serial.println();
    Serial.print(F("ARC Teensy hub | addr=0x"));
    if (HUB_ADDR < 0x10) Serial.print('0');
    Serial.print(HUB_ADDR, HEX);
    Serial.print(F(" | session=")); Serial.print(g_session);
    Serial.print(F(" | oled=")); Serial.println(oled_ok ? F("ok") : F("absent"));
    Serial.println(F("press Enter=status, m=route map, l=learned, i=i2c scan, o=oled test"));
    hub_map_print(Serial);
}

void loop() {
    uint32_t now = millis();
    pump_links(now);
    arc_reliable_tick(&g_reliable, now);
    maybe_heartbeat(now);
    hub_oled_tick(now);
    pump_usb(now);

    if (now - g_last_status_print_ms >= 5000) {
        g_last_status_print_ms = now;
        print_status(now);
    }
}
