import unittest

from arc_protocol import protocol
from arc.video_ports import video_port_for_sender


class VideoPortTests(unittest.TestCase):
    def test_sender_addresses_map_to_50xx_ports(self):
        self.assertEqual(video_port_for_sender(protocol.ADDR_SENDER_DOWN), 5011)
        self.assertEqual(video_port_for_sender(protocol.ADDR_SENDER_AIRBRAKE), 5012)
        self.assertEqual(video_port_for_sender(protocol.ADDR_SENDER_PAYLOAD), 5013)
        self.assertEqual(video_port_for_sender(protocol.ADDR_SENDER_GROUND), 5015)

    def test_non_sender_address_rejected(self):
        with self.assertRaises(ValueError):
            video_port_for_sender(protocol.ADDR_CONTROLLER)


if __name__ == "__main__":
    unittest.main()
