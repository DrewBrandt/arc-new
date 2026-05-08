// gen_vectors.c
// Generate test vectors for cross-language verification.
//
// The Python implementation of the same protocol must produce
// byte-identical output for these inputs. Run this binary, capture
// the output, and use it as expected values in Python tests.

#include "../src/arc_protocol.h"

#include <stdio.h>
#include <string.h>

static void print_hex(const char* label, const uint8_t* data, size_t len)
{
    printf("  %-20s ", label);
    for (size_t i = 0; i < len; i++) printf("%02x", data[i]);
    printf("\n");
}

static void emit_vector(const char* name,
                        uint8_t src, uint8_t dst,
                        uint8_t flags, uint8_t session, uint16_t seq,
                        uint8_t family, uint8_t type,
                        const uint8_t* payload, size_t payload_len)
{
    printf("vector: %s\n", name);
    printf("  src=0x%02x dst=0x%02x flags=0x%02x session=%u seq=%u family=0x%02x type=0x%02x\n",
           src, dst, flags, session, seq, family, type);
    print_hex("payload", payload, payload_len);

    uint8_t frame[ARC_MAX_FRAME_SIZE];
    int n = arc_frame_build(frame, sizeof(frame),
                            src, dst, flags, session, seq, family, type,
                            payload, payload_len);
    if (n < 0) { printf("  build failed: %d\n\n", n); return; }
    print_hex("frame", frame, n);

    uint8_t encoded[ARC_MAX_ENCODED_SIZE];
    int m = arc_cobs_encode(frame, n, encoded, sizeof(encoded));
    if (m < 0) { printf("  encode failed: %d\n\n", m); return; }
    print_hex("encoded", encoded, m);

    printf("\n");
}

int main(void)
{
    printf("ARC protocol test vectors\n");
    printf("=========================\n\n");

    // Empty payload, all-zero header fields.
    emit_vector("empty_zero", 0, 0, 0, 0, 0, 0, 0, NULL, 0);

    // Heartbeat.
    emit_vector("heartbeat",
                ARC_ADDR_FC_C, ARC_ADDR_FC_N,
                0, 1, 42,
                ARC_FAMILY_NETMGMT, ARC_NETMGMT_HEARTBEAT,
                NULL, 0);

    // Reliable command with small payload.
    {
        uint8_t pl[] = {0x10, 0x20, 0x30};
        emit_vector("small_command",
                    ARC_ADDR_GROUND, ARC_ADDR_FC_L,
                    ARC_FLAG_RELIABLE, 5, 0x1234,
                    ARC_FAMILY_FC_COORD, 0x42,
                    pl, sizeof(pl));
    }

    // Payload with embedded zeros (exercises COBS).
    {
        uint8_t pl[] = {0x00, 0xFF, 0x00, 0xAA, 0x00};
        emit_vector("payload_with_zeros",
                    ARC_ADDR_FC_L, ARC_ADDR_GROUND,
                    0, 7, 100,
                    ARC_FAMILY_FC_COORD, 0x01,
                    pl, sizeof(pl));
    }

    // Max-size payload.
    {
        uint8_t pl[ARC_MAX_PAYLOAD_SIZE];
        for (int i = 0; i < ARC_MAX_PAYLOAD_SIZE; i++) pl[i] = (uint8_t)(i & 0xFF);
        emit_vector("max_payload",
                    ARC_ADDR_FC_N, ARC_ADDR_GROUND,
                    ARC_FLAG_RELIABLE | ARC_FLAG_URGENT,
                    255, 0xFFFF,
                    ARC_FAMILY_FC_COORD, 0xFF,
                    pl, sizeof(pl));
    }

    return 0;
}
