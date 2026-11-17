"""What the Django driver does per database vendor (Django's ``DatabaseWrapper.vendor``): the capabilities it declares,
how it cancels a statement, which errors mean a cancelled statement or a lost connection, what an integrity error
reports and which database a profile names.

`vendor()` returns the `Vendor` of a vendor name: `PostgreSQL`, `SQLite`, `MySQL` and `Oracle` for Django's built-in
backends, `Vendor` with capabilities read from the backend's features for any other.
"""

from __future__ import annotations

import inspect
import itertools
import logging
import os
import re
import time

from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from functools import cache
from typing import Any

from adorm.drivers.base import Capabilities, is_connection_error
from adorm.drivers.django.translation import AGGREGATES, FUNCTIONS
from adorm.drivers.postgresql import (
    columns_sql,
    integrity_details as postgresql_integrity_details,
    MAX_IDENTIFIER_LENGTH,
)
from adorm.drivers.sql import POSTGRESQL_OPTIONS
from adorm.events import CONNECTION_LOST, NOTICE, NOTIFY
from adorm.exceptions import Error, NotSupportedError, OperationalError

logger = logging.getLogger(__name__)

__all__ = [
    "CALL_CANCELLED",
    "CAPABILITIES",
    "LOCK_WAIT_CANCELLED",
    "MYSQL_CANCEL_CONNECT_TIMEOUT",
    "MYSQL_CAPABILITIES",
    "ORACLE_CAPABILITIES",
    "POSTGRESQL_ONLY",
    "PSYCOPG2_CAPABILITIES",
    "READ_CAPABILITIES",
    "SQLITE_CAPABILITIES",
    "TRANSACTION_ENDED",
    "MySQL",
    "Oracle",
    "PostgreSQL",
    "SQLite",
    "Vendor",
    "engine_vendor",
    "features_capabilities",
    "is_refusal",
    "sqlite_error_code",
    "sqlite_integrity_details",
    "sqlite_interrupted",
    "vendor",
]

_LOOKUP_NAMES = (
    "exact",
    "iexact",
    "gt",
    "gte",
    "lt",
    "lte",
    "in",
    "range",
    "isnull",
    "contains",
    "icontains",
    "startswith",
    "istartswith",
    "endswith",
    "iendswith",
    "regex",
    "iregex",
    "overlap",
)

READ_CAPABILITIES = Capabilities(
    operations={"select", "get", "first", "last", "count", "exists", "aggregate", "in_bulk"},
    constructs={f"lookup.{name}" for name in _LOOKUP_NAMES}
    | {f"function.{name}" for name in FUNCTIONS}
    | {f"aggregate.{name}" for name in AGGREGATES}
    | {
        "path.nested",
        "clause.annotate",
        "clause.alias",
        "clause.group_by",
        "clause.distinct",
        "clause.distinct_on",
        "clause.only",
        "clause.defer",
        "clause.select_related",
        "clause.manager",
        "clause.union",
        "clause.intersection",
        "clause.difference",
        "clause.combinator.slice",
        "function.Trunc",
        "aggregate.distinct",
        "aggregate.filter",
        "expression.Case",
        "expression.Subquery",
        "expression.Exists",
        "expression.OuterRef",
        "expression.CombinedExpression",
    },
    cancellation=True,
)
"""The reads and constructs the Django query translation supports on PostgreSQL; the native PostgreSQL driver declares
them too."""

_WRITES = frozenset(
    {
        "create",
        "bulk_create",
        "create_from_parent",
        "save",
        "update",
        "delete",
        "delete_instance",
        "get_or_create",
        "update_or_create",
        "full_clean",
        "check_table",
    }
)

_ROW_LOCKS = frozenset(
    {
        "clause.select_for_update",
        "clause.select_for_update.nowait",
        "clause.select_for_update.skip_locked",
        "clause.select_for_update.of",
    }
)
_ON_CONFLICT = frozenset({"clause.on_conflict.ignore", "clause.on_conflict.update"})

_CASE_SENSITIVE_PATTERNS = frozenset({"lookup.contains", "lookup.startswith", "lookup.endswith"})

