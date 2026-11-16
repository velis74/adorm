"""Listening for backend notifications on a slot held for the purpose."""

from __future__ import annotations

import asyncio
import contextlib

from collections import deque
from collections.abc import AsyncIterator
from typing import Any, TYPE_CHECKING

from adorm.drivers.base import check_channel
from adorm.events import BackendEvent, CONNECTION_LOST, NOTIFY, OUTCOME_EVENTS
from adorm.exceptions import InterfaceError

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from adorm.executor.pool import Pool
    from adorm.executor.sessions import Session
    from adorm.executor.slot import SlotLease

__all__ = ["Listener", "listen"]

_NEW, _OPEN, _CLOSED = "new", "open", "closed"


def listen(*channels: str, session: Session | None = None) -> Listener:
    """A `Listener` for ``channels`` on ``session`` (default: the current session): ``async with adorm.listen("orders")
    as listener:``."""
    return Listener(*channels, session=session)


class Listener:
    """Notifications of backend channels (PostgreSQL's ``LISTEN`` / ``NOTIFY``), received on a slot the block holds.

    Entering acquires a slot for ``session`` from the `TransactionRunner` (`get_runner()`), as `adorm.atomic()` does
    (`SessionTimeout`, `DeadlockError`), and has its connection listen on ``channels``; leaving releases the slot, and
    the slot closes the connection while it still listens. A driver that cannot listen (`Driver.check_listen()`)
    raises `NotSupportedError` before a slot is taken. While the block runs, its flow holds the slot: an operation of
    the same flow that needs another slot of a full pool raises `DeadlockError`. Only the task that entered the block
    may leave it.

    `get()` and ``async for`` return each notification as ``BackendEvent(NOTIFY, channel, payload)``, in the order
    the connection received them; callbacks and other backend events of the slot go to the pool's `EventBus`. When
    the connection is lost (`CONNECTION_LOST`), its slot closes or its pool closes, the notifications received before
    are returned first, then `get()` raises `InterfaceError` in every waiting and later call. Leaving the block makes
    every waiting `get()` raise `InterfaceError`.
    """

    def __init__(self, *channels: str, session: Session | None = None) -> None:
        from adorm.executor.sessions import current_session

        self._initial = tuple(dict.fromkeys(check_channel(channel) for channel in channels))
        self.session = current_session() if session is None else session
        self._order: tuple[str, ...] = ()
        self._lease: SlotLease | None = None
        self._slot_cm: AbstractAsyncContextManager[SlotLease] | None = None
        self._watcher: asyncio.Task | None = None
        self._error: InterfaceError | None = None
        self._held: deque[BackendEvent] = deque()
        self._stopped = asyncio.Event()
        self._state = _NEW

    def __repr__(self) -> str:
        return f"<Listener {', '.join(self._order or self._initial)} on {self.session!r}, {self._state}>"

    @property
    def channels(self) -> tuple[str, ...]:
        """The channels the connection listens on, in the order they were added."""
        listening = self._listening()
        return tuple(channel for channel in self._order if channel in listening)

    @property
    def lease(self) -> SlotLease | None:
        """The lease of the slot the block holds; ``None`` outside the block."""
        return self._lease if self._state == _OPEN else None

    def _listening(self) -> frozenset[str]:
        lease = self._lease
        connection = None if lease is None else lease.slot.connection
        return frozenset() if connection is None else connection.listening

    async def __aenter__(self) -> Listener:
        from adorm.executor.sessions import get_runner, TransactionRunner

        if self._state != _NEW:
            raise InterfaceError(f"{self!r} was already entered")
        self._state = _CLOSED
        runner = get_runner()
        if not isinstance(runner, TransactionRunner):
            raise InterfaceError(f"no runner holds a slot for listening on {self.session!r} (runner {runner!r})")
        get_pool = getattr(runner, "get_pool", None)
        checked = get_pool is not None
        if checked:
            await self._check(await get_pool(self.session.using))
        slot_cm = runner.slot(self.session)
        lease = await slot_cm.__aenter__()
        try:
            if not checked:
                await self._check(lease.pool)
            self._lease, self._slot_cm = lease, slot_cm
            await self._listen(lease, self._initial)
        except BaseException:
            self._lease = self._slot_cm = None
            await slot_cm.__aexit__(None, None, None)
            raise
        self._watcher = asyncio.get_running_loop().create_task(self._watch(lease))
        self._state = _OPEN
        return self

    async def _check(self, pool: Pool) -> None:
        await pool.driver.check_listen(pool.profile)
        for channel in self._initial:
            pool.driver.capabilities.check_channel(channel)

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Release the slot; the slot closes the connection when it still listens. A waiting `get()` raises
        `InterfaceError`. A second call does nothing."""
        if self._state != _OPEN:
            return
        self._state = _CLOSED
        self._held.clear()
        self._stopped.set()
        watcher, self._watcher = self._watcher, None
        if watcher is not None:
            watcher.cancel()
        slot_cm, self._slot_cm = self._slot_cm, None
        self._lease = None
        if slot_cm is not None:
            await slot_cm.__aexit__(None, None, None)

    async def _watch(self, lease: SlotLease) -> None:
        """End the waits once the slot or its pool closes."""
        slot_closed = asyncio.ensure_future(lease.slot.wait_closed())
        pool_closed = asyncio.ensure_future(lease.pool.wait_closed())
        try:
            await asyncio.wait({slot_closed, pool_closed}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            slot_closed.cancel()
            pool_closed.cancel()
        self._watched(lease)

    def _watched(self, lease: SlotLease) -> None:
        if lease.slot.state == "closed":
            self._stop(lease, f"{lease.slot!r} closed")
        elif lease.pool.closed:
            self._stop(lease, f"pool {lease.pool.name!r} is closed")

    def _stop(self, lease: SlotLease, reason: str) -> None:
        """Keep the notifications received so far for `get()`, then have it raise `InterfaceError` with ``reason``."""
        if self._error is not None:
            return
        self._error = InterfaceError(f"{self!r} ended: {reason}")
        while not lease.events.empty():
            self._take(lease, lease.events.get_nowait())
        self._stopped.set()

    def _take(self, lease: SlotLease, event: Any) -> None:
        """Keep a notification for `get()`, stop on `CONNECTION_LOST` and hand anything else to the pool."""
        if isinstance(event, BackendEvent):
            if event.name == NOTIFY:
                self._held.append(event)
                return
            if event.name == CONNECTION_LOST:
                self._stop(lease, f"the connection was lost: {event.payload}")
                return
        if not isinstance(event, OUTCOME_EVENTS):  # the outcome of a cancelled listen() or unlisten()
            lease.pool.events.dispatch(event)

    def _open_lease(self) -> SlotLease:
        if self._state != _OPEN or self._lease is None:
            if self._error is not None:
                raise InterfaceError(str(self._error))
            raise InterfaceError(f"{self!r} is not open")
        if self._error is not None:
            raise InterfaceError(str(self._error))
        return self._lease

    async def _listen(self, lease: SlotLease, channels: tuple[str, ...]) -> None:
        for channel in channels:
            if channel in self._listening():
                continue
            if channel not in self._order:
                self._order = (*self._order, channel)
            await lease.call("listen", channel)

    async def listen(self, *channels: str) -> None:
        """Listen on ``channels`` too; a channel already listened on is skipped."""
        lease = self._open_lease()
        for channel in channels:
            lease.pool.driver.capabilities.check_channel(channel)
        await self._listen(lease, tuple(dict.fromkeys(channels)))

    async def unlisten(self, *channels: str) -> None:
        """Stop listening on ``channels``; a channel not listened on is skipped. Notifications already received stay
        queued for `get()`."""
        lease = self._open_lease()
        for channel in channels:
            lease.pool.driver.capabilities.check_channel(channel)
        for channel in dict.fromkeys(channels):
            if channel in self._listening():
                await lease.call("unlisten", channel)

    async def get(self) -> BackendEvent:
        """The next notification, waiting for it. Raises `InterfaceError` once the connection is lost, the slot or the
        pool closed and every notification received before is returned, also in a call that already waits; and
        outside the block. A cancelled wait loses no notification."""
        while True:
            if self._state == _OPEN and self._held:
                return self._held.popleft()
            lease = self._open_lease()
            if lease.pool.closed or lease.slot.state == "closed":
                self._watched(lease)
                continue
            if not lease.events.empty():
                self._take(lease, lease.events.get_nowait())
                continue
            getter = asyncio.ensure_future(lease.events.get())
            stopped = asyncio.ensure_future(self._stopped.wait())
            try:
                await asyncio.wait({getter, stopped}, return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                # A getter that already took an event keeps it for the next call; a pending one leaves it queued.
                if not getter.cancel() and not getter.cancelled():
                    self._keep(lease, getter.result())
                raise
            finally:
                stopped.cancel()
            if getter.cancel() or getter.cancelled():
                continue
            self._take(lease, getter.result())

    def _keep(self, lease: SlotLease, event: Any) -> None:
        with contextlib.suppress(Exception):
            self._take(lease, event)

    def __aiter__(self) -> AsyncIterator[BackendEvent]:
        return self

    async def __anext__(self) -> BackendEvent:
        return await self.get()
