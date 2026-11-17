"""Recording the SQL the drivers send to PostgreSQL, with the session each statement ran for.

The Django driver's statements are read with an execute wrapper on every Django connection
(``connection_created``), the asyncpg driver's with a query logger on every connection its profile's ``connect`` hook
opens. The profiles' ``on_acquire`` and ``on_release`` hooks record which session holds a connection. A statement
belongs to the session that held its connection when it started; a statement outside a lease (the setup of a new
connection) belongs to the session whose lease of the connection follows. A connection no session ever held is the
observer's (`OBSERVER`): the monitor `adorm.testing.wait_for_lock()` opens. Statements are kept only between
`Capture.start()` and `Capture.stop()`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading
import time

from typing import Any

from adorm import ConnectionProfile, PoolSettings
from adorm.drivers import Driver

_BEGIN_PENDING = "_adorm_demo_begin_pending"
"""The attribute of a Django connection whose autocommit was turned off and that has sent no statement since."""

_IDLE = 0  # psycopg.pq.TransactionStatus.IDLE

DESK_OPTION = "app.reader"
"""The custom connection option the desk profiles' ``on_acquire`` hook sets."""

OBSERVER = "demo"
"""The session name of the statements of connections no session held."""


@dataclasses.dataclass(frozen=True)
class Statement:
    """An SQL statement as the database received it: ``session`` is the name of the session it ran for (``None``
    outside a lease with no lease following, `OBSERVER` on a connection no session held), ``at`` and ``duration`` are
    seconds from `Capture.start()`."""

    session: str | None
    sql: str
    params: tuple[Any, ...]
    at: float
    duration: float
    error: str | None = None
    connection: int = dataclasses.field(default=0, repr=False, compare=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "session": self.session,
            "sql": self.sql,
            "params": [_param(value) for value in self.params],
            "at_ms": round(self.at * 1000, 1),
            "duration_ms": round(self.duration * 1000, 2),
            "error": self.error,
        }


def _param(value: Any) -> Any:
    """``value`` as JSON: lists and tuples item by item, dates, decimals and the like as pydantic serialises them, else
    its text."""
    if isinstance(value, (int, float, str, bool, type(None))):
        return value
    if isinstance(value, (list, tuple)):
        return [_param(item) for item in value]
    from pydantic_core import PydanticSerializationError, to_jsonable_python

    try:
        return to_jsonable_python(value)
    except PydanticSerializationError:
        return str(value)


def _error_text(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}" if str(error) else type(error).__name__


