"""Full in-memory topology flow tests.

These tests follow the system design doc's hub-and-spoke control plane:
FCs talk UART to paired Pis, Senders talk TCP/wifi to the Controller, and
the Controller is the hub for inter-FC routing plus FC_VIDEO execution.
"""

from __future__ import annotations

import unittest

from arc import messages, protocol as p
from arc.controller import Controller
from arc.controller_main import (
    SourceSwitcher,
    build_fc_video_status_report,
    make_fc_video_handler,
)
from arc.node import Node
from arc.sender import Sender


class DirectLink:
    def __init__(self, target):
        self.target = target
        self.sent: list[p.Frame] = []

    def send(self, frame: p.Frame) -> None:
        self.sent.append(frame)
        if isinstance(self.target, Node):
            self.target.receive(frame)
        else:
            self.target.receive(frame, now=0.0)


class FakeControllerPipeline:
    def __init__(self) -> None:
        self.layouts: list[str] = []
        self.overlays: list[str] = []
        self.sources: list[tuple[int, int]] = []

    def set_layout(self, name: str) -> None:
        self.layouts.append(name)

    def set_overlay(self, text: str) -> None:
        self.overlays.append(text)

    def set_source(self, slot_id: int, source_addr: int) -> None:
        self.sources.append((slot_id, source_addr))


