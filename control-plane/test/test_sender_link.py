import unittest

from arc import messages as m
from arc import protocol as p
from arc.sender_link import SenderLink, SenderLinkError


class CommandSink:
    def __init__(self):
        self.calls = []

    def send(self, dst, family, type, payload=b"", reliable=False, flags=0, now=0.0):
        self.calls.append(
            {
                "dst": dst,
                "family": family,
                "type": type,
                "payload": payload,
                "reliable": reliable,
                "flags": flags,
                "now": now,
            }
        )
        return p.Frame(
            src=p.ADDR_CONTROLLER,
            dst=dst,
            flags=p.FLAG_RELIABLE if reliable else flags,
            session=1,
            seq=len(self.calls),
            family=family,
            type=type,
            payload=payload,
        )


class SenderLinkTests(unittest.TestCase):
    def build_link(self, reliable_commands=True):
        sink = CommandSink()
        link = SenderLink(
            sender_addr=p.ADDR_SENDER_C,
            send_command=sink.send,
            reliable_commands=reliable_commands,
        )
        return link, sink

    def test_start_stop_and_hard_stop_emit_reliable_video_commands(self):
        link, sink = self.build_link()

        start = link.start_stream(now=1.0)
        stop = link.stop_stream(now=2.0)
        hard_stop = link.hard_stop(now=3.0)

        self.assertEqual(
            [call["type"] for call in sink.calls],
            [
                m.VideoType.START_STREAM,
                m.VideoType.STOP_STREAM,
                m.VideoType.HARD_STOP,
            ],
        )
        self.assertEqual([call["payload"] for call in sink.calls], [b"", b"", b""])
        self.assertEqual([call["reliable"] for call in sink.calls], [True, True, True])
        self.assertEqual([call["dst"] for call in sink.calls], [p.ADDR_SENDER_C] * 3)
        self.assertEqual([start.seq, stop.seq, hard_stop.seq], [1, 2, 3])

    def test_set_bitrate_encodes_payload(self):
        link, sink = self.build_link()

        frame = link.set_bitrate(2_500_000, now=4.0)

        self.assertEqual(frame.dst, p.ADDR_SENDER_C)
        self.assertEqual(frame.family, p.FAMILY_VIDEO)
        self.assertEqual(frame.type, m.VideoType.SET_BITRATE)
        self.assertEqual(frame.payload, m.SetBitrate(2_500_000).encode())
        self.assertEqual(sink.calls[0]["now"], 4.0)

    def test_reliability_can_be_disabled_for_tests_or_diagnostics(self):
        link, sink = self.build_link(reliable_commands=False)

        frame = link.start_stream()

        self.assertFalse(frame.flags & p.FLAG_RELIABLE)
        self.assertFalse(sink.calls[0]["reliable"])

    def test_status_report_updates_sender_state(self):
        link, _sink = self.build_link()
        report = m.StatusReport(
            state=0x03,
            cpu_temp_c=55,
            cpu_load_pct=40,
            free_disk_mb=4096,
            rssi_dbm=-70,
            tx_frames=123,
            dropped_frames=4,
        )
        frame = p.Frame(
            src=p.ADDR_SENDER_C,
            dst=p.ADDR_CONTROLLER,
            flags=0,
            session=2,
            seq=20,
            family=p.FAMILY_VIDEO,
            type=m.VideoType.STATUS_REPORT,
            payload=report.encode(),
        )

        decoded = link.handle_frame(frame, now=10.5)

        self.assertEqual(decoded, report)
        self.assertTrue(link.online)
        self.assertIsNotNone(link.last_status)
        self.assertEqual(link.last_status.report, report)
        self.assertEqual(link.last_status.seen_at, 10.5)

    def test_mark_offline_clears_status(self):
        link, _sink = self.build_link()
        report = m.StatusReport(0, 1, 2, 3, -4, 5, 6)
        frame = p.Frame(
            src=p.ADDR_SENDER_C,
            dst=p.ADDR_CONTROLLER,
            flags=0,
            session=1,
            seq=1,
            family=p.FAMILY_VIDEO,
            type=m.VideoType.STATUS_REPORT,
            payload=report.encode(),
        )
        link.handle_frame(frame, now=1.0)

        link.mark_offline()

        self.assertFalse(link.online)
        self.assertIsNone(link.last_status)

    def test_rejects_frames_from_wrong_source_or_type(self):
        link, _sink = self.build_link()
        good_report = m.StatusReport(0, 1, 2, 3, -4, 5, 6)
        cases = [
            p.Frame(
                src=p.ADDR_SENDER_L1,
                dst=p.ADDR_CONTROLLER,
                flags=0,
                session=1,
                seq=1,
                family=p.FAMILY_VIDEO,
                type=m.VideoType.STATUS_REPORT,
                payload=good_report.encode(),
            ),
            p.Frame(
                src=p.ADDR_SENDER_C,
                dst=p.ADDR_CONTROLLER,
                flags=0,
                session=1,
                seq=1,
                family=p.FAMILY_NETMGMT,
                type=m.NetMgmtType.HEARTBEAT,
                payload=b"",
            ),
            p.Frame(
                src=p.ADDR_SENDER_C,
                dst=p.ADDR_CONTROLLER,
                flags=0,
                session=1,
                seq=1,
                family=p.FAMILY_VIDEO,
                type=m.VideoType.START_STREAM,
                payload=b"",
            ),
        ]

        for frame in cases:
            with self.subTest(frame=frame):
                with self.assertRaises(SenderLinkError):
                    link.handle_frame(frame)


if __name__ == "__main__":
    unittest.main()
