// ARC fake-node bridge for Teensy 4.1.
//
// Pretends to be one ARC node at a time: FC-N/C/L, a sender telemetry source,
// or an ARCH-Mega power board. It keeps the network alive with periodic
// heartbeats/telemetry and translates plain-text commands typed in the USB
// serial monitor into ARC frames over the hardware UART.
// Frames coming back from the Controller are pretty-printed.
//
// ----------------------------------------------------------------------
// Wiring (Teensy 4.1 <-> hub or Raspberry Pi)
// ----------------------------------------------------------------------
//   Serial1..Serial4 can each impersonate a different ARC node. Cross TX/RX
//   with the hub Teensy's matching or intentionally mismatched UARTs to test
//   learned routing.
//
// Single-port Pi/controller bench wiring:
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
//   ports                                  list all fake serial ports
//   port <1|2|3|4>                         select port for manual commands
//   node <name|addr>                       set this fake node's ARC source addr
//   mode <flight|airbrake|payload|power>   set automatic telemetry shape
//   heartbeat | hb                         send NETMGMT HEARTBEAT now
//   telemetry | telem                      send one telemetry frame now
//
// Video / Controller (FC_VIDEO):
//   layout <id>                            SET_LAYOUT (numeric, 0..255)
//   source <slot> <name|addr>              SET_SOURCE
//                                          name: empty | local/pi-5-nose | down | airbrake | payload | ground
//   overlay <text>                         SET_OVERLAY (rest of line)
//   status                                 GET_STATUS
//
// Radio (cmd=command radio 0x20, g=ground 0x21):
//   freq <cmd|g> <hz>                        SET_FREQUENCY
//   txpower <cmd|g> <dbm>                    SET_TX_POWER (signed dBm)
//   radiostatus <cmd|g>                      GET_STATUS
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
#include "arc_messages_fc_coord.h"
#include "arc_messages_radio.h"
#include "arc_messages_power.h"

// ----------------------------------------------------------------------
// Configuration
// ----------------------------------------------------------------------
static constexpr uint8_t  CONTROLLER_ADDR       = ARC_ADDR_CONTROLLER;
static constexpr uint8_t  TELEMETRY_ADDR        = ARC_ADDR_GROUND;
static constexpr uint32_t USB_BAUD              = 115200;
static constexpr uint32_t LINK_BAUD             = 115200;
static constexpr uint32_t HEARTBEAT_INTERVAL_MS = 5000;
static constexpr uint32_t TELEMETRY_INTERVAL_MS = 1000;
static constexpr uint32_t ACK_TIMEOUT_MS        = 1000;

// ----------------------------------------------------------------------
// Per-boot state
// ----------------------------------------------------------------------
static uint8_t  g_session     = 1;
static bool     g_listen_mode = true;

enum FakeMode {
  MODE_FLIGHT,
  MODE_AIRBRAKE,
  MODE_PAYLOAD,
  MODE_POWER,
};

struct PendingAck {
  bool        waiting   = false;
  uint16_t    seq       = 0;
  uint32_t    sent_ms   = 0;
  const char* tag       = "";
};

// ----------------------------------------------------------------------
// Buffers
// ----------------------------------------------------------------------
static constexpr size_t CMD_BUF_SIZE = 240;
static char             g_cmd_buf[CMD_BUF_SIZE];
static size_t           g_cmd_len = 0;

static constexpr size_t RX_BUF_SIZE = ARC_MAX_ENCODED_SIZE + 4;

struct FakePort {
  const char*     name;
  HardwareSerial* serial;
  uint8_t         addr;
  FakeMode        mode;
  uint16_t        next_seq;
  uint32_t        last_heartbeat_ms;
  uint32_t        last_telemetry_ms;
  uint8_t         rx_buf[RX_BUF_SIZE];
  size_t          rx_len;
  PendingAck      pending;
};

static FakePort g_ports[] = {
  {"p1", &Serial1, ARC_ADDR_FC_N,          MODE_FLIGHT,   0, 0, 0, {}, 0, {}},
  {"p2", &Serial2, ARC_ADDR_FC_C,          MODE_FLIGHT,   0, 0, 0, {}, 0, {}},
  {"p3", &Serial3, ARC_ADDR_ARCH_MEGA_N,   MODE_POWER,    0, 0, 0, {}, 0, {}},
  {"p4", &Serial4, ARC_ADDR_SENDER_AIRBRAKE, MODE_AIRBRAKE, 0, 0, 0, {}, 0, {}},
};

