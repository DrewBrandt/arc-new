// ARC fake-FC-N bridge for Teensy 4.1.
//
// Pretends to be FC-N (address 0x02): keeps the Pi's PeerHealth happy
// with periodic heartbeats and translates plain-text commands typed in
// the USB serial monitor into ARC frames over the hardware UART.
// Frames coming back from the Controller are pretty-printed.
//
// ----------------------------------------------------------------------
// Wiring (Teensy 4.1 <-> Raspberry Pi)
// ----------------------------------------------------------------------
//   Teensy pin 1 (Serial1 TX) -> Pi GPIO 15 / RXD0 (header pin 10)
//   Teensy pin 0 (Serial1 RX) <- Pi GPIO 14 / TXD0 (header pin 8)
//   Teensy GND                <-> Pi GND (any GND pin)
// Both boards are 3.3 V on these pins; do NOT bridge 5 V or 3.3 V.
// Power each board separately via its own USB connection.
//
// ----------------------------------------------------------------------
// Pi side prerequisites
// ----------------------------------------------------------------------
//   sudo ./setup.sh controller       (already done on arcpi1)
// The setup script frees /dev/serial0, disables Bluetooth on the PL011,
// and configures arc-controller to listen on it at 115200 8N1.
//
// ----------------------------------------------------------------------
// Commands typed in the USB serial monitor
// ----------------------------------------------------------------------
//   help                                   this list
//   heartbeat | hb                         send NETMGMT HEARTBEAT now
//
// Video / Controller (FC_VIDEO):
//   layout <id>                            SET_LAYOUT (numeric, 0..255)
//   source <slot> <name|addr>              SET_SOURCE
//                                          name: empty | local | sender-c | sender-l1 | sender-l2 | sender-n | sender-ground
//   overlay <text>                         SET_OVERLAY (rest of line)
//   status                                 GET_STATUS
//
// Radio (r=rocket=0x20, g=ground=0x21):
//   freq <r|g> <hz>                        SET_FREQUENCY
//   txpower <r|g> <dbm>                    SET_TX_POWER (signed dBm)
//   radiostatus <r|g>                      GET_STATUS
//
// Power (n=nose=0x30, l=lower=0x31):
//   out <n|l> <chan> <on|off>              SET_OUTPUT
//   outmask <n|l> <enable> <state>         SET_OUTPUT_MASK (1-byte hex masks)
//   powerstatus <n|l>                      GET_STATUS
//
// Bridge:
//   session <n>                            override the SESSION byte (forces dedup reset on the Pi)
//   quiet | listen                         toggle incoming-frame printing
//
// Reliable commands are sent with the RELIABLE flag, so the Controller
// will ACK them. The bridge prints "ACK ... rtt=..ms" on receipt, or
// "NO-ACK ..." if no ACK arrives within ACK_TIMEOUT_MS.

#include <Arduino.h>
#include <strings.h>  // strcasecmp
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "arc_protocol.h"
#include "arc_messages_netmgmt.h"
#include "arc_messages_video.h"
#include "arc_messages_fc_video.h"
#include "arc_messages_radio.h"
#include "arc_messages_power.h"

// ----------------------------------------------------------------------
// Configuration
// ----------------------------------------------------------------------
static constexpr uint8_t  MY_ADDR               = ARC_ADDR_FC_N;
static constexpr uint8_t  CONTROLLER_ADDR       = ARC_ADDR_CONTROLLER;
static constexpr uint32_t USB_BAUD              = 115200;
static constexpr uint32_t LINK_BAUD             = 115200;
static constexpr uint32_t HEARTBEAT_INTERVAL_MS = 1000;
static constexpr uint32_t ACK_TIMEOUT_MS        = 1000;
#define LINK Serial1

// ----------------------------------------------------------------------
// Per-boot state
// ----------------------------------------------------------------------
static uint8_t  g_session     = 1;
static uint16_t g_next_seq    = 0;
static bool     g_listen_mode = true;
static uint32_t g_last_heartbeat_ms = 0;

struct PendingAck {
  bool        waiting   = false;
  uint16_t    seq       = 0;
  uint32_t    sent_ms   = 0;
  const char* tag       = "";
};
static PendingAck g_pending;

// ----------------------------------------------------------------------
// Buffers
// ----------------------------------------------------------------------
static constexpr size_t CMD_BUF_SIZE = 240;
static char             g_cmd_buf[CMD_BUF_SIZE];
static size_t           g_cmd_len = 0;

static constexpr size_t RX_BUF_SIZE = ARC_MAX_ENCODED_SIZE + 4;
static uint8_t          g_rx_buf[RX_BUF_SIZE];
static size_t           g_rx_len = 0;

// ----------------------------------------------------------------------
// Tiny helpers
// ----------------------------------------------------------------------
static void print_hex(uint8_t b) {
  if (b < 0x10) Serial.print('0');
  Serial.print(b, HEX);
}

