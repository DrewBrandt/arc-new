// arc_protocol.c
// Implementation of the ARC framing protocol.

#include "arc_protocol.h"

#include <string.h>

// ----------------------------------------------------------------------
// CRC-16/CCITT (XMODEM variant: poly 0x1021, init 0xFFFF, no final XOR).
//
// Bit-by-bit implementation -- small code footprint, no table. Fast
// enough for our frame sizes on every target CPU we care about.
// ----------------------------------------------------------------------
uint16_t arc_crc16(const uint8_t* data, size_t len)
{
    uint16_t crc = 0xFFFF;
    for (size_t i = 0; i < len; i++) {
        crc ^= ((uint16_t)data[i]) << 8;
        for (int b = 0; b < 8; b++) {
            if (crc & 0x8000) {
                crc = (crc << 1) ^ 0x1021;
            } else {
                crc <<= 1;
            }
        }
    }
    return crc;
}

// ----------------------------------------------------------------------
// COBS encode.
//
// Algorithm: walk the input, replacing every 0x00 with the distance
// (in bytes) to the next 0x00 or the end. Reserve the first output byte
// for the distance to the first 0x00 (or to the end if none). Write a
// 0x00 delimiter at the end of the output.
//
// Implementation here uses a "code position" trick: we leave a hole at
// the start of each run, fill it once we know the run length, and
// open a new hole. Simple and branchless on the hot path.
// ----------------------------------------------------------------------
int arc_cobs_encode(const uint8_t* in, size_t len, uint8_t* out, size_t out_capacity)
{
    if (out == NULL || (in == NULL && len > 0)) return ARC_ERR_BAD_ARG;
    if (out_capacity < len + 2) {
        return ARC_ERR_BUFFER;
    }

    size_t code_idx = 0;   // index of the byte we'll write the next code into
    size_t out_idx  = 1;   // we've reserved out[0] for the first code
    uint8_t code    = 1;   // running distance to next 0x00 (or end)

    for (size_t i = 0; i < len; i++) {
        if (in[i] == 0x00) {
            out[code_idx] = code;
            code_idx = out_idx++;
            code = 1;
        } else {
            out[out_idx++] = in[i];
            code++;
            if (code == 0xFF) {
                // Max run length; close it out and start a new one.
                out[code_idx] = code;
                code_idx = out_idx++;
                code = 1;
            }
        }
    }

    out[code_idx] = code;
    out[out_idx++] = 0x00;  // delimiter
    return (int)out_idx;
}

// ----------------------------------------------------------------------
// COBS decode.
//
// Walk the input, reading a code byte that tells us how far to the next
// implicit zero. Copy bytes between codes literally, then emit a 0x00
// (unless the code was 0xFF, indicating a max-length run with no
// terminating zero in the original).
//
// Input must end with a 0x00 delimiter byte.
// ----------------------------------------------------------------------
int arc_cobs_decode(const uint8_t* in, size_t len, uint8_t* out, size_t out_capacity)
{
    if (in == NULL || out == NULL) return ARC_ERR_BAD_ARG;
    if (len < 2) return ARC_ERR_BAD_COBS;
    if (in[len - 1] != 0x00) return ARC_ERR_BAD_COBS;

    size_t in_idx = 0;
    size_t out_idx = 0;
    // Frame ends at len-1 (the delimiter is not part of the encoded data).
    const size_t encoded_end = len - 1;

    while (in_idx < encoded_end) {
        uint8_t code = in[in_idx++];
        if (code == 0x00) return ARC_ERR_BAD_COBS;  // unexpected zero mid-frame

        // Code byte says: copy (code - 1) literal bytes, then emit a 0x00.
        size_t literal_count = (size_t)code - 1;
        if (in_idx + literal_count > encoded_end) return ARC_ERR_BAD_COBS;
        if (out_idx + literal_count > out_capacity) return ARC_ERR_BUFFER;

        memcpy(&out[out_idx], &in[in_idx], literal_count);
        in_idx += literal_count;
        out_idx += literal_count;

        // Emit the implicit 0x00 unless this was a max-length run or
        // we've reached the end of the encoded data.
        if (code != 0xFF && in_idx < encoded_end) {
            if (out_idx >= out_capacity) return ARC_ERR_BUFFER;
            out[out_idx++] = 0x00;
        }
    }

    return (int)out_idx;
}

