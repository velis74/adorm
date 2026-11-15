"""The executor's debug switch."""

from __future__ import annotations

import asyncio

__all__ = ["debug_enabled", "set_debug"]

_debug: bool | None = None


def set_debug(value: bool | None) -> bool | None:
    """Turn the executor's debug checks on or off; ``None`` follows the running event loop's debug mode. Returns the
    previous value."""
    global _debug
    if value is not None and not isinstance(value, bool):
        raise TypeError(f"debug is a bool or None, not {value!r}")
    previous, _debug = _debug, value
    return previous


def debug_enabled() -> bool:
    """Whether the executor's debug checks run: the value of `set_debug()`, or else the running event loop's debug
    mode."""
    if _debug is not None:
        return _debug
    try:
        return asyncio.get_running_loop().get_debug()
    except RuntimeError:
        return False
