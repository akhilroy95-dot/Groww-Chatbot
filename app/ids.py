"""Small identifier helpers.

Request IDs use the ULID shape (48-bit millisecond timestamp + 80 random bits,
Crockford base32, 26 characters) without adding a dependency.  They sort by
creation time, which makes the artifact folders easy to scan.
"""

from __future__ import annotations

import os
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(value: int, length: int) -> str:
    characters = [""] * length
    for position in range(length - 1, -1, -1):
        characters[position] = _CROCKFORD[value & 31]
        value >>= 5
    return "".join(characters)


def ulid() -> str:
    """Return a 26-character, time-sortable ULID string."""
    timestamp = int(time.time() * 1000) & ((1 << 48) - 1)
    randomness = int.from_bytes(os.urandom(10), "big")
    return _encode(timestamp, 10) + _encode(randomness, 16)


def request_id() -> str:
    return f"req_{ulid()}"
