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
  Sender stop/start handshake for a compositor slot via the
  ``SourceSwitcher``. GET_STATUS produces a STATUS_REPORT reply.

The compositor-slot reconciliation logic lives in
``arc.source_switcher``; the bench REPL lives in ``arc.bench_server``.
They are re-exported here so the test suite (and any external code that
imported them when this module was monolithic) keeps working.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from arc_protocol import messages, protocol
from arc_protocol.router import controller_routes
from arc.addressing import is_sender_addr
from arc.bench_server import (
    BENCH_CONTROL_HOST,
    BENCH_CONTROL_PORT,
    BenchCommandServer,
)
from arc.config import ControllerConfig, UartConfig, load_controller_config
from arc.controller import Controller
from arc.fc_video_status import ControllerVideoStatus, SenderVideoSnapshot
from arc.pipeline_controller import ControllerPipeline, PipelineError
from arc.runtime import (
    QueuedTcpLink,
    QueuedUartLink,
    TcpServer,
    now as _now,
    run_tick_loop,
    run_uart_link,
)
from arc.source_switcher import (
    EMPTY_SOURCE,
    LOCAL_SOURCE,
    SOURCE_SLOT_COUNT,
    SourceSwitcher,
)


log = logging.getLogger("arc.controller")
TELEMETRY_INTERVAL_S = 5.0


__all__ = [
    "BENCH_CONTROL_HOST",
    "BENCH_CONTROL_PORT",
    "BenchCommandServer",
    "EMPTY_SOURCE",
    "LOCAL_SOURCE",
    "SOURCE_SLOT_COUNT",
    "SourceSwitcher",
    "build_fc_video_status_report",
    "make_fc_video_handler",
    "main",
    "run",
]


@dataclass
class _TelemetrySample:
    now: float
    process_time_s: float
    rss_kb: int | None
    temp_c: float | None
    load1: float | None


