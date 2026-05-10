"""Controller-side GStreamer pipeline wrapper.

Builds the compositor + textoverlay + kmssink graph that produces the
Controller's analog composite output. The compositor has two source
slots; either slot may carry the local camera or a remote Sender's
UDP/RTP stream, or be hidden (alpha=0). Layouts are loaded from config
as bags of compositor pad properties and applied by mutating those
properties on the live pipeline -- both inputs keep flowing, so a
layout switch lands on the next output frame with no glitch.

GStreamer (``gi.repository.Gst``) is imported lazily inside
:meth:`ControllerPipeline.start`. The module is therefore importable on
machines without GStreamer; the pipeline only runs on the actual Pi.

Pad-property writes (xpos/ypos/width/height/alpha/zorder, textoverlay
text) are thread-safe on GstObject, so they are made directly from the
asyncio thread without bouncing through a GLib main loop. The control
plane has no GLib loop of its own.

Design doc references: Sections 7.3-7.5 (pipeline shape, layouts,
source switching), 9.1 (module placement), 12.3 (latency knobs:
``sync=false`` everywhere, ``rtpjitterbuffer latency=40``).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from arc import protocol
from arc.config import ControllerConfig
from arc.pipeline_errors import PipelineError
from arc.video_ports import video_port_for_sender

log = logging.getLogger(__name__)

__all__ = [
    "ControllerPipeline",
    "Layout",
    "PipelineError",
    "SlotProps",
    "SourceProps",
    "parse_layouts",
]


_PAD_PROP_KEYS = ("xpos", "ypos", "width", "height", "alpha", "zorder")
_PAD_PROP_ALIASES = {"z": "zorder"}
_SLOT_COUNT = 2
_EMPTY_SOURCE = protocol.ADDR_UNASSIGNED
_LOCAL_SOURCE = protocol.ADDR_CONTROLLER
_SUPPORTED_MIXERS = ("compositor", "glvideomixer")
_SUPPORTED_SWITCH_MODES = ("rebuild", "selector")
_DEFAULT_LOCAL_CAMERA = (
    "libcamerasrc ! videoconvert"
    " ! video/x-raw,width=640,height=480,format=I420,framerate=30/1"
    " ! queue max-size-buffers=2 leaky=downstream"
)
_BLACK_SOURCE = (
    "videotestsrc pattern=black is-live=true"
    " ! video/x-raw,width=640,height=480,framerate=30/1"
    " ! videoconvert"
    " ! video/x-raw,format=I420"
)
_BACKGROUND_SOURCE = (
    "videotestsrc pattern=black is-live=true"
    " ! video/x-raw,width=720,height=480,framerate=30000/1001"
    " ! videoconvert"
)
_OUTPUT_CAPS = "video/x-raw,width=720,height=480"
_GL_OUTPUT_CAPS = "video/x-raw(memory:GLMemory),width=720,height=480"


@dataclass(frozen=True)
class SlotProps:
    """Compositor pad property assignments for one slot.

    Defaults match GStreamer's compositor: position 0,0; width/height 0
    means "use the input's natural size"; full opacity; zorder 0.
    """

    xpos: int = 0
    ypos: int = 0
    width: int = 0
    height: int = 0
    alpha: float = 1.0
    zorder: int = 0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "SlotProps":
        normalised: dict[str, Any] = {}
        for key, value in raw.items():
            real = _PAD_PROP_ALIASES.get(key, key)
            if real not in _PAD_PROP_KEYS:
                raise PipelineError(f"unknown pad property '{key}'")
            normalised[real] = value
        return cls(**normalised)


@dataclass(frozen=True)
class Layout:
    """Resolved layout: pad-property assignments for both slots."""

    name: str
    slot_0: SlotProps
    slot_1: SlotProps


@dataclass(frozen=True)
class SourceProps:
    """Resolved source assignment for one compositor slot."""

    addr: int
    description: str


def parse_layouts(
    raw: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> dict[str, Layout]:
    """Resolve a TOML ``[layouts]`` table into :class:`Layout` objects.

    Pure function -- no GStreamer dependency, so it is unit-testable on
    machines without ``gi``.
    """

    out: dict[str, Layout] = {}
    for name, slot_table in raw.items():
        if not isinstance(slot_table, Mapping):
            raise PipelineError(f"layout {name!r} must be a table of slots")
        slot_0 = SlotProps.from_mapping(slot_table.get("slot_0", {}))
        slot_1 = SlotProps.from_mapping(slot_table.get("slot_1", {}))
        out[name] = Layout(name=name, slot_0=slot_0, slot_1=slot_1)
    return out


class ControllerPipeline:
    """Owns the Controller's GStreamer pipeline lifecycle.

    The slot input descriptions are constructor arguments so dynamic
    source switching can replace them; the defaults are local camera
    in slot 0 and a black test pattern in slot 1.
    """

    DEFAULT_LAYOUT = "local_full"

    def __init__(
        self,
        config: ControllerConfig,
        *,
        callsign: str | None = None,
        slot_0_source: str | None = None,
        slot_1_source: str | None = None,
        sink: str = "kmssink sync=false",
        mixer: str = "compositor",
        startup_layout: str | None = None,
        switch_mode: str | None = None,
    ) -> None:
        if mixer not in _SUPPORTED_MIXERS:
            raise PipelineError(
                f"unsupported mixer {mixer!r}; expected one of {_SUPPORTED_MIXERS}"
            )
        switch_mode = switch_mode or config.video.switch_mode
        if switch_mode not in _SUPPORTED_SWITCH_MODES:
            raise PipelineError(
                f"unsupported switch_mode {switch_mode!r}; expected one of {_SUPPORTED_SWITCH_MODES}"
            )
        self.config = config
        self.callsign = callsign or config.callsign
        self._sender_ips = {sender.addr: sender.ip for sender in config.senders}
        self._selector_sources = (
            _EMPTY_SOURCE,
            _LOCAL_SOURCE,
            *(sender.addr for sender in config.senders),
        )
        self._selector_pad_by_source = {
            source: f"sink_{idx}" for idx, source in enumerate(self._selector_sources)
        }
        self._slot_sources = [
            SourceProps(_LOCAL_SOURCE, slot_0_source or _DEFAULT_LOCAL_CAMERA),
            SourceProps(_EMPTY_SOURCE, slot_1_source or _BLACK_SOURCE),
        ]
        self.sink_factory = sink
        self.mixer = mixer
        self.switch_mode = switch_mode
        self.startup_layout = startup_layout or config.video.startup_layout
        self.layouts = parse_layouts(config.layouts)
        self._pipeline: Any = None
        self._compositor: Any = None
        self._textoverlay: Any = None
        self._selectors: list[Any] = []
        self._gst: Any = None
        self._current_layout: str | None = None
        self._overlay_text: str = self.callsign

    # --- public API ------------------------------------------------------

    def start(self) -> None:
        """Build and PLAY the pipeline. Idempotent if already running."""

        if self._pipeline is not None:
            return
        Gst = _import_gstreamer()
        self._gst = Gst
        Gst.init(None)

        pipeline_str = self.build_pipeline_description()
        log.info("controller pipeline: %s", pipeline_str)
        try:
            self._pipeline = Gst.parse_launch(pipeline_str)
        except Exception as exc:
            raise PipelineError(f"parse_launch failed: {exc}") from exc

        self._compositor = self._pipeline.get_by_name("comp")
        self._textoverlay = self._pipeline.get_by_name("overlay")
        if self._compositor is None or self._textoverlay is None:
            raise PipelineError(
                "pipeline missing required elements 'comp' or 'overlay'"
            )
        self._selectors = []
        if self.switch_mode == "selector":
            for slot_id in range(_SLOT_COUNT):
                selector = self._pipeline.get_by_name(f"slot{slot_id}_selector")
                if selector is None:
                    raise PipelineError(f"pipeline missing selector for slot {slot_id}")
                self._selectors.append(selector)

        self._textoverlay.set_property("text", self._overlay_text)
        initial = self._desired_layout()
        if initial is not None:
            self._apply_layout_now(initial)
            self._current_layout = initial.name
        if self.switch_mode == "selector":
            self._apply_selector_sources(range(_SLOT_COUNT))

        ret = self._pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            self.stop()
            raise PipelineError("pipeline failed to enter PLAYING state")

    def stop(self) -> None:
        """Tear the pipeline down to NULL. Idempotent."""

        if self._pipeline is None:
            return
        self._pipeline.set_state(self._gst.State.NULL)
        self._pipeline = None
        self._compositor = None
        self._textoverlay = None
        self._selectors = []

    def set_layout(self, name: str) -> None:
        """Switch to a named layout. Safe to call from asyncio."""

        layout = self.layouts.get(name)
        if layout is None:
            raise PipelineError(f"unknown layout: {name}")
        self._current_layout = name
        self._apply_layout_now(layout)

    def set_overlay(self, text: str) -> None:
        """Update the textoverlay (callsign + status). Safe to call from asyncio."""

        self._overlay_text = text
        if self._textoverlay is not None:
            self._textoverlay.set_property("text", text)

    def set_source(self, slot_id: int, source_addr: int) -> None:
        """Set one compositor slot source."""

        self.set_sources({slot_id: source_addr})

    def set_sources(self, sources: dict[int, int]) -> None:
        """Set one or more compositor slot sources with at most one rebuild."""

        resolved = {
            slot_id: self._resolve_source(slot_id, source_addr)
            for slot_id, source_addr in sources.items()
        }
        changed = {
            slot_id: source
            for slot_id, source in resolved.items()
            if self._slot_sources[slot_id] != source
        }
        if not changed:
            return
        was_running = self._pipeline is not None
        if was_running and self.switch_mode == "selector":
            for slot_id, source in changed.items():
                self._slot_sources[slot_id] = source
            self._apply_selector_sources(changed.keys())
            return

        if was_running:
            self.stop()
        for slot_id, source in changed.items():
            self._slot_sources[slot_id] = source
        if was_running:
            self.start()

    @property
    def current_layout(self) -> str | None:
        return self._current_layout

    @property
    def slot_sources(self) -> tuple[SourceProps, ...]:
        return tuple(self._slot_sources)

    # --- testable internals ---------------------------------------------

    def build_pipeline_description(self) -> str:
        """Return the gst-launch fragment for the full pipeline.

        Exposed so tests can assert on its shape without a live GStreamer.
        """

        # Quote the callsign carefully -- gst-launch accepts double-quoted
        # strings; embedded quotes would need escaping but a callsign
        # never contains them.
        callsign = self.callsign.replace('"', '')
        if self.mixer == "glvideomixer":
            # glvideomixer keeps frames in GL memory, so each input needs
            # glupload and the output needs gldownload before textoverlay
            # (which expects system-memory raw video).
            mixer_chain = (
                f"glvideomixer name=comp ! {_GL_OUTPUT_CAPS}"
                " ! gldownload ! videoconvert"
            )
            slot_tail = " ! glupload"
        else:
            mixer_chain = f"compositor name=comp ! {_OUTPUT_CAPS}"
            slot_tail = ""
        if self.switch_mode == "selector":
            return self._build_selector_pipeline_description(mixer_chain, slot_tail)
        return (
            f"{mixer_chain}"
            f" ! textoverlay name=overlay text=\"{callsign}\""
            f" font-desc=\"Sans 18\" valignment=top halignment=right"
            f" ! {self.sink_factory}"
            f" {_BACKGROUND_SOURCE}{slot_tail} ! comp.sink_0"
            f" {self._slot_sources[0].description}{slot_tail} ! comp.sink_1"
            f" {self._slot_sources[1].description}{slot_tail} ! comp.sink_2"
        )

    def _build_selector_pipeline_description(self, mixer_chain: str, slot_tail: str) -> str:
        callsign = self.callsign.replace('"', '')
        slot_branches = (
            f" input-selector name=slot0_selector sync-streams=false ! queue max-size-buffers=2 leaky=downstream{slot_tail} ! comp.sink_1"
            f" input-selector name=slot1_selector sync-streams=false ! queue max-size-buffers=2 leaky=downstream{slot_tail} ! comp.sink_2"
        )
        source_branches = []
        for idx, source_addr in enumerate(self._selector_sources):
            source = self._resolve_selector_source(source_addr)
            tee_name = f"source_{source_addr:02x}_tee"
            source_branches.append(
                f" {source.description} ! tee name={tee_name}"
                f" {tee_name}. ! queue max-size-buffers=2 leaky=downstream ! slot0_selector.sink_{idx}"
                f" {tee_name}. ! queue max-size-buffers=2 leaky=downstream ! slot1_selector.sink_{idx}"
            )
        return (
            f"{mixer_chain}"
            f" ! textoverlay name=overlay text=\"{callsign}\""
            f" font-desc=\"Sans 18\" valignment=top halignment=right"
            f" ! {self.sink_factory}"
            f" {_BACKGROUND_SOURCE}{slot_tail} ! comp.sink_0"
            f"{slot_branches}"
            f"{''.join(source_branches)}"
        )

    def _resolve_source(self, slot_id: int, source_addr: int) -> SourceProps:
        if not 0 <= slot_id < _SLOT_COUNT:
            raise PipelineError(f"source slot {slot_id} out of range")
        return self._resolve_selector_source(source_addr)

    def _resolve_selector_source(self, source_addr: int) -> SourceProps:
        if source_addr == _EMPTY_SOURCE:
            return SourceProps(source_addr, _BLACK_SOURCE)
        if source_addr == _LOCAL_SOURCE:
            return SourceProps(source_addr, _DEFAULT_LOCAL_CAMERA)
        if source_addr not in self._sender_ips:
            raise PipelineError(f"unknown source sender 0x{source_addr:02x}")
        return SourceProps(
            source_addr,
            _remote_sender_source(video_port_for_sender(source_addr)),
        )

    def _apply_selector_sources(self, slot_ids) -> None:
        if not self._selectors:
            return
        for slot_id in slot_ids:
            selector = self._selectors[slot_id]
            pad_name = self._selector_pad_by_source[self._slot_sources[slot_id].addr]
            pad = selector.get_static_pad(pad_name)
            if pad is None:
                raise PipelineError(f"selector slot {slot_id} missing pad {pad_name}")
            selector.set_property("active-pad", pad)

    def _initial_layout(self) -> Layout | None:
        if self.startup_layout is not None:
            layout = self.layouts.get(self.startup_layout)
            if layout is None:
                raise PipelineError(f"unknown startup layout: {self.startup_layout}")
            return layout
        if self.DEFAULT_LAYOUT in self.layouts:
            return self.layouts[self.DEFAULT_LAYOUT]
        if self.layouts:
            return next(iter(self.layouts.values()))
        return None

    def _desired_layout(self) -> Layout | None:
        if self._current_layout is not None:
            return self.layouts.get(self._current_layout)
        return self._initial_layout()

    def _apply_layout_now(self, layout: Layout) -> None:
        if self._compositor is None:
            return
        background = SlotProps(width=720, height=480, alpha=1.0, zorder=0)
        for pad_name, slot in (
            ("sink_0", background),
            ("sink_1", layout.slot_0),
            ("sink_2", layout.slot_1),
        ):
            pad = self._compositor.get_static_pad(pad_name)
            if pad is None:
                log.warning("compositor pad %s missing; skipping", pad_name)
                continue
            _set_pad_props(pad, slot)


# --- helpers ------------------------------------------------------------


def _set_pad_props(pad: Any, slot: SlotProps) -> None:
    pad.set_property("xpos", slot.xpos)
    pad.set_property("ypos", slot.ypos)
    pad.set_property("width", slot.width)
    pad.set_property("height", slot.height)
    pad.set_property("alpha", float(slot.alpha))
    pad.set_property("zorder", slot.zorder)


def _remote_sender_source(port: int) -> str:
    return (
        f"udpsrc port={port}"
        " caps=\"application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000\""
        " ! rtpjitterbuffer latency=100 drop-on-latency=true"
        " ! rtph264depay"
        " ! h264parse"
        " ! avdec_h264"
        " ! videoconvert"
        " ! video/x-raw,format=I420"
        " ! queue max-size-buffers=2 leaky=downstream"
    )


def _import_gstreamer() -> Any:
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError) as exc:
        raise PipelineError(
            "GStreamer Python bindings (gi.repository.Gst) not available; "
            "install python3-gi, gstreamer1.0-python3-plugin-loader, "
            "gstreamer1.0-libcamera"
        ) from exc
    return Gst


def main(argv: list[str] | None = None) -> int:
    """Standalone runner for the Step 6 manual smoke test.

    ``python -m arc.pipeline_controller --config /etc/arc/controller.toml``
    builds the pipeline using only the local camera and prints output to
    the composite pin. No Sender or control-plane code is involved.
    """

    import argparse
    import signal

    from arc.config import load_controller_config

    parser = argparse.ArgumentParser(prog="arc.pipeline_controller")
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--slot0",
        default=None,
        help="Override slot 0 source (default: libcamerasrc local camera)",
    )
    parser.add_argument(
        "--slot1",
        default=None,
        help="Override slot 1 source (default: black videotestsrc placeholder)",
    )
    parser.add_argument(
        "--sink",
        default=None,
        help="Override sink (default: 'kmssink sync=false'; use 'autovideosink' on a desktop)",
    )
    parser.add_argument(
        "--mixer",
        default=None,
        choices=list(_SUPPORTED_MIXERS),
        help=(
            "Compositor element (default: compositor). Try glvideomixer if"
            " compositor is too slow on this hardware; requires gstreamer1.0-gl."
        ),
    )
    args = parser.parse_args(argv)

    cfg = load_controller_config(args.config)
    kwargs: dict[str, Any] = {}
    if args.slot0 is not None:
        kwargs["slot_0_source"] = args.slot0
    if args.slot1 is not None:
        kwargs["slot_1_source"] = args.slot1
    if args.sink is not None:
        kwargs["sink"] = args.sink
    if args.mixer is not None:
        kwargs["mixer"] = args.mixer
    pipeline = ControllerPipeline(cfg, **kwargs)

    _import_gstreamer()
    from gi.repository import GLib  # type: ignore[import-not-found]

    loop = GLib.MainLoop()

    def _stop(*_: Any) -> None:
        loop.quit()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    pipeline.start()
    log.info("controller pipeline running; layout=%s", pipeline.current_layout)
    try:
        loop.run()
    finally:
        pipeline.stop()
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
