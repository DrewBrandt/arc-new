// arc_messages_netmgmt.h
// Payload helpers for the NETMGMT family (FAMILY = 0x00).
//
// The type constants live in arc_protocol.h (ARC_NETMGMT_*); this header
// adds the small payload struct + encode/decode pair for ACK so FC
// firmware doesn't have to hand-pack the seq number. HEARTBEAT and
// SESSION_RESET have empty payloads, so they need no helpers here.
//
// Mirrors control-plane/arc/messages.py (Ack class). Keep both sides in
// sync; the cross-language test vectors in examples/gen_vectors.c are
// the authority.

#ifndef ARC_MESSAGES_NETMGMT_H
#define ARC_MESSAGES_NETMGMT_H

#include "arc_protocol.h"

#ifdef __cplusplus
extern "C" {
#endif

// ----------------------------------------------------------------------
// ACK payload: 2 bytes, big-endian, the SEQ being acknowledged.
// ----------------------------------------------------------------------
#define ARC_NETMGMT_ACK_PAYLOAD_SIZE 2

typedef struct {
    uint16_t seq;
} arc_netmgmt_ack_t;

// ----------------------------------------------------------------------
// Encode an ACK payload into out. Returns bytes written (always 2) on
// success or a negative arc_result_t on error.
// ----------------------------------------------------------------------
int arc_netmgmt_ack_encode(const arc_netmgmt_ack_t* msg,
                           uint8_t* out, size_t out_capacity);

// ----------------------------------------------------------------------
// Decode an ACK payload. Returns ARC_OK or a negative arc_result_t.
// ----------------------------------------------------------------------
arc_result_t arc_netmgmt_ack_decode(const uint8_t* in, size_t len,
                                    arc_netmgmt_ack_t* msg);

#ifdef __cplusplus
}
#endif

#endif  // ARC_MESSAGES_NETMGMT_H