static constexpr uint8_t FAKE_PORT_COUNT = sizeof(g_ports) / sizeof(g_ports[0]);
static uint8_t g_active_port = 0;

// ----------------------------------------------------------------------
// Tiny helpers
// ----------------------------------------------------------------------
static void print_hex(uint8_t b) {
  if (b < 0x10) Serial.print('0');
  Serial.print(b, HEX);
}

static const char* source_name(uint8_t addr);
static const char* mode_name(FakeMode mode);

static FakePort* active_port() {
  return &g_ports[g_active_port];
}

static bool parse_port(const char* arg, uint8_t* out) {
  if (!arg || !out) return false;
  if ((arg[0] == 'p' || arg[0] == 'P') && arg[1] >= '1' && arg[1] <= '4' && arg[2] == '\0') {
    *out = (uint8_t)(arg[1] - '1');
    return *out < FAKE_PORT_COUNT;
  }
  char* end = nullptr;
  long v = strtol(arg, &end, 0);
  if (end && *end == '\0' && v >= 1 && v <= FAKE_PORT_COUNT) {
    *out = (uint8_t)(v - 1);
    return true;
  }
  return false;
}

static void print_port_summary(const FakePort* p) {
  Serial.print(p->name);
  Serial.print(F(" addr=0x")); print_hex(p->addr);
  Serial.print(F(" ")); Serial.print(source_name(p->addr));
  Serial.print(F(" mode=")); Serial.print(mode_name(p->mode));
}

struct SenderAlias { const char* name; uint8_t addr; };
static const SenderAlias kAliases[] = {
  {"empty",          ARC_ADDR_UNASSIGNED},
  {"off",            ARC_ADDR_UNASSIGNED},
  {"none",           ARC_ADDR_UNASSIGNED},
  {"local",          ARC_ADDR_CONTROLLER},
  {"controller",     ARC_ADDR_CONTROLLER},
  {"pi-5-nose",      ARC_ADDR_CONTROLLER},
  {"main-tube",      ARC_ADDR_CONTROLLER},
  // Flight computers
  {"fc-n",           ARC_ADDR_FC_N},
  {"fc-c",           ARC_ADDR_FC_C},
  {"fc-l",           ARC_ADDR_FC_L},
  {"nose-fc",        ARC_ADDR_FC_N},
  {"center-fc",      ARC_ADDR_FC_C},
  {"lower-fc",       ARC_ADDR_FC_L},
  {"down",           ARC_ADDR_SENDER_DOWN},
  {"airbrake",       ARC_ADDR_SENDER_AIRBRAKE},
  {"payload",        ARC_ADDR_SENDER_PAYLOAD},
  {"ground",         ARC_ADDR_SENDER_GROUND},
  // Radios
  {"radio-cmd",      ARC_ADDR_RADIO_CMD},
  {"cmd-radio",      ARC_ADDR_RADIO_CMD},
  {"radio-g",        ARC_ADDR_RADIO_G},
  {"ground-radio",   ARC_ADDR_RADIO_G},
  {"radio-data",     ARC_ADDR_RADIO_DATA},
  {"data-radio",     ARC_ADDR_RADIO_DATA},
  // Power boards
  {"arch-mega-n",    ARC_ADDR_ARCH_MEGA_N},
  {"arch-n",         ARC_ADDR_ARCH_MEGA_N},
  {"power-n",        ARC_ADDR_ARCH_MEGA_N},
  {"nose-power",     ARC_ADDR_ARCH_MEGA_N},
  {"arch-mega-l",    ARC_ADDR_ARCH_MEGA_L},
  {"arch-l",         ARC_ADDR_ARCH_MEGA_L},
  {"power-l",        ARC_ADDR_ARCH_MEGA_L},
  {"lower-power",    ARC_ADDR_ARCH_MEGA_L},
  {"arch-mega-c",    ARC_ADDR_ARCH_MEGA_C},
  {"arch-c",         ARC_ADDR_ARCH_MEGA_C},
  {"power-c",        ARC_ADDR_ARCH_MEGA_C},
  {"center-power",   ARC_ADDR_ARCH_MEGA_C},
};

