# Settings

Connection profiles, pool settings, the debug switch and the plugin API for profiles created at run time, in
`adorm.settings`, and the Django app that configures adorm from the Django settings. `configure`, `ConnectionProfile`
and `PoolSettings` are also importable from `adorm`. The [connection profiles topic](/topics/profiles) explains how they fit together.

```python
import adorm
from adorm import ConnectionProfile, PoolSettings
from adorm.settings import (
    add_profile_provider,
    close_pools,
    default_router,
    get_profile,
    import_object,
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

Every value is checked first; a `TypeError` or `ValueError` leaves the previous settings unchanged. The profiles,
providers and pool settings then change in one step ([`ProfileRegistry.reset()`](#adorm-settings-profileregistry-pool-none)).
Calling it without arguments removes every profile and provider. A profile with the same values as the registered one
of its name keeps the registered one and its pools. Pools that routers built from replaced or removed profiles close,
and those of profiles without own pool settings when `pool` changes.

```python
adorm.configure(
    profiles={
        "default": {"driver": "adorm.drivers.django.DjangoDriver", "alias": "default"},
        "reports": ConnectionProfile("reports", AsyncpgDriver(), params={"USER": "reports", "PASSWORD": PASSWORD}),
    },
    pool={"max_connections": 20, "acquire_timeout": 10},
)
```

### `adorm.ConnectionProfile`

`ConnectionProfile(name, driver, alias="default", params={}, connect=None, on_acquire=None, on_release=None,
pool=None, options={})`

A named connection profile, frozen. It satisfies the driver contract's
[`Profile`](/ref/drivers#adorm-drivers-profile) protocol.

| Field | Meaning |
|---|---|
| `name` | the profile name, the `using` of its sessions; a non-empty string |
| `driver` | a [`Driver`](/ref/drivers#adorm-drivers-driver) instance, a `Driver` subclass (instantiated without arguments) or the import path of either, `"package.module.Name"` or `"package.module:Name"` |
| `alias` | the target's database alias, a non-empty string |
| `params` | the driver parameters; stored as a read-only copy |
| `connect` | `None`, or `connect(profile)` returning a new connection in place of the one the driver opens; each driver documents what it returns (the [Django driver](/ref/drivers#adorm-drivers-django-djangoconnection): the DB-API connection Django's wrapper uses; the [native PostgreSQL driver](/ref/drivers#adorm-drivers-asyncpg-asyncpgdriver): an `asyncpg.Connection` or an awaitable of one) |
| `on_acquire` | `None`, or `on_acquire(connection, profile, session)`, run when a slot is assigned to `session`, before its first request |
| `on_release` | `None`, or `on_release(connection, profile, session)`, run when `session` returns a usable slot to the pool |
| `pool` | `None`, or the [`PoolSettings`](#adorm-poolsettings) (or a mapping of its arguments) of the profile's pool |
| `options` | the profile's database [connection options](/ref/options), the first layer under the session's and `connection_options()` blocks; stored as a read-only copy. Values are checked as in [`check_options()`](/ref/options#adorm-executor-options-check-options-options); `acquire_timeout` and `commit_timeout` are pool settings and raise `ValueError` here. A name the driver does not declare raises `NotSupportedError`, when the profile is created with a driver instance or class, and when the pool is created for a driver given by import path |

`connection` is the target's connection ([`DriverConnection.target`](/ref/drivers#adorm-drivers-driverconnection)).
A threaded driver runs `connect` and the hooks on the connection's thread and they are plain functions; a coroutine
function raises `TypeError` when the profile is created with a driver instance or class, and when the pool is
created for a driver given by import path. A threaded hook that does not stop when cancelled extends the release or
the acquisition by up to the driver's `cancel_grace`. An async driver awaits a hook's result when it is awaitable. A wrong
type raises `TypeError`, an empty `name` or `alias` `ValueError`. `params` and `connect` do not appear in the
representation. Profiles compare by identity ([`same_as()`](#same-as-other) compares their values) and copy as
themselves.

```python
def set_role(connection, profile, session):
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE " + connection.ops.quote_name(profile.params["role"]))


profile = ConnectionProfile("user:alice", "adorm.drivers.django.DjangoDriver", params={"role": "alice"},
                            on_acquire=set_role, on_release=reset_role)
reports = ConnectionProfile("reports", AsyncpgDriver(), options={"statement_timeout": 300},
                            pool=PoolSettings(acquire_timeout=60))
