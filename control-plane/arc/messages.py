"""Typed payload helpers for ARC control-plane message families."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from arc import protocol


class MessageError(ValueError):
    """Raised when a message payload is malformed."""


class NetMgmtType(IntEnum):
    HEARTBEAT = protocol.NETMGMT_HEARTBEAT
    ACK = protocol.NETMGMT_ACK
    SESSION_RESET = protocol.NETMGMT_SESSION_RESET


class VideoType(IntEnum):
    START_STREAM = 0x01
    STOP_STREAM = 0x02
    HARD_STOP = 0x03
    SET_BITRATE = 0x04
    STATUS_REPORT = 0x10


class FcVideoType(IntEnum):
    SET_LAYOUT = 0x01
    SET_SOURCE = 0x02
    SET_OVERLAY = 0x03
    GET_STATUS = 0x04


@dataclass(frozen=True)
class Ack:
    seq: int

    def encode(self) -> bytes:
        return _u16(self.seq, "seq").to_bytes(2, "big")

    @classmethod
    def decode(cls, payload: bytes) -> "Ack":
        _require_len(payload, 2, "ACK")
        return cls(seq=int.from_bytes(payload, "big"))


@dataclass(frozen=True)
class SetBitrate:
    bitrate_bps: int

    def encode(self) -> bytes:
        return _u32(self.bitrate_bps, "bitrate_bps").to_bytes(4, "big")

    @classmethod
    def decode(cls, payload: bytes) -> "SetBitrate":
        _require_len(payload, 4, "SET_BITRATE")
        return cls(bitrate_bps=int.from_bytes(payload, "big"))


@dataclass(frozen=True)
class StatusReport:
    state: int
    cpu_temp_c: int
    cpu_load_pct: int
    free_disk_mb: int
    rssi_dbm: int
    tx_frames: int
    dropped_frames: int

    def encode(self) -> bytes:
        return bytes(
            (
                _u8(self.state, "state"),
                _u8(self.cpu_temp_c, "cpu_temp_c"),
                _u8(self.cpu_load_pct, "cpu_load_pct"),
            )
        ) + b"".join(
            (
                _u16(self.free_disk_mb, "free_disk_mb").to_bytes(2, "big"),
                _i8(self.rssi_dbm, "rssi_dbm").to_bytes(1, "big", signed=True),
                _u16(self.tx_frames, "tx_frames").to_bytes(2, "big"),
                _u16(self.dropped_frames, "dropped_frames").to_bytes(2, "big"),
            )
        )

    @classmethod
    def decode(cls, payload: bytes) -> "StatusReport":
        _require_len(payload, 10, "STATUS_REPORT")
        return cls(
            state=payload[0],
            cpu_temp_c=payload[1],
            cpu_load_pct=payload[2],
            free_disk_mb=int.from_bytes(payload[3:5], "big"),
            rssi_dbm=int.from_bytes(payload[5:6], "big", signed=True),
            tx_frames=int.from_bytes(payload[6:8], "big"),
            dropped_frames=int.from_bytes(payload[8:10], "big"),
        )


@dataclass(frozen=True)
class SetLayout:
    layout_id: int

    def encode(self) -> bytes:
        return bytes((_u8(self.layout_id, "layout_id"),))

    @classmethod
    def decode(cls, payload: bytes) -> "SetLayout":
        _require_len(payload, 1, "SET_LAYOUT")
        return cls(layout_id=payload[0])


@dataclass(frozen=True)
class SetSource:
    slot_id: int
    sender_addr: int

    def encode(self) -> bytes:
        return bytes(
            (
                _u8(self.slot_id, "slot_id"),
                _u8(self.sender_addr, "sender_addr"),
            )
        )

    @classmethod
    def decode(cls, payload: bytes) -> "SetSource":
        _require_len(payload, 2, "SET_SOURCE")
        return cls(slot_id=payload[0], sender_addr=payload[1])


@dataclass(frozen=True)
class SetOverlay:
    text: str

    def encode(self) -> bytes:
        raw = self.text.encode("utf-8") + b"\x00"
        if len(raw) > protocol.MAX_PAYLOAD_SIZE:
            raise MessageError("SET_OVERLAY payload exceeds maximum ARC payload size")
        return raw

    @classmethod
    def decode(cls, payload: bytes) -> "SetOverlay":
        if not payload.endswith(b"\x00"):
            raise MessageError("SET_OVERLAY payload must be null-terminated")
        try:
            text = payload[:-1].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MessageError("SET_OVERLAY payload must be valid UTF-8") from exc
        return cls(text=text)


def decode_netmgmt(type: int, payload: bytes) -> object | None:
    msg_type = NetMgmtType(type)
    if msg_type is NetMgmtType.ACK:
        return Ack.decode(payload)
    _require_empty(payload, msg_type.name)
    return None


def decode_video(type: int, payload: bytes) -> object | None:
    msg_type = VideoType(type)
    if msg_type is VideoType.SET_BITRATE:
        return SetBitrate.decode(payload)
    if msg_type is VideoType.STATUS_REPORT:
        return StatusReport.decode(payload)
    _require_empty(payload, msg_type.name)
    return None


def decode_fc_video(type: int, payload: bytes) -> object | None:
    msg_type = FcVideoType(type)
    if msg_type is FcVideoType.SET_LAYOUT:
        return SetLayout.decode(payload)
    if msg_type is FcVideoType.SET_SOURCE:
        return SetSource.decode(payload)
    if msg_type is FcVideoType.SET_OVERLAY:
        return SetOverlay.decode(payload)
    _require_empty(payload, msg_type.name)
    return None


def decode_frame_payload(frame: protocol.Frame) -> object | bytes | None:
    """Decode known message families, leaving FC_COORD payloads opaque."""

    if frame.family == protocol.FAMILY_NETMGMT:
        return decode_netmgmt(frame.type, frame.payload)
    if frame.family == protocol.FAMILY_VIDEO:
        return decode_video(frame.type, frame.payload)
    if frame.family == protocol.FAMILY_FC_VIDEO:
        return decode_fc_video(frame.type, frame.payload)
    if frame.family == protocol.FAMILY_FC_COORD:
        return frame.payload
    raise MessageError(f"unknown message family 0x{frame.family:02x}")


def _require_len(payload: bytes, expected: int, name: str) -> None:
    if len(payload) != expected:
        raise MessageError(f"{name} payload must be {expected} bytes")


def _require_empty(payload: bytes, name: str) -> None:
    if payload:
        raise MessageError(f"{name} payload must be empty")


def _u8(value: int, name: str) -> int:
    if not 0 <= value <= 0xFF:
        raise MessageError(f"{name} must fit in one byte")
    return value


def _i8(value: int, name: str) -> int:
    if not -128 <= value <= 127:
        raise MessageError(f"{name} must fit in signed one byte")
    return value


def _u16(value: int, name: str) -> int:
    if not 0 <= value <= 0xFFFF:
        raise MessageError(f"{name} must fit in two bytes")
    return value


def _u32(value: int, name: str) -> int:
    if not 0 <= value <= 0xFFFFFFFF:
        raise MessageError(f"{name} must fit in four bytes")
    return value
