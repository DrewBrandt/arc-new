import unittest

from arc.learned_router import LearnedRouter
from arc.node import Node
from arc_protocol import protocol as p


class FakeLink:
    def __init__(self):
        self.sent = []

    def send(self, frame):
        self.sent.append(frame)


def frame(src, dst, *, flags=0, seq=1, family=p.FAMILY_NETMGMT, type=p.NETMGMT_HEARTBEAT):
    return p.Frame(
        src=src,
        dst=dst,
        flags=flags,
        session=7,
        seq=seq,
        family=family,
        type=type,
        payload=b"",
    )


class LearnedRouterTests(unittest.TestCase):
    def test_node_learns_ingress_and_routes_local_reply_back_to_source(self):
        airbrake = FakeLink()
        controller = Node(
            addr=p.ADDR_CONTROLLER,
            routes={},
            links={"airbrake": airbrake},
            session=9,
            first_seq=100,
        )

        heartbeat = frame(p.ADDR_SENDER_AIRBRAKE, p.ADDR_CONTROLLER)
        controller.receive(heartbeat, ingress="airbrake", now=1.0)

        reply = controller.send_local(
            dst=p.ADDR_SENDER_AIRBRAKE,
            family=p.FAMILY_VIDEO,
            type=0x01,
            payload=b"start",
            now=2.0,
        )

        self.assertEqual(controller.inbox, [heartbeat])
        self.assertEqual(airbrake.sent, [reply])

    def test_reliable_local_ack_uses_newly_learned_ingress(self):
        airbrake = FakeLink()
        controller = Node(
            addr=p.ADDR_CONTROLLER,
            routes={},
            links={"airbrake": airbrake},
            session=9,
            first_seq=100,
        )

        incoming = frame(
            p.ADDR_SENDER_AIRBRAKE,
            p.ADDR_CONTROLLER,
            flags=p.FLAG_RELIABLE,
            seq=0x1234,
            family=p.FAMILY_VIDEO,
            type=0x20,
        )
        controller.receive(incoming, ingress="airbrake", now=1.0)

        self.assertEqual(len(airbrake.sent), 1)
        ack = airbrake.sent[0]
        self.assertEqual(ack.src, p.ADDR_CONTROLLER)
        self.assertEqual(ack.dst, p.ADDR_SENDER_AIRBRAKE)
        self.assertEqual(ack.flags, p.FLAG_ACK)
        self.assertEqual(ack.payload, bytes.fromhex("1234"))

    def test_learned_route_expires_back_to_static_route(self):
        learned = FakeLink()
        static = FakeLink()
        router = LearnedRouter(
            my_addr=p.ADDR_CONTROLLER,
            routes={p.ADDR_SENDER_AIRBRAKE: "static"},
            links={"learned": learned, "static": static},
            local_handler=lambda _f: None,
            learned_ttl_s=10.0,
        )
        router.route(
            frame(p.ADDR_SENDER_AIRBRAKE, p.ADDR_CONTROLLER),
            ingress="learned",
            now=1.0,
        )

        outbound = frame(p.ADDR_CONTROLLER, p.ADDR_SENDER_AIRBRAKE, seq=2)
        router.route(outbound, now=5.0)
        router.route(outbound, now=12.5)

        self.assertEqual(learned.sent, [outbound])
        self.assertEqual(static.sent, [outbound])

    def test_broadcast_delivers_local_and_fans_out_except_ingress(self):
        delivered = []
        a = FakeLink()
        b = FakeLink()
        router = LearnedRouter(
            my_addr=p.ADDR_CONTROLLER,
            routes={},
            links={"a": a, "b": b},
            local_handler=delivered.append,
        )
        broadcast = frame(p.ADDR_FC_N, p.ADDR_BROADCAST)

        result = router.route(broadcast, ingress="a", now=1.0)

        self.assertEqual(result.action, "broadcast")
        self.assertEqual(delivered, [broadcast])
        self.assertEqual(a.sent, [])
        self.assertEqual(b.sent, [broadcast])


if __name__ == "__main__":
    unittest.main()
