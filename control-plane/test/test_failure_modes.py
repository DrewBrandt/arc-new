"""Failure-mode regression tests, mirroring design doc Section 12.4.

Each scenario the doc lists is exercised end-to-end in-memory. Where the
implementation has consciously diverged from the doc, the test pins the
implementation's behaviour and the divergence is noted in the test
docstring so future edits don't silently re-align.
"""

from __future__ import annotations

import unittest

from arc import messages as m
from arc import protocol as p
from arc.controller import Controller
from arc.controller_main import SourceSwitcher


class _FakePipeline:
    """Captures the SourceSwitcher's pipeline drives without GStreamer."""

    def __init__(self) -> None:
        self.source_calls: list[tuple[int, int]] = []
        self.batched_calls: list[dict[int, int]] = []
        self.layouts: list[str] = []
        self.overlays: list[str] = []

    def set_source(self, slot_id: int, source_addr: int) -> None:
        self.source_calls.append((slot_id, source_addr))

    def set_sources(self, requested: dict[int, int]) -> None:
        # Mirror the real ControllerPipeline interface; SourceSwitcher
        # prefers this when it exists. Recording both forms catches a
        # future refactor that drops one of them.
        self.batched_calls.append(dict(requested))
        for slot_id, addr in requested.items():
            self.source_calls.append((slot_id, addr))

    def set_layout(self, name: str) -> None:
        self.layouts.append(name)

    def set_overlay(self, text: str) -> None:
        self.overlays.append(text)


def _heartbeat(src: int, session: int = 1, seq: int = 0) -> p.Frame:
    return p.Frame(
        src=src,
        dst=p.ADDR_CONTROLLER,
        flags=0,
        session=session,
        seq=seq,
        family=p.FAMILY_NETMGMT,
        type=p.NETMGMT_HEARTBEAT,
        payload=b"",
    )


class _FakeLink:
    def __init__(self) -> None:
        self.sent: list[p.Frame] = []

    def send(self, frame: p.Frame) -> None:
        self.sent.append(frame)


def _sender_route_name(addr: int) -> str:
    aliases = {
        p.ADDR_SENDER_N: "sender-n",
        p.ADDR_SENDER_C: "sender-c",
        p.ADDR_SENDER_L1: "sender-l1",
        p.ADDR_SENDER_L2: "sender-l2",
        p.ADDR_SENDER_GROUND: "sender-ground",
    }
    return aliases.get(addr, f"sender-0x{addr:02x}")


def _make_controller_with_switcher(
    sender_addrs: tuple[int, ...] = (p.ADDR_SENDER_C, p.ADDR_SENDER_L1, p.ADDR_SENDER_GROUND),
    initial_sources: tuple[int, int] = (p.ADDR_CONTROLLER, p.ADDR_SENDER_C),
    peer_timeout_s: float = 3.0,
):
    pipeline = _FakePipeline()
    links = {"uart-fc-n": _FakeLink()}
    for addr in sender_addrs:
        links[_sender_route_name(addr)] = _FakeLink()
    controller = Controller(
        links=links,
        sender_addrs=sender_addrs,
        peer_timeout_s=peer_timeout_s,
        heartbeat_interval_s=10.0,  # don't fire heartbeats during tests
    )
    switcher = SourceSwitcher(
        controller,
        pipeline,
        sender_addrs,
        initial_sources=initial_sources,
    )
    return controller, switcher, pipeline, links


