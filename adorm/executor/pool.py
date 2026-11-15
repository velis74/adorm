"""Pools of slots, one per connection profile, and the router that assigns their slots to flows."""

from __future__ import annotations

import asyncio
import itertools
import logging
import math

from collections import deque
from collections.abc import AsyncIterator, Iterable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Any, TYPE_CHECKING

from adorm.events import EventBus
from adorm.exceptions import DeadlockError, InterfaceError, SessionTimeout
from adorm.executor.slot import Slot, SlotLease

if TYPE_CHECKING:
    from adorm.drivers.base import Driver, Profile
    from adorm.executor.sessions import Session
    from adorm.operations import Operation

__all__ = ["Pool", "Router", "held_leases", "holding"]

logger = logging.getLogger("adorm.executor")

DEFAULT_ACQUIRE_TIMEOUT = 30.0

_held: ContextVar[tuple[SlotLease, ...]] = ContextVar("adorm_held_leases", default=())


def held_leases() -> tuple[SlotLease, ...]:
    """The unreleased leases the current context holds: those of enclosing `holding()` blocks and
    `Router.slot()` blocks, inherited by tasks created inside them."""
    return tuple(lease for lease in _held.get() if not lease.released)


@contextmanager
def holding(lease: SlotLease) -> Iterator[SlotLease]:
    """Record for the ``with`` block that the current context holds ``lease``.

    Tasks created inside the block inherit the record. A flow that holds a lease and waits for another slot gets
    `DeadlockError` instead of `SessionTimeout`.
    """
    if not isinstance(lease, SlotLease):
        raise TypeError(f"holding() takes a SlotLease, not {type(lease).__name__}")
    token = _held.set((*_held.get(), lease))
    try:
        yield lease
    finally:
        _held.reset(token)


