"""Waiting in tests until an operation has reached the database, or a request waits there for a lock."""

from __future__ import annotations

import asyncio

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from adorm.exceptions import NotSupportedError
from adorm.executor.slot import on_request_started, Request, running_requests, RunningRequest

if TYPE_CHECKING:
    from adorm.drivers.base import BackendActivity, DatabaseMonitor
    from adorm.executor.pool import Pool
    from adorm.executor.sessions import Session

__all__ = ["LockWait", "running_operations", "wait_for_lock", "wait_for_operation"]


@dataclass(frozen=True, slots=True)
class LockWait:
    """A request waiting for a lock in the database: the `RunningRequest` and its session's `BackendActivity`."""

    running: RunningRequest
    activity: BackendActivity

    @property
    def request(self) -> Request:
        """The request that waits: an ``execute`` of an operation, or a transaction call such as ``commit``."""
        return self.running.request

    @property
    def operation(self) -> Any:
        """The operation that waits; ``None`` when the request is not an ``execute`` (`RunningRequest.operation`)."""
        return self.running.operation

    @property
    def blocking_pids(self) -> tuple[int, ...]:
        """The database sessions holding the locks it waits for."""
        return self.activity.blocking_pids


def _matcher(
    task: asyncio.Task | None, session: Session | None, *, operations: bool = True
) -> Callable[[RunningRequest], bool]:
    """Select the requests of ``task`` and ``session``; with ``operations`` only ``execute`` requests."""

    def matches(running: RunningRequest) -> bool:
        return (
            (not operations or running.operation is not None)
            and (task is None or running.sender is task)
            and (session is None or running.session is session)
        )

    return matches


def running_operations(
    task: asyncio.Task | None = None, *, session: Session | None = None
) -> tuple[RunningRequest, ...]:
    """The operations whose driver connection call runs now on a slot of the running event loop, in the order they
    started; with ``task`` only those the task sent, with ``session`` only those of the session."""
    matches = _matcher(task, session)
    return tuple(running for running in running_requests() if matches(running))


def _check_task(task: asyncio.Task | None, what: str) -> None:
    """Raise `RuntimeError` when ``task`` has finished: it can no longer send the request awaited."""
    if task is None or not task.done():
        return
    error = None if task.cancelled() else task.exception()
    outcome = "was cancelled" if task.cancelled() else f"raised {error!r}" if error is not None else "returned"
    raise RuntimeError(f"{task!r} {outcome} before {what}") from error


def _describe(task: asyncio.Task | None, session: Session | None, what: str = "an operation") -> str:
    parts = [f"of {task!r}"] if task is not None else []
    if session is not None:
        parts.append(f"of {session!r}")
    return " ".join([what, *parts])


async def wait_for_operation(
    task: asyncio.Task | None = None,
    *,
    session: Session | None = None,
    timeout: float = 10.0,
    interval: float = 0.01,
) -> RunningRequest:
    """Wait until an operation has been sent to the database: its driver connection call has started on a slot of the
    running event loop and has its database session (`RunningRequest.reached_session`). Return its `RunningRequest`,
    the first such of `running_operations()` when one runs already.

    ``task`` and ``session`` select the operation as in `running_operations()`. A started operation that still waits
    for a database session it shares with other connections is checked again every ``interval`` seconds. The
    operation may have ended by the time the caller resumes (`RunningRequest.running`). Raises `RuntimeError` when
    ``task`` finishes first and ``TimeoutError`` after ``timeout`` seconds."""
    matches = _matcher(task, session)
    wakeup = asyncio.Event()
    candidates: list[RunningRequest] = []

    def started(running: RunningRequest) -> None:
        if matches(running):
            candidates.append(running)
            wakeup.set()

    stop = on_request_started(started)
    candidates.extend(running_operations(task, session=session))
    what = f"{_describe(task, session)} was sent to the database"
    limit = asyncio.timeout(timeout)
    try:
        async with limit:
            while True:
                for running in candidates:
                    if running.reached_session:
                        return running
                # An operation that ended without its session never reached the database.
                candidates[:] = [running for running in candidates if running.running]
                _check_task(task, what)
                wakeup.clear()
                await _wait(wakeup, task, interval if candidates else None)
    except TimeoutError:
        if not limit.expired():
            raise
        raise TimeoutError(f"no {what.removeprefix('an ')} within {timeout:g} s") from None
    finally:
        stop()


