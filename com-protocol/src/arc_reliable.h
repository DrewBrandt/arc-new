// arc_reliable.h
// ACK / retry / dedup state machine for one ARC endpoint.
//
// Mirrors control-plane/arc/reliable.py. No dynamic allocation: all state
// lives in fixed-size tables sized at compile time. Tune the maxima
// below at compile time (-Darc_reliable_xxx=N) for memory-constrained
// targets like the Arduino Nano.

#ifndef ARC_RELIABLE_H
#define ARC_RELIABLE_H

#include "arc_protocol.h"

#ifdef __cplusplus
extern "C" {
#endif

// Compile-time table sizes. Override via -D flags as needed.
#ifndef ARC_RELIABLE_PENDING_MAX
#define ARC_RELIABLE_PENDING_MAX 8
#endif

#ifndef ARC_RELIABLE_SEEN_MAX
#define ARC_RELIABLE_SEEN_MAX 16
#endif

#ifndef ARC_RELIABLE_SOURCES_MAX
#define ARC_RELIABLE_SOURCES_MAX 6
#endif

// Per-pending payload buffer. Default fits any control-plane message we
// retry today (heartbeats, video commands). Anything larger fails to
// queue with ARC_ERR_TOO_LONG -- bump this if a new reliable message
// type needs more room.
#ifndef ARC_RELIABLE_PAYLOAD_MAX
#define ARC_RELIABLE_PAYLOAD_MAX 64
#endif

// ----------------------------------------------------------------------
// Callbacks supplied by the embedder.
//
// send_fn: forward a frame onto the network. Typically wired to
//   arc_router_route. Return value is ignored (errors are best-effort).
// deliver_fn: called when a non-duplicate frame addressed to my_addr
//   arrives. The frame pointer and its payload are valid only for the
//   duration of the call.
// fail_fn: called when a reliable send exhausts retries.
// ----------------------------------------------------------------------
typedef void (*arc_reliable_send_fn)(void* user, const arc_frame_t* frame);
typedef void (*arc_reliable_deliver_fn)(void* user, const arc_frame_t* frame);
typedef void (*arc_reliable_fail_fn)(void* user, const arc_frame_t* frame);

typedef struct {
    bool     in_use;
    uint32_t deadline_ms;
    uint8_t  retries;
    uint8_t  dst;
    uint8_t  flags;
    uint8_t  session;
    uint16_t seq;
    uint8_t  family;
    uint8_t  type;
    uint8_t  payload_len;
    uint8_t  payload[ARC_RELIABLE_PAYLOAD_MAX];
} arc_reliable_pending_t;

typedef struct {
    bool     in_use;
    uint8_t  src;
    uint8_t  session;
    uint16_t seq;
} arc_reliable_seen_t;

typedef struct {
    bool    in_use;
    uint8_t src;
    uint8_t session;
} arc_reliable_source_t;

typedef struct {
    uint8_t  my_addr;
    uint8_t  session;
    uint16_t next_seq;
    uint32_t timeout_ms;
    uint8_t  max_retries;

    arc_reliable_send_fn    send_fn;
    arc_reliable_deliver_fn deliver_fn;
    arc_reliable_fail_fn    fail_fn;
    void*    user;

    arc_reliable_pending_t pending[ARC_RELIABLE_PENDING_MAX];
    arc_reliable_seen_t    seen[ARC_RELIABLE_SEEN_MAX];
    uint8_t                seen_next_idx;  // FIFO replacement index
    arc_reliable_source_t  sources[ARC_RELIABLE_SOURCES_MAX];
} arc_reliable_t;

typedef enum {
    ARC_RX_IGNORED   = 0,  // dst != my_addr
    ARC_RX_DELIVERED = 1,
    ARC_RX_DUPLICATE = 2,
    ARC_RX_ACK       = 3,
} arc_rx_result_t;

// ----------------------------------------------------------------------
// Initialise an endpoint. fail_fn may be NULL if the embedder doesn't
// care about exhausted retries.
// ----------------------------------------------------------------------
void arc_reliable_init(arc_reliable_t* r,
                       uint8_t my_addr,
                       uint8_t session,
                       uint32_t timeout_ms,
                       uint8_t max_retries,
                       uint16_t first_seq,
                       arc_reliable_send_fn send_fn,
                       arc_reliable_deliver_fn deliver_fn,
                       arc_reliable_fail_fn fail_fn,
                       void* user);

// ----------------------------------------------------------------------
// Locally originate a frame.
//
// If reliable=true, ARC_FLAG_RELIABLE is OR'd into flags and the frame
// is parked in the pending table for retransmission. The assigned SEQ
// is written to *out_seq when out_seq is non-NULL.
//
// Returns ARC_OK, ARC_ERR_TOO_LONG (payload exceeds buffer), or
// ARC_ERR_BUFFER (pending table full).
// ----------------------------------------------------------------------
arc_result_t arc_reliable_send(arc_reliable_t* r,
                               uint8_t dst,
                               uint8_t family,
                               uint8_t type,
                               uint8_t flags,
                               bool reliable,
                               const uint8_t* payload,
                               size_t payload_len,
                               uint32_t now_ms,
                               uint16_t* out_seq);

// ----------------------------------------------------------------------
// Process one incoming frame. Acks clear pending entries; reliable
// frames addressed to me are deduped and ack'd; non-reliable frames are
// delivered straight through.
// ----------------------------------------------------------------------
arc_rx_result_t arc_reliable_receive(arc_reliable_t* r,
                                     const arc_frame_t* frame);

// ----------------------------------------------------------------------
// Drive timers. Call periodically with the current monotonic time in
// milliseconds. Pending frames whose deadlines elapse are retransmitted
// up to max_retries; after that fail_fn is invoked and the slot freed.
// ----------------------------------------------------------------------
void arc_reliable_tick(arc_reliable_t* r, uint32_t now_ms);

size_t arc_reliable_pending_count(const arc_reliable_t* r);

#ifdef __cplusplus
}
#endif

#endif  // ARC_RELIABLE_H
