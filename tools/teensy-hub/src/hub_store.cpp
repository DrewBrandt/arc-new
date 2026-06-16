// hub_store.cpp -- see hub_store.h.

#include "hub_store.h"
#include "hub_map.h"

#include <string.h>

#ifdef HUB_SD_LOG
#include <SD.h>
static bool g_sd_ok = false;
#endif

static hub_frame_rec_t g_ring[HUB_STORE_RING];
static uint16_t        g_ring_head = 0;   // index of next write
static uint16_t        g_ring_count = 0;  // entries filled (<= HUB_STORE_RING)

static uint32_t g_total_frames = 0;
static uint32_t g_errors = 0;

// 1-second throughput window.
static uint32_t g_window_start_ms = 0;
static uint16_t g_window_count = 0;
static uint16_t g_last_rate = 0;

typedef struct {
    bool     in_use;
    uint8_t  addr;
    uint32_t last_ms;
} hub_peer_t;
static hub_peer_t g_peers[HUB_PEER_MAX];

// Vitals snapshot.
static bool    g_have_radio = false;
static int8_t  g_radio_rssi = 0;
static int8_t  g_radio_snr = 0;
static bool    g_have_power = false;
static uint16_t g_power_bus_mv = 0;

void hub_store_init(uint32_t now_ms) {
    g_ring_head = 0;
    g_ring_count = 0;
    g_total_frames = 0;
    g_errors = 0;
    g_window_start_ms = now_ms;
    g_window_count = 0;
    g_last_rate = 0;
    memset(g_peers, 0, sizeof(g_peers));
    g_have_radio = false;
    g_have_power = false;

#ifdef HUB_SD_LOG
    g_sd_ok = SD.begin(BUILTIN_SDCARD);
#endif
}

static void note_peer(uint8_t addr, uint32_t now_ms) {
    int free_idx = -1;
    for (int i = 0; i < HUB_PEER_MAX; i++) {
        if (g_peers[i].in_use && g_peers[i].addr == addr) {
            g_peers[i].last_ms = now_ms;
            return;
        }
        if (!g_peers[i].in_use && free_idx < 0) free_idx = i;
    }
    if (free_idx >= 0) {
        g_peers[free_idx].in_use = true;
        g_peers[free_idx].addr = addr;
        g_peers[free_idx].last_ms = now_ms;
    }
    // Table full of distinct peers: silently drop (we only have so many nodes).
}

static void roll_window(uint32_t now_ms) {
    if (now_ms - g_window_start_ms >= 1000) {
        g_last_rate = g_window_count;
        g_window_count = 0;
        g_window_start_ms = now_ms;
    }
}

#ifdef HUB_SD_LOG
static void print_hex2(File& f, uint8_t v) {
    if (v < 0x10) f.print('0');
    f.print(v, HEX);
}

static const char* link_name_for_log(int8_t link) {
    return hub_route_link_name((HubRouteLink)link);
}

static void sd_log(const hub_frame_rec_t* rec, const arc_frame_t* frame) {
    if (!g_sd_ok) return;
    File f = SD.open("arc_hub.log", FILE_WRITE);
    if (!f) return;

    f.print((unsigned long)rec->t_ms);
    f.print(',');
    f.print(link_name_for_log(rec->in_link));
    f.print(',');
    f.print(link_name_for_log(rec->out_link));
    f.print(',');
    f.print(rec->route_result);
    f.print(',');
    print_hex2(f, rec->src);
    f.print(',');
    print_hex2(f, rec->dst);
    f.print(',');
    print_hex2(f, rec->flags);
    f.print(',');
    f.print(rec->session);
    f.print(',');
    f.print(rec->seq);
    f.print(',');
    print_hex2(f, rec->family);
    f.print(',');
    print_hex2(f, rec->type);
    f.print(',');
    f.print(rec->len);
    f.print(',');
    for (uint8_t i = 0; i < frame->payload_len; i++) {
        print_hex2(f, frame->payload[i]);
    }
    f.println();
    f.close();
}
#endif

void hub_store_record(const arc_frame_t* f, uint32_t now_ms,
                      int8_t in_link, int8_t out_link, int route_result) {
    if (!f) return;

    hub_frame_rec_t* rec = &g_ring[g_ring_head];
    rec->t_ms = now_ms;
    rec->in_link = in_link;
    rec->out_link = out_link;
    rec->route_result = (int16_t)route_result;
    rec->src = f->src;
    rec->dst = f->dst;
    rec->flags = f->flags;
    rec->session = f->session;
    rec->family = f->family;
    rec->type = f->type;
    rec->len = f->payload_len;
    rec->seq = f->seq;

    g_ring_head = (uint16_t)((g_ring_head + 1) % HUB_STORE_RING);
    if (g_ring_count < HUB_STORE_RING) g_ring_count++;

    g_total_frames++;
    roll_window(now_ms);
    g_window_count++;

    if (f->src != ARC_ADDR_TEENSY_HUB) {
        note_peer(f->src, now_ms);
    }

#ifdef HUB_SD_LOG
    sd_log(rec, f);
#endif
}

void hub_store_note_error(void) { g_errors++; }

uint32_t hub_store_total_frames(void) { return g_total_frames; }
uint32_t hub_store_error_count(void) { return g_errors; }

uint16_t hub_store_frames_per_sec(uint32_t now_ms) {
    roll_window(now_ms);
    return g_last_rate;
}

uint8_t hub_store_online_count(uint32_t now_ms, uint32_t timeout_ms) {
    uint8_t n = 0;
    for (int i = 0; i < HUB_PEER_MAX; i++) {
        if (g_peers[i].in_use && (now_ms - g_peers[i].last_ms) < timeout_ms) n++;
    }
    return n;
}

bool hub_store_peer_online(uint8_t addr, uint32_t now_ms, uint32_t timeout_ms) {
    for (int i = 0; i < HUB_PEER_MAX; i++) {
        if (g_peers[i].in_use && g_peers[i].addr == addr) {
            return (now_ms - g_peers[i].last_ms) < timeout_ms;
        }
    }
    return false;
}

void hub_store_set_radio_vitals(int8_t rssi_dbm, int8_t snr_db) {
    g_have_radio = true;
    g_radio_rssi = rssi_dbm;
    g_radio_snr = snr_db;
}
void hub_store_set_power_vitals(uint16_t bus_mv) {
    g_have_power = true;
    g_power_bus_mv = bus_mv;
}
bool     hub_store_have_radio_vitals(void) { return g_have_radio; }
int8_t   hub_store_radio_rssi(void) { return g_radio_rssi; }
int8_t   hub_store_radio_snr(void) { return g_radio_snr; }
bool     hub_store_have_power_vitals(void) { return g_have_power; }
uint16_t hub_store_power_bus_mv(void) { return g_power_bus_mv; }

const hub_frame_rec_t* hub_store_last(void) {
    if (g_ring_count == 0) return NULL;
    uint16_t idx = (uint16_t)((g_ring_head + HUB_STORE_RING - 1) % HUB_STORE_RING);
    return &g_ring[idx];
}
