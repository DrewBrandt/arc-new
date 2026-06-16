# ARC Teensy hub

Flight firmware for the **nosecone central router** (Teensy 4.1, ARC address
`0x05` / `teensy-hub`). Every nosecone node is a UART spoke on the Teensy. Node
identity is learned from ARC traffic, so a device can move to another spoke and
earn the new route by sending a heartbeat or any other frame.

This is the real router, not the bench impersonator in `../fake-fc-teensy`.

## What it does

- **Routes by destination** from the learned source-route table:
  - any valid source address observed on a spoke becomes `address -> spoke`.
  - `BROADCAST (0xff)` goes to every ARC UART spoke except the ingress spoke.
  - unknown destinations trigger a throttled discovery heartbeat broadcast.
- **Learns source routes** from any valid inbound ARC frame. If a node with
  source address `0x30` is seen on a different UART, replies to `0x30` use that
  learned UART until the route expires.
- **Generates its own traffic**: a 5-second broadcast NETMGMT heartbeat. Node
  heartbeats received since the last hub heartbeat are coalesced into that
  payload instead of being forwarded one-by-one.
- **Stores everything passing through** (`hub_store`): recent frame headers,
  counters, parse errors, peer liveness, vitals snapshots, and optional SD
  black-box rows with route result plus payload hex.
- **Quick-look OLED** (0.91" SSD1306, 128x32, I2C `0x3C`): rotating pages for
  spoke link state, peer/radio vitals, and throughput/power.

The USB console route map describes the learned-routing policy and address
names; there is no static device-to-port route table for ARC spokes.

## Data radio (RADIO_DATA, 0x22)

The live data downlink uses a proprietary, non-ARC packet format. It is not part
of the learned ARC spoke router. If this radio becomes an ARC node, it should
send heartbeats and earn a learned route like every other node.

## Wiring (Teensy 4.1)

| Physical link | Serial | Notes |
|---------------|--------|-------|
| serial1 | Serial1 | ARC UART spoke |
| serial2 | Serial2 | ARC UART spoke |
| serial3 | Serial3 | ARC UART spoke |
| serial4 | Serial4 | ARC UART spoke |
| serial5 | Serial5 | ARC UART spoke |
| serial6 | Serial6 | ARC UART spoke |
| serial7 | Serial7 | ARC UART spoke |
| serial8 | Serial8 | ARC UART spoke |
| OLED | I2C (SDA 18 / SCL 19) | SSD1306 @ 0x3C |

All assignments live in `src/hub_config.h`.

## Build

```
pio run                 # compile for teensy41
pio run -t upload       # flash
pio device monitor      # USB console

pio run -e teensy41-sdlog
pio run -e teensy41-sdlog -t upload
```

Use the `teensy41-sdlog` environment when you want SD black-box logging enabled.
It writes `arc_hub.log` on the built-in Teensy 4.1 SD card. Rows are CSV:

```
millis,in_link,out_link,route_result,src,dst,flags,session,seq,family,type,len,payload_hex
```

On the USB console, press Enter for one status line, `m` for the route policy,
or `l` for the learned route table.

The `arc_protocol` C library is pulled from GitHub
([DrewBrandt/arc-protocol](https://github.com/DrewBrandt/arc-protocol)), pinned
to a commit in `platformio.ini`. Bump that ref to pick up protocol changes (or
point it at a local checkout with `symlink://` during co-development).
