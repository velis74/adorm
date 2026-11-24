# Writing a driver

A driver executes adorm operations on another ORM or database library and implements the contract of the
[drivers topic](/topics/drivers) and the [drivers reference](/ref/drivers). This guide builds a threaded driver for
Python's `sqlite3` module and outlines a native async driver.

The examples execute a minimal operation type, `RawSQL`:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class RawSQL:
    sql: str
    params: tuple = ()
    kind: str = "raw"

    def constructs(self):
        return ()
```

An object with a `kind` and a `constructs()` method satisfies the [`Operation`](/ref/drivers#adorm-drivers-operation)
protocol.

## 1. Choose the execution mode

- A synchronous library: `mode = "threaded"`. Subclass `ThreadedDriver` and `ThreadedConnection`; every call of a
  connection runs on that connection's own thread. `sqlite3` connections, like Django's, refuse use from another
  thread by default, so `sqlite3` takes this mode.
- A library with native coroutines: `mode = "async"`. Subclass `Driver` and `DriverConnection`
  ([step 6](#_6-a-native-async-driver)).

A threaded driver with `direct = True` runs on the event loop's own thread, for one task per event loop such as a
Celery task. The driver's code is the same in both cases.

## 2. Implement the synchronous methods

```python
import sqlite3

from adorm.drivers import ThreadedConnection
from adorm.exceptions import InterfaceError

LOST = {sqlite3.SQLITE_IOERR, sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_CORRUPT}


class SQLiteConnection(ThreadedConnection):
    def connect_sync(self):
        if self.profile.connect is not None:
            self.db = self.profile.connect(self.profile)
        else:
            self.db = sqlite3.connect(self.profile.params["path"], isolation_level=None)
        self.db.row_factory = sqlite3.Row

    @property
    def target(self):
        return self.db              # what the profile's on_acquire and on_release hooks receive

    def execute_sync(self, operation):
        cursor = self.db.execute(operation.sql, operation.params)
        if cursor.description is None:
            return cursor.rowcount
        return [dict(row) for row in cursor.fetchall()]

    def begin_sync(self):
        self.db.execute("BEGIN")

    def commit_sync(self):
        self.db.execute("COMMIT")

    def rollback_sync(self):
        self.db.execute("ROLLBACK")

    def savepoint_sync(self, name):
        self.db.execute(f'SAVEPOINT "{name}"')

    def release_savepoint_sync(self, name):
        self.db.execute(f'RELEASE SAVEPOINT "{name}"')

    def rollback_to_savepoint_sync(self, name):
        self.db.execute(f'ROLLBACK TO SAVEPOINT "{name}"')

    def cancel_sync(self):
        self.db.interrupt()  # thread-safe; the running statement fails with sqlite3.OperationalError

    def is_cancellation(self, exc):
        # sqlite3 reports an interrupted statement without a SQLSTATE.
        code = getattr(getattr(exc, "original", None), "sqlite_errorcode", None)
        return code is not None and (code & 0xFF) == sqlite3.SQLITE_INTERRUPT

    def is_connection_lost(self, exc):
        # sqlite3 reports a missing table or a locked database as OperationalError without a SQLSTATE.
        code = getattr(getattr(exc, "original", None), "sqlite_errorcode", None)
        return isinstance(exc, InterfaceError) or (code is not None and (code & 0xFF) in LOST)

    def close_sync(self):
        if hasattr(self, "db"):
            self.db.close()
```

- `execute_sync()` returns plain data: dicts for rows, a count for writes. The core builds model instances.
- `connect_sync()` uses the profile's `connect` hook when it is set, on the connection's thread.
- `target` is the `sqlite3.Connection`; the profile's `on_acquire` and `on_release` hooks receive it on the
  connection's thread.
- `cancel_sync()` runs on another thread while a call runs, possibly during `connect_sync()`. It must return without
  waiting for the call.
- `map_error()` runs on the connection's thread. The default, `wrap_driver_error`, maps `sqlite3.IntegrityError` to
  `IntegrityError` by its PEP 249 class name.
- `is_cancellation()` decides which mapped error is the end of a cancelled statement, so that the awaiting task gets
  `asyncio.CancelledError`. The default accepts SQLSTATE `57014`; the override accepts SQLite's `SQLITE_INTERRUPT`.
- `is_connection_lost()` decides which errors leave the connection unusable. The default,
  [`is_connection_error`](/ref/drivers#adorm-drivers-is-connection-error-exc), follows PostgreSQL and treats every
  `OperationalError` without a SQLSTATE as a lost connection; with `sqlite3` a misspelt table name would discard the
  connection. The override counts only I/O and file errors and `InterfaceError`.
- `close_sync()` also runs after a failed `connect_sync()` and checks what exists.

## 3. Declare the driver and its capabilities

```python
from adorm.drivers import Capabilities, ThreadedDriver


