"""Slots: exclusive channels to one driver connection each, and the leases that hand a slot to a flow."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import enum
import itertools
import logging
import threading

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, cast, TYPE_CHECKING

from adorm.events import (
    BackendEvent,
    CallbackEvent,
    CANCELLED_BEFORE_START,
    CancelledEvent,
    ErrorEvent,
    Event,
    EventChannel,
    ON_COMMIT,
    ON_ROLLBACK,
    RESERVED_CALLBACKS,
    ResultEvent,
)
from adorm.exceptions import InterfaceError, TransactionClosed

if TYPE_CHECKING:
    from adorm.drivers.base import DriverConnection
    from adorm.executor.pool import Pool
    from adorm.executor.sessions import Session
    from adorm.executor.turns import Turn

__all__ = [
    "REQUEST_METHODS",
    "CancellationOutcome",
    "CloseReason",
    "Request",
    "RequestQueue",
    "RunningRequest",
    "Slot",
    "SlotLease",
    "cancellation_outcome",
    "cancelled_before_start",
    "on_request_started",
    "running_requests",
]

logger = logging.getLogger("adorm.executor")

REQUEST_METHODS = frozenset(
    {
        "execute",
        "begin",
        "commit",
        "rollback",
        "savepoint",
        "release_savepoint",
        "rollback_to_savepoint",
        "set_options",
        "listen",
        "unlisten",
    }
)
"""The `DriverConnection` methods a request can call."""

_request_ids = itertools.count(1)


class CancellationOutcome(enum.StrEnum):
    """What a cancelled request did in the database."""

    NOT_STARTED = "not_started"
    """It never reached the driver connection: it had no effect."""
    SUCCEEDED = "succeeded"
    """It finished with a result before the cancellation reached it: it took effect."""
    FAILED = "failed"
    """It finished with an error before the cancellation reached it: it had no effect."""
    UNKNOWN = "unknown"
    """It was cancelled while running, or failed with a lost connection: it may or may not have taken effect."""


class CloseReason(enum.StrEnum):
    """Why a slot closed its connection and left the pool."""

    DISCARDED = "discarded"
    """Its lease was released with ``discard=True``."""
    ABORTED = "aborted"
    """It rolled back a transaction that received no request within the lease's ``commit_timeout``."""
    UNCLEAN_END = "unclean_end"
    """Its lease ended inside a transaction: no successful ``commit`` or ``rollback`` ended it."""
    CONNECTION_ERROR = "connection_error"
    """A request failed with an error that leaves the connection unusable (`DriverConnection.is_connection_lost()`)."""
    CUSTOM_SQL = "custom_sql"
    """The connection ran SQL adorm did not generate (`DriverConnection.ran_custom_sql`)."""
    LISTENING = "listening"
    """The connection still listened for notifications when its lease ended (`DriverConnection.listening`)."""
    UNUSABLE = "unusable"
    """The connection reported itself unusable, or the slot stopped serving requests."""
    UNSETTLED = "unsettled"
    """The connection did not finish the work of its last call within the pool's ``release_timeout``
    (`DriverConnection.settle()`)."""
    HOOK_FAILED = "hook_failed"
    """Setting the session's options and running the profile's ``on_acquire`` hook, or running its ``on_release``
    hook, failed or timed out."""
    STALE = "stale"
    """It stayed idle for the pool's ``purge_stale_connections_at`` seconds."""
    POOL_CLOSED = "pool_closed"
    """Its pool was closed."""


_OUTCOME = "_adorm_cancellation_outcome"


class _Outcome:
    """The outcome of a cancelled request, refined when the slot delivers the request's own outcome later."""

    __slots__ = ("value",)

    def __init__(self, value: CancellationOutcome) -> None:
        self.value = value

    def follow(self, future: asyncio.Future) -> None:
        """Take the outcome of ``future``, the request's channel future, now or once it is settled."""
        if future.done():
            self._settle(future)
        else:
            future.add_done_callback(self._settle)

    def _settle(self, future: asyncio.Future) -> None:
        if future.cancelled():
            # A CancelledEvent: the request never started, or was cancelled while it ran.
            try:
                future.result()
            except asyncio.CancelledError as cancelled:
                if cancelled.args == (CANCELLED_BEFORE_START,):
                    self.value = CancellationOutcome.NOT_STARTED
            return
        error = future.exception()  # also marks the exception as retrieved
        self.value = CancellationOutcome.SUCCEEDED if error is None else failure_outcome(error)


def failure_outcome(error: BaseException) -> CancellationOutcome:
    """The `CancellationOutcome` of a request that failed with ``error``: `UNKNOWN` for a lost connection."""
    from adorm.drivers.base import is_connection_error

    return CancellationOutcome.UNKNOWN if is_connection_error(error) else CancellationOutcome.FAILED