POSTGRESQL_ONLY = frozenset({"lookup.overlap", "clause.distinct_on"})
"""Constructs only PostgreSQL has: array overlap and ``DISTINCT ON``."""

CAPABILITIES = Capabilities(
    operations=READ_CAPABILITIES.operations | _WRITES,
    constructs=READ_CAPABILITIES.constructs | _ROW_LOCKS | _ON_CONFLICT | {"clause.select_for_update.no_key"},
    connection_options=POSTGRESQL_OPTIONS,
    custom_options=True,
    backend_events={NOTIFY, NOTICE, CONNECTION_LOST},
    max_channel_length=MAX_IDENTIFIER_LENGTH,
    savepoints=True,
    cancellation=True,
)
"""What `DjangoDriver` supports on PostgreSQL with psycopg 3: `READ_CAPABILITIES`, the writes, row locks, savepoints,
the connection options of `adorm.drivers.sql.POSTGRESQL_OPTIONS`, custom options and the backend events `NOTIFY`,
`NOTICE` and `CONNECTION_LOST`."""

PSYCOPG2_CAPABILITIES = replace(CAPABILITIES, backend_events=frozenset())
"""What `DjangoDriver` supports on PostgreSQL with psycopg 2: `CAPABILITIES` without backend events."""

_WRITE_LOCKED_ROWS = frozenset(
    {"clause.select_for_update", "clause.select_for_update.of", "clause.select_for_update.no_key"}
)
"""The row locks a SQLite transaction gives by holding the database's write lock from ``BEGIN IMMEDIATE`` on; Django
sends no ``FOR UPDATE``."""

SQLITE_CAPABILITIES = Capabilities(
    operations=CAPABILITIES.operations,
    constructs=(
        READ_CAPABILITIES.constructs
        - POSTGRESQL_ONLY
        - {"lookup.contains", "lookup.startswith", "lookup.endswith", "clause.combinator.slice"}
    )
    | _ON_CONFLICT
    | _WRITE_LOCKED_ROWS,
    savepoints=True,
    cancellation=True,
)
"""What `DjangoDriver` supports on SQLite: `CAPABILITIES` without ``nowait`` and ``skip_locked`` row locks, connection
options, backend events,
the constructs of `POSTGRESQL_ONLY`, a sliced query in a set operation (``clause.combinator.slice``) and the
case-sensitive pattern lookups ``contains``, ``startswith`` and ``endswith``, which SQLite's ``LIKE`` matches without
regard to case."""

MYSQL_CAPABILITIES = Capabilities(
    operations=CAPABILITIES.operations,
    constructs=(READ_CAPABILITIES.constructs - POSTGRESQL_ONLY - {"clause.intersection", "clause.difference"})
    | (_ROW_LOCKS - {"clause.select_for_update.of"})
    | {"clause.on_conflict.ignore"},
    savepoints=True,
    cancellation=True,
)
"""What `DjangoDriver` supports on MySQL and MariaDB: `CAPABILITIES` without connection options, backend events,
``FOR UPDATE OF`` (MariaDB has none), ``FOR NO KEY UPDATE``, ``ON CONFLICT ... DO UPDATE`` (MySQL's
``ON DUPLICATE KEY UPDATE`` takes no conflict target), ``INTERSECT``, ``EXCEPT`` and the constructs of
`POSTGRESQL_ONLY`."""

ORACLE_CAPABILITIES = Capabilities(
    operations=CAPABILITIES.operations,
    constructs=(READ_CAPABILITIES.constructs - POSTGRESQL_ONLY) | _ROW_LOCKS,
    savepoints=True,
    cancellation=True,
)
"""What `DjangoDriver` supports on Oracle: `CAPABILITIES` without connection options, backend events,
``FOR NO KEY UPDATE``, ``ON CONFLICT`` and the constructs of `POSTGRESQL_ONLY`."""

