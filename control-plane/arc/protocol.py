"""ARC binary protocol helpers.

This mirrors the C implementation in com-protocol/src/arc_protocol.c.
No networking, retries, routing, or message catalogs live here.
"""

from __future__ import annotations

from dataclasses import dataclass


PROTOCOL_VERSION = 1

HEADER_SIZE = 9
CRC_SIZE = 2
OVERHEAD = HEADER_SIZE + CRC_SIZE

MAX_ENCODED_SIZE = 254
COBS_OVERHEAD = 2
MAX_FRAME_SIZE = MAX_ENCODED_SIZE - COBS_OVERHEAD
MAX_PAYLOAD_SIZE = MAX_FRAME_SIZE - OVERHEAD

ADDR_UNASSIGNED = 0x00
ADDR_GROUND = 0x01  # ground station (reached over RF via RADIO_CMD)
ADDR_FC_N = 0x02
ADDR_FC_C = 0x03
ADDR_FC_L = 0x04
ADDR_TEENSY_HUB = 0x05  # nosecone central router (Teensy 4.1)
ADDR_CONTROLLER = 0x10  # Pi 5 "pi-5-nose": video aggregator + WiFi gateway
ADDR_SENDER_DOWN = 0x11  # nose Pi 0, camera pointing down the rocket
ADDR_SENDER_AIRBRAKE = 0x12  # airbrake-bay Pi 0
ADDR_SENDER_PAYLOAD = 0x13  # payload-bay Pi 0
# 0x14 retired (was SENDER_L2)
ADDR_SENDER_GROUND = 0x15  # ground-side Pi 0
# Radios (0x20-0x2F reserved for radio-class nodes); both hang off the Teensy hub.
ADDR_RADIO_CMD = 0x20  # rocket command/status radio (on Teensy)
ADDR_RADIO_G = 0x21  # ground radio (OTA peer of RADIO_CMD)
ADDR_RADIO_DATA = 0x22  # live data downlink; proprietary, Teensy transcodes
# Power boards (0x30-0x3F reserved for power-class nodes).
ADDR_ARCH_MEGA_N = 0x30  # nosecone ARCH-Mega
ADDR_ARCH_MEGA_L = 0x31  # lower-bay ARCH-Mega
ADDR_ARCH_MEGA_C = 0x32  # center-section ARCH-Mega
ADDR_BROADCAST = 0xFF

FLAG_RELIABLE = 0x01
FLAG_URGENT = 0x02
FLAG_ACK = 0x04

FAMILY_NETMGMT = 0x00
FAMILY_FC_COORD = 0x01
FAMILY_VIDEO = 0x02
FAMILY_FC_VIDEO = 0x03
FAMILY_RADIO = 0x04
FAMILY_POWER = 0x05

NETMGMT_HEARTBEAT = 0x01
NETMGMT_ACK = 0x02
NETMGMT_SESSION_RESET = 0x03


class ArcProtocolError(ValueError):
    """Raised when bytes do not form a valid ARC frame."""


class ArcBufferError(ArcProtocolError):
    """Raised when encoded or decoded data exceeds protocol limits."""


@dataclass(frozen=True)
class Frame:
    src: int
    dst: int
    flags: int
    session: int
    seq: int
    family: int
    type: int
    payload: bytes = b""


def _u8(value: int, name: str) -> int:
    if not 0 <= value <= 0xFF:
        raise ValueError(f"{name} must fit in one byte")
    return value


def _u16(value: int, name: str) -> int:
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"{name} must fit in two bytes")
    return value


def crc16(data: bytes | bytearray | memoryview) -> int:
    """CRC-16/CCITT-FALSE, poly 0x1021, init 0xFFFF."""

    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def cobs_encode(data: bytes | bytearray | memoryview) -> bytes:
    """COBS-encode data and append the trailing 0x00 delimiter."""

    data = bytes(data)
    if len(data) > MAX_FRAME_SIZE:
        raise ArcBufferError("input exceeds maximum ARC frame size")

    out = bytearray()
    out.append(0)
    code_idx = 0
    code = 1

    for byte in data:
        if byte == 0:
            out[code_idx] = code
            code_idx = len(out)
            out.append(0)
            code = 1
        else:
            out.append(byte)
            code += 1
            if code == 0xFF:
                out[code_idx] = code
                code_idx = len(out)
                out.append(0)
                code = 1

    out[code_idx] = code
    out.append(0)

    if len(out) > MAX_ENCODED_SIZE:
        raise ArcBufferError("encoded frame exceeds maximum ARC encoded size")
    return bytes(out)


