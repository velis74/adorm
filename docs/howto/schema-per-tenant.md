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
await tenant.Member.objects.count()
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
operations before they reach the database. [`operation.models`](/ref/operations#models) names the tables an operation
reads or writes, cascades of a delete included; `None` means adorm cannot know them, and the hook refuses those too.

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
    if operation.models is None or operation.models & tenant_tables():
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
