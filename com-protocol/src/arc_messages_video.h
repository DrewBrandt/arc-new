// arc_messages_video.h
// Payload helpers for the VIDEO family (FAMILY = 0x02).
//
// Direction: Controller -> Sender for command types (START/STOP/HARD_STOP,
// SET_BITRATE), Sender -> Controller for STATUS_REPORT.
//
// Mirrors control-plane/arc/messages.py byte-for-byte. The Python
// suite cross-checks against the C wire format via shared test vectors.

#ifndef ARC_MESSAGES_VIDEO_H
#define ARC_MESSAGES_VIDEO_H

#include "arc_protocol.h"

#ifdef __cplusplus
extern "C" {
#endif

// ----------------------------------------------------------------------
// VIDEO message types.
// ----------------------------------------------------------------------
#define ARC_VIDEO_START_STREAM   0x01  // empty payload
#define ARC_VIDEO_STOP_STREAM    0x02  // empty payload (soft stop)
#define ARC_VIDEO_HARD_STOP      0x03  // empty payload (stop tx + recording)
#define ARC_VIDEO_SET_BITRATE    0x04  // arc_video_set_bitrate_t
#define ARC_VIDEO_STATUS_REPORT  0x10  // arc_video_status_report_t

// ----------------------------------------------------------------------
// SET_BITRATE: 4-byte big-endian unsigned bitrate in bits per second.
// ----------------------------------------------------------------------
#define ARC_VIDEO_SET_BITRATE_PAYLOAD_SIZE 4

typedef struct {
    uint32_t bitrate_bps;
} arc_video_set_bitrate_t;

int arc_video_set_bitrate_encode(const arc_video_set_bitrate_t* msg,
                                 uint8_t* out, size_t out_capacity);
arc_result_t arc_video_set_bitrate_decode(const uint8_t* in, size_t len,
                                          arc_video_set_bitrate_t* msg);

// ----------------------------------------------------------------------
// STATUS_REPORT: 10-byte fixed-size sender health summary, emitted
// roughly once per second from each Sender. Field order on the wire:
//
//   [state u8] [cpu_temp_c u8] [cpu_load_pct u8]
//   [free_disk_mb u16 BE] [rssi_dbm i8]
//   [tx_frames u16 BE] [dropped_frames u16 BE]
//
// ``state`` packs flags: bit 0 = recording, bit 1 = transmitting,
// remaining bits reserved for sender-defined error indicators. The
// Sender side owns the exact bit assignments; the Controller treats the
// byte as opaque except for surfacing it in its own status snapshot.
// ----------------------------------------------------------------------
#define ARC_VIDEO_STATUS_REPORT_PAYLOAD_SIZE 10

typedef struct {
    uint8_t  state;
    uint8_t  cpu_temp_c;
    uint8_t  cpu_load_pct;
    uint16_t free_disk_mb;
    int8_t   rssi_dbm;
    uint16_t tx_frames;
    uint16_t dropped_frames;
} arc_video_status_report_t;

int arc_video_status_report_encode(const arc_video_status_report_t* msg,
                                   uint8_t* out, size_t out_capacity);
arc_result_t arc_video_status_report_decode(const uint8_t* in, size_t len,
                                            arc_video_status_report_t* msg);

#ifdef __cplusplus
}
#endif

#endif  // ARC_MESSAGES_VIDEO_H
