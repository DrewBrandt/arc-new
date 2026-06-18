import asyncio
import unittest

from arc_protocol import protocol
from arc.hitl_bridge import (
    DEFAULT_HITL_ADDR,
    FC_COORD_HITL_SENSOR_SAMPLE,
    ArcHitlBridge,
    PhysicsSampleSource,
    parse_dst_list,
)


class _MemoryWriter:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        return None


class HitlBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_sample_fans_out_to_fc_destinations(self):
        writer = _MemoryWriter()
        source = PhysicsSampleSource(rate_hz=50.0)
        sample = source.next_sample()
        bridge = ArcHitlBridge(
            controller_host="127.0.0.1",
            controller_port=6000,
            dst_addrs=(protocol.ADDR_FC_N, protocol.ADDR_FC_C),
        )

        await bridge.send_sample(writer, sample)

        reader = asyncio.StreamReader()
        reader.feed_data(bytes(writer.data))
        reader.feed_eof()
        first = await reader.readexactly(1)
        frame1 = protocol.parse_frame(first + await reader.readexactly(first[0]))
        second = await reader.readexactly(1)
        frame2 = protocol.parse_frame(second + await reader.readexactly(second[0]))

        self.assertEqual(frame1.src, DEFAULT_HITL_ADDR)
        self.assertEqual(frame1.dst, protocol.ADDR_FC_N)
        self.assertEqual(frame1.family, protocol.FAMILY_FC_COORD)
        self.assertEqual(frame1.type, FC_COORD_HITL_SENSOR_SAMPLE)
        self.assertEqual(frame1.flags, 0)
        self.assertTrue(frame1.payload.startswith(b"HITL/"))
        self.assertEqual(frame2.dst, protocol.ADDR_FC_C)
        self.assertEqual(frame2.seq, frame1.seq + 1)

    def test_parse_dst_list_accepts_aliases(self):
        self.assertEqual(
            parse_dst_list("fc-n,airbrake,payload"),
            (protocol.ADDR_FC_N, protocol.ADDR_FC_C, protocol.ADDR_FC_L),
        )
        self.assertEqual(
            parse_dst_list("all"),
            (protocol.ADDR_FC_N, protocol.ADDR_FC_C, protocol.ADDR_FC_L),
        )


if __name__ == "__main__":
    unittest.main()