class ActiveSenderDropFailureMode(unittest.TestCase):
    """Section 12.4: 'Active Sender drops wifi'.

    Design wording is "compositor slot 1 freezes on last frame", which
    describes the GStreamer-default behaviour if nothing changes the
    pipeline. The current SourceSwitcher implementation goes further:
    once PeerHealth marks the desired source offline, the slot's active
    source flips to EMPTY (black). This test pins the implementation; if
    we ever decide to honour the doc literally, this assertion has to
    flip too (and the doc and code want to be re-aligned).
    """

    def test_active_remote_offline_drives_slot_to_empty_other_slots_unaffected(self):
        controller, switcher, pipeline, _links = _make_controller_with_switcher()
        # Bring Sender-C and Sender-L1 online.
        controller.receive(_heartbeat(p.ADDR_SENDER_C, session=10), now=1.0)
        controller.receive(_heartbeat(p.ADDR_SENDER_L1, session=11), now=1.0)
        switcher.reconcile(now=1.0)

        # Slot 1 should now be showing Sender-C; slot 0 stays local.
        self.assertEqual(switcher.active_sources, [p.ADDR_CONTROLLER, p.ADDR_SENDER_C])
        self.assertIn(p.ADDR_SENDER_C, switcher._streaming_remotes)
        # Snapshot the pipeline call count so we can assert *only* the
        # offline-driven set_source happens after this point.
        prior_calls = list(pipeline.source_calls)

        # Sender-L1 keeps speaking right before the tick so it stays online;
        # Sender-C goes silent past peer_timeout_s and is the only drop.
        controller.receive(_heartbeat(p.ADDR_SENDER_L1, session=11, seq=1), now=9.5)
        offline = controller.tick(now=10.0)
        self.assertEqual(offline, [p.ADDR_SENDER_C])
        self.assertFalse(controller.health.is_online(p.ADDR_SENDER_C))
        self.assertFalse(controller.sender(p.ADDR_SENDER_C).online)

        # Reconcile sees the desired source offline -> slot 1 -> EMPTY.
        switcher.reconcile(now=10.0)
        self.assertEqual(switcher.active_sources[1], p.ADDR_UNASSIGNED)
        new_calls = pipeline.source_calls[len(prior_calls):]
        self.assertEqual(new_calls, [(1, p.ADDR_UNASSIGNED)])

        # Sender-L1 was not in any slot but stays online and untouched.
        self.assertTrue(controller.health.is_online(p.ADDR_SENDER_L1))
        # The desired source for slot 1 is still Sender-C; it'll come back
        # automatically once a heartbeat is observed again.
        self.assertEqual(switcher.sources[1], p.ADDR_SENDER_C)


class FcNUartSilentFailureMode(unittest.TestCase):
    """Section 12.4: 'FC-N UART goes silent'.

    Expect: Controller detects the timeout, holds the last layout, logs
    the fault. The pipeline must not be touched as a side effect of the
    timeout itself.
    """

    def test_fc_n_silence_marks_offline_without_pipeline_disruption(self):
        controller, switcher, pipeline, _links = _make_controller_with_switcher()
        # FC-N speaks, then a remote sender comes online too so we have
        # a non-trivial active state to defend.
        controller.receive(_heartbeat(p.ADDR_FC_N, session=5), now=0.5)
        controller.receive(_heartbeat(p.ADDR_SENDER_C, session=10), now=0.5)
        switcher.reconcile(now=0.5)

        # FC-N applies a layout via the pipeline directly (simulating an
        # earlier SET_LAYOUT) so we have something concrete to watch.
        pipeline.set_layout("split")
        pipeline.set_overlay("KD3BBP / BOOST")
        prior_layouts = list(pipeline.layouts)
        prior_overlays = list(pipeline.overlays)
        prior_sources = list(pipeline.source_calls)
        prior_active = list(switcher.active_sources)

        # FC-N goes silent past peer_timeout_s. Sender-C keeps speaking
        # so it stays online and is unaffected.
        controller.receive(_heartbeat(p.ADDR_SENDER_C, session=10, seq=1), now=2.0)
        offline = controller.tick(now=4.5)

        self.assertEqual(offline, [p.ADDR_FC_N])
        self.assertFalse(controller.health.is_online(p.ADDR_FC_N))
        self.assertTrue(controller.health.is_online(p.ADDR_SENDER_C))

        # Reconcile after the timeout must not touch the pipeline (FC-N
        # going offline doesn't change desired sources).
        switcher.reconcile(now=4.5)
        self.assertEqual(switcher.active_sources, prior_active)
        self.assertEqual(pipeline.layouts, prior_layouts)
        self.assertEqual(pipeline.overlays, prior_overlays)
        self.assertEqual(pipeline.source_calls, prior_sources)


