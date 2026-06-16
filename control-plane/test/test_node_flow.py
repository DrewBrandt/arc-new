import unittest

from arc_protocol import protocol as p
from arc.node import Node
from arc_protocol.router import controller_routes, sender_routes


class DirectLink:
    def __init__(self, target):
        self.target = target
        self.sent = []

    def send(self, frame):
        self.sent.append(frame)
        self.target.receive(frame)


class NodeFlowTests(unittest.TestCase):
    def build_fc_c_to_fc_n_topology(self):
        fc_c = Node(
            addr=p.ADDR_FC_C,
            routes={},
            default_route="airbrake",
            session=3,
            first_seq=100,
        )
        sender_airbrake = Node(
            addr=p.ADDR_SENDER_AIRBRAKE,
            routes=sender_routes(p.ADDR_FC_C),
            default_route="controller",
            session=4,
        )
        controller = Node(
            addr=p.ADDR_CONTROLLER,
            routes=controller_routes(),
            session=5,
        )
        fc_n = Node(
            addr=p.ADDR_FC_N,
            routes={},
            default_route="controller",
            session=6,
            first_seq=200,
        )

        links = {
            "fc-c->airbrake": DirectLink(sender_airbrake),
            "airbrake->fc-c": DirectLink(fc_c),
            "airbrake->controller": DirectLink(controller),
            "controller->airbrake": DirectLink(sender_airbrake),
            "controller->fc-n": DirectLink(fc_n),
            "fc-n->controller": DirectLink(controller),
        }

        fc_c.set_links({"airbrake": links["fc-c->airbrake"]})
        sender_airbrake.set_links(
            {
                "uart-fc": links["airbrake->fc-c"],
                "controller": links["airbrake->controller"],
            }
        )
        controller.set_links(
            {
                "airbrake": links["controller->airbrake"],
                "uart-fc-n": links["controller->fc-n"],
            }
        )
        fc_n.set_links({"controller": links["fc-n->controller"]})
        return fc_c, sender_airbrake, controller, fc_n, links

    def test_reliable_frame_routes_to_fc_n_and_ack_returns_to_fc_c(self):
        fc_c, sender_airbrake, controller, fc_n, links = self.build_fc_c_to_fc_n_topology()

        sent = fc_c.send_local(
            dst=p.ADDR_FC_N,
            family=p.FAMILY_FC_COORD,
            type=0x20,
            payload=b"stage?",
            reliable=True,
            now=10.0,
        )

        self.assertEqual(fc_n.inbox, [sent])
        self.assertEqual(fc_c.reliable.pending_count, 0)
        self.assertEqual(sender_airbrake.inbox, [])
        self.assertEqual(controller.inbox, [])
        self.assertEqual(fc_c.failed, [])

        self.assertEqual(links["fc-c->airbrake"].sent, [sent])
        self.assertEqual(links["airbrake->controller"].sent, [sent])
        self.assertEqual(links["controller->fc-n"].sent, [sent])

        self.assertEqual(len(links["fc-n->controller"].sent), 1)
        ack = links["fc-n->controller"].sent[0]
        self.assertEqual(ack.src, p.ADDR_FC_N)
        self.assertEqual(ack.dst, p.ADDR_FC_C)
        self.assertEqual(ack.flags, p.FLAG_ACK)
        self.assertEqual(ack.payload, bytes((sent.seq >> 8, sent.seq & 0xFF)))
        self.assertEqual(links["controller->airbrake"].sent, [ack])
        self.assertEqual(links["airbrake->fc-c"].sent, [ack])

    def test_unreliable_frame_routes_without_ack_or_pending_state(self):
        fc_c, _sender_c, _controller, fc_n, links = self.build_fc_c_to_fc_n_topology()

        sent = fc_c.send_local(
            dst=p.ADDR_FC_N,
            family=p.FAMILY_FC_COORD,
            type=0x10,
            payload=b"telemetry",
            reliable=False,
            now=10.0,
        )

        self.assertEqual(fc_n.inbox, [sent])
        self.assertEqual(fc_c.reliable.pending_count, 0)
        self.assertEqual(links["fc-n->controller"].sent, [])

    def test_reliable_timeout_resends_through_route(self):
        fc_c = Node(
            addr=p.ADDR_FC_C,
            routes={},
            default_route="airbrake",
            session=3,
            timeout_s=1.0,
            max_retries=1,
        )
        drop_link = DropLink()
        fc_c.set_links({"airbrake": drop_link})

        sent = fc_c.send_local(
            dst=p.ADDR_FC_N,
            family=p.FAMILY_FC_COORD,
            type=0x20,
            reliable=True,
            now=0.0,
        )

        fc_c.tick(1.0)
        fc_c.tick(2.0)

        self.assertEqual(drop_link.sent, [sent, sent])
        self.assertEqual(fc_c.failed, [sent])
        self.assertEqual(fc_c.reliable.pending_count, 0)


class DropLink:
    def __init__(self):
        self.sent = []

    def send(self, frame):
        self.sent.append(frame)


if __name__ == "__main__":
    unittest.main()

