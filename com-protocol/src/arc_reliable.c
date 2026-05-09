// arc_reliable.c
// Implementation of the ARC reliable endpoint state machine.

#include "arc_reliable.h"

#include <string.h>

// Forward declarations of internal helpers.
static void emit_frame(arc_reliable_t* r, const arc_reliable_pending_t* p);
static void emit_ack(arc_reliable_t* r, const arc_frame_t* original);
static arc_reliable_pending_t* pending_alloc(arc_reliable_t* r);
static arc_reliable_pending_t* pending_find(arc_reliable_t* r,
                                            uint8_t dst,
                                            uint8_t session,
                                            uint16_t seq);
static bool seen_contains(const arc_reliable_t* r,
                          uint8_t src, uint8_t session, uint16_t seq);
static void seen_add(arc_reliable_t* r,
                     uint8_t src, uint8_t session, uint16_t seq);
static void seen_drop_source(arc_reliable_t* r, uint8_t src);
static bool source_session_changed(arc_reliable_t* r,
                                   uint8_t src, uint8_t session);
static uint16_t take_seq(arc_reliable_t* r);
static void pending_to_frame(const arc_reliable_pending_t* p,
                             uint8_t my_addr,
                             arc_frame_t* out);
static bool is_ack_frame(const arc_frame_t* frame);

void arc_reliable_init(arc_reliable_t* r,
                       uint8_t my_addr,
                       uint8_t session,
                       uint32_t timeout_ms,
                       uint8_t max_retries,
                       uint16_t first_seq,
                       arc_reliable_send_fn send_fn,
                       arc_reliable_deliver_fn deliver_fn,
                       arc_reliable_fail_fn fail_fn,
                       void* user)
{
    memset(r, 0, sizeof(*r));
    r->my_addr     = my_addr;
    r->session     = session;
    r->timeout_ms  = timeout_ms;
    r->max_retries = max_retries;
    r->next_seq    = first_seq;
    r->send_fn     = send_fn;
    r->deliver_fn  = deliver_fn;
    r->fail_fn     = fail_fn;
    r->user        = user;
}

arc_result_t arc_reliable_send(arc_reliable_t* r,
                               uint8_t dst,
                               uint8_t family,
                               uint8_t type,
                               uint8_t flags,
                               bool reliable,
                               const uint8_t* payload,
                               size_t payload_len,
                               uint32_t now_ms,
                               uint16_t* out_seq)
{
    if (payload_len > ARC_RELIABLE_PAYLOAD_MAX) return ARC_ERR_TOO_LONG;
    if (payload == NULL && payload_len > 0) return ARC_ERR_BAD_ARG;

    if (reliable) flags |= ARC_FLAG_RELIABLE;

    uint16_t seq = take_seq(r);

    // Stage the outbound frame in a pending slot (whether or not the
    // caller wants reliability) -- it's the simplest place to keep the
    // payload bytes alive across the send_fn call. If non-reliable,
    // we'll free the slot immediately after sending.
    arc_reliable_pending_t scratch;
    arc_reliable_pending_t* slot;
    if (reliable) {
        slot = pending_alloc(r);
        if (slot == NULL) return ARC_ERR_BUFFER;
    } else {
        slot = &scratch;
        memset(slot, 0, sizeof(*slot));
    }

    slot->in_use      = true;
    slot->deadline_ms = now_ms + r->timeout_ms;
    slot->retries     = 0;
    slot->dst         = dst;
    slot->flags       = flags;
    slot->session     = r->session;
    slot->seq         = seq;
    slot->family      = family;
    slot->type        = type;
    slot->payload_len = (uint8_t)payload_len;
    if (payload_len > 0) memcpy(slot->payload, payload, payload_len);

    emit_frame(r, slot);

    if (!reliable) {
        // scratch goes out of scope; nothing to free.
    }

    if (out_seq != NULL) *out_seq = seq;
    return ARC_OK;
}

