"""Tests for the sender pipeline that don't require GStreamer.

Live capture/encode runs only on the Pi; here we cover the pure-Python
pieces: pipeline shape, state-flag transitions across start/stop/hard
boundaries, the recording-path generator, and bitrate validation.
"""

from __future__ import annotations

import datetime as _dt
import unittest
from pathlib import Path

from arc.config import SenderConfig, UartConfig, VideoConfig
from arc.pipeline_sender import (
    I_FRAME_PERIOD,
    PipelineError,
    SenderPipeline,
)


def _config(
    addr: int = 0x12,
    name: str = "sender-c",
    width: int = 640,
    height: int = 480,
    framerate: int = 30,
    bitrate: int = 2_500_000,
    recording_path: str = "/tmp/arc-recordings",
) -> SenderConfig:
    return SenderConfig(
        addr=addr,
        name=name,
        paired_fc=0x03,
        controller_ip="10.42.0.1",
        controller_port=6000,
        controller_addr=0x10,
        video=VideoConfig(
            width=width,
            height=height,
            framerate=framerate,
            bitrate_bps=bitrate,
            recording_path=recording_path,
        ),
        uart=UartConfig(device="/dev/ttyAMA0"),
    )


def _fixed_clock(when: str = "2026-05-08 12:34:56") -> "callable":
    fixed = _dt.datetime.strptime(when, "%Y-%m-%d %H:%M:%S")
    return lambda: fixed


class PipelineDescriptionTests(unittest.TestCase):
    def test_contains_required_elements(self):
        pipe = SenderPipeline(_config(), clock=_fixed_clock())
        desc = pipe.build_pipeline_description()
        for needed in [
            "libcamerasrc",
            "v4l2h264enc",
            "tee name=t",
            "valve name=tx_valve drop=true",
            "valve name=rec_valve drop=true",
            "rtph264pay",
            "udpsink",
            "mp4mux",
            "filesink location=",
            "host=10.42.0.1",
            "port=5012",
            "profile=baseline",
            "h264parse config-interval=1",
            "sync=false",
        ]:
            self.assertIn(needed, desc, f"missing {needed!r} in pipeline:\n{desc}")

    def test_i_frame_period_pinned_to_design_doc_value(self):
        # Design doc §7.5: "Senders use I-frame period of 10".
        pipe = SenderPipeline(_config(), clock=_fixed_clock())
        desc = pipe.build_pipeline_description()
        self.assertEqual(I_FRAME_PERIOD, 10)
        self.assertIn(f"h264_i_frame_period={I_FRAME_PERIOD}", desc)

    def test_bitrate_appears_in_encoder_controls(self):
        pipe = SenderPipeline(_config(bitrate=1_500_000), clock=_fixed_clock())
        desc = pipe.build_pipeline_description()
        self.assertIn("video_bitrate=1500000", desc)

    def test_default_video_port_is_derived_from_sender_address(self):
        pipe = SenderPipeline(_config(addr=0x13, name="sender-l1"), clock=_fixed_clock())
        self.assertIn("port=5013", pipe.build_pipeline_description())

    def test_set_bitrate_takes_effect_on_next_description(self):
        pipe = SenderPipeline(_config(bitrate=2_500_000), clock=_fixed_clock())
        pipe.set_bitrate(3_000_000)
        self.assertEqual(pipe.bitrate_bps, 3_000_000)
        self.assertIn("video_bitrate=3000000", pipe.build_pipeline_description())

    def test_set_bitrate_rejects_non_positive(self):
        pipe = SenderPipeline(_config())
        with self.assertRaises(PipelineError):
            pipe.set_bitrate(0)
        with self.assertRaises(PipelineError):
            pipe.set_bitrate(-1)

    def test_caps_match_video_config(self):
        pipe = SenderPipeline(_config(width=1280, height=720, framerate=15), clock=_fixed_clock())
        desc = pipe.build_pipeline_description()
        self.assertIn("width=1280,height=720,framerate=15/1", desc)

    def test_explicit_rec_path_is_honoured(self):
        pipe = SenderPipeline(_config())
        forced = Path("/var/arc/recordings/explicit.mp4")
        desc = pipe.build_pipeline_description(forced)
        self.assertIn(f"filesink location={forced}", desc)

    def test_recording_path_includes_sender_name_and_timestamp(self):
        pipe = SenderPipeline(
            _config(name="sender-l1", recording_path="/var/arc/recordings"),
            clock=_fixed_clock("2026-05-08 12:34:56"),
        )
        path = pipe._next_recording_path()
        self.assertEqual(path, Path("/var/arc/recordings/sender-l1-20260508-123456.mp4"))

    def test_overrides_in_constructor_propagate(self):
        pipe = SenderPipeline(
            _config(),
            controller_ip="192.168.1.20",
            controller_port=6001,
            camera_source="videotestsrc is-live=true",
            encoder="x264enc tune=zerolatency",
            recording_path="/data/arc",
            clock=_fixed_clock(),
        )
        desc = pipe.build_pipeline_description()
        self.assertIn("host=192.168.1.20", desc)
        self.assertIn("port=6001", desc)
        self.assertIn("videotestsrc is-live=true", desc)
        self.assertIn("x264enc tune=zerolatency name=enc", desc)
        self.assertEqual(pipe.recording_dir, Path("/data/arc"))
        self.assertIn(str(Path("/data/arc") / "sender-c-20260508-123456.mp4"), desc)


