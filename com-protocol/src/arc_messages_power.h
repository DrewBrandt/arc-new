// arc_messages_power.h
// Payload helpers for the POWER family (FAMILY = 0x05).
//
// The ARCH-Mega power boards in the system are addressed as
// ARC_ADDR_ARCH_MEGA_N (nosecone), ARC_ADDR_ARCH_MEGA_C (center section),
// and ARC_ADDR_ARCH_MEGA_L (lower bay).
// Each board controls 6 channels (5V/3A each, 50W board total). The
// protocol supports up to 8 channels per board: SET_OUTPUT_MASK uses
// 1-byte enable/state masks, and STATUS_REPORT carries an explicit
// channel count so unused indices don't have to be reported.

#ifndef ARC_MESSAGES_POWER_H
#define ARC_MESSAGES_POWER_H

#include "arc_protocol.h"

#ifdef __cplusplus
extern "C" {
#endif

// ----------------------------------------------------------------------
// POWER message types.
// ----------------------------------------------------------------------
#define ARC_POWER_SET_OUTPUT       0x01  // arc_power_set_output_t
#define ARC_POWER_SET_OUTPUT_MASK  0x02  // arc_power_set_output_mask_t
#define ARC_POWER_GET_STATUS       0x03  // empty payload
#define ARC_POWER_STATUS_REPORT    0x10  // arc_power_status_report_t

// ----------------------------------------------------------------------
// Channel-state values used by SET_OUTPUT and reported in STATUS_REPORT.
// ----------------------------------------------------------------------
#define ARC_POWER_OFF   0x00
#define ARC_POWER_ON    0x01
// 0x02-0xFF reserved.

// Status-report channel state OR'd with these bits when the channel is
// in a fault state. Encoder rejects fault bits in SET_OUTPUT.
#define ARC_POWER_CHAN_FAULT_OVERCURRENT 0x40
#define ARC_POWER_CHAN_FAULT_THERMAL     0x80
#define ARC_POWER_CHAN_FAULT_MASK        (ARC_POWER_CHAN_FAULT_OVERCURRENT | \
                                          ARC_POWER_CHAN_FAULT_THERMAL)

// ----------------------------------------------------------------------
// Channel sizing.
// ----------------------------------------------------------------------
#ifndef ARC_POWER_MAX_CHANNELS
#define ARC_POWER_MAX_CHANNELS 8
#endif

// ----------------------------------------------------------------------
// SET_OUTPUT: drive a single channel. The state byte must be ARC_POWER_OFF
// or ARC_POWER_ON; encoder rejects fault/reserved bits.
// ----------------------------------------------------------------------
#define ARC_POWER_SET_OUTPUT_PAYLOAD_SIZE 2

typedef struct {
    uint8_t channel;
    uint8_t state;
} arc_power_set_output_t;

int arc_power_set_output_encode(const arc_power_set_output_t* msg,
                                uint8_t* out, size_t out_capacity);
arc_result_t arc_power_set_output_decode(const uint8_t* in, size_t len,
                                         arc_power_set_output_t* msg);

// ----------------------------------------------------------------------
// SET_OUTPUT_MASK: atomically reprogram multiple channels.
// ``enable_mask`` selects which channels this command touches (bit N
// touches channel N). ``state_mask`` says what state to drive them to
// (bit N: 0 = OFF, 1 = ON). State bits outside ``enable_mask`` are
// ignored.
// ----------------------------------------------------------------------
#define ARC_POWER_SET_OUTPUT_MASK_PAYLOAD_SIZE 2

typedef struct {
    uint8_t enable_mask;
    uint8_t state_mask;
} arc_power_set_output_mask_t;

int arc_power_set_output_mask_encode(const arc_power_set_output_mask_t* msg,
                                     uint8_t* out, size_t out_capacity);
arc_result_t arc_power_set_output_mask_decode(const uint8_t* in, size_t len,
                                              arc_power_set_output_mask_t* msg);

// ----------------------------------------------------------------------
// STATUS_REPORT: variable-length, sized for ARC_POWER_MAX_CHANNELS.
//
// Wire layout:
//   [channel_count u8]
//   [(state u8, current_ma u16 BE)] * channel_count
//   [bus_voltage_mv u16 BE]
//   [temp_c i8]
//
// channel_count of 6 (the ARCH-Mega's real channel count) gives:
//   1 + 6*3 + 2 + 1 = 22 bytes payload.
// ----------------------------------------------------------------------
typedef struct {
    uint8_t  state;       // ARC_POWER_OFF/ON (with optional fault bits in status)
    uint16_t current_ma;
} arc_power_channel_status_t;

typedef struct {
    uint8_t  channel_count;
    arc_power_channel_status_t channels[ARC_POWER_MAX_CHANNELS];
    uint16_t bus_voltage_mv;
    int8_t   temp_c;
} arc_power_status_report_t;

#define ARC_POWER_STATUS_REPORT_SIZE(channel_count) \
    ((size_t)1 + (size_t)(channel_count) * 3 + (size_t)3)

int arc_power_status_report_encode(const arc_power_status_report_t* msg,
                                   uint8_t* out, size_t out_capacity);
arc_result_t arc_power_status_report_decode(const uint8_t* in, size_t len,
                                            arc_power_status_report_t* msg);

#ifdef __cplusplus
}
#endif

#endif  // ARC_MESSAGES_POWER_H