struct SenderAlias { const char* name; uint8_t addr; };
static const SenderAlias kAliases[] = {
  {"empty",          ARC_ADDR_UNASSIGNED},
  {"off",            ARC_ADDR_UNASSIGNED},
  {"none",           ARC_ADDR_UNASSIGNED},
  {"local",          ARC_ADDR_CONTROLLER},
  {"controller",     ARC_ADDR_CONTROLLER},
  {"sender-n",       ARC_ADDR_SENDER_N},
  {"sender-c",       ARC_ADDR_SENDER_C},
  {"sender-l1",      ARC_ADDR_SENDER_L1},
  {"sender-l2",      ARC_ADDR_SENDER_L2},
  {"sender-ground",  ARC_ADDR_SENDER_GROUND},
  // Radios
  {"radio-r",        ARC_ADDR_RADIO_R},
  {"rocket-radio",   ARC_ADDR_RADIO_R},
  {"radio-g",        ARC_ADDR_RADIO_G},
  {"ground-radio",   ARC_ADDR_RADIO_G},
  // Power boards
  {"arch-mega-n",    ARC_ADDR_ARCH_MEGA_N},
  {"arch-n",         ARC_ADDR_ARCH_MEGA_N},
  {"nose-power",     ARC_ADDR_ARCH_MEGA_N},
  {"arch-mega-l",    ARC_ADDR_ARCH_MEGA_L},
  {"arch-l",         ARC_ADDR_ARCH_MEGA_L},
  {"lower-power",    ARC_ADDR_ARCH_MEGA_L},
};

static bool resolve_radio(const char* arg, uint8_t* out) {
  if (!arg) return false;
  if (strcasecmp(arg, "r") == 0 || strcasecmp(arg, "rocket") == 0) {
    *out = ARC_ADDR_RADIO_R; return true;
  }
  if (strcasecmp(arg, "g") == 0 || strcasecmp(arg, "ground") == 0) {
    *out = ARC_ADDR_RADIO_G; return true;
  }
  for (const auto& a : kAliases) {
    if (strcasecmp(arg, a.name) == 0
        && (a.addr == ARC_ADDR_RADIO_R || a.addr == ARC_ADDR_RADIO_G)) {
      *out = a.addr; return true;
    }
  }
  char* end = nullptr;
  long v = strtol(arg, &end, 0);
  if (end && *end == '\0' && (v == ARC_ADDR_RADIO_R || v == ARC_ADDR_RADIO_G)) {
    *out = (uint8_t)v; return true;
  }
  return false;
}

static bool resolve_power_board(const char* arg, uint8_t* out) {
  if (!arg) return false;
  if (strcasecmp(arg, "n") == 0 || strcasecmp(arg, "nose") == 0
      || strcasecmp(arg, "nosecone") == 0) {
    *out = ARC_ADDR_ARCH_MEGA_N; return true;
  }
  if (strcasecmp(arg, "l") == 0 || strcasecmp(arg, "lower") == 0) {
    *out = ARC_ADDR_ARCH_MEGA_L; return true;
  }
  for (const auto& a : kAliases) {
    if (strcasecmp(arg, a.name) == 0
        && (a.addr == ARC_ADDR_ARCH_MEGA_N || a.addr == ARC_ADDR_ARCH_MEGA_L)) {
      *out = a.addr; return true;
    }
  }
  char* end = nullptr;
  long v = strtol(arg, &end, 0);
  if (end && *end == '\0' && (v == ARC_ADDR_ARCH_MEGA_N || v == ARC_ADDR_ARCH_MEGA_L)) {
    *out = (uint8_t)v; return true;
  }
  return false;
}

static bool resolve_source(const char* arg, uint8_t* out) {
  if (!arg) return false;
  for (const auto& a : kAliases) {
    if (strcasecmp(arg, a.name) == 0) { *out = a.addr; return true; }
  }
  char* end = nullptr;
  long v = strtol(arg, &end, 0);
  if (end && *end == '\0' && v >= 0 && v <= 0xFF) { *out = (uint8_t)v; return true; }
  return false;
}

static const char* source_name(uint8_t addr) {
  for (const auto& a : kAliases) {
    if (a.addr == addr) return a.name;
  }
  static char buf[6];
  snprintf(buf, sizeof(buf), "0x%02X", addr);
  return buf;
}

static const char* family_name(uint8_t fam) {
  switch (fam) {
    case ARC_FAMILY_NETMGMT:  return "NETMGMT";
    case ARC_FAMILY_FC_COORD: return "FC_COORD";
    case ARC_FAMILY_VIDEO:    return "VIDEO";
    case ARC_FAMILY_FC_VIDEO: return "FC_VIDEO";
    case ARC_FAMILY_RADIO:    return "RADIO";
    case ARC_FAMILY_POWER:    return "POWER";
    default:                  return "?";
  }
}

