# ground-station (placeholder)

A bare-bones Python ground station to prove first-light over the radio link.
**Not** the real app — that's a Flutter app later (BLE telemetry/commands + USB
camera video). This is just enough to show the GS computer can talk to the
rocket through the radios.

## Data path

```
arc_gs.py ──BLE NUS──► ESP32 ground radio ──LoRa──► STM32 flight radio
  (this)   (decode ARC)   ("ARC-GS")                 (0x20, ARC node)
```

Both radios are ARC nodes; the link is half-duplex with the flight radio as
timing master (it transmits a heartbeat ~2/sec, then opens a command window).

## What it does

- Prints every downlink frame; the flight radio's heartbeats appear ~2/sec.
- Decodes first-pass telemetry reports:
  `FC_COORD FLIGHT_TELEMETRY`, `AIRBRAKE_TELEMETRY`, `PAYLOAD_TELEMETRY`,
  and `POWER BOARD_TELEMETRY`.
- `freq <MHz>` — sends a reliable `RADIO SET_FREQUENCY` to the flight radio
  (0x20). The flight radio ACKs, then both ends hop together (the ground radio
  mirrors the hop off the ACK); if the link then goes silent both revert.
- `phy <0|1>` — sends reliable `RADIO SET_PHY_PROFILE`:
  `0` = safe BW125 profile, `1` = fast BW500 profile.
- `q` / `quit` — exit.

## Run

```bash
pip install -r requirements.txt   # bleak; arc-protocol comes from the control-plane install
python arc_gs.py
```

Power the ground radio (advertises as `ARC-GS`) and the flight radio, then watch
heartbeats roll in and try `freq 915.5`, then `phy 1`.

## Automated first-light smoke test

Use this when you have the hardware on the bench and want a pass/fail check:

```bash
python arc_gs.py --smoke-freq 915.5
```

The smoke test:

1. Connects to the BLE ground radio named `ARC-GS`.
2. Waits for a flight-radio heartbeat from `0x20`.
3. Sends reliable `RADIO SET_FREQUENCY` to `915.5 MHz`.
4. Waits for the ACK.
5. Waits for another heartbeat after the hop window.

Useful knobs:

```bash
python arc_gs.py --device-name ARC-GS --scan-timeout 20 --smoke-freq 915.5
python arc_gs.py --smoke-freq 915.5 --ack-timeout 8 --post-hop-timeout 12
```

No-hardware protocol checks:

```bash
python -m unittest discover -s ground-station -p "test_*.py"
```
