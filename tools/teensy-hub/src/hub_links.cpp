// hub_links.cpp -- see hub_links.h.

#include "hub_links.h"
#include "hub_config.h"

#include <string.h>

static HubLink g_links[HUB_LINK_COUNT];

static void stash_non_arc(HubLink* l, const uint8_t* data, size_t len) {
    if (!l || !data || len == 0) return;
    if (len > sizeof(l->non_arc)) len = sizeof(l->non_arc);
    memcpy(l->non_arc, data, len);
    l->non_arc_len = len;
}

void hub_links_begin(void) {
    g_links[HUB_LINK_SPOKE1] = HubLink{"serial2", &HUB_SERIAL_SPOKE1, {}, 0, {}, 0, 0, 0, 0, 0};
    g_links[HUB_LINK_SPOKE2] = HubLink{"serial3", &HUB_SERIAL_SPOKE2, {}, 0, {}, 0, 0, 0, 0, 0};
    g_links[HUB_LINK_SPOKE3] = HubLink{"serial4", &HUB_SERIAL_SPOKE3, {}, 0, {}, 0, 0, 0, 0, 0};
    g_links[HUB_LINK_SPOKE4] = HubLink{"serial5", &HUB_SERIAL_SPOKE4, {}, 0, {}, 0, 0, 0, 0, 0};
    g_links[HUB_LINK_SPOKE5] = HubLink{"serial6", &HUB_SERIAL_SPOKE5, {}, 0, {}, 0, 0, 0, 0, 0};
    g_links[HUB_LINK_SPOKE6] = HubLink{"serial7", &HUB_SERIAL_SPOKE6, {}, 0, {}, 0, 0, 0, 0, 0};
    g_links[HUB_LINK_SPOKE7] = HubLink{"serial8", &HUB_SERIAL_SPOKE7, {}, 0, {}, 0, 0, 0, 0, 0};

    for (int i = 0; i < HUB_LINK_COUNT; i++) {
        g_links[i].serial->begin(HUB_LINK_BAUD);
    }
}

HubLink* hub_links_get(HubLinkId id) {
    if (id < 0 || id >= HUB_LINK_COUNT) return nullptr;
    return &g_links[id];
}

const char* hub_links_name(HubLinkId id) {
    HubLink* l = hub_links_get(id);
    return l ? l->name : "?";
}

size_t hub_link_take_non_arc(HubLinkId id, uint8_t* out, size_t cap) {
    HubLink* l = hub_links_get(id);
    if (!l || !out || cap == 0 || l->non_arc_len == 0) return 0;
    size_t n = l->non_arc_len;
    if (n > cap) n = cap;
    memcpy(out, l->non_arc, n);
    l->non_arc_len = 0;
    return n;
}

bool hub_links_online(HubLinkId id, uint32_t now_ms, uint32_t timeout_ms) {
    HubLink* l = hub_links_get(id);
    if (!l || l->last_rx_ms == 0) return false;
    return (now_ms - l->last_rx_ms) < timeout_ms;
}

int hub_link_read(HubLinkId id, uint8_t* out, size_t cap, uint32_t now_ms) {
    HubLink* l = hub_links_get(id);
    if (!l) return 0;

    if (l->non_arc_len > 0) return HUB_LINK_READ_NON_ARC;
    if (l->rx_len > 0
        && l->last_rx_byte_ms != 0
        && (now_ms - l->last_rx_byte_ms) >= HUB_NON_ARC_USB_IDLE_MS) {
        stash_non_arc(l, l->rx, l->rx_len);
        l->rx_len = 0;
        return HUB_LINK_READ_NON_ARC;
    }

    while (l->serial->available()) {
        int c = l->serial->read();
        if (c < 0) break;
        uint8_t b = (uint8_t)c;
        l->last_rx_byte_ms = now_ms;

        if (b == 0x00) {
            if (l->rx_len == 0) continue;  // resync / empty frame
            // arc_cobs_decode wants the trailing delimiter in its input.
            if (l->rx_len >= sizeof(l->rx)) {
                stash_non_arc(l, l->rx, l->rx_len);
                l->rx_len = 0;
                return HUB_LINK_READ_NON_ARC;
            }
            l->rx[l->rx_len++] = 0x00;
            int n = arc_cobs_decode(l->rx, l->rx_len, out, cap);
            size_t raw_len = l->rx_len;
            if (n < 0) {
                stash_non_arc(l, l->rx, raw_len);
                l->rx_len = 0;
                return HUB_LINK_READ_NON_ARC;
            }
            l->rx_len = 0;
            l->last_rx_ms = now_ms;
            l->rx_count++;
            return n;
        }

        if (l->rx_len < sizeof(l->rx)) {
            l->rx[l->rx_len++] = b;
        } else {
            stash_non_arc(l, l->rx, l->rx_len);
            l->rx[0] = b;
            l->rx_len = 1;
            return HUB_LINK_READ_NON_ARC;
        }
    }
    return 0;  // nothing complete yet
}

void hub_link_send(void* user, const arc_frame_t* frame) {
    HubLink* l = (HubLink*)user;
    if (!l || !frame) return;

    uint8_t built[ARC_MAX_FRAME_SIZE];
    int n = arc_frame_build(built, sizeof(built),
                            frame->src, frame->dst,
                            frame->flags, frame->session, frame->seq,
                            frame->family, frame->type,
                            frame->payload, frame->payload_len);
    if (n < 0) return;

    uint8_t enc[ARC_MAX_ENCODED_SIZE];
    int m = arc_cobs_encode(built, n, enc, sizeof(enc));
    if (m < 0) return;

    l->serial->write(enc, (size_t)m);
    l->tx_count++;
}

void hub_links_broadcast(const uint8_t* frame, int frame_len) {
    hub_links_broadcast_except(frame, frame_len, HUB_LINK_COUNT);
}

void hub_links_broadcast_except(const uint8_t* frame, int frame_len,
                                HubLinkId except_id) {
    if (!frame || frame_len <= 0) return;
    uint8_t enc[ARC_MAX_ENCODED_SIZE];
    int m = arc_cobs_encode(frame, (size_t)frame_len, enc, sizeof(enc));
    if (m < 0) return;
    for (int i = 0; i < HUB_LINK_COUNT; i++) {
        if ((HubLinkId)i == except_id) continue;
        g_links[i].serial->write(enc, (size_t)m);
        g_links[i].tx_count++;
    }
}
