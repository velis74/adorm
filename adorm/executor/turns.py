"""Turns of the transactions and the operations outside a transaction on a database session that several slots share.

A transaction holds the exclusive turn from before its slot is acquired until the slot has been returned or has left
the pool; operations outside a transaction hold a shared turn; a flow inside a transaction that holds the turn takes
none.
"""

from __future__ import annotations

import asyncio
import concurrent.futures

from collections import deque
from collections.abc import Callable
from typing import Any, TYPE_CHECKING

from adorm.exceptions import DeadlockError

if TYPE_CHECKING:
    from adorm.executor.sessions import Session

__all__ = ["DEADLOCK_CHECK_INTERVAL", "TransactionTurns", "Turn", "awaits"]

DEADLOCK_CHECK_INTERVAL = 0.05
"""Seconds between the checks of waiting turns for a transaction whose task awaits the waiting task."""

_STALLED_LOOP_GRACE = 1.0  # seconds a thread waits past its deadline for an event loop that does not answer


def awaits(task: asyncio.Future, target: asyncio.Future) -> bool:
    """Whether ``task`` is ``target`` or waits for it: it awaits ``target``, or a future that a done callback of
    ``target`` completes (a task awaiting it, ``asyncio.gather()``, ``asyncio.wait_for()``, ``asyncio.shield()``,
    ``asyncio.wait()``), and so on."""
    waiting: set[int] = set()
    pending: list[Any] = [task]
    while pending:
        current = pending.pop()
        if id(current) in waiting:
            continue
        waiting.add(id(current))
        waiter = getattr(current, "_fut_waiter", None)
        if isinstance(waiter, asyncio.Future):
            pending.append(waiter)
        pending.extend(getattr(current, "_children", None) or ())
    seen: set[int] = set()
    pending = [target]
    while pending:
        current = pending.pop()
        if id(current) in waiting:
            return True
        if id(current) in seen:
            continue
        seen.add(id(current))
        pending.extend(_completed_by(current))
    return False


def _completed_by(future: Any) -> list[asyncio.Future]:
    """The futures the done callbacks of ``future`` complete: those the callbacks are bound to, take as arguments
    (``functools.partial``) or hold in their closures."""
    found: list[asyncio.Future] = []
    for entry in list(getattr(future, "_callbacks", None) or ()):
        callback = entry[0] if isinstance(entry, tuple) else entry
        references: list[Any] = [getattr(callback, "__self__", None), *getattr(callback, "args", ())]
        function = getattr(callback, "func", callback)
        for cell in getattr(function, "__closure__", None) or ():
            try:
                references.append(cell.cell_contents)
            except ValueError:  # an empty cell
                continue
        found.extend(reference for reference in references if isinstance(reference, asyncio.Future))
    return found


class Turn:
    """A flow's turn on a `TransactionTurns`: exclusive for a transaction, shared for an operation outside one.
    ``session`` is the flow's session (``None`` for a turn taken with `TransactionTurns.take_blocking()`), ``task`` the
    task that took it; `release()` gives it up."""

    __slots__ = ("_future", "_loop", "_turns", "_what", "exclusive", "session", "task", "timeout")

    def __init__(
        self,
        turns: TransactionTurns,
        exclusive: bool,
        session: Session | None,
        task: asyncio.Task | None,
        timeout: float,
        what: str | None = None,
    ) -> None:
        self._turns = turns
        self.exclusive = exclusive
        self.session = session
        self.task = task
        self.timeout = timeout
        self._loop = asyncio.get_running_loop()
        self._future: asyncio.Future[None] = self._loop.create_future()
        self._what = what or f"{'transaction' if exclusive else 'operation'} of {session!r}"

    def __repr__(self) -> str:
        state = "held" if self.held else "waiting" if not self._future.done() else "ended"
        return f"<Turn of a {self._what}, {state}>"

    @property
    def held(self) -> bool:
        """Whether the turn is granted and not released."""
        return self in self._turns._granted

    def release(self) -> None:
        """Give up the turn; the next waiting turns that fit are granted. A second call does nothing. Called from
        another thread, the release is scheduled on the turn's event loop, unless that loop is closed."""
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop or self._loop.is_closed():
            self._turns._release(self)
        else:
            self._loop.call_soon_threadsafe(self._turns._release, self)