class StateTransitionTests(unittest.TestCase):
    """State-flag transitions when the live pipeline isn't running.

    With no GStreamer pipeline built, the public methods still update
    ``transmitting`` / ``recording`` correctly. This proves the state
    machine before any GStreamer interaction.
    """

    def test_initial_state_is_idle(self):
        pipe = SenderPipeline(_config())
        self.assertFalse(pipe.transmitting)
        self.assertFalse(pipe.recording)
        self.assertIsNone(pipe.recording_file)

    def test_stop_stream_when_idle_is_noop(self):
        pipe = SenderPipeline(_config())
        pipe.stop_stream()
        self.assertFalse(pipe.transmitting)
        self.assertFalse(pipe.recording)

    def test_hard_stop_when_idle_is_noop(self):
        pipe = SenderPipeline(_config())
        pipe.hard_stop()
        self.assertFalse(pipe.transmitting)
        self.assertFalse(pipe.recording)

    def test_stop_stream_clears_transmitting_flag_even_without_live_pipeline(self):
        pipe = SenderPipeline(_config())
        # Manually set the flag as if start_stream had run on a Pi.
        pipe._transmitting = True
        pipe._recording = True
        pipe.stop_stream()
        self.assertFalse(pipe.transmitting)
        self.assertTrue(pipe.recording, "soft stop must keep recording on")

    def test_hard_stop_clears_both_flags_even_without_live_pipeline(self):
        pipe = SenderPipeline(_config())
        pipe._transmitting = True
        pipe._recording = True
        pipe.hard_stop()
        self.assertFalse(pipe.transmitting)
        self.assertFalse(pipe.recording)

    def test_no_public_path_to_tx_only_state(self):
        # The illegal "transmitting=True, recording=False" combination
        # is unreachable through the public API: start_stream sets both
        # to True; stop_stream only ever clears transmitting; hard_stop
        # clears both. There is no method that turns recording off
        # while leaving transmitting on.
        pipe = SenderPipeline(_config())
        pipe._transmitting = True
        pipe._recording = True
        pipe.stop_stream()
        self.assertFalse(pipe.transmitting)
        self.assertTrue(pipe.recording)
        # And no public method to flip recording off without also
        # turning transmitting off:
        public_methods = {
            name
            for name in dir(pipe)
            if not name.startswith("_") and callable(getattr(pipe, name))
        }
        self.assertEqual(
            public_methods & {"stop_recording", "stop_record"},
            set(),
            "should not expose a stop-recording-only method",
        )


if __name__ == "__main__":
    unittest.main()