class ControllerRebootInFlightFailureMode(unittest.TestCase):
    """Section 12.4: 'Controller reboots in flight'.

    Expected: Controller boots in the configured startup layout. If the
    desired remote source is not online yet, the PIP slot stays "black"
    (in implementation terms: EMPTY) until that Sender connects. Once
    the Sender does connect, the next reconcile drives the slot to it
    and issues a START_STREAM.
    """

    def test_initial_reconcile_with_remote_offline_keeps_slot_empty(self):
        controller, switcher, pipeline, _links = _make_controller_with_switcher()
        # Fresh controller: no peers heard from yet, including the desired
        # Sender-C in slot 1. The switcher's active_sources for slot 1 is
        # already EMPTY at construction, so reconcile() finds nothing to
        # change and emits no pipeline calls (the boot screen stays black
        # naturally instead of via an explicit set_source).
        switcher.reconcile(now=0.0)
        self.assertEqual(switcher.active_sources, [p.ADDR_CONTROLLER, p.ADDR_UNASSIGNED])
        self.assertEqual(pipeline.source_calls, [])
        # No START_STREAM has been issued because the Sender isn't reachable.
        self.assertNotIn(p.ADDR_SENDER_C, switcher._streaming_remotes)

    def test_remote_sender_connecting_after_boot_brings_slot_online(self):
        controller, switcher, pipeline, _links = _make_controller_with_switcher()
        switcher.reconcile(now=0.0)  # boot reconcile, slot 1 -> EMPTY
        prior_calls = list(pipeline.source_calls)

        # Sender-C boots later and starts heartbeating.
        controller.receive(_heartbeat(p.ADDR_SENDER_C, session=10), now=2.0)
        switcher.reconcile(now=2.0)

        self.assertEqual(switcher.active_sources[1], p.ADDR_SENDER_C)
        self.assertIn((1, p.ADDR_SENDER_C), pipeline.source_calls[len(prior_calls):])
        # And the Controller actually told the Sender to start streaming.
        self.assertIn(p.ADDR_SENDER_C, switcher._streaming_remotes)


class IdleSenderOfflineFailureMode(unittest.TestCase):
    """Section 12.4: 'Sender-GND loses wifi at launch' (the launch case).

    Sender-GND is online but not wired into any compositor slot. When it
    drops, only its own state changes; slots and other senders are
    untouched, and no pipeline call is generated as a side effect.
    """

    def test_idle_sender_offline_does_not_disturb_active_slots(self):
        controller, switcher, pipeline, _links = _make_controller_with_switcher()
        # Sender-C is online and active; Sender-GND is online but idle.
        controller.receive(_heartbeat(p.ADDR_SENDER_C, session=10), now=1.0)
        controller.receive(_heartbeat(p.ADDR_SENDER_GROUND, session=20), now=1.0)
        switcher.reconcile(now=1.0)
        prior_active = list(switcher.active_sources)
        prior_calls = list(pipeline.source_calls)

        # Sender-GND drops at "launch". Sender-C keeps heartbeating so
        # only Sender-GND should appear in the offline transitions.
        controller.receive(_heartbeat(p.ADDR_SENDER_C, session=10, seq=1), now=4.5)
        offline = controller.tick(now=5.0)
        self.assertEqual(offline, [p.ADDR_SENDER_GROUND])
        self.assertFalse(controller.health.is_online(p.ADDR_SENDER_GROUND))

        # Sender-C remains online and active in slot 1.
        self.assertTrue(controller.health.is_online(p.ADDR_SENDER_C))
        switcher.reconcile(now=5.0)
        self.assertEqual(switcher.active_sources, prior_active)
        # No new pipeline calls for the GND drop -- slot composition unchanged.
        self.assertEqual(pipeline.source_calls, prior_calls)


if __name__ == "__main__":
    unittest.main()
