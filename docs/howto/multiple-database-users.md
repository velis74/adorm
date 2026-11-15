# Multiple database users

::: info Status
The Django ORM and native PostgreSQL drivers used in the examples are not part of the installed version. Any
`Driver` whose connection `target` is Django's connection wrapper or an `asyncpg.Connection` works (see
[Writing a driver](/howto/writing-a-driver)).
:::

This recipe runs each application user's queries as that user's own PostgreSQL role, with database privileges and
row-level security enforced by the database, while every connection logs in with one account. adorm has no user
management of its own: one [connection profile](/topics/profiles) logs in as the shared account, its `on_acquire`
hook switches the connection to the current user's role with `SET ROLE`, and its `on_release` hook switches it back
with `RESET ROLE`, so that the connection can serve the next user, whoever that is.

## 1. Create the roles

One login role may become every user role. `NOINHERIT` keeps the login role itself without the users' privileges:
it can only act through `SET ROLE`.

```sql
CREATE ROLE alice NOLOGIN;
CREATE ROLE bob NOLOGIN;
CREATE ROLE app_login LOGIN PASSWORD '...' NOINHERIT IN ROLE alice, bob;

GRANT SELECT ON clubs_member TO alice;            -- privileges belong to the user roles
```

A user role created later is added with `GRANT newuser TO app_login`.

## 2. Carry the current user

`on_acquire` runs in the context of the task that acquires the slot, so the role can come from a context variable
that the request middleware sets:

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

A threaded driver runs the hook on the connection's thread in a copy of the acquiring task's context, so the
variable reads the same there.

## 3. Write the hooks

The hooks receive the target's connection, the profile and the session. Quote the role as an identifier; `SET ROLE`
takes no query parameters. Without a current user the hook sets no role, and the connection runs as the login role,
which has no privileges of its own.

With the Django ORM driver the connection is Django's connection wrapper, and the hooks run on the slot's own thread:

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

`on_acquire` runs before the session's first request, outside a transaction, so the role holds for every
operation and transaction of the session on that slot. `on_release` runs before the slot returns to the pool, and
the next session, of any user, gets the slot only after it has finished. A `SET ROLE` that fails (an unknown role,
a role the login may not become) fails the acquisition and the slot is closed. A `RESET ROLE` that fails or does not
finish within the pool's `release_timeout` closes the slot as well, so a connection still in one user's role never
serves another user.

## 4. Register the profile

```python
import adorm

adorm.configure(
    profiles={
        "default": {
            "driver": "myproject.drivers.DjangoDriver",
            "alias": "default",                   # DATABASES["default"] logs in as app_login
            "on_acquire": set_role,
            "on_release": reset_role,
            "pool": {"max_connections": 20},
        },
    },
)
```

All users share the profile's pool, so `max_connections` caps the connections of the whole application.

## 5. Run queries

A request handler needs nothing beyond the middleware; its queries run on the default session in the user's role:

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
  request, and the hooks read `profile.params["role"]`. Each such profile has a pool of its own: connections are not
  shared between users, `max_connections` applies per user, and a pool keeps its idle connections until the profile
  is unregistered (`adorm.settings.unregister_profile()`), so the connection count grows with the number of users who
  were active. The database's connection limit for the login role (`ALTER ROLE app_login CONNECTION LIMIT ...`)
  caps them all together.
- **Row-level security without roles**: keep one role and set a variable instead, `SELECT set_config('app.user_id',
  %s, false)` in `on_acquire` (`$1` with asyncpg) and `RESET app.user_id` in `on_release`; policies read
  `current_setting('app.user_id')`.
- **A role per tenant**: the same hooks with the tenant's role in the context variable.
- **Several hooks**: a hook is any callable, so a function that calls `set_role()` and then sets
  `statement_timeout` combines both. `on_release` then resets both, `RESET ROLE` and `RESET statement_timeout`:
  `RESET ALL` resets the settings but not the role.