class SQLiteDriver(ThreadedDriver):
    name = "sqlite"
    connection_class = SQLiteConnection
    capabilities = Capabilities(operations={"raw"}, savepoints=True, cancellation=True)
```

`Capabilities` lists the operation kinds, constructs and connection options the driver supports, and whether it
implements savepoints and cancellation (both `False` by default). The connection checks every call against it before
the driver's code runs. A driver whose support depends on the database a profile names overrides
[`capabilities_for(profile)`](/ref/drivers#capabilities-for-profile), which the pool reads once without I/O. This driver
accepts no options, so `set_options()` raises `NotSupportedError`:

```python
await connection.set_options({"statement_timeout": 5}, local=False)
# NotSupportedError: unsupported connection options: statement_timeout
```

To support options, add their names to `connection_options` and implement `set_options_sync(options, local)`:
`local=True` limits them to the open transaction (`SET LOCAL`), and a value of `None` resets an option to its
default. The slot calls it before an operation, only with the options that changed
([connection options](/topics/options#what-reaches-the-database)). A PostgreSQL driver can build the statements with
[`set_options_sql()`](/ref/drivers#adorm-drivers-sql-set-options-sql-options-local):

```python
from adorm.drivers import Capabilities, ThreadedConnection, ThreadedDriver
from adorm.drivers.sql import POSTGRESQL_OPTIONS, set_options_sql


class PostgresDriver(ThreadedDriver):
    capabilities = Capabilities(operations={"raw"}, savepoints=True, connection_options=POSTGRESQL_OPTIONS)


class PostgresConnection(ThreadedConnection):
    def set_options_sync(self, options, local):
        sql = set_options_sql(options, local)   # "SET LOCAL lock_timeout = 500; SET LOCAL statement_timeout TO DEFAULT"
        if sql:
            with self.db.cursor() as cursor:
                cursor.execute(sql)
```

## 4. Report events

A connection reports events with `self.emit(event)`. On a threaded connection any thread may call it; the event
reaches the slot before the result of the call that emitted it. The names of backend events go in
`capabilities.backend_events`. A connection emits only `CallbackEvent` and `BackendEvent` values, never
`CallbackEvent("on_commit")` or `CallbackEvent("on_rollback")`: the slot delivers the outcome of each call and those
two callbacks itself, and drops anything else a connection emits.

```python
from adorm.events import BackendEvent


class NotifyingConnection(SQLiteConnection):
    def connect_sync(self):
        super().connect_sync()
        self.db.set_trace_callback(lambda sql: self.emit(BackendEvent("trace", payload=sql)))
```

A driver whose target delivers notifications declares `notify` and implements listening. An async connection
implements `_listen()` and `_unlisten()`; a threaded one `listen_sync()` and `unlisten_sync()`. When the target
reads its socket only during a call, a threaded connection also implements `idle_fileno()` and `idle_sync()`: the
connection's thread waits for that socket between calls and reads what arrived. An exception from `idle_sync()` makes
the connection unusable and is emitted as `BackendEvent("connection_lost")`, which ends an `adorm.listen()` block.

```python
from adorm.drivers.postgresql import listen_sql, unlisten_sql
from adorm.events import BackendEvent, NOTIFY


class ListeningConnection(PostgresConnection):       # self.db is a psycopg 3 connection
    channels = frozenset()

    def listen_sync(self, channel):
        self.db.execute(listen_sql(channel))
        self.channels |= {channel}

    def unlisten_sync(self, channel):
        self.db.execute(unlisten_sql(channel))
        self.channels -= {channel}

    def idle_fileno(self):
        return self.db.fileno() if self.channels else None

    def idle_sync(self):
        for notify in self.db.notifies(timeout=0):
            self.emit(BackendEvent(NOTIFY, notify.channel, notify.payload))


class ListeningDriver(PostgresDriver):
    connection_class = ListeningConnection
    capabilities = Capabilities(operations={"raw"}, backend_events={NOTIFY})
```

## 5. Use the connection

The slot calls a connection; a test or a script makes the same calls:

```python
import asyncio


class Profile:
    name = "default"
    alias = "default"
    params = {"path": ":memory:"}
    connect = None


