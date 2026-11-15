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
  `orm_name()`, completed with the entries a subclass inherits from its model bases) and the model registry
  `adorm.models.registry` (a `SchemaSource`,
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
- Events in `adorm.events`: `ResultEvent`, `ErrorEvent`, `CallbackEvent`, `BackendEvent` and `EventBus`.
- Executor pool and router in `adorm.executor`: a `Pool` of slots per connection profile (`max_connections`, `0`
  opening connections until the driver reports a refusal with `Driver.is_refusal()`, default
  `adorm.drivers.is_refusal()` on SQLSTATE class 53; `acquire_timeout`) and a `Router` that assigns slots to
  sessions in first-in, first-out order; a refusal also caps a pool below a non-zero `max_connections`, and a
  request whose open was refused keeps its place. An acquisition returns a `SlotLease` with a request queue (`Request`,
  `RequestQueue`) and an event queue (`ResultEvent` / `ErrorEvent` per request, callbacks and backend events),
  and `call()` for one request; requests run in order, in the sender's context, and cancelling a call cancels its
  request. Every acquisition times out with `SessionTimeout`, or `DeadlockError` with `held_sessions` when the flow
  holds a slot (`holding()`, `held_leases()`, `Router.slot()`); when tasks wait for slots that only they hold
  through `Router.slot()` or `Router.run()`, one holder gets `DeadlockError` (with `session` and `timeout`) at
  once. A released slot whose connection is unusable, or that a `begin` left in a transaction, leaves the pool; a
  failing slot fails its queued requests with `InterfaceError`. `close()` waits for slots being discarded and closes
  slots that finish opening afterwards. The router is a `Runner`.
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
- Demo application (`demo.py`, `demo/`): a Django project with clubs, members, activities (with a multi-table
  inheritance child) and billing (with a generic foreign key) on PostgreSQL, the matching adorm models with a
  `check_adorm_models` command comparing them with the Django models, and a FastAPI app whose endpoints build adorm
  operations and report what each available driver does not support; an end-to-end test runs it once per available
  driver, and `ADORM_DEMO_REQUIRED_DRIVERS` fails the suite when a listed driver is missing.
- Test suite on PostgreSQL (Django test project, pytest-django, pytest-asyncio) and CI; short tracebacks, so database
  URLs passed to fixtures do not appear in the output.
- Documentation site (VitePress, `docs/`, published on docs.velis.si): introduction, concepts (sessions,
  slots, pool, drivers), how-to guides and API reference.
