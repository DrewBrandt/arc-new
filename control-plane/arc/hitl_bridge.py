"""Native ARC HITL sample injector.

This is the laptop-side bridge for early full-rocket HITL bring-up. It emits
the same ASCII ``HITL/...`` sample line used by astra-support today, wrapped in
ARC frames so the Controller can route it to every configured FC.

The FC firmware side can decode the payload later. For now, this gives us a
native transport path that exercises Controller TCP ingress, ARC routing, and
per-FC fanout without requiring hardware-specific sensor parsing.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import time
from dataclasses import dataclass
from typing import Iterable

from arc_protocol import protocol
from arc.runtime import tcp_read_frame, tcp_write_frame


DEFAULT_HITL_ADDR = 0x70
DEFAULT_DST_ADDRS = (
    protocol.ADDR_FC_N,
    protocol.ADDR_FC_C,
    protocol.ADDR_FC_L,
)
FC_COORD_HITL_SENSOR_SAMPLE = 0x30
FC_COORD_HITL_CONTROL = 0x31

_ADDR_ALIASES = {
    "fc-n": protocol.ADDR_FC_N,
    "nose": protocol.ADDR_FC_N,
    "fc-c": protocol.ADDR_FC_C,
    "central": protocol.ADDR_FC_C,
    "airbrake": protocol.ADDR_FC_C,
    "fc-l": protocol.ADDR_FC_L,
    "lower": protocol.ADDR_FC_L,
    "payload": protocol.ADDR_FC_L,
    "controller": protocol.ADDR_CONTROLLER,
    "broadcast": protocol.ADDR_BROADCAST,
}


@dataclass(frozen=True)
class HitlSample:
    timestamp_s: float
    accel_mps2: tuple[float, float, float]
    gyro_radps: tuple[float, float, float]
    mag_ut: tuple[float, float, float]
    pressure_hpa: float
    temp_c: float
    lat_deg: float
    lon_deg: float
    alt_m: float
    fix: int
    sats: int
    heading_deg: float

    def to_hitl_line(self) -> str:
        return (
            f"HITL/{self.timestamp_s:.3f},"
            f"{self.accel_mps2[0]:.4f},{self.accel_mps2[1]:.4f},{self.accel_mps2[2]:.4f},"
            f"{self.gyro_radps[0]:.4f},{self.gyro_radps[1]:.4f},{self.gyro_radps[2]:.4f},"
            f"{self.mag_ut[0]:.2f},{self.mag_ut[1]:.2f},{self.mag_ut[2]:.2f},"
            f"{self.pressure_hpa:.2f},{self.temp_c:.2f},"
            f"{self.lat_deg:.7f},{self.lon_deg:.7f},{self.alt_m:.2f},"
            f"{self.fix},{self.sats},{self.heading_deg:.1f}\n"
        )


class PhysicsSampleSource:
    """Tiny dependency-free rocket-ish profile for native transport testing."""

    def __init__(self, rate_hz: float = 50.0) -> None:
        self.dt = 1.0 / rate_hz
        self.t = 0.0
        self.alt = 0.0
        self.vel = 0.0
        self.ignition_s = 2.0
        self.burnout_s = 4.0

    def next_sample(self) -> HitlSample:
        self.t += self.dt
        if self.t < self.ignition_s:
            accel_z = 0.0
            self.alt = 0.0
            self.vel = 0.0
        elif self.t < self.burnout_s:
            accel_z = 30.0
        elif self.alt > 0.0:
            accel_z = -9.81
        else:
            accel_z = 0.0
            self.alt = 0.0
            self.vel = 0.0

        self.vel += accel_z * self.dt
        self.alt = max(0.0, self.alt + self.vel * self.dt)
        measured_z = -accel_z - 9.81
        return HitlSample(
            timestamp_s=self.t,
            accel_mps2=(0.0, 0.0, measured_z),
            gyro_radps=(0.0, 0.0, 0.0),
            mag_ut=(0.0, 0.0, 0.0),
            pressure_hpa=pressure_from_msl_altitude(self.alt),
            temp_c=25.0,
            lat_deg=45.0,
            lon_deg=-122.0,
            alt_m=self.alt,
            fix=1,
            sats=8,
            heading_deg=0.0,
        )


def pressure_from_msl_altitude(altitude_m: float) -> float:
    altitude_ratio = max(0.0, 1.0 - (altitude_m / 44330.0))
    return 1013.25 * (altitude_ratio ** (1.0 / 0.1903))


class ArcHitlBridge:
    def __init__(
        self,
        *,
        controller_host: str,
        controller_port: int,
        src_addr: int = DEFAULT_HITL_ADDR,
        dst_addrs: Iterable[int] = DEFAULT_DST_ADDRS,
        session: int = 1,
        reliable_samples: bool = False,
        verbose: bool = False,
    ) -> None:
        self.controller_host = controller_host
        self.controller_port = controller_port
        self.src_addr = src_addr
        self.dst_addrs = tuple(dst_addrs)
        self.session = session
        self.reliable_samples = reliable_samples
        self.verbose = verbose
        self._seq = 0

    async def run(
        self,
        source: PhysicsSampleSource,
        *,
        rate_hz: float,
        duration_s: float | None = None,
    ) -> None:
        reader, writer = await asyncio.open_connection(
            self.controller_host, self.controller_port
        )
        rx_task = asyncio.create_task(self._rx_loop(reader))
        started_at = time.monotonic()
        try:
            await self._send_frame(
                writer,
                dst=protocol.ADDR_CONTROLLER,
                family=protocol.FAMILY_NETMGMT,
                mtype=protocol.NETMGMT_HEARTBEAT,
                payload=b"",
                reliable=True,
            )
            next_sample_at = time.monotonic()
            period = 1.0 / rate_hz
            while duration_s is None or time.monotonic() - started_at < duration_s:
                sample = source.next_sample()
                await self.send_sample(writer, sample)
                next_sample_at += period
                await asyncio.sleep(max(0.0, next_sample_at - time.monotonic()))
        finally:
            rx_task.cancel()
            writer.close()
            await writer.wait_closed()
            try:
                await rx_task
            except asyncio.CancelledError:
                pass

    async def send_sample(
        self,
        writer: asyncio.StreamWriter,
        sample: HitlSample,
    ) -> None:
        payload = sample.to_hitl_line().encode("utf-8")
        if len(payload) > protocol.MAX_PAYLOAD_SIZE:
            raise ValueError(
                f"HITL payload is {len(payload)} bytes; ARC max is {protocol.MAX_PAYLOAD_SIZE}"
            )
        for dst in self.dst_addrs:
            await self._send_frame(
                writer,
                dst=dst,
                family=protocol.FAMILY_FC_COORD,
                mtype=FC_COORD_HITL_SENSOR_SAMPLE,
                payload=payload,
                reliable=self.reliable_samples,
            )

    async def _send_frame(
        self,
        writer: asyncio.StreamWriter,
        *,
        dst: int,
        family: int,
        mtype: int,
        payload: bytes,
        reliable: bool = False,
    ) -> protocol.Frame:
        flags = protocol.FLAG_RELIABLE if reliable else 0
        frame = protocol.Frame(
            src=self.src_addr,
            dst=dst,
            flags=flags,
            session=self.session,
            seq=self._next_seq(),
            family=family,
            type=mtype,
            payload=payload,
        )
        await tcp_write_frame(writer, frame)
        if self.verbose:
            print(
                f"TX src=0x{frame.src:02x} dst=0x{frame.dst:02x} "
                f"family=0x{frame.family:02x} type=0x{frame.type:02x} "
                f"seq={frame.seq} len={len(frame.payload)}"
            )
        return frame

    async def _rx_loop(self, reader: asyncio.StreamReader) -> None:
        while True:
            frame = await tcp_read_frame(reader)
            if self.verbose:
                print(
                    f"RX src=0x{frame.src:02x} dst=0x{frame.dst:02x} "
                    f"family=0x{frame.family:02x} type=0x{frame.type:02x} "
                    f"seq={frame.seq} len={len(frame.payload)}"
                )

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq = (self._seq + 1) & 0xFFFF
        return seq


def parse_addr(value: str) -> int:
    normalized = value.strip().lower()
    if normalized in _ADDR_ALIASES:
        return _ADDR_ALIASES[normalized]
    addr = int(normalized, 0)
    if not 0 <= addr <= 0xFF:
        raise argparse.ArgumentTypeError("address must fit in one byte")
    return addr


def parse_dst_list(value: str) -> tuple[int, ...]:
    if value.strip().lower() in ("all", "all-fc", "rocket"):
        return DEFAULT_DST_ADDRS
    return tuple(parse_addr(part) for part in value.split(",") if part.strip())


async def _amain(args: argparse.Namespace) -> None:
    source = PhysicsSampleSource(rate_hz=args.rate_hz)
    bridge = ArcHitlBridge(
        controller_host=args.controller,
        controller_port=args.port,
        src_addr=args.addr,
        dst_addrs=args.dst,
        session=args.session,
        reliable_samples=args.reliable_samples,
        verbose=args.verbose,
    )
    if args.dry_run:
        sample = source.next_sample()
        print(sample.to_hitl_line().rstrip())
        print(
            "would send to "
            + ", ".join(f"0x{dst:02x}" for dst in bridge.dst_addrs)
            + f" as FC_COORD type 0x{FC_COORD_HITL_SENSOR_SAMPLE:02x}"
        )
        return
    await bridge.run(source, rate_hz=args.rate_hz, duration_s=args.duration)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inject native HITL samples over ARC TCP")
    parser.add_argument("--controller", default="127.0.0.1", help="Controller host/IP")
    parser.add_argument("--port", type=int, default=6000, help="Controller TCP port")
    parser.add_argument(
        "--addr",
        type=parse_addr,
        default=DEFAULT_HITL_ADDR,
        help="ARC source address for this HITL bridge",
    )
    parser.add_argument(
        "--dst",
        type=parse_dst_list,
        default=DEFAULT_DST_ADDRS,
        help="Destination list: all, fc-n,fc-c,fc-l, or comma-separated addresses",
    )
    parser.add_argument("--session", type=int, default=1, help="ARC session byte")
    parser.add_argument("--rate-hz", type=float, default=50.0, help="Sample rate")
    parser.add_argument("--duration", type=float, default=None, help="Stop after N seconds")
    parser.add_argument(
        "--reliable-samples",
        action="store_true",
        help="Mark sensor samples reliable; useful for protocol tests, not timing tests",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print one sample and exit")
    parser.add_argument("--verbose", action="store_true", help="Log TX/RX ARC frames")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    if args.rate_hz <= 0:
        raise SystemExit("--rate-hz must be greater than 0")
    if args.session < 0 or args.session > 0xFF:
        raise SystemExit("--session must fit in one byte")
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