def mark_cancellation(exc: BaseException, outcome: CancellationOutcome) -> _Outcome:
    """Record ``outcome`` on ``exc``, a ``CancelledError``, unless it already carries one; return the record."""
    record = getattr(exc, _OUTCOME, None)
    if record is None:
        record = _Outcome(outcome)
        setattr(exc, _OUTCOME, record)
    return record


def carry_cancellation(source: BaseException, target: BaseException) -> None:
    """Give ``target``, a later ``CancelledError`` of the same operation, the outcome record of ``source``."""
    record = getattr(source, _OUTCOME, None)
    if record is not None and getattr(target, _OUTCOME, None) is None:
        setattr(target, _OUTCOME, record)


def cancellation_outcome(exc: BaseException) -> CancellationOutcome | None:
    """The `CancellationOutcome` of the adorm operation or request whose cancellation raised ``exc``.

    ``exc`` is the ``CancelledError`` itself or an exception it caused, such as the ``TimeoutError`` of
    ``asyncio.timeout()``. ``None`` when no cancelled operation or request raised it. `UNKNOWN` becomes the
    request's actual outcome when the slot delivers it later; it is final once the slot has finished the request,
    which releasing the lease waits for.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        record = getattr(current, _OUTCOME, None)
        if record is not None:
            return record.value
        cause = current.__cause__
        if (
            cause is None
            and isinstance(current, TimeoutError)
            and isinstance(current.__context__, asyncio.CancelledError)
        ):
            # Python 3.11's asyncio.timeout() leaves the CancelledError on __context__ only.
            cause = current.__context__
        current = cause
    return None


def cancelled_before_start(exc: BaseException) -> bool:
    """Whether ``cancellation_outcome(exc)`` is `CancellationOutcome.NOT_STARTED`: ``exc`` is the cancellation of an
    operation or request that never reached the driver connection."""
    return cancellation_outcome(exc) is CancellationOutcome.NOT_STARTED


@dataclass(frozen=True, slots=True)
class Request:
    """A call of the driver connection's ``method`` with ``args``; its outcome event carries ``request_id``.

    ``options``, when not ``None``, are the database connection options the call runs with: before the call the slot
    sets those that differ from the values it set on the connection and resets to the default those it set that
    ``options`` lacks (`Slot`).
    """

    method: str
    args: tuple[Any, ...] = ()
    request_id: Any = field(default_factory=lambda: next(_request_ids))
    options: Mapping[str, Any] | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        if self.method not in REQUEST_METHODS:
            raise ValueError(f"unknown request method {self.method!r}; one of {', '.join(sorted(REQUEST_METHODS))}")
        if not isinstance(self.args, tuple):
            object.__setattr__(self, "args", tuple(self.args))
        if self.options is not None:
            if not isinstance(self.options, Mapping):
                raise TypeError(f"request options are a mapping or None, not {type(self.options).__name__}")
            object.__setattr__(self, "options", MappingProxyType(dict(self.options)))


@dataclass(frozen=True, slots=True, eq=False)
class RunningRequest:
    """A request whose call of the driver connection has started: the slot has set its connection options and handed
    it to the connection (`running_requests()`, `on_request_started()`).

    ``sender`` is the task that sent the request (``None`` when none did), ``session`` the session of its lease. On a
    connection that shares its database session with others, the call may still wait for that session
    (`in_session`, `reached_session`).
    """

    request: Request
    session: Session
    sender: asyncio.Task | None
    slot: Slot
    _holds: int = field(default=0, init=False, repr=False)
    _reached: bool = field(default=False, init=False, repr=False)

    @property
    def operation(self) -> Any:
        """The operation of an ``execute`` request, ``None`` for the other methods."""
        if self.request.method == "execute" and self.request.args:
            return self.request.args[0]
        return None

    @property
    def backend_pid(self) -> int | None:
        """The slot's `DriverConnection.backend_pid`."""
        connection = self.slot.connection
        return None if connection is None else connection.backend_pid

    @property
    def running(self) -> bool:
        """Whether the call has not ended yet."""
        return self.slot.running is self

    @property
    def in_session(self) -> bool:
        """Whether the call runs and has its database session now (`DriverConnection.holds_session`)."""
        connection = self.slot.connection
        return self.running and connection is not None and connection.holds_session

    @property
    def reached_session(self) -> bool:
        """Whether the call has had its database session, now or before it ended: always for a connection with a
        session of its own (`DriverConnection.session_holds`)."""
        if not self.running:
            return self._reached
        connection = self.slot.connection
        return connection is not None and self._has_held(connection)

    def _has_held(self, connection: DriverConnection) -> bool:
        return connection.holds_session or connection.session_holds > self._holds

    def __repr__(self) -> str:
        what = self.request.method if self.operation is None else f"{self.request.method} {self.operation!r}"
        return f"<RunningRequest {what} of {self.session!r} on slot {self.slot.slot_id}>"


