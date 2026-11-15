# The native PostgreSQL driver

::: info Status
The installed version of the native PostgreSQL driver executes reads: `select`, `get`, `first`, `last`, `count` and
`exists`, with the lookups, functions and clauses of the [Django driver](/topics/django-driver)'s reads
([`READ_CAPABILITIES`](/ref/drivers#adorm-drivers-django-read-capabilities)), arithmetic included. Writes, row
locks, savepoints, connection options and driver methods raise `NotSupportedError` before anything runs.
:::

[`AsyncpgDriver`](/ref/drivers#adorm-drivers-asyncpg-asyncpgdriver) executes adorm operations on
[asyncpg](https://magicstack.github.io/asyncpg/). It is an [async driver](/topics/drivers#execution-mode): a slot's
connection is an `asyncpg.Connection` and every call is a coroutine on the event loop. The driver starts no thread;
when `HOST` is a host name, asyncio resolves it while connecting in the loop's default executor. The SQL
comes from Django's query compiler ([SQL compilation for asyncpg](/topics/sql-compilation)), so lookups, functions,
custom fields and their converters behave as on the Django driver, and a read returns the same rows on both.

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
describe the models and compile SQL, never to connect: Django's own connections stay closed in a process that runs
only this driver.

## Connections

A connection belongs to one slot. It is opened with the `DATABASES` entry the profile's `alias` names, whose backend
must be Django's PostgreSQL backend on psycopg 3 (another backend raises `NotSupportedError` before anything
connects). The profile's `params` replace keys of that entry for the profile's connections; the shared `DATABASES`
entry stays as it is.

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

The connection lives as long as its slot; Django's `CONN_MAX_AGE` does not apply. The SQL is compiled for the
server's version, read when the connection opens.

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

### Transactions

`begin()` sends `BEGIN` (with the isolation level of `OPTIONS["isolation_level"]`), `commit()` and `rollback()` send
`COMMIT` and `ROLLBACK`. Reads inside `adorm.atomic()` run in one database transaction on the transaction's slot:

```python
async with adorm.atomic():
    member = await Member.objects.get(email="ann@example.com")
    count = await member.memberships.count()      # same transaction, same slot
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

A statement error leaves the connection in the pool; a connection error or a cancelled statement takes it out
([`is_connection_error()`](/ref/drivers#adorm-drivers-is-connection-error-exc)), and the next request gets a new
connection.

## Cancellation

Cancelling the task that awaits a read cancels the running statement: asyncpg sends PostgreSQL a cancel request
and the task gets `asyncio.CancelledError`. A read that completes just before the cancellation arrives is
discarded.

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

## Events

The driver emits no events: a read reports its outcome as the result of the call, which the slot delivers as a
`ResultEvent` or `ErrorEvent` (`CancelledEvent` for a cancelled read). It consumes none. The pool delivers
`CallbackEvent("on_commit")` and `CallbackEvent("on_rollback")` for its transactions, as for every driver. It
registers no asyncpg listeners, so `NOTIFY` messages and server log messages are not reported as `BackendEvent`.