async def main():
    connection = await SQLiteDriver().open_connection(Profile(), print)
    try:
        await connection.execute(RawSQL("CREATE TABLE item (name TEXT UNIQUE)"))
        await connection.begin()
        await connection.execute(RawSQL("INSERT INTO item VALUES (?)", ("a",)))
        await connection.savepoint("s1")
        await connection.execute(RawSQL("INSERT INTO item VALUES (?)", ("b",)))
        await connection.rollback_to_savepoint("s1")
        await connection.commit()
        print(await connection.execute(RawSQL("SELECT name FROM item")))   # [{'name': 'a'}]
    finally:
        await connection.close()


asyncio.run(main())
```

Cancelling the awaiting task interrupts the statement; the connection stays usable:

```python
task = asyncio.create_task(connection.execute(RawSQL(long_query)))
...
task.cancel()          # cancel_sync() interrupts the statement on the connection's thread
assert connection.is_usable()
```

## 6. A native async driver

An async driver subclasses `DriverConnection`, which checks operations, options and savepoint calls against the
capabilities and then calls `_execute()`, `_set_options()` and the `_savepoint()` family. The driver maps errors
itself and keeps a reference to the statement that `cancel()` stops. This example runs `RawSQL` on asyncpg; adorm's
driver for adorm queries on asyncpg is [`AsyncpgDriver`](/topics/postgresql-driver).

```python
import asyncio

import asyncpg

from adorm.drivers import Capabilities, Driver, DriverConnection
from adorm.exceptions import OperationalError, wrap_driver_error


class RawSQLConnection(DriverConnection):
    def __init__(self, driver, conn, emit):
        super().__init__(driver)
        self.conn, self.emit = conn, emit
        self.loop = asyncio.get_running_loop()
        self.statement = None
        self.cancel_requested = False
        self.usable = True

    async def run(self, coro):
        # The statement runs in its own task so that cancel() stops it without cancelling the caller.
        self.statement = asyncio.ensure_future(coro)
        self.cancel_requested = False
        try:
            return await self.statement
        except asyncio.CancelledError:
            if self.cancel_requested and not asyncio.current_task().cancelling():
                raise OperationalError("statement cancelled", sqlstate="57014") from None
            raise
        except Exception as exc:
            mapped = self.map_error(exc)
            self.usable = self.usable and not self.is_connection_lost(mapped)
            raise mapped from exc
        finally:
            self.statement = None

    async def _execute(self, operation):
        return [dict(r) for r in await self.run(self.conn.fetch(operation.sql, *operation.params))]

    def cancel(self):
        def cancel_statement():
            if self.statement is not None and not self.statement.done():
                self.cancel_requested = True
                self.statement.cancel()

        self.loop.call_soon_threadsafe(cancel_statement)

    def is_usable(self):
        return self.usable and not self.conn.is_closed()

    @property
    def target(self):
        return self.conn            # profile hooks receive the asyncpg.Connection

    # begin, commit, rollback, _savepoint, _release_savepoint, _rollback_to_savepoint, _set_options and close
    # follow the same pattern: await self.run(...) around the asyncpg call.


class RawSQLDriver(Driver):
    name = "raw-sql"
    mode = "async"
    capabilities = Capabilities(operations={"raw"}, savepoints=True, cancellation=True)

    async def open_connection(self, profile, emit):
        try:
            if profile.connect is not None:
                conn = await profile.connect(profile)
            else:
                conn = await asyncpg.connect(profile.params["dsn"])
        except Exception as exc:
            raise wrap_driver_error(exc) from exc
        return RawSQLConnection(self, conn, emit)
```

Cancelling the task that awaits `execute()` needs no driver code: asyncpg sends a cancel request to the server and
waits for the statement to end.

### Report database sessions

[`wait_for_lock()`](/ref/testing#adorm-testing-wait-for-lock-task-none-session-none-timeout-10-0-interval-0-01) needs
from a driver:

- the database session of each connection ([`backend_pid`](/ref/drivers#backend-pid));
- a monitor that reads the database's session activity on a connection of its own
  ([`open_monitor()`](/ref/drivers#async-open-monitor-profile)).

A driver over PostgreSQL runs [`activity_sql()`](/ref/drivers#adorm-drivers-postgresql-activity-sql-parameter):

```python
class RawSQLConnection(DriverConnection):
    @property
    def backend_pid(self):
        return self.conn.get_server_pid()


class RawSQLMonitor(DatabaseMonitor):
    def __init__(self, conn):
        self.conn = conn

    async def activity(self, pids):
        rows = await self.conn.fetch(activity_sql("$1"), list(pids))
        return {row[0]: backend_activity(*row) for row in rows}

    async def close(self):
        await self.conn.close()