class Capture:
    """The statements of the connections of the profiles `profile()` builds."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owners: dict[int, list[tuple[float, str | None]]] = {}
        self._statements: list[Statement] | None = None
        self._start = 0.0

    # Ownership

    def on_acquire(self, connection: Any, _profile: Any, session: Any) -> None:
        """The profiles' ``on_acquire`` hook: ``session`` holds ``connection`` from now on."""
        self._own(connection, session.session_name)

    def on_acquire_desk(self, connection: Any, profile: Any, session: Any) -> dict[str, str]:
        """The ``on_acquire`` hook of the desk profiles: `on_acquire()`, and the custom option `DESK_OPTION` set to the
        session's name for the lease."""
        self.on_acquire(connection, profile, session)
        return {DESK_OPTION: session.session_name}

    def on_release(self, connection: Any, _profile: Any, _session: Any) -> None:
        """The profiles' ``on_release`` hook: no session holds ``connection`` from now on."""
        self._own(connection, None)

    def _own(self, connection: Any, name: str | None) -> None:
        with self._lock:
            history = self._owners.setdefault(id(connection), [])
            history.append((time.monotonic(), name))
            del history[:-50]

    def _owner(self, connection: int, at: float) -> str | None:
        """The session that held ``connection`` at ``at``; outside a lease, the session of the lease that follows."""
        history = self._owners.get(connection)
        if not history:
            return OBSERVER
        owner = None
        for since, name in history:
            if since > at:
                return owner if owner is not None else name
            owner = name
        return owner

    # Recording

    def _record(self, connection: Any, sql: str, params: Any, started: float, ended: float, error: Any) -> None:
        with self._lock:
            if self._statements is None or started < self._start:
                return
            self._statements.append(
                Statement(
                    session=None,
                    connection=id(connection),
                    sql=sql,
                    params=tuple(params or ()) if not isinstance(params, dict) else tuple(params.items()),
                    at=started - self._start,
                    duration=ended - started,
                    error=None if error is None else _error_text(error),
                )
            )

    def start(self) -> None:
        """Start a recording; statements recorded before are dropped."""
        with self._lock:
            self._statements = []
            self._start = time.monotonic()

    def elapsed(self) -> float:
        """Seconds since `start()`."""
        return time.monotonic() - self._start

    async def stop(self) -> list[Statement]:
        """End the recording and return its statements in the order they started. Waits one loop iteration for the
        asyncpg query loggers, which run as callbacks of the loop."""
        await asyncio.sleep(0)
        with self._lock:
            statements, self._statements = self._statements or [], None
            start = self._start
            statements = [
                dataclasses.replace(statement, session=self._owner(statement.connection, start + statement.at))
                for statement in statements
            ]
        return sorted(statements, key=lambda statement: statement.at)

    # Drivers

    def _django_wrapper(self, execute: Any, sql: str, params: Any, many: bool, context: dict) -> Any:
        started = time.monotonic()
        connection = context["connection"]
        if getattr(connection, _BEGIN_PENDING, False):
            setattr(connection, _BEGIN_PENDING, False)
            raw = connection.connection
            if raw is not None and raw.info.transaction_status == _IDLE:
                # psycopg sends BEGIN with this statement.
                self._record(connection, "BEGIN", None, started, started, None)
        error = None
        try:
            return execute(sql, params, many, context)
        except Exception as exc:
            error = exc
            raise
        finally:
            self._record(context["connection"], sql, None if many else params, started, time.monotonic(), error)

    def _watch_transactions(self, connection: Any) -> None:
        """Record psycopg's transaction control on Django's ``connection``, which bypasses the execute wrappers:
        ``BEGIN`` when psycopg sends it, before the first statement after autocommit was turned off
        (`_django_wrapper()`), ``COMMIT`` and ``ROLLBACK`` when a transaction is open."""
        set_autocommit, commit, rollback = connection._set_autocommit, connection._commit, connection._rollback

        def in_transaction() -> bool:
            raw = connection.connection
            return raw is not None and raw.info.transaction_status != _IDLE

        def timed(function: Any, sql: str, *args: Any) -> Any:
            started = time.monotonic()
            error = None
            try:
                return function(*args)
            except Exception as exc:
                error = exc
                raise
            finally:
                self._record(connection, sql, None, started, time.monotonic(), error)

        def _set_autocommit(autocommit: bool) -> Any:
            result = set_autocommit(autocommit)
            setattr(connection, _BEGIN_PENDING, not autocommit)
            return result

        def _commit() -> Any:
            setattr(connection, _BEGIN_PENDING, False)
            return timed(commit, "COMMIT") if in_transaction() else commit()

        def _rollback() -> Any:
            setattr(connection, _BEGIN_PENDING, False)
            return timed(rollback, "ROLLBACK") if in_transaction() else rollback()

        connection._set_autocommit, connection._commit, connection._rollback = _set_autocommit, _commit, _rollback

    def watch_django(self) -> None:
        """Wrap the execution of every Django connection opened from now on."""
        from django.db.backends.signals import connection_created

        def created(sender: Any, connection: Any, **_kwargs: Any) -> None:  # noqa: ARG001 - signal signature
            if self._django_wrapper not in connection.execute_wrappers:
                connection.execute_wrappers.append(self._django_wrapper)
                self._watch_transactions(connection)

        connection_created.connect(created, weak=False, dispatch_uid=f"adorm-demo-capture-{id(self)}")

    async def connect_asyncpg(self, profile: Any) -> Any:
        """The asyncpg profiles' ``connect`` hook: the connection the driver would open, with a query logger."""
        import asyncpg

        from django.db import connections

        from adorm.drivers.asyncpg import connect_arguments

        conn = await asyncpg.connect(**connect_arguments(connections.settings[profile.alias]))

        def logged(record: Any) -> None:
            ended = time.monotonic()
            self._record(conn, record.query, record.args, ended - record.elapsed, ended, record.exception)

        conn.add_query_logger(logged)
        return conn

    def profile(self, name: str, driver: Driver, pool: PoolSettings, *, desk: bool = False) -> ConnectionProfile:
        """The connection profile ``name`` on ``driver`` whose connections this capture records, with
        `on_acquire_desk()` as its ``on_acquire`` hook when ``desk``; raises `NotImplementedError` for a driver whose
        SQL it cannot record."""
        if driver.name == "asyncpg":
            connect = self.connect_asyncpg
        elif driver.name == "django":
            connect = None
        else:
            raise NotImplementedError(f"the demo cannot record the SQL of the driver {driver.name!r}")
        on_acquire = self.on_acquire_desk if desk else self.on_acquire
        return ConnectionProfile(
            name, driver, connect=connect, on_acquire=on_acquire, on_release=self.on_release, pool=pool
        )


CAPTURE = Capture()
"""The demo's capture."""
