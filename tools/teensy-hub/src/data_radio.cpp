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
#include <string.h>

static uint32_t g_tx_count = 0;

void data_radio_begin(void) {
    // Serial1..Serial8 are all learned ARC spokes now. The proprietary data
    // radio transcode seam is intentionally disabled until it gets a dedicated
    // non-spoke transport.
}

void data_radio_emit(const arc_frame_t* frame) {
    (void)frame;
}

void data_radio_link_send(void* user, const arc_frame_t* frame) {
    (void)user;
    data_radio_emit(frame);
}

uint32_t data_radio_tx_count(void) { return g_tx_count; }
