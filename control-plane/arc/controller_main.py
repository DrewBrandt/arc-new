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
import contextlib
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
BENCH_CONTROL_HOST = "127.0.0.1"
BENCH_CONTROL_PORT = 6010


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
        keep_remote_streams: bool = False,
    ) -> None:
        self.controller = controller
        self.pipeline = pipeline
        self.sender_addrs = set(sender_addrs)
        self.keep_remote_streams = keep_remote_streams
        self._streaming_remotes: set[int] = set()
        defaults = [EMPTY_SOURCE] * slot_count
        if slot_count:
            defaults[0] = LOCAL_SOURCE
        if initial_sources is not None:
            for idx, source in enumerate(initial_sources[:slot_count]):
                defaults[idx] = source
        self.sources = defaults
        self.active_sources = [EMPTY_SOURCE] * slot_count
        if slot_count:
            self.active_sources[0] = LOCAL_SOURCE

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

        self.sources[slot_id] = source_addr
        self._reconcile_slot(slot_id, now=now)

    def set_sources(self, requested: dict[int, int], now: float = 0.0) -> None:
        for slot_id, source_addr in requested.items():
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

        for slot_id, source_addr in requested.items():
            self.sources[slot_id] = source_addr
        self._reconcile_slots(requested.keys(), now=now)

    def reconcile(self, now: float = 0.0) -> None:
        """Apply desired sources that are available on the control plane."""

        self._reconcile_slots(range(len(self.sources)), now=now)

    def _reconcile_slot(self, slot_id: int, now: float) -> None:
        self._reconcile_slots((slot_id,), now=now)

    def _reconcile_slots(self, slot_ids, now: float) -> None:
        if self.keep_remote_streams:
            self._sync_warm_remote_streams(now=now)
        pipeline_updates: dict[int, int] = {}
        for slot_id in slot_ids:
            next_active = self._next_active_for_slot(slot_id)
            active = self.active_sources[slot_id]
            if active == next_active:
                continue

            if self._is_remote_sender(active) and not self.keep_remote_streams:
                self._stop_sender_if_unused(active, changing_slot=slot_id, now=now)
            if self._is_remote_sender(next_active):
                self._ensure_sender_streaming(next_active, now=now)

            self.active_sources[slot_id] = next_active
            pipeline_updates[slot_id] = next_active

        if not pipeline_updates:
            return
        set_pipeline_sources = getattr(self.pipeline, "set_sources", None)
        if set_pipeline_sources is not None:
            set_pipeline_sources(pipeline_updates)
            return
        set_pipeline_source = getattr(self.pipeline, "set_source", None)
        if set_pipeline_source is not None:
            for slot_id, next_active in pipeline_updates.items():
                set_pipeline_source(slot_id, next_active)

    def _next_active_for_slot(self, slot_id: int) -> int:
        desired = self.sources[slot_id]
        next_active = desired
        if self._is_remote_sender(desired) and not self.controller.health.is_online(desired):
            next_active = EMPTY_SOURCE
        return next_active

    def _sync_warm_remote_streams(self, now: float) -> None:
        for addr in sorted(self.sender_addrs):
            if self.controller.health.is_online(addr):
                self._ensure_sender_streaming(addr, now=now)
            else:
                self._streaming_remotes.discard(addr)

    def _ensure_sender_streaming(self, addr: int, now: float) -> None:
        if addr in self._streaming_remotes:
            return
        self.controller.start_sender(addr, now=now)
        self._streaming_remotes.add(addr)

    def _stop_sender_if_unused(
        self,
        addr: int,
        *,
        changing_slot: int,
        now: float,
    ) -> None:
        for idx, active in enumerate(self.active_sources):
            if idx != changing_slot and active == addr:
                return
        self.controller.stop_sender(addr, now=now)
        self._streaming_remotes.discard(addr)

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


