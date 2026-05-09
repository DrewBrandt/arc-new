"""Controller-side command/status wrapper for one ARC Sender."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from arc import messages, protocol


SendCommand = Callable[
    [int, int, int, bytes, bool, int, float],
    protocol.Frame,
]


class SenderLinkError(RuntimeError):
    """Raised when a frame does not belong to this sender link."""


@dataclass(frozen=True)
class SenderStatus:
    report: messages.StatusReport
    seen_at: float


class SenderLink:
    """High-level Controller view of a Sender control-plane endpoint."""

    def __init__(
        self,
        sender_addr: int,
        send_command: SendCommand,
        reliable_commands: bool = True,
    ) -> None:
        self.sender_addr = sender_addr
        self.send_command = send_command
        self.reliable_commands = reliable_commands
        self.last_status: SenderStatus | None = None

    @property
    def online(self) -> bool:
        return self.last_status is not None

    def start_stream(self, now: float = 0.0) -> protocol.Frame:
        return self._send_video(messages.VideoType.START_STREAM, now=now)

    def stop_stream(self, now: float = 0.0) -> protocol.Frame:
        return self._send_video(messages.VideoType.STOP_STREAM, now=now)

    def hard_stop(self, now: float = 0.0) -> protocol.Frame:
        return self._send_video(messages.VideoType.HARD_STOP, now=now)

    def set_bitrate(self, bitrate_bps: int, now: float = 0.0) -> protocol.Frame:
        return self._send_video(
            messages.VideoType.SET_BITRATE,
            payload=messages.SetBitrate(bitrate_bps).encode(),
            now=now,
        )

    def handle_frame(self, frame: protocol.Frame, now: float = 0.0) -> messages.StatusReport:
        """Accept a Sender-originated STATUS_REPORT frame and update state."""

        if frame.src != self.sender_addr:
            raise SenderLinkError(
                f"expected frame from sender 0x{self.sender_addr:02x}, got 0x{frame.src:02x}"
            )
        if frame.family != protocol.FAMILY_VIDEO:
            raise SenderLinkError(f"expected VIDEO frame, got family 0x{frame.family:02x}")
        if frame.type != messages.VideoType.STATUS_REPORT:
            raise SenderLinkError(f"expected STATUS_REPORT, got type 0x{frame.type:02x}")

        report = messages.StatusReport.decode(frame.payload)
        self.last_status = SenderStatus(report=report, seen_at=now)
        return report

    def mark_offline(self) -> None:
        self.last_status = None

    def _send_video(
        self,
        type: messages.VideoType,
        payload: bytes = b"",
        now: float = 0.0,
    ) -> protocol.Frame:
        return self.send_command(
            self.sender_addr,
            protocol.FAMILY_VIDEO,
            type,
            payload,
            self.reliable_commands,
            0,
            now,
        )
