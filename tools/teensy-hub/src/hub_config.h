// hub_config.h
// Wiring, addressing, and timing for the ARC Teensy hub.
//
// The Teensy 4.1 sits at the center of the nosecone. Each direct spoke is a
// hardware UART. ARC nodes announce themselves with heartbeats/source traffic,
// so node addresses are learned at runtime instead of bound to fixed ports.
//
// ----------------------------------------------------------------------
// UART spoke assignment (Teensy 4.1 has Serial1..Serial8)
// ----------------------------------------------------------------------
//   Serial1..Serial8 -> ARC UART spokes. Device identity is learned.
//
// The OLED is a 0.91" SSD1306 (128x32) on I2C (Wire: SDA 18 / SCL 19) at 0x3C.

#ifndef HUB_CONFIG_H
#define HUB_CONFIG_H

#include <Arduino.h>
#include "arc_protocol.h"

static constexpr uint8_t  HUB_ADDR        = ARC_ADDR_TEENSY_HUB;  // 0x05
static constexpr uint32_t HUB_USB_BAUD    = 115200;  // USB debug console
static constexpr uint32_t HUB_LINK_BAUD   = 115200;  // all ARC UART spokes

// ARC spoke ports. These names are physical slots, not node identities.
#define HUB_SERIAL_SPOKE1     Serial1
#define HUB_SERIAL_SPOKE2     Serial2
#define HUB_SERIAL_SPOKE3     Serial3
#define HUB_SERIAL_SPOKE4     Serial4
#define HUB_SERIAL_SPOKE5     Serial5
#define HUB_SERIAL_SPOKE6     Serial6
#define HUB_SERIAL_SPOKE7     Serial7
#define HUB_SERIAL_SPOKE8     Serial8

// Self-generated traffic + liveness timing.
static constexpr uint32_t HUB_HEARTBEAT_MS = 5000;  // broadcast our heartbeat
static constexpr uint32_t HUB_PEER_TIMEOUT_MS = 15000;  // peer considered offline

// OLED display.
static constexpr uint8_t  HUB_OLED_I2C_ADDR = 0x3C;
static constexpr uint8_t  HUB_OLED_WIDTH    = 128;
static constexpr uint8_t  HUB_OLED_HEIGHT   = 32;
static constexpr uint32_t HUB_OLED_REDRAW_MS = 300;   // redraw cadence (~3 Hz)
static constexpr uint32_t HUB_OLED_PAGE_MS   = 3000;  // page rotation cadence

#endif  // HUB_CONFIG_H
