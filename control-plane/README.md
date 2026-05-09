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

> One test (`test.test_tcp_link.TcpLinkTests.test_tcp_frame_link_round_trip_ack`) is flaky on Windows due to `ProactorEventLoop` cleanup behaviour and may hang. The other 89 tests pass cleanly. On Linux the full suite runs to completion.

Controller process:

```
python -m arc.controller_main --config /etc/arc/controller.toml
```

Sender process:

```
python -m arc.sender_main --config /etc/arc/sender.toml
```

## Config

See `arc/config.py` for the full schema. A minimal Controller TOML:

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

[[senders]]
id = 0x12
name = "sender-c"
ip = "10.42.0.12"
paired_fc = 0x03
```

A minimal Sender TOML:

```toml
[node]
address = 0x12
name = "sender-c"
paired_fc = 0x03

[controller]
ip = "10.42.0.1"
port = 6000

[video]
width = 640
height = 480
framerate = 30
bitrate = 2_500_000

[uart]
device = "/dev/serial0"
baud = 115200
```

## Dependencies

- Python 3.11+ (uses `tomllib`)
- `pyserial-asyncio` for UART links (imported lazily; tests don't require it)

GStreamer is intentionally out of scope here — `Sender.transmitting` /
`Sender.recording` expose the state a video module would key off of.