// RADIO-family control (freq/txpower/status) only applies to the ARC-speaking
// radios: the command radio (cmd, 0x20) and the ground radio (g, 0x21). The
// live data downlink (RADIO_DATA, 0x22) speaks a proprietary protocol and is
// driven by the Teensy hub's transcode path, not these commands.
static bool resolve_radio(const char* arg, uint8_t* out) {
  if (!arg) return false;
  if (strcasecmp(arg, "cmd") == 0 || strcasecmp(arg, "command") == 0
      || strcasecmp(arg, "r") == 0 || strcasecmp(arg, "rocket") == 0) {
    *out = ARC_ADDR_RADIO_CMD; return true;
  }
  if (strcasecmp(arg, "g") == 0 || strcasecmp(arg, "ground") == 0) {
    *out = ARC_ADDR_RADIO_G; return true;
  }
  for (const auto& a : kAliases) {
    if (strcasecmp(arg, a.name) == 0
        && (a.addr == ARC_ADDR_RADIO_CMD || a.addr == ARC_ADDR_RADIO_G)) {
      *out = a.addr; return true;
    }
  }
  char* end = nullptr;
  long v = strtol(arg, &end, 0);
  if (end && *end == '\0' && (v == ARC_ADDR_RADIO_CMD || v == ARC_ADDR_RADIO_G)) {
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

static const char* mode_name(FakeMode mode) {
  switch (mode) {
    case MODE_FLIGHT:   return "flight";
    case MODE_AIRBRAKE: return "airbrake";
    case MODE_PAYLOAD:  return "payload";
    case MODE_POWER:    return "power";
    default:            return "?";
  }
}

static FakeMode default_mode_for_addr(uint8_t addr) {
  switch (addr) {
    case ARC_ADDR_SENDER_AIRBRAKE: return MODE_AIRBRAKE;
    case ARC_ADDR_SENDER_PAYLOAD:  return MODE_PAYLOAD;
    case ARC_ADDR_ARCH_MEGA_N:
    case ARC_ADDR_ARCH_MEGA_L:
    case ARC_ADDR_ARCH_MEGA_C:     return MODE_POWER;
    default:                       return MODE_FLIGHT;
  }
}

// ----------------------------------------------------------------------
// Frame TX
// ----------------------------------------------------------------------
static bool send_frame_to(FakePort* port, uint8_t dst, uint8_t flags, uint8_t family, uint8_t type,
                          const uint8_t* payload, size_t payload_len,
                          uint16_t* out_seq, const char* tag) {
  if (!port) return false;
  uint8_t frame[ARC_MAX_FRAME_SIZE];
  uint16_t seq = port->next_seq++;
  int n = arc_frame_build(frame, sizeof(frame),
                          port->addr, dst,
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
  port->serial->write(encoded, m);
  if (out_seq) *out_seq = seq;
  if (flags & ARC_FLAG_RELIABLE) {
    port->pending.waiting = true;
    port->pending.seq     = seq;
    port->pending.sent_ms = millis();
    port->pending.tag     = tag;
  }
  return true;
}

// Default destination = Controller, used by the FC_VIDEO and NETMGMT
// commands that always target the Controller.
static bool send_frame(uint8_t flags, uint8_t family, uint8_t type,
                       const uint8_t* payload, size_t payload_len,
                       uint16_t* out_seq, const char* tag) {
  return send_frame_to(active_port(), CONTROLLER_ADDR, flags, family, type,
                       payload, payload_len, out_seq, tag);
}

static void send_ack_for(FakePort* port, const arc_frame_t* original) {
  if (!port) return;
  uint8_t frame[ARC_MAX_FRAME_SIZE];
  int n = arc_frame_build_ack(frame, sizeof(frame), original, g_session, port->next_seq++);
  if (n < 0) return;
  uint8_t encoded[ARC_MAX_ENCODED_SIZE];
  int m = arc_cobs_encode(frame, n, encoded, sizeof(encoded));
  if (m < 0) return;
  port->serial->write(encoded, m);
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
  Serial.println(F("  ports                         list simulated UART ports"));
  Serial.println(F("  port <1|2|3|4>                select port for manual commands"));
  Serial.println(F("  node <name|addr>              set selected port ARC source"));
  Serial.println(F("  mode <flight|airbrake|payload|power>  set selected port telemetry shape"));
  Serial.println(F("  heartbeat | hb                send NETMGMT HEARTBEAT now"));
  Serial.println(F("  telemetry | telem             send one telemetry frame now"));
  Serial.println(F("Video / Controller (FC_VIDEO):"));
  Serial.println(F("  layout <id>                   SET_LAYOUT (0..255)"));
  Serial.println(F("  source <slot> <name|addr>     SET_SOURCE"));
  Serial.println(F("                                name: empty|local/pi-5-nose|down|airbrake|payload|ground"));
  Serial.println(F("  overlay <text>                SET_OVERLAY (rest of line)"));
  Serial.println(F("  status                        GET_STATUS"));
  Serial.println(F("Radio (cmd=command radio, g=ground):"));
  Serial.println(F("  freq <cmd|g> <hz>             RADIO SET_FREQUENCY"));
  Serial.println(F("  txpower <cmd|g> <dbm>         RADIO SET_TX_POWER (signed dBm)"));
  Serial.println(F("  radiostatus <cmd|g>           RADIO GET_STATUS"));
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
  // Heartbeats are broadcast so any neighbor learns this node's addr + link.
  if (send_frame_to(active_port(), ARC_ADDR_BROADCAST, 0, ARC_FAMILY_NETMGMT,
                    ARC_NETMGMT_HEARTBEAT, nullptr, 0, &seq, "heartbeat")) {
    Serial.print(F("TX -> 0x")); print_hex(ARC_ADDR_BROADCAST);
    Serial.print(F(" [NETMGMT HEARTBEAT] seq=")); Serial.println(seq);
  }
}

static void cmd_ports() {
  Serial.println(F("Ports:"));
  for (uint8_t i = 0; i < FAKE_PORT_COUNT; i++) {
    Serial.print(i == g_active_port ? F("* ") : F("  "));
    print_port_summary(&g_ports[i]);
    Serial.println();
  }
}

static void cmd_port(const char* arg) {
  uint8_t idx = 0;
  if (!parse_port(arg, &idx)) {
    Serial.println(F("usage: port <1|2|3|4>"));
    return;
  }
  g_active_port = idx;
  Serial.print(F("active "));
  print_port_summary(active_port());
  Serial.println();
}

static void cmd_node(const char* arg) {
  if (!arg) { Serial.println(F("usage: node <name|addr>")); return; }
  uint8_t addr = 0;
  if (!resolve_source(arg, &addr) || addr == ARC_ADDR_UNASSIGNED || addr == ARC_ADDR_BROADCAST) {
    Serial.print(F("unknown/invalid node: ")); Serial.println(arg);
    return;
  }
  FakePort* port = active_port();
  port->addr = addr;
  port->mode = default_mode_for_addr(addr);
  port->next_seq = 0;
  Serial.print(F("node "));
  print_port_summary(port);
  Serial.println();
}

static void cmd_mode(const char* arg) {
  if (!arg) { Serial.println(F("usage: mode <flight|airbrake|payload|power>")); return; }
  if (strcasecmp(arg, "flight") == 0 || strcasecmp(arg, "fc") == 0) {
    active_port()->mode = MODE_FLIGHT;
  } else if (strcasecmp(arg, "airbrake") == 0 || strcasecmp(arg, "brake") == 0) {
    active_port()->mode = MODE_AIRBRAKE;
  } else if (strcasecmp(arg, "payload") == 0 || strcasecmp(arg, "cnc") == 0) {
    active_port()->mode = MODE_PAYLOAD;
  } else if (strcasecmp(arg, "power") == 0 || strcasecmp(arg, "pwr") == 0) {
    active_port()->mode = MODE_POWER;
  } else {
    Serial.print(F("unknown mode: ")); Serial.println(arg);
    return;
  }
  Serial.print(F("mode "));
  print_port_summary(active_port());
  Serial.println();
}

static void send_flight_telemetry(FakePort* port, const char* tag, bool print_tx) {
  uint32_t now = millis();
  arc_fc_coord_flight_telemetry_t msg = {
    .time_ms = now,
    .stage = ARC_FC_COORD_STAGE_PAD,
    .accel_x_mg = 12,
    .accel_y_mg = -34,
    .accel_z_mg = 1002,
    .vel_x_cms = 0,
    .vel_y_cms = 0,
    .vel_z_cms = 0,
    .lat_e7 = 391234567,
    .lon_e7 = -1049876543,
    .alt_cm = 185000,
    .temp_cdeg = 2350,
    .voltage_mv = 11900,
    .gps_fix_quality = ARC_FC_COORD_GPS_FIX_3D,
    .roll_cdeg = 25,
    .pitch_cdeg = -120,
    .yaw_cdeg = 9012,
  };
  uint8_t payload[ARC_FC_COORD_FLIGHT_TELEMETRY_PAYLOAD_SIZE];
  if (arc_fc_coord_flight_telemetry_encode(&msg, payload, sizeof(payload)) < 0) {
    Serial.println(F("telemetry encode failed"));
    return;
  }
  uint16_t seq = 0;
  if (send_frame_to(port, TELEMETRY_ADDR, 0, ARC_FAMILY_FC_COORD,
                    ARC_FC_COORD_FLIGHT_TELEMETRY, payload, sizeof(payload),
                    &seq, tag)) {
    if (print_tx) {
      Serial.print(port->name); Serial.print(' ');
      Serial.print(F("TX -> 0x")); print_hex(TELEMETRY_ADDR);
      Serial.print(F(" [FC_COORD FLIGHT_TELEMETRY] seq=")); Serial.print(seq);
      Serial.print(F(" alt_cm=")); Serial.print(msg.alt_cm);
      Serial.print(F(" vbat_mv=")); Serial.println(msg.voltage_mv);
    }
  }
}

static void send_airbrake_telemetry(FakePort* port, const char* tag, bool print_tx) {
  uint32_t now = millis();
  arc_fc_coord_airbrake_telemetry_t msg = {
    .time_ms = now,
    .stage = ARC_FC_COORD_STAGE_COAST,
    .accel_x_mg = 8,
    .accel_y_mg = -11,
    .accel_z_mg = 998,
    .vel_x_cms = 2,
    .vel_y_cms = -3,
    .vel_z_cms = 1245,
    .temp_cdeg = 2410,
    .voltage_mv = 11850,
    .roll_cdeg = 15,
    .pitch_cdeg = -90,
    .yaw_cdeg = 4500,
    .airbrake_angle_cdeg = 1250,
    .predicted_apogee_cm = 305000,
    .original_apogee_estimate_cm = 300000,
    .blueraven_alt_cm = 125000,
  };
  uint8_t payload[ARC_FC_COORD_AIRBRAKE_TELEMETRY_PAYLOAD_SIZE];
  if (arc_fc_coord_airbrake_telemetry_encode(&msg, payload, sizeof(payload)) < 0) {
    Serial.println(F("airbrake telemetry encode failed"));
    return;
  }
  uint16_t seq = 0;
  if (send_frame_to(port, TELEMETRY_ADDR, 0, ARC_FAMILY_FC_COORD,
                    ARC_FC_COORD_AIRBRAKE_TELEMETRY, payload, sizeof(payload),
                    &seq, tag)) {
    if (print_tx) {
      Serial.print(port->name); Serial.print(' ');
      Serial.print(F("TX -> 0x")); print_hex(TELEMETRY_ADDR);
      Serial.print(F(" [FC_COORD AIRBRAKE_TELEMETRY] seq=")); Serial.print(seq);
      Serial.print(F(" angle_cdeg=")); Serial.print(msg.airbrake_angle_cdeg);
      Serial.print(F(" pred_apogee_cm=")); Serial.println(msg.predicted_apogee_cm);
    }
  }
}

static void send_payload_telemetry(FakePort* port, const char* tag, bool print_tx) {
  uint32_t now = millis();
  uint8_t pct = (uint8_t)((now / 1000UL) % 101UL);
  arc_fc_coord_payload_telemetry_t msg = {
    .time_ms = now,
    .stage = ARC_FC_COORD_STAGE_PAD,
    .accel_x_mg = -1,
    .accel_y_mg = -2,
    .accel_z_mg = 1000,
    .vel_x_cms = 0,
    .vel_y_cms = 0,
    .vel_z_cms = 0,
    .temp_cdeg = 2260,
    .voltage_mv = 12010,
    .roll_cdeg = 0,
    .pitch_cdeg = 0,
    .yaw_cdeg = 0,
    .motor_x_um = 123000,
    .motor_y_um = -45000,
    .percent_complete = pct,
  };
  uint8_t payload[ARC_FC_COORD_PAYLOAD_TELEMETRY_PAYLOAD_SIZE];
  if (arc_fc_coord_payload_telemetry_encode(&msg, payload, sizeof(payload)) < 0) {
    Serial.println(F("payload telemetry encode failed"));
    return;
  }
  uint16_t seq = 0;
  if (send_frame_to(port, TELEMETRY_ADDR, 0, ARC_FAMILY_FC_COORD,
                    ARC_FC_COORD_PAYLOAD_TELEMETRY, payload, sizeof(payload),
                    &seq, tag)) {
    if (print_tx) {
      Serial.print(port->name); Serial.print(' ');
      Serial.print(F("TX -> 0x")); print_hex(TELEMETRY_ADDR);
      Serial.print(F(" [FC_COORD PAYLOAD_TELEMETRY] seq=")); Serial.print(seq);
      Serial.print(F(" x_um=")); Serial.print(msg.motor_x_um);
      Serial.print(F(" y_um=")); Serial.print(msg.motor_y_um);
      Serial.print(F(" pct=")); Serial.println(msg.percent_complete);
    }
  }
}

static void send_power_telemetry_to(FakePort* port, uint8_t dst, const char* tag, bool print_tx) {
  arc_power_board_telemetry_t msg = {
    .output_on_mask = 0x2D,
    .output_fault_mask = 0x04,
    .battery_voltage_mv = 11980,
    .charge_status = ARC_POWER_CHARGE_CHARGING,
    .charge_voltage_mv = 12600,
  };
  uint8_t payload[ARC_POWER_BOARD_TELEMETRY_PAYLOAD_SIZE];
  if (arc_power_board_telemetry_encode(&msg, payload, sizeof(payload)) < 0) {
    Serial.println(F("power telemetry encode failed"));
    return;
  }
  uint16_t seq = 0;
  if (send_frame_to(port, dst, 0, ARC_FAMILY_POWER,
                    ARC_POWER_BOARD_TELEMETRY, payload, sizeof(payload),
                    &seq, tag)) {
    if (print_tx) {
      Serial.print(port->name); Serial.print(' ');
      Serial.print(F("TX -> 0x")); print_hex(dst);
      Serial.print(F(" [POWER BOARD_TELEMETRY] seq=")); Serial.print(seq);
      Serial.print(F(" on=0x")); print_hex(msg.output_on_mask);
      Serial.print(F(" fault=0x")); print_hex(msg.output_fault_mask);
      Serial.print(F(" bat_mv=")); Serial.println(msg.battery_voltage_mv);
    }
  }
}

static void send_selected_telemetry(FakePort* port, const char* tag, bool print_tx) {
  switch (port->mode) {
    case MODE_AIRBRAKE: send_airbrake_telemetry(port, tag, print_tx); break;
    case MODE_PAYLOAD:  send_payload_telemetry(port, tag, print_tx); break;
    case MODE_POWER:    send_power_telemetry_to(port, TELEMETRY_ADDR, tag, print_tx); break;
    default:            send_flight_telemetry(port, tag, print_tx); break;
  }
}

static void cmd_telemetry() {
  send_selected_telemetry(active_port(), "telemetry", true);
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
  if (!radio_str || !hz_str) { Serial.println(F("usage: freq <cmd|g> <hz>")); return; }
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
  if (send_frame_to(active_port(), dst, ARC_FLAG_RELIABLE, ARC_FAMILY_RADIO,
                    ARC_RADIO_SET_FREQUENCY, payload, sizeof(payload),
                    &seq, "RADIO SET_FREQUENCY")) {
    Serial.print(F("TX -> 0x")); print_hex(dst);
    Serial.print(F(" [RADIO SET_FREQUENCY] hz=")); Serial.print(hz);
    Serial.print(F(" seq=")); Serial.println(seq);
  }
}

static void cmd_txpower(const char* radio_str, const char* dbm_str) {
  if (!radio_str || !dbm_str) { Serial.println(F("usage: txpower <cmd|g> <dbm>")); return; }
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
  if (send_frame_to(active_port(), dst, ARC_FLAG_RELIABLE, ARC_FAMILY_RADIO,
                    ARC_RADIO_SET_TX_POWER, payload, sizeof(payload),
                    &seq, "RADIO SET_TX_POWER")) {
    Serial.print(F("TX -> 0x")); print_hex(dst);
    Serial.print(F(" [RADIO SET_TX_POWER] dbm=")); Serial.print(dbm);
    Serial.print(F(" seq=")); Serial.println(seq);
  }
}

static void cmd_radiostatus(const char* radio_str) {
  if (!radio_str) { Serial.println(F("usage: radiostatus <cmd|g>")); return; }
  uint8_t dst = 0;
  if (!resolve_radio(radio_str, &dst)) {
    Serial.print(F("unknown radio: ")); Serial.println(radio_str);
    return;
  }
  uint16_t seq = 0;
  if (send_frame_to(active_port(), dst, ARC_FLAG_RELIABLE, ARC_FAMILY_RADIO,
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
  if (send_frame_to(active_port(), dst, ARC_FLAG_RELIABLE, ARC_FAMILY_POWER,
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
  if (send_frame_to(active_port(), dst, ARC_FLAG_RELIABLE, ARC_FAMILY_POWER,
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
  if (send_frame_to(active_port(), dst, ARC_FLAG_RELIABLE, ARC_FAMILY_POWER,
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
  } else if (strcasecmp(tok, "ports") == 0) {
    cmd_ports();
  } else if (strcasecmp(tok, "port") == 0) {
    cmd_port(strtok(nullptr, " \t"));
  } else if (strcasecmp(tok, "node") == 0 || strcasecmp(tok, "addr") == 0) {
    cmd_node(strtok(nullptr, " \t"));
  } else if (strcasecmp(tok, "mode") == 0) {
    cmd_mode(strtok(nullptr, " \t"));
  } else if (strcasecmp(tok, "heartbeat") == 0 || strcasecmp(tok, "hb") == 0) {
    cmd_heartbeat();
  } else if (strcasecmp(tok, "telemetry") == 0 || strcasecmp(tok, "telem") == 0) {
    cmd_telemetry();
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
static void handle_decoded_frame(FakePort* port, const uint8_t* frame_buf, int frame_len) {
  arc_frame_t f;
  arc_result_t r = arc_frame_parse(frame_buf, frame_len, &f);
  if (r != ARC_OK) {
    if (g_listen_mode) {
      Serial.print(F("RX (parse err ")); Serial.print(r); Serial.println(')');
    }
    return;
  }

  bool addressed_to_me = (f.dst == port->addr);
  if (addressed_to_me && (f.flags & ARC_FLAG_RELIABLE)) {
    send_ack_for(port, &f);
  }

  if (addressed_to_me && f.family == ARC_FAMILY_POWER && f.type == ARC_POWER_GET_STATUS) {
    send_power_telemetry_to(port, f.src, "POWER BOARD_TELEMETRY", g_listen_mode);
    return;
  }

  // ACK matching for reliable commands.
  if (f.family == ARC_FAMILY_NETMGMT && f.type == ARC_NETMGMT_ACK
      && f.payload_len == ARC_NETMGMT_ACK_PAYLOAD_SIZE) {
    arc_netmgmt_ack_t ack;
    if (arc_netmgmt_ack_decode(f.payload, f.payload_len, &ack) == ARC_OK) {
      if (port->pending.waiting && ack.seq == port->pending.seq) {
        uint32_t rtt = millis() - port->pending.sent_ms;
        Serial.print(port->name); Serial.print(F(" ACK ")); Serial.print(port->pending.tag);
        Serial.print(F(" seq=")); Serial.print(ack.seq);
        Serial.print(F(" rtt=")); Serial.print(rtt); Serial.println(F("ms"));
        port->pending.waiting = false;
      } else if (g_listen_mode) {
        Serial.print(F("RX <- 0x")); print_hex(f.src);
        Serial.print(F(" [NETMGMT ACK] seq=")); Serial.println(ack.seq);
      }
      return;
    }
  }

  if (g_listen_mode) print_frame(&f);
}

static void pump_link(FakePort* port) {
  while (port->serial->available()) {
    int c = port->serial->read();
    if (c < 0) break;
    uint8_t b = (uint8_t)c;
    if (b == 0x00) {
      if (port->rx_len == 0) continue;  // resync byte / empty frame
      // arc_cobs_decode requires the trailing 0x00 delimiter in its input.
      if (port->rx_len >= RX_BUF_SIZE) { port->rx_len = 0; continue; }
      port->rx_buf[port->rx_len++] = 0x00;
      uint8_t decoded[ARC_MAX_FRAME_SIZE];
      int n = arc_cobs_decode(port->rx_buf, port->rx_len, decoded, sizeof(decoded));
      port->rx_len = 0;
      if (n < 0) {
        if (g_listen_mode) {
          Serial.print(F("RX (cobs err ")); Serial.print(n); Serial.println(')');
        }
        continue;
      }
      handle_decoded_frame(port, decoded, n);
    } else {
      if (port->rx_len < RX_BUF_SIZE) {
        port->rx_buf[port->rx_len++] = b;
      } else {
        port->rx_len = 0;
        Serial.println(F("(rx overflow, resyncing)"));
      }
    }
  }
}

// ----------------------------------------------------------------------
// Periodic tasks
// ----------------------------------------------------------------------
static void maybe_heartbeat(FakePort* port) {
  uint32_t now = millis();
  if (now - port->last_heartbeat_ms < HEARTBEAT_INTERVAL_MS) return;
  port->last_heartbeat_ms = now;
  uint16_t seq = 0;
  send_frame_to(port, ARC_ADDR_BROADCAST, 0, ARC_FAMILY_NETMGMT, ARC_NETMGMT_HEARTBEAT, nullptr, 0, &seq, "auto-hb");
}

static void maybe_telemetry(FakePort* port) {
  uint32_t now = millis();
  if (now - port->last_telemetry_ms < TELEMETRY_INTERVAL_MS) return;
  port->last_telemetry_ms = now;
  send_selected_telemetry(port, "auto-telem", false);
}

static void check_ack_timeout(FakePort* port) {
  if (!port->pending.waiting) return;
  if (millis() - port->pending.sent_ms < ACK_TIMEOUT_MS) return;
  Serial.print(port->name); Serial.print(F(" NO-ACK ")); Serial.print(port->pending.tag);
  Serial.print(F(" seq=")); Serial.println(port->pending.seq);
  port->pending.waiting = false;
}

// ----------------------------------------------------------------------
// Setup / loop
// ----------------------------------------------------------------------
void setup() {
  Serial.begin(USB_BAUD);
  for (uint8_t i = 0; i < FAKE_PORT_COUNT; i++) {
    g_ports[i].serial->begin(LINK_BAUD);
  }

  uint32_t start = millis();
  while (!Serial && millis() - start < 2000) { /* wait briefly for USB */ }

  // Random session at boot so the Controller resets its dedup window.
  randomSeed(analogRead(A0) ^ micros());
  g_session = (uint8_t)random(1, 256);

  Serial.println();
  Serial.print(F("ARC fake multi-node bridge | ports=")); Serial.print(FAKE_PORT_COUNT);
  Serial.print(F(" | session=0x")); print_hex(g_session);
  Serial.println();
  cmd_ports();
  Serial.println(F("type 'help' for commands"));
}

void loop() {
  pump_usb();
  for (uint8_t i = 0; i < FAKE_PORT_COUNT; i++) {
    FakePort* port = &g_ports[i];
    pump_link(port);
    maybe_heartbeat(port);
    maybe_telemetry(port);
    check_ack_timeout(port);
  }
}
