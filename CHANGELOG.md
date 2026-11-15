# Changelog

## 0.0.1 - 2026-11-15

- Package `adorm` (typed, `py.typed`) with optional extras `django` and `asyncpg`.
- Exceptions modelled on Django's: `ObjectDoesNotExist` and `MultipleObjectsReturned` with per-model
  `DoesNotExist` and `MultipleObjectsReturned`, `ValidationError`, the PEP 249 database errors with `original` and
  `sqlstate` (`IntegrityError`, `ProtectedError`, `RestrictedError`, `DataError`, `DatabaseError` for errors adorm
  does not recognise, …), `TransactionClosed`, and the executor errors `SessionTimeout`, `DeadlockError` and
  `OperationTimeout`. `adorm.exceptions.wrap_driver_error()` maps driver errors by SQLSTATE class, then by PEP 249 class name, and
  maps `OSError` to `OperationalError`; non-`Exception` exceptions such as `asyncio.CancelledError` pass through.
- Driver contract in `adorm.drivers`: `Driver` and `DriverConnection` (operation in, plain data out, transaction
  and savepoint calls, connection options, thread-safe `cancel()`, `is_usable()`, `map_error()`,
  `is_connection_lost()`), `Capabilities` (operation kinds, constructs, connection options, backend events,
  savepoints and cancellation, the last two off unless declared) with `check()`, `check_savepoints()` and
  `check_options()`, enforced by `DriverConnection` before every call reaches a driver, the `Operation` and
  `Profile` protocols (with the profile's `connect` hook), `is_connection_error()`, and `ThreadedDriver` /
  `ThreadedConnection`, which run a synchronous target on one thread per connection, or direct on the event
  loop's thread, with the caller's context, cancellation and error mapping. Importing `adorm` imports no ORM.
- Query building blocks in `adorm.query`, without I/O or ORM imports: immutable nodes compared by value, `F`,
  `OuterRef`, `Value`, arithmetic (`CombinedExpression`), `OrderBy`, `Q` with `&`, `|` and `~`, the lookups
  `exact`, `iexact`, `gt`, `gte`, `lt`, `lte`, `in` (values or a query as subquery), `range`, `isnull`,
  `startswith`, `istartswith`, `endswith`, `iendswith`, `contains`, `icontains`, `regex` and `iregex` with relation
  traversal (`club__name`) and a lookup registry, the functions `Lower`, `Upper`, `Length`, `Coalesce`, `Concat`,
  `Now`, `Trunc` and `TruncYear` … `TruncTime`, the aggregates `Count`, `Sum`, `Avg`, `Min` and `Max` with
  `distinct`, `filter` and `default`, `Case` / `When`, `Subquery` and `Exists`, and the immutable `Query` (`filter`,
  `exclude`, `annotate`, `order_by`, `values`, `distinct`, slicing). Every node reports its driver construct names
  through `constructs()`; plain values are copied when a node is built. The common names are importable from
  `adorm`.
- `Query` clauses `values_list()` (`flat`, `named`), `only()`, `defer()`, `select_related()`, `none()`,
  `select_for_update()` (`ForUpdate`: `nowait`, `skip_locked`, `of`, `no_key`) and `|` / `&`, with the construct names
  `clause.only`, `clause.defer`, `clause.select_related` and `clause.select_for_update[.<option>]`; `values()` and
  `values_list()` set the query's `row_format`.
- Operation description in `adorm.operations`: `Select`, `Get`, `First`, `Last`, `Count`, `Exists`, `Create`, `Save`
  (`update_fields`, `force_insert`, `force_update`), `Update`, `Delete` and `GetOrCreate` (`defaults`), immutable and
  compared by value, registered by kind in `OPERATIONS`, satisfying the driver contract's `Operation` protocol. The
  constructors apply Django's restrictions (no slice for `Last`, `Update`, `Delete` and `GetOrCreate`, no
  `distinct(fields)` for `Update` and `Delete`, no field references in inserted values).
- JSON-safe encoding of operations and query nodes (`to_dict()` / `from_dict()`): values are encoded with a pydantic
  `TypeAdapter` of the type the model schema gives their position (`adorm.schema`: `ModelSchema` with `orm_names`,
  `Schema`, `SchemaSource`, `resolve_field_type()`), including a `Value` inside a `Case` or `Coalesce` and lookups on
  annotations; untyped values carry an inferred type name (`adorm.codec`). Lookups declare the type of their
  right-hand side with `Lookup.rhs_type()` and `Lookup.rhs_is_sequence`; the text pattern lookups take a string. A
  field unknown to the schema of a known model, NaN and infinity raise `ValueError`; bytes are base64 at any depth of
  `Annotated`, unions and containers. Decoding re-checks what the node and operation constructors check and raises
  `ValueError`. An `output_type` must be a type `adorm.codec.type_name()` names.
