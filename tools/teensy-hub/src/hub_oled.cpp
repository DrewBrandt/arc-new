// hub_oled.cpp -- see hub_oled.h.

#include "hub_oled.h"
#include "hub_config.h"
#include "hub_store.h"
#include "hub_links.h"
#include "data_radio.h"

#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

static Adafruit_SSD1306 g_display(HUB_OLED_WIDTH, HUB_OLED_HEIGHT, &Wire, -1);
static bool     g_ok = false;
static uint8_t  g_page = 0;
static uint32_t g_last_redraw_ms = 0;
static uint32_t g_last_page_ms = 0;

static constexpr uint8_t HUB_OLED_PAGES = 3;

bool hub_oled_begin(void) {
    Wire.begin();
    g_ok = g_display.begin(SSD1306_SWITCHCAPVCC, HUB_OLED_I2C_ADDR);
    if (g_ok) {
        g_display.clearDisplay();
        g_display.setTextSize(1);
        g_display.setTextColor(SSD1306_WHITE);
        g_display.setCursor(0, 0);
        g_display.println(F("ARC HUB"));
        g_display.println(F("booting..."));
        g_display.display();
    }
    return g_ok;
}

static char online_mark(bool up) { return up ? 'o' : 'x'; }

static void draw_links(uint32_t now_ms) {
    g_display.setCursor(0, 0);
    g_display.print(F("ARC HUB   up:"));
    g_display.print(now_ms / 1000UL);
    g_display.println('s');

    g_display.print(F("pi5:"));
    g_display.print(online_mark(hub_links_online(HUB_LINK_PI5, now_ms, HUB_PEER_TIMEOUT_MS)));
    g_display.print(F(" fc:"));
    g_display.print(online_mark(hub_links_online(HUB_LINK_FC, now_ms, HUB_PEER_TIMEOUT_MS)));
    g_display.print(F(" pwr:"));
    g_display.print(online_mark(hub_links_online(HUB_LINK_POWER, now_ms, HUB_PEER_TIMEOUT_MS)));
    g_display.print(F(" rc:"));
    g_display.println(online_mark(hub_links_online(HUB_LINK_RADIO_CMD, now_ms, HUB_PEER_TIMEOUT_MS)));
}

static void draw_peers(uint32_t now_ms) {
    g_display.setCursor(0, 0);
    g_display.print(F("peers online: "));
    g_display.println(hub_store_online_count(now_ms, HUB_PEER_TIMEOUT_MS));

    if (hub_store_have_radio_vitals()) {
        g_display.print(F("rssi:"));
        g_display.print((int)hub_store_radio_rssi());
        g_display.print(F(" snr:"));
        g_display.println((int)hub_store_radio_snr());
    } else {
        g_display.println(F("radio: no status"));
    }
}

static void draw_throughput(uint32_t now_ms) {
    g_display.setCursor(0, 0);
    g_display.print(F("fps:"));
    g_display.print(hub_store_frames_per_sec(now_ms));
    g_display.print(F(" tot:"));
    g_display.println(hub_store_total_frames());

    g_display.print(F("err:"));
    g_display.print(hub_store_error_count());
    if (hub_store_have_power_vitals()) {
        g_display.print(F(" bus:"));
        g_display.print(hub_store_power_bus_mv() / 1000.0f, 1);
        g_display.println('V');
    } else {
        g_display.println(F(" bus:--"));
    }

    g_display.print(F("data-tx:"));
    g_display.println(data_radio_tx_count());
}

void hub_oled_tick(uint32_t now_ms) {
    if (!g_ok) return;

    if (now_ms - g_last_page_ms >= HUB_OLED_PAGE_MS) {
        g_last_page_ms = now_ms;
        g_page = (uint8_t)((g_page + 1) % HUB_OLED_PAGES);
    }
    if (now_ms - g_last_redraw_ms < HUB_OLED_REDRAW_MS) return;
    g_last_redraw_ms = now_ms;

    g_display.clearDisplay();
    switch (g_page) {
        case 0: draw_links(now_ms); break;
        case 1: draw_peers(now_ms); break;
        default: draw_throughput(now_ms); break;
    }
    g_display.display();
}
