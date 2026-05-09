"""TCP stream framing for ARC control-plane frames."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress

from arc import protocol


FrameHandler = Callable[[protocol.Frame], None | Awaitable[None]]


class TcpFrameLink:
    """Async TCP link that writes ARC frames using the LEN-prefixed format."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        on_frame: FrameHandler | None = None,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.on_frame = on_frame
        self.closed = False

    async def send(self, frame: protocol.Frame) -> None:
        await write_frame(self.writer, frame)

    async def run(self) -> None:
        try:
            while True:
                frame = await read_frame(self.reader)
                if self.on_frame is not None:
                    result = self.on_frame(frame)
                    if result is not None:
                        await result
        except (EOFError, asyncio.IncompleteReadError, ConnectionError):
            self.closed = True

    def close(self) -> None:
        self.writer.close()

    async def wait_closed(self) -> None:
        await _wait_closed_quietly(self.writer)


class QueuedTcpLink:
    """Synchronous router link backed by an async TCP connection.

    send(frame) only queues the frame. A background client or accepted-server
    task owns connection lifetime and marks the link online/offline.
    """

    def __init__(
        self,
        on_frame: FrameHandler,
        reconnect_delay_s: float = 1.0,
        max_queue: int = 100,
    ) -> None:
        self.on_frame = on_frame
        self.reconnect_delay_s = reconnect_delay_s
        self.queue: asyncio.Queue[protocol.Frame] = asyncio.Queue(maxsize=max_queue)
        self.online = False
        self.dropped = 0
        self._stopping = False
        self._writer: asyncio.StreamWriter | None = None

    def send(self, frame: protocol.Frame) -> None:
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            self.dropped += 1

    async def run_client(self, host: str, port: int) -> None:
        while not self._stopping:
            try:
                reader, writer = await asyncio.open_connection(host, port)
            except OSError:
                self.online = False
                await asyncio.sleep(self.reconnect_delay_s)
                continue

            await self.run_connected(reader, writer)
            if not self._stopping:
                await asyncio.sleep(self.reconnect_delay_s)

    async def run_connected(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._writer = writer
        self.online = True
        tx_task = asyncio.create_task(self._tx_loop(writer))
        rx_task = asyncio.create_task(self._rx_loop(reader))
        try:
            await asyncio.wait(
                {tx_task, rx_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            self.online = False
            for task in (tx_task, rx_task):
                if not task.done():
                    task.cancel()
            for task in (tx_task, rx_task):
                with suppress(
                    asyncio.CancelledError,
                    EOFError,
                    OSError,
                    ConnectionError,
                    asyncio.IncompleteReadError,
                ):
                    await task
            writer.close()
            await _wait_closed_quietly(writer)
            self._writer = None

    async def stop(self) -> None:
        self._stopping = True
        if self._writer is not None:
            self._writer.close()
            await _wait_closed_quietly(self._writer)

    async def _tx_loop(self, writer: asyncio.StreamWriter) -> None:
        while True:
            frame = await self.queue.get()
            try:
                await write_frame(writer, frame)
            finally:
                self.queue.task_done()

    async def _rx_loop(self, reader: asyncio.StreamReader) -> None:
        while True:
            frame = await read_frame(reader)
            result = self.on_frame(frame)
            if result is not None:
                await result


async def write_frame(writer: asyncio.StreamWriter, frame: protocol.Frame) -> None:
    """Write one unencoded ARC frame to a TCP stream."""

    writer.write(protocol.build_frame(
        frame.src,
        frame.dst,
        frame.flags,
        frame.session,
        frame.seq,
        frame.family,
        frame.type,
        frame.payload,
    ))
    await writer.drain()


async def read_frame(reader: asyncio.StreamReader) -> protocol.Frame:
    """Read one LEN-prefixed ARC frame from a TCP stream."""

    len_byte = await reader.readexactly(1)
    body_len = len_byte[0]
    if body_len + 1 > protocol.MAX_FRAME_SIZE:
        raise protocol.ArcBufferError("TCP frame exceeds maximum ARC frame size")

    body = await reader.readexactly(body_len)
    return protocol.parse_frame(len_byte + body)


async def _wait_closed_quietly(writer: asyncio.StreamWriter) -> None:
    with suppress(OSError, ConnectionError, TimeoutError):
        await asyncio.wait_for(writer.wait_closed(), timeout=0.2)
