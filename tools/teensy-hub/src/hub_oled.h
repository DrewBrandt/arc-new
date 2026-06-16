// hub_oled.h
// 0.91" SSD1306 (128x32, I2C 0x3C on Wire) quick-look status display.
//
// Rotates through a few pages: spoke link state, peer/radio vitals, and
// throughput/power. Reads everything from hub_store, hub_links, and
// data_radio; it owns no protocol state of its own.

#ifndef HUB_OLED_H
#define HUB_OLED_H

#include <Arduino.h>

// Returns false if the panel did not ACK on I2C (firmware keeps running).
bool hub_oled_begin(void);

// Call every loop; redraws on its own cadence and rotates pages.
void hub_oled_tick(uint32_t now_ms);

// Force a visible test pattern for a few seconds. Useful when the panel ACKs on
// I2C but appears visually blank.
void hub_oled_self_test(uint32_t hold_ms);

// Print detected I2C addresses on the OLED bus.
void hub_oled_scan_i2c(Print& out);

#endif  // HUB_OLED_H
