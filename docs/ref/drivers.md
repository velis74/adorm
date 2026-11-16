# Drivers

The driver contract, importable from `adorm.drivers` (defined in `adorm.drivers.base`). Overview:
[drivers topic](/topics/drivers); tutorial: [Writing a driver](/howto/writing-a-driver). Importing `adorm.drivers`
imports no ORM or database library.

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

#### `capabilities_for(profile)`

What the driver supports on connections for `profile`; defaults to `capabilities`. A driver whose support depends on
the database a profile names overrides it ([`DjangoDriver`](#adorm-drivers-django-djangodriver)). Does no I/O. A
[pool](/ref/pool) reads it once, as its `capabilities`, and checks operations and connection options against it.

```python
DjangoDriver().capabilities_for(ConnectionProfile("default", DjangoDriver))   # SQLITE_CAPABILITIES for a SQLite alias
```

#### `check(operation, *, schema_hashes=None, capabilities=None)`

Raises `NotSupportedError` when the driver does not support `operation`: its kind or a construct it uses is missing
from `capabilities` (default: the driver's `capabilities`; a pool passes its own)
([`Capabilities.check()`](#check-operation-driver-none) with the driver's `name`), or it is a
`Call` of a method the registered model does not declare. When a construct is missing and a lookup of the operation's
query does not resolve against the model registry ([`check_lookups()`](/ref/lookups#resolving-keys)), it raises that
[`FieldError`](/ref/exceptions#adorm-fielderror-message-none-model-none-key-none-original-none-sqlstate-none)
instead; an unresolvable key is a `FieldError` on every driver.

Then it compares the schema hash the client expects of every model of
[`operation_models(operation)`](#adorm-drivers-operation-models-operation) with the driver's
([`check_schema_hash()`](#check-schema-hash-label-expected)), which raises
[`SchemaMismatchError`](/ref/exceptions#adorm-schemamismatcherror-message-none-model-none-expected-none-actual-none-driver-none-reason-none).
`schema_hashes` holds the client's hashes by label, as the client sends them on connect
([`registry.schema_hashes()`](/ref/models#adorm-models-registry)); `None` (the default) takes each model's
`_meta.schema_hash` from this process's model registry. A label missing from `schema_hashes` is not checked.

Does no I/O. The [router](/ref/pool) calls it before acquiring a slot for an operation outside a transaction, so an
unsupported operation or a mismatched model neither waits for a slot nor opens one;
[`DriverConnection.execute()`](#adorm-drivers-driverconnection) calls it before every execution.

```python
class Unaccent(Func):
    arity = 1


driver = DjangoDriver()
driver.check(Select(Member.objects.filter(first_name="Ana").query))   # supported: returns None
driver.check(Select(Member.objects.annotate(n=Unaccent("first_name")).query))
# NotSupportedError: 'select' operation needs unsupported function.Unaccent (driver 'django')

# a driver executing operations for a client of another process, with the hashes the client sent
driver.check(operation, schema_hashes=client_hashes)
```

#### `schema_hash(label)`

Returns the schema hash of the model `label` as the driver's target defines it, computed as
[`adorm generate`](/ref/codegen#adorm-generate) computes the hash in a generated model's
[`ModelMeta`](/ref/models#adorm-modelmeta-label-pk-id-schema-hash-none-orm-names-none-concrete-model-none). `None`
(the default) means the driver does not compute schema hashes and its models are not checked. Raises `LookupError`
when the target has no model `label`. Does no I/O.
[`DjangoDriver`](#adorm-drivers-django-djangodriver) and [`AsyncpgDriver`](#adorm-drivers-asyncpg-asyncpgdriver)
return [`model_schema_hash(label)`](#adorm-drivers-django-model-schema-hash-label) of the installed Django model.

```python
class AppDriver(Driver):
    def schema_hash(self, label):
        return self.registry.hash_of(label)   # raises LookupError for a model the target lacks
```

#### `check_schema_hash(label, expected)`

Raises [`SchemaMismatchError`](/ref/exceptions#adorm-schemamismatcherror-message-none-model-none-expected-none-actual-none-driver-none-reason-none)
when `expected`, the schema hash the client's model `label` was generated with, differs from
[`schema_hash(label)`](#schema-hash-label), or when the target has no such model or its hash cannot be computed
(`actual` is `None`, `reason` says which). Passes when `expected` is `None` (a hand-written model, a label the client
did not register) or `schema_hash()` returns `None`.

- The driver's hash of a label is computed at the label's first check and kept for the driver's lifetime; an error
  of `schema_hash()` is kept too and reported in every later mismatch. `expected` is compared at every check.
- Callable from any thread; concurrent first checks of one label compute its hash once. Does no I/O.
- [`check()`](#check-operation-schema-hashes-none-capabilities-none) calls it for every model of an operation.

```python
driver.check_schema_hash("clubs.Club", Club._meta.schema_hash)
# SchemaMismatchError: the adorm model clubs.Club was generated with schema hash e8c2eed312ab7b68, the django
# driver's model has 51f0c9a4b2d71e03: run adorm generate
```

#### `async check_listen(profile)`

Raises `NotSupportedError` when a connection for `profile` cannot listen for notifications; opens no connection.
Defaults to [`capabilities_for(profile).check_backend_event("notify", name)`](#check-backend-event-name-driver-none).
[`adorm.listen()`](/ref/events#adorm-listen-channels-session-none) calls it before it takes a slot. `DjangoDriver` also raises it inside
Django's `TestCase`, whose shared connection receives no notifications.

```python
await DjangoDriver().check_listen(profile)   # with psycopg 2: NotSupportedError: the backend event 'notify' is not supported (driver 'django')
```

#### `is_refusal(exc)`

Returns whether `exc`, an adorm exception raised by `open_connection()`, means the database refuses more
connections. A [pool](/ref/pool#adorm-executor-pool-driver-profile-max-connections-0-acquire-timeout-30-0-commit-timeout-60-0-release-timeout-30-0-purge-stale-connections-at-300-0-check-schema-false) with `max_connections=0` then stops opening connections and queues the
request. Defaults to [`is_refusal(exc)`](#adorm-drivers-is-refusal-exc); override it when the library reports a
refused connection without a SQLSTATE:

```python
class DjangoDriver(ThreadedDriver):
    def is_refusal(self, exc):
        # psycopg raises "too many connections" without a SQLSTATE
        return super().is_refusal(exc) or "too many connections" in str(exc.original)
```

#### `database_key(profile)`

Returns a hashable value naming the database `profile` connects to, or `None` (the default) when the driver cannot
tell. Profiles with equal keys share what adorm caches per database
([`adorm.content_types`](./models#adorm-contenttypes)); a profile whose key is `None` has a cache of its own. Does no
I/O. [`DjangoDriver`](#adorm-drivers-django-djangodriver) and
[`AsyncpgDriver`](#adorm-drivers-asyncpg-asyncpgdriver) return
[`adorm.drivers.django.database_key(profile)`](#adorm-drivers-django-database-key-profile).

```python
class TenantDriver(DjangoDriver):
    def database_key(self, profile):
        return ("tenant", profile.params["tenant"])    # every profile of a tenant shares its content type ids
```

#### `async open_monitor(profile)`

Opens a [`DatabaseMonitor`](#adorm-drivers-databasemonitor) on a connection of its own to `profile`'s database,
outside every pool. Raises `NotSupportedError` unless overridden. [`DjangoDriver`](#adorm-drivers-django-djangodriver)
returns a [`DjangoMonitor`](#adorm-drivers-django-djangomonitor-profile), [`AsyncpgDriver`](#adorm-drivers-asyncpg-asyncpgdriver)
an [`AsyncpgMonitor`](#adorm-drivers-asyncpg-asyncpgmonitor-conn-close-timeout-5-0).
[`wait_for_lock()`](/ref/testing#adorm-testing-wait-for-lock-task-none-session-none-timeout-10-0-interval-0-01) reads
the sessions of running operations through it.

```python
async with await driver.open_monitor(profile) as monitor:
    activity = await monitor.activity([connection.backend_pid])
```

### `adorm.drivers.DatabaseMonitor`

Abstract base class of what `Driver.open_monitor()` returns: a dedicated connection that reads the database's
reports on other sessions. Used from one event loop; an `async with` block closes it.

| Method | Does |
|---|---|
| `async activity(pids)` | the [`BackendActivity`](#adorm-drivers-backendactivity) of each session in `pids` ([`backend_pid`](#backend-pid) values) as a dict by pid, read now; a pid without a session is left out |
| `async close()` | closes the monitor's connection; logs errors instead of raising them; a second call does nothing |

```python
class RawSQLMonitor(DatabaseMonitor):
    def __init__(self, conn):
        self.conn = conn

    async def activity(self, pids):
        rows = await self.conn.fetch(activity_sql("$1"), list(pids))
        return {row[0]: backend_activity(*row) for row in rows}

    async def close(self):
        await self.conn.close()
```

### `adorm.drivers.BackendActivity`

A frozen dataclass: what the database reports about one session (PostgreSQL's `pg_stat_activity`).

| Attribute | Value |
|---|---|
| `pid` | the session's server process id |
| `state` | `"active"`, `"idle"`, `"idle in transaction"` …; `None` when not reported |
| `wait_event_type`, `wait_event` | what the session waits for, such as `"Lock"` and `"transactionid"` for a row lock; `None` when it waits for nothing |
| `blocking_pids` | a tuple of the sessions holding the locks it waits for (`pg_blocking_pids()`); empty when it waits for none |
| `waiting_for_lock` | a property: whether `wait_event_type` is `"Lock"`, a wait for a row, a table, a transaction id or an advisory lock |

```python
BackendActivity(4711, "active", "Lock", "transactionid", (4702,)).waiting_for_lock    # True
```

### `adorm.drivers.ExecutionMode`

`Literal["threaded", "async"]`. `"threaded"`: each connection runs a synchronous target on its own thread.
`"async"`: native coroutines on the event loop. A threaded driver with `direct = True` runs on the event loop's
thread instead (see [`ThreadedDriver`](#adorm-drivers-threadeddriver)).

### `adorm.drivers.Capabilities`

`Capabilities(operations=frozenset(), constructs=frozenset(), connection_options=frozenset(), backend_events=frozenset(), savepoints=False, cancellation=False, max_channel_length=None)`

What a driver supports; a frozen, hashable dataclass. The four name fields take any iterable of strings, stored as
`frozenset`; a plain string raises `TypeError`.

| Field | Contents |
|---|---|
| `operations` | operation kinds (`Operation.kind`) |
| `constructs` | construct names (`Operation.constructs()`), `<category>.<name>` |
| `connection_options` | option names `set_options()` accepts |
| `backend_events` | `BackendEvent.name` values the driver emits |
| `savepoints` | the driver implements savepoints; `False` by default |
| `cancellation` | the driver cancels a running statement; `False` by default |
| `max_channel_length` | the longest notification channel name the backend accepts, in bytes of UTF-8; `None` (the default) for no limit |

#### `unsupported(operation)`

Returns the unsupported names `operation` uses, as a list: the operation's kind first when unsupported, then the
unsupported constructs, sorted. Empty for a supported operation.

#### `check(operation, driver=None)`

Raises `NotSupportedError` naming every unsupported name, or returns `None`. `driver`, the driver's name, ends the
message and is the error's [`driver`](/ref/exceptions#adorm-notsupportederror); the error's `unsupported` holds the
names `unsupported(operation)` returns.

#### `check_savepoints(driver=None)`

Raises `NotSupportedError` (`unsupported` `("savepoints",)`) when `savepoints` is `False`.

#### `check_backend_event(name, driver=None)`

Raises `NotSupportedError` (`unsupported` `(name,)`) when `name` is not in `backend_events`.

```python
CAPABILITIES.check_backend_event("notify", "asyncpg")    # declared: returns None
Capabilities().check_backend_event("notify", "memory")   # NotSupportedError: the backend event 'notify' is not supported (driver 'memory')
```

#### `check_channel(channel)`

Returns `channel` when [`adorm.drivers.check_channel()`](#adorm-drivers-check-channel-channel) accepts it and it is
at most `max_channel_length` bytes in UTF-8; raises `TypeError` or `ValueError` otherwise.
`DriverConnection.listen()`, `unlisten()` and [`adorm.listen()`](/ref/events#adorm-listen-channels-session-none) check the names with it.

```python
CAPABILITIES.check_channel("orders")    # 'orders'
CAPABILITIES.check_channel("x" * 64)    # ValueError: the channel name 'xxx…' is longer than 63 bytes
```

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

Returns whether `exc`, an adorm exception, leaves a PostgreSQL connection unusable. An `adorm.Error` whose
`connection_lost` a connection set (a threaded connection sets it from its `is_connection_lost()`) gives that value.
Otherwise `True` for:

- `InterfaceError`;
- `OperationalError` without a SQLSTATE, or with a SQLSTATE of class `08` (connection exception) or `57P01` to
  `57P03` (shutdown);
- a `BaseException` that is not an `Exception`, except `asyncio.CancelledError`.

The default of [`DriverConnection.is_connection_lost()`](#adorm-drivers-driverconnection).

```python
is_connection_error(OperationalError(sqlstate="08006"))   # True
is_connection_error(OperationalError(sqlstate="57014"))   # False
```

### `adorm.drivers.operation_models(operation)`

The labels of the models whose tables `operation` reads or writes, without repeats:

- its `model`;
- the model of every query inside it (a `Subquery`, `Exists`, `pk__in=queryset`, the parts of a `union()`);
- every model a relation path of those queries reaches, as far as the model registry resolves it. Paths are those of
  lookups and `F()` references in filters, annotations, ordering and written values, and of `values()`, `only()`,
  `defer()`, `distinct()` and `select_related()`. A many-to-many path reaches the related model; an `OuterRef()` path
  is resolved in the query it refers to; `select_related()` without fields adds no labels.

Empty for an operation without a `model`.

```python
from adorm.drivers import operation_models

operation_models(Select(Membership.objects.select_related("club", "member").query))
# ('clubs.Membership', 'clubs.Club', 'clubs.Member')
operation_models(Select(Club.objects.filter(Exists(Membership.objects.filter(club=OuterRef("pk")))).query))
# ('clubs.Club', 'clubs.Membership')
operation_models(Select(Member.objects.filter(memberships__club__city="Koper").query))
# ('clubs.Member', 'clubs.Membership', 'clubs.Club')
```

### `adorm.drivers.check_channel(channel)`

Returns `channel`, a notification channel name, when it is a non-empty string without NUL; raises `TypeError` for a
value that is not a string and `ValueError` otherwise. The name is case-sensitive (`"Orders"` and `"orders"` are
different channels); the PostgreSQL drivers quote it in `LISTEN`. A driver's length limit is checked by
[`Capabilities.check_channel()`](#check-channel-channel).

```python
check_channel("orders")       # 'orders'
check_channel("")             # ValueError: a channel name is a non-empty string without NUL, not ''
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

Abstract base class of a driver's connection; `driver` is an attribute. One slot owns the connection and awaits one
call at a time from one event loop. Every method raises adorm exceptions only; `asyncio.CancelledError` passes
through.

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
| `async listen(channel)` | checks the name ([`capabilities.check_channel()`](#check-channel-channel)) and that `capabilities.backend_events` has `notify`, adds the channel to `listening`, then awaits `_listen(channel)`; from then on each notification on the channel is emitted as `BackendEvent("notify", channel, payload)`, also while no call runs. A failed call removes the channel again; a cancelled one leaves it, since the database may have run it |
| `async unlisten(channel)` | awaits `_unlisten(channel)` for a channel in `listening` and then removes it; nothing for another channel. A notification received before may still be emitted |
| `listening` | the `frozenset` of channels the connection listens on; the slot closes a connection that still listens when its lease ends ([`CloseReason.LISTENING`](/ref/pool#adorm-executor-closereason)) |
| `async set_options(options, local)` | checks the option names against `capabilities.connection_options`, then sets them from a mapping; `local=True` limits them to the open transaction (`SET LOCAL`); a value of `None` resets the option to its default (`RESET`, or `SET LOCAL … TO DEFAULT`). The slot calls it with the options that changed ([`Slot`](/ref/pool#adorm-executor-slot)) |
| `cancel()` | cancels the running statement, if any; callable from any thread, returns without waiting; the call raises the target's cancellation error (`is_cancellation()`; SQLSTATE `57014` on PostgreSQL) |
| `is_usable()` | whether the connection can serve further calls; does no I/O |
| `async close()` | closes the connection; logs errors instead of raising them; a second call does nothing |
| `target` | the target's connection that the profile's `on_acquire` and `on_release` hooks receive; defaults to the driver connection itself |
| `async call_hook(hook, *args)` | calls `hook(self.target, *args)`, awaits the result when it is awaitable and returns it; an exception the hook raises is mapped with `map_error()` |
| `map_error(exc)` | returns the adorm exception for an exception the target raised; defaults to `wrap_driver_error(exc)` |
| `is_connection_lost(exc)` | whether a mapped adorm exception leaves the connection unusable; defaults to [`is_connection_error(exc)`](#adorm-drivers-is-connection-error-exc). A driver for a target that reports statement errors as `OperationalError` without a SQLSTATE overrides it. The slot closes a connection whose request failed with such an error when the lease ends, whatever `is_usable()` says. A threaded connection records the answer in the error's `connection_lost` |
| `is_cancellation(exc)` | whether a mapped adorm exception is the error of a statement the target cancelled; defaults to SQLSTATE `57014`. A [threaded connection](#adorm-drivers-threadedconnection) raises `CancelledError` for a cancelled call that ended with such an error |
| `capabilities` | what the connection supports: [`Driver.capabilities_for()`](#capabilities-for-profile) of its `profile` attribute, the driver's `capabilities` without one |
| `ran_custom_sql` | whether the connection ran SQL adorm did not generate ([below](#ran-custom-sql)) |
| `backend_pid` | the process id of the connection's session on the database server ([below](#backend-pid)) |
| `holds_session`, `session_holds` | whether the running call has a database session the connection shares with others, and how many times it has taken it ([below](#holds-session-session-holds)) |
| `settled`, `async settle()` | whether, and until when, the connection still works for its last call after the call's outcome ([below](#settled)) |

A failed check raises `NotSupportedError`, naming the driver, before the subclass is called: `execute()` runs
[`Driver.check()`](#check-operation-schema-hashes-none-capabilities-none) with the connection's `capabilities`, the savepoint calls
check `savepoints`, `set_options()` checks `connection_options`. A subclass implements:

| Method | Required | Does |
|---|---|---|
| `async _execute(operation)` | yes | executes a supported operation |
| `begin()`, `commit()`, `rollback()`, `cancel()`, `is_usable()`, `close()` | yes | as above |
| `async _savepoint(name)`, `async _release_savepoint(name)`, `async _rollback_to_savepoint(name)` | no | savepoints; raise `NotSupportedError` unless overridden |
| `async _set_options(options, local)` | no | sets supported options from a dict, `None` values to their default; raises `NotSupportedError` for any option unless overridden |
| `async _listen(channel)`, `async _unlisten(channel)` | no | start and stop listening; raise `NotSupportedError` unless overridden. A driver that implements them declares `notify` in `backend_events` |

Backend events a connection emits through the `emit` callable it opened with
([events](/topics/events#backend-events)):

| Event | When |
|---|---|
| `BackendEvent("notify", channel, payload)` ([`NOTIFY`](/ref/events#adorm-events-notify)) | a notification arrives on a channel the connection listens on |
| `BackendEvent("notice", severity, {...})` ([`NOTICE`](/ref/events#adorm-events-notice)) | the server sends a notice or warning with a statement's result; emitted before that request's outcome |
| `BackendEvent("connection_lost", None, reason)` ([`CONNECTION_LOST`](/ref/events#adorm-events-connection-lost)) | the database connection ended without adorm closing it; at most once per connection. A driver that declares it detects the end at least while the connection listens and no call runs, and possibly at other times (asyncpg: whenever the server ends the connection). `is_usable()` is then `False` |

```python
class RawConnection(DriverConnection):
    async def _listen(self, channel):
        await self.conn.add_listener(channel, self._notified)

    async def _unlisten(self, channel):
        await self.conn.remove_listener(channel, self._notified)

    def _notified(self, _conn, _pid, channel, payload):
        self.emit(BackendEvent(NOTIFY, channel, payload))
```

#### `ran_custom_sql`

Whether the connection ran SQL that adorm did not generate; `False` until `mark_custom_sql()` is called. The slot
closes such a connection when its lease ends, also after a successful commit or rollback: that SQL may have changed
the connection's state (a `SET`, a temporary table, a prepared statement, an advisory lock).

#### `mark_custom_sql()`

Marks the connection as having run custom SQL. A driver calls it when code it runs for an operation executes its
own SQL; the [Django driver](#adorm-drivers-django-djangoconnection) does so for raw SQL of signal receivers, managers
and `on_commit()` callbacks. SQL of profile hooks and of `set_options()` (which the slot tracks) is not custom.
Callable from any thread; the mark is never cleared.

```python
class ReportsConnection(ThreadedConnection):
    def execute_sync(self, operation):
        if operation.kind == "report":
            self.mark_custom_sql()                 # the report's SQL comes from the application
            return run_report(self.db, operation.name)
        ...
```

#### `backend_pid`

The process id of the connection's session on the database server (PostgreSQL's `pg_backend_pid()`), the key of
[`DatabaseMonitor.activity()`](#adorm-drivers-databasemonitor); `None` (the default) when the driver does not know it.
Does no I/O. The Django driver reads it on the connection's thread on connect and before each call; the native
PostgreSQL driver returns asyncpg's `get_server_pid()`.

```python
class RawSQLConnection(DriverConnection):
    @property
    def backend_pid(self):
        return self.conn.get_server_pid()
```

#### `holds_session`, `session_holds`

For a connection that shares its database session with other connections and takes it for each call, such as the
[shared test connection](/ref/testing#adorm-testing-django-sharedconnectionmixin) of Django's `TestCase`:
`holds_session` is whether the running call has the session now, `session_holds` how many times the connection has
taken it. Defaults: `True` and `0` (a connection with its own session). Both do no I/O and are callable from any
thread. [`adorm.testing`](/ref/testing#waiting-for-the-database) does not report a call that waits for the session,
nor a lock wait of its `backend_pid` (held by another call's statement).

#### `settled`

Whether the connection has finished the work its last call runs after the call's outcome, such as Django's
`on_commit()` callbacks ([`after_call_sync()`](#adorm-drivers-threadedconnection)). `True` unless overridden.

#### `async settle()`

Waits until the connection is `settled`; returns at once unless overridden. When a lease ends on an unsettled
connection, `release()` returns at once and the slot awaits `settle()` (within the pool's `release_timeout`) before
deciding whether the connection stays in the pool, so custom SQL of that work is taken into account. Until then the
slot is in use.

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

A driver that cannot run the target ORM's code, such as a native SQL driver, declares neither; both then raise
`NotSupportedError`.

#### Cancellation

Cancelling the task that awaits `execute()` cancels the running statement; the call raises
`asyncio.CancelledError`, the connection stays usable, and the slot rolls back an open transaction.

What a cancelled request method (`execute()`, `commit()` …) does determines the
[`CancellationOutcome`](/ref/pool#adorm-cancellationoutcome) of the caller's `CancelledError` and the
[`TransactionOutcome`](/ref/transactions#adorm-transactionoutcome) of a cancelled commit:

| The method | Outcome |
|---|---|
| returns its result | `SUCCEEDED`; a `commit()` is `COMMITTED` |
| raises an error | `FAILED`; `UNKNOWN` for a connection error ([`is_connection_error()`](#adorm-drivers-is-connection-error-exc)); a `commit()` is `ROLLED_BACK`, or `UNKNOWN` for a connection error |
| raises `asyncio.CancelledError` | `UNKNOWN` |

A method whose statement finished before the cancellation reached it returns that result or raises that error. It
raises `CancelledError` only when the statement's effect is unknown, e.g. a statement the target cancelled.

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
synchronous methods; the async contract methods run them on that thread and await the results. Created inside a
running event loop; attributes `driver`, `profile` and `thread`.

Methods a subclass implements. All run on the connection's thread in a copy of the awaiting task's context: the
caller's context variables are visible, changes made inside the call are not propagated back.

| Method | Required | Does |
|---|---|---|
| `connect_sync()` | yes | opens the target's connection for `self.profile`, with `self.profile.connect(self.profile)` when it is set |
| `execute_sync(operation)` | yes | executes the operation, returns plain data |
| `begin_sync()`, `commit_sync()`, `rollback_sync()` | yes | transaction control |
| `close_sync()` | yes | closes the target's connection; also runs after a failed `connect_sync()` |
| `savepoint_sync(name)`, `release_savepoint_sync(name)`, `rollback_to_savepoint_sync(name)` | no | savepoints; raise `NotSupportedError` unless overridden |
| `set_options_sync(options, local)` | no | connection options, `None` values to their default; raises `NotSupportedError` for any option unless overridden |
| `prepare_call_sync()` | no | runs before every other `*_sync` method and every hook, in the same context copy, to prepare that context; an exception it raises is the call's; does nothing unless overridden |
| `listen_sync(channel)`, `unlisten_sync(channel)` | no | start and stop listening (`listen()`, `unlisten()`); raise `NotSupportedError` unless overridden |
| `idle_fileno()` | no | a file descriptor to watch while no call runs, `None` for none (the default); asked after every call, not watched once the connection is closed or unusable |
| `idle_sync()` | no | reads what arrived on `idle_fileno()`, such as notifications, and emits it; runs between calls when the descriptor is readable. An exception it raises makes the connection unusable, is logged and is emitted as `BackendEvent("connection_lost", payload=<the mapped error's text>)` |

`cancel_sync()` is the only method that runs on another thread, while a call is running. It asks the target to
cancel the running statement and returns once the request is delivered, without waiting for the call. The
connection's thread starts no further call until `cancel_sync()` returns, so the request cannot reach a later
statement. It may run during `connect_sync()`, before the target's connection exists. Does nothing unless
overridden.

Behaviour around those methods:

- The capability checks of `DriverConnection` run before anything is queued.
- An exception from a `*_sync` method is converted with `map_error()` on the thread. On a dedicated thread a
  `KeyboardInterrupt` or `SystemExit` becomes `InterfaceError`; on a direct connection it propagates. An error for
  which `is_connection_lost()` is true makes `is_usable()` return `False`.
- Cancelling the awaiting task: a call that has not started never runs; for a running call `cancel_sync()` runs
  on a short-lived thread of its own, and the task waits for the call to finish. A request method (`execute()`,
  `begin()`, `commit()`, `rollback()`, the savepoint methods, `set_options()`) then returns the call's result or
  raises its error, and raises `asyncio.CancelledError` when the call failed with an error `is_cancellation()` accepts
  (by default SQLSTATE `57014`); `start()` and
  `call_hook()` always raise `asyncio.CancelledError`. When the call does not finish within `cancel_grace`, the
  connection becomes unusable, the task raises `asyncio.CancelledError`, and further calls raise `InterfaceError`. Cancelling `open_connection()` during `connect_sync()` works the same
  way, then runs `close_sync()` and stops the thread.
- `cancel()` runs `cancel_sync()` for the running call: on a thread of its own when called on the event loop's
  thread, otherwise on the calling thread. It does nothing when no call is running.
- `close()` runs `close_sync()` on the thread and stops the thread; a call after it raises `InterfaceError`.
- Between calls, while `idle_fileno()` returns a descriptor, the thread waits for that descriptor or the next call,
  whichever comes first, and runs `idle_sync()` when the descriptor is readable; a queued call wakes it at once. A
  `direct` connection has the event loop watch the descriptor (`loop.add_reader()`) and runs `idle_sync()` on the
  loop's thread.
- `after_call_sync(fn)`, called from a `*_sync` method, runs `fn()` on the thread once that call has ended and its
  outcome is delivered, before the next call starts, in the call's context. `cancel()` never reaches it; an
  exception it raises is logged on the `adorm.drivers` logger. It is for work that must not decide or delay the call's
  outcome, such as Django's `on_commit()` callbacks after `commit_sync()`. From the delivery of the outcome until
  that work has run, `settled` is `False` and `settle()` waits for it.
- `call_hook(hook, *args)` runs `hook(self.target, *args)` on the thread like a `*_sync` method; a hook that returns
  an awaitable raises `InterfaceError`. A subclass sets `target` to what profile hooks receive, such as Django's
  connection wrapper.
- On a direct connection no running event loop is visible during a `*_sync` call, so a check against use from an
  event loop (Django's `async_unsafe`) accepts it. A running direct call is cancelled only through `cancel()` from
  another thread.

#### `emit(event)`

Reports an event to the slot. Callable from any thread; the event is delivered on the event loop, before the
result of the call that emitted it. The slot drops and logs any event other than `CallbackEvent` and `BackendEvent`,
and a `CallbackEvent` named `on_commit` or `on_rollback`
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
adorm queries, values and lookups into Django query sets and keyword arguments. Importing either imports Django;
operations need `django.setup()`. Usage: [Django driver topic](/topics/django-driver).

```python
from adorm.drivers.django import DjangoDriver
from adorm.drivers.django.translation import translate
```

### `adorm.drivers.django.DjangoDriver`

A [`ThreadedDriver`](#adorm-drivers-threadeddriver): `name` `"django"`, `mode` `"threaded"`, `connection_class`
[`DjangoConnection`](#adorm-drivers-django-djangoconnection), `capabilities`
[`CAPABILITIES`](#adorm-drivers-django-capabilities), what it supports on PostgreSQL. Created without arguments; a
profile names it by class, instance or import path. It runs on the database of any Django backend.

| Method | Does |
|---|---|
| `capabilities_for(profile)` | the capabilities of the database `profile`'s alias names: [`profile_vendor(profile)`](#adorm-drivers-django-profile-vendor-profile)`.capabilities()` ([vendors](#adorm-drivers-django-vendors)); `capabilities` for an alias without a `DATABASES` entry or whose backend does not load. Reads the backend class of the entry's `ENGINE`; does not connect |
| `is_refusal(exc)` | [`is_refusal(exc)`](#adorm-drivers-is-refusal-exc) or [`vendors.is_refusal(exc)`](#adorm-drivers-django-vendors-is-refusal-exc) (MySQL's and Oracle's errors of too many connections) |
| `database_key(profile)` | [`database_key(profile)`](#adorm-drivers-django-database-key-profile) |
| `async open_monitor(profile)` | a started [`DjangoMonitor`](#adorm-drivers-django-djangomonitor-profile); `NotSupportedError` for a database other than PostgreSQL, before anything opens |
| `async check_listen(profile)` | also `NotSupportedError` inside Django's `TestCase` |

`open_connection(profile, emit)` creates the connection with
[`adorm.testing.django.django_connection()`](/ref/testing#adorm-testing-django-django-connection-driver-profile-emit) and starts it:
a `connection_class` connection outside Django's test environment, one that shares the test's connection inside a
Django `TestCase` ([test mode](/topics/testing)).

`schema_hash(label)` is [`model_schema_hash(label)`](#adorm-drivers-django-model-schema-hash-label), the hash of the
installed Django model, which [`Driver.check_schema_hash()`](#check-schema-hash-label-expected) compares with the
generated model's.

```python
adorm.configure(profiles={"default": {"driver": "adorm.drivers.django.DjangoDriver", "alias": "default"}})
driver = DjangoDriver()
pool = Pool(driver, ConnectionProfile("default", driver), max_connections=10)
driver.schema_hash("clubs.Club")   # 'e8c2eed312ab7b68'
```

### `adorm.drivers.django.DjangoConnection`

The [`ThreadedConnection`](#adorm-drivers-threadedconnection) of `DjangoDriver`.

| Method | Does |
|---|---|
| `connect_sync()` | takes `django.db.connections[profile.alias]` of the connection's thread and its `vendor` (an `ENGINE` in `profile.params` other than the entry's raises `NotSupportedError`) ([`wrapper_vendor()`](#adorm-drivers-django-wrapper-vendor-db)); `profile.params` replace keys of that wrapper's `settings_dict` (the `DATABASES` entry itself is unchanged), and then `CONN_HEALTH_CHECKS` becomes `False`, `CONN_MAX_AGE` `None` and `OPTIONS["pool"]` is left out, so the connection is opened directly, never taken from Django's pool, and Django neither pings nor closes the connection the slot keeps; a connection of Django's pool that a `direct` driver's thread holds goes back to that pool first; `profile.connect(profile)`, when set, returns the DB-API connection that Django's `get_new_connection()` uses in place of the one it opens, and Django configures it (isolation level from `OPTIONS`, time zone, role); a psycopg 3 connection is opened with the wrapper's `get_connection_params()`, which carry Django's cursor class and type adapters; the vendor adjusts the settings ([`Vendor.slot_settings()`](#adorm-drivers-django-vendors-vendor-name-features-none)) and refuses those a slot cannot use (`check_settings()`, `NotSupportedError` before connecting); then `ensure_connection()` and `Vendor.connected()` (on SQLite: WAL and the progress handler). A thread's wrapper already connected with other `OPTIONS` (a `direct` driver's) is closed first |
| `execute_sync(operation)` | a read [`translate()`](#adorm-drivers-django-translation-translate-query)s the operation's query and evaluates it on `profile.alias`: `select` → list of rows, `get` → the row (Django's `get()`), `first` / `last` → the row or `None`, `count` → `int`, `exists` → `bool` (for a combined query, whether its first row exists), `aggregate` → a dict by name (Django's `aggregate()` on the query set of [`aggregation()`](#adorm-drivers-django-translation-aggregation-query-aggregations)); a write runs as the table below shows. Every statement through the wrapper's cursor is watched (`connection.execute_wrapper()`): unless, searching its callers upward, a frame of Django's query compiler or a database backend (Django's or the alias's backend package) comes before a signal dispatch, an `on_commit()` callback runner or the driver's own code, it calls [`mark_custom_sql()`](#mark-custom-sql) (raw SQL of a `post_save` receiver, `Manager.raw()`); frames of instrumentation wrapping the cursor are skipped. Django's own refusal of what the database cannot run, raised before any SQL (a `django.db.NotSupportedError` or a `DatabaseError` without a database library's error as its cause, such as a sliced query in a set operation on SQLite), raises `NotSupportedError` without `original`, which leaves an open transaction usable |
| `begin_sync()` | enters an outermost `transaction.atomic(using=profile.alias)` block, on SQLite waiting for the write lock (below); a second `begin`, or a wrapper already inside an atomic block (a `direct` driver's thread inside Django code's `atomic()`), raises `TransactionManagementError`. On the shared connection of a Django `TestCase` the block is a savepoint of the test's transaction ([`SharedConnectionMixin`](/ref/testing#adorm-testing-django-sharedconnectionmixin)) |
| `savepoint_sync(name)` | creates a Django savepoint (`savepoint()`) and adds it to the block's savepoint ids, so Django's `on_commit()` callbacks registered inside it belong to it; `name` already open, or no transaction, raises `TransactionManagementError` |
| `release_savepoint_sync(name)` | releases the savepoint and the savepoints opened after it |
| `rollback_to_savepoint_sync(name)` | rolls back to the savepoint, which stays open, discarding the savepoints opened after it and the `on_commit()` callbacks registered since; clears Django's `needs_rollback` first, since Django refuses every statement of a transaction marked for rollback |
| `commit_sync()` | leaves the block, which commits, and hands Django's `on_commit()` callbacks to `after_call_sync()` (see below), watched for custom SQL as in `execute_sync()`; raises `TransactionManagementError` and rolls back when Django marked the transaction for rollback or the database transaction failed, `InterfaceError` when the connection was closed inside it |
| `rollback_sync()` | leaves the block with a rollback; nothing without an open transaction |
| `transaction_ended` | whether the database ended the open transaction on its own: SQLite after an interrupted `INSERT`, `UPDATE` or `DELETE` (`Vendor.transaction_ended()`, checked after every failed operation). Until the rollback, operations, savepoint methods and `commit_sync()` raise `OperationalError` [`TRANSACTION_ENDED`](#adorm-drivers-django-vendors-transaction-ended) without running SQL; `commit_sync()` leaves the block first |
| `set_options_sync(options, local)` | [`set_options_sql(options, local)`](#adorm-drivers-sql-set-options-sql-options-local) on a cursor of the wrapper, one `execute()` without parameters; nothing for no options |
| `cancel_sync()` | [`vendor.cancel()`](#adorm-drivers-django-vendors-vendor-name-features-none) on the DB-API connection under the wrapper: psycopg's `cancel_safe()` (`cancel()` without it), SQLite's `interrupt()` and the end of a wait for the write lock, MySQL's `KILL QUERY` on a connection of its own, Oracle's `cancel()`; nothing for another backend and before the connection exists |
| `close_sync()` | the wrapper's `close()`; inside a transaction it also leaves the atomic block, so the wrapper keeps no transaction state for the next connection of its thread (a `direct` driver's); then the wrapper gets back the settings, DB-API module, health check flag and closing time it had before `connect_sync()` |
| `prepare_call_sync()` | [`adopt_context_locals()`](#adorm-drivers-django-adopt-context-locals), then reads `backend_pid` and, for a DB-API connection of psycopg 3 it has not seen, adds the notice and notify handlers that emit `NOTICE` ([`notice_event()`](#adorm-drivers-django-notice-event-diagnostic)) and `NOTIFY` (also in `connect_sync()`) |
| `listen_sync(channel)`, `unlisten_sync(channel)` | [`listen_sql(channel)`](#adorm-drivers-postgresql-listen-sql-channel) or `unlisten_sql(channel)` on a cursor of the wrapper; with psycopg 2 `listen_sync()` raises `NotSupportedError` |
| `idle_fileno()` | the socket of the psycopg 3 connection while it listens on a channel |
| `idle_sync()` | reads the socket's input (`pgconn.consume_input()`) and hands each notification to psycopg's notify handler; a connection libpq reports as bad raises `OperationalError` |
| `backend_pid` | `vendor.backend_pid()` of the DB-API connection (PostgreSQL's process id, MySQL's connection id, `None` elsewhere), read on the connection's thread when it connects and before each call |
| `map_error(exc)` | [`map_django_error(exc, vendor)`](#adorm-drivers-django-map-django-error-exc-vendor-none) |
| `is_connection_lost(exc)`, `is_cancellation(exc)` | those of `vendor`: on SQLite only an `InterfaceError` loses the connection and `SQLITE_INTERRUPT` or a cancelled lock wait is a cancellation; MySQL's error numbers (2006, 2013, 2055 lost; 1317 cancelled); Oracle's `ORA-` numbers (`ORA-03113` and others lost; `ORA-01013` cancelled) |
| `vendor` | the connection's [`Vendor`](#adorm-drivers-django-vendors-vendor-name-features-none) |
| `waiting_for_write_lock` | whether the running call waits for SQLite's write lock; readable from any thread |

| Write | Django call | Result |
|---|---|---|
| `create` | `create(**values)` of the manager `manager` names (`_base_manager` without one), through `db_manager(profile.alias)` | [`instance_row()`](#adorm-drivers-django-translation-instance-row-instance) of the new instance |
| `bulk_create` | [`check_bulk_create()`](#adorm-drivers-django-translation-check-bulk-create-operation), then `bulk_create(objs, **bulk_create_options(operation))` of the manager `manager` names (`_base_manager` without one), through `db_manager(profile.alias)`; `objs` are `Model(**values)` per row | a list of `instance_row()` of each instance, in order |
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

Django's `on_commit()` callbacks registered during a transaction (by a `save()` override, a signal receiver):

- run on the connection's thread, in registration order, after `commit()` has returned and before the connection's
  next call ([`after_call_sync()`](#adorm-drivers-threadedconnection));
- are not reached by cancelling the task that awaits the commit; the slot's `CallbackEvent("on_commit")` does not
  wait for them;
- are logged on the `adorm.drivers.django` logger when they raise, and the commit stands; one registered without
  `robust=True` stops the callbacks after it, as in Django;
- do not run for a rolled-back savepoint or transaction.

Outside a transaction Django runs a callback at once, inside the operation that registers it.

On SQLite ([`vendors.SQLite`](#adorm-drivers-django-vendors-sqlite)) every transaction begins with `BEGIN IMMEDIATE`,
and a write operation outside a transaction (`create`, `bulk_create`, `save`, `update`, `delete`, `get_or_create`,
`update_or_create`, a driver method call) runs in a transaction of its own that begins so: the write lock is taken
before the first statement. While another connection holds it, the connection tries again after pauses of 1 ms
growing to 100 ms, with SQLite's busy timeout set to 0 meanwhile, until the busy timeout of the `DATABASES` entry
(`OPTIONS["timeout"]`, 5 seconds by default) passes; then the operation raises Django's `OperationalError`
`database is locked`. `waiting_for_write_lock` is true meanwhile. A cancellation ends the wait at once. A driver
method outside a transaction holds the write lock for its whole call, whether it writes or not.

A [`CheckTable`](/ref/operations#adorm-operations-checktable-model-fields-many-to-many) operation reads the
catalogue through the wrapper's cursor outside the execute wrapper (not custom SQL). The expected tables come
from [`expected_tables(operation, self.db, driver.name, check_types=...)`](#adorm-drivers-django-expected-tables-operation-connection-driver-none-check-types-true), each table's
columns from `vendor.columns()`: [`columns_sql("%s")`](#adorm-drivers-postgresql-columns-sql-parameter) on PostgreSQL,
Django's introspection (`table_names()`, `get_table_description()`) elsewhere. Column types are compared on PostgreSQL
and SQLite, whose catalogues report a column's type as Django declared it; on other databases only the columns and
their nullability. The result is the
[`table_problems()`](#adorm-drivers-postgresql-table-problems-table-rows) of every table after the `unknown_field`
problems. A label without an installed Django model raises `SchemaMismatchError` before any statement; an open
transaction stays usable.

`target` (what profile hooks receive) is the wrapper, `self.db`; `alias` is `profile.alias`. `is_usable()` is also
false once the `TestCase` atomic blocks on the test's connection differ from those open when the connection was opened
([`TestDatabase.current()`](/ref/testing#adorm-testing-django-testdatabase)): after the `TestCase` test it was opened
in, or, for one opened in another Django test, while a `TestCase` transaction is open.

### `adorm.drivers.django.DjangoMonitor(profile)`

The [`DatabaseMonitor`](#adorm-drivers-databasemonitor) of `DjangoDriver`, opened by `open_monitor(profile)`: a
dedicated Django connection wrapper on a dedicated thread, for the `DATABASES` entry `profile.alias` with
`profile.params` and the `connect` hook applied as in `DjangoConnection.connect_sync()`. Inside a Django test it
connects to the test database; inside a `TestCase` it does not share the test's connection.
The constructor opens nothing; `async start()` opens the wrapper's connection on the monitor's thread and closes the
monitor on failure. `open_monitor()` calls both.
`activity(pids)` runs [`activity_sql("%s")`](#adorm-drivers-postgresql-activity-sql-parameter) on the wrapper's cursor;
errors go through `map_django_error()`. An alias without an entry raises `ProgrammingError`, a backend other than
PostgreSQL `NotSupportedError`: the other databases have no session catalogue the monitor reads.

```python
async with await DjangoDriver().open_monitor(ConnectionProfile("default", driver)) as monitor:
    await monitor.activity([pid])
```

### `adorm.drivers.django.notice_event(diagnostic)`

The [`NOTICE`](/ref/events#adorm-events-notice) backend event of a psycopg 3 notice (`psycopg.errors.Diagnostic`):
`channel` is its untranslated severity (`severity_nonlocalized`, else `severity`), `payload`
[`notice_payload()`](#adorm-drivers-postgresql-notice-payload-severity-sqlstate-message-detail-hint) of its fields.

```python
notice_event(diagnostic)
# BackendEvent(name='notice', channel='WARNING', payload={'severity': 'WARNING', 'sqlstate': '01000',
#              'message': 'check the clubs', 'detail': None, 'hint': None})
```

### `adorm.drivers.django.backend_pid(db)`

The server's id of the session of the DB-API connection under Django's wrapper `db`: PostgreSQL's process id
(psycopg 3's `info.backend_pid`, psycopg 2's `get_backend_pid()`), MySQL's connection id (`thread_id()`); `None`
while the wrapper has no connection and on other databases.

### `adorm.drivers.django.profile_vendor(profile)`

The [`Vendor`](#adorm-drivers-django-vendors-vendor-name-features-none) of the database `profile` names: of the backend
of its alias's `DATABASES` entry ([`engine_vendor()`](#adorm-drivers-django-vendors-engine-vendor-engine)); a
profile's `params` do not change the backend (`DjangoConnection` refuses an `ENGINE` among them). `None` for an alias
without an entry or a backend that does not load. Does not connect.

```python
profile_vendor(ConnectionProfile("local", DjangoDriver, alias="local"))   # DATABASES["local"] is SQLite
# <SQLite 'sqlite'>
```

### `adorm.drivers.django.wrapper_vendor(db)`

The [`Vendor`](#adorm-drivers-django-vendors-vendor-name-features-none) of Django's connection wrapper `db`: by its
`vendor`, with its features class for a backend without a vendor of its own.

### `adorm.drivers.django.CAPABILITIES`

The [`Capabilities`](#adorm-drivers-capabilities) of `DjangoDriver` on PostgreSQL, with psycopg 3. The constructs
are those Django's PostgreSQL backend supports (`clause.distinct_on`, `contains` on array and JSON fields); the
other databases have the [capabilities of their vendor](#adorm-drivers-django-vendors).

| Field | Value |
|---|---|
| `operations` | `select`, `get`, `first`, `last`, `count`, `exists`, `aggregate`, `in_bulk`, `create`, `bulk_create`, `create_from_parent`, `save`, `update`, `delete`, `delete_instance`, `get_or_create`, `update_or_create`, `full_clean`, `check_table` |
| `constructs` | `lookup.<name>` for `exact`, `iexact`, `gt`, `gte`, `lt`, `lte`, `in`, `range`, `isnull`, `contains`, `icontains`, `startswith`, `istartswith`, `endswith`, `iendswith`, `regex`, `iregex`, `overlap`; `path.nested`; `clause.annotate`, `clause.alias`, `clause.group_by`, `clause.distinct`, `clause.distinct_on`, `clause.only`, `clause.defer`, `clause.select_related`, `clause.manager`, `clause.union`, `clause.intersection`, `clause.difference`, `clause.select_for_update` with `.nowait`, `.skip_locked`, `.of`, `.no_key`; `clause.on_conflict.ignore`, `clause.on_conflict.update`; `function.<name>` for `Lower`, `Upper`, `Length`, `Coalesce`, `Concat`, `Now`, `Trunc` and every `Trunc<kind>`; `aggregate.<name>` for `Count`, `Sum`, `Avg`, `Min`, `Max`, with `aggregate.distinct` and `aggregate.filter`; `expression.Case`, `expression.Subquery`, `expression.Exists`, `expression.OuterRef`, `expression.CombinedExpression` |
| `savepoints`, `cancellation` | `True` |
| `connection_options` | [`POSTGRESQL_OPTIONS`](#adorm-drivers-sql-postgresql-options): `statement_timeout`, `lock_timeout`, `idle_in_transaction_session_timeout` |
| `backend_events` | `notify`, `notice`, `connection_lost` |
| `max_channel_length` | [`MAX_IDENTIFIER_LENGTH`](#adorm-drivers-postgresql-max-identifier-length) (63) |

`DjangoDriver.capabilities` is `CAPABILITIES` when Django uses psycopg 3 and
[`PSYCOPG2_CAPABILITIES`](#adorm-drivers-django-psycopg2-capabilities) with psycopg 2.

### `adorm.drivers.django.PSYCOPG2_CAPABILITIES`

`CAPABILITIES` without `backend_events`: with psycopg 2 the Django driver emits no backend events and `adorm.listen()`
raises `NotSupportedError` before it takes a slot.

### `adorm.drivers.django.READ_CAPABILITIES`

The reads of [`CAPABILITIES`](#adorm-drivers-django-capabilities): `operations` `select`, `get`, `first`, `last`,
`count`, `exists`, `aggregate`, `in_bulk`, the `constructs` without `clause.select_for_update[...]`, and
`cancellation`; no savepoints.

### `adorm.drivers.django.model_row(instance)`

Returns a Django model instance as a [`ModelRow`](./operations#adorm-operations-modelrow-model-row): the model's label
and [`instance_row(instance)`](#adorm-drivers-django-translation-instance-row-instance).

```python
model_row(Author.objects.only("name").get(slug="ann"))
# ModelRow(model='library.Author', row={'id': 1, 'name': 'Ann'})
```

### `adorm.drivers.django.map_django_error(exc, vendor=None)`

Returns the adorm exception for `exc`, raised by Django or its database library on a database of `vendor` (a
[`Vendor`](#adorm-drivers-django-vendors-vendor-name-features-none); PostgreSQL's by default), with `exc` as
`__cause__`:

| `exc` | Result |
|---|---|
| `django.core.exceptions.ObjectDoesNotExist` (a model's `DoesNotExist`) | `ObjectDoesNotExist` with Django's message |
| `django.core.exceptions.MultipleObjectsReturned` | `MultipleObjectsReturned` with Django's message |
| `FieldError`, `FieldDoesNotExist` | [`FieldError`](/ref/exceptions#adorm-fielderror-message-none-model-none-key-none-original-none-sqlstate-none) with Django's message (`original` is `exc`) |
| Django's `ValidationError` | [`validation_error(None, exc)`](#adorm-drivers-django-translation-validation-error-label-exc): each error with its message, `code` and `params`, by field as Django keys them, a list, or one error |
| `django.db.transaction.TransactionManagementError` | `TransactionManagementError` |
| `SynchronousOnlyOperation` | `InterfaceError` |
| `django.db.models.ProtectedError`, `RestrictedError` | `ProtectedError`, `RestrictedError` with Django's message and the objects as [`model_row()`](#adorm-drivers-django-model-row-instance) values (`protected_objects`, `restricted_objects`) |
| anything else | [`wrap_driver_error(exc, details)`](/ref/exceptions#adorm-exceptions-wrap-driver-error-exc-details-none): the SQLSTATE of the psycopg error a `django.db` error carries, then the PEP 249 class name of Django's exception (SQLite, MySQL and Oracle report no SQLSTATE: `sqlstate` is `None`), else `DatabaseError`; an `IntegrityError` with the violation `vendor.integrity_details(exc)` reads (on PostgreSQL [`integrity_details(exc)`](#adorm-drivers-postgresql-integrity-details-exc)), the label of the model whose table it names as `model` ([`table_model()`](#adorm-drivers-django-translation-table-model-table)) and that model's adorm field names of the key's columns as `fields` ([`column_fields()`](#adorm-drivers-django-translation-column-fields-model-columns)), except for the key of a referenced row ([`references_key()`](#adorm-drivers-postgresql-references-key-detail)) |

```python
map_django_error(Author.DoesNotExist("Author matching query does not exist."))
# ObjectDoesNotExist('Author matching query does not exist.')

exc = map_django_error(unique_violation)  # django.db.IntegrityError of library_reservation_one_per_copy
(exc.model, exc.columns, exc.fields)
# ('library.Reservation', ('book_id', 'copy'), ('book_id', 'copy_'))
```

### `adorm.drivers.django.adopt_context_locals()`

Re-binds the values of asgiref `Local` objects in the current context (Django's active language and time zone) to
the current thread. asgiref 3.13 and later tag such a value with the thread that set it; in a context copied from an
event loop task and run on another thread it reads as unset until this call. Untagged values (earlier asgiref
versions) and other context variables are unchanged. The `django` and `asyncpg` extras require asgiref 3.x.
`DjangoConnection.prepare_call_sync()` calls it before every call.

```python
with translation.override("sl"):
    context = contextvars.copy_context()

def read():
    adopt_context_locals()
    return translation.get_language()

threading.Thread(target=context.run, args=(read,)).start()     # read() returns "sl"
```

### `adorm.drivers.django.clear_content_types_on_migrate()`

Connects Django's `post_migrate` signal to [`adorm.content_types.clear()`](./models#adorm-contenttypes), once per
process (`dispatch_uid="adorm.content_types"`). `migrate` and `flush` (run by a `TransactionTestCase` after each test)
recreate the content type rows with other ids. `DjangoDriver.open_connection()` and
`AsyncpgDriver.open_connection()` call it.

```python
clear_content_types_on_migrate()
call_command("migrate")          # post_migrate: adorm.content_types forgets every cached id
```

### `adorm.drivers.django.database_key(profile)`

The database of the `DATABASES` entry `profile.alias` with `profile.params` replacing its keys,
[`Vendor.database_key(entry)`](#adorm-drivers-django-vendors-vendor-name-features-none): `(vendor, host, port, name)`
(`HOST` and `PORT` as strings, `""` when not set), on SQLite `("sqlite", path)` with the file's real path (a
`file:` URI as written). `None` for a profile with a `connect` hook, an alias without an entry, a
backend that does not load, an in-memory SQLite database and a `NAME` that is not hashable. The database key of `DjangoDriver` and
`AsyncpgDriver` ([`Driver.database_key()`](#database-key-profile)).

```python
database_key(ConnectionProfile("reports", DjangoDriver(), alias="default", on_acquire=set_role))
# ("postgresql", "localhost", "5432", "club"), the same as for the profile "default"
```

### `adorm.drivers.django.model_schema_hash(label)`

The schema hash of the installed Django model `label`:
[`adorm.codegen.django.schema_hash()`](/ref/codegen#adorm-codegen-django-schema-hash-model-warn-true) with
`warn=False`: no `CodegenWarning`, the process's warning filters untouched; callable from any thread. Raises `LookupError` when no such model is installed and `CodegenError` for a model the generator cannot
describe. Defined in `adorm.drivers.django.schema`; the `schema_hash()` of `DjangoDriver` and
`AsyncpgDriver`.

```python
from adorm.drivers.django import model_schema_hash

model_schema_hash("clubs.Club") == Club._meta.schema_hash   # True while the generated module is up to date
```

### `adorm.drivers.django.expected_tables(operation, connection, driver=None, *, check_types=True)`

What the [`CheckTable`](/ref/operations#adorm-operations-checktable-model-fields-many-to-many) `operation` expects of
the database, for the Django database wrapper `connection`: a list of
[`ExpectedTable`](#adorm-drivers-postgresql-expectedtable-name-regclass-columns) values, and a list of
`unknown_field` problem dicts for the names in `operation.fields` that are no concrete field of the model (looked up by
name or attname) and in `operation.many_to_many` that are no many-to-many relation of it (a reverse one named by its
accessor name).

- A column belongs to the table of the model that declares its field: an inherited field of a multi-table
  inheritance child is in the parent's table.
- Each column's type is the field's `db_type(connection)` (no connection is opened), `None` with `check_types` false;
  it takes `NULL` when the field has `null=True` and is not the primary key.
- A many-to-many relation adds its automatic link table with every column, the primary key without a type (adorm
  neither reads nor writes it); an explicit link model is checked as a model of its own.

The table names are quoted with `connection.ops.quote_name()`. Raises
[`SchemaMismatchError`](/ref/exceptions#adorm-schemamismatcherror-message-none-model-none-expected-none-actual-none-driver-none-reason-none)
with `actual` `None` when no such model is installed, naming `driver` in it. Defined in `adorm.drivers.django.schema`.

```python
tables, problems = expected_tables(CheckTable("library.Book", ("isbn", "title"), ("authors",)), connection)
[table.name for table in tables]   # ['library_book', 'library_book_authors']
tables[0].columns[1]               # ExpectedColumn(name='title', type='varchar(200)', nullable=False, field='title')
```

### `adorm.drivers.django.vendors`

What the Django driver does per database backend, by Django's `DatabaseWrapper.vendor`. A
[`DjangoConnection`](#adorm-drivers-django-djangoconnection) holds the `Vendor` of its wrapper, and
[`DjangoDriver.capabilities_for()`](#adorm-drivers-django-djangodriver) returns the `Vendor`'s capabilities of a
profile's database.

| Vendor | Capabilities | Cancellation | Errors | Tested |
|---|---|---|---|---|
| `postgresql` | [`CAPABILITIES`](#adorm-drivers-django-capabilities) (psycopg 2: `PSYCOPG2_CAPABILITIES`) | psycopg's cancel request | SQLSTATE; psycopg's diagnostics | in CI |
| `sqlite` | [`SQLITE_CAPABILITIES`](#adorm-drivers-django-vendors-sqlite-capabilities) | `interrupt()`, a progress handler, the end of a lock wait | `sqlite3` result codes; the table and columns of the constraint message | in CI (`tests.djangoproject.settings_sqlite`) |
| `mysql` (MySQL, MariaDB) | [`MYSQL_CAPABILITIES`](#adorm-drivers-django-vendors-mysql-capabilities) | `KILL QUERY` on a connection opened per cancellation | MySQL's error numbers; key, column and constraint from the message | no |
| `oracle` | [`ORACLE_CAPABILITIES`](#adorm-drivers-django-vendors-oracle-capabilities) | the driver's `cancel()` | `ORA-` numbers; constraint, table and column from the message | no |
| any other | [`features_capabilities()`](#adorm-drivers-django-vendors-features-capabilities-features) of its features class | none | PEP 249 class names; no details | no |

```python
from adorm.drivers.django.vendors import engine_vendor

engine_vendor("django.db.backends.sqlite3").capabilities() == SQLITE_CAPABILITIES   # True
```

#### `adorm.drivers.django.vendors.Vendor(name="", features=None)`

The driver's behaviour on databases of the vendor `name`. The class itself serves backends without a vendor of their
own; `features` is the backend's `DatabaseFeatures` class.

| Member | Does |
|---|---|
| `name` | Django's vendor name (`DatabaseWrapper.vendor`) |
| `capabilities()` | what the driver supports: here [`features_capabilities(features)`](#adorm-drivers-django-vendors-features-capabilities-features) |
| `slot_settings(settings)` | the `DATABASES` entry a slot's connection opens with; here unchanged |
| `check_settings(settings)` | raises `NotSupportedError` for an entry a slot cannot use; here nothing |
| `connected(connection)` | configures the DB-API connection of a [`DjangoConnection`](#adorm-drivers-django-djangoconnection) once it is open; here nothing |
| `cancel(connection, raw)` | cancels the statement running on the DB-API connection `raw`; here nothing |
| `backend_pid(raw)` | the server's id of the session; here `None` |
| `is_cancellation(exc)` | whether a mapped error is a cancelled statement's; here `False` |
| `is_connection_lost(exc)` | whether a mapped error leaves the connection unusable; here [`is_connection_error(exc)`](#adorm-drivers-is-connection-error-exc) |
| `integrity_details(exc)` | the keyword arguments of `IntegrityError` the error gives; here `{}` |
| `database_key(entry)` | `(name, HOST, PORT, NAME)` of the `DATABASES` entry |
| `transaction_ended(raw)` | whether the database ended the transaction open on the DB-API connection `raw` on its own after a failed statement; here `False` |
| `check_types()` | whether the schema check compares column types; here `False` |
| `columns(db, cursor, table)` | the rows of [`table_problems()`](#adorm-drivers-postgresql-table-problems-table-rows) for an [`ExpectedTable`](#adorm-drivers-postgresql-expectedtable-name-regclass-columns), read with Django's introspection; the type is `None` unless `check_types()` |
| `writes_wait_for_lock` | whether writes take the database's write lock when they begin; here `False` |

#### `adorm.drivers.django.vendors.PostgreSQL`

`CAPABILITIES` (`PSYCOPG2_CAPABILITIES` with psycopg 2); cancellation with psycopg's `cancel_safe()` (`cancel()` on
psycopg 2); `backend_pid` the server process; SQLSTATE `57014` is a cancellation; integrity details from psycopg's
diagnostics; column types compared, the columns read with [`columns_sql()`](#adorm-drivers-postgresql-columns-sql-parameter).

#### `adorm.drivers.django.vendors.SQLite`

SQLite allows one writer at a time beside any number of readers.

- `slot_settings()` sets `OPTIONS["transaction_mode"]` to `IMMEDIATE` (an `EXCLUSIVE` one stays): every transaction
  takes the write lock when it begins. `check_settings()` raises `NotSupportedError` before Django 5.1, which has no
  `transaction_mode`, and for an in-memory database (`":memory:"`, `mode=memory`), which has no WAL and, unless
  it is a shared cache, is visible to its own connection only.
- `connected()` switches the database to WAL (`PRAGMA journal_mode=WAL`, kept in the database file), so reads run
  beside the writer, and installs a progress handler that stops the statements of a
  cancelled call every [`PROGRESS_STEPS`](#adorm-drivers-django-vendors-sqlite) (1000) virtual machine instructions.
- `writes_wait_for_lock`: a write outside a transaction runs in one of its own; `take_write_lock(connection,
  atomic)` enters the outermost atomic block and retries while another connection holds the lock (`SQLITE_BUSY`,
  `SQLITE_LOCKED`), until the connection's busy timeout (`PRAGMA busy_timeout`) passes, with it set to 0 meanwhile
  and restored afterwards.
- `cancel()` ends a wait for the write lock (`OperationalError` [`LOCK_WAIT_CANCELLED`](#adorm-drivers-django-vendors-lock-wait-cancelled))
  and calls `interrupt()`; `interrupt()` before a statement starts is lost, the progress handler stops it.
- `is_cancellation()`: `SQLITE_INTERRUPT` or `LOCK_WAIT_CANCELLED`. `is_connection_lost()`: never an
  `OperationalError` (a lock, a full disk), as SQLite has no server connection. `transaction_ended()`: the
  connection is no longer in a transaction, as after an interrupted `INSERT`, `UPDATE` or `DELETE`, which rolls back
  the whole transaction.
- `integrity_details()`: [`sqlite_integrity_details()`](#adorm-drivers-django-vendors-sqlite-integrity-details-message).
- `database_key()`: `("sqlite", realpath(NAME))`, `("sqlite", NAME)` for a `file:` URI; `None` for an in-memory database.
- `check_types()`: `True`; SQLite's catalogue keeps a column's declared type.

#### `adorm.drivers.django.vendors.MySQL`

MySQL and MariaDB. `cancel()` opens a connection, with the profile's `connect` hook when it has one, else with the
wrapper's `get_connection_params()`, runs `KILL QUERY <thread_id>` and closes it; `backend_pid` is the connection id. Error 1317 is a cancellation; 2006, 2013 and 2055 a
lost connection, other operational errors not. `integrity_details()` reads the key name and value of a duplicate
entry (1062), the column of a null violation (1048) and the table, constraint and columns of a foreign key (1451,
1452). Untested: no CI service runs MySQL.

#### `adorm.drivers.django.vendors.Oracle`

`cancel()` is the driver's `cancel()`. `ORA-01013` is a cancellation; `ORA-00028`, `ORA-01012`, `ORA-03113`,
`ORA-03114`, `ORA-03135`, `ORA-12537`, `ORA-12570` and python-oracledb's `DPY-1001`, `DPY-4011` a lost connection.
`integrity_details()` reads the owner and constraint of `ORA-00001`, `ORA-02290`, `ORA-02291`, `ORA-02292` and the
table and column of `ORA-01400`. Untested.

#### `adorm.drivers.django.vendors.SQLITE_CAPABILITIES`

`CAPABILITIES` without `clause.select_for_update` (and its options), the [`POSTGRESQL_ONLY`](#adorm-drivers-django-vendors-postgresql-only)
constructs, `lookup.contains`, `lookup.startswith` and `lookup.endswith` (SQLite's `LIKE` ignores the case of ASCII
letters), connection options and backend events. It has `clause.on_conflict.ignore` and `.update`, the set
operations, savepoints and cancellation.

#### `adorm.drivers.django.vendors.MYSQL_CAPABILITIES`

`CAPABILITIES` without the `POSTGRESQL_ONLY` constructs, `clause.select_for_update.of` (MariaDB has none; the vendor
name does not tell MariaDB apart without connecting), `clause.select_for_update.no_key`, `clause.on_conflict.update`
(`ON DUPLICATE KEY UPDATE` takes no conflict target), `clause.intersection`, `clause.difference` (MySQL before
8.0.31), connection options and backend events.

#### `adorm.drivers.django.vendors.ORACLE_CAPABILITIES`

`CAPABILITIES` without the `POSTGRESQL_ONLY` constructs, `clause.select_for_update.no_key`, `clause.on_conflict.ignore`,
`clause.on_conflict.update`, connection options and backend events.

#### `adorm.drivers.django.vendors.POSTGRESQL_ONLY`

`lookup.overlap` and `clause.distinct_on`: array overlap and `DISTINCT ON`.

#### `adorm.drivers.django.vendors.LOCK_WAIT_CANCELLED`

The message of the `OperationalError` a write on SQLite ends with when its call is cancelled while it waits for the
write lock; the caller gets `CancelledError`.

#### `adorm.drivers.django.vendors.TRANSACTION_ENDED`

The message of the `OperationalError` every request of a transaction that SQLite rolled back on its own raises, until
the rollback ([`DjangoConnection.transaction_ended`](#adorm-drivers-django-djangoconnection)).

#### `adorm.drivers.django.vendors.vendor(name, features=None)`

The `Vendor` of Django's vendor name `name`, one instance per name: `PostgreSQL`, `SQLite`, `MySQL`, `Oracle`; for
another name a `Vendor` with the capabilities of the `DatabaseFeatures` class `features`.

#### `adorm.drivers.django.vendors.engine_vendor(engine)`

The `Vendor` of the Django backend `engine` (a `DATABASES` entry's `ENGINE`), from its `DatabaseWrapper` class without
connecting. Raises `ImportError` or `ImproperlyConfigured` for a backend that does not load.

#### `adorm.drivers.django.vendors.features_capabilities(features)`

The capabilities of a backend without a vendor of its own, from its `DatabaseFeatures` class: `CAPABILITIES` without
connection options, backend events, cancellation and the `POSTGRESQL_ONLY` constructs; a row lock, `ON CONFLICT` and
set operation only when its flag (`has_select_for_update`, `has_select_for_update_nowait`, …,
`supports_ignore_conflicts`, `supports_update_conflicts_with_target`, `supports_select_union`, …) is `True` on the class; savepoints
when `uses_savepoints` is. A flag the backend computes per connection (a property) counts as `False`.

```python
features_capabilities(load_backend("django_cockroachdb").DatabaseWrapper.features_class)
```

#### `adorm.drivers.django.vendors.is_refusal(exc)`

Whether `exc`, raised while opening a connection, is MySQL's `ER_CON_COUNT_ERROR` (1040) or
`ER_TOO_MANY_USER_CONNECTIONS` (1203), or Oracle's `ORA-00018`, `ORA-12516`, `ORA-12519` or `ORA-12520`.

#### `adorm.drivers.django.vendors.sqlite_error_code(exc)`

The primary result code (`sqlite_errorcode & 0xFF`) of the first `sqlite3` exception on `exc`'s chain (`original` of
an adorm error, else `__cause__`); `None` without one.

#### `adorm.drivers.django.vendors.sqlite_integrity_details(message)`

The keyword arguments of `IntegrityError` that SQLite's constraint message gives: `detail` the message; for a `UNIQUE`
or `NOT NULL` violation `table_name` and `columns` (not for an expression index, `index 'name'`); for a `CHECK`
violation `constraint_name`. SQLite reports no values and does not name the table of a foreign key.

```python
sqlite_integrity_details("UNIQUE constraint failed: clubs_membership.club_id, clubs_membership.member_id")
# {'detail': '...', 'table_name': 'clubs_membership', 'columns': ('club_id', 'member_id')}
```

### `adorm.drivers.django.translation.translate(query)`

Returns the [`TranslatedQuery`](#adorm-drivers-django-translation-translatedquery) of `query`, a
[`Query`](/ref/query). Opens no database connection.

- The query set starts from the Django manager named by `Query.manager`, or from the model's `_base_manager` without
  one. A manager the model lacks raises `ProgrammingError`.
- Annotations (Django's `annotate()`), aliases (`alias()`), conditions (a lookup on an expression through an
  `alias()` added before the expression is used), ordering (the model's `Meta.ordering` when the query has none and
  is not grouped), `distinct()` with or without fields, the slice and `none()` follow as in Django.
- A grouped query (`Query.group_by`) becomes `annotate()` of the annotations without an aggregate, `values()` of the
  grouping names and `annotate()` of the annotations with one, as Django groups `values().annotate()`.
- Each of `Query.filters` is one Django `filter()` call, so a multi-valued relation is joined once per filter, as
  in Django.
- Field paths go through [`orm_path()`](#adorm-drivers-django-translation-orm-path-model-path-annotations); a model
  instance as a value stands for its primary key, or, compared with a foreign key whose `to_field` is another field,
  for that field's value; a `Value` with `output_type` gets
  [`output_field(output_type)`](#adorm-drivers-django-translation-output-field-output-type); a query in an `in`
  lookup becomes a Django query set ([`subquery()`](#adorm-drivers-django-translation-subquery-query)).
- Arithmetic (`CombinedExpression`) becomes Django's `CombinedExpression` with the same connector (`%` as Django's
  `MOD`), its `output_type` as the output field.
- Functions in [`FUNCTIONS`](#adorm-drivers-django-translation-functions) and `Trunc` (Django's `Trunc`, or
  `TruncDate` / `TruncTime` for those kinds), aggregates in
  [`AGGREGATES`](#adorm-drivers-django-translation-aggregates) (with `distinct`, `filter` and `default`; `Count("*")`
  counts rows), `Case` and `When`, `Subquery` and `Exists` (`~Exists` as Django's negated `Exists`) become Django's
  classes. An `output_type` that differs from the type the class implies becomes the `output_field`. An `OuterRef`
  inside a subquery becomes Django's `OuterRef` with the path translated on the enclosing query's model; outside a
  subquery it raises `ProgrammingError`.
- `select_for_update()` becomes Django's `select_for_update()` with the same options; the paths in `of` are
  translated, `"self"` stays. Django evaluates it only inside a transaction.
- A set operation becomes Django's `union()` (with `all`), `intersection()` or `difference()` of the translated
  combined queries, at the level of the query itself (an `OuterRef` in them refers to the enclosing query), then
  `order_by()` and the slice; a `none()` combination becomes the first query set's `none()`. The ordering is translated
  on the model of the first combined query that is not itself combined; after `values()` or `values_list()`, a key is
  a name those methods give, mapped to the first query's column at the same position, and another key raises
  `DatabaseError` (`ORDER BY term does not match any column in the result set.`). A combined query that selects model rows selects the fields of `values()` or `values_list()`
  given after the set operation; the rows are built as the first combined query's rows, keyed by those names column
  by column (a different number of names raises `ProgrammingError`). As a subquery, a combined query of model rows
  selects its primary key.
- A node without a translation (a function or aggregate class of a driver's own, such as a `Func` subclass a driver adds) raises
  `NotSupportedError`; a `select_related()` path that is not a relation of the adorm model, or not a foreign key or one-to-one relation,
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
segment Django does not know is kept for Django to report.

```python
orm_path(Note, "copy___title")          # "copy__title"
orm_path(Book, "tagging_set__weight")   # "tagging__weight"
orm_path(Placement, "order_")           # "_order"
```

### `adorm.drivers.django.translation.model_queryset(query)`

Returns the Django query set of the model rows of `query`, built as `translate()` builds it (manager, annotations,
filters, ordering, distinct, slice, `none()`, `select_for_update()`) without selecting columns. The Django driver runs
`update()`, `delete()`, `get_or_create()` and `update_or_create()` on it; a manager's `get_queryset()` and its query
set's overrides apply.

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

### `adorm.drivers.django.translation.bulk_create_options(operation)`

Returns the keyword arguments of Django's `bulk_create()` for a
[`BulkCreate`](/ref/operations#adorm-operations-bulkcreate-model-rows-batch-size-none-ignore-conflicts-false-update-conflicts-false-update-fields-none-unique-fields-none-manager-none)
operation: `batch_size`, `ignore_conflicts`, `update_conflicts`, and `update_fields` and `unique_fields` with each
adorm field name translated by the model's `orm_names` (`"pk"` kept), or `None`.

```python
bulk_create_options(BulkCreate("library.Note", [{}], update_conflicts=True, update_fields=["json_"], unique_fields=["pk"]))
# {"batch_size": None, "ignore_conflicts": False, "update_conflicts": True,
#  "update_fields": ["json"], "unique_fields": ["pk"]}
```

### `adorm.drivers.django.translation.check_bulk_create(operation)`

Raises `ProgrammingError` for a `BulkCreate` operation that Django's `bulk_create()` refuses on PostgreSQL, before
any statement runs: a model with a multi-table inheritance parent (a proxy model is allowed), and under
`update_conflicts` no `unique_fields`, a primary key in `update_fields`, or a field without a column of its own (a
many-to-many field, a reverse relation) in `update_fields` or `unique_fields`. A name that is no field raises
[`FieldError`](./exceptions#adorm-fielderror-message-none-model-none-key-none-original-none-sqlstate-none). Both SQL
drivers call it.

```python
check_bulk_create(BulkCreate("library.Edition", [{"isbn": "9790000000010"}]))
# ProgrammingError: bulk_create() cannot insert rows of library.Edition, a multi-table inheritance child
```

### `adorm.drivers.django.translation.lookup_kwargs(query, lookups)`

Returns the keyword arguments of Django's `get_or_create()` and `update_or_create()` for the adorm `lookups`
(`(key, value)` pairs) on the model of `query`. Django creates the row from the arguments without `__`:

- a key without `__` and without a lookup name stays a field name, renamed by `orm_names`; a foreign key named by
  its relation (`mentor`) becomes its attname (`mentor_id`), a model instance value becomes its key;
- a key with a lookup name or across relations is translated as in `filter()` and only filters.

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

### `adorm.drivers.django.translation.table_model(table)`

Returns the installed concrete Django model (no proxy) whose `db_table` is `table`, auto-created many-to-many through
models included; a model registered with adorm comes first when several share the table. `None` when there is none.

```python
table_model("library_tagging")      # Tagging
table_model("pg_temp_parent")       # None
```

### `adorm.drivers.django.translation.column_fields(model, columns)`

Returns the adorm field names of the columns of the Django model `model`'s own table, in the order of `columns`: the
fields of the registered adorm model, or the Django attnames when no adorm model is registered. A column that is no
field of that table (an index expression, a column of a parent's table) is left out.

```python
column_fields(dj.Tagging, ["book_isbn"])                     # ('book_id',): db_column="book_isbn"
column_fields(dj.Reservation, ["book_id", "copy"])           # ('book_id', 'copy_')
column_fields(dj.Author, ["lower((slug)::text)", "name"])    # ('name',)
```

### `adorm.drivers.django.translation.check_in_bulk_field(operation)`

Raises `ProgrammingError` unless the field of the [`InBulk`](./operations#adorm-operations-inbulk-query-field-name-pk-ids-none)
`operation` is the primary key, a field unique on its own (`unique=True` or a single-field `UniqueConstraint` without
a condition) or the only field the query is distinct on, as Django's `in_bulk()` requires; `FieldError` for a name
that is no concrete field. Both drivers call it before the select.

### `adorm.drivers.django.translation.parent_link_field(model)`

The primary key of the Django model `model` when it is the parent link of a multi-table inheritance child
(`parent_link=True`); `ProgrammingError` otherwise.

### `adorm.drivers.django.translation.child_instance(operation, parent)`

The Django instance a [`CreateFromParent`](./operations#adorm-operations-createfromparent-model-parent-pk-values)
`operation` saves: the field values of `parent` (the stored parent instance), the operation's values and the parent
link set to the parent's primary key. A value of a field the child does not declare itself raises
`ProgrammingError`.

```python
parent = dj.Book.objects.get(pk=isbn)
edition = child_instance(CreateFromParent("library.Edition", isbn, {"released": day}), parent)
edition.title == parent.title     # True
```

### `adorm.drivers.django.translation.clean_needs_row(operation)`

Whether the instance of a [`FullClean`](./operations#adorm-operations-fullclean-model-pk-values-exclude-validate-unique-true-validate-constraints-true-adding-true)
`operation` is built on its stored row: the operation is not `adding`, has a primary key and its values lack some of
the model's concrete fields.

### `adorm.drivers.django.translation.clean_instance(operation, alias, stored)`

The Django instance a `FullClean` `operation` validates: the operation's values over `stored` (the row the driver read
when `clean_needs_row()` is true; `None` when there is none), over the values alone when they are complete
(`Model.from_db()`), or over a new instance's defaults with the operation's primary key. Its `_state.adding` is the
operation's `adding` and its `_state.db` is `alias`, the database Django's constraint validation reads.

### `adorm.drivers.django.translation.pinned_alias(alias)`

Context manager: every query that Django code in the block routes through `django.db.router` (a query set without
`using()`, `save()` without `using`, an instance's validation) goes to the database `alias`.

- A router placed first in `router.routers`, ahead of the project's `DATABASE_ROUTERS`, answers `db_for_read()` and
  `db_for_write()` with the alias of the innermost block of the current context; outside a block it answers `None`
  and the project's routers decide.
- The alias is a context variable: other threads and contexts keep their routing; leaving an inner block restores the
  outer block's alias.
- Both drivers run `full_clean()` in a block of the profile's alias.

```python
from adorm.drivers.django.translation import pinned_alias

with pinned_alias("tenant"):
    instance.full_clean()                       # unique checks query "tenant"
    Member.objects.filter(active=True).count()  # "tenant"
Member.objects.count()                          # the project's routers decide
```

### `adorm.drivers.django.translation.clean_exclude(operation)`

The Django field names of the `exclude` names of a `FullClean` operation (adorm field or relation names, mapped
through `ModelMeta.orm_names`); names without a Django field are left out.

### `adorm.drivers.django.translation.validation_error(label, exc)`

The `adorm.ValidationError` for Django's `ValidationError` `exc` raised for an instance of the model `label`
(`None` when unknown).

- Each error keeps Django's message as text (`%` placeholders unfilled), `code` and `params`. A message whose text
  depends on its params (a lazy plural from `ngettext_lazy`) is formatted, with `%` doubled.
- In `params`, a Django model instance or class becomes the model's label, a lazy translation becomes text.
- With a model, errors are keyed by adorm name: a foreign key's under the adorm relation's name when the adorm model
  declares it (else its column field), errors of the whole model under `"__all__"`; the names in `unique_check` are
  adorm names. Without a model, or for a label Django does not know, the keys stay Django's.
- An error without a dict keeps its list of errors, or its single error.

Both drivers apply it to a Django `ValidationError` raised by any operation, with the operation's model ([`operation_model()`](#adorm-drivers-django-translation-operation-model-operation)).

```python
from django.core.exceptions import ValidationError as DjangoValidationError

exc = DjangoValidationError({"copy": [DjangoValidationError("Too high.", code="max_value")]})
error = validation_error("library.Reservation", exc)      # adorm field copy_ (Django field copy)
error.error_dict["copy_"][0].code                          # 'max_value'
```

### `adorm.drivers.django.translation.operation_model(operation)`

The label of the model an operation works on: its `model`, else its query's `model`; `None` when it has neither.

### `adorm.drivers.django.translation.subquery(query)`

Returns the Django query set of `query` used inside another query: its model rows (Django compares their primary
key), or, after `values()` or `values_list()` with fields, those columns. An `OuterRef` in `query` raises
`ProgrammingError`; inside a query that `translate()` translates, `OuterRef` refers to that query.

### `adorm.drivers.django.translation.aggregation(query, aggregations)`

Returns the Django query set of `query`'s rows, built as [`model_queryset()`](#adorm-drivers-django-translation-model-queryset-query)
builds it, and a dict of the Django expressions of `aggregations` (`(name, expression)` pairs, as in an
[`Aggregate`](/ref/operations#adorm-operations-aggregate-query-aggregations) operation), translated against the
query's annotations. The Django driver runs `queryset.aggregate(**expressions)`; the native PostgreSQL driver
compiles it with [`compile_aggregate()`](#compile-aggregate-query-aggregates).

```python
queryset, expressions = aggregation(Query("library.Author").alias(n=Count("books")), [("most", Max("n"))])
queryset.aggregate(**expressions)          # {"most": 3}
```

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

A read-only mapping from adorm function class names to Django function classes of the same name: `Lower`, `Upper`,
`Length`, `Coalesce`, `Concat`, `Now`, `TruncYear`, `TruncQuarter`, `TruncMonth`, `TruncWeek`, `TruncDay`,
`TruncHour`, `TruncMinute`, `TruncSecond`, `TruncDate`, `TruncTime`. `Trunc` translates separately.

### `adorm.drivers.django.translation.AGGREGATES`

A read-only mapping from adorm aggregate class names to Django aggregate classes of the same name: `Count`, `Sum`,
`Avg`, `Min`, `Max`.

### `adorm.drivers.django.translation.SELECT_RELATED_DEPTH`

`5`: how many relations deep `select_related()` without paths follows non-null foreign keys, as Django's
`Query.max_depth`.

## Native PostgreSQL driver

`adorm.drivers.asyncpg` executes adorm operations on asyncpg, natively async. Importing it imports asyncpg, Django
and psycopg 3 (the `asyncpg` extra, `pip install "adorm[asyncpg]"`); connections need `django.setup()`. Usage:
[native PostgreSQL driver topic](/topics/postgresql-driver). The names below are defined in
`adorm.drivers.asyncpg.driver`.

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

Inside a Django `TestCase` ([`find_test_database()`](/ref/testing#adorm-testing-django-find-test-database-alias)) it raises
`NotSupportedError` before anything connects; an asyncpg connection can neither see nor roll back the test's
transaction. In another Django test the connection's `is_usable()` becomes false once a `TestCase` transaction is open
on the test's connection.

`schema_hash(label)` is [`model_schema_hash(label)`](#adorm-drivers-django-model-schema-hash-label), the hash of the
installed Django model.

```python
adorm.configure(profiles={"default": {"driver": "adorm.drivers.asyncpg.AsyncpgDriver", "alias": "default"}})
driver = AsyncpgDriver()
pool = Pool(driver, ConnectionProfile("default", driver, params={"USER": "reports"}), max_connections=10)
driver.schema_hash("clubs.Club")   # 'e8c2eed312ab7b68'
```

### `adorm.drivers.asyncpg.AsyncpgConnection`

The [`DriverConnection`](#adorm-drivers-driverconnection) of `AsyncpgDriver`. `conn` is the `asyncpg.Connection`,
also `target`, what profile hooks receive; `compiler` is its `DjangoCompiler`; `profile` its profile.

| Method | Does |
|---|---|
| `_execute(operation)` | a read: [`translate()`](#adorm-drivers-django-translation-translate-query)s the operation's query, compiles it on the event loop and runs one statement: `select` → list of rows, `get` → the row (as Django's `get()`: no ordering unless sliced or `distinct()` on fields, at most [`MAX_GET_RESULTS`](#adorm-drivers-asyncpg-max-get-results) rows fetched, Django's messages), `first` / `last` → the row or `None` (ordered as Django's `first()` / `last()`, one row), `count` → `int` ([`compile_count()`](#compile-count-query-in-transaction-false)), `exists` → `bool` (Django's `Query.exists()`; for a combined query, whether its first row exists), `aggregate` → a dict by name ([`compile_aggregate()`](#compile-aggregate-query-aggregates) of [`aggregation()`](#adorm-drivers-django-translation-aggregation-query-aggregations); no statement runs when the query matches no rows), `in_bulk` → list of rows ([`check_in_bulk_field()`](#adorm-drivers-django-translation-check-in-bulk-field-operation), then one statement per [`IN_BULK_BATCH_SIZE`](#adorm-drivers-asyncpg-in-bulk-batch-size) ids). A write: the [`WRITES`](#adorm-drivers-asyncpg-writes-writes) method of a new [`Writer`](#adorm-drivers-asyncpg-writes-writer-connection), with the results of the Django driver. Errors go through `map_error()`; Django's `SynchronousOnlyOperation` while a query is built (a manager's `get_queryset()` that accesses the database) raises `NotSupportedError`. Only an error or a cancellation of a statement, or a cancellation inside a transaction the write opened, can make the connection unusable. While the transaction is rollback-only (a write failed after its first statement, outside a savepoint of its own) every operation raises `TransactionManagementError` |
| `_execute(CheckTable)` | [`expected_tables(operation, compiler.connection, driver.name)`](#adorm-drivers-django-expected-tables-operation-connection-driver-none-check-types-true), then one [`columns_sql("$1")`](#adorm-drivers-postgresql-columns-sql-parameter) statement per table; the result is the [`table_problems()`](#adorm-drivers-postgresql-table-problems-table-rows) of every table after the `unknown_field` problems. A label without an installed Django model raises `SchemaMismatchError` before any statement |
| `begin()` | `BEGIN`, or `BEGIN ISOLATION LEVEL <level>` for `OPTIONS["isolation_level"]` ([`ISOLATION_LEVELS`](#adorm-drivers-asyncpg-isolation-levels)); inside a transaction `TransactionManagementError` |
| `commit()`, `rollback()` | `COMMIT`, `ROLLBACK`; a `COMMIT` whose status is `ROLLBACK` (the transaction failed earlier) raises `TransactionManagementError`, and so does `commit()` of a rollback-only transaction, after sending `ROLLBACK` instead |
| `savepoint(name)`, `release_savepoint(name)`, `rollback_to_savepoint(name)` | `SAVEPOINT`, `RELEASE SAVEPOINT`, `ROLLBACK TO SAVEPOINT` with `name` quoted. Outside a transaction, for a savepoint name already open (`savepoint`) and for one that is not open (the others) `TransactionManagementError`. Releasing a savepoint releases those opened after it; rolling back to one discards those opened after it and keeps it open, and ends the rollback-only state of a write that failed after it was opened |
| `set_options(options, local)` | the statements of [`set_options_sql(options, local)`](#adorm-drivers-sql-set-options-sql-options-local) in one simple-protocol message; errors as for any statement |
| `cancel()` | cancels the statement running when it is called, from any thread; that call raises `OperationalError` with SQLSTATE `57014`, a statement that starts later is not affected. Nothing without a running statement |
| `is_usable()` | `False` once a connection error was raised, once a statement was cancelled before it finished, once a write was cancelled inside a transaction of its own (also right after its `BEGIN` or during its `COMMIT`) or that transaction's `ROLLBACK` failed or was cancelled, after `close()` and when asyncpg reports the connection closed |
| `close()` | asyncpg's `close()` within the driver's `close_timeout` (asyncpg aborts the connection when it fails); logs errors, a second call does nothing |
| `backend_pid` | asyncpg's `get_server_pid()` |
| `_listen(channel)`, `_unlisten(channel)` | asyncpg's `add_listener()` and `remove_listener()`, which send `LISTEN` and `UNLISTEN` with the channel quoted; errors as for any statement |
| `map_error(exc)` | [`map_django_error(exc)`](#adorm-drivers-django-map-django-error-exc-vendor-none): Django's exceptions as on the Django driver, asyncpg's by their SQLSTATE; asyncpg's `InternalClientError` (its protocol in an unexpected state, as after the server ended an idle connection) is `InterfaceError` |

- Each statement runs in its own task; `cancel()` stops it without cancelling the caller.
- Cancelling the task that awaits `execute()` cancels the statement too (asyncpg sends a cancel request) and raises
  `asyncio.CancelledError`.
- A statement cancelled either way before it finished makes the connection unusable, and the slot closes it instead
  of releasing it to the pool: the server may have run it without the protocol's closing Sync message and keep its
  transaction and locks open until the connection's next message.
- A statement inside a transaction compiles with `in_transaction=True`.
- On open, the connection registers asyncpg's log and termination listeners. Every notice or warning the server sends
  is emitted as a [`NOTICE`](/ref/events#adorm-events-notice) event before the outcome of the request that caused it.
  A connection that ends without `close()` (the server terminated the session) becomes unusable and emits
  [`CONNECTION_LOST`](/ref/events#adorm-events-connection-lost), also while no call runs.
- asyncpg reads notifications on the event loop as they arrive.

```python
connection = await AsyncpgDriver().open_connection(ConnectionProfile("default", driver), emit)
await connection.begin()
await connection.savepoint("adorm_sp_1")
await connection.execute(Create("library.Tag", {"name": "night"}))
await connection.rollback_to_savepoint("adorm_sp_1")
await connection.commit()
```

### `adorm.drivers.asyncpg.AsyncpgMonitor(conn, close_timeout=5.0)`

The [`DatabaseMonitor`](#adorm-drivers-databasemonitor) of `AsyncpgDriver` on the `asyncpg.Connection` `conn`.
`AsyncpgDriver.open_monitor(profile)` opens `conn` as `open_connection()` does (steps 1 to 3), also inside a Django
`TestCase`, where it connects to the test database; it reads only `pg_stat_activity`. `activity(pids)` runs
[`activity_sql("$1")`](#adorm-drivers-postgresql-activity-sql-parameter); errors go through `map_django_error()`.
`close()` is asyncpg's `close()` within `close_timeout`.

```python
async with await AsyncpgDriver().open_monitor(profile) as monitor:
    (await monitor.activity([pid]))[pid].state      # "idle in transaction"
```

### `adorm.drivers.asyncpg.CAPABILITIES`

The [`Capabilities`](#adorm-drivers-capabilities) of `AsyncpgDriver`: the `operations` and `constructs` of the
Django driver's [`CAPABILITIES`](#adorm-drivers-django-capabilities) (every read, `aggregate` included, the writes,
`check_table` and every construct listed there), `savepoints`, `cancellation` and the `connection_options` of
[`POSTGRESQL_OPTIONS`](#adorm-drivers-sql-postgresql-options). `backend_events`: `notify`, `notice`,
`connection_lost`; `max_channel_length`: [`MAX_IDENTIFIER_LENGTH`](#adorm-drivers-postgresql-max-identifier-length).

### `adorm.drivers.asyncpg.writes.Writer(connection)`

Runs write operations on `connection`, an `AsyncpgConnection`, with Django's write logic and without Django's model
methods and signals ([what runs and what does not](/topics/postgresql-driver#what-runs-and-what-does-not)). Every
statement is compiled by the connection's `DjangoCompiler` and awaited on its `asyncpg.Connection`. Model instances are
Django's, built with the model's constructor or `Model.from_db()`.

| Method | Django's counterpart |
|---|---|
| `create(operation)`, `save(operation)`, `update(operation)`, `delete(operation)`, `delete_instance(operation)`, `get_or_create(operation)`, `update_or_create(operation)` | what the [Django driver](#adorm-drivers-django-djangoconnection) calls for that operation; the same results |
| `bulk_create(operation)` | the Django driver's `bulk_create` call, with `bulk_create_instances()`; the same results |
| `bulk_create_instances(model, objs, *, batch_size=None, ignore_conflicts=False, update_conflicts=False, update_fields=None, unique_fields=None)` | `QuerySet.bulk_create()` of the installed Django version: the instances with a primary key (one a field default generates is set first) and those without inserted separately, in batches of at most `batch_size` rows and at most `MAX_QUERY_PARAMS // columns` rows, `RETURNING` the fields the database sets (not under `ignore_conflicts`); `ON CONFLICT DO NOTHING` or `DO UPDATE`; more than one statement in `atomic(savepoint=False)`; on Django 6 the `_order` of a model with `order_with_respect_to` numbered first |
| `create_from_parent(operation)` | `save_base(raw=True)` of a multi-table inheritance child: the parent's row read with `get()`, the child's table inserted as `Model.save()` inserts it |
| `full_clean(operation)` | `Model.full_clean()`, run as Python on the event loop with [`DjangoCompiler.replay()`](#adorm-drivers-asyncpg-compiler-djangocompiler-alias-default-server-version-params-none): each statement the validation runs is awaited, then the validation runs again with its rows, so the model's `clean()` and overrides run as on the Django driver, once per statement plus once |
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
`delete()`, `create()`, `bulk_create()`, `update()`, `get_or_create()`, `update_or_create()`, `get()`,
`select_for_update()`), and a delete that reaches an `on_delete` handler other than Django's own, raise
`NotSupportedError` before any statement changes data. A cancellation inside a transaction opened by `atomic()` makes
the connection unusable; the slot closes it and the server rolls the transaction back.

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

The `Writer` method for each write operation kind: `create`, `bulk_create`, `create_from_parent`, `full_clean`, `save`, `update`,
`delete`, `delete_instance`, `get_or_create`, `update_or_create`.

### `adorm.drivers.asyncpg.writes.MAX_VALIDATION_STATEMENTS`

How many statements one `full_clean` operation runs at most (100); one more raises `NotSupportedError`.

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
and `application_name` and `client_encoding`, which asyncpg sends to the server as settings. Any other key raises
`NotSupportedError` before connecting: asyncpg would send it as a server setting, and the server refuses a
libpq-only parameter such as `keepalives`.

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

### `adorm.drivers.asyncpg.IN_BULK_BATCH_SIZE`

`10_000`: the most ids one statement of an `in_bulk` names. More ids run one statement per batch on the operation's
connection; the result holds the rows of all batches.

### `adorm.drivers.asyncpg.server_version_num(version)`

Returns PostgreSQL's `server_version_num` for an asyncpg `ServerVersion` (`Connection.get_server_version()`):
`170006` for 17.6, `90624` for 9.6.24. asyncpg reads a version from 10 on as `major`, `0`, `micro` (17.6 is
`ServerVersion(17, 0, 6, ...)`).

## SQL statements

`adorm.drivers.sql` holds helpers for drivers that execute SQL themselves, whatever generates the SQL. It imports
no ORM or database library.

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

Returns `sql` (PEP 249 `format` paramstyle, as Django's compiler and psycopg use) with every `%s` replaced by `$1`,
`$2` … in order and every `%%` by `%`. `casts`, when given,
holds one entry per placeholder: a PostgreSQL type name appends `::<type>`, `None` leaves the placeholder bare.

`%` is interpreted everywhere in `sql`, inside quoted literals and identifiers too, as in psycopg. Any
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

## PostgreSQL errors and sessions

`adorm.drivers.postgresql` reads what PostgreSQL reports about a violated constraint from the errors of psycopg 3,
psycopg 2 and asyncpg, and holds the query of the database's sessions that the drivers' monitors run. It imports no
ORM or database library. The Django and the native PostgreSQL drivers pass
[`integrity_details`](#adorm-drivers-postgresql-integrity-details-exc) to
[`wrap_driver_error()`](/ref/exceptions#adorm-exceptions-wrap-driver-error-exc-details-none) through
[`map_django_error()`](#adorm-drivers-django-map-django-error-exc-vendor-none).

```python
from adorm.drivers.postgresql import integrity_details, parse_key_detail, references_key
```

### `adorm.drivers.postgresql.listen_sql(channel)`

`LISTEN` for `channel` as a quoted identifier, so that the name is used exactly as given.

```python
listen_sql('Orders "EU"')     # 'LISTEN "Orders ""EU"""'
```

### `adorm.drivers.postgresql.unlisten_sql(channel)`

`UNLISTEN` for `channel`, quoted as in [`listen_sql()`](#adorm-drivers-postgresql-listen-sql-channel).

```python
unlisten_sql("orders")        # 'UNLISTEN "orders"'
```

### `adorm.drivers.postgresql.MAX_IDENTIFIER_LENGTH`

`63`: the longest PostgreSQL identifier in bytes (`NAMEDATALEN - 1`), and so the longest channel name; the
`max_channel_length` of the Django and the native PostgreSQL driver.

### `adorm.drivers.postgresql.notice_payload(severity, sqlstate, message, detail, hint)`

The payload of a [`NOTICE`](/ref/events#adorm-events-notice) event: a dict of the notice's untranslated severity,
SQLSTATE, primary message, detail and hint, `None` for a field the server did not send.

```python
notice_payload("NOTICE", "00000", "clubs updated", None, None)
# {'severity': 'NOTICE', 'sqlstate': '00000', 'message': 'clubs updated', 'detail': None, 'hint': None}
```

### `adorm.drivers.postgresql.activity_sql(parameter)`

The query of the sessions whose pids are in the integer array bound to `parameter` (`"%s"` for psycopg, `"$1"` for
asyncpg): `pid`, `state`, `wait_event_type`, `wait_event` and `pg_blocking_pids(pid)` from `pg_stat_activity`, one
row per session found, in [`backend_activity()`](#adorm-drivers-postgresql-backend-activity-pid-state-wait-event-type-wait-event-blocking-pids)'s
argument order.

```python
activity_sql("$1")
# 'SELECT pid, state, wait_event_type, wait_event, pg_blocking_pids(pid) FROM pg_stat_activity
#  WHERE pid = ANY($1::integer[])'
```

### `adorm.drivers.postgresql.backend_activity(pid, state, wait_event_type, wait_event, blocking_pids)`

The [`BackendActivity`](#adorm-drivers-backendactivity) of a row of `activity_sql()`; `blocking_pids` becomes a tuple
of `int`, `None` an empty one.

```python
backend_activity(4711, "active", "Lock", "tuple", [4702])
# BackendActivity(pid=4711, state='active', wait_event_type='Lock', wait_event='tuple', blocking_pids=(4702,))
```

### `adorm.drivers.postgresql.integrity_details(exc)`

The keyword arguments of [`IntegrityError`](/ref/exceptions#adorm-integrityerror-message-none-constraint-name-none-table-name-none-schema-name-none-columns-values-detail-none-model-none-fields-kwargs)
that describe a violation (`constraint_name`, `table_name`, `schema_name`, `columns`, `values`, `detail`), read
from the first exception with a SQLSTATE in the `__cause__` chain of `exc`: the `diag` object of psycopg 3 and
psycopg 2 (`message_detail` is the detail), or the attributes of an asyncpg error. `columns` and `values` come from
[`parse_key_detail()`](#adorm-drivers-postgresql-parse-key-detail-detail) of the detail; without a key there,
`columns` is the reported column (a not-null violation). An attribute that is not text counts as not reported. `{}`
when the chain has no SQLSTATE.

```python
from adorm.exceptions import IntegrityError, wrap_driver_error

wrapped = wrap_driver_error(exc, details=integrity_details)
raise IntegrityError(str(exc), original=exc, sqlstate=exc.sqlstate, **integrity_details(exc)) from exc
```

### `adorm.drivers.postgresql.parse_key_detail(detail)`

The columns and values of the key in PostgreSQL's detail message, as two tuples; `((), ())` for a detail without a
key. The value of a key of one column is the whole text; the values of a key of several columns are `()` when they
do not split at `", "` into one part per column.

```python
parse_key_detail('Key (club_id, member_id)=(3, 17) already exists.')
# (('club_id', 'member_id'), ('3', '17'))
parse_key_detail('Key ("Code", lower(name::text))=(A1, ana) already exists.')
# (('Code', 'lower(name::text)'), ('A1', 'ana'))
parse_key_detail('Key (club_id)=(9) is not present in table "clubs_club".')
# (('club_id',), ('9',))
parse_key_detail('Key (name)=(Koper, Sailing) already exists.')
# (('name',), ('Koper, Sailing',))
parse_key_detail('Key (name, city)=(Koper, Sailing, Koper) already exists.')
# (('name', 'city'), ())
parse_key_detail('Failing row contains (3, null).')
# ((), ())
```

### `adorm.drivers.postgresql.references_key(detail)`

Whether the key in PostgreSQL's detail message is a referenced row's (`Key (…)=(…) is still referenced from table
…`, a delete or update of the row). Its columns are then the referenced table's, not those of the violated
constraint's `table_name`.

```python
references_key('Key (id)=(1) is still referenced from table "clubs_membership".')   # True
references_key('Key (club_id)=(9) is not present in table "clubs_club".')           # False
```

## Table checks

`adorm.drivers.postgresql` also compares a table's columns in PostgreSQL's catalogue with a
model's expectations. A driver turns a [`CheckTable`](/ref/operations#adorm-operations-checktable-model-fields-many-to-many) operation into
`ExpectedTable` values (the Django and the native PostgreSQL drivers with
[`expected_tables()`](#adorm-drivers-django-expected-tables-operation-connection-driver-none-check-types-true)), runs `columns_sql()` for each and
returns the `table_problems()`.

```python
from adorm.drivers.postgresql import columns_sql, ExpectedColumn, ExpectedTable, normalize_type, table_problems
```

### `adorm.drivers.postgresql.ExpectedColumn(name, type, nullable, field=None)`

A column a model expects: its `name`, its `type` as the ORM declares it (`"varchar(100)"`; `None` leaves the type
unchecked), whether it takes `NULL`, and the ORM name of its `field` (`None` for a column of an automatic link table).

### `adorm.drivers.postgresql.ExpectedTable(name, regclass, columns)`

A table a model expects: its `name` as the ORM names it, the same name quoted for `to_regclass()` (`regclass`, e.g.
`'"clubs_club"'`) and its `columns`, a tuple of `ExpectedColumn`.

### `adorm.drivers.postgresql.columns_sql(parameter)`

The query of the columns of the table whose quoted name is bound to `parameter` (`"%s"` for psycopg, `"$1"` for
asyncpg), resolved on the connection's `search_path` with `to_regclass()`. Each row is `(table exists, column name,
type, not null, has a default)`, in column order; the type is spelt as `format_type()` prints it, and an identity or
generated column counts as one with a default. A missing table gives one row `(False, None, None, None, None)`.

```python
rows = await conn.fetch(columns_sql("$1"), '"clubs_club"')
# [(True, 'id', 'bigint', True, True), (True, 'name', 'character varying(100)', True, False), ...]
```

### `adorm.drivers.postgresql.normalize_type(type_name)`

`type_name`, a PostgreSQL type as an ORM declares it or `format_type()` prints it, in `format_type()`'s spelling:
lower case, aliases spelt out, no spaces inside parentheses, array dimensions without a size. Two types match when
their normalised forms are equal.

| Declared | Normalised |
|---|---|
| `varchar(100)`, `char(3)` | `character varying(100)`, `character(3)` |
| `int`, `serial`, `int8`, `bigserial`, `int2`, `bool` | `integer`, `integer`, `bigint`, `bigint`, `smallint`, `boolean` |
| `numeric(8, 2)`, `decimal(8,2)` | `numeric(8,2)` |
| `float8`, `float4` | `double precision`, `real` |
| `timestamptz`, `timestamp`, `time`, `timetz` | `timestamp with time zone`, `timestamp without time zone`, `time without time zone`, `time with time zone` |
| `varchar(30)[10]` | `character varying(30)[]` |

```python
normalize_type("varchar(30)[10]") == normalize_type("character varying(30)[]")   # True
```

### `adorm.drivers.postgresql.table_problems(table, rows)`

The differences between the `ExpectedTable` `table` and the `rows` of `columns_sql()` for it, as the problem dicts of
a `CheckTable` result (keys `kind`, `table`, `column`, `field`, `expected`, `actual`):

| `kind` | When |
|---|---|
| `missing_table` | the table does not exist; it is the only problem of the table |
| `missing_column` | an expected column does not exist |
| `column_type` | an expected column's type differs (`normalize_type()`); `expected` is the declared type, `actual` the catalogue's |
| `nullability` | an expected column takes `NULL` and the model expects `NOT NULL`, or the other way round; `expected` and `actual` are `"NULL"` or `"NOT NULL"` |
| `required_column` | a column the model does not expect is `NOT NULL` without a default, identity or generation, so an insert of the model fails; `actual` is its type |

The problems follow the expected columns' order, `required_column` problems last. A column the model does not expect
that takes `NULL` or has a default is not a problem.

```python
table = ExpectedTable("clubs_club", '"clubs_club"', (ExpectedColumn("name", "varchar(100)", False, "name"),))
table_problems(table, [(True, "name", "character varying(50)", True, False)])
# [{'kind': 'column_type', 'table': 'clubs_club', 'column': 'name', 'field': 'name',
#   'expected': 'varchar(100)', 'actual': 'character varying(50)'}]
```

## Django's compiler for asyncpg

`adorm.drivers.asyncpg.compiler` compiles Django queries into SQL for asyncpg, without a database connection; the
[native PostgreSQL driver](#native-postgresql-driver) builds its statements with it. It imports Django, psycopg 3 (Django's PostgreSQL backend must run on it) and asyncpg, which the `asyncpg` extra
installs (`pip install "adorm[asyncpg]"`). [SQL compilation for asyncpg](/topics/sql-compilation) describes the
behaviour it relies on.

```python
from adorm.drivers.asyncpg.compiler import bind_param, DjangoCompiler, DjangoStatement, MAX_QUERY_PARAMS, StatementNeeded
```

### `adorm.drivers.asyncpg.compiler.DjangoCompiler(alias="default", *, server_version, params=None)`

Compiles Django queries for the `DATABASES` entry `alias` into [`DjangoStatement`](#adorm-drivers-asyncpg-compiler-djangostatement)
objects, on a dedicated database wrapper (`connection`) built from that entry and never connected. Django's
thread-local connection for `alias` is not touched; compiling works in any thread and inside a running event loop.
One instance can be shared by threads and tasks.

- `server_version` is PostgreSQL's `server_version_num` (`170006` for 17.6). Django chooses the SQL for `AnyValue`,
  `JSONObject`, `JSONArray` and `UUID7` by it; a version that lacks a construct raises Django's `NotSupportedError`.
- `params` replace keys of the entry for this compiler, as a connection profile's `params` do; the resulting entry is
  the attribute `settings_dict`.
- Parameters are bound for the entry's `OPTIONS["server_side_binding"]` (attribute `server_side_binding`).
- The wrapper's `features.max_query_params` is [`MAX_QUERY_PARAMS`](#adorm-drivers-asyncpg-compiler-max-query-params):
  on Django 6 `compiler.connection.ops.bulk_batch_size(fields, objs)` returns batch sizes asyncpg accepts; Django
  5.2's PostgreSQL backend does not read it.

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

A select whose filter matches nothing (`pk__in=[]`) compiles with `WHERE 0 = 1`. A set operation (`union()`,
`intersection()`, `difference()`; also in `compile_count()`) leaves out its combined queries that match nothing as
Django does; when every one matches nothing, they compile with `WHERE 0 = 1`. An update or delete whose filter
matches nothing, and an update without values, compile into an empty statement ([`empty`](#empty)), which the caller
does not execute.

`in_transaction` tells whether the statement runs inside a transaction: a `select_for_update()` query compiles only
then and raises adorm's `TransactionManagementError` otherwise, as Django does. `returning` names the Django fields
an update returns: the statement ends with `RETURNING` and keeps them in [`returning`](#returned-records); on Django
5.2, which does not compile it, `returning` raises adorm's `NotSupportedError`.

Raises adorm's `NotSupportedError` for:

- an insert ([`compile_insert()`](#compile-insert-query-returning) compiles one);
- an update that sets fields of a multi-table inheritance parent, which Django runs as one statement per table after
  selecting the keys ([`Writer.execute_update()`](#adorm-drivers-asyncpg-writes-writer-connection) runs it);
- a statement with more than `MAX_QUERY_PARAMS` parameters;
- a construct that needs a connection to compile: `SearchHeadline` with options (psycopg quotes them).

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

#### `compile_aggregate(query, aggregates)`

Compiles Django's `aggregate(**aggregates)` of `query`, a Django select `QuerySet` or `Query`, into a
[`DjangoAggregation`](#adorm-drivers-asyncpg-compiler-djangoaggregation): the statement Django's
`get_aggregation()` builds, directly or over a subquery of the sliced, distinct or grouped rows, and the conversion
of its one row into Django's result. Django builds it on the compiler's wrapper, registered for the call under a
dedicated alias in the calling thread's connections; the statement is captured instead of run, so neither the
`DATABASES` alias nor any connection is touched. `query` is not changed; its row locks are dropped, as in Django.
The compiler can be shared by threads and by tasks of one event loop. Unknown fields raise Django's `FieldError`.

```python
aggregation = compiler.compile_aggregate(Book.objects.filter(genre="fiction"), {"pages": Sum("pages")})
aggregation.statement.sql     # 'SELECT SUM("library_book"."pages") AS "pages" FROM "library_book" WHERE ...'
aggregation.result(await conn.fetchrow(aggregation.statement.sql, *aggregation.statement.params))
# {"pages": 1525}
```

#### `replay(fn, answers, aliases=())`

Runs `fn`, synchronous Django code that reads the database (Django's `Model.full_clean()`), and returns its result.

- While `fn` runs, the compiler's database wrapper is the connection of `aliases` and `DEFAULT_DB_ALIAS` in the
  calling thread. The aliases get their previous connections back when `fn` returns. On an event loop's thread no
  other task runs while `fn` does, so only `fn` sees the replacement.
- Every statement `fn` runs is answered in order from `answers`: `(sql, rows)` pairs of the compiled statement and its
  rows as asyncpg returns them, loaded as [`rows()`](#rows-records) loads them before Django's own converters.
- A statement past the answers raises [`StatementNeeded`](#adorm-drivers-asyncpg-compiler-statementneeded) with that
  statement compiled. It is a `BaseException` and is raised also when `fn` catches it, returns, or raises another
  exception after requesting the statement.
- The caller runs the statement, appends its answer and calls again: `fn` runs once per statement plus once and must
  run the same statements each time; a different statement raises `NotSupportedError`.

```python
answers = []
while True:
    try:
        result = compiler.replay(validate, answers, ("default",))
        break
    except StatementNeeded as needed:
        rows = await conn.fetch(needed.statement.sql, *needed.statement.params)
        answers.append((needed.statement.sql, [tuple(row) for row in rows]))
```

### `adorm.drivers.asyncpg.compiler.StatementNeeded`

The `BaseException` subclass `replay()` raises for a statement it has no answer for: `statement` is that statement as a
[`DjangoStatement`](#adorm-drivers-asyncpg-compiler-djangostatement), with numbered placeholders and asyncpg
parameters.

### `adorm.drivers.asyncpg.compiler.DjangoAggregation`

An aggregation compiled by `compile_aggregate()`.

| Member | Meaning |
|---|---|
| `statement` | the [`DjangoStatement`](#adorm-drivers-asyncpg-compiler-djangostatement) that returns the one row; [`empty`](#empty) when the query matches no rows (`none()`, `pk__in=[]`), and nothing is run |
| `result(record)` | Django's result for `record`, the statement's row (`None` for no row or an empty statement): a dict by name, values loaded as in [`rows()`](#rows-records) and converted by the aggregates' fields (a `Decimal` sum, an aware `datetime` in the connection's time zone); over no rows `Count` gives `0` and the others their `default` or `None` |

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
order, with the values Django's own evaluation of the query returns on psycopg:

- the columns Django adds only for ordering or `extra(select=)` are dropped;
- `inet` / `cidr` values are text and `uuid` values `uuid.UUID` (in arrays too), as psycopg loads them;
- `timestamptz` values get the time zone of the compiler's connection (`UTC`, or the entry's `TIME_ZONE`); under
  `USE_TZ = False` they are naive in Django's `TIME_ZONE`, as Django's psycopg loader returns them;
- the fields' `from_db_value()` converters are applied, JSON decoding included.

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
has under psycopg (the server then resolves the statement as with psycopg), or `None` for an untyped parameter.
`server_side_binding` selects psycopg's binding mode; the default, client-side binding, is Django's. The modes differ
only for numbers and lists of numbers.

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
