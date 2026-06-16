// data_radio.h
// Optional transcode seam for the live data downlink (RADIO_DATA, 0x22).
//
// This radio does NOT speak ARC. Its packet format is owned by a third party
// who will not change it, and it cannot act as a network-wide messaging
// transport. This module is intentionally not part of the learned ARC UART
// router. If the data radio becomes an ARC node, it should heartbeat and earn a
// learned route like everything else.
//
// Call data_radio_emit() explicitly from a future transcode owner if needed.

#ifndef DATA_RADIO_H
#define DATA_RADIO_H

#include <Arduino.h>
#include "arc_protocol.h"

// Reserved hook for a future proprietary data-radio transport. Currently no-op
// because all eight Teensy hardware UARTs are ARC spokes.
void data_radio_begin(void);

// Transcode one ARC frame into the vendor format and write it to the radio.
void data_radio_emit(const arc_frame_t* frame);

// Legacy adapter signature for code that wants to emit a frame through the
// proprietary radio explicitly.
void data_radio_link_send(void* user, const arc_frame_t* frame);

// Number of frames transcoded so far (for diagnostics / OLED).
uint32_t data_radio_tx_count(void);

#endif  // DATA_RADIO_H
