import unittest

from arc_protocol import protocol as p
from arc_protocol.router import RouteError, Router, controller_routes, sender_routes


class FakeLink:
    def __init__(self):
        self.sent = []

    def send(self, frame):
        self.sent.append(frame)


def frame_for(dst):
    return p.Frame(
        src=p.ADDR_FC_C,
        dst=dst,
        flags=0,
        session=1,
        seq=42,
        family=p.FAMILY_FC_COORD,
        type=0x10,
        payload=b"abc",
    )


class RouterTests(unittest.TestCase):
    def test_delivers_local_frame(self):
        delivered = []
        links = {"airbrake": FakeLink()}
        router = Router(
            my_addr=p.ADDR_CONTROLLER,
            routes={p.ADDR_FC_C: "airbrake"},
            links=links,
            local_handler=delivered.append,
        )

        frame = frame_for(p.ADDR_CONTROLLER)
        result = router.route(frame)

        self.assertEqual(result.action, "local")
        self.assertIsNone(result.link_name)
        self.assertEqual(delivered, [frame])
        self.assertEqual(links["airbrake"].sent, [])

    def test_forwards_routed_frame(self):
        delivered = []
        links = {"airbrake": FakeLink()}
        router = Router(
            my_addr=p.ADDR_CONTROLLER,
            routes={p.ADDR_FC_C: "airbrake"},
            links=links,
            local_handler=delivered.append,
        )

        frame = frame_for(p.ADDR_FC_C)
        result = router.route(frame)

        self.assertEqual(result.action, "forwarded")
        self.assertEqual(result.link_name, "airbrake")
        self.assertEqual(delivered, [])
        self.assertEqual(links["airbrake"].sent, [frame])

    def test_unknown_destination_raises(self):
        router = Router(
            my_addr=p.ADDR_CONTROLLER,
            routes={},
            links={},
            local_handler=lambda frame: None,
        )

        with self.assertRaises(RouteError):
            router.route(frame_for(p.ADDR_FC_L))

    def test_missing_link_raises(self):
        router = Router(
            my_addr=p.ADDR_CONTROLLER,
            routes={p.ADDR_FC_C: "airbrake"},
            links={},
            local_handler=lambda frame: None,
        )

        with self.assertRaises(RouteError):
            router.route(frame_for(p.ADDR_FC_C))

    def test_controller_route_table(self):
        links = {
            "uart-fc-n": FakeLink(),
            "airbrake": FakeLink(),
            "payload": FakeLink(),
            "down": FakeLink(),
            "ground": FakeLink(),
        }
        router = Router(
            my_addr=p.ADDR_CONTROLLER,
            routes=controller_routes(),
            links=links,
            local_handler=lambda frame: None,
        )

        cases = [
            (p.ADDR_FC_N, "uart-fc-n"),
            (p.ADDR_GROUND, "uart-fc-n"),
            (p.ADDR_FC_C, "airbrake"),
            (p.ADDR_FC_L, "payload"),
            (p.ADDR_SENDER_AIRBRAKE, "airbrake"),
        ]
        for dst, link_name in cases:
            with self.subTest(dst=dst):
                frame = frame_for(dst)
                result = router.route(frame)
                self.assertEqual(result.link_name, link_name)
                self.assertEqual(links[link_name].sent[-1], frame)

    def test_sender_routes_paired_fc_and_defaults_to_controller(self):
        links = {"uart-fc": FakeLink(), "controller": FakeLink()}
        router = Router(
            my_addr=p.ADDR_SENDER_AIRBRAKE,
            routes=sender_routes(p.ADDR_FC_C),
            links=links,
            local_handler=lambda frame: None,
            default_route="controller",
        )

        fc_frame = frame_for(p.ADDR_FC_C)
        controller_frame = frame_for(p.ADDR_CONTROLLER)

        self.assertEqual(router.route(fc_frame).link_name, "uart-fc")
        self.assertEqual(router.route(controller_frame).link_name, "controller")
        self.assertEqual(links["uart-fc"].sent, [fc_frame])
        self.assertEqual(links["controller"].sent, [controller_frame])

    def test_video_only_sender_defaults_everything_to_controller(self):
        links = {"controller": FakeLink()}
        router = Router(
            my_addr=p.ADDR_SENDER_GROUND,
            routes=sender_routes(),
            links=links,
            local_handler=lambda frame: None,
            default_route="controller",
        )

        frame = frame_for(p.ADDR_FC_L)
        self.assertEqual(router.route(frame).link_name, "controller")
        self.assertEqual(links["controller"].sent, [frame])


if __name__ == "__main__":
    unittest.main()

