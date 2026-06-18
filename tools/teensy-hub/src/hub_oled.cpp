// hub_oled.cpp -- see hub_oled.h.

#include "hub_oled.h"
#include "hub_config.h"
#include "hub_store.h"
#include "hub_links.h"
#include "data_radio.h"

#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

static Adafruit_SSD1306 g_display0(HUB_OLED_WIDTH, HUB_OLED_HEIGHT, &Wire, -1);
static Adafruit_SSD1306 g_display1(HUB_OLED_WIDTH, HUB_OLED_HEIGHT, &Wire1, -1);
static Adafruit_SSD1306 g_display2(HUB_OLED_WIDTH, HUB_OLED_HEIGHT, &Wire2, -1);
static Adafruit_SSD1306* g_display = &g_display0;
static TwoWire* g_wire = &Wire;
static const char* g_wire_name = "Wire";
static uint8_t  g_oled_addr = HUB_OLED_I2C_ADDR;
static bool     g_ok = false;
static uint8_t  g_page = 0;
static uint32_t g_last_redraw_ms = 0;
static uint32_t g_last_page_ms = 0;
static uint32_t g_test_until_ms = 0;
static uint32_t g_last_health_ms = 0;

static constexpr uint8_t  HUB_OLED_PAGES = 3;
// How often to retry detecting a panel that is absent/disconnected. Each retry
// is a single bounded I2C probe, so a missing OLED never stalls routing.
static constexpr uint32_t HUB_OLED_RECOVER_MS = 3000;

static bool probe_addr(TwoWire& bus, uint8_t addr) {
    bus.beginTransmission(addr);
    return bus.endTransmission() == 0;
}

static bool select_oled_bus(void) {
    struct Candidate {
        TwoWire* bus;
        Adafruit_SSD1306* display;
        const char* name;
    };
    Candidate candidates[] = {
        {&Wire,  &g_display0, "Wire"},
        {&Wire1, &g_display1, "Wire1"},
        {&Wire2, &g_display2, "Wire2"},
    };
    const uint8_t addrs[] = {HUB_OLED_I2C_ADDR, 0x3D};

    for (Candidate& candidate : candidates) {
        candidate.bus->begin();
        candidate.bus->setClock(100000);
        delay(5);
        for (uint8_t addr : addrs) {
            if (probe_addr(*candidate.bus, addr)) {
                g_wire = candidate.bus;
                g_display = candidate.display;
                g_wire_name = candidate.name;
                g_oled_addr = addr;
                return true;
            }
        }
    }
    g_wire = &Wire;
    g_display = &g_display0;
    g_wire_name = "Wire";
    g_oled_addr = HUB_OLED_I2C_ADDR;
    return false;
}

// Drive the panel on the already-selected bus through its init + splash.
// Returns false if it does not come up so the caller can keep running headless.
static bool init_panel(void) {
    if (!g_display->begin(SSD1306_SWITCHCAPVCC, g_oled_addr, false)) return false;

    g_display->ssd1306_command(SSD1306_DISPLAYON);
    g_display->ssd1306_command(SSD1306_NORMALDISPLAY);
    g_display->ssd1306_command(SSD1306_SETCONTRAST);
    g_display->ssd1306_command(0xFF);

    g_display->clearDisplay();
    g_display->fillRect(0, 0, HUB_OLED_WIDTH, HUB_OLED_HEIGHT, SSD1306_WHITE);
    g_display->display();
    delay(600);

    g_display->clearDisplay();
    g_display->setTextSize(1);
    g_display->setTextColor(SSD1306_WHITE);
    g_display->setCursor(0, 0);
    g_display->println(F("ARC HUB"));
    g_display->println(F("booting..."));
    g_display->display();
    return true;
}

bool hub_oled_begin(void) {
    g_last_health_ms = millis();
    g_ok = select_oled_bus() && init_panel();
    return g_ok;
}

void hub_oled_self_test(uint32_t hold_ms) {
    if (!g_ok) return;
    g_display->ssd1306_command(SSD1306_DISPLAYON);
    g_display->ssd1306_command(SSD1306_NORMALDISPLAY);
    g_display->ssd1306_command(SSD1306_SETCONTRAST);
    g_display->ssd1306_command(0xFF);

    g_display->clearDisplay();
    g_display->fillRect(0, 0, HUB_OLED_WIDTH, HUB_OLED_HEIGHT, SSD1306_WHITE);
    g_display->drawRect(0, 0, HUB_OLED_WIDTH, HUB_OLED_HEIGHT, SSD1306_BLACK);
    g_display->setTextSize(1);
    g_display->setTextColor(SSD1306_BLACK, SSD1306_WHITE);
    g_display->setCursor(12, 12);
    g_display->print(F("OLED TEST"));
    g_display->display();
    g_test_until_ms = millis() + hold_ms;
}

