"""asyncio entrypoint for an ARC Sender process.

Wires together:
- A ``Sender`` instance built from a TOML config.
- A TCP client that maintains a connection to the Controller and
  reconnects on drop.
- An optional UART link to a paired flight computer.
- A periodic tick driving reliability retries, heartbeat emission, and
  peer-liveness checks.
- A periodic STATUS_REPORT emission to the Controller.

GStreamer pipeline construction is out of scope here; the Sender state
exposed by ``sender.transmitting``/``sender.recording`` is what a video
module reads to decide when to start/stop encoding.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from arc import messages
from arc.config import SenderConfig, load_sender_config
from arc.runtime import (
    QueuedTcpLink,
    QueuedUartLink,
    run_tick_loop,
    run_uart_link,
)
from arc.sender import Sender


log = logging.getLogger("arc.sender")

STATUS_REPORT_INTERVAL_S = 1.0


async def run(cfg: SenderConfig, status_provider=None) -> None:
    sender = Sender(
        addr=cfg.addr,
        paired_fc=cfg.paired_fc,
        controller_addr=cfg.controller_addr,
        heartbeat_interval_s=cfg.heartbeat_interval_s,
        peer_timeout_s=cfg.peer_timeout_s,
    )

    controller_link = QueuedTcpLink(lambda f: sender.receive(f, _now()))
    links = {"controller": controller_link}

    fc_link: QueuedUartLink | None = None
    if cfg.paired_fc is not None and cfg.uart is not None:
        fc_link = QueuedUartLink(lambda f: sender.receive(f, _now()))
        links["uart-fc"] = fc_link

    sender.set_links(links)

    stop_event = asyncio.Event()

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

    try:
        await stop_event.wait()
    finally:
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