def _check_timeout(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0 or math.isinf(value):
        raise ValueError(f"{what} is a positive, finite number of seconds, not {value!r}")
    return float(value)


_OPEN = object()  # handed to a waiter instead of a slot: it may open a connection, the capacity is reserved
_sequence = itertools.count()


class _Waiter:
    __slots__ = ("future", "pool", "sequence", "session", "task", "timeout")

    def __init__(
        self, pool: Pool, task: asyncio.Task | None, session: Session, timeout: float, sequence: int | None = None
    ) -> None:
        self.pool = pool
        self.task = task
        self.session = session
        self.timeout = timeout
        self.future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.sequence = next(_sequence) if sequence is None else sequence


class Pool:
    """The slots of one connection profile.

    Slots open on demand, up to ``max_connections``; ``0`` opens new ones until the database refuses a connection
    (`Driver.is_refusal()`). After a refusal the pool holds at most as many slots as it had then, whatever
    ``max_connections`` is, until a slot leaves the pool. A request finds an idle slot, opens a new one, or waits in
    a first-in, first-out queue for a slot to be returned. ``acquire_timeout`` is the default number of seconds a
    request waits.

    A pool serves one event loop; its slots are acquired through a `Router`. ``events`` receives the callbacks and
    backend events a connection emits while no lease holds its slot.
    """

    def __init__(
        self,
        driver: Driver,
        profile: Profile,
        *,
        max_connections: int = 0,
        acquire_timeout: float = DEFAULT_ACQUIRE_TIMEOUT,
    ) -> None:
        if isinstance(max_connections, bool) or not isinstance(max_connections, int) or max_connections < 0:
            raise ValueError(f"max_connections is a non-negative integer, not {max_connections!r}")
        self.driver = driver
        self.profile = profile
        self.max_connections = max_connections
        self.acquire_timeout = _check_timeout(acquire_timeout, "acquire_timeout")
        self.events = EventBus()
        self.closed = False
        self.router: Router | None = None
        self._slots: set[Slot] = set()
        self._idle: deque[Slot] = deque()
        self._opening = 0
        self._refused_at: int | None = None
        self._waiters: deque[_Waiter] = deque()
        self._closing: set[asyncio.Task] = set()

    @property
    def name(self) -> str:
        """The profile name, the ``using`` of the sessions the pool serves."""
        return self.profile.name

    @property
    def limit(self) -> int | None:
        """The current maximum number of slots: the lower of ``max_connections`` and the slot count at the last
        refusal, ``None`` when neither applies."""
        limits = [limit for limit in (self.max_connections or None, self._refused_at) if limit is not None]
        return min(limits) if limits else None

    @property
    def size(self) -> int:
        """Slots in the pool, opening ones included."""
        return len(self._slots) + self._opening

    @property
    def idle(self) -> int:
        """Slots no lease holds."""
        return len(self._idle)

    @property
    def in_use(self) -> int:
        """Slots a lease holds or that are returning to the pool."""
        return len(self._slots) - len(self._idle)

    @property
    def waiting(self) -> int:
        """Requests waiting for a slot."""
        return sum(1 for waiter in self._waiters if not waiter.future.done())

    def leases(self) -> list[SlotLease]:
        """The leases currently holding a slot of this pool."""
        return [slot.lease for slot in self._slots if slot.lease is not None and not slot.lease.released]

    def __repr__(self) -> str:
        return f"<Pool {self.name!r} size={self.size} idle={self.idle} waiting={self.waiting}>"

    def _can_open(self) -> bool:
        limit = self.limit
        return not self.closed and (limit is None or self.size < limit)

    def _take_idle(self) -> Slot | None:
        while self._idle:
            slot = self._idle.popleft()
            if slot.is_usable():
                return slot
            self._discard(slot)
        return None

    def _discard(self, slot: Slot) -> None:
        self._slots.discard(slot)
        task = asyncio.get_running_loop().create_task(slot.close())
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)
        self._refused_at = None

    async def _open(self) -> Slot:
        """Open a slot in capacity reserved by the caller (``_opening``)."""
        slot = Slot(self)
        try:
            await slot.open()
        except BaseException as exc:
            self._opening -= 1
            if self._refused(exc) and self._slots:
                self._refused_at = len(self._slots) + self._opening
            self._capacity_freed()
            raise
        self._opening -= 1
        if self.closed:
            await slot.close()
            raise InterfaceError(f"pool {self.name!r} is closed")
        self._slots.add(slot)
        return slot

    def _refused(self, exc: BaseException) -> bool:
        try:
            return isinstance(exc, Exception) and bool(self.driver.is_refusal(exc))
        except Exception:
            logger.exception("%s.is_refusal failed", type(self.driver).__name__)
            return False

    def _returned(self, slot: Slot) -> None:
        """A slot returned in a usable state: hand it to the first waiter, else make it idle."""
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.future.done():
                waiter.future.set_result(slot)
                return
        self._idle.append(slot)

    def _removed(self, slot: Slot) -> None:
        """A slot left the pool after its connection closed."""
        self._slots.discard(slot)
        self._refused_at = None
        self._capacity_freed()

    def _capacity_freed(self) -> None:
        """Let the first waiter open a connection in capacity that became free."""
        while self._waiters and self._can_open():
            waiter = self._waiters.popleft()
            if not waiter.future.done():
                self._opening += 1
                waiter.future.set_result(_OPEN)
                return

    async def close(self) -> None:
        """Close idle slots and refuse further requests; waiting requests raise `InterfaceError`. Returns once the
        idle slots and the slots being discarded are closed. A held slot closes when its lease is released, a slot
        still opening as soon as it is open."""
        if self.closed:
            return
        self.closed = True
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.future.done():
                waiter.future.set_exception(InterfaceError(f"pool {self.name!r} is closed"))
        idle, self._idle = list(self._idle), deque()
        for slot in idle:
            self._slots.discard(slot)
        await asyncio.gather(*(slot.close() for slot in idle), *self._closing, return_exceptions=True)


