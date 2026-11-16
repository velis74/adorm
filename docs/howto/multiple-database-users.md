# Multiple database users

::: info Status
The [Django driver](/topics/django-driver) and the [native PostgreSQL driver](/topics/postgresql-driver) run the
hooks of this recipe and execute reads, writes, row locks and savepoints. Any `Driver` whose connection `target` is Django's connection wrapper or an
`asyncpg.Connection` works too (see [Writing a driver](/howto/writing-a-driver)).
:::

This recipe runs each application user's queries as that user's PostgreSQL role, so the database enforces privileges
and row-level security, while every connection logs in with one account. adorm has no user management of its own:

- one [connection profile](/topics/profiles) logs in as the shared account;
- its `on_acquire` hook switches the connection to the current user's role with `SET ROLE`;
- its `on_release` hook switches it back with `RESET ROLE` before the connection serves the next user.

## 1. Create the roles

One login role may become every user role. With `NOINHERIT` the login role has none of the users' privileges and
acts only through `SET ROLE`.

```sql
CREATE ROLE alice NOLOGIN;
CREATE ROLE bob NOLOGIN;
CREATE ROLE app_login LOGIN PASSWORD '...' NOINHERIT IN ROLE alice, bob;

GRANT SELECT ON clubs_member TO alice;            -- privileges belong to the user roles
```

A user role created later is added with `GRANT newuser TO app_login`.

## 2. Carry the current user

`on_acquire` runs in the context of the task that acquires the slot, so the role can come from a context variable set
by the request middleware:

```python
from contextvars import ContextVar

db_role: ContextVar[str | None] = ContextVar("db_role", default=None)


async def db_role_middleware(request, call_next):
    token = db_role.set(request.user.username if request.user.is_authenticated else None)
    try:
        return await call_next(request)
    finally:
        db_role.reset(token)
```

A threaded driver runs the hook on the connection's thread in a copy of the acquiring task's context, with the same
variable value.

## 3. Write the hooks

The hooks receive the target's connection, the profile and the session. Quote the role as an identifier; `SET ROLE`
takes no query parameters. Without a current user the hook sets no role and the connection runs as the login role,
which has no privileges of its own.

With the Django ORM driver the connection is Django's connection wrapper and the hooks run on the slot's thread:

```python
def set_role(connection, profile, session):
    role = db_role.get()
    if role is not None:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE " + connection.ops.quote_name(role))


def reset_role(connection, profile, session):
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
```

With the native PostgreSQL driver the connection is an `asyncpg.Connection` and the hooks are coroutines:

```python
def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def set_role(connection, profile, session):
    role = db_role.get()
    if role is not None:
        await connection.execute("SET ROLE " + quote_identifier(role))


async def reset_role(connection, profile, session):
    await connection.execute("RESET ROLE")
```

- `on_acquire` runs before the session's first request, outside a transaction; the role holds for every operation and
  transaction of the session on that slot.
- `on_release` runs before the slot returns to the pool; the next session gets the slot only after it has finished.
- A `SET ROLE` that fails (an unknown role, a role the login may not become) fails the acquisition and closes the
  slot.
- A `RESET ROLE` that fails or does not finish within the pool's `release_timeout` closes the slot, so a connection in
  one user's role never serves another user.

## 4. Register the profile

```python
import adorm

adorm.configure(
    profiles={
        "default": {
            "driver": "adorm.drivers.django.DjangoDriver",
            "alias": "default",                   # DATABASES["default"] logs in as app_login
            "on_acquire": set_role,
            "on_release": reset_role,
            "pool": {"max_connections": 20},
        },
    },
)
```

With the [Django app](/topics/profiles#configuring-from-django-settings) installed, the same profile goes into the
`ADORM` setting, with the hooks as import paths:

```python
# settings.py
ADORM = {
    "profiles": {
        "default": {
            "driver": "adorm.drivers.django.DjangoDriver",
            "on_acquire": "myapp.roles.set_role",
            "on_release": "myapp.roles.reset_role",
            "pool": {"max_connections": 20},
        },
    },
}
```

All users share the profile's pool; `max_connections` caps the connections of the whole application.

## 5. Run queries

A request handler needs only the middleware; its queries run on the default session in the user's role:

```python
async def member_list(request):
    return [m async for m in Member.objects.filter(active=True)]
```

`SELECT current_user` then returns the user's role and `session_user` the login role; a table the role has no
privilege on raises `ProgrammingError` with SQLSTATE `42501`.

## Variations

- **The role from the session**: a hook can read `session.session_name` instead of a context variable, e.g. with
  sessions created as `adorm.create_session("default", f"user:{username}")`.
- **A profile per user**: a [profile provider](/topics/profiles#profiles-created-at-run-time) creates
  `ConnectionProfile(f"user:{name}", ..., params={"role": name}, on_acquire=..., on_release=...)` on the user's first
  request, and the hooks read `profile.params["role"]`. Each such profile has its own pool: users share no
  connections and `max_connections` applies per user. A pool closes an idle connection after
  `purge_stale_connections_at` seconds (300 by default), so the connection count follows the users active within that
  time. `adorm.settings.unregister_profile()` closes a user's pool at once. The login role's connection limit
  (`ALTER ROLE app_login CONNECTION LIMIT ...`) caps all of them together.
- **Row-level security without roles**: keep one role and set a variable instead, `SELECT set_config('app.user_id',
  %s, false)` in `on_acquire` (`$1` with asyncpg) and `RESET app.user_id` in `on_release`; policies read
  `current_setting('app.user_id')`.
- **A role per tenant**: the same hooks with the tenant's role in the context variable.
- **Several hooks**: a hook is any callable; a function that calls `set_role()` and then sets `app.user_id` combines
  both. `on_release` then runs `RESET ROLE` and `RESET app.user_id`: `RESET ALL` resets the settings but not the role.
- **Time limits per user**: `statement_timeout` and `lock_timeout` are [connection options](/topics/options), set by
  the profile's `options`, `session.set_options()` or `adorm.connection_options()`. A hook may set them too; the slot
  sets its own values again after each hook.