_FEATURE_CONSTRUCTS: tuple[tuple[str, str], ...] = (
    ("has_select_for_update", "clause.select_for_update"),
    ("has_select_for_update_nowait", "clause.select_for_update.nowait"),
    ("has_select_for_update_skip_locked", "clause.select_for_update.skip_locked"),
    ("has_select_for_update_of", "clause.select_for_update.of"),
    ("has_select_for_no_key_update", "clause.select_for_update.no_key"),
    ("supports_ignore_conflicts", "clause.on_conflict.ignore"),
    ("supports_update_conflicts_with_target", "clause.on_conflict.update"),
    ("supports_select_union", "clause.union"),
    ("supports_select_intersection", "clause.intersection"),
    ("supports_select_difference", "clause.difference"),
    ("supports_slicing_ordering_in_compound", "clause.combinator.slice"),
)
"""The Django feature flags that decide constructs of a backend without a `Vendor` of its own."""

_COMBINATORS = frozenset({"clause.union", "clause.intersection", "clause.difference", "clause.combinator.slice"})


def features_capabilities(features: type) -> Capabilities:
    """The capabilities of a backend without a `Vendor` of its own, from its ``DatabaseFeatures`` class
    ``features``: `CAPABILITIES` without connection options, backend events, cancellation, the constructs of
    `POSTGRESQL_ONLY`, and without each row lock, ``ON CONFLICT`` and set operation whose feature flag is not ``True``
    on the class; savepoints when ``uses_savepoints`` is ``True``. A flag the class computes per connection counts
    as ``False``."""

    def flag(name: str) -> bool:
        return inspect.getattr_static(features, name, False) is True

    constructs = set(READ_CAPABILITIES.constructs - POSTGRESQL_ONLY - _COMBINATORS)
    constructs |= {construct for name, construct in _FEATURE_CONSTRUCTS if flag(name)}
    if "clause.select_for_update" not in constructs:
        constructs -= _ROW_LOCKS
    return Capabilities(
        operations=CAPABILITIES.operations,
        constructs=constructs,
        savepoints=flag("uses_savepoints"),
    )


def _is_psycopg3() -> bool:
    from django.db.backends.postgresql.psycopg_any import is_psycopg3

    return is_psycopg3


