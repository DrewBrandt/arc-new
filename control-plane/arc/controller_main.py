"""asyncio entrypoint for the ARC Controller process.

Wires together:
- A ``Controller`` instance built from a TOML config.
- A UART link to FC-N.
- A TCP server accepting connections from each Sender; the source IP
  identifies which Sender the connection belongs to.
- A periodic tick driving reliability retries, heartbeat emission, and
  peer-liveness checks.

GStreamer integration is intentionally out of scope here -- this is the
control-plane process. Video pipelines belong in a separate module that
this entrypoint can later import and wire to ``Controller.start_sender``
and ``Controller.set_sender_bitrate``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from arc import protocol
from arc.config import ControllerConfig, load_controller_config
from arc.controller import Controller
from arc.runtime import (
    QueuedTcpLink,
    QueuedUartLink,
    TcpServer,
    run_tick_loop,
    run_uart_link,
)


log = logging.getLogger("arc.controller")


async def run(cfg: ControllerConfig) -> None:
    controller = Controller(
        sender_addrs=tuple(s.addr for s in cfg.senders),
        heartbeat_interval_s=cfg.heartbeat_interval_s,
        peer_timeout_s=cfg.peer_timeout_s,
    )

    # Build links: UART for FC-N, one TCP link per Sender keyed by route name.
    fc_uart = QueuedUartLink(lambda f: controller.receive(f, _now()))
    tcp_links_by_route = {
        _sender_route_name(addr): QueuedTcpLink(
            lambda f, _addr=addr: controller.receive(f, _now())
        )
        for addr in (s.addr for s in cfg.senders)
    }
    controller.set_links({"uart-fc-n": fc_uart, **tcp_links_by_route})

    # Map Sender source IPs to their TCP links so the TCP server can attach.
    ip_to_link = {
        s.ip: tcp_links_by_route[_sender_route_name(s.addr)] for s in cfg.senders
    }

    server = TcpServer(
        host="0.0.0.0",
        port=cfg.listen_port,
        link_for_peer=ip_to_link.get,
    )
    await server.start()
    log.info("Controller listening on :%d", cfg.listen_port)

    stop_event = asyncio.Event()

    def _on_stop_signal() -> None:
        log.info("stop signal received")
        stop_event.set()

    _install_signal_handlers(_on_stop_signal)

    tasks = [
        asyncio.create_task(
            run_uart_link(fc_uart, cfg.uart.device, cfg.uart.baud, stop_event=stop_event)
        ),
        asyncio.create_task(
            run_tick_loop(controller.tick, interval_s=0.05, stop_event=stop_event)
        ),
    ]

    try:
        await stop_event.wait()
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        for link in (fc_uart, *tcp_links_by_route.values()):
            await link.stop()
        await server.stop()


def _sender_route_name(addr: int) -> str:
    aliases = {
        protocol.ADDR_SENDER_N: "sender-n",
        protocol.ADDR_SENDER_C: "sender-c",
        protocol.ADDR_SENDER_L1: "sender-l1",
        protocol.ADDR_SENDER_L2: "sender-l2",
        protocol.ADDR_SENDER_GROUND: "sender-ground",
    }
    return aliases.get(addr, f"sender-0x{addr:02x}")


def _now() -> float:
    return asyncio.get_running_loop().time()


def _install_signal_handlers(callback) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, callback)
        except (NotImplementedError, RuntimeError):
            # Windows doesn't support add_signal_handler for SIGTERM;
            # KeyboardInterrupt still terminates the process.
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="ARC Controller process")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/etc/arc/controller.toml"),
        help="Path to controller TOML config",
    )
    parser.add_argument(
        "--log-level", default="INFO", help="Python logging level"
    )
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level.upper())

    cfg = load_controller_config(args.config)
    asyncio.run(run(cfg))


if __name__ == "__main__":
    main()