```

#### `get_driver()`

The profile's driver instance. An import path is imported and a class instantiated on the first call; later calls
return the same instance. Raises `ValueError` for a malformed path, `ImportError` when the module or name does not
exist, and `TypeError` when the name is not a `Driver` or a `Driver` subclass.

#### `same_as(other)`

Whether `other` is a `ConnectionProfile` with values equal to this profile's in every field.

```python
ConnectionProfile("default", DjangoDriver).same_as(ConnectionProfile("default", DjangoDriver))   # True
```

#### `replace(**changes)`

A copy of the profile with the given fields changed.

#### `coerce(name, value)`

A class method: `value` when it is a profile named `name` (`ValueError` for another name), else
`ConnectionProfile(name=name, **value)`.

### `adorm.PoolSettings`

`PoolSettings(max_connections=0, acquire_timeout=30.0, commit_timeout=60.0, release_timeout=30.0, purge_stale_connections_at=300.0)`

The settings of a profile's pool, frozen; see [`Pool`](/ref/pool#adorm-executor-pool-driver-profile-max-connections-0-acquire-timeout-30-0-commit-timeout-60-0-release-timeout-30-0-purge-stale-connections-at-300-0)
for their meaning. `max_connections` is a non-negative integer; the timeouts and `purge_stale_connections_at` are
positive, finite numbers of seconds, and `commit_timeout` and `purge_stale_connections_at` may be `None`. Other values
raise `ValueError`.

| Method | Returns |
|---|---|
| `as_kwargs()` | the settings as a dict of `Pool` keyword arguments |
| `PoolSettings.coerce(value)` | `value` when it is a `PoolSettings`, else `PoolSettings(**value)` for a mapping; `TypeError` otherwise |

```python
PoolSettings(max_connections=10, commit_timeout=None).as_kwargs()
# {'max_connections': 10, 'acquire_timeout': 30.0, 'commit_timeout': None, 'release_timeout': 30.0,
#  'purge_stale_connections_at': 300.0}
PoolSettings(purge_stale_connections_at=None)     # idle connections stay until the pool closes
```

## Run-time profiles

### `adorm.settings.profiles`

The [`ProfileRegistry`](#adorm-settings-profileregistry-pool-none) of the settings: `configure()` fills it, the
functions below change it, and the default routers take their pools from it.

### `adorm.settings.register_profile(profile, *, replace=False)`

Registers `profile` in the settings and returns it. A registered name raises `ValueError` unless `replace`;
routers close the pool of a replaced profile.

```python
register_profile(ConnectionProfile("tenant:acme", AsyncpgDriver(), params={"NAME": "tenant_acme"}))
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
        return ConnectionProfile(name, AsyncpgDriver(), params={"NAME": "tenant_" + name.removeprefix("tenant:")})
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
| `async resolve(name)` | the registered profile `name`, else the first profile a provider returns, registered first; `InterfaceError` when there is none, `ValueError` when a provider returns a profile of another name, `TypeError` when it returns something else. When the profiles or providers change while a provider runs, its answer is discarded and `name` is resolved again from the current ones |
| `add_provider(provider)`, `remove_provider(provider)` | as the settings functions above |
| `providers` | the providers, in the order they are asked |
| `reset(profiles=(), providers=(), *, pool=None)` | replaces every profile, every provider and `pool` in one step, so no reader sees a state between the old and the new ones. A profile with the same values as the registered one of its name (`same_as()`) keeps the registered one. Routers retire the pools of profiles that are removed or replaced, and of kept profiles without own pool settings when `pool` changes. Anything but `ConnectionProfile` objects and callables raises `TypeError`, two profiles of one name or invalid pool settings `ValueError`, and then nothing changes |
| `clear()` | removes every profile and provider (`reset()` without arguments) |
| `is_empty()` | whether there is neither a profile nor a provider |
| `name in registry`, `len(registry)`, `iter(registry)` | membership, count and the registered profiles |
| `pool` | the default `PoolSettings` |