def _chain(exc: BaseException | None) -> Iterable[BaseException]:
    """``exc``, then the exceptions it wraps: ``original`` of an adorm error, else ``__cause__``."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        original = exc.original if isinstance(exc, Error) else None
        exc = original if original is not None else exc.__cause__


def _errno(exc: BaseException) -> int | None:
    """The error number of the first exception on ``exc``'s chain whose first argument is an ``int`` (MySQL's
    ``MySQLdb`` and ``pymysql``)."""
    for current in _chain(exc):
        if current.args and isinstance(current.args[0], int) and not isinstance(current.args[0], bool):
            return current.args[0]
    return None


def _oracle_code(exc: BaseException) -> int | None:
    """The ``ORA-`` error number of the first exception on ``exc``'s chain that carries one (``oracledb`` and
    ``cx_Oracle`` put an error object with ``code`` in the first argument)."""
    for current in _chain(exc):
        code = getattr(current.args[0], "code", None) if current.args else None
        if isinstance(code, int) and code:
            return code
    return None


def _message(exc: BaseException) -> str:
    """The message of the database library's exception on ``exc``'s chain, the last one: its second argument after an
    error number (MySQL), else its text."""
    last = list(_chain(exc))[-1]
    args = last.args
    if len(args) >= 2 and isinstance(args[0], int) and isinstance(args[1], str):
        return args[1]
    return str(last)


CALL_CANCELLED = "canceling the statement: its call was cancelled"
"""The message of `Vendor.cancelled_error()`."""


class Vendor:
    """The Django driver's behaviour on databases of the vendor ``name``.

    This base class serves backends without a vendor of their own: capabilities from the backend's features
    (`features_capabilities()`), no cancellation, no server process id, `adorm.drivers.is_connection_error()` for lost
    connections and no details of integrity errors.
    """

    name: str = ""
    writes_wait_for_lock = False
    """Whether a write takes the database's write lock first (`DjangoConnection.waiting_for_write_lock`)."""

    def __init__(self, name: str = "", features: type | None = None) -> None:
        if name:
            self.name = name
        self._features = features

    def capabilities(self) -> Capabilities:
        """What the driver supports on this vendor's databases."""
        if self._features is None:
            return features_capabilities(object)
        return features_capabilities(self._features)

    def slot_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """``settings``, the ``DATABASES`` entry a slot's connection opens with, adjusted for the vendor."""
        return settings

    def check_settings(self, settings: Mapping[str, Any]) -> None:
        """Raise `NotSupportedError` when a slot's connection cannot open with the ``DATABASES`` entry ``settings``."""

    def connected(self, connection: Any) -> None:
        """Configure the DB-API connection of the slot's `DjangoConnection` ``connection`` once it is open."""

    def cancel(self, connection: Any, raw: Any) -> None:
        """Cancel the statement running on the DB-API connection ``raw`` of ``connection``. Does nothing here."""

    def backend_pid(self, raw: Any) -> int | None:  # noqa: ARG002 - subclasses use it
        """The server's id of the session of the DB-API connection ``raw``; ``None`` here."""
        return None

    def cancelled_error(self, db: Any) -> BaseException:
        """The DB-API error a statement of a cancelled call raises before it runs, on Django's connection wrapper
        ``db``; `is_cancellation()` recognises it. Here the backend's ``OperationalError`` `CALL_CANCELLED`."""
        return db.Database.OperationalError(CALL_CANCELLED)

    def is_cancellation(self, exc: BaseException) -> bool:
        """Whether ``exc``, a mapped adorm exception, is the error of a cancelled statement: here the error of
        `cancelled_error()`."""
        return any(str(current) == CALL_CANCELLED for current in _chain(exc))

    def is_connection_lost(self, exc: BaseException) -> bool:
        """Whether ``exc``, a mapped adorm exception, leaves the connection unusable: `is_connection_error()` here."""
        return is_connection_error(exc)

    def integrity_details(self, exc: BaseException) -> dict[str, Any]:  # noqa: ARG002 - subclasses use it
        """The keyword arguments of `adorm.IntegrityError` read from ``exc``; ``{}`` here."""
        return {}

    def database_key(self, entry: Mapping[str, Any]) -> Any:
        """``(name, host, port, database name)`` of the ``DATABASES`` entry ``entry``."""
        return (self.name, str(entry.get("HOST") or ""), str(entry.get("PORT") or ""), entry.get("NAME"))

    def transaction_ended(self, raw: Any) -> bool:  # noqa: ARG002 - subclasses use it
        """Whether the database ended the transaction open on the DB-API connection ``raw`` on its own after a failed
        statement; ``False`` here."""
        return False

    def check_types(self) -> bool:
        """Whether the schema check compares column types: the catalogue reports them as Django's ``db_type()``
        spells them; ``False`` here."""
        return False

    def columns(self, db: Any, cursor: Any, table: Any) -> list[tuple[Any, ...]]:
        """The columns of the `adorm.drivers.postgresql.ExpectedTable` ``table`` as rows of
        `adorm.drivers.postgresql.table_problems()`, read with Django's introspection on ``cursor``: ``(table exists,
        column, type, not null, has a default)``; a missing table gives one row ``(False, None, None, None, None)``.
        The type is ``None`` unless `check_types()`."""
        introspection = db.introspection
        if table.name not in introspection.table_names(cursor):
            return [(False, None, None, None, None)]
        check_types = self.check_types()
        return [
            (
                True,
                info.name,
                info.type_code if check_types else None,
                not info.null_ok,
                getattr(info, "default", None) is not None,
            )
            for info in introspection.get_table_description(cursor, table.name)
        ]

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r}>"


