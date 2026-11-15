"""Slots: exclusive channels to one driver connection each, and the leases that hand a slot to a flow."""

from __future__ import annotations

import asyncio
import contextvars
import itertools
import logging

from collections import deque
from dataclasses import dataclass, field
from typing import Any, cast, TYPE_CHECKING

from adorm.events import (
    BackendEvent,
    CallbackEvent,
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

__all__ = ["REQUEST_METHODS", "Request", "RequestQueue", "Slot", "SlotLease", "cancelled_before_start"]

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
    }
)
"""The `DriverConnection` methods a request can call."""

_request_ids = itertools.count(1)

_UNSENT = "_adorm_cancelled_before_start"


def cancelled_before_start(exc: BaseException) -> bool:
    """Whether ``exc`` is the cancellation of a `SlotLease.call()` whose request was still queued, so that it never
    reached the driver connection."""
    return getattr(exc, _UNSENT, False)


@dataclass(frozen=True, slots=True)
class Request:
    """A call of the driver connection's ``method`` with ``args``; its outcome event carries ``request_id``."""

    method: str
    args: tuple[Any, ...] = ()
    request_id: Any = field(default_factory=lambda: next(_request_ids))

    def __post_init__(self) -> None:
        if self.method not in REQUEST_METHODS:
            raise ValueError(f"unknown request method {self.method!r}; one of {', '.join(sorted(REQUEST_METHODS))}")
        if not isinstance(self.args, tuple):
            object.__setattr__(self, "args", tuple(self.args))


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
    ``channel.on()`` runs it. ``owner`` is the task that acquired the lease.
    """

    __slots__ = (
        "_aborted",
        "_bound",
        "_released",
        "channel",
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
        self._released = False
        self._aborted: str | None = None
        self._bound = False  # released only by ``owner``

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
        """Whether the slot rolled back the lease's transaction and ended the lease (the pool's ``commit_timeout``)."""
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

    async def call(self, method: str, *args: Any) -> Any:
        """Send ``Request(method, args)`` and return its result or raise its error.

        Cancelling the caller cancels the request: a queued one never runs, a running one is cancelled in the
        driver connection. `cancelled_before_start()` tells the two apart on the raised ``CancelledError``. A
        `CancelledEvent` for a request the caller did not cancel raises `InterfaceError`. A lease that is released
        raises `InterfaceError`, one the slot aborted `TransactionClosed`.
        """
        request = Request(method, args)
        self._check_open()
        future = self.channel.expect(request.request_id)
        try:
            self._send(request)
        except BaseException:
            self.channel.forget(request.request_id)
            raise
        try:
            return await future
        except asyncio.CancelledError as exc:
            task = asyncio.current_task()
            if future.cancelled() and task is not None and not task.cancelling():
                raise InterfaceError(f"request {request.request_id!r} ({method}) was cancelled by the slot") from None
            # The request's CancelledEvent, or an outcome that won the race, settles the already cancelled future.
            if future.cancelled() and self.slot._cancel(request):
                setattr(exc, _UNSENT, True)
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
        if self._released:
            return
        self._released = True
        await self.slot._finish(self, discard)

    async def __aenter__(self) -> SlotLease:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.release()


_IDLE, _LEASED, _FINISHING, _CLOSED = "idle", "leased", "finishing", "closed"


class Slot:
    """An exclusive channel to one driver connection of a pool.

    A task of its own runs the requests of the current lease one at a time, in order, and delivers each outcome
    to the lease's channel: a `CancelledEvent` for a request cancelled before it finished, and a `CallbackEvent`
    named `ON_COMMIT` or `ON_ROLLBACK` before the outcome of a successful ``commit`` or ``rollback``. When that task
    fails, the queued requests fail with `InterfaceError` and the slot becomes unusable. Events the connection emits
    go to the current lease's channel, or to the pool's `EventBus` while no lease holds the slot; only `CallbackEvent`
    and `BackendEvent` values are accepted, and a `CallbackEvent` with a name in `RESERVED_CALLBACKS` is dropped.

    When the lease is inside a transaction and no request arrives within the pool's ``commit_timeout``, the slot
    rolls the transaction back, delivers ``CallbackEvent(ON_ROLLBACK)``, ends the lease (later requests raise
    `TransactionClosed`) and leaves the pool. A rollback that does not finish within ``commit_timeout`` either is
    cancelled in the driver connection.
    """

    _ids = itertools.count(1)

    def __init__(self, pool: Pool) -> None:
        self.pool = pool
        self.slot_id = next(self._ids)
        self.connection: DriverConnection | None = None
        self.lease: SlotLease | None = None
        self.state = _IDLE
        self._queue: deque[tuple[SlotLease, Request, contextvars.Context]] = deque()
        self._wakeup = asyncio.Event()
        self._running: tuple[Request, asyncio.Task] | None = None
        self._cancelled: Request | None = None
        self._in_transaction = False
        self._idle = asyncio.Event()
        self._idle.set()
        self._server: asyncio.Task | None = None
        self._finisher: asyncio.Task | None = None

    def __repr__(self) -> str:
        return f"<Slot {self.slot_id} of {self.pool.name!r}, {self.state}>"

    async def open(self) -> None:
        """Open the driver connection and start serving requests."""
        self.connection = await self.pool.driver.open_connection(self.pool.profile, self._emit)
        self._server = asyncio.get_running_loop().create_task(
            self._serve(), name=f"adorm-slot-{self.slot_id}", context=contextvars.Context()
        )

    def is_usable(self) -> bool:
        """Whether the slot can serve another lease."""
        return (
            self.state != _CLOSED
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
        self._queue.append((lease, request, contextvars.copy_context()))
        self._idle.clear()
        self._wakeup.set()

    def _cancel(self, request: Request) -> bool:
        """Cancel ``request``; return whether it was still queued. A queued request never runs and gets
        ``CancelledEvent(started=False)`` at once; a running one is cancelled in the driver connection."""
        for index, (lease, queued, _context) in enumerate(self._queue):
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
        current: tuple[SlotLease, Request, contextvars.Context] | None = None
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
                current = self._queue.popleft()
                await self._run(*current)
                current = None
        except Exception as exc:
            logger.exception("serving %r failed", self)
            error = InterfaceError(f"{self!r} stopped serving requests: {exc}")
            failed = ([current] if current is not None else []) + list(self._queue)
            self._queue.clear()
            for lease, request, _context in failed:
                self._deliver(lease, ErrorEvent(request.request_id, error))
            self._idle.set()

    def _commit_deadline(self) -> float | None:
        """Seconds to wait for the next request of a lease inside a transaction; ``None`` for no limit."""
        lease = self.lease
        if not self._in_transaction or lease is None or lease.released:
            return None
        return self.pool.commit_timeout

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
        self._deliver(lease, CallbackEvent(ON_ROLLBACK))
        self.state = _FINISHING
        self._finisher = asyncio.get_running_loop().create_task(self._return(lease, True))

    async def _run(self, lease: SlotLease, request: Request, context: contextvars.Context) -> None:
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
        # The connection method runs in the context of the flow that sent the request.
        call = asyncio.get_running_loop().create_task(coroutine, context=context)
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
                event = ErrorEvent(request_id, cast("BaseException", call.exception()))
            elif self._cancelled is request:
                event = CancelledEvent(request_id, started=True)
            else:
                event = ErrorEvent(request_id, InterfaceError(f"{request.method}() was cancelled by the driver"))
        except Exception as exc:
            event = ErrorEvent(request_id, exc)
        finally:
            self._running = None
            self._cancelled = None
        if isinstance(event, ResultEvent):
            if request.method == "begin":
                self._in_transaction = True
            elif request.method in ("commit", "rollback"):
                self._in_transaction = False
                self._deliver(lease, CallbackEvent(ON_COMMIT if request.method == "commit" else ON_ROLLBACK))
        self._deliver(lease, event)

    async def _finish(self, lease: SlotLease, discard: bool) -> None:
        if self.lease is not lease or self.state != _LEASED:
            return
        self.state = _FINISHING
        self._finisher = asyncio.get_running_loop().create_task(self._return(lease, discard))
        await asyncio.shield(self._finisher)

    async def _return(self, lease: SlotLease, discard: bool) -> None:
        try:
            await self._idle.wait()
        finally:
            self.lease = None
            lease.channel.close()
            keep = not (discard or self.pool.closed or self._in_transaction or not self.is_usable())
            if keep and self.pool.on_release is not None:
                keep = await self._on_release(lease) and self.is_usable() and not self.pool.closed
            if keep:
                self.state = _IDLE
                self.pool._returned(self)
            else:
                await self.close()
                self.pool._removed(self)

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

    async def close(self) -> None:
        """Stop serving and close the connection. Logs errors instead of raising them."""
        if self.state == _CLOSED:
            return
        self.state = _CLOSED
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
        if self.connection is not None:
            try:
                await self.connection.close()
            except Exception:
                logger.exception("closing the connection of %r failed", self)
