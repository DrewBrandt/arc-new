"""Small ACK/retry/dedup state machine for ARC endpoints."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from arc import protocol


SendFrame = Callable[[protocol.Frame], None]
DeliverFrame = Callable[[protocol.Frame], None]
FailFrame = Callable[[protocol.Frame], None]


@dataclass
class _Pending:
    frame: protocol.Frame
    deadline: float
    retries: int = 0


@dataclass(frozen=True)
class ReceiveResult:
    action: str


class ReliableEndpoint:
    """Reliability for frames originated or terminated by one ARC node."""

    def __init__(
        self,
        my_addr: int,
        session: int,
        send_frame: SendFrame,
        deliver_frame: DeliverFrame,
        fail_frame: FailFrame | None = None,
        timeout_s: float = 1.0,
        max_retries: int = 3,
        first_seq: int = 0,
    ) -> None:
        self.my_addr = my_addr
        self.session = session
        self.send_frame = send_frame
        self.deliver_frame = deliver_frame
        self.fail_frame = fail_frame
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._next_seq = first_seq & 0xFFFF
        self._pending: dict[tuple[int, int, int], _Pending] = {}
        self._seen: set[tuple[int, int, int]] = set()
        self._source_sessions: dict[int, int] = {}

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def send(
        self,
        dst: int,
        family: int,
        type: int,
        payload: bytes = b"",
        reliable: bool = False,
        flags: int = 0,
        now: float = 0.0,
    ) -> protocol.Frame:
        if reliable:
            flags |= protocol.FLAG_RELIABLE

        frame = protocol.Frame(
            src=self.my_addr,
            dst=dst,
            flags=flags,
            session=self.session,
            seq=self._take_seq(),
            family=family,
            type=type,
            payload=bytes(payload),
        )
        if reliable:
            self._pending[self._pending_key(frame)] = _Pending(
                frame=frame,
                deadline=now + self.timeout_s,
            )
        self.send_frame(frame)
        return frame

    def receive(self, frame: protocol.Frame) -> ReceiveResult:
        if frame.dst != self.my_addr:
            return ReceiveResult("ignored")

        if _is_ack(frame):
            self._handle_ack(frame)
            return ReceiveResult("ack")

        if frame.flags & protocol.FLAG_RELIABLE:
            self._reset_seen_if_session_changed(frame)
            seen_key = (frame.src, frame.session, frame.seq)
            self._send_ack(frame)
            if seen_key in self._seen:
                return ReceiveResult("duplicate")
            self._seen.add(seen_key)

        self.deliver_frame(frame)
        return ReceiveResult("delivered")

    def tick(self, now: float) -> None:
        expired = [
            key
            for key, pending in self._pending.items()
            if now >= pending.deadline
        ]
        for key in expired:
            pending = self._pending.get(key)
            if pending is None:
                continue

            if pending.retries >= self.max_retries:
                del self._pending[key]
                if self.fail_frame is not None:
                    self.fail_frame(pending.frame)
                continue

            pending.retries += 1
            pending.deadline = now + self.timeout_s
            self.send_frame(pending.frame)

    def _take_seq(self) -> int:
        seq = self._next_seq
        self._next_seq = (self._next_seq + 1) & 0xFFFF
        return seq

    def _send_ack(self, frame: protocol.Frame) -> None:
        ack_payload = bytes((frame.seq >> 8, frame.seq & 0xFF))
        ack = protocol.Frame(
            src=self.my_addr,
            dst=frame.src,
            flags=protocol.FLAG_ACK,
            session=self.session,
            seq=self._take_seq(),
            family=protocol.FAMILY_NETMGMT,
            type=protocol.NETMGMT_ACK,
            payload=ack_payload,
        )
        self.send_frame(ack)

    def _handle_ack(self, frame: protocol.Frame) -> None:
        if len(frame.payload) != 2:
            return
        acked_seq = (frame.payload[0] << 8) | frame.payload[1]
        self._pending.pop((frame.src, self.session, acked_seq), None)

    def _reset_seen_if_session_changed(self, frame: protocol.Frame) -> None:
        old_session = self._source_sessions.get(frame.src)
        if old_session == frame.session:
            return
        self._source_sessions[frame.src] = frame.session
        self._seen = {
            key
            for key in self._seen
            if key[0] != frame.src
        }

    @staticmethod
    def _pending_key(frame: protocol.Frame) -> tuple[int, int, int]:
        return (frame.dst, frame.session, frame.seq)


def _is_ack(frame: protocol.Frame) -> bool:
    return (
        bool(frame.flags & protocol.FLAG_ACK)
        and frame.family == protocol.FAMILY_NETMGMT
        and frame.type == protocol.NETMGMT_ACK
    )
