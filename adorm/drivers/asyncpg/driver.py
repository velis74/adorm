"""The native PostgreSQL driver: adorm operations executed on asyncpg, on the event loop, without threads."""

from __future__ import annotations

import asyncio
import inspect
import logging
import shlex

from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import urlencode

import asyncpg

from django.core.exceptions import SynchronousOnlyOperation, ValidationError as DjangoValidationError

from adorm.drivers.asyncpg.compiler import DjangoCompiler, DjangoStatement
from adorm.drivers.asyncpg.writes import Writer, WRITES
from adorm.drivers.base import BackendActivity, Capabilities, DatabaseMonitor, Driver, DriverConnection, Profile
from adorm.drivers.django.driver import (
    CAPABILITIES as DJANGO_CAPABILITIES,
    clear_content_types_on_migrate,
    connection_defaults,
    database_key,
    map_django_error,
)
from adorm.drivers.django.schema import expected_tables, model_schema_hash
from adorm.drivers.django.tenants import tenant_scope
from adorm.drivers.django.translation import (
    aggregation,
    check_in_bulk_field,
    combined_aggregation,
    operation_model,
    translate,
    validation_error,
)
from adorm.drivers.postgresql import (
    activity_sql,
    backend_activity,
    columns_sql,
    MAX_IDENTIFIER_LENGTH,
    notice_payload,
    table_problems,
)
from adorm.drivers.sql import POSTGRESQL_OPTIONS, set_options_sql
from adorm.events import BackendEvent, CONNECTION_LOST, Emit, NOTICE, NOTIFY
from adorm.exceptions import (
    InterfaceError,
    MultipleObjectsReturned,
    NotSupportedError,
    ObjectDoesNotExist,
    OperationalError,
    ProgrammingError,
    TransactionManagementError,
)

__all__ = [
    "CAPABILITIES",
    "DSN_PARAMETERS",
    "IN_BULK_BATCH_SIZE",
    "ISOLATION_LEVELS",
    "MAX_GET_RESULTS",
    "AsyncpgConnection",
    "AsyncpgDriver",
    "AsyncpgMonitor",
    "connect_arguments",
    "server_version_num",
]

logger = logging.getLogger("adorm.drivers.asyncpg")

CAPABILITIES = Capabilities(
    operations=DJANGO_CAPABILITIES.operations,
    constructs=DJANGO_CAPABILITIES.constructs,
    connection_options=POSTGRESQL_OPTIONS,
    custom_options=True,
    backend_events={NOTIFY, NOTICE, CONNECTION_LOST},
    max_channel_length=MAX_IDENTIFIER_LENGTH,
    savepoints=True,
    cancellation=True,
)
"""What `AsyncpgDriver` supports: the operations and constructs of `adorm.drivers.django.CAPABILITIES` (reads,
writes, row locks), savepoints, cancellation, the connection options of `adorm.drivers.sql.POSTGRESQL_OPTIONS`,
custom options and the backend events `NOTIFY`, `NOTICE` and `CONNECTION_LOST`."""

MAX_GET_RESULTS = 21
"""How many rows ``get`` fetches at most to report how many matched, as Django's ``get()``."""

IN_BULK_BATCH_SIZE = 10_000
"""The most ids an ``in_bulk`` statement names; more ids run one statement per batch."""

ISOLATION_LEVELS = {
    1: "READ UNCOMMITTED",
    2: "READ COMMITTED",
    3: "REPEATABLE READ",
    4: "SERIALIZABLE",
}
"""The ``BEGIN`` isolation level for each value of psycopg's ``IsolationLevel`` (``OPTIONS["isolation_level"]``)."""

# OPTIONS keys that configure psycopg, Django's own pool or the compiler; they do not reach asyncpg.
_IGNORED_OPTIONS = frozenset({"pool", "prepare_threshold", "cursor_factory", "context", "server_side_binding"})
_ADORM_OPTIONS = frozenset({"assume_role", "isolation_level", "options", "connect_timeout"})

DSN_PARAMETERS = frozenset(
    {
        "passfile",
        "sslmode",
        "sslcert",
        "sslkey",
        "sslrootcert",
        "sslcrl",
        "sslpassword",
        "sslnegotiation",
        "ssl_min_protocol_version",
        "ssl_max_protocol_version",
        "target_session_attrs",
        "krbsrvname",
        "gsslib",
        "service",
        "application_name",
        "client_encoding",
    }
)
"""The ``OPTIONS`` keys passed in asyncpg's ``dsn``: libpq parameters asyncpg reads, and ``application_name`` and
``client_encoding``, which it sends to the server as settings."""


