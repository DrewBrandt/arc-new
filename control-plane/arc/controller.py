"""Small Controller orchestration shell for ARC control-plane pieces."""

from __future__ import annotations

from collections.abc import Mapping

from arc import messages, protocol
from arc.health import Heartbeat, PeerHealth
from arc.node import Node
from arc.router import Link, controller_routes
from arc.sender_link import SenderLink, SenderLinkError


class ControllerError(RuntimeError):
    """Raised when a local frame cannot be handled by the Controller."""


class Controller:
    """Compose routing, reliability, and Sender command/status handling."""

    def __init__(
        self,
        links: Mapping[str, Link] | None = None,
        sender_addrs: tuple[int, ...] = (
            protocol.ADDR_SENDER_N,
            protocol.ADDR_SENDER_C,
            protocol.ADDR_SENDER_L1,
            protocol.ADDR_SENDER_L2,
            protocol.ADDR_SENDER_GROUND,
        ),
        session: int = 1,
        timeout_s: float = 1.0,
        max_retries: int = 3,
        first_seq: int = 0,
        fc_n_addr: int = protocol.ADDR_FC_N,
        heartbeat_interval_s: float = 1.0,
        peer_timeout_s: float = 3.0,
    ) -> None:
        self.node = Node(
            addr=protocol.ADDR_CONTROLLER,
            routes=controller_routes(),
            links=links,
            session=session,
            timeout_s=timeout_s,
            max_retries=max_retries,
            first_seq=first_seq,
        )
        self.senders = {
            addr: SenderLink(addr, self.node.send_local)
            for addr in sender_addrs
        }
        self.fc_n_addr = fc_n_addr
        self.heartbeat = Heartbeat(
            self.node.send_local,
            dst=fc_n_addr,
            interval_s=heartbeat_interval_s,
        )
        self.health = PeerHealth(
            peers=(*sender_addrs, fc_n_addr),
            timeout_s=peer_timeout_s,
        )
        self.unhandled_frames: list[protocol.Frame] = []

    def set_links(self, links: Mapping[str, Link]) -> None:
        self.node.set_links(links)

    def receive(self, frame: protocol.Frame, now: float = 0.0) -> None:
        """Route an incoming frame and handle any resulting local deliveries."""

        self.health.observe(frame, now=now)
        before = len(self.node.inbox)
        self.node.receive(frame)
        for delivered in self.node.inbox[before:]:
            self._handle_local_frame(delivered, now=now)

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
        if frame.family == protocol.FAMILY_VIDEO and frame.type == messages.VideoType.STATUS_REPORT:
            sender = self.senders.get(frame.src)
            if sender is None:
                raise ControllerError(f"status from unknown sender 0x{frame.src:02x}")
            try:
                sender.handle_frame(frame, now=now)
            except SenderLinkError as exc:
                raise ControllerError(str(exc)) from exc
            return

        if frame.family == protocol.FAMILY_NETMGMT and frame.type == protocol.NETMGMT_HEARTBEAT:
            # PeerHealth already absorbed it via observe(); nothing else to do.
            return

        self.unhandled_frames.append(frame)
