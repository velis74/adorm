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
  `encode_error()` / `decode_error()` keeping the adorm exception class, message, SQLSTATE, sessions, timeout,
  phase and the message, code and params of each validation error, params through the event's value codec).
  `EventChannel` is the client end of a slot's event queue: it settles the futures of awaited requests with their
  outcome, runs the handlers registered for a callback name and queues the rest, handing unread callbacks and backend
  events to a fallback `EventBus` when it closes. `from_dict()` rejects fields of the wrong
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
  modules and a model the generator cannot describe, exit with status 2. The current directory and the
  `--pythonpath` directories go to the front of the import path, ahead of installed packages of the same name. API
  in `adorm.codegen` (model description, rendering, files) and `adorm.codegen.django`.
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
- Native PostgreSQL driver `adorm.drivers.asyncpg.AsyncpgDriver` (async, an `asyncpg.Connection` per slot, no threads
  of its own) for reads: the operations and constructs of the Django driver, with the same query translation, compiled
  on the event loop by `DjangoCompiler` and run as one statement each (`get` as Django's `get()`, `count` over the
  query as a subquery, `exists` as Django's `exists()`); transactions with the `DATABASES` entry's isolation level;
  cancellation of the running statement, by the awaiting task or by `cancel()` from any thread (SQLSTATE `57014`);
  a statement cancelled before it finished makes the connection unusable, so the slot closes it instead of
  returning it to the pool with a transaction and locks the server may still hold.
  Connections are opened from the profile's `DATABASES` entry and `params` (`connect_arguments()`: the libpq `OPTIONS`
  asyncpg reads through its `dsn` (`DSN_PARAMETERS`; other keys raise `NotSupportedError`), `-c` settings, connect
  timeout) with Django's session time zone and `assume_role`, or by the profile's `connect` hook; the compiler follows
  the server's version (`server_version_num()`). Errors map as on the Django driver (asyncpg's `InternalClientError`
  as `InterfaceError`); a manager's `get_queryset()` that accesses the database raises `NotSupportedError` and leaves
  the connection in the pool. `DjangoCompiler` gains `params` (keys replacing the entry's), `settings_dict` and
  `compile_count()`; `DjangoStatement.rows()` returns `timestamptz` values in the connection's time zone (naive
  without `USE_TZ`), as Django's psycopg loader does. The demo runs on the driver (`--driver asyncpg`).
- Documentation: "The native PostgreSQL driver" topic.
- Django ORM driver `adorm.drivers.django.DjangoDriver` (threaded, one Django connection per slot thread) for reads:
  `select`, `get`, `first`, `last`, `count` and `exists` with every lookup, relation paths in adorm names (renamed
  fields and reverse accessor names translated), `Q`, a query as subquery in `in`, `Subquery`, `Lower` / `Upper`,
  ordering with NULL placement, `distinct()` and `DISTINCT ON`, slicing, `values()` / `values_list()`, `only()` /
  `defer()`, `none()`, `select_related()` (nested rows in the same query) and the query's Django manager; transactions
  for reads and cancellation of a running statement. Django's exceptions become adorm's (`map_django_error()`), and
  every call sees the caller's active language and time zone (`adopt_context_locals()`). The driver supports
  PostgreSQL aliases; profile `params` and the `connect` hook apply per connection, with Django configuring the
  hook's connection. `adorm.drivers.django.translation` turns an adorm `Query` into a Django query set without a
  connection: each filter is one Django `filter()` call, a model instance compared with a foreign key stands for its
  `to_field`, and a typed `Value` gets the Django field for its type (`output_field()`). The `django` and `asyncpg`
  extras require asgiref 3.x.
- `Query.filters`: one condition per `filter()` / `exclude()` call, so a multi-valued relation is joined once per
  call as in Django; `query & other` keeps both queries' filters. `Query.where` is the filters joined with AND.
