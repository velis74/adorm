# The Django driver

::: info Status
The installed version of the Django driver executes reads (`select`, `get`, `first`, `last`, `count`, `exists`),
writes (`create`, `save`, `update`, `delete`, `delete_instance`, `get_or_create`, `update_or_create`), row locks
(`select_for_update()`), transactions and savepoints. Connection options and driver methods raise
`NotSupportedError` before anything runs.
:::

[`DjangoDriver`](/ref/drivers#adorm-drivers-django-djangodriver) executes adorm operations with Django's ORM. It is
a [threaded driver](/topics/drivers#execution-mode): every slot has a thread of its own with that thread's Django
connection, and the driver calls only Django's synchronous ORM methods on it. The application never touches
Django's query sets; the driver builds them from adorm's [operation description](/ref/operations). The
[native PostgreSQL driver](/topics/postgresql-driver) builds the same query sets and runs their SQL on asyncpg.

```bash
pip install "adorm[django]"
```

```python
import django

import adorm

django.setup()      # the driver needs Django's app registry; the API process sets Django up once
adorm.configure(profiles={"default": {"driver": "adorm.drivers.django.DjangoDriver", "alias": "default"}})

from myapp.adorm_models import Activity, Member


async def coaches(club_slug: str) -> list[Member]:
    activities = Activity.objects.filter(club__slug=club_slug, coach__isnull=False)
    return [member async for member in Member.objects.filter(pk__in=activities.values("coach")).order_by("last_name")]
```

## Connections

The driver supports PostgreSQL databases: a profile whose alias uses another database backend raises
`NotSupportedError` when its first connection opens.

A connection is `django.db.connections[profile.alias]` on the slot's thread: Django's connection wrapper for that
`DATABASES` entry, opened when the slot opens and closed when the slot leaves the pool. Its lifetime is the slot's
([slot lifecycle](/topics/pool#slot-lifecycle)): the wrapper runs with `CONN_MAX_AGE = None` and
`CONN_HEALTH_CHECKS = False`, so neither Django's `close_old_connections()` (which its request signals and Celery's
Django integration call) nor a health check closes or pings it. With Django's own connection pool configured on the
alias (`OPTIONS["pool"]`), the slot's connection is still opened directly, not taken from that pool, and closing the
slot ends it on the server. The profile's `params` replace keys of the `DATABASES` entry for the connections of that
profile, and the shared `DATABASES` entry stays as it is. On a `direct` driver the event loop thread's wrapper gets
its own settings back when the slot closes. The `connect` hook, when set, returns the DB-API connection (a psycopg
connection) the wrapper uses instead of opening one; Django then configures it as its own
(`OPTIONS["isolation_level"]`, time zone, `assume_role`). A psycopg 3 connection needs the parameters Django opens its
own with (its cursor class and type adapters); the hook runs on the slot's thread, so it gets them from the thread's
wrapper.

```python
import psycopg
from django.db import connections


def connect(profile):
    params = connections[profile.alias].get_connection_params()    # the profile's params applied
    return psycopg.connect(**params, sslrootcert=CA_FILE)
```

```python
adorm.configure(
    profiles={
        "default": {"driver": "adorm.drivers.django.DjangoDriver", "alias": "default"},
        # the same database, other credentials
        "reports": {
            "driver": "adorm.drivers.django.DjangoDriver",
            "alias": "default",
            "params": {"USER": "reports", "PASSWORD": REPORTS_PASSWORD},
            "pool": {"max_connections": 2},
        },
    },
)
```

Profile hooks (`on_acquire`, `on_release`) receive the wrapper and run on the slot's thread; see
[Multiple database users](/howto/multiple-database-users).

Every ORM call of the driver runs on the profile's alias: query sets with `using(alias)`, `save(using=alias)`,
`transaction.atomic(using=alias)`.

## The caller's context

Every call runs in a copy of the context of the task that awaits it. Django's active language and time zone are
part of it: a manager whose `get_queryset()` reads `translation.get_language()` or
`timezone.get_current_timezone()` sees the caller's values, also when concurrent tasks with different languages
share the pool.

```python
from django.utils import translation

with translation.override("sl"):
    titles = [row async for row in Article.translated.values_list("title", flat=True)]
```

Django keeps these values in asgiref `Local` objects, whose values are tied to the thread that set them; the driver
re-binds them to the slot's thread before each call
([`adopt_context_locals()`](/ref/drivers#adorm-drivers-django-adopt-context-locals)). Changes a call makes to the
context stay in its copy.

## What a query becomes

The driver turns an adorm [`Query`](/ref/query) into a Django query set
([`translate()`](/ref/drivers#adorm-drivers-django-translation-translate-query)) and evaluates it with
`values_list()`:

| adorm | Django |
|---|---|
| the model label | `apps.get_model(label)`, auto-created many-to-many through models included (`"library.Book_authors"`) |
| no manager / `Query.manager` | `Model._base_manager` / the Django manager of that name, so its `get_queryset()` applies |
| each `filter()` / `exclude()` call | one Django `filter()` call with a Django `Q`; `exclude()` is a negated `Q`, with Django's NULL handling |
| `Q`, `&`, `|`, `~` on conditions | Django `Q`, `&`, `|`, `~` |
| `qs1 & qs2`, `qs1 \| qs2` | the filters of both / one filter with both queries' conditions joined with OR |
| lookups (`exact` … `iregex`) | the lookup of the same name on the field, so `contains` on an `ArrayField` or `JSONField` is Django's containment |
| a lookup on an expression (`Exact(Lower("name"), "ann")`) | the expression as an `alias()` and the lookup on it |
| `F`, `Value`, `Lower`, `Upper` | `F`, `Value`, `Lower`, `Upper`; a `Value` with `output_type` gets the Django field for it (`int` → `IntegerField` …) |
| a model instance as a value | its primary key, or the value of the field a foreign key's `to_field` names |
| a query in `in` (`pk__in=qs`, `pk__in=qs.values("x")`) | a Django query set in `__in` (one SQL statement) |
| `Subquery(query)` | `Subquery(queryset)` |
| `order_by()` with `asc(nulls_first=True)` … | `order_by()` with `asc()` / `desc()` and the same NULL placement |
| no ordering | the model's `Meta.ordering` |
| `distinct()`, `distinct("field")` | `distinct()`, `distinct("field")` (PostgreSQL's `DISTINCT ON`) |
| slicing | slicing (`LIMIT` / `OFFSET`) |
| `none()` | `none()`: no statement runs |
| `select_related()` | the related fields joined into the same `values_list()` |

Each adorm filter is one Django `filter()` call, so a condition on a multi-valued relation (a reverse foreign key, a
many-to-many relation) joins that relation once per call, as in Django:

```python
# a book with an author named Ann and an author named Cid
await Book.objects.filter(authors__name="Ann").filter(authors__name="Cid").count()
# a book with one author named Ann whose mentor is Cid
await Book.objects.filter(authors__name="Ann", authors__mentor__name="Cid").count()
```

`qs1 & qs2` keeps the filters of both queries, so it matches as the chained calls do. `qs1 | qs2` puts the
conditions of both queries into one filter, so the filters of each side share their joins.

Field paths use adorm names. Each segment is translated with the `orm_names` of the adorm model it reaches, and a
reverse relation's accessor name becomes its query name:

```python
notes = [note async for note in Note.objects.filter(copy___title="Dune")]   # Django: copy__title (Note.copy_)
await Book.objects.filter(tagging_set__weight=3).count()                     # Django: tagging__weight
await Book.objects.filter(schema_="v2").exists()                              # Django: schema
```

## Rows

Every read returns rows keyed by adorm names; the core builds the instances.

- A model row has the model's fields, minus those `only()` and `defer()` leave out (they are deferred on the
  instance), plus the annotations.
- `select_related("club__owner")` adds the related row under the relation's name, nested for each step; a missing
  related row (a NULL foreign key, no row on the other side of a one-to-one) is `None`. Awaiting the relation then
  reads the cache:

  ```python
  activity = await Activity.objects.select_related("club", "coach").get(pk=1)
  club = await activity.club       # no query
  coach = await activity.coach     # None when the activity has no coach, still no query
  ```

  `select_related()` without paths follows the non-null foreign keys, up to five relations deep, as Django does. A
  path that is not a foreign key or one-to-one relation raises `ProgrammingError`. `only("name", "club__name")`
  limits the related row's fields as well.
- `values()` and `values_list()` rows are keyed by the selected names, as written (`"club__name"`, `"pk"`). Without
  names, `values()` selects the adorm model's fields and the annotations.

`get()` runs Django's `get()`: no row raises the model's `DoesNotExist`, several rows its `MultipleObjectsReturned`,
with Django's messages. `first()` and `last()` order by the primary key when neither the query nor the model has an
ordering; `first()` on a sliced query needs `order_by()` (the core raises `TypeError` without it). `count()` and `exists()` run Django's `count()` and `exists()`.

Relations work as on any driver: `await member.club` runs a `get`, `club.memberships` and many-to-many relations
are queries.

```python
club = await Club.objects.get(slug="koper-sailing")
active = await club.memberships.filter(active=True).count()
tags = [tag.name async for tag in (await Activity.objects.get(pk=1)).tags.order_by("name")]
content_type_id = await ContentType.objects.values_list("id", flat=True).get(app_label="billing", model="invoice")
```

## Writes

Writes run through Django's model and query set methods, so a model's `save()` and `delete()` overrides, Django's
signals and the overrides of the query's manager run as in Django code, on the slot's thread:

| adorm | Django |
|---|---|
| `Model.objects.create(**values)` | `create(**values)` of `_base_manager`, or of the manager the query names (`Query.manager`), so its `create()` override receives arguments that are not fields |
| `obj.save()` of a new instance (no primary key, or `force_insert`) | `Model(**values).save()` |
| `obj.save()` with a primary key | the instance with the adorm instance's fields loaded and the others deferred, then `save()`: Django updates only the loaded fields |
| `obj.save(update_fields=[...])`, `force_update=True` | `save(update_fields=[...])`, `save(force_update=True)`: a missing row raises `DatabaseError` |
| `qs.update(**values)` | `update(**values)` on the query's query set |
| `qs.delete()` | `delete()` on the query's query set (its manager's overrides apply): Django's collector cascades, sets NULL and fires `pre_delete` / `post_delete`; no model's `delete()` runs |
| `obj.delete()` | the instance read with `_base_manager`, then its `delete()`: the model's `delete()` override runs, and Django's own deletes it with the collector; a missing row deletes nothing |
| `qs.get_or_create(...)`, `qs.update_or_create(...)` | `get_or_create(...)`, `update_or_create(...)` on the query's query set |

A save of an instance with a primary key that the database has no row for inserts the row when the instance was not
loaded with every field, as Django does for a complete instance; the driver checks the row with one `exists()` query
first, and the target's defaults fill the fields the instance never set. The values the target set (the primary key,
`default`, `auto_now_add`, a `save()` override's changes) come back to the instance:

```python
book = await Book.objects.create(isbn="9780000000099", title="New", pages=10)
book.added, book.published       # set by Django: auto_now_add and default=timezone.now

ann = await Author.objects.only("rating").get(slug="ann")
ann.rating = 4
await ann.save()                 # UPDATE library_author SET rating = 4 WHERE id = ...
```

Field names and lookup keys are adorm names: the driver renames them with `orm_names` and passes a foreign key named
by its relation as its column, so `get_or_create()` also creates the row with it:

```python
note = await Note.objects.create(content_type_id=book_type, object_id=isbn, json_="[]")   # Django: json="[]"
tagging, created = await Tagging.objects.get_or_create(book=dune, tag=rare, defaults={"weight": 2})
# Django: get_or_create(book_id=dune.pk, tag_id=rare.pk, defaults={"weight": 2})
```

Arithmetic (`F("rating") + 1`) becomes Django's `CombinedExpression` in updates, saves and annotations:

```python
await Activity.objects.filter(name="Crag day").update(capacity=F("capacity") + 5)
crag.capacity = F("capacity") + 1
await crag.save(update_fields=["capacity"])      # crag.capacity stays the expression, as in Django
```

A `delete()` override that deletes nothing (a soft delete) returns `(0, {})` to adorm unless it returns Django's
`(total, {label: count})` itself, and the adorm instance keeps its primary key:

```python
class Fine(models.Model):                       # Django
    def delete(self, using=None, keep_parents=False):
        self.waived = True
        self.save(using=using, update_fields=["waived"])

await fine.delete()                             # (0, {}): the fine is waived, fine.pk is unchanged
await Fine.objects.filter(pk=fine.pk).delete()  # (1, {"library.Fine": 1}): no model's delete() runs
```

A delete that an `on_delete=PROTECT` or `RESTRICT` reference forbids raises `ProtectedError` or `RestrictedError`
before anything is deleted. The driver reads the referencing rows again with every field, so the exception's objects
are complete instances of the registered models. The refusal leaves the transaction usable, as in Django:

```python
async with adorm.atomic():
    try:
        await member.delete()
    except adorm.ProtectedError as exc:
        numbers = [invoice.number for invoice in exc.protected_objects]     # Invoice instances
    await member.save(update_fields=["level"])                             # the transaction goes on
```

## Transactions and savepoints

`begin()` enters an outermost Django `transaction.atomic()` block and `commit()` / `rollback()` leave it, so the
operations inside `adorm.atomic()` run in one database transaction on the transaction's slot, and Django code that
runs on the driver side (a `save()` override, `get_or_create()`, a signal receiver) sees an atomic block as it would
in a Django view. A nested `adorm.atomic()` is a Django savepoint in that block:

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

A database error inside a savepoint (an `IntegrityError`, a cancelled statement) is rolled back to the savepoint, which
clears Django's mark for rollback, so the enclosing block goes on and commits:

```python
async with adorm.atomic():
    await Tag.objects.create(name="outer")
    try:
        async with adorm.atomic():
            await Tag.objects.create(name="rare")        # a duplicate: IntegrityError
    except adorm.IntegrityError:
        pass
    await Tag.objects.create(name="after")               # stored with "outer"
```

`begin()` on a [direct](/topics/drivers#execution-mode) connection whose thread is already inside a Django atomic
block raises `TransactionManagementError`.

A commit that the database refuses (a deferred foreign key, which is how Django creates foreign keys on PostgreSQL,
checked at `COMMIT`) raises its error, here `IntegrityError` with SQLSTATE `23503`, and the transaction is rolled back.
A transaction that failed on the driver side without the core seeing the error (Django marked it for rollback, or
PostgreSQL aborted it) is rolled back by `commit()`, which raises `TransactionManagementError`, so the commit is never
reported for it.

### Row locks

`select_for_update()` becomes Django's `select_for_update()` with `nowait`, `skip_locked`, `of` (adorm paths, `"self"`)
and `no_key`. A read of rows with `select_for_update()` outside a transaction raises `TransactionManagementError`
in the core, as Django does:

```python
async with adorm.atomic():
    invoice = await Invoice.objects.select_for_update().get(pk=pk)      # other flows wait for the row
    invoice.status = "paid"
    await invoice.save(update_fields=["status"])

async with adorm.atomic():
    free = [job async for job in Job.objects.select_for_update(skip_locked=True).filter(state="new")[:10]]
```

`nowait` on a locked row raises `OperationalError` with SQLSTATE `55P03`. Django's `update_or_create()` locks the row
it updates the same way, and `get_or_create()` retries its read after a concurrent insert of the same unique key, so
two flows creating the same row create it once.

### Django's `on_commit()` callbacks

Code that runs on the driver side may register Django `on_commit()` callbacks, typically a signal receiver or a
`save()` override that queues a job. Inside an adorm transaction they run once the commit's outcome is delivered, on
the slot's thread, in registration order, and before the slot's next request; the callbacks registered with
[`tx.on_commit()`](/topics/transactions#running-code-after-the-commit) run on the client when the slot delivers
`CallbackEvent("on_commit")`, which does not wait for Django's. Cancelling the client task while Django's callbacks
run cancels neither them nor the commit:

```python
def queue_welcome(sender, instance, created, **kwargs):           # a Django post_save receiver
    if created:
        transaction.on_commit(lambda: send_welcome.delay(instance.pk))

async with adorm.atomic() as tx:
    member = await Member.objects.create(...)
    tx.on_commit(lambda: cache.invalidate("members"))
# after COMMIT: send_welcome.delay() on the slot's thread, cache.invalidate() on the client
```

A callback registered in a savepoint that rolls back, or in a transaction that rolls back, does not run. A callback
that raises after the commit is logged on the `adorm.drivers.django` logger and does not undo the commit; one
registered without `robust=True` stops the callbacks after it, as in Django. Outside an adorm transaction Django runs
a callback at once, inside the write that registers it.

### Signals

Django's signals (`pre_save`, `post_save`, `pre_delete`, `post_delete`, `m2m_changed` …) fire on the driver side,
on the slot's thread, in a copy of the context of the task that awaits the write, so a receiver reads the caller's
context variables and Django's active language. adorm has no signals of its own and the client does not see Django's:
they produce no event. Reads build no Django model instances, so `pre_init` and `post_init` fire only for writes.

## Errors

The driver raises adorm's [exceptions](/ref/exceptions)
([`map_django_error()`](/ref/drivers#adorm-drivers-django-map-django-error-exc)):

| Django | adorm |
|---|---|
| `Model.DoesNotExist`, `Model.MultipleObjectsReturned` | `ObjectDoesNotExist`, `MultipleObjectsReturned`; the query set raises the adorm model's own |
| `FieldError` (an unknown field or lookup) | `ProgrammingError` |
| `ValidationError` (a value Django rejects, such as an invalid UUID) | `ValidationError` |
| `TransactionManagementError` | `TransactionManagementError` |
| `ProtectedError`, `RestrictedError` | `ProtectedError`, `RestrictedError` with the referencing objects as instances of the registered models |
| `django.db` errors | by the SQLSTATE of the psycopg error they carry: an invalid regular expression is `DataError` with SQLSTATE `2201B`, a cancelled statement `OperationalError` with `57014`, a terminated connection `OperationalError` with `57P01` |
| anything else (Django's `ValueError` for a value of the wrong type) | `DatabaseError` with the original exception in `original` |

An unknown model label or manager raises `ProgrammingError`. A connection that reports a lost connection is
dropped by its slot; the next request gets a new one. Other errors leave the connection in the pool.

## Custom SQL

Code that Django runs inside an operation can execute SQL of its own: a `post_save` receiver, a manager's `create()`
or `delete()` override, a model's `save()`, or an `on_commit()` callback. When such code runs a statement through
the connection's cursor that does not come from Django's query compiler or database backend, the driver marks the
connection ([`mark_custom_sql()`](/ref/drivers#mark-custom-sql)), and its slot closes the connection when the lease
ends, also after a commit: the statement may have changed the connection's state.

The driver looks at the callers of Django's cursor wrapper. The nearest one in Django's query compiler or in a
database backend (Django's, or the package of the alias's own backend, such as PostGIS) makes the statement ORM SQL;
code between them, such as instrumentation that wraps the cursor (Sentry, tracers, django-debug-toolbar), does not
change that. A signal receiver or an `on_commit()` callback reached first makes it custom SQL.

| On the slot's thread, inside an operation | Custom SQL |
|---|---|
| query sets, `save()`, `delete()`, `bulk_create()`, `update()`, `select_for_update()` | no |
| Django's `transaction.atomic()` and savepoints | no |
| `connection.cursor().execute(...)`, `Manager.raw()` | yes |
| the same in an `on_commit()` callback, of the transaction or run at once outside one | yes; after a commit the slot decides once the callbacks have run |
| the profile's `on_acquire` and `on_release` hooks, connection options | no |

```python
from django.db import connection
from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender=Member)
def refresh_stats(instance, **kwargs):
    with connection.cursor() as cursor:                     # custom SQL: the connection is closed after the lease
        cursor.execute("REFRESH MATERIALIZED VIEW member_stats")


@receiver(post_save, sender=Member)
def audit(instance, **kwargs):
    AuditEntry.objects.create(member_id=instance.pk)       # the ORM: the connection stays in the pool
```

Not detected:

- SQL sent past Django's execute wrappers: through the DB-API connection itself (`connection.connection.execute()`),
  the cursor wrapper's `callproc()`, and psycopg 3 cursor methods the wrapper passes through (`copy()`, `stream()`);
- SQL embedded in ORM expressions (`RawSQL`, `extra()`, a `Func` with its own template), and SQL that a custom field
  or expression runs while Django compiles or executes a query: it counts as the compiler's.

## Connection options

The driver declares the [connection options](/topics/options) `statement_timeout`, `lock_timeout` and
`idle_in_transaction_session_timeout`, in seconds. It sets them with
[`set_options_sql()`](/ref/drivers#adorm-drivers-sql-set-options-sql-options-local) on a cursor of the thread's
connection: `SET` / `RESET` outside a transaction, `SET LOCAL` inside one.

```python
with adorm.connection_options(lock_timeout=2):
    await Member.objects.filter(pk=1).update(active=False)   # SET lock_timeout = 2000, then the UPDATE
```

## Cancellation

Cancelling the task that awaits an operation cancels the running statement: the driver calls psycopg's `cancel_safe()`
(`cancel()` on psycopg 2) from another thread, the statement ends with `57014`, the task gets
`asyncio.CancelledError` and the connection stays in the pool. The `CancelledError` carries
`CancellationOutcome.UNKNOWN` for a statement the database cancelled. A statement that completed on the connection's
thread before the cancellation reached the database delivers its outcome to the slot: the task still gets
`CancelledError`, with `CancellationOutcome.SUCCEEDED`, or `FAILED` for an error, and a commit is
`TransactionOutcome.COMMITTED`; a read's rows are discarded. See [cancellation](/topics/cancellation).

```python
task = asyncio.create_task(Report.objects.filter(year=2026).count())
task.cancel()          # the database stops the statement
```

A cancelled write inside a transaction, such as one waiting for a row lock, rolls the transaction back: the writes
before it are undone with it, and the slot returns to the pool. Inside a nested `adorm.atomic()` only that savepoint is
rolled back and the enclosing block goes on. A write outside a transaction that completes just before the
cancellation arrives stays written.

## Events

The driver emits no events and consumes none. Each operation reports its outcome as the result of the call, which
the slot delivers as a `ResultEvent` or `ErrorEvent` (`CancelledEvent` for a cancelled operation). The slot delivers
`CallbackEvent("on_commit")` after a successful `commit()`, and `CallbackEvent("on_rollback")` after a rollback; the
transaction's own `on_commit()` callbacks run on that event. Django's `on_commit()` callbacks run on the slot's thread
after the commit's outcome, before the slot's next request. Django's signals and `on_commit()` callbacks stay on the driver side and produce no event.
