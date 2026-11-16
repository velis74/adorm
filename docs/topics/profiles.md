# Connection profiles

::: info Status
The installed version provides connection profiles, their hooks, run-time profiles and the default router built
from them, connection options and pool settings in a profile, and the Django app `adorm.contrib.django`, which
configures adorm from the `ADORM` setting.
The [Django driver](/topics/django-driver) and the [native PostgreSQL driver](/topics/postgresql-driver) run
profiles' hooks; both execute reads, writes, row locks and savepoints. Any `Driver` implementation (see [Writing a driver](/howto/writing-a-driver))
works.
:::

A **connection profile** is a named entry in adorm's settings. A session's `using` is the name of a profile, as a
database alias is in Django. The profile says how its connections are made: the driver, the target's database
alias, extra driver parameters and the hooks that run around a connection's use. A call never carries connection
parameters: different database accounts are different profiles.

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
the executor's debug switch. A profile is a [`ConnectionProfile`](/ref/settings#adorm-connectionprofile) or a
mapping of its arguments; every value is checked before anything changes, so an invalid configuration leaves the
previous one in place. The new profiles, providers and pool settings replace the old ones in one step: an operation
running meanwhile in another thread or event loop sees either the old configuration or the new one, never a state
between them, so a profile name present in both resolves to the old or the new profile, never to none. A profile with
the same values as the registered one keeps the registered one and its pools.

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

The driver may be a `Driver` instance, a `Driver` subclass (instantiated without arguments) or an import path,
`"package.module.Name"` or `"package.module:Name"`. A path is imported when the profile's pool is created, so
configuring adorm imports no ORM.

