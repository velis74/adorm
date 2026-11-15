"""The drivers the conformance suite runs, and how each one blocks an operation for the failure tests.

Every driver of the demo (`demo.api.drivers.DRIVERS`) has an entry here; a driver that is not available is skipped
with the reason. ``create()`` returns a driver instance over the seeded test database. ``blocker()`` returns a
`Blocker`: while it holds, the operation ``blocked()`` builds waits in the database until it is released.
"""

from __future__ import annotations

import asyncio
import weakref

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import asyncpg

from adorm.drivers import Driver
from demo.api import drivers as demo_drivers, memory
from demo.models import MODELS
from demo.models.clubs import Club
from tests.library_data import in_thread

WAITING = (
    "SELECT pid FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND query LIKE '%clubs_club%'"
    " AND pid <> pg_backend_pid()"
)


class Blocker:
    """Holds what ``blocked()`` needs, so that operation waits in the database until `release()`."""

    lost_connections = False
    """Whether `terminate()` can end the server connection of a waiting operation."""

    async def hold(self) -> None:
        raise NotImplementedError

    async def release(self) -> None:
        raise NotImplementedError

    async def waiting(self) -> Any:
        """Return once an operation waits for the hold; the value identifies the waiter."""
        raise NotImplementedError

    async def idle(self) -> None:
        """Return once no operation waits for the hold."""
        raise NotImplementedError

    def blocked(self, slug: str) -> Awaitable[Any]:
        """An operation on the club ``slug`` that waits while the hold lasts."""
        raise NotImplementedError

    async def terminate(self, waiter: Any) -> None:
        raise NotImplementedError

    async def open_transactions(self) -> int:
        """How many connections other than the blocker's are in an open transaction, with or without a statement."""
        return 0

    async def transactions_ended(self) -> None:
        """Return once no connection other than the blocker's is in an open transaction; fail after 5 seconds."""
        async with asyncio.timeout(5):
            while await self.open_transactions():
                await asyncio.sleep(0.01)

    async def close(self) -> None:
        raise NotImplementedError


class TableLock(Blocker):
    """An exclusive lock on the clubs' table, taken and watched on two asyncpg connections of its own; a read of
    clubs waits for it."""

    lost_connections = True

    def __init__(self, url: str) -> None:
        self.url = url
        self.holder: asyncpg.Connection | None = None
        self.monitor: asyncpg.Connection | None = None
        self.transaction: Any = None

    async def _connections(self) -> tuple[asyncpg.Connection, asyncpg.Connection]:
        if self.holder is None:
            self.holder = await asyncpg.connect(self.url)
            self.monitor = await asyncpg.connect(self.url)
        return self.holder, self.monitor  # type: ignore[return-value]

    async def hold(self) -> None:
        holder, _ = await self._connections()
        self.transaction = holder.transaction()
        await self.transaction.start()
        await holder.execute("LOCK TABLE clubs_club IN ACCESS EXCLUSIVE MODE")

    async def release(self) -> None:
        transaction, self.transaction = self.transaction, None
        if transaction is not None:
            await transaction.rollback()

    async def _poll(self, present: bool) -> Any:
        _, monitor = await self._connections()
        async with asyncio.timeout(10):
            while True:
                pid = await monitor.fetchval(WAITING)
                if (pid is not None) is present:
                    return pid
                await asyncio.sleep(0.01)

    async def waiting(self) -> Any:
        return await self._poll(True)

    async def idle(self) -> None:
        await self._poll(False)

    def blocked(self, slug: str) -> Awaitable[Any]:
        return Club.objects.filter(slug=slug).count()

    async def terminate(self, waiter: Any) -> None:
        _, monitor = await self._connections()
        await monitor.execute("SELECT pg_terminate_backend($1)", waiter)

    async def open_transactions(self) -> int:
        holder, monitor = await self._connections()
        return await monitor.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
            " AND xact_start IS NOT NULL AND pid <> pg_backend_pid() AND pid <> $1",
            holder.get_server_pid(),
        )

    async def close(self) -> None:
        await self.release()
        for connection in (self.holder, self.monitor):
            if connection is not None:
                await connection.close()


class RowLock(Blocker):
    """A transaction on a memory connection of its own that has updated every club, so another update of a club
    waits for its row lock."""

    def __init__(self, driver: TrackedMemoryDriver) -> None:
        self.driver = driver
        self.connection: memory.MemoryConnection | None = None

    async def hold(self) -> None:
        from adorm.operations import Update
        from adorm.query import Query

        self.connection = await self.driver.open_connection(None, lambda _event: None)
        await self.connection.begin()
        await self.connection.execute(Update(Query("clubs.Club"), {"city": "Held"}))

    async def release(self) -> None:
        connection, self.connection = self.connection, None
        if connection is not None:
            await connection.rollback()
            await connection.close()

    async def waiting(self) -> Any:
        return await self._until(lambda: next(iter(self.driver.database.waiters), None))

    async def idle(self) -> None:
        await self._until(lambda: not self.driver.database.waiters)

    @staticmethod
    async def _until(probe: Callable[[], Any]) -> Any:
        for _ in range(10_000):
            value = probe()
            if value:
                return value
            await asyncio.sleep(0)
        raise AssertionError("the condition was not reached")

    def blocked(self, slug: str) -> Awaitable[Any]:
        return Club.objects.filter(slug=slug).update(city="Blocked")

    async def open_transactions(self) -> int:
        connections = [connection for connection in self.driver.connections if connection is not self.connection]
        return sum(1 for connection in connections if connection.in_transaction and not connection.closed)

    async def close(self) -> None:
        await self.release()


class TrackedMemoryDriver(memory.MemoryDriver):
    """The memory driver, keeping a weak set of the connections it opened."""

    def __init__(self, database: memory.MemoryDatabase) -> None:
        super().__init__(database)
        self.connections: weakref.WeakSet[memory.MemoryConnection] = weakref.WeakSet()

    async def open_connection(self, profile: Any, emit: Any) -> memory.MemoryConnection:
        connection = await super().open_connection(profile, emit)
        self.connections.add(connection)
        return connection


@dataclass(frozen=True)
class ConformanceDriver:
    """A driver under test: ``create(url)`` returns an instance over the seeded test database at ``url``;
    ``blocker(driver, url)`` returns its `Blocker`."""

    name: str
    create: Callable[[str], Awaitable[Driver]]
    blocker: Callable[[Driver, str], Blocker]

    def unavailable_reason(self) -> str | None:
        return demo_drivers.get(self.name).unavailable_reason()


async def _sql_driver(name: str) -> Driver:
    return demo_drivers.get(name).driver_class()()


async def _memory_driver(_url: str) -> Driver:
    tables = await in_thread(lambda: memory.django_tables(MODELS))
    return TrackedMemoryDriver(memory.MemoryDatabase(tables))


DRIVERS: tuple[ConformanceDriver, ...] = (
    ConformanceDriver("django", lambda _url: _sql_driver("django"), lambda _driver, url: TableLock(url)),
    ConformanceDriver("asyncpg", lambda _url: _sql_driver("asyncpg"), lambda _driver, url: TableLock(url)),
    ConformanceDriver("memory", _memory_driver, lambda driver, _url: RowLock(driver)),  # type: ignore[arg-type]
)
