"""adorm's settings: connection profiles, pool defaults, the debug switch and providers of run-time profiles."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import threading
import weakref

from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Hashable, Iterable, Mapping
from dataclasses import asdict, dataclass, field, fields
from types import MappingProxyType
from typing import Any, TYPE_CHECKING

from adorm.drivers.base import Driver
from adorm.exceptions import InterfaceError
from adorm.executor.debug import set_debug
from adorm.executor.options import check_profile_options
from adorm.executor.pool import (
    check_hook,
    check_schema_mode,
    check_timeout,
    DEFAULT_ACQUIRE_TIMEOUT,
    DEFAULT_COMMIT_TIMEOUT,
    DEFAULT_PURGE_STALE_CONNECTIONS_AT,
    DEFAULT_RELEASE_TIMEOUT,
    Router,
    SchemaCheckMode,
)

if TYPE_CHECKING:
    from adorm.executor.sessions import Session
    from adorm.operations import Operation

__all__ = [
    "ConnectionProfile",
    "OperationCheck",
    "PoolSettings",
    "ProfileProvider",
    "ProfileRegistry",
    "add_profile_provider",
    "close_pools",
    "configure",
    "default_router",
    "get_profile",
    "import_object",
    "profiles",
    "register_profile",
    "remove_profile_provider",
    "unregister_profile",
]

Hook = Callable[[Any, "ConnectionProfile", "Session"], Any]
"""``hook(connection, profile, session)``: ``connection`` is the target's connection (`DriverConnection.target`)."""

OperationCheck = Callable[["Operation", "Session"], None]
"""``check_operation(operation, session)``: a plain function the pool calls before it runs ``operation`` for
``session``; it refuses the operation by raising."""

ProfileProvider = Callable[[str], "ConnectionProfile | None | Awaitable[ConnectionProfile | None]"]
"""``provider(name)`` returns the profile ``name``, ``None`` when it does not provide it, or an awaitable of either."""


@dataclass(frozen=True)
class PoolSettings:
    """The settings of a profile's pool; see `adorm.executor.Pool` for their meaning."""

    max_connections: int = 0
    acquire_timeout: float = DEFAULT_ACQUIRE_TIMEOUT
    commit_timeout: float | None = DEFAULT_COMMIT_TIMEOUT
    release_timeout: float = DEFAULT_RELEASE_TIMEOUT
    purge_stale_connections_at: float | None = DEFAULT_PURGE_STALE_CONNECTIONS_AT
    check_schema: SchemaCheckMode = False

    def __post_init__(self) -> None:
        value = self.max_connections
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"max_connections is a non-negative integer, not {value!r}")
        check_timeout(self.acquire_timeout, "acquire_timeout")
        if self.commit_timeout is not None:
            check_timeout(self.commit_timeout, "commit_timeout")
        check_timeout(self.release_timeout, "release_timeout")
        if self.purge_stale_connections_at is not None:
            check_timeout(self.purge_stale_connections_at, "purge_stale_connections_at")
        check_schema_mode(self.check_schema)

    @classmethod
    def coerce(cls, value: PoolSettings | Mapping[str, Any]) -> PoolSettings:
        """``value`` when it is a `PoolSettings`, else ``PoolSettings(**value)``."""
        if isinstance(value, PoolSettings):
            return value
        if isinstance(value, Mapping):
            return cls(**value)
        raise TypeError(f"pool settings are a PoolSettings or a mapping, not {type(value).__name__}")

    def as_kwargs(self) -> dict[str, Any]:
        """The settings as keyword arguments of `adorm.executor.Pool`."""
        return asdict(self)


def import_object(path: str, what: str = "object") -> Any:
    """The object at ``path``, ``"package.module.Name"`` or ``"package.module:Name"``; ``what`` names it in errors.
    Raises `ValueError` for a path of another form and `ImportError` when the module or the name does not exist."""
    module_name, sep, attribute = path.partition(":") if ":" in path else path.rpartition(".")
    if not sep or not module_name or not attribute:
        raise ValueError(f"a {what} path is 'package.module.Name' or 'package.module:Name', not {path!r}")
    module = importlib.import_module(module_name)
    try:
        return getattr(module, attribute)
    except AttributeError:
        raise ImportError(f"module {module_name!r} has no {what} {attribute!r}") from None


logger = logging.getLogger("adorm.settings")

_driver_lock = threading.Lock()