- `First` (`first()`) refuses a sliced query without an ordering of its own with `TypeError`.
- `ThreadedConnection.prepare_call_sync()`: a hook that prepares each call's context on the connection's thread.
- Documentation: "The Django driver" topic.
- Driver conformance suite (`tests/conformance`, part of the test run): a catalogue of operations over the demo's
  schema and data set that every demo driver (Django, asyncpg, memory) runs, each case with the operation kinds and
  constructs it uses; a driver that declares them all must return the catalogue's result, any other driver must
  refuse it with `NotSupportedError` before a connection is reached; a case may list drivers that refuse it while
  executing, for limits that depend on the schema or the rows (`refusals`). A case no driver runs has a Django ORM
  mirror that returns its expected result over the seeded database. Coverage checks fail when an operation kind or
  construct has no case, or a driver declares a name adorm does not know. The same drivers run the failure tests:
  refusals while the pool is full and inside transactions, concurrent tasks on one transaction, cancellation of a
  waiting operation (also racing its completion; the released slot keeps no transaction or lock open) and
  connections lost mid-operation, and the transaction tests: nested `atomic()` blocks commit and roll back on a
  driver with savepoints and are refused on one without, and an undeclared connection option is refused.
- `Driver.check()`: the capability check (and the `call` method check) that `DriverConnection.execute()` runs;
  `Router.run()` runs it before acquiring a slot, so an unsupported operation neither waits for nor opens one.
  `Capabilities.check()`, `check_savepoints()` and `check_options()` take the driver's name, and the
  `NotSupportedError` they raise carries `driver` and `unsupported` (the missing names).
- Construct names `path.nested` (a field path with several segments, reported by `F` and by a query's `values()`,
  `only()`, `defer()` and `distinct()` fields; `adorm.query.NESTED_PATH`) and `clause.annotate` (a query with
  annotations); the Django and asyncpg drivers declare both. `adorm.query.CLAUSES` lists the clause construct names
  and `adorm.query.construct_names()` every construct name an operation can report.
- Demo memory driver: a negated condition keeps a row whose field in a lookup is NULL, as Django's `exclude()` does;
  a model instance in a condition stands for its primary key; an update reaches the rows a multi-table inheritance
  parent and child share; inserting or deleting a row of a multi-table inheritance child and deleting a row a foreign
  key references raise `NotSupportedError` (checked for every row before any is deleted); relation paths and
  annotations are refused before execution (`path.nested`, `clause.annotate`).
- Demo reads on two drivers: the API loads every available driver and registers each as the connection profile named
  after it (the active driver is also `default`). The read endpoints of `/api/data` run on the driver the header
  `X-Adorm-Driver` names (422 for a name that is not an available driver), the writes on the active driver; `Traced`
  reports the `driver`. `GET /api/data/compare/members` reads one page of members on every available driver
  concurrently and reports whether they agree, with an adorm error of one read in that read's `error`. Tests run the
  reads on the Django and asyncpg drivers in one process: the same answers, concurrent reads on both, a full pool of
  one driver not delaying the other, cancellation of a waiting read cancelling its statement, and no slot left in use.
- Django driver writes and transactions: `create` (through the query's manager), `save` (`update_fields`,
  `force_insert`, `force_update`; an instance with a primary key is saved with only its fields loaded, and inserted
  when its row is missing), `update`, `delete` (Django's collector: cascades, `SET_NULL`, `ProtectedError` and
  `RestrictedError` with the referencing objects read again in full), `delete_instance` (the model's own `delete()`,
  an override included), `get_or_create`, `update_or_create`, `select_for_update()` with `nowait`, `skip_locked`, `of`
  and `no_key`, and savepoints. A transaction is an outermost Django atomic block and a savepoint a Django savepoint in
  it, so Django's `on_commit()` callbacks run on the slot's thread after the commit's outcome is delivered and before
  the next call, out of reach of cancellation, and are dropped with a rolled-back savepoint; a failing callback is
  logged and does not undo the commit. A database error inside a savepoint is rolled back to it and the enclosing
  block goes on. A commit of a transaction Django marked for rollback, or that PostgreSQL aborted, rolls back and
  raises `TransactionManagementError`; `begin()` inside a Django atomic block of a direct connection's thread raises
  it too. Django's signals fire on the driver side only. Every ORM call runs on the profile's alias.
  `adorm.drivers.django.READ_CAPABILITIES` holds the reads and arithmetic (`expression.CombinedExpression`);
  `model_row()` turns a Django instance into a `ModelRow`; `adorm.drivers.django.translation` gains
  `model_queryset()`, `field_kwargs()`, `lookup_kwargs()` and `instance_row()`.