class PostgreSQL(Vendor):
    """PostgreSQL: the full `CAPABILITIES` (`PSYCOPG2_CAPABILITIES` with psycopg 2), cancellation with libpq's
    cancel request, SQLSTATEs and psycopg's diagnostics for errors."""

    name = "postgresql"

    def capabilities(self) -> Capabilities:
        return CAPABILITIES if _is_psycopg3() else PSYCOPG2_CAPABILITIES

    def cancel(self, connection: Any, raw: Any) -> None:  # noqa: ARG002 - the vendor contract
        cancel = getattr(raw, "cancel_safe", None) or raw.cancel
        cancel()

    def backend_pid(self, raw: Any) -> int | None:
        info = getattr(raw, "info", None)
        pid = getattr(info, "backend_pid", None) if info is not None else raw.get_backend_pid()
        return int(pid) if pid else None

    def cancelled_error(self, db: Any) -> BaseException:
        errors = getattr(db.Database, "errors", None) if _is_psycopg3() else None
        if errors is not None:
            return errors.QueryCanceled("canceling statement due to user request")
        return super().cancelled_error(db)

    def is_cancellation(self, exc: BaseException) -> bool:
        return getattr(exc, "sqlstate", None) == "57014" or super().is_cancellation(exc)

    def integrity_details(self, exc: BaseException) -> dict[str, Any]:
        return postgresql_integrity_details(exc)

    def database_key(self, entry: Mapping[str, Any]) -> Any:
        return ("postgresql", str(entry.get("HOST") or ""), str(entry.get("PORT") or ""), entry.get("NAME"))

    def check_types(self) -> bool:
        return True

    def columns(self, db: Any, cursor: Any, table: Any) -> list[tuple[Any, ...]]:  # noqa: ARG002 - the contract
        cursor.execute(columns_sql("%s"), [table.regclass])
        return cursor.fetchall()


SQLITE_BUSY = 5
"""SQLite's ``SQLITE_BUSY``: another connection holds the lock."""
SQLITE_LOCKED = 6
"""SQLite's ``SQLITE_LOCKED``: a connection of the same shared cache holds the lock."""
SQLITE_INTERRUPT = 9
"""SQLite's ``SQLITE_INTERRUPT``: ``interrupt()`` or a progress handler stopped the statement."""

LOCK_WAIT_CANCELLED = "canceling the wait for SQLite's write lock due to user request"
"""The message of the `OperationalError` a write raises when it is cancelled while it waits for the write lock."""

TRANSACTION_ENDED = "SQLite rolled back the whole transaction after an interrupted write"
"""The message of the `OperationalError` every request of a transaction that SQLite rolled back raises, until the
rollback."""

_LOCK_DELAYS = (0.001, 0.002, 0.005, 0.01, 0.015, 0.02, 0.025, 0.025, 0.025, 0.05, 0.05, 0.1)
"""The pauses between attempts to take the write lock, in seconds, as SQLite's busy handler makes them; the last
repeats."""

PROGRESS_STEPS = 1000
"""How many virtual machine instructions SQLite runs between checks for a cancelled call."""

_SQLITE_CONSTRAINT = re.compile(r"^(?P<kind>UNIQUE|NOT NULL|CHECK|FOREIGN KEY) constraint failed(?::\s*(?P<rest>.*))?$")


def sqlite_error_code(exc: BaseException) -> int | None:
    """The primary result code of the first ``sqlite3`` exception on ``exc``'s chain (``sqlite_errorcode``)."""
    for current in _chain(exc):
        code = getattr(current, "sqlite_errorcode", None)
        if isinstance(code, int):
            return code & 0xFF
    return None


def sqlite_interrupted() -> BaseException:
    """The ``sqlite3.OperationalError`` of a statement ``interrupt()`` stopped (``SQLITE_INTERRUPT``)."""
    import sqlite3

    exc = sqlite3.OperationalError("interrupted")
    exc.sqlite_errorcode = SQLITE_INTERRUPT  # type: ignore[attr-defined]
    exc.sqlite_errorname = "SQLITE_INTERRUPT"  # type: ignore[attr-defined]
    return exc


def sqlite_integrity_details(message: str) -> dict[str, Any]:
    """The keyword arguments of `adorm.IntegrityError` that SQLite's constraint message ``message`` gives: the table
    and columns of a ``UNIQUE`` or ``NOT NULL`` violation (``"UNIQUE constraint failed: t.a, t.b"``), the
    constraint name of a ``CHECK`` violation; ``detail`` is the message. SQLite reports no values."""
    match = _SQLITE_CONSTRAINT.match(message.strip())
    details: dict[str, Any] = {"detail": message}
    if match is None:
        return details
    rest = (match["rest"] or "").strip()
    if match["kind"] == "CHECK":
        details["constraint_name"] = rest or None
    elif match["kind"] in ("UNIQUE", "NOT NULL") and rest and not rest.startswith("index "):
        tables, columns = set(), []
        for item in rest.split(","):
            table, _, column = item.strip().rpartition(".")
            tables.add(table)
            columns.append(column)
        if len(tables) == 1 and "" not in tables:
            details["table_name"] = tables.pop()
            details["columns"] = tuple(columns)
    return details


