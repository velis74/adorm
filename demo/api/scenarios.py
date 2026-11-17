"""Concurrency scenarios: flows in concurrent tasks on one driver, each in a session of its own, and what each saw.

A flow is a coroutine function on the library models that takes ``log`` (records what it saw) and its arguments; the
scenario's choreography starts the flows and lets them on at fixed points, observing the database with
`adorm.testing.wait_for_lock()` and the pool's counters, so that every run takes the same course.
"""

from __future__ import annotations

import asyncio
import datetime
import time

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import adorm

from adorm.settings import default_router
from adorm.testing import wait_for_lock
from demo.api.capture import CAPTURE, OBSERVER, Statement
from demo.api.examples import source, TODAY
from demo.models.library import Book, Edition, Loan, Reader

RACE_ISBN = "9780000060002"
OTHER_ISBN = "9780000070009"
"""Editions the scenarios lock; their ``available`` and the loans are restored after every run."""

T = TypeVar("T")


class Timeline:
    """What the flows and the observer saw, in order, in milliseconds since the run started."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def log(self, flow: str, text: str, kind: str = "info") -> None:
        self.events.append({"at_ms": round(CAPTURE.elapsed() * 1000, 1), "flow": flow, "kind": kind, "text": text})


async def uninterrupted(awaitable: Awaitable[T]) -> T:
    """Await ``awaitable`` in a task of its own until it ends, also when the current task is cancelled meanwhile; that
    cancellation is raised once the task has ended."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError from (None if task.cancelled() else task.exception())
    return task.result()


def describe(exc: BaseException) -> dict[str, Any]:
    """An exception as a flow's outcome; ``held_sessions`` names the sessions of a `DeadlockError`."""
    held = getattr(exc, "held_sessions", None)
    return {
        "status": "error",
        "error": type(exc).__name__,
        "sqlstate": getattr(exc, "sqlstate", None),
        "message": str(exc).splitlines()[0] if str(exc) else type(exc).__name__,
        "held_sessions": None if held is None else [session.session_name for session in held],
    }