def cobs_decode(data: bytes | bytearray | memoryview) -> bytes:
    """Decode a COBS frame that includes the trailing 0x00 delimiter."""

    data = bytes(data)
    if len(data) < 2 or data[-1] != 0:
        raise ArcProtocolError("malformed COBS frame")

    out = bytearray()
    in_idx = 0
    encoded_end = len(data) - 1

    while in_idx < encoded_end:
        code = data[in_idx]
        in_idx += 1
        if code == 0:
            raise ArcProtocolError("unexpected zero inside COBS frame")

        literal_count = code - 1
        if in_idx + literal_count > encoded_end:
            raise ArcProtocolError("COBS code points past frame end")

        out.extend(data[in_idx : in_idx + literal_count])
        in_idx += literal_count

        if code != 0xFF and in_idx < encoded_end:
            out.append(0)

    if len(out) > MAX_FRAME_SIZE:
        raise ArcBufferError("decoded frame exceeds maximum ARC frame size")
    return bytes(out)


def build_frame(
    src: int,
    dst: int,
    flags: int,
    session: int,
    seq: int,
    family: int,
    type: int,
    payload: bytes | bytearray | memoryview = b"",
) -> bytes:
    """Build an unencoded ARC frame, LEN through CRC."""

    payload = bytes(payload)
    if len(payload) > MAX_PAYLOAD_SIZE:
        raise ArcBufferError("payload exceeds maximum ARC payload size")

    total = OVERHEAD + len(payload)
    frame = bytearray(total)
    frame[0] = total - 1
    frame[1] = _u8(src, "src")
    frame[2] = _u8(dst, "dst")
    frame[3] = _u8(flags, "flags")
    frame[4] = _u8(session, "session")
    frame[5] = _u16(seq, "seq") >> 8
    frame[6] = seq & 0xFF
    frame[7] = _u8(family, "family")
    frame[8] = _u8(type, "type")
    frame[HEADER_SIZE : HEADER_SIZE + len(payload)] = payload

    crc = crc16(frame[: HEADER_SIZE + len(payload)])
    frame[HEADER_SIZE + len(payload)] = crc >> 8
    frame[HEADER_SIZE + len(payload) + 1] = crc & 0xFF
    return bytes(frame)


def parse_frame(data: bytes | bytearray | memoryview) -> Frame:
    """Validate and parse an unencoded ARC frame."""

    data = bytes(data)
    if len(data) < OVERHEAD:
        raise ArcProtocolError("frame is too short")
    if len(data) > MAX_FRAME_SIZE:
        raise ArcBufferError("frame exceeds maximum ARC frame size")

    declared_len = data[0]
    if declared_len + 1 != len(data):
        raise ArcProtocolError("LEN field does not match frame size")

    payload_len = len(data) - OVERHEAD
    expected_crc = crc16(data[: HEADER_SIZE + payload_len])
    actual_crc = (data[HEADER_SIZE + payload_len] << 8) | data[HEADER_SIZE + payload_len + 1]
    if expected_crc != actual_crc:
        raise ArcProtocolError("bad frame CRC")

    return Frame(
        src=data[1],
        dst=data[2],
        flags=data[3],
        session=data[4],
        seq=(data[5] << 8) | data[6],
        family=data[7],
        type=data[8],
        payload=data[HEADER_SIZE : HEADER_SIZE + payload_len],
    )


def build_ack(original: Frame, my_session: int, my_seq: int) -> bytes:
    """Build a NETMGMT ACK frame for a received frame."""

    payload = bytes((original.seq >> 8, original.seq & 0xFF))
    return build_frame(
        src=original.dst,
        dst=original.src,
        flags=FLAG_ACK,
        session=my_session,
        seq=my_seq,
        family=FAMILY_NETMGMT,
        type=NETMGMT_ACK,
        payload=payload,
    )


def encode_frame(frame: Frame) -> bytes:
    """Build and COBS-encode a frame for serial/radio links."""

    return cobs_encode(
        build_frame(
            frame.src,
            frame.dst,
            frame.flags,
            frame.session,
            frame.seq,
            frame.family,
            frame.type,
            frame.payload,
        )
    )


def decode_frame(data: bytes | bytearray | memoryview) -> Frame:
    """COBS-decode and parse a serial/radio frame."""

    return parse_frame(cobs_decode(data))

