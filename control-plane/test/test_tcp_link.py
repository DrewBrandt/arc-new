import asyncio
import unittest
from contextlib import suppress

from arc import protocol as p
from arc.node import Node
from arc.tcp_link import QueuedTcpLink, TcpFrameLink, read_frame, write_frame


def sample_frame(seq=1, payload=b"hello"):
    return p.Frame(
        src=p.ADDR_SENDER_C,
        dst=p.ADDR_CONTROLLER,
        flags=p.FLAG_RELIABLE,
        session=3,
        seq=seq,
        family=p.FAMILY_VIDEO,
        type=0x10,
        payload=payload,
    )


class ChunkedReader:
    def __init__(self, chunks):
        self.chunks = [bytearray(chunk) for chunk in chunks]

    async def readexactly(self, n):
        out = bytearray()
        while len(out) < n:
            if not self.chunks:
                raise asyncio.IncompleteReadError(bytes(out), n)
            chunk = self.chunks[0]
            take = min(n - len(out), len(chunk))
            out.extend(chunk[:take])
            del chunk[:take]
            if not chunk:
                self.chunks.pop(0)
        return bytes(out)


class TcpLinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_localhost_write_and_read_one_frame(self):
        received = []
        done = asyncio.Event()

        async def handle(reader, writer):
            try:
                received.append(await read_frame(reader))
            finally:
                done.set()
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            frame = sample_frame()
            await write_frame(writer, frame)
            writer.close()
            await writer.wait_closed()

            await asyncio.wait_for(done.wait(), timeout=1.0)
            self.assertEqual(received, [frame])
        finally:
            server.close()
            await server.wait_closed()

    async def test_read_frame_handles_fragmented_stream(self):
        frame = sample_frame(payload=b"a\x00b")
        raw = p.build_frame(
            frame.src,
            frame.dst,
            frame.flags,
            frame.session,
            frame.seq,
            frame.family,
            frame.type,
            frame.payload,
        )
        reader = ChunkedReader([raw[:1], raw[1:3], raw[3:7], raw[7:]])

        self.assertEqual(await read_frame(reader), frame)

    async def test_read_frame_rejects_oversized_len(self):
        reader = ChunkedReader([bytes([p.MAX_FRAME_SIZE])])

        with self.assertRaises(p.ArcBufferError):
            await read_frame(reader)

    async def test_tcp_frame_link_round_trip_ack(self):
        server_frames = []
        client_frames = []
        server_link_ready = asyncio.Event()

        async def server_on_frame(frame):
            server_frames.append(frame)
            await server_link.send(
                p.Frame(
                    src=frame.dst,
                    dst=frame.src,
                    flags=p.FLAG_ACK,
                    session=9,
                    seq=500,
                    family=p.FAMILY_NETMGMT,
                    type=p.NETMGMT_ACK,
                    payload=bytes((frame.seq >> 8, frame.seq & 0xFF)),
                )
            )

        async def handle(reader, writer):
            nonlocal server_link
            server_link = TcpFrameLink(reader, writer, server_on_frame)
            server_link_ready.set()
            await server_link.run()

        server_link = None
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            client_link = TcpFrameLink(reader, writer, client_frames.append)
            client_task = asyncio.create_task(client_link.run())
            await asyncio.wait_for(server_link_ready.wait(), timeout=1.0)

            frame = sample_frame(seq=0x1234)
            await client_link.send(frame)

            deadline = asyncio.get_running_loop().time() + 1.0
            while len(client_frames) < 1 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0)

            self.assertEqual(server_frames, [frame])
            self.assertEqual(len(client_frames), 1)
            ack = client_frames[0]
            self.assertEqual(ack.src, p.ADDR_CONTROLLER)
            self.assertEqual(ack.dst, p.ADDR_SENDER_C)
            self.assertEqual(ack.payload, bytes.fromhex("1234"))

            client_link.close()
            await client_link.wait_closed()
            await asyncio.wait_for(client_task, timeout=1.0)
        finally:
            server.close()
            await server.wait_closed()

    async def test_queued_tcp_link_carries_reliable_node_flow(self):
        client = Node(
            addr=p.ADDR_SENDER_C,
            routes={},
            default_route="server",
            session=3,
            first_seq=0x1234,
        )
        server_node = Node(
            addr=p.ADDR_CONTROLLER,
            routes={p.ADDR_SENDER_C: "client"},
            session=9,
        )
        server_link = QueuedTcpLink(server_node.receive, reconnect_delay_s=0.01)

        async def handle(reader, writer):
            await server_link.run_connected(reader, writer)

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client_link = QueuedTcpLink(client.receive, reconnect_delay_s=0.01)
        client.set_links({"server": client_link})
        server_node.set_links({"client": server_link})
        client_task = asyncio.create_task(client_link.run_client("127.0.0.1", port))
        try:
            await wait_until(lambda: client_link.online and server_link.online)

            sent = client.send_local(
                dst=p.ADDR_CONTROLLER,
                family=p.FAMILY_VIDEO,
                type=0x01,
                payload=b"start",
                reliable=True,
                now=1.0,
            )

            await wait_until(lambda: client.reliable.pending_count == 0)
            self.assertEqual(server_node.inbox, [sent])
            self.assertEqual(client.failed, [])
            self.assertEqual(server_node.failed, [])
        finally:
            await client_link.stop()
            await server_link.stop()
            client_task.cancel()
            with suppress(asyncio.CancelledError):
                await client_task
            server.close()
            await server.wait_closed()

    async def test_queued_tcp_client_reconnects_without_blocking_sends(self):
        received = []
        accepted_writers = []

        client_link = QueuedTcpLink(received.append, reconnect_delay_s=0.01)
        frame = sample_frame(seq=7)

        async def handle(reader, writer):
            accepted_writers.append(writer)
            try:
                while True:
                    received.append(await read_frame(reader))
            except EOFError:
                pass
            finally:
                writer.close()
                with suppress(OSError, ConnectionError, TimeoutError):
                    await asyncio.wait_for(writer.wait_closed(), timeout=0.2)

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client_task = asyncio.create_task(client_link.run_client("127.0.0.1", port))
        try:
            await wait_until(lambda: client_link.online and len(accepted_writers) == 1)
            accepted_writers[0].close()
            await wait_until(lambda: len(accepted_writers) >= 2)

            client_link.send(frame)
            await wait_until(lambda: len(received) == 1)
            self.assertEqual(received, [frame])
        finally:
            await client_link.stop()
            client_task.cancel()
            with suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(client_task, timeout=0.2)
            for writer in accepted_writers:
                writer.close()
                with suppress(OSError, ConnectionError):
                    await asyncio.wait_for(writer.wait_closed(), timeout=0.2)
            server.close()
            await server.wait_closed()


async def wait_until(predicate, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("timed out waiting for condition")


if __name__ == "__main__":
    unittest.main()
