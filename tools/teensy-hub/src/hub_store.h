// hub_store.h
// "Store everything passing through": a small black box for the hub.
//
// Every frame the hub sees (forwarded or locally delivered) is recorded:
//   - a ring buffer of recent frame headers (for the USB console / debugging),
//   - per-family and total counters plus an error tally,
//   - a last-seen timestamp per source address (peer liveness),
//   - a vitals snapshot (last radio RSSI/SNR, last power bus voltage) that the
//     OLED reads for its quick-look pages.
//
// Optional SD black-box logging is compiled in with -DHUB_SD_LOG.

#ifndef HUB_STORE_H
#define HUB_STORE_H

#include <stdint.h>
#include <stdbool.h>
#include "arc_protocol.h"

#ifdef __cplusplus
extern "C" {
#endif

#ifndef HUB_STORE_RING
#define HUB_STORE_RING 32
#endif

#ifndef HUB_PEER_MAX
#define HUB_PEER_MAX 16
#endif

typedef struct {
    uint32_t t_ms;
    uint8_t  src;
    uint8_t  dst;
    uint8_t  family;
    uint8_t  type;
    uint8_t  len;
    uint16_t seq;
} hub_frame_rec_t;

void hub_store_init(uint32_t now_ms);

// Record one parsed frame seen on any link. Updates ring, counters, the
// per-source last-seen table, and (if HUB_SD_LOG) appends to the SD log.
void hub_store_record(const arc_frame_t* f, uint32_t now_ms);

// Note a decode/parse failure on a link.
void hub_store_note_error(void);

// Counters / throughput.
uint32_t hub_store_total_frames(void);
uint32_t hub_store_error_count(void);
uint16_t hub_store_frames_per_sec(uint32_t now_ms);  // last completed 1 s window

// Peer liveness.
uint8_t hub_store_online_count(uint32_t now_ms, uint32_t timeout_ms);
bool    hub_store_peer_online(uint8_t addr, uint32_t now_ms, uint32_t timeout_ms);

// Vitals snapshot (set by the main deliver path when status reports arrive).
void    hub_store_set_radio_vitals(int8_t rssi_dbm, int8_t snr_db);
void    hub_store_set_power_vitals(uint16_t bus_mv);
bool    hub_store_have_radio_vitals(void);
int8_t  hub_store_radio_rssi(void);
int8_t  hub_store_radio_snr(void);
bool    hub_store_have_power_vitals(void);
uint16_t hub_store_power_bus_mv(void);

// Most-recent ring entry, for console dumps. Returns NULL if empty.
const hub_frame_rec_t* hub_store_last(void);

#ifdef __cplusplus
}
#endif

#endif  // HUB_STORE_H
