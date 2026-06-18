// hub_links.h
// The hub's direct UART spokes. Each link does COBS framing over a Teensy
// HardwareSerial and is registered with the arc_router as a forwarding link.
//
// The data radio is NOT one of these: it is TX-only and speaks a proprietary
// protocol, so it lives in data_radio.{h,cpp}.

#ifndef HUB_LINKS_H
#define HUB_LINKS_H

#include <Arduino.h>
#include "arc_protocol.h"

// Direct ARC UART spokes, in physical-slot order. Device identity is learned
// from ARC source addresses, not implied by this enum.
enum HubLinkId {
    HUB_LINK_SPOKE1 = 0,
    HUB_LINK_SPOKE2,
    HUB_LINK_SPOKE3,
    HUB_LINK_SPOKE4,
    HUB_LINK_SPOKE5,
    HUB_LINK_SPOKE6,
    HUB_LINK_SPOKE7,
    HUB_LINK_COUNT
};

struct HubLink {
    const char*     name;
    HardwareSerial* serial;
    uint8_t         rx[ARC_MAX_ENCODED_SIZE + 4];
    size_t          rx_len;
    uint8_t         non_arc[ARC_MAX_ENCODED_SIZE + 4];
    size_t          non_arc_len;
    uint32_t        last_rx_byte_ms;
    uint32_t        last_rx_ms;
    uint32_t        tx_count;
    uint32_t        rx_count;
};

static constexpr int HUB_LINK_READ_NON_ARC = -1000;

// Open all spoke UARTs at the configured baud.
void hub_links_begin(void);

HubLink* hub_links_get(HubLinkId id);
const char* hub_links_name(HubLinkId id);

// True if a frame was received on this link within timeout_ms.
bool hub_links_online(HubLinkId id, uint32_t now_ms, uint32_t timeout_ms);

// Pull COBS bytes off one link's UART. When a full frame is decoded it is
// written into `out` (capacity `cap`) and the decoded length is returned.
// Returns 0 when no complete frame is ready yet. Returns
// HUB_LINK_READ_NON_ARC when bytes arrived that are not a valid ARC packet;
// call hub_link_take_non_arc() to retrieve them.
int hub_link_read(HubLinkId id, uint8_t* out, size_t cap, uint32_t now_ms);

// Copy and clear the most recent non-ARC bytes captured for this link.
size_t hub_link_take_non_arc(HubLinkId id, uint8_t* out, size_t cap);

// arc_router link send_fn: COBS-encode `frame` and write it to the UART that
// `user` (a HubLink*) points at.
void hub_link_send(void* user, const arc_frame_t* frame);

// Write an already-built, unencoded frame to every UART spoke (used for the
// hub's broadcast heartbeat). COBS encoding is applied per link.
void hub_links_broadcast(const uint8_t* frame, int frame_len);

// Broadcast to every UART spoke except `except_id`. Pass HUB_LINK_COUNT to send
// to all spokes.
void hub_links_broadcast_except(const uint8_t* frame, int frame_len,
                                HubLinkId except_id);

#endif  // HUB_LINKS_H