class Router:
    """Assigns the slots of its pools, one pool per connection profile, to the sessions that request them.

    Every acquisition has a timeout. It raises `SessionTimeout`, or `DeadlockError` when the waiting flow
    already holds a slot (a lease its task acquired, or one recorded with `holding()`). When every slot of the
    pool a flow waits for is held through `slot()` or `run()` by tasks that themselves wait for a slot that cannot
    become free, one of those tasks gets `DeadlockError` at once.

    A router serves one event loop. It is a `Runner`: ``set_runner(router)`` makes it run the operations of
    sessions outside a transaction.
    """

    def __init__(self, pools: Iterable[Pool] = ()) -> None:
        self._pools: dict[str, Pool] = {}
        self._waiting: dict[asyncio.Task, _Waiter] = {}
        for pool in pools:
            self.add(pool)

    def add(self, pool: Pool) -> Pool:
        """Route the sessions of ``pool.name`` to ``pool``. A pool belongs to one router; a name to one pool."""
        if not isinstance(pool, Pool):
            raise TypeError(f"a router takes Pool objects, not {type(pool).__name__}")
        if pool.router is not None:
            raise ValueError(f"{pool!r} already belongs to a router")
        if pool.name in self._pools:
            raise ValueError(f"a pool for profile {pool.name!r} is already routed")
        pool.router = self
        self._pools[pool.name] = pool
        return pool

    def pool(self, using: str) -> Pool:
        """The pool of the profile ``using``. Raises `InterfaceError` when there is none."""
        try:
            return self._pools[using]
        except KeyError:
            raise InterfaceError(f"no pool serves connection profile {using!r}") from None

    @property
    def pools(self) -> tuple[Pool, ...]:
        """The routed pools."""
        return tuple(self._pools.values())

    def held_sessions(self, task: asyncio.Task | None = None) -> tuple[Session, ...]:
        """The sessions of the unreleased leases a flow holds: those ``task`` acquired and, when ``task`` is the
        current task (the default), those of the current context's `held_leases()`."""
        current = asyncio.current_task()
        task = current if task is None else task
        leases = [lease for pool in self._pools.values() for lease in pool.leases() if lease.owner is task]
        if task is current:
            leases += [lease for lease in held_leases() if lease not in leases]
        sessions: list[Session] = []
        for lease in leases:
            if lease.session not in sessions:
                sessions.append(lease.session)
        return tuple(sessions)

    async def acquire(self, session: Session, *, timeout: float | None = None) -> SlotLease:
        """Acquire a slot of ``session.using``'s pool for ``session``; release it with `SlotLease.release()`.

        ``timeout`` (seconds) defaults to the pool's ``acquire_timeout``. Raises `SessionTimeout` or
        `DeadlockError`, `InterfaceError` when the pool is closed, and the driver's error when opening a
        connection fails for another reason than a refusal.
        """
        pool = self.pool(session.using)
        timeout = pool.acquire_timeout if timeout is None else _check_timeout(timeout, "timeout")
        if pool.closed:
            raise InterfaceError(f"pool {pool.name!r} is closed")
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        slot = pool._take_idle()
        refused = False
        if slot is None and not pool._waiters and pool._can_open():
            pool._opening += 1
            slot = await self._open(pool, session, deadline, timeout)
            refused = slot is None
        if slot is None:
            slot = await self._wait(pool, session, task, deadline, timeout, front=refused)
        return slot._lease_to(session, task)

    async def _acquire_bound(self, session: Session, timeout: float | None) -> SlotLease:
        """`acquire()` a lease that only the acquiring task releases, so it counts for immediate deadlocks."""
        lease = await self.acquire(session, timeout=timeout)
        lease._bound = True
        return lease

    async def _open(self, pool: Pool, session: Session, deadline: float, timeout: float) -> Slot | None:
        """Open a slot in reserved capacity; ``None`` when the database refused and other slots exist."""
        loop = asyncio.get_running_loop()
        try:
            async with asyncio.timeout_at(deadline):
                return await pool._open()
        except TimeoutError:
            raise self._timeout(session, timeout) from None
        except Exception as exc:
            if not pool._refused(exc) or not pool._slots:
                raise
            if loop.time() >= deadline:
                raise self._timeout(session, timeout) from None
            return None

    async def _wait(
        self,
        pool: Pool,
        session: Session,
        task: asyncio.Task | None,
        deadline: float,
        timeout: float,
        *,
        front: bool = False,
    ) -> Slot:
        """Wait in the pool's queue; ``front`` for a request whose open was refused, which keeps its place."""
        loop = asyncio.get_running_loop()
        sequence: int | None = None
        while True:
            waiter = _Waiter(pool, task, session, timeout, sequence)
            sequence = waiter.sequence
            if front:
                pool._waiters.appendleft(waiter)
            else:
                pool._waiters.append(waiter)
            front = True
            if task is not None:
                self._waiting[task] = waiter
            try:
                pool._capacity_freed()
                self._detect_deadlock()
                async with asyncio.timeout_at(deadline):
                    result = await waiter.future
            except BaseException as exc:
                self._abandon(waiter)
                if isinstance(exc, TimeoutError) and not isinstance(exc, SessionTimeout):
                    raise self._timeout(session, timeout) from None
                raise
            finally:
                if task is not None and self._waiting.get(task) is waiter:
                    del self._waiting[task]
            if result is not _OPEN:
                return result
            slot = await self._open(pool, session, deadline, timeout)
            if slot is not None:
                return slot
            if loop.time() >= deadline:
                raise self._timeout(session, timeout)

    def _abandon(self, waiter: _Waiter) -> None:
        """Remove a waiter that stops waiting; pass on what it was handed meanwhile."""
        pool = waiter.pool
        try:
            pool._waiters.remove(waiter)
        except ValueError:
            pass
        future = waiter.future
        if not future.done():
            future.cancel()
            return
        if future.cancelled() or future.exception() is not None:
            self._detect_deadlock()
            return
        result = future.result()
        if result is _OPEN:
            pool._opening -= 1
            pool._capacity_freed()
        else:
            pool._returned(result)
        self._detect_deadlock()

    def _timeout(self, session: Session, timeout: float) -> SessionTimeout:
        held = self.held_sessions()
        if held:
            return DeadlockError(held_sessions=held, session=session, timeout=timeout)
        return SessionTimeout(session=session, timeout=timeout)

    def _detect_deadlock(self) -> None:
        """Fail one task of a set of waiting tasks none of which can get a slot.

        A waiting task is stuck when its pool cannot open a slot, has no idle or returning slot, and every lease
        of the pool is bound (taken through `slot()` or `run()`) and belongs to a stuck task. The stuck set is
        what remains after removing, until nothing changes, every waiter that does not meet this. The failed task
        is the newest stuck one that holds a slot of a pool a stuck task waits for, so that its release lets the
        others proceed.
        """
        stuck = {task: waiter for task, waiter in self._waiting.items() if not waiter.future.done()}
        changed = True
        while changed and stuck:
            changed = False
            for task, waiter in list(stuck.items()):
                pool = waiter.pool
                leases = pool.leases()
                if (
                    pool._can_open()
                    or pool._opening
                    or pool._idle
                    or len(leases) < len(pool._slots)
                    or any(not lease._bound or lease.owner not in stuck for lease in leases)
                ):
                    del stuck[task]
                    changed = True
        if not stuck:
            return
        holders = {lease.owner for pool in {w.pool for w in stuck.values()} for lease in pool.leases()}
        candidates = [item for item in stuck.items() if item[0] in holders]
        if not candidates:
            return
        task, waiter = max(candidates, key=lambda item: item[1].sequence)
        held = self.held_sessions(task)
        pools = sorted({w.pool.name for w in stuck.values()})
        error = DeadlockError(
            f"deadlock: every slot of {', '.join(map(repr, pools))} is held by a flow waiting for a slot"
            + (f"; this flow holds {', '.join(map(repr, held))}" if held else ""),
            held_sessions=held,
            session=waiter.session,
            timeout=waiter.timeout,
        )
        waiter.future.set_exception(error)

    @asynccontextmanager
    async def slot(self, session: Session, *, timeout: float | None = None) -> AsyncIterator[SlotLease]:
        """Acquire a slot for ``session`` for the ``async with`` block, recorded with `holding()`, and release it
        on exit. The lease counts for deadlocks raised at once; one from `acquire()` does not, as any task may
        release it."""
        lease = await self._acquire_bound(session, timeout)
        try:
            with holding(lease):
                yield lease
        finally:
            await lease.release()

    async def run(self, session: Session, operation: Operation) -> Any:
        """Acquire a slot for ``session``, execute ``operation``, release the slot and return the result."""
        lease = await self._acquire_bound(session, None)
        try:
            return await lease.call("execute", operation)
        finally:
            await lease.release()

    async def close(self) -> None:
        """Close every pool."""
        await asyncio.gather(*(pool.close() for pool in self._pools.values()))