- `adorm.operations.UpdateOrCreate` (`defaults`, `create_defaults`), `QuerySet.update_or_create()` and the bound
  model's `update_or_create()`; the demo memory driver executes it.
- `adorm.operations.DeleteInstance` (kind `delete_instance`) and `Model.delete()`: deletes the instance's row as the
  target deletes one instance and, when rows were deleted, sets its primary key to `None`; the demo memory driver
  executes it. A `ProtectedError` or `RestrictedError` from `QuerySet.delete()` or `Model.delete()` carries instances
  of the registered models, and one without a SQLSTATE leaves the transaction usable.
- A read of rows with `select_for_update()` outside a transaction raises `TransactionManagementError` in the session,
  on every driver.
- `ThreadedConnection.after_call_sync(fn)`: work that runs on the connection's thread after a call's outcome is
  delivered and before the next call, out of reach of `cancel()`.
- Native PostgreSQL driver writes and transactions: the driver declares the Django driver's `CAPABILITIES` and
  savepoints. `adorm.drivers.asyncpg.writes.Writer` runs `create`, `save`, `update`, `delete`, `delete_instance`,
  `get_or_create` and `update_or_create` with Django's write logic on Django's queries and model instances (multi-table
  inheritance inserts and updates, `RETURNING` of the values the database sets, fields' `pre_save()`,
  `order_with_respect_to`, Django's deletion collector with every built-in `on_delete` handler, generic relations and
  `ProtectedError` / `RestrictedError` with complete instances), each statement awaited on asyncpg, with the Django
  driver's results. No model method, manager or query set override and no Django signal runs: a write whose model,
  manager or query set overrides the Django method it would call, and a delete reaching an `on_delete` handler other
  than Django's, raise `NotSupportedError` before anything is written. Statements Django runs in one atomic block run
  in a transaction of the driver's own outside `adorm.atomic()` (a cancellation gives the connection up, so the server
  rolls it back) and in savepoints where Django uses them. Savepoints are tracked by name; `begin()` inside a
  transaction and a `COMMIT` that PostgreSQL turns into a rollback raise `TransactionManagementError`. A write of
  several statements that fails partway through inside a transaction, outside a savepoint, leaves the transaction
  rollback-only, as Django's `needs_rollback`: later operations and the commit raise `TransactionManagementError`.
  Delete statements stay within asyncpg's 32 767 parameters. `adorm.drivers.asyncpg.writes.Collector` is Django's
  deletion collector on a `Writer`. `DjangoCompiler.compile_insert()`, `compile(..., returning=...)` and
  `DjangoStatement.returned()` compile inserts and `RETURNING` clauses (an update's `RETURNING` on Django 6; on
  Django 5.2 `returning` raises `NotSupportedError`). The conformance suite holds every driver to
  what it does with Python code of the target ORM: model `save()` / `delete()` overrides, query set `create()` /
  `update()` overrides, custom `on_delete` handlers, Django's signals and `on_commit()` callbacks, `pre_init` /
  `post_init` (`tests/conformance/test_driver_code.py`).
- Outcome of a cancellation: the `CancelledError` of a cancelled operation or `SlotLease.call()` carries a
  `CancellationOutcome` (`NOT_STARTED`, `SUCCEEDED`, `FAILED`, `UNKNOWN`), read with `adorm.cancellation_outcome(exc)`,
  also through the `TimeoutError` of `asyncio.timeout()`. A cancellation racing the completion of a request is
  `UNKNOWN` until the slot delivers the request's own outcome, which `Router.run()` waits for. `Atomic.outcome`
  reports the `TransactionOutcome` of a block (`COMMITTED`, `ROLLED_BACK`, `UNKNOWN` for a cancelled or lost commit;
  `ROLLED_BACK` for a savepoint that was rolled back). After a confirmed commit whose end was cancelled, the block's
  `on_commit` callbacks run in a task of their own; a commit by another task cancelled before it was confirmed makes the
  owner's end of the block raise `TransactionManagementError`. A cancelled `ThreadedConnection` request method returns
  the result, or raises the error, of a call that finished before the cancellation reached the target, so the Django
  driver reports `SUCCEEDED`, `FAILED` and `COMMITTED` for statements that completed despite a cancellation.
