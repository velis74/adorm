# Settings

Connection profiles, pool settings, the debug switch and the plugin API for profiles created at run time, in
`adorm.settings`. `configure`, `ConnectionProfile` and `PoolSettings` are also importable from `adorm`. The
[connection profiles topic](/topics/profiles) explains how they fit together.

```python
import adorm
from adorm import ConnectionProfile, PoolSettings
from adorm.settings import (
    add_profile_provider,
    close_pools,
    default_router,
    get_profile,
    ProfileRegistry,
    profiles,
    register_profile,
    remove_profile_provider,
    unregister_profile,
)
```

The settings functions are synchronous and thread-safe; only `close_pools()` is a coroutine.

## Configuration

### `adorm.configure(*, profiles=None, providers=(), pool=None, debug=None)`

Replaces adorm's settings: the profiles of [`adorm.settings.profiles`](#adorm-settings-profiles), its providers and
default pool settings, and the executor's debug switch.

| Argument | Meaning |
|---|---|
| `profiles` | a mapping of profile names to a [`ConnectionProfile`](#adorm-connectionprofile) of that name or a mapping of its keyword arguments without `name` |
| `providers` | [profile providers](#adorm-settings-profileprovider), asked in this order |
| `pool` | the [`PoolSettings`](#adorm-poolsettings), or a mapping of its arguments, of profiles that set none; `None` for the defaults |
| `debug` | the executor's debug switch ([`set_debug()`](/ref/transactions)); `None` follows the running event loop's debug mode |

Every value is checked first; a `TypeError` or `ValueError` leaves the previous settings unchanged. Calling it
without arguments removes every profile and provider. Pools that routers built from replaced or removed profiles
close.

```python
adorm.configure(
    profiles={
        "default": {"driver": "myproject.drivers.DjangoDriver", "alias": "default"},
        "reports": ConnectionProfile("reports", AsyncpgDriver(), params={"dsn": REPORTS_DSN}),
    },
    pool={"max_connections": 20, "acquire_timeout": 10},
)
```

### `adorm.ConnectionProfile`

`ConnectionProfile(name, driver, alias="default", params={}, connect=None, on_acquire=None, on_release=None,
pool=None)`

A named connection profile, frozen. It satisfies the driver contract's
[`Profile`](/ref/drivers#adorm-drivers-profile) protocol.

| Field | Meaning |
|---|---|
| `name` | the profile name, the `using` of its sessions; a non-empty string |
| `driver` | a [`Driver`](/ref/drivers#adorm-drivers-driver) instance, a `Driver` subclass (instantiated without arguments) or the import path of either, `"package.module.Name"` or `"package.module:Name"` |
| `alias` | the target's database alias, a non-empty string |
| `params` | the driver parameters; stored as a read-only copy |
| `connect` | `None`, or `connect(profile)` returning the target's connection in place of the driver's default |
| `on_acquire` | `None`, or `on_acquire(connection, profile, session)`, run when a slot is assigned to `session`, before its first request |
| `on_release` | `None`, or `on_release(connection, profile, session)`, run when `session` returns a usable slot to the pool |
| `pool` | `None`, or the [`PoolSettings`](#adorm-poolsettings) (or a mapping of its arguments) of the profile's pool |

`connection` is the target's connection ([`DriverConnection.target`](/ref/drivers#adorm-drivers-driverconnection)).
A threaded driver runs `connect` and the hooks on the connection's thread and they are plain functions; a coroutine
function raises `TypeError` when the profile is created with a driver instance or class, and when the pool is
created for a driver given by import path. A threaded hook that does not stop when cancelled extends the release or
the acquisition by up to the driver's `cancel_grace`. An async driver awaits a hook's result when it is awaitable. A wrong
type raises `TypeError`, an empty `name` or `alias` `ValueError`. `params` and `connect` do not appear in the
representation. Profiles compare by identity and copy as themselves.

```python
def set_role(connection, profile, session):
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE " + connection.ops.quote_name(profile.params["role"]))


profile = ConnectionProfile("user:alice", "myproject.drivers.DjangoDriver", params={"role": "alice"},
                            on_acquire=set_role, on_release=reset_role)
```

#### `get_driver()`

The profile's driver instance. An import path is imported and a class instantiated on the first call; later calls
return the same instance. Raises `ValueError` for a malformed path, `ImportError` when the module or name does not
exist, and `TypeError` when the name is not a `Driver` or a `Driver` subclass.

#### `replace(**changes)`

A copy of the profile with the given fields changed.

#### `coerce(name, value)`

A class method: `value` when it is a profile named `name` (`ValueError` for another name), else
`ConnectionProfile(name=name, **value)`.

### `adorm.PoolSettings`

`PoolSettings(max_connections=0, acquire_timeout=30.0, commit_timeout=60.0, release_timeout=30.0)`

The settings of a profile's pool, frozen; see [`Pool`](/ref/pool#adorm-executor-pool-driver-profile-max-connections-0-acquire-timeout-30-0-commit-timeout-60-0-release-timeout-30-0)
for their meaning. `max_connections` is a non-negative integer; the timeouts are positive, finite numbers of seconds,
and `commit_timeout` may be `None`. Other values raise `ValueError`.

| Method | Returns |
|---|---|
| `as_kwargs()` | the settings as a dict of `Pool` keyword arguments |
| `PoolSettings.coerce(value)` | `value` when it is a `PoolSettings`, else `PoolSettings(**value)` for a mapping; `TypeError` otherwise |

```python
PoolSettings(max_connections=10, commit_timeout=None).as_kwargs()
# {'max_connections': 10, 'acquire_timeout': 30.0, 'commit_timeout': None, 'release_timeout': 30.0}
```

## Run-time profiles

### `adorm.settings.profiles`

The [`ProfileRegistry`](#adorm-settings-profileregistry-pool-none) of the settings: `configure()` fills it, the
functions below change it, and the default routers take their pools from it.

### `adorm.settings.register_profile(profile, *, replace=False)`

Registers `profile` in the settings and returns it. A registered name raises `ValueError` unless `replace`;
routers close the pool of a replaced profile.

```python
register_profile(ConnectionProfile("tenant:acme", AsyncpgDriver(), params={"dsn": acme_dsn}))
```

### `adorm.settings.unregister_profile(name)`

Removes the profile `name` from the settings and returns it, `None` when it was not registered. Routers close its
pool; a provider may provide the name again on its next use.

### `adorm.settings.get_profile(name)`

The registered profile `name`, `None` when there is none. Providers are not asked.

### `adorm.settings.ProfileProvider`

The type of a profile provider: `provider(name)` returns the `ConnectionProfile` named `name`, `None` when it does
not provide that name, or an awaitable of either.

### `adorm.settings.add_profile_provider(provider)`

Adds `provider` to the settings, after the providers added before it, and returns it, so it works as a decorator.
Anything but a callable raises `TypeError`.

```python
@add_profile_provider
def per_tenant(name):
    if name.startswith("tenant:"):
        return ConnectionProfile(name, AsyncpgDriver(), params={"dsn": dsn_of(name.removeprefix("tenant:"))})
    return None
```

### `adorm.settings.remove_profile_provider(provider)`

Removes `provider` from the settings; the profiles it provided stay registered. An unknown provider raises
`ValueError`.

### `adorm.settings.ProfileRegistry(*, pool=None)`

Connection profiles by name, with providers that create profiles at run time. Thread-safe. `pool` is the
`PoolSettings` (or a mapping of its arguments) of profiles that set none, available as the `pool` attribute.

| Member | Does |
|---|---|
| `register(profile, *, replace=False)` | registers `profile` under its name and returns it; a registered name raises `ValueError` unless `replace`; anything but a `ConnectionProfile` raises `TypeError` |
| `unregister(name)` | removes the profile `name` and returns it, `None` when it was not registered |
| `get(name)` | the registered profile `name`, `None` when there is none |
| `async resolve(name)` | the registered profile `name`, else the first profile a provider returns, registered first; `InterfaceError` when there is none, `ValueError` when a provider returns a profile of another name, `TypeError` when it returns something else |
| `add_provider(provider)`, `remove_provider(provider)` | as the settings functions above |
| `providers` | the providers, in the order they are asked |
| `clear()` | removes every profile and provider |
| `is_empty()` | whether there is neither a profile nor a provider |
| `name in registry`, `len(registry)`, `iter(registry)` | membership, count and the registered profiles |
| `pool` | the default `PoolSettings` |

A [`Router`](/ref/pool#adorm-executor-router-pools-profiles-none) built with the registry creates pools from it and
closes the pool of a profile that is unregistered or replaced by another object.

```python
registry = ProfileRegistry(pool=PoolSettings(max_connections=5))
registry.register(ConnectionProfile("default", AsyncpgDriver(), params={"dsn": DSN}))
profile = await registry.resolve("default")
```

## Default routers

### `adorm.settings.default_router(*, create=True)`

The [`Router`](/ref/pool#adorm-executor-router-pools-profiles-none) of the running event loop built from
[`profiles`](#adorm-settings-profiles), created on the first call; with `create=False`, `None` when there is none
yet. Raises `RuntimeError` outside a running event loop.
The router's pools close when the loop shuts down its asynchronous generators, as `asyncio.run()`,
`asyncio.Runner` and asgiref's `async_to_sync` do, or on `close_pools()`. Creating a router drops the routers of
loops that closed without either: their threaded connections close on their own threads, other connections are
reported in a warning on the `adorm.settings` logger.
[`get_runner()`](/ref/sessions#adorm-executor-get-runner) returns it when no runner is set and the settings have a
profile or a provider.

### `adorm.settings.close_pools()`

A coroutine: closes the pools of the running event loop's default router and drops the router; the next operation builds a new
one.

```python
await adorm.settings.close_pools()
```

## Django settings

### `ADORM_EXPOSE`

A list of `"app_label.Model.manager.method"` strings in the Django settings: methods of a manager or of its query set
class that [`adorm generate`](/howto/generating-models#managers) writes as driver methods, for code the project
cannot decorate with [`@expose`](/ref/codegen#adorm-expose-func). Only code generation reads it, through
[`exposed_methods()`](/ref/codegen#adorm-codegen-django-exposed-methods); the default is no methods. A string in
place of the list, an entry of another form, a model Django does not know, a manager the model does not have and a
method the manager does not have raise `CodegenError`. See [managers and driver methods](/topics/managers).

```python
# settings.py
ADORM_EXPOSE = [
    "auth.User.objects.create_user",
]
```