class Run:
    """One run of a scenario on the connection profile ``using``."""

    def __init__(self, using: str) -> None:
        self.using = using
        self.timeline = Timeline()
        self.outcomes: dict[str, dict[str, Any]] = {}
        self.tasks: list[asyncio.Task] = []

    def observe(self, text: str) -> None:
        self.timeline.log(OBSERVER, text, "observe")

    def flow(self, name: str, function: Callable[..., Awaitable[Any]], *args: Any) -> asyncio.Task:
        """Start ``function(log, *args)`` in a task of its own, in the session ``(using, name)``."""

        def log(text: str, kind: str = "info") -> None:
            self.timeline.log(name, text, kind)

        async def run() -> None:
            with adorm.create_session(self.using, name).activate():
                try:
                    result = await function(log, *args)
                except asyncio.CancelledError as exc:
                    outcome = adorm.cancellation_outcome(exc)
                    log(f"cancelled; the statement it ran: {outcome}", "error")
                    self.outcomes[name] = {"status": "cancelled", "statement": str(outcome)}
                    return
                except Exception as exc:
                    self.outcomes[name] = outcome = describe(exc)
                    sqlstate = f" (SQLSTATE {outcome['sqlstate']})" if outcome["sqlstate"] else ""
                    log(f"{outcome['error']}{sqlstate}: {outcome['message']}", "error")
                    return
                self.outcomes[name] = {"status": "ok", "result": result}

        task = asyncio.create_task(run(), name=f"flow {name}")
        self.tasks.append(task)
        return task

    async def stop_flows(self) -> None:
        """Cancel the flows that have not ended and wait for them; a cancelled flow rolls back its transaction."""
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def until(self, event: asyncio.Event, *tasks: asyncio.Task, timeout: float = 10.0) -> None:
        """Wait until ``event`` is set; raises `RuntimeError` when one of ``tasks`` ends first."""
        waiter = asyncio.ensure_future(event.wait())
        try:
            async with asyncio.timeout(timeout):
                done, _ = await asyncio.wait({waiter, *tasks}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        if waiter not in done:
            raise RuntimeError(f"a flow ended before the scenario could go on: {self.outcomes}")

    async def lock_wait(self, task: asyncio.Task, who: str, *, required: bool = True) -> None:
        """Wait until ``task``'s statement waits for a lock in the database and record it. Unless ``required``, a
        ``task`` that ends before the wait is seen is no error."""
        try:
            waiting = await wait_for_lock(task)
        except RuntimeError:
            if required or not task.done():
                raise
            return
        self.observe(f"{who} waits for a lock held by database session {', '.join(map(str, waiting.blocking_pids))}")

    def pool(self) -> Any:
        return default_router().pool(self.using)

    async def pool_state(self, predicate: Callable[[Any], bool], timeout: float = 10.0) -> Any:
        """Wait until ``predicate(pool)`` holds, checking whenever the loop runs; returns the pool."""
        pool = self.pool()
        async with asyncio.timeout(timeout):
            while not predicate(pool):
                await asyncio.sleep(0.005)
        return pool


def pause_at(reached: asyncio.Event, go: asyncio.Event) -> Callable[[], Awaitable[None]]:
    """A ``pause`` argument of a flow: it sets ``reached`` and waits for ``go``."""

    async def pause() -> None:
        reached.set()
        await go.wait()

    return pause


async def no_pause() -> None:
    """A ``pause`` argument that returns at once."""


# Flows


async def lend_last_copy(log, isbn: str, reader_email: str, pause) -> str:
    async with adorm.atomic():
        log("select_for_update() of the edition")
        edition = await Edition.objects.select_for_update().get(isbn=isbn)
        log(f"has the row lock; {edition.available} available")
        await pause()
        if edition.available == 0:
            raise LookupError(f"no copy of {isbn} left")
        reader = await Reader.objects.get(email=reader_email)
        await Loan.objects.create(
            edition_id=edition.id, reader_id=reader.id, borrowed=TODAY, due=TODAY + datetime.timedelta(days=21)
        )
        edition.available -= 1
        await edition.save(update_fields=["available"])
        log("created the loan")
    log("committed", "ok")
    return "lent"


async def hold_edition(log, isbn: str, pause) -> str:
    async with adorm.atomic():
        await Edition.objects.select_for_update().get(isbn=isbn)
        log("has the row lock")
        await pause()
    log("committed", "ok")
    return "held"


async def lock_with_timeout(log, isbn: str) -> str:
    async with adorm.atomic():
        with adorm.connection_options(lock_timeout=0.5):
            log("select_for_update() with lock_timeout=0.5")
            await Edition.objects.select_for_update().get(isbn=isbn)
            log("has the row lock", "ok")
    return "locked"


async def read_for_update(log, isbn: str) -> str:
    async with adorm.atomic():
        log("select_for_update() of the edition")
        await Edition.objects.select_for_update().get(isbn=isbn)
        log("has the row lock", "ok")
    return "locked"


async def lock_two(log, first: str, second: str, pause) -> str:
    async with adorm.atomic():
        await Edition.objects.select_for_update().get(isbn=first)
        log(f"has the lock of {first}")
        await pause()
        log(f"select_for_update() of {second}")
        await Edition.objects.select_for_update().get(isbn=second)
        log(f"has the lock of {second}")
    log("committed", "ok")
    return "locked both"


async def hold_a_slot(log, pause) -> str:
    async with adorm.atomic():
        books = await Book.objects.count()
        log(f"transaction open, holds a slot ({books} books)")
        await pause()
    log("committed; the slot is back in the pool", "ok")
    return "held"


async def read_in_a_second_session(log, using: str, second: str, pause) -> int:
    async with adorm.atomic():
        log("transaction open, holds a slot")
        await pause()
        log(f"Book.objects.count() in the session {second!r} asks for a second slot")
        books = await adorm.create_session(using, second).Book.objects.count()
        log(f"{books} books")
    log("committed", "ok")
    return books


async def count_books(log) -> int:
    log("Book.objects.count() asks for a slot")
    books = await Book.objects.count()
    log(f"{books} books", "ok")
    return books


# Choreographies


async def race(run: Run) -> None:
    a_locked, go = asyncio.Event(), asyncio.Event()
    a = run.flow("A", lend_last_copy, RACE_ISBN, "ana@example.com", pause_at(a_locked, go))
    await run.until(a_locked, a)
    b = run.flow("B", lend_last_copy, RACE_ISBN, "bojan@example.com", no_pause)
    await run.lock_wait(b, "B")
    run.observe("A goes on")
    go.set()
    await asyncio.gather(a, b)


async def lock_timeout(run: Run) -> None:
    a_locked, go = asyncio.Event(), asyncio.Event()
    a = run.flow("A", hold_edition, RACE_ISBN, pause_at(a_locked, go))
    await run.until(a_locked, a)
    b = run.flow("B", lock_with_timeout, RACE_ISBN)
    await run.lock_wait(b, "B", required=False)
    await b
    run.observe("A goes on")
    go.set()
    await a


async def cancel(run: Run) -> None:
    a_locked, go = asyncio.Event(), asyncio.Event()
    a = run.flow("A", hold_edition, RACE_ISBN, pause_at(a_locked, go))
    await run.until(a_locked, a)
    b = run.flow("B", read_for_update, RACE_ISBN)
    await run.lock_wait(b, "B")
    run.observe("cancels B's task")
    b.cancel()
    await b
    pool = await run.pool_state(lambda pool: pool.in_use == 1)
    run.observe(f"{pool.in_use} slot in use: B's slot is back in the pool, A still holds its own")
    go.set()
    await a


async def deadlock(run: Run) -> None:
    a_locked, a_go, b_locked, b_go = asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()
    a = run.flow("A", lock_two, RACE_ISBN, OTHER_ISBN, pause_at(a_locked, a_go))
    b = run.flow("B", lock_two, OTHER_ISBN, RACE_ISBN, pause_at(b_locked, b_go))
    await run.until(a_locked, a, b)
    await run.until(b_locked, a, b)
    a_go.set()
    await run.lock_wait(a, "A")
    b_go.set()
    run.observe("B asks for A's row: PostgreSQL detects the cycle after deadlock_timeout")
    await asyncio.gather(a, b)


async def slot_deadlock(run: Run) -> None:
    a_ready, a_go, b_ready, b_go = asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()
    a = run.flow("A", read_in_a_second_session, run.using, "A2", pause_at(a_ready, a_go))
    b = run.flow("B", read_in_a_second_session, run.using, "B2", pause_at(b_ready, b_go))
    await run.until(a_ready, a, b)
    await run.until(b_ready, a, b)
    pool = run.pool()
    run.observe(f"{pool.in_use} of {pool.limit} slots in use")
    a_go.set()
    await run.pool_state(lambda pool: pool.waiting == 1)
    run.observe("A waits for a slot B holds")
    b_go.set()
    run.observe("B asks for a slot too: every slot is held by a flow waiting for a slot")
    await asyncio.gather(a, b)


async def pool_exhaustion(run: Run) -> None:
    a_ready, a_go, b_ready, b_go = asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()
    a = run.flow("A", hold_a_slot, pause_at(a_ready, a_go))
    b = run.flow("B", hold_a_slot, pause_at(b_ready, b_go))
    await run.until(a_ready, a, b)
    await run.until(b_ready, a, b)
    pool = run.pool()
    run.observe(f"{pool.in_use} of {pool.limit} slots in use")
    c = run.flow("C", count_books)
    pool = await run.pool_state(lambda pool: pool.waiting == 1)
    run.observe(f"C waits for a slot ({pool.waiting} waiting, acquire_timeout {pool.acquire_timeout:g} s)")
    await c
    run.observe("A goes on")
    a_go.set()
    await a
    d = run.flow("D", count_books)
    await d
    b_go.set()
    await b


@dataclass(frozen=True)
class Scenario:
    """A scenario: ``choreography(run)`` starts and steers the ``flows``; ``small_pool`` runs it on the profile with
    two slots."""

    id: str
    title: str
    description: str
    choreography: Callable[[Run], Awaitable[None]]
    flows: tuple[Callable[..., Awaitable[Any]], ...]
    small_pool: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "code": "\n\n\n".join(source(flow) for flow in self.flows),
            "choreography": source(self.choreography),
        }


