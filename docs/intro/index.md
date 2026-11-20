# What is adorm

adorm (asynchronous database ORM, pronounced "adore") is a **pure async ORM** for Python. Every call that touches
the database is a coroutine or returns an awaitable; the API has no synchronous form. Only configuration is
synchronous.

adorm itself does not talk to a database. Two parts stand between the application and the database:

- A **driver** executes adorm's operations on its target: a database library or an existing ORM. The native
  PostgreSQL driver runs on asyncpg; the Django driver runs on the Django ORM.
- A **pool** of **slots** per connection profile hands the driver's connections to the application. An async
  driver runs on the event loop. A threaded driver runs a **synchronous** library on a thread owned by one slot,
  with that slot's connection: the event loop awaits the slot and never runs synchronous database code.

The application uses the same async API on every driver. A synchronous database library serves an async application
without `sync_to_async` wrappers, thread-pool executors or worker processes in application code.

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

[Quick start](/intro/quickstart) sets up a project and shows the API in one page.

::: info Status
adorm is pre-release. The [API reference](/ref/) lists what the installed version provides.
:::

## Rationale

Async Python applications (FastAPI, Starlette, Django's ASGI views) serve requests on an event loop. Many database
libraries and ORMs, the Django ORM among them, are synchronous: a call blocks its thread, and on the event loop it
blocks every other request with it. The Django ORM's async methods (`aget`, `acreate`, …) run the synchronous ORM in
a thread through `sync_to_async`, one call at a time; forward relation access and transactions have no async form.
An async application on such an ORM mixes two styles, wraps blocks of ORM code in threads, or moves business logic
into Celery workers because the ORM cannot run on the event loop.

adorm gives such an application one async database API and takes the synchronous code out of it:

- The application describes queries and model operations and awaits them. Whether a driver runs them on the event
  loop or on a slot's thread does not change the application's code.
- Connection handling (pooling, transactions that hold a connection, timeouts, deadlock detection, cancellation of a
  running query) is in adorm, the same for every driver, not in each application.
- An existing synchronous ORM remains usable as a driver, with its models, migrations and database support.

adorm's API is modelled on the Django ORM: querysets, lookups, `Q` and `F`, managers and model instances work as in
Django, without the `a` prefix. With the Django driver an existing Django project moves to adorm one task at a time,
on its existing models and migrations.

## Design goals

- **Pure async.** Everything that executes is a coroutine or returns an awaitable: `get`, `save`, `count`,
  `await member.club`, `async for` over a queryset. The application has one async API whether it runs in a web
  process, a task handler or a Celery task.
- **Queries without I/O.** `Q`, `F`, aggregates and lookups are adorm's own. Building a query needs no connection and
  produces an [operation](/ref/operations): a description of the read or write, encodable as JSON.
- **A core independent of the target.** Queries, models, sessions and the pool import no ORM and no database library;
  they reach a target only through the [driver contract](/topics/drivers). A driver declares its capabilities, and an
  operation it does not support raises `NotSupportedError`. Two drivers on different targets, the Django ORM and
  asyncpg, implement the same contract and pass the same [conformance suite](/topics/conformance).
- **Synchronous libraries as drivers.** A threaded driver runs its library only on its slots' threads; a library
  whose connections are bound to a thread (the Django ORM) keeps one connection per slot.
- **adorm owns the connections.** A pool of slots per [connection profile](/topics/profiles), logical
  [sessions](/topics/sessions), [transactions](/topics/transactions) that hold one slot, layered
  [connection options](/topics/options), acquisition timeouts and [deadlock detection](/topics/pool#deadlocks).
  adorm, not the ORM, decides how long a connection lives.
- **Failures that say what happened.** Exceptions are adorm's own, modelled on Django's (`DoesNotExist`,
  `IntegrityError`, …). A timeout says whether the operation waited for a slot or was executing; a
  [cancellation](/topics/cancellation) says whether the operation took effect.
- **One schema.** Generated [models](/topics/models) are pydantic `BaseModel` subclasses: the same schema serves the
  database layer, validation and serialisation, for example in
  [fastapi-viewsets](:fastapi_viewsets:/).
- **Tests unchanged.** Code that uses adorm is tested with Django's test classes; slots use the test's connection
  ([testing](/topics/testing)).

### Future design considerations

The following directions shape the current design but are not implemented yet.

- **A standalone ORM.** With a native driver adorm needs no Django: it has its own schema definition, SQL
  generation and migrations, the migrations not necessarily versioned as in Django. The Django driver remains for
  projects and packages that move from the Django ORM.
- **A separate server.** The pool and the drivers run in a server process (for example one per PostgreSQL server)
  that every application connects to. In the application a remote driver sends adorm's operation descriptions to the
  server over [muxws](:muxws:/) or HTTP; the server executes them with an ORM driver. The remote driver sends only the
  profile name: database credentials are the server's configuration, and the server authenticates the applications
  that connect to it (plugins; built in: a shared secret and Ed25519 key pairs). The core has no special case for it:
  the remote driver implements the same driver contract.
- **A TypeScript client.** The same API in the browser: a TypeScript client builds the same operation descriptions
  and sends them to a server (FastAPI or similar) that executes them through adorm. GraphQL serves a similar purpose
  with a standard query language and schema of its own; adorm's client instead uses the API and models of the Python
  side, so a query reads the same on the backend and the frontend. The server decides which operations a client may
  run.
- **Rust and Go ports.** The operation descriptions and their encoding are language-neutral JSON. Clients in Rust and
  Go send them to the separate server; a port of the server itself (pool, slots, router) is considered where its
  throughput or memory use matters.

## What adorm is not

- adorm does not patch Django and does not wrap the Django ORM's async methods. A task uses either adorm or
  synchronous Django ORM code; there is no mixed mode.
- adorm has no primitive that runs an arbitrary synchronous function. The application awaits a query or an
  operation; the driver decides how it runs.
- adorm has no signals (`pre_save`, `post_save`, …). Signals of the target ORM fire inside the driver and are
  not visible to adorm's client. The native PostgreSQL driver sends none and refuses a write whose signals have
  receivers the profile does not list as covered.
- adorm has no migrations. With the Django driver the schema belongs to Django's migrations (`manage.py migrate`);
  the native PostgreSQL driver assumes the tables exist. adorm checks the schema instead: the schema hash when a model
  is used, and the tables and columns on `adorm.check_schema()`, `adorm check-schema` or a pool's start-up check
  (`check_schema`). See [Schema checks](/topics/schema-checks).

## Requirements

- Python 3.11 or newer
- Django 5.2 or newer (both drivers use it)
- The `django` extra for the Django ORM driver
- The `asyncpg` extra for the native PostgreSQL driver. It installs asyncpg, Django and psycopg 3: the driver compiles
  its SQL with Django's compiler.

```bash
pip install "adorm[django]"
pip install "adorm[asyncpg]"
```

## Related projects

- [fastapi-viewsets](:fastapi_viewsets:/): DRF-style viewsets for FastAPI with a typed TypeScript client.
- [muxws](:muxws:/): multiplexed, cancellable streams over one WebSocket.
- [docs.velis.si](:docs_velis:/) lists the other libraries in this family.
