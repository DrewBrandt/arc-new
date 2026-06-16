# ARC fake node

Teensy 4.1 bench tool that speaks ARC over `Serial1`..`Serial4` and can
pretend to be multiple node addresses without reflashing.

Use this with one Teensy running `tools/teensy-hub` and another running this
fake-node firmware. Cross-connect one or more UARTs between them, and the hub
will learn routes from the source addresses it sees.

## Build

```bash
pio run -e teensy41
pio run -e teensy41 -t upload
pio device monitor
```

## Useful Console Commands

```text
ports
port 1
port 2
port 3
port 4

node fc-n
node fc-c
node fc-l
node airbrake
node payload
node power-n
node power-l
node power-c

mode flight
mode airbrake
mode payload
mode power

telemetry
heartbeat
```

`port` selects which fake UART subsequent manual commands configure/use.
`ports` lists all simulated ports. `node` changes the selected port's ARC
source address. It also picks a sensible telemetry mode for that address: power
addresses default to `power`, airbrake/payload sender addresses default to their
matching telemetry, and FC addresses default to `flight`.

Every configured port emits heartbeat and one telemetry frame per second. This
is enough for the Teensy hub to learn `src -> UART` routes from whichever hub
port each fake serial port is plugged into.

## Default Ports

| Fake port | Teensy serial | Default node | Default mode |
|-----------|---------------|--------------|--------------|
| `p1` | `Serial1` | `fc-n` | `flight` |
| `p2` | `Serial2` | `fc-c` | `flight` |
| `p3` | `Serial3` | `power-n` | `power` |
| `p4` | `Serial4` | `airbrake` | `airbrake` |

## Example

```text
ports
port 2
node fc-l
mode flight
telemetry

port 3
node power-l
telemetry
```

On the hub console, press `l` to see the learned route table.
