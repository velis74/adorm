"""Slots: exclusive channels to one driver connection each, and the leases that hand a slot to a flow."""

from __future__ import annotations

import asyncio
import contextvars
import itertools
import logging

from collections import deque
from dataclasses import dataclass, field
from typing import Any, cast, TYPE_CHECKING

from adorm.events import ErrorEvent, Event, ResultEvent
from adorm.exceptions import InterfaceError

if TYPE_CHECKING:
    from adorm.drivers.base import DriverConnection
    from adorm.executor.pool import Pool
    from adorm.executor.sessions import Session

__all__ = ["REQUEST_METHODS", "Request", "RequestQueue", "Slot", "SlotLease"]

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
        """Queue ``request``. Raises `InterfaceError` after the lease is released."""
        self._lease._send(request)

    async def put(self, request: Request) -> None:
        """Queue ``request``; the queue is unbounded, so it never waits."""
        self._lease._send(request)

    async def join(self) -> None:
        """Wait until every request queued so far has run."""
        await self._lease.slot._drained(self._lease)

    def qsize(self) -> int:
        """How many queued requests have not started yet."""
        return len(self._lease.slot._queue)


class SlotLease:
    """A slot handed to one flow: a request queue to write to and an event queue to read from.

    ``requests`` takes `Request` objects; ``events`` receives a `ResultEvent` or an `ErrorEvent` per request, and
    the callbacks and backend events the connection emits while the lease is held. A request sent with `call()`
    does not appear on ``events``: its result is returned. ``owner`` is the task that acquired the lease.
    """

    __slots__ = ("_bound", "_calls", "_released", "events", "owner", "pool", "requests", "session", "slot")

    def __init__(self, slot: Slot, session: Session, owner: asyncio.Task | None) -> None:
        self.slot = slot
        self.pool: Pool = slot.pool
        self.session = session
        self.owner = owner
        self.requests = RequestQueue(self)
        self.events: asyncio.Queue[Event] = asyncio.Queue()
        self._calls: dict[Any, asyncio.Future] = {}
        self._released = False
        self._bound = False  # released only by ``owner``

    @property
    def released(self) -> bool:
        """Whether `release()` was called."""
        return self._released

    def __repr__(self) -> str:
        state = "released" if self._released else "held"
        return f"<SlotLease {self.slot.slot_id} of {self.pool.name!r} for {self.session!r}, {state}>"

    def _send(self, request: Request) -> None:
        if self._released:
            raise InterfaceError(f"{self!r} cannot take requests")
        if not isinstance(request, Request):
            raise TypeError(f"a lease takes Request objects, not {type(request).__name__}")
        self.slot._enqueue(self, request)

    async def call(self, method: str, *args: Any) -> Any:
        """Send ``Request(method, args)`` and return its result or raise its error.

        Cancelling the caller cancels the request: a queued one never runs, a running one is cancelled in the
        driver connection.
        """
        request = Request(method, args)
        future = asyncio.get_running_loop().create_future()
        self._calls[request.request_id] = future
        try:
            self._send(request)
        except BaseException:
            del self._calls[request.request_id]
            raise
        try:
            return await future
        except asyncio.CancelledError:
            # A request cancelled before it started has no outcome; a running one delivers to the dropped future.
            if self.slot._cancel(request):
                self._calls.pop(request.request_id, None)
            raise

    def _deliver(self, event: Event) -> None:
        request_id = getattr(event, "request_id", None)
        future = self._calls.pop(request_id, None) if isinstance(event, (ResultEvent, ErrorEvent)) else None
        if future is None:
            self.events.put_nowait(event)
        elif not future.done():
            if isinstance(event, ResultEvent):
                future.set_result(event.value)
            else:
                future.set_exception(event.error)

    async def release(self, *, discard: bool = False) -> None:
        """Return the slot to the pool once every queued request has run; a second call does nothing.

        ``discard`` closes the slot's connection instead, as does a connection that is no longer usable or that a
        ``begin`` left in a transaction not ended by a successful ``commit`` or ``rollback``. The return completes
        even when the caller is cancelled meanwhile. Callbacks and backend events left unread on ``events`` go to
        the pool's `EventBus`.
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
    to the lease. When that task fails, the queued requests fail with `InterfaceError` and the slot becomes
    unusable. Events the connection emits go to the current lease, or to the pool's `EventBus` while no lease
    holds the slot.
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
        lease = self.lease
        if lease is not None and not lease.released:
            lease.events.put_nowait(event)
        else:
            self.pool.events.dispatch(event)

    def _enqueue(self, lease: SlotLease, request: Request) -> None:
        self._queue.append((lease, request, contextvars.copy_context()))
        self._idle.clear()
        self._wakeup.set()

    def _cancel(self, request: Request) -> bool:
        """Cancel ``request``; return whether it was still queued, so that it never runs and has no outcome."""
        for index, (_lease, queued, _context) in enumerate(self._queue):
            if queued is request:
                del self._queue[index]
                if not self._queue and self._running is None:
                    self._idle.set()
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
                    await self._wakeup.wait()
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
                lease._deliver(ErrorEvent(request.request_id, error))
            self._idle.set()

    async def _run(self, lease: SlotLease, request: Request, context: contextvars.Context) -> None:
        connection = cast("DriverConnection", self.connection)
        request_id = request.request_id
        try:
            coroutine = getattr(connection, request.method)(*request.args)
        except Exception as exc:  # wrong arguments
            lease._deliver(ErrorEvent(request_id, exc))
            return
        if not asyncio.iscoroutine(coroutine):
            error = InterfaceError(f"{type(connection).__name__}.{request.method}() did not return a coroutine")
            lease._deliver(ErrorEvent(request_id, error))
            return
        # The connection method runs in the context of the flow that sent the request.
        call = asyncio.get_running_loop().create_task(coroutine, context=context)
        self._running = (request, call)
        event: Event | None
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
                event = None
                lease._calls.pop(request_id, None)
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
        if event is not None:
            lease._deliver(event)

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
            while not lease.events.empty():
                event = lease.events.get_nowait()
                if not isinstance(event, (ResultEvent, ErrorEvent)):
                    self.pool.events.dispatch(event)
            if discard or self.pool.closed or self._in_transaction or not self.is_usable():
                await self.close()
                self.pool._removed(self)
            else:
                self.state = _IDLE
                self.pool._returned(self)

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
