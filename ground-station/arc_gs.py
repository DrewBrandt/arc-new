#!/usr/bin/env python3
"""Bare-bones ARC ground station (placeholder).

Connects over BLE to the ESP32 ground radio (NUS, advertised as "ARC-GS"),
decodes the ARC frames it forwards from the flight radio, and lets you send a
few commands back. This is a first-light proof that the GS computer can talk to
the rocket through the radio link -- not the real app (that's Flutter later).

What it does:
  * prints every downlink frame (heartbeats show up ~2/sec from the flight radio)
  * `freq <MHz>`  -> sends RADIO SET_FREQUENCY to the flight radio (0x20),
                     reliable, so it ACKs and both ends hop together
  * `q` / `quit`  -> exit

Wire format: the ground radio COBS-frames ARC over BLE, so we split the notify
stream on 0x00 and hand each frame to arc_protocol.decode_frame().

Requires:  pip install bleak   (arc-protocol is already installed)
Run:       python arc_gs.py
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import random
import sys
import time

try:
    from bleak import BleakClient, BleakScanner
except ImportError:  # pragma: no cover - lets protocol tests run without BLE deps
    BleakClient = None
    BleakScanner = None

LOCAL_ARC_PROTOCOL = Path(__file__).resolve().parents[2] / "arc-protocol" / "python"
if LOCAL_ARC_PROTOCOL.exists():
    sys.path.insert(0, str(LOCAL_ARC_PROTOCOL))
LOCAL_CONTROL_PLANE = Path(__file__).resolve().parents[1] / "control-plane"
if LOCAL_CONTROL_PLANE.exists():
    sys.path.insert(0, str(LOCAL_CONTROL_PLANE))

from arc_protocol import protocol, messages
from arc.fc_video_status import ControllerVideoStatus, SenderVideoSnapshot

# Nordic UART Service (must match the ground radio firmware)
NUS_SERVICE = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
NUS_RX_WRITE = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"  # we WRITE here (uplink)
NUS_TX_NOTIFY = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"  # we get NOTIFY here (downlink)
DEVICE_NAME = "ARC-GS"

# This GS app's ARC identity.
MY_ADDR = protocol.ADDR_GROUND  # 0x01
SESSION = random.randint(1, 255)

ADDR_ALIASES = {
    "ground": protocol.ADDR_GROUND,
    "fc-n": protocol.ADDR_FC_N,
    "fc-c": protocol.ADDR_FC_C,
    "fc-l": protocol.ADDR_FC_L,
    "hub": protocol.ADDR_TEENSY_HUB,
    "teensy-hub": protocol.ADDR_TEENSY_HUB,
    "pi-5-nose": protocol.ADDR_CONTROLLER,
    "controller": protocol.ADDR_CONTROLLER,
    "down": protocol.ADDR_SENDER_DOWN,
    "airbrake": protocol.ADDR_SENDER_AIRBRAKE,
    "payload": protocol.ADDR_SENDER_PAYLOAD,
    "sender-ground": protocol.ADDR_SENDER_GROUND,
    "radio-cmd": protocol.ADDR_RADIO_CMD,
    "cmd-radio": protocol.ADDR_RADIO_CMD,
    "radio-g": protocol.ADDR_RADIO_G,
    "ground-radio": protocol.ADDR_RADIO_G,
    "radio-data": protocol.ADDR_RADIO_DATA,
    "arch-mega-n": protocol.ADDR_ARCH_MEGA_N,
    "arch-n": protocol.ADDR_ARCH_MEGA_N,
    "power-n": protocol.ADDR_ARCH_MEGA_N,
    "arch-mega-l": protocol.ADDR_ARCH_MEGA_L,
    "arch-l": protocol.ADDR_ARCH_MEGA_L,
    "power-l": protocol.ADDR_ARCH_MEGA_L,
    "arch-mega-c": protocol.ADDR_ARCH_MEGA_C,
    "arch-c": protocol.ADDR_ARCH_MEGA_C,
    "power-c": protocol.ADDR_ARCH_MEGA_C,
}
ADDR_NAMES = {value: key for key, value in ADDR_ALIASES.items()}
ADDR_NAMES.update(
    {
        protocol.ADDR_UNASSIGNED: "empty",
        protocol.ADDR_GROUND: "Ground Station",
        protocol.ADDR_FC_N: "FC-N",
        protocol.ADDR_FC_C: "FC-C",
        protocol.ADDR_FC_L: "FC-L",
        protocol.ADDR_TEENSY_HUB: "Teensy Hub",
        protocol.ADDR_CONTROLLER: "Pi Controller",
        protocol.ADDR_SENDER_DOWN: "Down Sender",
        protocol.ADDR_SENDER_AIRBRAKE: "Airbrake Sender",
        protocol.ADDR_SENDER_PAYLOAD: "Payload Sender",
        protocol.ADDR_SENDER_GROUND: "Ground Sender",
        protocol.ADDR_RADIO_CMD: "Rocket Radio (Cmd)",
        protocol.ADDR_RADIO_G: "Ground Radio",
        protocol.ADDR_RADIO_DATA: "Data Radio",
    }
)


class GroundStation:
    def __init__(self, client):
        self.client = client
        self._rx = bytearray()  # COBS reassembly of the notify stream
        self._seq = 0
        self._hb_count = 0
        self._last_heartbeat = None
        self._heartbeat_event = asyncio.Event()
        self._ack_waiters: dict[int, asyncio.Event] = {}
        self._acks: dict[int, protocol.Frame] = {}

    @property
    def heartbeat_count(self) -> int:
        return self._hb_count

    # ---- receive path -------------------------------------------------
    def on_notify(self, _char, data: bytearray):
        for b in data:
            self._rx.append(b)
            if b == 0x00:  # end of a COBS frame
                self._handle_cobs(bytes(self._rx))
                self._rx.clear()

    def _handle_cobs(self, cobs: bytes):
        if len(cobs) <= 1:
            return
        try:
            f = protocol.decode_frame(cobs)
        except Exception as exc:  # noqa: BLE001 - placeholder tool
            print(f"  [rx] undecodable frame ({len(cobs)} B): {exc}")
            return
        if f.dst == MY_ADDR and f.flags & protocol.FLAG_RELIABLE:
            self._schedule_ack(f)
        ts = time.strftime("%H:%M:%S")
        if f.family == protocol.FAMILY_NETMGMT and f.type == protocol.NETMGMT_HEARTBEAT:
            self._hb_count += 1
            self._last_heartbeat = f
            self._heartbeat_event.set()
            print(f"  [{ts}] HEARTBEAT from 0x{f.src:02x}  (#{self._hb_count})")
        elif f.family == protocol.FAMILY_NETMGMT and f.type == protocol.NETMGMT_ACK:
            acked = int.from_bytes(f.payload[:2], "big") if len(f.payload) >= 2 else None
            if acked is not None:
                self._acks[acked] = f
                waiter = self._ack_waiters.get(acked)
                if waiter is not None:
                    waiter.set()
            print(f"  [{ts}] ACK from 0x{f.src:02x}  acked_seq={acked}")
        else:
            detail = describe_payload(f)
            if detail:
                lines = detail.splitlines()
                print(
                    f"  [{ts}] {lines[0]} from {format_addr(f.src)} "
                    f"seq={f.seq}"
                )
                for line in lines[1:]:
                    print(f"          {line}")
            else:
                print(
                    f"  [{ts}] frame src=0x{f.src:02x} dst=0x{f.dst:02x} "
                    f"fam=0x{f.family:02x} type=0x{f.type:02x} seq={f.seq} "
                    f"len={len(f.payload)}"
                )

    # ---- send path ----------------------------------------------------
    async def _send_frame(self, dst, family, mtype, payload=b"", reliable=True):
        flags = protocol.FLAG_RELIABLE if reliable else 0
        seq = self._seq
        frame = protocol.build_frame(
            MY_ADDR, dst, flags, SESSION, seq, family, mtype, payload
        )
        self._seq = (self._seq + 1) & 0xFFFF
        cobs = protocol.cobs_encode(frame)  # ends in the 0x00 delimiter
        await self.client.write_gatt_char(NUS_RX_WRITE, cobs, response=False)
        return seq

    def _schedule_ack(self, frame: protocol.Frame) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._send_ack(frame))

    async def _send_ack(self, frame: protocol.Frame) -> int:
        seq = self._seq
        ack = protocol.build_frame(
            MY_ADDR,
            frame.src,
            protocol.FLAG_ACK,
            SESSION,
            seq,
            protocol.FAMILY_NETMGMT,
            protocol.NETMGMT_ACK,
            frame.seq.to_bytes(2, "big"),
        )
        self._seq = (self._seq + 1) & 0xFFFF
        await self.client.write_gatt_char(
            NUS_RX_WRITE, protocol.cobs_encode(ack), response=False
        )
        return seq

    async def send_freq(self, mhz: float):
        hz = int(round(mhz * 1_000_000))
        payload = hz.to_bytes(4, "big")
        seq = await self._send_frame(
            protocol.ADDR_RADIO_CMD,
            protocol.FAMILY_RADIO,
            int(messages.RadioType.SET_FREQUENCY),
            payload,
        )
        print(f"  -> sent SET_FREQUENCY {mhz:.3f} MHz to flight radio (0x20), seq={seq}")
        return seq

    async def send_phy_profile(self, profile_id: int):
        payload = profile_id.to_bytes(1, "big")
        seq = await self._send_frame(
            protocol.ADDR_RADIO_CMD,
            protocol.FAMILY_RADIO,
            int(messages.RadioType.SET_PHY_PROFILE),
            payload,
        )
        print(f"  -> sent SET_PHY_PROFILE {profile_id} to flight radio (0x20), seq={seq}")
        return seq

    async def ping(self, dst: int, timeout: float):
        started = time.monotonic()
        seq = await self._send_frame(
            dst,
            protocol.FAMILY_NETMGMT,
            protocol.NETMGMT_HEARTBEAT,
            reliable=True,
        )
        print(f"  -> ping 0x{dst:02x} seq={seq}")
        ack = await self.wait_for_ack(seq, timeout)
        rtt_ms = (time.monotonic() - started) * 1000.0
        print(f"  <- pong 0x{ack.src:02x} seq={seq} rtt={rtt_ms:.0f}ms")
        return ack, rtt_ms

    async def request_status(self):
        seq = await self._send_frame(
            protocol.ADDR_CONTROLLER,
            protocol.FAMILY_FC_VIDEO,
            int(messages.FcVideoType.GET_STATUS),
            reliable=True,
        )
        print(f"  -> status request to {format_addr(protocol.ADDR_CONTROLLER)} seq={seq}")
        return seq

    async def wait_for_heartbeat(self, timeout: float, after_count: int = 0):
        if self._hb_count > after_count:
            return self._last_heartbeat
        while self._hb_count <= after_count:
            self._heartbeat_event.clear()
            await asyncio.wait_for(self._heartbeat_event.wait(), timeout=timeout)
        return self._last_heartbeat

    async def wait_for_ack(self, seq: int, timeout: float):
        if seq in self._acks:
            return self._acks[seq]
        event = self._ack_waiters.setdefault(seq, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        finally:
            self._ack_waiters.pop(seq, None)
        return self._acks[seq]


def describe_payload(f: protocol.Frame) -> str | None:
    if (
        f.family == protocol.FAMILY_FC_VIDEO
        and f.type == int(messages.FcVideoType.STATUS_REPORT)
    ):
        try:
            return describe_fc_video_status(ControllerVideoStatus.decode(f.payload))
        except Exception:
            return None
    try:
        decoded = messages.decode_frame_payload(f)
    except Exception:
        return None
    if isinstance(decoded, messages.FlightTelemetry):
        return (
            "FC_TELEM "
            f"stage={stage_name(decoded.stage)} "
            f"alt={decoded.alt_cm / 100:.1f}m "
            f"vel_z={decoded.vel_z_cms / 100:.1f}m/s "
            f"gps={gps_fix_name(decoded.gps_fix_quality)} "
            f"vbat={decoded.voltage_mv / 1000:.2f}V "
            f"att=({decoded.roll_cdeg / 100:.1f},"
            f"{decoded.pitch_cdeg / 100:.1f},"
            f"{decoded.yaw_cdeg / 100:.1f})deg"
        )
    if isinstance(decoded, messages.AirbrakeTelemetry):
        return (
            "AIRBRAKE_TELEM "
            f"stage={stage_name(decoded.stage)} "
            f"angle={decoded.airbrake_angle_cdeg / 100:.1f}deg "
            f"pred_apogee={decoded.predicted_apogee_cm / 100:.1f}m "
            f"br_alt={decoded.blueraven_alt_cm / 100:.1f}m "
            f"vbat={decoded.voltage_mv / 1000:.2f}V"
        )
    if isinstance(decoded, messages.PayloadTelemetry):
        return (
            "PAYLOAD_TELEM "
            f"stage={stage_name(decoded.stage)} "
            f"x={decoded.motor_x_um / 1000:.1f}mm "
            f"y={decoded.motor_y_um / 1000:.1f}mm "
            f"done={decoded.percent_complete}% "
            f"vbat={decoded.voltage_mv / 1000:.2f}V"
        )
    if isinstance(decoded, messages.PowerBoardTelemetry):
        return (
            "POWER_TELEM "
            f"on=0x{decoded.output_on_mask:02x} "
            f"fault=0x{decoded.output_fault_mask:02x} "
            f"bat={decoded.battery_voltage_mv / 1000:.2f}V "
            f"charge={charge_name(decoded.charge_status)} "
            f"chg_v={decoded.charge_voltage_mv / 1000:.2f}V"
        )
    return None


def describe_fc_video_status(status: ControllerVideoStatus) -> str:
    layout = status.layout or "(none)"
    desired = format_sources(status.desired_sources)
    active = format_sources(status.active_sources)
    online = [
        sender
        for sender in status.senders
        if sender.flags & messages.FC_VIDEO_STATUS_FLAG_ONLINE
    ]
    lines = [
        "FC_VIDEO STATUS",
        f"layout={layout}",
        f"desired={desired}",
        f"active={active}",
        "connected="
        + (", ".join(format_addr(sender.addr) for sender in online) if online else "none"),
        "senders:",
    ]
    for sender in status.senders:
        parts = [format_addr(sender.addr), sender_flags(sender.flags)]
        if sender.status is not None:
            report = sender.status
            parts.append(
                "video="
                f"state=0x{report.state:02x} "
                f"cpu={report.cpu_temp_c}C/{report.cpu_load_pct}% "
                f"disk={report.free_disk_mb}MB "
                f"rssi={report.rssi_dbm}dBm "
                f"tx={report.tx_frames} drop={report.dropped_frames}"
            )
        else:
            parts.append("video=no-report")
        lines.append("  " + " | ".join(parts))
    return "\n".join(lines)


def format_sources(sources: tuple[int, ...]) -> str:
    if not sources:
        return "(none)"
    return ", ".join(
        f"slot{i}={format_addr(source)}" for i, source in enumerate(sources)
    )


def sender_flags(flags: int) -> str:
    bits = []
    if flags & messages.FC_VIDEO_STATUS_FLAG_ONLINE:
        bits.append("online")
    else:
        bits.append("offline")
    if flags & messages.FC_VIDEO_STATUS_FLAG_TRANSMITTING:
        bits.append("streaming")
    if flags & messages.FC_VIDEO_STATUS_FLAG_RECORDING:
        bits.append("recording")
    return "/".join(bits)


def format_addr(addr: int) -> str:
    return f"{ADDR_NAMES.get(addr, f'0x{addr:02x}')} (0x{addr:02x})"


def stage_name(stage: int) -> str:
    names = {
        messages.FC_COORD_STAGE_UNKNOWN: "unknown",
        messages.FC_COORD_STAGE_PAD: "pad",
        messages.FC_COORD_STAGE_BOOST: "boost",
        messages.FC_COORD_STAGE_COAST: "coast",
        messages.FC_COORD_STAGE_DROGUE: "drogue",
        messages.FC_COORD_STAGE_MAIN: "main",
        messages.FC_COORD_STAGE_LANDED: "landed",
    }
    return names.get(stage, f"0x{stage:02x}")


def gps_fix_name(fix: int) -> str:
    names = {
        messages.FC_COORD_GPS_FIX_NONE: "none",
        messages.FC_COORD_GPS_FIX_2D: "2d",
        messages.FC_COORD_GPS_FIX_3D: "3d",
        messages.FC_COORD_GPS_FIX_DGPS: "dgps",
        messages.FC_COORD_GPS_FIX_RTK_FLOAT: "rtk-float",
        messages.FC_COORD_GPS_FIX_RTK_FIXED: "rtk-fixed",
    }
    return names.get(fix, f"0x{fix:02x}")


def charge_name(status: int) -> str:
    names = {
        messages.POWER_CHARGE_UNPLUGGED: "unplugged",
        messages.POWER_CHARGE_PLUGGED: "plugged",
        messages.POWER_CHARGE_CHARGING: "charging",
        messages.POWER_CHARGE_FAULT: "fault",
    }
    return names.get(status, f"0x{status:02x}")


def parse_addr(text: str) -> int:
    key = text.strip().lower()
    if key in ADDR_ALIASES:
        return ADDR_ALIASES[key]
    value = int(key, 0)
    if not 0 <= value <= 0xFF:
        raise ValueError("address out of range")
    return value


HELP = "commands:  status   ping <addr|name>   freq <MHz>   phy <0|1>   q/quit   help"


async def repl(gs: GroundStation):
    loop = asyncio.get_event_loop()
    print(HELP)
    while True:
        line = (await loop.run_in_executor(None, sys.stdin.readline)).strip()
        if not line:
            continue
        parts = line.split()
        cmd = parts[0].lower()
        if cmd in ("q", "quit", "exit"):
            return
        if cmd == "help":
            print(HELP)
        elif cmd == "status" and len(parts) == 1:
            await gs.request_status()
        elif cmd == "ping" and len(parts) in (2, 3):
            try:
                timeout = float(parts[2]) if len(parts) == 3 else 2.0
                await gs.ping(parse_addr(parts[1]), timeout)
            except asyncio.TimeoutError:
                print(f"  ping timeout: {parts[1]}")
            except ValueError:
                print("  usage: ping <addr|name> [timeout_s]   e.g. ping 0x05")
        elif cmd == "freq" and len(parts) == 2:
            try:
                await gs.send_freq(float(parts[1]))
            except ValueError:
                print("  usage: freq <MHz>   e.g. freq 915.5")
        elif cmd == "phy" and len(parts) == 2:
            try:
                profile_id = int(parts[1], 0)
                if not 0 <= profile_id <= 255:
                    raise ValueError
                await gs.send_phy_profile(profile_id)
            except ValueError:
                print("  usage: phy <0|1>   0=BW125 safe, 1=BW500 fast")
        else:
            print(f"  ? {line!r}\n  {HELP}")


async def run_smoke(gs: GroundStation, freq_mhz: float, args) -> bool:
    print(f"Smoke: waiting for initial heartbeat ({args.heartbeat_timeout:.1f}s timeout) ...")
    try:
        first = await gs.wait_for_heartbeat(args.heartbeat_timeout)
    except asyncio.TimeoutError:
        print("Smoke FAIL: no heartbeat before timeout")
        return False

    start_count = gs.heartbeat_count
    print(f"Smoke: heartbeat OK from 0x{first.src:02x}; sending freq command ...")
    seq = await gs.send_freq(freq_mhz)

    try:
        ack = await gs.wait_for_ack(seq, args.ack_timeout)
    except asyncio.TimeoutError:
        print(f"Smoke FAIL: no ACK for seq={seq} before timeout")
        return False

    print(f"Smoke: ACK OK from 0x{ack.src:02x}; waiting for post-hop heartbeat ...")
    try:
        post = await gs.wait_for_heartbeat(args.post_hop_timeout, after_count=start_count)
    except asyncio.TimeoutError:
        print("Smoke FAIL: no heartbeat after ACK/frequency-hop window")
        return False

    print(f"Smoke PASS: post-hop heartbeat from 0x{post.src:02x}")
    return True


async def connect_and_run(args) -> int:
    if BleakScanner is None or BleakClient is None:
        print("bleak is not installed. Run: pip install -r requirements.txt")
        return 2

    print(f"Scanning for {args.device_name!r} ...")
    device = await BleakScanner.find_device_by_name(args.device_name, timeout=args.scan_timeout)
    if device is None:
        print(f"  not found. Is the ground radio powered and advertising as {args.device_name!r}?")
        return 1
    print(f"Connecting to {device.address} ...")
    async with BleakClient(device) as client:
        gs = GroundStation(client)
        await client.start_notify(NUS_TX_NOTIFY, gs.on_notify)
        print(f"Connected. GS addr=0x{MY_ADDR:02x} session=0x{SESSION:02x}.")
        if args.smoke_freq is not None:
            ok = await run_smoke(gs, args.smoke_freq, args)
            await client.stop_notify(NUS_TX_NOTIFY)
            return 0 if ok else 1
        await repl(gs)
        await client.stop_notify(NUS_TX_NOTIFY)
    print("disconnected.")
    return 0


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Bare-bones ARC BLE ground station")
    parser.add_argument("--device-name", default=DEVICE_NAME, help="BLE advertised name")
    parser.add_argument("--scan-timeout", type=float, default=15.0)
    parser.add_argument(
        "--smoke-freq",
        type=float,
        metavar="MHz",
        help="run heartbeat -> SET_FREQUENCY -> ACK -> post-hop heartbeat, then exit",
    )
    parser.add_argument("--heartbeat-timeout", type=float, default=8.0)
    parser.add_argument("--ack-timeout", type=float, default=5.0)
    parser.add_argument("--post-hop-timeout", type=float, default=8.0)
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None):
    return await connect_and_run(parse_args(argv))


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
