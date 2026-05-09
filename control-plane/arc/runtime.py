"""asyncio runtime helpers shared by Controller and Sender entrypoints.

These pieces wire the transport-free protocol/control-plane primitives
into a real event loop. They are deliberately small: each function does
one job (open a UART, accept Sender connections, drive ticks). The two
process-level entrypoints in ``controller_main`` and ``sender_main``
compose them.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

from arc import protocol
from arc.tcp_link import QueuedTcpLink, read_frame as tcp_read_frame, write_frame as tcp_write_frame
from arc.uart_link import QueuedUartLink


FrameSink = Callable[[protocol.Frame, float], None]


def now() -> float:
    """Monotonic clock used everywhere. Asyncio loop time matches this."""

    return asyncio.get_running_loop().time()


async def open_serial_link(device: str, baud: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a pyserial-asyncio stream and return reader/writer.

    Imported lazily so the rest of the package stays importable on hosts
    without pyserial installed (e.g. CI).
    """

    import serial_asyncio  # type: ignore[import-not-found]

    reader, writer = await serial_asyncio.open_serial_connection(
        url=device, baudrate=baud
    )
    return reader, writer


async def run_uart_link(
    link: QueuedUartLink,
    device: str,
    baud: int,
    reconnect_delay_s: float = 1.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Open a UART, hold it, reconnect on failure until stop_event is set."""

    while True:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            reader, writer = await open_serial_link(device, baud)
        except OSError:
            await asyncio.sleep(reconnect_delay_s)
            continue
        await link.run_connected(reader, writer)
        if stop_event is not None and stop_event.is_set():
            return
        await asyncio.sleep(reconnect_delay_s)


async def run_tick_loop(
    tick: Callable[[float], Awaitable[None] | None],
    interval_s: float = 0.05,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Call ``tick(now)`` on a steady cadence until stop_event is set."""

    loop = asyncio.get_running_loop()
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        result = tick(loop.time())
        if asyncio.iscoroutine(result):
            await result
        await asyncio.sleep(interval_s)


class TcpServer:
    """asyncio.start_server wrapper that binds an accepted connection to a link.

    Used Controller-side: each accepted connection is assigned to the
    pre-existing ``QueuedTcpLink`` for the Sender that connected,
    identified by source IP. If no Sender matches, the connection is
    closed.
    """

    def __init__(
        self,
        host: str,
        port: int,
        link_for_peer: Callable[[str], QueuedTcpLink | None],
    ) -> None:
        self.host = host
        self.port = port
        self.link_for_peer = link_for_peer
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        with contextlib.suppress(OSError):
            await self._server.wait_closed()
        self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        peer_ip = peer[0] if peer else ""
        link = self.link_for_peer(peer_ip)
        if link is None:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            return
        await link.run_connected(reader, writer)


# Re-export for convenience.
__all__ = [
    "FrameSink",
    "QueuedTcpLink",
    "QueuedUartLink",
    "TcpServer",
    "now",
    "open_serial_link",
    "run_tick_loop",
    "run_uart_link",
    "tcp_read_frame",
    "tcp_write_frame",
]
