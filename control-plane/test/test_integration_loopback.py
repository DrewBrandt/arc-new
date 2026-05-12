"""Process-level integration test: real Controller + Sender over loopback TCP.

Unit tests cover the layers individually; this test wires up the actual
``controller_main.run()`` and ``sender_main.run()`` entry points and
exercises them end-to-end through real asyncio TCP. GStreamer is stubbed
out with fake pipelines so no system dependency is needed.

The flow exercised here:

1. Both processes start; the Sender's TCP client connects to the
   Controller's TCP server.
2. The Sender's periodic heartbeat reaches the Controller, which marks
   it ``ONLINE`` in PeerHealth.
3. A bench-server client (over a second loopback TCP socket) issues
   ``source 1 sender-c``.
4. The Controller's SourceSwitcher resolves the slot to Sender-C, drives
   the (fake) Controller pipeline, and issues a VIDEO START_STREAM frame
   addressed to the Sender.
5. The frame traverses the same TCP link in reverse and lands at the
   Sender's pipeline as ``start_stream()``.

If any of those links is broken the test fails; this catches a class of
asyncio/wiring bugs that pure unit tests (which use direct in-memory
links and skip the whole event loop) can't.
"""

from __future__ import annotations

import asyncio
import socket
import unittest

from arc import protocol
from arc.config import (
    ControllerConfig,
    ControllerVideoConfig,
    SenderConfig,
    SenderEntry,
    UartConfig,
    VideoConfig,
)
from arc.controller_main import run as run_controller
from arc.sender_main import run as run_sender


def _free_port() -> int:
    """Pick a TCP port the OS isn't using right now.

    There's a tiny race between us closing this socket and the server
    binding the same port; in practice the integration test runs
    serially against a clean dev machine and this is fine.
    """

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FakeControllerPipeline:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.layouts: list[str] = []
        self.overlays: list[str] = []
        self.source_calls: list[tuple[int, int]] = []

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def set_layout(self, name: str) -> None:
        self.layouts.append(name)

    def set_overlay(self, text: str) -> None:
        self.overlays.append(text)

    def set_sources(self, requested: dict[int, int]) -> None:
        for slot_id, addr in requested.items():
            self.source_calls.append((slot_id, addr))

    def set_source(self, slot_id: int, source_addr: int) -> None:
        # Provided so a future SourceSwitcher refactor that drops the
        # batched form still drives this fake. The current switcher
        # prefers set_sources when both exist.
        self.source_calls.append((slot_id, source_addr))


class _FakeSenderPipeline:
    def __init__(self) -> None:
        self.started_count = 0
        self.stopped_count = 0
        self.hard_stops = 0
        self.bitrates: list[int] = []
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1

    def start_stream(self) -> None:
        self.started_count += 1

    def stop_stream(self) -> None:
        self.stopped_count += 1

    def hard_stop(self) -> None:
        self.hard_stops += 1

    def set_bitrate(self, bitrate_bps: int) -> None:
        self.bitrates.append(bitrate_bps)


async def _bench_command(host: str, port: int, line: str, timeout: float = 1.0) -> str:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=timeout
    )
    try:
        writer.write((line + "\n").encode("utf-8"))
        await writer.drain()
        data = await asyncio.wait_for(reader.read(4096), timeout=timeout)
        return data.decode("utf-8", errors="replace")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def _wait_until(
    predicate, timeout: float = 2.0, interval: float = 0.02
) -> None:
    """Spin on ``predicate`` until it returns truthy, raising on timeout."""

    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        if predicate():
            return
        if loop.time() >= deadline:
            raise AssertionError(f"predicate did not become true within {timeout}s")
        await asyncio.sleep(interval)


