# Drivers

::: info Status
adorm provides the driver contract (`adorm.drivers`), the event types (`adorm.events`), the
[slots and pool](/topics/pool) that call drivers, and two drivers that execute reads, writes, row locks and
savepoints: the [Django driver](/topics/django-driver) and the [native PostgreSQL driver](/topics/postgresql-driver).
:::

A driver executes adorm operations on one target: an ORM such as Django's or SQLAlchemy, or a database library
such as asyncpg. The core of adorm (queries, models, sessions, the pool) does not import a target and uses only the
contract on this page. Application code runs unchanged on any driver that implements it.

```text
slot ── operation ──▶ DriverConnection ──▶ target (Django ORM, asyncpg, SQLAlchemy …)
  ▲                        │
  └── result / error ──────┤   return value of execute(), an adorm exception
  └── events ──────────────┘   emit(CallbackEvent | BackendEvent)
```

## Operations in, plain data out

A slot passes its connection one operation at a time. An [operation](/ref/operations) describes a read or write:
its `kind` (`"select"`, `"update"` …), the constructs it uses (`"lookup.icontains"`, `"function.Lower"` …), models by
label and fields by adorm name. The driver translates it for the target and executes it. A [`Query`](/ref/query) is
an immutable tree of [expressions](/ref/expressions) and [lookups](/ref/lookups): `walk()` visits its nodes,
`constructs()` returns the construct names the operation reports to `Capabilities`.

`execute()` returns plain data: rows as dicts keyed by adorm field names, a scalar or a count. The core builds
model instances from the rows; no target object crosses the contract.

An adorm name can differ from the target's: a generated model renames fields that clash with pydantic (`json_` for
Django's `json`). The driver translates every segment of a field path, every key of written values and every key of
returned rows with the `orm_names` of the model that segment reaches: `ModelMeta.orm_name()` on a model
class, `ModelSchema.orm_names` from a [schema source](/ref/encoding#adorm-schema-schemasource). A name `orm_names`
does not list is the same on both sides.

```python
Note._meta.orm_name("json_")                                     # "json"
registry.model_schema("library.Note").orm_names["copy_"]         # "copy"
# copy___title: "copy_" on library.Note, then "title" on library.Book -> Django's "copy__title"
```

Operations convert to JSON-safe data, each value encoded by its field's type ([Operation
encoding](/ref/encoding)). A driver that forwards operations to another process sends `operation.to_dict(schema)`;
the receiving side executes `Operation.from_dict(data, schema)`.

```python
rows = await connection.execute(operation)    # [{"id": 1, "name": "Ana"}, ...]
```

Transactions are separate calls: `begin()`, `savepoint(name)`, `release_savepoint(name)`,
`rollback_to_savepoint(name)`, `commit()` and `rollback()`. The slot calls them; a driver does not open or end a
transaction on its own.

## Events

A connection reports events through the `emit` callable it receives when it opens:

| Event | Meaning | Example |
|---|---|---|
| `CallbackEvent(name, payload)` | a point in the connection's life the client runs callbacks for; not `on_commit` or `on_rollback`, which the slot delivers itself | `cache_cleared` |
| `BackendEvent(name, channel, payload)` | something the database or the target raised on its own | PostgreSQL `NOTIFY`, a server notice, a lost connection |

The slot forwards them over its event queue, together with the outcome of each request (`ResultEvent`,
`ErrorEvent`, `CancelledEvent`) and its own `CallbackEvent("on_commit")` and `CallbackEvent("on_rollback")` after a
successful commit or rollback. The slot drops and logs any other event a connection emits, including outcome events
and the names `on_commit` and `on_rollback`. On the client the lease's event channel interprets each event, and the
pool's [`EventBus`](/ref/events#adorm-events-eventbus) dispatches unclaimed ones to subscribers
([events topic](/topics/events)). Signals of the target (Django's `post_save`, SQLAlchemy's ORM events) and the
target's own `on_commit` hooks run on the driver side only; the client sees only what the driver emits.