_running_lock = threading.Lock()
_running: dict[RunningRequest, asyncio.AbstractEventLoop] = {}
_watchers: dict[object, tuple[asyncio.AbstractEventLoop, Callable[[RunningRequest], None]]] = {}


def running_requests() -> tuple[RunningRequest, ...]:
    """The requests whose driver connection call runs now on a slot of the running event loop, in the order they
    started."""
    loop = asyncio.get_running_loop()
    with _running_lock:
        return tuple(running for running, owner in _running.items() if owner is loop)


def on_request_started(callback: Callable[[RunningRequest], None]) -> Callable[[], None]:
    """Call ``callback(running)`` whenever the driver connection call of a request starts on a slot of the running
    event loop; return a function that stops it.

    The callback runs on the loop, in the slot's task, before the connection method runs; an exception it raises is
    logged."""
    if not callable(callback):
        raise TypeError(f"a request callback is callable, not {callback!r}")
    loop = asyncio.get_running_loop()
    key = object()
    with _running_lock:
        _watchers[key] = (loop, callback)

    def stop() -> None:
        with _running_lock:
            _watchers.pop(key, None)

    return stop


def _report_started(running: RunningRequest) -> None:
    loop = asyncio.get_running_loop()
    with _running_lock:
        _running[running] = loop
        callbacks = [callback for owner, callback in _watchers.values() if owner is loop]
    for callback in callbacks:
        try:
            callback(running)
        except Exception:
            logger.exception("request callback %r failed for %r", callback, running)


def _report_ended(running: RunningRequest) -> None:
    with _running_lock:
        _running.pop(running, None)


class RequestQueue:
    """The writing end of a lease: requests put here run on the slot one at a time, in order."""

    __slots__ = ("_lease",)

    def __init__(self, lease: SlotLease) -> None:
        self._lease = lease

    def put_nowait(self, request: Request) -> None:
        """Queue ``request``. Raises `TransactionClosed` when the slot aborted the lease, `InterfaceError` after the
        lease is released and `TypeError` for anything but a `Request`."""
        self._lease._send(request)

    async def put(self, request: Request) -> None:
        """Queue ``request``; the queue is unbounded, so it never waits."""
        self._lease._send(request)

    def cancel(self, request: Request) -> bool:
        """Cancel ``request``: a queued one never runs, a running one is cancelled in the driver connection. Its
        outcome on ``events`` is a `CancelledEvent`, or its result or error when it finished before the cancellation
        reached it. Returns whether it was still queued; a request that is neither queued nor running is left alone."""
        return self._lease.slot._cancel(request)

    async def join(self) -> None:
        """Wait until every request queued so far has run."""
        await self._lease.slot._drained(self._lease)

    def qsize(self) -> int:
        """How many queued requests have not started yet."""
        return len(self._lease.slot._queue)