def _covered_receivers(value: Any, profile: str) -> tuple[str, ...]:
    """``value`` as a tuple of dotted paths; another value raises `TypeError`, a path without a module `ValueError`."""
    where = f"covered_receivers of profile {profile!r}"
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise TypeError(f"{where} is a list of dotted paths, not {value!r}")
    entries = tuple(value)
    for entry in entries:
        if not isinstance(entry, str):
            raise TypeError(f"{where} lists dotted paths (module.qualname), not {entry!r}")
        if "." not in entry or not all(entry.split(".")):
            raise ValueError(f"{where} lists dotted paths (module.qualname), not {entry!r}")
    return tuple(dict.fromkeys(entries))


@dataclass(frozen=True, eq=False)
class ConnectionProfile:
    """A named connection profile: what a session's ``using`` refers to.

    ``driver`` is a `Driver` instance, a `Driver` subclass (instantiated without arguments) or the import path of
    either (``"package.module.Name"`` or ``"package.module:Name"``), imported on first use. ``alias`` is the
    target's database alias and ``params`` the driver parameters. The hooks:

    - ``connect(profile)`` opens a connection in place of the one the driver opens; each driver documents what it
      returns (the Django driver: the DB-API connection Django's wrapper uses; the asyncpg driver: an
      ``asyncpg.Connection``);
    - ``on_acquire(connection, profile, session)`` runs when a slot is assigned to a session, before its first
      request, and returns ``None`` or a mapping of database connection options for the session's lease, the layer
      between ``options`` and the session's (`adorm.executor.Pool`);
    - ``on_release(connection, profile, session)`` runs when the session returns a usable slot to the pool;
    - ``content_types_key(profile, session)`` returns a hashable value that splits the profile's content type cache
      (`adorm.content_types`): lookups with equal values share a cache. It is a plain function, called in the context
      of the lookup, e.g. to read the current tenant from a context variable.
    - ``check_operation(operation, session)`` runs for every operation of the profile's sessions, synchronously in the
      context of the awaiting task, before the operation reaches a slot or a transaction; it refuses the operation by
      raising, and the awaited call raises that exception unchanged (`adorm.executor.Pool.check`).

    ``covered_receivers`` lists Django signal receivers by dotted path (``module.qualname``; a bound method by its
    class, ``module.Class.method``) whose work the application does itself: a driver that does not send Django's
    signals writes despite them (`adorm.drivers.asyncpg`). Stored as a tuple.

    ``connection`` is the target's connection. SQL a hook runs on it that the driver sees
    (`DriverConnection.call_hook()`) is custom SQL: the slot leaves the pool when the lease ends. A threaded driver runs
    the hooks on the connection's thread and they are plain functions; an async driver awaits a hook's result when it is
    awaitable. ``options`` are the database connection options of the profile, the first layer of
    `adorm.connection_options()`; ``acquire_timeout`` and ``commit_timeout`` are pool settings and raise `ValueError`
    there, and an option a driver given as an instance or class does not declare raises `NotSupportedError`. ``pool``
    overrides the registry's pool defaults.
    ``params`` and ``connect`` do not appear in the representation. A profile copies as itself.
    """

    name: str
    driver: Driver | type[Driver] | str
    alias: str = "default"
    params: Mapping[str, Any] = field(default_factory=dict, repr=False)
    connect: Callable[[Any], Any] | None = field(default=None, repr=False)
    on_acquire: Hook | None = None
    on_release: Hook | None = None
    pool: PoolSettings | None = None
    options: Mapping[str, Any] = field(default_factory=dict)
    content_types_key: Callable[[Any, Any], Hashable] | None = None
    covered_receivers: Iterable[str] = ()
    check_operation: OperationCheck | None = None

    def __post_init__(self) -> None:
        for what in ("name", "alias"):
            value = getattr(self, what)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{what} is a non-empty string, not {value!r}")
        driver = self.driver
        if (
            not (isinstance(driver, (Driver, str)) or (isinstance(driver, type) and issubclass(driver, Driver)))
            or driver == ""
        ):
            raise TypeError(f"driver is a Driver, a Driver subclass or an import path, not {driver!r}")
        if not isinstance(self.params, Mapping):
            raise TypeError(f"params is a mapping, not {type(self.params).__name__}")
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))
        # The mode of a driver given by import path is checked when its pool is created.
        mode = None if isinstance(driver, str) else getattr(driver, "mode", None)
        for what in ("connect", "on_acquire", "on_release"):
            check_hook(getattr(self, what), what, self.name, mode)
        key = self.content_types_key
        if key is not None and (not callable(key) or inspect.iscoroutinefunction(key)):
            raise TypeError(f"content_types_key of profile {self.name!r} is a plain function or None, not {key!r}")
        check = self.check_operation
        if check is not None and (not callable(check) or inspect.iscoroutinefunction(check)):
            raise TypeError(f"check_operation of profile {self.name!r} is a plain function or None, not {check!r}")
        object.__setattr__(self, "covered_receivers", _covered_receivers(self.covered_receivers, self.name))
        options = check_profile_options(self.options, self.name)
        object.__setattr__(self, "options", options)
        capabilities = None if isinstance(driver, str) else getattr(driver, "capabilities", None)
        if capabilities is not None:
            capabilities.check_options(
                [name for name, value in options.items() if value is not None], getattr(driver, "name", None)
            )
        if self.pool is not None:
            object.__setattr__(self, "pool", PoolSettings.coerce(self.pool))
        object.__setattr__(self, "_driver", driver if isinstance(driver, Driver) else None)

    def __copy__(self) -> ConnectionProfile:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> ConnectionProfile:
        return self

    @classmethod
    def coerce(cls, name: str, value: ConnectionProfile | Mapping[str, Any]) -> ConnectionProfile:
        """``value`` when it is a profile named ``name``, else ``ConnectionProfile(name=name, **value)``."""
        if isinstance(value, ConnectionProfile):
            if value.name != name:
                raise ValueError(f"profile {value.name!r} is registered under the name {name!r}")
            return value
        if isinstance(value, Mapping):
            return cls(name=name, **value)
        raise TypeError(f"a connection profile is a ConnectionProfile or a mapping, not {type(value).__name__}")

    def get_driver(self) -> Driver:
        """The profile's driver instance, imported and created on the first call. Thread-safe."""
        driver = self._driver  # type: ignore[attr-defined]
        if driver is not None:
            return driver
        with _driver_lock:
            if self._driver is None:  # type: ignore[attr-defined]
                found = import_object(self.driver, "driver") if isinstance(self.driver, str) else self.driver
                if isinstance(found, type) and issubclass(found, Driver):
                    found = found()
                if not isinstance(found, Driver):
                    raise TypeError(f"{self.driver!r} of profile {self.name!r} is not a Driver")
                object.__setattr__(self, "_driver", found)
            return self._driver  # type: ignore[attr-defined]

    def same_as(self, other: ConnectionProfile) -> bool:
        """Whether ``other`` has the same values as this profile in every field."""
        if not isinstance(other, ConnectionProfile):
            return False
        return all(getattr(self, f.name) == getattr(other, f.name) for f in fields(self))

    def replace(self, **changes: Any) -> ConnectionProfile:
        """A copy of the profile with ``changes``."""
        values = {f.name: getattr(self, f.name) for f in fields(self)}
        values.update(changes)
        return ConnectionProfile(**values)