SCENARIOS: dict[str, Scenario] = {
    scenario.id: scenario
    for scenario in (
        Scenario(
            "race",
            "Two flows race for the last copy",
            "A and B lend the last copy of one edition. A locks the row with select_for_update(); B's "
            "select_for_update() waits until A commits, then reads the row A changed and finds no copy left.",
            race,
            (lend_last_copy,),
        ),
        Scenario(
            "lock_timeout",
            "A lock wait with a timeout",
            "A holds the row lock. B sets lock_timeout=0.5 with adorm.connection_options(): PostgreSQL ends B's "
            "wait after half a second with SQLSTATE 55P03, and adorm raises OperationalError.",
            lock_timeout,
            (hold_edition, lock_with_timeout),
        ),
        Scenario(
            "cancel",
            "Cancelling a running statement",
            "B's select_for_update() waits for A's lock. Cancelling B's task cancels the statement in the database; "
            "the slot rolls back and returns to the pool, and A is not disturbed.",
            cancel,
            (hold_edition, read_for_update),
        ),
        Scenario(
            "deadlock",
            "A deadlock in the database",
            "A locks War and Peace's edition and B Anna Karenina's; then each asks for the other's row. PostgreSQL "
            "detects the cycle after deadlock_timeout and aborts one transaction with SQLSTATE 40P01; the other one "
            "commits.",
            deadlock,
            (lock_two,),
        ),
        Scenario(
            "slot_deadlock",
            "A deadlock adorm detects",
            "The profile's pool has two slots. A and B each hold one in a transaction, then each asks for a second "
            "slot for another session. Every slot is held by a flow waiting for a slot: adorm raises DeadlockError in "
            "B at once, without waiting for acquire_timeout, with the sessions B holds in held_sessions. B rolls back, "
            "and A gets B's slot and commits.",
            slot_deadlock,
            (read_in_a_second_session,),
            small_pool=True,
        ),
        Scenario(
            "pool_exhaustion",
            "Pool exhaustion",
            "The profile's pool has two slots and acquire_timeout=1. A and B hold both in open transactions; C "
            "waits for a slot and gets SessionTimeout. Once A commits, D gets a slot at once.",
            pool_exhaustion,
            (hold_a_slot, count_books),
            small_pool=True,
        ),
    )
}


