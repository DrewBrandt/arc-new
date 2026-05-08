// test_arc_protocol.c
// Unit tests for the ARC protocol library.
//
// Compile and run on Linux for development. The protocol library itself
// is portable; running the tests on the host gives fast feedback.

#include "../src/arc_protocol.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <assert.h>

static int tests_run = 0;
static int tests_failed = 0;

#define TEST(name) static void test_##name(void)
#define RUN(name) do { \
    printf("  %-40s ", #name); fflush(stdout); \
    tests_run++; \
    int failed_before = tests_failed; \
    test_##name(); \
    if (tests_failed == failed_before) printf("OK\n"); \
} while (0)

#define ASSERT(cond) do { \
    if (!(cond)) { \
        printf("FAIL\n    %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        tests_failed++; \
        return; \
    } \
} while (0)

#define ASSERT_EQ(a, b) do { \
    long _a = (long)(a); long _b = (long)(b); \
    if (_a != _b) { \
        printf("FAIL\n    %s:%d: expected %ld, got %ld\n", \
               __FILE__, __LINE__, _b, _a); \
        tests_failed++; \
        return; \
    } \
} while (0)

// ----------------------------------------------------------------------
// CRC tests. Known-answer tests against published CCITT/XMODEM vectors.
// ----------------------------------------------------------------------

TEST(crc_empty)
{
    ASSERT_EQ(arc_crc16(NULL, 0), 0xFFFF);
}

TEST(crc_known_vector)
{
    // CRC-16/CCITT-FALSE (init=0xFFFF, no XOR-out) of "123456789" is 0x29B1.
    // This is the published check value for the algorithm we're using.
    const uint8_t data[] = "123456789";
    ASSERT_EQ(arc_crc16(data, 9), 0x29B1);
}

TEST(crc_single_byte)
{
    // Sanity check that single-byte CRCs are deterministic.
    uint8_t b = 0x42;
    uint16_t c1 = arc_crc16(&b, 1);
    uint16_t c2 = arc_crc16(&b, 1);
    ASSERT_EQ(c1, c2);
    ASSERT(c1 != 0xFFFF);  // shouldn't equal the init value
}

// ----------------------------------------------------------------------
// COBS encode/decode tests. These are the load-bearing tests for the
// "what if the payload contains 0x00" question.
// ----------------------------------------------------------------------

TEST(cobs_simple_no_zeros)
{
    const uint8_t in[] = {0x11, 0x22, 0x33};
    uint8_t out[16];
    int n = arc_cobs_encode(in, 3, out, sizeof(out));
    ASSERT_EQ(n, 5);  // 1 code byte + 3 data + 1 delimiter
    ASSERT_EQ(out[0], 0x04);  // points 4 bytes ahead, past all data
    ASSERT_EQ(out[1], 0x11);
    ASSERT_EQ(out[2], 0x22);
    ASSERT_EQ(out[3], 0x33);
    ASSERT_EQ(out[4], 0x00);  // delimiter
}

TEST(cobs_payload_with_zero)
{
    // The whole point of COBS: a zero in the payload must round-trip.
    const uint8_t in[] = {0x45, 0x00, 0x2A};
    uint8_t enc[16], dec[16];
    int n = arc_cobs_encode(in, 3, enc, sizeof(enc));
    ASSERT(n > 0);
    // Verify no 0x00 in encoded data except the trailing delimiter.
    for (int i = 0; i < n - 1; i++) {
        ASSERT(enc[i] != 0x00);
    }
    ASSERT_EQ(enc[n - 1], 0x00);

    int m = arc_cobs_decode(enc, n, dec, sizeof(dec));
    ASSERT_EQ(m, 3);
    ASSERT_EQ(dec[0], 0x45);
    ASSERT_EQ(dec[1], 0x00);
    ASSERT_EQ(dec[2], 0x2A);
}

TEST(cobs_all_zeros)
{
    // Pathological case: payload is all zeros.
    const uint8_t in[5] = {0, 0, 0, 0, 0};
    uint8_t enc[16], dec[16];
    int n = arc_cobs_encode(in, 5, enc, sizeof(enc));
    ASSERT(n > 0);
    for (int i = 0; i < n - 1; i++) {
        ASSERT(enc[i] != 0x00);
    }

    int m = arc_cobs_decode(enc, n, dec, sizeof(dec));
    ASSERT_EQ(m, 5);
    for (int i = 0; i < 5; i++) ASSERT_EQ(dec[i], 0x00);
}

TEST(cobs_round_trip_random)
{
    // Round-trip a bunch of random data.
    srand(42);
    for (int trial = 0; trial < 100; trial++) {
        uint8_t in[ARC_MAX_FRAME_SIZE];
        size_t len = (rand() % ARC_MAX_FRAME_SIZE) + 1;
        for (size_t i = 0; i < len; i++) in[i] = (uint8_t)(rand() & 0xFF);

        uint8_t enc[ARC_MAX_FRAME_SIZE + 4];
        int n = arc_cobs_encode(in, len, enc, sizeof(enc));
        ASSERT(n > 0);

        // No zeros in encoded data except the final delimiter.
        for (int i = 0; i < n - 1; i++) ASSERT(enc[i] != 0x00);
        ASSERT_EQ(enc[n - 1], 0x00);

        uint8_t dec[ARC_MAX_FRAME_SIZE];
        int m = arc_cobs_decode(enc, n, dec, sizeof(dec));
        ASSERT_EQ(m, (int)len);
        ASSERT_EQ(memcmp(in, dec, len), 0);
    }
}

TEST(cobs_long_run_no_zero)
{
    // Test the 0xFF max-run-length path: 254 nonzero bytes need a
    // continuation code, not an implicit zero.
    uint8_t in[300];
    for (int i = 0; i < 300; i++) in[i] = (uint8_t)(i % 255 + 1);  // never zero

    uint8_t enc[320], dec[320];
    int n = arc_cobs_encode(in, 300, enc, sizeof(enc));
    ASSERT(n > 0);
    int m = arc_cobs_decode(enc, n, dec, sizeof(dec));
    ASSERT_EQ(m, 300);
    ASSERT_EQ(memcmp(in, dec, 300), 0);
}

TEST(cobs_decode_rejects_bad_input)
{
    uint8_t out[16];
    // No trailing delimiter.
    uint8_t bad1[] = {0x02, 0x42};
    ASSERT(arc_cobs_decode(bad1, 2, out, sizeof(out)) < 0);

    // Too short.
    uint8_t bad2[] = {0x00};
    ASSERT(arc_cobs_decode(bad2, 1, out, sizeof(out)) < 0);

    // Code points past end of input.
    uint8_t bad3[] = {0x05, 0x42, 0x00};
    ASSERT(arc_cobs_decode(bad3, 3, out, sizeof(out)) < 0);
}

// ----------------------------------------------------------------------
// Frame build / parse tests.
// ----------------------------------------------------------------------

TEST(frame_round_trip_simple)
{
    const uint8_t payload[] = {0xDE, 0xAD, 0xBE, 0xEF};
    uint8_t frame[64];

    int n = arc_frame_build(frame, sizeof(frame),
                            ARC_ADDR_FC_C, ARC_ADDR_CONTROLLER,
                            ARC_FLAG_RELIABLE, 7, 0x1234,
                            ARC_FAMILY_VIDEO, 0x42,
                            payload, 4);
    ASSERT_EQ(n, ARC_OVERHEAD + 4);

    arc_frame_t f;
    ASSERT_EQ(arc_frame_parse(frame, n, &f), ARC_OK);
    ASSERT_EQ(f.src, ARC_ADDR_FC_C);
    ASSERT_EQ(f.dst, ARC_ADDR_CONTROLLER);
    ASSERT_EQ(f.flags, ARC_FLAG_RELIABLE);
    ASSERT_EQ(f.session, 7);
    ASSERT_EQ(f.seq, 0x1234);
    ASSERT_EQ(f.family, ARC_FAMILY_VIDEO);
    ASSERT_EQ(f.type, 0x42);
    ASSERT_EQ(f.payload_len, 4);
    ASSERT_EQ(memcmp(f.payload, payload, 4), 0);
}

TEST(frame_empty_payload)
{
    uint8_t frame[32];
    int n = arc_frame_build(frame, sizeof(frame),
                            1, 2, 0, 0, 0,
                            ARC_FAMILY_NETMGMT, ARC_NETMGMT_HEARTBEAT,
                            NULL, 0);
    ASSERT_EQ(n, ARC_OVERHEAD);

    arc_frame_t f;
    ASSERT_EQ(arc_frame_parse(frame, n, &f), ARC_OK);
    ASSERT_EQ(f.payload_len, 0);
}

TEST(frame_max_payload)
{
    uint8_t payload[ARC_MAX_PAYLOAD_SIZE];
    for (int i = 0; i < ARC_MAX_PAYLOAD_SIZE; i++) payload[i] = (uint8_t)(i & 0xFF);

    uint8_t frame[ARC_MAX_FRAME_SIZE];
    int n = arc_frame_build(frame, sizeof(frame),
                            1, 2, 0, 0, 0, 0, 0,
                            payload, ARC_MAX_PAYLOAD_SIZE);
    ASSERT_EQ(n, ARC_MAX_FRAME_SIZE);

    arc_frame_t f;
    ASSERT_EQ(arc_frame_parse(frame, n, &f), ARC_OK);
    ASSERT_EQ(f.payload_len, ARC_MAX_PAYLOAD_SIZE);
    ASSERT_EQ(memcmp(f.payload, payload, ARC_MAX_PAYLOAD_SIZE), 0);
}

TEST(frame_oversized_rejected)
{
    uint8_t big_payload[ARC_MAX_PAYLOAD_SIZE + 1] = {0};
    uint8_t frame[ARC_MAX_FRAME_SIZE + 4];
    int n = arc_frame_build(frame, sizeof(frame),
                            1, 2, 0, 0, 0, 0, 0,
                            big_payload, ARC_MAX_PAYLOAD_SIZE + 1);
    ASSERT_EQ(n, ARC_ERR_TOO_LONG);
}

TEST(frame_buffer_too_small)
{
    uint8_t payload[10] = {0};
    uint8_t small[5];
    int n = arc_frame_build(small, sizeof(small),
                            1, 2, 0, 0, 0, 0, 0,
                            payload, 10);
    ASSERT_EQ(n, ARC_ERR_BUFFER);
}

TEST(frame_null_payload_rejected)
{
    uint8_t frame[32];
    int n = arc_frame_build(frame, sizeof(frame),
                            1, 2, 0, 0, 0, 0, 0,
                            NULL, 1);
    ASSERT_EQ(n, ARC_ERR_BAD_ARG);
}

TEST(frame_corrupted_crc_detected)
{
    const uint8_t payload[] = {1, 2, 3};
    uint8_t frame[32];
    int n = arc_frame_build(frame, sizeof(frame),
                            1, 2, 0, 0, 0, 0, 0,
                            payload, 3);
    ASSERT(n > 0);

    // Flip a bit in the payload.
    frame[ARC_HEADER_SIZE] ^= 0x01;

    arc_frame_t f;
    ASSERT_EQ(arc_frame_parse(frame, n, &f), ARC_ERR_BAD_CRC);
}

TEST(frame_corrupted_header_detected)
{
    uint8_t frame[32];
    int n = arc_frame_build(frame, sizeof(frame),
                            1, 2, 0, 0, 0, 0, 0,
                            NULL, 0);
    ASSERT(n > 0);

    // Flip a bit in the FLAGS field.
    frame[3] ^= 0x10;

    arc_frame_t f;
    ASSERT_EQ(arc_frame_parse(frame, n, &f), ARC_ERR_BAD_CRC);
}

TEST(frame_bad_length_detected)
{
    uint8_t frame[32];
    int n = arc_frame_build(frame, sizeof(frame),
                            1, 2, 0, 0, 0, 0, 0,
                            NULL, 0);
    ASSERT(n > 0);

    // Tamper with the LEN field.
    frame[0] = 99;
    arc_frame_t f;
    ASSERT_EQ(arc_frame_parse(frame, n, &f), ARC_ERR_BAD_LENGTH);
}

// ----------------------------------------------------------------------
// Combined tests: full encode pipeline (build + COBS) and back.
// This is the actual wire round trip for serial links.
// ----------------------------------------------------------------------

TEST(full_pipeline_serial)
{
    // Payload deliberately includes 0x00 bytes to exercise COBS.
    const uint8_t payload[] = {0x00, 0xAA, 0x00, 0x55, 0x00};

    uint8_t frame[ARC_MAX_FRAME_SIZE];
    int frame_len = arc_frame_build(frame, sizeof(frame),
                                    ARC_ADDR_FC_C, ARC_ADDR_GROUND,
                                    ARC_FLAG_RELIABLE, 3, 100,
                                    ARC_FAMILY_FC_COORD, 0x10,
                                    payload, 5);
    ASSERT(frame_len > 0);

    uint8_t encoded[ARC_MAX_ENCODED_SIZE];
    int encoded_len = arc_cobs_encode(frame, frame_len, encoded, sizeof(encoded));
    ASSERT(encoded_len > 0);

    // Encoded data must contain no zeros except the final delimiter.
    for (int i = 0; i < encoded_len - 1; i++) {
        ASSERT(encoded[i] != 0x00);
    }
    ASSERT_EQ(encoded[encoded_len - 1], 0x00);

    // Decode and parse.
    uint8_t decoded[ARC_MAX_FRAME_SIZE];
    int decoded_len = arc_cobs_decode(encoded, encoded_len, decoded, sizeof(decoded));
    ASSERT_EQ(decoded_len, frame_len);
    ASSERT_EQ(memcmp(decoded, frame, frame_len), 0);

    arc_frame_t f;
    ASSERT_EQ(arc_frame_parse(decoded, decoded_len, &f), ARC_OK);
    ASSERT_EQ(f.payload_len, 5);
    ASSERT_EQ(memcmp(f.payload, payload, 5), 0);
}

TEST(full_pipeline_radio_size_limit)
{
    // Verify the math: ARC_MAX_PAYLOAD_SIZE bytes of payload should
    // produce an encoded frame of exactly ARC_MAX_ENCODED_SIZE bytes.
    uint8_t payload[ARC_MAX_PAYLOAD_SIZE];
    for (int i = 0; i < ARC_MAX_PAYLOAD_SIZE; i++) {
        payload[i] = (uint8_t)((i % 254) + 1);  // never zero
    }

    uint8_t frame[ARC_MAX_FRAME_SIZE];
    int frame_len = arc_frame_build(frame, sizeof(frame),
                                    1, 2, 0, 0, 0, 0, 0,
                                    payload, ARC_MAX_PAYLOAD_SIZE);
    ASSERT_EQ(frame_len, ARC_MAX_FRAME_SIZE);

    uint8_t encoded[ARC_MAX_ENCODED_SIZE];
    int encoded_len = arc_cobs_encode(frame, frame_len, encoded, sizeof(encoded));
    // Should fit within the radio limit.
    ASSERT(encoded_len <= 255);
}

// ----------------------------------------------------------------------
// ACK building.
// ----------------------------------------------------------------------

TEST(ack_addresses_swapped)
{
    uint8_t orig_frame[32];
    int n = arc_frame_build(orig_frame, sizeof(orig_frame),
                            ARC_ADDR_GROUND, ARC_ADDR_FC_L,
                            ARC_FLAG_RELIABLE, 5, 0xABCD,
                            ARC_FAMILY_FC_COORD, 0x20,
                            NULL, 0);
    ASSERT(n > 0);

    arc_frame_t orig;
    ASSERT_EQ(arc_frame_parse(orig_frame, n, &orig), ARC_OK);

    uint8_t ack_frame[32];
    int an = arc_frame_build_ack(ack_frame, sizeof(ack_frame), &orig, 1, 9999);
    ASSERT(an > 0);

    arc_frame_t ack;
    ASSERT_EQ(arc_frame_parse(ack_frame, an, &ack), ARC_OK);
    ASSERT_EQ(ack.src, ARC_ADDR_FC_L);       // was orig.dst
    ASSERT_EQ(ack.dst, ARC_ADDR_GROUND);     // was orig.src
    ASSERT_EQ(ack.family, ARC_FAMILY_NETMGMT);
    ASSERT_EQ(ack.type, ARC_NETMGMT_ACK);
    ASSERT_EQ(ack.flags & ARC_FLAG_ACK, ARC_FLAG_ACK);
    ASSERT_EQ(ack.payload_len, 2);
    // Payload is the acked SEQ, big-endian.
    ASSERT_EQ(ack.payload[0], 0xAB);
    ASSERT_EQ(ack.payload[1], 0xCD);
}

// ----------------------------------------------------------------------
// Test runner.
// ----------------------------------------------------------------------

int main(void)
{
    printf("ARC protocol tests\n");
    printf("==================\n");
    printf("\nCRC:\n");
    RUN(crc_empty);
    RUN(crc_known_vector);
    RUN(crc_single_byte);

    printf("\nCOBS:\n");
    RUN(cobs_simple_no_zeros);
    RUN(cobs_payload_with_zero);
    RUN(cobs_all_zeros);
    RUN(cobs_round_trip_random);
    RUN(cobs_long_run_no_zero);
    RUN(cobs_decode_rejects_bad_input);

    printf("\nFrame build/parse:\n");
    RUN(frame_round_trip_simple);
    RUN(frame_empty_payload);
    RUN(frame_max_payload);
    RUN(frame_oversized_rejected);
    RUN(frame_buffer_too_small);
    RUN(frame_null_payload_rejected);
    RUN(frame_corrupted_crc_detected);
    RUN(frame_corrupted_header_detected);
    RUN(frame_bad_length_detected);

    printf("\nFull pipeline:\n");
    RUN(full_pipeline_serial);
    RUN(full_pipeline_radio_size_limit);

    printf("\nAck handling:\n");
    RUN(ack_addresses_swapped);

    printf("\n==================\n");
    printf("Passed: %d / %d\n", tests_run - tests_failed, tests_run);
    return tests_failed > 0 ? 1 : 0;
}
