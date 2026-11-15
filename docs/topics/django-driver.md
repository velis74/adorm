# The Django driver

::: info Status
The installed version of the Django driver executes reads: `select`, `get`, `first`, `last`, `count` and `exists`.
Writes (`create`, `save`, `update`, `delete`, `get_or_create`), savepoints, connection options and driver methods
raise `NotSupportedError` before anything runs.
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
`DATABASES` entry, opened when the slot opens and closed when the slot leaves the pool. Its lifetime is the slot's;
Django's `CONN_MAX_AGE` does not apply. The profile's `params` replace keys of the `DATABASES` entry for the
connections of that profile, and the shared `DATABASES` entry stays as it is. The `connect` hook, when set, returns
the DB-API connection (a psycopg connection) the wrapper uses instead of opening one; Django then configures it as
its own (`OPTIONS["isolation_level"]`, time zone, `assume_role`). A psycopg 3 connection needs the parameters Django
opens its own with (its cursor class and type adapters); the hook runs on the slot's thread, so it gets them from
the thread's wrapper. A `connect` hook together with Django's connection pool (`OPTIONS["pool"]`) raises
`NotSupportedError`.

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

`begin()` turns Django's autocommit off and `commit()` / `rollback()` end the transaction and turn it back on, so
reads inside `adorm.atomic()` run in one database transaction on the transaction's slot:

```python
async with adorm.atomic():
    member = await Member.objects.get(email="ann@example.com")
    count = await member.memberships.count()      # same transaction, same slot
```

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

## Errors

The driver raises adorm's [exceptions](/ref/exceptions)
([`map_django_error()`](/ref/drivers#adorm-drivers-django-map-django-error-exc)):

| Django | adorm |
|---|---|
| `Model.DoesNotExist`, `Model.MultipleObjectsReturned` | `ObjectDoesNotExist`, `MultipleObjectsReturned`; the query set raises the adorm model's own |
| `FieldError` (an unknown field or lookup) | `ProgrammingError` |
| `ValidationError` (a value Django rejects, such as an invalid UUID) | `ValidationError` |
| `TransactionManagementError` | `TransactionManagementError` |
| `django.db` errors | by the SQLSTATE of the psycopg error they carry: an invalid regular expression is `DataError` with SQLSTATE `2201B`, a cancelled statement `OperationalError` with `57014`, a terminated connection `OperationalError` with `57P01` |
| anything else (Django's `ValueError` for a value of the wrong type) | `DatabaseError` with the original exception in `original` |

An unknown model label or manager raises `ProgrammingError`. A connection that reports a lost connection is
dropped by its slot; the next request gets a new one. Other errors leave the connection in the pool.

## Cancellation

Cancelling the task that awaits a read cancels the running statement: the driver calls psycopg's `cancel_safe()`
(`cancel()` on psycopg 2) from another thread, the statement ends with `57014`, the task gets
`asyncio.CancelledError` and the connection stays in the pool. A read that completes just before the cancellation
arrives is discarded.

```python
task = asyncio.create_task(Report.objects.filter(year=2026).count())
task.cancel()          # the database stops the statement
```

## Events

The driver emits no events: a read reports its outcome as the result of the call, which the slot delivers as a
`ResultEvent` or `ErrorEvent` (`CancelledEvent` for a cancelled read). It consumes none. Reads build no Django model
instances, so Django's `pre_init` and `post_init` signals do not fire.