class TransactionTurns:
    """The turns of the transactions and the operations outside a transaction on one shared database session.

    A transaction holds the turn alone; operations outside a transaction hold it together. Turns are granted in the
    order they were asked for: an operation asked for after a waiting transaction waits for it. A flow whose context
    holds the lease of a transaction that holds the turn (`enclosed()`) takes no turn: its work nests in that
    transaction.

    A wait that cannot end raises `DeadlockError`: at once when the task of the transaction that holds the turn awaits
    the waiting task (`awaits()`, checked when the wait starts and every `DEADLOCK_CHECK_INTERVAL` seconds), else when
    the acquisition's deadline passes. ``advice`` ends the message of every `DeadlockError` it raises.
    """

    def __init__(self, name: str, advice: str = "") -> None:
        self.name = name
        self.advice = advice
        self._granted: list[Turn] = []
        self._queue: deque[Turn] = deque()
        self._check: asyncio.TimerHandle | None = None
        self._check_loop: asyncio.AbstractEventLoop | None = None
        self._held: dict[Turn, Callable[[asyncio.Task | None], tuple[Any, ...]]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def __repr__(self) -> str:
        return f"<TransactionTurns of {self.name}: {len(self._granted)} held, {self.waiting} waiting>"

    @property
    def holders(self) -> tuple[Turn, ...]:
        """The turns held now, in the order they were granted."""
        return tuple(self._granted)

    @property
    def waiting(self) -> int:
        """How many turns wait."""
        return sum(1 for turn in self._queue if not turn._future.done())

    def enclosed(self) -> bool:
        """Whether the current context holds the unreleased lease of a transaction that holds the turn
        (`adorm.executor.held_leases()`)."""
        from adorm.executor.pool import held_leases

        return any(
            turn is not None and turn._turns is self and turn.exclusive and turn.held
            for turn in (getattr(lease, "_turn", None) for lease in held_leases())
        )

    def _prune(self) -> None:
        """Drop the turns of closed event loops: nothing can release or grant them any more (a ``TestCase`` test runs
        each ``async_to_sync()`` call on a loop of its own)."""
        self._granted = [turn for turn in self._granted if not turn._loop.is_closed()]
        if any(turn._loop.is_closed() for turn in self._queue):
            self._queue = deque(turn for turn in self._queue if not turn._loop.is_closed())

    def _fits(self, turn: Turn) -> bool:
        if turn.exclusive:
            return not self._granted
        return not any(holder.exclusive for holder in self._granted)

    async def take(
        self,
        *,
        exclusive: bool,
        session: Session,
        deadline: float,
        timeout: float,
        held_sessions: Callable[[asyncio.Task | None], tuple[Any, ...]] = lambda _task: (),
    ) -> Turn:
        """Wait for a turn, exclusive for a transaction, and return it. ``deadline`` is the event loop time the wait
        ends at with `DeadlockError` (``timeout`` and ``held_sessions(task)`` describe it). Cancelling the wait gives
        up the turn, also one granted meanwhile."""
        return await self._take(
            Turn(self, exclusive, session, asyncio.current_task(), timeout), deadline, held_sessions
        )

    async def _take(
        self, turn: Turn, deadline: float, held_sessions: Callable[[asyncio.Task | None], tuple[Any, ...]]
    ) -> Turn:
        self._loop = turn._loop
        self._prune()
        if not self._queue and self._fits(turn):
            self._granted.append(turn)
            turn._future.set_result(None)
            return turn
        self._queue.append(turn)
        self._held[turn] = held_sessions
        waiting = asyncio.timeout_at(deadline)
        try:
            self._detect()
            self._schedule_check()
            async with waiting:
                await turn._future
        except BaseException as exc:
            self._abandon(turn)
            if isinstance(exc, TimeoutError) and waiting.expired():
                raise self._error(turn, f"stayed held for {turn.timeout:g} s") from None
            raise
        finally:
            self._held.pop(turn, None)
        return turn

    def take_blocking(self, what: str, timeout: float | None = None) -> Turn | None:
        """From a thread other than the event loop's, block until a shared turn is granted and return it; ``what``
        describes the waiting work in a `DeadlockError`. ``None``, without waiting, when no turn has been taken on a
        running event loop yet or when called on that loop's thread. ``timeout`` defaults to the longest one of the
        transactions that hold or wait for the turn; past it the wait raises `DeadlockError`. `Turn.release()` gives
        the turn up from the thread."""
        loop = self._loop
        if loop is None or loop.is_closed() or not loop.is_running():
            return None
        try:
            if asyncio.get_running_loop() is loop:
                return None
        except RuntimeError:
            pass

        seconds = timeout
        if seconds is None:
            transactions = [turn.timeout for turn in (*tuple(self._granted), *tuple(self._queue)) if turn.exclusive]
            seconds = max(transactions, default=_default_timeout())

        async def take() -> Turn:
            turn = Turn(self, False, None, None, seconds, what)
            return await self._take(turn, turn._loop.time() + seconds, lambda _task: ())

        future = asyncio.run_coroutine_threadsafe(take(), loop)
        try:
            return future.result(seconds + _STALLED_LOOP_GRACE)
        except concurrent.futures.TimeoutError:
            if not future.cancel() and not future.cancelled() and future.exception() is None:
                # Granted as the wait timed out.
                future.result().release()
            raise DeadlockError(
                f"the event loop of the turns on {self.name} stopped answering while a {what} waited",
                session=None,
                timeout=seconds,
            ) from None

    def _abandon(self, turn: Turn) -> None:
        future = turn._future
        if not future.done():
            future.cancel()
        elif not future.cancelled() and future.exception() is None:
            self._release(turn)
            return
        try:
            self._queue.remove(turn)
        except ValueError:
            pass
        self._grant()

    def _release(self, turn: Turn) -> None:
        try:
            self._granted.remove(turn)
        except ValueError:
            return
        self._grant()

    def _grant(self) -> None:
        self._prune()
        queue = self._queue
        while queue:
            head = queue[0]
            if head._future.done():
                queue.popleft()
                continue
            if not self._fits(head):
                break
            queue.popleft()
            self._granted.append(head)
            head._future.set_result(None)

    def _error(self, turn: Turn, what: str) -> DeadlockError:
        holders = ", ".join(holder._what for holder in self._granted) or "nobody"
        held = self._held.get(turn, lambda _task: ())(turn.task)
        message = f"the turn on {self.name} {what} while a {turn._what} waited for it (held by {holders})"
        if self.advice:
            message += f"; {self.advice}"
        return DeadlockError(message, held_sessions=held, session=turn.session, timeout=turn.timeout)

    def _detect(self) -> None:
        """Fail every waiting turn the task of a transaction that holds the turn awaits."""
        holders = [holder for holder in self._granted if holder.exclusive and holder.task is not None]
        if not holders:
            return
        for turn in self._queue:
            if turn._future.done() or turn.task is None:
                continue
            for holder in holders:
                if awaits(holder.task, turn.task):  # type: ignore[arg-type]
                    turn._future.set_exception(
                        self._error(turn, f"is held by a transaction whose task awaits {turn.task!r}")
                    )
                    break

    def _schedule_check(self) -> None:
        loop = asyncio.get_running_loop()
        if self._check is not None and self._check_loop is loop:
            return
        stale, stale_loop = self._check, self._check_loop
        if stale is not None and stale_loop is not None and not stale_loop.is_closed():
            try:
                stale_loop.call_soon_threadsafe(stale.cancel)
            except RuntimeError:
                pass  # closed meanwhile
        self._check_loop = loop
        self._check = loop.call_later(DEADLOCK_CHECK_INTERVAL, self._periodic_check, loop)

    def _periodic_check(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._check_loop is not loop:
            return  # replaced by a check on another loop
        self._check = None
        if not self.waiting:
            return
        self._detect()
        self._schedule_check()


def _default_timeout() -> float:
    from adorm.executor.pool import DEFAULT_ACQUIRE_TIMEOUT

    return DEFAULT_ACQUIRE_TIMEOUT
