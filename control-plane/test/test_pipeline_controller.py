"""Tests for the controller pipeline that don't require GStreamer.

The pipeline itself only runs on a Pi with libcamera + DRM/KMS, so the
unit tests cover the pure-Python pieces: layout parsing, the launch
description we hand to ``parse_launch``, and the error paths that
trigger before any GStreamer import.
"""

from __future__ import annotations

import unittest

from arc import protocol
from arc.config import ControllerConfig, SenderEntry, UartConfig
from arc.pipeline_controller import (
    ControllerPipeline,
    Layout,
    PipelineError,
    SlotProps,
    parse_layouts,
)


def _config(layouts: dict | None = None) -> ControllerConfig:
    return ControllerConfig(
        addr=protocol.ADDR_CONTROLLER,
        callsign="KD3BBP",
        uart=UartConfig(device="/dev/null"),
        listen_port=6000,
        senders=(
            SenderEntry(
                addr=protocol.ADDR_SENDER_C,
                name="sender-c",
                ip="10.42.0.12",
                paired_fc=protocol.ADDR_FC_C,
            ),
        ),
        layouts=layouts or {},
    )


class FakePad:
    def __init__(self):
        self.props = {}

    def set_property(self, name, value):
        self.props[name] = value


class FakeElement:
    def __init__(self):
        self.props = {}

    def set_property(self, name, value):
        self.props[name] = value


class FakeCompositor:
    def __init__(self):
        self.pads = {"sink_0": FakePad(), "sink_1": FakePad(), "sink_2": FakePad()}

    def get_static_pad(self, name):
        return self.pads.get(name)


class FakeGstPipeline:
    def __init__(self):
        self.comp = FakeCompositor()
        self.overlay = FakeElement()
        self.states = []

    def get_by_name(self, name):
        return {"comp": self.comp, "overlay": self.overlay}.get(name)

    def set_state(self, state):
        self.states.append(state)
        return FakeGst.StateChangeReturn.SUCCESS


class FakeGst:
    class State:
        PLAYING = "PLAYING"
        NULL = "NULL"

    class StateChangeReturn:
        SUCCESS = "SUCCESS"
        FAILURE = "FAILURE"

    launched = []
    pipelines = []

    @classmethod
    def init(cls, _):
        pass

    @classmethod
    def parse_launch(cls, desc):
        cls.launched.append(desc)
        pipeline = FakeGstPipeline()
        cls.pipelines.append(pipeline)
        return pipeline


class SlotPropsTests(unittest.TestCase):
    def test_defaults_match_compositor_defaults(self):
        s = SlotProps()
        self.assertEqual((s.xpos, s.ypos, s.width, s.height), (0, 0, 0, 0))
        self.assertEqual(s.alpha, 1.0)
        self.assertEqual(s.zorder, 0)

    def test_from_mapping_accepts_all_known_keys(self):
        s = SlotProps.from_mapping(
            {"xpos": 10, "ypos": 20, "width": 640, "height": 480, "alpha": 0.5, "zorder": 2}
        )
        self.assertEqual(s.xpos, 10)
        self.assertEqual(s.ypos, 20)
        self.assertEqual(s.width, 640)
        self.assertEqual(s.height, 480)
        self.assertEqual(s.alpha, 0.5)
        self.assertEqual(s.zorder, 2)

    def test_from_mapping_z_aliases_to_zorder(self):
        # The design doc writes "z=2" in its example layouts; compositor's
        # property is actually "zorder".
        self.assertEqual(SlotProps.from_mapping({"z": 3}).zorder, 3)

    def test_from_mapping_rejects_unknown_keys(self):
        with self.assertRaises(PipelineError):
            SlotProps.from_mapping({"nope": 1})