class SQLite(Vendor):
    """SQLite: one writer at a time, readers beside it.

    A slot's connection runs in WAL mode (``PRAGMA journal_mode=WAL``, which the database file keeps) and starts
    every transaction with ``BEGIN IMMEDIATE`` (``OPTIONS["transaction_mode"]``, Django 5.1 and newer). A transaction
    and a write outside one take the write lock when they begin; `DjangoConnection` waits for it itself, for as long
    as the connection's busy timeout (``OPTIONS["timeout"]``, 5 seconds by default), and a cancellation ends the wait.
    A running statement is cancelled with ``interrupt()``.
    """

    name = "sqlite"
    writes_wait_for_lock = True

    def capabilities(self) -> Capabilities:
        return SQLITE_CAPABILITIES

    def slot_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        options = dict(settings.get("OPTIONS", {}))
        if str(options.get("transaction_mode") or "").upper() != "EXCLUSIVE":
            options["transaction_mode"] = "IMMEDIATE"
        return {**settings, "OPTIONS": options}

    def check_settings(self, settings: Mapping[str, Any]) -> None:
        import django

        if django.VERSION < (5, 1):
            raise NotSupportedError("the Django driver needs Django 5.1 or newer on SQLite (transaction_mode)")
        if self.database_key(settings) is None:
            raise NotSupportedError(
                "the Django driver needs a SQLite database file: an in-memory database has no WAL mode, and a private "
                "one is visible to its own connection only"
            )

    def connected(self, connection: Any) -> None:
        raw = connection.db.connection
        raw.execute("PRAGMA journal_mode=WAL")
        raw.set_progress_handler(connection._cancelled, PROGRESS_STEPS)

    def cancel(self, connection: Any, raw: Any) -> None:
        connection._cancel_lock_wait()
        raw.interrupt()

    def cancelled_error(self, db: Any) -> BaseException:  # noqa: ARG002 - the vendor contract
        return sqlite_interrupted()

    def is_cancellation(self, exc: BaseException) -> bool:
        if isinstance(exc, OperationalError) and str(exc) == LOCK_WAIT_CANCELLED:
            return True
        return sqlite_error_code(exc) == SQLITE_INTERRUPT

    def transaction_ended(self, raw: Any) -> bool:
        # An interrupted INSERT, UPDATE or DELETE rolls back the whole transaction, savepoints included.
        return not raw.in_transaction

    def is_connection_lost(self, exc: BaseException) -> bool:
        # SQLite has no server connection: its operational errors (a lock, a full disk) leave the connection usable.
        return not isinstance(exc, OperationalError) and is_connection_error(exc)

    def integrity_details(self, exc: BaseException) -> dict[str, Any]:
        return sqlite_integrity_details(_message(exc))

    def database_key(self, entry: Mapping[str, Any]) -> Any:
        name = str(entry.get("NAME") or "")
        if name in ("", ":memory:"):
            return None
        if name.startswith("file:"):
            path, _, query = name[5:].partition("?")
            if path == ":memory:" or "mode=memory" in query:
                return None
            return ("sqlite", name)
        return ("sqlite", os.path.realpath(name))

    def check_types(self) -> bool:
        return True

    def take_write_lock(self, connection: Any, atomic: Any) -> None:
        """Enter Django's outermost atomic block ``atomic`` (``BEGIN IMMEDIATE``) on ``connection``, trying again while
        another connection holds the write lock, until the busy timeout passes; then raise the last error. A
        cancellation of the call ends the wait with `OperationalError` `LOCK_WAIT_CANCELLED`."""
        from django.db import OperationalError as DjangoOperationalError

        db = connection.db
        db.ensure_connection()
        raw = db.connection
        (milliseconds,) = raw.execute("PRAGMA busy_timeout").fetchone()
        deadline = time.monotonic() + milliseconds / 1000
        delays = itertools.chain(_LOCK_DELAYS, itertools.repeat(_LOCK_DELAYS[-1]))
        raw.execute("PRAGMA busy_timeout = 0")
        connection._waiting_for_write_lock = True
        try:
            while True:
                try:
                    atomic.__enter__()
                    return
                except DjangoOperationalError as exc:
                    if sqlite_error_code(exc) not in (SQLITE_BUSY, SQLITE_LOCKED):
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise
                connection._wait_for_lock(min(next(delays), remaining))
        finally:
            connection._waiting_for_write_lock = False
            raw.execute(f"PRAGMA busy_timeout = {int(milliseconds)}")


