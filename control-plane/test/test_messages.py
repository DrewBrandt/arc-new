import unittest

from arc import messages as m
from arc import protocol as p


class MessageTests(unittest.TestCase):
    def test_ack_round_trip(self):
        ack = m.Ack(seq=0xABCD)

        self.assertEqual(ack.encode(), bytes.fromhex("abcd"))
        self.assertEqual(m.Ack.decode(ack.encode()), ack)

    def test_video_set_bitrate_round_trip(self):
        msg = m.SetBitrate(bitrate_bps=2_500_000)

        self.assertEqual(msg.encode(), bytes.fromhex("002625a0"))
        self.assertEqual(m.SetBitrate.decode(msg.encode()), msg)

    def test_video_status_report_round_trip(self):
        msg = m.StatusReport(
            state=0x03,
            cpu_temp_c=54,
            cpu_load_pct=42,
            free_disk_mb=1200,
            rssi_dbm=-67,
            tx_frames=300,
            dropped_frames=2,
        )

        self.assertEqual(msg.encode(), bytes.fromhex("03362a04b0bd012c0002"))
        self.assertEqual(m.StatusReport.decode(msg.encode()), msg)

    def test_fc_video_set_layout_round_trip(self):
        msg = m.SetLayout(layout_id=2)

        self.assertEqual(msg.encode(), b"\x02")
        self.assertEqual(m.SetLayout.decode(msg.encode()), msg)

    def test_fc_video_set_source_round_trip(self):
        msg = m.SetSource(slot_id=1, sender_addr=p.ADDR_SENDER_L1)

        self.assertEqual(msg.encode(), bytes((1, p.ADDR_SENDER_L1)))
        self.assertEqual(m.SetSource.decode(msg.encode()), msg)

    def test_fc_video_set_overlay_round_trip(self):
        msg = m.SetOverlay(text="KD3BBP flight")

        self.assertEqual(msg.encode(), b"KD3BBP flight\x00")
        self.assertEqual(m.SetOverlay.decode(msg.encode()), msg)

    def test_empty_control_messages_reject_payloads(self):
        with self.assertRaises(m.MessageError):
            m.decode_video(m.VideoType.START_STREAM, b"nope")

        with self.assertRaises(m.MessageError):
            m.decode_fc_video(m.FcVideoType.GET_STATUS, b"nope")

    def test_invalid_lengths_rejected(self):
        cases = [
            (m.Ack.decode, b"\x00"),
            (m.SetBitrate.decode, b"\x00\x01"),
            (m.StatusReport.decode, bytes(9)),
            (m.SetLayout.decode, b""),
            (m.SetSource.decode, b"\x01"),
        ]
        for decoder, payload in cases:
            with self.subTest(decoder=decoder.__qualname__):
                with self.assertRaises(m.MessageError):
                    decoder(payload)

    def test_numeric_ranges_rejected(self):
        cases = [
            lambda: m.Ack(0x10000).encode(),
            lambda: m.SetBitrate(-1).encode(),
            lambda: m.StatusReport(0, 0, 0, 0, -129, 0, 0).encode(),
            lambda: m.StatusReport(0, 101, 0, 0x10000, 0, 0, 0).encode(),
            lambda: m.SetLayout(0x100).encode(),
            lambda: m.SetSource(0, 0x100).encode(),
        ]
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(m.MessageError):
                    case()

    def test_overlay_validation(self):
        with self.assertRaises(m.MessageError):
            m.SetOverlay.decode(b"KD3BBP")

        with self.assertRaises(m.MessageError):
            m.SetOverlay.decode(b"\xff\x00")

        too_long = "x" * p.MAX_PAYLOAD_SIZE
        with self.assertRaises(m.MessageError):
            m.SetOverlay(too_long).encode()

    def test_decode_frame_payload_for_known_families(self):
        video = p.Frame(
            src=p.ADDR_CONTROLLER,
            dst=p.ADDR_SENDER_C,
            flags=p.FLAG_RELIABLE,
            session=1,
            seq=1,
            family=p.FAMILY_VIDEO,
            type=m.VideoType.SET_BITRATE,
            payload=m.SetBitrate(1_000_000).encode(),
        )
        fc_video = p.Frame(
            src=p.ADDR_FC_N,
            dst=p.ADDR_CONTROLLER,
            flags=p.FLAG_RELIABLE,
            session=1,
            seq=2,
            family=p.FAMILY_FC_VIDEO,
            type=m.FcVideoType.SET_SOURCE,
            payload=m.SetSource(1, p.ADDR_SENDER_C).encode(),
        )
        fc_coord = p.Frame(
            src=p.ADDR_FC_C,
            dst=p.ADDR_FC_N,
            flags=0,
            session=1,
            seq=3,
            family=p.FAMILY_FC_COORD,
            type=0x99,
            payload=b"opaque",
        )

        self.assertEqual(m.decode_frame_payload(video), m.SetBitrate(1_000_000))
        self.assertEqual(m.decode_frame_payload(fc_video), m.SetSource(1, p.ADDR_SENDER_C))
        self.assertEqual(m.decode_frame_payload(fc_coord), b"opaque")

    def test_unknown_type_or_family_rejected(self):
        with self.assertRaises(ValueError):
            m.decode_video(0xFF, b"")

        frame = p.Frame(
            src=1,
            dst=2,
            flags=0,
            session=1,
            seq=1,
            family=0xFE,
            type=0,
            payload=b"",
        )
        with self.assertRaises(m.MessageError):
            m.decode_frame_payload(frame)


if __name__ == "__main__":
    unittest.main()
