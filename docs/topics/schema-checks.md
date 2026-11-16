# Schema checks

adorm does not create or alter tables: the target ORM's migrations own the schema, and adorm assumes the tables
exist. Two checks catch a schema that has drifted from the adorm models:

- the **schema hash**, compared when a model is used on a driver: an operation on a model generated from another
  version of the ORM model fails with `SchemaMismatchError`, every other model keeps working;
- the **table check**: `adorm.check_schema()`, `adorm check-schema` and a pool's start-up check (the pool setting
  `check_schema`) compare each model with the database's tables and columns and list every difference.

## The schema hash at use

[`adorm generate`](/howto/generating-models#schema-hash) writes the hash of each Django model into the generated
model's `_meta.schema_hash`. A driver that knows its target's models computes the same hash
([`Driver.schema_hash()`](/ref/drivers#schema-hash-label)); the Django and the native PostgreSQL drivers compute it
from the installed Django models. Before an operation runs, the driver compares the two for every model the operation
reaches ([`operation_models()`](/ref/drivers#adorm-drivers-operation-models-operation)): its own, those of
subqueries and `union()` parts, and those its relation paths in filters, annotations, ordering, `values()` and
`select_related()` lead to:

```python
await Club.objects.count()
# SchemaMismatchError: the adorm model clubs.Club was generated with schema hash e8c2eed312ab7b68, the django
# driver's model has 51f0c9a4b2d71e03: run adorm generate

await Member.objects.count()                              # a model that matches is not affected
await Membership.objects.select_related("club").first()   # SchemaMismatchError for clubs.Club
await Member.objects.filter(memberships__club__city="Koper").count()   # SchemaMismatchError for clubs.Club
```

- The driver computes the hash of a label at the label's first use and keeps it; the model's hash is compared at every
  use, so a reloaded module with a new hash is compared with the driver's at once.
- A model the target lacks fails the same way, with `actual` `None` and the `reason` in the error.
- The hashes compared are the client's: those of the model registry of the process by default. A driver executing
  operations for clients in other processes receives each client's hashes when it connects
  ([`Driver.check(operation, schema_hashes=...)`](/ref/drivers#check-operation-schema-hashes-none)).
- The check runs before the operation waits for a slot. Inside a transaction it leaves the transaction usable: the
  operation never reached the connection.
- A model without a hash (written by hand) and a driver whose `schema_hash()` returns `None` (the demo's memory driver)
  are not checked.

```python
try:
    await Club.objects.create(name="Piran Rowing")
except adorm.SchemaMismatchError as exc:
    log.error("regenerate the adorm models: %s (driver %s)", exc.model, exc.driver)
```

## The table check

`adorm.check_schema()` compares models with the database of a session's profile. For each model it executes one
[`CheckTable`](/ref/operations#adorm-operations-checktable-model-fields-many-to-many) operation, which checks the
schema hash too, and reads the database's catalogue for:

- the model's table, and the tables of multi-table inheritance parents that hold its inherited fields;
- every column of the model's fields: that it exists, its type and whether it takes `NULL`;
- the automatic link tables of its many-to-many relations;
- columns the model does not set that are `NOT NULL` without a default, since every insert of the model fails on
  them.

It returns the [`SchemaProblem`](/ref/checks#adorm-schemaproblem) values it finds, an empty tuple when everything
matches. A column of the table that the model does not know and that takes `NULL` or has a default is not a problem.

```python
from myproject.adorm_models import MODELS

problems = await adorm.check_schema(MODELS)
for problem in problems:
    print(problem)
# clubs.Club.city: the column clubs_club.city does not exist
# clubs.Member.level: the column clubs_member.level is character varying(10), the model expects varchar(20)
# billing.Invoice: the column billing_invoice.currency (character(3)) is NOT NULL without a default; the model does not set it
```

Run it where the application starts, with `raise_on_problems=True` to refuse to start on a mismatch. The check takes
a slot for each model's `check_table` operation (one catalogue query per table of the model); it changes nothing.

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

import adorm

from myproject.adorm_models import MODELS


@asynccontextmanager
async def lifespan(app: FastAPI):
    await adorm.check_schema(MODELS, raise_on_problems=True)   # SchemaCheckError lists every problem
    yield
    await adorm.settings.close_pools()


app = FastAPI(lifespan=lifespan)
```

A profile other than `default` is checked through a session of it:

```python
await adorm.check_schema(MODELS, session=adorm.create_session("reports"))
```

## At pool start-up

The pool setting `check_schema` runs the table check without code in the application. With `"warn"` or `"raise"`, a
pool checks every registered model before it serves its first acquisition, once per pool, on one of its slots;
concurrent first operations and transactions wait for it within their acquisition timeout.

- `"warn"` logs the problems as one warning on the `adorm.executor` logger and goes on;
- `"raise"` makes every acquisition of the pool raise `SchemaCheckError` with the problems, so no operation runs on
  drifted tables;
- a check that fails with an error (the database is unreachable, the driver does not declare `check_table`) raises
  that error, and the next acquisition checks again.

```python
adorm.configure(profiles={...}, pool={"check_schema": "raise"})       # every profile
adorm.configure(profiles={"reports": {"driver": AsyncpgDriver, "pool": {"check_schema": "warn"}}})

# settings.py
ADORM = {"pool": {"check_schema": "raise"}}
```

```python
try:
    await Club.objects.count()        # the first operation of the pool waits for the check
except adorm.SchemaCheckError as exc:
    for problem in exc.problems:
        log.error("%s", problem)
```

The registered models are those of the generated modules the application has imported when the pool starts.

Types are compared as PostgreSQL spells them (`varchar(100)` and `character varying(100)` match,
[`normalize_type()`](/ref/drivers#adorm-drivers-postgresql-normalize-type-type-name)). Tables are found on the
connection's `search_path`, as the queries find them.

## From the command line

`adorm check-schema` runs the same check in a process of its own, with the profiles of the Django settings or of
modules that call `adorm.configure()`. It exits with status 1 when it finds problems, so a deployment or CI step can
stop on it:

```sh
adorm check-schema --settings myproject.settings -m myproject.adorm_models
adorm check-schema --settings myproject.settings -m myproject.adorm_models --using reports clubs billing.Invoice
adorm check-schema --configure myproject.adorm_config -m myproject.adorm_models   # without Django settings
```

[`adorm check-schema`](/ref/checks#adorm-check-schema) lists its options and exit statuses.

## Drivers

A driver takes part in the checks through two parts of the [driver contract](/topics/drivers):

- [`schema_hash(label)`](/ref/drivers#schema-hash-label) returns its target's hash of a model, or `None` to leave its
  models unchecked; [`check_schema_hash(label, expected)`](/ref/drivers#check-schema-hash-label-expected) compares it
  with the client's;
- the operation kind `check_table` in its [capabilities](/topics/drivers#capabilities), executed by reading the
  catalogue. The PostgreSQL helpers [`columns_sql()` and `table_problems()`](/ref/drivers#table-checks) do the
  comparison; a driver only lists what each model expects.

[Writing a driver](/howto/writing-a-driver#schema-checks) shows both.
