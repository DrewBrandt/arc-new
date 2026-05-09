import unittest

from arc import protocol as p
from arc.health import Heartbeat, PeerHealth
from arc.node import Node
from arc.router import Link


class CapturingLink:
    def __init__(self) -> None:
        self.sent: list[p.Frame] = []

    def send(self, frame: p.Frame) -> None:
        self.sent.append(frame)


class HeartbeatTests(unittest.TestCase):
    def test_emits_at_first_tick(self):
        sent: list[p.Frame] = []
        link = CapturingLink()
        node = Node(
            addr=p.ADDR_SENDER_C,
            routes={},
            default_route="controller",
            links={"controller": link},
            session=2,
        )
        hb = Heartbeat(node.send_local, dst=p.ADDR_CONTROLLER, interval_s=1.0)

        frame = hb.tick(now=10.0)
        self.assertIsNotNone(frame)
        self.assertEqual(frame.family, p.FAMILY_NETMGMT)
        self.assertEqual(frame.type, p.NETMGMT_HEARTBEAT)
        self.assertEqual(frame.dst, p.ADDR_CONTROLLER)
        self.assertEqual(link.sent, [frame])

    def test_respects_interval(self):
        link = CapturingLink()
        node = Node(
            addr=p.ADDR_SENDER_C,
            routes={},
            default_route="controller",
            links={"controller": link},
            session=2,
        )
        hb = Heartbeat(node.send_local, dst=p.ADDR_CONTROLLER, interval_s=1.0)

        hb.tick(now=10.0)
        self.assertIsNone(hb.tick(now=10.5))
        self.assertIsNotNone(hb.tick(now=11.0))
        self.assertEqual(len(link.sent), 2)


class PeerHealthTests(unittest.TestCase):
    def test_unobserved_peer_is_offline(self):
        health = PeerHealth(peers=[p.ADDR_SENDER_C], timeout_s=3.0)
        self.assertFalse(health.is_online(p.ADDR_SENDER_C))
        self.assertIsNone(health.last_seen(p.ADDR_SENDER_C))

    def test_first_frame_marks_peer_online(self):
        health = PeerHealth(peers=[p.ADDR_SENDER_C], timeout_s=3.0)
        health.observe(_frame(p.ADDR_SENDER_C, session=1), now=10.0)
        self.assertTrue(health.is_online(p.ADDR_SENDER_C))
        self.assertEqual(health.last_seen(p.ADDR_SENDER_C), 10.0)

    def test_silence_past_timeout_marks_peer_offline(self):
        health = PeerHealth(peers=[p.ADDR_SENDER_C], timeout_s=3.0)
        health.observe(_frame(p.ADDR_SENDER_C, session=1), now=10.0)

        self.assertEqual(health.offline_peers(now=12.0), [])
        self.assertEqual(health.offline_peers(now=13.0), [p.ADDR_SENDER_C])
        self.assertFalse(health.is_online(p.ADDR_SENDER_C))
        # Already-offline peers are not re-reported.
        self.assertEqual(health.offline_peers(now=14.0), [])

    def test_session_change_resets_online(self):
        health = PeerHealth(peers=[p.ADDR_SENDER_C], timeout_s=3.0)
        health.observe(_frame(p.ADDR_SENDER_C, session=1), now=10.0)
        health.observe(_frame(p.ADDR_SENDER_C, session=2), now=10.5)
        self.assertTrue(health.is_online(p.ADDR_SENDER_C))

    def test_unknown_peer_observation_is_ignored(self):
        health = PeerHealth(peers=[p.ADDR_SENDER_C], timeout_s=3.0)
        health.observe(_frame(p.ADDR_SENDER_L1, session=1), now=10.0)
        self.assertFalse(health.is_online(p.ADDR_SENDER_L1))


def _frame(src: int, session: int = 1) -> p.Frame:
    return p.Frame(
        src=src,
        dst=p.ADDR_CONTROLLER,
        flags=0,
        session=session,
        seq=1,
        family=p.FAMILY_NETMGMT,
        type=p.NETMGMT_HEARTBEAT,
        payload=b"",
    )


if __name__ == "__main__":
    unittest.main()