- Model base class `adorm.Model` (pydantic `BaseModel`, `extra="allow"`, by-alias serialisation, input by name or
  alias, no protected `model_` namespace) with `ModelMeta` (label, primary key, `schema_hash`, `orm_names` with
  `orm_name()`, completed with the entries a subclass inherits from its model bases) and the model registry `adorm.models.registry` (a `SchemaSource`,
  `model_schema()`). `from_row()` builds instances with `model_construct()`; fields a row lacks are deferred and
  raise `AttributeError`; relation rows fill the relation cache. Equality and hash by model label and primary key.
  Input accepts `pk` for the primary key field and, from Python, a relation instance for its column; extra values
  may not hide class attributes.
  `save()` writes the fields in `model_fields_set` through a `Save` operation (`update_fields`, `force_insert`,
  `force_update`) and sets the values the target returns. `ForeignKey` descriptors return an awaitable that loads
  the related instance with `Get` and caches it; assigning an instance sets the column. `ReverseOneToOne`
  (`column`) loads and caches the row on the other side of a one-to-one relation. `ReverseForeignKey`
  (`column`) and `ManyToMany` (`through`, `source_column`, `target_column`) carry their join columns and return a
  `QuerySet` of the related rows on the owner's executor. Instances run operations on their `Executor` or the one `set_executor_resolver()`
  supplies.
- Sessions in `adorm.executor` (also importable from `adorm`): `create_session(using, session_name)` returns one
  `Session` per pair while it is referenced (thread-safe; `Session(using, session_name)` returns the same object), `current_session()` reads the context variable that `Session.activate()` sets
  (default `("default", "default")`). `Model.objects`, `session.<ModelName>` and `session.bind()` return a
  `BoundModel`, bound when read and taking no slot. `Session.execute()` runs an operation in the session's
  transaction active in the current context (`bind_transaction()`, `active_transaction()`), else on the `Runner`
  set with `set_runner()`. Importing `adorm` installs an executor resolver that returns the current session.
- `adorm.QuerySet`: a `Query` with the model class and the session that runs it; the building methods of `Query`,
  `async for`, `get()`, `first()`, `last()`, `count()`, `exists()`, `create()`, `get_or_create()`, `update()` and
  `delete()`, rows shaped by the row format, generic `ObjectDoesNotExist` and `MultipleObjectsReturned` raised as the model's own,
  and no operation for reads, `update()` and `delete()` after `none()`. Query sets and bound models copy as themselves
  and pickle.
- Events in `adorm.events`: `ResultEvent`, `ErrorEvent`, `CancelledEvent`, `CallbackEvent`, `BackendEvent` and
  `EventBus`, with a JSON-safe form (`Event.to_dict()` / `Event.from_dict()` with a pluggable value codec,
  `encode_error()` / `decode_error()` keeping the adorm exception class, message, SQLSTATE, sessions, timeout and
  phase). `EventChannel` is the client end of a slot's event queue: it settles the futures of awaited requests
  with their outcome, runs the handlers registered for a callback name and queues the rest, handing unread
  callbacks and backend events to a fallback `EventBus` when it closes. `from_dict()` rejects fields of the wrong
  type with `ValueError`.
- Executor pool and router in `adorm.executor`: a `Pool` of slots per connection profile (`max_connections`, `0`
  opening connections until the driver reports a refusal with `Driver.is_refusal()`, default
  `adorm.drivers.is_refusal()` on SQLSTATE class 53; `acquire_timeout`) and a `Router` that assigns slots to
  sessions in first-in, first-out order; a refusal also caps a pool below a non-zero `max_connections`, and a
  request whose open was refused keeps its place. An acquisition returns a `SlotLease` with a request queue (`Request`,
  `RequestQueue`) and an event channel (`SlotLease.channel`, its queue `events`) that receives exactly one outcome
  per request (`ResultEvent`, `ErrorEvent`, `CancelledEvent`), `CallbackEvent("on_commit")` / `("on_rollback")`
  before the outcome of a successful commit or rollback, and callbacks and backend events (a connection emits only
  `CallbackEvent` and `BackendEvent`; other events and names in `RESERVED_CALLBACKS` are dropped), and `call()` for
  one request (a `CancelledEvent` the caller did not ask for raises `InterfaceError`); requests run in order, in the
  sender's context, and cancelling a call, or `RequestQueue.cancel()`, cancels its request. Every acquisition times out with `SessionTimeout`, or `DeadlockError` with `held_sessions` when the flow
  holds a slot (`holding()`, `held_leases()`, `Router.slot()`); when tasks wait for slots that only they hold
  through `Router.slot()` or `Router.run()`, one holder gets `DeadlockError` (with `session` and `timeout`) at
  once. A released slot whose connection is unusable, or that a `begin` left in a transaction, leaves the pool; a
  failing slot fails its queued requests with `InterfaceError`. `close()` waits for slots being discarded and closes
  slots that finish opening afterwards. The router is a `Runner`.
