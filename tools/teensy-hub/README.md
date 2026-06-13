# ARC Teensy hub

Flight firmware for the **nosecone central router** (Teensy 4.1, ARC address
`0x05` / `teensy-hub`). Every nosecone node is a UART spoke on the Teensy;
everything off-nosecone is forwarded to the Pi 5 gateway (`pi-5-nose`).

This is the real router, not the bench impersonator in `../fake-fc-teensy`.

## What it does

- **Routes by destination** using the shared `arc_router` (one link per spoke):
  - `FC-N (0x02)`, `ARCH-Mega-N (0x30)`, `command radio (0x20)`, `pi-5-nose
    (0x10)` → their own UARTs.
  - `GROUND (0x01)` and `RADIO_G (0x21)` → the command radio (the RF link to
    the ground station).
  - `RADIO_DATA (0x22)` → **transcoded**, not ARC-forwarded (see below).
  - everything else → default link to `pi-5-nose` (WiFi gateway).
- **Generates its own traffic**: a 1 Hz broadcast NETMGMT heartbeat and 0.5 Hz
  RADIO/POWER status polls.
- **Stores everything passing through** (`hub_store`): a ring buffer of recent
  frame headers, per-frame counters, an error tally, per-source liveness, and a
  vitals snapshot. Optional SD black-box logging with `-DHUB_SD_LOG`.
- **Quick-look OLED** (0.91" SSD1306, 128×32, I2C `0x3C`): rotating pages for
  spoke link state, peer/radio vitals, and throughput/power.

## Data radio (RADIO_DATA, 0x22)

The live data downlink uses a proprietary, non-ARC packet format. The hub
**terminates** ARC frames routed to `0x22` and re-emits the telemetry in the
vendor's framing inside `data_radio.cpp`. That framing is currently a
documented **placeholder** (`pack_vendor_frame()`); swap in the real encoding
when the vendor spec is available — nothing else should need to change.

## Wiring (Teensy 4.1)

| Spoke | Serial | Notes |
|-------|--------|-------|
| pi-5-nose (0x10) | Serial1 | also the off-nosecone gateway |
| FC-N (0x02) | Serial2 | |
| ARCH-Mega-N (0x30) | Serial3 | power board |
| command radio (0x20) | Serial4 | RF link to ground |
| data radio (0x22) | Serial5 | proprietary, TX-only |
| OLED | I2C (SDA 18 / SCL 19) | SSD1306 @ 0x3C |

All assignments live in `src/hub_config.h`.

## Build

```
pio run                 # compile for teensy41
pio run -t upload       # flash
pio device monitor      # USB console (press Enter for a status line)
```

The `arc_protocol` C library is pulled in by symlink from `../../com-protocol`,
so protocol changes show up on the next build with no copy step.
