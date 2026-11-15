# The native PostgreSQL driver

::: info Status
The installed version of the native PostgreSQL driver executes what the [Django driver](/topics/django-driver)
executes ([`CAPABILITIES`](/ref/drivers#adorm-drivers-django-capabilities)): the reads (`select`, `get`, `first`,
`last`, `count`, `exists`) with their lookups, functions and clauses, the writes (`create`, `save`, `update`,
`delete`, `delete_instance`, `get_or_create`, `update_or_create`), row locks (`select_for_update()`), transactions and
savepoints. Connection options and driver methods raise `NotSupportedError` before anything runs.
:::

[`AsyncpgDriver`](/ref/drivers#adorm-drivers-asyncpg-asyncpgdriver) executes adorm operations on
[asyncpg](https://magicstack.github.io/asyncpg/). It is an [async driver](/topics/drivers#execution-mode): a slot's
connection is an `asyncpg.Connection` and every call is a coroutine on the event loop. The driver starts no thread;
when `HOST` is a host name, asyncio resolves it while connecting in the loop's default executor. The SQL
comes from Django's query compiler ([SQL compilation for asyncpg](/topics/sql-compilation)), so lookups, functions,
custom fields and their converters behave as on the Django driver, and a read returns the same rows on both. Writes
follow Django's write logic with Django's queries and model instances, without Django's model methods and signals
([what runs and what does not](#what-runs-and-what-does-not)).

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

The application code is the same as on the Django driver; only the profile's driver differs. Django is used to
describe the models, build model instances and compile SQL, never to connect: Django's own connections stay closed in
a process that runs only this driver.

## Connections

A connection belongs to one slot. It is opened with the `DATABASES` entry the profile's `alias` names, whose backend
must be Django's PostgreSQL backend on psycopg 3 (another backend raises `NotSupportedError` before anything
connects). The profile's `params` replace keys of that entry for the profile's connections; the shared `DATABASES`
entry stays as it is. `CONN_MAX_AGE` and `CONN_HEALTH_CHECKS` have no effect: the slot decides how long a connection
lives ([slot lifecycle](/topics/pool#slot-lifecycle)). The driver runs only the SQL it compiles, so it never marks a
connection for custom SQL.

In Django's tests the driver runs inside `TransactionTestCase`, on the process's test database. Inside a `TestCase` a
connection raises `NotSupportedError` when it opens: it could neither see nor roll back the test's transaction
([testing](/topics/testing)).

| `DATABASES` key | asyncpg |
|---|---|
| `NAME`, `USER`, `PASSWORD`, `HOST`, `PORT` | `database`, `user`, `password`, `host`, `port`; an empty value leaves asyncpg's default, `NAME` `None` is `postgres` |
| `TIME_ZONE`, Django's `USE_TZ` and `TIME_ZONE` | the session time zone (`TimeZone`), as Django sets it on its own connections |
| `OPTIONS["options"]` | its `-c name=value`, `-cname=value` and `--name=value` settings go to `server_settings`; any other token raises `NotSupportedError` |
| `OPTIONS["connect_timeout"]` | `timeout` of `asyncpg.connect()` |
| `OPTIONS["assume_role"]` | `SET ROLE` after the connection opens |
| `OPTIONS["isolation_level"]` (a psycopg `IsolationLevel` value) | `BEGIN ISOLATION LEVEL …` for every transaction |
| `OPTIONS["server_side_binding"]` | how parameters are typed ([parameter types](/topics/sql-compilation#parameter-types-psycopg-and-asyncpg)) |
| `OPTIONS["pool"]`, `["prepare_threshold"]`, `["cursor_factory"]`, `["context"]` | no effect: they configure psycopg and Django's pool |
| `OPTIONS` keys in [`DSN_PARAMETERS`](/ref/drivers#adorm-drivers-asyncpg-dsn-parameters) (`sslmode`, `sslrootcert`, `application_name` …) | libpq parameters in asyncpg's `dsn` |
| other `OPTIONS` keys (`keepalives`, `tcp_user_timeout`, `gssencmode` …) | `NotSupportedError` before connecting: asyncpg does not read them and would send them to the server as settings |

[`connect_arguments()`](/ref/drivers#adorm-drivers-asyncpg-connect-arguments-settings-dict-time-zone-none) shows
the result for an entry:

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

The SQL is compiled for the server's version, read when the connection opens.

### The `connect` hook

A profile's `connect` hook opens the connection in place of `asyncpg.connect()`: `connect(profile)` returns an
`asyncpg.Connection` or an awaitable of one. The driver then sets the session time zone (when it differs) and
`assume_role` on it, as on a connection it opens itself; anything else returned raises `InterfaceError`.

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
the driver awaits what they return when it is awaitable. See [Multiple database users](/howto/multiple-database-users).

```python
async def on_acquire(connection, profile, session):
    await connection.execute("SELECT set_config('application_name', $1, false)", session.session_name)
```


## What a read runs

A read translates the operation's query into a Django query set exactly as the Django driver does
([what a query becomes](/topics/django-driver#what-a-query-becomes)), compiles it on the event loop with a
[`DjangoCompiler`](/ref/drivers#adorm-drivers-asyncpg-compiler-djangocompiler-alias-default-server-version-params-none)
of the connection, which never connects, and runs one statement:

| Operation | Statement | Result |
|---|---|---|
| `select` | the query's `SELECT` | the rows |
| `get` | the query without its ordering (unless it is sliced or `distinct()` on fields), `LIMIT 21` | the row; none raises `ObjectDoesNotExist`, more than one `MultipleObjectsReturned`, with Django's messages (`it returned more than 20!` for [`MAX_GET_RESULTS`](/ref/drivers#adorm-drivers-asyncpg-max-get-results) rows) |
| `first` / `last` | the query ordered as written (reversed for `last`), by the primary key without an ordering, `LIMIT 1` | the row or `None` |
| `count` | `SELECT COUNT(*) FROM (<query>) subquery` | `int` |
| `exists` | `SELECT 1 AS "a" … LIMIT 1`, Django's `exists()` query | `bool` |

`count` counts the rows the query returns, so a slice, `distinct()` and `DISTINCT ON` are applied before counting:

```python
await Member.objects.order_by("joined")[10:].count()           # rows after the first ten
await Activity.objects.filter(tags__isnull=False).distinct().count()
```

The translation and compilation run on the event loop thread, so a Django manager's `get_queryset()` must only
build a query: one that accesses the database (a related row, `Site.objects.get_current()`, an uncached
`ContentType.objects.get_for_model()`) raises `NotSupportedError`, and the connection stays in the pool. The same
manager works on the Django driver, which builds the query on the connection's thread. A manager whose
`get_queryset()` reads the active language or time zone sees the values of the task that runs the read, because
the read runs in that task's context:

```python
from django.utils import translation

with translation.override("sl"):
    titles = [row async for row in Article.translated.values_list("title", flat=True)]
```

## Writes

A write builds Django's queries and model instances from the operation, as the Django driver does, and runs the
statements Django's own method would run, one after the other on the slot's connection:

| adorm | What runs |
|---|---|
| `Model.objects.create(**values)` | `Model(**values)`, then Django's insert of each table of the model (a multi-table inheritance child: the parents' rows first), with the fields' `pre_save()` (`auto_now_add`) and defaults; `RETURNING` gives the primary key and the values the database sets |
| `obj.save()` of a new instance, `force_insert` | the same insert |
| `obj.save()` with a primary key | `Model.from_db()` with the adorm instance's fields loaded; `UPDATE` of the loaded fields per table, then the insert when no row was updated, as Django's `save()`; a partly loaded instance whose row is missing is inserted after one `exists()` query, as on the Django driver |
| `obj.save(update_fields=[...])`, `force_update=True` | `UPDATE` of those fields; no row updated raises `DatabaseError` |
| `qs.update(**values)` | Django's `UPDATE`; one that sets fields of a multi-table inheritance parent selects the keys first and updates each table, in a transaction |
| `qs.delete()`, `obj.delete()` | Django's deletion collector: the referencing rows by `on_delete` (`CASCADE`, `PROTECT`, `RESTRICT`, `SET_NULL`, `SET_DEFAULT`, `SET()`, `DO_NOTHING`, and the `DB_*` handlers of Django 6.1, which the database applies), generic relations, multi-table inheritance parents, then the `UPDATE`s and `DELETE`s, in a transaction |
| `qs.get_or_create(...)`, `qs.update_or_create(...)` | Django's steps: the `get()`, then the create in a savepoint (in a transaction outside one) and a second `get()` after an `IntegrityError`; `update_or_create()` reads the row with `select_for_update()` and saves it in one atomic block |

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

Results are those of the Django driver: the row as stored for `create`, `save`, `get_or_create` and
`update_or_create`, the number of rows for `update`, `(total, {label: count})` for deletes, as Django counts them.
A delete that a `PROTECT` or `RESTRICT` reference forbids raises `ProtectedError` or `RestrictedError` before
anything is deleted, with the referencing rows read in full, and leaves the transaction usable:

```python
async with adorm.atomic():
    try:
        await member.delete()
    except adorm.ProtectedError as exc:
        numbers = [invoice.number for invoice in exc.protected_objects]     # Invoice instances
    await member.save(update_fields=["level"])                             # the transaction goes on
```

Several statements that Django runs in one atomic block (a multi-table insert or update, a delete with cascades,
`update_or_create()`) run in a transaction of their own outside `adorm.atomic()`. Inside one they run in its
transaction, and a step that Django runs in a savepoint (the insert of `get_or_create()`) runs in a savepoint. A write
of several statements that fails after its first statement ran, without a savepoint around it (a value of a
multi-table child that fails to convert after the parent's row was inserted), leaves the transaction rollback-only,
as Django's `atomic(savepoint=False)` does: later operations of the transaction raise `TransactionManagementError`
and its end rolls back, also when the error was caught. Rolling back to a savepoint opened before the write clears it.

```python
async with adorm.atomic():
    try:
        await Edition.objects.create(isbn=isbn, title="Third", pages=3, released=today)
    except adorm.NotSupportedError:         # a field's pre_save() of library_edition queried the database
        pass
# TransactionManagementError: the library_book row is rolled back with the transaction
```

A delete names at most [`BATCH_SIZE`](/ref/drivers#adorm-drivers-asyncpg-writes-batch-size) keys per foreign key in
one statement, and at most 32 767 parameters
([`MAX_QUERY_PARAMS`](/ref/drivers#adorm-drivers-asyncpg-compiler-max-query-params)) in all: a delete of more rows
runs more statements, in its transaction.

### What runs and what does not

The driver runs no Python code of the target ORM beyond building queries and instances. Differences from the
[Django driver](/topics/django-driver#writes):

| | Django driver | native PostgreSQL driver |
|---|---|---|
| a model's `save()` (or `save_base()`) override | runs when the write saves | `create`, `save`, `get_or_create` and `update_or_create` raise `NotSupportedError` before anything runs; `get_or_create()` and `update_or_create()` are refused before the lookup, also when the row exists |
| a model's `delete()` override | runs for `obj.delete()` | `obj.delete()` raises `NotSupportedError` before anything runs |
| a manager's or query set's override of the method the write calls | runs | the write raises `NotSupportedError` before anything runs: `create()` for `create`; `update()` for `update`; `delete()` for `qs.delete()`; `get_or_create()`, `get()`, `create()` for `get_or_create`; `update_or_create()`, `select_for_update()`, `get_or_create()`, `get()`, `create()` for `update_or_create` |
| a manager's `get_queryset()` | applies | applies (it must only build a query) |
| `pre_save`, `post_save`, `pre_delete`, `post_delete`, `m2m_changed` | fire on the slot's thread | do not fire |
| `pre_init`, `post_init` | fire for every instance a write builds | fire for every instance a write builds, on the event loop |
| Django's `transaction.on_commit()` | runs after the commit, on the slot's thread | no Django code runs that could register one |
| an `on_delete` handler other than Django's own | runs | raises `NotSupportedError` before anything is deleted |
| fields' `pre_save()` (`auto_now`, `auto_now_add`), defaults, `get_db_prep_save()` | run | run |

The refusals name the override:

```python
await Reader.objects.create(name="Eve", username="eve")
# NotSupportedError: the query set of library.Reader overrides create(); the asyncpg driver does not run Python code
# of the target ORM, the Django driver does
await fine.delete()            # NotSupportedError: library.Fine overrides delete(); ...
await Fine.objects.filter(pk=fine.pk).delete()      # (1, {"library.Fine": 1}): no model's delete() is involved
```

A model that needs its overrides or signals is written on a profile of the Django driver; both drivers can serve the
same database in one process:

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

An instance saves and deletes on the session of the query set that loaded it ([saving](/topics/models#saving)), so
one loaded on the native driver's profile is written there also inside this block; load it on the Django profile
to write it there.

The [conformance suite](/topics/conformance#python-code-of-the-target-orm) holds both drivers to this table.

## Transactions and savepoints

`begin()` sends `BEGIN` (with the isolation level of `OPTIONS["isolation_level"]`), `commit()` and `rollback()` send
`COMMIT` and `ROLLBACK`; a nested `adorm.atomic()` is a `SAVEPOINT` with `RELEASE SAVEPOINT` or
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

A database error inside a savepoint is rolled back to it and the enclosing block goes on. A `COMMIT` that
PostgreSQL turns into a rollback because the transaction failed earlier, or of a transaction a write left
rollback-only, raises `TransactionManagementError` and rolls back, so the commit is never reported for it; one the database refuses (a deferred foreign key, as Django creates them) raises its
error, here `IntegrityError` with SQLSTATE `23503`.

### Row locks

`select_for_update()` with `nowait`, `skip_locked`, `of` and `no_key` compiles to PostgreSQL's `FOR UPDATE` clause.
`nowait` on a locked row raises `OperationalError` with SQLSTATE `55P03`. `update_or_create()` locks the row it
updates the same way, and `get_or_create()` reads the row again after its insert fails on a concurrent insert of the
same unique key, so two flows creating the same row create it once:

```python
async with adorm.atomic():
    invoice = await Invoice.objects.select_for_update().get(pk=pk)      # other flows wait for the row
    invoice.status = "paid"
    await invoice.save(update_fields=["status"])
```

## Rows

Rows are those the Django driver returns: keyed by adorm names, with the nested rows of `select_related()`, and
values as Django's own evaluation on psycopg returns them ([`DjangoStatement.rows()`](/ref/drivers#rows-records)):
fields' converters applied (JSON decoded), `inet` / `cidr` as text, `uuid` as `uuid.UUID`, and `timestamptz` values
in the connection's time zone (`UTC`, or the entry's `TIME_ZONE`), naive in Django's `TIME_ZONE` when `USE_TZ` is
off.

```python
book = await Book.objects.get(isbn="9780000000001")
book.published         # datetime.datetime(2024, 3, 1, 12, 30, tzinfo=datetime.timezone.utc)
```

## Errors

Errors become adorm's [exceptions](/ref/exceptions): those of the translation (an unknown field, an invalid value)
through [`map_django_error()`](/ref/drivers#adorm-drivers-django-map-django-error-exc) as on the Django driver,
asyncpg's by their SQLSTATE ([`wrap_driver_error()`](/ref/exceptions#adorm-exceptions-wrap-driver-error-exc)):

| Error | adorm |
|---|---|
| an unknown field or lookup (Django's `FieldError`) | `ProgrammingError` |
| a manager's `get_queryset()` that accesses the database | `NotSupportedError` |
| a value Django rejects (an invalid UUID) | `ValidationError` |
| an invalid regular expression | `DataError`, SQLSTATE `2201B` |
| a cancelled statement | `OperationalError`, SQLSTATE `57014` |
| a terminated backend, a closed socket | `OperationalError` (`57P01`, `08003` …) or `InterfaceError` |
| an unknown database when connecting | `OperationalError`, SQLSTATE `3D000` |
| a duplicate key, a violated foreign key (also at `COMMIT`) | `IntegrityError`, SQLSTATE `23505`, `23503` |
| a delete a `PROTECT` or `RESTRICT` reference forbids | `ProtectedError`, `RestrictedError` with the referencing rows as instances of the registered models |
| a save with `force_update` or `update_fields` that updated no row | `DatabaseError` |
| an override of a Django method the write would call, an `on_delete` handler other than Django's | `NotSupportedError` |

A statement error leaves the connection in the pool; a connection error or a cancelled statement takes it out
([`is_connection_error()`](/ref/drivers#adorm-drivers-is-connection-error-exc)), and the next request gets a new
connection.

## Connection options

The driver declares the [connection options](/topics/options) `statement_timeout`, `lock_timeout` and
`idle_in_transaction_session_timeout`, in seconds, and sets them with the statements of
[`set_options_sql()`](/ref/drivers#adorm-drivers-sql-set-options-sql-options-local) in one simple-protocol message.
Values set in `OPTIONS["options"]` (`-c statement_timeout=30s`) are the connection's defaults, to which `RESET`
and `SET LOCAL … TO DEFAULT` return.

```python
with adorm.connection_options(statement_timeout=0.5):
    await Member.objects.count()          # OperationalError, SQLSTATE 57014, after half a second
```

## Cancellation

Cancelling the task that awaits an operation cancels the running statement: asyncpg sends PostgreSQL a cancel request
and the task gets `asyncio.CancelledError`. A read that completes just before the cancellation arrives is
discarded. The `CancelledError` of a statement or commit cancelled while it ran carries
`CancellationOutcome.UNKNOWN` (`TransactionOutcome.UNKNOWN` for a commit), also when it completed just before the
cancellation; see [cancellation](/topics/cancellation).

[`AsyncpgConnection.cancel()`](/ref/drivers#adorm-drivers-asyncpg-asyncpgconnection) may be called from any
thread: it cancels the statement running at that moment, which then raises `OperationalError` with SQLSTATE
`57014`; a statement that starts later is not affected.

A statement cancelled either way before it finished takes its connection out of the pool: the server may have run
the statement without the protocol's closing Sync message, and then keeps its transaction and locks open until
the connection's next message. The slot closes the connection, and the next request gets a new one.

```python
task = asyncio.create_task(Report.objects.filter(year=2026).count())
task.cancel()          # the database stops the statement
```

A cancelled write inside `adorm.atomic()` rolls the transaction back, or only its savepoint inside a nested block. A
write of several statements outside a transaction runs in a transaction of its own; cancelled before its `COMMIT`
completed, it takes its connection out of the pool, and closing the connection rolls that transaction back, so none
of its statements stays:

```python
task = asyncio.create_task(Edition.objects.create(isbn=isbn, title="Third", pages=3, released=today))
task.cancel()          # neither the library_book row nor the library_edition row is stored
```

## Events

The driver emits no events: an operation reports its outcome as the result of the call, which the slot delivers as a
`ResultEvent` or `ErrorEvent` (`CancelledEvent` for a cancelled operation). It consumes none. The pool delivers
`CallbackEvent("on_commit")` and `CallbackEvent("on_rollback")` for its transactions, as for every driver; the
transaction's own `on_commit()` callbacks run on that event. Django's signals do not fire and Django's `on_commit()`
callbacks are not registered, so neither produces an event. It registers no asyncpg listeners, so `NOTIFY` messages
and server log messages are not reported as `BackendEvent`.