- Transactions: `adorm.atomic()` and `Session.atomic()` return an `Atomic` block on the current or the given
  session. Entering it acquires a slot through the runner's `slot()` (a `TransactionRunner`) and begins a
  transaction, so `SessionTimeout` and `DeadlockError` are raised at entry; a block inside a transaction of the same
  session is a savepoint on the same slot. Savepoints opened by sibling tasks run one after another, and operations
  other tasks send to the enclosing block wait while a savepoint is open; a wait longer than `acquire_timeout`
  raises `DeadlockError`. The block is the session's active
  transaction for its context and for tasks created inside it. The end waits for the operations already sent, then
  commits (releases the savepoint), or rolls back (to the savepoint) on an exception; `commit()` and `rollback()` end
  it earlier; the exit of a block that another task is ending waits for that end and raises its error. Savepoints
  that other tasks leave open are rolled back before the enclosing block commits. Later operations, also from tasks
  that inherited the context, raise `TransactionClosed`. After an operation of the block failed with a database
  error (including a `NotSupportedError` from the server) or was cancelled after it reached the connection, further
  operations raise `TransactionManagementError` and the end rolls back, raising `TransactionManagementError` on a
  normal exit. In debug mode (`adorm.executor.set_debug()`, by default asyncio's debug mode) the end warns about
  tasks that used the block and are still running. `get_runner()` returns the runner;
  `adorm.executor.slot.cancelled_before_start()` tells whether a cancelled request never reached the connection.
- `on_commit` callbacks: `Atomic.on_commit(func, robust=False)` and `await adorm.on_commit(func, robust=False,
  session=None)` (which runs `func` at once outside a transaction). The callbacks run in registration order once the
  slot reports the commit with `CallbackEvent("on_commit")` and has returned to the pool, outside the transaction in
  the block's context; awaitables are awaited. A released savepoint passes them to its enclosing block; a rollback
  drops them. A failing callback stops the rest and propagates from the end of the block, unless it is `robust`.
- `Pool(commit_timeout=60.0)`: a slot inside a transaction that gets no request within that many seconds rolls the
  transaction back (cancelling a rollback that takes as long), delivers
  `CallbackEvent("on_rollback")`, ends its lease (`SlotLease.aborted`; further requests
  raise `TransactionClosed`) and leaves the pool; `None` sets no limit.
- Connection profiles in `adorm.settings`: `adorm.configure()` (profiles, profile providers, default
  `PoolSettings`, debug switch; validated before anything changes), `ConnectionProfile` (driver as an instance, a
  class or an import path imported on first use; alias, read-only `params` kept out of the representation, pool
  settings and the hooks `connect`, `on_acquire` and `on_release`), and the `ProfileRegistry` behind
  `adorm.settings.profiles` with `register_profile()`, `unregister_profile()`, `get_profile()` and providers that
  create profiles at run time (`add_profile_provider()`, sync or async). `Router(profiles=registry)` creates the pool
  of a profile on its first use, once for concurrent first uses, and closes the pool of a profile that is
  unregistered or replaced; `Router.get_pool()` and `Router.remove()`. Once a profile or provider is configured,
  `get_runner()` returns the running event loop's default router (`adorm.settings.default_router()`,
  `close_pools()`); its pools close when the loop shuts down its asynchronous generators (`asyncio.run()`,
  `asyncio.Runner`, asgiref's `async_to_sync`), and the threaded connections of loops closed without that close when
  the next default router is created. A coroutine-function hook or `connect` with a threaded driver raises
  `TypeError`. `on_acquire(connection, profile, session)` runs when a slot is assigned to a session, within the
  acquisition's timeout; `on_release` runs before a usable slot returns to the pool, within the new
  `Pool(release_timeout=30.0)`. A hook that fails, times out or is cancelled takes its slot out of the pool. Hooks
  receive the target's connection through the new `DriverConnection.target` and run through
  `DriverConnection.call_hook()`, on the connection's thread for a threaded driver. How-to: several database users
  sharing one login role and one pool, with `SET ROLE` in `on_acquire` and `RESET ROLE` in `on_release`.
- `adorm generate` (console script `adorm`, also `python -m adorm`): writes one module of adorm models per Django app
  and a package `__init__.py` with `MODELS`, from the Django models' field types, choices, constraints, defaults,
  foreign key columns, forward and reverse relations (`ForeignKey`, `ReverseForeignKey`, `ReverseOneToOne`,
  `ManyToMany`), multi-table inheritance and proxies. Names that clash with pydantic or adorm (`model_dump`, `json`,
  `copy`, `schema` …) get a `_` suffix, an alias with the original name and an `orm_names` entry, and so does a name
  with leading underscores (`_order` of `order_with_respect_to` is `order_`); field paths split so that a segment may
  end in `_` (`copy___title`). Modules and names the module imports are aliased when a field or relation has their
  name. Relations that reference a field other than the target's primary key (`to_field`), generic relations and
  forward relations to models that are not generated are reported as warnings. Each model carries a schema hash
  (`adorm.codegen.django.schema_hash()`). `--check` exits with status 1 when a file is missing, stale, obsolete or
  not written by the generator; such files are never replaced. Errors, including a cycle of base classes between
  modules and a model the generator cannot describe, exit with status 2. API in `adorm.codegen` (model description,
  rendering, files) and `adorm.codegen.django`.
- Managers and driver methods: `adorm.Manager(orm_name=None, *, method_manager=None)` declared in a model class body (`objects = Manager()`
  on `Model`), bound to the current session when read, with `queryset_class`, `get_queryset()`, `from_queryset()`,
  `QuerySet.as_manager()` and query set methods forwarded to the manager; `BoundModel` gains `name`, `orm_name`,
  `get_queryset()` and access to the model's other managers, `Session.bind(model, manager)` binds any of them.
  `Query(model, manager=...)` and `Create(..., manager=...)` name the target ORM's manager the driver runs through
  (construct `clause.manager`), so overridden `get_queryset()`, `create()`, `delete()` … apply; `create()` on such a
  manager passes the values that are not fields. Reverse foreign keys and many-to-many relations start from the
  target's `objects` manager. `adorm.driver_method` declares a method the driver runs on the manager's
  `method_manager`, sent as the operation `Call` with its own `manager`, so a manager with only driver methods keeps
  its queries on every driver (arguments typed by `adorm.schema.MethodSchema` in `ModelSchema.methods`, results with
  `ModelRow` turned into instances, also in mapping values; model instances as arguments raise `TypeError`);
  `DriverConnection.execute()` runs only methods the registered model declares
  (`adorm.models.find_driver_method()`). `adorm generate` writes managers, manager and query set classes and the
  driver methods marked with `adorm.expose` or listed in the `ADORM_EXPOSE` setting; `adorm.client_base` names a
  hand-written base class for client-side methods. Only overrides of `adorm.codegen.django.ROUTED_METHODS` that the
  client base does not define give a generated manager its ORM name; a public method that is neither exposed nor
  ported is reported as `CodegenWarning`. The schema hash covers managers with an ORM name.
- Demo application (`demo.py`, `demo/`): a Django project with clubs, members, activities (with a multi-table
  inheritance child) and billing (with a generic foreign key) on PostgreSQL, the adorm models generated from it by
  `adorm generate` into `demo/models` (setup checks them with `--check` and compares them with the Django models with
  the `check_adorm_models` command), and a FastAPI app. Its `/api/operations` endpoints build adorm operations and
  report what each available driver does not support; its `/api/data` endpoints run the generated models through
  sessions (header `X-Adorm-Session`), transactions, savepoints, `on_commit` callbacks and concurrent tasks on the
  executor and answer the calls the driver received (a slot timeout answers 503, a database deadlock 409, both with
  an `ExecutorError` body); `/api/executor` reports the pools. The demo's in-memory driver
  (`demo.api.memory`, loaded from the demo database, with transactions, savepoints and row locks with deadlock
  detection) runs them until an ORM driver is available. An end-to-end test runs the demo and its data endpoints once
  per available driver, and `ADORM_DEMO_REQUIRED_DRIVERS` fails the suite when a listed driver is missing.
- Test suite on PostgreSQL (Django test project, pytest-django, pytest-asyncio) and CI; short tracebacks, so database
  URLs passed to fixtures do not appear in the output.
- Documentation site (VitePress, `docs/`, published on docs.velis.si): introduction, concepts (sessions,
  slots, pool, drivers, transactions), how-to guides and API reference.
- `adorm.drivers.sql`: `Statement` and `numbered_placeholders()`, which turns `%s` / `%%` placeholders into
  PostgreSQL's `$n`, optionally with casts. `adorm.drivers.asyncpg.compiler`: `DjangoCompiler` compiles Django
  selects, updates, deletes and aggregates for asyncpg on a database wrapper that never connects (server version
  given, `select_for_update()` checked against `in_transaction`, parameter limit `MAX_QUERY_PARAMS`, empty
  statements for queries that affect no rows); `bind_param()` turns Django's psycopg parameters into asyncpg values
  with the cast psycopg's client-side or server-side binding implies; `DjangoStatement.rows()` returns asyncpg rows
  as Django returns them on psycopg. The `asyncpg` extra installs Django and psycopg 3 as well.
- Documentation: "SQL compilation for asyncpg" topic.
