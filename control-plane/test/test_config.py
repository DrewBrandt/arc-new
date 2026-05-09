import tempfile
import textwrap
import unittest
from pathlib import Path

from arc import protocol as p
from arc.config import (
    ConfigError,
    ControllerConfig,
    ControllerVideoConfig,
    SenderConfig,
    load_controller_config,
    load_sender_config,
)


def write_toml(content: str) -> Path:
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".toml", delete=False, encoding="utf-8"
    )
    tmp.write(textwrap.dedent(content))
    tmp.close()
    return Path(tmp.name)


class ControllerConfigTests(unittest.TestCase):
    def test_loads_full_controller_config(self):
        path = write_toml(
            """
            [node]
            address = 0x10

            [uart]
            device = "/dev/serial0"
            baud = 115200

            [overlay]
            callsign = "KD3BBP"

            [controller]
            listen_port = 6000

            [video]
            mixer = "glvideomixer"
            sink = "kmssink connector-id=51 sync=false"

            [[senders]]
            id = 0x12
            name = "sender-c"
            ip = "10.42.0.12"
            paired_fc = 0x03

            [[senders]]
            id = 0x14
            name = "sender-l2"
            ip = "10.42.0.14"

            [layouts.split]
            slot_0 = { xpos = 0, ypos = 0, width = 640, height = 480, alpha = 1.0 }
            slot_1 = { xpos = 640, ypos = 0, width = 640, height = 480, alpha = 1.0 }

            [sources]
            slot_0 = 0x10
            slot_1 = 0x12
            """
        )
        cfg = load_controller_config(path)
        self.assertIsInstance(cfg, ControllerConfig)
        self.assertEqual(cfg.addr, p.ADDR_CONTROLLER)
        self.assertEqual(cfg.callsign, "KD3BBP")
        self.assertEqual(cfg.uart.device, "/dev/serial0")
        self.assertEqual(cfg.listen_port, 6000)
        self.assertEqual(len(cfg.senders), 2)
        self.assertEqual(cfg.senders[0].addr, 0x12)
        self.assertEqual(cfg.senders[0].paired_fc, 0x03)
        self.assertIsNone(cfg.senders[1].paired_fc)
        self.assertIn("split", cfg.layouts)
        self.assertEqual(cfg.initial_sources, (p.ADDR_CONTROLLER, p.ADDR_SENDER_C))
        self.assertIsInstance(cfg.video, ControllerVideoConfig)
        self.assertEqual(cfg.video.mixer, "glvideomixer")
        self.assertEqual(cfg.video.sink, "kmssink connector-id=51 sync=false")

    def test_controller_video_defaults_to_software_compositor(self):
        path = write_toml(
            """
            [node]
            address = 0x10

            [uart]
            device = "/dev/serial0"

            [overlay]
            callsign = "KD3BBP"
            """
        )
        cfg = load_controller_config(path)
        self.assertEqual(cfg.video.mixer, "compositor")
        self.assertEqual(cfg.video.sink, "kmssink sync=false")

    def test_rejects_wrong_node_address(self):
        path = write_toml(
            """
            [node]
            address = 0x11

            [uart]
            device = "/dev/serial0"

            [overlay]
            callsign = "KD3BBP"
            """
        )
        with self.assertRaises(ConfigError):
            load_controller_config(path)

    def test_missing_uart_section_raises(self):
        path = write_toml(
            """
            [node]
            address = 0x10
            [overlay]
            callsign = "KD3BBP"
            """
        )
        with self.assertRaises(ConfigError):
            load_controller_config(path)


class SenderConfigTests(unittest.TestCase):
    def test_loads_sender_with_paired_fc(self):
        path = write_toml(
            """
            [node]
            address = 0x12
            name = "sender-c"
            paired_fc = 0x03

            [controller]
            ip = "10.42.0.1"
            port = 6000

            [video]
            width = 640
            height = 480
            framerate = 30
            bitrate = 2500000
            start_stream_on_boot = true

            [recording]
            path = "/var/arc/recordings/"

            [uart]
            device = "/dev/serial0"
            baud = 115200
            """
        )
        cfg = load_sender_config(path)
        self.assertIsInstance(cfg, SenderConfig)
        self.assertEqual(cfg.addr, 0x12)
        self.assertEqual(cfg.paired_fc, 0x03)
        self.assertEqual(cfg.controller_ip, "10.42.0.1")
        self.assertEqual(cfg.controller_port, 6000)
        self.assertEqual(cfg.video.bitrate_bps, 2_500_000)
        self.assertTrue(cfg.video.start_stream_on_boot)
        self.assertIsNotNone(cfg.uart)
        self.assertEqual(cfg.uart.device, "/dev/serial0")

    def test_video_only_sender_omits_uart(self):
        path = write_toml(
            """
            [node]
            address = 0x14
            name = "sender-l2"

            [controller]
            ip = "10.42.0.1"

            [video]
            width = 640
            height = 480
            framerate = 30
            bitrate = 2500000
            """
        )
        cfg = load_sender_config(path)
        self.assertIsNone(cfg.paired_fc)
        self.assertIsNone(cfg.uart)

    def test_paired_fc_without_uart_raises(self):
        path = write_toml(
            """
            [node]
            address = 0x12
            paired_fc = 0x03

            [controller]
            ip = "10.42.0.1"

            [video]
            width = 640
            height = 480
            framerate = 30
            bitrate = 2500000
            """
        )
        with self.assertRaises(ConfigError):
            load_sender_config(path)

    def test_missing_controller_ip_raises(self):
        path = write_toml(
            """
            [node]
            address = 0x12

            [controller]

            [video]
            bitrate = 2500000
            """
        )
        with self.assertRaises(ConfigError):
            load_sender_config(path)


class FileNotFoundTests(unittest.TestCase):
    def test_missing_file_raises_config_error(self):
        with self.assertRaises(ConfigError):
            load_controller_config("/no/such/path.toml")


if __name__ == "__main__":
    unittest.main()
