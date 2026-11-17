# The Django driver

::: info Status
The Django driver executes:

- reads (`select`, `get`, `first`, `last`, `count`, `exists`, `aggregate`) with annotations, aliases, grouping,
  functions, aggregates and subqueries;
- writes (`create`, `bulk_create`, `save`, `update`, `delete`, `delete_instance`, `get_or_create`,
  `update_or_create`);
- row locks (`select_for_update()`), transactions and savepoints;
- the connection options of [`POSTGRESQL_OPTIONS`](/ref/drivers#adorm-drivers-sql-postgresql-options) on PostgreSQL.

It runs on PostgreSQL, SQLite, MySQL, MariaDB, Oracle and other Django backends; what a database cannot do is not
declared ([databases](#databases)). The test suite runs on PostgreSQL and SQLite.
:::

[`DjangoDriver`](/ref/drivers#adorm-drivers-django-djangodriver) executes adorm operations with Django's ORM. It is
a [threaded driver](/topics/drivers#execution-mode): every slot has its own thread with that thread's Django
connection, and the driver calls only Django's synchronous ORM methods on it. The driver builds Django query sets
from adorm's [operation description](/ref/operations); the application does not use them. The
[native PostgreSQL driver](/topics/postgresql-driver) builds the same query sets and runs their SQL on asyncpg.

```bash
pip install "adorm[django]"
```

```python
# settings.py
INSTALLED_APPS = [..., "adorm.contrib.django"]       # configures adorm on django.setup()
```

```python
import django

django.setup()      # the driver needs Django's app registry; the API process sets Django up once

from myapp.adorm_models import Activity, Member


async def coaches(club_slug: str) -> list[Member]:
    activities = Activity.objects.filter(club__slug=club_slug, coach__isnull=False)
    return [member async for member in Member.objects.filter(pk__in=activities.values("coach")).order_by("last_name")]
```

With the app [`adorm.contrib.django`](/topics/profiles#configuring-from-django-settings) and no `ADORM` setting, every
alias of `DATABASES` is a profile of the same name on this driver. Without the app, the process calls
`adorm.configure(profiles={"default": {"driver": "adorm.drivers.django.DjangoDriver", "alias": "default"}})` after
`django.setup()`.

## Connections

A connection is `django.db.connections[profile.alias]` on the slot's thread: Django's connection wrapper for that
`DATABASES` entry. It opens when the slot opens and closes when the slot leaves the pool
([slot lifecycle](/topics/pool#slot-lifecycle)).

- The wrapper runs with `CONN_MAX_AGE = None` and `CONN_HEALTH_CHECKS = False`. Neither `close_old_connections()`
  (called by Django's request signals and Celery's Django integration) nor a health check closes or pings it.
- With Django's connection pool on the alias (`OPTIONS["pool"]`), the slot opens its connection directly, not from
  that pool; closing the slot ends it on the server.
- The profile's `params` replace keys of the `DATABASES` entry for that profile's connections. The shared `DATABASES`
  entry is unchanged. On PostgreSQL, `OPTIONS["assume_role"]` starts the profile's `role`
  [connection option](/topics/options). On a `direct` driver the event loop thread's wrapper gets its own settings back when the slot
  closes.
- The `connect` hook, when set, returns the DB-API connection (on PostgreSQL a psycopg connection, on SQLite a
  `sqlite3` connection) the wrapper uses instead of
  opening one. Django configures it as its own (`OPTIONS["isolation_level"]`, time zone, `assume_role`). A psycopg 3
  connection needs Django's connection parameters (cursor class, type adapters); the hook runs on the slot's thread
  and reads them from the thread's wrapper.

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

Profile hooks (`on_acquire`, `on_release`) receive the wrapper and run on the slot's thread; a statement they run
through the wrapper is custom SQL. See [Multiple database users](/howto/multiple-database-users).

Every ORM call of the driver runs on the profile's alias: query sets with `using(alias)`, `save(using=alias)`,
`transaction.atomic(using=alias)`. Inside `full_clean()`, the queries Django routes itself (unique checks,
constraints, foreign key validation, queries of the model's `clean()`) also go to the profile's alias, ahead of the
project's `DATABASE_ROUTERS` ([`pinned_alias()`](/ref/drivers#adorm-drivers-django-translation-pinned-alias-alias)).
They run on the slot's connection and see its open transaction:

```python
adorm.configure(profiles={"tenant": {"driver": "adorm.drivers.django.DjangoDriver", "alias": "tenant"}})

tenant = adorm.create_session("tenant")
async with tenant.atomic():
    await tenant.Member.create(name="Ann", email="ann@example.com")
    with tenant.activate():
        await Member(name="Ann 2", email="ann@example.com").full_clean()
        # ValidationError({'email': ['Member with this Email already exists.']})
```

A `full_clean()` that a model's `save()` calls itself is not pinned; its queries go to the alias the project's
routers choose.

## Databases

The driver runs on the database of any Django backend. What it supports on a profile's database is
[`DjangoDriver.capabilities_for(profile)`](/ref/drivers#adorm-drivers-django-djangodriver), read from the backend of
the alias's `ENGINE` without connecting; the pool checks every operation and connection option against it before it
takes a slot, so what the database cannot do raises `NotSupportedError` before anything runs.
`DjangoDriver.capabilities` are those on PostgreSQL.

| | PostgreSQL | SQLite | MySQL, MariaDB | Oracle |
|---|---|---|---|---|
| capabilities | [`CAPABILITIES`](/ref/drivers#adorm-drivers-django-capabilities) | [`SQLITE_CAPABILITIES`](/ref/drivers#adorm-drivers-django-vendors-sqlite-capabilities) | [`MYSQL_CAPABILITIES`](/ref/drivers#adorm-drivers-django-vendors-mysql-capabilities) | [`ORACLE_CAPABILITIES`](/ref/drivers#adorm-drivers-django-vendors-oracle-capabilities) |
| `select_for_update()` | with `nowait`, `skip_locked`, `of`, `no_key` | no | with `nowait`, `skip_locked` | with `nowait`, `skip_locked`, `of` |
| `distinct(*fields)`, `overlap` | yes | no | no | no |
| `contains`, `startswith`, `endswith` | yes | no: `LIKE` ignores the case of ASCII letters (the `i...` lookups remain) | yes | yes |
| `intersection()`, `difference()` | yes | yes | no | yes |
| `bulk_create(ignore_conflicts=...)` | yes | yes | yes | no |
| `bulk_create(update_conflicts=...)` | yes | yes | no: `ON DUPLICATE KEY UPDATE` takes no `unique_fields` | no |
| connection options (`statement_timeout` …) | yes | no | no | no |
| `adorm.listen()`, notices | yes (psycopg 3) | no | no | no |
| cancellation | psycopg's cancel request | `interrupt()` | `KILL QUERY` on one more connection | `cancel()` |
| errors | SQLSTATE, constraint, columns, values | no SQLSTATE; table and columns | no SQLSTATE; MySQL's error number, key | no SQLSTATE; `ORA-` number, constraint |
| [`wait_for_lock()`](/ref/testing#adorm-testing-wait-for-lock-task-none-session-none-timeout-10-0-interval-0-01), monitor | yes | no | no | no |
| tested | yes | yes | no | no |

A backend without a vendor of its own declares what its features class allows
([`features_capabilities()`](/ref/drivers#adorm-drivers-django-vendors-features-capabilities-features)) and has no
cancellation. MySQL and Oracle are not tested; capabilities that depend on the server version (`SKIP LOCKED` on
MariaDB before 10.6, `INTERSECT` on MySQL before 8.0.31) are refused by Django when the query is compiled, before any
SQL runs: adorm raises `NotSupportedError`, and an enclosing transaction goes on.

```python
pool = Pool(driver, ConnectionProfile("local", driver, alias="local"))   # DATABASES["local"] is SQLite
pool.capabilities is SQLITE_CAPABILITIES                                  # True
await Member.objects.select_for_update().get(pk=1)
# NotSupportedError: 'get' operation needs unsupported clause.select_for_update (driver 'django')
```

Results follow the database: SQLite and MySQL sort `NULL` first in ascending order and last in descending order,
PostgreSQL and Oracle the other way round; on SQLite `Lower()`, `Upper()` and the case-insensitive lookups fold ASCII
letters only; on MySQL a case-insensitive collation makes `exact` case-insensitive; Oracle stores an empty string as
`NULL`. Django refuses a sliced or ordered query inside a set operation on SQLite: adorm raises `NotSupportedError`
when the query runs.

### SQLite

SQLite has one writer at a time beside any number of readers.

- A slot's connection switches the database to WAL (`PRAGMA journal_mode=WAL`, kept in the database file): readers
  run while a transaction writes and see the last committed state.
- Every transaction begins with `BEGIN IMMEDIATE` (`OPTIONS["transaction_mode"]`): it holds the write lock from its
  start to its end, whether it writes or not. A write or a [driver method](/topics/managers#driver-methods) outside a
  transaction runs in a transaction of its own that begins the same way.
- A transaction or write that finds the lock taken waits for it in the slot, for as long as the busy timeout
  (`OPTIONS["timeout"]`, 5 seconds by default), then raises `OperationalError` `database is locked`. The slot stays in
  the pool. Cancelling the task ends the wait at once.
- A cancelled `INSERT`, `UPDATE` or `DELETE` inside a transaction rolls back the whole transaction, savepoints
  included: every further operation of the transaction and of its enclosing blocks raises
  `TransactionManagementError` (`OperationalError` `TRANSACTION_ENDED` as the cause), and the outermost block rolls
  back. A cancelled read rolls back nothing.
- Before Django 5.1, which has no `transaction_mode`, and for an in-memory database (`":memory:"`, `file::memory:`,
  `mode=memory`) a slot's connection raises `NotSupportedError`: a private in-memory database is visible to its own
  connection only, and none has WAL.

```python
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3",
                         "OPTIONS": {"timeout": 20}}}   # a write waits up to 20 s for the write lock
```

What this means for the pool:

- Transactions on several slots run one after another: a slot inside a transaction holds the write lock, and the
  slots whose transactions or writes wait for it stay acquired meanwhile. Keep transactions short; a transaction that
  awaits other work holds the lock for that long.
- A write of another session that a task runs while its own transaction holds the lock (two sessions of one task)
  waits for the lock the same task holds; adorm's [deadlock detection](/topics/pool#deadlocks) does not see it, and the
  write raises `OperationalError` `database is locked` after the busy timeout.
- Reads outside transactions never wait for the lock, so `max_connections` above the number of concurrent writers
  serves readers; more slots do not add write throughput.
- With `max_connections=0`, the default, the pool has no limit on SQLite: SQLite refuses no connection, and every
  write or transaction that waits for the lock holds a slot, its thread and its connection. A burst of N writers opens
  N slots. Give a SQLite profile's pool an explicit `max_connections`.

## The caller's context

Every call runs in a copy of the context of the awaiting task. This includes Django's active language and time
zone: a manager whose `get_queryset()` reads `translation.get_language()` or `timezone.get_current_timezone()` sees
the caller's values, also when concurrent tasks with different languages share the pool.

```python
from django.utils import translation

with translation.override("sl"):
    titles = [row async for row in Article.translated.values_list("title", flat=True)]
```

Django keeps these values in asgiref `Local` objects, which are bound to a thread. The driver re-binds them to the
slot's thread before each call ([`adopt_context_locals()`](/ref/drivers#adorm-drivers-django-adopt-context-locals)).
Context changes a call makes stay in its copy.

## What a query becomes

The driver translates an adorm [`Query`](/ref/query) into a Django query set
([`translate()`](/ref/drivers#adorm-drivers-django-translation-translate-query)) and evaluates it with
`values_list()`:

| adorm | Django |
|---|---|
| the model label | `apps.get_model(label)`, auto-created many-to-many through models included (`"library.Book_authors"`) |
| no manager / `Query.manager` | `Model._base_manager` / the Django manager of that name, so its `get_queryset()` applies |
| each `filter()` / `exclude()` call | one Django `filter()` call with a Django `Q`; `exclude()` is a negated `Q`, with Django's NULL handling |
| `Q`, `&`, `|`, `~` on conditions | Django `Q`, `&`, `|`, `~` |
| `qs1 & qs2`, `qs1 \| qs2` | the filters of both / one filter with both queries' conditions joined with OR |
| lookups (`exact` … `iregex`, `overlap`) | the lookup of the same name on the field, so `contains` on an `ArrayField` or `JSONField` is Django's containment and `overlap` is the `ArrayField` lookup; a lookup that a `JSONField` or `HStoreField` does not have (`overlap`) raises `FieldError` on the field, on a key inside it and on an annotation that refers to the field (`annotate(e=F("extra"))`), where Django would read the name as a key |
| the values of `overlap` (`disciplines__overlap=["Lead", "Dinghy"]`) | a list, also when empty |
| a lookup on an expression (`Exact(Lower("name"), "ann")`) | the expression as an `alias()` and the lookup on it |
| `F`, `Value`, `Lower`, `Upper`, `Length`, `Coalesce`, `Concat`, `Now`, `Trunc…` | the Django class of the same name; a `Value` with `output_type` gets the Django field for it (`int` → `IntegerField` …); `Trunc(x, "date")` / `"time"` is `TruncDate` / `TruncTime` |
| `Count`, `Sum`, `Avg`, `Min`, `Max` with `distinct`, `filter`, `default` | the Django aggregate with the same options; `Count("*")` counts rows |
| `Case`, `When` | `Case`, `When` |
| `annotate()` / `alias()` | `annotate()` / `alias()`, in the order added |
| `values("x").annotate(n=Count(...))` (`Query.group_by`) | `annotate()` of the annotations without an aggregate, `values("x")`, `annotate()` of the aggregates: Django's `GROUP BY` |
| a model instance as a value | its primary key, or the value of the field a foreign key's `to_field` names |
| a query in `in` (`pk__in=qs`, `pk__in=qs.values("x")`) | a Django query set in `__in` (one SQL statement) |
| a query in `overlap` (`disciplines__overlap=qs.values("x")`) | a Django query set of that one column in `__overlap` (`ARRAY(subquery)`); a query without selected columns selects the primary key |
| `Subquery(query)`, `Exists(query)`, `~Exists(query)` | `Subquery(queryset)`, `Exists(queryset)`, `~Exists(queryset)` |
| `OuterRef("pk")` in a subquery | `OuterRef("pk")`, the path translated on the enclosing query's model |
| `order_by()` with `asc(nulls_first=True)` … | `order_by()` with `asc()` / `desc()` and the same NULL placement |
| no ordering | the model's `Meta.ordering`, except on a grouped query |
| `distinct()`, `distinct("field")` | `distinct()`, `distinct("field")` (PostgreSQL's `DISTINCT ON`) |
| slicing | slicing (`LIMIT` / `OFFSET`) |
| `none()` | `none()`: no statement runs |
| `select_related()` | the related fields joined into the same `values_list()` |
| `union(*others, all=)`, `intersection()`, `difference()` | Django's `union(all=)`, `intersection()`, `difference()` of the translated combined queries, then `order_by()` and the slice; `values()` after the set operation is applied to each combined query of model rows, and an ordering key that only that `values()` names orders by the first query's column at the same position; Django leaves out empty combined queries; as a subquery, a combination of model rows selects the primary key |

Each adorm filter is one Django `filter()` call. A condition on a multi-valued relation (reverse foreign key,
many-to-many) joins that relation once per call, as in Django:

```python
# a book with an author named Ann and an author named Cid
await Book.objects.filter(authors__name="Ann").filter(authors__name="Cid").count()
# a book with one author named Ann whose mentor is Cid
await Book.objects.filter(authors__name="Ann", authors__mentor__name="Cid").count()
```

`qs1 & qs2` keeps the filters of both queries and matches as the chained calls do. `qs1 | qs2` puts the conditions
of both queries into one filter; the filters of each side share their joins.

Field paths use adorm names. Each segment is translated with the `orm_names` of the adorm model it reaches; a
reverse relation's accessor name becomes its query name:

```python
notes = [note async for note in Note.objects.filter(copy___title="Dune")]   # Django: copy__title (Note.copy_)
await Book.objects.filter(tagging_set__weight=3).count()                     # Django: tagging__weight
await Book.objects.filter(schema_="v2").exists()                              # Django: schema
```

## Rows

Every read returns rows keyed by adorm names; the core builds the instances.

- A model row has the model's fields, minus those `only()` and `defer()` leave out (deferred on the instance), plus
  the annotations.
- `select_related("club__owner")` adds the related row under the relation's name, nested per step. A missing related
  row (NULL foreign key, no row on the other side of a one-to-one) is `None`. Awaiting the relation reads the cache:

  ```python
  activity = await Activity.objects.select_related("club", "coach").get(pk=1)
  club = await activity.club       # no query
  coach = await activity.coach     # None when the activity has no coach, still no query
  ```

  `select_related()` without paths follows the non-null foreign keys up to five relations deep, as Django does. A
  path that is not a foreign key or one-to-one relation raises `ProgrammingError`. `only("name", "club__name")`
  limits the related row's fields as well.
- `values()` and `values_list()` rows are keyed by the selected names as written (`"club__name"`, `"pk"`). Without
  names, `values()` selects the adorm model's fields and the annotations.

Read operations:

- `get()` runs Django's `get()`. No row raises the model's `DoesNotExist`, several rows its
  `MultipleObjectsReturned`, with Django's messages.
- `first()` and `last()` order by the primary key when neither the query nor the model has an ordering. On a sliced
  query, and on a grouped query, they need `order_by()`; the core raises `TypeError` without it.
- `count()` runs Django's `count()`; on a grouped query it counts the groups.
- `exists()` runs Django's `exists()`; on a combined query it reads the first row (`queryset[:1]`).
- `aggregate()` runs Django's `aggregate()` on the query's model rows and returns its dict.
- `in_bulk()` checks the field as Django's `in_bulk()` does
  ([`check_in_bulk_field()`](/ref/drivers#adorm-drivers-django-translation-check-in-bulk-field-operation)), then reads
  the query's rows filtered with `<field>__in`.

Relations work as on any driver: `await member.club` runs a `get`; `club.memberships` and many-to-many relations
are queries.

```python
club = await Club.objects.get(slug="koper-sailing")
active = await club.memberships.filter(active=True).count()
tags = [tag.name async for tag in (await Activity.objects.get(pk=1)).tags.order_by("name")]
content_type_id = await ContentType.objects.values_list("id", flat=True).get(app_label="billing", model="invoice")
```

## Writes

Writes run through Django's model and query set methods on the slot's thread. A model's `save()` and `delete()`
overrides, Django's signals and the overrides of the query's manager run as in Django code:

| adorm | Django |
|---|---|
| `Model.objects.create(**values)` | `create(**values)` of `_base_manager`, or of the manager the query names (`Query.manager`), so its `create()` override receives arguments that are not fields |
| `Model.objects.bulk_create(objs, ...)` | `bulk_create([Model(**values), ...], batch_size=..., ignore_conflicts=..., update_conflicts=..., update_fields=..., unique_fields=...)` of `_base_manager` or of the query's manager: Django inserts the rows without `save()` and without signals, in one statement per batch (several in one atomic block), and sets the primary keys the database returns (`can_return_rows_from_bulk_insert`; none on MySQL) |
| `obj.save()` of a new instance (no primary key, or `force_insert`) | `Model(**values).save()` |
| `obj.save()` with a primary key | the instance with the adorm instance's fields loaded and the others deferred, then `save()`: Django updates only the loaded fields |
| `obj.save(update_fields=[...])`, `force_update=True` | `save(update_fields=[...])`, `save(force_update=True)`: a missing row raises `DatabaseError` |
| `qs.update(**values)` | `update(**values)` on the query's query set |
| `qs.delete()` | `delete()` on the query's query set (its manager's overrides apply): Django's collector cascades, sets NULL and fires `pre_delete` / `post_delete`; no model's `delete()` runs |
| `obj.delete()` | the instance read with `_base_manager`, then its `delete()`: the model's `delete()` override runs, and Django's own deletes it with the collector; a missing row deletes nothing |
| `qs.get_or_create(...)`, `qs.update_or_create(...)` | `get_or_create(...)`, `update_or_create(...)` on the query's query set |
| `Child.objects.create_from_parent(parent, **values)` | the parent read with `_base_manager`, the child built with its values and the parent link, the child's fields' `pre_save()`, then `save_base(raw=True, force_insert=True)`: only the child's table is inserted, `pre_save` / `post_save` receive `raw=True`, and a `save()` override does not run |
| `obj.full_clean(exclude=[...])` | `full_clean(exclude=..., validate_unique=..., validate_constraints=...)` of the instance with the adorm instance's values over its stored row; the model's `clean()` and every query of the validation, its own included, run on the slot's connection (the profile's alias, whatever the project's routers choose) |

Saving an instance with a primary key that has no row in the database inserts the row when the instance was not
loaded with every field, as Django does for a complete instance. The driver checks for the row with one `exists()`
query first; the target's defaults fill the fields the instance never set. The values the target set (primary key,
`default`, `auto_now_add`, changes by a `save()` override) are copied to the instance:

```python
book = await Book.objects.create(isbn="9780000000099", title="New", pages=10)
book.added, book.published       # set by Django: auto_now_add and default=timezone.now

ann = await Author.objects.only("rating").get(slug="ann")
ann.rating = 4
await ann.save()                 # UPDATE library_author SET rating = 4 WHERE id = ...
```

Field names and lookup keys are adorm names. The driver renames them with `orm_names` and passes a foreign key named
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

A `delete()` override that deletes nothing (a soft delete) returns `(0, {})` to adorm, unless it returns Django's
`(total, {label: count})` itself. The adorm instance keeps its primary key:

```python
class Fine(models.Model):                       # Django
    def delete(self, using=None, keep_parents=False):
        self.waived = True
        self.save(using=using, update_fields=["waived"])

await fine.delete()                             # (0, {}): the fine is waived, fine.pk is unchanged
await Fine.objects.filter(pk=fine.pk).delete()  # (1, {"library.Fine": 1}): no model's delete() runs
```

A delete forbidden by an `on_delete=PROTECT` or `RESTRICT` reference raises `ProtectedError` or `RestrictedError`
before anything is deleted. The exception's objects are complete instances of the registered models; the driver
reads the referencing rows again with every field. The transaction stays usable, as in Django:

```python
async with adorm.atomic():
    try:
        await member.delete()
    except adorm.ProtectedError as exc:
        numbers = [invoice.number for invoice in exc.protected_objects]     # Invoice instances
    await member.save(update_fields=["level"])                             # the transaction goes on
```

## Transactions and savepoints

`begin()` enters an outermost Django `transaction.atomic()` block; `commit()` / `rollback()` leave it. The
operations inside `adorm.atomic()` run in one database transaction on the transaction's slot. Django code on the
driver side (a `save()` override, `get_or_create()`, a signal receiver) sees an atomic block, as in a Django view. A
nested `adorm.atomic()` is a Django savepoint in that block:

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

A database error inside a savepoint (an `IntegrityError`, a cancelled statement) rolls back to the savepoint and
clears Django's rollback mark. The enclosing block continues and can commit:

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

A commit the database refuses raises its error and rolls the transaction back. Example: a deferred foreign key
(Django creates foreign keys on PostgreSQL and SQLite as deferred, checked at `COMMIT`) raises `IntegrityError`, on
PostgreSQL with SQLSTATE `23503`, on SQLite with the detail `FOREIGN KEY constraint failed` and no table. A transaction that failed on the driver side without the core seeing the error (Django marked it for
rollback, or PostgreSQL aborted it) is rolled back by `commit()`, which raises `TransactionManagementError`.

### Row locks

`select_for_update()` becomes Django's `select_for_update()` with `nowait`, `skip_locked`, `of` (adorm paths,
`"self"`) and `no_key` (PostgreSQL only); SQLite has no row locks ([databases](#databases)). Reading rows with `select_for_update()` outside a transaction raises
`TransactionManagementError` in the core, as in Django:

```python
async with adorm.atomic():
    invoice = await Invoice.objects.select_for_update().get(pk=pk)      # other flows wait for the row
    invoice.status = "paid"
    await invoice.save(update_fields=["status"])

async with adorm.atomic():
    free = [job async for job in Job.objects.select_for_update(skip_locked=True).filter(state="new")[:10]]
```

`nowait` on a locked row raises `OperationalError`, on PostgreSQL with SQLSTATE `55P03`. Django's `update_or_create()` locks the row
it updates the same way. `get_or_create()` retries its read after a concurrent insert of the same unique key, so two
flows creating the same row create it once.

### Django's `on_commit()` callbacks

Code on the driver side, such as a signal receiver or a `save()` override, may register Django `on_commit()`
callbacks. Inside an adorm transaction they run after the commit's outcome is delivered, on the slot's thread, in
registration order, before the slot's next request. Callbacks registered with
[`tx.on_commit()`](/topics/transactions#running-code-after-the-commit) run on the client when the slot delivers
`CallbackEvent("on_commit")`, without waiting for Django's. Cancelling the client task while Django's callbacks run
cancels neither them nor the commit:

```python
def queue_welcome(sender, instance, created, **kwargs):           # a Django post_save receiver
    if created:
        transaction.on_commit(lambda: send_welcome.delay(instance.pk))

async with adorm.atomic() as tx:
    member = await Member.objects.create(...)
    tx.on_commit(lambda: cache.invalidate("members"))
# after COMMIT: send_welcome.delay() on the slot's thread, cache.invalidate() on the client
```

- A callback registered in a savepoint or transaction that rolls back does not run.
- A callback that raises after the commit is logged on the `adorm.drivers.django` logger and does not undo the
  commit. One registered without `robust=True` stops the callbacks after it, as in Django.
- Outside an adorm transaction Django runs a callback immediately, inside the write that registers it.

### Signals

Django's signals (`pre_save`, `post_save`, `pre_delete`, `post_delete`, `m2m_changed` …) fire on the slot's thread,
in a copy of the awaiting task's context; a receiver reads the caller's context variables and Django's active
language. adorm has no signals of its own, and Django's produce no event on the client. Reads build no Django model
instances, so `pre_init` and `post_init` fire only for writes.

## Errors

The driver raises adorm's [exceptions](/ref/exceptions)
([`map_django_error()`](/ref/drivers#adorm-drivers-django-map-django-error-exc-vendor-none)):

| Django | adorm |
|---|---|
| `Model.DoesNotExist`, `Model.MultipleObjectsReturned` | `ObjectDoesNotExist`, `MultipleObjectsReturned`; the query set raises the adorm model's own |
| `FieldError`, `FieldDoesNotExist` (an unknown field or lookup) | `FieldError` |
| a segment after a field that is neither a relation nor a JSON field (a Django transform such as `joined__year`), refused before Django sees it | `FieldError` |
| `ValidationError` (a value Django rejects, such as an invalid UUID; a model's `save()` that calls `full_clean()`) | `ValidationError` with each error's message, `code` and `params`, keyed by the operation model's adorm field names |
| `TransactionManagementError` | `TransactionManagementError` |
| `ProtectedError`, `RestrictedError` | `ProtectedError`, `RestrictedError` with the referencing objects as instances of the registered models |
| `django.db` errors on PostgreSQL | by the SQLSTATE of the psycopg error they carry: an invalid regular expression is `DataError` with SQLSTATE `2201B`, a cancelled statement `OperationalError` with `57014`, a terminated connection `OperationalError` with `57P01`; an [`IntegrityError`](/ref/exceptions#adorm-integrityerror-message-none-constraint-name-none-table-name-none-schema-name-none-columns-values-detail-none-model-none-fields-kwargs) carries the constraint, table, columns and values from the psycopg error's `diag`, the model of the table and its adorm field names of the columns |
| `django.db` errors on other databases | by the PEP 249 class of Django's exception, `sqlstate` `None`: on SQLite `IntegrityError` with the table, columns, model and fields of a `UNIQUE` or `NOT NULL` violation and the name of a `CHECK` constraint, no values; a lock held past the busy timeout `OperationalError` `database is locked`. MySQL and Oracle: the key, constraint and columns their messages name ([vendors](/ref/drivers#adorm-drivers-django-vendors)) |
| a sliced or ordered query in a set operation on SQLite, which Django refuses before sending it | `NotSupportedError` |
| anything else (Django's `ValueError` for a value of the wrong type) | `DatabaseError` with the original exception in `original` |

An unknown model label or manager raises `ProgrammingError`. A connection that reports a lost connection is dropped
by its slot; the next request gets a new one. Other errors leave the connection in the pool. What counts as a lost
connection follows the database: on SQLite only an `InterfaceError` (a closed connection), on MySQL the errors 2006,
2013 and 2055, on Oracle `ORA-03113` and its kin; the error's `connection_lost` tells.

```python
try:
    await Club.objects.create(name="Veslaški klub Koper", slug="koper-sailing", ...)
except adorm.IntegrityError as exc:   # on SQLite
    exc.sqlstate, exc.table_name, exc.fields   # None, 'clubs_club', ('slug',)
```

## Custom SQL

Django code inside an operation can execute its own SQL: a `post_save` receiver, a manager's `create()` or `delete()`
override, a model's `save()`, an `on_commit()` callback. When such code runs a statement through the connection's
cursor that does not come from Django's query compiler or database backend, the driver marks the connection
([`mark_custom_sql()`](/ref/drivers#mark-custom-sql)). The slot closes a marked connection when the lease ends, also
after a commit, as the statement may have changed the connection's state.

The driver inspects the callers of Django's cursor wrapper:

- The nearest caller in Django's query compiler or in a database backend (Django's, or the package of the alias's
  own backend, such as PostGIS) makes the statement ORM SQL. Instrumentation that wraps the cursor in between
  (Sentry, tracers, django-debug-toolbar) does not change that.
- A signal receiver or an `on_commit()` callback reached first makes it custom SQL.

| On the slot's thread, inside an operation | Custom SQL |
|---|---|
| query sets, `save()`, `delete()`, `bulk_create()`, `update()`, `select_for_update()` | no |
| Django's `transaction.atomic()` and savepoints | no |
| `connection.cursor().execute(...)`, `Manager.raw()` | yes |
| the same in an `on_commit()` callback, of the transaction or run at once outside one | yes; after a commit the slot decides once the callbacks have run |
| connection options, also those an `on_acquire` hook returns | no |
| a statement the profile's `on_acquire` or `on_release` hook runs through the wrapper | yes |

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

## Schema checks

The driver's [`schema_hash()`](/ref/drivers#schema-hash-label) is the hash of the installed Django model. An
operation on a generated model that is out of date with it raises `SchemaMismatchError` before it runs. The driver
executes `check_table` for [`adorm.check_schema()`](/topics/schema-checks) with one catalogue query per table on
PostgreSQL and Django's introspection (`get_table_description()`) on other databases. It compares the columns' types
with Django's `db_type()` for the alias on PostgreSQL and SQLite, and only columns and nullability on the others,
whose catalogues report types in their own terms. `check_table` reads the catalogue through a cursor of the
thread's connection outside the execute wrappers; it is not custom SQL.

```python
await adorm.check_schema([Club, Member])   # () while the tables match the models
```

## Connection options

On PostgreSQL the driver declares the [connection options](/topics/options) `statement_timeout`, `lock_timeout` and
`idle_in_transaction_session_timeout`, in seconds; on other databases none, and a profile, session or block that sets
one raises `NotSupportedError` (a profile's options when its pool is created). It sets them with
[`set_options_sql()`](/ref/drivers#adorm-drivers-sql-set-options-sql-options-local) on a cursor of the thread's
connection: `SET` / `RESET` outside a transaction, `SET LOCAL` inside one.

```python
with adorm.connection_options(lock_timeout=2):
    await Member.objects.filter(pk=1).update(active=False)   # SET lock_timeout = 2000, then the UPDATE
```

## Cancellation

Cancelling the task that awaits an operation cancels the running statement from another thread; the task gets
`asyncio.CancelledError`, and the connection stays in the pool.

| Database | The driver | The statement ends with |
|---|---|---|
| PostgreSQL | psycopg's `cancel_safe()` (`cancel()` on psycopg 2) | SQLSTATE `57014` |
| SQLite | `interrupt()`; a statement the call starts after it, other than a `ROLLBACK`, raises before it runs; a wait for the write lock ends | `SQLITE_INTERRUPT`, or `LOCK_WAIT_CANCELLED` |
| MySQL, MariaDB | `KILL QUERY` on a connection opened for each cancellation (with the profile's `connect` hook when it has one, else with a 5-second connect timeout); a connection that cannot open, as at `max_connections`, loses the cancellation | error 1317 |
| Oracle | the driver's `cancel()` | `ORA-01013` |
| another backend | nothing: the call runs on, and after the driver's `cancel_grace` seconds the slot gives up its connection | |

- A statement the database cancelled: the `CancelledError` carries `CancellationOutcome.UNKNOWN`.
- A statement that completed on the connection's thread before the cancellation reached the database: the task gets
  `CancelledError` with `CancellationOutcome.SUCCEEDED`, or `FAILED` for an error. A commit is
  `TransactionOutcome.COMMITTED`; a read's rows are discarded.

See [cancellation](/topics/cancellation).

```python
task = asyncio.create_task(Report.objects.filter(year=2026).count())
task.cancel()          # the database stops the statement
```

A cancelled write inside a transaction, such as one waiting for a row lock, rolls the transaction back, including the
writes before it; the slot returns to the pool. Inside a nested `adorm.atomic()` only that savepoint is rolled back
and the enclosing block continues, except on SQLite, which rolls back the whole transaction ([SQLite](#sqlite)). A write outside a transaction that completes just before the cancellation arrives
stays written.

## In Django's tests

Inside Django's `TestCase` the slots share the test's connection, and a transaction is a savepoint in the test's
transaction. Inside `TransactionTestCase` they open their own connections. See [Testing](/topics/testing).

On SQLite a `TransactionTestCase` needs a test database file (`DATABASES[alias]["TEST"]["NAME"]`): Django's default
test database is in memory, which the driver refuses ([SQLite](#sqlite)). Inside a `TestCase` on SQLite, a cancelled
write rolls back the test's transaction, with the test data of `setUpTestData()`.

```python
class MemberTests(TestCase):
    async def test_count(self):
        assert await Member.objects.count() == 0       # on the test's connection, rolled back with the test
```

## Events

- The slot delivers each operation's outcome as a `ResultEvent` or `ErrorEvent` (`CancelledEvent` for a cancelled
  operation).
- The slot delivers `CallbackEvent("on_commit")` after a successful `commit()` and `CallbackEvent("on_rollback")`
  after a rollback; the transaction's own `on_commit()` callbacks run on that event.
- Django's `on_commit()` callbacks run on the slot's thread after the commit's outcome, before the slot's next
  request. Django's signals and `on_commit()` callbacks produce no event.
- The driver consumes no events.

On PostgreSQL with psycopg 3 each connection emits three [backend events](/topics/events#backend-events) from handlers it adds to
the psycopg connection. A connection that does not listen detects that the server ended it only when its next call
fails:

| Event | Source | When |
|---|---|---|
| `BackendEvent("notice", severity, {...})` | psycopg's notice handler, on the slot's thread | the server sends a notice or warning while a statement runs; it arrives before the outcome of the request that caused it |
| `BackendEvent("notify", channel, payload)` | psycopg's notify handler | a notification on a channel an [`adorm.listen()`](/topics/events#listening-for-notifications) block listens on. While the connection listens and no call runs, its thread waits on the connection's socket and reads notifications as they arrive; a call queued meanwhile wakes it at once. A `direct` driver has the event loop watch the socket |
| `BackendEvent("connection_lost", None, reason)` | the thread's wait on the socket | reading the socket failed or libpq reports the connection bad while the connection listens; the connection is unusable from then on |

`LISTEN` and `UNLISTEN` run on the wrapper's cursor and are not custom SQL.

With psycopg 2 the driver emits no backend events: `DjangoDriver.capabilities` is
[`PSYCOPG2_CAPABILITIES`](/ref/drivers#adorm-drivers-django-psycopg2-capabilities), and `adorm.listen()` raises
`NotSupportedError` before it takes a slot.

On the shared connection of Django's `TestCase`, the connection that holds it emits the notices of its statements,
and `adorm.listen()` raises `NotSupportedError`: PostgreSQL delivers notifications on commit, and the test's
transaction never commits. A test that listens uses `TransactionTestCase`.

```python
pool.events.subscribe(BackendEvent, lambda e: print(e.channel, e.payload["message"]), name="notice")
await Club.objects.filter(slug="koper-sailing").update(city="Koper")   # prints "NOTICE clubs updated" (a trigger)
```