class ProfileRegistry:
    """Connection profiles by name, with providers that create profiles at run time.

    `resolve()` returns a registered profile, or asks the providers in the order they were added and registers the
    first profile one returns. ``pool`` holds the pool settings of profiles that set none. Routers built with the
    registry retire the pool of a profile that is unregistered or replaced. Thread-safe.
    """

    def __init__(self, *, pool: PoolSettings | Mapping[str, Any] | None = None) -> None:
        self._lock = threading.RLock()
        self._profiles: dict[str, ConnectionProfile] = {}
        self._providers: list[ProfileProvider] = []
        self._routers: weakref.WeakSet[Router] = weakref.WeakSet()
        self._generation = 0
        self.pool = PoolSettings() if pool is None else PoolSettings.coerce(pool)

    def __contains__(self, name: object) -> bool:
        return name in self._profiles

    def __iter__(self) -> Any:
        return iter(list(self._profiles.values()))

    def __len__(self) -> int:
        return len(self._profiles)

    @property
    def providers(self) -> tuple[ProfileProvider, ...]:
        """The providers, in the order they are asked."""
        return tuple(self._providers)

    def is_empty(self) -> bool:
        """Whether the registry has neither profiles nor providers."""
        return not self._profiles and not self._providers

    def get(self, name: str) -> ConnectionProfile | None:
        """The registered profile ``name``, ``None`` when there is none. Providers are not asked."""
        return self._profiles.get(name)

    def register(self, profile: ConnectionProfile, *, replace: bool = False) -> ConnectionProfile:
        """Register ``profile`` under its name. A registered name raises `ValueError` unless ``replace``."""
        if not isinstance(profile, ConnectionProfile):
            raise TypeError(f"register() takes a ConnectionProfile, not {type(profile).__name__}")
        with self._lock:
            previous = self._profiles.get(profile.name)
            if previous is not None and not replace:
                raise ValueError(f"connection profile {profile.name!r} is already registered")
            self._profiles[profile.name] = profile
            self._generation += 1
        if previous is not None and previous is not profile:
            self._changed(profile.name)
        return profile

    def unregister(self, name: str) -> ConnectionProfile | None:
        """Remove the profile ``name`` and return it, ``None`` when it was not registered. A provider may provide
        the name again on its next use."""
        with self._lock:
            profile = self._profiles.pop(name, None)
            self._generation += 1
        if profile is not None:
            self._changed(name)
        return profile

    def add_provider(self, provider: ProfileProvider) -> ProfileProvider:
        """Add ``provider``; it is asked after the providers added before it."""
        if not callable(provider):
            raise TypeError(f"a profile provider is a callable, not {type(provider).__name__}")
        with self._lock:
            self._providers.append(provider)
            self._generation += 1
        return provider

    def remove_provider(self, provider: ProfileProvider) -> None:
        """Remove ``provider``; the profiles it provided stay registered. An unknown provider raises `ValueError`."""
        with self._lock:
            self._providers.remove(provider)
            self._generation += 1

    def clear(self) -> None:
        """Remove every profile and provider."""
        self.reset()

    def reset(
        self,
        profiles: Iterable[ConnectionProfile] = (),
        providers: Iterable[ProfileProvider] = (),
        *,
        pool: PoolSettings | Mapping[str, Any] | None = None,
    ) -> None:
        """Replace every profile, every provider and the default pool settings in one step: no reader sees a state
        between the old and the new ones. A profile with the same values as the registered one of its name
        (`ConnectionProfile.same_as()`) keeps the registered one. Routers retire the pools of profiles that are
        removed or replaced, and of kept profiles without own pool settings when the default pool settings change.
        Raises `TypeError` or `ValueError` (two profiles of one name) and then changes nothing."""
        built: dict[str, ConnectionProfile] = {}
        for profile in profiles:
            if not isinstance(profile, ConnectionProfile):
                raise TypeError(f"reset() takes ConnectionProfile objects, not {type(profile).__name__}")
            if profile.name in built:
                raise ValueError(f"connection profile {profile.name!r} is given twice")
            built[profile.name] = profile
        providers = list(providers)
        for provider in providers:
            if not callable(provider):
                raise TypeError(f"a profile provider is a callable, not {type(provider).__name__}")
        defaults = PoolSettings() if pool is None else PoolSettings.coerce(pool)
        with self._lock:
            previous, previous_pool = self._profiles, self.pool
            for name, profile in built.items():
                kept = previous.get(name)
                if kept is not None and kept.same_as(profile):
                    built[name] = kept
            self._profiles = built
            self._providers = providers
            self.pool = defaults
            self._generation += 1
        for name, profile in previous.items():
            if built.get(name) is not profile or (profile.pool is None and defaults != previous_pool):
                self._changed(name)

    async def resolve(self, name: str) -> ConnectionProfile:
        """The profile ``name``: the registered one, else the first one a provider returns, registered. Raises
        `InterfaceError` when there is none, and `ValueError` when a provider returns a profile of another name. When
        the profiles or providers change while a provider runs, the providers' answer is discarded and the profile
        is resolved again from the current ones."""
        while True:
            with self._lock:
                profile = self._profiles.get(name)
                if profile is not None:
                    return profile
                generation = self._generation
                providers = tuple(self._providers)
            result = await self._ask(providers, name)
            with self._lock:
                if self._generation != generation:
                    continue
                if result is None:
                    raise InterfaceError(f"no connection profile is named {name!r}")
                return self._profiles.setdefault(name, result)

    @staticmethod
    async def _ask(providers: Iterable[ProfileProvider], name: str) -> ConnectionProfile | None:
        """The first profile one of ``providers`` returns for ``name``, ``None`` when none does."""
        for provider in providers:
            result = provider(name)
            if inspect.isawaitable(result):
                result = await result
            if result is None:
                continue
            if not isinstance(result, ConnectionProfile):
                raise TypeError(f"profile provider {provider!r} returned {type(result).__name__} for {name!r}")
            if result.name != name:
                raise ValueError(f"profile provider {provider!r} returned profile {result.name!r} for {name!r}")
            return result
        return None

    def _attach(self, router: Router) -> None:
        self._routers.add(router)

    def _changed(self, name: str) -> None:
        for router in list(self._routers):
            router._profile_changed(name)


