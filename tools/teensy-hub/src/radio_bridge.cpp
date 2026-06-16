#ifdef HUB_SERIAL_BRIDGE

#include <Arduino.h>

#include "hub_config.h"

#ifndef HUB_BRIDGE_SERIAL
#define HUB_BRIDGE_SERIAL HUB_SERIAL_SPOKE1
#endif

#ifndef HUB_BRIDGE_BAUD
#define HUB_BRIDGE_BAUD HUB_LINK_BAUD
#endif

#ifndef HUB_BRIDGE_LABEL
#define HUB_BRIDGE_LABEL "serial1"
#endif

static uint32_t g_usb_to_radio = 0;
static uint32_t g_radio_to_usb = 0;

static void pump_radio_to_usb() {
    while (HUB_BRIDGE_SERIAL.available()) {
        int c = HUB_BRIDGE_SERIAL.read();
        if (c < 0) break;
        Serial.write((uint8_t)c);
        g_radio_to_usb++;
    }
}

static void print_status() {
    Serial.print(F("\r\n[bridge] up="));
    Serial.print(millis() / 1000UL);
    Serial.print(F("s usb->"));
    Serial.print(HUB_BRIDGE_LABEL);
    Serial.print('=');
    Serial.print(g_usb_to_radio);
    Serial.print(F(" "));
    Serial.print(HUB_BRIDGE_LABEL);
    Serial.print(F("->usb="));
    Serial.println(g_radio_to_usb);
}

void setup() {
    Serial.begin(HUB_USB_BAUD);
    HUB_BRIDGE_SERIAL.begin(HUB_BRIDGE_BAUD);

    uint32_t start = millis();
    while (!Serial && millis() - start < 1500) { /* brief wait for USB */ }

    Serial.println();
    Serial.print(F("ARC hub serial bridge | USB <-> "));
    Serial.print(HUB_BRIDGE_LABEL);
    Serial.print(F(" | baud="));
    Serial.println(HUB_BRIDGE_BAUD);
    Serial.println(F("raw bytes pass both ways; send Ctrl+T for counters"));
}

void loop() {
    while (Serial.available()) {
        int c = Serial.read();
        if (c < 0) break;
        if (c == 0x14) {
            print_status();
            continue;
        }
        HUB_BRIDGE_SERIAL.write((uint8_t)c);
        g_usb_to_radio++;
    }
    pump_radio_to_usb();
}

#endif  // HUB_SERIAL_BRIDGE