A [`Router`](/ref/pool#adorm-executor-router-pools-profiles-none) built with the registry creates pools from it and
retires the pool of a profile that is unregistered or replaced by another object, or that has no pool settings of its
own when the default ones change.

```python
registry = ProfileRegistry(pool=PoolSettings(max_connections=5))
registry.register(ConnectionProfile("default", AsyncpgDriver()))
profile = await registry.resolve("default")
```

### `adorm.settings.import_object(path, what="object")`

The object at `path`, `"package.module.Name"` or `"package.module:Name"`. `what` names the object in error messages.
A path of another form raises `ValueError`; a module that does not exist, or a module without the name, raises
`ImportError`. Driver paths of profiles and the import paths of the [`ADORM`](#adorm) setting are imported with it.

```python
hook = import_object("myapp.roles:set_role", "hook")
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

### `ADORM`

adorm's configuration in the Django settings, read by the [Django app](#django-app) on `django.setup()` and whenever a
test overrides it. A mapping with the keys of [`adorm.configure()`](#adorm-configure-profiles-none-providers-pool-none-debug-none);
a missing setting or `None` is an empty mapping.

| Key | Value |
|---|---|
| `profiles` | as in `configure()`; a profile mapping's `connect`, `on_acquire` and `on_release` may also be import paths. Missing or `None`: [`default_profiles()`](#adorm-contrib-django-default-profiles); `{}`: no profile |
| `providers` | a list of profile providers or their import paths |
| `pool` | the default `PoolSettings` or a mapping of its arguments |
| `debug` | the executor's debug switch, a bool or `None` |

Import paths have the form `"package.module.name"` or `"package.module:name"` and are imported in `ready()`, after the
models are loaded. A profile's driver given by import path stays a path in the profile; it is imported in `ready()` to
check the profile's hooks and options against the driver. Another key, a value of the wrong type, an import path
that does not import (whatever the error its module raises) and a value `configure()` refuses raise
`ImproperlyConfigured`, whose message starts with the entry, such as `ADORM['providers'][0]`.

```python
# settings.py
ADORM = {
    "profiles": {
        "default": {
            "driver": "adorm.drivers.django.DjangoDriver",
            "on_acquire": "myapp.roles.set_role",
            "on_release": "myapp.roles.reset_role",
        },
    },
    "providers": ["myapp.tenants:tenant_profile"],
    "pool": {"max_connections": 10, "acquire_timeout": 10, "commit_timeout": 30},
}
```

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

## Django app

The app `adorm.contrib.django`, in the `django` extra. Importing its modules imports Django.

```python
# settings.py
INSTALLED_APPS = [..., "adorm.contrib.django"]
```

```python
from adorm.contrib.django import configure_from_settings, DEFAULT_DRIVER, default_profiles, settings_configuration
```

### `adorm.contrib.django.apps.AdormConfig`

The app's configuration, label `adorm`; the app has no models. `ready()` calls
[`configure_from_settings()`](#adorm-contrib-django-configure-from-settings) and connects a `setting_changed` receiver
that calls it again whenever `ADORM` changes, and whenever `DATABASES` changes while `ADORM` has no `profiles`, on
entering and on leaving `override_settings`. An invalid setting
raises `ImproperlyConfigured` from `django.setup()`, or from entering the override.

### `adorm.contrib.django.configure_from_settings()`

Configures adorm from the Django settings: calls `adorm.configure()` with
[`settings_configuration()`](#adorm-contrib-django-settings-configuration), replacing adorm's whole configuration.
Raises `ImproperlyConfigured`, and then changes nothing. For a process that sets Django up without the app:

```python
django.setup()
configure_from_settings()
```

### `adorm.contrib.django.settings_configuration()`

The keyword arguments of `adorm.configure()` that [`ADORM`](#adorm) and `DATABASES` describe: `profiles` as
`ConnectionProfile` objects with their hooks imported, `providers` as callables, `pool` as `PoolSettings` or `None`,
and `debug`. A driver given by import path is imported to check the profile against it. Raises
`ImproperlyConfigured` for an invalid value.

```python
kwargs = settings_configuration()
kwargs["profiles"]["default"].on_acquire      # the function "myapp.roles.set_role" names
```

### `adorm.contrib.django.default_profiles()`

The profiles of a setting without `profiles`: one per alias of `DATABASES`, named after it, as a mapping of profile
arguments.

```python
default_profiles()
# {"default": {"driver": "adorm.drivers.django.DjangoDriver", "alias": "default"}}
```

### `adorm.contrib.django.DEFAULT_DRIVER`

`"adorm.drivers.django.DjangoDriver"`, the driver of [`default_profiles()`](#adorm-contrib-django-default-profiles).
