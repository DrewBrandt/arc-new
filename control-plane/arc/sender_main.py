"""asyncio entrypoint for an ARC Sender process.

Wires together:
- A ``Sender`` instance built from a TOML config.
- A TCP client that maintains a connection to the Controller and
  reconnects on drop.
- An optional UART link to a paired flight computer.
- A periodic tick driving reliability retries, heartbeat emission, and
  peer-liveness checks.
- A periodic STATUS_REPORT emission to the Controller.
- A ``SenderPipeline`` driven by VIDEO commands. Pipeline lifecycle is
  best-effort: if GStreamer is unavailable (dev machine) the control
  plane still runs and pipeline calls are logged-and-swallowed.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from arc import messages, protocol
from arc.config import SenderConfig, load_sender_config
from arc.pipeline_sender import PipelineError, SenderPipeline
from arc.runtime import (
    QueuedTcpLink,
    QueuedUartLink,
    run_tick_loop,
    run_uart_link,
)
from arc.sender import Sender


log = logging.getLogger("arc.sender")

STATUS_REPORT_INTERVAL_S = 1.0


def make_video_command_handler(pipeline: SenderPipeline):
    """Adapter: VIDEO commands -> SenderPipeline state transitions.

    Pipeline failures (e.g., GStreamer not installed on a dev machine)
    are logged but never propagate; the control plane keeps running.
    """

    def on_video_command(
        video_type: messages.VideoType, frame: protocol.Frame
    ) -> None:
        try:
            if video_type is messages.VideoType.START_STREAM:
                pipeline.start_stream()
            elif video_type is messages.VideoType.STOP_STREAM:
                pipeline.stop_stream()
            elif video_type is messages.VideoType.HARD_STOP:
                pipeline.hard_stop()
            elif video_type is messages.VideoType.SET_BITRATE:
                bitrate = messages.SetBitrate.decode(frame.payload).bitrate_bps
                pipeline.set_bitrate(bitrate)
        except PipelineError:
            log.exception(
                "pipeline failed to apply VIDEO command %s", video_type.name
            )

    return on_video_command


async def run(
    cfg: SenderConfig,
    status_provider=None,
    *,
    pipeline=None,
    stop_event: asyncio.Event | None = None,
    ready_event: asyncio.Event | None = None,
) -> None:
    """Run the Sender process.

    ``stop_event`` lets a test (or any caller owning its own lifecycle)
    request shutdown without signals. ``ready_event`` is set once the
    Sender has finished its setup-time work and the asyncio tasks are
    spawned, so tests can wait deterministically.
    """

    if pipeline is None:
        pipeline = SenderPipeline(cfg)
    video_handler = make_video_command_handler(pipeline)
    sender = Sender(
        addr=cfg.addr,
        paired_fc=cfg.paired_fc,
        controller_addr=cfg.controller_addr,
        heartbeat_interval_s=cfg.heartbeat_interval_s,
        peer_timeout_s=cfg.peer_timeout_s,
        video_command_handler=video_handler,
    )

    controller_link = QueuedTcpLink(lambda f: sender.receive(f, _now()))
    links = {"controller": controller_link}

    fc_link: QueuedUartLink | None = None
    if cfg.paired_fc is not None and cfg.uart is not None:
        fc_link = QueuedUartLink(lambda f: sender.receive(f, _now()))
        links["uart-fc"] = fc_link

    sender.set_links(links)
    if cfg.video.start_stream_on_boot:
        log.info("Sender %s starting video stream on boot", cfg.name)
        _apply_boot_video_command(
            sender,
            messages.VideoType.START_STREAM,
            now=_now(),
        )

    owns_stop_event = stop_event is None
    if stop_event is None:
        stop_event = asyncio.Event()

    if owns_stop_event:
        def _on_stop_signal() -> None:
            log.info("stop signal received")
            stop_event.set()

        _install_signal_handlers(_on_stop_signal)

    tasks = [
        asyncio.create_task(
            controller_link.run_client(cfg.controller_ip, cfg.controller_port)
        ),
        asyncio.create_task(
            run_tick_loop(sender.tick, interval_s=0.05, stop_event=stop_event)
        ),
        asyncio.create_task(
            _status_loop(sender, status_provider, stop_event)
        ),
    ]
    if fc_link is not None and cfg.uart is not None:
        tasks.append(
            asyncio.create_task(
                run_uart_link(
                    fc_link, cfg.uart.device, cfg.uart.baud, stop_event=stop_event
                )
            )
        )

    log.info("Sender %s started, target controller=%s:%d",
             cfg.name, cfg.controller_ip, cfg.controller_port)

    if ready_event is not None:
        ready_event.set()

    try:
        await stop_event.wait()
    finally:
        try:
            pipeline.hard_stop()
        except PipelineError:
            log.exception("pipeline hard_stop on shutdown failed")
        await controller_link.stop()
        if fc_link is not None:
            await fc_link.stop()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


async def _status_loop(
    sender: Sender,
    status_provider,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        try:
            report = (status_provider or _default_status_provider)(sender)
            sender.report_status(report, now=_now())
        except Exception:
            log.exception("status report failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=STATUS_REPORT_INTERVAL_S)
        except asyncio.TimeoutError:
            continue


def _default_status_provider(sender: Sender) -> messages.StatusReport:
    state = 0
    if sender.transmitting:
        state |= 0x01
    if sender.recording:
        state |= 0x02
    return messages.StatusReport(
        state=state,
        cpu_temp_c=0,
        cpu_load_pct=0,
        free_disk_mb=0,
        rssi_dbm=0,
        tx_frames=0,
        dropped_frames=0,
    )


def _apply_boot_video_command(
    sender: Sender,
    video_type: messages.VideoType,
    now: float = 0.0,
) -> None:
    sender.receive(
        protocol.Frame(
            src=sender.controller_addr,
            dst=sender.addr,
            flags=0,
            session=0,
            seq=0,
            family=protocol.FAMILY_VIDEO,
            type=video_type,
            payload=b"",
        ),
        now=now,
    )


def _now() -> float:
    return asyncio.get_running_loop().time()


def _install_signal_handlers(callback) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, callback)
        except (NotImplementedError, RuntimeError):
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="ARC Sender process")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/etc/arc/sender.toml"),
        help="Path to sender TOML config",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level.upper())

    cfg = load_sender_config(args.config)
    asyncio.run(run(cfg))


if __name__ == "__main__":
    main()