// ----------------------------------------------------------------------
// Frame TX
// ----------------------------------------------------------------------
static bool send_frame_to(uint8_t dst, uint8_t flags, uint8_t family, uint8_t type,
                          const uint8_t* payload, size_t payload_len,
                          uint16_t* out_seq, const char* tag) {
  uint8_t frame[ARC_MAX_FRAME_SIZE];
  uint16_t seq = g_next_seq++;
  int n = arc_frame_build(frame, sizeof(frame),
                          MY_ADDR, dst,
                          flags, g_session, seq,
                          family, type,
                          payload, payload_len);
  if (n < 0) {
    Serial.print(F("ERR build="));
    Serial.println(n);
    return false;
  }
  uint8_t encoded[ARC_MAX_ENCODED_SIZE];
  int m = arc_cobs_encode(frame, n, encoded, sizeof(encoded));
  if (m < 0) {
    Serial.print(F("ERR cobs="));
    Serial.println(m);
    return false;
  }
  LINK.write(encoded, m);
  if (out_seq) *out_seq = seq;
  if (flags & ARC_FLAG_RELIABLE) {
    g_pending.waiting = true;
    g_pending.seq     = seq;
    g_pending.sent_ms = millis();
    g_pending.tag     = tag;
  }
  return true;
}

// Default destination = Controller, used by the FC_VIDEO and NETMGMT
// commands that always target the Controller.
static bool send_frame(uint8_t flags, uint8_t family, uint8_t type,
                       const uint8_t* payload, size_t payload_len,
                       uint16_t* out_seq, const char* tag) {
  return send_frame_to(CONTROLLER_ADDR, flags, family, type,
                       payload, payload_len, out_seq, tag);
}

// ----------------------------------------------------------------------
// Pretty printers
// ----------------------------------------------------------------------
static void print_video_status(const arc_frame_t* f) {
  arc_video_status_report_t r;
  if (arc_video_status_report_decode(f->payload, f->payload_len, &r) != ARC_OK) {
    Serial.println(F("[VIDEO STATUS_REPORT (bad payload)]"));
    return;
  }
  Serial.print(F("[VIDEO STATUS_REPORT] state=0x"));
  print_hex(r.state);
  Serial.print(F(" cpu="));   Serial.print(r.cpu_temp_c);   Serial.print(F("C"));
  Serial.print(F(" load="));  Serial.print(r.cpu_load_pct); Serial.print(F("%"));
  Serial.print(F(" disk="));  Serial.print(r.free_disk_mb); Serial.print(F("MB"));
  Serial.print(F(" rssi="));  Serial.print((int)r.rssi_dbm);
  Serial.print(F(" tx="));    Serial.print(r.tx_frames);
  Serial.print(F(" drop="));  Serial.println(r.dropped_frames);
}

static void print_fc_video_status(const arc_frame_t* f) {
  arc_fc_video_status_report_t r;
  if (arc_fc_video_status_report_decode(f->payload, f->payload_len, &r) != ARC_OK) {
    Serial.println(F("[FC_VIDEO STATUS_REPORT (bad payload)]"));
    return;
  }
  Serial.print(F("[FC_VIDEO STATUS_REPORT] slots=["));
  for (uint8_t i = 0; i < r.slot_count; i++) {
    if (i) Serial.print(',');
    Serial.print(source_name(r.slots[i]));
  }
  Serial.print(F("] senders=["));
  for (uint8_t i = 0; i < r.sender_count; i++) {
    if (i) Serial.print(',');
    Serial.print(source_name(r.senders[i].addr));
    Serial.print(':');
    bool any = false;
    if (r.senders[i].flags & ARC_FC_VIDEO_STATUS_FLAG_ONLINE) {
      Serial.print(F("ONLINE")); any = true;
    }
    if (r.senders[i].flags & ARC_FC_VIDEO_STATUS_FLAG_TRANSMITTING) {
      if (any) Serial.print('|');
      Serial.print(F("TX")); any = true;
    }
    if (r.senders[i].flags & ARC_FC_VIDEO_STATUS_FLAG_RECORDING) {
      if (any) Serial.print('|');
      Serial.print(F("REC")); any = true;
    }
    if (!any) Serial.print(F("offline"));
  }
  Serial.println(']');
}

static void print_radio_status(const arc_frame_t* f) {
  arc_radio_status_report_t r;
  if (arc_radio_status_report_decode(f->payload, f->payload_len, &r) != ARC_OK) {
    Serial.println(F("[RADIO STATUS_REPORT (bad payload)]"));
    return;
  }
  Serial.print(F("[RADIO STATUS_REPORT] freq="));
  Serial.print(r.frequency_hz); Serial.print(F("Hz"));
  Serial.print(F(" txpwr=")); Serial.print((int)r.tx_power_dbm); Serial.print(F("dBm"));
  Serial.print(F(" rssi="));  Serial.print((int)r.rssi_dbm);
  Serial.print(F(" snr="));   Serial.print((int)r.snr_db);
  if (r.error_flags) {
    Serial.print(F(" err=0x")); print_hex(r.error_flags);
  }
  Serial.print(F(" rx="));    Serial.print(r.packets_rx);
  Serial.print(F(" tx="));    Serial.println(r.packets_tx);
}

