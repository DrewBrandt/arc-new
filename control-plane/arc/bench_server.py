"""Localhost-only bench control surface for the Controller.

Exists so video routing can be poked from an SSH session while
``arc-controller`` is running, without an FC in the loop. The wire
protocol is one-line-in / one-line-out plain text over TCP on
``127.0.0.1:6010``; the matching command-line client lives in
``controller_cli.py``.

This is deliberately separate from the FC_VIDEO control plane:
- It bypasses the binary frame protocol so it can be driven with
  netcat / a Python REPL / etc.
- It carries cycle/rotate verbs that aren't part of FC_VIDEO at all
  -- they're bench-only conveniences for stepping through source
  permutations.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

from arc import runtime
from arc.source_switcher import EMPTY_SOURCE, LOCAL_SOURCE, SourceSwitcher


BENCH_CONTROL_HOST = "127.0.0.1"
BENCH_CONTROL_PORT = 6010


class BenchCommandServer:
    """Localhost-only bench controls for testing video without an FC wired in."""

    def __init__(
        self,
        pipeline,
        source_switcher: SourceSwitcher,
        layout_names: list[str],
        sender_names: dict[str, int],
        *,
        host: str = BENCH_CONTROL_HOST,
        port: int = BENCH_CONTROL_PORT,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.source_switcher = source_switcher
        self.layout_names = layout_names
        self.sender_names = {k.lower(): v for k, v in sender_names.items()}
        self.host = host
        self.port = port
        self._now_fn = now_fn or runtime.now
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
            response = self.execute(line.decode("utf-8", errors="replace"), now=self._now_fn())
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
                self.source_switcher.set_source(slot, source, now=self._now_fn())
                await asyncio.sleep(interval)

    async def _rotate_loop(self, interval: float, sources: list[int]) -> None:
        idx = 0
        while True:
            self._rotate_once(idx, sources, now=self._now_fn())
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
