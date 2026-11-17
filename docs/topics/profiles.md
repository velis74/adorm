# Connection profiles

A **connection profile** is a named entry in adorm's settings: the driver, the target's database alias, extra
driver parameters, the hooks that run around a connection's use, connection options and pool settings. A session's
`using` is a profile name, as a database alias is in Django. A call carries no connection parameters; different
database accounts are different profiles.

The [Django driver](/topics/django-driver) and the [native PostgreSQL driver](/topics/postgresql-driver) run profile
hooks and execute reads, writes, row locks and savepoints. Any `Driver` implementation works (see [Writing a driver](/howto/writing-a-driver)). The Django app
`adorm.contrib.django` configures adorm from the `ADORM` setting.

```python
import adorm

adorm.configure(
    profiles={
        "default": {"driver": "adorm.drivers.django.DjangoDriver", "alias": "default"},
        "reports": {
            "driver": "adorm.drivers.asyncpg.AsyncpgDriver",
            "params": {"USER": "reports", "PASSWORD": REPORTS_PASSWORD},
            "pool": {"max_connections": 4, "acquire_timeout": 5},
        },
    },
    pool={"max_connections": 20},       # profiles without "pool" use these settings
)

await Member.objects.count()                                   # a slot of "default"
reports = adorm.create_session("reports", "nightly")
await reports.Member.objects.filter(active=True).count()       # a slot of "reports"
```

## Configuring