_registry = profiles = ProfileRegistry()
"""The connection profiles of `configure()`, `register_profile()` and the default routers."""

_routers: dict[asyncio.AbstractEventLoop, Router] = {}
_closers: dict[asyncio.AbstractEventLoop, AsyncGenerator[None, None]] = {}
_routers_lock = threading.Lock()


def configure(
    *,
    profiles: Mapping[str, ConnectionProfile | Mapping[str, Any]] | None = None,
    providers: Iterable[ProfileProvider] = (),
    pool: PoolSettings | Mapping[str, Any] | None = None,
    debug: bool | None = None,
) -> None:
    """Replace adorm's settings: the connection profiles by name (a `ConnectionProfile` or the keyword arguments of
    one), the profile providers, the pool settings of profiles that set none, and the executor's debug switch
    (`adorm.executor.set_debug()`). Everything is validated before anything changes, and the profiles, providers and
    pool settings change in one step (`ProfileRegistry.reset()`)."""
    registry = _registry
    if profiles is not None and not isinstance(profiles, Mapping):
        raise TypeError(f"profiles is a mapping of names to profiles, not {type(profiles).__name__}")
    built = [ConnectionProfile.coerce(name, value) for name, value in (profiles or {}).items()]
    if debug is not None and not isinstance(debug, bool):
        raise TypeError(f"debug is a bool or None, not {debug!r}")
    registry.reset(built, providers, pool=pool)
    set_debug(debug)


