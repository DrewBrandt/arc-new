"""Route ARC frames with source-address learning.

The Pi controller/senders have a few known static routes, but bench wiring and
sender TCP sessions are much nicer when traffic can teach the router where a
node lives. This mirrors the Teensy hub behavior at the Python control-plane
level: if a frame from address A arrives on link X, future frames to A prefer
link X until the route ages out.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from arc_protocol import protocol
from arc_protocol.router import Link, RouteError, RouteResult


LocalHandler = Callable[[protocol.Frame], None]


@dataclass(frozen=True)
class LearnedRoute:
    link_name: str
    seen_at: float


class LearnedRouter:
    """Router with learned source routes ahead of static/default routes."""

    def __init__(
        self,
        my_addr: int,
        routes: Mapping[int, str],
        links: Mapping[str, Link],
        local_handler: LocalHandler,
        default_route: str | None = None,
        learned_ttl_s: float = 10.0,
    ) -> None:
        self.my_addr = my_addr
        self.routes = dict(routes)
        self.links = dict(links)
        self.local_handler = local_handler
        self.default_route = default_route
        self.learned_ttl_s = learned_ttl_s
        self.learned: dict[int, LearnedRoute] = {}

    def route(
        self,
        frame: protocol.Frame,
        *,
        ingress: str | None = None,
        now: float = 0.0,
    ) -> RouteResult:
        self._learn(frame, ingress=ingress, now=now)

        if frame.dst == self.my_addr:
            self.local_handler(frame)
            return RouteResult("local")

        if frame.dst == protocol.ADDR_BROADCAST:
            self.local_handler(frame)
            for name, link in self.links.items():
                if name == ingress:
                    continue
                link.send(frame)
            return RouteResult("broadcast")

        link_name = self._link_name_for(frame.dst, now=now)
        if link_name is None:
            raise RouteError(f"no route for destination 0x{frame.dst:02x}")

        link = self.links.get(link_name)
        if link is None:
            raise RouteError(
                f"route for 0x{frame.dst:02x} uses missing link {link_name!r}"
            )

        link.send(frame)
        return RouteResult("forwarded", link_name)

    def _learn(self, frame: protocol.Frame, *, ingress: str | None, now: float) -> None:
        if ingress is None or ingress not in self.links:
            return
        if frame.src in (
            protocol.ADDR_UNASSIGNED,
            protocol.ADDR_BROADCAST,
            self.my_addr,
        ):
            return
        self.learned[frame.src] = LearnedRoute(link_name=ingress, seen_at=now)

    def _link_name_for(self, dst: int, *, now: float) -> str | None:
        learned = self.learned.get(dst)
        if learned is not None:
            if now - learned.seen_at <= self.learned_ttl_s:
                return learned.link_name
            del self.learned[dst]

        return self.routes.get(dst, self.default_route)
