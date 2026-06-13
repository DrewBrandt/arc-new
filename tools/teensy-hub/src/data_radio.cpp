// data_radio.cpp -- see data_radio.h.
//
// ====================================================================
// TODO(vendor-format): THIS IS A PLACEHOLDER FRAMING.
// --------------------------------------------------------------------
// The real data radio uses a proprietary packet format we do not yet have
// the spec for. Until we do, this emits a self-describing placeholder frame
// so the transcode path is wired end-to-end and testable. When the vendor
// spec arrives, replace `pack_vendor_frame()` (and only that) with the real
// encoding -- the routing, the call site, and the field projection above it
// should not need to change.
// ====================================================================

#include "data_radio.h"
#include "hub_config.h"

#include <string.h>

static uint32_t g_tx_count = 0;

void data_radio_begin(void) {
    HUB_SERIAL_DATA_RADIO.begin(HUB_DATA_BAUD);
}

// Project an ARC frame down to the fields the live display actually wants.
// Keep this provider-agnostic; only pack_vendor_frame() should know bytes.
struct DataPoint {
    uint8_t  source;    // ARC src address the telemetry came from
    uint8_t  family;    // ARC family (so the ground side can route/label it)
    uint8_t  type;      // ARC message type
    uint8_t  len;       // payload length
    const uint8_t* payload;
};

// PLACEHOLDER vendor framing:
//   [0xA5][source][family][type][len][payload...][xor checksum over all prior]
// Replace this body with the real vendor encoding when available.
static int pack_vendor_frame(const DataPoint* dp, uint8_t* out, size_t cap) {
    size_t need = 5 + dp->len + 1;
    if (need > cap) return -1;
    size_t i = 0;
    out[i++] = 0xA5;
    out[i++] = dp->source;
    out[i++] = dp->family;
    out[i++] = dp->type;
    out[i++] = dp->len;
    for (uint8_t j = 0; j < dp->len; j++) out[i++] = dp->payload[j];
    uint8_t xsum = 0;
    for (size_t k = 0; k < i; k++) xsum ^= out[k];
    out[i++] = xsum;
    return (int)i;
}

void data_radio_emit(const arc_frame_t* frame) {
    if (!frame) return;

    DataPoint dp;
    dp.source = frame->src;
    dp.family = frame->family;
    dp.type = frame->type;
    dp.len = frame->payload_len;
    dp.payload = frame->payload;

    uint8_t vbuf[ARC_MAX_FRAME_SIZE + 8];
    int n = pack_vendor_frame(&dp, vbuf, sizeof(vbuf));
    if (n < 0) return;

    HUB_SERIAL_DATA_RADIO.write(vbuf, (size_t)n);
    g_tx_count++;
}

void data_radio_link_send(void* user, const arc_frame_t* frame) {
    (void)user;
    data_radio_emit(frame);
}

uint32_t data_radio_tx_count(void) { return g_tx_count; }
