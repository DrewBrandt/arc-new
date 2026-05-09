// test_arc_reliable.c
// Unit tests for arc_reliable.

#include "../src/arc_reliable.h"

#include <stdio.h>
#include <string.h>

static int tests_run = 0;
static int tests_failed = 0;

#define TEST(name) static void test_##name(void)
#define RUN(name) do { \
    printf("  %-45s ", #name); fflush(stdout); \
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
// Capture state for tests: every send_fn call records the frame.
// ----------------------------------------------------------------------

#define CAPTURE_MAX 32

typedef struct {
    arc_frame_t frame;
    uint8_t     payload_buf[ARC_RELIABLE_PAYLOAD_MAX];
} captured_frame_t;

typedef struct {
    captured_frame_t sent[CAPTURE_MAX];
    size_t           sent_count;
    captured_frame_t delivered[CAPTURE_MAX];
    size_t           delivered_count;
    captured_frame_t failed[CAPTURE_MAX];
    size_t           failed_count;
} capture_t;

static void capture_push(captured_frame_t* arr, size_t* count,
                         const arc_frame_t* frame)
{
    if (*count >= CAPTURE_MAX) return;
    captured_frame_t* slot = &arr[*count];
    slot->frame = *frame;
    if (frame->payload_len > 0 && frame->payload != NULL) {
        memcpy(slot->payload_buf, frame->payload, frame->payload_len);
        slot->frame.payload = slot->payload_buf;
    } else {
        slot->frame.payload = NULL;
    }
    (*count)++;
}

static void cap_send(void* user, const arc_frame_t* frame)
{
    capture_t* c = (capture_t*)user;
    capture_push(c->sent, &c->sent_count, frame);
}

static void cap_deliver(void* user, const arc_frame_t* frame)
{
    capture_t* c = (capture_t*)user;
    capture_push(c->delivered, &c->delivered_count, frame);
}

static void cap_fail(void* user, const arc_frame_t* frame)
{
    capture_t* c = (capture_t*)user;
    capture_push(c->failed, &c->failed_count, frame);
}

static void capture_reset(capture_t* c)
{
    memset(c, 0, sizeof(*c));
}

// ----------------------------------------------------------------------
// Tests.
// ----------------------------------------------------------------------

TEST(send_unreliable_does_not_park)
{
    capture_t cap; capture_reset(&cap);
    arc_reliable_t r;
    arc_reliable_init(&r, 0x10, 1, /*timeout_ms=*/1000, /*max_retries=*/3,
                      /*first_seq=*/0,
                      cap_send, cap_deliver, cap_fail, &cap);

    uint16_t seq = 0xFFFF;
    uint8_t payload[] = {0xAA, 0xBB};
    arc_result_t rc = arc_reliable_send(
        &r, 0x12, ARC_FAMILY_VIDEO, 0x01, /*flags=*/0, /*reliable=*/false,
        payload, sizeof(payload), /*now_ms=*/0, &seq);

    ASSERT_EQ(rc, ARC_OK);
    ASSERT_EQ(seq, 0);
    ASSERT_EQ(cap.sent_count, 1u);
    ASSERT_EQ(arc_reliable_pending_count(&r), 0u);
    ASSERT_EQ(cap.sent[0].frame.src, 0x10);
    ASSERT_EQ(cap.sent[0].frame.dst, 0x12);
    ASSERT_EQ(cap.sent[0].frame.flags & ARC_FLAG_RELIABLE, 0);
}

TEST(send_reliable_parks_until_ack)
{
    capture_t cap; capture_reset(&cap);
    arc_reliable_t r;
    arc_reliable_init(&r, 0x10, 7, 1000, 3, 100,
                      cap_send, cap_deliver, cap_fail, &cap);

    uint16_t seq = 0;
    uint8_t payload[] = {1, 2, 3};
    arc_reliable_send(&r, 0x12, ARC_FAMILY_VIDEO, 0x05, 0, true,
                      payload, sizeof(payload), 0, &seq);
    ASSERT_EQ(seq, 100);
    ASSERT_EQ(arc_reliable_pending_count(&r), 1u);
    ASSERT(cap.sent[0].frame.flags & ARC_FLAG_RELIABLE);

    // Build a synthetic ACK addressed back to us with payload = seq.
    uint8_t ack_payload[2] = { (uint8_t)(seq >> 8), (uint8_t)(seq & 0xFF) };
    arc_frame_t ack = {
        .src = 0x12, .dst = 0x10,
        .flags = ARC_FLAG_ACK,
        .session = 7,
        .seq = 0xAAAA,
        .family = ARC_FAMILY_NETMGMT,
        .type = ARC_NETMGMT_ACK,
        .payload = ack_payload,
        .payload_len = 2,
    };
    arc_rx_result_t res = arc_reliable_receive(&r, &ack);
    ASSERT_EQ(res, ARC_RX_ACK);
    ASSERT_EQ(arc_reliable_pending_count(&r), 0u);
}

TEST(tick_retries_then_fails)
{
    capture_t cap; capture_reset(&cap);
    arc_reliable_t r;
    arc_reliable_init(&r, 0x10, 1, /*timeout_ms=*/100, /*max_retries=*/2, 0,
                      cap_send, cap_deliver, cap_fail, &cap);

    arc_reliable_send(&r, 0x12, ARC_FAMILY_VIDEO, 1, 0, true, NULL, 0,
                      /*now_ms=*/0, NULL);
    ASSERT_EQ(cap.sent_count, 1u);

    arc_reliable_tick(&r, 100);  // first retry
    ASSERT_EQ(cap.sent_count, 2u);
    ASSERT_EQ(arc_reliable_pending_count(&r), 1u);

    arc_reliable_tick(&r, 200);  // second retry
    ASSERT_EQ(cap.sent_count, 3u);
    ASSERT_EQ(arc_reliable_pending_count(&r), 1u);

    arc_reliable_tick(&r, 300);  // exhausted
    ASSERT_EQ(cap.sent_count, 3u);
    ASSERT_EQ(arc_reliable_pending_count(&r), 0u);
    ASSERT_EQ(cap.failed_count, 1u);
    ASSERT_EQ(cap.failed[0].frame.dst, 0x12);
}

TEST(receive_non_reliable_delivers_directly)
{
    capture_t cap; capture_reset(&cap);
    arc_reliable_t r;
    arc_reliable_init(&r, 0x10, 1, 1000, 3, 0,
                      cap_send, cap_deliver, cap_fail, &cap);

    uint8_t payload[] = {0xDE, 0xAD};
    arc_frame_t in = {
        .src = 0x12, .dst = 0x10, .flags = 0, .session = 5, .seq = 42,
        .family = ARC_FAMILY_VIDEO, .type = 0x10,
        .payload = payload, .payload_len = 2,
    };
    arc_rx_result_t res = arc_reliable_receive(&r, &in);
    ASSERT_EQ(res, ARC_RX_DELIVERED);
    ASSERT_EQ(cap.delivered_count, 1u);
    // No ack for non-reliable frame.
    ASSERT_EQ(cap.sent_count, 0u);
}

TEST(receive_reliable_acks_and_dedups)
{
    capture_t cap; capture_reset(&cap);
    arc_reliable_t r;
    arc_reliable_init(&r, 0x10, 1, 1000, 3, 0,
                      cap_send, cap_deliver, cap_fail, &cap);

    arc_frame_t in = {
        .src = 0x12, .dst = 0x10, .flags = ARC_FLAG_RELIABLE,
        .session = 5, .seq = 7,
        .family = ARC_FAMILY_VIDEO, .type = 0x10,
        .payload = NULL, .payload_len = 0,
    };
    ASSERT_EQ(arc_reliable_receive(&r, &in), ARC_RX_DELIVERED);
    ASSERT_EQ(cap.delivered_count, 1u);
    ASSERT_EQ(cap.sent_count, 1u);  // ack
    ASSERT(cap.sent[0].frame.flags & ARC_FLAG_ACK);
    ASSERT_EQ(cap.sent[0].frame.dst, 0x12);

    // Same frame again: still ack, but no second delivery.
    ASSERT_EQ(arc_reliable_receive(&r, &in), ARC_RX_DUPLICATE);
    ASSERT_EQ(cap.delivered_count, 1u);
    ASSERT_EQ(cap.sent_count, 2u);  // second ack
}

TEST(session_change_resets_dedup)
{
    capture_t cap; capture_reset(&cap);
    arc_reliable_t r;
    arc_reliable_init(&r, 0x10, 1, 1000, 3, 0,
                      cap_send, cap_deliver, cap_fail, &cap);

    arc_frame_t in = {
        .src = 0x12, .dst = 0x10, .flags = ARC_FLAG_RELIABLE,
        .session = 5, .seq = 1,
        .family = ARC_FAMILY_VIDEO, .type = 0x10,
        .payload = NULL, .payload_len = 0,
    };
    arc_reliable_receive(&r, &in);
    ASSERT_EQ(cap.delivered_count, 1u);

    // Same SEQ, new session -> not a duplicate.
    in.session = 6;
    arc_reliable_receive(&r, &in);
    ASSERT_EQ(cap.delivered_count, 2u);
}

TEST(frame_for_other_dst_is_ignored)
{
    capture_t cap; capture_reset(&cap);
    arc_reliable_t r;
    arc_reliable_init(&r, 0x10, 1, 1000, 3, 0,
                      cap_send, cap_deliver, cap_fail, &cap);

    arc_frame_t in = {
        .src = 0x12, .dst = 0x99, .flags = ARC_FLAG_RELIABLE,
        .session = 1, .seq = 1,
        .family = ARC_FAMILY_VIDEO, .type = 0x10,
        .payload = NULL, .payload_len = 0,
    };
    ASSERT_EQ(arc_reliable_receive(&r, &in), ARC_RX_IGNORED);
    ASSERT_EQ(cap.delivered_count, 0u);
    ASSERT_EQ(cap.sent_count, 0u);
}

TEST(payload_too_long_rejected)
{
    capture_t cap; capture_reset(&cap);
    arc_reliable_t r;
    arc_reliable_init(&r, 0x10, 1, 1000, 3, 0,
                      cap_send, cap_deliver, cap_fail, &cap);

    uint8_t big[ARC_RELIABLE_PAYLOAD_MAX + 1] = {0};
    arc_result_t rc = arc_reliable_send(
        &r, 0x12, ARC_FAMILY_VIDEO, 1, 0, true,
        big, sizeof(big), 0, NULL);
    ASSERT_EQ(rc, ARC_ERR_TOO_LONG);
}

TEST(pending_table_full_returns_buffer)
{
    capture_t cap; capture_reset(&cap);
    arc_reliable_t r;
    arc_reliable_init(&r, 0x10, 1, 1000, 3, 0,
                      cap_send, cap_deliver, cap_fail, &cap);

    for (size_t i = 0; i < ARC_RELIABLE_PENDING_MAX; i++) {
        arc_result_t rc = arc_reliable_send(
            &r, 0x12, ARC_FAMILY_VIDEO, 1, 0, true, NULL, 0, 0, NULL);
        ASSERT_EQ(rc, ARC_OK);
    }
    arc_result_t rc = arc_reliable_send(
        &r, 0x12, ARC_FAMILY_VIDEO, 1, 0, true, NULL, 0, 0, NULL);
    ASSERT_EQ(rc, ARC_ERR_BUFFER);
}

int main(void)
{
    printf("test_arc_reliable\n");
    RUN(send_unreliable_does_not_park);
    RUN(send_reliable_parks_until_ack);
    RUN(tick_retries_then_fails);
    RUN(receive_non_reliable_delivers_directly);
    RUN(receive_reliable_acks_and_dedups);
    RUN(session_change_resets_dedup);
    RUN(frame_for_other_dst_is_ignored);
    RUN(payload_too_long_rejected);
    RUN(pending_table_full_returns_buffer);
    printf("\n%d tests run, %d failed\n", tests_run, tests_failed);
    return tests_failed == 0 ? 0 : 1;
}