| Setting | Meaning |
|---|---|
| `max_connections` | the maximum number of slots of the profile's pool; `0` opens connections until the database refuses one |
| `acquire_timeout` | seconds a request waits for a slot, then `SessionTimeout` |
| `commit_timeout` | seconds a slot inside a transaction waits for the next request before it rolls back; `None` for no limit |
| `release_timeout` | seconds the `on_release` hook may run before the slot leaves the pool, and the time a released slot waits for its connection to settle |
| `purge_stale_connections_at` | seconds an idle slot stays in the pool before its connection closes (default 300); `None` keeps idle slots until the pool closes |
| `check_schema` | `"warn"` or `"raise"` runs the [schema check](/topics/schema-checks#at-pool-start-up) before the pool serves its first acquisition; `False` (the default) does not |

`acquire_timeout` and `commit_timeout` are also the profile layer of the [connection options](/topics/options) of
the same names, which a session or a `connection_options()` block can override.

## Configuring from Django settings

Each process configures adorm itself. In a Django project the app `adorm.contrib.django` does it on `django.setup()`
(in its `AppConfig.ready()`), so the web workers, the API process, Celery workers and management commands all run with
the same configuration:

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

`ADORM` holds the keyword arguments of `adorm.configure()`: `profiles`, `providers`, `pool` and `debug`. Without
`profiles`, every alias of `DATABASES` becomes a profile of the same name on the
[Django driver](/topics/django-driver), so the setting above gives a `default` profile (and one per further alias)
with the pool settings shown; without `ADORM` at all, the same profiles have the default pool settings. `"profiles": {}`
configures no profile.

The settings module is imported before Django's apps are loaded, so it cannot import code that imports models. A
profile's hooks (`connect`, `on_acquire`, `on_release`) and the providers may therefore be import paths,
`"package.module.name"` or `"package.module:name"`; the app imports them in `ready()`, when the models are loaded. A
driver given by import path stays a path in the profile, but the app imports it in `ready()` too, to check the
profile's hooks and options against it; a misspelled driver path stops `django.setup()`.

```python
ADORM = {
    "profiles": {
        "default": {
            "driver": "adorm.drivers.django.DjangoDriver",
            "alias": "default",
            "on_acquire": "myapp.roles.set_role",
            "on_release": "myapp.roles.reset_role",
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
`ADORM['profiles']['default']['on_acquire']: module 'myapp.roles' has no hook 'set_rol'`; adorm's previous configuration stays.

A test that overrides `ADORM` (`override_settings`, pytest-django's `settings` fixture) reconfigures adorm on entry
and again on exit, as does one that overrides `DATABASES` while `ADORM` has no `profiles`. Profiles whose values do
not change keep their pools. Pools of the replaced profiles close; a transaction that holds a slot of such a pool
finishes on it, a request waiting for a slot of it continues on the new profile's pool, and operations after the
change use the new profiles.

```python
@override_settings(ADORM={"profiles": {"default": {"driver": "adorm.drivers.django.DjangoDriver"}}, "debug": True})
class DebugTests(TransactionTestCase):
    ...
```

A process that does not install the app calls
[`configure_from_settings()`](/ref/settings#adorm-contrib-django-configure-from-settings) after `django.setup()` to
apply the same setting, or `adorm.configure()` with its own arguments. A later `adorm.configure()` call replaces the
configuration the app made.

## Connection options

A profile's `options` are database connection options every session of the profile runs with, unless the session
or a [`connection_options()`](/topics/options) block sets another value. A name the driver does not declare raises
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

Once a profile or a provider is configured, operations outside a transaction and `atomic()` blocks run on the
**default router** of the running event loop
([`adorm.settings.default_router()`](/ref/settings#adorm-settings-default-router-create-true)). It creates the pool
of a profile on the first session that names it. Each event loop has its own default router, because a pool
serves one event loop; [`set_runner()`](/ref/sessions#adorm-executor-set-runner-runner) takes precedence over it.
The router's pools close when the loop shuts down, with `asyncio.run()`, `asyncio.Runner` or asgiref's
`async_to_sync`, or on `close_pools()`.

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
| `on_acquire(connection, profile, session)` | when a slot is assigned to a session, before its first request | `SET ROLE`, `set_config()` for row-level security |
| `on_release(connection, profile, session)` | when the session returns a usable slot to the pool | `RESET ROLE`, undoing what `on_acquire` set |

`connection` is the target's connection (for the Django ORM driver Django's connection wrapper, for asyncpg an
`asyncpg.Connection`), `session` the session that gets or returns the slot. A threaded driver runs the hooks on the
connection's thread, so they are plain functions, and so is `connect`: a coroutine function raises `TypeError` when
the profile is created, or when its pool is created for a driver given by import path. An async driver awaits the
result when it is awaitable.

`on_acquire` runs in the context of the task that acquires the slot, so it can read context variables the
application set, such as the current user. One profile, and one pool, then serves every user.

```python
def on_acquire(connection, profile, session):
    with connection.cursor() as cursor:                    # Django's connection, on the slot's thread
        cursor.execute("SELECT set_config('app.user_id', %s, false)", [str(current_user.get().pk)])


def on_release(connection, profile, session):
    with connection.cursor() as cursor:
        cursor.execute("RESET app.user_id")
```

`on_acquire` runs with the session's [connection options](/topics/options) already set. A hook may set or reset
options adorm manages, `RESET ALL` and `DISCARD ALL` included: after each hook the slot sets its options again before
the next operation. A value a hook sets holds until a layer sets that option; once that layer ends, the slot resets
the option to the database's default, not to the hook's value. The profile's `options` keep a value in every case.

An atomic operation outside a transaction runs `on_acquire`, the operation and `on_release`; a transaction runs
`on_acquire` before `BEGIN` and `on_release` after `COMMIT` or `ROLLBACK`. The next session gets the slot only after
`on_release` has finished.

What happens when a hook does not finish:

| Situation | Outcome |
|---|---|
| `on_acquire` raises | the acquisition, or the entry of `atomic()`, raises the error (mapped by the driver); the slot leaves the pool |
| `on_acquire` runs past the acquisition's timeout | it is cancelled, the slot leaves the pool, the acquisition raises `SessionTimeout` (`DeadlockError` when the flow holds another slot) |
| the acquiring task is cancelled during `on_acquire` | the hook is cancelled and the slot leaves the pool |
| `on_release` raises or runs past `release_timeout` | the error is logged, the slot leaves the pool |
| the slot leaves the pool anyway (discarded, connection unusable, transaction left open, rolled back after `commit_timeout`) | `on_release` does not run |

A threaded driver cancels a hook by asking the connection's thread to stop it; a hook that does not stop extends the
release or the acquisition by up to the driver's `cancel_grace` seconds, after which the connection is given up.

A slot that left the pool closes its connection; the next request opens a fresh one, so session state set by
`on_acquire` never reaches another session. The [multiple database users](/howto/multiple-database-users) recipe
uses `on_acquire` and `on_release` for `SET ROLE`.

## Profiles created at run time

Profiles can be registered while the application runs, for example one per user:

```python
from adorm.settings import register_profile, unregister_profile

register_profile(ConnectionProfile("user:alice", DjangoDriver, on_acquire=..., on_release=...))
...
unregister_profile("user:alice")       # its pools close
```

A **profile provider** creates profiles on demand: a callable that takes a profile name and returns a
`ConnectionProfile`, `None` when it does not provide that name, or an awaitable of either. The first session that
names an unknown profile asks the providers in the order they were added; the first profile returned is registered
and used from then on.

```python
from adorm.settings import add_profile_provider


@add_profile_provider
async def per_user(name: str) -> ConnectionProfile | None:
    if not name.startswith("user:"):
        return None
    user = name.removeprefix("user:")
    return ConnectionProfile(name, DjangoDriver, params={"role": await role_of(user)},
                             on_acquire=set_role, on_release=reset_role)


session = adorm.create_session(f"user:{request.user.username}", "web")
await session.Member.objects.count()
```

Each profile has a pool of its own, and a pool closes an idle connection after its `purge_stale_connections_at`
seconds ([slot lifecycle](/topics/pool#slot-lifecycle)). Profiles per user therefore hold connections for the users
active within that time; a single profile whose `on_acquire` reads the user from a context variable shares its
connections between users.

```python
adorm.configure(providers=[per_user], pool={"max_connections": 2, "purge_stale_connections_at": 120})
```

Concurrent first uses of one name ask the providers once. A provider that raises fails every waiting request; the
next request asks again.

When a profile is unregistered or replaced (`register_profile(profile, replace=True)`), or the default pool settings
change for a profile without its own, every router built from the settings stops routing to its pool and closes it:
idle slots close at once and held slots when their lease is released. The next session that names the profile gets
a pool built from the current profile, or from a provider when it is no longer registered; a request waiting for a
slot of the closed pool moves to that pool within its timeout, and raises `InterfaceError` when no profile of the name
remains.

## Remote execution

A profile lives where its pool runs. Its parameters, credentials and hooks are not part of a session, an operation
or anything sent with them: a session is the pair `(using, session_name)`, and `using` is only the profile name.
When the pool runs in a separate adorm server, the application's driver sends the profile name and the server's
own settings define the profile.

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

Connection profiles emit no events of their own and consume none. Callbacks and backend events a connection emits
while a hook runs follow the [pool's rules](/topics/pool#events): during `on_acquire` they go to the new lease's
`events` queue, during `on_release` to the pool's `events` bus.
