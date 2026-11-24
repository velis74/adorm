# Changelog

## 0.1.1 - 2026-11-24

- Connection profile hook `check_operation(operation, session)` (`ConnectionProfile.check_operation`, an import path
  in the `ADORM` setting; type `adorm.settings.OperationCheck`): a plain function called for every operation of the
  profile's sessions, in the awaiting task's context, before the operation reaches a slot or a transaction and before
  any I/O, on every bundled driver and inside `atomic()` blocks. An exception it raises propagates unchanged and
  nothing reaches the database; inside a block, the block stays usable. `adorm.executor.Pool.check()` runs it. The
  pool's start-up schema check does not.
- `Operation.models`: the labels of the models whose tables an operation reads or writes (relation paths, subqueries,
  many-to-many through tables, `select_related()`, parent tables of multi-table inheritance, concrete models of
  proxies, the cascades of a delete), `None` when adorm cannot know them (driver methods, third-party operations, a
  path through a relation the target ORM knows and adorm does not).
- `ModelMeta.delete_models`: the models deleting rows of a model reads or writes, by Django's deletion collector
  (`on_delete`), written by `adorm generate` (`adorm.codegen.django.delete_models_of()`,
  `ModelSpec.delete_models`). Generate the models again to get it; without it `Operation.models` of a delete is
  `None`.
- Tenant relations: a read-only reverse relation from a shared model to a model whose rows live in several tenant
  schemas. Declared on the Django model with `adorm.contrib.django.TenantRelation(to, field=, schema=,
  tenant_models=, session=)`, which adds no column, migration or system check; `adorm generate` checks it and emits
  `adorm.TenantRelation` (`RelationSpec` kind `"tenant"`; the schema path, or an expression of `F`, `Value`,
  `Concat`, `Lower`, `Upper`, `Replace`, `Left`, `Right`, `LPad`, `Coalesce` and `Cast`). `get_tenant_relations()`.
