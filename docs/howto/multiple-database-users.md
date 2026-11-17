# Multiple database users

::: info Status
The [Django driver](/topics/django-driver) and the [native PostgreSQL driver](/topics/postgresql-driver) set the
`role` and custom [connection options](/topics/options) of this recipe and execute reads, writes, row locks and
savepoints. Any `Driver` that declares these options works too (see [Writing a driver](/howto/writing-a-driver)).
:::

This recipe runs each application user's queries as that user's PostgreSQL role, so the database enforces privileges
and row-level security, while every connection logs in with one account. adorm has no user management of its own:

- one [connection profile](/topics/profiles) logs in as the shared account;
- the `role` [connection option](/topics/options) switches the connection to the current user's role: its
  `on_acquire` hook returns it for each use of a slot, or a profile per user sets it in its `options`;
- the slot sets the role with `SET role` before the session's first operation and replaces it for the next session;
  no hook runs SQL.

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

## 3. Write the hook

The hook returns the connection options of the session's use of the slot. Without a current user it returns `None`
and the connection runs as the login role, which has no privileges of its own. The same function serves both drivers:

```python
def current_role(connection, profile, session):
    role = db_role.get()
    return None if role is None else {"role": role}
```

- The slot sends `SET role = "alice"` (the name quoted as an identifier) before the session's first request, outside
  a transaction; the role holds for every operation and transaction of the session on that slot.
- The next session on the slot runs with its own role: the slot sends `SET role = "bob"`, or, for a session without
  a user, restores the profile's role: `RESET role`, or `SET role` to `OPTIONS["assume_role"]` (see below). A session
  of the same user on the same slot sends nothing.
- A role the login may not become, or one that does not exist, fails the acquisition and closes the slot.
- A `with adorm.connection_options(role=...)` block inside the session runs as another role and restores the hook's
  role when it ends.
- Inside Django's `TestCase` the role is set with `SET LOCAL` before each operation
  ([testing](/topics/testing#connection-options-and-profile-hooks)).

A hook that runs SQL on its `connection` argument instead runs custom SQL: the slot cannot tell what that SQL set,
so it closes the connection when the session releases it.

## 4. Register the profile

```python
import adorm

adorm.configure(
    profiles={
        "default": {
            "driver": "adorm.drivers.django.DjangoDriver",
            "alias": "default",                   # DATABASES["default"] logs in as app_login
            "on_acquire": current_role,
            "pool": {"max_connections": 20},
        },
    },
)
```

With the [Django app](/topics/profiles#configuring-from-django-settings) installed, the same profile goes into the
`ADORM` setting, with the hook as an import path:

```python
# settings.py
ADORM = {
    "profiles": {
        "default": {
            "driver": "adorm.drivers.django.DjangoDriver",
            "on_acquire": "myapp.roles.current_role",
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

- **The role from the session**: the hook reads `session.session_name` instead of a context variable, e.g. with
  sessions created as `adorm.create_session("default", f"user:{username}")`.
- **A profile per user**: a [profile provider](/topics/profiles#profiles-created-at-run-time) creates
  `ConnectionProfile(f"user:{name}", ..., options={"role": name})` on the user's first request; no hook is needed.
  Each such profile has its own pool: users share no connections and `max_connections` applies per user. A pool
  closes an idle connection after `purge_stale_connections_at` seconds (300 by default), so the connection count
  follows the users active within that time. `adorm.settings.unregister_profile()` closes a user's pool at once. The
  login role's connection limit (`ALTER ROLE app_login CONNECTION LIMIT ...`) caps all of them together.
- **Row-level security without roles**: keep one role and return a custom option instead,
  `{"app.user_id": str(user.pk)}`; policies read `current_setting('app.user_id', true)`. A session without a user
  runs with the option reset, which that call reads as `NULL` or an empty string.
- **A role per tenant**: the same hook with the tenant's role in the context variable.
- **Role and user together**: the hook returns both, `{"role": role, "app.user_id": user_id}`.
- **Time limits per user**: `statement_timeout` and `lock_timeout` are connection options too; the hook may return
  them with the role.
- **`assume_role`**: a `DATABASES` entry's `OPTIONS["assume_role"]` starts the profile's `role`; a user's role from
  the hook replaces it for that user's sessions, and a session without a user runs with it.
