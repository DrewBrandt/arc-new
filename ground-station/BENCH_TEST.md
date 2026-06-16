# ARC Radio First-Light Bench Test

Goal: prove the command/status path before moving on to telemetry schema,
Flutter, or video integration.

```
Python GS -> BLE NUS -> ESP32 ground radio -> LoRa -> STM32 flight radio
          <- BLE NUS <- ESP32 ground radio <- LoRa <-
```

## Software Preflight

Run these before hardware time:

```powershell
cd C:\Users\Drew\Documents\arc-new
python -m unittest discover -s ground-station -p "test_*.py"

cd "C:\Users\Drew\Documents\Avionics Personal\Side_Projects\Radio_Code"
pio run -e stm32h723vehx

cd "C:\Users\Drew\Documents\Avionics Personal.worktrees\launch-jan\Side_Projects\ESP32FC"
pio run -e ESP32FC
```

Expected: all three commands pass. The ESP32 build may print a NimBLE
`svc->start()` deprecation warning; that is not a blocker.

## Flash

Flight radio:

```powershell
cd "C:\Users\Drew\Documents\Avionics Personal\Side_Projects\Radio_Code"
pio run -e stm32h723vehx -t upload
```

Ground radio:

```powershell
cd "C:\Users\Drew\Documents\Avionics Personal.worktrees\launch-jan\Side_Projects\ESP32FC"
pio run -e ESP32FC -t upload
```

## Smoke Test

Power both radios, then run:

```powershell
cd C:\Users\Drew\Documents\arc-new\ground-station
python arc_gs.py --smoke-freq 915.5
```

Pass criteria:

- BLE connects to `ARC-GS`.
- First heartbeat arrives from `0x20`.
- `SET_FREQUENCY 915.500 MHz` is sent with a sequence number.
- ACK arrives from `0x20` for that same sequence number.
- Another heartbeat arrives after the hop window.

Manual mode, if you want to watch or poke:

```powershell
python arc_gs.py
freq 915.5
phy 1      # fast BW500 profile
phy 0      # safe BW125 profile
```

## Fast Failure Triage

`not found` while scanning:

- Ground ESP32 is not powered, not flashed with `ESP32FC`, or not advertising
  as `ARC-GS`.

Connects but no heartbeat:

- Flight radio not powered/flashed.
- LoRa PHY mismatch. Current first-light firmware is `915.0 MHz`, SF7, BW125,
  CR4/5, sync `0x12`, preamble 8, CRC on, explicit header.
- Check antennas and close-range placement.

Heartbeat arrives but no ACK:

- Ground BLE write path is working enough to connect, but uplink may not be
  transmitted in the command window.
- Flight radio may not be receiving IRQ/data, or the command did not parse as
  `RADIO SET_FREQUENCY`.

ACK arrives but no post-hop heartbeat:

- One side hopped and the other did not. Wait at least 5 seconds for revert,
  then rerun at `915.5` or power-cycle both radios back to default.

## After First Pass

Keep BW125 for the first successful proof. Once heartbeat/freq-hop passes, use
`phy 1` to test the generated `RADIO SET_PHY_PROFILE` path and switch both radios
to BW500 via the ACK-mirrored profile switch. Use `phy 0` to return to the safe
profile.
