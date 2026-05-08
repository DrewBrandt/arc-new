# arc-protocol

Framing and routing protocol for the ARC video/telemetry network.

Portable C implementation that compiles on Arduino/AVR, Teensy/ARM, and
desktop Linux. No dynamic allocation, no exceptions, no STL. C-compatible
API for easy wrapping from Python via cffi or ctypes.

## What it does

- **Frame format** with addressing, sequence numbers, session IDs, flags,
  protocol family/type fields, and CRC-16/CCITT-FALSE.
- **COBS encoding** for serial/radio links so payloads can contain any
  byte value including 0x00. Encoded frames are unambiguously delimited
  by a single 0x00 byte.
- **Length-prefixed mode** for stream transports (TCP) where the
  underlying stream is reliable and ordered.
- **Routing-friendly headers**: SRC and DST in every frame so intermediate
  nodes can forward without parsing the payload.

## Frame layout

```
[ LEN | SRC | DST | FLAGS | SESSION | SEQ_HI | SEQ_LO | FAMILY | TYPE | PAYLOAD... | CRC_HI | CRC_LO ]
```

- `LEN` (1 byte) - counts everything that follows, up to and including CRC.
- `SRC`, `DST` (1 byte each) - node addresses. See `arc_protocol.h` for assignments.
- `FLAGS` (1 byte) - RELIABLE, URGENT, ACK, plus reserved bits.
- `SESSION` (1 byte) - increments on each reboot of the source node.
  Receivers reset dedup state when SESSION changes.
- `SEQ` (2 bytes, big-endian) - sequence number, scoped to the
  (SRC, DST, SESSION) tuple.
- `FAMILY` (1 byte) - protocol family (NETMGMT, FC_COORD, VIDEO, FC_VIDEO).
- `TYPE` (1 byte) - message type within the family.
- `PAYLOAD` - variable, up to 241 bytes.
- `CRC` (2 bytes, big-endian) - CRC-16/CCITT-FALSE over LEN through end-of-payload.

Total overhead: 11 bytes. Maximum payload that fits in a 255-byte radio
frame after COBS encoding: **241 bytes**.

## Building

```
make test       # builds and runs the unit tests
make vectors    # builds and prints canonical test vectors
```

## Layout

```
src/        protocol library used by firmware and host tools
test/       host-side unit tests
examples/   small utilities, including test vector generation
```

## Using from C / Arduino

Add `src/arc_protocol.c` and `src/arc_protocol.h` to your project. The
library has no dependencies beyond the C standard library headers
`<stdint.h>`, `<stddef.h>`, `<stdbool.h>`, and `<string.h>`.

Build a frame:

```c
uint8_t frame[ARC_MAX_FRAME_SIZE];
int n = arc_frame_build(frame, sizeof(frame),
                        ARC_ADDR_FC_C, ARC_ADDR_FC_N,
                        ARC_FLAG_RELIABLE, my_session, my_seq++,
                        ARC_FAMILY_FC_COORD, MY_COMMAND_TYPE,
                        payload, payload_len);
```

For serial/radio, COBS-encode it:

```c
uint8_t encoded[ARC_MAX_ENCODED_SIZE];
int m = arc_cobs_encode(frame, n, encoded, sizeof(encoded));
// transmit `encoded` (m bytes, ending with 0x00)
```

For TCP, send the frame directly with a length prefix.

Receive side:

```c
uint8_t decoded[ARC_MAX_FRAME_SIZE];
int n = arc_cobs_decode(received_buf, received_len, decoded, sizeof(decoded));
arc_frame_t f;
if (arc_frame_parse(decoded, n, &f) == ARC_OK) {
    if (f.dst == MY_ADDR) {
        // dispatch by f.family / f.type
    } else {
        // forward to next hop based on routing table
    }
}
```

## Test vectors

`examples/gen_vectors.c` produces canonical encoded outputs for a fixed
set of inputs. The Python implementation (when written) must produce
byte-identical output for the same inputs. Run:

```
make vectors
./build/gen_vectors > test_vectors.txt
```

Then use `test_vectors.txt` as the source of truth in Python tests.

## Design notes

**CRC-16/CCITT-FALSE** (poly=0x1021, init=0xFFFF) is used rather than
XMODEM (init=0x0000) because the non-zero init catches a class of errors
involving leading zero bytes that XMODEM misses. The bit-by-bit
implementation is small enough that no lookup table is needed even on
AVR; performance is fine for our frame sizes.

**COBS overhead is exactly 2 bytes** for any frame up to 254 bytes
unencoded. The library caps frames at 252 bytes unencoded to keep this
predictable.

**The CRC covers the full frame** including the LEN byte and all header
fields. Routers don't need to recompute it because they don't modify the
frame -- end-to-end integrity is what we verify.

**No dynamic allocation anywhere.** All buffers are caller-provided.
The library is safe to call from interrupt handlers as long as the
caller-provided buffers aren't shared.
