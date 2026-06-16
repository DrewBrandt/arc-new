import unittest

from arc_protocol import protocol as p
from arc_protocol.reliable import ReliableEndpoint


class ReliableEndpointTests(unittest.TestCase):
    def endpoint(self, my_addr=p.ADDR_CONTROLLER, session=7, **kwargs):
        sent = []
        delivered = []
        failed = []
        endpoint = ReliableEndpoint(
            my_addr=my_addr,
            session=session,
            send_frame=sent.append,
            deliver_frame=delivered.append,
            fail_frame=failed.append,
            **kwargs,
        )
        return endpoint, sent, delivered, failed

    def test_unreliable_send_is_not_pending(self):
        endpoint, sent, delivered, failed = self.endpoint()

        frame = endpoint.send(
            dst=p.ADDR_SENDER_AIRBRAKE,
            family=p.FAMILY_VIDEO,
            type=0x10,
            payload=b"status",
            reliable=False,
            now=10.0,
        )

        self.assertEqual(sent, [frame])
        self.assertEqual(endpoint.pending_count, 0)
        self.assertEqual(delivered, [])
        self.assertEqual(failed, [])
        self.assertFalse(frame.flags & p.FLAG_RELIABLE)

    def test_reliable_send_tracks_pending_until_ack(self):
        endpoint, sent, delivered, failed = self.endpoint(first_seq=100)

        frame = endpoint.send(
            dst=p.ADDR_SENDER_AIRBRAKE,
            family=p.FAMILY_VIDEO,
            type=0x01,
            reliable=True,
            now=1.0,
        )

        self.assertEqual(endpoint.pending_count, 1)
        self.assertTrue(frame.flags & p.FLAG_RELIABLE)

        ack = p.Frame(
            src=p.ADDR_SENDER_AIRBRAKE,
            dst=p.ADDR_CONTROLLER,
            flags=p.FLAG_ACK,
            session=2,
            seq=55,
            family=p.FAMILY_NETMGMT,
            type=p.NETMGMT_ACK,
            payload=bytes((0x00, 0x64)),
        )
        result = endpoint.receive(ack)

        self.assertEqual(result.action, "ack")
        self.assertEqual(endpoint.pending_count, 0)
        self.assertEqual(sent, [frame])
        self.assertEqual(delivered, [])
        self.assertEqual(failed, [])

    def test_timeout_retransmits_then_failure_after_retries(self):
        endpoint, sent, delivered, failed = self.endpoint(timeout_s=1.0, max_retries=2)
        frame = endpoint.send(
            dst=p.ADDR_SENDER_AIRBRAKE,
            family=p.FAMILY_VIDEO,
            type=0x01,
            reliable=True,
            now=0.0,
        )

        endpoint.tick(0.5)
        self.assertEqual(sent, [frame])
        self.assertEqual(failed, [])

        endpoint.tick(1.0)
        self.assertEqual(sent, [frame, frame])
        self.assertEqual(endpoint.pending_count, 1)

        endpoint.tick(2.0)
        self.assertEqual(sent, [frame, frame, frame])
        self.assertEqual(endpoint.pending_count, 1)

        endpoint.tick(3.0)
        self.assertEqual(sent, [frame, frame, frame])
        self.assertEqual(failed, [frame])
        self.assertEqual(endpoint.pending_count, 0)
        self.assertEqual(delivered, [])

    def test_reliable_incoming_is_delivered_and_acked(self):
        endpoint, sent, delivered, failed = self.endpoint(first_seq=9)
        incoming = p.Frame(
            src=p.ADDR_FC_C,
            dst=p.ADDR_CONTROLLER,
            flags=p.FLAG_RELIABLE,
            session=3,
            seq=0x1234,
            family=p.FAMILY_FC_COORD,
            type=0x20,
            payload=b"cmd",
        )

        result = endpoint.receive(incoming)

        self.assertEqual(result.action, "delivered")
        self.assertEqual(delivered, [incoming])
        self.assertEqual(failed, [])
        self.assertEqual(len(sent), 1)

        ack = sent[0]
        self.assertEqual(ack.src, p.ADDR_CONTROLLER)
        self.assertEqual(ack.dst, p.ADDR_FC_C)
        self.assertEqual(ack.flags, p.FLAG_ACK)
        self.assertEqual(ack.session, 7)
        self.assertEqual(ack.seq, 9)
        self.assertEqual(ack.family, p.FAMILY_NETMGMT)
        self.assertEqual(ack.type, p.NETMGMT_ACK)
        self.assertEqual(ack.payload, bytes.fromhex("1234"))

    def test_duplicate_reliable_incoming_is_acked_but_not_redelivered(self):
        endpoint, sent, delivered, failed = self.endpoint()
        incoming = p.Frame(
            src=p.ADDR_FC_C,
            dst=p.ADDR_CONTROLLER,
            flags=p.FLAG_RELIABLE,
            session=3,
            seq=1,
            family=p.FAMILY_FC_COORD,
            type=0x20,
            payload=b"cmd",
        )

        self.assertEqual(endpoint.receive(incoming).action, "delivered")
        self.assertEqual(endpoint.receive(incoming).action, "duplicate")

        self.assertEqual(delivered, [incoming])
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0].payload, bytes.fromhex("0001"))
        self.assertEqual(sent[1].payload, bytes.fromhex("0001"))
        self.assertEqual(failed, [])

    def test_session_change_resets_dedup_for_source(self):
        endpoint, sent, delivered, failed = self.endpoint()
        first = p.Frame(
            src=p.ADDR_FC_C,
            dst=p.ADDR_CONTROLLER,
            flags=p.FLAG_RELIABLE,
            session=3,
            seq=1,
            family=p.FAMILY_FC_COORD,
            type=0x20,
            payload=b"old",
        )
        second = p.Frame(
            src=p.ADDR_FC_C,
            dst=p.ADDR_CONTROLLER,
            flags=p.FLAG_RELIABLE,
            session=4,
            seq=1,
            family=p.FAMILY_FC_COORD,
            type=0x20,
            payload=b"new",
        )

        self.assertEqual(endpoint.receive(first).action, "delivered")
        self.assertEqual(endpoint.receive(first).action, "duplicate")
        self.assertEqual(endpoint.receive(second).action, "delivered")

        self.assertEqual(delivered, [first, second])
        self.assertEqual(len(sent), 3)
        self.assertEqual(failed, [])

    def test_frame_for_another_destination_is_ignored(self):
        endpoint, sent, delivered, failed = self.endpoint()
        incoming = p.Frame(
            src=p.ADDR_FC_C,
            dst=p.ADDR_SENDER_AIRBRAKE,
            flags=p.FLAG_RELIABLE,
            session=3,
            seq=1,
            family=p.FAMILY_FC_COORD,
            type=0x20,
            payload=b"cmd",
        )

        self.assertEqual(endpoint.receive(incoming).action, "ignored")
        self.assertEqual(sent, [])
        self.assertEqual(delivered, [])
        self.assertEqual(failed, [])


if __name__ == "__main__":
    unittest.main()