static void print_power_status(const arc_frame_t* f) {
  arc_power_status_report_t r;
  if (arc_power_status_report_decode(f->payload, f->payload_len, &r) != ARC_OK) {
    Serial.println(F("[POWER STATUS_REPORT (bad payload)]"));
    return;
  }
  Serial.print(F("[POWER STATUS_REPORT] bus="));
  Serial.print(r.bus_voltage_mv); Serial.print(F("mV"));
  Serial.print(F(" temp="));     Serial.print((int)r.temp_c); Serial.print(F("C"));
  Serial.print(F(" channels=["));
  for (uint8_t i = 0; i < r.channel_count; i++) {
    if (i) Serial.print(',');
    Serial.print(i); Serial.print(':');
    uint8_t base = r.channels[i].state & ~ARC_POWER_CHAN_FAULT_MASK;
    Serial.print(base == ARC_POWER_ON ? F("on") : F("off"));
    Serial.print('@'); Serial.print(r.channels[i].current_ma); Serial.print(F("mA"));
    if (r.channels[i].state & ARC_POWER_CHAN_FAULT_OVERCURRENT) Serial.print(F("!OC"));
    if (r.channels[i].state & ARC_POWER_CHAN_FAULT_THERMAL)     Serial.print(F("!THERM"));
  }
  Serial.println(']');
}

static void print_frame(const arc_frame_t* f) {
  Serial.print(F("RX <- 0x")); print_hex(f->src); Serial.print(' ');
  if (f->family == ARC_FAMILY_VIDEO && f->type == ARC_VIDEO_STATUS_REPORT) {
    print_video_status(f); return;
  }
  if (f->family == ARC_FAMILY_FC_VIDEO && f->type == ARC_FC_VIDEO_STATUS_REPORT) {
    print_fc_video_status(f); return;
  }
  if (f->family == ARC_FAMILY_RADIO && f->type == ARC_RADIO_STATUS_REPORT) {
    print_radio_status(f); return;
  }
  if (f->family == ARC_FAMILY_POWER && f->type == ARC_POWER_STATUS_REPORT) {
    print_power_status(f); return;
  }
  if (f->family == ARC_FAMILY_NETMGMT && f->type == ARC_NETMGMT_HEARTBEAT) {
    Serial.println(F("[NETMGMT HEARTBEAT]")); return;
  }
  Serial.print('['); Serial.print(family_name(f->family));
  Serial.print(F(" type=0x")); print_hex(f->type);
  Serial.print(F(" len="));    Serial.print(f->payload_len);
  if (f->flags) { Serial.print(F(" flags=0x")); print_hex(f->flags); }
  Serial.println(']');
}

// ----------------------------------------------------------------------
// Commands
// ----------------------------------------------------------------------
static void cmd_help() {
  Serial.println(F("Commands:"));
  Serial.println(F("  help                          this message"));
  Serial.println(F("  heartbeat | hb                send NETMGMT HEARTBEAT now"));
  Serial.println(F("Video / Controller (FC_VIDEO):"));
  Serial.println(F("  layout <id>                   SET_LAYOUT (0..255)"));
  Serial.println(F("  source <slot> <name|addr>     SET_SOURCE"));
  Serial.println(F("                                name: empty|local|sender-c|sender-l1|sender-l2|sender-n|sender-ground"));
  Serial.println(F("  overlay <text>                SET_OVERLAY (rest of line)"));
  Serial.println(F("  status                        GET_STATUS"));
  Serial.println(F("Radio (r=rocket, g=ground):"));
  Serial.println(F("  freq <r|g> <hz>               RADIO SET_FREQUENCY"));
  Serial.println(F("  txpower <r|g> <dbm>           RADIO SET_TX_POWER (signed dBm)"));
  Serial.println(F("  radiostatus <r|g>             RADIO GET_STATUS"));
  Serial.println(F("Power (n=nose, l=lower):"));
  Serial.println(F("  out <n|l> <chan> <on|off>     POWER SET_OUTPUT"));
  Serial.println(F("  outmask <n|l> <enable> <state>  POWER SET_OUTPUT_MASK (hex bytes, e.g. 0x15 0x11)"));
  Serial.println(F("  powerstatus <n|l>             POWER GET_STATUS"));
  Serial.println(F("Bridge:"));
  Serial.println(F("  session <n>                   override SESSION byte"));
  Serial.println(F("  quiet | listen                toggle incoming-frame printing"));
}

static void cmd_heartbeat() {
  uint16_t seq = 0;
  if (send_frame(0, ARC_FAMILY_NETMGMT, ARC_NETMGMT_HEARTBEAT, nullptr, 0, &seq, "heartbeat")) {
    Serial.print(F("TX -> 0x")); print_hex(CONTROLLER_ADDR);
    Serial.print(F(" [NETMGMT HEARTBEAT] seq=")); Serial.println(seq);
  }
}

