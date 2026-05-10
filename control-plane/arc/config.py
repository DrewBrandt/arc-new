"""TOML configuration loading for ARC Controller and Sender processes.

Mirrors the schema sketched in design doc Section 10. Two top-level
shapes are supported, distinguished by the ``[node]`` role:

- Controller: declares its UART device, listening port for Senders, and
  a list of ``[[senders]]`` with addresses, names, IPs, and optional
  paired flight computers.
- Sender: declares its address and paired FC, the Controller's IP, and
  the video subsystem parameters used to build the GStreamer pipeline.

The loader returns plain dataclasses; runtime code wires them into Node,
Controller, Sender, and the link transports.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from arc import protocol


class ConfigError(ValueError):
    """Raised when a config file is missing required fields or malformed."""


@dataclass(frozen=True)
class UartConfig:
    device: str
    baud: int = 115200


@dataclass(frozen=True)
class VideoConfig:
    width: int = 640
    height: int = 480
    framerate: int = 30
    bitrate_bps: int = 2_500_000
    encoder: str = "v4l2h264enc"
    recording_path: str = "/var/arc/recordings/"
    start_stream_on_boot: bool = False


@dataclass(frozen=True)
class ControllerVideoConfig:
    mixer: str = "compositor"
    sink: str = "kmssink sync=false"
    startup_layout: str | None = None
    switch_mode: str = "rebuild"


@dataclass(frozen=True)
class SenderEntry:
    addr: int
    name: str
    ip: str
    paired_fc: int | None = None


@dataclass(frozen=True)
class ControllerConfig:
    addr: int
    callsign: str
    uart: UartConfig
    listen_port: int
    senders: tuple[SenderEntry, ...]
    heartbeat_interval_s: float = 1.0
    peer_timeout_s: float = 3.0
    layouts: Mapping[str, Mapping[str, Mapping[str, float]]] = field(default_factory=dict)
    initial_sources: tuple[int, int] = (
        protocol.ADDR_CONTROLLER,
        protocol.ADDR_UNASSIGNED,
    )
    video: ControllerVideoConfig = field(default_factory=ControllerVideoConfig)


@dataclass(frozen=True)
class SenderConfig:
    addr: int
    name: str
    paired_fc: int | None
    controller_ip: str
    controller_port: int
    controller_addr: int
    video: VideoConfig
    uart: UartConfig | None = None
    heartbeat_interval_s: float = 1.0
    peer_timeout_s: float = 3.0


def load_controller_config(path: str | Path) -> ControllerConfig:
    raw = _read(path)
    node = _require(raw, "node", path)
    addr = _require_int(node, "address", path, "[node]")
    if addr != protocol.ADDR_CONTROLLER:
        raise ConfigError(
            f"{path}: [node].address 0x{addr:02x} is not the Controller address"
            f" 0x{protocol.ADDR_CONTROLLER:02x}"
        )
    overlay = raw.get("overlay", {})
    callsign = _as_str(overlay.get("callsign"), path, "[overlay].callsign")

    uart_section = _require(raw, "uart", path)
    uart = UartConfig(
        device=_as_str(uart_section.get("device"), path, "[uart].device"),
        baud=int(uart_section.get("baud", 115200)),
    )

    controller_section = raw.get("controller", {})
    listen_port = int(controller_section.get("listen_port", 6000))

    senders_raw = raw.get("senders") or []
    if not isinstance(senders_raw, list):
        raise ConfigError(f"{path}: [[senders]] must be an array of tables")
    senders = tuple(_parse_sender_entry(entry, path) for entry in senders_raw)

    health = raw.get("health", {})
    heartbeat_interval_s = float(health.get("heartbeat_interval_s", 1.0))
    peer_timeout_s = float(health.get("peer_timeout_s", 3.0))

    layouts = raw.get("layouts", {})
    if not isinstance(layouts, dict):
        raise ConfigError(f"{path}: [layouts] must be a table")
    sources = raw.get("sources", {})
    if sources and not isinstance(sources, dict):
        raise ConfigError(f"{path}: [sources] must be a table")
    initial_sources = (
        int(sources.get("slot_0", protocol.ADDR_CONTROLLER)),
        int(sources.get("slot_1", protocol.ADDR_UNASSIGNED)),
    )
    video_section = raw.get("video", {})
    if video_section and not isinstance(video_section, dict):
        raise ConfigError(f"{path}: [video] must be a table")
    video = ControllerVideoConfig(
        mixer=_as_str(video_section.get("mixer", "compositor"), path, "[video].mixer"),
        sink=_as_str(video_section.get("sink", "kmssink sync=false"), path, "[video].sink"),
        startup_layout=_optional_str(
            video_section.get("startup_layout"), path, "[video].startup_layout"
        ),
        switch_mode=_as_str(
            video_section.get("switch_mode", "rebuild"), path, "[video].switch_mode"
        ),
    )

    return ControllerConfig(
        addr=addr,
        callsign=callsign,
        uart=uart,
        listen_port=listen_port,
        senders=senders,
        heartbeat_interval_s=heartbeat_interval_s,
        peer_timeout_s=peer_timeout_s,
        layouts=layouts,
        initial_sources=initial_sources,
        video=video,
    )


def load_sender_config(path: str | Path) -> SenderConfig:
    raw = _read(path)
    node = _require(raw, "node", path)
    addr = _require_int(node, "address", path, "[node]")
    name = _as_str(node.get("name", f"sender-0x{addr:02x}"), path, "[node].name")
    paired_fc = node.get("paired_fc")
    if paired_fc is not None:
        paired_fc = int(paired_fc)
        if not 0 <= paired_fc <= 0xFF:
            raise ConfigError(f"{path}: [node].paired_fc must be a byte")

    controller_section = _require(raw, "controller", path)
    controller_ip = _as_str(controller_section.get("ip"), path, "[controller].ip")
    controller_port = int(controller_section.get("port", 6000))
    controller_addr = int(controller_section.get("address", protocol.ADDR_CONTROLLER))

    video_section = raw.get("video", {})
    recording_section = raw.get("recording", {})
    video = VideoConfig(
        width=int(video_section.get("width", 640)),
        height=int(video_section.get("height", 480)),
        framerate=int(video_section.get("framerate", 30)),
        bitrate_bps=int(video_section.get("bitrate", 2_500_000)),
        encoder=_as_str(video_section.get("encoder", "v4l2h264enc"), path, "[video].encoder"),
        recording_path=_as_str(
            recording_section.get("path", "/var/arc/recordings/"),
            path,
            "[recording].path",
        ),
        start_stream_on_boot=bool(video_section.get("start_stream_on_boot", False)),
    )

    uart: UartConfig | None = None
    uart_section = raw.get("uart")
    if uart_section:
        uart = UartConfig(
            device=_as_str(uart_section.get("device"), path, "[uart].device"),
            baud=int(uart_section.get("baud", 115200)),
        )
    if paired_fc is not None and uart is None:
        raise ConfigError(
            f"{path}: [node].paired_fc is set, so [uart].device is required"
        )

    health = raw.get("health", {})
    heartbeat_interval_s = float(health.get("heartbeat_interval_s", 1.0))
    peer_timeout_s = float(health.get("peer_timeout_s", 3.0))

    return SenderConfig(
        addr=addr,
        name=name,
        paired_fc=paired_fc,
        controller_ip=controller_ip,
        controller_port=controller_port,
        controller_addr=controller_addr,
        video=video,
        uart=uart,
        heartbeat_interval_s=heartbeat_interval_s,
        peer_timeout_s=peer_timeout_s,
    )


def _read(path: str | Path) -> dict:
    p = Path(path)
    try:
        with p.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: malformed TOML: {exc}") from exc


def _parse_sender_entry(entry: object, path: str | Path) -> SenderEntry:
    if not isinstance(entry, dict):
        raise ConfigError(f"{path}: each [[senders]] entry must be a table")
    addr = _require_int(entry, "id", path, "[[senders]]")
    name = _as_str(entry.get("name", f"sender-0x{addr:02x}"), path, "[[senders]].name")
    ip = _as_str(entry.get("ip"), path, "[[senders]].ip")
    paired_fc = entry.get("paired_fc")
    if paired_fc is not None:
        paired_fc = int(paired_fc)
    return SenderEntry(addr=addr, name=name, ip=ip, paired_fc=paired_fc)


def _require(table: Mapping[str, object], key: str, path: str | Path) -> Mapping[str, object]:
    value = table.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: missing required [{key}] section")
    return value


def _require_int(
    table: Mapping[str, object],
    key: str,
    path: str | Path,
    section: str,
) -> int:
    if key not in table:
        raise ConfigError(f"{path}: {section}.{key} is required")
    value = table[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{path}: {section}.{key} must be an integer")
    return value


def _as_str(value: object, path: str | Path, name: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{path}: {name} must be a string")
    return value


def _optional_str(value: object, path: str | Path, name: str) -> str | None:
    if value is None:
        return None
    return _as_str(value, path, name)