// ----------------------------------------------------------------------
// Frame build / parse.
// ----------------------------------------------------------------------

int arc_frame_build(uint8_t* out, size_t out_capacity,
                    uint8_t src, uint8_t dst,
                    uint8_t flags, uint8_t session, uint16_t seq,
                    uint8_t family, uint8_t type,
                    const uint8_t* payload, size_t payload_len)
{
    if (out == NULL || (payload == NULL && payload_len > 0)) return ARC_ERR_BAD_ARG;
    if (payload_len > ARC_MAX_PAYLOAD_SIZE) return ARC_ERR_TOO_LONG;
    size_t total = ARC_OVERHEAD + payload_len;
    if (out_capacity < total) return ARC_ERR_BUFFER;

    // LEN counts everything after itself, up to and including CRC.
    out[0] = (uint8_t)(total - 1);
    out[1] = src;
    out[2] = dst;
    out[3] = flags;
    out[4] = session;
    out[5] = (uint8_t)(seq >> 8);
    out[6] = (uint8_t)(seq & 0xFF);
    out[7] = family;
    out[8] = type;

    if (payload_len > 0 && payload != NULL) {
        memcpy(&out[ARC_HEADER_SIZE], payload, payload_len);
    }

    // CRC covers LEN through end-of-payload.
    uint16_t crc = arc_crc16(out, ARC_HEADER_SIZE + payload_len);
    out[ARC_HEADER_SIZE + payload_len]     = (uint8_t)(crc >> 8);
    out[ARC_HEADER_SIZE + payload_len + 1] = (uint8_t)(crc & 0xFF);

    return (int)total;
}

arc_result_t arc_frame_parse(const uint8_t* in, size_t len, arc_frame_t* frame)
{
    if (in == NULL || frame == NULL) return ARC_ERR_BAD_ARG;
    if (len < ARC_OVERHEAD) return ARC_ERR_TOO_SHORT;
    if (len > ARC_MAX_FRAME_SIZE) return ARC_ERR_TOO_LONG;

    uint8_t declared_len = in[0];
    if ((size_t)declared_len + 1 != len) return ARC_ERR_BAD_LENGTH;

    size_t payload_len = len - ARC_OVERHEAD;
    if (payload_len > ARC_MAX_PAYLOAD_SIZE) return ARC_ERR_TOO_LONG;

    // Verify CRC over LEN through end-of-payload.
    uint16_t expected_crc = arc_crc16(in, ARC_HEADER_SIZE + payload_len);
    uint16_t actual_crc =
        ((uint16_t)in[ARC_HEADER_SIZE + payload_len] << 8) |
         (uint16_t)in[ARC_HEADER_SIZE + payload_len + 1];
    if (expected_crc != actual_crc) return ARC_ERR_BAD_CRC;

    frame->src         = in[1];
    frame->dst         = in[2];
    frame->flags       = in[3];
    frame->session     = in[4];
    frame->seq         = ((uint16_t)in[5] << 8) | (uint16_t)in[6];
    frame->family      = in[7];
    frame->type        = in[8];
    frame->payload     = (payload_len > 0) ? &in[ARC_HEADER_SIZE] : NULL;
    frame->payload_len = (uint8_t)payload_len;

    return ARC_OK;
}

int arc_frame_build_ack(uint8_t* out, size_t out_capacity,
                        const arc_frame_t* original,
                        uint8_t my_session, uint16_t my_seq)
{
    if (original == NULL) return ARC_ERR_BAD_ARG;

    // Ack payload: the SEQ being acked, big-endian.
    uint8_t ack_payload[2];
    ack_payload[0] = (uint8_t)(original->seq >> 8);
    ack_payload[1] = (uint8_t)(original->seq & 0xFF);

    return arc_frame_build(
        out, out_capacity,
        original->dst,                  // src: I'm the original recipient
        original->src,                  // dst: send back to the original sender
        ARC_FLAG_ACK,                   // flags: this is an ack, not reliable
        my_session,
        my_seq,
        ARC_FAMILY_NETMGMT,
        ARC_NETMGMT_ACK,
        ack_payload,
        sizeof(ack_payload));
}