async def _wait(wakeup: asyncio.Event, task: asyncio.Task | None, interval: float | None) -> None:
    """Wait until ``wakeup`` is set, ``task`` finishes or ``interval`` seconds pass."""
    pending: set[asyncio.Future[Any]] = {asyncio.ensure_future(wakeup.wait())}
    if task is not None:
        pending.add(task)
    try:
        await asyncio.wait(pending, timeout=interval, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in pending:
            if waiter is not task:
                waiter.cancel()


async def _activity(monitors: dict[Pool, DatabaseMonitor], running: tuple[RunningRequest, ...]) -> list[LockWait]:
    """The lock waits among ``running``, read with a monitor per pool opened on first use."""
    by_pool: dict[Pool, list[tuple[RunningRequest, int, int]]] = {}
    for request in running:
        connection = request.slot.connection
        pid = request.backend_pid
        if connection is None or pid is None:
            driver = request.slot.pool.driver.name
            raise NotSupportedError(f"the {driver} driver does not report the database session of its connections")
        # A call waiting for a session it shares cannot wait in the database; another call has that session.
        if request.in_session:
            by_pool.setdefault(request.slot.pool, []).append((request, pid, connection.session_holds))
    waits: list[LockWait] = []
    for pool, requests in by_pool.items():
        monitor = monitors.get(pool)
        if monitor is None:
            monitor = monitors[pool] = await pool.driver.open_monitor(pool.profile)
        activity = await monitor.activity({pid for _request, pid, _holds in requests})
        for request, pid, holds in requests:
            found = activity.get(pid)
            # Still holding the same session after the read: the wait belongs to this request, not to another one.
            if (
                found is not None
                and found.waiting_for_lock
                and request.in_session
                and request.slot.connection is not None
                and request.slot.connection.session_holds == holds
            ):
                waits.append(LockWait(request, found))
    return waits


async def wait_for_lock(
    task: asyncio.Task | None = None,
    *,
    session: Session | None = None,
    timeout: float = 10.0,
    interval: float = 0.01,
) -> LockWait:
    """Wait until a request waits for a lock in the database (`BackendActivity.waiting_for_lock`) and return the
    `LockWait`.

    Every request a slot runs counts, not only operations: a ``commit`` waits for the locks a deferred constraint
    takes (`LockWait.request`). ``task`` selects the requests the task sent, ``session`` those of the session. Each
    check reads the database sessions of the selected requests that run then with their session
    (`RunningRequest.in_session`) through a `DatabaseMonitor` of their pool's driver (`Driver.open_monitor()`), opened
    on first use and closed on return; checks repeat every ``interval`` seconds and whenever a selected request
    starts. Raises `NotSupportedError` when the driver does not report its connections' database sessions
    (`DriverConnection.backend_pid`) or cannot monitor them, `RuntimeError` when ``task`` finishes first and
    ``TimeoutError`` after ``timeout`` seconds."""
    matches = _matcher(task, session, operations=False)
    wakeup = asyncio.Event()

    def started(running: RunningRequest) -> None:
        if matches(running):
            wakeup.set()

    stop = on_request_started(started)
    monitors: dict[Pool, DatabaseMonitor] = {}
    what = f"{_describe(task, session, 'a request')} waited for a lock"
    limit = asyncio.timeout(timeout)
    try:
        async with limit:
            while True:
                _check_task(task, what)
                wakeup.clear()
                waits = await _activity(monitors, tuple(filter(matches, running_requests())))
                if waits:
                    return waits[0]
                await _wait(wakeup, task, interval)
    except TimeoutError:
        if not limit.expired():
            raise
        raise TimeoutError(f"no {what.removeprefix('a ')} within {timeout:g} s") from None
    finally:
        stop()
        await asyncio.gather(*(monitor.close() for monitor in monitors.values()), return_exceptions=True)