- `adorm.TenantQuerySet`, what a tenant relation returns on an instance: one `Select` lists the instance's schemas,
  one statement (`UNION ALL` of the schemas' tables) reads the rows, with `filter()`, `exclude()`, `annotate()`,
  `order_by()`, slicing, `values()`, `values_list()`, `only()`, `defer()`, `select_related()`, `first()`, `last()`,
  `get()`, `count()`, `exists()`, `aggregate()` and `schemas()` across all schemas. Writes through the relation,
  `distinct()`, `prefetch_related()`, `select_for_update()` and set operations raise `NotSupportedError`.
- An instance read in a tenant schema keeps it (`ModelState.tenant_schema`, `Model.from_row(tenant_schema=)`): its
  `save()`, `delete()`, `full_clean()`, `refresh_from_db()` and loads of relations to the schema's models run in that
  schema, on any session. Instances equal and hash by schema too when they have one.
- `adorm.TenantSchema` and `Query.in_tenant_schema(schema, models)`: the tables of the models qualify with the schema
  wherever the query reads them, on the Django driver for every Django backend (the backend's `quote_name()`) and on
  the native PostgreSQL driver (`adorm.drivers.django.tenants`). `Save`, `DeleteInstance` and `FullClean` take
  `tenant_schema`; the query writes refuse a query in a tenant schema. Construct `clause.tenant_schema`.
- `Operation.schemas` and `Operation.unqualified_models`: the schemas an operation names and the models whose tables
  it leaves to the search path. The schema-per-tenant how-to's `check_operation` reads `unqualified_models`.
- `aggregate()` over a set operation (`Aggregate` of a combined query): the expressions aggregate the combined rows,
  named by their columns. Construct `aggregate.combined`.
- How-to schema per tenant: the tenant in the session name, refusing tenant tables without a tenant, and reading
  across tenants.

## 0.1.0 - 2026-11-20

- Breaking: the native PostgreSQL driver refuses a write Django would send `pre_save`, `post_save`, `pre_delete` or
  `post_delete` for, with `NotSupportedError` before anything is written, when the signal has a live receiver for the
  sender. Such a write used to run without the receivers. List the receivers whose work the application does itself
  in the new profile setting `ConnectionProfile.covered_receivers` (dotted paths, `module.qualname`), or write the
  model on a Django driver profile, where the receivers run. A model with delete receivers is not fast-deleted.
- Breaking: `build_lookup()` keeps `exact` and `iexact` with `None` as `Exact` / `IExact` nodes (`rhs_accepts_none`)
  instead of building `IsNull`. On a field, relation or expression it still matches NULL; at a key inside a JSON
  field (`address__note=None`) it matches JSON `null`, as in Django. `address=None` on the JSON field itself matches
  SQL NULL.
- Transforms in field paths: `year`, `iso_year`, `quarter`, `month`, `week`, `week_day`, `iso_week_day`, `day`,
  `hour`, `minute`, `second`, `date` and `time`, chained, in lookup keys, `F()`, `values()`, `values_list()`,
  `order_by()`, `distinct()` and grouping, computed in the current time zone for a datetime. `register_transform()`,
  `Transform`, `TRANSFORMS`; construct names `transform.<name>`.
- JSON fields: list indexes in field paths (`tags__0`) and the lookups `contained_by`, `has_key`, `has_keys` and
  `has_any_keys`.
- Database functions: `Cast`, `Collate`, `Greatest`, `Least`, `NullIf`, `JSONArray`, `JSONObject`, `Extract` and
  the `Extract*` functions, the math functions (`Abs` to `Sqrt`, `Pi`, `Random`), the text functions (`Chr`, `Ord`,
  `Left`, `Right`, `LPad`, `RPad`, `LTrim`, `RTrim`, `Trim`, `Repeat`, `Replace`, `Reverse`, `StrIndex`, `Substr`),
  the hash functions (`MD5`, `SHA1` to `SHA512`), `UUID4` and `UUID7`.
- Own expressions: a `Func`, `Lookup` or `Transform` subclass declares its SQL in `function`, `template` (and
  `arg_joiner`) or `templates`, one template per database vendor; the Django driver and the native PostgreSQL driver
  render it and declare its construct name on the vendors it has a template for, also for classes defined after the
  driver was imported. `adorm.drivers.django.register_translation()` registers a translation into a Django expression
  of a function, lookup or transform for both drivers; `register_sqlite_function()` registers a Python function on
  the Django driver's SQLite connections. `adorm.query.sql_constructs()`, `adorm.drivers.Constructs`. Recipe: own expressions.
- `UUID4` and `UUID7` with Django 5.2 and 6: Django's function where Django and the database have it, else
  `GEN_RANDOM_UUID()` and PostgreSQL 18's `UUIDV7()` on PostgreSQL (`UUID7` is refused before 18) and SQLite functions
  of adorm's, generated in Python, on SQLite.
- `adorm.uuid7()`: a version 7 UUID generated in Python (`uuid.uuid7()` from Python 3.14 on), e.g. as a field
  default (`default=adorm.uuid7`).
- A transform's `applies_to()` and `output_type()` get the field's type without `Annotated` and `Optional`.
- `Concat` without `output_type` concatenates arguments of different text fields on the Django-based drivers.
- `Model.refresh_from_db(fields=None)`: reloads the loaded fields, or the named ones, and clears cached relations and
  prefetched rows, as Django does.
- An unknown last segment of a key that names its lookup (`joined__yeer__exact`) is reported as an unsupported
  transform, as `resolve_lookup()` does. `orm_path()` takes `implicit`.
- The asyncpg compiler writes the time zone name and the `DATE_TRUNC` precision into the SQL as literals, so a
  `distinct()` on a datetime transform matches its `order_by()`.

## 0.0.5 - 2026-11-19

- Connection option `search_path` (PostgreSQL's `search_path`): a list or tuple of schema names, set like `role` from
  the profile, the session, a `connection_options()` block or an `on_acquire` hook. Recipe: schema per tenant.
- `ConnectionProfile.content_types_key(profile, session)`: splits the profile's content type cache, e.g. per tenant
  when each tenant's schema has its own content type table.
- A threaded connection asks `idle_fileno()` only once `connect_sync()` has succeeded, not before it runs or after
  it fails.

## 0.0.4 - 2026-11-18

- `adorm.testing.wait_for_lock()` reports a lock wait of every request a slot runs, not only of operations: a
  `commit` that waits for the lock of a deferred foreign key is reported. `LockWait.request` is the waiting request;
  `LockWait.operation` is `None` for requests that are not an `execute`.
- A slot closed in the loop step in which its connection call ends delivers that call's outcome to the caller and
  stops serving. Before, the caller got `TypeError: invalid exception object` or `Slot.close()` and the event loop's
  shutdown never finished.

## 0.0.3 - 2026-11-17

- Deadlock detection checks each pool of the waiting tasks once per pass instead of once per waiting task: with
  many tasks waiting for a slot, waiting for and releasing a slot no longer costs time in proportion to the number
  of waiting tasks times the pool size.

## 0.0.2 - 2026-11-17

- PyPI project page: installation, an example and links to the documentation; documentation and changelog links
  and the author's e-mail in the package metadata.

## 0.0.1 - 2026-11-17

- Initial release.