class _ControllerTelemetry:
    """Periodic low-overhead diagnostics for the running Controller daemon."""

    def __init__(
        self,
        controller: Controller,
        source_switcher: SourceSwitcher,
        serial_links_by_route: dict[str, QueuedUartLink],
        tcp_links_by_route: dict[str, QueuedTcpLink],
        *,
        interval_s: float = TELEMETRY_INTERVAL_S,
    ) -> None:
        self.controller = controller
        self.source_switcher = source_switcher
        self.serial_links_by_route = serial_links_by_route
        self.tcp_links_by_route = tcp_links_by_route
        self.interval_s = interval_s
        self._next_report_at: float | None = None
        self._last_tick_at: float | None = None
        self._max_tick_gap_s = 0.0
        self._last_sample: _TelemetrySample | None = None
        self._last_gst_buffers: dict[str, int] = {}

    def observe_tick(self, now: float) -> None:
        if self._last_tick_at is not None:
            self._max_tick_gap_s = max(self._max_tick_gap_s, now - self._last_tick_at)
        self._last_tick_at = now

    def maybe_log(self, now: float) -> None:
        if self._next_report_at is None:
            self._next_report_at = now + self.interval_s
            self._last_sample = self._sample(now)
            return
        if now < self._next_report_at:
            return

        sample = self._sample(now)
        cpu_pct = self._cpu_pct(sample)
        rss = f"{sample.rss_kb}kB" if sample.rss_kb is not None else "n/a"
        temp = f"{sample.temp_c:.1f}C" if sample.temp_c is not None else "n/a"
        load1 = f"{sample.load1:.2f}" if sample.load1 is not None else "n/a"
        tcp = " ".join(
            f"{name}:online={int(link.online)},q={link.queue.qsize()},drop={link.dropped}"
            for name, link in sorted(self.tcp_links_by_route.items())
        )
        serial = " ".join(
            f"{name}:online={int(link.online)},q={link.queue.qsize()},"
            f"drop={link.dropped},bad={link.bad_frames},disc={link.disconnects},"
            f"reason={link.last_disconnect_reason or '-'}"
            for name, link in sorted(self.serial_links_by_route.items())
        )
        gst = self._gst_summary(sample)
        log.info(
            "telemetry cpu=%.1f%% rss=%s temp=%s load1=%s tick_gap=%.1fms "
            "pending=%d inbox=%d failed=%d unhandled=%d "
            "desired=%s active=%s serial=[%s] tcp=[%s] gst=[%s]",
            cpu_pct,
            rss,
            temp,
            load1,
            self._max_tick_gap_s * 1000.0,
            self.controller.node.reliable.pending_count,
            len(self.controller.node.inbox),
            len(self.controller.node.failed),
            len(self.controller.unhandled_frames),
            _format_sources(self.source_switcher.sources),
            _format_sources(self.source_switcher.active_sources),
            serial,
            tcp,
            gst,
        )
        self._last_sample = sample
        self._max_tick_gap_s = 0.0
        self._next_report_at = now + self.interval_s

    def _sample(self, now: float) -> _TelemetrySample:
        return _TelemetrySample(
            now=now,
            process_time_s=time.process_time(),
            rss_kb=_read_rss_kb(),
            temp_c=_read_cpu_temp_c(),
            load1=_read_load1(),
        )

    def _cpu_pct(self, sample: _TelemetrySample) -> float:
        if self._last_sample is None:
            return 0.0
        elapsed = max(sample.now - self._last_sample.now, 1e-6)
        cpu_elapsed = sample.process_time_s - self._last_sample.process_time_s
        return max(0.0, (cpu_elapsed / elapsed) * 100.0)

    def _gst_summary(self, sample: _TelemetrySample) -> str:
        snapshot_fn = getattr(self.source_switcher.pipeline, "telemetry_snapshot", None)
        if snapshot_fn is None:
            return "n/a"
        try:
            snapshot = snapshot_fn()
        except Exception:
            log.exception("video pipeline telemetry snapshot failed")
            return "error"

        buffers = snapshot.get("buffers", {})
        queues = snapshot.get("queues", {})
        latency = snapshot.get("latency", {})
        elapsed = (
            max(sample.now - self._last_sample.now, 1e-6)
            if self._last_sample is not None
            else self.interval_s
        )
        buffer_parts: list[str] = []
        if isinstance(buffers, dict):
            for name, count in sorted(buffers.items()):
                try:
                    total = int(count)
                except (TypeError, ValueError):
                    continue
                prior = self._last_gst_buffers.get(name, total)
                fps = max(0.0, (total - prior) / elapsed)
                buffer_parts.append(f"{name}={fps:.1f}fps")
                self._last_gst_buffers[name] = total

        # Suppress queues that are empty -- with leaky=downstream queues at
        # max-size-buffers=2 the vast majority sit at zero and just clutter
        # the log. Total count is reported so a regression in *which* queue
        # appears is still visible.
        queue_parts: list[str] = []
        total_queues = 0
        if isinstance(queues, dict):
            total_queues = len(queues)
            for name, levels in sorted(queues.items()):
                if not isinstance(levels, dict):
                    continue
                buffers_level = levels.get("current-level-buffers") or 0
                time_level = levels.get("current-level-time") or 0
                try:
                    b_int = int(buffers_level)
                except (TypeError, ValueError):
                    b_int = 0
                try:
                    t_int = int(time_level)
                except (TypeError, ValueError):
                    t_int = 0
                if b_int == 0 and t_int == 0:
                    continue
                queue_parts.append(f"{name}:b={b_int},t={t_int/1_000_000.0:.1f}ms")

        latency_parts: list[str] = []
        if isinstance(latency, dict):
            for name, stats in sorted(latency.items()):
                if not isinstance(stats, dict):
                    continue
                try:
                    last_ms = int(stats.get("last_ns", 0)) / 1_000_000.0
                    max_ms = int(stats.get("max_ns", 0)) / 1_000_000.0
                except (TypeError, ValueError):
                    continue
                latency_parts.append(f"{name}=last={last_ms:.1f}ms,max={max_ms:.1f}ms")

        queues_summary = (
            f"{len(queue_parts)}/{total_queues}nonzero:{','.join(queue_parts)}"
            if queue_parts
            else f"all-empty/{total_queues}"
        )
        return (
            f"buffers({','.join(buffer_parts) or 'none'})"
            f" queues({queues_summary})"
            f" latency({','.join(latency_parts) or 'none'})"
        )


