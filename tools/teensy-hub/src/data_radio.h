// data_radio.h
// Raw payload downlink for the live external telemetry radio.
//
// This radio does NOT speak ARC. Its packet format is owned by a third party
// who will not change it, and it cannot act as a network-wide messaging
// transport. This module is intentionally not part of the learned ARC UART
// router. The hub writes only the ARC payload bytes plus a trailing null byte,
// without ARC headers or COBS framing, to Serial1.

#ifndef DATA_RADIO_H
#define DATA_RADIO_H

#include <Arduino.h>
#include "arc_protocol.h"

// Open the fixed non-ARC telemetry UART.
void data_radio_begin(void);

// Write one ARC frame's payload bytes plus a null terminator to the radio.
void data_radio_emit(const arc_frame_t* frame);

// Legacy adapter signature for code that wants to emit a frame through the
// proprietary radio explicitly.
void data_radio_link_send(void* user, const arc_frame_t* frame);

// Number of non-empty payloads sent so far (for diagnostics / OLED).
uint32_t data_radio_tx_count(void);

#endif  // DATA_RADIO_H
