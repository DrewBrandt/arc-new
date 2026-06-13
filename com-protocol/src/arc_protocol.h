// arc_protocol.h
// Framing and routing protocol for the ARC video/telemetry network.
//
// Designed to be portable across Arduino/AVR, ARM Cortex (Teensy, etc.),
// and desktop Linux. No dynamic allocation, no exceptions, no STL.
// C-compatible API so it can be wrapped from Python via cffi/ctypes.

#ifndef ARC_PROTOCOL_H
#define ARC_PROTOCOL_H

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

// ----------------------------------------------------------------------
// Protocol version. Bump when the wire format changes incompatibly.
// ----------------------------------------------------------------------
#define ARC_PROTOCOL_VERSION 1

// ----------------------------------------------------------------------
// Sizing constants.
//
// Frame layout on the wire (before COBS encoding for serial links):
//   [LEN][SRC][DST][FLAGS][SESSION][SEQ_HI][SEQ_LO][FAMILY][TYPE]
//   [PAYLOAD...][CRC_HI][CRC_LO]
//
// LEN counts everything that follows it, up to and including the CRC.
// ----------------------------------------------------------------------
#define ARC_HEADER_SIZE     9   // LEN through TYPE inclusive
#define ARC_CRC_SIZE        2
#define ARC_OVERHEAD        (ARC_HEADER_SIZE + ARC_CRC_SIZE)  // 11

// Radio link is 255 bytes. With COBS framing we need 1 pointer byte and
// 1 delimiter byte minimum. We cap encoded frames at 254 to keep COBS
// overhead constant at 2 bytes, giving a clean payload budget.
#define ARC_MAX_ENCODED_SIZE    254
#define ARC_COBS_OVERHEAD       2
#define ARC_MAX_FRAME_SIZE      (ARC_MAX_ENCODED_SIZE - ARC_COBS_OVERHEAD)  // 252
#define ARC_MAX_PAYLOAD_SIZE    (ARC_MAX_FRAME_SIZE - ARC_OVERHEAD)         // 241

// ----------------------------------------------------------------------
// Address space. Documented in the protocol spec; defined here so all
// implementations agree.
// ----------------------------------------------------------------------
#define ARC_ADDR_UNASSIGNED     0x00
#define ARC_ADDR_GROUND         0x01  // ground station (reached over RF via RADIO_CMD)
#define ARC_ADDR_FC_N           0x02
#define ARC_ADDR_FC_C           0x03
#define ARC_ADDR_FC_L           0x04
#define ARC_ADDR_TEENSY_HUB     0x05  // nosecone central router (Teensy 4.1)
#define ARC_ADDR_CONTROLLER     0x10  // Pi 5 "pi-5-nose": video aggregator + WiFi gateway
#define ARC_ADDR_SENDER_DOWN    0x11  // nose Pi 0, camera pointing down the rocket
#define ARC_ADDR_SENDER_AIRBRAKE 0x12 // airbrake-bay Pi 0
#define ARC_ADDR_SENDER_PAYLOAD 0x13  // payload-bay Pi 0
// 0x14 retired (was SENDER_L2)
#define ARC_ADDR_SENDER_GROUND  0x15  // ground-side Pi 0
// Radios (0x20-0x2F reserved for radio-class nodes); both hang off the Teensy hub.
#define ARC_ADDR_RADIO_CMD      0x20  // rocket command/status radio (on Teensy)
#define ARC_ADDR_RADIO_G        0x21  // ground radio (OTA peer of RADIO_CMD)
#define ARC_ADDR_RADIO_DATA     0x22  // live data downlink; proprietary, Teensy transcodes
// Power boards (0x30-0x3F reserved for power-class nodes).
#define ARC_ADDR_ARCH_MEGA_N    0x30  // nosecone ARCH-Mega
#define ARC_ADDR_ARCH_MEGA_L    0x31  // lower-bay ARCH-Mega
#define ARC_ADDR_ARCH_MEGA_C    0x32  // center-section ARCH-Mega
#define ARC_ADDR_BROADCAST      0xFF

// ----------------------------------------------------------------------
// Flag bits.
// ----------------------------------------------------------------------
#define ARC_FLAG_RELIABLE   0x01  // sender expects ack, will retry on timeout
#define ARC_FLAG_URGENT     0x02  // prioritize over queued traffic
#define ARC_FLAG_ACK        0x04  // this frame is itself an ack
// 0x08 - 0x80 reserved