static void cmd_layout(const char* arg) {
  if (!arg) { Serial.println(F("usage: layout <id>")); return; }
  long id = strtol(arg, nullptr, 0);
  if (id < 0 || id > 0xFF) { Serial.println(F("layout id must be 0..255")); return; }
  arc_fc_video_set_layout_t msg = { (uint8_t)id };
  uint8_t payload[ARC_FC_VIDEO_SET_LAYOUT_PAYLOAD_SIZE];
  arc_fc_video_set_layout_encode(&msg, payload, sizeof(payload));
  uint16_t seq = 0;
  if (send_frame(ARC_FLAG_RELIABLE, ARC_FAMILY_FC_VIDEO, ARC_FC_VIDEO_SET_LAYOUT,
                 payload, sizeof(payload), &seq, "SET_LAYOUT")) {
    Serial.print(F("TX -> 0x")); print_hex(CONTROLLER_ADDR);
    Serial.print(F(" [FC_VIDEO SET_LAYOUT] id=")); Serial.print(id);
    Serial.print(F(" seq=")); Serial.println(seq);
  }
}

static void cmd_source(const char* slot_str, const char* src_str) {
  if (!slot_str || !src_str) { Serial.println(F("usage: source <slot> <name|addr>")); return; }
  long slot = strtol(slot_str, nullptr, 0);
  if (slot < 0 || slot > 0xFF) { Serial.println(F("slot must be 0..255")); return; }
  uint8_t src = 0;
  if (!resolve_source(src_str, &src)) {
    Serial.print(F("unknown source: ")); Serial.println(src_str);
    return;
  }
  arc_fc_video_set_source_t msg = { (uint8_t)slot, src };
  uint8_t payload[ARC_FC_VIDEO_SET_SOURCE_PAYLOAD_SIZE];
  arc_fc_video_set_source_encode(&msg, payload, sizeof(payload));
  uint16_t seq = 0;
  if (send_frame(ARC_FLAG_RELIABLE, ARC_FAMILY_FC_VIDEO, ARC_FC_VIDEO_SET_SOURCE,
                 payload, sizeof(payload), &seq, "SET_SOURCE")) {
    Serial.print(F("TX -> 0x")); print_hex(CONTROLLER_ADDR);
    Serial.print(F(" [FC_VIDEO SET_SOURCE] slot=")); Serial.print(slot);
    Serial.print(F(" src=")); Serial.print(source_name(src));
    Serial.print(F(" seq=")); Serial.println(seq);
  }
}

static void cmd_overlay(const char* text) {
  if (!text) text = "";
  size_t text_len = strlen(text);
  uint8_t payload[ARC_MAX_PAYLOAD_SIZE];
  int n = arc_fc_video_set_overlay_encode(text, text_len, payload, sizeof(payload));
  if (n < 0) { Serial.println(F("overlay too long")); return; }
  uint16_t seq = 0;
  if (send_frame(ARC_FLAG_RELIABLE, ARC_FAMILY_FC_VIDEO, ARC_FC_VIDEO_SET_OVERLAY,
                 payload, (size_t)n, &seq, "SET_OVERLAY")) {
    Serial.print(F("TX -> 0x")); print_hex(CONTROLLER_ADDR);
    Serial.print(F(" [FC_VIDEO SET_OVERLAY] text=\""));
    Serial.print(text);
    Serial.print(F("\" seq=")); Serial.println(seq);
  }
}

static void cmd_status() {
  uint16_t seq = 0;
  if (send_frame(ARC_FLAG_RELIABLE, ARC_FAMILY_FC_VIDEO, ARC_FC_VIDEO_GET_STATUS,
                 nullptr, 0, &seq, "GET_STATUS")) {
    Serial.print(F("TX -> 0x")); print_hex(CONTROLLER_ADDR);
    Serial.print(F(" [FC_VIDEO GET_STATUS] seq=")); Serial.println(seq);
  }
}

static void cmd_session(const char* arg) {
  if (!arg) { Serial.println(F("usage: session <n>")); return; }
  long s = strtol(arg, nullptr, 0);
  if (s < 0 || s > 0xFF) { Serial.println(F("session must be 0..255")); return; }
  g_session = (uint8_t)s;
  Serial.print(F("session = 0x")); print_hex(g_session); Serial.println();
}

// ----------------------------------------------------------------------
// RADIO commands
// ----------------------------------------------------------------------
static void cmd_freq(const char* radio_str, const char* hz_str) {
  if (!radio_str || !hz_str) { Serial.println(F("usage: freq <r|g> <hz>")); return; }
  uint8_t dst = 0;
  if (!resolve_radio(radio_str, &dst)) {
    Serial.print(F("unknown radio: ")); Serial.println(radio_str);
    return;
  }
  char* end = nullptr;
  long hz = strtol(hz_str, &end, 0);
  if (!end || *end != '\0' || hz < 0) {
    Serial.println(F("freq must be a non-negative integer in Hz"));
    return;
  }
  arc_radio_set_frequency_t msg = { (uint32_t)hz };
  uint8_t payload[ARC_RADIO_SET_FREQUENCY_PAYLOAD_SIZE];
  arc_radio_set_frequency_encode(&msg, payload, sizeof(payload));
  uint16_t seq = 0;
  if (send_frame_to(dst, ARC_FLAG_RELIABLE, ARC_FAMILY_RADIO,
                    ARC_RADIO_SET_FREQUENCY, payload, sizeof(payload),
                    &seq, "RADIO SET_FREQUENCY")) {
    Serial.print(F("TX -> 0x")); print_hex(dst);
    Serial.print(F(" [RADIO SET_FREQUENCY] hz=")); Serial.print(hz);
    Serial.print(F(" seq=")); Serial.println(seq);
  }
}

