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
comes from it instead of the driver's default. The connection reports events by calling `emit` with a
[`CallbackEvent` or a `BackendEvent`](/ref/events). A failure raises an adorm exception.

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
A [pool](/ref/pool#adorm-executor-pool-driver-profile-max-connections-0-acquire-timeout-30-0-commit-timeout-60-0-release-timeout-30-0) with `max_connections=0` then stops opening connections and queues the
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
[`ConnectionProfile`](/ref/settings#adorm-connectionprofile) satisfies it.

| Member | Meaning |
|---|---|
| `name` | the `using` value |
| `alias` | the target's database alias, e.g. a key of Django's `DATABASES` |
| `params` | a mapping of driver parameters |
| `connect` | `None`, or a callable that opens the target's connection in place of the driver's default: `connect(profile)` returns the target's connection. A threaded driver calls it in `connect_sync()` on the connection's thread; an async driver awaits its result in `open_connection()` |
| `on_acquire`, `on_release` | optional; when present and not `None`, [hooks](/topics/profiles#hooks) the pool calls as `hook(connection, profile, session)` with the connection's [`target`](#adorm-drivers-driverconnection) |

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
| `async execute(operation)` | checks `operation` against `driver.capabilities` and a [`Call`](./operations#adorm-operations-call-query-method-target-queryset-args-kwargs-manager-none) with [`find_driver_method()`](./models#adorm-models-find-driver-method-label-manager-target-method), then awaits `_execute(operation)`; returns rows as dicts keyed by adorm field names, a scalar or a count; field path segments, value keys and row keys are adorm names, translated to and from the target's with the reached model's `orm_names` ([Drivers](/topics/drivers#operations-in-plain-data-out)) |
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
| `target` | the target's connection that the profile's `on_acquire` and `on_release` hooks receive; defaults to the driver connection itself |
| `async call_hook(hook, *args)` | calls `hook(self.target, *args)`, awaits the result when it is awaitable and returns it; an exception the hook raises is mapped with `map_error()` |
| `map_error(exc)` | returns the adorm exception for an exception the target raised; defaults to `wrap_driver_error(exc)` |
| `is_connection_lost(exc)` | whether a mapped adorm exception leaves the connection unusable; defaults to [`is_connection_error(exc)`](#adorm-drivers-is-connection-error-exc). A driver for a target that reports statement errors as `OperationalError` without a SQLSTATE overrides it |

A failed check raises `NotSupportedError` before the subclass is called: an operation kind or construct the
capabilities do not list, and a `Call` of a method the registered model does not declare. What a subclass
implements:

| Method | Required | Does |
|---|---|---|
| `async _execute(operation)` | yes | executes a supported operation |
| `begin()`, `commit()`, `rollback()`, `cancel()`, `is_usable()`, `close()` | yes | as above |
| `async _savepoint(name)`, `async _release_savepoint(name)`, `async _rollback_to_savepoint(name)` | no | savepoints; raise `NotSupportedError` unless overridden |
| `async _set_options(options, local)` | no | sets supported options from a dict; raises `NotSupportedError` for any option unless overridden |

#### Managers and driver methods

A driver that supports the construct `clause.manager` starts every query that carries a
[`manager`](./query#adorm-query-query-model-manager-none) from that manager of the target ORM, and inserts a
`Create` with a `manager` through that manager's `create()`, passing every value; a query without one reads the
model's rows without manager logic. A driver that supports the operation `call` runs `Call` on the target ORM's
manager `Call.manager`: on the manager itself (`target == "manager"`) or on the query set of `query` started from
that manager (`"queryset"`), with `args` and `kwargs`. It returns every instance of a target model in the result,
also inside lists, tuples and the values of mappings, as a
[`ModelRow`](./operations#adorm-operations-modelrow-model-row) of adorm field names. For Django:

```python
def execute_call(call):
    model = apps.get_model(call.model)
    manager = getattr(model, call.manager)
    target = manager if call.target == "manager" else build_queryset(manager.all(), call.query)
    result = getattr(target, call.method)(*call.args, **dict(call.kwargs))
    return to_plain(result)          # Django instances become ModelRow(label, row)
```

A driver that cannot run the target ORM's code, such as a native SQL driver, lists neither, so both raise
`NotSupportedError`.

#### Cancellation

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
- `call_hook(hook, *args)` runs `hook(self.target, *args)` on the thread like a `*_sync` method; a hook that returns
  an awaitable raises `InterfaceError`. A subclass sets `target` to what profile hooks receive, such as Django's
  connection wrapper.
- On a direct connection no running event loop is visible during a `*_sync` call, so a check against use from an
  event loop (Django's `async_unsafe`) accepts it. A running direct call is cancelled only through `cancel()` from
  another thread.

#### `emit(event)`

Reports an event to the slot. Callable from any thread, including the connection's thread; the event is
delivered on the event loop, before the result of the call that emitted it. The slot accepts only `CallbackEvent` and
`BackendEvent`; it drops and logs any other event, and a `CallbackEvent` named `on_commit` or `on_rollback`
([`RESERVED_CALLBACKS`](/ref/events#adorm-events-reserved-callbacks)).

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

## SQL statements

`adorm.drivers.sql` holds what a driver that executes SQL itself needs regardless of where the SQL comes from. It
imports no ORM or database library.

```python
from adorm.drivers.sql import numbered_placeholders, Statement
```

### `adorm.drivers.sql.Statement`

`Statement(sql, params=())`

A frozen dataclass: an SQL statement with numbered placeholders (`$1`, `$2` …) and its positional parameters, in
the form `asyncpg.Connection.fetch()` takes them.

```python
statement = Statement('SELECT "name" FROM "clubs_club" WHERE "id" = $1::int8', (42,))
rows = await conn.fetch(statement.sql, *statement.params)
```

### `adorm.drivers.sql.numbered_placeholders(sql, casts=None)`

Returns `sql` with every `%s` replaced by `$1`, `$2` … in order and every `%%` by `%`: the PEP 249 `format`
paramstyle Django's compiler and psycopg use, turned into PostgreSQL's numbered placeholders. `casts`, when given,
holds one entry per placeholder: a PostgreSQL type name appends `::<type>`, `None` leaves the placeholder bare.

`%` is interpreted everywhere in `sql`, inside quoted literals and identifiers too, as psycopg interprets it. Any
other `%` sequence (`%d`, `%(name)s`, a lone `%`) raises `ValueError`, and so does a `casts` sequence whose length
differs from the number of placeholders.

```python
numbered_placeholders('SELECT "a" FROM "t" WHERE "b" = %s AND "c" LIKE %s')
# 'SELECT "a" FROM "t" WHERE "b" = $1 AND "c" LIKE $2'
numbered_placeholders("REPLACE(%s, E'%%', E'\\\\%%')")
# "REPLACE($1, E'%', E'\\\\%')"
numbered_placeholders("SELECT %s, %s", ["int4", None])
# 'SELECT $1::int4, $2'
```

## Django's compiler for asyncpg

`adorm.drivers.asyncpg.compiler` compiles Django queries into SQL for asyncpg, without a database connection. It
imports Django, psycopg 3 (Django's PostgreSQL backend must run on it) and asyncpg, which the `asyncpg` extra
installs (`pip install "adorm[asyncpg]"`). [SQL compilation for asyncpg](/topics/sql-compilation) describes the
behaviour it relies on.

```python
from adorm.drivers.asyncpg.compiler import bind_param, DjangoCompiler, DjangoStatement, MAX_QUERY_PARAMS
```

### `adorm.drivers.asyncpg.compiler.DjangoCompiler(alias="default", *, server_version)`

Compiles Django queries for the `DATABASES` entry `alias` into [`DjangoStatement`](#adorm-drivers-asyncpg-compiler-djangostatement)
objects. It compiles on a database wrapper of its own (`connection`), built from that entry and never connected,
so Django's thread-local connection for `alias` is not touched and compiling works in any thread and inside a
running event loop. One instance can be shared by threads and tasks.

`server_version` is PostgreSQL's `server_version_num` (`170006` for 17.6). Django reads it to choose the SQL for
`AnyValue`, `JSONObject`, `JSONArray` and `UUID7`; a version that lacks a construct raises Django's
`NotSupportedError` for it, as Django does. Parameters are bound for the entry's `OPTIONS["server_side_binding"]`
(attribute `server_side_binding`). The wrapper's `features.max_query_params` is
[`MAX_QUERY_PARAMS`](#adorm-drivers-asyncpg-compiler-max-query-params), so
`compiler.connection.ops.bulk_batch_size(fields, objs)` returns batch sizes asyncpg accepts.

An entry whose backend is not PostgreSQL, or whose PostgreSQL backend runs on psycopg2, raises `NotSupportedError`
(adorm's).

```python
server_version = int(await conn.fetchval("SHOW server_version_num"))
compiler = DjangoCompiler("default", server_version=server_version)
```

#### `compile(query, *, in_transaction=False)`

Compiles `query`, a Django `QuerySet` or `Query` (select, update, delete or aggregate), into a `DjangoStatement`:
`%s` placeholders become `$n`, each parameter goes through [`bind_param()`](#adorm-drivers-asyncpg-compiler-bind-param-value-server-side-binding-false)
in the compiler's binding mode and its placeholder gets the cast `bind_param()` returns.

A select whose filter matches nothing (`pk__in=[]`) compiles with `WHERE 0 = 1`. An update or delete whose filter
matches nothing, and an update without values, compile into an empty statement ([`empty`](#empty)), which the caller
does not execute.

`in_transaction` tells whether the statement runs inside a transaction: a `select_for_update()` query compiles only
then and raises adorm's `TransactionManagementError` otherwise, as Django does.

Raises adorm's `NotSupportedError` for an insert, which Django compiles into one statement per batch, for an update
that sets fields of a multi-table inheritance parent, which Django runs as one statement per table after selecting
the keys on its own connection, for a statement with more than `MAX_QUERY_PARAMS` parameters, and for a construct
that needs a connection to compile: `SearchHeadline` with options quotes them through psycopg.

```python
statement = compiler.compile(Book.objects.filter(title__icontains="war", pages__gte=100).order_by("isbn"))
statement.sql      # '... WHERE ("library_book"."pages" >= $1::int4 AND UPPER("library_book"."title"::text) LIKE UPPER($2)) ...'
statement.params   # (100, '%war%')
records = await conn.fetch(statement.sql, *statement.params)
```

### `adorm.drivers.asyncpg.compiler.DjangoStatement`

A [`Statement`](#adorm-drivers-sql-statement) compiled by `DjangoCompiler`. Its `compiler` field holds Django's
compiler for the query; it is left out of comparison and `repr()`.

#### `empty`

`True` when `sql` is `""`: the query affects no rows and is not executed.

```python
statement = compiler.compile(update_query)
count = 0 if statement.empty else int((await conn.execute(statement.sql, *statement.params)).split()[-1])
```

#### `rows(records)`

Returns `records`, the asyncpg `Record` objects (or tuples) the statement fetched, as a list of tuples in select
order: the values Django's own evaluation of the query returns on psycopg. It drops the columns Django adds only
for ordering or `extra(select=)`, returns `inet` / `cidr` values as text and `uuid` values as `uuid.UUID` (in arrays
too), as psycopg loads them, and applies the fields' `from_db_value()` converters, JSON decoding included.

```python
statement = compiler.compile(Book.objects.values("isbn", "extra"))
statement.rows(await conn.fetch(statement.sql, *statement.params))
# [('9780000000001', {'tags': ['x', 'y']}), ...]
```

### `adorm.drivers.asyncpg.compiler.bind_param(value, *, server_side_binding=False)`

Returns `(asyncpg_value, cast)` for a parameter Django prepared for psycopg. `cast` is the PostgreSQL type the value
has under psycopg, so the server resolves the statement as it does there, or `None` for an untyped parameter.
`server_side_binding` selects psycopg's binding mode; the default, client-side binding, is Django's. The two modes
differ only for numbers and lists of numbers.

| Parameter | asyncpg value | Cast, client-side binding | Cast, server-side binding |
|---|---|---|---|
| `None`, `str` | unchanged | none: the type comes from the context | none |
| `int`, psycopg `Int2`, `Int4`, `Int8`, `IntNumeric`, `Oid` (Django wraps integer field values) | `int` | `int4`, `int8` or `numeric`, the type of the integer literal | `int`: the smallest of `int2`, `int4`, `int8`, `numeric`; a wrapper: `int2`, `int4`, `int8`, `numeric`, `oid` |
| finite `float`, psycopg `Float4`, `Float8` | `Decimal` of the float's `repr()` (client-side), `float` (server-side) | `numeric` | `float8`; a wrapper: `float4`, `float8` |
| infinite or NaN `float` | `float` | `float8` | `float8` |
| `bool` | unchanged | `bool` | `bool` |
| `Decimal` | unchanged | `numeric` | `numeric` |
| `datetime` | unchanged | `timestamptz` when aware, `timestamp` when naive | the same |
| `date`, `time`, `timedelta`, `UUID` | unchanged | `date`, `time` (`timetz` when aware), `interval`, `uuid` | the same |
| `bytes`, `bytearray`, `memoryview`, psycopg `Binary` | `bytes` | `bytea` | `bytea` |
| psycopg `Jsonb`, `Json` | the JSON text, from the wrapper's `dumps` | `jsonb`, `json` | the same |
| `IPv4Address` / `IPv6Address`, `IPv4Network` / `IPv6Network` | unchanged | `inet`, `cidr` | the same |
| psycopg `Range` | `asyncpg.Range` | none | none |
| `list`, `tuple` (nested too) | a list of the items' asyncpg values as server-side binding converts them | the array of the items' widest integer type (`int2[]` … `numeric[]`), else of the first non-`None` item's type (`float8[]`, `date[]`, `jsonb[]` …); none without a typed item (`str` items, only `None`, empty) | the same |

Any other psycopg object raises `NotSupportedError`; any other value passes unchanged without a cast.

```python
bind_param(Int2(100))                               # (100, 'int4')
bind_param(Int2(100), server_side_binding=True)     # (100, 'int2')
bind_param(0.1)                                     # (Decimal('0.1'), 'numeric')
bind_param([30000, 70000])                          # ([30000, 70000], 'int4[]')
bind_param(Jsonb({"a": 1}))         # ('{"a": 1}', 'jsonb')
bind_param(datetime(2024, 1, 1))    # (datetime(2024, 1, 1), 'timestamp')
bind_param("%war%")                 # ('%war%', None)
```

### `adorm.drivers.asyncpg.compiler.MAX_QUERY_PARAMS`

`32767`: the most parameters asyncpg sends with one statement; a statement with more raises
`asyncpg.InterfaceError`.