void hub_oled_scan_i2c(Print& out) {
    struct Bus {
        TwoWire* bus;
        const char* name;
    };
    Bus buses[] = {
        {&Wire,  "Wire"},
        {&Wire1, "Wire1"},
        {&Wire2, "Wire2"},
    };
    for (Bus& bus : buses) {
        bus.bus->begin();
        bus.bus->setClock(100000);
        out.print(F("[oled] "));
        out.print(bus.name);
        out.print(F(" scan:"));
        bool any = false;
        for (uint8_t addr = 1; addr < 0x7F; addr++) {
            if (probe_addr(*bus.bus, addr)) {
                any = true;
                out.print(F(" 0x"));
                if (addr < 0x10) out.print('0');
                out.print(addr, HEX);
            }
        }
        if (!any) out.print(F(" none"));
        out.println();
    }
    out.print(F("[oled] selected "));
    out.print(g_wire_name);
    out.print(F(" addr=0x"));
    if (g_oled_addr < 0x10) out.print('0');
    out.print(g_oled_addr, HEX);
    out.print(F(" ok="));
    out.println(g_ok ? F("1") : F("0"));
}

static char online_mark(bool up) { return up ? 'o' : 'x'; }

static void draw_links(uint32_t now_ms) {
    g_display->setCursor(0, 0);
    g_display->print(F("ARC HUB   up:"));
    g_display->print(now_ms / 1000UL);
    g_display->println('s');

    g_display->print(F("s2:"));
    g_display->print(online_mark(hub_links_online(HUB_LINK_SPOKE1, now_ms, HUB_PEER_TIMEOUT_MS)));
    g_display->print(F(" s3:"));
    g_display->print(online_mark(hub_links_online(HUB_LINK_SPOKE2, now_ms, HUB_PEER_TIMEOUT_MS)));
    g_display->print(F(" s4:"));
    g_display->print(online_mark(hub_links_online(HUB_LINK_SPOKE3, now_ms, HUB_PEER_TIMEOUT_MS)));
    g_display->print(F(" s5:"));
    g_display->println(online_mark(hub_links_online(HUB_LINK_SPOKE4, now_ms, HUB_PEER_TIMEOUT_MS)));

    g_display->print(F("s6:"));
    g_display->print(online_mark(hub_links_online(HUB_LINK_SPOKE5, now_ms, HUB_PEER_TIMEOUT_MS)));
    g_display->print(F(" s7:"));
    g_display->print(online_mark(hub_links_online(HUB_LINK_SPOKE6, now_ms, HUB_PEER_TIMEOUT_MS)));
    g_display->print(F(" s8:"));
    g_display->print(online_mark(hub_links_online(HUB_LINK_SPOKE7, now_ms, HUB_PEER_TIMEOUT_MS)));
    g_display->println(F(" tx1"));
}

static void draw_peers(uint32_t now_ms) {
    g_display->setCursor(0, 0);
    g_display->print(F("peers online: "));
    g_display->println(hub_store_online_count(now_ms, HUB_PEER_TIMEOUT_MS));

    if (hub_store_have_radio_vitals()) {
        g_display->print(F("rssi:"));
        g_display->print((int)hub_store_radio_rssi());
        g_display->print(F(" snr:"));
        g_display->println((int)hub_store_radio_snr());
    } else {
        g_display->println(F("radio: no status"));
    }
}

static void draw_throughput(uint32_t now_ms) {
    g_display->setCursor(0, 0);
    g_display->print(F("fps:"));
    g_display->print(hub_store_frames_per_sec(now_ms));
    g_display->print(F(" tot:"));
    g_display->println(hub_store_total_frames());

    g_display->print(F("err:"));
    g_display->print(hub_store_error_count());
    if (hub_store_have_power_vitals()) {
        g_display->print(F(" bus:"));
        g_display->print(hub_store_power_bus_mv() / 1000.0f, 1);
        g_display->println('V');
    } else {
        g_display->println(F(" bus:--"));
    }

    g_display->print(F("data-tx:"));
    g_display->println(data_radio_tx_count());
}

void hub_oled_tick(uint32_t now_ms) {
    // No panel right now: occasionally re-probe so one that gets (re)connected
    // after boot comes back on its own. A single bounded I2C probe -- it can
    // never stall the router the way pushing a framebuffer to a dead panel would.
    if (!g_ok) {
        if (now_ms - g_last_health_ms >= HUB_OLED_RECOVER_MS) {
            g_last_health_ms = now_ms;
            if (probe_addr(*g_wire, g_oled_addr)) g_ok = init_panel();
        }
        return;
    }

    if ((int32_t)(now_ms - g_test_until_ms) < 0) return;

    if (now_ms - g_last_page_ms >= HUB_OLED_PAGE_MS) {
        g_last_page_ms = now_ms;
        g_page = (uint8_t)((g_page + 1) % HUB_OLED_PAGES);
    }
    if (now_ms - g_last_redraw_ms < HUB_OLED_REDRAW_MS) return;
    g_last_redraw_ms = now_ms;

    // Before pushing the framebuffer (~32 I2C writes), confirm the panel still
    // ACKs. A loose/unplugged OLED would otherwise block the loop ~1-2s per
    // redraw while every write times out -- enough to overflow the UART RX FIFOs
    // and choke frame routing. Drop it instead; the recovery probe re-adopts it.
    if (!probe_addr(*g_wire, g_oled_addr)) {
        g_ok = false;
        g_last_health_ms = now_ms;
        return;
    }

    g_display->clearDisplay();
    switch (g_page) {
        case 0: draw_links(now_ms); break;
        case 1: draw_peers(now_ms); break;
        default: draw_throughput(now_ms); break;
    }
    g_display->display();
}