class SlotLease:
    """A slot handed to one flow: a request queue to write to and an event channel to read from.

    ``requests`` takes `Request` objects. ``channel`` is the lease's `EventChannel`; its queue ``events`` receives
    the outcome of each request (`ResultEvent`, `ErrorEvent` or `CancelledEvent`) and the callbacks and backend
    events the slot and its connection deliver while the lease is held, except what the channel interprets itself:
    the outcome of a request sent with `call()` is returned, and a callback with a handler registered with
    ``channel.on()`` runs it. ``owner`` is the task that acquired the lease. ``commit_timeout`` is the number of
    seconds the slot waits for the next request of the lease inside a transaction (``None`` for no limit); it
    starts as the pool's and `Router.acquire()` sets the ``commit_timeout`` connection option in effect.
    ``hook_options`` are the database connection options the profile's ``on_acquire`` hook returned for the lease,
    the layer between the profile's options and the session's (`ResolvedOptions.for_lease()`).
    """

    __slots__ = (
        "_aborted",
        "_bound",
        "_released",
        "_turn",
        "channel",
        "commit_timeout",
        "hook_options",
        "owner",
        "pool",
        "requests",
        "session",
        "slot",
    )

    def __init__(self, slot: Slot, session: Session, owner: asyncio.Task | None) -> None:
        self.slot = slot
        self.pool: Pool = slot.pool
        self.session = session
        self.owner = owner
        self.requests = RequestQueue(self)
        self.channel = EventChannel(slot.pool.events)
        self.commit_timeout: float | None = slot.pool.commit_timeout
        self.hook_options: Mapping[str, Any] = MappingProxyType({})
        self._released = False
        self._aborted: str | None = None
        self._bound = False  # released only by ``owner``
        self._turn: Turn | None = None  # given up once the slot is returned

    @property
    def events(self) -> asyncio.Queue[Event]:
        """The queue of events nobody claimed: ``channel.events``."""
        return self.channel.events

    @property
    def released(self) -> bool:
        """Whether `release()` was called or the slot aborted the lease's transaction."""
        return self._released

    @property
    def aborted(self) -> bool:
        """Whether the slot rolled back the lease's transaction and ended the lease (``commit_timeout``)."""
        return self._aborted is not None

    def __repr__(self) -> str:
        state = "released" if self._released else "held"
        return f"<SlotLease {self.slot.slot_id} of {self.pool.name!r} for {self.session!r}, {state}>"

    def _check_open(self) -> None:
        if self._aborted is not None:
            raise TransactionClosed(self._aborted)
        if self._released:
            raise InterfaceError(f"{self!r} cannot take requests")

    def _send(self, request: Request) -> None:
        self._check_open()
        if not isinstance(request, Request):
            raise TypeError(f"a lease takes Request objects, not {type(request).__name__}")
        self.slot._enqueue(self, request)

    async def call(self, method: str, *args: Any, options: Mapping[str, Any] | None = None) -> Any:
        """Send ``Request(method, args, options=options)`` and return its result or raise its error.

        Cancelling the caller cancels the request: a queued one never runs, a running one is cancelled in the
        driver connection. The raised ``CancelledError`` carries the request's `CancellationOutcome`
        (`cancellation_outcome()`). A `CancelledEvent` for a request the caller did not cancel raises
        `InterfaceError`. A lease that is released raises `InterfaceError`, one the slot aborted `TransactionClosed`.
        """
        request = Request(method, args, options=options)
        self._check_open()
        future = self.channel.expect(request.request_id)
        try:
            self._send(request)
        except BaseException:
            self.channel.forget(request.request_id)
            raise
        try:
            # Shielded, so that the outcome still arrives after the caller is cancelled.
            return await asyncio.shield(future)
        except asyncio.CancelledError as exc:
            task = asyncio.current_task()
            if future.cancelled() and task is not None and not task.cancelling():
                raise InterfaceError(f"request {request.request_id!r} ({method}) was cancelled by the slot") from None
            if not future.done() and self.slot._cancel(request):
                mark_cancellation(exc, CancellationOutcome.NOT_STARTED)
            else:
                mark_cancellation(exc, CancellationOutcome.UNKNOWN).follow(future)
            raise

    async def release(self, *, discard: bool = False) -> None:
        """Return the slot to the pool once every queued request has run; a second call does nothing.

        ``discard`` closes the slot's connection instead, as does a connection that is no longer usable or that a
        ``begin`` left in a transaction not ended by a successful ``commit`` or ``rollback``. The return completes
        even when the caller is cancelled meanwhile. A slot that stays in the pool first runs the profile's
        ``on_release`` hook within the pool's ``release_timeout``; when the hook fails or times out, the slot leaves
        the pool. The lease's channel then closes: callbacks and backend events left unread on ``events`` go to the
        pool's `EventBus`.
        """
        await self._release(CloseReason.DISCARDED if discard else None)

    async def _release(self, reason: CloseReason | None) -> None:
        """`release()`; a ``reason`` closes the slot with that `CloseReason`."""
        if self._released:
            return
        self._released = True
        await self.slot._finish(self, reason)

    def _returned(self) -> None:
        """Give up the lease's turn (`adorm.executor.turns.Turn`) once the slot has been returned or has left the
        pool."""
        turn, self._turn = self._turn, None
        if turn is not None:
            turn.release()

    async def _wait_returned(self) -> None:
        """Wait until the slot has finished returning this released lease, through any cancellation meanwhile."""
        finisher = self.slot._finisher
        while finisher is not None and not finisher.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait({finisher})

    async def __aenter__(self) -> SlotLease:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.release()


_IDLE, _LEASED, _FINISHING, _CLOSED = "idle", "leased", "finishing", "closed"

_Queued = tuple[SlotLease, Request, contextvars.Context, asyncio.Task | None]

_UNKNOWN = object()  # an option value a failed or cancelled set_options() may or may not have set


