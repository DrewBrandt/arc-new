"""Static frame routing for the ARC control plane."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from arc import protocol


class Link(Protocol):
    """A route target that can forward parsed ARC frames."""

    def send(self, frame: protocol.Frame) -> None:
        """Forward a frame onto this link."""


class RouteError(RuntimeError):
    """Raised when a frame cannot be routed."""


@dataclass(frozen=True)
class RouteResult:
    action: str
    link_name: str | None = None


LocalHandler = Callable[[protocol.Frame], None]


class Router:
    """Route parsed ARC frames using a static destination table."""

    def __init__(
        self,
        my_addr: int,
        routes: Mapping[int, str],
        links: Mapping[str, Link],
        local_handler: LocalHandler,
        default_route: str | None = None,
    ) -> None:
        self.my_addr = my_addr
        self.routes = dict(routes)
        self.links = dict(links)
        self.local_handler = local_handler
        self.default_route = default_route

    def route(self, frame: protocol.Frame) -> RouteResult:
        if frame.dst == self.my_addr:
            self.local_handler(frame)
            return RouteResult("local")

        link_name = self.routes.get(frame.dst, self.default_route)
        if link_name is None:
            raise RouteError(f"no route for destination 0x{frame.dst:02x}")

        link = self.links.get(link_name)
        if link is None:
            raise RouteError(f"route for 0x{frame.dst:02x} uses missing link {link_name!r}")

        link.send(frame)
        return RouteResult("forwarded", link_name)


def controller_routes() -> dict[int, str]:
    """Static routes for the nosecone Controller node."""

    return {
        protocol.ADDR_GROUND: "uart-fc-n",
        protocol.ADDR_FC_N: "uart-fc-n",
        protocol.ADDR_FC_C: "sender-c",
        protocol.ADDR_FC_L: "sender-l1",
        protocol.ADDR_SENDER_N: "sender-n",
        protocol.ADDR_SENDER_C: "sender-c",
        protocol.ADDR_SENDER_L1: "sender-l1",
        protocol.ADDR_SENDER_L2: "sender-l2",
        protocol.ADDR_SENDER_GROUND: "sender-ground",
    }


def sender_routes(paired_fc: int | None = None) -> dict[int, str]:
    """Static routes for a Sender node.

    Senders use this table plus default_route="controller". If a Sender has
    a paired flight computer, frames for that FC go over its UART link.
    """

    if paired_fc is None:
        return {}
    return {paired_fc: "uart-fc"}