arc_rx_result_t arc_reliable_receive(arc_reliable_t* r,
                                     const arc_frame_t* frame)
{
    if (frame->dst != r->my_addr) return ARC_RX_IGNORED;

    if (is_ack_frame(frame)) {
        // ACK payload is the SEQ being acked, big-endian.
        if (frame->payload_len != 2 || frame->payload == NULL) {
            return ARC_RX_ACK;  // malformed ACK; drop silently like Python
        }
        uint16_t acked_seq =
            ((uint16_t)frame->payload[0] << 8) | (uint16_t)frame->payload[1];
        // Pending entries are keyed by (dst, session, seq) where dst is
        // the original dst of the outbound frame -- which is frame->src
        // here (the peer that's now acking us).
        arc_reliable_pending_t* p =
            pending_find(r, frame->src, r->session, acked_seq);
        if (p != NULL) p->in_use = false;
        return ARC_RX_ACK;
    }

    if (frame->flags & ARC_FLAG_RELIABLE) {
        if (source_session_changed(r, frame->src, frame->session)) {
            seen_drop_source(r, frame->src);
        }
        bool duplicate =
            seen_contains(r, frame->src, frame->session, frame->seq);
        emit_ack(r, frame);
        if (duplicate) return ARC_RX_DUPLICATE;
        seen_add(r, frame->src, frame->session, frame->seq);
    }

    if (r->deliver_fn != NULL) r->deliver_fn(r->user, frame);
    return ARC_RX_DELIVERED;
}

void arc_reliable_tick(arc_reliable_t* r, uint32_t now_ms)
{
    for (size_t i = 0; i < ARC_RELIABLE_PENDING_MAX; i++) {
        arc_reliable_pending_t* p = &r->pending[i];
        if (!p->in_use) continue;
        // Treat (now_ms - deadline) as signed to handle wrap-around for
        // the millisecond clock. If now is at-or-after the deadline, the
        // difference is non-negative when interpreted that way.
        int32_t delta = (int32_t)(now_ms - p->deadline_ms);
        if (delta < 0) continue;

        if (p->retries >= r->max_retries) {
            arc_frame_t failed;
            pending_to_frame(p, r->my_addr, &failed);
            p->in_use = false;
            if (r->fail_fn != NULL) r->fail_fn(r->user, &failed);
            continue;
        }

        p->retries++;
        p->deadline_ms = now_ms + r->timeout_ms;
        emit_frame(r, p);
    }
}

size_t arc_reliable_pending_count(const arc_reliable_t* r)
{
    size_t count = 0;
    for (size_t i = 0; i < ARC_RELIABLE_PENDING_MAX; i++) {
        if (r->pending[i].in_use) count++;
    }
    return count;
}

// ----------------------------------------------------------------------
// Internal helpers.
// ----------------------------------------------------------------------

static void emit_frame(arc_reliable_t* r, const arc_reliable_pending_t* p)
{
    arc_frame_t f;
    pending_to_frame(p, r->my_addr, &f);
    if (r->send_fn != NULL) r->send_fn(r->user, &f);
}

static void emit_ack(arc_reliable_t* r, const arc_frame_t* original)
{
    uint8_t ack_payload[2];
    ack_payload[0] = (uint8_t)(original->seq >> 8);
    ack_payload[1] = (uint8_t)(original->seq & 0xFF);

    arc_frame_t ack;
    ack.src         = r->my_addr;
    ack.dst         = original->src;
    ack.flags       = ARC_FLAG_ACK;
    ack.session     = r->session;
    ack.seq         = take_seq(r);
    ack.family      = ARC_FAMILY_NETMGMT;
    ack.type        = ARC_NETMGMT_ACK;
    ack.payload     = ack_payload;
    ack.payload_len = 2;

    if (r->send_fn != NULL) r->send_fn(r->user, &ack);
}

