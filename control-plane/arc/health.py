"""Heartbeat emission and peer-liveness tracking.

The protocol defines a NETMGMT_HEARTBEAT frame sent on a steady cadence.
Receivers track when each peer last spoke (any frame counts, not just
heartbeats) and surface peers as offline once a timeout elapses, per the
failure modes described in the design doc Section 12.4.

This module is transport-free. Hook it up to ``Node.send_local`` /
``Node.receive`` in an asyncio entrypoint with a periodic ``tick``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from arc import protocol
from arc.router import RouteError


SendFrame = Callable[
    [int, int, int, bytes, bool, int, float],
    protocol.Frame,
]


@dataclass
class _PeerState:
    last_seen: float
    last_session: int | None = None
    online: bool = False


class Heartbeat:
    """Periodically emit NETMGMT_HEARTBEAT to a single destination."""

    def __init__(
        self,
        send_local: SendFrame,
        dst: int,
        interval_s: float = 1.0,
        first_at: float | None = None,
    ) -> None:
        self.send_local = send_local
        self.dst = dst
        self.interval_s = interval_s
        self._next_at: float | None = first_at

    def tick(self, now: float) -> protocol.Frame | None:
        """Emit a heartbeat if it's time. Returns the frame if sent."""

        if self._next_at is None:
            self._next_at = now
        if now < self._next_at:
            return None

        # The link may be unconfigured (test harness) or temporarily down.
        # Heartbeats are best-effort by design, so swallow RouteError and
        # try again next interval.
        try:
            frame = self.send_local(
                self.dst,
                protocol.FAMILY_NETMGMT,
                protocol.NETMGMT_HEARTBEAT,
                b"",
                False,
                0,
                now,
            )
        except RouteError:
            frame = None
        self._next_at = now + self.interval_s
        return frame


class PeerHealth:
    """Track which peers are alive based on observed traffic."""

    def __init__(
        self,
        peers: Iterable[int],
        timeout_s: float = 3.0,
    ) -> None:
        self.timeout_s = timeout_s
        self._peers: dict[int, _PeerState] = {
            addr: _PeerState(last_seen=float("-inf")) for addr in peers
        }

    def add_peer(self, addr: int) -> None:
        if addr not in self._peers:
            self._peers[addr] = _PeerState(last_seen=float("-inf"))

    def observe(self, frame: protocol.Frame, now: float) -> None:
        """Update peer state from any received frame."""

        state = self._peers.get(frame.src)
        if state is None:
            return
        state.last_seen = now
        if (
            state.last_session is not None
            and state.last_session != frame.session
        ):
            # Peer rebooted; treat as freshly online.
            state.online = False
        state.last_session = frame.session
        state.online = True

    def is_online(self, addr: int) -> bool:
        state = self._peers.get(addr)
        return bool(state and state.online)

    def offline_peers(self, now: float) -> list[int]:
        """Return peers that have just transitioned to offline."""

        transitions: list[int] = []
        for addr, state in self._peers.items():
            if state.online and (now - state.last_seen) >= self.timeout_s:
                state.online = False
                transitions.append(addr)
        return transitions

    def last_seen(self, addr: int) -> float | None:
        state = self._peers.get(addr)
        if state is None or state.last_seen == float("-inf"):
            return None
        return state.last_seen

    def peers(self) -> list[int]:
        return list(self._peers.keys())