- Connection options and operation timeouts (`adorm.executor.options`): `statement_timeout`, `lock_timeout`,
  `idle_in_transaction_session_timeout` and adorm's `acquire_timeout` and `commit_timeout` in layers, the last one
  winning: the profile (`ConnectionProfile.options`, its pool settings), the session (`Session.set_options()`,
  `Session.options`) and `with adorm.connection_options(...)` blocks, inherited by tasks. Every operation's request
  carries the database options in effect (`Request.options`, `SlotLease.call(..., options=...)`); the slot remembers
  what it set on its connection (`Slot.options`) and calls `set_options()` only with the differences, `None` resetting
  an option: `SET` / `RESET` outside a transaction, `SET LOCAL` inside one, restored by a rollback to a savepoint.
  A transaction's `begin` and `commit` carry the options too, so no transaction runs with options another session
  left on the slot. `on_acquire` runs with the session's options set; after each profile hook the slot sets its
  options again (`Slot.forget_options()`). Time limits are checked up to PostgreSQL's 2147483.647 s
  (`MAX_TIMEOUT_OPTION`). A request cancelled while the slot sets its options is `NOT_STARTED`.
  An option the driver does not declare raises `NotSupportedError` before a slot is taken. `Router.acquire()` and
  transactions take `acquire_timeout` from the options in effect; a lease's `commit_timeout` is the option in effect
  when it is acquired. The Django and the native PostgreSQL drivers declare the three PostgreSQL time limits
  (`adorm.drivers.sql.POSTGRESQL_OPTIONS`, `set_options_sql()`). `async with adorm.timeout(5):` (`Timeout`) wraps
  `asyncio.timeout()` and raises `OperationTimeout` with the `phase` (`"waiting_for_slot"` or `"executing"`), the
  `operation` and its `session` (new attribute, encoded with the error) of the operation the expiry interrupted, also
  in tasks created in the block; `cancellation_outcome()` of the error is that operation's outcome.
- Slot lifecycle: adorm sets how long a connection lives. `Pool(purge_stale_connections_at=300.0)` (also in
  `PoolSettings`, `None` to keep idle slots) closes a slot that stayed idle that long; a request takes the idle slot
  returned last and never one past that age. Acquiring a slot does nothing to its connection. At the end of a lease
  the slot closes its connection and leaves the pool when the lease ended inside a transaction, a request failed
  with an error `is_connection_lost()` accepts (whatever `is_usable()` says), or the connection ran custom SQL
  (`DriverConnection.mark_custom_sql()`, `ran_custom_sql`), also after a commit; `Slot.close_reason` holds the
  `CloseReason` (`adorm.executor`) and `Slot.idle_since` the time it became idle. `DriverConnection.settled` and
  `settle()` report work that runs after a call's outcome (`ThreadedConnection.after_call_sync()`); a lease's release
  does not wait for it, and the slot decides once it has run, within `release_timeout` (else `CloseReason.UNSETTLED`);
  `Pool.close()` waits for such slots. The Django driver marks custom SQL that an operation or an `on_commit()`
  callback runs through the connection's cursor, judged by the nearest caller in Django's query compiler or a
  database backend (cursor instrumentation in between does not count), and runs its connections without
  `CONN_MAX_AGE`, `CONN_HEALTH_CHECKS` and Django's connection pool (`OPTIONS["pool"]`), so a connection the slot
  closes ends on the server; a `direct` driver's thread gets its wrapper's settings back when the slot closes.
