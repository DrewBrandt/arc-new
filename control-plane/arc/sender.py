"""Sender-side orchestration shell for ARC control-plane pieces.

Symmetric to ``controller.Controller``: composes routing, reliability,
and the local STATUS_REPORT generator. Real Senders also relay frames
between the Controller (TCP) and a paired flight computer (UART); this
class doesn't own those transports, it just plugs them in as ``Link``s.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Callable

from arc_protocol import messages, protocol
from arc.health import Heartbeat, PeerHealth
from arc.node import Node
from arc_protocol.router import Link
from arc_protocol.router import sender_routes


VideoCommandHandler = Callable[["messages.VideoType", protocol.Frame], None]


class SenderError(RuntimeError):
    """Raised when a Sender cannot handle a locally-delivered frame."""


class Sender:
    """Compose routing, reliability, and the Sender command/status surface."""

    def __init__(
        self,
        addr: int,
        paired_fc: int | None = None,
        controller_addr: int = protocol.ADDR_CONTROLLER,
        links: Mapping[str, Link] | None = None,
        session: int = 1,
        timeout_s: float = 1.0,
        max_retries: int = 3,
        first_seq: int = 0,
        heartbeat_interval_s: float = 1.0,
        peer_timeout_s: float = 3.0,
        video_command_handler: VideoCommandHandler | None = None,
        name: str = "",
    ) -> None:
        self.addr = addr
        self.paired_fc = paired_fc
        self.controller_addr = controller_addr
        self.name = name
        self.node = Node(
            addr=addr,
            routes=sender_routes(paired_fc),
            links=links,
            default_route="controller",
            session=session,
            timeout_s=timeout_s,
            max_retries=max_retries,
            first_seq=first_seq,
        )

        self.transmitting = False
        self.recording = True
        self.bitrate_bps: int | None = None
        self.last_command: protocol.Frame | None = None
        self.video_command_handler = video_command_handler

        peers: tuple[int, ...] = (controller_addr,)
        if paired_fc is not None:
            peers = (controller_addr, paired_fc)
        # Broadcast (not directed at the Controller): neighbors learn our
        # address + ingress link from the heartbeat, so any node can route to
        # us. Receivers track liveness by src, so this still feeds PeerHealth.
        self.heartbeat = Heartbeat(
            self.node.send_local,
            dst=protocol.ADDR_BROADCAST,
            interval_s=heartbeat_interval_s,
        )
        self.health = PeerHealth(peers=peers, timeout_s=peer_timeout_s)
        self.unhandled_frames: list[protocol.Frame] = []

    def set_links(self, links: Mapping[str, Link]) -> None:
        self.node.set_links(links)

    def receive(
        self,
        frame: protocol.Frame,
        now: float = 0.0,
        *,
        ingress: str | None = None,
    ) -> None:
        """Route an inbound frame; handle anything delivered locally."""

        self.health.observe(frame, now=now)
        before = len(self.node.inbox)
        self.node.receive(frame, ingress=ingress, now=now)
        for delivered in self.node.inbox[before:]:
            self._handle_local_frame(delivered, now=now)

    def tick(self, now: float) -> list[int]:
        """Advance reliability + heartbeat. Returns peers that just went offline."""

        self.node.tick(now)
        self.heartbeat.tick(now)
        return self.health.offline_peers(now)

    def report_status(
        self,
        report: messages.StatusReport,
        now: float = 0.0,
        reliable: bool = False,
    ) -> protocol.Frame:
        """Emit a VIDEO STATUS_REPORT to the Controller."""

        return self.node.send_local(
            dst=self.controller_addr,
            family=protocol.FAMILY_VIDEO,
            type=messages.VideoType.STATUS_REPORT,
            payload=report.encode(),
            reliable=reliable,
            now=now,
        )

    def _handle_local_frame(self, frame: protocol.Frame, now: float) -> None:
        if frame.family == protocol.FAMILY_VIDEO:
            self._apply_video_command(frame, now=now)
            return
        if frame.family == protocol.FAMILY_NETMGMT and frame.type == protocol.NETMGMT_HEARTBEAT:
            return
        # FC_COORD frames addressed to this Sender are unusual (the Sender
        # has no FC role of its own); FC_VIDEO commands are addressed to
        # the Controller, not here. Anything else is buffered for callers
        # that wire in further dispatch.
        self.unhandled_frames.append(frame)

    def report_info(self, dst: int | None = None, now: float = 0.0) -> protocol.Frame:
        """Emit a VIDEO INFO_REPORT describing this Sender.

        Sent in reply to a Controller GET_INFO so the Controller can learn
        the Sender's friendly name and paired FC at discovery time without
        having it pre-declared in config.
        """

        report = messages.VideoInfoReport(
            name=self.name,
            paired_fc=self.paired_fc
            if self.paired_fc is not None
            else protocol.ADDR_UNASSIGNED,
        )
        return self.node.send_local(
            dst=self.controller_addr if dst is None else dst,
            family=protocol.FAMILY_VIDEO,
            type=messages.VideoType.INFO_REPORT,
            payload=report.encode(),
            reliable=True,
            now=now,
        )

    def _apply_video_command(self, frame: protocol.Frame, now: float = 0.0) -> None:
        try:
            video_type = messages.VideoType(frame.type)
        except ValueError as exc:
            raise SenderError(f"unknown VIDEO type 0x{frame.type:02x}") from exc

        if video_type is messages.VideoType.GET_INFO:
            # Identity query, not a stream-state change: reply to whoever
            # asked and leave transmit/record state (and the pipeline
            # handler) untouched.
            self.report_info(dst=frame.src or self.controller_addr, now=now)
            return
        if video_type is messages.VideoType.INFO_REPORT:
            raise SenderError("Sender received its own INFO_REPORT")

        self.last_command = frame
        if video_type is messages.VideoType.START_STREAM:
            self.transmitting = True
            self.recording = True
        elif video_type is messages.VideoType.STOP_STREAM:
            self.transmitting = False
            # Recording continues per design doc Section 7.2 (soft stop).
        elif video_type is messages.VideoType.HARD_STOP:
            self.transmitting = False
            self.recording = False
        elif video_type is messages.VideoType.SET_BITRATE:
            self.bitrate_bps = messages.SetBitrate.decode(frame.payload).bitrate_bps
        elif video_type is messages.VideoType.STATUS_REPORT:
            # Senders only emit STATUS_REPORT; receiving one is a misroute.
            raise SenderError("Sender received its own STATUS_REPORT")
        else:
            raise SenderError(f"unhandled VIDEO type {video_type!r}")

        if self.video_command_handler is not None:
            self.video_command_handler(video_type, frame)
