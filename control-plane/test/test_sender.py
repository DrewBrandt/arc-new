import unittest

from arc import messages, protocol as p
from arc.router import Link
from arc.sender import Sender, SenderError


class CapturingLink:
    def __init__(self) -> None:
        self.sent: list[p.Frame] = []

    def send(self, frame: p.Frame) -> None:
        self.sent.append(frame)


def make_sender(addr: int = p.ADDR_SENDER_C, paired_fc: int | None = p.ADDR_FC_C) -> tuple[Sender, CapturingLink]:
    controller_link = CapturingLink()
    fc_link: Link | None = CapturingLink() if paired_fc is not None else None
    links: dict[str, Link] = {"controller": controller_link}
    if fc_link is not None:
        links["uart-fc"] = fc_link
    return (
        Sender(
            addr=addr,
            paired_fc=paired_fc,
            links=links,
            session=2,
            first_seq=0x100,
        ),
        controller_link,
    )


class SenderTests(unittest.TestCase):
    def test_default_state_is_recording_only(self):
        sender, _ = make_sender()
        self.assertFalse(sender.transmitting)
        self.assertTrue(sender.recording)
        self.assertIsNone(sender.bitrate_bps)

    def test_start_stream_enables_both_recording_and_transmitting(self):
        sender, _ = make_sender()
        cmd = _video_command(messages.VideoType.START_STREAM)
        sender.receive(cmd)
        self.assertTrue(sender.transmitting)
        self.assertTrue(sender.recording)

    def test_stop_stream_keeps_recording(self):
        sender, _ = make_sender()
        sender.receive(_video_command(messages.VideoType.START_STREAM))
        sender.receive(_video_command(messages.VideoType.STOP_STREAM))
        self.assertFalse(sender.transmitting)
        self.assertTrue(sender.recording)

    def test_hard_stop_disables_everything(self):
        sender, _ = make_sender()
        sender.receive(_video_command(messages.VideoType.START_STREAM))
        sender.receive(_video_command(messages.VideoType.HARD_STOP))
        self.assertFalse(sender.transmitting)
        self.assertFalse(sender.recording)

    def test_set_bitrate_updates_value(self):
        sender, _ = make_sender()
        payload = messages.SetBitrate(bitrate_bps=2_500_000).encode()
        sender.receive(
            _video_command(messages.VideoType.SET_BITRATE, payload=payload),
        )
        self.assertEqual(sender.bitrate_bps, 2_500_000)

    def test_unknown_video_type_raises(self):
        sender, _ = make_sender()
        cmd = p.Frame(
            src=p.ADDR_CONTROLLER,
            dst=p.ADDR_SENDER_C,
            flags=0,
            session=1,
            seq=0,
            family=p.FAMILY_VIDEO,
            type=0xEE,
            payload=b"",
        )
        with self.assertRaises(SenderError):
            sender.receive(cmd)

    def test_report_status_sends_video_status_report(self):
        sender, controller_link = make_sender()
        report = messages.StatusReport(
            state=0x03,
            cpu_temp_c=42,
            cpu_load_pct=18,
            free_disk_mb=1024,
            rssi_dbm=-70,
            tx_frames=5,
            dropped_frames=0,
        )
        frame = sender.report_status(report)
        self.assertEqual(controller_link.sent, [frame])
        self.assertEqual(frame.family, p.FAMILY_VIDEO)
        self.assertEqual(frame.type, messages.VideoType.STATUS_REPORT)
        self.assertEqual(frame.dst, p.ADDR_CONTROLLER)
        self.assertEqual(messages.StatusReport.decode(frame.payload), report)

    def test_fc_coord_for_paired_fc_routes_via_uart(self):
        # FC_COORD command bound for the paired FC should leave via uart-fc.
        controller_link = CapturingLink()
        fc_link = CapturingLink()
        sender = Sender(
            addr=p.ADDR_SENDER_C,
            paired_fc=p.ADDR_FC_C,
            links={"controller": controller_link, "uart-fc": fc_link},
            session=2,
        )
        frame = p.Frame(
            src=p.ADDR_FC_N,
            dst=p.ADDR_FC_C,
            flags=0,
            session=1,
            seq=42,
            family=p.FAMILY_FC_COORD,
            type=0x10,
            payload=b"cmd",
        )
        sender.receive(frame)
        self.assertEqual(fc_link.sent, [frame])
        self.assertEqual(controller_link.sent, [])

    def test_unknown_dst_routes_to_controller_via_default(self):
        # No paired FC: any non-local destination falls through default
        # route "controller".
        sender, controller_link = make_sender(paired_fc=None)
        frame = p.Frame(
            src=p.ADDR_FC_C,
            dst=p.ADDR_FC_N,
            flags=0,
            session=1,
            seq=1,
            family=p.FAMILY_FC_COORD,
            type=0x01,
            payload=b"",
        )
        sender.receive(frame)
        self.assertEqual(controller_link.sent, [frame])


def _video_command(video_type: messages.VideoType, payload: bytes = b"") -> p.Frame:
    return p.Frame(
        src=p.ADDR_CONTROLLER,
        dst=p.ADDR_SENDER_C,
        flags=0,
        session=1,
        seq=0,
        family=p.FAMILY_VIDEO,
        type=video_type,
        payload=payload,
    )


if __name__ == "__main__":
    unittest.main()
