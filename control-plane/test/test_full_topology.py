"""Full in-memory topology flow tests.

These tests follow the system design doc's hub-and-spoke control plane:
FCs talk UART to paired Pis, Senders talk TCP/wifi to the Controller, and
the Controller is the hub for inter-FC routing plus FC_VIDEO execution.
"""

from __future__ import annotations

import unittest

from arc import messages, protocol as p
from arc.controller import Controller
from arc.controller_main import SourceSwitcher, make_fc_video_handler
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


if __name__ == "__main__":
    unittest.main()