class ParseLayoutsTests(unittest.TestCase):
    def test_design_doc_layouts_round_trip(self):
        # Layout examples drawn from design doc Section 7.4.
        raw = {
            "local_full": {
                "slot_0": {"xpos": 0, "ypos": 0, "width": 1280, "height": 480, "alpha": 1.0},
                "slot_1": {"alpha": 0.0},
            },
            "remote_full": {
                "slot_0": {"alpha": 0.0},
                "slot_1": {"xpos": 0, "ypos": 0, "width": 1280, "height": 480, "alpha": 1.0},
            },
            "split": {
                "slot_0": {"xpos": 0, "ypos": 0, "width": 640, "height": 480, "alpha": 1.0},
                "slot_1": {"xpos": 640, "ypos": 0, "width": 640, "height": 480, "alpha": 1.0},
            },
            "pip_remote_big": {
                "slot_0": {"xpos": 960, "ypos": 360, "width": 320, "height": 120, "z": 2, "alpha": 1.0},
                "slot_1": {"xpos": 0, "ypos": 0, "width": 1280, "height": 480, "z": 1, "alpha": 1.0},
            },
        }
        layouts = parse_layouts(raw)
        self.assertEqual(set(layouts), set(raw))
        self.assertIsInstance(layouts["local_full"], Layout)
        self.assertEqual(layouts["local_full"].slot_0.width, 1280)
        self.assertEqual(layouts["local_full"].slot_1.alpha, 0.0)
        # z alias resolved
        self.assertEqual(layouts["pip_remote_big"].slot_0.zorder, 2)
        self.assertEqual(layouts["pip_remote_big"].slot_1.zorder, 1)

    def test_missing_slot_uses_defaults(self):
        layouts = parse_layouts({"only_zero": {"slot_0": {"alpha": 1.0}}})
        self.assertEqual(layouts["only_zero"].slot_1, SlotProps())

    def test_layout_must_be_table(self):
        with self.assertRaises(PipelineError):
            parse_layouts({"bad": "not a table"})  # type: ignore[arg-type]


