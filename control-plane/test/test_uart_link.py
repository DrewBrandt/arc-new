import asyncio
import unittest
from contextlib import suppress

from arc import protocol as p
from arc.node import Node
from arc.uart_link import (
    DELIMITER,
    QueuedUartLink,
    UartFrameLink,
    read_frame,
    write_frame,
)


def sample_frame(seq=1, payload=b"hello"):
    return p.Frame(
        src=p.ADDR_FC_C,
        dst=p.ADDR_SENDER_C,
        flags=p.FLAG_RELIABLE,
        session=4,
        seq=seq,
        family=p.FAMILY_FC_COORD,
        type=0x10,
        payload=payload,
    )


class _PipeWriter:
    """asyncio.StreamWriter stand-in that feeds a StreamReader."""

    def __init__(self, sink: asyncio.StreamReader) -> None:
        self._sink = sink
        self._closed = False

    def write(self, data: bytes) -> None:
        if self._closed:
            raise ConnectionError("writer closed")
        self._sink.feed_data(bytes(data))

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._sink.feed_eof()

    async def wait_closed(self) -> None:
        return None


def make_pipe() -> tuple[
    asyncio.StreamReader,
    _PipeWriter,
    asyncio.StreamReader,
    _PipeWriter,
]:
    """Two-way in-memory byte pipe (a_reader, a_writer, b_reader, b_writer)."""

    a_to_b = asyncio.StreamReader()
    b_to_a = asyncio.StreamReader()
    a_writer = _PipeWriter(a_to_b)  # a writes -> b reads
    b_writer = _PipeWriter(b_to_a)  # b writes -> a reads
    return b_to_a, a_writer, a_to_b, b_writer


class UartFramingTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_then_read_round_trip(self):
        _a_reader, a_writer, b_reader, _b_writer = make_pipe()
        await write_frame(a_writer, sample_frame(seq=7))

        decoded = await read_frame(b_reader)
        self.assertEqual(decoded, sample_frame(seq=7))

    async def test_read_returns_none_on_bad_cobs(self):
        reader = asyncio.StreamReader()
        reader.feed_data(b"\x05\xff\xff\xff\xff\x00")
        self.assertIsNone(await read_frame(reader))

    async def test_read_skips_lone_delimiter(self):
        reader = asyncio.StreamReader()
        reader.feed_data(DELIMITER)
        self.assertIsNone(await read_frame(reader))

    async def test_read_raises_eof_on_closed_stream(self):
        reader = asyncio.StreamReader()
        reader.feed_eof()
        with self.assertRaises(asyncio.IncompleteReadError):
            await read_frame(reader)

    async def test_payload_with_zero_byte_round_trips(self):
        frame = sample_frame(seq=2, payload=b"\x00\x01\x00\x02")
        _a_reader, a_writer, b_reader, _b_writer = make_pipe()
        await write_frame(a_writer, frame)
        self.assertEqual(await read_frame(b_reader), frame)


class UartFrameLinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_round_trip_through_two_links(self):
        a_reader, a_writer, b_reader, b_writer = make_pipe()
        a_received: list[p.Frame] = []
        b_received: list[p.Frame] = []

        a_link = UartFrameLink(a_reader, a_writer, a_received.append)
        b_link = UartFrameLink(b_reader, b_writer, b_received.append)

        a_task = asyncio.create_task(a_link.run())
        b_task = asyncio.create_task(b_link.run())
        try:
            await a_link.send(sample_frame(seq=1))
            await b_link.send(sample_frame(seq=2, payload=b"resp"))

            await _wait_until(lambda: len(a_received) == 1 and len(b_received) == 1)
            self.assertEqual(b_received[0].seq, 1)
            self.assertEqual(a_received[0].seq, 2)
        finally:
            a_link.close()
            b_link.close()
            await _quietly_finish(a_task, b_task)


class QueuedUartLinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_carries_node_traffic_with_reliability(self):
        a_node = Node(
            addr=p.ADDR_FC_C,
            routes={},
            default_route="peer",
            session=3,
            first_seq=0x100,
        )
        b_node = Node(
            addr=p.ADDR_SENDER_C,
            routes={p.ADDR_FC_C: "peer"},
            session=9,
        )

        a_reader, a_writer, b_reader, b_writer = make_pipe()
        a_link = QueuedUartLink(a_node.receive)
        b_link = QueuedUartLink(b_node.receive)
        a_node.set_links({"peer": a_link})
        b_node.set_links({"peer": b_link})

        a_task = asyncio.create_task(a_link.run_connected(a_reader, a_writer))
        b_task = asyncio.create_task(b_link.run_connected(b_reader, b_writer))
        try:
            await _wait_until(lambda: a_link.online and b_link.online)
            sent = a_node.send_local(
                dst=p.ADDR_SENDER_C,
                family=p.FAMILY_FC_COORD,
                type=0x05,
                payload=b"telemetry",
                reliable=True,
                now=0.0,
            )
            await _wait_until(lambda: a_node.reliable.pending_count == 0)
            self.assertEqual(b_node.inbox, [sent])
            self.assertEqual(a_node.failed, [])
        finally:
            await a_link.stop()
            await b_link.stop()
            await _quietly_finish(a_task, b_task)

    async def test_drops_corrupted_frame_without_breaking_link(self):
        b_received: list[p.Frame] = []
        _a_reader, a_writer, b_reader, b_writer = make_pipe()
        link = QueuedUartLink(b_received.append)

        run_task = asyncio.create_task(link.run_connected(b_reader, b_writer))
        try:
            good = p.encode_frame(sample_frame(seq=3))
            garbage = b"\x05\xff\xff\xff\xff\x00"
            another = p.encode_frame(sample_frame(seq=4))
            a_writer.write(good + garbage + another)
            await _wait_until(lambda: len(b_received) >= 2)
            self.assertEqual([f.seq for f in b_received], [3, 4])
            self.assertGreaterEqual(link.bad_frames, 1)
        finally:
            await link.stop()
            await _quietly_finish(run_task)

    async def test_send_drops_when_queue_full(self):
        link = QueuedUartLink(lambda _f: None, max_queue=1)
        link.send(sample_frame(seq=1))
        link.send(sample_frame(seq=2))
        self.assertEqual(link.dropped, 1)


async def _wait_until(predicate, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("timed out waiting for condition")


async def _quietly_finish(*tasks: asyncio.Task) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError, asyncio.IncompleteReadError, EOFError):
            await task


if __name__ == "__main__":
    unittest.main()
