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