static void cmd_txpower(const char* radio_str, const char* dbm_str) {
  if (!radio_str || !dbm_str) { Serial.println(F("usage: txpower <r|g> <dbm>")); return; }
  uint8_t dst = 0;
  if (!resolve_radio(radio_str, &dst)) {
    Serial.print(F("unknown radio: ")); Serial.println(radio_str);
    return;
  }
  long dbm = strtol(dbm_str, nullptr, 0);
  if (dbm < -128 || dbm > 127) {
    Serial.println(F("dBm must fit in int8 (-128..127)"));
    return;
  }
  arc_radio_set_tx_power_t msg = { (int8_t)dbm };
  uint8_t payload[ARC_RADIO_SET_TX_POWER_PAYLOAD_SIZE];
  arc_radio_set_tx_power_encode(&msg, payload, sizeof(payload));
  uint16_t seq = 0;
  if (send_frame_to(dst, ARC_FLAG_RELIABLE, ARC_FAMILY_RADIO,
                    ARC_RADIO_SET_TX_POWER, payload, sizeof(payload),
                    &seq, "RADIO SET_TX_POWER")) {
    Serial.print(F("TX -> 0x")); print_hex(dst);
    Serial.print(F(" [RADIO SET_TX_POWER] dbm=")); Serial.print(dbm);
    Serial.print(F(" seq=")); Serial.println(seq);
  }
}

static void cmd_radiostatus(const char* radio_str) {
  if (!radio_str) { Serial.println(F("usage: radiostatus <r|g>")); return; }
  uint8_t dst = 0;
  if (!resolve_radio(radio_str, &dst)) {
    Serial.print(F("unknown radio: ")); Serial.println(radio_str);
    return;
  }
  uint16_t seq = 0;
  if (send_frame_to(dst, ARC_FLAG_RELIABLE, ARC_FAMILY_RADIO,
                    ARC_RADIO_GET_STATUS, nullptr, 0, &seq, "RADIO GET_STATUS")) {
    Serial.print(F("TX -> 0x")); print_hex(dst);
    Serial.print(F(" [RADIO GET_STATUS] seq=")); Serial.println(seq);
  }
}

// ----------------------------------------------------------------------
// POWER commands
// ----------------------------------------------------------------------
static void cmd_out(const char* board_str, const char* chan_str, const char* state_str) {
  if (!board_str || !chan_str || !state_str) {
    Serial.println(F("usage: out <n|l> <chan> <on|off>"));
    return;
  }
  uint8_t dst = 0;
  if (!resolve_power_board(board_str, &dst)) {
    Serial.print(F("unknown power board: ")); Serial.println(board_str);
    return;
  }
  long chan = strtol(chan_str, nullptr, 0);
  if (chan < 0 || chan > 0xFF) {
    Serial.println(F("channel must be 0..255"));
    return;
  }
  uint8_t state;
  if (strcasecmp(state_str, "on") == 0)       state = ARC_POWER_ON;
  else if (strcasecmp(state_str, "off") == 0) state = ARC_POWER_OFF;
  else { Serial.println(F("state must be on|off")); return; }

  arc_power_set_output_t msg = { (uint8_t)chan, state };
  uint8_t payload[ARC_POWER_SET_OUTPUT_PAYLOAD_SIZE];
  if (arc_power_set_output_encode(&msg, payload, sizeof(payload)) < 0) {
    Serial.println(F("encode failed"));
    return;
  }
  uint16_t seq = 0;
  if (send_frame_to(dst, ARC_FLAG_RELIABLE, ARC_FAMILY_POWER,
                    ARC_POWER_SET_OUTPUT, payload, sizeof(payload),
                    &seq, "POWER SET_OUTPUT")) {
    Serial.print(F("TX -> 0x")); print_hex(dst);
    Serial.print(F(" [POWER SET_OUTPUT] chan=")); Serial.print(chan);
    Serial.print(F(" state=")); Serial.print(state == ARC_POWER_ON ? F("on") : F("off"));
    Serial.print(F(" seq=")); Serial.println(seq);
  }
}