def _build_controller_cfg(*, listen_port: int, sender_ip: str) -> ControllerConfig:
    return ControllerConfig(
        addr=protocol.ADDR_CONTROLLER,
        callsign="KD3BBP",
        uart=UartConfig(device="/dev/null/fake-uart", baud=115200),
        listen_port=listen_port,
        senders=(
            SenderEntry(
                addr=protocol.ADDR_SENDER_C,
                name="sender-c",
                ip=sender_ip,
                paired_fc=None,
            ),
        ),
        heartbeat_interval_s=0.05,
        peer_timeout_s=0.5,
        layouts={
            "split": {
                "slot_0": {"xpos": 0, "ypos": 0, "width": 640, "height": 480, "alpha": 1.0},
                "slot_1": {"xpos": 640, "ypos": 0, "width": 640, "height": 480, "alpha": 1.0},
            },
        },
        initial_sources=(protocol.ADDR_CONTROLLER, protocol.ADDR_UNASSIGNED),
        video=ControllerVideoConfig(
            mixer="compositor",
            sink="fakesink",
            startup_layout=None,
            switch_mode="rebuild",
            warm_remote_streams=False,
        ),
    )


def _build_sender_cfg(*, controller_ip: str, controller_port: int) -> SenderConfig:
    return SenderConfig(
        addr=protocol.ADDR_SENDER_C,
        name="sender-c",
        paired_fc=None,
        controller_ip=controller_ip,
        controller_port=controller_port,
        controller_addr=protocol.ADDR_CONTROLLER,
        video=VideoConfig(
            width=640,
            height=480,
            framerate=30,
            bitrate_bps=1_200_000,
            encoder="fakeenc",
            recording_path="/tmp/arc-fake-recordings",
            start_stream_on_boot=False,
        ),
        uart=None,
        heartbeat_interval_s=0.05,
        peer_timeout_s=0.5,
    )


class LoopbackIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_bench_source_command_reaches_sender_pipeline(self):
        controller_port = _free_port()
        bench_port = _free_port()

        controller_pipeline = _FakeControllerPipeline()
        sender_pipeline = _FakeSenderPipeline()

        controller_cfg = _build_controller_cfg(
            listen_port=controller_port, sender_ip="127.0.0.1"
        )
        sender_cfg = _build_sender_cfg(
            controller_ip="127.0.0.1", controller_port=controller_port
        )

        stop_event = asyncio.Event()
        controller_ready = asyncio.Event()
        sender_ready = asyncio.Event()

        controller_task = asyncio.create_task(
            run_controller(
                controller_cfg,
                pipeline=controller_pipeline,
                stop_event=stop_event,
                ready_event=controller_ready,
                bench_host="127.0.0.1",
                bench_port=bench_port,
            )
        )
        sender_task = asyncio.create_task(
            run_sender(
                sender_cfg,
                pipeline=sender_pipeline,
                stop_event=stop_event,
                ready_event=sender_ready,
            )
        )

        try:
            await asyncio.wait_for(controller_ready.wait(), timeout=2.0)
            await asyncio.wait_for(sender_ready.wait(), timeout=2.0)

            # The Controller pipeline must have been started during run().
            self.assertTrue(controller_pipeline.started)

            # Heartbeats need to round-trip before the Controller will
            # treat sender-c as online. Probe via the bench `status`
            # command; retry until "sender-c" shows up in the online list.
            async def sender_visible_online() -> bool:
                reply = await _bench_command("127.0.0.1", bench_port, "status")
                return "online" in reply and "sender-c" in reply

            async def poll_online() -> None:
                while not await sender_visible_online():
                    await asyncio.sleep(0.05)

            await asyncio.wait_for(poll_online(), timeout=3.0)

            # Drive a SET_SOURCE through the bench server, then watch
            # both pipelines react.
            reply = await _bench_command(
                "127.0.0.1", bench_port, "source 1 sender-c"
            )
            self.assertIn("OK", reply)

            await _wait_until(
                lambda: any(
                    call == (1, protocol.ADDR_SENDER_C)
                    for call in controller_pipeline.source_calls
                ),
                timeout=2.0,
            )
            await _wait_until(
                lambda: sender_pipeline.started_count >= 1,
                timeout=2.0,
            )
        finally:
            stop_event.set()
            for task in (controller_task, sender_task):
                try:
                    await asyncio.wait_for(task, timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass


if __name__ == "__main__":
    unittest.main()