class BenchCommandServer:
    """Localhost-only bench controls for testing video without an FC wired in."""

    def __init__(
        self,
        pipeline: ControllerPipeline,
        source_switcher: SourceSwitcher,
        layout_names: list[str],
        sender_names: dict[str, int],
        *,
        host: str = BENCH_CONTROL_HOST,
        port: int = BENCH_CONTROL_PORT,
    ) -> None:
        self.pipeline = pipeline
        self.source_switcher = source_switcher
        self.layout_names = layout_names
        self.sender_names = {k.lower(): v for k, v in sender_names.items()}
        self.host = host
        self.port = port
        self._server: asyncio.base_events.Server | None = None
        self._cycle_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)

    async def stop(self) -> None:
        self._stop_cycle()
        if self._server is None:
            return
        self._server.close()
        with contextlib.suppress(OSError):
            await self._server.wait_closed()
        self._server = None

    def execute(self, line: str, now: float = 0.0) -> str:
        parts = line.strip().split()
        if not parts:
            return self._help()
        command = parts[0].lower()
        args = parts[1:]
        try:
            if command in ("help", "?"):
                return self._help()
            if command == "status":
                return self._status()
            if command == "layout":
                return self._layout(args)
            if command == "source":
                return self._source(args, now=now)
            if command == "cycle":
                return self._cycle(args)
            if command == "rotate":
                return self._rotate(args)
            if command in ("stop-cycle", "stop_cycle"):
                self._stop_cycle()
                return "OK cycle stopped"
        except ValueError as exc:
            return f"ERR {exc}"
        return f"ERR unknown command {command!r}\n{self._help()}"

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=30.0)
            response = self.execute(line.decode("utf-8", errors="replace"), now=_now())
            writer.write((response.rstrip() + "\n").encode("utf-8"))
            await writer.drain()
        except (asyncio.TimeoutError, OSError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    def _help(self) -> str:
        return (
            "commands: status | layout NAME_OR_INDEX | source SLOT SOURCE | "
            "cycle SLOT INTERVAL_SECONDS SOURCE... | "
            "rotate INTERVAL_SECONDS SOURCE... | stop-cycle\n"
            "sources: empty/off, local/controller, 0x12, sender-c, sender-l1, ..."
        )

    def _status(self) -> str:
        desired = ", ".join(
            f"slot{i}={self._format_source(src)}"
            for i, src in enumerate(self.source_switcher.sources)
        )
        active = ", ".join(
            f"slot{i}={self._format_source(src)}"
            for i, src in enumerate(self.source_switcher.active_sources)
        )
        online = [
            self._format_source(addr)
            for addr in sorted(self.source_switcher.sender_addrs)
            if self.source_switcher.controller.health.is_online(addr)
        ]
        return (
            f"OK desired: {desired}\n"
            f"active: {active}\n"
            f"online: {', '.join(online) if online else 'none'}"
        )

    def _layout(self, args: list[str]) -> str:
        if len(args) != 1:
            raise ValueError("usage: layout NAME_OR_INDEX")
        layout = self._resolve_layout(args[0])
        self.pipeline.set_layout(layout)
        return f"OK layout {layout}"

    def _source(self, args: list[str], now: float) -> str:
        if len(args) != 2:
            raise ValueError("usage: source SLOT SOURCE")
        slot = self._parse_slot(args[0])
        source = self._parse_source(args[1])
        self.source_switcher.set_source(slot, source, now=now)
        return f"OK source slot{slot} {self._format_source(source)}"

    def _cycle(self, args: list[str]) -> str:
        if len(args) < 3:
            raise ValueError("usage: cycle SLOT INTERVAL_SECONDS SOURCE...")
        slot = self._parse_slot(args[0])
        try:
            interval = float(args[1])
        except ValueError as exc:
            raise ValueError("interval must be a number of seconds") from exc
        if interval <= 0:
            raise ValueError("interval must be greater than 0")
        sources = [self._parse_source(arg) for arg in args[2:]]
        self._stop_cycle()
        self._cycle_task = asyncio.create_task(self._cycle_loop(slot, interval, sources))
        names = " ".join(self._format_source(source) for source in sources)
        return f"OK cycling slot{slot} every {interval:g}s: {names}"

    def _rotate(self, args: list[str]) -> str:
        if len(args) < 3:
            raise ValueError("usage: rotate INTERVAL_SECONDS SOURCE...")
        try:
            interval = float(args[0])
        except ValueError as exc:
            raise ValueError("interval must be a number of seconds") from exc
        if interval <= 0:
            raise ValueError("interval must be greater than 0")
        sources = [self._parse_source(arg) for arg in args[1:]]
        self._stop_cycle()
        self._cycle_task = asyncio.create_task(self._rotate_loop(interval, sources))
        names = " ".join(self._format_source(source) for source in sources)
        return f"OK rotating main/PIP every {interval:g}s: {names}"

    async def _cycle_loop(
        self,
        slot: int,
        interval: float,
        sources: list[int],
    ) -> None:
        while True:
            for source in sources:
                self.source_switcher.set_source(slot, source, now=_now())
                await asyncio.sleep(interval)

    async def _rotate_loop(self, interval: float, sources: list[int]) -> None:
        idx = 0
        while True:
            self._rotate_once(idx, sources, now=_now())
            idx += 1
            await asyncio.sleep(interval)

    def _rotate_once(self, idx: int, sources: list[int], now: float) -> None:
        main = sources[idx % len(sources)]
        pip = sources[(idx + 1) % len(sources)]
        self.source_switcher.set_sources({0: main, 1: pip}, now=now)

    def _stop_cycle(self) -> None:
        if self._cycle_task is None:
            return
        self._cycle_task.cancel()
        self._cycle_task = None

    def _resolve_layout(self, value: str) -> str:
        if value.isdigit():
            idx = int(value)
            if 0 <= idx < len(self.layout_names):
                return self.layout_names[idx]
            raise ValueError(f"layout index {idx} out of range")
        if value in self.layout_names:
            return value
        raise ValueError(
            f"unknown layout {value!r}; choices: {', '.join(self.layout_names)}"
        )

    def _parse_slot(self, value: str) -> int:
        try:
            slot = int(value, 0)
        except ValueError as exc:
            raise ValueError("slot must be an integer") from exc
        if not 0 <= slot < len(self.source_switcher.sources):
            raise ValueError(f"slot {slot} out of range")
        return slot

    def _parse_source(self, value: str) -> int:
        normalized = value.lower()
        if normalized in ("empty", "off", "none", "black", "0"):
            return EMPTY_SOURCE
        if normalized in ("local", "controller", "camera"):
            return LOCAL_SOURCE
        if normalized in self.sender_names:
            return self.sender_names[normalized]
        try:
            source = int(value, 0)
        except ValueError as exc:
            raise ValueError(f"unknown source {value!r}") from exc
        if not self.source_switcher._is_known_source(source):
            raise ValueError(f"unknown source 0x{source:02x}")
        return source

    def _format_source(self, source: int) -> str:
        if source == EMPTY_SOURCE:
            return "empty"
        if source == LOCAL_SOURCE:
            return "local"
        for name, addr in self.sender_names.items():
            if addr == source:
                return f"{name}(0x{source:02x})"
        return f"0x{source:02x}"


async def run(cfg: ControllerConfig, *, pipeline=None) -> None:
    if pipeline is None:
        pipeline = ControllerPipeline(
            cfg,
            sink=cfg.video.sink,
            mixer=cfg.video.mixer,
            startup_layout=cfg.video.startup_layout,
            switch_mode=cfg.video.switch_mode,
        )
    layout_names = list(cfg.layouts.keys())

    controller = Controller(
        sender_addrs=tuple(s.addr for s in cfg.senders),
        heartbeat_interval_s=cfg.heartbeat_interval_s,
        peer_timeout_s=cfg.peer_timeout_s,
    )
    source_switcher = SourceSwitcher(
        controller,
        pipeline,
        tuple(s.addr for s in cfg.senders),
        initial_sources=cfg.initial_sources,
        keep_remote_streams=cfg.video.switch_mode == "selector",
    )
    try:
        source_switcher.reconcile()
    except PipelineError:
        log.exception("video pipeline failed to apply initial sources")

    try:
        pipeline.start()
    except PipelineError:
        log.exception("video pipeline failed to start; running control-plane only")

    controller.fc_video_handler = make_fc_video_handler(
        pipeline,
        layout_names,
        source_switcher,
    )
    bench_server = BenchCommandServer(
        pipeline,
        source_switcher,
        layout_names,
        {s.name: s.addr for s in cfg.senders},
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
    try:
        await bench_server.start()
        log.info(
            "Bench control listening on %s:%d",
            bench_server.host,
            bench_server.port,
        )
    except OSError:
        log.exception("bench control server failed to start")

    stop_event = asyncio.Event()

    def _on_stop_signal() -> None:
        log.info("stop signal received")
        stop_event.set()

    _install_signal_handlers(_on_stop_signal)

    def _tick(now: float) -> None:
        controller.tick(now)
        try:
            source_switcher.reconcile(now=now)
        except PipelineError:
            log.exception("video pipeline failed while reconciling sources")

    tasks = [
        asyncio.create_task(
            run_uart_link(fc_uart, cfg.uart.device, cfg.uart.baud, stop_event=stop_event)
        ),
        asyncio.create_task(
            run_tick_loop(_tick, interval_s=0.05, stop_event=stop_event)
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
        await bench_server.stop()


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
