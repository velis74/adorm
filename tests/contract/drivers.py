"""Minimal drivers that implement the driver contract on real backends.

They execute raw SQL only (operation kinds ``raw`` and ``listen``) and exist to exercise the contract: the Django
driver as a threaded driver on thread-bound Django connections, the asyncpg driver as a native async driver.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from adorm.drivers import (
    Capabilities,
    Driver,
    DriverConnection,
    ThreadedConnection,
    ThreadedDriver,
)
from adorm.events import BackendEvent, Emit, Event
from adorm.exceptions import InterfaceError, OperationalError, wrap_driver_error

logger = logging.getLogger(__name__)

OPTIONS = frozenset({"statement_timeout", "lock_timeout"})
CAPABILITIES = Capabilities(
    operations={"raw", "listen"},
    connection_options=OPTIONS,
    backend_events={"notify"},
    savepoints=True,
    cancellation=True,
)


@dataclass(frozen=True)
class RawQuery:
    sql: str
    params: tuple[Any, ...] = ()
    kind: str = "raw"

    def constructs(self) -> Iterable[str]:
        return ()


@dataclass(frozen=True)
class Listen:
    channel: str
    kind: str = "listen"

    def constructs(self) -> Iterable[str]:
        return ()


@dataclass(frozen=True)
class Profile:
    name: str
    alias: str = "default"
    params: Mapping[str, Any] = field(default_factory=dict)
    connect: Callable[[Any], Any] | None = None


@dataclass
class Recorder:
    """An ``emit`` callable that records events and the thread each arrived on."""

    events: list[Event] = field(default_factory=list)
    threads: list[int] = field(default_factory=list)
    arrived: asyncio.Event = field(default_factory=asyncio.Event)
    on_event: Callable[[Event], None] | None = None

    def __call__(self, event: Event) -> None:
        self.events.append(event)
        self.threads.append(threading.get_ident())
        self.arrived.set()
        if self.on_event is not None:
            self.on_event(event)


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def option_value(name: str, value: Any) -> str:
    if name in OPTIONS:
        return f"{round(float(value) * 1000)}ms"
    return str(value)


# Django ORM, threaded


class DjangoConnection(ThreadedConnection):
    def connect_sync(self) -> None:
        from django.db import connections

        self.db = connections[self.profile.alias]
        if self.profile.params:
            # The wrapper belongs to this thread; its settings may differ from the alias's.
            self.db.settings_dict = {**self.db.settings_dict, **self.profile.params}
        if self.profile.connect is not None:
            self.db.get_new_connection = lambda _params: self.profile.connect(self.profile)
        self.db.ensure_connection()
        self.pg = self.db.connection
        self.pg.add_notify_handler(lambda n: self.emit(BackendEvent("notify", n.channel, n.payload)))

    @property
    def target(self) -> Any:
        return self.db

    def _run(self, sql: str, params: Iterable[Any] = ()) -> Any:
        with self.db.cursor() as cursor:
            cursor.execute(sql, tuple(params))
            if cursor.description is None:
                return cursor.rowcount
            columns = [c.name for c in cursor.description]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def execute_sync(self, operation: Any) -> Any:
        if operation.kind == "listen":
            return self._run(f"LISTEN {quote(operation.channel)}")
        # Parameters are numbered in order of appearance in the suite's SQL.
        return self._run(re.sub(r"\$\d+", "%s", operation.sql), operation.params)

    def begin_sync(self) -> None:
        self.db.set_autocommit(False)

    def commit_sync(self) -> None:
        self.db.commit()
        self.db.set_autocommit(True)

    def rollback_sync(self) -> None:
        self.db.rollback()
        self.db.set_autocommit(True)

    def savepoint_sync(self, name: str) -> None:
        self._run(f"SAVEPOINT {quote(name)}")

    def release_savepoint_sync(self, name: str) -> None:
        self._run(f"RELEASE SAVEPOINT {quote(name)}")

    def rollback_to_savepoint_sync(self, name: str) -> None:
        self._run(f"ROLLBACK TO SAVEPOINT {quote(name)}")

    def set_options_sync(self, options: Mapping[str, Any], local: bool) -> None:
        for name, value in options.items():
            self._run("SELECT set_config(%s, %s, %s)", (name, option_value(name, value), local))

    def cancel_sync(self) -> None:
        # Runs on another thread, possibly while connect_sync() has not yet set self.pg.
        pg = getattr(self, "pg", None)
        if pg is not None:
            pg.cancel_safe()

    def close_sync(self) -> None:
        db = getattr(self, "db", None)
        if db is not None:
            db.close()


class DjangoDriver(ThreadedDriver):
    name = "django-raw"
    connection_class = DjangoConnection
    capabilities = CAPABILITIES


class DjangoDirectDriver(DjangoDriver):
    direct = True


# asyncpg, native async


class AsyncpgConnection(DriverConnection):
    def __init__(self, driver: AsyncpgDriver, conn: asyncpg.Connection, emit: Emit) -> None:
        super().__init__(driver)
        self.conn = conn
        self._emit = emit
        self._loop = asyncio.get_running_loop()
        self._tx: Any = None
        self._statement: asyncio.Task | None = None
        self._cancel_requested = False
        self._usable = True
        self._closed = False

    @property
    def target(self) -> Any:
        return self.conn

    async def _guard(self, coro: Any) -> Any:
        # The statement runs in its own task so that cancel() can stop it without cancelling the caller.
        task = asyncio.ensure_future(coro)
        self._statement = task
        self._cancel_requested = False
        try:
            return await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if self._cancel_requested and current is not None and not current.cancelling():
                raise OperationalError("statement cancelled", sqlstate="57014") from None
            raise
        except Exception as exc:
            mapped = self.map_error(exc)
            if self.is_connection_lost(mapped):
                self._usable = False
            raise mapped from exc
        finally:
            self._statement = None

    async def _run(self, sql: str, params: Iterable[Any] = ()) -> Any:
        if self._closed:
            raise InterfaceError("asyncpg connection is closed")
        statement = await self._guard(self.conn.prepare(sql))
        if not statement.get_attributes():
            status = await self._guard(self.conn.execute(sql, *params))
            parts = status.split()
            return int(parts[-1]) if parts and parts[-1].isdigit() else 0
        return [dict(r) for r in await self._guard(statement.fetch(*params))]

    async def _execute(self, operation: Any) -> Any:
        if operation.kind == "listen":
            await self._guard(self.conn.add_listener(operation.channel, self._notify))
            return 0
        return await self._run(operation.sql, operation.params)

    def _notify(self, _conn: Any, _pid: int, channel: str, payload: str) -> None:
        self._emit(BackendEvent("notify", channel, payload))

    async def begin(self) -> None:
        self._tx = self.conn.transaction()
        await self._guard(self._tx.start())

    async def commit(self) -> None:
        tx, self._tx = self._tx, None
        await self._guard(tx.commit())

    async def rollback(self) -> None:
        tx, self._tx = self._tx, None
        await self._guard(tx.rollback())

    async def _savepoint(self, name: str) -> None:
        await self._run(f"SAVEPOINT {quote(name)}")

    async def _release_savepoint(self, name: str) -> None:
        await self._run(f"RELEASE SAVEPOINT {quote(name)}")

    async def _rollback_to_savepoint(self, name: str) -> None:
        await self._run(f"ROLLBACK TO SAVEPOINT {quote(name)}")

    async def _set_options(self, options: dict[str, Any], local: bool) -> None:
        for name, value in options.items():
            await self._run("SELECT set_config($1, $2, $3)", (name, option_value(name, value), local))

    def cancel(self) -> None:
        def cancel_statement() -> None:
            if self._statement is not None and not self._statement.done():
                self._cancel_requested = True
                self._statement.cancel()

        self._loop.call_soon_threadsafe(cancel_statement)

    def is_usable(self) -> bool:
        return self._usable and not self._closed and not self.conn.is_closed()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.conn.close()
        except Exception:
            logger.exception("closing asyncpg connection failed")


class AsyncpgDriver(Driver):
    name = "asyncpg-raw"
    mode = "async"
    capabilities = CAPABILITIES

    async def open_connection(self, profile: Any, emit: Emit) -> AsyncpgConnection:
        try:
            if profile.connect is not None:
                conn = await profile.connect(profile)
            else:
                conn = await asyncpg.connect(profile.params["dsn"])
        except Exception as exc:
            raise wrap_driver_error(exc) from exc
        return AsyncpgConnection(self, conn, emit)