def _format_sources(sources) -> str:
    return ",".join(f"0x{source:02x}" for source in sources)


def _read_rss_kb() -> int | None:
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _read_cpu_temp_c() -> float | None:
    for path in (
        "/sys/class/thermal/thermal_zone0/temp",
        "/sys/class/hwmon/hwmon0/temp1_input",
    ):
        try:
            raw = Path(path).read_text(encoding="utf-8").strip()
            return int(raw) / 1000.0
        except (OSError, ValueError):
            continue
    return None


def _read_load1() -> float | None:
    try:
        return os.getloadavg()[0]
    except (AttributeError, OSError):
        return None


def build_fc_video_status_report(
    controller: Controller,
    source_switcher: SourceSwitcher | None,
    sender_addrs: tuple[int, ...] | None = None,
    *,
    layout: str | None = None,
) -> ControllerVideoStatus:
    """Snapshot of the Controller's view, used to answer GET_STATUS.

    Slot sources come from the SourceSwitcher's currently-active slots
    (what the compositor is actually showing) so a slot whose desired
    source is offline reports as empty rather than as the unreachable
    Sender.

    Per-sender flags reflect the Controller's own state:
    ``ONLINE`` from PeerHealth, ``TRANSMITTING`` from the last command
    actually sent through that SenderLink, and ``RECORDING`` from the
    same source -- the Sender records on boot and we only stop it on
    HARD_STOP.
    """

    if source_switcher is not None:
        slots = tuple(source_switcher.active_sources)
    else:
        slots = ()

    visible_sender_addrs = (
        tuple(sorted(controller.senders)) if sender_addrs is None else sender_addrs
    )
    sender_entries: list[SenderVideoSnapshot] = []
    for addr in visible_sender_addrs:
        flags = 0
        online = controller.health.is_online(addr)
        if online:
            flags |= messages.FC_VIDEO_STATUS_FLAG_ONLINE
        link = controller.senders.get(addr)
        if link is not None and online:
            last = link.last_command_type
            if last is messages.VideoType.START_STREAM:
                flags |= messages.FC_VIDEO_STATUS_FLAG_TRANSMITTING
            if last is not messages.VideoType.HARD_STOP:
                flags |= messages.FC_VIDEO_STATUS_FLAG_RECORDING
        sender_entries.append(
            SenderVideoSnapshot(
                addr=addr,
                flags=flags,
                status=link.last_status.report if link and link.last_status else None,
            )
        )

    desired = tuple(source_switcher.sources) if source_switcher is not None else ()
    return ControllerVideoStatus(
        layout=layout or "",
        desired_sources=desired,
        active_sources=slots,
        senders=tuple(sender_entries),
    )


