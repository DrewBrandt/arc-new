// test_arc_router.c
// Unit tests for arc_router.

#include "../src/arc_router.h"

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

typedef struct {
    int  count;
    uint8_t last_dst;
    uint8_t last_src;
} link_capture_t;

typedef struct {
    int     count;
    uint8_t last_dst;
} local_capture_t;

static void link_send_cb(void* user, const arc_frame_t* frame)
{
    link_capture_t* c = (link_capture_t*)user;
    c->count++;
    c->last_dst = frame->dst;
    c->last_src = frame->src;
}

static void local_cb(void* user, const arc_frame_t* frame)
{
    local_capture_t* c = (local_capture_t*)user;
    c->count++;
    c->last_dst = frame->dst;
}

TEST(local_dst_dispatched_to_local)
{
    local_capture_t local = {0};
    arc_router_t r;
    arc_router_init(&r, 0x10, local_cb, &local);

    arc_frame_t f = {
        .src = 0x12, .dst = 0x10, .flags = 0, .session = 1, .seq = 1,
        .family = ARC_FAMILY_VIDEO, .type = 1,
        .payload = NULL, .payload_len = 0,
    };
    int res = arc_router_route(&r, &f);
    ASSERT_EQ(res, ARC_ROUTE_LOCAL);
    ASSERT_EQ(local.count, 1);
    ASSERT_EQ(local.last_dst, 0x10);
}

TEST(static_route_forwards_to_link)
{
    local_capture_t local = {0};
    link_capture_t link_a = {0};
    link_capture_t link_b = {0};
    arc_router_t r;
    arc_router_init(&r, 0x10, local_cb, &local);

    int la = arc_router_add_link(&r, link_send_cb, &link_a);
    int lb = arc_router_add_link(&r, link_send_cb, &link_b);
    ASSERT(la >= 0); ASSERT(lb >= 0);

    ASSERT_EQ(arc_router_add_route(&r, 0x12, la), ARC_OK);
    ASSERT_EQ(arc_router_add_route(&r, 0x14, lb), ARC_OK);

    arc_frame_t to_a = { .src = 0x10, .dst = 0x12 };
    ASSERT_EQ(arc_router_route(&r, &to_a), ARC_ROUTE_FORWARDED);
    ASSERT_EQ(link_a.count, 1);
    ASSERT_EQ(link_b.count, 0);

    arc_frame_t to_b = { .src = 0x10, .dst = 0x14 };
    ASSERT_EQ(arc_router_route(&r, &to_b), ARC_ROUTE_FORWARDED);
    ASSERT_EQ(link_a.count, 1);
    ASSERT_EQ(link_b.count, 1);
}

TEST(default_link_used_when_no_route)
{
    local_capture_t local = {0};
    link_capture_t fallback = {0};
    arc_router_t r;
    arc_router_init(&r, 0x12, local_cb, &local);

    int idx = arc_router_add_link(&r, link_send_cb, &fallback);
    arc_router_set_default(&r, idx);

    arc_frame_t f = { .src = 0x12, .dst = 0x10 };  // controller, no explicit route
    ASSERT_EQ(arc_router_route(&r, &f), ARC_ROUTE_FORWARDED);
    ASSERT_EQ(fallback.count, 1);
    ASSERT_EQ(fallback.last_dst, 0x10);
}

TEST(no_route_returns_error)
{
    local_capture_t local = {0};
    arc_router_t r;
    arc_router_init(&r, 0x10, local_cb, &local);

    arc_frame_t f = { .src = 0x10, .dst = 0xAB };
    int res = arc_router_route(&r, &f);
    ASSERT(res < 0);
}

TEST(replacing_route_for_same_dst)
{
    local_capture_t local = {0};
    link_capture_t link_a = {0};
    link_capture_t link_b = {0};
    arc_router_t r;
    arc_router_init(&r, 0x10, local_cb, &local);

    int la = arc_router_add_link(&r, link_send_cb, &link_a);
    int lb = arc_router_add_link(&r, link_send_cb, &link_b);

    arc_router_add_route(&r, 0x12, la);
    arc_router_add_route(&r, 0x12, lb);  // overwrite

    arc_frame_t f = { .src = 0x10, .dst = 0x12 };
    arc_router_route(&r, &f);
    ASSERT_EQ(link_a.count, 0);
    ASSERT_EQ(link_b.count, 1);
}

TEST(invalid_link_idx_rejected)
{
    arc_router_t r;
    arc_router_init(&r, 0x10, NULL, NULL);

    // Add no links yet.
    ASSERT_EQ(arc_router_add_route(&r, 0x12, 0), ARC_ERR_BAD_ARG);
    ASSERT_EQ(arc_router_add_route(&r, 0x12, ARC_ROUTER_MAX_LINKS), ARC_ERR_BAD_ARG);
    ASSERT_EQ(arc_router_set_default(&r, ARC_ROUTER_MAX_LINKS), ARC_ERR_BAD_ARG);
}

int main(void)
{
    printf("test_arc_router\n");
    RUN(local_dst_dispatched_to_local);
    RUN(static_route_forwards_to_link);
    RUN(default_link_used_when_no_route);
    RUN(no_route_returns_error);
    RUN(replacing_route_for_same_dst);
    RUN(invalid_link_idx_rejected);
    printf("\n%d tests run, %d failed\n", tests_run, tests_failed);
    return tests_failed == 0 ? 0 : 1;
}
