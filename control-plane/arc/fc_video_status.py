"""Rich FC_VIDEO STATUS_REPORT payload shared by Controller and GS tools."""

from __future__ import annotations

from dataclasses import dataclass

from arc_protocol import messages, protocol


# FVS2 adds a per-sender friendly name (length-prefixed UTF-8) after the
# fixed status block. The magic bump means an older FVS1 decoder rejects the
# payload outright instead of mis-parsing the trailing name bytes.
MAGIC = b"FVS2"
SENDER_FIXED_LEN = 13  # addr + flags + has_status + 10-byte status


@dataclass(frozen=True)
class SenderVideoSnapshot:
    addr: int
    flags: int
    status: messages.StatusReport | None = None
    name: str = ""


@dataclass(frozen=True)
class ControllerVideoStatus:
    layout: str
    desired_sources: tuple[int, ...]
    active_sources: tuple[int, ...]
    senders: tuple[SenderVideoSnapshot, ...]

    def encode(self) -> bytes:
        layout = self.layout.encode("utf-8")
        if len(layout) > 0xFF:
            raise messages.MessageError("layout name is too long")
        out = bytearray(MAGIC)
        out.append(len(layout))
        out.extend(layout)
        _append_sources(out, self.desired_sources, "desired_sources")
        _append_sources(out, self.active_sources, "active_sources")
        if len(self.senders) > 0xFF:
            raise messages.MessageError("too many senders")
        out.append(len(self.senders))
        for sender in self.senders:
            out.append(_u8(sender.addr, "sender addr"))
            if sender.flags & ~messages.FC_VIDEO_STATUS_FLAGS_MASK:
                raise messages.MessageError(
                    f"sender flags 0x{sender.flags:02x} include reserved bits"
                )
            out.append(sender.flags)
            if sender.status is None:
                out.append(0)
                out.extend(b"\x00" * 10)
            else:
                out.append(1)
                out.extend(sender.status.encode())
            name = sender.name.encode("utf-8")
            if len(name) > 0xFF:
                raise messages.MessageError("sender name is too long")
            out.append(len(name))
            out.extend(name)
        if len(out) > protocol.MAX_PAYLOAD_SIZE:
            raise messages.MessageError("FC_VIDEO STATUS_REPORT exceeds max payload")
        return bytes(out)

    @classmethod
    def decode(cls, payload: bytes) -> "ControllerVideoStatus":
        if not payload.startswith(MAGIC):
            raise messages.MessageError("FC_VIDEO STATUS_REPORT has wrong magic")
        pos = len(MAGIC)
        layout_len, pos = _take_u8(payload, pos, "layout length")
        if len(payload) < pos + layout_len:
            raise messages.MessageError("FC_VIDEO STATUS_REPORT truncated layout")
        try:
            layout = payload[pos : pos + layout_len].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise messages.MessageError("layout name is not UTF-8") from exc
        pos += layout_len
        desired_sources, pos = _take_sources(payload, pos, "desired_sources")
        active_sources, pos = _take_sources(payload, pos, "active_sources")
        sender_count, pos = _take_u8(payload, pos, "sender count")
        senders: list[SenderVideoSnapshot] = []
        for _ in range(sender_count):
            if len(payload) < pos + SENDER_FIXED_LEN:
                raise messages.MessageError("FC_VIDEO STATUS_REPORT truncated sender")
            addr = payload[pos]
            flags = payload[pos + 1]
            has_status = payload[pos + 2]
            raw_status = payload[pos + 3 : pos + 13]
            pos += SENDER_FIXED_LEN
            name_len, pos = _take_u8(payload, pos, "sender name length")
            if len(payload) < pos + name_len:
                raise messages.MessageError("FC_VIDEO STATUS_REPORT truncated sender name")
            try:
                name = payload[pos : pos + name_len].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise messages.MessageError("sender name is not UTF-8") from exc
            pos += name_len
            if flags & ~messages.FC_VIDEO_STATUS_FLAGS_MASK:
                raise messages.MessageError(
                    f"sender flags 0x{flags:02x} include reserved bits"
                )
            if has_status not in (0, 1):
                raise messages.MessageError("sender status validity must be 0 or 1")
            status = messages.StatusReport.decode(raw_status) if has_status else None
            senders.append(
                SenderVideoSnapshot(addr=addr, flags=flags, status=status, name=name)
            )
        if pos != len(payload):
            raise messages.MessageError("FC_VIDEO STATUS_REPORT has trailing bytes")
        return cls(
            layout=layout,
            desired_sources=desired_sources,
            active_sources=active_sources,
            senders=tuple(senders),
        )


def _append_sources(out: bytearray, sources: tuple[int, ...], name: str) -> None:
    if len(sources) > 0xFF:
        raise messages.MessageError(f"{name} has too many entries")
    out.append(len(sources))
    for source in sources:
        out.append(_u8(source, name))


def _take_sources(payload: bytes, pos: int, name: str) -> tuple[tuple[int, ...], int]:
    count, pos = _take_u8(payload, pos, f"{name} count")
    if len(payload) < pos + count:
        raise messages.MessageError(f"FC_VIDEO STATUS_REPORT truncated {name}")
    return tuple(payload[pos : pos + count]), pos + count


def _take_u8(payload: bytes, pos: int, name: str) -> tuple[int, int]:
    if len(payload) <= pos:
        raise messages.MessageError(f"FC_VIDEO STATUS_REPORT missing {name}")
    return payload[pos], pos + 1


def _u8(value: int, name: str) -> int:
    if not 0 <= value <= 0xFF:
        raise messages.MessageError(f"{name} must fit in one byte")
    return value