class ControllerPipelineTests(unittest.TestCase):
    def test_construct_without_gstreamer(self):
        # Importing and instantiating must not require gi.
        pipe = ControllerPipeline(_config({"local_full": {"slot_0": {"alpha": 1.0}}}))
        self.assertEqual(pipe.callsign, "KD3BBP")
        self.assertIsNone(pipe.current_layout)
        self.assertIn("local_full", pipe.layouts)

    def test_pipeline_description_contains_required_elements(self):
        pipe = ControllerPipeline(_config())
        desc = pipe.build_pipeline_description()
        self.assertIn("compositor name=comp", desc)
        self.assertIn("textoverlay name=overlay", desc)
        self.assertIn("KD3BBP", desc)  # callsign burned in
        self.assertIn("kmssink", desc)  # default analog sink
        self.assertIn("comp.sink_0", desc)
        self.assertIn("comp.sink_1", desc)
        self.assertIn("comp.sink_2", desc)
        self.assertIn("libcamerasrc", desc)  # default slot 0 source
        self.assertIn("format=I420", desc)
        self.assertIn("video/x-raw,width=720,height=480", desc)

    def test_set_layout_unknown_raises(self):
        pipe = ControllerPipeline(_config())
        with self.assertRaises(PipelineError):
            pipe.set_layout("does_not_exist")

    def test_set_layout_records_current_even_before_start(self):
        # set_layout is callable before start(); the marshalled apply
        # is a no-op until the pipeline is built, but the desired
        # current_layout is recorded so start() can resume it.
        pipe = ControllerPipeline(
            _config({"split": {"slot_0": {"alpha": 1.0}, "slot_1": {"alpha": 1.0}}})
        )
        pipe.set_layout("split")
        self.assertEqual(pipe.current_layout, "split")

    def test_startup_layout_can_be_configured(self):
        pipe = ControllerPipeline(
            _config(
                {
                    "local_full": {"slot_0": {"alpha": 1.0}},
                    "split": {"slot_0": {"alpha": 0.5}, "slot_1": {"alpha": 1.0}},
                }
            ),
            startup_layout="split",
        )

        self.assertEqual(pipe._initial_layout().name, "split")

    def test_unknown_startup_layout_raises(self):
        pipe = ControllerPipeline(
            _config({"local_full": {"slot_0": {"alpha": 1.0}}}),
            startup_layout="missing",
        )

        with self.assertRaises(PipelineError):
            pipe._initial_layout()

    def test_set_overlay_records_text_when_idle(self):
        pipe = ControllerPipeline(_config())
        pipe.set_overlay("KD3BBP / TEST")
        self.assertEqual(pipe._overlay_text, "KD3BBP / TEST")

    def test_callsign_override_takes_precedence(self):
        pipe = ControllerPipeline(_config(), callsign="W1AW")
        self.assertEqual(pipe.callsign, "W1AW")
        self.assertIn("W1AW", pipe.build_pipeline_description())

    def test_custom_slot_sources_appear_in_pipeline(self):
        pipe = ControllerPipeline(
            _config(),
            slot_0_source="udpsrc port=5000 ! fakesink",
            slot_1_source="videotestsrc pattern=smpte",
        )
        desc = pipe.build_pipeline_description()
        self.assertIn("udpsrc port=5000", desc)
        self.assertIn("videotestsrc pattern=smpte", desc)

    def test_set_source_records_remote_sender_source(self):
        pipe = ControllerPipeline(_config())
        pipe.set_source(1, protocol.ADDR_SENDER_C)

        self.assertEqual(pipe.slot_sources[1].addr, protocol.ADDR_SENDER_C)
        desc = pipe.build_pipeline_description()
        self.assertIn("udpsrc port=5012", desc)
        self.assertIn("rtpjitterbuffer latency=40", desc)
        self.assertIn("rtph264depay", desc)
        self.assertIn("v4l2h264dec", desc)

    def test_set_source_empty_and_local_sources(self):
        pipe = ControllerPipeline(_config())
        pipe.set_source(1, protocol.ADDR_SENDER_C)
        pipe.set_source(1, protocol.ADDR_UNASSIGNED)
        pipe.set_source(0, protocol.ADDR_CONTROLLER)

        self.assertEqual(pipe.slot_sources[0].addr, protocol.ADDR_CONTROLLER)
        self.assertEqual(pipe.slot_sources[1].addr, protocol.ADDR_UNASSIGNED)
        desc = pipe.build_pipeline_description()
        self.assertIn("libcamerasrc", desc)
        self.assertIn("videotestsrc pattern=black", desc)

    def test_set_source_rejects_unknown_sender_and_bad_slot(self):
        pipe = ControllerPipeline(_config())
        with self.assertRaises(PipelineError):
            pipe.set_source(1, protocol.ADDR_SENDER_L1)
        with self.assertRaises(PipelineError):
            pipe.set_source(3, protocol.ADDR_SENDER_C)

    def test_default_mixer_is_compositor(self):
        pipe = ControllerPipeline(_config())
        self.assertEqual(pipe.mixer, "compositor")
        desc = pipe.build_pipeline_description()
        self.assertIn("compositor name=comp", desc)
        self.assertNotIn("glvideomixer", desc)
        self.assertNotIn("glupload", desc)
        self.assertNotIn("gldownload", desc)

    def test_glvideomixer_inserts_gl_elements(self):
        pipe = ControllerPipeline(_config(), mixer="glvideomixer")
        desc = pipe.build_pipeline_description()
        # Mixer element swapped, with gldownload + videoconvert before textoverlay.
        self.assertIn(
            "glvideomixer name=comp ! video/x-raw(memory:GLMemory),width=720,height=480"
            " ! gldownload ! videoconvert",
            desc,
        )
        # Background plus both user-controlled slots are fed via glupload.
        self.assertEqual(desc.count(" ! glupload ! comp.sink_"), 3)
        self.assertIn("videotestsrc pattern=black is-live=true", desc)
        # textoverlay/sink/layout-application stay unchanged.
        self.assertIn("textoverlay name=overlay", desc)
        self.assertIn("kmssink", desc)

    def test_unknown_mixer_raises(self):
        with self.assertRaises(PipelineError):
            ControllerPipeline(_config(), mixer="bogusmixer")

    def test_set_source_rebuilds_live_pipeline_and_preserves_state(self):
        import arc.pipeline_controller as pc

        original = pc._import_gstreamer
        FakeGst.launched = []
        FakeGst.pipelines = []
        pc._import_gstreamer = lambda: FakeGst
        try:
            pipe = ControllerPipeline(
                _config({"split": {"slot_0": {"alpha": 1.0}, "slot_1": {"alpha": 1.0}}})
            )
            pipe.set_overlay("KD3BBP / BOOST")
            pipe.set_layout("split")
            pipe.start()
            pipe.set_source(1, protocol.ADDR_SENDER_C)
        finally:
            pc._import_gstreamer = original

        self.assertEqual(len(FakeGst.launched), 2)
        self.assertIn("videotestsrc pattern=black", FakeGst.launched[0])
        self.assertIn("udpsrc port=5012", FakeGst.launched[1])
        self.assertEqual(FakeGst.pipelines[0].states, ["PLAYING", "NULL"])
        self.assertEqual(FakeGst.pipelines[1].states, ["PLAYING"])
        self.assertEqual(FakeGst.pipelines[1].overlay.props["text"], "KD3BBP / BOOST")
        self.assertEqual(FakeGst.pipelines[1].comp.pads["sink_0"].props["width"], 720)
        self.assertEqual(FakeGst.pipelines[1].comp.pads["sink_0"].props["height"], 480)
        self.assertEqual(FakeGst.pipelines[1].comp.pads["sink_0"].props["zorder"], 0)
        self.assertEqual(FakeGst.pipelines[1].comp.pads["sink_1"].props["alpha"], 1.0)
        self.assertEqual(FakeGst.pipelines[1].comp.pads["sink_2"].props["alpha"], 1.0)
        self.assertEqual(pipe.current_layout, "split")


if __name__ == "__main__":
    unittest.main()
