"""UART/serial framing for ARC control-plane frames.

Serial and radio links use COBS-encoded ARC frames terminated by 0x00.
This module provides:

- ``read_frame``/``write_frame``: low-level helpers over an asyncio
  reader/writer pair.
- ``UartFrameLink``: a long-running async loop that pumps frames in both
  directions over an existing reader/writer.
- ``QueuedUartLink``: a synchronous ``Link`` (matching ``router.Link``)
  backed by an async UART connection. ``send(frame)`` only enqueues; a
  background task owns the connection lifetime.

The async transport itself is decoupled from pyserial. Tests pass in
in-memory streams; production code wires pyserial-asyncio's
``StreamReader``/``StreamWriter`` in. See ``open_serial_connection``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress

from arc import protocol


FrameHandler = Callable[[protocol.Frame], None | Awaitable[None]]

DELIMITER = b"\x00"


class UartFrameLink:
    """Async UART link that reads/writes COBS-framed ARC frames."""

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
                if frame is None:
                    continue
                if self.on_frame is not None:
                    result = self.on_frame(frame)
                    if result is not None:
                        await result
        except (EOFError, asyncio.IncompleteReadError):
            self.closed = True

    def close(self) -> None:
        self.writer.close()

    async def wait_closed(self) -> None:
        await _wait_closed_quietly(self.writer)


class QueuedUartLink:
    """Synchronous router ``Link`` backed by an async UART connection.

    Mirrors ``QueuedTcpLink``: ``send(frame)`` enqueues; a background task
    holds the connection and pumps frames. Bad frames (CRC, COBS, length)
    are dropped with a counter rather than terminating the link, since a
    real serial line will see occasional corruption.
    """

    def __init__(
        self,
        on_frame: FrameHandler,
        max_queue: int = 100,
    ) -> None:
        self.on_frame = on_frame
        self.queue: asyncio.Queue[protocol.Frame] = asyncio.Queue(maxsize=max_queue)
        self.online = False
        self.dropped = 0
        self.bad_frames = 0
        self._stopping = False
        self._writer: asyncio.StreamWriter | None = None

    def send(self, frame: protocol.Frame) -> None:
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            self.dropped += 1

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
            if frame is None:
                self.bad_frames += 1
                continue
            result = self.on_frame(frame)
            if result is not None:
                await result


async def write_frame(writer: asyncio.StreamWriter, frame: protocol.Frame) -> None:
    """COBS-encode a frame and write it to a serial stream."""

    encoded = protocol.encode_frame(frame)
    writer.write(encoded)
    await writer.drain()


async def read_frame(reader: asyncio.StreamReader) -> protocol.Frame | None:
    """Read one COBS-delimited frame from a serial stream.

    Returns ``None`` when the frame is malformed (bad COBS / bad CRC /
    bad length); callers can resync to the next 0x00 by calling again.
    Raises ``asyncio.IncompleteReadError`` on EOF, matching
    ``StreamReader.readuntil``.
    """

    raw = await reader.readuntil(DELIMITER)
    if raw == DELIMITER:
        return None
    try:
        return protocol.decode_frame(raw)
    except protocol.ArcProtocolError:
        return None


async def _wait_closed_quietly(writer: asyncio.StreamWriter) -> None:
    with suppress(OSError, ConnectionError, TimeoutError):
        await asyncio.wait_for(writer.wait_closed(), timeout=0.2)
