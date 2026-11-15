# Writing a driver

A driver executes adorm operations on another ORM or database library. It implements the contract described in
the [drivers topic](/topics/drivers) and the [drivers reference](/ref/drivers). This guide builds a threaded
driver for Python's `sqlite3` module and outlines a native async driver.

The examples execute a minimal operation type of their own, `RawSQL`, so that they run as shown:

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

Any object with a `kind` and a `constructs()` method satisfies the [`Operation`](/ref/drivers#adorm-drivers-operation)
protocol.

## 1. Choose the execution mode

A synchronous library gets `mode = "threaded"`: subclass `ThreadedDriver` and `ThreadedConnection`, and every
call of a connection runs on that connection's own thread. `sqlite3` connections refuse use from another thread
by default, like Django's, so this is the mode for it.

A library with native coroutines gets `mode = "async"`: subclass `Driver` and `DriverConnection` directly
([step 6](#_6-a-native-async-driver)).

A threaded driver also runs direct, on the event loop's own thread, when `direct = True` is set on it: for one
task per event loop, such as a Celery task. The driver's code is the same in both cases.

## 2. Implement the synchronous methods

```python
import sqlite3

from adorm.drivers import ThreadedConnection
from adorm.exceptions import InterfaceError, OperationalError

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

    def map_error(self, exc):
        if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT:
            # A cancelled statement, not a lost connection.
            return OperationalError("statement cancelled", original=exc, sqlstate="57014")
        return super().map_error(exc)

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
- `cancel_sync()` runs on another thread while a call runs, possibly while `connect_sync()` runs. It must return
  without waiting for the call.
- `map_error()` runs on the connection's thread. The default, `wrap_driver_error`, already maps
  `sqlite3.IntegrityError` to `IntegrityError` by its PEP 249 class name. The override gives the cancelled
  statement SQLSTATE `57014`, the code adorm uses for a cancelled statement.
- `is_connection_lost()` decides which errors leave the connection unusable. The default,
  [`is_connection_error`](/ref/drivers#adorm-drivers-is-connection-error-exc), follows PostgreSQL and treats every
  `OperationalError` without a SQLSTATE as a lost connection; for `sqlite3` that would discard the connection on a
  misspelt table name. The override counts only I/O and file errors and `InterfaceError`.
- `close_sync()` also runs after a failed `connect_sync()`, so it checks what exists.

## 3. Declare the driver and its capabilities

```python
from adorm.drivers import Capabilities, ThreadedDriver


class SQLiteDriver(ThreadedDriver):
    name = "sqlite"
    connection_class = SQLiteConnection
    capabilities = Capabilities(operations={"raw"}, savepoints=True, cancellation=True)
```

`Capabilities` lists every operation kind and construct the driver executes and every connection option it
accepts, and whether it implements savepoints and cancellation (both `False` unless declared). The connection
checks every call against it before the driver's code runs. This driver accepts no options, so `set_options()`
raises `NotSupportedError`:

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

A connection reports events by calling `self.emit(event)`; on a threaded connection it may be called from any
thread, including the connection's, and the event reaches the slot before the result of the call that emitted it. Add the
names of the backend events to `capabilities.backend_events`. Emit only `CallbackEvent` and `BackendEvent` values,
and never `CallbackEvent("on_commit")` or `CallbackEvent("on_rollback")`: the slot delivers the outcome of each call
and those two callbacks itself, and drops anything else a connection emits.

```python
from adorm.events import BackendEvent


class NotifyingConnection(SQLiteConnection):
    def connect_sync(self):
        super().connect_sync()
        self.db.set_trace_callback(lambda sql: self.emit(BackendEvent("trace", payload=sql)))
```

## 5. Use the connection

The slot drives a connection; the same calls work in a test or a script:

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

Cancelling the awaiting task interrupts the statement, and the connection remains usable:

```python
task = asyncio.create_task(connection.execute(RawSQL(long_query)))
...
task.cancel()          # cancel_sync() interrupts the statement on the connection's thread
assert connection.is_usable()
```

## 6. A native async driver

An async driver implements `DriverConnection` directly. `DriverConnection` checks operations, options and
savepoint calls against the capabilities and then calls `_execute()`, `_set_options()` and the `_savepoint()`
family. The driver maps errors itself and keeps the statement that `cancel()` must stop. This one runs the
`RawSQL` operations on asyncpg; adorm's own driver for adorm queries on asyncpg is
[`AsyncpgDriver`](/topics/postgresql-driver).

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

Cancelling the task that awaits `execute()` needs no code: the cancellation reaches asyncpg, which sends a cancel
request to the server and waits for the statement to end.

## 7. Run the conformance suite

The repository's [driver conformance suite](/topics/conformance) runs every bundled driver over the demo's schema and
data set. A driver joins it with an entry in `tests/conformance/drivers.py`: a factory that returns an instance over
the seeded PostgreSQL test database, and a `Blocker` that makes one of its operations wait in the database. A driver
over that database reuses `TableLock`:

```python
DRIVERS = (
    ...,
    ConformanceDriver("psycopg", lambda _url: _sql_driver("psycopg"), lambda _driver, url: TableLock(url)),
)
```

A driver over another store loads the seeded data into it in its factory, as the memory driver does, and brings a
`Blocker` subclass of its own.

The driver must also be one of the demo's drivers (`demo.api.drivers.DRIVERS`), which a test checks. Each case of
the catalogue then runs on it: a case whose operation kinds and constructs the driver declares must return the
catalogue's result, any other case must raise `NotSupportedError` naming only names the driver lacks. A construct
the driver declares but gets wrong fails the case; one it does not declare cannot reach its code.

```console
$ ADORM_TEST_DATABASE_URL=postgres://… uv run pytest tests/conformance -k psycopg
```

## Checklist

- Every method raises adorm exceptions only; `asyncio.CancelledError` passes through.
- A field path or lookup key the target cannot resolve raises `FieldError` without a SQLSTATE, before any statement
  runs: an unknown field or relation, a segment after a field that is neither a relation nor a JSON field (never
  passed on as a transform of the target ORM) and a lookup on a relation whose `on_relations` is false. The
  transaction it is raised in stays usable; an error the database reports for a missing column keeps its SQLSTATE
  (`42703`, a `ProgrammingError`).
- Results are plain data keyed by adorm field names.
- Every segment of a field path (`copy___title` is `copy_`, then `title`), every key of written values and every key of
  returned rows is translated with the `orm_names` of the model it reaches (`ModelMeta.orm_name()`, or
  `ModelSchema.orm_names` from a schema source); a name it does not list is the same in the target.
- `capabilities` lists exactly what the driver executes, including `savepoints` and `cancellation` when it
  implements them, and the conformance suite passes on it.
- With `clause.manager`, a query and a `Create` that name a manager run through that manager of the target ORM; with
  the operation `call`, a `Call` runs the method on the target manager `Call.manager` or its query set and returns model instances as
  `ModelRow` values ([managers and driver methods](/ref/drivers#managers-and-driver-methods)).
- `connect_sync()` or `open_connection()` uses `profile.connect` when it is set.
- `target` returns the target's connection object, which the profile's `on_acquire` and `on_release` hooks receive.
- A cancelled statement reports SQLSTATE `57014`, not a connection error. When the cancellation can leave the
  server connection with an open transaction or locks, `is_usable()` returns `False` so that the slot closes it.
- A request method cancelled after its statement finished returns that result or raises that error, which the
  caller's `CancelledError` reports as `CancellationOutcome.SUCCEEDED` or `FAILED` (`COMMITTED` for a commit); it
  raises `asyncio.CancelledError` only when the statement's effect is unknown, which reports `UNKNOWN`
  ([driver contract](/ref/drivers#cancellation)). `ThreadedConnection` does this for a threaded driver.
- `is_connection_lost()` is true only for errors after which the connection cannot be used; override it when the
  target reports statement errors as `OperationalError` without a SQLSTATE. The slot closes a connection whose
  request failed with such an error.
- Code the driver runs for an operation that executes SQL of its own (application code of the target ORM) calls
  `mark_custom_sql()`, so that the slot closes the connection when the lease ends; work handed to `after_call_sync()`
  is part of the call it came from ([custom SQL](/ref/drivers#mark-custom-sql)).
- Nothing in the driver closes or pings a connection on a schedule of its own: the slot decides its lifetime
  (`purge_stale_connections_at`).
- `close()` never raises and can be called twice.
- The driver's ORM is imported in the driver's own module only, never by `adorm` itself.