// ----------------------------------------------------------------------
// Protocol families.
// ----------------------------------------------------------------------
#define ARC_FAMILY_NETMGMT  0x00  // heartbeat, link diagnostics
#define ARC_FAMILY_FC_COORD 0x01  // FC-to-FC: telemetry, commands, config
#define ARC_FAMILY_VIDEO    0x02  // controller-to-sender: stream control, status
#define ARC_FAMILY_FC_VIDEO 0x03  // FC-to-controller: layout/source commands
#define ARC_FAMILY_RADIO    0x04  // anyone-to-radio: freq/power control, RSSI status
#define ARC_FAMILY_POWER    0x05  // anyone-to-ARCH-Mega: output switching, current/voltage status

// ----------------------------------------------------------------------
// Net-management message types (FAMILY = 0x00).
// ----------------------------------------------------------------------
#define ARC_NETMGMT_HEARTBEAT       0x01
#define ARC_NETMGMT_ACK             0x02
#define ARC_NETMGMT_SESSION_RESET   0x03

// ----------------------------------------------------------------------
// Result codes.
// ----------------------------------------------------------------------
typedef enum {
    ARC_OK              = 0,
    ARC_ERR_BUFFER      = -1,  // output buffer too small
    ARC_ERR_TOO_SHORT   = -2,  // input doesn't contain a complete frame
    ARC_ERR_TOO_LONG    = -3,  // input exceeds maximum frame size
    ARC_ERR_BAD_LENGTH  = -4,  // LEN field disagrees with actual length
    ARC_ERR_BAD_CRC     = -5,
    ARC_ERR_BAD_COBS    = -6,  // malformed COBS encoding
    ARC_ERR_BAD_VERSION = -7,
    ARC_ERR_BAD_ARG     = -8,  // invalid pointer/argument combination
} arc_result_t;

// ----------------------------------------------------------------------
// Decoded frame structure. PAYLOAD points into a caller-provided buffer.
// ----------------------------------------------------------------------
typedef struct {
    uint8_t  src;
    uint8_t  dst;
    uint8_t  flags;
    uint8_t  session;
    uint16_t seq;
    uint8_t  family;
    uint8_t  type;
    const uint8_t* payload;
    uint8_t  payload_len;
} arc_frame_t;

// ----------------------------------------------------------------------
// CRC-16/CCITT (poly 0x1021, init 0xFFFF). Standard, well-tested.
// ----------------------------------------------------------------------
uint16_t arc_crc16(const uint8_t* data, size_t len);

// ----------------------------------------------------------------------
// COBS encode/decode.
//
// arc_cobs_encode: encodes `in` (len bytes) into `out`, which must be at
// least `len + 2` bytes (worst case: 1 pointer + delimiter). Returns the
// number of bytes written (including the trailing 0x00 delimiter).
//
// arc_cobs_decode: decodes `in` (len bytes including delimiter) into
// `out`, which must be at least `len - 2` bytes. Returns the number of
// decoded bytes, or a negative arc_result_t on error.
// ----------------------------------------------------------------------
int arc_cobs_encode(const uint8_t* in, size_t len, uint8_t* out, size_t out_capacity);
int arc_cobs_decode(const uint8_t* in, size_t len, uint8_t* out, size_t out_capacity);

// ----------------------------------------------------------------------
// Build a frame in the caller's buffer.
//
// Writes the unencoded frame (LEN through CRC) into `out`. Does NOT
// apply COBS encoding -- callers wanting to send over a serial link
// should call arc_cobs_encode on the result.
//
// Returns frame size on success, negative arc_result_t on error.
// ----------------------------------------------------------------------
int arc_frame_build(uint8_t* out, size_t out_capacity,
                    uint8_t src, uint8_t dst,
                    uint8_t flags, uint8_t session, uint16_t seq,
                    uint8_t family, uint8_t type,
                    const uint8_t* payload, size_t payload_len);

// ----------------------------------------------------------------------
// Parse an unencoded frame (LEN through CRC). Validates length and CRC.
// On success, populates *frame with fields and a pointer into the input
// buffer for the payload. The input buffer must remain valid for as
// long as the caller uses frame->payload.
// ----------------------------------------------------------------------
arc_result_t arc_frame_parse(const uint8_t* in, size_t len, arc_frame_t* frame);

// ----------------------------------------------------------------------
// Convenience: build an ACK frame for a given received frame.
// ACKs are sent with FAMILY=NETMGMT, TYPE=ACK, payload = the SEQ being
// acked (2 bytes, big-endian), with SRC/DST swapped from the original.
// ----------------------------------------------------------------------
int arc_frame_build_ack(uint8_t* out, size_t out_capacity,
                        const arc_frame_t* original,
                        uint8_t my_session, uint16_t my_seq);

#ifdef __cplusplus
}
#endif

#endif // ARC_PROTOCOL_H
