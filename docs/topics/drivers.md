# Drivers

::: info Status
The installed version provides the driver contract (`adorm.drivers`), the event types (`adorm.events`) and the
[slots and pool](/topics/pool) that call drivers. The Django ORM and native PostgreSQL drivers are not part of it
yet.
:::

A driver executes adorm operations on one target: an ORM such as Django's or SQLAlchemy, or a database library
such as asyncpg. The core of adorm (queries, models, sessions, the pool) never imports a target; everything it
knows about one comes through the contract on this page, so the same application code runs on any driver that
implements it.

```text
slot ── operation ──▶ DriverConnection ──▶ target (Django ORM, asyncpg, SQLAlchemy …)
  ▲                        │
  └── result / error ──────┤   return value of execute(), an adorm exception
  └── events ──────────────┘   emit(CallbackEvent | BackendEvent)
```

## Operations in, plain data out

A slot hands its connection one operation at a time. An [operation](/ref/operations) is adorm's description of a
read or write; it names its `kind` (`"select"`, `"update"` …) and the constructs it uses (`"lookup.icontains"`,
`"function.Lower"` …), models by their label and fields by their adorm names. The driver translates it into the
target's terms and executes it. A [`Query`](/ref/query) is an immutable tree of [expressions](/ref/expressions) and
[lookups](/ref/lookups): `walk()` visits its nodes and `constructs()` names the constructs it uses, the names an
operation reports to `Capabilities`.

`execute()` returns plain data: rows as dicts keyed by adorm field names, a scalar or a count. The core builds
model instances from the rows, so no target object crosses the contract.

An adorm name can differ from the target's: a generated model renames fields that clash with pydantic (`json_` for
Django's `json`). The driver translates every segment of a field path, every key of the values it writes and every
key of the rows it returns with the `orm_names` of the model that segment reaches: `ModelMeta.orm_name()` on a model
class, `ModelSchema.orm_names` from a [schema source](/ref/encoding#adorm-schema-schemasource). A name `orm_names`
does not list is the same on both sides.

```python
Note._meta.orm_name("json_")                                     # "json"
registry.model_schema("library.Note").orm_names["copy_"]         # "copy"
# copy___title: "copy_" on library.Note, then "title" on library.Book -> Django's "copy__title"
```

Operations convert to JSON-safe data with every value encoded by its field's type ([Operation
encoding](/ref/encoding)), so a driver that forwards them to another process sends `operation.to_dict(schema)` and
the receiving side executes `Operation.from_dict(data, schema)`.

```python
rows = await connection.execute(operation)    # [{"id": 1, "name": "Ana"}, ...]
```

Transactions are calls of their own: `begin()`, `savepoint(name)`, `release_savepoint(name)`,
`rollback_to_savepoint(name)`, `commit()` and `rollback()`. The slot decides when to call them; a driver never
opens or ends a transaction on its own.

## Events

Besides return values a connection reports events through the `emit` callable it receives when it opens:

| Event | Meaning | Example |
|---|---|---|
| `CallbackEvent(name, payload)` | a point in the connection's life the client runs callbacks for | `on_commit` |
| `BackendEvent(name, channel, payload)` | something the database or the target raised on its own | PostgreSQL `NOTIFY` |

