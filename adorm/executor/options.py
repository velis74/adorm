"""Connection options in layers (profile, session, block) and the client-side time limit of adorm operations."""

from __future__ import annotations

import asyncio
import math
import threading

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from types import MappingProxyType, TracebackType
from typing import Any, cast, TYPE_CHECKING

from adorm.drivers.base import is_custom_option
from adorm.exceptions import OperationTimeout, TimeoutPhase
from adorm.executor.slot import carry_cancellation

if TYPE_CHECKING:
    from adorm.executor.pool import Pool
    from adorm.executor.sessions import Session
    from adorm.executor.slot import SlotLease

__all__ = [
    "ADORM_OPTIONS",
    "MAX_TIMEOUT_OPTION",
    "ROLE_OPTION",
    "SEARCH_PATH_OPTION",
    "TIMEOUT_OPTIONS",
    "ResolvedOptions",
    "Timeout",
    "block_options",
    "check_hook_options",
    "check_options",
    "connection_options",
    "resolve_options",
    "session_options",
    "set_session_options",
    "timeout",
]

ADORM_OPTIONS = frozenset({"acquire_timeout", "commit_timeout"})
"""Options adorm applies itself; they never reach a driver."""

TIMEOUT_OPTIONS = frozenset({"statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout"})
"""Database options whose value is a number of seconds (``0`` for no limit), at most `MAX_TIMEOUT_OPTION`."""

MAX_TIMEOUT_OPTION = 2147483.647
"""The largest value of a `TIMEOUT_OPTIONS` option: PostgreSQL's limit of 2147483647 milliseconds."""

ROLE_OPTION = "role"
"""The database option naming the role the connection acts as (PostgreSQL's ``SET ROLE``): a non-empty string."""

SEARCH_PATH_OPTION = "search_path"
"""The database option naming the schemas unqualified names resolve in, in order (PostgreSQL's ``search_path``): a
non-empty list or tuple of non-empty strings, kept as a tuple."""


_EMPTY: Mapping[str, Any] = MappingProxyType({})


