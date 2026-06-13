// hub_config.h
// Wiring, addressing, and timing for the ARC Teensy hub.
//
// The Teensy 4.1 sits at the center of the nosecone. Each direct spoke is a
// hardware UART; everything off-nosecone is forwarded to the Pi 5 gateway.
//
// ----------------------------------------------------------------------
// UART spoke assignment (Teensy 4.1 has Serial1..Serial8)
// ----------------------------------------------------------------------
//   Serial1  -> pi-5-nose   (Controller 0x10; also the off-nosecone gateway)
//   Serial2  -> FC-N        (0x02)
//   Serial3  -> ARCH-Mega-N (power, 0x30)
//   Serial4  -> command radio (RADIO_CMD 0x20; RF link to the ground station)
//   Serial5  -> data radio  (RADIO_DATA 0x22; proprietary, TX-only transcode)
//
// The OLED is a 0.91" SSD1306 (128x32) on I2C (Wire: SDA 18 / SCL 19) at 0x3C.

#ifndef HUB_CONFIG_H
#define HUB_CONFIG_H

#include <Arduino.h>
#include "arc_protocol.h"

static constexpr uint8_t  HUB_ADDR        = ARC_ADDR_TEENSY_HUB;  // 0x05
static constexpr uint32_t HUB_USB_BAUD    = 115200;  // USB debug console
static constexpr uint32_t HUB_LINK_BAUD   = 115200;  // all ARC UART spokes
static constexpr uint32_t HUB_DATA_BAUD   = 115200;  // data-radio UART

// Serial ports per spoke. Change here if the harness is wired differently.
#define HUB_SERIAL_PI5        Serial1
#define HUB_SERIAL_FC         Serial2
#define HUB_SERIAL_POWER      Serial3
#define HUB_SERIAL_RADIO_CMD  Serial4
#define HUB_SERIAL_DATA_RADIO Serial5

// Self-generated traffic + liveness timing.
static constexpr uint32_t HUB_HEARTBEAT_MS = 1000;  // broadcast our heartbeat
static constexpr uint32_t HUB_POLL_MS      = 2000;  // poll radio/power status
static constexpr uint32_t HUB_PEER_TIMEOUT_MS = 3000;  // peer considered offline

// OLED display.
static constexpr uint8_t  HUB_OLED_I2C_ADDR = 0x3C;
static constexpr uint8_t  HUB_OLED_WIDTH    = 128;
static constexpr uint8_t  HUB_OLED_HEIGHT   = 32;
static constexpr uint32_t HUB_OLED_REDRAW_MS = 300;   // redraw cadence (~3 Hz)
static constexpr uint32_t HUB_OLED_PAGE_MS   = 3000;  // page rotation cadence

#endif  // HUB_CONFIG_H