_MYSQL_LOST = frozenset({2006, 2013, 2055})
"""MySQL client errors of a lost server connection: server gone away, lost during a query, lost while reading."""
_MYSQL_INTERRUPTED = 1317
"""MySQL's ``ER_QUERY_INTERRUPTED``: ``KILL QUERY`` stopped the statement."""
_MYSQL_REFUSALS = frozenset({1040, 1203})
"""MySQL's ``ER_CON_COUNT_ERROR`` and ``ER_TOO_MANY_USER_CONNECTIONS``."""
_MYSQL_DUPLICATE = re.compile(r"Duplicate entry '(?P<value>.*)' for key '(?P<key>[^']*)'")
_MYSQL_NULL = re.compile(r"Column '(?P<column>[^']*)' cannot be null")
_MYSQL_FOREIGN_KEY = re.compile(
    r"\(`[^`]*`\.`(?P<table>[^`]*)`, CONSTRAINT `(?P<name>[^`]*)` FOREIGN KEY \((?P<cols>[^)]*)\)"
)


MYSQL_CANCEL_CONNECT_TIMEOUT = 5
"""The connect timeout, in seconds, of the connection `MySQL.cancel()` opens with the wrapper's parameters."""


class MySQL(Vendor):
    """MySQL and MariaDB: cancellation with ``KILL QUERY`` on a connection of its own, errors by MySQL's error
    numbers."""

    name = "mysql"

    def capabilities(self) -> Capabilities:
        return MYSQL_CAPABILITIES

    def cancel(self, connection: Any, raw: Any) -> None:
        """Run ``KILL QUERY`` on a connection of its own, opened with the profile's ``connect`` hook when it has one,
        else with the wrapper's parameters and a connect timeout of `MYSQL_CANCEL_CONNECT_TIMEOUT`. A connection that
        cannot open loses the cancellation: the call ends with its statement or after ``cancel_grace``."""
        db = connection.db
        profile = getattr(connection, "profile", None)
        connect = getattr(profile, "connect", None)
        try:
            if connect is not None:
                killer = connect(profile)
            else:
                params = {**db.get_connection_params(), "connect_timeout": MYSQL_CANCEL_CONNECT_TIMEOUT}
                killer = db.get_new_connection(params)
        except Exception:
            logger.warning("MySQL: cannot open a connection to cancel a statement", exc_info=True)
            return
        try:
            cursor = killer.cursor()
            cursor.execute(f"KILL QUERY {int(raw.thread_id())}")
            cursor.close()
        finally:
            killer.close()

    def backend_pid(self, raw: Any) -> int | None:
        return int(raw.thread_id())

    def cancelled_error(self, db: Any) -> BaseException:
        return db.Database.OperationalError(_MYSQL_INTERRUPTED, "Query execution was interrupted")

    def is_cancellation(self, exc: BaseException) -> bool:
        return _errno(exc) == _MYSQL_INTERRUPTED

    def is_connection_lost(self, exc: BaseException) -> bool:
        if isinstance(exc, OperationalError):
            return _errno(exc) in _MYSQL_LOST
        return is_connection_error(exc)

    def integrity_details(self, exc: BaseException) -> dict[str, Any]:
        message = _message(exc)
        details: dict[str, Any] = {"detail": message}
        if match := _MYSQL_DUPLICATE.search(message):
            table, _, name = match["key"].rpartition(".")
            details.update(constraint_name=name, table_name=table or None, values=(match["value"],))
        elif match := _MYSQL_NULL.search(message):
            details["columns"] = (match["column"],)
        elif match := _MYSQL_FOREIGN_KEY.search(message):
            columns = tuple(column.strip().strip("`") for column in match["cols"].split(","))
            details.update(constraint_name=match["name"], table_name=match["table"], columns=columns)
        return details