- Test mode (`adorm.testing.django`; the `adorm.testing` package imports no ORM): while Django's test environment is
  set up, the Django driver finds the test's connection when a slot opens one (`find_test_database()`: the thread that
  waits in `async_to_sync()` under Django's async test methods, else the event loop's thread). Inside a `TestCase` every
  slot shares it (`SharedConnectionMixin`, `SharedTestConnection`), one call at a time; a transaction is a savepoint in
  the test's transaction, and a transaction or an operation outside one that starts while another flow's transaction is
  open raises `TransactionOverlapError` (new), as do an operation of a transaction while a transaction begun inside it
  is open, and a transaction that ends, or whose slot closes, while one begun inside it is open. Statements of Django
  ORM code on other threads take turns on the shared connection and follow the same rule. Operations outside a
  transaction run in savepoints of their own, a failed operation inside a transaction rolls back to its innermost
  savepoint at once, a call cancelled while it waits for the connection ends its wait, and connection options are
  applied per call with `SET LOCAL`. Django's `on_commit()` callbacks stay with the test's transaction, so
  `captureOnCommitCallbacks()` works unchanged. A profile with `params`, a `connect` hook or `on_acquire` /
  `on_release` hooks raises `NotSupportedError` inside a `TestCase`. Inside a `TransactionTestCase` the slots open
  connections of their own to the process's test database; `manage.py test --parallel` and pytest-django work
  unchanged. A connection opened in a `TestCase` test is unusable after that test, one opened in another Django test
  while a `TestCase` transaction is open. The native PostgreSQL driver raises `NotSupportedError` inside a `TestCase`.
- Demo writes and transactions on every driver: registrations lock the activity with `select_for_update()` and take
  `nowait`, `lock_timeout` (`adorm.connection_options()`) and `timeout` (`adorm.timeout()`);
  `POST /api/data/invoices/settlements` pays the earliest due open invoices, read with
  `select_for_update(skip_locked=True)`. `ExecutorError` gains `sqlstate`, `phase`, `operation` and `outcome`; a lock
  that is not available (SQLSTATE `55P03`) answers 409, `OperationTimeout` 504 and `NotSupportedError` 501. The demo
  memory driver executes `select_for_update()` with `nowait` and `skip_locked`. Tests run the write flows in process
  with the Django and the asyncpg driver active, and the end-to-end test runs them on every driver.
- `cancellation_outcome()` of a cancellation that reaches `atomic()` while it waits for a slot is `NOT_STARTED`.
- Annotations, expressions and aggregates on the Django and native PostgreSQL drivers: the functions `Length`,
  `Coalesce`, `Concat`, `Now`, `Trunc` and `TruncYear` … `TruncTime`, the aggregates `Count` (also `Count("*")`),
  `Sum`, `Avg`, `Min` and `Max` with `distinct`, `filter` and `default`, `Case` / `When`, `Exists` and `~Exists`, and
  `OuterRef` in a `Subquery` or `Exists`, translated on the enclosing query's model; `OuterRef(OuterRef(...))`
  (`OuterRef.levels`) refers to a query further out. `Now()` is the statement's start time.
- `Query.alias()` / `QuerySet.alias()` (`Query.aliases`, construct `clause.alias`): named expressions that
  conditions and orderings use, not returned in the rows; selecting one raises `ValueError`.
- Grouping: `values()` followed by an aggregate annotation groups the rows by the selected names
  (`Query.group_by`, `Query.is_grouped`, `Query.aggregate_names()`, construct `clause.group_by`), as Django's
  `values().annotate()`; an annotation without an aggregate added later joins the grouping (`Query.group_mark`).
  `First`, `Last` on a grouped query need an ordering, `Update` refuses one.
- Filters and annotations apply in the order they were added (`Query.filter_marks`), as in Django: an aggregate
  annotation after a filter on a multi-valued relation aggregates only the related rows the filter matched.
- `QuerySet.aggregate(*args, **kwargs)` and the `Aggregate` operation (kind `aggregate`): aggregates over the rows,
  over a subquery of sliced, distinct or grouped rows; a positional aggregate takes `Aggregate.default_alias`.
  `aggregate()` without arguments is `{}`; a query distinct on fields and names Django refuses as column aliases raise
  at construction. An empty query of plain aggregates is answered without an operation. The native PostgreSQL driver compiles it with
  `DjangoCompiler.compile_aggregate()` (`DjangoAggregation`), the Django driver translates it with
  `adorm.drivers.django.translation.aggregation()` (`AGGREGATES`).