static void cmd_outmask(const char* board_str, const char* enable_str, const char* state_str) {
  if (!board_str || !enable_str || !state_str) {
    Serial.println(F("usage: outmask <n|l> <enable> <state>  (e.g. outmask n 0x15 0x11)"));
    return;
  }
  uint8_t dst = 0;
  if (!resolve_power_board(board_str, &dst)) {
    Serial.print(F("unknown power board: ")); Serial.println(board_str);
    return;
  }
  long en = strtol(enable_str, nullptr, 0);
  long st = strtol(state_str, nullptr, 0);
  if (en < 0 || en > 0xFF || st < 0 || st > 0xFF) {
    Serial.println(F("enable and state must each fit in 1 byte"));
    return;
  }
  arc_power_set_output_mask_t msg = { (uint8_t)en, (uint8_t)st };
  uint8_t payload[ARC_POWER_SET_OUTPUT_MASK_PAYLOAD_SIZE];
  arc_power_set_output_mask_encode(&msg, payload, sizeof(payload));
  uint16_t seq = 0;
  if (send_frame_to(dst, ARC_FLAG_RELIABLE, ARC_FAMILY_POWER,
                    ARC_POWER_SET_OUTPUT_MASK, payload, sizeof(payload),
                    &seq, "POWER SET_OUTPUT_MASK")) {
    Serial.print(F("TX -> 0x")); print_hex(dst);
    Serial.print(F(" [POWER SET_OUTPUT_MASK] enable=0x")); print_hex((uint8_t)en);
    Serial.print(F(" state=0x")); print_hex((uint8_t)st);
    Serial.print(F(" seq=")); Serial.println(seq);
  }
}

static void cmd_powerstatus(const char* board_str) {
  if (!board_str) { Serial.println(F("usage: powerstatus <n|l>")); return; }
  uint8_t dst = 0;
  if (!resolve_power_board(board_str, &dst)) {
    Serial.print(F("unknown power board: ")); Serial.println(board_str);
    return;
  }
  uint16_t seq = 0;
  if (send_frame_to(dst, ARC_FLAG_RELIABLE, ARC_FAMILY_POWER,
                    ARC_POWER_GET_STATUS, nullptr, 0, &seq, "POWER GET_STATUS")) {
    Serial.print(F("TX -> 0x")); print_hex(dst);
    Serial.print(F(" [POWER GET_STATUS] seq=")); Serial.println(seq);
  }
}

// ----------------------------------------------------------------------
// Command dispatcher
// ----------------------------------------------------------------------
static void process_command(char* line) {
  while (*line == ' ' || *line == '\t') line++;
  if (*line == '\0') return;

  char* tok = strtok(line, " \t");
  if (!tok) return;

  if (strcasecmp(tok, "help") == 0 || strcmp(tok, "?") == 0) {
    cmd_help();
  } else if (strcasecmp(tok, "heartbeat") == 0 || strcasecmp(tok, "hb") == 0) {
    cmd_heartbeat();
  } else if (strcasecmp(tok, "layout") == 0) {
    cmd_layout(strtok(nullptr, " \t"));
  } else if (strcasecmp(tok, "source") == 0) {
    char* a = strtok(nullptr, " \t");
    char* b = strtok(nullptr, " \t");
    cmd_source(a, b);
  } else if (strcasecmp(tok, "overlay") == 0) {
    // Take everything after "overlay " literally so the text can have spaces.
    char* rest = strtok(nullptr, "");
    if (rest) while (*rest == ' ' || *rest == '\t') rest++;
    cmd_overlay(rest);
  } else if (strcasecmp(tok, "status") == 0) {
    cmd_status();
  } else if (strcasecmp(tok, "freq") == 0) {
    char* a = strtok(nullptr, " \t");
    char* b = strtok(nullptr, " \t");
    cmd_freq(a, b);
  } else if (strcasecmp(tok, "txpower") == 0) {
    char* a = strtok(nullptr, " \t");
    char* b = strtok(nullptr, " \t");
    cmd_txpower(a, b);
  } else if (strcasecmp(tok, "radiostatus") == 0) {
    cmd_radiostatus(strtok(nullptr, " \t"));
  } else if (strcasecmp(tok, "out") == 0) {
    char* a = strtok(nullptr, " \t");
    char* b = strtok(nullptr, " \t");
    char* c = strtok(nullptr, " \t");
    cmd_out(a, b, c);
  } else if (strcasecmp(tok, "outmask") == 0) {
    char* a = strtok(nullptr, " \t");
    char* b = strtok(nullptr, " \t");
    char* c = strtok(nullptr, " \t");
    cmd_outmask(a, b, c);
  } else if (strcasecmp(tok, "powerstatus") == 0) {
    cmd_powerstatus(strtok(nullptr, " \t"));
  } else if (strcasecmp(tok, "session") == 0) {
    cmd_session(strtok(nullptr, " \t"));
  } else if (strcasecmp(tok, "quiet") == 0) {
    g_listen_mode = false; Serial.println(F("(quiet)"));
  } else if (strcasecmp(tok, "listen") == 0) {
    g_listen_mode = true; Serial.println(F("(listening)"));
  } else {
    Serial.print(F("unknown command: ")); Serial.println(tok);
    cmd_help();
  }
}