[`adorm.configure()`](/ref/settings#adorm-configure-profiles-none-providers-pool-none-debug-none) replaces the whole
configuration: the profiles, the [profile providers](#profiles-created-at-run-time), the default pool settings and
the executor's [debug switch](/ref/transactions#debug-mode). A profile is a
[`ConnectionProfile`](/ref/settings#adorm-connectionprofile) or a mapping of its arguments.

- Every value is checked before anything changes; an invalid configuration leaves the previous one in place.
- Profiles, providers and pool settings are replaced in one step. An operation in another thread or event loop sees
  the old or the new configuration; a profile name present in both resolves to the old or the new profile, never to
  none.
- A profile with the same values as the registered one keeps the registered one and its pools.

```python
from adorm import ConnectionProfile, PoolSettings

adorm.configure(
    profiles={
        "default": ConnectionProfile(
            "default",
            DjangoDriver,                         # a Driver subclass, an instance, or an import path
            alias="default",
            pool=PoolSettings(max_connections=10, commit_timeout=30),
        ),
    },
    debug=True,
)
```

The driver is a `Driver` instance, a `Driver` subclass (instantiated without arguments) or an import path,
`"package.module.Name"` or `"package.module:Name"`. A path is imported when the profile's pool is created;
configuring adorm imports no ORM.

| Setting | Meaning |
|---|---|
| `max_connections` | the maximum number of slots of the profile's pool; `0` opens connections until the database refuses one |
| `acquire_timeout` | seconds a request waits for a slot, then `SessionTimeout` |
| `commit_timeout` | seconds a slot inside a transaction waits for the next request before it rolls back; `None` for no limit |
| `release_timeout` | seconds the `on_release` hook may run before the slot leaves the pool, and the time a released slot waits for its connection to settle |
| `purge_stale_connections_at` | seconds an idle slot stays in the pool before its connection closes (default 300); `None` keeps idle slots until the pool closes |
| `check_schema` | `"warn"` or `"raise"` runs the [schema check](/topics/schema-checks#at-pool-start-up) before the pool serves its first acquisition; `False` (the default) does not |

`acquire_timeout` and `commit_timeout` are the profile layer of the [connection options](/topics/options) of the
same names; a session or a `connection_options()` block overrides them.

## Configuring from Django settings

Each process configures adorm itself. In a Django project the app `adorm.contrib.django` does it in
`AppConfig.ready()` on `django.setup()`, in every process that sets Django up (web workers, Celery workers,
management commands):

```python
# settings.py
INSTALLED_APPS = [
    ...,
    "adorm.contrib.django",
]

ADORM = {
    "pool": {"max_connections": 10, "acquire_timeout": 10, "commit_timeout": 30},
}
```

`ADORM` holds the keyword arguments of `adorm.configure()`: `profiles`, `providers`, `pool` and `debug`.

- Without `profiles`, every alias of `DATABASES` becomes a profile of the same name on the
  [Django driver](/topics/django-driver). The setting above gives a `default` profile, and one per further alias,
  with the pool settings shown.
- Without `ADORM`, the same profiles have the default pool settings.
- `"profiles": {}` configures no profile.

The settings module cannot import code that imports models. A profile's hooks (`connect`, `on_acquire`,
`on_release`) and the providers may be import paths, `"package.module.name"` or `"package.module:name"`; the app
imports them in `ready()`, after the models are loaded. A driver given by import path stays a path in the profile;
the app imports it in `ready()` to check the profile's hooks and options against it, so a misspelled driver path
stops `django.setup()`.

```python
ADORM = {
    "profiles": {
        "default": {
            "driver": "adorm.drivers.django.DjangoDriver",
            "alias": "default",
            "on_acquire": "myapp.roles.current_role",
            "options": {"statement_timeout": 30},
        },
        "reports": {
            "driver": "adorm.drivers.asyncpg.AsyncpgDriver",
            "alias": "reports",
            "pool": {"max_connections": 4},
        },
    },
    "providers": ["myapp.tenants.tenant_profile"],
    "pool": {"max_connections": 20},
    "debug": DEBUG,
}
```

An invalid value stops `django.setup()` with `ImproperlyConfigured`, which names the entry, for example
`ADORM['profiles']['default']['on_acquire']: module 'myapp.roles' has no hook 'current_rol'`; adorm's previous configuration stays.

Overriding `ADORM` in a test (`override_settings`, pytest-django's `settings` fixture) reconfigures adorm on entry
and on exit; so does overriding `DATABASES` while `ADORM` has no `profiles`.

- Profiles whose values do not change keep their pools.
- Pools of replaced profiles close. A transaction holding a slot of such a pool finishes on it; a request waiting for
  one of its slots continues on the new profile's pool.
- Later operations use the new profiles.

```python
@override_settings(ADORM={"profiles": {"default": {"driver": "adorm.drivers.django.DjangoDriver"}}, "debug": True})
class DebugTests(TransactionTestCase):
    ...
```

A process without the app calls
[`configure_from_settings()`](/ref/settings#adorm-contrib-django-configure-from-settings) after `django.setup()`, or
`adorm.configure()` with its own arguments. A later `adorm.configure()` call replaces the app's configuration.

## Connection options

A profile's `options` are the database connection options of its sessions, unless the session or a
[`connection_options()`](/topics/options) block sets another value. A name the driver does not declare raises
`NotSupportedError` when the profile is created with a driver instance or class, else when its pool is created.

```python
adorm.configure(
    profiles={
        "default": {"driver": "adorm.drivers.django.DjangoDriver", "options": {"statement_timeout": 30}},
        "reports": {"driver": "adorm.drivers.asyncpg.AsyncpgDriver", "options": {"statement_timeout": 600}},
    }
)
```

The slot sets them once per connection and keeps them; see [what reaches the database](/topics/options#what-reaches-the-database).

## The default router

With a profile or a provider configured, operations outside a transaction and `atomic()` blocks run on the
**default router** of the running event loop
([`adorm.settings.default_router()`](/ref/settings#adorm-settings-default-router-create-true)).

- It creates a profile's pool on the first session that names it.
- Each event loop has its own default router; a pool serves one event loop.
- [`set_runner()`](/ref/sessions#adorm-executor-set-runner-runner) takes precedence over it.
- Its pools close when the loop shuts down under `asyncio.run()`, `asyncio.Runner` or asgiref's `async_to_sync`, or
  on `close_pools()`.

```python
async def lifespan(app):
    yield
    await adorm.settings.close_pools()      # closes the connections of this event loop
```

A session whose `using` names no profile raises `InterfaceError` on its first operation.

## Hooks

A profile has three optional hooks:

| Hook | Runs | Typical use |
|---|---|---|
| `connect(profile)` | when a slot opens its connection, instead of the driver's default | credentials from a vault, custom connection arguments |
| `on_acquire(connection, profile, session)` | when a slot is assigned to a session, before its first request; returns `None` or a mapping of [connection options](/topics/options) for the session's lease | the current user's `role`, a custom option such as `app.user_id` for row-level security |
| `on_release(connection, profile, session)` | when the session returns a usable slot to the pool; its return value is ignored | state outside the database connection, such as a log of who held the slot |

`connection` is the target's connection: Django's connection wrapper for the Django driver, an
`asyncpg.Connection` for asyncpg. `session` is the session that gets or returns the slot.

A threaded driver runs `connect` and the hooks on the connection's thread; they must be plain functions. A coroutine
function raises `TypeError` when the profile is created, or, for a driver given by import path, when its pool is
created. An async driver awaits a hook's result when it is awaitable.

`on_acquire` runs in the context of the task that acquires the slot and can read the application's context
variables, such as the current user; one profile and one pool can serve every user.

```python
def on_acquire(connection, profile, session):
    user = current_user.get()
    if user is None:
        return None                                        # the profile's options only
    return {"role": user.username, "app.user_id": str(user.pk)}
```

The options `on_acquire` returns are the lease's layer between the profile's `options` and the session's
([layers](/topics/options#connection-options)). The slot sets them before the acquisition completes and sends only
what differs from the connection's current values; a lease of the same user on the same slot sends nothing. They
end with the lease: the next lease's operations run with that lease's options, and a block that overrides one of
them restores the hook's value when it ends. A value the database refuses (a role the login may not become) fails
the acquisition. An option the driver does not declare raises `NotSupportedError`; another return value raises
`TypeError`, and `acquire_timeout` or `commit_timeout` `ValueError`. Each of these takes the slot out of the pool.

Session parameters are set only through connection options. SQL a hook runs on `connection` is custom SQL: the slot
does not know what it changed, so it leaves the pool when the lease ends, as after [custom SQL of an
operation](/topics/pool#slot-lifecycle), and `on_release` does not run for it. The Django driver sees the statements
a hook runs through Django's wrapper (not those on its DB-API connection), the native PostgreSQL driver every
statement on the `asyncpg.Connection` but `COPY`.
`on_acquire` runs before the slot sets the lease's options, so SQL it runs sees the options the previous lease left.

An atomic operation outside a transaction runs `on_acquire`, the operation and `on_release`. A transaction runs
`on_acquire` before `BEGIN` and `on_release` after `COMMIT` or `ROLLBACK`. The next session gets the slot after
`on_release` finishes.

Hook failures:

| Situation | Outcome |
|---|---|
| `on_acquire` raises | the acquisition, or the entry of `atomic()`, raises the error (mapped by the driver); the slot leaves the pool |
| `on_acquire` runs past the acquisition's timeout | it is cancelled, the slot leaves the pool, the acquisition raises `SessionTimeout` (`DeadlockError` when the flow holds another slot) |
| the acquiring task is cancelled during `on_acquire` | the hook is cancelled and the slot leaves the pool |
| `on_release` raises or runs past `release_timeout` | the error is logged, the slot leaves the pool |
| the slot leaves the pool anyway (discarded, connection unusable, transaction left open, rolled back after `commit_timeout`) | `on_release` does not run |

A threaded driver cancels a hook by asking the connection's thread to stop it. A hook that does not stop extends
the release or the acquisition by up to the driver's `cancel_grace` seconds; then the connection is given up.

A slot that leaves the pool closes its connection, and the next request opens a new one; session state set by a
hook's SQL never reaches another session. The [multiple database users](/howto/multiple-database-users) recipe
returns the user's `role` from `on_acquire`.

## Profiles created at run time

Profiles can be registered at run time, for example one per user:

```python
from adorm.settings import register_profile, unregister_profile

register_profile(ConnectionProfile("user:alice", DjangoDriver, options={"role": "alice"}))
...
unregister_profile("user:alice")       # its pools close
```

A **profile provider** creates profiles on demand: a callable that takes a profile name and returns a
`ConnectionProfile`, `None` when it does not provide that name, or an awaitable of either. The first session that
names an unknown profile asks the providers in the order they were added; the first profile returned is registered.

```python
from adorm.settings import add_profile_provider


@add_profile_provider
async def per_user(name: str) -> ConnectionProfile | None:
    if not name.startswith("user:"):
        return None
    user = name.removeprefix("user:")
    return ConnectionProfile(name, DjangoDriver, options={"role": await role_of(user)})


session = adorm.create_session(f"user:{request.user.username}", "web")
await session.Member.objects.count()
```

Each profile has its own pool, which closes an idle connection after `purge_stale_connections_at` seconds
([slot lifecycle](/topics/pool#slot-lifecycle)). Per-user profiles hold connections for the users active within
that time. A single profile whose `on_acquire` reads the user from a context variable shares its connections
between users.

```python
adorm.configure(providers=[per_user], pool={"max_connections": 2, "purge_stale_connections_at": 120})
```

Concurrent first uses of one name ask the providers once. A provider that raises fails every waiting request; the
next request asks again.

When a profile is unregistered or replaced (`register_profile(profile, replace=True)`), or the default pool settings
change for a profile without its own, every router built from the settings stops routing to its pool and closes it.
Idle slots close at once, held slots when their lease is released. The next session that names the profile gets a
pool built from the current profile, or from a provider when none is registered. A request waiting for a slot of the
closed pool moves to that pool within its timeout, or raises `InterfaceError` when no profile of the name remains.

## Where a profile lives

A profile lives where its pool runs. Its parameters, credentials and hooks are not part of a session or an
operation: a session is the pair `(using, session_name)`, and `using` is the profile name.

## Routers with profiles

A [`Router`](/ref/pool#adorm-executor-router-pools-profiles-none) built in code can take its pools from a
[`ProfileRegistry`](/ref/settings#adorm-settings-profileregistry-pool-none) as the default router does; pools
added with `add()` take precedence over profiles of the same name and are not affected by profile changes.

```python
from adorm.executor import Router, set_runner
from adorm.settings import ProfileRegistry

registry = ProfileRegistry(pool={"max_connections": 5})
registry.register(ConnectionProfile("default", AsyncpgDriver()))
router = Router(profiles=registry)
set_runner(router)
```

## Events

Connection profiles emit and consume no events. Callbacks and backend events a connection emits while a hook runs
follow the [pool's rules](/topics/pool#events): during `on_acquire` they go to the new lease's `events` queue, during
`on_release` to the pool's `events` bus.
