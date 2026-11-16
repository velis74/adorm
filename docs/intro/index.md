# What is adorm

adorm (asynchronous database ORM, pronounced "adore") is an asynchronous ORM for Python. Applications build
queries and model operations with adorm's API, and a **driver** executes them: natively on PostgreSQL (asyncpg) or
through an existing ORM (Django).

::: info Status
adorm is pre-release. The [API reference](/ref/) lists what the installed version provides.
:::

## Why adorm

adorm's API is modelled on the Django ORM: querysets, lookups, `Q` and `F`, managers and model instances work as in
Django, without the `a` prefix. With the Django driver an existing Django project moves to adorm one task at a time,
on its existing models and migrations.

Django's ORM is synchronous. Its `a`-prefixed methods (`aget`, `acreate`, …) run the synchronous ORM in a thread
through `sync_to_async`, one call at a time. Forward relation access and transactions have no async form.
An async application on Django's ORM therefore mixes two styles, wraps blocks of ORM code in threads, or moves business
logic into Celery workers because the ORM cannot run on the event loop.

adorm provides one async API for every driver:

- Every call that touches the database is a coroutine or returns an awaitable. Only configuration is synchronous.
- The names are the Django ORM's, without the `a` prefix: `get`, `save`, `count`, `await member.club`, `async for`
  over a queryset.
- `Q`, `F`, aggregates and lookups are adorm's own. Building a query needs no connection and produces an adorm
  operation description.
- A driver executes that description on its target. The Django ORM driver calls only the synchronous Django ORM, on
  a thread owned by one slot. The native PostgreSQL driver runs every database call on the event loop through
  asyncpg, without threads; it uses Django only without I/O: Django's compiler builds the SQL, and Django's models
  supply the field, relation and deletion metadata and the schema hash.
- adorm manages the connections: a pool of slots per connection profile, logical sessions, transactions that hold one
  slot, acquisition timeouts and deadlock detection.

```python
import adorm

from myapp.adorm_models import Activity, Member


async def rename_member(member_id: int, name: str) -> None:
    async with adorm.atomic():
        member = await Member.objects.get(pk=member_id)
        member.name = name
        await member.save()
        await Activity.objects.create(member_id=member.pk, kind="renamed")


async def active_names() -> list[str]:
    return [m.name async for m in Member.objects.filter(active=True).order_by("name")]
```

## What adorm is not

- adorm does not patch Django and does not wrap the Django ORM's async methods. A task uses either adorm or
  synchronous Django ORM code; there is no mixed mode.
- adorm has no primitive that runs an arbitrary synchronous function. The application awaits a query or an
  operation; the driver decides how it runs.
- adorm has no signals (`pre_save`, `post_save`, …). Signals of the target ORM fire inside the driver and are
  not visible to adorm's client.
- adorm has no migrations. With the Django driver the schema belongs to Django's migrations (`manage.py migrate`);
  the native PostgreSQL driver assumes the tables exist. adorm checks the schema instead: the schema hash when a model
  is used, and the tables and columns on `adorm.check_schema()`, `adorm check-schema` or a pool's start-up check
  (`check_schema`). See [Schema checks](/topics/schema-checks).

## Requirements

- Python 3.11 or newer
- The `django` extra for the Django ORM driver
- The `asyncpg` extra for the native PostgreSQL driver. It installs asyncpg, Django and psycopg 3: the driver compiles
  its SQL with Django's compiler.

```bash
pip install "adorm[django]"
pip install "adorm[asyncpg]"
```

## Related projects

- [fastapi-viewsets](:fastapi_viewsets:/) serves adorm models through DRF-style viewsets.
- [muxws](:muxws:/) carries operations between an application and a separate adorm server.
- [docs.velis.si](:docs_velis:/) lists the other libraries in this family.