class RawSQLDriver(Driver):
    async def open_monitor(self, profile):
        return RawSQLMonitor(await asyncpg.connect(profile.params["dsn"]))
```

Without them `wait_for_lock()` raises `NotSupportedError`; `wait_for_operation()` works on every driver.

### Schema checks

A driver whose target knows the models takes part in the [schema checks](/topics/schema-checks):

- `schema_hash(label)` returns the target's hash of a model, computed as `adorm generate` computes it, and raises
  `LookupError` for a model the target lacks. The driver computes a label's hash once and keeps it.
- [`check()`](/ref/drivers#check-operation-schema-hashes-none-capabilities-none) compares it with the hash the client
  expects for every model an operation reaches and raises `SchemaMismatchError` on a difference.
- The client's hashes come from the process's model registry by default. A driver that executes operations for
  clients in other processes receives each client's hashes when the client connects (the client sends
  [`registry.schema_hashes()`](/ref/models#adorm-models-registry)) and passes them as
  `check(operation, schema_hashes=...)`.

The operation `check_table` compares what a model expects of the database with the catalogue. A PostgreSQL driver
builds [`ExpectedTable`](/ref/drivers#adorm-drivers-postgresql-expectedtable-name-regclass-columns) values and uses
the comparison helpers of `adorm.drivers.postgresql`:

```python
from adorm import SchemaMismatchError
from adorm.drivers.postgresql import columns_sql, ExpectedColumn, ExpectedTable, table_problems


class RawSQLDriver(Driver):
    capabilities = Capabilities(operations={"raw", "check_table"})

    def schema_hash(self, label):
        return SCHEMA_HASHES[label]          # KeyError is a LookupError: the target has no such model


class RawSQLConnection(DriverConnection):
    async def _execute(self, operation):
        if operation.kind == "check_table":
            table = TABLES.get(operation.model)  # {"name": ..., "columns": {orm_name: (column, type, nullable)}}
            if table is None:
                reason = f"the target has no model {operation.model}"
                raise SchemaMismatchError(reason, model=operation.model, driver=self.driver.name, reason=reason)
            columns = tuple(
                ExpectedColumn(*table["columns"][name], field=name) for name in operation.fields
            )
            expected = ExpectedTable(table["name"], f'"{table["name"]}"', columns)
            rows = await self.conn.fetch(columns_sql("$1"), expected.regclass)
            return table_problems(expected, rows)
        ...
