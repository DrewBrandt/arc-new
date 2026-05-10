# ARC control plane

Python control-plane code for the ARC Controller and Sender processes.

## Layout

```
arc/protocol.py        frame build/parse, COBS, CRC, address/family constants
arc/messages.py        typed NETMGMT, VIDEO, and FC_VIDEO payload helpers
arc/router.py          static route lookup and local/forward dispatch
arc/reliable.py        per-endpoint ACK/retry/dedup state machine
arc/health.py          heartbeat emitter + peer-online tracker
arc/node.py            in-memory composition harness for flow tests
arc/tcp_link.py        asyncio TCP LEN-prefixed frame transport
arc/uart_link.py       asyncio UART/serial COBS frame transport
arc/sender_link.py     Controller-side wrapper around one Sender
arc/controller.py      Controller orchestration shell
arc/sender.py          Sender orchestration shell
arc/pipeline_controller.py
                       Controller GStreamer pipeline (GL mixer +
                       textoverlay + kmssink). gi/Gst imported lazily.
arc/pipeline_sender.py Sender GStreamer pipeline (libcamerasrc +
                       configurable H.264 encoder + tee + valves to udpsink and
                       mp4mux/filesink). gi/Gst imported lazily.
arc/pipeline_errors.py Shared PipelineError raised by both pipelines.
arc/video_ports.py     Deterministic Sender video port mapping
                       (0x12 -> UDP/RTP 5012).
arc/config.py          TOML config loader (Python 3.11+ tomllib)
arc/runtime.py         asyncio glue: serial open, tick loop, TCP server
arc/controller_main.py Controller process entrypoint
arc/sender_main.py     Sender process entrypoint
test/                  stdlib unittest tests
```

## Running

Tests:

```
python -m unittest discover -s test
```

Controller process:

```
python -m arc.controller_main --config /etc/arc/controller.toml
```

Bench video controls without an FC:

```
python -m arc.controller_cli status
python -m arc.controller_cli source 1 sender-c
python -m arc.controller_cli source 1 sender-l1
python -m arc.controller_cli cycle 1 5 sender-c sender-l1
python -m arc.controller_cli rotate 5 local sender-c sender-l1
python -m arc.controller_cli stop-cycle
python -m arc.controller_cli layout split
```

The CLI talks to the Controller's localhost-only bench control socket
(`127.0.0.1:6010`). Use it over SSH on `arcpi1` while `arc-controller`
is running. It is separate from the flight FC protocol and exists so
hardware can be tested before FC-N is wired in.

Sender process:

```
python -m arc.sender_main --config /etc/arc/sender.toml
```

Remote Sender video uses deterministic Controller UDP/RTP ports derived
from the Sender address: Sender-N `0x11 -> 5011`, Sender-C `0x12 -> 5012`,
Sender-L1 `0x13 -> 5013`, Sender-L2 `0x14 -> 5014`, and Sender-GND
`0x15 -> 5015`. The Sender pipeline derives its `udpsink` port from its
own address; the Controller derives each `udpsrc` port from the selected
source address.

## Config

See `arc/config.py` for the full schema. The setup script generates a
bench-ready Pi 5 Controller config similar to:

```toml
[node]
address = 0x10

[uart]
device = "/dev/serial0"
baud = 115200

[overlay]
callsign = "KD3BBP"

[controller]
listen_port = 6000

[video]
mixer = "glvideomixer"
sink = "kmssink driver-name=drm-rp1-vec sync=false"
startup_layout = "split"
switch_mode = "selector"

[layouts.local_full]
slot_0 = { xpos = 40, ypos = 0, width = 640, height = 480, alpha = 1.0 }
slot_1 = { alpha = 0.0 }

[layouts.split]
slot_0 = { xpos = 40, ypos = 0, width = 640, height = 480, alpha = 1.0, z = 1 }
slot_1 = { xpos = 420, ypos = 280, width = 240, height = 160, alpha = 1.0, z = 2 }

[sources]
slot_0 = 0x10
slot_1 = 0x12

[[senders]]
id = 0x12
name = "sender-c"
ip = "arcpi2.local"
paired_fc = 0x03
```

