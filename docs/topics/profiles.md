# Connection profiles

::: info Status
The installed version provides connection profiles, their hooks, run-time profiles and the default router built
from them. Connection options in a profile and slot lifetimes (`purge_stale_connections_at`) are not part of it yet.
Neither are the Django ORM and native PostgreSQL drivers used in the examples; any `Driver` implementation (see
[Writing a driver](/howto/writing-a-driver)) works.
:::

A **connection profile** is a named entry in adorm's settings. A session's `using` is the name of a profile, as a
database alias is in Django. The profile says how its connections are made: the driver, the target's database
alias, extra driver parameters and the hooks that run around a connection's use. A call never carries connection
parameters: different database accounts are different profiles.

```python
import adorm

adorm.configure(
    profiles={
        "default": {"driver": "myproject.drivers.DjangoDriver", "alias": "default"},
        "reports": {
            "driver": "myproject.drivers.AsyncpgDriver",
            "params": {"dsn": REPORTS_DSN},
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
previous one in place.

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
| `release_timeout` | seconds the `on_release` hook may run before the slot leaves the pool |

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
| `on_acquire(connection, profile, session)` | when a slot is assigned to a session, before its first request | `SET ROLE`, `SET statement_timeout`, `set_config()` for row-level security |
| `on_release(connection, profile, session)` | when the session returns a usable slot to the pool | `RESET ROLE`, `RESET statement_timeout`, undoing what `on_acquire` set |

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
        cursor.execute("SET statement_timeout = '5s'")


def on_release(connection, profile, session):
    with connection.cursor() as cursor:
        cursor.execute("RESET statement_timeout")
```

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

Each profile has a pool of its own, and a pool keeps its idle connections until the pool closes. Profiles per user
therefore hold connections per user that was active; a single profile whose `on_acquire` reads the user from a
context variable shares its connections between users.

Concurrent first uses of one name ask the providers once. A provider that raises fails every waiting request; the
next request asks again.

When a profile is unregistered or replaced (`register_profile(profile, replace=True)`), every router built from the
settings stops routing to its pool and closes it: idle slots close at once, held slots when their lease is released,
and requests waiting for a slot of that pool raise `InterfaceError`. The next session that names the profile gets
a pool built from the current profile, or from a provider when it is no longer registered.

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
registry.register(ConnectionProfile("default", AsyncpgDriver(), params={"dsn": DSN}))
router = Router(profiles=registry)
set_runner(router)
```

## Events

Connection profiles emit no events of their own and consume none. Callbacks and backend events a connection emits
while a hook runs follow the [pool's rules](/topics/pool#events): during `on_acquire` they go to the new lease's
`events` queue, during `on_release` to the pool's `events` bus.
