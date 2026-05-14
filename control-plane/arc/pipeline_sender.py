"""Sender-side GStreamer pipeline wrapper.

Captures from the local CSI camera, encodes once with the configured
H.264 encoder, and tees the encoded stream to two branches gated by
``valve`` elements:

  - TX:     ``rtph264pay → udpsink``  (Controller:50XX) -- gated by ``tx_valve``
  - Record: ``mp4mux  → filesink``    (recording dir)   -- gated by ``rec_valve``

A single encoder feeds both branches. Each branch's valve can
``drop=true`` to suppress output without tearing the pipeline down.

State semantics mirror the control-plane VIDEO commands and the
``Sender.transmitting`` / ``Sender.recording`` flags:

  - :meth:`start_stream`  -- pipeline PLAYING, both valves open.
  - :meth:`stop_stream`   -- ``tx_valve`` closed; recording continues
    (the design doc's "soft stop").
  - :meth:`hard_stop`     -- pipeline NULL; recording file finalised.

The TX-while-not-recording combination is intentionally not exposed:
design doc §7.2 says it is "not a meaningful state and should not be
implemented." The state machine here cannot reach it.

GStreamer (``gi.repository.Gst``, ``GLib``) is imported lazily inside
:meth:`SenderPipeline.start_stream`. The module is therefore importable
and unit-testable on machines without GStreamer; the live pipeline only
runs on the actual Pi.

Design doc references: §5.1 (latency knobs, ~2.5 Mbps target, no
reliability), §7.2 (sender pipeline shape and state semantics), §7.5
("A keyframe is always emitted at pipeline start"), §9.2 (module
placement), §12.3 (``sync=false`` everywhere on receive paths -- and
on the TX udpsink so we don't pace by clock).
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
from pathlib import Path
from typing import Any, Callable

from arc.config import SenderConfig
from arc.pipeline_errors import PipelineError
from arc.video_ports import video_port_for_sender

log = logging.getLogger(__name__)

__all__ = ["I_FRAME_PERIOD", "PipelineError", "SenderPipeline"]


I_FRAME_PERIOD = 10  # one keyframe every 10 frames (~333 ms at 30 fps).
_BRANCH_QUEUE = (
    "queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=downstream"
)
# Map user-facing rotation (degrees clockwise) to videoflip method values.
# videoflip method nicks: 0=none, 1=clockwise (90 CW), 2=rotate-180,
# 3=counterclockwise (90 CCW, i.e. 270 CW).
_ROTATION_TO_VIDEOFLIP_METHOD = {0: 0, 90: 1, 180: 2, 270: 3}


class SenderPipeline:
    """Owns the Sender's GStreamer pipeline lifecycle.

    The pipeline has three reachable states:

    - **idle**:  no pipeline; equivalent to ``hard_stop``.
    - **recording**: pipeline PLAYING with ``tx_valve`` closed.
    - **streaming**: pipeline PLAYING with both valves open.

    The standalone runner starts in **streaming**; the
    ``start_stream`` / ``stop_stream`` / ``hard_stop`` transitions
    correspond directly to the VIDEO family commands handled by
    :class:`arc.sender.Sender`.
    """

    def __init__(
        self,
        config: SenderConfig,
        *,
        controller_ip: str | None = None,
        controller_port: int | None = None,
        recording_path: str | Path | None = None,
        camera_source: str = "libcamerasrc",
        encoder: str | None = None,
        clock: Callable[[], _dt.datetime] = _dt.datetime.now,
    ) -> None:
        self.config = config
        self.controller_ip = controller_ip or config.controller_ip
        self.controller_port = (
            controller_port
            if controller_port is not None
            else video_port_for_sender(config.addr)
        )
        self.camera_source = camera_source
        self.encoder = encoder or config.video.encoder
        self._clock = clock
        self._bitrate_bps = config.video.bitrate_bps

        rec_dir = Path(recording_path) if recording_path else Path(config.video.recording_path)
        self.recording_dir = rec_dir
        self._recording_file: Path | None = None

        self._pipeline: Any = None
        self._tx_valve: Any = None
        self._rec_valve: Any = None
        self._gst: Any = None

        self._transmitting = False
        self._recording = False

    # --- public API ------------------------------------------------------

    @property
    def transmitting(self) -> bool:
        return self._transmitting

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def bitrate_bps(self) -> int:
        return self._bitrate_bps

    @property
    def recording_file(self) -> Path | None:
        """Path of the active recording, or None when idle."""
        return self._recording_file

    def start_stream(self) -> None:
        """Enter the *streaming* state: pipeline PLAYING, both valves open.

        Idempotent. If currently *idle*, builds the pipeline. If
        currently *recording*, just opens ``tx_valve``.
        """

        if self._pipeline is None:
            self._build_and_play()
        self._transmitting = True
        self._recording = True
        self._set_valve(self._tx_valve, drop=False)
        self._set_valve(self._rec_valve, drop=False)

    def stop_stream(self) -> None:
        """Soft stop: close ``tx_valve``; recording continues unchanged.

        Idempotent.
        """

        self._transmitting = False
        if self._pipeline is None:
            return
        self._set_valve(self._tx_valve, drop=True)

    def hard_stop(self) -> None:
        """Tear the pipeline down to NULL; finalise the recording file."""

        self._transmitting = False
        self._recording = False
        if self._pipeline is None:
            return
        self._pipeline.set_state(self._gst.State.NULL)
        self._pipeline = None
        self._tx_valve = None
        self._rec_valve = None
        self._recording_file = None

    def set_bitrate(self, bitrate_bps: int) -> None:
        """Update the encoder bitrate.

        Takes effect on the next :meth:`start_stream`. Hardware encoder
        bitrate controls are not reliably re-settable on a live encoder
        across all v4l2 driver versions; rebuilding on the next stream
        start is the safe path.
        """

        if bitrate_bps <= 0:
            raise PipelineError("bitrate must be positive")
        self._bitrate_bps = bitrate_bps

    # --- testable helpers ------------------------------------------------

    def build_pipeline_description(self, rec_path: Path | None = None) -> str:
        """Return the gst-launch fragment for the sender pipeline.

        ``rec_path`` pins the recording filename; if ``None``, a fresh
        timestamp-based path is allocated.
        """

        if rec_path is None:
            rec_path = self._next_recording_path()
        encoder = self._encoder_description()
        rotation = self.config.video.rotation
        if rotation not in _ROTATION_TO_VIDEOFLIP_METHOD:
            raise PipelineError(
                f"unsupported rotation {rotation}; expected 0, 90, 180, or 270"
            )
        flip_chain = (
            f" ! videoflip method={_ROTATION_TO_VIDEOFLIP_METHOD[rotation]}"
            if rotation != 0
            else ""
        )
        return (
            f"{self.camera_source}"
            f" ! video/x-raw,width={self.config.video.width},"
            f"height={self.config.video.height},"
            f"framerate={self.config.video.framerate}/1"
            f" ! videoconvert"
            f"{flip_chain}"
            f" ! {encoder}"
            f" ! video/x-h264,profile=baseline"
            f" ! h264parse config-interval=1"
            f" ! tee name=t"
            f" t. ! {_BRANCH_QUEUE} ! valve name=tx_valve drop=true"
            f" ! rtph264pay pt=96 config-interval=1"
            f" ! udpsink host={self.controller_ip} port={self.controller_port}"
            f" sync=false"
            f" t. ! {_BRANCH_QUEUE} ! valve name=rec_valve drop=true"
            f" ! mp4mux"
            f" ! filesink location={rec_path}"
        )

    def _encoder_description(self) -> str:
        if self.encoder.split(maxsplit=1)[0] == "v4l2h264enc":
            return (
                f"{self.encoder} name=enc"
                f" extra-controls=\"controls,h264_i_frame_period={I_FRAME_PERIOD}"
                f",video_bitrate={self._bitrate_bps}\""
            )
        return f"{self.encoder} name=enc"

    def _next_recording_path(self) -> Path:
        ts = self._clock().strftime("%Y%m%d-%H%M%S")
        return self.recording_dir / f"{self.config.name}-{ts}.mp4"

    # --- internals -------------------------------------------------------

    def _build_and_play(self) -> None:
        Gst = _import_gstreamer()
        self._gst = Gst
        Gst.init(None)

        try:
            os.makedirs(self.recording_dir, exist_ok=True)
        except OSError as exc:
            raise PipelineError(
                f"cannot create recording directory {self.recording_dir}: {exc}"
            ) from exc

        self._recording_file = self._next_recording_path()
        pipeline_str = self.build_pipeline_description(self._recording_file)
        log.info("sender pipeline: %s", pipeline_str)
        try:
            self._pipeline = Gst.parse_launch(pipeline_str)
        except Exception as exc:
            raise PipelineError(f"parse_launch failed: {exc}") from exc

        self._tx_valve = self._pipeline.get_by_name("tx_valve")
        self._rec_valve = self._pipeline.get_by_name("rec_valve")
        if self._tx_valve is None or self._rec_valve is None:
            raise PipelineError("pipeline missing tx_valve or rec_valve")

        ret = self._pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            self.hard_stop()
            raise PipelineError("pipeline failed to enter PLAYING state")

    def _set_valve(self, valve: Any, *, drop: bool) -> None:
        if valve is None:
            return
        valve.set_property("drop", drop)


# --- helpers ------------------------------------------------------------


def _import_gstreamer() -> Any:
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError) as exc:
        raise PipelineError(
            "GStreamer Python bindings (gi.repository.Gst) not available; "
            "install python3-gi, gstreamer1.0-python3-plugin-loader, "
            "gstreamer1.0-libcamera, and gstreamer1.0-plugins-good"
        ) from exc
    return Gst


def main(argv: list[str] | None = None) -> int:
    """Standalone runner for the Step 7 manual smoke test.

    ``python -m arc.pipeline_sender --config /etc/arc/sender.toml`` builds
    the pipeline and immediately enters the *streaming* state. Verify
    the manual deliverable by watching for RTP/H.264 packets at the
    Controller's per-Sender UDP port -- e.g. Sender-C (0x12) uses 5012::

        gst-launch-1.0 udpsrc port=5012 caps='application/x-rtp,encoding-name=H264,payload=96' \\
            ! rtpjitterbuffer ! rtph264depay ! avdec_h264 ! autovideosink sync=false
    """

    import argparse
    import signal

    from arc.config import load_sender_config

    parser = argparse.ArgumentParser(prog="arc.pipeline_sender")
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--source",
        default=None,
        help="Override camera source (default: libcamerasrc; use 'videotestsrc is-live=true' off-Pi)",
    )
    parser.add_argument(
        "--encoder",
        default=None,
        help="Override encoder (default: v4l2h264enc; use 'x264enc tune=zerolatency speed-preset=ultrafast bitrate=2500' off-Pi)",
    )
    parser.add_argument(
        "--controller-ip",
        default=None,
        help="Override Controller IP (default: from sender config)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="UDP port on the Controller for RTP video (default: 50XX from Sender address)",
    )
    parser.add_argument(
        "--recording-dir",
        default=None,
        help="Override recording directory (default: from sender config)",
    )
    args = parser.parse_args(argv)

    cfg = load_sender_config(args.config)
    kwargs: dict[str, Any] = {"controller_port": args.port}
    if args.source is not None:
        kwargs["camera_source"] = args.source
    if args.encoder is not None:
        kwargs["encoder"] = args.encoder
    if args.controller_ip is not None:
        kwargs["controller_ip"] = args.controller_ip
    if args.recording_dir is not None:
        kwargs["recording_path"] = args.recording_dir
    pipeline = SenderPipeline(cfg, **kwargs)

    _import_gstreamer()
    from gi.repository import GLib  # type: ignore[import-not-found]

    loop = GLib.MainLoop()

    def _stop(*_: Any) -> None:
        loop.quit()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    pipeline.start_stream()
    log.info(
        "sender %s streaming to %s:%d, recording to %s",
        cfg.name,
        pipeline.controller_ip,
        pipeline.controller_port,
        pipeline.recording_file,
    )
    try:
        loop.run()
    finally:
        pipeline.hard_stop()
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
