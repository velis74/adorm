# Drivers

The driver contract, importable from `adorm.drivers` (defined in `adorm.drivers.base`). The
[drivers topic](/topics/drivers) explains how the pieces fit; [Writing a driver](/howto/writing-a-driver) builds
one step by step. Importing `adorm.drivers` imports no ORM or database library.

```python
from adorm.drivers import Capabilities, Driver, DriverConnection, ThreadedConnection, ThreadedDriver
```

## Driver

### `adorm.drivers.Driver`

Abstract base class of every driver.

| Attribute | Meaning |
|---|---|
| `name` | the driver's name, e.g. `"django"`; also used in thread names and messages |
| `mode` | an [`ExecutionMode`](#adorm-drivers-executionmode); any other value raises `TypeError` when the class is defined |
| `capabilities` | a [`Capabilities`](#adorm-drivers-capabilities) value |

#### `async open_connection(profile, emit)`

Opens a connection for `profile` (a [`Profile`](#adorm-drivers-profile)) and returns a
[`DriverConnection`](#adorm-drivers-driverconnection). When `profile.connect` is set, the target's connection
comes from it instead of the driver's default. The connection reports events by calling `emit` with an
[`Event`](/ref/events). A failure raises an adorm exception.

```python
class AsyncpgDriver(Driver):
    name = "asyncpg"
    mode = "async"
    capabilities = Capabilities(operations={"select"}, connection_options={"statement_timeout"})

    async def open_connection(self, profile, emit):
        try:
            if profile.connect is not None:
                conn = await profile.connect(profile)
            else:
                conn = await asyncpg.connect(profile.params["dsn"])
        except Exception as exc:
            raise wrap_driver_error(exc) from exc
        return AsyncpgConnection(self, conn, emit)
```

#### `is_refusal(exc)`

Returns whether `exc`, an adorm exception `open_connection()` raised, means the database refuses more connections.
A [pool](/ref/pool#adorm-executor-pool-driver-profile-max-connections-0-acquire-timeout-30-0) with `max_connections=0` then stops opening connections and queues the
request. Defaults to [`is_refusal(exc)`](#adorm-drivers-is-refusal-exc). A driver whose library reports a refused
connection without a SQLSTATE overrides it:

```python
class DjangoDriver(ThreadedDriver):
    def is_refusal(self, exc):
        # psycopg raises "too many connections" without a SQLSTATE
        return super().is_refusal(exc) or "too many connections" in str(exc.original)
```

### `adorm.drivers.ExecutionMode`

`Literal["threaded", "async"]`. `"threaded"`: each connection runs a synchronous target on its own thread.
`"async"`: native coroutines on the event loop. A threaded driver with `direct = True` runs on the event loop's
thread instead (see [`ThreadedDriver`](#adorm-drivers-threadeddriver)).

### `adorm.drivers.Capabilities`

`Capabilities(operations=frozenset(), constructs=frozenset(), connection_options=frozenset(), backend_events=frozenset(), savepoints=False, cancellation=False)`

What a driver supports; a frozen, hashable dataclass. The four name fields accept any iterable of strings and
are stored as `frozenset`; a plain string raises `TypeError`.

| Field | Contents |
|---|---|
| `operations` | operation kinds (`Operation.kind`) |
| `constructs` | construct names (`Operation.constructs()`), `<category>.<name>` |
| `connection_options` | option names `set_options()` accepts |
| `backend_events` | `BackendEvent.name` values the driver emits |
| `savepoints` | the driver implements savepoints; `False` by default |
| `cancellation` | the driver cancels a running statement; `False` by default |

#### `unsupported(operation)`

Returns the names `operation` uses that the driver does not support, as a list: the operation's kind first when
it is unsupported, then the unsupported constructs in sorted order. An empty list means the operation is
supported.

#### `check(operation)`

Raises `NotSupportedError` naming every unsupported name, or returns `None`.

#### `check_savepoints()`

Raises `NotSupportedError` when `savepoints` is `False`.

#### `check_options(options)`

Raises `NotSupportedError` naming every option in `options` (any iterable of names, e.g. a dict) the driver does
not support.

```python
caps = Capabilities(operations={"select"}, constructs={"lookup.exact"}, connection_options={"statement_timeout"})

caps.unsupported(operation)        # ["lookup.icontains"] for a select with an icontains lookup
caps.check(operation)              # NotSupportedError: 'select' operation needs unsupported lookup.icontains
caps.check_options({"work_mem": "4MB"})   # NotSupportedError: unsupported connection options: work_mem
```

### `adorm.drivers.Operation`

A runtime-checkable protocol: what a driver needs to know about an operation before executing it.

| Member | Meaning |
|---|---|
| `kind` | the operation's name, e.g. `"select"` |
| `constructs()` | an iterable of the construct names the operation uses |

adorm's operations ([`adorm.operations`](./operations)) satisfy it; their kinds are listed in
[`OPERATIONS`](./operations#adorm-operations-operations).

### `adorm.drivers.Profile`

A runtime-checkable protocol for the connection profile a connection is opened for.

| Member | Meaning |
|---|---|
| `name` | the `using` value |
| `alias` | the target's database alias, e.g. a key of Django's `DATABASES` |
| `params` | a mapping of driver parameters |
| `connect` | `None`, or a callable that opens the target's connection in place of the driver's default: `connect(profile)` returns the target's connection. A threaded driver calls it in `connect_sync()` on the connection's thread; an async driver awaits its result in `open_connection()` |

### `adorm.drivers.is_connection_error(exc)`

Returns whether `exc`, an adorm exception, leaves a PostgreSQL connection unusable: `True` for `InterfaceError`,
for `OperationalError` without a SQLSTATE or with a SQLSTATE of class `08` (connection exception) or `57P01` to
`57P03` (shutdown), and for exceptions that are not an `Exception` other than `asyncio.CancelledError`. It is the
default of [`DriverConnection.is_connection_lost()`](#adorm-drivers-driverconnection).

```python
is_connection_error(OperationalError(sqlstate="08006"))   # True
is_connection_error(OperationalError(sqlstate="57014"))   # False
```

### `adorm.drivers.is_refusal(exc)`

Returns whether `exc` is an `OperationalError` with a SQLSTATE of class `53` (insufficient resources, such as
`53300` `too_many_connections`). It is the default of [`Driver.is_refusal()`](#is-refusal-exc).

```python
is_refusal(OperationalError(sqlstate="53300"))   # True
is_refusal(OperationalError())                   # False: no SQLSTATE
```

## DriverConnection

### `adorm.drivers.DriverConnection`

`DriverConnection(driver)`

Abstract base class of a driver's connection; `driver` is available as an attribute. A connection is owned by
exactly one slot, which awaits one call at a time from one event loop. Every method raises adorm exceptions only;
`asyncio.CancelledError` passes through.

The contract the slot calls:

| Method | Does |
|---|---|
| `async execute(operation)` | checks `operation` against `driver.capabilities`, then awaits `_execute(operation)`; returns rows as dicts keyed by adorm field names, a scalar or a count |
| `async begin()` | starts a transaction |
| `async savepoint(name)` | checks `capabilities.savepoints`, then creates the savepoint `name` |
| `async release_savepoint(name)` | checks `capabilities.savepoints`, then releases the savepoint `name` |
| `async rollback_to_savepoint(name)` | checks `capabilities.savepoints`, then rolls back to the savepoint `name` |
| `async commit()` | commits the open transaction |
| `async rollback()` | rolls back the open transaction |
| `async set_options(options, local)` | checks the option names against `capabilities.connection_options`, then sets them from a mapping; `local=True` limits them to the open transaction (`SET LOCAL`) |
| `cancel()` | cancels the running statement, if any; callable from any thread, returns without waiting; the call raises `OperationalError` with SQLSTATE `57014` |
| `is_usable()` | whether the connection can serve further calls; does no I/O |
| `async close()` | closes the connection; logs errors instead of raising them; a second call does nothing |
| `map_error(exc)` | returns the adorm exception for an exception the target raised; defaults to `wrap_driver_error(exc)` |
| `is_connection_lost(exc)` | whether a mapped adorm exception leaves the connection unusable; defaults to [`is_connection_error(exc)`](#adorm-drivers-is-connection-error-exc). A driver for a target that reports statement errors as `OperationalError` without a SQLSTATE overrides it |

A failed check raises `NotSupportedError` before the subclass is called. What a subclass implements:

| Method | Required | Does |
|---|---|---|
| `async _execute(operation)` | yes | executes a supported operation |
| `begin()`, `commit()`, `rollback()`, `cancel()`, `is_usable()`, `close()` | yes | as above |
| `async _savepoint(name)`, `async _release_savepoint(name)`, `async _rollback_to_savepoint(name)` | no | savepoints; raise `NotSupportedError` unless overridden |
| `async _set_options(options, local)` | no | sets supported options from a dict; raises `NotSupportedError` for any option unless overridden |

Cancelling the task that awaits `execute()` cancels the running statement; the call raises
`asyncio.CancelledError`, the connection stays usable, and the slot rolls back an open transaction.

```python
task = asyncio.create_task(connection.execute(operation))
...
task.cancel()                 # the statement stops on the server
await connection.rollback()   # when a transaction was open
```

## Threaded drivers

### `adorm.drivers.ThreadedDriver`

Base class of drivers for a synchronous target. `mode` is `"threaded"`. Subclasses set `name`, `capabilities`
and `connection_class`, a [`ThreadedConnection`](#adorm-drivers-threadedconnection) subclass. `cancel_grace`
and `direct` can be set on the class or on an instance.

| Attribute | Default | Meaning |
|---|---|---|
| `connection_class` | none | the connection class `open_connection()` instantiates |
| `cancel_grace` | `5.0` | seconds a cancelled call may take to finish before its connection is given up |
| `direct` | `False` | `True` runs every `*_sync` method on the event loop's thread instead of a dedicated thread; each call blocks the loop while it runs |

`open_connection(profile, emit)` creates `connection_class(driver, profile, emit)` and awaits its `start()`.

```python
class SQLiteDriver(ThreadedDriver):
    name = "sqlite"
    connection_class = SQLiteConnection
    capabilities = Capabilities(operations={"raw"})
```

### `adorm.drivers.ThreadedConnection`

`ThreadedConnection(driver, profile, emit)`

A [`DriverConnection`](#adorm-drivers-driverconnection) whose work runs on one thread: a dedicated thread named
`adorm-<driver name>-<n>`, or the event loop's thread when the driver is `direct`. Subclasses implement
synchronous methods; the async contract methods run them on that thread and await their results. Created inside a
running event loop; `driver` and `profile` are available as attributes, the thread as `thread`.

Methods a subclass implements, all run on the connection's thread in a copy of the awaiting task's context (the
caller's context variables are visible; changes made inside the call do not flow back):

| Method | Required | Does |
|---|---|---|
| `connect_sync()` | yes | opens the target's connection for `self.profile`, with `self.profile.connect(self.profile)` when it is set |
| `execute_sync(operation)` | yes | executes the operation, returns plain data |
| `begin_sync()`, `commit_sync()`, `rollback_sync()` | yes | transaction control |
| `close_sync()` | yes | closes the target's connection; also runs after a failed `connect_sync()` |
| `savepoint_sync(name)`, `release_savepoint_sync(name)`, `rollback_to_savepoint_sync(name)` | no | savepoints; raise `NotSupportedError` unless overridden |
| `set_options_sync(options, local)` | no | connection options; raises `NotSupportedError` for any option unless overridden |

`cancel_sync()` is the one method that runs on another thread, while a call is running. It asks the target to
cancel the running statement and returns once the request is delivered; it never waits for the call. The
connection's thread starts no further call until `cancel_sync()` returns, so the request cannot reach a later
statement. It may run while `connect_sync()` runs, before the target's connection exists. It does nothing
unless overridden.

What the connection does around those methods:

- The capability checks of `DriverConnection` run before anything is queued.
- An exception from a `*_sync` method is converted with `map_error()` on the thread. On a dedicated thread a
  `KeyboardInterrupt` or `SystemExit` becomes `InterfaceError`; on a direct connection it propagates. An error for
  which `is_connection_lost()` is true makes `is_usable()` return `False`.
- Cancelling the awaiting task: a call that has not started never runs; for a running call `cancel_sync()` runs
  on a short-lived thread of its own, and the task waits for the call to finish, then raises
  `asyncio.CancelledError`. When the call does not finish within `cancel_grace`, the connection becomes unusable
  and further calls raise `InterfaceError`. Cancelling `open_connection()` during `connect_sync()` works the same
  way, then runs `close_sync()` and stops the thread.
- `cancel()` runs `cancel_sync()` for the running call: on a thread of its own when called on the event loop's
  thread, otherwise on the calling thread. It does nothing when no call is running.
- `close()` runs `close_sync()` on the thread and stops the thread; a call after it raises `InterfaceError`.
- On a direct connection no running event loop is visible during a `*_sync` call, so a check against use from an
  event loop (Django's `async_unsafe`) accepts it. A running direct call is cancelled only through `cancel()` from
  another thread.

#### `emit(event)`

Reports an event to the slot. Callable from any thread, including the connection's thread; the event is
delivered on the event loop, before the result of the call that emitted it.

#### `async start()`

Starts the thread (unless the driver is `direct`) and runs `connect_sync()` on it. On failure it runs
`close_sync()`, stops the thread and raises the mapped error. `ThreadedDriver.open_connection()` calls it.

```python
class SQLiteConnection(ThreadedConnection):
    def connect_sync(self):
        self.db = sqlite3.connect(self.profile.params["path"], isolation_level=None)
        self.db.row_factory = sqlite3.Row

    def execute_sync(self, operation):
        cursor = self.db.execute(operation.sql, operation.params)
        return cursor.rowcount if cursor.description is None else [dict(row) for row in cursor.fetchall()]

    def cancel_sync(self):
        self.db.interrupt()

    ...
```