static arc_reliable_pending_t* pending_alloc(arc_reliable_t* r)
{
    for (size_t i = 0; i < ARC_RELIABLE_PENDING_MAX; i++) {
        if (!r->pending[i].in_use) return &r->pending[i];
    }
    return NULL;
}

static arc_reliable_pending_t* pending_find(arc_reliable_t* r,
                                            uint8_t dst,
                                            uint8_t session,
                                            uint16_t seq)
{
    for (size_t i = 0; i < ARC_RELIABLE_PENDING_MAX; i++) {
        arc_reliable_pending_t* p = &r->pending[i];
        if (p->in_use && p->dst == dst &&
            p->session == session && p->seq == seq) {
            return p;
        }
    }
    return NULL;
}

static bool seen_contains(const arc_reliable_t* r,
                          uint8_t src, uint8_t session, uint16_t seq)
{
    for (size_t i = 0; i < ARC_RELIABLE_SEEN_MAX; i++) {
        const arc_reliable_seen_t* s = &r->seen[i];
        if (s->in_use && s->src == src &&
            s->session == session && s->seq == seq) {
            return true;
        }
    }
    return false;
}

static void seen_add(arc_reliable_t* r,
                     uint8_t src, uint8_t session, uint16_t seq)
{
    // Try free slot first.
    for (size_t i = 0; i < ARC_RELIABLE_SEEN_MAX; i++) {
        if (!r->seen[i].in_use) {
            r->seen[i].in_use  = true;
            r->seen[i].src     = src;
            r->seen[i].session = session;
            r->seen[i].seq     = seq;
            return;
        }
    }
    // Full: evict in FIFO order.
    arc_reliable_seen_t* victim = &r->seen[r->seen_next_idx];
    victim->in_use  = true;
    victim->src     = src;
    victim->session = session;
    victim->seq     = seq;
    r->seen_next_idx = (uint8_t)((r->seen_next_idx + 1) % ARC_RELIABLE_SEEN_MAX);
}

static void seen_drop_source(arc_reliable_t* r, uint8_t src)
{
    for (size_t i = 0; i < ARC_RELIABLE_SEEN_MAX; i++) {
        if (r->seen[i].in_use && r->seen[i].src == src) {
            r->seen[i].in_use = false;
        }
    }
}

static bool source_session_changed(arc_reliable_t* r,
                                   uint8_t src, uint8_t session)
{
    arc_reliable_source_t* free_slot = NULL;
    for (size_t i = 0; i < ARC_RELIABLE_SOURCES_MAX; i++) {
        arc_reliable_source_t* s = &r->sources[i];
        if (!s->in_use) {
            if (free_slot == NULL) free_slot = s;
            continue;
        }
        if (s->src == src) {
            if (s->session == session) return false;
            s->session = session;
            return true;
        }
    }
    // First time we've seen this source.
    if (free_slot != NULL) {
        free_slot->in_use  = true;
        free_slot->src     = src;
        free_slot->session = session;
    }
    // Source table full -- can't track this peer. Treat as not-changed
    // so we still dedup within the current run.
    return false;
}

static uint16_t take_seq(arc_reliable_t* r)
{
    uint16_t seq = r->next_seq;
    r->next_seq = (uint16_t)(r->next_seq + 1);  // wraps at 65536
    return seq;
}

static void pending_to_frame(const arc_reliable_pending_t* p,
                             uint8_t my_addr,
                             arc_frame_t* out)
{
    out->src         = my_addr;
    out->dst         = p->dst;
    out->flags       = p->flags;
    out->session     = p->session;
    out->seq         = p->seq;
    out->family      = p->family;
    out->type        = p->type;
    out->payload     = (p->payload_len > 0) ? p->payload : NULL;
    out->payload_len = p->payload_len;
}

static bool is_ack_frame(const arc_frame_t* frame)
{
    return (frame->flags & ARC_FLAG_ACK) != 0
        && frame->family == ARC_FAMILY_NETMGMT
        && frame->type == ARC_NETMGMT_ACK;
}
