# Schema per tenant

This recipe runs each tenant's queries in that tenant's PostgreSQL schema, as
[django-tenants](https://django-tenants.readthedocs.io/) lays out the database: a schema per tenant with the
tenant's tables and `public` with the shared ones. The [Django driver](/topics/django-driver) on PostgreSQL and the
[native PostgreSQL driver](/topics/postgresql-driver) set the `search_path` [connection option](/topics/options).

## The tenant in a context variable

The application keeps the current tenant's schema in a context variable, set per request. The profile's `on_acquire`
hook returns the search path from it; with no tenant the profile's search path applies.

```python
import contextvars

current_schema: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_schema", default=None)


def on_acquire(connection, profile, session):
    schema = current_schema.get()
    return None if schema is None else {"search_path": (schema, "public")}
```

```python
ADORM = {
    "profiles": {
        "default": {"driver": "adorm.drivers.asyncpg.AsyncpgDriver", "on_acquire": "myapp.tenants.on_acquire"},
    },
}
```

The same value set in a block applies without a hook:

```python
with adorm.connection_options(search_path=(tenant.schema_name, "public")):
    await Member.objects.count()
```

## The tenant in the session

A [session](/topics/sessions) can carry the tenant instead: its `session_name` is the tenant's schema, and
`"default"` is the session without a tenant. `on_acquire` reads it from the session it gets:

```python
def on_acquire_from_session(connection, profile, session):
    if session.session_name == "default":
        return None
    return {"search_path": (session.session_name, "public")}


def content_types_key_from_session(profile, session):
    return session.session_name
```

```python
ADORM = {
    "profiles": {
        "default": {
            "driver": "adorm.drivers.asyncpg.AsyncpgDriver",
            "on_acquire": "myapp.tenants.on_acquire_from_session",
            "content_types_key": "myapp.tenants.content_types_key_from_session",
        },
    },
}
```

The tenant's session runs its queries in the tenant's schema, through its bound models or as the current session:

```python
tenant = adorm.create_session("default", "tenant_a")
await tenant.bind(Member).count()
with tenant.activate():
    await Member.objects.count()
```

Sessions with different names share the profile's pool. Each lease gets the search path of its session, so a slot
that served one tenant serves the next with that tenant's path. The `content_types_key` above is the
[content types](#content-types) key of this variant.

## Content types

When `django.contrib.contenttypes` is in each tenant's schema (django-tenants' `TENANT_APPS`, needed when
`django.contrib.auth` is there), the content type ids differ between tenants. The profile's `content_types_key`
gives each tenant a [content type cache](/ref/models#adorm-contenttypes) of its own:

```python
def content_types_key(profile, session):
    return current_schema.get()
```

```python
ADORM = {
    "profiles": {
        "default": {
            "driver": "adorm.drivers.asyncpg.AsyncpgDriver",
            "on_acquire": "myapp.tenants.on_acquire",
            "content_types_key": "myapp.tenants.content_types_key",
        },
    },
}
```

With `django.contrib.contenttypes` in `public` only, every tenant reads the same ids and the profile needs no key.

## Refusing tenant tables without a tenant

Without a tenant the search path is the profile's, and a query of a tenant model reads `public`, which has no such
table or a stale one. The profile's [`check_operation`](/topics/profiles#refusing-operations) hook refuses those
operations before they reach the database.
[`operation.unqualified_models`](/ref/operations#unqualified-models) names the tables an operation reads or writes
through the search path, cascades of a delete included; `None` means adorm cannot know them, and the hook refuses those
too. Tables the operation qualifies with a schema of its own ([reading across tenants](#reading-across-tenants)) are
not in it.

The tenant tables are those of the apps in django-tenants' `TENANT_APPS` that are not in `SHARED_APPS`, automatic
many-to-many through tables included:

```python
import functools

from django.apps import apps
from django.conf import settings


class NoTenant(Exception):
    """An operation on a tenant table without a current tenant."""


@functools.cache
def tenant_tables():
    shared = set(settings.SHARED_APPS)
    return frozenset(
        model._meta.label
        for config in apps.get_app_configs()
        if config.name in settings.TENANT_APPS and config.name not in shared
        for model in config.get_models(include_auto_created=True)
    )


def check_operation(operation, session):
    if session.session_name != "default" or current_schema.get() is not None:
        return                                             # a tenant: from the session or the context variable
    unqualified = operation.unqualified_models
    if unqualified is None or unqualified & tenant_tables():
        raise NoTenant(f"{operation.kind} of {operation.model} needs a tenant")
```

```python
ADORM = {
    "profiles": {
        "default": {
            "driver": "adorm.drivers.asyncpg.AsyncpgDriver",
            "on_acquire": "myapp.tenants.on_acquire",
            "check_operation": "myapp.tenants.check_operation",
        },
    },
}
```

The awaited call raises `NoTenant` itself; inside an `atomic()` block the block stays usable. Entering `atomic()`,
savepoints, commit, rollback, `listen()` and the pool's start-up schema check are not operations the hook sees.

## Reading across tenants

A shared model can read the rows a tenant model holds for it in every tenant's schema: a person's enrolments in each
organisation they belong to. The organisations of a person come from shared tables (`PersonOrganisation`, with the
organisation's `schema_name`); the enrolments live in one table per organisation's schema. The relation is declared on
the shared Django model with [`TenantRelation`](/ref/tenant-relations#adorm-contrib-django-tenantrelation-to-field-schema-tenant-models-none-session-none),
which adds no column and no migration:

```python
from django.db import models

from adorm.contrib.django import TenantRelation


class Person(models.Model):
    name = models.CharField(max_length=100)
    enrolments = TenantRelation(
        "tenancy.Enrolment",
        field="person",
        schema="organisations__organisation__schema_name",
        tenant_models=["tenancy.Team", "tenancy.Enrolment"],
    )
```

`field` is the enrolment's foreign key to the person, `schema` the path from a person to the schema names of their
organisations, and `tenant_models` the models whose tables each tenant schema holds (an app label stands for all its
models). `adorm generate` emits the relation on the generated model, and `adorm generate --check` keeps it current:

```text
class Person(Model):
    ...
    enrolments = TenantRelation(
        "tenancy.Enrolment",
        column="person_id",
        schema="organisations__organisation__schema_name",
        models=("tenancy.Enrolment", "tenancy.Team"),
    )
```

On an instance the relation reads the rows of every schema the person belongs to, as one statement; filters, the
ordering, the slice, counts and aggregates apply to all of them together:

```python
person = await Person.objects.get(name="Ana")
recent = [enrolment async for enrolment in person.enrolments.filter(role="coach").order_by("-joined")[:10]]
enrolments = await person.enrolments.count()
fees = await person.enrolments.aggregate(Sum("fee"), coaches=Count("id", filter=Q(role="coach")))
teams = [name async for name in person.enrolments.order_by("team__name").values_list("team__name", flat=True)]
people = [await enrolment.person async for enrolment in person.enrolments.select_related("person")]
```

A join to a tenant model (`team`) reads the same schema's table; a join to a shared model (`person`) reads `public`.
Each enrolment remembers its schema: `save()`, `delete()`, `refresh_from_db()` and `await enrolment.team` work in that
schema's tables on any session, and enrolments with the same primary key in two schemas are different instances.
Writes through the relation itself (`create()`, `update()`, `delete()`) raise `NotSupportedError`.

```python
enrolment = await person.enrolments.order_by("joined").first()
enrolment.fee = 0
await enrolment.save()                 # an UPDATE of the enrolment's schema's table
team = await enrolment.team            # the team in the same schema
```

A read runs two operations: a `Select` of the person's schema names from the shared tables, then the union of the
schemas' rows (`SELECT ... FROM "org_a"."tenancy_enrolment" ... UNION ALL SELECT ... FROM "org_b"."tenancy_enrolment"
...`). A person without organisations runs the first one only. Both go through `check_operation`: the first reads
shared tables, and the second names the schema of every tenant table it reads, in
[`operation.schemas`](/ref/operations#schemas), so its `unqualified_models` hold only the shared models it joins. The
hook above lets both run without a tenant and still refuses `Enrolment.objects.count()` and the save of an enrolment
read without a schema. A hook that allows only some schemas compares `operation.schemas` with them.

With the [tenant in the session](#the-tenant-in-the-session), the declaration's argument
`session="myapp.tenants.session_for_schema"` names a function that gives each enrolment its tenant's session, for the
operations that run through the search path afterwards:

```python
def session_for_schema(schema, executor):
    return adorm.create_session("default", schema)
```

Limits:

- One statement reads every schema: its parameters repeat in each branch (asyncpg sends at most 32767 per statement,
  more raises `NotSupportedError`), and the native PostgreSQL driver caches one prepared statement per combination of
  schemas.
- A schema in the listing that does not exist raises the database's error (`ProgrammingError`); no schema is skipped.
- Shared tables are read through the search path, so a tenant's own table of the same name, found first on its search
  path, is read instead.
- The listing and the union are two statements: outside a transaction they can see different states of the shared
  tables. Inside `atomic()` they share the transaction.
- Raw SQL (signal receivers' `cursor.execute()`) is not qualified.
- On SQLite the tenants are attached databases (`ATTACH DATABASE ... AS org_a`, at most 10 by default, 125 at most)
  and a compound statement has at most 500 parts; foreign keys cannot cross databases.

## What the application ensures

adorm sets the search path on its own slots only:

- Django's connections are not adorm's. The django-tenants database backend sets `search_path` from the connection's
  tenant before every cursor, so on the Django driver it overrides the option. A profile of the Django driver uses a
  `DATABASES` alias whose `ENGINE` is `django.db.backends.postgresql` (the same database), or the application keeps
  the backend from setting it.
- One transaction runs with one search path. Code that needs another schema at the same time uses a
  [session](/topics/sessions) of its own, which takes a slot of its own.
- asyncpg caches prepared statements per connection, and PostgreSQL plans a cached statement again when the search
  path changes. When tenants' tables differ (a migration applied to some tenants only), a cached statement whose
  result columns change raises asyncpg's `InvalidCachedStatementError`. The profile's `connect` hook can open
  connections with `statement_cache_size=0`.
