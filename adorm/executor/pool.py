"""Pools of slots, one per connection profile, and the router that assigns their slots to flows."""

from __future__ import annotations

import asyncio
import inspect
import itertools
import logging
import time

from collections import deque
from collections.abc import AsyncIterator, Iterable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Any, cast, TYPE_CHECKING

from adorm.events import EventBus
from adorm.exceptions import DeadlockError, InterfaceError, SessionTimeout
from adorm.executor.options import check_profile_options, check_timeout, resolve_options, ResolvedOptions, tracking
from adorm.executor.slot import (
    CancellationOutcome,
    carry_cancellation,
    CloseReason,
    failure_outcome,
    mark_cancellation,
    Slot,
    SlotLease,
)

if TYPE_CHECKING:
    from adorm.drivers.base import Driver, DriverConnection, Profile
    from adorm.executor.sessions import Session
    from adorm.operations import Operation
    from adorm.settings import ConnectionProfile, ProfileRegistry

__all__ = ["Pool", "Router", "held_leases", "holding"]

logger = logging.getLogger("adorm.executor")

DEFAULT_ACQUIRE_TIMEOUT = 30.0
DEFAULT_COMMIT_TIMEOUT = 60.0
DEFAULT_RELEASE_TIMEOUT = 30.0
DEFAULT_PURGE_STALE_CONNECTIONS_AT = 300.0

# The event loop runs a timer up to this much early.
_CLOCK_RESOLUTION = time.get_clock_info("monotonic").resolution

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


