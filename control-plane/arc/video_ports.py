"""Deterministic UDP/RTP video ports for ARC Senders."""

from __future__ import annotations

from arc.addressing import is_sender_addr


VIDEO_PORT_PREFIX = 50


def video_port_for_sender(sender_addr: int) -> int:
    """Return the Controller UDP/RTP port for one Sender address.

    Sender addresses are assigned so their hex suffix is human-readable:
    ``0x12`` maps to port ``5012``. This keeps bench debugging obvious
    while giving every remote source its own stable receive port.
    """

    if not is_sender_addr(sender_addr):
        raise ValueError(f"not an ARC Sender address: 0x{sender_addr:02x}")
    suffix = f"{sender_addr:02x}"
    if not suffix.isdecimal():
        raise ValueError(
            f"sender address cannot map to decimal port: 0x{sender_addr:02x}"
        )
    return int(f"{VIDEO_PORT_PREFIX}{suffix}")
