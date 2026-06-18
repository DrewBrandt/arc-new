"""ARC address range helpers."""

from __future__ import annotations

from arc_protocol import protocol


SENDER_ADDR_MIN = protocol.ADDR_SENDER_DOWN
SENDER_ADDR_MAX = 0x19


def is_sender_addr(addr: int) -> bool:
    """Return true for ARC video sender addresses."""

    return SENDER_ADDR_MIN <= addr <= SENDER_ADDR_MAX


def sender_addr_range() -> tuple[int, ...]:
    """Return the assignable ARC video sender address range."""

    return tuple(range(SENDER_ADDR_MIN, SENDER_ADDR_MAX + 1))
