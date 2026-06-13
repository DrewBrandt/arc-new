// data_radio.h
// Transcode seam for the live data downlink (RADIO_DATA, 0x22).
//
// This radio does NOT speak ARC. Its packet format is owned by a third party
// who will not change it, and it cannot act as a network-wide messaging
// transport. So the hub TERMINATES ARC frames addressed to RADIO_DATA here and
// re-emits the interesting telemetry in the vendor's own framing. All of the
// vendor-specific knowledge is meant to live in this one translation unit.
//
// Wire it into the router as the link for RADIO_DATA:
//   int idx = arc_router_add_link(&router, data_radio_link_send, nullptr);
//   arc_router_add_route(&router, ARC_ADDR_RADIO_DATA, idx);

#ifndef DATA_RADIO_H
#define DATA_RADIO_H

#include <Arduino.h>
#include "arc_protocol.h"

// Open the data-radio UART.
void data_radio_begin(void);

// Transcode one ARC frame into the vendor format and write it to the radio.
void data_radio_emit(const arc_frame_t* frame);

// arc_router link send_fn (user is unused). Lets the router treat the data
// radio like any other destination link; routing to 0x22 transcodes.
void data_radio_link_send(void* user, const arc_frame_t* frame);

// Number of frames transcoded so far (for diagnostics / OLED).
uint32_t data_radio_tx_count(void);

#endif  // DATA_RADIO_H