def check_hook(hook: Any, what: str, profile_name: str, mode: str | None) -> None:
    """Raise `TypeError` unless ``hook`` is ``None`` or a callable that a driver of ``mode`` can call: a threaded
    driver calls hooks on the connection's thread, so they are plain functions."""
    if hook is None:
        return
    if not callable(hook):
        raise TypeError(f"{what} of profile {profile_name!r} is a callable or None, not {type(hook).__name__}")
    if mode == "threaded" and inspect.iscoroutinefunction(hook):
        raise TypeError(
            f"{what} of profile {profile_name!r} is a coroutine function; "
            "a threaded driver runs it on the connection's thread, so it is a plain function"
        )


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
    request waits. ``commit_timeout`` is the number of seconds a slot inside a transaction waits for its lease's next
    request before it rolls the transaction back and leaves the pool; ``None`` sets no limit.

    ``purge_stale_connections_at`` is the number of seconds a slot may stay idle: then it closes its connection and
    leaves the pool (``None``: idle slots stay until the pool closes). A request takes the idle slot returned last, so
    the slots a smaller load no longer needs reach that age. Acquiring a slot does nothing to its connection; when the
    slot leaves the pool at the end of a lease is up to the slot (`Slot`).

    The profile's ``on_acquire`` and ``on_release`` attributes, when present and not ``None``, are its hooks: the
    router calls ``on_acquire`` when it assigns a slot to a session, after setting the session's database connection
    options, the slot calls ``on_release`` when the session returns it usable. After either hook the slot sets its
    options again before the next request (`Slot`). ``release_timeout`` is the number of seconds ``on_release`` may
    run before the slot leaves the pool instead.

    The profile's ``options`` attribute, when present and not ``None``, holds its database connection options, the
    first layer of `adorm.connection_options()`; ``options`` is that mapping, checked. An option the driver does
    not declare raises `NotSupportedError` and ``acquire_timeout`` or ``commit_timeout`` among them `ValueError`.

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
        commit_timeout: float | None = DEFAULT_COMMIT_TIMEOUT,
        release_timeout: float = DEFAULT_RELEASE_TIMEOUT,
        purge_stale_connections_at: float | None = DEFAULT_PURGE_STALE_CONNECTIONS_AT,
    ) -> None:
        if isinstance(max_connections, bool) or not isinstance(max_connections, int) or max_connections < 0:
            raise ValueError(f"max_connections is a non-negative integer, not {max_connections!r}")
        self.driver = driver
        self.profile = profile
        self.max_connections = max_connections
        self.acquire_timeout = check_timeout(acquire_timeout, "acquire_timeout")
        self.commit_timeout = None if commit_timeout is None else check_timeout(commit_timeout, "commit_timeout")
        self.release_timeout = check_timeout(release_timeout, "release_timeout")
        self.purge_stale_connections_at = (
            None
            if purge_stale_connections_at is None
            else check_timeout(purge_stale_connections_at, "purge_stale_connections_at")
        )
        check_hook(getattr(profile, "connect", None), "connect", profile.name, driver.mode)
        self.on_acquire = self._hook(profile, "on_acquire")
        self.on_release = self._hook(profile, "on_release")
        self.options = check_profile_options(getattr(profile, "options", None), profile.name)
        driver.capabilities.check_options(
            [name for name, value in self.options.items() if value is not None], driver.name
        )
        self.events = EventBus()
        self.closed = False
        self.retired = False
        self.router: Router | None = None
        self._slots: set[Slot] = set()
        self._idle: deque[Slot] = deque()
        self._opening = 0
        self._refused_at: int | None = None
        self._waiters: deque[_Waiter] = deque()
        self._closing: set[asyncio.Task] = set()
        self._purge_handle: asyncio.TimerHandle | None = None

    def _hook(self, profile: Profile, what: str) -> Any:
        hook = getattr(profile, what, None)
        check_hook(hook, what, profile.name, self.driver.mode)
        return hook

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
        """The idle slot returned last; stale and unusable idle slots met on the way leave the pool."""
        now = asyncio.get_running_loop().time()
        while self._idle:
            slot = self._idle.pop()
            if self._stale(slot, now):
                self._discard(slot, CloseReason.STALE)
            elif not slot.is_usable():
                self._discard(slot, CloseReason.UNUSABLE)
            else:
                slot.idle_since = None
                self._schedule_purge()
                return slot
        self._schedule_purge()
        return None

    def _stale(self, slot: Slot, now: float) -> bool:
        after = self.purge_stale_connections_at
        return after is not None and slot.idle_since is not None and now - slot.idle_since >= after

    def _schedule_purge(self) -> None:
        """Have `_purge()` run when the oldest idle slot becomes stale."""
        handle, after = self._purge_handle, self.purge_stale_connections_at
        if after is None or self.closed or not self._idle:
            if handle is not None:
                handle.cancel()
                self._purge_handle = None
            return
        when = cast("float", self._idle[0].idle_since) + after
        if handle is not None:
            if handle.when() == when:
                return
            handle.cancel()
        self._purge_handle = asyncio.get_running_loop().call_at(when, self._purge)

    def _purge(self) -> None:
        """Close the idle slots that are stale; the oldest are first in the idle queue."""
        self._purge_handle = None
        now = asyncio.get_running_loop().time() + _CLOCK_RESOLUTION
        while self._idle and self._stale(self._idle[0], now):
            self._discard(self._idle.popleft(), CloseReason.STALE)
        self._schedule_purge()

    def _discard(self, slot: Slot, reason: CloseReason) -> None:
        self._slots.discard(slot)
        self._track(asyncio.get_running_loop().create_task(slot.close(reason)))
        self._refused_at = None

    def _track(self, task: asyncio.Task) -> None:
        """Have `close()` wait for ``task``, which closes a slot or returns it to the pool."""
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

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
        slot.idle_since = asyncio.get_running_loop().time()
        self._idle.append(slot)
        self._schedule_purge()

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
        idle slots, the slots being discarded and the released slots whose connections are settling are closed. A held
        slot closes when its lease is released, a slot still opening as soon as it is open."""
        if self.closed:
            return
        self.closed = True
        self._schedule_purge()
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.future.done():
                waiter.future.set_exception(InterfaceError(f"pool {self.name!r} is closed"))
        idle, self._idle = list(self._idle), deque()
        for slot in idle:
            self._slots.discard(slot)
        await asyncio.gather(
            *(slot.close(CloseReason.POOL_CLOSED) for slot in idle), *self._closing, return_exceptions=True
        )


class Router:
    """Assigns the slots of its pools, one pool per connection profile, to the sessions that request them.

    Every acquisition has a timeout. It raises `SessionTimeout`, or `DeadlockError` when the waiting flow
    already holds a slot (a lease its task acquired, or one recorded with `holding()`). When every slot of the
    pool a flow waits for is held through `slot()` or `run()` by tasks that themselves wait for a slot that cannot
    become free, one of those tasks gets `DeadlockError` at once.

    With ``profiles``, a `ProfileRegistry`, a session whose ``using`` no added pool serves gets a pool created
    from the registry's profile of that name (`ProfileRegistry.resolve()`), with the profile's pool settings or the
    registry's. When that profile is unregistered or replaced, or the registry's default pool settings change for a
    profile without its own, the router retires its pool: it stops routing to it and closes it. The next acquisition,
    and one waiting on the retired pool, uses a pool created from the current profile.

    A router serves one event loop. It is a `Runner`: ``set_runner(router)`` makes it run the operations of
    sessions outside a transaction.
    """

    def __init__(self, pools: Iterable[Pool] = (), *, profiles: ProfileRegistry | None = None) -> None:
        self._pools: dict[str, Pool] = {}
        self._waiting: dict[asyncio.Task, _Waiter] = {}
        self.profiles = profiles
        self._loop: asyncio.AbstractEventLoop | None = None
        self._resolving: dict[str, asyncio.Future[Pool]] = {}
        self._from_profiles: set[Pool] = set()
        self._retiring: set[asyncio.Task] = set()
        for pool in pools:
            self.add(pool)
        if profiles is not None:
            profiles._attach(self)

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

    async def get_pool(self, using: str) -> Pool:
        """The pool of the profile ``using``, created from ``profiles`` when no pool serves it. Raises
        `InterfaceError` when there is no such pool or profile, and the error of creating the profile's driver."""
        pool = self._pools.get(using)
        if pool is not None:
            return pool
        if self.profiles is None:
            raise InterfaceError(f"no pool serves connection profile {using!r}")
        self._loop = asyncio.get_running_loop()
        while True:
            future = self._resolving.get(using)
            if future is None:
                break
            # Another task creates the pool; a creator that was cancelled leaves it to the next one.
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                if not future.cancelled():
                    raise
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
            pool = self._pools.get(using)
            if pool is not None:
                return pool
        future = self._loop.create_future()
        self._resolving[using] = future
        try:
            pool = await self._create_pool(using)
        except asyncio.CancelledError:
            future.cancel()
            raise
        except BaseException as exc:
            future.set_exception(exc)
            future.exception()  # retrieved here: the creating task raises it, waiters re-raise it
            raise
        finally:
            del self._resolving[using]
        future.set_result(pool)
        return pool

    async def _create_pool(self, using: str) -> Pool:
        registry = cast("ProfileRegistry", self.profiles)
        profile: ConnectionProfile = await registry.resolve(using)
        settings = registry.pool if profile.pool is None else profile.pool
        pool = Pool(profile.get_driver(), profile, **settings.as_kwargs())
        existing = self._pools.get(using)
        if existing is not None:  # added meanwhile
            return existing
        self.add(pool)
        self._from_profiles.add(pool)
        if registry.get(using) is not profile:
            # Unregistered or replaced while the pool was created.
            self._retire(using, pool)
        return pool

    def _profile_changed(self, name: str) -> None:
        """Retire the pool created from the profile ``name``; callable from any thread."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        if asyncio._get_running_loop() is loop:
            self._retire(name)
        else:
            try:
                loop.call_soon_threadsafe(self._retire, name)
            except RuntimeError:
                pass  # the loop closed meanwhile

    def _retire(self, name: str, pool: Pool | None = None) -> None:
        pool = self._pools.get(name) if pool is None else pool
        if pool is None or pool not in self._from_profiles or self._pools.get(name) is not pool:
            return
        del self._pools[name]
        self._from_profiles.discard(pool)
        pool.retired = True
        task = asyncio.get_running_loop().create_task(pool.close())
        self._retiring.add(task)
        task.add_done_callback(self._retiring.discard)

    async def remove(self, using: str) -> Pool:
        """Stop routing the sessions of ``using`` to its pool, close the pool (`Pool.close()`) and return it. Raises
        `InterfaceError` when no pool serves ``using``. A router with ``profiles`` creates a new pool for the next
        acquisition."""
        pool = self.pool(using)
        del self._pools[using]
        self._from_profiles.discard(pool)
        await pool.close()
        return pool

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

        ``timeout`` (seconds) defaults to the ``acquire_timeout`` connection option in effect (`resolve_options()`:
        the pool's ``acquire_timeout`` unless the session or a `connection_options()` block sets one). The lease's
        ``commit_timeout`` is the ``commit_timeout`` option in effect. Raises `NotSupportedError` before waiting for a
        database option in effect that the driver does not declare, `SessionTimeout` or `DeadlockError`,
        `InterfaceError` when the pool is closed, and the driver's error when opening a connection fails for another
        reason than a refusal.
        """
        pool = await self.get_pool(session.using)
        return await self._acquire(pool, session, timeout, resolve_options(session, pool))

    async def _acquire(
        self, pool: Pool, session: Session, timeout: float | None, options: ResolvedOptions
    ) -> SlotLease:
        """Acquire a slot of ``pool``; when ``pool`` is retired after a profile change, of the pool that serves the
        session's profile now, within the same deadline."""
        timeout = options.acquire_timeout if timeout is None else check_timeout(timeout, "timeout")
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            try:
                return await self._acquire_from(pool, session, deadline, timeout, options)
            except InterfaceError:
                if not pool.retired:
                    raise
            pool = await self.get_pool(session.using)
            options = resolve_options(session, pool)

    async def _acquire_from(
        self, pool: Pool, session: Session, deadline: float, timeout: float, options: ResolvedOptions
    ) -> SlotLease:
        pool.driver.capabilities.check_options(options.database, pool.driver.name)
        if pool.closed or pool.retired:
            raise InterfaceError(f"pool {pool.name!r} is closed")
        task = asyncio.current_task()
        slot = pool._take_idle()
        refused = False
        if slot is None and not pool._waiters and pool._can_open():
            pool._opening += 1
            slot = await self._open(pool, session, deadline, timeout)
            refused = slot is None
        if slot is None:
            slot = await self._wait(pool, session, task, deadline, timeout, front=refused)
        lease = slot._lease_to(session, task)
        lease.commit_timeout = options.commit_timeout
        if pool.on_acquire is not None:
            await self._on_acquire(pool, lease, deadline, timeout, options)
        return lease

    async def _on_acquire(
        self, pool: Pool, lease: SlotLease, deadline: float, timeout: float, options: ResolvedOptions
    ) -> None:
        """Set the session's database options, then run the profile's ``on_acquire`` hook, within the acquisition's
        deadline; on failure discard the slot."""
        slot = lease.slot
        connection = cast("DriverConnection", slot.connection)
        hook_timeout = asyncio.timeout_at(deadline)
        try:
            async with hook_timeout:
                await slot.set_options_for(options.database)
                await connection.call_hook(pool.on_acquire, pool.profile, lease.session)
            slot.forget_options()
        except BaseException as exc:
            # The hook may have left the connection in any state.
            await lease._release(CloseReason.HOOK_FAILED)
            if isinstance(exc, TimeoutError) and hook_timeout.expired():
                message = f"on_acquire of profile {pool.name!r} did not finish within {timeout:g} s"
                raise self._timeout(lease.session, timeout, message) from None
            raise

    async def _acquire_bound(
        self, session: Session, timeout: float | None, pool: Pool | None = None, options: ResolvedOptions | None = None
    ) -> SlotLease:
        """`acquire()` a lease that only the acquiring task releases, so it counts for immediate deadlocks."""
        if pool is None:
            pool = await self.get_pool(session.using)
        if options is None:
            options = resolve_options(session, pool)
        lease = await self._acquire(pool, session, timeout, options)
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

    def _timeout(self, session: Session, timeout: float, message: str | None = None) -> SessionTimeout:
        held = self.held_sessions()
        if held:
            if message is not None:
                message += f" for {session!r} while holding {', '.join(map(repr, held))}"
            return DeadlockError(message, held_sessions=held, session=session, timeout=timeout)
        if message is not None:
            message += f" for {session!r}"
        return SessionTimeout(message, session=session, timeout=timeout)

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
        """Acquire a slot for ``session``, execute ``operation``, release the slot and return the result.

        An operation the pool's driver does not support, or a database connection option in effect that it does not
        declare, raises `NotSupportedError` before a slot is acquired (`Driver.check()`). The slot sets the database
        options in effect (`resolve_options()`) before it executes the operation. A cancellation carries the
        operation's `CancellationOutcome`, final when the ``CancelledError`` is raised: `NOT_STARTED` while it waits
        for a slot, else that of `SlotLease.call()`. The enclosing `adorm.timeout()` blocks see the operation in
        the phase ``"waiting_for_slot"``, then ``"executing"``."""
        try:
            pool = await self.get_pool(session.using)
            pool.driver.check(operation)
            options = resolve_options(session, pool)
            pool.driver.capabilities.check_options(options.database, pool.driver.name)
            with tracking("waiting_for_slot", operation, session):
                lease = await self._acquire_bound(session, None, pool, options)
            if lease.pool is not pool:
                # acquired from the pool that replaced a retired one
                pool = lease.pool
                try:
                    pool.driver.check(operation)
                    options = resolve_options(session, pool)
                except BaseException:
                    await lease.release()
                    raise
        except asyncio.CancelledError as exc:
            mark_cancellation(exc, CancellationOutcome.NOT_STARTED)
            raise
        cancelled: asyncio.CancelledError | None = None
        outcome = CancellationOutcome.UNKNOWN
        try:
            with tracking("executing", operation, session):
                result = await lease.call("execute", operation, options=options.database)
            outcome = CancellationOutcome.SUCCEEDED
        except asyncio.CancelledError as exc:
            cancelled = exc
            raise
        except Exception as exc:
            outcome = failure_outcome(exc)
            raise
        finally:
            try:
                await lease.release()
            except asyncio.CancelledError as exc:
                # Cancelled again, or only now: the outcome is the operation's, final once the slot is returned.
                await lease._wait_returned()
                if cancelled is not None:
                    carry_cancellation(cancelled, exc)
                else:
                    mark_cancellation(exc, outcome)
                raise
        return result

    async def close(self) -> None:
        """Close every pool, those retired after a profile change included."""
        await asyncio.gather(*(pool.close() for pool in self._pools.values()), *self._retiring)