class Slot:
    """An exclusive channel to one driver connection of a pool.

    A task of its own runs the requests of the current lease one at a time, in order, and delivers each outcome
    to the lease's channel: a `CancelledEvent` for a request cancelled before it finished, and a `CallbackEvent`
    named `ON_COMMIT` or `ON_ROLLBACK` before the outcome of a successful ``commit`` or ``rollback``. When that task
    fails, the queued requests and those sent later fail with `InterfaceError` and the slot becomes unusable. Events
    the connection emits go to the current lease's channel, or to the pool's `EventBus` while no lease holds the slot;
    only `CallbackEvent` and `BackendEvent` values are accepted, and a `CallbackEvent` with a name in
    `RESERVED_CALLBACKS` is dropped.

    When the lease is inside a transaction and no request arrives within the lease's ``commit_timeout``, the slot
    rolls the transaction back, delivers ``CallbackEvent(ON_ROLLBACK)``, ends the lease (later requests raise
    `TransactionClosed`) and leaves the pool. A rollback that does not finish within ``commit_timeout`` either is
    cancelled in the driver connection.

    The slot remembers the database options it set on the connection. Before a request with ``options`` it calls
    the connection's ``set_options()`` with the options whose value differs from the remembered one, ``None`` for
    each remembered option the request lacks, and nothing when all match. Outside a transaction it sets them for the
    connection (``local=False``); inside one with ``local=True`` (``SET LOCAL``), remembered until the transaction
    ends and restored by a rollback to a savepoint. An option whose setting failed or was cancelled is set again by
    the next request with options. A new connection starts with the options the driver sets on it
    (`Driver.connection_defaults()`). After a profile hook ran custom SQL, every remembered option counts as unknown
    and is set again.

    When its lease ends, the slot waits for the connection to settle (`DriverConnection.settle()`; the release does not
    wait for it), then leaves the pool, closing the connection, when the lease was released with ``discard``, the
    lease ended inside a transaction, a request failed with an error that leaves the connection unusable, the
    connection ran custom SQL (`DriverConnection.ran_custom_sql`), even after a commit, the connection still listens
    for notifications (`DriverConnection.listening`), or the connection is no longer usable. ``close_reason`` tells
    which. Acquiring the slot does nothing to the connection.
    """

    _ids = itertools.count(1)

    def __init__(self, pool: Pool) -> None:
        self.pool = pool
        self.slot_id = next(self._ids)
        self.connection: DriverConnection | None = None
        self.lease: SlotLease | None = None
        self.state = _IDLE
        self._queue: deque[_Queued] = deque()
        self._wakeup = asyncio.Event()
        self._running: tuple[Request, asyncio.Task] | None = None
        self._current: _Queued | None = None
        self._started: RunningRequest | None = None
        self._cancelled: Request | None = None
        self._in_transaction = False
        self._options: dict[str, Any] = {}  # set for the connection
        self._local_options: dict[str, Any] = {}  # set for the open transaction
        self._savepoint_options: list[tuple[str, dict[str, Any]]] = []
        self._idle = asyncio.Event()
        self._idle.set()
        self._server: asyncio.Task | None = None
        self._finisher: asyncio.Task | None = None
        self._broken = False
        self.idle_since: float | None = None
        self.close_reason: CloseReason | None = None
        self._closed = asyncio.Event()

    def __repr__(self) -> str:
        return f"<Slot {self.slot_id} of {self.pool.name!r}, {self.state}>"

    async def wait_closed(self) -> None:
        """Wait until the slot is closed (`close()`)."""
        await self._closed.wait()

    @property
    def running(self) -> RunningRequest | None:
        """The request whose driver connection call runs now, ``None`` when none does."""
        return self._started

    async def open(self) -> None:
        """Open the driver connection and start serving requests."""
        self.connection = await self.pool.driver.open_connection(self.pool.profile, self._emit)
        self._options = dict(self.pool.connection_defaults)
        self._server = asyncio.get_running_loop().create_task(
            self._serve(), name=f"adorm-slot-{self.slot_id}", context=contextvars.Context()
        )

    def is_usable(self) -> bool:
        """Whether the slot can serve another lease."""
        return (
            self.state != _CLOSED
            and not self._broken
            and self.connection is not None
            and self.connection.is_usable()
            and self._server is not None
            and not self._server.done()
        )

    def _lease_to(self, session: Session, owner: asyncio.Task | None) -> SlotLease:
        self.lease = SlotLease(self, session, owner)
        self.state = _LEASED
        return self.lease

    def _emit(self, event: Event) -> None:
        if not isinstance(event, (CallbackEvent, BackendEvent)):
            logger.error(
                "%r emitted %r; a connection emits only callbacks and backend events; dropped", self.connection, event
            )
            return
        if isinstance(event, CallbackEvent) and event.name in RESERVED_CALLBACKS:
            logger.error("%r emitted %r, a callback only the slot delivers; dropped", self.connection, event)
            return
        lease = self.lease
        if lease is not None and not lease.released:
            self._deliver(lease, event)
        else:
            self.pool.events.dispatch(event)

    def _deliver(self, lease: SlotLease, event: Event) -> None:
        """Hand ``event`` to ``lease``'s channel."""
        lease.channel.deliver(event)

    def _enqueue(self, lease: SlotLease, request: Request) -> None:
        if self.state == _CLOSED or self._server is None or self._server.done():
            self._deliver(lease, ErrorEvent(request.request_id, InterfaceError(f"{self!r} serves no requests")))
            return
        self._queue.append((lease, request, contextvars.copy_context(), asyncio.current_task()))
        self._idle.clear()
        self._wakeup.set()

    def _cancel(self, request: Request) -> bool:
        """Cancel ``request``; return whether it was still queued. A queued request never runs and gets
        ``CancelledEvent(started=False)`` at once; a running one is cancelled in the driver connection."""
        for index, (lease, queued, _context, _sender) in enumerate(self._queue):
            if queued is request:
                del self._queue[index]
                if not self._queue and self._running is None:
                    self._idle.set()
                self._deliver(lease, CancelledEvent(request.request_id, started=False))
                return True
        running = self._running
        if running is not None and running[0] is request:
            self._cancelled = request
            running[1].cancel()
        return False

    async def _drained(self, lease: SlotLease) -> None:  # noqa: ARG002 - one lease at a time
        await self._idle.wait()

    async def _serve(self) -> None:
        try:
            while True:
                if not self._queue:
                    self._idle.set()
                    self._wakeup.clear()
                    deadline = self._commit_deadline()
                    if deadline is None:
                        await self._wakeup.wait()
                        continue
                    try:
                        async with asyncio.timeout(deadline):
                            await self._wakeup.wait()
                    except TimeoutError:
                        if not self._queue and self._commit_deadline() is not None:
                            await self._abort(deadline)
                    continue
                self._current = self._queue.popleft()
                await self._run(*self._current)
                self._current = None
        except Exception as exc:
            logger.exception("serving %r failed", self)
            self._fail_outstanding(InterfaceError(f"{self!r} stopped serving requests: {exc}"))

    def _fail_outstanding(self, error: InterfaceError) -> None:
        """Answer the running request and every queued one with ``error``."""
        failed = ([self._current] if self._current is not None else []) + list(self._queue)
        self._current = None
        self._queue.clear()
        for lease, request, _context, _sender in failed:
            self._deliver(lease, ErrorEvent(request.request_id, error))
        self._idle.set()

    def _commit_deadline(self) -> float | None:
        """Seconds to wait for the next request of a lease inside a transaction; ``None`` for no limit."""
        lease = self.lease
        if not self._in_transaction or lease is None or lease.released:
            return None
        return lease.commit_timeout

    @property
    def options(self) -> Mapping[str, Any]:
        """The database options in effect on the connection by what the slot set: those it set for the connection,
        replaced inside a transaction by those it set for the transaction. ``None`` stands for the database's
        default; an option whose setting failed is left out."""
        current = {**self._options, **self._local_options} if self._in_transaction else dict(self._options)
        return MappingProxyType({name: value for name, value in current.items() if value is not _UNKNOWN})

    def _option_changes(self, desired: Mapping[str, Any]) -> dict[str, Any]:
        """The options ``set_options()`` must set so that the connection runs with ``desired``."""
        if self._in_transaction:
            current = {**self._options, **self._local_options}
        else:
            current = self._options
        names = sorted(desired.keys() | current.keys())
        return {
            name: desired.get(name)
            for name in names
            if current.get(name) is _UNKNOWN or current.get(name) != desired.get(name)
        }

    async def set_options_for(self, desired: Mapping[str, Any]) -> None:
        """Bring the connection's database options to ``desired`` outside a request (`_set_options()`), as after the
        ``on_acquire`` hook."""
        await self._set_options(desired)

    async def _set_options(self, desired: Mapping[str, Any]) -> None:
        changes = self._option_changes(desired)
        if not changes:
            return
        local = self._in_transaction
        remembered = self._local_options if local else self._options
        remembered.update(dict.fromkeys(changes, _UNKNOWN))
        await cast("DriverConnection", self.connection).set_options(changes, local)
        for name, value in changes.items():
            if value is None and not local:
                del remembered[name]
            else:
                remembered[name] = value

    async def _call(
        self, desired: Mapping[str, Any] | None, coroutine: Any, started: list[bool], running: RunningRequest
    ) -> Any:
        """Set ``desired`` (`_set_options()`) unless it is ``None``, then await ``coroutine``, the request's call;
        ``started[0]`` becomes true and ``running`` is reported once the call starts."""
        if desired is not None:
            await self._set_options(desired)
        started[0] = True
        connection = self.connection
        if connection is not None:
            object.__setattr__(running, "_holds", connection.session_holds)
        self._started = running
        _report_started(running)
        return await coroutine

    def forget_options(self) -> None:
        """Treat every option the slot set as unknown, so the next request with options sets it again (custom SQL
        may have changed it)."""
        self._options = dict.fromkeys(self._options, _UNKNOWN)

    def _end_transaction_options(self) -> None:
        self._local_options = {}
        self._savepoint_options = []

    def _track_options(self, request: Request, succeeded: bool) -> None:
        """Follow the transaction's options through the end of ``request``, a transaction or savepoint call."""
        method = request.method
        if method in ("commit", "rollback") or (method == "begin" and succeeded):
            self._end_transaction_options()
            return
        if not request.args or method not in ("savepoint", "release_savepoint", "rollback_to_savepoint"):
            return
        name = request.args[0]
        index = next((i for i, (saved, _) in enumerate(self._savepoint_options) if saved == name), None)
        if method == "savepoint":
            if succeeded:
                self._savepoint_options.append((name, dict(self._local_options)))
        elif not succeeded:
            if method == "rollback_to_savepoint":
                self._local_options = dict.fromkeys(self._local_options, _UNKNOWN)
        elif index is not None:
            if method == "release_savepoint":
                del self._savepoint_options[index:]
            else:
                self._local_options = dict(self._savepoint_options[index][1])
                del self._savepoint_options[index + 1 :]

    async def _abort(self, deadline: float) -> None:
        """Roll back the transaction of a lease that sent no request in time, end the lease and leave the pool."""
        lease = cast("SlotLease", self.lease)
        lease._aborted = (
            f"slot {self.slot_id} of {self.pool.name!r} rolled back the transaction of {lease.session!r}: "
            f"no request within {deadline:g} s"
        )
        lease._released = True
        logger.warning("%s", lease._aborted)
        connection = cast("DriverConnection", self.connection)
        try:
            async with asyncio.timeout(deadline):
                await connection.rollback()
        except TimeoutError:
            connection.cancel()
            logger.error("rolling back the abandoned transaction of %r did not finish within %g s", self, deadline)
        except Exception:
            logger.exception("rolling back the abandoned transaction of %r failed", self)
        self._end_transaction_options()
        self._deliver(lease, CallbackEvent(ON_ROLLBACK))
        self.state = _FINISHING
        self._finisher = asyncio.get_running_loop().create_task(self._return(lease, CloseReason.ABORTED))

    async def _run(
        self, lease: SlotLease, request: Request, context: contextvars.Context, sender: asyncio.Task | None
    ) -> None:
        connection = cast("DriverConnection", self.connection)
        request_id = request.request_id
        try:
            coroutine = getattr(connection, request.method)(*request.args)
        except Exception as exc:  # wrong arguments
            self._deliver(lease, ErrorEvent(request_id, exc))
            return
        if not asyncio.iscoroutine(coroutine):
            error = InterfaceError(f"{type(connection).__name__}.{request.method}() did not return a coroutine")
            self._deliver(lease, ErrorEvent(request_id, error))
            return
        inner = coroutine
        started = [False]
        running = RunningRequest(request, lease.session, sender, self)
        coroutine = self._call(request.options, inner, started, running)
        # The connection method runs in the context of the flow that sent the request.
        call = asyncio.get_running_loop().create_task(coroutine, context=context)
        # A call cancelled before it started never ran the connection method.
        call.add_done_callback(lambda _call: inner.close())
        self._running = (request, call)
        event: Event
        try:
            event = ResultEvent(request_id, await asyncio.shield(call))
        except asyncio.CancelledError:
            if not call.done():
                # The slot itself is being closed.
                call.cancel()
                raise
            if not call.cancelled():
                event = self._failed(request_id, cast("BaseException", call.exception()))
            elif self._cancelled is request:
                event = CancelledEvent(request_id, started=started[0])
            else:
                event = ErrorEvent(request_id, InterfaceError(f"{request.method}() was cancelled by the driver"))
        except Exception as exc:
            event = self._failed(request_id, exc)
        finally:
            self._running = None
            self._cancelled = None
            if self._started is running and connection is not None:
                object.__setattr__(running, "_reached", running._has_held(connection))
            self._started = None
            _report_ended(running)
        self._track_options(request, isinstance(event, ResultEvent))
        if isinstance(event, ResultEvent):
            if request.method == "begin":
                self._in_transaction = True
            elif request.method in ("commit", "rollback"):
                self._in_transaction = False
                self._deliver(lease, CallbackEvent(ON_COMMIT if request.method == "commit" else ON_ROLLBACK))
        self._deliver(lease, event)

    def _failed(self, request_id: Any, error: BaseException) -> ErrorEvent:
        """The outcome of a request whose connection call raised ``error``; an error that leaves the connection unusable
        (`DriverConnection.is_connection_lost()`) makes the slot leave the pool at the end of the lease."""
        try:
            lost = bool(cast("DriverConnection", self.connection).is_connection_lost(error))
        except Exception:
            logger.exception("%s.is_connection_lost failed", type(self.connection).__name__)
            lost = True
        if lost:
            self._broken = True
        return ErrorEvent(request_id, error)

    async def _finish(self, lease: SlotLease, reason: CloseReason | None) -> None:
        if self.lease is lease and self.state == _CLOSED:
            # Closed while leased (`close()`): the slot already left the pool's service.
            self.lease = None
            lease.channel.close()
            self.pool._removed(self)
            lease._returned()
            return
        if self.lease is not lease or self.state != _LEASED:
            return
        self.state = _FINISHING
        self._finisher = asyncio.get_running_loop().create_task(self._return(lease, reason))
        await asyncio.shield(self._finisher)

    def _end_reason(self) -> CloseReason | None:
        """Why the slot leaves the pool at the end of a lease; ``None`` when it stays."""
        if self.pool.closed:
            return CloseReason.POOL_CLOSED
        if self._broken:
            return CloseReason.CONNECTION_ERROR
        if self._in_transaction:
            return CloseReason.UNCLEAN_END
        connection = self.connection
        if connection is not None and connection.ran_custom_sql:
            return CloseReason.CUSTOM_SQL
        if not self.is_usable():
            return CloseReason.UNUSABLE
        if connection is not None and connection.listening:
            return CloseReason.LISTENING
        return None

    async def _return(self, lease: SlotLease, reason: CloseReason | None) -> None:
        try:
            await self._idle.wait()
        finally:
            self.lease = None
            lease.channel.close()
            connection = self.connection
            if reason is None and connection is not None and not connection.settled and self.is_usable():
                # The connection still works for the last request (Django's on_commit() callbacks): the release
                # completes now, and the slot decides once that work is done; closing the pool waits for it.
                self._finisher = asyncio.get_running_loop().create_task(self._conclude(lease, reason))
                self.pool._track(self._finisher)
            else:
                await self._conclude(lease, reason)

    async def _conclude(self, lease: SlotLease, reason: CloseReason | None) -> None:
        """Return the slot of the ended ``lease`` to the pool, or close it for ``reason`` or the slot's own reason."""
        try:
            try:
                if reason is None:
                    reason = await self._release_reason(lease)
            except BaseException:
                await self._leave(CloseReason.UNUSABLE)
                raise
            if reason is None:
                self.state = _IDLE
                self.pool._returned(self)
            else:
                await self._leave(reason)
        finally:
            lease._returned()

    async def _release_reason(self, lease: SlotLease) -> CloseReason | None:
        """Why the slot leaves the pool once the connection has settled; ``None`` when it stays."""
        settled = await self._settle()
        reason = self._end_reason()
        if reason is None and not settled:
            return CloseReason.UNSETTLED
        if reason is None and self.pool.on_release is not None:
            return self._end_reason() if await self._on_release(lease) else CloseReason.HOOK_FAILED
        return reason

    async def _leave(self, reason: CloseReason) -> None:
        try:
            await self.close(reason)
        finally:
            self.pool._removed(self)

    async def _settle(self) -> bool:
        """Wait, within the pool's ``release_timeout``, until the connection has finished the work of the lease's
        last request (`DriverConnection.settle()`); return whether it did."""
        connection = self.connection
        if connection is None or not self.is_usable():
            return True
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            return False
        timeout = self.pool.release_timeout
        try:
            async with asyncio.timeout(timeout):
                await connection.settle()
        except TimeoutError:
            logger.error("%r did not settle within %g s; the slot leaves the pool", self, timeout)
            return False
        except Exception:
            logger.exception("settling %r failed; the slot leaves the pool", self)
            return False
        return True

    async def _on_release(self, lease: SlotLease) -> bool:
        """Run the profile's ``on_release`` hook for ``lease``'s session within the pool's ``release_timeout``;
        return whether it succeeded."""
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            return False
        connection = cast("DriverConnection", self.connection)
        timeout = self.pool.release_timeout
        try:
            async with asyncio.timeout(timeout):
                await connection.call_hook(self.pool.on_release, self.pool.profile, lease.session)
        except TimeoutError:
            logger.error("on_release of %r did not finish within %g s; the slot leaves the pool", self, timeout)
            return False
        except Exception:
            logger.exception("on_release of %r failed; the slot leaves the pool", self)
            return False
        return True

    async def close(self, reason: CloseReason = CloseReason.POOL_CLOSED) -> None:
        """Stop serving and close the connection; ``close_reason`` becomes ``reason``. The running request, the queued
        ones and those sent later fail with `InterfaceError`; a lease holding the slot frees the pool's capacity when it
        is released. Logs errors instead of raising them; a second call does nothing."""
        if self.state == _CLOSED:
            return
        self.state = _CLOSED
        self.close_reason = CloseReason(reason)
        self.idle_since = None
        self._closed.set()
        logger.debug("%r leaves the pool: %s", self, self.close_reason)
        server = self._server
        if server is not None and not server.done():
            server.cancel()
            try:
                await server
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
            except Exception:
                logger.exception("serving %r failed", self)
        self._fail_outstanding(InterfaceError(f"{self!r} is closed"))
        if self.connection is not None:
            try:
                await self.connection.close()
            except Exception:
                logger.exception("closing the connection of %r failed", self)
