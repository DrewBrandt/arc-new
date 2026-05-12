// arc_messages_fc_video.h
// Payload helpers for the FC_VIDEO family (FAMILY = 0x03).
//
// Direction: FC -> Controller for the command types (SET_LAYOUT,
// SET_SOURCE, SET_OVERLAY, GET_STATUS); Controller -> FC for
// STATUS_REPORT, which is the reply to GET_STATUS.
//
// Mirrors control-plane/arc/messages.py byte-for-byte.

#ifndef ARC_MESSAGES_FC_VIDEO_H
#define ARC_MESSAGES_FC_VIDEO_H

#include "arc_protocol.h"

#ifdef __cplusplus
extern "C" {
#endif

// ----------------------------------------------------------------------
// FC_VIDEO message types.
// ----------------------------------------------------------------------
#define ARC_FC_VIDEO_SET_LAYOUT     0x01  // arc_fc_video_set_layout_t
#define ARC_FC_VIDEO_SET_SOURCE     0x02  // arc_fc_video_set_source_t
#define ARC_FC_VIDEO_SET_OVERLAY    0x03  // null-terminated UTF-8 string
#define ARC_FC_VIDEO_GET_STATUS     0x04  // empty payload
#define ARC_FC_VIDEO_STATUS_REPORT  0x10  // arc_fc_video_status_report_t

// ----------------------------------------------------------------------
// SET_LAYOUT: 1-byte layout id. The id maps to a named layout in the
// Controller's config by insertion order.
// ----------------------------------------------------------------------
#define ARC_FC_VIDEO_SET_LAYOUT_PAYLOAD_SIZE 1

typedef struct {
    uint8_t layout_id;
} arc_fc_video_set_layout_t;

int arc_fc_video_set_layout_encode(const arc_fc_video_set_layout_t* msg,
                                   uint8_t* out, size_t out_capacity);
arc_result_t arc_fc_video_set_layout_decode(const uint8_t* in, size_t len,
                                            arc_fc_video_set_layout_t* msg);

// ----------------------------------------------------------------------
// SET_SOURCE: pick a source (Sender address, ARC_ADDR_CONTROLLER for the
// local camera, or ARC_ADDR_UNASSIGNED for an empty slot) for one
// compositor slot.
// ----------------------------------------------------------------------
#define ARC_FC_VIDEO_SET_SOURCE_PAYLOAD_SIZE 2

typedef struct {
    uint8_t slot_id;
    uint8_t sender_addr;
} arc_fc_video_set_source_t;

int arc_fc_video_set_source_encode(const arc_fc_video_set_source_t* msg,
                                   uint8_t* out, size_t out_capacity);
arc_result_t arc_fc_video_set_source_decode(const uint8_t* in, size_t len,
                                            arc_fc_video_set_source_t* msg);

// ----------------------------------------------------------------------
// SET_OVERLAY: null-terminated UTF-8 string for the textoverlay element.
// Length-bounded by the maximum payload size (241 bytes including the
// terminator). The Controller ignores the trailing NUL.
// ----------------------------------------------------------------------

// Encode a UTF-8 string + trailing NUL into out. text need not be NUL-
// terminated; the encoder appends one. Returns total bytes written
// (text_len + 1) or a negative arc_result_t.
int arc_fc_video_set_overlay_encode(const char* text, size_t text_len,
                                    uint8_t* out, size_t out_capacity);

// Decode the overlay payload into the caller's buffer. The output is
// always NUL-terminated; out_capacity must be at least len bytes (the
// payload already includes its own NUL). Returns the string length
// (excluding the NUL) on success or a negative arc_result_t.
int arc_fc_video_set_overlay_decode(const uint8_t* in, size_t len,
                                    char* out, size_t out_capacity);

// ----------------------------------------------------------------------
// STATUS_REPORT: Controller's reply to GET_STATUS. Variable-length
// summary describing what the Controller is currently doing.
//
// Wire layout:
//   [slot_count u8]
//   [slot_source u8] * slot_count       -- active source per slot
//   [sender_count u8]
//   [(sender_addr u8, flags u8)] * sender_count
//
// flags bits:
//   bit 0 (0x01) = sender online (control-plane heartbeat seen recently)
//   bit 1 (0x02) = sender currently transmitting (Controller's view)
//   bit 2 (0x04) = recording started on this Sender
//   higher bits  = reserved, must be zero
//
// Compile-time caps keep the helper allocation-free. Override via
// -D for memory-constrained targets.
// ----------------------------------------------------------------------
#ifndef ARC_FC_VIDEO_STATUS_MAX_SLOTS
#define ARC_FC_VIDEO_STATUS_MAX_SLOTS 4
#endif

#ifndef ARC_FC_VIDEO_STATUS_MAX_SENDERS
#define ARC_FC_VIDEO_STATUS_MAX_SENDERS 8
#endif

#define ARC_FC_VIDEO_STATUS_FLAG_ONLINE       0x01
#define ARC_FC_VIDEO_STATUS_FLAG_TRANSMITTING 0x02
#define ARC_FC_VIDEO_STATUS_FLAG_RECORDING    0x04

typedef struct {
    uint8_t addr;
    uint8_t flags;
} arc_fc_video_sender_status_t;

typedef struct {
    uint8_t  slot_count;
    uint8_t  slots[ARC_FC_VIDEO_STATUS_MAX_SLOTS];
    uint8_t  sender_count;
    arc_fc_video_sender_status_t senders[ARC_FC_VIDEO_STATUS_MAX_SENDERS];
} arc_fc_video_status_report_t;

// Encoded length for a given slot/sender count: 1 + slot_count + 1 + 2*sender_count.
#define ARC_FC_VIDEO_STATUS_REPORT_SIZE(slot_count, sender_count) \
    ((size_t)2 + (size_t)(slot_count) + (size_t)(sender_count) * 2)

int arc_fc_video_status_report_encode(const arc_fc_video_status_report_t* msg,
                                      uint8_t* out, size_t out_capacity);
arc_result_t arc_fc_video_status_report_decode(const uint8_t* in, size_t len,
                                               arc_fc_video_status_report_t* msg);

#ifdef __cplusplus
}
#endif

#endif  // ARC_MESSAGES_FC_VIDEO_H
