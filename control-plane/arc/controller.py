"""Small Controller orchestration shell for ARC control-plane pieces."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Callable

from arc_protocol import messages, protocol
from arc.addressing import is_sender_addr
from arc.health import Heartbeat, PeerHealth
from arc.node import Node
from arc_protocol.router import Link, controller_routes
from arc.sender_link import SenderLink, SenderLinkError


log = logging.getLogger("arc.controller")

FcVideoHandler = Callable[["messages.FcVideoType", protocol.Frame], None]


class ControllerError(RuntimeError):
    """Raised when a local frame cannot be handled by the Controller."""


class Controller:
    """Compose routing, reliability, and Sender command/status handling."""

    def __init__(
        self,
        links: Mapping[str, Link] | None = None,
        sender_addrs: tuple[int, ...] = (),
        session: int = 1,
        timeout_s: float = 1.0,
        max_retries: int = 3,
        first_seq: int = 0,
        fc_n_addr: int = protocol.ADDR_FC_N,
        routes: Mapping[int, str] | None = None,
        extra_peer_addrs: tuple[int, ...] = (),
        heartbeat_interval_s: float = 1.0,
        peer_timeout_s: float = 3.0,
        fc_video_handler: FcVideoHandler | None = None,
        retain_local_history: bool = True,
    ) -> None:
        self.node = Node(
            addr=protocol.ADDR_CONTROLLER,
            routes=routes if routes is not None else controller_routes(),
            links=links,
            session=session,
            timeout_s=timeout_s,
            max_retries=max_retries,
            first_seq=first_seq,
        )
        self.fc_n_addr = fc_n_addr
        # Heartbeats are broadcast, not directed: any neighbor that hears one
        # learns our address and the link it arrived on, so any node can act as
        # a router. The hub still coalesces them; receivers key on src, not dst.
        self.heartbeat = Heartbeat(
            self.node.send_local,
            dst=protocol.ADDR_BROADCAST,
            interval_s=heartbeat_interval_s,
        )
        self.health = PeerHealth(
            peers=(fc_n_addr, *extra_peer_addrs),
            timeout_s=peer_timeout_s,
        )
        self.senders: dict[int, SenderLink] = {}
        for addr in sender_addrs:
            self.ensure_sender(addr)
        self.unhandled_frames: list[protocol.Frame] = []
        self.fc_video_handler = fc_video_handler
        self.retain_local_history = retain_local_history

    def set_links(self, links: Mapping[str, Link]) -> None:
        self.node.set_links(links)

    def ensure_sender(
        self,
        sender_addr: int,
        *,
        route_name: str | None = None,
    ) -> SenderLink | None:
        """Register a video sender address discovered from ARC traffic."""

        if not is_sender_addr(sender_addr):
            return None
        sender = self.senders.get(sender_addr)
        if sender is None:
            sender = SenderLink(sender_addr, self.node.send_local)
            self.senders[sender_addr] = sender
            self.health.add_peer(sender_addr)
        if route_name is not None:
            self.node.router.routes[sender_addr] = route_name
        return sender

    def receive(
        self,
        frame: protocol.Frame,
        now: float = 0.0,
        *,
        ingress: str | None = None,
    ) -> None:
        """Route an incoming frame and handle any resulting local deliveries."""

        if is_sender_addr(frame.src):
            sender = self.ensure_sender(frame.src, route_name=ingress)
            if (
                sender is not None
                and sender.name is None
                and not sender.info_requested
            ):
                # First time we've heard from this Sender (or it just
                # reconnected): ask who it is. Reliable, so the reliability
                # layer retries until the Sender acks the query.
                try:
                    sender.request_info(now=now)
                except Exception:
                    # No usable route/link yet (e.g. discovered from a relayed
                    # frame before its link is up). Clear the flag so the next
                    # frame re-asks once the link exists.
                    sender.info_requested = False
                    log.debug(
                        "deferring info request for sender 0x%02x: no route yet",
                        frame.src,
                    )
        self.health.observe(frame, now=now)
        before = len(self.node.inbox)
        self.node.receive(frame, ingress=ingress, now=now)
        for delivered in self.node.inbox[before:]:
            self._handle_local_frame(delivered, now=now)
        if not self.retain_local_history:
            self.node.inbox.clear()

    def tick(self, now: float) -> list[int]:
        """Advance reliability + heartbeat. Returns peers that just went offline."""

        self.node.tick(now)
        self.heartbeat.tick(now)
        offline = self.health.offline_peers(now)
        for addr in offline:
            sender = self.senders.get(addr)
            if sender is not None:
                sender.mark_offline()
        return offline

    def sender(self, sender_addr: int) -> SenderLink:
        try:
            return self.senders[sender_addr]
        except KeyError as exc:
            raise ControllerError(f"unknown sender 0x{sender_addr:02x}") from exc

    def start_sender(self, sender_addr: int, now: float = 0.0) -> protocol.Frame:
        return self.sender(sender_addr).start_stream(now=now)

    def stop_sender(self, sender_addr: int, now: float = 0.0) -> protocol.Frame:
        return self.sender(sender_addr).stop_stream(now=now)

    def hard_stop_sender(self, sender_addr: int, now: float = 0.0) -> protocol.Frame:
        return self.sender(sender_addr).hard_stop(now=now)

    def set_sender_bitrate(
        self,
        sender_addr: int,
        bitrate_bps: int,
        now: float = 0.0,
    ) -> protocol.Frame:
        return self.sender(sender_addr).set_bitrate(bitrate_bps, now=now)

    def _handle_local_frame(self, frame: protocol.Frame, now: float) -> None:
        if frame.family == protocol.FAMILY_VIDEO and frame.type in (
            messages.VideoType.STATUS_REPORT,
            messages.VideoType.INFO_REPORT,
        ):
            sender = self.senders.get(frame.src)
            if sender is None:
                raise ControllerError(
                    f"video report from unknown sender 0x{frame.src:02x}"
                )
            try:
                result = sender.handle_frame(frame, now=now)
            except SenderLinkError as exc:
                raise ControllerError(str(exc)) from exc
            if frame.type == messages.VideoType.INFO_REPORT:
                log.info(
                    "sender 0x%02x identified as %r (paired_fc=%s)",
                    frame.src,
                    result.name,
                    f"0x{result.paired_fc:02x}"
                    if result.paired_fc != protocol.ADDR_UNASSIGNED
                    else "none",
                )
            return

        if frame.family == protocol.FAMILY_NETMGMT and frame.type == protocol.NETMGMT_HEARTBEAT:
            # PeerHealth already absorbed it via observe(); nothing else to do.
            return

        if frame.family == protocol.FAMILY_FC_VIDEO:
            self._handle_fc_video(frame)
            return

        self.unhandled_frames.append(frame)

    def _handle_fc_video(self, frame: protocol.Frame) -> None:
        try:
            fc_video_type = messages.FcVideoType(frame.type)
        except ValueError as exc:
            raise ControllerError(
                f"unknown FC_VIDEO type 0x{frame.type:02x}"
            ) from exc

        if self.fc_video_handler is None:
            self.unhandled_frames.append(frame)
            return
        self.fc_video_handler(fc_video_type, frame)