The slot forwards them over its event queue together with `ResultEvent` and `ErrorEvent`, the outcome of each
request. On the client side an [`EventBus`](/ref/events#adorm-events-eventbus) dispatches them to subscribers.
Signals of the target (Django's `post_save`, SQLAlchemy's ORM events) and the target's own `on_commit` hooks run
on the driver side only; the client sees what the driver emits.

## Capabilities

A driver declares what it supports in a [`Capabilities`](/ref/drivers#adorm-drivers-capabilities) value:
operation kinds, constructs, connection options, backend event names, savepoints and cancellation. `savepoints`
and `cancellation` default to `False`, so a driver declares only what it implements.

`DriverConnection` checks every call against the declaration before the driver sees it: an operation that uses an
unsupported kind or construct, an unsupported connection option, or a savepoint call on a driver without
savepoints raises `NotSupportedError` and nothing is executed. The checks live in the concrete `execute()`,
`set_options()` and savepoint methods; a driver implements `_execute()`, `_set_options()` and the `_savepoint()`
family, for threaded and async drivers alike.

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
`expression.CombinedExpression`) and `clause.<clause>` (`clause.distinct_on`, `clause.select_for_update` …). Query
nodes report theirs through `constructs()`; the [expression reference](/ref/expressions) lists them.

## Execution mode

A driver's `mode` says how its connections run:

- **`"threaded"`**: a synchronous target runs on a dedicated thread per connection. Every call of a connection
  runs on that thread, so a target whose connections are bound to the thread that opened them (Django) works
  unchanged. [`ThreadedDriver`](/ref/drivers#adorm-drivers-threadeddriver) and
  [`ThreadedConnection`](/ref/drivers#adorm-drivers-threadedconnection) implement the bridge: the driver
  writes plain synchronous methods.
- **`"async"`**: native coroutines on the event loop, no threads.

A threaded driver can also run **direct**: with `direct = True` on the driver, every `*_sync` method runs on the
event loop's own thread instead of a dedicated one. This is for one task per event loop, such as a Celery task:
each call blocks the loop while it runs. During a direct call no running event loop is visible on the thread, so a
target's check against use from an event loop (Django's `async_unsafe`) accepts adorm's calls and still applies
to everything else. A running direct call cannot be interrupted by cancelling a task, since the loop is blocked;
`cancel()` from another thread cancels it.

Every `*_sync` call runs in a copy of the awaiting task's context: context variables set by the caller (Django's
active translation, for example) are visible to the target; changes made inside the call do not flow back.

A connection belongs to one slot, which awaits one call at a time.

## Cancellation

Cancelling the task that awaits `execute()` cancels the statement running on the target. The call raises
`asyncio.CancelledError`; the connection stays usable and the slot rolls back an open transaction. When the
statement completed just before the cancellation arrived, its effect remains and its result is discarded.

`cancel()` cancels the running statement from any thread without cancelling the awaiting task: the call then
raises `OperationalError` with SQLSTATE `57014` (`query_canceled`). A cancellation request reaches only the
statement it was meant for; a threaded connection starts no further call until `cancel_sync()` returns.

`cancel()` returns without waiting. Called on the event loop's thread, a threaded connection runs
`cancel_sync()` on a thread of its own, so a slow cancel request never blocks the loop. Cancelling the awaiting
task does the same.

A threaded call that does not finish within the driver's `cancel_grace` after a cancellation leaves the
connection unusable: `is_usable()` returns `False` and the slot drops it. A driver that declares
`cancellation=False` does not stop a running statement; cancelling the task waits for the call to finish.

## Errors

Every error a connection raises is an adorm [exception](/ref/exceptions); `asyncio.CancelledError` passes
through. `DriverConnection.map_error()` converts the target's exceptions, by default with
[`wrap_driver_error`](/ref/exceptions#adorm-exceptions-wrap-driver-error-exc): SQLSTATE first, then the PEP 249
class name. A driver overrides it where the target reports an error without a SQLSTATE that adorm classifies
differently, for example a cancelled statement.

`DriverConnection.is_connection_lost()` decides whether a mapped error leaves the connection unusable. The
default, [`is_connection_error`](/ref/drivers#adorm-drivers-is-connection-error-exc), follows PostgreSQL:
`InterfaceError`, and `OperationalError` without a SQLSTATE or with a connection or shutdown SQLSTATE. A target
that reports ordinary statement errors as `OperationalError` without a SQLSTATE (`sqlite3` for a missing table
or a locked database, MySQL drivers for lock wait timeouts) overrides it, so that only a lost connection counts.
A slot drops an unusable connection; the next request gets a fresh one.

## Profiles

A connection is opened for a [`Profile`](/ref/drivers#adorm-drivers-profile): its `name` (the `using` value), the
target's database `alias`, driver `params` and an optional `connect` hook. When `connect` is set, the driver
opens the target's connection with `connect(profile)` instead of its default: a threaded driver calls it in
`connect_sync()` on the connection's thread, an async driver awaits its result in `open_connection()`.

## The contract on Django ORM, asyncpg and SQLAlchemy

The tables below map each element of the contract onto the Django ORM, native PostgreSQL (asyncpg) and
SQLAlchemy.

### Execution mode

| Django ORM | native PostgreSQL (asyncpg) | SQLAlchemy |
|---|---|---|
| `"threaded"`: Django's ORM is synchronous, its connections are bound to the thread that opened them and its `async_unsafe` check refuses calls from an event loop thread | `"async"` | `"async"` with `AsyncConnection` over an async DBAPI (asyncpg, psycopg); `"threaded"` with a synchronous `Connection`, which is not thread-safe but not thread-bound |
| direct: `direct = True`; the connection is the loop thread's own Django connection, and `async_unsafe` accepts adorm's calls because no running loop is visible during them | not applicable | direct with a synchronous `Connection` |

Under Django's `TestCase` the test's connection is bound to the test thread and wrapped in the test's
transaction. When the event loop runs on the test thread (pytest-asyncio), this maps onto direct calls: the
connection's `connect_sync()` takes that same connection, and `begin()`, `commit()` and `rollback()` become
savepoint calls inside the test's transaction. When the
loop runs on another thread (asgiref's `async_to_sync`, as Django's test runner does for `async def` test
methods), no direct call reaches the test's connection; the installed contract does not provide that placement.

### Profiles and the `connect` hook

| Django ORM | native PostgreSQL (asyncpg) | SQLAlchemy |
|---|---|---|
| `alias` names a `DATABASES` entry; `connect(profile)` runs in `connect_sync()` on the connection's thread and returns the DB-API connection Django's wrapper uses in place of `get_new_connection()` | `await connect(profile)` returns the `asyncpg.Connection` in place of `asyncpg.connect()` | `connect(profile)` returns the `Connection` (threaded) or an awaitable of the `AsyncConnection` (async) in place of `engine.connect()` |

### Operation translation and results

| Django ORM | native PostgreSQL (asyncpg) | SQLAlchemy |
|---|---|---|
| the model from its label (`apps.get_model`), adorm lookups and expressions turned into Django's, a `QuerySet` evaluated with `values()` | SQL built from the model metadata (table and column names) by Django's or SQLAlchemy's compiler, run with `fetch()` | Core `select()` / `update()` / `insert()` / `delete()` on the mapped tables |
| rows from `values()`, keys renamed from ORM to adorm field names (`orm_names`) | `Record` to dict | `Row._mapping` to dict |

### Transactions and savepoints

| Call | Django ORM | native PostgreSQL (asyncpg) | SQLAlchemy |
|---|---|---|---|
| `begin()` | `set_autocommit(False)` | `Transaction.start()` | `conn.begin()` |
| `commit()` / `rollback()` | `commit()` / `rollback()`, then `set_autocommit(True)` | `Transaction.commit()` / `rollback()` | `Transaction.commit()` / `rollback()` |
| `savepoint(name)` | `SAVEPOINT` with the given name | `SAVEPOINT` | `begin_nested()`; the driver keeps a map from name to `NestedTransaction` |
| `release_savepoint(name)` | `RELEASE SAVEPOINT` | `RELEASE SAVEPOINT` | `NestedTransaction.commit()` |
| `rollback_to_savepoint(name)` | `ROLLBACK TO SAVEPOINT` | `ROLLBACK TO SAVEPOINT` | `NestedTransaction.rollback()` |
| `set_options(options, local)` | `set_config(name, value, local)` on a cursor | `set_config()` | `set_config()` through `exec_driver_sql()` |

### Cancellation

| Django ORM | native PostgreSQL (asyncpg) | SQLAlchemy |
|---|---|---|
| `cancel_sync()` calls `cancel_safe()` on the psycopg connection under Django's wrapper (`connection.connection`); psycopg's cancel is thread-safe and returns once the request is delivered | task cancellation: asyncpg sends a cancel request and waits for the statement to end; `cancel()` cancels the task running the statement | async: task cancellation reaches the DBAPI through SQLAlchemy's greenlet bridge; threaded: `cancel_safe()` on the DBAPI connection |

### Errors

| Django ORM | native PostgreSQL (asyncpg) | SQLAlchemy |
|---|---|---|
| `django.db.IntegrityError` and its siblings carry the psycopg error, with its `sqlstate`, as `__cause__` | exceptions carry `sqlstate` | `sqlalchemy.exc.DBAPIError` subclasses are named after the PEP 249 classes and carry the DBAPI error as `orig` and `__cause__` |
| connection loss: the default `is_connection_lost()`; psycopg reports a lost connection as `OperationalError` without a SQLSTATE | the default `is_connection_lost()` | the default on PostgreSQL; on other dialects `is_connection_lost()` uses `DBAPIError.connection_invalidated` |

### Backend events

| Django ORM | native PostgreSQL (asyncpg) | SQLAlchemy |
|---|---|---|
| `add_notify_handler()` on the psycopg connection; the handler runs on the connection's thread while a statement executes and calls `emit()` | `add_listener()`; the callback runs on the event loop | through the raw DBAPI connection (`driver_connection`), as for the DBAPI itself |

### Capabilities

| Django ORM | native PostgreSQL (asyncpg) | SQLAlchemy |
|---|---|---|
| Django's lookups, functions and aggregates, and model methods that run on the driver side | the constructs its SQL compiler emits | the constructs SQLAlchemy Core expresses; Django-specific ones (custom manager methods) are not supported |