- Lookup keys built at run time: `adorm.query.resolve_lookup(target, key)` resolves a key such as
  `f"{field}__{op}"` against a model class, label, `Query` or `QuerySet` (annotation names included) and the model
  registry, without an operation, and returns a `ResolvedLookup` (model, field, lookup class, field type, keys inside a
  JSON field, `value_type` of the right-hand side); `adorm.query.check_lookups(target, condition)` checks every lookup
  and field reference of a condition. `adorm.FieldError` (a `ProgrammingError`, with `model` and `key`) reports an
  unknown field or relation, an unknown lookup name (with the closest name), a segment after a field that is neither
  a relation nor a JSON field, and a lookup a relation does not take (`Lookup.on_relations`: `exact`, `gt`, `gte`,
  `lt`, `lte`, `in`, `isnull`). Every driver raises it for such a key: the Django and native PostgreSQL drivers map
  Django's `FieldError` and `FieldDoesNotExist` to it and refuse a path past a scalar field before Django sees it,
  the demo's memory driver resolves keys with `resolve_lookup()`, and `Driver.check()` raises it in place of
  `NotSupportedError` when the operation also lacks a construct. `adorm.schema.resolve_field_type()` resolves paths
  as `resolve_lookup()` does and raises `FieldError`. A `FieldError` without a SQLSTATE leaves the transaction it is
  raised in usable.
- `QuerySet.prefetch_related(*lookups)` (`QuerySet.prefetch_lookups`), `adorm.Prefetch(lookup, queryset=None,
  to_attr=None)`, `adorm.GenericPrefetch(lookup, querysets, to_attr=None)` and
  `adorm.prefetch_related_objects(instances, *lookups, executor=None)`: related objects of forward and reverse
  foreign keys, reverse one-to-one, many-to-many and generic foreign keys read with one `Select` per relation,
  level and batch of `adorm.models.prefetch.BATCH_SIZE` keys (the links first for a many-to-many relation, the
  content types first for a generic foreign key), on the
  executor of the instances, after `async for`, `get()`, `first()`, `last()` and `in_bulk()`. Prefetched rows fill
  the relation caches; the query set of a reverse or many-to-many relation reads them for `async for`, `count()` and
  `exists()`. A `Prefetch` query set filters, orders and continues with its own lookups; `to_attr` stores the rows as
  an extra value, which a later path segment may follow, also across the models of a generic foreign key.
- `adorm.GenericForeignKey(ct_field="content_type", fk_field="object_id")`: `await obj.item` reads the content type,
  finds the registered model (`ModelRegistry.get_for_content_type()`) and loads the row, cached for the field values it
  was loaded for; a missing row is `None`. `adorm.models.get_generic_foreign_keys()`, `CONTENT_TYPE_MODEL`.
  `adorm generate` writes it for Django's `GenericForeignKey` (`RelationSpec` kind `generic_foreign_key`, `ct_field`).
- `QuerySet.in_bulk(id_list=None, *, field_name="pk")` and the `InBulk` operation (kind `in_bulk`): instances keyed by
  the primary key, a unique field or a unique foreign key (keyed by its column), which the Django and native
  PostgreSQL drivers check as Django does (`adorm.drivers.django.translation.check_in_bulk_field()`); the native
  PostgreSQL driver runs one statement per `adorm.drivers.asyncpg.IN_BULK_BATCH_SIZE` ids; the demo's memory driver
  takes the primary key.
- `QuerySet.create_from_parent(parent, **values)` and the `CreateFromParent` operation (kind `create_from_parent`):
  the row of a multi-table inheritance child for an existing parent row, the child's table only
  (`adorm.models.parent_link()`); the Django driver saves it with `save_base(raw=True)`, the native PostgreSQL driver
  inserts the child's table and refuses a child model that overrides `save_base()`.
- `Model.full_clean(*, exclude=None, validate_unique=True, validate_constraints=True)` and the `FullClean` operation
  (kind `full_clean`): Django's `full_clean()` of the instance over its stored row, errors as `ValidationError` keyed by
  adorm names, each error with Django's message, `code` and `params` (`unique_check` in adorm names), identical on
  both drivers. Both drivers map Django's `ValidationError` raised by any operation the same way, keyed by the
  operation's model (`adorm.drivers.django.translation.operation_model()`); on the Django driver this includes a
  model's `save()` that calls `full_clean()`. Every query of `Model.full_clean()` runs on the profile's alias, ahead of
  the project's Django routers (`adorm.drivers.django.translation.pinned_alias()`); a `full_clean()` that a model's
  `save()` calls itself queries the alias the project's routers choose. The native PostgreSQL driver runs
  `full_clean()` as Python on the event loop with `DjangoCompiler.replay()` (`StatementNeeded`, a `BaseException`;
  `MAX_VALIDATION_STATEMENTS`), awaiting each statement the validation runs.