class FullTopologyTests(unittest.TestCase):
    def build_topology(self):
        pipeline = FakeControllerPipeline()
        controller = Controller(
            sender_addrs=(
                p.ADDR_SENDER_C,
                p.ADDR_SENDER_L1,
                p.ADDR_SENDER_L2,
            ),
            session=10,
            first_seq=1000,
            heartbeat_interval_s=10.0,
            peer_timeout_s=10.0,
        )

        video_calls: list[tuple[int, messages.VideoType, p.Frame]] = []

        def video_handler(sender_addr: int):
            def handle(video_type: messages.VideoType, frame: p.Frame) -> None:
                video_calls.append((sender_addr, video_type, frame))

            return handle

        sender_c = Sender(
            addr=p.ADDR_SENDER_C,
            paired_fc=p.ADDR_FC_C,
            controller_addr=p.ADDR_CONTROLLER,
            session=20,
            first_seq=2000,
            heartbeat_interval_s=10.0,
            peer_timeout_s=10.0,
            video_command_handler=video_handler(p.ADDR_SENDER_C),
        )
        sender_l1 = Sender(
            addr=p.ADDR_SENDER_L1,
            paired_fc=p.ADDR_FC_L,
            controller_addr=p.ADDR_CONTROLLER,
            session=21,
            first_seq=2100,
            heartbeat_interval_s=10.0,
            peer_timeout_s=10.0,
            video_command_handler=video_handler(p.ADDR_SENDER_L1),
        )
        sender_l2 = Sender(
            addr=p.ADDR_SENDER_L2,
            paired_fc=None,
            controller_addr=p.ADDR_CONTROLLER,
            session=22,
            first_seq=2200,
            heartbeat_interval_s=10.0,
            peer_timeout_s=10.0,
            video_command_handler=video_handler(p.ADDR_SENDER_L2),
        )

        fc_n = Node(
            addr=p.ADDR_FC_N,
            routes={},
            default_route="controller",
            session=30,
            first_seq=3000,
        )
        fc_c = Node(
            addr=p.ADDR_FC_C,
            routes={},
            default_route="sender-c",
            session=31,
            first_seq=3100,
        )
        fc_l = Node(
            addr=p.ADDR_FC_L,
            routes={},
            default_route="sender-l1",
            session=32,
            first_seq=3200,
        )

        links = {
            "fc-n->controller": DirectLink(controller),
            "controller->fc-n": DirectLink(fc_n),
            "controller->sender-c": DirectLink(sender_c),
            "sender-c->controller": DirectLink(controller),
            "sender-c->fc-c": DirectLink(fc_c),
            "fc-c->sender-c": DirectLink(sender_c),
            "controller->sender-l1": DirectLink(sender_l1),
            "sender-l1->controller": DirectLink(controller),
            "sender-l1->fc-l": DirectLink(fc_l),
            "fc-l->sender-l1": DirectLink(sender_l1),
            "controller->sender-l2": DirectLink(sender_l2),
            "sender-l2->controller": DirectLink(controller),
        }

        controller.set_links(
            {
                "uart-fc-n": links["controller->fc-n"],
                "sender-c": links["controller->sender-c"],
                "sender-l1": links["controller->sender-l1"],
                "sender-l2": links["controller->sender-l2"],
            }
        )
        sender_c.set_links(
            {
                "controller": links["sender-c->controller"],
                "uart-fc": links["sender-c->fc-c"],
            }
        )
        sender_l1.set_links(
            {
                "controller": links["sender-l1->controller"],
                "uart-fc": links["sender-l1->fc-l"],
            }
        )
        sender_l2.set_links({"controller": links["sender-l2->controller"]})
        fc_n.set_links({"controller": links["fc-n->controller"]})
        fc_c.set_links({"sender-c": links["fc-c->sender-c"]})
        fc_l.set_links({"sender-l1": links["fc-l->sender-l1"]})
        controller.fc_video_handler = make_fc_video_handler(
            pipeline,
            ["local_full", "split", "remote_full"],
            SourceSwitcher(
                controller,
                pipeline,
                (p.ADDR_SENDER_C, p.ADDR_SENDER_L1, p.ADDR_SENDER_L2),
            ),
        )

        return {
            "pipeline": pipeline,
            "video_calls": video_calls,
            "controller": controller,
            "sender_c": sender_c,
            "sender_l1": sender_l1,
            "sender_l2": sender_l2,
            "fc_n": fc_n,
            "fc_c": fc_c,
            "fc_l": fc_l,
            "links": links,
        }

    def test_fleet_control_fc_routing_status_and_fc_video(self):
        topo = self.build_topology()
        controller: Controller = topo["controller"]
        sender_c: Sender = topo["sender_c"]
        sender_l1: Sender = topo["sender_l1"]
        sender_l2: Sender = topo["sender_l2"]
        fc_n: Node = topo["fc_n"]
        fc_c: Node = topo["fc_c"]
        fc_l: Node = topo["fc_l"]
        pipeline: FakeControllerPipeline = topo["pipeline"]
        video_calls = topo["video_calls"]
        controller.health.observe(
            p.Frame(
                src=p.ADDR_SENDER_L1,
                dst=p.ADDR_CONTROLLER,
                flags=0,
                session=21,
                seq=0,
                family=p.FAMILY_NETMGMT,
                type=p.NETMGMT_HEARTBEAT,
                payload=b"",
            ),
            now=1.0,
        )
        links = topo["links"]

        controller.start_sender(p.ADDR_SENDER_C, now=1.0)
        controller.set_sender_bitrate(p.ADDR_SENDER_L2, 1_800_000, now=1.1)

        self.assertTrue(sender_c.transmitting)
        self.assertTrue(sender_c.recording)
        self.assertEqual(sender_l2.bitrate_bps, 1_800_000)
        self.assertEqual(controller.node.reliable.pending_count, 0)
        self.assertEqual(
            [(addr, vt) for addr, vt, _ in video_calls],
            [
                (p.ADDR_SENDER_C, messages.VideoType.START_STREAM),
                (p.ADDR_SENDER_L2, messages.VideoType.SET_BITRATE),
            ],
        )

        telemetry = fc_c.send_local(
            dst=p.ADDR_FC_N,
            family=p.FAMILY_FC_COORD,
            type=0x10,
            payload=b"central-telemetry",
            reliable=True,
            now=2.0,
        )

        self.assertEqual(fc_n.inbox, [telemetry])
        self.assertEqual(fc_c.reliable.pending_count, 0)
        self.assertEqual(links["fc-c->sender-c"].sent[-1], telemetry)
        self.assertEqual(links["sender-c->controller"].sent[-1], telemetry)
        self.assertEqual(links["controller->fc-n"].sent[-1], telemetry)

        command = fc_n.send_local(
            dst=p.ADDR_FC_L,
            family=p.FAMILY_FC_COORD,
            type=0x20,
            payload=b"deploy?",
            reliable=True,
            now=3.0,
        )

        self.assertEqual(fc_l.inbox, [command])
        self.assertEqual(fc_n.reliable.pending_count, 0)
        self.assertEqual(links["fc-n->controller"].sent[-1], command)
        self.assertEqual(links["controller->sender-l1"].sent[-1], command)
        self.assertEqual(links["sender-l1->fc-l"].sent[-1], command)

        layout = fc_n.send_local(
            dst=p.ADDR_CONTROLLER,
            family=p.FAMILY_FC_VIDEO,
            type=messages.FcVideoType.SET_LAYOUT,
            payload=messages.SetLayout(1).encode(),
            reliable=True,
            now=4.0,
        )
        overlay = fc_n.send_local(
            dst=p.ADDR_CONTROLLER,
            family=p.FAMILY_FC_VIDEO,
            type=messages.FcVideoType.SET_OVERLAY,
            payload=messages.SetOverlay("KD3BBP / BOOST").encode(),
            reliable=True,
            now=4.1,
        )
        source = fc_n.send_local(
            dst=p.ADDR_CONTROLLER,
            family=p.FAMILY_FC_VIDEO,
            type=messages.FcVideoType.SET_SOURCE,
            payload=messages.SetSource(1, p.ADDR_SENDER_L1).encode(),
            reliable=True,
            now=4.2,
        )

        self.assertEqual(fc_n.reliable.pending_count, 0)
        self.assertIn(layout, controller.node.inbox)
        self.assertIn(overlay, controller.node.inbox)
        self.assertIn(source, controller.node.inbox)
        self.assertEqual(pipeline.layouts, ["split"])
        self.assertEqual(pipeline.overlays, ["KD3BBP / BOOST"])
        self.assertEqual(pipeline.sources, [(1, p.ADDR_SENDER_L1)])
        self.assertTrue(sender_l1.transmitting)

        report = messages.StatusReport(
            state=0x03,
            cpu_temp_c=47,
            cpu_load_pct=23,
            free_disk_mb=4096,
            rssi_dbm=-58,
            tx_frames=120,
            dropped_frames=2,
        )
        sender_c.report_status(report, now=5.0)

        sender_status = controller.sender(p.ADDR_SENDER_C).last_status
        self.assertIsNotNone(sender_status)
        self.assertEqual(sender_status.report, report)
        self.assertTrue(controller.health.is_online(p.ADDR_SENDER_C))
        self.assertTrue(sender_c.health.is_online(p.ADDR_CONTROLLER))
        self.assertFalse(sender_l2.transmitting)

    def test_get_status_reply_round_trip_to_fc_n(self):
        topo = self.build_topology()
        controller: Controller = topo["controller"]
        sender_c: Sender = topo["sender_c"]
        sender_l1: Sender = topo["sender_l1"]
        sender_l2: Sender = topo["sender_l2"]
        fc_n: Node = topo["fc_n"]
        pipeline: FakeControllerPipeline = topo["pipeline"]
        sender_addrs = (p.ADDR_SENDER_C, p.ADDR_SENDER_L1, p.ADDR_SENDER_L2)

        # Re-install the FC_VIDEO handler with controller wired in so
        # GET_STATUS produces an actual reply.
        controller.fc_video_handler = make_fc_video_handler(
            pipeline,
            ["local_full", "split", "remote_full"],
            SourceSwitcher(controller, pipeline, sender_addrs),
            controller=controller,
            sender_addrs=sender_addrs,
            fc_n_addr=p.ADDR_FC_N,
            now=lambda: 7.0,
        )

        # Sender-C is online and currently being told to stream;
        # Sender-L1 is online but idle; Sender-L2 has been hard-stopped.
        for addr, session in (
            (p.ADDR_SENDER_C, 20),
            (p.ADDR_SENDER_L1, 21),
            (p.ADDR_SENDER_L2, 22),
        ):
            controller.health.observe(
                p.Frame(
                    src=addr,
                    dst=p.ADDR_CONTROLLER,
                    flags=0,
                    session=session,
                    seq=0,
                    family=p.FAMILY_NETMGMT,
                    type=p.NETMGMT_HEARTBEAT,
                    payload=b"",
                ),
                now=6.0,
            )
        controller.start_sender(p.ADDR_SENDER_C, now=6.1)
        controller.hard_stop_sender(p.ADDR_SENDER_L2, now=6.2)

        # Drain inboxes to make the next assertion specific to GET_STATUS.
        fc_n.inbox.clear()

        # FC-N issues GET_STATUS.
        request = fc_n.send_local(
            dst=p.ADDR_CONTROLLER,
            family=p.FAMILY_FC_VIDEO,
            type=messages.FcVideoType.GET_STATUS,
            payload=b"",
            reliable=True,
            now=7.0,
        )
        self.assertEqual(fc_n.reliable.pending_count, 0)
        self.assertIn(request, controller.node.inbox)

        # Find the STATUS_REPORT reply that landed at FC-N.
        replies = [
            f for f in fc_n.inbox
            if f.family == p.FAMILY_FC_VIDEO
            and f.type == messages.FcVideoType.STATUS_REPORT
        ]
        self.assertEqual(len(replies), 1)
        report = messages.FcVideoStatusReport.decode(replies[0].payload)

        # Slot 0 is local camera; slot 1 starts empty (no SET_SOURCE issued).
        self.assertEqual(
            report.slots, (p.ADDR_CONTROLLER, p.ADDR_UNASSIGNED)
        )
        flags_by_addr = {s.addr: s.flags for s in report.senders}
        self.assertEqual(set(flags_by_addr), set(sender_addrs))
        # Sender-C: online + transmitting + recording
        self.assertEqual(
            flags_by_addr[p.ADDR_SENDER_C],
            messages.FC_VIDEO_STATUS_FLAG_ONLINE
            | messages.FC_VIDEO_STATUS_FLAG_TRANSMITTING
            | messages.FC_VIDEO_STATUS_FLAG_RECORDING,
        )
        # Sender-L1: online, no command issued -> recording assumed (boot default), not transmitting
        self.assertEqual(
            flags_by_addr[p.ADDR_SENDER_L1],
            messages.FC_VIDEO_STATUS_FLAG_ONLINE
            | messages.FC_VIDEO_STATUS_FLAG_RECORDING,
        )
        # Sender-L2: online but HARD_STOP cleared the recording bit
        self.assertEqual(
            flags_by_addr[p.ADDR_SENDER_L2],
            messages.FC_VIDEO_STATUS_FLAG_ONLINE,
        )

        # Sanity: build_fc_video_status_report agrees with the wire payload.
        direct = build_fc_video_status_report(
            controller,
            None,  # slots are taken from switcher only when one is supplied
            sender_addrs,
        )
        self.assertEqual(
            {s.addr: s.flags for s in direct.senders},
            flags_by_addr,
        )

        # Quiet the senders so unrelated assertions on sender state don't trip.
        del sender_c, sender_l1, sender_l2


if __name__ == "__main__":
    unittest.main()
