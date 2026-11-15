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
class RawSQLDriver(Driver):
    name = "raw-sql"
    mode = "async"
    capabilities = Capabilities(operations={"raw"}, connection_options={"statement_timeout"})

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

#### `check(operation)`

Raises `NotSupportedError` when the driver does not support `operation`: its kind or a construct it uses is missing
from `capabilities` ([`Capabilities.check()`](#check-operation-driver-none) with the driver's `name`), or it is a
`Call` of a method the registered model does not declare. Does no I/O. The
[router](/ref/pool) calls it before it acquires a slot for an operation outside a transaction, so an unsupported
operation neither waits for a slot nor opens one, and
[`DriverConnection.execute()`](#adorm-drivers-driverconnection) calls it before every execution.

```python
driver = DjangoDriver()
driver.check(Select(Member.objects.filter(first_name="Ana").query))   # supported: returns None
driver.check(Select(Member.objects.annotate(n=Length("first_name")).query))
# NotSupportedError: 'select' operation needs unsupported function.Length (driver 'django')
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

#### `check(operation, driver=None)`

Raises `NotSupportedError` naming every unsupported name, or returns `None`. `driver` is the driver's name: the
message ends with it, and the error's [`driver`](/ref/exceptions#adorm-notsupportederror) is set to it; the error's
`unsupported` holds the names `unsupported(operation)` returns.

#### `check_savepoints(driver=None)`

Raises `NotSupportedError` (`unsupported` `("savepoints",)`) when `savepoints` is `False`.

#### `check_options(options, driver=None)`

Raises `NotSupportedError` naming every option in `options` (any iterable of names, e.g. a dict) the driver does
not support; `unsupported` holds those names, sorted.

```python
caps = Capabilities(operations={"select"}, constructs={"lookup.exact"}, connection_options={"statement_timeout"})

caps.unsupported(operation)        # ["lookup.icontains"] for a select with an icontains lookup
caps.check(operation, "raw-sql")   # NotSupportedError: 'select' operation needs unsupported lookup.icontains (driver 'raw-sql')
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
| `options` | optional; when present and not `None`, the profile's database [connection options](/ref/options), which the pool reads |

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
| `async set_options(options, local)` | checks the option names against `capabilities.connection_options`, then sets them from a mapping; `local=True` limits them to the open transaction (`SET LOCAL`); a value of `None` resets the option to its default (`RESET`, or `SET LOCAL … TO DEFAULT`). The slot calls it with the options that changed ([`Slot`](/ref/pool#adorm-executor-slot)) |
| `cancel()` | cancels the running statement, if any; callable from any thread, returns without waiting; the call raises `OperationalError` with SQLSTATE `57014` |
| `is_usable()` | whether the connection can serve further calls; does no I/O |
| `async close()` | closes the connection; logs errors instead of raising them; a second call does nothing |
| `target` | the target's connection that the profile's `on_acquire` and `on_release` hooks receive; defaults to the driver connection itself |
| `async call_hook(hook, *args)` | calls `hook(self.target, *args)`, awaits the result when it is awaitable and returns it; an exception the hook raises is mapped with `map_error()` |
| `map_error(exc)` | returns the adorm exception for an exception the target raised; defaults to `wrap_driver_error(exc)` |
| `is_connection_lost(exc)` | whether a mapped adorm exception leaves the connection unusable; defaults to [`is_connection_error(exc)`](#adorm-drivers-is-connection-error-exc). A driver for a target that reports statement errors as `OperationalError` without a SQLSTATE overrides it |

A failed check raises `NotSupportedError` before the subclass is called: `execute()` runs
[`Driver.check()`](#check-operation), and the savepoint calls and `set_options()` check `savepoints` and
`connection_options`, each error naming the driver. What a subclass
implements:

| Method | Required | Does |
|---|---|---|
| `async _execute(operation)` | yes | executes a supported operation |
| `begin()`, `commit()`, `rollback()`, `cancel()`, `is_usable()`, `close()` | yes | as above |
| `async _savepoint(name)`, `async _release_savepoint(name)`, `async _rollback_to_savepoint(name)` | no | savepoints; raise `NotSupportedError` unless overridden |
| `async _set_options(options, local)` | no | sets supported options from a dict, `None` values to their default; raises `NotSupportedError` for any option unless overridden |

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

What a cancelled `execute()`, `commit()` or other request method does decides the
[`CancellationOutcome`](/ref/pool#adorm-cancellationoutcome) of the caller's `CancelledError` and the
[`TransactionOutcome`](/ref/transactions#adorm-transactionoutcome) of a cancelled commit:

| The method | Outcome |
|---|---|
| returns its result | `SUCCEEDED`; a `commit()` is `COMMITTED` |
| raises an error | `FAILED`; `UNKNOWN` for a connection error ([`is_connection_error()`](#adorm-drivers-is-connection-error-exc)); a `commit()` is `ROLLED_BACK`, or `UNKNOWN` for a connection error |
| raises `asyncio.CancelledError` | `UNKNOWN` |

A method whose statement finished before the cancellation reached it returns that result or raises that error; it
raises `CancelledError` only when the statement's effect is unknown, such as a statement the target cancelled.

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
| `set_options_sync(options, local)` | no | connection options, `None` values to their default; raises `NotSupportedError` for any option unless overridden |
| `prepare_call_sync()` | no | runs before every other `*_sync` method and every hook, in the same context copy, to prepare that context; an exception it raises is the call's; does nothing unless overridden |

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
  on a short-lived thread of its own, and the task waits for the call to finish. A request method (`execute()`,
  `begin()`, `commit()`, `rollback()`, the savepoint methods, `set_options()`) then returns the call's result or
  raises its error, and raises `asyncio.CancelledError` when the call failed with SQLSTATE `57014`; `start()` and
  `call_hook()` always raise `asyncio.CancelledError`. When the call does not finish within `cancel_grace`, the
  connection becomes unusable, the task raises `asyncio.CancelledError`, and further calls raise `InterfaceError`. Cancelling `open_connection()` during `connect_sync()` works the same
  way, then runs `close_sync()` and stops the thread.
- `cancel()` runs `cancel_sync()` for the running call: on a thread of its own when called on the event loop's
  thread, otherwise on the calling thread. It does nothing when no call is running.
- `close()` runs `close_sync()` on the thread and stops the thread; a call after it raises `InterfaceError`.
- `after_call_sync(fn)`, called from a `*_sync` method, runs `fn()` on the thread once that call has ended and its
  outcome is delivered, before the next call starts, in the call's context. `cancel()` never reaches it, and an
  exception it raises is logged on the `adorm.drivers` logger. Work that must not decide or delay the call's outcome
  goes there, such as Django's `on_commit()` callbacks after `commit_sync()`.
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

## Django driver

`adorm.drivers.django` executes adorm reads and writes with Django's ORM; `adorm.drivers.django.translation` turns
adorm queries, values and lookups into Django query sets and keyword arguments. Importing either imports Django; operations need Django set up (`django.setup()`).
The [Django driver topic](/topics/django-driver) shows them in use.

```python
from adorm.drivers.django import DjangoDriver
from adorm.drivers.django.translation import translate
```

### `adorm.drivers.django.DjangoDriver`

A [`ThreadedDriver`](#adorm-drivers-threadeddriver): `name` `"django"`, `mode` `"threaded"`, `connection_class`
[`DjangoConnection`](#adorm-drivers-django-djangoconnection), `capabilities`
[`CAPABILITIES`](#adorm-drivers-django-capabilities). Created without arguments; a profile names it by class,
instance or import path.

```python
adorm.configure(profiles={"default": {"driver": "adorm.drivers.django.DjangoDriver", "alias": "default"}})
driver = DjangoDriver()
pool = Pool(driver, ConnectionProfile("default", driver), max_connections=10)
```

### `adorm.drivers.django.DjangoConnection`

The [`ThreadedConnection`](#adorm-drivers-threadedconnection) of `DjangoDriver`.

| Method | Does |
|---|---|
| `connect_sync()` | takes `django.db.connections[profile.alias]` of the connection's thread (a backend other than PostgreSQL raises `NotSupportedError`); `profile.params` replace keys of that wrapper's `settings_dict` (the `DATABASES` entry itself is unchanged); `profile.connect(profile)`, when set, returns the DB-API connection that Django's `get_new_connection()` uses in place of the one it opens, and Django configures it (isolation level from `OPTIONS`, time zone, role); a psycopg 3 connection is opened with the wrapper's `get_connection_params()`, which carry Django's cursor class and type adapters; with Django's `OPTIONS["pool"]` it raises `NotSupportedError`; then `ensure_connection()` |
| `execute_sync(operation)` | a read [`translate()`](#adorm-drivers-django-translation-translate-query)s the operation's query and evaluates it on `profile.alias`: `select` → list of rows, `get` → the row (Django's `get()`), `first` / `last` → the row or `None`, `count` → `int`, `exists` → `bool`; a write runs as the table below shows |
| `begin_sync()` | enters an outermost `transaction.atomic(using=profile.alias)` block; a second `begin`, or a wrapper already inside an atomic block (a `direct` driver's thread inside Django code's `atomic()`), raises `TransactionManagementError` |
| `savepoint_sync(name)` | creates a Django savepoint (`savepoint()`) and adds it to the block's savepoint ids, so Django's `on_commit()` callbacks registered inside it belong to it; `name` already open, or no transaction, raises `TransactionManagementError` |
| `release_savepoint_sync(name)` | releases the savepoint and the savepoints opened after it |
| `rollback_to_savepoint_sync(name)` | rolls back to the savepoint, which stays open, discarding the savepoints opened after it and the `on_commit()` callbacks registered since; clears Django's `needs_rollback` first, since Django refuses every statement of a transaction marked for rollback |
| `commit_sync()` | leaves the block, which commits, and hands Django's `on_commit()` callbacks to `after_call_sync()` (see below); raises `TransactionManagementError` and rolls back when Django marked the transaction for rollback or the database transaction failed, `InterfaceError` when the connection was closed inside it |
| `rollback_sync()` | leaves the block with a rollback; nothing without an open transaction |
| `set_options_sync(options, local)` | [`set_options_sql(options, local)`](#adorm-drivers-sql-set-options-sql-options-local) on a cursor of the wrapper, one `execute()` without parameters; nothing for no options |
| `cancel_sync()` | `cancel_safe()` on the psycopg connection under the wrapper (`cancel()` without it); nothing before the connection exists |
| `close_sync()` | the wrapper's `close()`; inside a transaction it also leaves the atomic block, so the wrapper keeps no transaction state for the next connection of its thread (a `direct` driver's) |
| `prepare_call_sync()` | [`adopt_context_locals()`](#adorm-drivers-django-adopt-context-locals) |
| `map_error(exc)` | [`map_django_error(exc)`](#adorm-drivers-django-map-django-error-exc) |

| Write | Django call | Result |
|---|---|---|
| `create` | `create(**values)` of the manager `manager` names (`_base_manager` without one), through `db_manager(profile.alias)` | [`instance_row()`](#adorm-drivers-django-translation-instance-row-instance) of the new instance |
| `save` without `pk`, or with `force_insert` | `Model(**values)`, the `pk` set when given, `save(force_insert=...)` | the instance's row |
| `save` with `pk` | `Model.from_db()` with the given fields loaded and the others deferred, then `save(update_fields=..., force_update=...)`; Django updates a partly loaded instance's loaded fields only. Without `update_fields` and `force_update`, a partly loaded instance whose row is missing is inserted (`force_insert`), the target's defaults filling the other fields; the check is one `exists()` query | the instance's row, without fields that hold an expression |
| `update` | `update(**values)` on [`model_queryset()`](#adorm-drivers-django-translation-model-queryset-query) | the number of rows |
| `delete` | `delete()` on `model_queryset()`: Django's collector cascades, sets NULL, and refuses `PROTECT` and `RESTRICT` references; the objects of a refusal are read again with every field; no model's `delete()` runs | `(total, {label: count})` |
| `delete_instance` | the instance read with `_base_manager.get(pk=pk)`, then its `delete(using=...)`, so the model's `delete()` override runs; refusals as for `delete` | Django's `(total, {label: count})`; `(0, {})` for a missing row or an override that returns something else |
| `get_or_create` | `get_or_create(defaults=..., **lookups)` on `model_queryset()` | `(row, created)` |
| `update_or_create` | `update_or_create(defaults=..., create_defaults=..., **lookups)` on `model_queryset()` | `(row, created)` |

Values go through [`field_kwargs()`](#adorm-drivers-django-translation-field-kwargs-query-values) and lookups through
[`lookup_kwargs()`](#adorm-drivers-django-translation-lookup-kwargs-query-lookups). Every call runs on
`profile.alias` (`using()`, `save(using=...)`). Django's model methods (`save()` and `delete()` overrides), its signals and its own
atomic blocks run on the connection's thread as they run in Django code; the signals are not reported to the client.

Django's `on_commit()` callbacks registered during a transaction (by a `save()` override, a signal receiver) run
after the commit, on the connection's thread, in registration order, once `commit()` has returned and before the
connection's next call ([`after_call_sync()`](#adorm-drivers-threadedconnection)): cancelling the task that awaits
the commit reaches neither the commit nor them, and the slot's `CallbackEvent("on_commit")` does not wait for them. A callback that raises is logged on the `adorm.drivers.django` logger,
since the commit stands; one registered without `robust=True` stops the callbacks after it, as in Django. Callbacks
of a rolled-back savepoint or transaction do not run. Outside a transaction Django runs a callback at once, inside
the operation that registers it.

`target`, what profile hooks receive, is the wrapper (`self.db`). `alias` is `profile.alias`.

### `adorm.drivers.django.CAPABILITIES`

The [`Capabilities`](#adorm-drivers-capabilities) of `DjangoDriver`:

| Field | Value |
|---|---|
| `operations` | `select`, `get`, `first`, `last`, `count`, `exists`, `create`, `save`, `update`, `delete`, `delete_instance`, `get_or_create`, `update_or_create` |
| `constructs` | `lookup.<name>` for `exact`, `iexact`, `gt`, `gte`, `lt`, `lte`, `in`, `range`, `isnull`, `contains`, `icontains`, `startswith`, `istartswith`, `endswith`, `iendswith`, `regex`, `iregex`; `path.nested`; `clause.annotate`, `clause.distinct`, `clause.distinct_on`, `clause.only`, `clause.defer`, `clause.select_related`, `clause.manager`, `clause.select_for_update` with `.nowait`, `.skip_locked`, `.of`, `.no_key`; `function.Lower`, `function.Upper`; `expression.Subquery`, `expression.CombinedExpression` |
| `savepoints`, `cancellation` | `True` |
| `connection_options` | [`POSTGRESQL_OPTIONS`](#adorm-drivers-sql-postgresql-options): `statement_timeout`, `lock_timeout`, `idle_in_transaction_session_timeout` |
| `backend_events` | none |

The constructs are those Django's PostgreSQL backend supports (`clause.distinct_on`, `contains` on array and JSON
fields); `DjangoConnection` refuses other database backends.

### `adorm.drivers.django.READ_CAPABILITIES`

The reads of [`CAPABILITIES`](#adorm-drivers-django-capabilities): `operations` `select`, `get`, `first`, `last`,
`count`, `exists`, the `constructs` without `clause.select_for_update[...]`, and `cancellation`; no savepoints.

### `adorm.drivers.django.model_row(instance)`

Returns a Django model instance as a [`ModelRow`](./operations#adorm-operations-modelrow-model-row): the model's label
and [`instance_row(instance)`](#adorm-drivers-django-translation-instance-row-instance).

```python
model_row(Author.objects.only("name").get(slug="ann"))
# ModelRow(model='library.Author', row={'id': 1, 'name': 'Ann'})
```

### `adorm.drivers.django.map_django_error(exc)`

Returns the adorm exception for `exc`, raised by Django or psycopg, with `exc` as `__cause__`:

| `exc` | Result |
|---|---|
| `django.core.exceptions.ObjectDoesNotExist` (a model's `DoesNotExist`) | `ObjectDoesNotExist` with Django's message |
| `django.core.exceptions.MultipleObjectsReturned` | `MultipleObjectsReturned` with Django's message |
| `FieldError`, `FieldDoesNotExist` | `ProgrammingError` (`original` is `exc`) |
| Django's `ValidationError` | `ValidationError` with the same messages (by field, a list, or one message with `code` and `params`) |
| `django.db.transaction.TransactionManagementError` | `TransactionManagementError` |
| `SynchronousOnlyOperation` | `InterfaceError` |
| `django.db.models.ProtectedError`, `RestrictedError` | `ProtectedError`, `RestrictedError` with Django's message and the objects as [`model_row()`](#adorm-drivers-django-model-row-instance) values (`protected_objects`, `restricted_objects`) |
| anything else | [`wrap_driver_error(exc)`](/ref/exceptions#adorm-exceptions-wrap-driver-error-exc): the SQLSTATE of the psycopg error a `django.db` error carries, then the PEP 249 class name, else `DatabaseError` |

```python
map_django_error(Author.DoesNotExist("Author matching query does not exist."))
# ObjectDoesNotExist('Author matching query does not exist.')
```

### `adorm.drivers.django.adopt_context_locals()`

Re-binds the values of asgiref `Local` objects in the current context (Django's active language and time zone) to
the current thread. asgiref 3.13 and later tag such a value with the thread that set it, so in a context copied
from an event loop task and run on another thread it reads as unset; after the call it reads as in the task. A
value without that tag (earlier asgiref versions, which need no re-binding) and other context variables are
unchanged. The `django` and `asyncpg` extras require asgiref 3.x. `DjangoConnection.prepare_call_sync()` calls it before every call.

```python
with translation.override("sl"):
    context = contextvars.copy_context()

def read():
    adopt_context_locals()
    return translation.get_language()

threading.Thread(target=context.run, args=(read,)).start()     # read() returns "sl"
```

### `adorm.drivers.django.translation.translate(query)`

Returns the [`TranslatedQuery`](#adorm-drivers-django-translation-translatedquery) of `query`, a
[`Query`](/ref/query). Builds Django objects only; opens no database connection.

- The query set starts from the Django manager named by `Query.manager`, or from the model's `_base_manager` without
  one. A manager the model lacks raises `ProgrammingError`.
- Annotations, conditions (a lookup on an expression through an `alias()`), ordering (the model's `Meta.ordering`
  when the query has none), `distinct()` with or without fields, the slice and `none()` follow as in Django.
- Each of `Query.filters` is one Django `filter()` call, so a multi-valued relation is joined once per filter, as
  in Django.
- Field paths go through [`orm_path()`](#adorm-drivers-django-translation-orm-path-model-path-annotations); a model
  instance as a value stands for its primary key, or, compared with a foreign key whose `to_field` is another field,
  for that field's value; a `Value` with `output_type` gets
  [`output_field(output_type)`](#adorm-drivers-django-translation-output-field-output-type); a query in an `in`
  lookup becomes a Django query set ([`subquery()`](#adorm-drivers-django-translation-subquery-query)).
- Arithmetic (`CombinedExpression`) becomes Django's `CombinedExpression` with the same connector (`%` as Django's
  `MOD`), its `output_type` as the output field.
- `select_for_update()` becomes Django's `select_for_update()` with the same options; the paths in `of` are
  translated, `"self"` stays. Django evaluates it only inside a transaction.
- A node without a translation (`OuterRef`, `Exists`, aggregates, functions not in
  [`FUNCTIONS`](#adorm-drivers-django-translation-functions)) raises `NotSupportedError`;
  a `select_related()` path that is not a relation of the adorm model, or not a foreign key or one-to-one relation,
  raises `ProgrammingError`. Unknown fields raise Django's `FieldError` when the query set is built.

```python
translated = translate(Query("library.Author").select_related("mentor").only("name", "mentor__name"))
translated.columns          # ('id', 'name', 'mentor__id', 'mentor__name')
translated.rows(translated.queryset)
# [{'id': 1, 'name': 'Ann', 'mentor': None}, {'id': 2, 'name': 'Bob', 'mentor': {'id': 1, 'name': 'Ann'}}, ...]
```

### `adorm.drivers.django.translation.TranslatedQuery`

A query in Django's terms.

| Member | Meaning |
|---|---|
| `queryset` | the Django query set of the rows, ending in `values_list(*columns)`; its `get()`, `first()`, `last()`, `count()` and `exists()` evaluate the query |
| `columns` | the Django query paths it selects, each once |
| `row(values)` | the adorm row of one tuple of `queryset` |
| `rows(records)` | the adorm rows of tuples of `queryset` |

A model row has the adorm model's fields that `only()` and `defer()` leave loaded (the primary key always), keyed by
adorm name, then the annotations; each relation `select_related()` follows adds the related row under the relation's
name, or `None` when the related primary key is NULL. `select_related()` without paths follows the non-null foreign
keys up to [`SELECT_RELATED_DEPTH`](#adorm-drivers-django-translation-select-related-depth) relations deep. A
`values()` / `values_list()` row is keyed by the selected names, as written; without names by the model's fields
and the annotations. A model without a registered adorm model has its Django fields by attname.

### `adorm.drivers.django.translation.orm_path(model, path, annotations=())`

Returns Django's query path for the adorm field path `path` on the Django model `model`. Each segment is renamed
with the `orm_names` of the adorm model registered for the Django model that segment is on; a reverse relation's
accessor name becomes its query name. A path whose first segment is in `annotations` is returned unchanged; a
segment Django does not know is kept, so that Django reports it.

```python
orm_path(Note, "copy___title")          # "copy__title"
orm_path(Book, "tagging_set__weight")   # "tagging__weight"
orm_path(Placement, "order_")           # "_order"
```

### `adorm.drivers.django.translation.model_queryset(query)`

Returns the Django query set of the model rows of `query`, built as `translate()` builds it (manager, annotations,
filters, ordering, distinct, slice, `none()`, `select_for_update()`) without selecting columns. The Django driver runs
`update()`, `delete()`, `get_or_create()` and `update_or_create()` on it, so a manager's `get_queryset()` and the
overrides of its query set apply.

```python
model_queryset(Query("library.Loan", manager="objects").filter(due__lt=today)).delete()
# Django: Loan.objects.all().filter(due__lt=today).delete()
```

### `adorm.drivers.django.translation.field_kwargs(query, values)`

Returns the keyword arguments Django takes for the adorm field values `values` (a mapping or `(name, value)` pairs)
of the model of `query`, a `Query` or a model label. Each name is translated with the model's `orm_names`; an
expression is translated as in `query` (`F`, arithmetic, functions); a model instance stands for the value the field
refers to, as in a lookup.

```python
field_kwargs("library.Note", {"json_": "{}", "copy_id": book})     # {"json": "{}", "copy_id": book.pk}
field_kwargs("library.Author", {"rating": F("rating") + 1})       # {"rating": CombinedExpression(F(rating) + Value(1))}
```

### `adorm.drivers.django.translation.lookup_kwargs(query, lookups)`

Returns the keyword arguments of Django's `get_or_create()` and `update_or_create()` for the adorm `lookups`
(`(key, value)` pairs) on the model of `query`. Django creates the row with the arguments without `__`, so a key
without `__` and without a lookup name stays a field name: renamed by `orm_names`, and a foreign key named by its
relation (`mentor`) becomes its attname (`mentor_id`) with the key of a model instance as value. A key with a lookup
name or across relations is translated as in `filter()` and only filters.

```python
lookup_kwargs(Query("library.Author"), [("mentor", ann), ("name__iexact", "bob")])
# {"mentor_id": ann.pk, "name__iexact": "bob"}
```

### `adorm.drivers.django.translation.instance_row(instance)`

Returns the adorm row of a Django model instance: the fields of the registered adorm model by adorm name (a model
without one: its concrete fields by attname), with the instance's values. A field the instance has not loaded and a
field holding an expression (its value is known only to the database) are left out; no query runs.

```python
instance_row(Note.objects.only("json").get(pk=code))      # {"code": code, "json_": "{}"}
```

### `adorm.drivers.django.translation.django_model(label)`

Returns the installed Django model with the label `label` (`"app_label.ModelName"`), auto-created many-to-many
through models included (`"library.Book_authors"`). Raises `ProgrammingError` when there is none.

### `adorm.drivers.django.translation.subquery(query)`

Returns the Django query set of `query` used inside another query: its model rows (Django compares their primary
key), or, after `values()` or `values_list()` with fields, those columns.

### `adorm.drivers.django.translation.output_field(output_type)`

Returns a new Django field instance for an adorm `output_type`: `bool` → `BooleanField`, `int` → `IntegerField`,
`float` → `FloatField`, `str` → `TextField`, `Decimal` → `DecimalField`, `datetime` → `DateTimeField`, `date` →
`DateField`, `time` → `TimeField`, `timedelta` → `DurationField`, `UUID` → `UUIDField`, `bytes` → `BinaryField`,
`dict` and `list` → `JSONField`. Another type (`tuple`, `set`, `frozenset`) raises `NotSupportedError`.

```python
Query("library.Author").annotate(rank=Value(None, output_type=int)).values("name", "rank")
# Django: annotate(rank=Value(None, output_field=IntegerField()))
```

### `adorm.drivers.django.translation.FUNCTIONS`

A read-only mapping from adorm function class names to Django function classes: `"Lower"` → `Lower`, `"Upper"` →
`Upper`.

### `adorm.drivers.django.translation.SELECT_RELATED_DEPTH`

`5`: how many relations deep `select_related()` without paths follows non-null foreign keys, as Django's
`Query.max_depth`.

## Native PostgreSQL driver

`adorm.drivers.asyncpg` executes adorm operations on asyncpg, natively async. Importing it imports asyncpg, Django
and psycopg 3 (the `asyncpg` extra, `pip install "adorm[asyncpg]"`); connections need Django set up
(`django.setup()`). The [native PostgreSQL driver topic](/topics/postgresql-driver) shows it in use. The names below
are defined in `adorm.drivers.asyncpg.driver`.

```python
from adorm.drivers.asyncpg import AsyncpgDriver, connect_arguments
```

### `adorm.drivers.asyncpg.AsyncpgDriver`

A [`Driver`](#adorm-drivers-driver): `name` `"asyncpg"`, `mode` `"async"`, `capabilities`
[`CAPABILITIES`](#adorm-drivers-asyncpg-capabilities). Created without arguments; a profile names it by class,
instance or import path. `close_timeout` (`5.0`) is how many seconds closing a connection may take before asyncpg
aborts it.

#### `async open_connection(profile, emit)`

Opens an [`AsyncpgConnection`](#adorm-drivers-asyncpg-asyncpgconnection) for `profile`:

1. Takes the `DATABASES` entry `profile.alias` (Django's `connections.settings`) with `profile.params` replacing
   its keys. An alias without an entry raises `ProgrammingError`, a backend other than PostgreSQL
   `NotSupportedError`, before anything connects.
2. Opens the connection with `asyncpg.connect(**connect_arguments(entry, time_zone=...))`, the time zone being the
   one Django sets on its own connections (Django's `TIME_ZONE` without `USE_TZ`, else the entry's `TIME_ZONE` or
   `UTC`); or with `profile.connect(profile)`, awaited when it returns an awaitable, which must give an
   `asyncpg.Connection` (anything else raises `InterfaceError`).
3. Sets the session time zone when the connection's differs and assumes `OPTIONS["assume_role"]`.
4. Builds the connection's [`DjangoCompiler`](#adorm-drivers-asyncpg-compiler-djangocompiler-alias-default-server-version-params-none)
   for the alias, `profile.params` and the server's version
   ([`server_version_num()`](#adorm-drivers-asyncpg-server-version-num-version)).

A failure closes the connection and raises an adorm exception (`OperationalError` with SQLSTATE `3D000` for an
unknown database, `53300` for too many connections, which [`is_refusal()`](#is-refusal-exc) recognises).

```python
adorm.configure(profiles={"default": {"driver": "adorm.drivers.asyncpg.AsyncpgDriver", "alias": "default"}})
driver = AsyncpgDriver()
pool = Pool(driver, ConnectionProfile("default", driver, params={"USER": "reports"}), max_connections=10)
```

### `adorm.drivers.asyncpg.AsyncpgConnection`

The [`DriverConnection`](#adorm-drivers-driverconnection) of `AsyncpgDriver`. `conn` is the `asyncpg.Connection`,
also `target`, what profile hooks receive; `compiler` is its `DjangoCompiler`; `profile` its profile.

| Method | Does |
|---|---|
| `_execute(operation)` | a read: [`translate()`](#adorm-drivers-django-translation-translate-query)s the operation's query, compiles it on the event loop and runs one statement: `select` → list of rows, `get` → the row (as Django's `get()`: no ordering unless sliced or `distinct()` on fields, at most [`MAX_GET_RESULTS`](#adorm-drivers-asyncpg-max-get-results) rows fetched, Django's messages), `first` / `last` → the row or `None` (ordered as Django's `first()` / `last()`, one row), `count` → `int` ([`compile_count()`](#compile-count-query-in-transaction-false)), `exists` → `bool` (Django's `Query.exists()`). A write: the [`WRITES`](#adorm-drivers-asyncpg-writes-writes) method of a new [`Writer`](#adorm-drivers-asyncpg-writes-writer-connection), with the results of the Django driver. Errors go through `map_error()`; Django's `SynchronousOnlyOperation` while a query is built (a manager's `get_queryset()` that accesses the database) raises `NotSupportedError`. Only an error or a cancellation of a statement, or a cancellation inside a transaction the write opened, can make the connection unusable. While the transaction is rollback-only (a write failed after its first statement, outside a savepoint of its own) every operation raises `TransactionManagementError` |
| `begin()` | `BEGIN`, or `BEGIN ISOLATION LEVEL <level>` for `OPTIONS["isolation_level"]` ([`ISOLATION_LEVELS`](#adorm-drivers-asyncpg-isolation-levels)); inside a transaction `TransactionManagementError` |
| `commit()`, `rollback()` | `COMMIT`, `ROLLBACK`; a `COMMIT` whose status is `ROLLBACK` (the transaction failed earlier) raises `TransactionManagementError`, and so does `commit()` of a rollback-only transaction, after sending `ROLLBACK` instead |
| `savepoint(name)`, `release_savepoint(name)`, `rollback_to_savepoint(name)` | `SAVEPOINT`, `RELEASE SAVEPOINT`, `ROLLBACK TO SAVEPOINT` with `name` quoted. Outside a transaction, for a savepoint name already open (`savepoint`) and for one that is not open (the others) `TransactionManagementError`. Releasing a savepoint releases those opened after it; rolling back to one discards those opened after it and keeps it open, and ends the rollback-only state of a write that failed after it was opened |
| `set_options(options, local)` | the statements of [`set_options_sql(options, local)`](#adorm-drivers-sql-set-options-sql-options-local) in one simple-protocol message; errors as for any statement |
| `cancel()` | cancels the statement running when it is called, from any thread; that call raises `OperationalError` with SQLSTATE `57014`, a statement that starts later is not affected. Nothing without a running statement |
| `is_usable()` | `False` once a connection error was raised, once a statement was cancelled before it finished, once a write was cancelled inside a transaction of its own (also right after its `BEGIN` or during its `COMMIT`) or that transaction's `ROLLBACK` failed or was cancelled, after `close()` and when asyncpg reports the connection closed |
| `close()` | asyncpg's `close()` within the driver's `close_timeout` (asyncpg aborts the connection when it fails); logs errors, a second call does nothing |
| `map_error(exc)` | [`map_django_error(exc)`](#adorm-drivers-django-map-django-error-exc): Django's exceptions as on the Django driver, asyncpg's by their SQLSTATE; asyncpg's `InternalClientError` (its protocol in an unexpected state, as after the server ended an idle connection) is `InterfaceError` |

Each statement runs in a task of its own, so `cancel()` stops it without cancelling the caller. Cancelling the
task that awaits `execute()` cancels the statement as well (asyncpg sends a cancel request) and raises
`asyncio.CancelledError`. A statement cancelled either way before it finished makes the connection unusable: the
server may have run it without the protocol's closing Sync message and keep its transaction and locks open until
the connection's next message, so the slot closes the connection instead of releasing it to the pool. A statement
inside a transaction compiles with `in_transaction=True`.

```python
connection = await AsyncpgDriver().open_connection(ConnectionProfile("default", driver), emit)
await connection.begin()
await connection.savepoint("adorm_sp_1")
await connection.execute(Create("library.Tag", {"name": "night"}))
await connection.rollback_to_savepoint("adorm_sp_1")
await connection.commit()
```

### `adorm.drivers.asyncpg.CAPABILITIES`

The [`Capabilities`](#adorm-drivers-capabilities) of `AsyncpgDriver`: the `operations` and `constructs` of the
Django driver's [`CAPABILITIES`](#adorm-drivers-django-capabilities) (the reads, the writes `create`, `save`, `update`,
`delete`, `delete_instance`, `get_or_create`, `update_or_create`; every lookup, `Lower`, `Upper`, `Subquery`,
arithmetic (`CombinedExpression`), `distinct`, `DISTINCT ON`, `only`, `defer`, `select_related`, the query's manager,
`select_for_update()` with its options, field paths across relations (`path.nested`) and annotations
(`clause.annotate`)), `savepoints`, `cancellation` and the `connection_options` of
[`POSTGRESQL_OPTIONS`](#adorm-drivers-sql-postgresql-options). No `backend_events`.

### `adorm.drivers.asyncpg.writes.Writer(connection)`

Runs write operations on `connection`, an `AsyncpgConnection`, with Django's write logic and without Django's model
methods and signals ([what runs and what does not](/topics/postgresql-driver#what-runs-and-what-does-not)). Every
statement is compiled by the connection's `DjangoCompiler` and awaited on its `asyncpg.Connection`. Model instances are
Django's, built with the model's constructor or `Model.from_db()`.

| Method | Django's counterpart |
|---|---|
| `create(operation)`, `save(operation)`, `update(operation)`, `delete(operation)`, `delete_instance(operation)`, `get_or_create(operation)`, `update_or_create(operation)` | what the [Django driver](#adorm-drivers-django-djangoconnection) calls for that operation; the same results |
| `save_instance(instance, *, force_insert=False, force_update=False, update_fields=None)` | `Model.save()`: parents' tables first, an `UPDATE` then an `INSERT` per table, `RETURNING` the primary key and the values the database sets |
| `create_instance(model, values)` | `QuerySet.create()` |
| `update_queryset(queryset, values)`, `execute_update(query)` | `QuerySet.update()`, the update compiler; an update of multi-table inheritance parent fields selects the keys, then updates each table |
| `delete_queryset(queryset)` | `QuerySet.delete()` with the [`Collector`](#adorm-drivers-asyncpg-writes-collector-writer) |
| `update_batch(model, keys, values)` | `UpdateQuery.update_batch()`: `values` set on the rows with primary keys `keys`, in batches of `GET_ITERATOR_CHUNK_SIZE` (100) |
| `delete_batch(model, keys)` | `DeleteQuery.delete_batch()`: the rows with primary keys `keys` deleted in batches of `GET_ITERATOR_CHUNK_SIZE`; returns the number deleted |
| `raw_delete(queryset)` | `QuerySet._raw_delete()`: the rows of `queryset` deleted with one statement; returns the number deleted |
| `atomic(*, savepoint=True)` | `transaction.atomic()`: a transaction of its own outside the connection's, a savepoint inside it; with `savepoint` false the block runs in the connection's transaction, and an error after a statement of the block ran leaves that transaction rollback-only (Django's `needs_rollback`) |
| `get(queryset, lookups=None)`, `instances(queryset)`, `keys(queryset)`, `exists(queryset)`, `rows(queryset)`, `row_count(query)` | `get()`, model instances by primary key, `values_list("pk")`, `exists()`, the rows of a query, the rows a statement affected |

A write whose model, manager or query set overrides the Django method it would call (`save()`, `save_base()`,
`delete()`, `create()`, `update()`, `get_or_create()`, `update_or_create()`, `get()`, `select_for_update()`), and a
delete that reaches an `on_delete` handler other than Django's own, raise `NotSupportedError` before any statement
changes data. A cancellation inside a transaction `atomic()` opened makes the connection unusable, so the slot closes
it and the server rolls the transaction back.

```python
writer = Writer(connection)
instance = await writer.create_instance(dj.Tag, {"name": "night"})       # INSERT ... RETURNING "id"
await writer.update_queryset(dj.Edition.objects.filter(number=2), {"title": "Second", "number": 3})
total, counts = await writer.delete_queryset(dj.Author.objects.filter(slug="ann"))
```

### `adorm.drivers.asyncpg.writes.Collector(writer)`

Django's deletion `Collector` on a `Writer`: `collect_queryset(queryset)` and `collect(model, keys)` collect the rows
a delete removes, the rows its `on_delete` handlers update and the references that refuse it, by primary key;
`delete()` then runs the deletes and updates and returns `(total, {label: count})`. It sends no signals and runs only
Django's own handlers (`CASCADE`, `PROTECT`, `RESTRICT`, `SET_NULL`, `SET_DEFAULT`, `SET()`, `DO_NOTHING` and the
`DB_*` handlers, which the database applies); another handler raises `NotSupportedError` before anything is deleted.
`ProtectedError` and `RestrictedError` carry the referencing rows as Django instances. Its statements stay within
`BATCH_SIZE` keys per foreign key and `MAX_QUERY_PARAMS` parameters; more than one runs in `Writer.atomic()`.

```python
collector = Collector(Writer(connection))
await collector.collect_queryset(dj.Author.objects.filter(slug="ann"))
total, counts = await collector.delete()
```

### `adorm.drivers.asyncpg.writes.WRITES`

The `Writer` method for each write operation kind: `create`, `save`, `update`, `delete`, `delete_instance`,
`get_or_create`, `update_or_create`.

### `adorm.drivers.asyncpg.writes.BATCH_SIZE`

`10000`: the most keys one statement of a delete's collection or of a multi-table inheritance update names per
foreign key; a statement naming keys for several foreign keys, and the `SET_NULL` / `SET()` updates of a delete joined
into one statement, stay within [`MAX_QUERY_PARAMS`](#adorm-drivers-asyncpg-compiler-max-query-params) keys. More keys
run in batches. Deletes and the updates of a delete's `SET_*` handlers by key run in batches of Django's
`GET_ITERATOR_CHUNK_SIZE` (100), as Django runs them.

### `adorm.drivers.asyncpg.connect_arguments(settings_dict, *, time_zone=None)`

Returns the keyword arguments of `asyncpg.connect()` for a Django `DATABASES` entry:

| Entry | Argument |
|---|---|
| `NAME`, `USER`, `PASSWORD`, `HOST` | `database`, `user`, `password`, `host`; empty values are left out, `NAME` `None` is `"postgres"` |
| `PORT` | `port` as `int`, left out when empty |
| `OPTIONS["options"]` | its `-c name=value`, `-cname=value` and `--name=value` settings in `server_settings` (dashes in the name become underscores); any other token raises `NotSupportedError` |
| `OPTIONS["connect_timeout"]` | `timeout` as `float` |
| `time_zone` | `server_settings["TimeZone"]` |
| `OPTIONS["isolation_level"]` | checked only: a value that is not one of psycopg's `IsolationLevel` values raises `ProgrammingError` |
| `OPTIONS["assume_role"]` | none; the connection applies it |
| `OPTIONS["pool"]`, `["prepare_threshold"]`, `["cursor_factory"]`, `["context"]`, `["server_side_binding"]` | none |
| `OPTIONS` keys in [`DSN_PARAMETERS`](#adorm-drivers-asyncpg-dsn-parameters) | URL-encoded into `dsn` (`"postgresql://?sslmode=require"`), which asyncpg parses |
| any other `OPTIONS` key | raises `NotSupportedError` naming it |

```python
connect_arguments({"NAME": "clubs", "USER": "app", "PORT": "6432", "OPTIONS": {"sslmode": "require"}})
# {'database': 'clubs', 'user': 'app', 'port': 6432, 'dsn': 'postgresql://?sslmode=require'}
connect_arguments({"NAME": None, "OPTIONS": {"options": "-c search_path=app"}}, time_zone="UTC")
# {'database': 'postgres', 'server_settings': {'search_path': 'app', 'TimeZone': 'UTC'}}
```

### `adorm.drivers.asyncpg.DSN_PARAMETERS`

The `OPTIONS` keys `connect_arguments()` passes in asyncpg's `dsn`: the libpq parameters asyncpg reads (`passfile`,
`sslmode`, `sslcert`, `sslkey`, `sslrootcert`, `sslcrl`, `sslpassword`, `sslnegotiation`,
`ssl_min_protocol_version`, `ssl_max_protocol_version`, `target_session_attrs`, `krbsrvname`, `gsslib`, `service`)
and `application_name` and `client_encoding`, which asyncpg sends to the server as settings. asyncpg sends any other
`dsn` parameter to the server as a setting, so a libpq-only parameter such as `keepalives` would make the server
refuse the connection; `connect_arguments()` refuses it before connecting.

### `adorm.drivers.asyncpg.ISOLATION_LEVELS`

The isolation level `begin()` names for each value of psycopg's `IsolationLevel`: `1` `READ UNCOMMITTED`, `2`
`READ COMMITTED`, `3` `REPEATABLE READ`, `4` `SERIALIZABLE`.

```python
"default": {
    "driver": "adorm.drivers.asyncpg.AsyncpgDriver",
    "params": {"OPTIONS": {"isolation_level": psycopg.IsolationLevel.SERIALIZABLE}},
}
```

### `adorm.drivers.asyncpg.MAX_GET_RESULTS`

`21`: the most rows a `get` fetches, as Django's `get()`; with that many it reports `it returned more than 20!`.

### `adorm.drivers.asyncpg.server_version_num(version)`

Returns PostgreSQL's `server_version_num` for an asyncpg `ServerVersion` (`Connection.get_server_version()`):
`170006` for 17.6, `90624` for 9.6.24. asyncpg reads a version from 10 on as `major`, `0`, `micro` (17.6 is
`ServerVersion(17, 0, 6, ...)`).

## SQL statements

`adorm.drivers.sql` holds what a driver that executes SQL itself needs regardless of where the SQL comes from. It
imports no ORM or database library.

```python
from adorm.drivers.sql import numbered_placeholders, POSTGRESQL_OPTIONS, set_options_sql, Statement
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

### `adorm.drivers.sql.POSTGRESQL_OPTIONS`

`frozenset({"statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout"})`: the
[connection options](/ref/options) `set_options_sql()` sets, PostgreSQL's time limits. The Django and the native
PostgreSQL drivers declare them.

### `adorm.drivers.sql.set_options_sql(options, local)`

The statements that set `options` on a PostgreSQL connection, separated by `; `, in the order of `options`; an empty
string for no options. Values are seconds, non-negative and finite, sent as whole milliseconds (`0` means no limit; a
positive value below one millisecond becomes one millisecond). A value of `None` resets the option to its default.
Raises `ValueError` for a name outside `POSTGRESQL_OPTIONS` and for any other value.

| `local` | A value | `None` |
|---|---|---|
| `False` | `SET name = <ms>` | `RESET name` |
| `True` | `SET LOCAL name = <ms>` | `SET LOCAL name TO DEFAULT` |

```python
set_options_sql({"statement_timeout": 5, "lock_timeout": None}, local=True)
# 'SET LOCAL statement_timeout = 5000; SET LOCAL lock_timeout TO DEFAULT'
```

## Django's compiler for asyncpg

`adorm.drivers.asyncpg.compiler` compiles Django queries into SQL for asyncpg, without a database connection; the
[native PostgreSQL driver](#native-postgresql-driver) builds its statements with it. It imports Django, psycopg 3 (Django's PostgreSQL backend must run on it) and asyncpg, which the `asyncpg` extra
installs (`pip install "adorm[asyncpg]"`). [SQL compilation for asyncpg](/topics/sql-compilation) describes the
behaviour it relies on.

```python
from adorm.drivers.asyncpg.compiler import bind_param, DjangoCompiler, DjangoStatement, MAX_QUERY_PARAMS
```

### `adorm.drivers.asyncpg.compiler.DjangoCompiler(alias="default", *, server_version, params=None)`

Compiles Django queries for the `DATABASES` entry `alias` into [`DjangoStatement`](#adorm-drivers-asyncpg-compiler-djangostatement)
objects. It compiles on a database wrapper of its own (`connection`), built from that entry and never connected,
so Django's thread-local connection for `alias` is not touched and compiling works in any thread and inside a
running event loop. One instance can be shared by threads and tasks.

`server_version` is PostgreSQL's `server_version_num` (`170006` for 17.6). Django reads it to choose the SQL for
`AnyValue`, `JSONObject`, `JSONArray` and `UUID7`; a version that lacks a construct raises Django's
`NotSupportedError` for it, as Django does. `params` replace keys of the entry for this compiler, as a connection
profile's `params` do; the resulting entry is the attribute `settings_dict`. Parameters are bound for the entry's
`OPTIONS["server_side_binding"]` (attribute `server_side_binding`). The wrapper's `features.max_query_params` is
[`MAX_QUERY_PARAMS`](#adorm-drivers-asyncpg-compiler-max-query-params), so on Django 6
`compiler.connection.ops.bulk_batch_size(fields, objs)` returns batch sizes asyncpg accepts; Django 5.2's PostgreSQL
backend does not read it.

An entry whose backend is not PostgreSQL, or whose PostgreSQL backend runs on psycopg2, raises `NotSupportedError`
(adorm's).

```python
server_version = int(await conn.fetchval("SHOW server_version_num"))
compiler = DjangoCompiler("default", server_version=server_version)
reports = DjangoCompiler("default", server_version=server_version, params={"TIME_ZONE": "Europe/Ljubljana"})
```

#### `compile(query, *, in_transaction=False, returning=())`

Compiles `query`, a Django `QuerySet` or `Query` (select, update, delete or aggregate), into a `DjangoStatement`:
`%s` placeholders become `$n`, each parameter goes through [`bind_param()`](#adorm-drivers-asyncpg-compiler-bind-param-value-server-side-binding-false)
in the compiler's binding mode and its placeholder gets the cast `bind_param()` returns.

A select whose filter matches nothing (`pk__in=[]`) compiles with `WHERE 0 = 1`. An update or delete whose filter
matches nothing, and an update without values, compile into an empty statement ([`empty`](#empty)), which the caller
does not execute.

`in_transaction` tells whether the statement runs inside a transaction: a `select_for_update()` query compiles only
then and raises adorm's `TransactionManagementError` otherwise, as Django does. `returning` names the Django fields
an update returns: the statement ends with `RETURNING` and keeps them in [`returning`](#returned-records); on Django
5.2, which does not compile it, `returning` raises adorm's `NotSupportedError`.

Raises adorm's `NotSupportedError` for an insert ([`compile_insert()`](#compile-insert-query-returning) compiles one),
for an update that sets fields of a multi-table inheritance parent, which Django runs as one statement per table
after selecting the keys on its own connection ([`Writer.execute_update()`](#adorm-drivers-asyncpg-writes-writer-connection)
runs it), for a statement with more than `MAX_QUERY_PARAMS` parameters, and for a construct that needs a connection
to compile: `SearchHeadline` with options quotes them through psycopg.

```python
statement = compiler.compile(Book.objects.filter(title__icontains="war", pages__gte=100).order_by("isbn"))
statement.sql      # '... WHERE ("library_book"."pages" >= $1::int4 AND UPPER("library_book"."title"::text) LIKE UPPER($2)) ...'
statement.params   # (100, '%war%')
records = await conn.fetch(statement.sql, *statement.params)
```

#### `compile_insert(query, returning=())`

Compiles `query`, a Django `InsertQuery` of one row, into a `DjangoStatement` that returns the Django fields
`returning` in order (`RETURNING`, none when it is empty). The fields' `pre_save()` (`auto_now_add`) and
`get_db_prep_save()` run while compiling, as in Django's insert. A query that compiles into several statements, and one
with more than `MAX_QUERY_PARAMS` parameters, raise `NotSupportedError`.

```python
query = InsertQuery(Author)
query.insert_values([Author._meta.get_field("name"), Author._meta.get_field("slug")], [Author(name="Ana", slug="ana")])
statement = compiler.compile_insert(query, [Author._meta.pk])
statement.sql      # 'INSERT INTO "library_author" ("name", "slug") VALUES ($1, $2) RETURNING "library_author"."id"'
statement.returned(await conn.fetch(statement.sql, *statement.params))     # [(42,)]
```

#### `compile_count(query, *, in_transaction=False)`

Compiles the number of rows of `query`, a Django select `QuerySet` or `Query`, into a `DjangoStatement` that returns
one row with one `bigint` column: `SELECT COUNT(*) FROM (<query>) subquery`, the inner query with column aliases. It
counts the rows the query returns, after its `distinct()`, slice and annotations; the inner query keeps its ordering
only when a slice or `distinct()` on fields needs it, as Django's `count()` does. A query that matches nothing counts
0. `query` is not changed. `in_transaction` and the errors are those of `compile()`.

```python
statement = compiler.compile_count(Book.objects.order_by("isbn")[1:])
statement.sql      # 'SELECT COUNT(*) FROM (SELECT "library_book"."isbn" AS "col1", ... OFFSET 1) subquery'
await conn.fetchval(statement.sql, *statement.params)
```

### `adorm.drivers.asyncpg.compiler.DjangoStatement`

A [`Statement`](#adorm-drivers-sql-statement) compiled by `DjangoCompiler`. Its `compiler` field holds Django's
compiler for the query and its `returning` field the Django fields of its `RETURNING` clause (empty without one); both
are left out of comparison and `repr()`.

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
too), as psycopg loads them, gives `timestamptz` values the time zone of the compiler's connection (`UTC`, or the
entry's `TIME_ZONE`; under `USE_TZ = False` naive values in Django's `TIME_ZONE`, as Django's psycopg loader returns
them), and applies the fields' `from_db_value()` converters, JSON decoding included.

```python
statement = compiler.compile(Book.objects.values("isbn", "extra"))
statement.rows(await conn.fetch(statement.sql, *statement.params))
# [('9780000000001', {'tags': ['x', 'y']}), ...]
```

#### `returned(records)`

Returns `records`, the rows of the statement's `RETURNING` clause, as a list of tuples of the values Django's own
insert or update returns: values loaded as `rows()` loads them, then the `from_db_value()` converters of the
`returning` fields.

```python
statement = compiler.compile_insert(query, [Book._meta.get_field("extra")])
statement.returned(await conn.fetch(statement.sql, *statement.params))      # [({'series': 'dune'},)]
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
