# What is adorm

adorm (Async Django ORM, pronounced "adore") is an asynchronous ORM interface for Python. Applications build
queries and model operations with adorm's own API, and a **driver** executes them on a target ORM or database.
The first drivers target the Django ORM and PostgreSQL directly; a SQLAlchemy driver can follow.

::: info Status
adorm is pre-release. The [API reference](/ref/) lists what the installed version provides.
:::

## The problem adorm solves

Django's ORM is synchronous. Its `a`-prefixed methods (`aget`, `acreate`, …) run the synchronous ORM in a
thread through `sync_to_async`, one call at a time, and part of the API (forward relation access,
transactions) has no async form at all. An async application therefore ends up mixing two styles,
wrapping blocks of ORM code in threads, or moving business logic into Celery workers only because the ORM
cannot run on the event loop.

adorm gives the application one async API, whatever executes the work:

- Every call that touches the database is a coroutine or returns an awaitable. Only configuration is
  synchronous.
- The API follows the Django ORM, without the `a` prefix: `get`, `save`, `count`, `await member.club`,
  `async for` over a queryset.
- Query building blocks (`Q`, `F`, aggregates, lookups) are adorm's own. Building a query needs no connection;
  it produces an adorm operation description.
- A driver turns that description into work on its target. The Django ORM driver calls only the synchronous
  Django ORM, on a thread owned by one slot; the native PostgreSQL driver runs on asyncpg without threads.
- Connections are managed by adorm: a pool of slots per connection profile, logical sessions, transactions
  that hold one slot, acquisition timeouts and deadlock detection.

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

- adorm does not patch Django and does not wrap the Django ORM's own async methods. An application uses either
  adorm or synchronous Django ORM code for a given task; there is no mixed mode.
- adorm has no primitive that runs an arbitrary synchronous function. The application builds a query or an
  operation and awaits its execution; the driver decides how it runs.
- adorm has no signals (`pre_save`, `post_save`, …). Signals of the target ORM fire inside the driver and are
  not visible to adorm's client.
- Schema migrations remain the target ORM's job (for Django, `manage.py migrate`).

## Requirements

- Python 3.11 or newer
- The `django` extra for the Django ORM driver, the `asyncpg` extra for the native PostgreSQL driver

```bash
pip install "adorm[django]"
pip install "adorm[asyncpg]"
```

## Related projects

- [fastapi-viewsets](:fastapi_viewsets:/) serves adorm models through DRF-style viewsets.
- [muxws](:muxws:/) carries operations between an application and a separate adorm server.
- [docs.velis.si](:docs_velis:/) lists the other libraries in this family.