The Controller starts in split/PIP mode. If Sender-C is not online yet,
slot 1 stays black; once the sender is observed on the control plane, the
Controller starts its stream and rebuilds slot 1 to `udpsrc port=5012`.

The setup script generates a Zero 2 W-friendly Sender config similar to:

```toml
[node]
address = 0x12
name = "sender-c"
paired_fc = 0x03

[controller]
ip = "arcpi1.local"
port = 6000

[video]
width = 640
height = 480
framerate = 15
bitrate = 1_200_000
encoder = "x264enc tune=zerolatency speed-preset=ultrafast key-int-max=15 bitrate=1200"
start_stream_on_boot = true

[uart]
device = "/dev/serial0"
baud = 115200
```

The software encoder is intentional for the current Raspberry Pi OS image:
on the tested Zero 2 W, `bcm2835-codec`/`v4l2h264enc` failed to start
streaming even with a fake source. If a future kernel/firmware restores
hardware encode, set `encoder = "v4l2h264enc"` and raise bitrate/framerate
as appropriate.

When adding a new Sender to an already-configured Controller, regenerate the
Controller config with an explicit sender list:

```bash
sudo ./setup.sh controller --force-config \
  --senders "0x12:sender-c:arcpi2.local:0x03,0x13:sender-l1:arcpi3.local:0x04"
```

Then reboot the Controller. Without `--force-config`, setup preserves the
existing `/etc/arc/controller.toml`.

## Hardware Notes

- The tested Controller target is Raspberry Pi 5 using composite output.
  Pi 5 composite is exposed through the RP1 VEC DRM driver, so the sink is
  `kmssink driver-name=drm-rp1-vec sync=false`.
- The setup script enables Pi 5 composite with
  `dtoverlay=vc4-kms-v3d-pi5,composite`, `enable_tvout=1`, and
  `video=Composite-1:720x480i,tv_mode=NTSC`.
- Controller and Sender services boot in `multi-user.target` so desktop
  components do not steal KMS, camera, or media devices.
- Lab WiFi profiles are forced to 2.4 GHz (`802-11-wireless.band bg`) so
  Zero 2 W senders can join them reliably. This also keeps a Pi 5 controller
  on the 2.4 GHz copy of the ARC lab SSID; it is a per-profile setting, not a
  global radio disable.
- Sender video uses RTP/H.264 over UDP on deterministic ports: Sender-C
  (`0x12`) sends to Controller UDP port `5012`.
- Bench source switching is available on the Controller with
  `python -m arc.controller_cli source 1 sender-c` or
  `python -m arc.controller_cli cycle 1 5 sender-c sender-l1`.
  Use `python -m arc.controller_cli rotate 5 local sender-c sender-l1`
  to rotate three pictures through main, offscreen/rest, and PIP. Rotation
  uses the Controller's selector switch mode, which keeps the KMS/compositor
  pipeline running and flips active input-selector pads instead of rebuilding
  the graph. This is smoother, but it means configured remote UDP/decode
  branches exist in the running graph; avoid configuring extra unused senders
  on underpowered controller hardware.

## Dependencies

- Python 3.11+ (uses `tomllib`)
- `pyserial-asyncio` for UART links (imported lazily; tests don't require it)

GStreamer is wired into the orchestrators on a best-effort basis:
`controller_main` builds a `ControllerPipeline` and dispatches FC_VIDEO
commands to it; `sender_main` builds a `SenderPipeline` and dispatches
VIDEO commands. If `gi`/Gst is unavailable (typical on dev machines)
the pipeline raises `PipelineError`, the adapter logs it, and the
control plane keeps running. On the Pi the pipelines come up live.
