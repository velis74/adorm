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
  `TypeAdapter` of the type the model schema gives their position (`adorm.schema`: `ModelSchema`, `Schema`,
  `SchemaSource`, `resolve_field_type()`), including a `Value` inside a `Case` or `Coalesce` and lookups on
  annotations; untyped values carry an inferred type name (`adorm.codec`). Lookups declare the type of their
  right-hand side with `Lookup.rhs_type()` and `Lookup.rhs_is_sequence`; the text pattern lookups take a string. A
  field unknown to the schema of a known model, NaN and infinity raise `ValueError`; bytes are base64 at any depth of
  `Annotated`, unions and containers. Decoding re-checks what the node and operation constructors check and raises
  `ValueError`. An `output_type` must be a type `adorm.codec.type_name()` names.
- Model base class `adorm.Model` (pydantic `BaseModel`, `extra="allow"`, by-alias serialisation, input by name or
  alias) with `ModelMeta` (label, primary key) and the model registry `adorm.models.registry` (a `SchemaSource`,
  `model_schema()`). `from_row()` builds instances with `model_construct()`; fields a row lacks are deferred and
  raise `AttributeError`; relation rows fill the relation cache. Equality and hash by model label and primary key.
  Input accepts `pk` for the primary key field and, from Python, a relation instance for its column; extra values
  may not hide class attributes.
  `save()` writes the fields in `model_fields_set` through a `Save` operation (`update_fields`, `force_insert`,
  `force_update`) and sets the values the target returns. `ForeignKey` descriptors return an awaitable that loads
  the related instance with `Get` and caches it; assigning an instance sets the column. `ReverseForeignKey`
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
- Demo application (`demo.py`, `demo/`): a Django project with clubs, members, activities (with a multi-table
  inheritance child) and billing (with a generic foreign key) on PostgreSQL, the matching adorm models with a
  `check_adorm_models` command comparing them with the Django models, and a FastAPI app whose endpoints build adorm
  operations and report what each available driver does not support; an end-to-end test runs it once per available
  driver, and `ADORM_DEMO_REQUIRED_DRIVERS` fails the suite when a listed driver is missing.
- Test suite on PostgreSQL (Django test project, pytest-django, pytest-asyncio) and CI; short tracebacks, so database
  URLs passed to fixtures do not appear in the output.
- Documentation site (VitePress, `docs/`, published on docs.velis.si): introduction, concepts (sessions,
  slots, pool, drivers), how-to guides and API reference.