_ORACLE_CANCELLED = 1013
"""``ORA-01013``: user requested cancel of current operation."""
_ORACLE_LOST = frozenset({28, 1012, 3113, 3114, 3135, 12537, 12570})
"""Oracle errors of a lost session: killed, not logged on, end-of-file on the channel, not connected, lost contact,
connection closed."""
_ORACLE_CLOSED = ("DPY-1001", "DPY-4011", "DPI-1010", "DPI-1080")
"""python-oracledb's and ODPI-C's errors of a closed or lost connection."""
_ORACLE_CONSTRAINT = re.compile(r"ORA-0(?:0001|2291|2292|2290): [^(]*\((?P<owner>[^.()]+)\.(?P<name>[^()]+)\)")
_ORACLE_NULL = re.compile(r'ORA-01400: [^(]*\("(?P<owner>[^"]+)"\."(?P<table>[^"]+)"\."(?P<column>[^"]+)"\)')


class Oracle(Vendor):
    """Oracle: cancellation with the driver's ``cancel()``, errors by their ``ORA-`` numbers."""

    name = "oracle"

    def capabilities(self) -> Capabilities:
        return ORACLE_CAPABILITIES

    def cancel(self, connection: Any, raw: Any) -> None:  # noqa: ARG002 - the vendor contract
        raw.cancel()

    def is_cancellation(self, exc: BaseException) -> bool:
        return _oracle_code(exc) == _ORACLE_CANCELLED or super().is_cancellation(exc)

    def is_connection_lost(self, exc: BaseException) -> bool:
        if isinstance(exc, OperationalError):
            return _oracle_code(exc) in _ORACLE_LOST or any(code in str(exc) for code in _ORACLE_CLOSED)
        return is_connection_error(exc)

    def integrity_details(self, exc: BaseException) -> dict[str, Any]:
        message = _message(exc)
        details: dict[str, Any] = {"detail": message}
        if match := _ORACLE_CONSTRAINT.search(message):
            details.update(constraint_name=match["name"], schema_name=match["owner"])
        elif match := _ORACLE_NULL.search(message):
            details.update(schema_name=match["owner"], table_name=match["table"], columns=(match["column"],))
        return details


_ORACLE_REFUSALS = frozenset({18, 12516, 12519, 12520})
"""Oracle errors of a database that accepts no more sessions: maximum sessions exceeded, no listener handler."""


def is_refusal(exc: BaseException) -> bool:
    """Whether ``exc``, raised while opening a connection, is MySQL's or Oracle's refusal of more connections: MySQL's
    ``ER_CON_COUNT_ERROR`` (1040) or ``ER_TOO_MANY_USER_CONNECTIONS`` (1203), ``ORA-00018``, ``ORA-12516``,
    ``ORA-12519`` or ``ORA-12520``."""
    return _errno(exc) in _MYSQL_REFUSALS or _oracle_code(exc) in _ORACLE_REFUSALS


_VENDORS: dict[str, Callable[[], Vendor]] = {
    "postgresql": PostgreSQL,
    "sqlite": SQLite,
    "mysql": MySQL,
    "oracle": Oracle,
}


def vendor(name: str, features: type | None = None) -> Vendor:
    """The `Vendor` of Django's vendor name ``name``, one instance per name; for a name without one, a `Vendor` with
    the capabilities of the backend's ``DatabaseFeatures`` class ``features``."""
    return _vendor(name, None if name in _VENDORS else features)


@cache
def _vendor(name: str, features: type | None) -> Vendor:
    factory = _VENDORS.get(name)
    return factory() if factory is not None else Vendor(name, features)


def engine_vendor(engine: str) -> Vendor:
    """The `Vendor` of the Django backend ``engine`` (``DATABASES[...]["ENGINE"]``), read from its
    ``DatabaseWrapper`` class without connecting. Raises ``ImportError`` or ``ImproperlyConfigured`` for a backend
    that does not load."""
    from django.db.utils import load_backend

    wrapper = load_backend(engine).DatabaseWrapper
    return vendor(wrapper.vendor, getattr(wrapper, "features_class", None))