def _server_settings(options: str) -> dict[str, str]:
    """The run-time parameters of a libpq ``options`` string (``-c name=value`` and ``--name=value``)."""
    found: dict[str, str] = {}
    tokens = shlex.split(options)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "-c" and index + 1 < len(tokens):  # noqa: S105 - a libpq flag
            setting = tokens[index + 1]
            index += 2
        elif token.startswith("-c"):
            setting = token[2:]
            index += 1
        elif token.startswith("--"):
            setting = token[2:]
            index += 1
        else:
            raise NotSupportedError(f"unsupported libpq options {options!r}: only -c name=value is passed to asyncpg")
        name, separator, value = setting.partition("=")
        if not separator or not name:
            raise NotSupportedError(f"unsupported libpq options {options!r}: only -c name=value is passed to asyncpg")
        found[name.replace("-", "_")] = value
    return found


def _isolation_level(value: Any) -> str:
    try:
        return ISOLATION_LEVELS[int(value)]
    except (KeyError, TypeError, ValueError):
        raise ProgrammingError(
            f"invalid transaction isolation level {value!r}; use one of the psycopg.IsolationLevel values"
        ) from None


def connect_arguments(settings_dict: Mapping[str, Any], *, time_zone: str | None = None) -> dict[str, Any]:
    """The keyword arguments of ``asyncpg.connect()`` for a Django ``DATABASES`` entry.

    ``NAME``, ``USER``, ``PASSWORD``, ``HOST`` and ``PORT`` become ``database``, ``user``, ``password``, ``host`` and
    ``port`` (empty values are left to asyncpg's defaults; ``NAME`` ``None`` is ``postgres``). ``OPTIONS``:
    ``connect_timeout`` becomes ``timeout``, the ``-c name=value`` settings of ``options`` and ``time_zone`` (as
    ``TimeZone``) go to ``server_settings``; ``assume_role`` and ``isolation_level`` are applied by the connection;
    ``pool``, ``prepare_threshold``, ``cursor_factory``, ``context`` and ``server_side_binding`` do not concern
    asyncpg; the keys in `DSN_PARAMETERS` (``sslmode``, ``sslrootcert``, ``application_name`` …) go to asyncpg's
    ``dsn``. Any other key (a libpq parameter asyncpg does not read, such as ``keepalives``) and ``options`` other
    than run-time settings raise `NotSupportedError`.
    """
    options = dict(settings_dict.get("OPTIONS") or {})
    arguments: dict[str, Any] = {}
    name = settings_dict.get("NAME")
    if name is None:
        arguments["database"] = "postgres"
    elif name:
        arguments["database"] = name
    for key, argument in (("USER", "user"), ("PASSWORD", "password"), ("HOST", "host")):
        if settings_dict.get(key):
            arguments[argument] = settings_dict[key]
    if settings_dict.get("PORT"):
        arguments["port"] = int(settings_dict["PORT"])
    server_settings = _server_settings(options["options"]) if options.get("options") else {}
    if options.get("connect_timeout") is not None:
        arguments["timeout"] = float(options["connect_timeout"])
    if "isolation_level" in options:
        _isolation_level(options["isolation_level"])
    if time_zone is not None:
        server_settings["TimeZone"] = time_zone
    if server_settings:
        arguments["server_settings"] = server_settings
    unsupported = sorted(set(options) - _IGNORED_OPTIONS - _ADORM_OPTIONS - DSN_PARAMETERS)
    if unsupported:
        raise NotSupportedError(f"the asyncpg driver does not support the OPTIONS {', '.join(map(repr, unsupported))}")
    libpq = {k: str(v) for k, v in options.items() if k in DSN_PARAMETERS and v is not None}
    if libpq:
        arguments["dsn"] = "postgresql://?" + urlencode(libpq)
    return arguments