def check_timeout(value: Any, what: str) -> float:
    """``value`` as a float; `ValueError` unless it is a positive, finite number of seconds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0 or math.isinf(value):
        raise ValueError(f"{what} is a positive, finite number of seconds, not {value!r}")
    return float(value)


def _check_text(name: str, value: Any) -> Any:
    if value is not None and (not isinstance(value, str) or "\x00" in value):
        raise ValueError(f"{name} is a string without NUL or None, not {value!r}")
    return value


def _check_search_path(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return None
    if (
        not isinstance(value, (list, tuple))
        or not value
        or not all(isinstance(schema, str) and schema and "\x00" not in schema for schema in value)
    ):
        raise ValueError(
            f"search_path is a non-empty list or tuple of schema names, non-empty strings without NUL, or None, "
            f"not {value!r}"
        )
    return tuple(value)


def _check_option(name: Any, value: Any) -> Any:
    if not isinstance(name, str) or not (name.isidentifier() or is_custom_option(name)):
        raise ValueError(
            f"a connection option name is an identifier or a custom option name (app.user_id), not {name!r}"
        )
    if is_custom_option(name):
        return _check_text(name, value)
    if name == ROLE_OPTION:
        if value == "":
            raise ValueError("role is a non-empty string or None, not ''")
        return _check_text(name, value)
    if name == SEARCH_PATH_OPTION:
        return _check_search_path(value)
    if name == "acquire_timeout":
        return check_timeout(value, name)
    if name == "commit_timeout":
        return None if value is None else check_timeout(value, name)
    if name in TIMEOUT_OPTIONS and value is not None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= MAX_TIMEOUT_OPTION:
            raise ValueError(f"{name} is a number of seconds from 0 to {MAX_TIMEOUT_OPTION} or None, not {value!r}")
        return value
    return value


def check_options(options: Mapping[str, Any]) -> Mapping[str, Any]:
    """``options`` checked, as a read-only mapping. Raises `ValueError` for a name that is neither an identifier nor a
    custom option name (`is_custom_option()`), for an
    ``acquire_timeout`` that is not a positive, finite number of seconds, for a ``commit_timeout`` that is neither
    that nor ``None``, for a value of `TIMEOUT_OPTIONS` that is neither a number of seconds from 0 to
    `MAX_TIMEOUT_OPTION` nor ``None``, for a ``role`` that is neither a non-empty string without NUL nor ``None``, for a
    ``search_path`` that is neither a non-empty list or tuple of such strings (kept as a tuple) nor ``None`` and for a
    custom option (`is_custom_option()`) whose value is neither a string without NUL nor ``None``. Other values
    are checked by the driver that sets them."""
    if not isinstance(options, Mapping):
        raise TypeError(f"connection options are a mapping, not {type(options).__name__}")
    return MappingProxyType({name: _check_option(name, value) for name, value in options.items()})


def check_profile_options(options: Mapping[str, Any] | None, profile_name: str) -> Mapping[str, Any]:
    """The database options of a connection profile, checked; the adorm options belong to its pool settings."""
    checked = check_options(options or {})
    misplaced = sorted(ADORM_OPTIONS & checked.keys())
    if misplaced:
        raise ValueError(
            f"{', '.join(misplaced)} of profile {profile_name!r} is a pool setting, not a connection option"
        )
    return checked


def check_hook_options(result: Any, hook: str, profile_name: str) -> Mapping[str, Any]:
    """The database options a profile hook returned, checked (`check_options()`): ``None`` for none, else a mapping
    without adorm options. Raises `TypeError` for another value and `ValueError` as `check_options()`."""
    if result is None:
        return _EMPTY
    if not isinstance(result, Mapping):
        raise TypeError(
            f"{hook} of profile {profile_name!r} returns a mapping of connection options or None, "
            f"not {type(result).__name__}"
        )
    checked = check_options(result)
    misplaced = sorted(ADORM_OPTIONS & checked.keys())
    if misplaced:
        raise ValueError(f"{hook} of profile {profile_name!r} cannot set {', '.join(misplaced)}")
    return checked


# Layers

_block: ContextVar[Mapping[str, Any]] = ContextVar("adorm_connection_options", default=_EMPTY)
_sessions: dict[tuple[str, str], Mapping[str, Any]] = {}
_sessions_lock = threading.Lock()


@contextmanager
def connection_options(**options: Any) -> Iterator[Mapping[str, Any]]:
    """Set connection options for the ``with`` block (a synchronous ``with``: it only sets a context variable).

    The block's options replace those of enclosing blocks, of the session and of the profile with the same names;
    tasks created inside the block inherit them. ``None`` for a database option means the database's default. The
    value bound by ``as`` is the block layer in effect inside the block. Raises `ValueError` as `check_options()`.
    """
    checked = check_options(options)
    layer = MappingProxyType({**_block.get(), **checked})
    token = _block.set(layer)
    try:
        yield layer
    finally:
        _block.reset(token)


def block_options() -> Mapping[str, Any]:
    """The options of the enclosing `connection_options()` blocks of the current context."""
    return _block.get()


def session_options(session: Session) -> Mapping[str, Any]:
    """The options set on ``session`` (`set_session_options()`)."""
    return _sessions.get(session.key, _EMPTY)


def set_session_options(session: Session, options: Mapping[str, Any]) -> Mapping[str, Any]:
    """Replace the options of ``session``, the pair ``(using, session_name)``; an empty mapping removes them. Returns
    the checked options. Thread-safe."""
    checked = check_options(options)
    with _sessions_lock:
        if checked:
            _sessions[session.key] = checked
        else:
            _sessions.pop(session.key, None)
    return checked


@dataclass(frozen=True, slots=True)
class ResolvedOptions:
    """The options in effect for a session on a pool: the adorm options and the database options to set, without
    those whose value is ``None`` (the database's default). ``profile`` is the profile's layer of database options
    and ``overrides`` the session's and the blocks' layers above it, ``None`` values included."""

    acquire_timeout: float
    commit_timeout: float | None
    database: Mapping[str, Any]
    profile: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)
    overrides: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)

    def for_lease(self, lease: SlotLease) -> Mapping[str, Any]:
        """The database options to set on ``lease``'s slot: ``database`` with the options its profile's
        ``on_acquire`` hook returned (`SlotLease.hook_options`) between the profile's layer and the session's."""
        hook = lease.hook_options
        if not hook:
            return self.database
        merged = {**self.profile, **hook, **self.overrides}
        return MappingProxyType({name: value for name, value in merged.items() if value is not None})


def resolve_options(session: Session, pool: Pool) -> ResolvedOptions:
    """The options in effect in the current context for ``session`` on ``pool``: the profile's options (its pool's
    ``acquire_timeout`` and ``commit_timeout`` and ``pool.options``), replaced by the session's, replaced by those of
    the enclosing blocks."""
    overrides = {**session_options(session), **_block.get()}
    acquire_timeout = overrides.pop("acquire_timeout", pool.acquire_timeout)
    commit_timeout = overrides.pop("commit_timeout", pool.commit_timeout)
    merged = {**pool.options, **overrides}
    database = MappingProxyType({name: value for name, value in merged.items() if value is not None})
    return ResolvedOptions(acquire_timeout, commit_timeout, database, pool.options, MappingProxyType(overrides))


# Client-side time limit

_anchors: ContextVar[tuple[Timeout, ...]] = ContextVar("adorm_timeouts", default=())


class _Progress:
    """What an adorm operation of a timeout block is doing."""

    __slots__ = ("error", "operation", "phase", "session")

    def __init__(self, phase: TimeoutPhase, operation: Any, session: Session | None) -> None:
        self.phase = phase
        self.operation = operation
        self.session = session
        self.error: BaseException | None = None  # the exception that interrupted the operation


@contextmanager
def tracking(phase: TimeoutPhase, operation: Any, session: Session | None) -> Iterator[None]:
    """Record for the enclosing `timeout()` blocks that ``operation`` of ``session`` is in ``phase`` during the
    ``with`` block. A block that has expired keeps the first record whose ``with`` block an exception left."""
    anchors = _anchors.get()
    if not anchors:
        yield
        return
    progress = _Progress(phase, operation, session)
    for anchor in anchors:
        anchor._active.append(progress)
    try:
        yield
    except BaseException as exc:
        progress.error = exc
        for anchor in anchors:
            if anchor._interrupted is None and anchor.expired():
                anchor._interrupted = progress
        raise
    finally:
        for anchor in anchors:
            anchor._active.remove(progress)


class Timeout:
    """A time limit for the adorm operations of an ``async with`` block: ``async with adorm.timeout(5) as t:``.

    It wraps `asyncio.timeout()`; on expiry the block raises `OperationTimeout` instead of ``TimeoutError``.
    Operations of the block, including those of tasks that inherit its context, report what they do to it:
    `OperationTimeout.phase` is ``"waiting_for_slot"`` or ``"executing"``, `OperationTimeout.operation` the operation
    (an adorm `Operation`, or the name of a transaction request such as ``"begin"`` or ``"commit"``) and
    `OperationTimeout.session` its session. The operation is the one the expiry interrupted, else the oldest one
    still in progress; both are ``None`` when no operation was in progress. `cancellation_outcome()` of the
    ``OperationTimeout`` is the interrupted operation's outcome, also when it ran in a child task.
    """

    __slots__ = ("_active", "_delay", "_interrupted", "_timeout", "_token")

    def __init__(self, delay: float | None) -> None:
        if delay is not None and (isinstance(delay, bool) or not isinstance(delay, (int, float)) or math.isnan(delay)):
            raise ValueError(f"a timeout is a number of seconds or None, not {delay!r}")
        self._delay = delay
        self._timeout: asyncio.Timeout | None = None
        self._token: Token | None = None
        self._active: list[_Progress] = []
        self._interrupted: _Progress | None = None

    def __repr__(self) -> str:
        if self._timeout is None:
            return f"<adorm.Timeout delay={self._delay!r}, not entered>"
        state = "expired" if self.expired() else "active"
        return f"<adorm.Timeout when={self.when()!r}, {state}>"

    def when(self) -> float | None:
        """The deadline in the event loop's time, ``None`` for no limit or before the block is entered."""
        return None if self._timeout is None else self._timeout.when()

    def reschedule(self, when: float | None) -> None:
        """Move the deadline to ``when`` (the event loop's time, ``None`` for no limit). Raises `RuntimeError` before
        the block is entered."""
        if self._timeout is None:
            raise RuntimeError("the timeout block has not been entered")
        self._timeout.reschedule(when)

    def expired(self) -> bool:
        """Whether the deadline passed and cancelled the block."""
        return self._timeout is not None and self._timeout.expired()

    async def __aenter__(self) -> Timeout:
        if self._timeout is not None:
            raise RuntimeError("a timeout block can be entered once")
        self._timeout = asyncio.timeout(self._delay)
        await self._timeout.__aenter__()
        self._token = _anchors.set((*_anchors.get(), self))
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> bool | None:
        _anchors.reset(self._token)  # type: ignore[arg-type]
        timeout = cast("asyncio.Timeout", self._timeout)
        try:
            return await timeout.__aexit__(exc_type, exc, tb)
        except TimeoutError as error:
            if not timeout.expired():
                raise
            raise self._error() from error

    def _error(self) -> OperationTimeout:
        progress = self._interrupted or (self._active[0] if self._active else None)
        if progress is None:
            return OperationTimeout(timeout=self._delay)
        error = OperationTimeout(
            phase=progress.phase, operation=progress.operation, session=progress.session, timeout=self._delay
        )
        if progress.error is not None:
            carry_cancellation(progress.error, error)
        return error


def timeout(delay: float | None) -> Timeout:
    """A `Timeout` block of ``delay`` seconds (``None`` for no limit): ``async with adorm.timeout(5):``."""
    return Timeout(delay)