def make_fc_video_handler(
    pipeline: ControllerPipeline,
    layout_names: list[str],
    source_switcher: SourceSwitcher | None = None,
    *,
    controller: Controller | None = None,
    sender_addrs: tuple[int, ...] = (),
    fc_n_addr: int = protocol.ADDR_FC_N,
    now: Callable[[], float] = lambda: 0.0,
):
    """Adapter: FC_VIDEO commands -> ControllerPipeline mutations.

    Pipeline failures (e.g., GStreamer not installed on a dev machine)
    are logged but never propagate. ``controller`` is required to handle
    GET_STATUS; without it the GET_STATUS reply is skipped and the
    request is logged.
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
                if controller is None:
                    log.info(
                        "GET_STATUS request received with no controller wired in"
                    )
                    return
                report = build_fc_video_status_report(
                    controller,
                    source_switcher,
                    None,
                    layout=getattr(pipeline, "current_layout", None),
                )
                controller.node.send_local(
                    dst=frame.src if frame.src != 0 else fc_n_addr,
                    family=protocol.FAMILY_FC_VIDEO,
                    type=messages.FcVideoType.STATUS_REPORT,
                    payload=report.encode(),
                    reliable=True,
                    now=now(),
                )
            elif fc_video_type is messages.FcVideoType.GET_LAYOUTS:
                if controller is None:
                    log.info(
                        "GET_LAYOUTS request received with no controller wired in"
                    )
                    return
                layouts_report = messages.LayoutsReport(names=tuple(layout_names))
                controller.node.send_local(
                    dst=frame.src if frame.src != 0 else fc_n_addr,
                    family=protocol.FAMILY_FC_VIDEO,
                    type=messages.FcVideoType.LAYOUTS_REPORT,
                    payload=layouts_report.encode(),
                    reliable=True,
                    now=now(),
                )
        except PipelineError:
            log.exception(
                "pipeline failed to apply FC_VIDEO command %s",
                fc_video_type.name,
            )

    return on_fc_video


def _sender_route_name(addr: int) -> str:
    aliases = {
        protocol.ADDR_SENDER_DOWN: "down",
        protocol.ADDR_SENDER_AIRBRAKE: "airbrake",
        protocol.ADDR_SENDER_PAYLOAD: "payload",
        protocol.ADDR_SENDER_GROUND: "ground",
    }
    return aliases.get(addr, f"sender-0x{addr:02x}")


def _hitl_route_name(name: str, addr: int) -> str:
    safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in name).strip("-")
    return f"hitl-{safe}" if safe else f"hitl-0x{addr:02x}"


def _controller_routes_for_config(cfg: ControllerConfig) -> dict[int, str]:
    routes = dict(controller_routes())
    if cfg.fc_usb is None:
        return routes

    routes.update(
        {
            protocol.ADDR_FC_N: "fc-usb",
            protocol.ADDR_GROUND: "uart-hub",
            protocol.ADDR_TEENSY_HUB: "uart-hub",
            protocol.ADDR_RADIO_CMD: "uart-hub",
            protocol.ADDR_RADIO_G: "uart-hub",
            protocol.ADDR_RADIO_DATA: "uart-hub",
            protocol.ADDR_ARCH_MEGA_N: "uart-hub",
        }
    )
    return routes


def _install_signal_handlers(callback) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, callback)
        except (NotImplementedError, RuntimeError):
            # Windows doesn't support add_signal_handler for SIGTERM;
            # KeyboardInterrupt still terminates the process.
            pass


async def run(
    cfg: ControllerConfig,
    *,
    pipeline=None,
    stop_event: asyncio.Event | None = None,
    ready_event: asyncio.Event | None = None,
    bench_host: str = BENCH_CONTROL_HOST,
    bench_port: int = BENCH_CONTROL_PORT,
) -> None:
    """Run the Controller process.

    ``stop_event`` lets tests (or any caller that owns its own lifecycle)
    trigger a clean shutdown without sending OS signals. If omitted, an
    internal Event is created and signal handlers are installed for
    SIGINT/SIGTERM as before.

    ``ready_event`` is set once both the FC TCP listener and the bench
    control socket are accepting connections, so tests don't have to
    poll-and-pray.

    ``bench_host`` / ``bench_port`` override the bench-server bind
    address, used by tests to avoid clashing with a real running
    Controller on the dev box.
    """
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
        routes=_controller_routes_for_config(cfg),
        extra_peer_addrs=tuple(p.addr for p in cfg.hitl_peers),
        heartbeat_interval_s=cfg.heartbeat_interval_s,
        peer_timeout_s=cfg.peer_timeout_s,
        retain_local_history=False,
    )
    source_switcher = SourceSwitcher(
        controller,
        pipeline,
        tuple(s.addr for s in cfg.senders),
        initial_sources=cfg.initial_sources,
        keep_remote_streams=cfg.video.warm_remote_streams,
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
        controller=controller,
        sender_addrs=tuple(s.addr for s in cfg.senders),
        now=_now,
    )
    bench_server = BenchCommandServer(
        pipeline,
        source_switcher,
        layout_names,
        {s.name: s.addr for s in cfg.senders},
        host=bench_host,
        port=bench_port,
    )

    # Build links: serial ARC links, HITL peers from config, and video Senders
    # lazily from their ARC source address. A Sender no longer has to be
    # listed in controller.toml to get a route; its first heartbeat/status
    # frame is enough for the TCP server to bind the connection.
    serial_links_by_route: dict[str, QueuedUartLink] = {}
    serial_devices_by_route: dict[str, UartConfig] = {}
    if cfg.fc_usb is None:
        serial_devices_by_route["uart-fc-n"] = cfg.uart
    else:
        serial_devices_by_route["uart-hub"] = cfg.uart
        serial_devices_by_route["fc-usb"] = cfg.fc_usb

    for route in serial_devices_by_route:
        serial_links_by_route[route] = QueuedUartLink(
            lambda f, _route=route: controller.receive(
                f, _now(), ingress=_route
            )
        )
    controller_links = dict(serial_links_by_route)
    tcp_links_by_route: dict[str, QueuedTcpLink] = {}
    tcp_links_by_addr: dict[int, QueuedTcpLink] = {}

    def _register_tcp_link(addr: int, route: str) -> QueuedTcpLink:
        link = tcp_links_by_route.get(route)
        if link is None:
            link = QueuedTcpLink(
                lambda f, _route=route: controller.receive(
                    f, _now(), ingress=_route
                )
            )
            tcp_links_by_route[route] = link
            controller_links[route] = link
            controller.node.router.links[route] = link
        tcp_links_by_addr[addr] = link
        controller.node.router.routes[addr] = route
        return link

    def _ensure_sender_tcp_link(addr: int) -> QueuedTcpLink | None:
        if not is_sender_addr(addr):
            return None
        route = _sender_route_name(addr)
        link = _register_tcp_link(addr, route)
        if addr not in controller.senders:
            log.info("discovered Sender 0x%02x on route %s", addr, route)
        controller.ensure_sender(addr, route_name=route)
        source_switcher.add_sender(addr)
        return link

    for sender in cfg.senders:
        _ensure_sender_tcp_link(sender.addr)

    hitl_route_by_addr = {
        p.addr: _hitl_route_name(p.name, p.addr) for p in cfg.hitl_peers
    }
    for addr, route in hitl_route_by_addr.items():
        _register_tcp_link(addr, route)
    controller.set_links(controller_links)
    telemetry = _ControllerTelemetry(
        controller,
        source_switcher,
        serial_links_by_route,
        tcp_links_by_route,
    )

    # Map Sender source IPs to their TCP links so the TCP server can attach.
    ip_to_link = {
        s.ip: tcp_links_by_route[_sender_route_name(s.addr)]
        for s in cfg.senders
    }
    ip_to_link.update(
        {
            p.ip: tcp_links_by_route[hitl_route_by_addr[p.addr]]
            for p in cfg.hitl_peers
            if p.ip is not None
        }
    )

    server = TcpServer(
        host="0.0.0.0",
        port=cfg.listen_port,
        link_for_peer=ip_to_link.get,
        link_for_frame=lambda f: tcp_links_by_addr.get(f.src)
        or _ensure_sender_tcp_link(f.src),
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

    if ready_event is not None:
        ready_event.set()

    owns_stop_event = stop_event is None
    if stop_event is None:
        stop_event = asyncio.Event()

    if owns_stop_event:
        def _on_stop_signal() -> None:
            log.info("stop signal received")
            stop_event.set()

        _install_signal_handlers(_on_stop_signal)

    def _tick(now: float) -> None:
        telemetry.observe_tick(now)
        controller.tick(now)
        drain_bus = getattr(pipeline, "drain_bus", None)
        if drain_bus is not None:
            try:
                drain_bus()
            except PipelineError:
                log.exception("video pipeline reported an error")
        try:
            source_switcher.reconcile(now=now)
        except PipelineError:
            log.exception("video pipeline failed while reconciling sources")
        telemetry.maybe_log(now)

    tasks = [
        *(
            asyncio.create_task(
                run_uart_link(
                    serial_links_by_route[route],
                    serial_cfg.device,
                    serial_cfg.baud,
                    stop_event=stop_event,
                )
            )
            for route, serial_cfg in serial_devices_by_route.items()
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
        for link in (
            *serial_links_by_route.values(),
            *dict.fromkeys(tcp_links_by_route.values()),
        ):
            await link.stop()
        await server.stop()
        await bench_server.stop()


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
