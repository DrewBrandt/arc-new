"""Tests for the wiring between control-plane shells and the pipelines.

Covers:
- :class:`arc.sender.Sender` invokes its ``video_command_handler`` after
  applying each VIDEO command.
- :class:`arc.controller.Controller` dispatches FC_VIDEO frames to its
  ``fc_video_handler`` (and falls back to ``unhandled_frames`` when no
  handler is registered, preserving prior behaviour).
- The adapter helpers in ``sender_main`` and ``controller_main`` route
  to the corresponding pipeline methods and swallow ``PipelineError``.

No GStreamer is loaded; the pipelines are replaced with simple recorders.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import Any

from arc import messages, protocol
from arc.controller import Controller, ControllerError
from arc.controller_main import SourceSwitcher, make_fc_video_handler
from arc.pipeline_sender import PipelineError
from arc.sender import Sender
from arc.sender_main import _apply_boot_video_command, make_video_command_handler


def _frame(
    *,
    src: int,
    dst: int,
    family: int,
    type: int,
    payload: bytes = b"",
    seq: int = 0,
    flags: int = 0,
    session: int = 1,
) -> protocol.Frame:
    return protocol.Frame(
        src=src,
        dst=dst,
        flags=flags,
        session=session,
        seq=seq,
        family=family,
        type=type,
        payload=payload,
    )


# --- Sender -> video_command_handler ---------------------------------


class SenderVideoHandlerTests(unittest.TestCase):
    def setUp(self):
        self.calls: list[tuple[messages.VideoType, protocol.Frame]] = []

        def handler(vt: messages.VideoType, frame: protocol.Frame) -> None:
            self.calls.append((vt, frame))

        self.sender = Sender(
            addr=protocol.ADDR_SENDER_C,
            paired_fc=protocol.ADDR_FC_C,
            video_command_handler=handler,
        )

    def _deliver(self, type_: int, payload: bytes = b"") -> None:
        frame = _frame(
            src=protocol.ADDR_CONTROLLER,
            dst=self.sender.addr,
            family=protocol.FAMILY_VIDEO,
            type=type_,
            payload=payload,
        )
        self.sender._handle_local_frame(frame, now=0.0)

    def test_handler_called_for_start_stream(self):
        self._deliver(messages.VideoType.START_STREAM)
        self.assertEqual(len(self.calls), 1)
        vt, _ = self.calls[0]
        self.assertIs(vt, messages.VideoType.START_STREAM)
        self.assertTrue(self.sender.transmitting)
        self.assertTrue(self.sender.recording)

    def test_handler_called_for_each_command_type(self):
        self._deliver(messages.VideoType.START_STREAM)
        self._deliver(messages.VideoType.STOP_STREAM)
        self._deliver(messages.VideoType.HARD_STOP)
        self._deliver(messages.VideoType.SET_BITRATE, payload=(2_500_000).to_bytes(4, "big"))
        types = [vt for vt, _ in self.calls]
        self.assertEqual(
            types,
            [
                messages.VideoType.START_STREAM,
                messages.VideoType.STOP_STREAM,
                messages.VideoType.HARD_STOP,
                messages.VideoType.SET_BITRATE,
            ],
        )

    def test_handler_called_after_state_already_updated(self):
        # The handler runs once flag mutation is complete, so it sees
        # the post-command state.
        observed: list[tuple[bool, bool]] = []

        def handler(vt, frame):
            observed.append((self.sender.transmitting, self.sender.recording))

        self.sender.video_command_handler = handler
        self._deliver(messages.VideoType.START_STREAM)
        self._deliver(messages.VideoType.STOP_STREAM)
        self.assertEqual(observed, [(True, True), (False, True)])

    def test_handler_optional_default_no_op(self):
        sender = Sender(addr=protocol.ADDR_SENDER_C)
        # No handler registered; applying a command must not raise.
        frame = _frame(
            src=protocol.ADDR_CONTROLLER,
            dst=sender.addr,
            family=protocol.FAMILY_VIDEO,
            type=messages.VideoType.START_STREAM,
        )
        sender._handle_local_frame(frame, now=0.0)
        self.assertTrue(sender.transmitting)


# --- Controller -> fc_video_handler ----------------------------------


class ControllerFcVideoHandlerTests(unittest.TestCase):
    def _deliver(
        self,
        controller: Controller,
        type_: int,
        payload: bytes = b"",
    ) -> None:
        frame = _frame(
            src=protocol.ADDR_FC_N,
            dst=protocol.ADDR_CONTROLLER,
            family=protocol.FAMILY_FC_VIDEO,
            type=type_,
            payload=payload,
        )
        controller._handle_local_frame(frame, now=0.0)

    def test_handler_invoked_for_each_known_type(self):
        calls: list[tuple[messages.FcVideoType, protocol.Frame]] = []
        ctrl = Controller(fc_video_handler=lambda vt, f: calls.append((vt, f)))

        self._deliver(ctrl, messages.FcVideoType.SET_LAYOUT, payload=b"\x02")
        self._deliver(
            ctrl,
            messages.FcVideoType.SET_OVERLAY,
            payload=b"KD3BBP test\x00",
        )
        self._deliver(ctrl, messages.FcVideoType.SET_SOURCE, payload=b"\x01\x12")
        self._deliver(ctrl, messages.FcVideoType.GET_STATUS)

        types = [vt for vt, _ in calls]
        self.assertEqual(
            types,
            [
                messages.FcVideoType.SET_LAYOUT,
                messages.FcVideoType.SET_OVERLAY,
                messages.FcVideoType.SET_SOURCE,
                messages.FcVideoType.GET_STATUS,
            ],
        )
        # When a handler is registered, frames do NOT accumulate in
        # unhandled_frames.
        self.assertEqual(ctrl.unhandled_frames, [])

    def test_unknown_fc_video_type_raises(self):
        ctrl = Controller(fc_video_handler=lambda vt, f: None)
        with self.assertRaises(ControllerError):
            self._deliver(ctrl, type_=0xEE)

    def test_no_handler_falls_back_to_unhandled_frames(self):
        # Preserves prior behaviour: control-plane keeps these for
        # callers that wire in their own dispatch later.
        ctrl = Controller()  # no handler
        self._deliver(ctrl, messages.FcVideoType.SET_LAYOUT, payload=b"\x00")
        self.assertEqual(len(ctrl.unhandled_frames), 1)
        self.assertEqual(
            ctrl.unhandled_frames[0].family, protocol.FAMILY_FC_VIDEO
        )

    def test_existing_video_status_report_path_unchanged(self):
        # FC_VIDEO dispatch must not steal VIDEO STATUS_REPORT frames.
        ctrl = Controller(
            sender_addrs=(protocol.ADDR_SENDER_C,),
            fc_video_handler=lambda vt, f: None,
        )
        report = messages.StatusReport(
            state=0x03,
            cpu_temp_c=40,
            cpu_load_pct=15,
            free_disk_mb=12_000,
            rssi_dbm=-55,
            tx_frames=900,
            dropped_frames=0,
        )
        frame = _frame(
            src=protocol.ADDR_SENDER_C,
            dst=protocol.ADDR_CONTROLLER,
            family=protocol.FAMILY_VIDEO,
            type=messages.VideoType.STATUS_REPORT,
            payload=report.encode(),
        )
        ctrl._handle_local_frame(frame, now=0.0)
        # Status was absorbed by the SenderLink, not by FC_VIDEO logic.
        self.assertEqual(ctrl.unhandled_frames, [])


# --- Adapter helpers --------------------------------------------------


@dataclass
class FakeSenderPipeline:
    started: int = 0
    stopped: int = 0
    hard_stopped: int = 0
    bitrate: int = 0

    def start_stream(self) -> None:
        self.started += 1

    def stop_stream(self) -> None:
        self.stopped += 1

    def hard_stop(self) -> None:
        self.hard_stopped += 1

    def set_bitrate(self, bps: int) -> None:
        self.bitrate = bps


@dataclass
class FakeControllerPipeline:
    layouts_set: list[str] = None
    overlays_set: list[str] = None
    sources_set: list[tuple[int, int]] = None

    def __post_init__(self):
        self.layouts_set = []
        self.overlays_set = []
        self.sources_set = []

    def set_layout(self, name: str) -> None:
        self.layouts_set.append(name)

    def set_overlay(self, text: str) -> None:
        self.overlays_set.append(text)

    def set_source(self, slot_id: int, source_addr: int) -> None:
        self.sources_set.append((slot_id, source_addr))


@dataclass
class FakeLink:
    sent: list[protocol.Frame] = None

    def __post_init__(self):
        self.sent = []

    def send(self, frame: protocol.Frame) -> None:
        self.sent.append(frame)


class SenderMainAdapterTests(unittest.TestCase):
    def test_dispatches_each_video_command_to_pipeline(self):
        pipe = FakeSenderPipeline()
        handler = make_video_command_handler(pipe)
        f = _frame(
            src=0,
            dst=0,
            family=protocol.FAMILY_VIDEO,
            type=messages.VideoType.START_STREAM,
        )
        handler(messages.VideoType.START_STREAM, f)
        handler(messages.VideoType.STOP_STREAM, f)
        handler(messages.VideoType.HARD_STOP, f)
        bitrate_frame = _frame(
            src=0,
            dst=0,
            family=protocol.FAMILY_VIDEO,
            type=messages.VideoType.SET_BITRATE,
            payload=(1_800_000).to_bytes(4, "big"),
        )
        handler(messages.VideoType.SET_BITRATE, bitrate_frame)
        self.assertEqual(pipe.started, 1)
        self.assertEqual(pipe.stopped, 1)
        self.assertEqual(pipe.hard_stopped, 1)
        self.assertEqual(pipe.bitrate, 1_800_000)

    def test_pipeline_error_is_swallowed(self):
        class BoomPipeline:
            def start_stream(self) -> None:
                raise PipelineError("no GStreamer")

            def stop_stream(self) -> None: ...
            def hard_stop(self) -> None: ...
            def set_bitrate(self, bps: int) -> None: ...

        handler = make_video_command_handler(BoomPipeline())
        # Must not propagate.
        handler(
            messages.VideoType.START_STREAM,
            _frame(
                src=0, dst=0, family=protocol.FAMILY_VIDEO,
                type=messages.VideoType.START_STREAM,
            ),
        )

    def test_boot_video_command_updates_sender_and_pipeline(self):
        pipe = FakeSenderPipeline()
        sender = Sender(
            addr=protocol.ADDR_SENDER_C,
            paired_fc=None,
            video_command_handler=make_video_command_handler(pipe),
        )

        _apply_boot_video_command(sender, messages.VideoType.START_STREAM)

        self.assertTrue(sender.transmitting)
        self.assertTrue(sender.recording)
        self.assertEqual(pipe.started, 1)


class ControllerMainAdapterTests(unittest.TestCase):
    def test_set_layout_resolves_id_to_name_by_insertion_order(self):
        pipe = FakeControllerPipeline()
        handler = make_fc_video_handler(pipe, ["local_full", "split", "remote_full"])
        f = _frame(
            src=0,
            dst=0,
            family=protocol.FAMILY_FC_VIDEO,
            type=messages.FcVideoType.SET_LAYOUT,
            payload=b"\x01",
        )
        handler(messages.FcVideoType.SET_LAYOUT, f)
        self.assertEqual(pipe.layouts_set, ["split"])

    def test_set_layout_out_of_range_is_logged_no_call(self):
        pipe = FakeControllerPipeline()
        handler = make_fc_video_handler(pipe, ["local_full"])
        f = _frame(
            src=0,
            dst=0,
            family=protocol.FAMILY_FC_VIDEO,
            type=messages.FcVideoType.SET_LAYOUT,
            payload=b"\x05",  # id 5, only 1 layout configured
        )
        handler(messages.FcVideoType.SET_LAYOUT, f)
        self.assertEqual(pipe.layouts_set, [])

    def test_set_overlay_extracts_text(self):
        pipe = FakeControllerPipeline()
        handler = make_fc_video_handler(pipe, [])
        f = _frame(
            src=0,
            dst=0,
            family=protocol.FAMILY_FC_VIDEO,
            type=messages.FcVideoType.SET_OVERLAY,
            payload=b"KD3BBP / TEST\x00",
        )
        handler(messages.FcVideoType.SET_OVERLAY, f)
        self.assertEqual(pipe.overlays_set, ["KD3BBP / TEST"])

    def test_set_source_starts_new_sender_and_records_pipeline_source(self):
        pipe = FakeControllerPipeline()
        link = FakeLink()
        controller = Controller(
            links={"sender-c": link},
            sender_addrs=(protocol.ADDR_SENDER_C,),
        )
        switcher = SourceSwitcher(
            controller,
            pipe,
            (protocol.ADDR_SENDER_C,),
        )
        controller.health.observe(
            _frame(
                src=protocol.ADDR_SENDER_C,
                dst=protocol.ADDR_CONTROLLER,
                family=protocol.FAMILY_NETMGMT,
                type=protocol.NETMGMT_HEARTBEAT,
            ),
            now=1.0,
        )
        handler = make_fc_video_handler(pipe, ["a"], switcher)
        src_frame = _frame(
            src=0,
            dst=0,
            family=protocol.FAMILY_FC_VIDEO,
            type=messages.FcVideoType.SET_SOURCE,
            payload=b"\x00\x12",
        )
        handler(messages.FcVideoType.SET_SOURCE, src_frame)

        self.assertEqual(switcher.sources[0], protocol.ADDR_SENDER_C)
        self.assertEqual(pipe.sources_set, [(0, protocol.ADDR_SENDER_C)])
        self.assertEqual(len(link.sent), 1)
        self.assertEqual(link.sent[0].type, messages.VideoType.START_STREAM)

    def test_set_source_switches_between_remote_senders(self):
        pipe = FakeControllerPipeline()
        c_link = FakeLink()
        l1_link = FakeLink()
        controller = Controller(
            links={"sender-c": c_link, "sender-l1": l1_link},
            sender_addrs=(protocol.ADDR_SENDER_C, protocol.ADDR_SENDER_L1),
        )
        switcher = SourceSwitcher(
            controller,
            pipe,
            (protocol.ADDR_SENDER_C, protocol.ADDR_SENDER_L1),
        )
        for addr in (protocol.ADDR_SENDER_C, protocol.ADDR_SENDER_L1):
            controller.health.observe(
                _frame(
                    src=addr,
                    dst=protocol.ADDR_CONTROLLER,
                    family=protocol.FAMILY_NETMGMT,
                    type=protocol.NETMGMT_HEARTBEAT,
                ),
                now=1.0,
            )
        handler = make_fc_video_handler(pipe, [], switcher)

        handler(
            messages.FcVideoType.SET_SOURCE,
            _frame(
                src=0,
                dst=0,
                family=protocol.FAMILY_FC_VIDEO,
                type=messages.FcVideoType.SET_SOURCE,
                payload=messages.SetSource(1, protocol.ADDR_SENDER_C).encode(),
            ),
        )
        handler(
            messages.FcVideoType.SET_SOURCE,
            _frame(
                src=0,
                dst=0,
                family=protocol.FAMILY_FC_VIDEO,
                type=messages.FcVideoType.SET_SOURCE,
                payload=messages.SetSource(1, protocol.ADDR_SENDER_L1).encode(),
            ),
        )

        self.assertEqual([f.type for f in c_link.sent], [
            messages.VideoType.START_STREAM,
            messages.VideoType.STOP_STREAM,
        ])
        self.assertEqual([f.type for f in l1_link.sent], [
            messages.VideoType.START_STREAM,
        ])
        self.assertEqual(switcher.sources[1], protocol.ADDR_SENDER_L1)
        self.assertEqual(
            pipe.sources_set,
            [
                (1, protocol.ADDR_SENDER_C),
                (1, protocol.ADDR_SENDER_L1),
            ],
        )

    def test_set_source_rejects_unknown_source_and_bad_slot(self):
        pipe = FakeControllerPipeline()
        link = FakeLink()
        controller = Controller(
            links={"sender-c": link},
            sender_addrs=(protocol.ADDR_SENDER_C,),
        )
        switcher = SourceSwitcher(controller, pipe, (protocol.ADDR_SENDER_C,))
        handler = make_fc_video_handler(pipe, [], switcher)

        for payload in (
            messages.SetSource(0, protocol.ADDR_SENDER_L1).encode(),
            messages.SetSource(9, protocol.ADDR_SENDER_C).encode(),
        ):
            handler(
                messages.FcVideoType.SET_SOURCE,
                _frame(
                    src=0,
                    dst=0,
                    family=protocol.FAMILY_FC_VIDEO,
                    type=messages.FcVideoType.SET_SOURCE,
                    payload=payload,
                ),
            )

        self.assertEqual(link.sent, [])
        self.assertEqual(pipe.sources_set, [])
        self.assertEqual(
            switcher.sources,
            [protocol.ADDR_CONTROLLER, protocol.ADDR_UNASSIGNED],
        )

    def test_get_status_is_logged_no_pipeline_call(self):
        pipe = FakeControllerPipeline()
        handler = make_fc_video_handler(pipe, ["a"])
        status_frame = _frame(
            src=0,
            dst=0,
            family=protocol.FAMILY_FC_VIDEO,
            type=messages.FcVideoType.GET_STATUS,
        )
        handler(messages.FcVideoType.GET_STATUS, status_frame)
        self.assertEqual(pipe.layouts_set, [])
        self.assertEqual(pipe.overlays_set, [])
        self.assertEqual(pipe.sources_set, [])

    def test_pipeline_error_is_swallowed(self):
        class BoomPipeline:
            def set_layout(self, name: str) -> None:
                raise PipelineError("no GStreamer")

            def set_overlay(self, text: str) -> None: ...

        handler = make_fc_video_handler(BoomPipeline(), ["a"])
        f = _frame(
            src=0,
            dst=0,
            family=protocol.FAMILY_FC_VIDEO,
            type=messages.FcVideoType.SET_LAYOUT,
            payload=b"\x00",
        )
        handler(messages.FcVideoType.SET_LAYOUT, f)


if __name__ == "__main__":
    unittest.main()
