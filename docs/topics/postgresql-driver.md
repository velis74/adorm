# The native PostgreSQL driver

::: info Status
The native PostgreSQL driver executes what the [Django driver](/topics/django-driver) executes
([`CAPABILITIES`](/ref/drivers#adorm-drivers-django-capabilities)):

- reads (`select`, `get`, `first`, `last`, `count`, `exists`, `aggregate`) with their lookups, functions,
  aggregates, subqueries and clauses;
- writes (`create`, `bulk_create`, `save`, `update`, `delete`, `delete_instance`, `get_or_create`,
  `update_or_create`);
- row locks (`select_for_update()`), transactions, savepoints and the connection options.

Driver methods raise `NotSupportedError` before anything runs.
:::

[`AsyncpgDriver`](/ref/drivers#adorm-drivers-asyncpg-asyncpgdriver) executes adorm operations on
[asyncpg](https://magicstack.github.io/asyncpg/). It is an [async driver](/topics/drivers#execution-mode): a slot's
connection is an `asyncpg.Connection` and every call is a coroutine on the event loop. The driver starts no thread.
It uses Django only for work without I/O: compiling SQL, model metadata (fields, relations, deletion rules) and the
schema hash; a Django call that would query the database raises instead of blocking the loop.
When `HOST` is a host name, asyncio resolves it in the loop's default executor while connecting.

- The SQL comes from Django's query compiler ([SQL compilation for asyncpg](/topics/sql-compilation)). Lookups,
  functions, custom fields and their converters behave as on the Django driver, and a read returns the same rows on
  both.
- Writes follow Django's write logic with Django's queries and model instances, without Django's model methods and
  signals ([what runs and what does not](#what-runs-and-what-does-not)); a write whose signals have receivers is
  refused unless the profile lists them as [covered](#signal-receivers).

```bash
pip install "adorm[asyncpg]"     # asyncpg, Django and psycopg 3
```

```python
import django

import adorm

django.setup()      # the driver compiles with Django's models and DATABASES; set Django up once per process
adorm.configure(profiles={"default": {"driver": "adorm.drivers.asyncpg.AsyncpgDriver", "alias": "default"}})

from myapp.adorm_models import Activity, Member

async def coaches(club_slug: str) -> list[Member]:
    activities = Activity.objects.filter(club__slug=club_slug, coach__isnull=False)
    return [member async for member in Member.objects.filter(pk__in=activities.values("coach")).order_by("last_name")]
```

Application code is the same as on the Django driver; only the profile's driver differs. Django describes the
models, builds model instances and compiles SQL; it never connects. Django's own connections stay closed in a process
that runs only this driver.

## Connections

A connection belongs to one slot. It opens with the `DATABASES` entry the profile's `alias` names.

- The entry's backend must be Django's PostgreSQL backend on psycopg 3; another backend raises `NotSupportedError`
  before anything connects.
- The profile's `params` replace keys of that entry for the profile's connections. The shared `DATABASES` entry is
  unchanged.
- `CONN_MAX_AGE` and `CONN_HEALTH_CHECKS` have no effect; the slot decides how long a connection lives
  ([slot lifecycle](/topics/pool#slot-lifecycle)).
- The driver runs only the SQL it compiles and never marks a connection for custom SQL.

In Django's tests the driver runs inside `TransactionTestCase`, on the process's test database. Inside a `TestCase` a
connection raises `NotSupportedError` when it opens, as it can neither see nor roll back the test's transaction
([testing](/topics/testing)).

| `DATABASES` key | asyncpg |
|---|---|
| `NAME`, `USER`, `PASSWORD`, `HOST`, `PORT` | `database`, `user`, `password`, `host`, `port`; an empty value leaves asyncpg's default, `NAME` `None` is `postgres` |
| `TIME_ZONE`, Django's `USE_TZ` and `TIME_ZONE` | the session time zone (`TimeZone`), as Django sets it on its own connections |
| `OPTIONS["options"]` | its `-c name=value`, `-cname=value` and `--name=value` settings go to `server_settings`; any other token raises `NotSupportedError` |
| `OPTIONS["connect_timeout"]` | `timeout` of `asyncpg.connect()` |
| `OPTIONS["assume_role"]` | `SET ROLE` after the connection opens; the profile's `role` [connection option](/topics/options) starts with it |
| `OPTIONS["isolation_level"]` (a psycopg `IsolationLevel` value) | `BEGIN ISOLATION LEVEL …` for every transaction |
| `OPTIONS["server_side_binding"]` | how parameters are typed ([parameter types](/topics/sql-compilation#parameter-types-psycopg-and-asyncpg)) |
| `OPTIONS["pool"]`, `["prepare_threshold"]`, `["cursor_factory"]`, `["context"]` | no effect: they configure psycopg and Django's pool |
| `OPTIONS` keys in [`DSN_PARAMETERS`](/ref/drivers#adorm-drivers-asyncpg-dsn-parameters) (`sslmode`, `sslrootcert`, `application_name` …) | libpq parameters in asyncpg's `dsn` |
| other `OPTIONS` keys (`keepalives`, `tcp_user_timeout`, `gssencmode` …) | `NotSupportedError` before connecting: asyncpg does not read them and would send them to the server as settings |

[`connect_arguments()`](/ref/drivers#adorm-drivers-asyncpg-connect-arguments-settings-dict-time-zone-none) returns
the arguments for an entry:

```python
connect_arguments(
    {"NAME": "clubs", "USER": "app", "HOST": "db", "PORT": "5432",
     "OPTIONS": {"sslmode": "verify-full", "options": "-c statement_timeout=30s"}},
    time_zone="UTC",
)
# {'database': 'clubs', 'user': 'app', 'host': 'db', 'port': 5432,
#  'server_settings': {'statement_timeout': '30s', 'TimeZone': 'UTC'}, 'dsn': 'postgresql://?sslmode=verify-full'}
```

```python
adorm.configure(
    profiles={
        "default": {"driver": "adorm.drivers.asyncpg.AsyncpgDriver", "alias": "default"},
        # the same database, other credentials
        "reports": {
            "driver": "adorm.drivers.asyncpg.AsyncpgDriver",
            "alias": "default",
            "params": {"USER": "reports", "PASSWORD": REPORTS_PASSWORD},
            "pool": {"max_connections": 2},
        },
    },
)
```

SQL is compiled for the server version read when the connection opens.

### The `connect` hook

A profile's `connect` hook opens the connection in place of `asyncpg.connect()`. `connect(profile)` returns an
`asyncpg.Connection` or an awaitable of one; anything else raises `InterfaceError`. The driver then sets the session
time zone (when it differs) and `assume_role` on it, as on a connection it opens itself.

```python
import ssl

import asyncpg

async def connect(profile):
    context = ssl.create_default_context(cafile=CA_FILE)
    return await asyncpg.connect(DSN, ssl=context, statement_cache_size=0)     # behind a transaction-mode pooler

adorm.configure(profiles={"default": {"driver": "adorm.drivers.asyncpg.AsyncpgDriver", "connect": connect}})
```

### Hooks

`on_acquire` and `on_release` receive the `asyncpg.Connection`. They may be coroutine functions or plain functions;
the driver awaits an awaitable return value. `on_acquire` returns the lease's connection options. A statement a hook
runs on the connection is custom SQL: a query logger on the connection sees it, and the slot leaves the pool when
the lease ends. See [Multiple database users](/howto/multiple-database-users).

```python
async def on_acquire(connection, profile, session):
    return {"role": await role_of(current_user.get())}
```

## What a read runs

A read translates the operation's query into a Django query set as the Django driver does
([what a query becomes](/topics/django-driver#what-a-query-becomes)). It compiles the query set on the event loop
with the connection's
[`DjangoCompiler`](/ref/drivers#adorm-drivers-asyncpg-compiler-djangocompiler-alias-default-server-version-params-none),
which never connects, and runs one statement (`in_bulk` with many ids: one per batch):

| Operation | Statement | Result |
|---|---|---|
| `select` | the query's `SELECT` | the rows |
| `get` | the query without its ordering (unless it is sliced or `distinct()` on fields), `LIMIT 21` | the row; none raises `ObjectDoesNotExist`, more than one `MultipleObjectsReturned`, with Django's messages (`it returned more than 20!` for [`MAX_GET_RESULTS`](/ref/drivers#adorm-drivers-asyncpg-max-get-results) rows) |
| `first` / `last` | the query ordered as written (reversed for `last`), by the primary key without an ordering, `LIMIT 1` | the row or `None` |
| `count` | `SELECT COUNT(*) FROM (<query>) subquery` | `int` |
| `exists` | `SELECT 1 AS "a" … LIMIT 1`, Django's `exists()` query; for a set operation, the combined query with `LIMIT 1` | `bool` |
| `aggregate` | the statement of Django's `aggregate()`: `SELECT <aggregates> FROM …`, or `FROM (<query>) subquery` over a sliced, distinct or grouped query or one with aggregate annotations; none for a query that matches no rows | a dict by name, converted as Django converts it |
| `in_bulk` | the field checked as Django's `in_bulk()` checks it (`ProgrammingError` otherwise), then the query's `SELECT` filtered with `<field> IN (...)`, one statement per [`IN_BULK_BATCH_SIZE`](/ref/drivers#adorm-drivers-asyncpg-in-bulk-batch-size) ids | the rows |

`count` counts the rows the query returns; a slice, `distinct()` and `DISTINCT ON` apply before counting:

```python
await Member.objects.order_by("joined")[10:].count()           # rows after the first ten
await Activity.objects.filter(tags__isnull=False).distinct().count()
```

`aggregate` compiles the statement Django's `get_aggregation()` builds
([`compile_aggregate()`](/ref/drivers#compile-aggregate-query-aggregates)) and returns what Django's `aggregate()`
returns for its row:

```python
await Membership.objects.order_by("-fee")[:3].aggregate(Sum("fee"))     # {"fee__sum": Decimal("420.00")}
await Invoice.objects.filter(status="void").aggregate(n=Count("*"))      # {"n": 0}
```

Translation and compilation run on the event loop thread, so a Django manager's `get_queryset()` may only build a
query. One that accesses the database (a related row, `Site.objects.get_current()`, an uncached
`ContentType.objects.get_for_model()`) raises `NotSupportedError`; the connection stays in the pool. The Django
driver accepts such a manager, as it builds the query on the connection's thread. A `get_queryset()` that reads the
active language or time zone sees the values of the task that runs the read:

```python
from django.utils import translation

with translation.override("sl"):
    titles = [row async for row in Article.translated.values_list("title", flat=True)]
```

## Writes

A write builds Django's queries and model instances from the operation, as the Django driver does. It runs the
statements Django's own method would run, in sequence on the slot's connection:

| adorm | What runs |
|---|---|
| `Model.objects.create(**values)` | `Model(**values)`, then Django's insert of each table of the model (a multi-table inheritance child: the parents' rows first), with the fields' `pre_save()` (`auto_now_add`) and defaults; `RETURNING` gives the primary key and the values the database sets |
| `Model.objects.bulk_create(objs, ...)` | Django's `bulk_create()` steps for the installed Django version: `Model(**values)` per row, the instances with a primary key and those without inserted separately, one `INSERT ... RETURNING` per batch (`ON CONFLICT DO NOTHING` / `DO UPDATE` for the conflict options), more than one statement in a transaction; no `save()`, no signals |
| `obj.save()` of a new instance, `force_insert` | the same insert |
| `obj.save()` with a primary key | `Model.from_db()` with the adorm instance's fields loaded; `UPDATE` of the loaded fields per table, then the insert when no row was updated, as Django's `save()`; a partly loaded instance whose row is missing is inserted after one `exists()` query, as on the Django driver |
| `obj.save(update_fields=[...])`, `force_update=True` | `UPDATE` of those fields; no row updated raises `DatabaseError` |
| `qs.update(**values)` | Django's `UPDATE`; one that sets fields of a multi-table inheritance parent selects the keys first and updates each table, in a transaction |
| `qs.delete()`, `obj.delete()` | Django's deletion collector: the referencing rows by `on_delete` (`CASCADE`, `PROTECT`, `RESTRICT`, `SET_NULL`, `SET_DEFAULT`, `SET()`, `DO_NOTHING`, and the `DB_*` handlers of Django 6.1, which the database applies), generic relations, multi-table inheritance parents, then the `UPDATE`s and `DELETE`s, in a transaction |
| `qs.get_or_create(...)`, `qs.update_or_create(...)` | Django's steps: the `get()`, then the create in a savepoint (in a transaction outside one) and a second `get()` after an `IntegrityError`; `update_or_create()` reads the row with `select_for_update()` and saves it in one atomic block |
| `Child.objects.create_from_parent(parent, **values)` | the parent's row read as Django's `get()`, then Django's insert of the child's own table only, with the parent link and the child's fields |
| `obj.full_clean(exclude=[...])` | Django's `full_clean()` as Python on the event loop: each statement the validation runs (unique checks, constraints, queries of the model's `clean()`, routed to the profile's alias whatever the project's routers choose) is compiled and awaited, and the validation runs again with the rows until it needs no more statements |

```python
book = await Book.objects.create(isbn="9780000000099", title="New", pages=10)
book.added, book.published       # auto_now_add and default=timezone.now, set by Django's fields

ann = await Author.objects.only("rating").get(slug="ann")
ann.rating = 4
await ann.save()                 # UPDATE library_author SET rating = 4 WHERE id = ...

edition = await Edition.objects.create(isbn="9780000000077", title="Third", pages=3, released=today)
# INSERT INTO library_book ... ; INSERT INTO library_edition ... (in one transaction)

total, counts = await Author.objects.filter(slug="ann").delete()
# (3, {"library.Book_authors": 2, "library.Author": 1}): pupils' mentor set to NULL, many-to-many rows deleted
```

Results match the Django driver:

- `create`, `save`, `get_or_create`, `update_or_create`: the row as stored;
- `bulk_create`: a list of the rows as stored, in order;
- `update`: the number of rows;
- deletes: `(total, {label: count})`, as Django counts them.

A delete forbidden by a `PROTECT` or `RESTRICT` reference raises `ProtectedError` or `RestrictedError` before
anything is deleted, with the referencing rows read in full. The transaction stays usable:

```python
async with adorm.atomic():
    try:
        await member.delete()
    except adorm.ProtectedError as exc:
        numbers = [invoice.number for invoice in exc.protected_objects]     # Invoice instances
    await member.save(update_fields=["level"])                             # the transaction goes on
```

Writes that Django runs as several statements in one atomic block (a multi-table insert or update, a delete with
cascades, `update_or_create()`, a `bulk_create()` of several batches):

- outside `adorm.atomic()`, run in a transaction of their own;
- inside one, run in its transaction; a step Django runs in a savepoint (the insert of `get_or_create()`) runs in a
  savepoint.

A write of several statements that fails after its first statement, without a savepoint around it, leaves the
transaction rollback-only, as Django's `atomic(savepoint=False)` does. Example: a value of a multi-table child fails
to convert after the parent's row was inserted. Later operations of the transaction raise
`TransactionManagementError`, and its end rolls back, also when the error was caught. Rolling back to a savepoint
opened before the write clears the state.

```python
async with adorm.atomic():
    try:
        await Edition.objects.create(isbn=isbn, title="Third", pages=3, released=today)
    except adorm.NotSupportedError:         # a field's pre_save() of library_edition queried the database
        pass
# TransactionManagementError: the library_book row is rolled back with the transaction
```

Statement size limits:

- A delete names at most [`BATCH_SIZE`](/ref/drivers#adorm-drivers-asyncpg-writes-batch-size) keys per foreign key in
  one statement, and at most 32 767 parameters
  ([`MAX_QUERY_PARAMS`](/ref/drivers#adorm-drivers-asyncpg-compiler-max-query-params)) in all. A larger delete runs
  more statements in its transaction.
- A `bulk_create()` inserts at most `MAX_QUERY_PARAMS` divided by the number of columns rows per statement, fewer when
  `batch_size` is smaller.
- Django's PostgreSQL backend limits a statement only with psycopg's `server_side_binding` (65 535 parameters).
  Without it, the Django driver inserts a call without `batch_size` in one statement. The statements of one call run
  in one transaction on both drivers, so the stored rows are the same.

```python
await Member.objects.bulk_create(members)      # 10 000 members without primary keys, 7 columns: 3 INSERTs of 4 681 rows at most
```

### What runs and what does not

The driver runs no Python code of the target ORM beyond building queries and instances. Differences from the
[Django driver](/topics/django-driver#writes):

| | Django driver | native PostgreSQL driver |
|---|---|---|
| a model's `save()` (or `save_base()`) override | runs when the write saves | `create`, `save`, `get_or_create` and `update_or_create` raise `NotSupportedError` before anything runs; `get_or_create()` and `update_or_create()` are refused before the lookup, also when the row exists |
| a child model's `save_base()` override in `create_from_parent` | runs (`save()` does not run) | `create_from_parent` raises `NotSupportedError` before anything runs; a `save()` override is not called, as on the Django driver |
| a model's `delete()` override | runs for `obj.delete()` | `obj.delete()` raises `NotSupportedError` before anything runs |
| a manager's or query set's override of the method the write calls | runs | the write raises `NotSupportedError` before anything runs: `create()` for `create`; `bulk_create()` for `bulk_create`; `update()` for `update`; `delete()` for `qs.delete()`; `get_or_create()`, `get()`, `create()` for `get_or_create`; `update_or_create()`, `select_for_update()`, `get_or_create()`, `get()`, `create()` for `update_or_create` |
| a manager's `get_queryset()` | applies | applies (it must only build a query) |
| `pre_save`, `post_save`, `pre_delete`, `post_delete` | fire on the slot's thread | are not sent; a write they would be sent for raises `NotSupportedError` before anything is written when they have a receiver the profile's `covered_receivers` does not list ([signal receivers](#signal-receivers)) |
| `pre_init`, `post_init` | fire for every instance a write builds | fire for every instance a write builds, on the event loop |
| Django's `transaction.on_commit()` | runs after the commit, on the slot's thread | no Django code runs that could register one |
| an `on_delete` handler other than Django's own | runs | raises `NotSupportedError` before anything is deleted |
| fields' `pre_save()` (`auto_now`, `auto_now_add`), defaults, `get_db_prep_save()` | run | run |
| the model's `clean()` and validation overrides in `full_clean()` | run once | run once per statement the validation runs, plus once; validation that runs other statements when it runs again raises `NotSupportedError`, and so does one that runs more than [`MAX_VALIDATION_STATEMENTS`](/ref/drivers#adorm-drivers-asyncpg-writes-max-validation-statements) |

The error message names the override:

```python
await Reader.objects.create(name="Eve", username="eve")
# NotSupportedError: the query set of library.Reader overrides create(); the asyncpg driver does not run Python code
# of the target ORM, the Django driver does
await fine.delete()            # NotSupportedError: library.Fine overrides delete(); ...
await Fine.objects.filter(pk=fine.pk).delete()      # (1, {"library.Fine": 1}): no model's delete() is involved
```

Write a model that needs its overrides or signal receivers on a Django driver profile. Both drivers can serve the same
database in one process:

```python
adorm.configure(
    profiles={
        "default": {"driver": "adorm.drivers.asyncpg.AsyncpgDriver", "alias": "default"},
        "orm": {"driver": "adorm.drivers.django.DjangoDriver", "alias": "default"},
    },
)

with adorm.create_session("orm").activate():
    await Reader.objects.create(name="Eve", username="eve")     # ReaderQuerySet.create() runs
```

An instance saves and deletes on the session of the query set that loaded it ([saving](/topics/models#saving)). An
instance loaded on the native driver's profile is written there, also inside this block; to write it on the Django
profile, load it there.

The [conformance suite](/topics/conformance#python-code-of-the-target-orm) tests both drivers against this table.

### Signal receivers

Django sends `pre_save` and `post_save` for `create`, `save`, `create_from_parent` and the insert or update of
`get_or_create` and `update_or_create`, with the instance's model (a proxy model, not its concrete model; a
multi-table inheritance child, not its parents) as sender. It sends `pre_delete` and `post_delete` for every row its
collector deletes, by `obj.delete()` and `qs.delete()`: cascaded rows and the parent rows of a multi-table inheritance
child included. `bulk_create`, `update` and a `SET_NULL`, `SET_DEFAULT` or `SET()` update of a delete send no signal.
adorm has no many-to-many writes, so no operation is due an `m2m_changed`; the rows of an auto-created many-to-many
table a delete removes send no signal, as in Django.

The native driver sends none of these signals. A write Django would send one for raises `NotSupportedError` before it
writes anything, when the signal has a live receiver for the sender that the profile's
[`covered_receivers`](/ref/settings#adorm-connectionprofile) does not list:

- a save checks its model before its first statement; `get_or_create` and `update_or_create` check after the lookup,
  so a `get_or_create` that finds the row is not refused;
- a delete checks every model it collected rows of after the collection and before its first `DELETE` or `UPDATE`;
  a model with any `pre_delete` or `post_delete` receiver, covered or not, is not fast-deleted: its rows' primary
  keys are selected and the rows deleted by primary key, as Django's collector does;
- receivers connected for every sender (`sender=None`) count for every model; a weak receiver that has been garbage
  collected and a disconnected one do not count. Receivers are looked up with Django's `Signal._live_receivers()`.

The message names the model, each uncovered receiver by dotted path (`module.qualname`; a bound method by its class,
`module.Class.method`) and its signal. A refusal inside `adorm.atomic()` leaves the transaction usable.

```python
await Author.objects.create(name="Eve", slug="eve")
# NotSupportedError: Django signal receivers of library.Author (post_save: library.signals.index_author); the asyncpg
# driver does not send the target ORM's signals, the Django driver does. A receiver whose work the application does
# is listed in covered_receivers of profile 'default'
await shelf.delete()       # NotSupportedError: Django signal receivers of library.Placement (post_delete: ...) ...
```

`covered_receivers` lists receivers whose work the application does on the adorm side, e.g. after the adorm write;
the native driver writes despite them and still does not call them. The Django driver ignores the setting: there the
receivers run.

```python
adorm.configure(
    profiles={
        "default": {
            "driver": "adorm.drivers.asyncpg.AsyncpgDriver",
            "covered_receivers": ["library.signals.index_author"],
        },
    },
)
```

## Transactions and savepoints

`begin()` sends `BEGIN` (with the isolation level of `OPTIONS["isolation_level"]`); `commit()` and `rollback()` send
`COMMIT` and `ROLLBACK`. A nested `adorm.atomic()` is a `SAVEPOINT` with `RELEASE SAVEPOINT` or
`ROLLBACK TO SAVEPOINT`. The operations inside `adorm.atomic()` run in one database transaction on the transaction's
slot:

```python
async with adorm.atomic():
    member = await Member.objects.create(first_name="Ana", last_name="Novak", email="ana@example.org", joined=now)
    try:
        async with adorm.atomic():                       # SAVEPOINT
            await Membership.objects.create(club_id=club.pk, member_id=member.pk, since=today, fee=fee)
            raise Full
    except Full:
        pass                                             # ROLLBACK TO SAVEPOINT: only the membership is undone
# COMMIT: the member is stored
```

- A database error inside a savepoint rolls back to it; the enclosing block continues.
- A `COMMIT` that PostgreSQL turns into a rollback (the transaction failed earlier), or the commit of a transaction a
  write left rollback-only, raises `TransactionManagementError` and rolls back.
- A `COMMIT` the database refuses raises its error, e.g. `IntegrityError` with SQLSTATE `23503` for a deferred foreign
  key (Django creates foreign keys as deferred).

### Row locks

`select_for_update()` with `nowait`, `skip_locked`, `of` and `no_key` compiles to PostgreSQL's `FOR UPDATE` clause.
`nowait` on a locked row raises `OperationalError` with SQLSTATE `55P03`. `update_or_create()` locks the row it
updates the same way. `get_or_create()` reads the row again when its insert fails on a concurrent insert of the same
unique key, so two flows creating the same row create it once:

```python
async with adorm.atomic():
    invoice = await Invoice.objects.select_for_update().get(pk=pk)      # other flows wait for the row
    invoice.status = "paid"
    await invoice.save(update_fields=["status"])
```

## Rows

Rows are those the Django driver returns: keyed by adorm names, with the nested rows of `select_related()`, and
values as Django on psycopg returns them ([`DjangoStatement.rows()`](/ref/drivers#rows-records)):

- fields' converters applied (JSON decoded);
- `inet` / `cidr` as text, `uuid` as `uuid.UUID`;
- `timestamptz` in the connection's time zone (`UTC`, or the entry's `TIME_ZONE`); naive in Django's `TIME_ZONE`
  when `USE_TZ` is off.

```python
book = await Book.objects.get(isbn="9780000000001")
book.published         # datetime.datetime(2024, 3, 1, 12, 30, tzinfo=datetime.timezone.utc)
```

## Errors

Errors become adorm's [exceptions](/ref/exceptions). Translation errors (an unknown field, an invalid value) go
through [`map_django_error()`](/ref/drivers#adorm-drivers-django-map-django-error-exc-vendor-none) as on the Django driver;
asyncpg's errors are mapped by SQLSTATE
([`wrap_driver_error()`](/ref/exceptions#adorm-exceptions-wrap-driver-error-exc-details-none)):

| Error | adorm |
|---|---|
| an unknown field or lookup (Django's `FieldError`, `FieldDoesNotExist`), a segment after a scalar field | [`FieldError`](/ref/exceptions#adorm-fielderror-message-none-model-none-key-none-original-none-sqlstate-none), which leaves a transaction usable |
| a manager's `get_queryset()` that accesses the database | `NotSupportedError` |
| a value Django rejects (an invalid UUID), a failed `full_clean()` | `ValidationError` with each error's message, `code` and `params`, keyed by adorm field names |
| an invalid regular expression | `DataError`, SQLSTATE `2201B` |
| a cancelled statement | `OperationalError`, SQLSTATE `57014` |
| a terminated backend, a closed socket | `OperationalError` (`57P01`, `08003` …) or `InterfaceError` |
| an unknown database when connecting | `OperationalError`, SQLSTATE `3D000` |
| a duplicate key, a violated foreign key (also at `COMMIT`) | [`IntegrityError`](/ref/exceptions#adorm-integrityerror-message-none-constraint-name-none-table-name-none-schema-name-none-columns-values-detail-none-model-none-fields-kwargs), SQLSTATE `23505`, `23503`, with the constraint, table, columns and values from asyncpg's error, the model of the table and its adorm field names of the columns |
| a delete a `PROTECT` or `RESTRICT` reference forbids | `ProtectedError`, `RestrictedError` with the referencing rows as instances of the registered models |
| a save with `force_update` or `update_fields` that updated no row | `DatabaseError` |
| an override of a Django method the write would call, an `on_delete` handler other than Django's | `NotSupportedError` |

A statement error leaves the connection in the pool. A connection error or a cancelled statement takes it out
([`is_connection_error()`](/ref/drivers#adorm-drivers-is-connection-error-exc)); the next request gets a new
connection.

## Schema checks

The driver's [`schema_hash()`](/ref/drivers#schema-hash-label) is the hash of the installed Django model. An
operation on a generated model that is out of date with it raises `SchemaMismatchError` before it runs. The driver
executes `check_table` for [`adorm.check_schema()`](/topics/schema-checks) with one catalogue query per table, with
the columns' types from Django's `db_type()` for the alias.

```python
await adorm.check_schema([Club, Member])   # () while the tables match the models
```

## Connection options

The driver declares the [connection options](/topics/options) `statement_timeout`, `lock_timeout` and
`idle_in_transaction_session_timeout`, in seconds. It sets them with the statements of
[`set_options_sql()`](/ref/drivers#adorm-drivers-sql-set-options-sql-options-local) in one simple-protocol message.
Values set in `OPTIONS["options"]` (`-c statement_timeout=30s`) are the connection's defaults; `RESET` and
`SET LOCAL … TO DEFAULT` return to them.

```python
with adorm.connection_options(statement_timeout=0.5):
    await Member.objects.count()          # OperationalError, SQLSTATE 57014, after half a second
```

## Cancellation

Cancelling the task that awaits an operation cancels the running statement: asyncpg sends PostgreSQL a cancel request
and the task gets `asyncio.CancelledError`.

- A read that completes just before the cancellation arrives is discarded.
- The `CancelledError` of a statement or commit cancelled while it ran carries `CancellationOutcome.UNKNOWN`
  (`TransactionOutcome.UNKNOWN` for a commit), also when it completed just before the cancellation. See
  [cancellation](/topics/cancellation).

[`AsyncpgConnection.cancel()`](/ref/drivers#adorm-drivers-asyncpg-asyncpgconnection) may be called from any thread.
It cancels the statement running at that moment, which raises `OperationalError` with SQLSTATE `57014`; a statement
that starts later is not affected.

A statement cancelled either way before it finished takes its connection out of the pool: the server may have run
the statement without the protocol's closing Sync message, and keeps its transaction and locks open until the
connection's next message. The slot closes the connection; the next request gets a new one.

```python
task = asyncio.create_task(Report.objects.filter(year=2026).count())
task.cancel()          # the database stops the statement
```

A cancelled write inside `adorm.atomic()` rolls the transaction back, or only its savepoint inside a nested block. A
write of several statements outside a transaction runs in its own transaction. Cancelled before its `COMMIT`
completes, it takes its connection out of the pool; closing the connection rolls the transaction back, and none of
its statements is kept:

```python
task = asyncio.create_task(Edition.objects.create(isbn=isbn, title="Third", pages=3, released=today))
task.cancel()          # neither the library_book row nor the library_edition row is stored
```

## Events

- The slot delivers each operation's outcome as a `ResultEvent` or `ErrorEvent` (`CancelledEvent` for a cancelled
  operation).
- The pool delivers `CallbackEvent("on_commit")` and `CallbackEvent("on_rollback")` for its transactions, as for every
  driver; the transaction's own `on_commit()` callbacks run on that event.
- Django's signals are not sent and Django's `on_commit()` callbacks are not registered; neither produces an event.
- The driver consumes no events.

Each connection emits three [backend events](/topics/events#backend-events):

| Event | Source | When |
|---|---|---|
| `BackendEvent("notice", severity, {...})` | asyncpg's log listener | the server sends a notice or warning (`RAISE NOTICE` of a trigger, a `WARNING`); it arrives before the outcome of the request that caused it |
| `BackendEvent("notify", channel, payload)` | asyncpg's `add_listener()` | a notification on a channel an [`adorm.listen()`](/topics/events#listening-for-notifications) block listens on; asyncpg reads it on the event loop as soon as it arrives |
| `BackendEvent("connection_lost", None, reason)` | asyncpg's termination listener | the server ended the connection, whether a call runs, the connection listens or its slot is idle in the pool; the connection is unusable from then on |

```python
pool.events.subscribe(BackendEvent, lambda e: log.warning("%s", e.payload["message"]), name="notice", channel="WARNING")

async with adorm.listen("orders") as listener:      # LISTEN "orders" on a slot of its own
    event = await listener.get()                     # BackendEvent("notify", "orders", "42")
```
