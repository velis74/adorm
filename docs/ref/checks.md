# Schema checks

`adorm.check_schema()` and the `adorm check-schema` command compare models with the database and the driver's
models; `adorm.checks` holds what they report. The schema hash a driver compares at each operation is described with
[`Driver.check_schema_hash()`](/ref/drivers#check-schema-hash-label-expected); the [schema checks topic](/topics/schema-checks)
shows both in use.

```python
from adorm import check_schema, SchemaCheckError, SchemaMismatchError, SchemaProblem
from adorm.checks import check_models, SchemaProblemKind, table_check
```

## Command line

### `adorm check-schema`

```text
adorm check-schema [-h] -m MODULE [--using USING] [--settings SETTINGS] [--configure MODULE] [--pythonpath PYTHONPATH]
                   [label ...]
```

The command:

1. puts the current directory and each `--pythonpath` directory on the import path;
2. with `--settings` or `DJANGO_SETTINGS_MODULE`, sets Django up (as [`adorm generate`](/ref/codegen#adorm-generate)
   does) and configures adorm from the Django settings unless the app `adorm.contrib.django` does
   ([`ensure_configured()`](/ref/settings#adorm-contrib-django-ensure-configured)); without either, Django is not
   imported;
3. imports every `--configure` module (one that calls `adorm.configure()` or registers profiles, for a driver not
   configured from Django settings) and every `-m`/`--models` module (the package `adorm generate` wrote, which
   registers its models);
4. stops with status 2 without a configured profile or profile provider;
5. runs [`check_schema()`](#adorm-check-schema-models-none-session-none-raise-on-problems-false) on a session of the
   profile `--using` (default `default`) and closes the pools.

Each `label` is an app label, selecting every registered model of the app, or a model label (`app_label.Model`);
without labels every registered model is checked. `python -m adorm check-schema` is the same command.

Each problem goes to standard error as one line (`str(problem)`), followed by `<n> problems in <m> models.`; without
problems standard output has `<m> models checked, no problems found.`

| Exit status | Meaning |
|---|---|
| 0 | every model matches |
| 1 | problems were found |
| 2 | an error: a usage error, no profile configured, Django missing with `--settings`, a label that selects no registered model, an unknown profile, a driver that does not check tables, a database error, any other exception (printed as `adorm check-schema: <type>: <message>`) |

```text
$ adorm check-schema --settings myproject.settings -m myproject.adorm_models clubs
clubs.Club.city: the column clubs_club.city does not exist
clubs.Member: generated with schema hash ce2ac49a8d9e7e5b, the driver's model has 0d4f8e2b6a913c57
2 problems in 3 models.
$ echo $?
1
$ adorm check-schema --configure myproject.adorm_config -m myproject.adorm_models
14 models checked, no problems found.
```

## API

### `adorm.check_schema(models=None, *, session=None, raise_on_problems=False)`

Compares `models` (model classes or registered labels; default: every registered model) with the database and the
driver of `session` (default: the [current session](/ref/sessions#adorm-current-session)) and returns a tuple of
[`SchemaProblem`](#adorm-schemaproblem) values in the order of `models`, empty when they match. It reads the
database's catalogue and changes nothing.

Each model is checked with one [`CheckTable`](/ref/operations#adorm-operations-checktable-model-fields-many-to-many)
operation ([`table_check(model)`](#adorm-checks-table-check-model)) executed on the session: on a slot of its own
outside a transaction, in the session's transaction inside one. The driver's
[`check()`](/ref/drivers#check-operation-schema-hashes-none) compares the model's schema hash with its own first; a
[`SchemaMismatchError`](/ref/exceptions#adorm-schemamismatcherror-message-none-model-none-expected-none-actual-none-driver-none-reason-none),
and a model the driver's target does not have, become a `schema_hash` problem; the model's tables are then not
compared. A model given twice is checked once. A problem of a table several models share (a proxy model and its
concrete model, a multi-table inheritance parent's table, a link table both ends of a many-to-many relation name) is
reported once, for the first of them. Field names in the problems are adorm names.

With `raise_on_problems` it raises [`SchemaCheckError`](/ref/exceptions#adorm-schemacheckerror-message-none-problems)
with the problems instead of returning them; without problems it returns `()`.

A label without a registered model raises `LookupError`, a class that is not a model with `_meta` `TypeError`, a
driver that does not declare `check_table` (the demo's memory driver) `NotSupportedError`. Slot and database errors
pass through.

```python
from demo.models import MODELS

problems = await adorm.check_schema(MODELS)
for problem in problems:
    print(problem)

await adorm.check_schema([Club, "clubs.Member"], session=adorm.create_session("reports"), raise_on_problems=True)
```

### `adorm.SchemaProblem`

`SchemaProblem(model, kind, table=None, column=None, field=None, expected=None, actual=None, detail=None)`

A difference `check_schema()` found for the model `model` (a label): its [`kind`](#adorm-checks-schemaproblemkind),
the database `table` and `column`, the adorm name of the model's `field` (or relation), the values compared as
`expected` and `actual`, and for a `schema_hash` problem without `actual` the `detail` of why the driver has no hash
(the [`reason`](/ref/exceptions#adorm-schemamismatcherror-message-none-model-none-expected-none-actual-none-driver-none-reason-none)
of the error); each is `None` where it does not apply. A frozen dataclass; an unknown `kind` raises
`ValueError`. `str()` gives one line:

| `kind` | `str()` |
|---|---|
| `schema_hash` | `clubs.Member: generated with schema hash ce2ac49a8d9e7e5b, the driver's model has 0d4f8e2b6a913c57`, or `clubs.Ghost: cannot be compared with the driver's model: the target has no model clubs.Ghost` (`detail`) |
| `missing_table` | `clubs.Club: the table clubs_club does not exist` |
| `missing_column` | `clubs.Club.city: the column clubs_club.city does not exist` |
| `column_type` | `clubs.Club.name: the column clubs_club.name is character varying(50), the model expects varchar(100)` |
| `nullability` | `clubs.Member.birth_date: the column clubs_member.birth_date is NOT NULL, the model expects NULL` |
| `unknown_field` | `clubs.Club.city: the driver's model has no such field` |
| `required_column` | `clubs.Club: the column clubs_club.code (text) is NOT NULL without a default; the model does not set it` |

### `adorm.checks.SchemaProblemKind`

`Literal["schema_hash", "missing_table", "missing_column", "column_type", "nullability", "unknown_field",
"required_column"]`:

| Kind | Meaning | `expected`, `actual` |
|---|---|---|
| `schema_hash` | the model's schema hash differs from the driver's model's, or the driver has no hash for the model: no such model, or it could not be computed (`detail` says which) | the model's hash, the driver's (`None` when it has none) |
| `missing_table` | the database has no table `table` (resolved on the connection's `search_path`) | |
| `missing_column` | the table has no column `column` for `field` | |
| `column_type` | the column's type differs | the type the ORM declares, the type the catalogue prints |
| `nullability` | the column takes `NULL` and the model expects `NOT NULL`, or the other way round | `"NULL"` or `"NOT NULL"` |
| `unknown_field` | the driver's model has no field, or no many-to-many relation, `field` | |
| `required_column` | the table has a column the model does not set that is `NOT NULL` without a default, so an insert of the model fails | `actual`: the column's type |

### `adorm.checks.check_models(models, execute)`

The problems of `models` (model classes with `_meta`) as `check_schema()` finds them, with `execute`, an async
callable, executing each model's `table_check()` operation (`check_schema()` passes the session's `execute`, a
pool's [start-up check](/ref/pool#adorm-executor-pool-driver-profile-max-connections-0-acquire-timeout-30-0-commit-timeout-60-0-release-timeout-30-0-purge-stale-connections-at-300-0-check-schema-false)
one that runs on its slot). A `SchemaMismatchError` of the checked model becomes a `schema_hash` problem; other
errors pass through.

```python
problems = await check_models([Club, Member], session.execute)
```

### `adorm.checks.table_check(model)`

The [`CheckTable`](/ref/operations#adorm-operations-checktable-model-fields-many-to-many) operation of the model class
`model`: its label, the ORM names of its fields and of its many-to-many relations (`ModelMeta.orm_name()`). An
abstract model raises `TypeError`.

```python
table_check(Club)
# CheckTable(model='clubs.Club', fields=('id', 'name', 'slug', 'founded', 'city', 'address', 'disciplines'),
#            many_to_many=('members',))
```
