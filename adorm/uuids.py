"""UUIDs generated in Python: `uuid7()` for column defaults and for databases without a UUID v7 function."""

from __future__ import annotations

import os
import time
import uuid

__all__ = ["uuid7"]


def _uuid7() -> uuid.UUID:
    milliseconds = time.time_ns() // 1_000_000
    value = (milliseconds & ((1 << 48) - 1)) << 80 | int.from_bytes(os.urandom(10), "big")
    value = (value & ~(0xF << 76)) | 0x7 << 76
    value = (value & ~(0x3 << 62)) | 0x2 << 62
    return uuid.UUID(int=value)


def uuid7() -> uuid.UUID:
    """A new version 7 UUID (RFC 9562): the Unix time in milliseconds in the first 48 bits, the version and variant
    bits, the rest random. ``uuid.uuid7()`` of the standard library where Python has it (3.14 and later). Usable as a
    field default (``default=adorm.uuid7``)."""
    return _STDLIB_UUID7() if _STDLIB_UUID7 is not None else _uuid7()


_STDLIB_UUID7 = getattr(uuid, "uuid7", None)