```

A `check_table` of a model the target lacks raises `SchemaMismatchError` (with `actual` `None` and a `reason`) before
any statement; the transaction stays usable and `check_schema()` reports it as a `schema_hash` problem.

Reading the catalogue is SQL the driver generates and does not call `mark_custom_sql()`.

## 7. Run the conformance suite

The repository's [driver conformance suite](/topics/conformance) runs every bundled driver over the test suite's club
schema and data. A driver joins it with a `ConformanceDriver` entry in `tests/conformance/drivers.py`, whose fields
are:

| Field | Value |
|---|---|
| `name` | the name `pytest -k` selects |
| `target` | the driver class as `"module:Name"` |
| `create(url)` | a coroutine returning an instance over the seeded test database at `url` |
| `blocker(driver, url)` | a `Blocker` that makes one of the driver's operations wait in the database |
| `driver_code` | what the driver does with Python code of the target ORM that a write would call: `"runs"` it, `"refused"` (a write that would call an override or handler raises), `"not run"` |
| `notifier(driver, url)` | a `Notifier` that sends notifications to the driver's listeners from outside the pool |

A driver over the PostgreSQL test database uses `TableLock` and `PostgresNotifier`:

```python
DRIVERS = (
    ...,
    ConformanceDriver(
        "psycopg",
        "mypackage.drivers:PsycopgDriver",
        lambda _url: _sql_driver("psycopg"),
        lambda _driver, url: TableLock(url),
        "refused",
        lambda _driver, url: PostgresNotifier(url),
    ),
)
```

A driver over another store loads the seeded data into it in its factory, as the memory driver does, and has its
own `Blocker` subclass.

A test checks that every driver class in the modules under `adorm.drivers` has an entry, and another that the demo runs
it. Every case of the catalogue runs on it: a case whose operation kinds and constructs the driver declares must return
the catalogue's result, any other case must raise `NotSupportedError` naming only names the driver lacks. A declared
construct the driver executes wrongly fails the case; an undeclared one does not reach its code.

```bash
ADORM_TEST_DATABASE_URL=postgres://… uv run pytest tests/conformance -k psycopg
```

## Checklist

- Every method raises adorm exceptions only; `asyncio.CancelledError` passes through.
- A field path or lookup key the target cannot resolve raises `FieldError` without a SQLSTATE, before any statement
  runs: an unknown field or relation, a segment after a field that is neither a relation nor a JSON field and is no
  registered transform applying there (`Transform.applies_to()`), and a lookup on a relation whose `on_relations` is
  false. A transform the driver runs is declared as the construct `transform.<name>`. The
  transaction it is raised in stays usable; an error the database reports for a missing column keeps its SQLSTATE
  (`42703`, a `ProgrammingError`).
- An `IntegrityError` describes the violation (`constraint_name`, `table_name`, `schema_name`, `columns`, `values`,
  `detail`) as the database reports it, and names the model of the table (`model`) and its adorm field names of the
  key's columns (`fields`). A driver over PostgreSQL passes
  [`integrity_details`](/ref/drivers#adorm-drivers-postgresql-integrity-details-exc) to
  `wrap_driver_error(exc, details=...)` and maps the columns to field names with the target ORM's column mapping and
  `orm_names`.
- Results are plain data keyed by adorm field names.
- Every segment of a field path (`copy___title` is `copy_`, then `title`), every key of written values and every key of
  returned rows is translated with the `orm_names` of the model it reaches (`ModelMeta.orm_name()`, or
  `ModelSchema.orm_names` from a schema source); a name it does not list is the same in the target.
- `capabilities` lists exactly what the driver executes, including `savepoints` and `cancellation` when it
  implements them, and the conformance suite passes on it.
- With `clause.tenant_schema`, every table of a model in a query's
  [`TenantSchema`](/ref/query#adorm-tenantschema-schema-models) `models` is read as the schema's table, per query and
  per query of a set operation, its subqueries included, and the `tenant_schema` of `Save`, `DeleteInstance` and
  `FullClean` qualifies the tables of its models in every statement the write runs. With `aggregate.combined`, an
  `Aggregate` over a combined query aggregates the combined rows. A driver without SQL declares neither, and a
  [`TenantRelation`](/ref/tenant-relations) refuses to read on it.
- With `clause.manager`, a query and a `Create` that name a manager run through that manager of the target ORM; with
  the operation `call`, a `Call` runs the method on the target manager `Call.manager` or its query set and returns model
  instances as `ModelRow` values ([managers and driver methods](/ref/drivers#managers-and-driver-methods)).
- `connect_sync()` or `open_connection()` uses `profile.connect` when it is set.
- `target` returns the target's connection object, which the profile's `on_acquire` and `on_release` hooks receive.
  A driver that sees the statements run on it marks those a hook runs with `mark_custom_sql()`; session parameters
  reach the connection as connection options, which `on_acquire` returns.
- `backend_pid` names the connection's database session and `open_monitor()` reads sessions on a connection of its
  own, when the database reports lock waits.
- A connection that shares its database session with other connections and takes it per call reports
  `holds_session` and `session_holds` ([driver contract](/ref/drivers#holds-session-session-holds)).
- A cancelled statement reports SQLSTATE `57014`, not a connection error. When the cancellation can leave the
  server connection with an open transaction or locks, `is_usable()` returns `False` and the slot closes it.
- A request method cancelled after its statement finished returns that result or raises that error, which the
  caller's `CancelledError` reports as `CancellationOutcome.SUCCEEDED` or `FAILED` (`COMMITTED` for a commit); it
  raises `asyncio.CancelledError` only when the statement's effect is unknown, which reports `UNKNOWN`
  ([driver contract](/ref/drivers#cancellation)). `ThreadedConnection` does this for a threaded driver.
- `is_connection_lost()` is true only for errors after which the connection cannot be used; override it when the
  target reports statement errors as `OperationalError` without a SQLSTATE. The slot closes a connection whose
  request failed with such an error.
- Code the driver runs for an operation that executes SQL of its own (application code of the target ORM) calls
  `mark_custom_sql()`, and the slot closes the connection when the lease ends; work handed to `after_call_sync()`
  is part of the call it came from ([custom SQL](/ref/drivers#mark-custom-sql)).
- `schema_hash()` returns the target's hash of a model as `adorm generate` computes it, or `None` when the driver
  cannot tell; with `check_table` declared, the operation returns the problems of `table_problems()` or the same dicts
  ([schema checks](#schema-checks)).
- Nothing in the driver closes or pings a connection on a schedule of its own: the slot decides its lifetime
  (`purge_stale_connections_at`).
- `close()` never raises and can be called twice.
- The driver's ORM is imported in the driver's own module only, never by `adorm` itself.
