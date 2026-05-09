"""asyncio entrypoint for the ARC Controller process.

Wires together:
- A ``Controller`` instance built from a TOML config.
- A UART link to FC-N.
- A TCP server accepting connections from each Sender; the source IP
  identifies which Sender the connection belongs to.
- A periodic tick driving reliability retries, heartbeat emission, and
  peer-liveness checks.
- A ``ControllerPipeline`` driven by FC_VIDEO commands. SET_LAYOUT
  resolves the 1-byte layout id to a name by config insertion order.
  SET_OVERLAY updates the textoverlay text. SET_SOURCE drives the
  Sender stop/start handshake for a compositor slot. GET_STATUS is
  recognised but its reply is not yet implemented -- it is logged at
  INFO and otherwise ignored.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from arc import messages, protocol
from arc.config import ControllerConfig, load_controller_config
from arc.controller import Controller
from arc.pipeline_controller import ControllerPipeline, PipelineError
from arc.runtime import (
    QueuedTcpLink,
    QueuedUartLink,
    TcpServer,
    run_tick_loop,
    run_uart_link,
)


log = logging.getLogger("arc.controller")

EMPTY_SOURCE = protocol.ADDR_UNASSIGNED
LOCAL_SOURCE = protocol.ADDR_CONTROLLER
SOURCE_SLOT_COUNT = 2


class SourceSwitcher:
    """Controller-side state and Sender handshake for FC_VIDEO SET_SOURCE.

    Source IDs are carried in the existing 1-byte ``sender_addr`` field:
    ``0x00`` means empty, ``0x10`` means Controller local camera, and any
    configured Sender address means a remote source.
    """

    def __init__(
        self,
        controller: Controller,
        pipeline: ControllerPipeline,
        sender_addrs: tuple[int, ...],
        *,
        slot_count: int = SOURCE_SLOT_COUNT,
        initial_sources: tuple[int, ...] | None = None,
    ) -> None:
        self.controller = controller
        self.pipeline = pipeline
        self.sender_addrs = set(sender_addrs)
        defaults = [EMPTY_SOURCE] * slot_count
        if slot_count:
            defaults[0] = LOCAL_SOURCE
        if initial_sources is not None:
            for idx, source in enumerate(initial_sources[:slot_count]):
                defaults[idx] = source
        self.sources = defaults

    def set_source(self, slot_id: int, source_addr: int, now: float = 0.0) -> None:
        if not 0 <= slot_id < len(self.sources):
            log.warning(
                "SET_SOURCE slot=%d out of range (0..%d)",
                slot_id,
                len(self.sources) - 1,
            )
            return

        if not self._is_known_source(source_addr):
            log.warning("SET_SOURCE unknown source 0x%02x", source_addr)
            return

        previous = self.sources[slot_id]
        if previous == source_addr:
            return

        if self._is_remote_sender(previous):
            self.controller.stop_sender(previous, now=now)
        if self._is_remote_sender(source_addr):
            self.controller.start_sender(source_addr, now=now)

        self.sources[slot_id] = source_addr
        set_pipeline_source = getattr(self.pipeline, "set_source", None)
        if set_pipeline_source is not None:
            set_pipeline_source(slot_id, source_addr)

    def _is_known_source(self, source_addr: int) -> bool:
        return (
            source_addr in (EMPTY_SOURCE, LOCAL_SOURCE)
            or source_addr in self.sender_addrs
        )

    def _is_remote_sender(self, source_addr: int) -> bool:
        return source_addr in self.sender_addrs


def make_fc_video_handler(
    pipeline: ControllerPipeline,
    layout_names: list[str],
    source_switcher: SourceSwitcher | None = None,
):
    """Adapter: FC_VIDEO commands -> ControllerPipeline mutations.

    Pipeline failures (e.g., GStreamer not installed on a dev machine)
    are logged but never propagate.
    """

    def on_fc_video(
        fc_video_type: messages.FcVideoType, frame: protocol.Frame
    ) -> None:
        try:
            if fc_video_type is messages.FcVideoType.SET_LAYOUT:
                layout_id = messages.SetLayout.decode(frame.payload).layout_id
                if 0 <= layout_id < len(layout_names):
                    pipeline.set_layout(layout_names[layout_id])
                else:
                    log.warning(
                        "SET_LAYOUT id=%d out of range (0..%d)",
                        layout_id,
                        len(layout_names) - 1 if layout_names else -1,
                    )
            elif fc_video_type is messages.FcVideoType.SET_OVERLAY:
                text = messages.SetOverlay.decode(frame.payload).text
                pipeline.set_overlay(text)
            elif fc_video_type is messages.FcVideoType.SET_SOURCE:
                msg = messages.SetSource.decode(frame.payload)
                if source_switcher is None:
                    log.info(
                        "SET_SOURCE slot=%d source=0x%02x received with no source switcher",
                        msg.slot_id,
                        msg.sender_addr,
                    )
                else:
                    source_switcher.set_source(
                        msg.slot_id,
                        msg.sender_addr,
                    )
            elif fc_video_type is messages.FcVideoType.GET_STATUS:
                log.info("GET_STATUS request received (status reply not yet implemented)")
        except PipelineError:
            log.exception(
                "pipeline failed to apply FC_VIDEO command %s",
                fc_video_type.name,
            )

    return on_fc_video


async def run(cfg: ControllerConfig, *, pipeline=None) -> None:
    if pipeline is None:
        pipeline = ControllerPipeline(
            cfg,
            sink=cfg.video.sink,
            mixer=cfg.video.mixer,
        )
    layout_names = list(cfg.layouts.keys())
    for slot_id, source_addr in enumerate(cfg.initial_sources):
        try:
            pipeline.set_source(slot_id, source_addr)
        except PipelineError:
            log.exception(
                "video pipeline failed to set initial source slot=%d source=0x%02x",
                slot_id,
                source_addr,
            )

    try:
        pipeline.start()
    except PipelineError:
        log.exception("video pipeline failed to start; running control-plane only")

    controller = Controller(
        sender_addrs=tuple(s.addr for s in cfg.senders),
        heartbeat_interval_s=cfg.heartbeat_interval_s,
        peer_timeout_s=cfg.peer_timeout_s,
    )
    controller.fc_video_handler = make_fc_video_handler(
        pipeline,
        layout_names,
        SourceSwitcher(
            controller,
            pipeline,
            tuple(s.addr for s in cfg.senders),
            initial_sources=cfg.initial_sources,
        ),
    )

    # Build links: UART for FC-N, one TCP link per Sender keyed by route name.
    fc_uart = QueuedUartLink(lambda f: controller.receive(f, _now()))
    tcp_links_by_route = {
        _sender_route_name(addr): QueuedTcpLink(
            lambda f, _addr=addr: controller.receive(f, _now())
        )
        for addr in (s.addr for s in cfg.senders)
    }
    tcp_links_by_addr = {
        s.addr: tcp_links_by_route[_sender_route_name(s.addr)] for s in cfg.senders
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
        link_for_frame=lambda f: tcp_links_by_addr.get(f.src),
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
        try:
            pipeline.stop()
        except PipelineError:
            log.exception("pipeline stop on shutdown failed")
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
