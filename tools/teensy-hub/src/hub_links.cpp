// hub_links.cpp -- see hub_links.h.

#include "hub_links.h"
#include "hub_config.h"

static HubLink g_links[HUB_LINK_COUNT];

void hub_links_begin(void) {
    g_links[HUB_LINK_PI5]       = HubLink{"pi-5-nose", &HUB_SERIAL_PI5,       {}, 0, 0, 0, 0};
    g_links[HUB_LINK_FC]        = HubLink{"fc-n",       &HUB_SERIAL_FC,        {}, 0, 0, 0, 0};
    g_links[HUB_LINK_POWER]     = HubLink{"power",      &HUB_SERIAL_POWER,     {}, 0, 0, 0, 0};
    g_links[HUB_LINK_RADIO_CMD] = HubLink{"radio-cmd",  &HUB_SERIAL_RADIO_CMD, {}, 0, 0, 0, 0};

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

bool hub_links_online(HubLinkId id, uint32_t now_ms, uint32_t timeout_ms) {
    HubLink* l = hub_links_get(id);
    if (!l || l->last_rx_ms == 0) return false;
    return (now_ms - l->last_rx_ms) < timeout_ms;
}

int hub_link_read(HubLinkId id, uint8_t* out, size_t cap, uint32_t now_ms) {
    HubLink* l = hub_links_get(id);
    if (!l) return 0;

    while (l->serial->available()) {
        int c = l->serial->read();
        if (c < 0) break;
        uint8_t b = (uint8_t)c;

        if (b == 0x00) {
            if (l->rx_len == 0) continue;  // resync / empty frame
            // arc_cobs_decode wants the trailing delimiter in its input.
            if (l->rx_len >= sizeof(l->rx)) { l->rx_len = 0; continue; }
            l->rx[l->rx_len++] = 0x00;
            int n = arc_cobs_decode(l->rx, l->rx_len, out, cap);
            l->rx_len = 0;
            if (n < 0) return n;  // COBS error; caller tallies it
            l->last_rx_ms = now_ms;
            l->rx_count++;
            return n;
        }

        if (l->rx_len < sizeof(l->rx)) {
            l->rx[l->rx_len++] = b;
        } else {
            l->rx_len = 0;  // overflow; resync on next delimiter
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
    if (!frame || frame_len <= 0) return;
    uint8_t enc[ARC_MAX_ENCODED_SIZE];
    int m = arc_cobs_encode(frame, (size_t)frame_len, enc, sizeof(enc));
    if (m < 0) return;
    for (int i = 0; i < HUB_LINK_COUNT; i++) {
        g_links[i].serial->write(enc, (size_t)m);
        g_links[i].tx_count++;
    }
}