class AsyncpgConnection(DriverConnection):
    """An ``asyncpg.Connection`` for one slot, with a `DjangoCompiler` for its profile's ``DATABASES`` entry.

    Each operation translates into Django query sets, queries and model instances, compiles them on the event loop
    without a database connection and runs the statements on asyncpg; writes run Django's write logic as `Writer`
    does, without Django's model methods and signals. ``target`` (what profile hooks receive) is the
    ``asyncpg.Connection``; every statement a hook runs on it, ``COPY`` excepted, marks the connection with
    `mark_custom_sql()`.
    """

    driver: AsyncpgDriver

    def __init__(
        self,
        driver: AsyncpgDriver,
        profile: Profile,
        emit: Emit,
        conn: asyncpg.Connection,
        compiler: DjangoCompiler,
    ) -> None:
        super().__init__(driver)
        self.profile = profile
        self.conn = conn
        self.compiler = compiler
        self._emit = emit
        self._loop = asyncio.get_running_loop()
        options = compiler.settings_dict.get("OPTIONS") or {}
        level = options.get("isolation_level")
        self._begin = "BEGIN" if level is None else f"BEGIN ISOLATION LEVEL {_isolation_level(level)}"
        self._statement: asyncio.Task | None = None
        self._cancelled: asyncio.Task | None = None
        self._statement_failed = False
        self._savepoints: list[str] = []
        # The savepoint depth at which a write failed partway through, leaving the transaction rollback-only.
        self._rollback_only: int | None = None
        self._statements = 0
        self._usable = True
        self._closed = False
        self._test: Any = None
        conn.add_log_listener(self._on_log)
        conn.add_termination_listener(self._on_terminated)

    @property
    def target(self) -> Any:
        return self.conn

    async def call_hook(self, hook: Callable[..., Any], *args: Any) -> Any:
        """`DriverConnection.call_hook()`; a statement the hook runs on ``target`` (``COPY`` excepted) marks the
        connection with `mark_custom_sql()`."""
        queries = self.conn._protocol.queries_count
        try:
            return await super().call_hook(hook, *args)
        finally:
            if self.conn._protocol.queries_count != queries:
                self.mark_custom_sql()

    # Backend events

    def _on_notify(self, _conn: Any, _pid: int, channel: str, payload: str) -> None:
        self._emit(BackendEvent(NOTIFY, channel, payload))

    def _on_log(self, _conn: Any, message: Any) -> None:
        severity = getattr(message, "severity_en", None) or getattr(message, "severity", None)
        payload = notice_payload(
            severity,
            getattr(message, "sqlstate", None),
            getattr(message, "message", None),
            getattr(message, "detail", None),
            getattr(message, "hint", None),
        )
        self._emit(BackendEvent(NOTICE, severity, payload))

    def _on_terminated(self, _conn: Any) -> None:
        if self._closed:
            return
        self._usable = False
        self._emit(BackendEvent(CONNECTION_LOST, payload="the server connection ended"))

    async def _listen(self, channel: str) -> None:
        """Listen with ``asyncpg.Connection.add_listener()``."""
        await self._guarded(self.conn.add_listener(channel, self._on_notify))

    async def _unlisten(self, channel: str) -> None:
        await self._guarded(self.conn.remove_listener(channel, self._on_notify))

    @property
    def backend_pid(self) -> int | None:
        """``conn.get_server_pid()``."""
        return self.conn.get_server_pid()

    # Statements

    async def _run(self, coroutine: Any) -> Any:
        """Await ``coroutine`` in a task of its own, so that `cancel()` stops it without cancelling the caller."""
        if self._closed:
            coroutine.close()
            raise InterfaceError("asyncpg connection is closed")
        task = asyncio.ensure_future(coroutine)
        self._statement = task
        self._statements += 1
        try:
            return await task
        except asyncio.CancelledError:
            if not task.done() or task.cancelled():
                # The server may have run the statement without the protocol's Sync, so it keeps the transaction
                # and its locks open; the connection is closed instead of being reused.
                self._usable = False
            current = asyncio.current_task()
            if self._cancelled is task and current is not None and not current.cancelling():
                raise OperationalError("canceling statement due to user request", sqlstate="57014") from None
            raise
        except BaseException:
            self._statement_failed = True
            raise
        finally:
            self._statement = None
            self._cancelled = None

    def _compile(self, query: Any, *, count: bool = False, returning: Any = ()) -> DjangoStatement:
        in_transaction = self.conn.is_in_transaction()
        if count:
            return self.compiler.compile_count(query, in_transaction=in_transaction)
        return self.compiler.compile(query, in_transaction=in_transaction, returning=returning)

    async def _fetch(self, statement: DjangoStatement) -> list[tuple[Any, ...]]:
        return statement.rows(await self._run(self.conn.fetch(statement.sql, *statement.params)))

    async def _fetch_returned(self, statement: DjangoStatement) -> list[tuple[Any, ...]]:
        """The rows of the ``RETURNING`` clause of ``statement``, an insert or update; none for an empty one."""
        if statement.empty:
            return []
        return statement.returned(await self._run(self.conn.fetch(statement.sql, *statement.params)))

    async def _row_count(self, statement: DjangoStatement) -> int:
        """Run ``statement``, an insert, update or delete, and return the number of rows it affected."""
        if statement.empty:
            return 0
        status = await self._run(self.conn.execute(statement.sql, *statement.params))
        return int(status.rpartition(" ")[2])

    async def _execute(self, operation: Any) -> Any:
        if self._rollback_only is not None:
            raise TransactionManagementError(
                "a write failed partway through in this transaction; it can only be rolled back"
            )
        self._statement_failed = False
        try:
            if operation.kind == "check_table":
                return await self._check_table(operation)
            write = WRITES.get(operation.kind)
            if write is not None:
                with tenant_scope(getattr(operation, "tenant_schema", None)):
                    return await write(Writer(self), operation)
            return await self._read(operation)
        except asyncio.CancelledError:
            raise
        except SynchronousOnlyOperation as exc:
            raise NotSupportedError(
                f"building the {operation.kind!r} query accessed the database, which the asyncpg driver does not do "
                "on the event loop: a Django manager's get_queryset() must only build a query"
            ) from exc
        except DjangoValidationError as exc:
            raise validation_error(operation_model(operation), exc) from exc
        except Exception as exc:
            mapped = self._mapped(exc) if self._statement_failed else self.map_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc

    async def _check_table(self, operation: Any) -> list[dict[str, Any]]:
        """The problems of a `CheckTable`, read from the catalogue."""
        tables, problems = expected_tables(operation, self.compiler.connection, self.driver.name)
        for table in tables:
            rows = await self._run(self.conn.fetch(columns_sql("$1"), table.regclass))
            problems.extend(table_problems(table, rows))
        return problems

    async def _read(self, operation: Any) -> Any:
        kind = operation.kind
        if kind == "aggregate":
            if operation.query.is_combined:
                compiled = self.compiler.compile_combined_aggregate(
                    combined_aggregation(operation.query, operation.aggregations)
                )
            else:
                queryset, expressions = aggregation(operation.query, operation.aggregations)
                compiled = self.compiler.compile_aggregate(queryset, expressions)
            if compiled.statement.empty:
                return compiled.result(None)
            statement = compiled.statement
            return compiled.result(await self._run(self.conn.fetchrow(statement.sql, *statement.params)))
        if kind == "in_bulk":
            check_in_bulk_field(operation)
            ids = operation.ids
            if ids is None or len(ids) <= IN_BULK_BATCH_SIZE:
                translated = translate(operation.select_query())
                return translated.rows(await self._fetch(self._compile(translated.queryset)))
            rows = []
            for start in range(0, len(ids), IN_BULK_BATCH_SIZE):
                batch = list(ids[start : start + IN_BULK_BATCH_SIZE])
                translated = translate(operation.query.filter(**{f"{operation.field_name}__in": batch}))
                rows.extend(translated.rows(await self._fetch(self._compile(translated.queryset))))
            return rows
        translated = translate(operation.query)
        queryset = translated.queryset
        if kind == "select":
            return translated.rows(await self._fetch(self._compile(queryset)))
        if kind == "get":
            return translated.row(await self._get(queryset))
        if kind in ("first", "last"):
            if kind == "first":
                ordered = queryset if queryset.ordered else queryset.order_by("pk")
            else:
                ordered = queryset.reverse() if queryset.ordered else queryset.order_by("-pk")
            rows = await self._fetch(self._compile(ordered[:1]))
            return translated.row(rows[0]) if rows else None
        if kind == "count":
            statement = self._compile(queryset, count=True)
            return await self._run(self.conn.fetchval(statement.sql, *statement.params))
        if kind == "exists":
            # Django's exists() selects a constant in the queries of a union only, not in a nested combination.
            statement = self._compile(queryset[:1] if operation.query.is_combined else queryset.query.exists())
            return await self._run(self.conn.fetchrow(statement.sql, *statement.params)) is not None
        raise NotSupportedError(f"the asyncpg driver does not execute {kind!r} operations")

    async def _get(self, queryset: Any) -> tuple[Any, ...]:
        """The one row of ``queryset``, selected as Django's ``get()`` selects it."""
        clone = queryset.all()
        if clone.query.can_filter() and not clone.query.distinct_fields:
            clone = clone.order_by()
        clone.query.set_limits(high=MAX_GET_RESULTS)
        rows = await self._fetch(self._compile(clone))
        if len(rows) == 1:
            return rows[0]
        name = queryset.model._meta.object_name
        if not rows:
            raise ObjectDoesNotExist(f"{name} matching query does not exist.")
        found = len(rows) if len(rows) < MAX_GET_RESULTS else f"more than {MAX_GET_RESULTS - 1}"
        raise MultipleObjectsReturned(f"get() returned more than one {name} -- it returned {found}!")

    async def _command(self, sql: str) -> str:
        """Run ``sql``, a statement without parameters, and return its status; errors are mapped."""
        return await self._guarded(self.conn.execute(sql))

    async def _guarded(self, coroutine: Any) -> Any:
        """`_run()` ``coroutine``; errors are mapped."""
        try:
            return await self._run(coroutine)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            mapped = self._mapped(exc)
            if mapped is exc:
                raise
            raise mapped from exc

    # Transactions

    async def begin(self) -> None:
        """Start a transaction (``BEGIN``, with the entry's isolation level). Raises `TransactionManagementError` when
        one is open."""
        if self.conn.is_in_transaction():
            raise TransactionManagementError("a transaction is already open on this connection")
        self._savepoints = []
        self._rollback_only = None
        await self._command(self._begin)

    async def commit(self) -> None:
        """Commit the open transaction. A transaction that failed earlier, in the database or by a write that failed
        partway through, is rolled back instead; that raises `TransactionManagementError`, so the commit is never
        reported for it."""
        self._savepoints = []
        if self._rollback_only is not None:
            self._rollback_only = None
            await self._command("ROLLBACK")
            raise TransactionManagementError("a write failed partway through, so the transaction was rolled back")
        status = await self._command("COMMIT")
        if status == "ROLLBACK":
            raise TransactionManagementError("the transaction failed earlier, so it was rolled back")

    async def rollback(self) -> None:
        self._savepoints = []
        self._rollback_only = None
        await self._command("ROLLBACK")

    async def _set_options(self, options: dict[str, Any], local: bool) -> None:
        """Set the connection options (time limits, `role`, `search_path`, custom options) with `set_options_sql()`,
        in one simple-protocol message."""
        sql = set_options_sql(options, local)
        if sql:
            await self._command(sql)

    def _savepoint_index(self, name: str) -> int:
        if not self.conn.is_in_transaction():
            raise TransactionManagementError("no transaction is open on this connection")
        try:
            return self._savepoints.index(name)
        except ValueError:
            raise TransactionManagementError(f"no savepoint {name!r} is open") from None

    async def _savepoint(self, name: str) -> None:
        if not self.conn.is_in_transaction():
            raise TransactionManagementError("no transaction is open on this connection")
        if name in self._savepoints:
            raise TransactionManagementError(f"the savepoint {name!r} is already open")
        await self._command(f"SAVEPOINT {_quote_name(name)}")
        self._savepoints.append(name)

    async def _release_savepoint(self, name: str) -> None:
        index = self._savepoint_index(name)
        await self._command(f"RELEASE SAVEPOINT {_quote_name(name)}")
        # Releasing a savepoint releases the savepoints opened after it.
        del self._savepoints[index:]
        if self._rollback_only is not None:
            self._rollback_only = min(self._rollback_only, index)

    async def _rollback_to_savepoint(self, name: str) -> None:
        index = self._savepoint_index(name)
        await self._command(f"ROLLBACK TO SAVEPOINT {_quote_name(name)}")
        del self._savepoints[index + 1 :]
        if self._rollback_only is not None and self._rollback_only > index:
            self._rollback_only = None  # the failed write is undone

    def _mark_rollback_only(self) -> None:
        """Leave the open transaction rollback-only: a write failed after some of its statements ran."""
        depth = len(self._savepoints)
        self._rollback_only = depth if self._rollback_only is None else min(self._rollback_only, depth)

    # Connection

    def cancel(self) -> None:
        """Cancel the statement running now, if any; a statement that starts later is not affected. Callable from
        any thread. The cancelled call raises `OperationalError` with SQLSTATE ``57014``."""
        task = self._statement
        if task is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._cancel_statement, task)
        except RuntimeError:
            logger.warning("event loop closed, cannot cancel the running asyncpg statement")

    def _cancel_statement(self, task: asyncio.Task) -> None:
        if task is self._statement and not task.done():
            self._cancelled = task
            task.cancel()

    def is_usable(self) -> bool:
        """Also false, for a connection opened in a Django test, while a ``TestCase`` transaction is open on the test's
        connection (`adorm.testing.django.TestDatabase.current()`)."""
        usable = self._usable and not self._closed and not self.conn.is_closed()
        return usable and (self._test is None or self._test.current())

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.conn.close(timeout=self.driver.close_timeout)
        except Exception:
            logger.exception("closing asyncpg connection failed")

    def map_error(self, exc: BaseException) -> BaseException:
        """`map_django_error()`; asyncpg's ``InternalClientError`` (its protocol in an unexpected state, as after the
        server ended an idle connection) is `InterfaceError`."""
        if isinstance(exc, asyncpg.InternalClientError):
            return InterfaceError(str(exc), original=exc)
        return map_django_error(exc)

    def _mapped(self, exc: BaseException) -> BaseException:
        """``exc`` of a statement, mapped; a connection error makes the connection unusable."""
        mapped = self.map_error(exc)
        if self.is_connection_lost(mapped):
            self._usable = False
        return mapped


