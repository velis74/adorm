# Settings

Connection profiles, pool settings, the debug switch and the plugin API for profiles created at run time, in
`adorm.settings`, and the Django app that configures adorm from the Django settings. `configure`, `ConnectionProfile`
and `PoolSettings` are also importable from `adorm`. See the [connection profiles topic](/topics/profiles).

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
| `debug` | the executor's [debug switch](/ref/transactions#debug-mode), as [`set_debug()`](/ref/transactions#adorm-executor-set-debug-value) sets it; `None` follows the running event loop's debug mode |

- Every value is checked first; a `TypeError` or `ValueError` leaves the previous settings unchanged.
- Profiles, providers and pool settings change in one step
  ([`ProfileRegistry.reset()`](#adorm-settings-profileregistry-pool-none)).
- Without arguments it removes every profile and provider; without `debug` it sets the debug switch to `None`.
- A profile with the same values as the registered one of its name keeps the registered one and its pools.
- Routers close the pools of replaced or removed profiles, and, when `pool` changes, those of profiles without own
  pool settings.

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
pool=None, options={}, content_types_key=None)`

A named connection profile, frozen. It satisfies the driver contract's
[`Profile`](/ref/drivers#adorm-drivers-profile) protocol.

| Field | Meaning |
|---|---|
| `name` | the profile name, the `using` of its sessions; a non-empty string |
| `driver` | a [`Driver`](/ref/drivers#adorm-drivers-driver) instance, a `Driver` subclass (instantiated without arguments) or the import path of either, `"package.module.Name"` or `"package.module:Name"` |
| `alias` | the target's database alias, a non-empty string |
| `params` | the driver parameters; stored as a read-only copy |
| `connect` | `None`, or `connect(profile)` returning a new connection in place of the one the driver opens; each driver documents what it returns (the [Django driver](/ref/drivers#adorm-drivers-django-djangoconnection): the DB-API connection Django's wrapper uses; the [native PostgreSQL driver](/ref/drivers#adorm-drivers-asyncpg-asyncpgdriver): an `asyncpg.Connection` or an awaitable of one) |
| `on_acquire` | `None`, or `on_acquire(connection, profile, session)`, run when a slot is assigned to `session`, before its first request; it returns `None` or a mapping of database [connection options](/ref/options) for the session's lease, the layer between `options` and the session's ([hooks](/topics/profiles#hooks)) |
| `on_release` | `None`, or `on_release(connection, profile, session)`, run when `session` returns a usable slot to the pool; its return value is ignored |
| `content_types_key` | `None`, or a plain function `content_types_key(profile, session)` returning a hashable value; [content type](/ref/models) lookups of the profile share a cache only when it returns equal values. It runs in the context of the lookup, so it can read a context variable (the current tenant). An unhashable value raises `TypeError` in the lookup |
| `pool` | `None`, or the [`PoolSettings`](#adorm-poolsettings) (or a mapping of its arguments) of the profile's pool |
| `options` | the profile's database [connection options](/ref/options), the first layer under the session's and `connection_options()` blocks; stored as a read-only copy. Values are checked as in [`check_options()`](/ref/options#adorm-executor-options-check-options-options); `acquire_timeout` and `commit_timeout` are pool settings and raise `ValueError` here. A name the driver does not declare raises `NotSupportedError`, when the profile is created with a driver instance or class, and when the pool is created for a driver given by import path |

`connection` is the target's connection ([`DriverConnection.target`](/ref/drivers#adorm-drivers-driverconnection)).
SQL a hook runs on it that the driver sees ([`call_hook()`](/ref/drivers#adorm-drivers-driverconnection)) is custom
SQL: the slot leaves the pool when the lease ends.

- A threaded driver runs `connect` and the hooks on the connection's thread; they are plain functions. A coroutine
  function raises `TypeError` when the profile is created with a driver instance or class, or, for a driver given by
  import path, when the pool is created.
- A threaded hook that does not stop when cancelled extends the release or the acquisition by up to the driver's
  `cancel_grace`.
- An async driver awaits a hook's result when it is awaitable.
- A wrong type raises `TypeError`, an empty `name` or `alias` `ValueError`.
- `params` and `connect` are left out of the representation.
- Profiles compare by identity ([`same_as()`](#same-as-other) compares values) and copy as themselves.

```python
def current_role(connection, profile, session):
    user = current_user.get()
    return None if user is None else {"role": user.username}


profile = ConnectionProfile("default", "adorm.drivers.django.DjangoDriver", on_acquire=current_role)
alice = ConnectionProfile("user:alice", "adorm.drivers.django.DjangoDriver", options={"role": "alice"})
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

`PoolSettings(max_connections=0, acquire_timeout=30.0, commit_timeout=60.0, release_timeout=30.0,
purge_stale_connections_at=300.0, check_schema=False)`

The settings of a profile's pool, frozen; their meaning is under [`Pool`](/ref/pool#adorm-executor-pool-driver-profile-max-connections-0-acquire-timeout-30-0-commit-timeout-60-0-release-timeout-30-0-purge-stale-connections-at-300-0-check-schema-false).

- `max_connections`: a non-negative integer.
- The timeouts and `purge_stale_connections_at`: positive, finite numbers of seconds; `commit_timeout` and
  `purge_stale_connections_at` may be `None`.
- `check_schema`: `False`, `"warn"` or `"raise"`, the pool's [start-up schema check](/topics/schema-checks#at-pool-start-up).

Other values raise `ValueError`.

| Method | Returns |
|---|---|
| `as_kwargs()` | the settings as a dict of `Pool` keyword arguments |
| `PoolSettings.coerce(value)` | `value` when it is a `PoolSettings`, else `PoolSettings(**value)` for a mapping; `TypeError` otherwise |

```python
PoolSettings(max_connections=10, commit_timeout=None).as_kwargs()
# {'max_connections': 10, 'acquire_timeout': 30.0, 'commit_timeout': None, 'release_timeout': 30.0,
#  'purge_stale_connections_at': 300.0, 'check_schema': False}
PoolSettings(purge_stale_connections_at=None)     # idle connections stay until the pool closes
adorm.configure(profiles={...}, pool={"check_schema": "raise"})    # every profile's pool checks the schema first
```

## Run-time profiles

### `adorm.settings.profiles`

The [`ProfileRegistry`](#adorm-settings-profileregistry-pool-none) of the settings: `configure()` fills it, the
functions below change it, and the default routers take their pools from it.

### `adorm.settings.register_profile(profile, *, replace=False)`

Registers `profile` in the settings and returns it. A registered name raises `ValueError` unless `replace`.
Routers close the pool of a replaced profile.

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
| `async resolve(name)` | the registered profile `name`, else the first profile a provider returns, registered first; `InterfaceError` when there is none, `ValueError` when a provider returns a profile of another name, `TypeError` when it returns something else. When the profiles or providers change while a provider runs, its answer is discarded and `name` is resolved again |
| `add_provider(provider)`, `remove_provider(provider)` | as the settings functions above |
| `providers` | the providers, in the order they are asked |
| `reset(profiles=(), providers=(), *, pool=None)` | replaces every profile, every provider and `pool` in one step; no reader sees an intermediate state. A profile with the same values as the registered one of its name (`same_as()`) keeps the registered one. Routers retire the pools of removed or replaced profiles, and, when `pool` changes, of kept profiles without own pool settings. Anything but `ConnectionProfile` objects and callables raises `TypeError`, two profiles of one name or invalid pool settings `ValueError`; nothing changes then |
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
hook = import_object("myapp.roles:current_role", "hook")
```

## Default routers

### `adorm.settings.default_router(*, create=True)`

The [`Router`](/ref/pool#adorm-executor-router-pools-profiles-none) of the running event loop built from
[`profiles`](#adorm-settings-profiles), created on the first call. With `create=False`, `None` when none exists.
Raises `RuntimeError` outside a running event loop.

- Its pools close when the loop shuts down its asynchronous generators (as `asyncio.run()`, `asyncio.Runner` and
  asgiref's `async_to_sync` do), or on `close_pools()`.
- Creating a router drops the routers of loops that closed without either: their threaded connections close on their
  own threads; other connections are reported in a warning on the `adorm.settings` logger.
- [`get_runner()`](/ref/sessions#adorm-executor-get-runner) returns it when no runner is set and the settings have a
  profile or a provider.

### `adorm.settings.close_pools()`

A coroutine: closes the pools of the running event loop's default router and drops the router. The next operation
builds a new one.

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
| `profiles` | as in `configure()`; a profile mapping's `connect`, `on_acquire`, `on_release` and `content_types_key` may also be import paths. Missing or `None`: [`default_profiles()`](#adorm-contrib-django-default-profiles); `{}`: no profile |
| `providers` | a list of profile providers or their import paths |
| `pool` | the default `PoolSettings` or a mapping of its arguments |
| `debug` | the executor's [debug switch](/ref/transactions#debug-mode), a bool or `None`; missing: `None` |

Import paths have the form `"package.module.name"` or `"package.module:name"` and are imported in `ready()`, after the
models are loaded. A profile's driver given by import path stays a path in the profile; `ready()` imports it to check
the profile's hooks and options. `ImproperlyConfigured`, with a message starting with the entry (such as
`ADORM['providers'][0]`), is raised for another key, a value of the wrong type, an import path that does not import
(whatever error its module raises) and a value `configure()` refuses.

```python
# settings.py
ADORM = {
    "profiles": {
        "default": {
            "driver": "adorm.drivers.django.DjangoDriver",
            "on_acquire": "myapp.roles.current_role",
        },
    },
    "providers": ["myapp.tenants:tenant_profile"],
    "pool": {"max_connections": 10, "acquire_timeout": 10, "commit_timeout": 30, "check_schema": "raise"},
}
```

### `ADORM_EXPOSE`

A list of `"app_label.Model.manager.method"` strings in the Django settings: methods of a manager or of its query set
class that [`adorm generate`](/howto/generating-models#managers) writes as driver methods, for code the project
cannot decorate with [`@expose`](/ref/codegen#adorm-expose-func). Only code generation reads it, through
[`exposed_methods()`](/ref/codegen#adorm-codegen-django-exposed-methods). Default: no methods. `CodegenError` is
raised for a string in place of the list, an entry of another form, a model Django does not know, a manager the model
does not have and a method the manager does not have. See [managers and driver methods](/topics/managers).

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
from adorm.contrib.django import (
    configure_from_settings,
    DEFAULT_DRIVER,
    default_profiles,
    ensure_configured,
    settings_configuration,
)
```

### `adorm.contrib.django.apps.AdormConfig`

The app's configuration, label `adorm`; the app has no models. `ready()` calls
[`configure_from_settings()`](#adorm-contrib-django-configure-from-settings) and connects a `setting_changed` receiver
that calls it again on entering and leaving `override_settings` of `ADORM`, or of `DATABASES` while `ADORM` has no
`profiles`. An invalid setting raises `ImproperlyConfigured` from `django.setup()` or from entering the override.

### `adorm.contrib.django.configure_from_settings()`

Calls `adorm.configure()` with [`settings_configuration()`](#adorm-contrib-django-settings-configuration),
replacing adorm's whole configuration. On `ImproperlyConfigured` nothing changes. For a process that sets Django up
without the app:

```python
django.setup()
configure_from_settings()
```

### `adorm.contrib.django.ensure_configured()`

Calls [`configure_from_settings()`](#adorm-contrib-django-configure-from-settings) unless the app
`adorm.contrib.django` is installed (its `ready()` configures adorm on `django.setup()`). Requires a set-up Django.
[`adorm check-schema`](/ref/checks#adorm-check-schema) calls it.

```python
django.setup()
ensure_configured()   # adorm has the profiles of ADORM and DATABASES, with or without the app
```

### `adorm.contrib.django.settings_configuration()`

The keyword arguments of `adorm.configure()` that [`ADORM`](#adorm) and `DATABASES` describe: `profiles` as
`ConnectionProfile` objects with their hooks imported, `providers` as callables, `pool` as `PoolSettings` or `None`,
and `debug`. A driver given by import path is imported to check the profile against it. Raises
`ImproperlyConfigured` for an invalid value.

```python
kwargs = settings_configuration()
kwargs["profiles"]["default"].on_acquire      # the function "myapp.roles.current_role" names
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
