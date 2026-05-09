"""Small in-memory node harness for control-plane tests."""

from __future__ import annotations

from collections.abc import Mapping

from arc import protocol
from arc.reliable import ReliableEndpoint
from arc.router import Link, Router


class Node:
    """Compose routing and reliability for one ARC node.

    This is intentionally transport-free. Real TCP/UART code can later supply
    Link implementations with the same send(frame) shape used by the tests.
    """

    def __init__(
        self,
        addr: int,
        routes: Mapping[int, str],
        links: Mapping[str, Link] | None = None,
        default_route: str | None = None,
        session: int = 1,
        timeout_s: float = 1.0,
        max_retries: int = 3,
        first_seq: int = 0,
    ) -> None:
        self.addr = addr
        self.inbox: list[protocol.Frame] = []
        self.failed: list[protocol.Frame] = []
        self.router = Router(
            my_addr=addr,
            routes=routes,
            links=links or {},
            local_handler=self._receive_local,
            default_route=default_route,
        )
        self.reliable = ReliableEndpoint(
            my_addr=addr,
            session=session,
            send_frame=self._send_from_reliable,
            deliver_frame=self.inbox.append,
            fail_frame=self.failed.append,
            timeout_s=timeout_s,
            max_retries=max_retries,
            first_seq=first_seq,
        )

    def set_links(self, links: Mapping[str, Link]) -> None:
        self.router.links = dict(links)

    def send_local(
        self,
        dst: int,
        family: int,
        type: int,
        payload: bytes = b"",
        reliable: bool = False,
        flags: int = 0,
        now: float = 0.0,
    ) -> protocol.Frame:
        return self.reliable.send(
            dst=dst,
            family=family,
            type=type,
            payload=payload,
            reliable=reliable,
            flags=flags,
            now=now,
        )

    def receive(self, frame: protocol.Frame) -> None:
        self.router.route(frame)

    def tick(self, now: float) -> None:
        self.reliable.tick(now)

    def _receive_local(self, frame: protocol.Frame) -> None:
        self.reliable.receive(frame)

    def _send_from_reliable(self, frame: protocol.Frame) -> None:
        self.router.route(frame)