- `IntegrityError` describes the violated constraint: `constraint_name`, `table_name`, `schema_name`, `columns`,
  `values` (the key's values as PostgreSQL prints them), `detail`, the `key` property, and `model` and `fields` (the
  label of the table's model and its adorm field names of the key's columns, following `db_column` and renamed
  fields). The Django and the native PostgreSQL drivers report the same values: `map_django_error()` passes
  `adorm.drivers.postgresql.integrity_details` to `wrap_driver_error(exc, details=...)` and maps the columns with
  `adorm.drivers.django.translation.table_model()` and `column_fields()`. `adorm.drivers.postgresql` also has
  `parse_key_detail()` and `references_key()`. The memory driver fills `model`, `fields`, `columns` and, for a
  duplicate primary key, `values` and `detail`. Encoded errors carry them, and `decode_error()` rejects malformed
  ones.
- Lookup `overlap` (`adorm.query.Overlap`, construct `lookup.overlap`): an array field with at least one item in
  common with an iterable of values, a query of one column or an array expression; each value is encoded as an item
  of the field's type. The Django and the native PostgreSQL drivers run it as Django's `ArrayField` `overlap`
  (PostgreSQL's `&&`), the memory driver on lists. `Lookup.applies_to()` tells whether a lookup applies to a field's
  type; `resolve_lookup()` and `check_lookups()` raise `FieldError` for `overlap` on a field that is not an array or a
  key inside a JSON field. A lookup that a JSON or hstore field does not have raises `FieldError` on both SQL drivers,
  also on an annotation that refers to such a field, instead of reading the name as a key. The demo's `Club` has the
  array field `disciplines`.
- `QuerySet.bulk_create(objs, batch_size=None, ignore_conflicts=False, update_conflicts=False, update_fields=None,
  unique_fields=None)` (also on bound models and managers) and the `BulkCreate` operation (kind `bulk_create`, the
  constructs `clause.on_conflict.ignore` and `clause.on_conflict.update`): the instances inserted with one operation,
  each with the fields it set, and returned with the values the database set, as Django's `bulk_create()`. The Django
  driver calls `bulk_create()` of the base manager or the query's manager; the native PostgreSQL driver runs Django's
  `bulk_create()` steps for the installed Django version, at most `MAX_QUERY_PARAMS` parameters per statement, and
  refuses a manager or query set that overrides `bulk_create()`. Several statements of one call run in one
  transaction on both drivers. Both refuse what Django refuses on PostgreSQL with `ProgrammingError`
  (`adorm.drivers.django.translation.check_bulk_create()`, `bulk_create_options()`). The demo's memory driver inserts
  every row or none and refuses the conflict options. Without `update_conflicts`, `update_fields` and `unique_fields`
  are ignored, and with no instances only `batch_size` is checked, as in Django.
- Set operations `Query.union(*others, all=False)`, `intersection()` and `difference()`, also on query sets, bound
  models and managers (constructs `clause.union`, `clause.intersection`, `clause.difference`; `Query.combinator`,
  `combinator_all`, `combined_queries`, `is_combined`, `adorm.query.COMBINATORS`), with Django's handling of empty
  queries (`none()` and empty slices). After a set operation only ordering, slicing, `values()` / `values_list()`, `none()`, further set
  operations and the reads `Select`, `Get` (without conditions), `First`, `Last`, `Count` and `Exists` apply; other
  query methods, `prefetch_related()` and the operations `InBulk`, `Aggregate`, `Update`, `Delete`, `GetOrCreate`
  and `UpdateOrCreate` raise `NotSupportedError`. The codec encodes the combined queries at the level of the combined
  query, each typed by its own model. The Django and the native PostgreSQL drivers translate them into Django's
  `union()`, `intersection()` and `difference()`, also as subqueries, and an ordering by a column that `values()`
  names after the set operation orders by the first query's column at the same position; the native PostgreSQL
  driver leaves out empty combined queries as Django does. The demo's memory driver compares the rows column by
  column by value and combines empty queries as Django compiles them.
