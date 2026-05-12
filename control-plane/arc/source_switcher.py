"""Compositor source switching for the ARC Controller.

The Controller has two compositor slots; each slot has a *desired*
source address (a Sender, the Controller's local camera, or empty)
and an *active* source address (what's currently wired into the
pipeline). The two diverge when the desired source is a remote Sender
that hasn't come online yet, or has dropped offline: in that case the
active source falls back to EMPTY until reconcile() sees the Sender
return.

Switching strategies:
- ``keep_remote_streams=False`` (default, "cold"): only senders that
  are visible in an active slot are kept streaming. The Controller
  sends STOP_STREAM to online-but-hidden senders so they don't waste
  WiFi and decode time.
- ``keep_remote_streams=True`` ("warm"): every online sender is kept
  streaming regardless of slot membership. Higher load, but slot
  switches don't wait for the next sender pipeline to spin up.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping

from arc import protocol
from arc.controller import Controller


log = logging.getLogger(__name__)

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
        pipeline,
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
        self._stopped_remotes: set[int] = set()
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

    def set_sources(self, requested: Mapping[int, int], now: float = 0.0) -> None:
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

    def _reconcile_slots(self, slot_ids: Iterable[int], now: float) -> None:
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

        if not self.keep_remote_streams:
            self._sync_cold_remote_streams(now=now)

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
                self._stopped_remotes.discard(addr)

    def _sync_cold_remote_streams(self, now: float) -> None:
        visible = {
            source for source in self.active_sources if self._is_remote_sender(source)
        }
        for addr in sorted(self.sender_addrs):
            if not self.controller.health.is_online(addr):
                self._streaming_remotes.discard(addr)
                self._stopped_remotes.discard(addr)
                continue
            if addr in visible:
                self._ensure_sender_streaming(addr, now=now)
            elif addr not in self._stopped_remotes:
                self.controller.stop_sender(addr, now=now)
                self._streaming_remotes.discard(addr)
                self._stopped_remotes.add(addr)

    def _ensure_sender_streaming(self, addr: int, now: float) -> None:
        if addr in self._streaming_remotes:
            return
        self.controller.start_sender(addr, now=now)
        self._streaming_remotes.add(addr)
        self._stopped_remotes.discard(addr)

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
        self._stopped_remotes.add(addr)

    def _is_known_source(self, source_addr: int) -> bool:
        return (
            source_addr in (EMPTY_SOURCE, LOCAL_SOURCE)
            or source_addr in self.sender_addrs
        )

    def _is_remote_sender(self, source_addr: int) -> bool:
        return source_addr in self.sender_addrs