A driver lists the backend event names it emits in `Capabilities.backend_events`. The names adorm interprets:

| Name | Emitted | Interpreted by |
|---|---|---|
| `notify` | for each notification on a channel the connection listens on (`listen(channel)`), also while no call runs | [`adorm.listen()`](/topics/events#listening-for-notifications) |
| `notice` | for each notice or warning the server sends, before the outcome of the request that caused it | subscribers of the pool's `EventBus` |
| `connection_lost` | at most once, when the driver notices that the database connection ended without adorm closing it: at least while the connection listens and no call runs, at other times when the driver can tell (the native PostgreSQL driver: whenever the server ends the connection) | `adorm.listen()`, which raises `InterfaceError` |

A threaded connection that must read while idle returns a descriptor from `idle_fileno()`; its thread then runs
`idle_sync()` whenever the descriptor is readable ([`ThreadedConnection`](/ref/drivers#adorm-drivers-threadedconnection)).

## Capabilities

A driver declares what it supports in a [`Capabilities`](/ref/drivers#adorm-drivers-capabilities) value:
operation kinds, constructs, connection options, backend event names, savepoints, cancellation and the longest
notification channel name (`max_channel_length`). `savepoints` and `cancellation` default to `False`.

A driver whose support depends on the database a profile names declares it per profile in
[`capabilities_for(profile)`](/ref/drivers#capabilities-for-profile); the pool reads it once and checks against it.
The Django driver declares there what the backend of the profile's alias supports
([databases](/topics/django-driver#databases)).

`DriverConnection` checks every call against the declaration before the driver sees it. An operation with an
unsupported kind or construct, an unsupported connection option, or a savepoint call on a driver without savepoints
raises `NotSupportedError`, and nothing is executed. The error names the driver and the missing names
(`NotSupportedError.driver`, `.unsupported`).

- Outside a transaction the router checks an operation with
  [`Driver.check()`](/ref/drivers#check-operation-schema-hashes-none-capabilities-none) before it acquires a slot, so an
  unsupported operation does not wait for a slot of a full pool.
- Inside a transaction the check in `execute()` refuses before anything reaches the database; the transaction stays
  usable.

The checks are in the concrete `execute()`, `set_options()` and savepoint methods. A driver, threaded or async,
implements `_execute()`, `_set_options()` and the `_savepoint()` family.

```python
Capabilities(
    operations={"select", "get", "count", "create", "save", "update", "delete"},
    constructs={"lookup.exact", "lookup.icontains", "function.Lower", "aggregate.Count"},
    connection_options={"statement_timeout", "lock_timeout"},
    backend_events={"notify"},
)
```

Construct names have the form `<category>.<name>`: `lookup.<lookup name>`, `function.<class name>`,
`aggregate.<class name>` (with `aggregate.distinct` and `aggregate.filter`), `expression.<class name>`
(`expression.Case`, `expression.Subquery`, `expression.Exists`, `expression.OuterRef`,
`expression.CombinedExpression`), `clause.<clause>` (`clause.annotate`, `clause.distinct_on`,
`clause.select_for_update` …) and `path.nested`, a field path across a relation or into a JSON field
(`club__name`, `address__street`). Query nodes report theirs through `constructs()`. The
[expression reference](/ref/expressions) lists them; [`construct_names()`](/ref/query#adorm-query-construct-names)
returns them all.

```python
query = Member.objects.filter(memberships__club__slug="koper-sailing").query
MemoryDriver.capabilities.unsupported(Select(query))   # ['path.nested']
```

The [driver conformance suite](/topics/conformance) checks every bundled driver against its declaration over the
same schema and data.

## Execution mode

A driver's `mode` says how its connections run:

- **`"threaded"`**: a synchronous target runs on a dedicated thread per connection. Every call of a connection
  runs on that thread, which suits a target whose connections are bound to the thread that opened them (Django).
  [`ThreadedDriver`](/ref/drivers#adorm-drivers-threadeddriver) and
  [`ThreadedConnection`](/ref/drivers#adorm-drivers-threadedconnection) provide the thread handling; the driver
  implements synchronous methods.
- **`"async"`**: native coroutines on the event loop, no threads.

A threaded driver with `direct = True` runs **direct**: every `*_sync` method runs on the event loop's own thread.
Each call blocks the loop while it runs; direct mode is for one task per event loop, such as a Celery task. During a
direct call no running event loop is visible on the thread, so a target's check against use from an event loop
(Django's `async_unsafe`) accepts adorm's calls and applies to all other code. Cancelling a task does not interrupt
a running direct call; `cancel()` from another thread does.

Every `*_sync` call runs in a copy of the awaiting task's context: the target sees context variables set by the
caller; changes made inside the call do not reach the caller. A value bound to the thread that set it (an asgiref
`Local`, which holds Django's active language) must be re-bound on the connection's thread in
`prepare_call_sync()`, which runs before each call in its context copy. The Django driver re-binds there.

A connection belongs to one slot, which awaits one call at a time.

## Cancellation

Cancelling the task that awaits `execute()` cancels the statement running on the target. The call raises
`asyncio.CancelledError`; the slot rolls back an open transaction, and closes the connection when the driver
reports it unusable (the native PostgreSQL driver does after every statement cancelled before it finished). A
statement that completed before the cancellation arrived keeps its effect; its result is discarded.

`cancel()` cancels the running statement from any thread without cancelling the awaiting task: the call then
raises the target's cancellation error (`DriverConnection.is_cancellation()`; SQLSTATE `57014`, `query_canceled`, on
PostgreSQL). A cancellation request reaches only the
statement it was meant for; a threaded connection starts no further call until `cancel_sync()` returns.

`cancel()` returns without waiting. On the event loop's thread, a threaded connection runs `cancel_sync()` on a
separate thread, so a slow cancel request does not block the loop; the same applies to cancelling the awaiting task.

A threaded call that does not finish within the driver's `cancel_grace` after a cancellation leaves the
connection unusable: `is_usable()` returns `False` and the slot drops it. A driver that declares
`cancellation=False` does not stop a running statement; cancelling the task waits for the call to finish.

## Errors

Every error a connection raises is an adorm [exception](/ref/exceptions); `asyncio.CancelledError` passes
through. `DriverConnection.map_error()` converts the target's exceptions, by default with
[`wrap_driver_error`](/ref/exceptions#adorm-exceptions-wrap-driver-error-exc-details-none): SQLSTATE first, then the PEP
249 class name. A driver overrides it for errors the target reports without a SQLSTATE that adorm classifies
differently. A driver whose target reports a cancelled statement without SQLSTATE `57014` overrides
`DriverConnection.is_cancellation()`.

`DriverConnection.is_connection_lost()` decides whether a mapped error leaves the connection unusable. The
default, [`is_connection_error`](/ref/drivers#adorm-drivers-is-connection-error-exc), follows PostgreSQL:
`InterfaceError`, and `OperationalError` without a SQLSTATE or with a connection or shutdown SQLSTATE. A target
that reports ordinary statement errors as `OperationalError` without a SQLSTATE (`sqlite3` for a missing table
or a locked database, MySQL drivers for lock wait timeouts) overrides it to count only a lost connection.
A slot drops an unusable connection and a connection whose request failed with such an error, even when the driver
reports it usable; the next request gets a new one.

## Custom SQL

A connection that runs SQL adorm did not generate, such as SQL of application code the target ORM runs on the
driver side, calls [`mark_custom_sql()`](/ref/drivers#mark-custom-sql). The slot then closes the connection when the
lease ends, also after a commit, because that SQL may have changed the connection's state. Work that runs after a
call's outcome (Django's `on_commit()` callbacks) counts too: the slot waits for
[`settle()`](/ref/drivers#async-settle) before it decides.

```python
def execute_sync(self, operation):
    with self.db.execute_wrapper(self.watch):          # the Django driver's way to see what SQL code runs
        return run(operation)
```

## Profiles

A connection is opened for a [`Profile`](/ref/drivers#adorm-drivers-profile): its `name` (the `using` value), the
target's database `alias`, driver `params` and an optional `connect` hook. When `connect` is set, the driver
opens the target's connection with `connect(profile)`: a threaded driver calls it in
`connect_sync()` on the connection's thread, an async driver awaits its result in `open_connection()`.

The profile's `on_acquire` and `on_release` hooks run through the connection's `call_hook()`, which passes them
the connection's `target`, the target's own connection object. A threaded connection runs them on its thread.
Session parameters reach the connection as connection options, which `on_acquire` returns; a driver that sees the
statements of its target marks the SQL a hook runs as custom SQL. See [connection profiles](/topics/profiles#hooks).

```python
class DjangoConnection(ThreadedConnection):
    @property
    def target(self):
        return self.db              # django.db.backends.base.base.BaseDatabaseWrapper
```

## The contract on Django ORM, asyncpg and SQLAlchemy

How each element of the contract maps onto the Django ORM, native PostgreSQL (asyncpg) and SQLAlchemy:

### Execution mode

| Django ORM | native PostgreSQL (asyncpg) | SQLAlchemy |
|---|---|---|
| `"threaded"`: Django's ORM is synchronous, its connections are bound to the thread that opened them and its `async_unsafe` check refuses calls from an event loop thread | `"async"` | `"async"` with `AsyncConnection` over an async DBAPI (asyncpg, psycopg); `"threaded"` with a synchronous `Connection`, which is not thread-safe but not thread-bound |
| direct: `direct = True`; the connection is the loop thread's own Django connection, and `async_unsafe` accepts adorm's calls because no running loop is visible during them | not applicable | direct with a synchronous `Connection` |

Under Django's `TestCase` the test's connection is bound to the test thread and wrapped in the test's
transaction. With the event loop on the test thread (pytest-asyncio), direct calls use it: `connect_sync()` takes
that connection, and `begin()`, `commit()` and `rollback()` become savepoint calls inside the test's transaction.
With the loop on another thread (asgiref's `async_to_sync`, which Django's test runner uses for `async def` test
methods), no direct call reaches the test's connection, and adorm does not support that setup.

### Profiles and the `connect` hook

| Django ORM | native PostgreSQL (asyncpg) | SQLAlchemy |
|---|---|---|
| `alias` names a `DATABASES` entry; `connect(profile)` runs in `connect_sync()` on the connection's thread and returns the DB-API connection Django's wrapper uses in place of `get_new_connection()` | `alias` names a `DATABASES` entry, whose keys become `asyncpg.connect()` arguments; `connect(profile)` returns the `asyncpg.Connection` (or an awaitable of it) in place of `asyncpg.connect()` | `connect(profile)` returns the `Connection` (threaded) or an awaitable of the `AsyncConnection` (async) in place of `engine.connect()` |

### Operation translation and results

| Django ORM | native PostgreSQL (asyncpg) | SQLAlchemy |
|---|---|---|
| the model from its label (`apps.get_model`), adorm lookups and expressions turned into Django's, a `QuerySet` evaluated with `values()` | the same Django query set, compiled without a connection by [`DjangoCompiler`](/ref/drivers#adorm-drivers-asyncpg-compiler-djangocompiler-alias-default-server-version-params-none) ([SQL compilation for asyncpg](./sql-compilation)), run with `fetch()` | Core `select()` / `update()` / `insert()` / `delete()` on the mapped tables |
| writes through the model's and the query set's methods (`save()`, `delete()`, `update()` …), Django's signals included | Django's write logic on Django's queries and model instances ([`Writer`](/ref/drivers#adorm-drivers-asyncpg-writes-writer-connection)), each statement compiled and awaited; no model method, override or signal runs, and a write that would run one is refused unless its signal receivers are [covered](/topics/postgresql-driver#signal-receivers) | Core statements |
| rows from `values_list()`, keyed by adorm field names (`orm_names`) | [`DjangoStatement.rows()`](/ref/drivers#rows-records): tuples in select order with Django's converters applied, keyed by adorm field names as on the Django driver | `Row._mapping` to dict |

### Transactions and savepoints

| Call | Django ORM | native PostgreSQL (asyncpg) | SQLAlchemy |
|---|---|---|---|
| `begin()` | an outermost `transaction.atomic()` block | `BEGIN` (with the isolation level of `OPTIONS`) | `conn.begin()` |
| `commit()` / `rollback()` | leaving that block | `COMMIT` / `ROLLBACK`; a `COMMIT` that rolls back a failed transaction raises `TransactionManagementError` | `Transaction.commit()` / `rollback()` |
| `savepoint(name)` | `SAVEPOINT` with the given name | `SAVEPOINT` | `begin_nested()`; the driver keeps a map from name to `NestedTransaction` |
| `release_savepoint(name)` | `RELEASE SAVEPOINT` | `RELEASE SAVEPOINT` | `NestedTransaction.commit()` |
| `rollback_to_savepoint(name)` | `ROLLBACK TO SAVEPOINT` | `ROLLBACK TO SAVEPOINT` | `NestedTransaction.rollback()` |
| `set_options(options, local)` | [`set_options_sql()`](/ref/drivers#adorm-drivers-sql-set-options-sql-options-local) on a cursor: `SET` / `RESET`, or `SET LOCAL` / `SET LOCAL … TO DEFAULT` | the same statements in one simple-protocol message | `set_config()` through `exec_driver_sql()` |

### Cancellation

| Django ORM | native PostgreSQL (asyncpg) | SQLAlchemy |
|---|---|---|
| `cancel_sync()` calls the vendor's [`Vendor.cancel()`](/ref/drivers#adorm-drivers-django-vendors) on the DB-API connection under Django's wrapper (`connection.connection`): psycopg's thread-safe `cancel_safe()` on PostgreSQL, `interrupt()` on SQLite, `KILL QUERY` on MySQL, `cancel()` on Oracle | task cancellation: asyncpg sends a cancel request and handles it before the connection's next statement; `cancel()` cancels the task running the statement | async: task cancellation reaches the DBAPI through SQLAlchemy's greenlet bridge; threaded: `cancel_safe()` on the DBAPI connection |

### Errors

| Django ORM | native PostgreSQL (asyncpg) | SQLAlchemy |
|---|---|---|
| `django.db.IntegrityError` and its siblings carry the database library's error as `__cause__`: by its `sqlstate` on PostgreSQL, by the PEP 249 class and the vendor's error numbers elsewhere | exceptions carry `sqlstate` | `sqlalchemy.exc.DBAPIError` subclasses are named after the PEP 249 classes and carry the DBAPI error as `orig` and `__cause__` |
| connection loss: `Vendor.is_connection_lost()`; on PostgreSQL the default, as psycopg reports a lost connection as `OperationalError` without a SQLSTATE | the default `is_connection_lost()` | the default on PostgreSQL; on other dialects `is_connection_lost()` uses `DBAPIError.connection_invalidated` |

### Backend events

| Django ORM | native PostgreSQL (asyncpg) | SQLAlchemy |
|---|---|---|
| `add_notify_handler()` and `add_notice_handler()` on the psycopg connection; the handlers run on the connection's thread and call `emit()`, during a statement or, between calls, from `idle_sync()` while `idle_fileno()` returns the connection's socket | `add_listener()`, `add_log_listener()` and `add_termination_listener()`; the callbacks run on the event loop | through the raw DBAPI connection (`driver_connection`), as for the DBAPI itself |

### Capabilities

| Django ORM | native PostgreSQL (asyncpg) | SQLAlchemy |
|---|---|---|
| Django's lookups, functions and aggregates, and model methods that run on the driver side | the constructs its SQL compiler emits | the constructs SQLAlchemy Core expresses; Django-specific ones (custom manager methods) are not supported |
