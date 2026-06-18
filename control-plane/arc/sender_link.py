"""Controller-side command/status wrapper for one ARC Sender."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from arc_protocol import messages, protocol


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
        self.last_command_type: messages.VideoType | None = None
        # Friendly identity learned from a VIDEO INFO_REPORT (None until the
        # Sender answers our GET_INFO). ``info_requested`` gates re-sending the
        # request so discovery only costs one query per online session.
        self.name: str | None = None
        self.paired_fc: int | None = None
        self.info_requested: bool = False

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

    def request_info(self, now: float = 0.0) -> protocol.Frame:
        """Ask the Sender to identify itself via a VIDEO INFO_REPORT.

        Sent unreliably: a discovered Sender keeps emitting heartbeats, so a
        lost query is simply re-asked on the next frame. Keeping it out of the
        reliable queue means a transient missing route never strands a pending
        retransmit.
        """

        self.info_requested = True
        # Deliberately not routed through _send_video: GET_INFO is an identity
        # query, not a stream command, so it must not disturb
        # ``last_command_type`` (which drives the TRANSMITTING/RECORDING flags).
        return self.send_command(
            self.sender_addr,
            protocol.FAMILY_VIDEO,
            messages.VideoType.GET_INFO,
            b"",
            False,
            0,
            now,
        )

    def handle_frame(
        self, frame: protocol.Frame, now: float = 0.0
    ) -> messages.StatusReport | messages.VideoInfoReport:
        """Accept a Sender-originated VIDEO frame and update link state.

        Handles STATUS_REPORT (periodic health) and INFO_REPORT (the reply
        to ``request_info``); both arrive on the VIDEO family from this
        Sender's address.
        """

        if frame.src != self.sender_addr:
            raise SenderLinkError(
                f"expected frame from sender 0x{self.sender_addr:02x}, got 0x{frame.src:02x}"
            )
        if frame.family != protocol.FAMILY_VIDEO:
            raise SenderLinkError(f"expected VIDEO frame, got family 0x{frame.family:02x}")

        if frame.type == messages.VideoType.INFO_REPORT:
            info = messages.VideoInfoReport.decode(frame.payload)
            self.name = info.name
            self.paired_fc = (
                info.paired_fc if info.paired_fc != protocol.ADDR_UNASSIGNED else None
            )
            return info
        if frame.type == messages.VideoType.STATUS_REPORT:
            report = messages.StatusReport.decode(frame.payload)
            self.last_status = SenderStatus(report=report, seen_at=now)
            return report

        raise SenderLinkError(
            f"expected STATUS_REPORT or INFO_REPORT, got type 0x{frame.type:02x}"
        )

    def mark_offline(self) -> None:
        self.last_status = None
        # Allow re-discovery on reconnect: a swapped-in unit at the same
        # address gets a fresh GET_INFO and can report a new name.
        self.info_requested = False

    def _send_video(
        self,
        type: messages.VideoType,
        payload: bytes = b"",
        now: float = 0.0,
    ) -> protocol.Frame:
        self.last_command_type = type
        return self.send_command(
            self.sender_addr,
            protocol.FAMILY_VIDEO,
            type,
            payload,
            self.reliable_commands,
            0,
            now,
        )