// ----------------------------------------------------------------------
// USB-side line reader
// ----------------------------------------------------------------------
static void pump_usb() {
  while (Serial.available()) {
    int c = Serial.read();
    if (c < 0) break;
    if (c == '\r') continue;
    if (c == '\n') {
      g_cmd_buf[g_cmd_len] = '\0';
      process_command(g_cmd_buf);
      g_cmd_len = 0;
      continue;
    }
    if (g_cmd_len + 1 < CMD_BUF_SIZE) {
      g_cmd_buf[g_cmd_len++] = (char)c;
    } else {
      Serial.println(F("(line too long, dropped)"));
      g_cmd_len = 0;
    }
  }
}

// ----------------------------------------------------------------------
// UART-side COBS frame reader
// ----------------------------------------------------------------------
static void handle_decoded_frame(const uint8_t* frame_buf, int frame_len) {
  arc_frame_t f;
  arc_result_t r = arc_frame_parse(frame_buf, frame_len, &f);
  if (r != ARC_OK) {
    if (g_listen_mode) {
      Serial.print(F("RX (parse err ")); Serial.print(r); Serial.println(')');
    }
    return;
  }

  // ACK matching for reliable commands.
  if (f.family == ARC_FAMILY_NETMGMT && f.type == ARC_NETMGMT_ACK
      && f.payload_len == ARC_NETMGMT_ACK_PAYLOAD_SIZE) {
    arc_netmgmt_ack_t ack;
    if (arc_netmgmt_ack_decode(f.payload, f.payload_len, &ack) == ARC_OK) {
      if (g_pending.waiting && ack.seq == g_pending.seq) {
        uint32_t rtt = millis() - g_pending.sent_ms;
        Serial.print(F("ACK ")); Serial.print(g_pending.tag);
        Serial.print(F(" seq=")); Serial.print(ack.seq);
        Serial.print(F(" rtt=")); Serial.print(rtt); Serial.println(F("ms"));
        g_pending.waiting = false;
      } else if (g_listen_mode) {
        Serial.print(F("RX <- 0x")); print_hex(f.src);
        Serial.print(F(" [NETMGMT ACK] seq=")); Serial.println(ack.seq);
      }
      return;
    }
  }

  if (g_listen_mode) print_frame(&f);
}

static void pump_link() {
  while (LINK.available()) {
    int c = LINK.read();
    if (c < 0) break;
    uint8_t b = (uint8_t)c;
    if (b == 0x00) {
      if (g_rx_len == 0) continue;  // resync byte / empty frame
      uint8_t decoded[ARC_MAX_FRAME_SIZE];
      int n = arc_cobs_decode(g_rx_buf, g_rx_len, decoded, sizeof(decoded));
      g_rx_len = 0;
      if (n < 0) {
        if (g_listen_mode) {
          Serial.print(F("RX (cobs err ")); Serial.print(n); Serial.println(')');
        }
        continue;
      }
      handle_decoded_frame(decoded, n);
    } else {
      if (g_rx_len < RX_BUF_SIZE) {
        g_rx_buf[g_rx_len++] = b;
      } else {
        g_rx_len = 0;
        Serial.println(F("(rx overflow, resyncing)"));
      }
    }
  }
}

// ----------------------------------------------------------------------
// Periodic tasks
// ----------------------------------------------------------------------
static void maybe_heartbeat() {
  uint32_t now = millis();
  if (now - g_last_heartbeat_ms < HEARTBEAT_INTERVAL_MS) return;
  g_last_heartbeat_ms = now;
  uint16_t seq = 0;
  send_frame(0, ARC_FAMILY_NETMGMT, ARC_NETMGMT_HEARTBEAT, nullptr, 0, &seq, "auto-hb");
}

static void check_ack_timeout() {
  if (!g_pending.waiting) return;
  if (millis() - g_pending.sent_ms < ACK_TIMEOUT_MS) return;
  Serial.print(F("NO-ACK ")); Serial.print(g_pending.tag);
  Serial.print(F(" seq=")); Serial.println(g_pending.seq);
  g_pending.waiting = false;
}

// ----------------------------------------------------------------------
// Setup / loop
// ----------------------------------------------------------------------
void setup() {
  Serial.begin(USB_BAUD);
  LINK.begin(LINK_BAUD);

  uint32_t start = millis();
  while (!Serial && millis() - start < 2000) { /* wait briefly for USB */ }

  // Random session at boot so the Controller resets its dedup window.
  randomSeed(analogRead(A0) ^ micros());
  g_session = (uint8_t)random(1, 256);

  Serial.println();
  Serial.print(F("ARC fake FC-N bridge | addr=0x")); print_hex(MY_ADDR);
  Serial.print(F(" -> 0x"));     print_hex(CONTROLLER_ADDR);
  Serial.print(F(" | session=0x")); print_hex(g_session);
  Serial.println();
  Serial.println(F("type 'help' for commands"));
}

void loop() {
  pump_usb();
  pump_link();
  maybe_heartbeat();
  check_ack_timeout();
}