def register_profile(profile: ConnectionProfile, *, replace: bool = False) -> ConnectionProfile:
    """Register ``profile`` in the settings at run time (`ProfileRegistry.register()`)."""
    return _registry.register(profile, replace=replace)


def unregister_profile(name: str) -> ConnectionProfile | None:
    """Remove the profile ``name`` from the settings (`ProfileRegistry.unregister()`); default routers close its
    pool."""
    return _registry.unregister(name)


def get_profile(name: str) -> ConnectionProfile | None:
    """The registered profile ``name``, ``None`` when there is none."""
    return _registry.get(name)


def add_profile_provider(provider: ProfileProvider) -> ProfileProvider:
    """Add a provider of run-time profiles to the settings (`ProfileRegistry.add_provider()`). Usable as a
    decorator."""
    return _registry.add_provider(provider)


def remove_profile_provider(provider: ProfileProvider) -> None:
    """Remove a provider from the settings (`ProfileRegistry.remove_provider()`)."""
    _registry.remove_provider(provider)


def default_router(*, create: bool = True) -> Router | None:
    """The router of the running event loop built from the settings' profiles; created on the first call unless
    ``create`` is false. Raises `RuntimeError` outside a running event loop.

    The router's pools close when the loop shuts down its asynchronous generators (`asyncio.run()`,
    `asyncio.Runner`, asgiref's ``async_to_sync``) or on `close_pools()`. Creating a router also drops the routers
    of loops that closed without either; their threaded connections are closed on their own threads.
    """
    loop = asyncio.get_running_loop()
    router = _routers.get(loop)
    if router is None and create:
        with _routers_lock:
            router = _routers.get(loop)
            if router is None:
                _drop_closed_loops()
                router = _routers[loop] = Router(profiles=_registry)
                closer = _closers[loop] = _close_at_shutdown(loop, router)
                # Started here so that the loop tracks it and closes it at shutdown.
                try:
                    closer.asend(None).send(None)
                except StopIteration:
                    pass
    return router


async def _close_at_shutdown(loop: asyncio.AbstractEventLoop, router: Router) -> AsyncIterator[None]:
    try:
        yield
    finally:
        with _routers_lock:
            if _routers.get(loop) is router:
                del _routers[loop]
                _closers.pop(loop, None)
        await router.close()


def _drop_closed_loops() -> None:
    """Forget the routers of closed loops; close their threaded connections, report the others."""
    for loop in [loop for loop in _routers if loop.is_closed()]:
        router = _routers.pop(loop)
        _closers.pop(loop, None)
        left = 0
        for pool in router.pools:
            for slot in pool._slots:
                close = getattr(slot.connection, "_close_detached", None)
                if close is None or not close():
                    left += 1
        if left:
            logger.warning(
                "an event loop closed without closing adorm's pools; %d connection(s) of its default router are "
                "left open",
                left,
            )


async def close_pools() -> None:
    """Close the pools of the running event loop's default router and drop the router; the next operation builds a
    new one."""
    loop = asyncio.get_running_loop()
    with _routers_lock:
        _routers.pop(loop, None)
        closer = _closers.pop(loop, None)
    if closer is not None:
        await closer.aclose()


def _default_runner() -> Router | None:
    """The default router of the running event loop when the settings have a profile or a provider."""
    if _registry.is_empty():
        return None
    try:
        return default_router()
    except RuntimeError:
        return None