def _quote_name(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def _configure(conn: asyncpg.Connection, settings_dict: Mapping[str, Any], time_zone: str) -> None:
    """Set the session time zone when it differs from ``time_zone`` and assume ``OPTIONS["assume_role"]``."""
    if conn.get_settings().TimeZone != time_zone:
        await conn.execute("SELECT set_config('TimeZone', $1, false)", time_zone)
    role = (settings_dict.get("OPTIONS") or {}).get("assume_role")
    if role:
        await conn.execute("SELECT set_config('role', $1, false)", role)


class AsyncpgDriver(Driver):
    """Executes adorm reads and writes on asyncpg, natively async.

    A connection is an ``asyncpg.Connection`` opened with the profile's ``DATABASES`` entry (``alias``) and
    ``params``, which replace keys of that entry; SQL is compiled from Django query sets by a `DjangoCompiler` for the
    same entry and the server's version. Django must be set up (``django.setup()``) before the first connection
    opens. ``close_timeout`` is how many seconds closing a connection may take before it is aborted.
    """

    name = "asyncpg"
    mode = "async"
    capabilities = CAPABILITIES
    close_timeout: float = 5.0

    def database_key(self, profile: Profile) -> Any:
        """The PostgreSQL database of ``profile`` (`adorm.drivers.django.database_key()`)."""
        return database_key(profile)

    def connection_defaults(self, profile: Profile) -> dict[str, Any]:
        """The ``role`` of ``OPTIONS["assume_role"]`` (`adorm.drivers.django.connection_defaults()`)."""
        return connection_defaults(profile)

    def schema_hash(self, label: str) -> str:
        """The schema hash of the installed Django model ``label``, which the driver compiles queries for
        (`adorm.drivers.django.model_schema_hash()`)."""
        return model_schema_hash(label)

    async def open_connection(self, profile: Profile, emit: Emit) -> AsyncpgConnection:
        """Open ``profile``'s connection: ``profile.connect(profile)`` when the profile has a ``connect`` hook (its
        result is awaited when awaitable and must be an ``asyncpg.Connection``, else `InterfaceError`), otherwise
        ``asyncpg.connect()`` with `connect_arguments()`. Either way the session time zone is set to Django's for the
        entry and ``OPTIONS["assume_role"]`` is assumed. An alias without a ``DATABASES`` entry raises
        `ProgrammingError` and a backend that is not PostgreSQL `NotSupportedError`, both before anything
        connects. Inside Django's ``TestCase`` (`adorm.testing.django.find_test_database()`) it raises
        `NotSupportedError`: its connection could not see or roll back the test's transaction. Connects Django's
        ``post_migrate`` to `adorm.content_types.clear()`."""
        from adorm.testing.django import find_test_database

        clear_content_types_on_migrate()
        test = await find_test_database(profile.alias)
        if test is not None and test.in_test_case:
            raise NotSupportedError(
                f"profile {profile.name!r}: the asyncpg driver cannot run inside the transaction of Django's TestCase; "
                "use TransactionTestCase"
            )
        conn, compiler = await self._connect(profile)
        connection = AsyncpgConnection(self, profile, emit, conn, compiler)
        connection._test = test
        return connection

    async def open_monitor(self, profile: Profile) -> AsyncpgMonitor:
        """Open an `AsyncpgMonitor` on a connection opened as `open_connection()` opens one, also inside Django's
        ``TestCase``: it reads only ``pg_stat_activity``."""
        conn, _compiler = await self._connect(profile)
        return AsyncpgMonitor(conn, self.close_timeout)

    async def _connect(self, profile: Profile) -> tuple[asyncpg.Connection, DjangoCompiler]:
        conn = None
        try:
            settings_dict = _settings_dict(profile)
            time_zone = _time_zone(settings_dict)
            if profile.connect is None:
                conn = await asyncpg.connect(**connect_arguments(settings_dict, time_zone=time_zone))
            else:
                conn = profile.connect(profile)
                if inspect.isawaitable(conn):
                    conn = await conn
                if not isinstance(conn, asyncpg.Connection):
                    raise InterfaceError(f"the connect hook of {profile.name!r} returned {conn!r}, not a connection")
            await _configure(conn, settings_dict, time_zone)
            compiler = DjangoCompiler(
                profile.alias, server_version=server_version_num(conn.get_server_version()), params=profile.params
            )
        except BaseException as exc:
            if isinstance(conn, asyncpg.Connection):
                conn.terminate()
            mapped = map_django_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc
        return conn, compiler


class AsyncpgMonitor(DatabaseMonitor):
    """A `DatabaseMonitor` on an ``asyncpg.Connection`` of its own (``conn``)."""

    def __init__(self, conn: asyncpg.Connection, close_timeout: float = 5.0) -> None:
        self.conn = conn
        self.close_timeout = close_timeout

    async def activity(self, pids: Iterable[int]) -> dict[int, BackendActivity]:
        try:
            rows = await self.conn.fetch(activity_sql("$1"), [int(pid) for pid in pids])
        except Exception as exc:
            mapped = map_django_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc
        return {activity.pid: activity for activity in (backend_activity(*row) for row in rows)}

    async def close(self) -> None:
        if self.conn.is_closed():
            return
        try:
            await self.conn.close(timeout=self.close_timeout)
        except Exception:
            logger.exception("closing the asyncpg monitor failed")


def server_version_num(version: Any) -> int:
    """PostgreSQL's ``server_version_num`` for an asyncpg ``ServerVersion`` (``170006`` for 17.6, ``90624`` for
    9.6.24); asyncpg reads 17.6 as ``major`` 17, ``micro`` 6."""
    if version.major >= 10:
        return version.major * 10000 + version.micro
    return version.major * 10000 + version.minor * 100 + version.micro


def _settings_dict(profile: Profile) -> dict[str, Any]:
    """``profile``'s ``DATABASES`` entry with its ``params`` applied; it must use a PostgreSQL backend."""
    from django.db import connections
    from django.db.utils import load_backend

    try:
        entry = connections.settings[profile.alias]
    except KeyError:
        raise ProgrammingError(f"DATABASES has no entry {profile.alias!r} for profile {profile.name!r}") from None
    settings_dict = {**entry, **profile.params}
    vendor = load_backend(settings_dict["ENGINE"]).DatabaseWrapper.vendor
    if vendor != "postgresql":
        raise NotSupportedError(f"the asyncpg driver supports PostgreSQL databases, not {vendor!r}")
    return settings_dict


def _time_zone(settings_dict: Mapping[str, Any]) -> str:
    """The session time zone Django sets for a ``DATABASES`` entry."""
    from django.conf import settings

    if not settings.USE_TZ:
        return settings.TIME_ZONE
    return settings_dict.get("TIME_ZONE") or "UTC"
