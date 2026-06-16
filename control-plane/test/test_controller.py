import unittest

from arc_protocol import messages as m
from arc_protocol import protocol as p
from arc.controller import Controller, ControllerError


class FakeLink:
    def __init__(self):
        self.sent = []

    def send(self, frame):
        self.sent.append(frame)


class ControllerTests(unittest.TestCase):
    def test_sender_command_uses_node_reliability_and_routes_to_link(self):
        link = FakeLink()
        controller = Controller(
            links={"airbrake": link},
            sender_addrs=(p.ADDR_SENDER_AIRBRAKE,),
            session=9,
            first_seq=100,
        )

        frame = controller.start_sender(p.ADDR_SENDER_AIRBRAKE, now=5.0)

        self.assertEqual(frame.src, p.ADDR_CONTROLLER)
        self.assertEqual(frame.dst, p.ADDR_SENDER_AIRBRAKE)
        self.assertEqual(frame.flags, p.FLAG_RELIABLE)
        self.assertEqual(frame.session, 9)
        self.assertEqual(frame.seq, 100)
        self.assertEqual(frame.family, p.FAMILY_VIDEO)
        self.assertEqual(frame.type, m.VideoType.START_STREAM)
        self.assertEqual(frame.payload, b"")
        self.assertEqual(link.sent, [frame])
        self.assertEqual(controller.node.reliable.pending_count, 1)

    def test_sender_bitrate_command_encodes_payload(self):
        link = FakeLink()
        controller = Controller(
            links={"airbrake": link},
            sender_addrs=(p.ADDR_SENDER_AIRBRAKE,),
        )

        frame = controller.set_sender_bitrate(p.ADDR_SENDER_AIRBRAKE, 2_500_000)

        self.assertEqual(frame.type, m.VideoType.SET_BITRATE)
        self.assertEqual(frame.payload, m.SetBitrate(2_500_000).encode())
        self.assertEqual(link.sent, [frame])

    def test_status_report_routes_to_matching_sender_link(self):
        controller = Controller(sender_addrs=(p.ADDR_SENDER_AIRBRAKE,))
        report = m.StatusReport(
            state=0x03,
            cpu_temp_c=51,
            cpu_load_pct=22,
            free_disk_mb=2048,
            rssi_dbm=-65,
            tx_frames=77,
            dropped_frames=1,
        )
        frame = p.Frame(
            src=p.ADDR_SENDER_AIRBRAKE,
            dst=p.ADDR_CONTROLLER,
            flags=0,
            session=1,
            seq=55,
            family=p.FAMILY_VIDEO,
            type=m.VideoType.STATUS_REPORT,
            payload=report.encode(),
        )

        controller.receive(frame, now=12.5)

        status = controller.sender(p.ADDR_SENDER_AIRBRAKE).last_status
        self.assertIsNotNone(status)
        self.assertEqual(status.report, report)
        self.assertEqual(status.seen_at, 12.5)
        self.assertEqual(controller.unhandled_frames, [])

    def test_unhandled_local_frame_is_kept_for_future_controller_logic(self):
        controller = Controller(sender_addrs=(p.ADDR_SENDER_AIRBRAKE,))
        frame = p.Frame(
            src=p.ADDR_FC_N,
            dst=p.ADDR_CONTROLLER,
            flags=0,
            session=1,
            seq=10,
            family=p.FAMILY_FC_VIDEO,
            type=m.FcVideoType.GET_STATUS,
            payload=b"",
        )

        controller.receive(frame)

        self.assertEqual(controller.unhandled_frames, [frame])

    def test_unknown_sender_status_is_rejected(self):
        controller = Controller(sender_addrs=(p.ADDR_SENDER_AIRBRAKE,))
        frame = p.Frame(
            src=p.ADDR_SENDER_PAYLOAD,
            dst=p.ADDR_CONTROLLER,
            flags=0,
            session=1,
            seq=1,
            family=p.FAMILY_VIDEO,
            type=m.VideoType.STATUS_REPORT,
            payload=m.StatusReport(0, 1, 2, 3, -4, 5, 6).encode(),
        )

        with self.assertRaises(ControllerError):
            controller.receive(frame)

    def test_unknown_sender_command_is_rejected(self):
        controller = Controller(sender_addrs=(p.ADDR_SENDER_AIRBRAKE,))

        with self.assertRaises(ControllerError):
            controller.start_sender(p.ADDR_SENDER_PAYLOAD)

    def test_tick_emits_heartbeat_to_fc_n(self):
        link = FakeLink()
        controller = Controller(
            links={"uart-fc-n": link},
            sender_addrs=(p.ADDR_SENDER_AIRBRAKE,),
            heartbeat_interval_s=1.0,
        )

        offline = controller.tick(now=0.0)

        self.assertEqual(offline, [])
        self.assertEqual(len(link.sent), 1)
        hb = link.sent[0]
        self.assertEqual(hb.dst, p.ADDR_FC_N)
        self.assertEqual(hb.family, p.FAMILY_NETMGMT)
        self.assertEqual(hb.type, p.NETMGMT_HEARTBEAT)

    def test_silent_sender_is_marked_offline_after_timeout(self):
        controller = Controller(
            sender_addrs=(p.ADDR_SENDER_AIRBRAKE,),
            peer_timeout_s=3.0,
        )
        report = m.StatusReport(0x03, 50, 20, 1024, -60, 10, 0)
        frame = p.Frame(
            src=p.ADDR_SENDER_AIRBRAKE,
            dst=p.ADDR_CONTROLLER,
            flags=0,
            session=1,
            seq=1,
            family=p.FAMILY_VIDEO,
            type=m.VideoType.STATUS_REPORT,
            payload=report.encode(),
        )
        controller.receive(frame, now=10.0)
        self.assertTrue(controller.health.is_online(p.ADDR_SENDER_AIRBRAKE))
        self.assertTrue(controller.sender(p.ADDR_SENDER_AIRBRAKE).online)

        offline = controller.tick(now=14.0)

        self.assertEqual(offline, [p.ADDR_SENDER_AIRBRAKE])
        self.assertFalse(controller.health.is_online(p.ADDR_SENDER_AIRBRAKE))
        self.assertFalse(controller.sender(p.ADDR_SENDER_AIRBRAKE).online)

    def test_inbound_heartbeat_is_absorbed_not_buffered(self):
        controller = Controller(sender_addrs=(p.ADDR_SENDER_AIRBRAKE,))
        hb = p.Frame(
            src=p.ADDR_SENDER_AIRBRAKE,
            dst=p.ADDR_CONTROLLER,
            flags=0,
            session=1,
            seq=7,
            family=p.FAMILY_NETMGMT,
            type=p.NETMGMT_HEARTBEAT,
            payload=b"",
        )
        controller.receive(hb, now=1.0)
        self.assertEqual(controller.unhandled_frames, [])
        self.assertTrue(controller.health.is_online(p.ADDR_SENDER_AIRBRAKE))

    def test_daemon_mode_does_not_retain_delivered_frame_history(self):
        controller = Controller(
            sender_addrs=(p.ADDR_SENDER_AIRBRAKE,),
            retain_local_history=False,
        )
        report = m.StatusReport(0x03, 50, 20, 1024, -60, 10, 0)

        for seq in range(3):
            controller.receive(
                p.Frame(
                    src=p.ADDR_SENDER_AIRBRAKE,
                    dst=p.ADDR_CONTROLLER,
                    flags=0,
                    session=1,
                    seq=seq,
                    family=p.FAMILY_VIDEO,
                    type=m.VideoType.STATUS_REPORT,
                    payload=report.encode(),
                ),
                now=float(seq),
            )

        self.assertEqual(controller.node.inbox, [])
        self.assertEqual(controller.sender(p.ADDR_SENDER_AIRBRAKE).last_status.report, report)


if __name__ == "__main__":
    unittest.main()