async def snapshot() -> tuple[dict[str, int], int]:
    """The ``available`` of the scenarios' editions and the highest loan id."""
    available = {
        isbn: count
        async for isbn, count in Edition.objects.filter(isbn__in=[RACE_ISBN, OTHER_ISBN]).values_list(
            "isbn", "available"
        )
    }
    last = await Loan.objects.aggregate(last=adorm.Max("id"))
    return available, last["last"] or 0


RESTORE_LOCK_TIMEOUT = 5.0
"""Seconds `restore()` waits for a row lock before it raises."""


async def restore(state: tuple[dict[str, int], int]) -> None:
    """Delete the loans created after `snapshot()` and restore the editions' ``available``."""
    available, last = state
    async with adorm.atomic():
        # Inside the transaction: SET LOCAL, which ends with the commit.
        with adorm.connection_options(lock_timeout=RESTORE_LOCK_TIMEOUT):
            await Loan.objects.filter(id__gt=last).delete()
            for isbn, count in available.items():
                await Edition.objects.filter(isbn=isbn).update(available=count)


async def run_scenario(scenario: Scenario, driver: str) -> dict[str, Any]:
    """Run ``scenario`` on ``driver`` and return what every flow saw, the statements and the outcomes. When the
    choreography raises, or the run is cancelled, the flows still running are cancelled before the data is
    restored. Stopping the flows and the recording and restoring the data run to their end also when the run is
    cancelled meanwhile."""
    using = f"{driver}-small" if scenario.small_pool else driver
    with adorm.create_session(driver, "setup").activate():
        state = await snapshot()
    run = Run(using)

    async def end(failed: bool) -> list[Statement]:
        if failed:
            await run.stop_flows()
        statements = await CAPTURE.stop()
        with adorm.create_session(driver, "setup").activate():
            await restore(state)
        return statements

    started = time.monotonic()
    failed = True
    try:
        with adorm.create_session(driver, "setup").activate():
            await Edition.objects.filter(isbn=RACE_ISBN).update(available=1)
        CAPTURE.start()
        started = time.monotonic()
        await scenario.choreography(run)
        failed = False
    finally:
        elapsed = time.monotonic() - started
        statements = await uninterrupted(end(failed))
    return {
        "id": scenario.id,
        "driver": driver,
        "ms": round(elapsed * 1000, 1),
        "flows": sorted(run.outcomes),
        "outcomes": run.outcomes,
        "events": run.timeline.events,
        "statements": [statement.as_dict() for statement in statements if statement.session != OBSERVER],
    }
