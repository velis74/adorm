"""Connection profiles: settings, the profile registry and its providers, profile-backed routers and the
``on_acquire`` / ``on_release`` hooks."""

import asyncio
import contextvars
import logging
import pickle
import sys
import threading

import pytest

import adorm
import adorm.settings

from adorm import (
    ConnectionProfile,
    create_session,
    DatabaseError,
    DeadlockError,
    InterfaceError,
    PoolSettings,
    SessionTimeout,
    TransactionClosed,
)
from adorm.drivers import Capabilities, ThreadedConnection, ThreadedDriver
from adorm.executor import debug_enabled, get_runner, Pool, Router, set_debug, set_runner
from adorm.settings import ProfileRegistry
from tests.executor_fakes import FakeConnection, FakeDriver, Op

REPEAT = range(20)
LONG = 30
GUARD = 5

request_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("test_profiles_request", default=None)


async def until(predicate):
    """Yield to the loop until ``predicate()`` holds."""
    async with asyncio.timeout(GUARD):
        while not predicate():
            await asyncio.sleep(0)


@pytest.fixture(autouse=True)
async def clean_settings():
    adorm.configure()
    previous = set_runner(None)
    yield
    await adorm.settings.close_pools()
    adorm.configure()
    set_runner(previous)


class Hooks:
    """Hooks that record their calls on the fake connection; a gate holds a hook, an error makes it raise."""

    def __init__(self):
        self.log: list[tuple[str, int, str]] = []
        self.gates: dict[str, asyncio.Event] = {}
        self.errors: dict[str, BaseException] = {}
        self.entered: dict[str, asyncio.Event] = {"on_acquire": asyncio.Event(), "on_release": asyncio.Event()}
        self.cancelled: list[str] = []
        self.contexts: list[str | None] = []
        self.once: set[str] = set()  # hooks whose error is raised only by their next call

    async def _run(self, what, connection, session):
        self.log.append((what, connection.number, session.session_name))
        self.contexts.append(request_var.get())
        connection.calls.append((what, session.session_name))
        self.entered[what].set()
        try:
            gate = self.gates.get(what)
            if gate is not None:
                await gate.wait()
        except asyncio.CancelledError:
            self.cancelled.append(what)
            raise
        error = self.errors.pop(what, None) if what in self.once else self.errors.get(what)
        if error is not None:
            raise error

    async def on_acquire(self, connection, profile, session):
        assert profile.name == session.using
        await self._run("on_acquire", connection, session)

    async def on_release(self, connection, profile, session):
        assert profile.name == session.using
        await self._run("on_release", connection, session)


def profile(name="default", *, driver=None, hooks=None, **kwargs):
    if hooks is not None:
        kwargs.setdefault("on_acquire", hooks.on_acquire)
        kwargs.setdefault("on_release", hooks.on_release)
    return ConnectionProfile(name, FakeDriver() if driver is None else driver, **kwargs)


def routed(prof, **settings):
    registry = ProfileRegistry(pool=PoolSettings(acquire_timeout=LONG, **settings))
    registry.register(prof)
    return Router(profiles=registry), registry


def session(name="s", using="default"):
    return create_session(using, name)


# ConnectionProfile


def test_profile_defaults_and_immutability():
    driver = FakeDriver()
    prof = ConnectionProfile("default", driver)
    assert (prof.alias, dict(prof.params), prof.connect, prof.on_acquire, prof.on_release, prof.pool) == (
        "default",
        {},
        None,
        None,
        None,
        None,
    )
    assert prof.get_driver() is driver
    with pytest.raises(AttributeError):
        prof.name = "other"


def test_profile_params_are_copied_and_read_only():
    params = {"dsn": "postgres://db/app"}
    prof = profile(params=params)
    params["dsn"] = "changed"
    assert prof.params["dsn"] == "postgres://db/app"
    with pytest.raises(TypeError):
        prof.params["dsn"] = "x"


def test_profile_representation_hides_params_and_connect():
    prof = profile(params={"PASSWORD": "s3cret"}, connect=lambda p: p)
    assert "s3cret" not in repr(prof)
    assert "connect" not in repr(prof)
    assert "'default'" in repr(prof)


@pytest.mark.parametrize(
    ("kwargs", "error", "match"),
    [
        ({"name": ""}, ValueError, "name"),
        ({"name": 1}, ValueError, "name"),
        ({"alias": ""}, ValueError, "alias"),
        ({"driver": 42}, TypeError, "driver"),
        ({"driver": ""}, TypeError, "driver"),
        ({"driver": object}, TypeError, "driver"),
        ({"params": [("a", 1)]}, TypeError, "params"),
        ({"connect": "connect"}, TypeError, "connect"),
        ({"on_acquire": 1}, TypeError, "on_acquire"),
        ({"on_release": "x"}, TypeError, "on_release"),
        ({"pool": 3}, TypeError, "pool settings"),
    ],
)
def test_profile_validation(kwargs, error, match):
    values = {"name": "default", "driver": FakeDriver(), **kwargs}
    with pytest.raises(error, match=match):
        ConnectionProfile(**values)


def test_profile_pool_settings_from_a_mapping():
    prof = profile(pool={"max_connections": 3})
    assert prof.pool == PoolSettings(max_connections=3)


class CountingDriver(FakeDriver):
    created = 0

    def __init__(self):
        super().__init__()
        type(self).created += 1


driver_instance = FakeDriver()


@pytest.mark.parametrize(
    "path", ["tests.test_profiles.CountingDriver", "tests.test_profiles:CountingDriver", CountingDriver]
)
def test_profile_driver_by_class_or_import_path_is_created_once(path):
    prof = ConnectionProfile("default", path)
    first = prof.get_driver()
    assert isinstance(first, CountingDriver)
    assert prof.get_driver() is first


def test_profile_driver_path_may_name_an_instance():
    assert ConnectionProfile("default", "tests.test_profiles.driver_instance").get_driver() is driver_instance


@pytest.mark.parametrize(
    ("path", "error"),
    [
        ("CountingDriver", ValueError),
        ("tests.test_profiles.Missing", ImportError),
        ("tests.no_such_module.Driver", ImportError),
        ("tests.test_profiles.REPEAT", TypeError),
        ("tests.test_profiles:", ValueError),
    ],
)
def test_profile_driver_path_errors(path, error):
    prof = ConnectionProfile("default", path)
    with pytest.raises(error):
        prof.get_driver()


@pytest.mark.parametrize("attempt", REPEAT)
def test_profile_driver_is_created_once_across_threads(attempt):  # noqa: ARG001
    class Slow(FakeDriver):
        made = 0
        barrier = threading.Barrier(8)

        def __init__(self):
            super().__init__()
            type(self).made += 1

    prof = ConnectionProfile("default", Slow)
    results = []

    def worker():
        Slow.barrier.wait()
        results.append(prof.get_driver())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert Slow.made == 1
    assert len({id(r) for r in results}) == 1


def test_profile_replace():
    prof = profile(params={"a": 1})
    other = prof.replace(name="reports", params={"a": 2})
    assert (other.name, dict(other.params), other.driver) == ("reports", {"a": 2}, prof.driver)
    assert prof.name == "default"


def test_profile_coerce():
    driver = FakeDriver()
    prof = ConnectionProfile.coerce("reports", {"driver": driver, "alias": "replica"})
    assert (prof.name, prof.alias) == ("reports", "replica")
    assert ConnectionProfile.coerce("reports", prof) is prof
    with pytest.raises(ValueError, match="registered under the name"):
        ConnectionProfile.coerce("other", prof)
    with pytest.raises(TypeError):
        ConnectionProfile.coerce("reports", 1)


def test_a_session_carries_only_the_profile_name():
    secret = "s3cret-password"  # noqa: S105
    adorm.settings.register_profile(profile("tenant", params={"PASSWORD": secret}))
    data = pickle.dumps(create_session("tenant", "job"))
    assert secret.encode() not in data
    assert pickle.loads(data) is create_session("tenant", "job")


# PoolSettings


def test_pool_settings_defaults_and_kwargs():
    settings = PoolSettings()
    assert settings.as_kwargs() == {
        "max_connections": 0,
        "acquire_timeout": 30.0,
        "commit_timeout": 60.0,
        "release_timeout": 30.0,
        "purge_stale_connections_at": 300.0,
        "check_schema": False,
    }
    assert PoolSettings(commit_timeout=None).commit_timeout is None
    assert PoolSettings(purge_stale_connections_at=None).purge_stale_connections_at is None


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"max_connections": -1}, "max_connections"),
        ({"max_connections": True}, "max_connections"),
        ({"acquire_timeout": 0}, "acquire_timeout"),
        ({"commit_timeout": float("inf")}, "commit_timeout"),
        ({"release_timeout": -1}, "release_timeout"),
        ({"purge_stale_connections_at": 0}, "purge_stale_connections_at"),
        ({"purge_stale_connections_at": float("nan")}, "purge_stale_connections_at"),
    ],
)
def test_pool_settings_validation(kwargs, match):
    with pytest.raises(ValueError, match=match):
        PoolSettings(**kwargs)


def test_pool_release_timeout_is_validated():
    with pytest.raises(ValueError, match="release_timeout"):
        Pool(FakeDriver(), profile(), release_timeout=0)


@pytest.mark.parametrize("value", [0, -5, float("inf"), "300"])
def test_pool_purge_stale_connections_at_is_validated(value):
    with pytest.raises(ValueError, match="purge_stale_connections_at"):
        Pool(FakeDriver(), profile(), purge_stale_connections_at=value)


# ProfileRegistry


def test_registry_register_get_and_unregister():
    registry = ProfileRegistry()
    assert registry.is_empty()
    prof = registry.register(profile())
    assert registry.get("default") is prof
    assert "default" in registry
    assert len(registry) == 1
    assert list(registry) == [prof]
    with pytest.raises(ValueError, match="already registered"):
        registry.register(profile())
    other = registry.register(profile(), replace=True)
    assert registry.get("default") is other
    assert registry.unregister("default") is other
    assert registry.unregister("default") is None
    assert registry.get("default") is None
    with pytest.raises(TypeError):
        registry.register({"name": "x"})


async def test_registry_resolve_asks_providers_in_order_and_registers_the_result():
    registry = ProfileRegistry()
    asked = []

    def none(name):
        asked.append(("none", name))

    async def tenants(name):
        asked.append(("tenants", name))
        return profile(name) if name.startswith("tenant:") else None

    def never(name):
        asked.append(("never", name))
        return profile(name)

    for provider in (none, tenants):
        registry.add_provider(provider)
    registry.add_provider(never)
    assert registry.providers == (none, tenants, never)

    prof = await registry.resolve("tenant:7")
    assert prof.name == "tenant:7"
    assert registry.get("tenant:7") is prof
    assert asked == [("none", "tenant:7"), ("tenants", "tenant:7")]
    asked.clear()
    assert await registry.resolve("tenant:7") is prof
    assert asked == []

    registry.remove_provider(never)
    with pytest.raises(InterfaceError, match="'other'"):
        await registry.resolve("other")
    with pytest.raises(ValueError, match="not in list"):
        registry.remove_provider(never)


async def test_registry_resolve_rejects_a_wrong_profile():
    registry = ProfileRegistry()
    registry.add_provider(lambda name: profile("x"))  # noqa: ARG005
    with pytest.raises(ValueError, match="returned profile 'x' for 'y'"):
        await registry.resolve("y")
    registry.clear()
    registry.add_provider(lambda name: {"driver": FakeDriver()})  # noqa: ARG005
    with pytest.raises(TypeError, match="returned dict"):
        await registry.resolve("y")
    with pytest.raises(TypeError):
        registry.add_provider("provider")


async def test_registry_a_registered_profile_wins_over_a_concurrent_provider_result():
    registry = ProfileRegistry()
    gate = asyncio.Event()

    async def slow(name):
        await gate.wait()
        return profile(name)

    registry.add_provider(slow)
    task = asyncio.create_task(registry.resolve("p"))
    await asyncio.sleep(0)
    registered = registry.register(profile("p"))
    gate.set()
    assert await task is registered


def test_registry_clear():
    registry = ProfileRegistry()
    registry.register(profile())
    registry.add_provider(lambda name: None)  # noqa: ARG005
    registry.clear()
    assert registry.is_empty()


# configure()


def test_configure_registers_profiles_and_settings():
    driver = FakeDriver()
    adorm.configure(
        profiles={
            "default": {"driver": driver, "alias": "default"},
            "reports": ConnectionProfile("reports", driver, pool=PoolSettings(max_connections=2)),
        },
        providers=[lambda name: None],  # noqa: ARG005
        pool={"acquire_timeout": 5},
        debug=True,
    )
    try:
        registry = adorm.settings.profiles
        assert sorted(p.name for p in registry) == ["default", "reports"]
        assert adorm.settings.get_profile("default").get_driver() is driver
        assert registry.pool == PoolSettings(acquire_timeout=5)
        assert len(registry.providers) == 1
        assert debug_enabled() is True
    finally:
        set_debug(None)


def test_configure_replaces_the_previous_settings():
    adorm.configure(profiles={"a": {"driver": FakeDriver()}}, providers=[lambda name: None])  # noqa: ARG005
    adorm.configure(profiles={"b": {"driver": FakeDriver()}})
    assert [p.name for p in adorm.settings.profiles] == ["b"]
    assert adorm.settings.profiles.providers == ()
    assert adorm.settings.profiles.pool == PoolSettings()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"profiles": {"ok": {"driver": FakeDriver()}, "bad": {"driver": 1}}},
        {"profiles": {"ok": {"driver": FakeDriver()}, "bad": ConnectionProfile("other", FakeDriver())}},
        {"profiles": [ConnectionProfile("ok", FakeDriver())]},
        {"profiles": {"ok": {"driver": FakeDriver()}}, "providers": ["provider"]},
        {"profiles": {"ok": {"driver": FakeDriver()}}, "pool": {"max_connections": -1}},
        {"profiles": {"ok": {"driver": FakeDriver()}}, "debug": "yes"},
    ],
)
def test_configure_changes_nothing_when_a_value_is_invalid(kwargs):
    adorm.configure(profiles={"kept": {"driver": FakeDriver()}})
    with pytest.raises((TypeError, ValueError)):
        adorm.configure(**kwargs)
    assert [p.name for p in adorm.settings.profiles] == ["kept"]


# Replacing the configuration in one step (ProfileRegistry.reset(), configure())


def test_routers_are_told_about_a_change_only_once_the_new_profile_is_in_place():
    seen = []

    class Observer:
        def _profile_changed(self, name):
            seen.append((name, adorm.settings.get_profile(name)))

    observer = Observer()
    adorm.configure(profiles={"default": {"driver": FakeDriver()}, "gone": {"driver": FakeDriver()}})
    adorm.settings.profiles._attach(observer)
    adorm.configure(profiles={"default": {"driver": FakeDriver()}})
    current = adorm.settings.get_profile("default")
    assert sorted(seen, key=lambda item: item[0]) == [("default", current), ("gone", None)]


def test_reset_validates_before_it_changes_anything():
    registry = ProfileRegistry()
    kept = registry.register(ConnectionProfile("kept", FakeDriver()))
    for args, kwargs in [
        (([ConnectionProfile("a", FakeDriver()), ConnectionProfile("a", FakeDriver())],), {}),
        ((["a"],), {}),
        (((), [1]), {}),
        ((), {"pool": {"max_connections": -1}}),
    ]:
        with pytest.raises((TypeError, ValueError)):
            registry.reset(*args, **kwargs)
        assert list(registry) == [kept]
    registry.reset([kept], pool={"acquire_timeout": 1})
    assert list(registry) == [kept]
    assert registry.pool == PoolSettings(acquire_timeout=1)


@pytest.mark.parametrize("attempt", range(3))
def test_concurrent_configuration_never_hides_a_profile_or_fails(attempt):  # noqa: ARG001
    """Threads reconfiguring adorm while others read it: every reader finds the profile, no configure() fails."""
    drivers = [FakeDriver() for _ in range(4)]
    adorm.configure(profiles={"default": {"driver": drivers[0]}})
    stop = threading.Event()
    errors: list[BaseException] = []
    missing = []

    def configure(driver):
        try:
            for _ in range(300):
                adorm.configure(profiles={"default": {"driver": driver}}, pool={"acquire_timeout": 1})
        except BaseException as exc:
            errors.append(exc)

    def read():
        while not stop.is_set():
            if adorm.settings.get_profile("default") is None:
                missing.append(True)

    def resolve():
        async def main():
            while not stop.is_set():
                try:
                    await adorm.settings.profiles.resolve("default")
                except BaseException as exc:
                    errors.append(exc)
                    return

        asyncio.run(main())

    switch = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        readers = [threading.Thread(target=read), threading.Thread(target=resolve)]
        writers = [threading.Thread(target=configure, args=(driver,)) for driver in drivers]
        for thread in readers + writers:
            thread.start()
        for thread in writers:
            thread.join(GUARD * 6)
        stop.set()
        for thread in readers:
            thread.join(GUARD)
    finally:
        sys.setswitchinterval(switch)
    assert errors == []
    assert missing == []
    assert len(adorm.settings.profiles) == 1


def test_a_profile_with_the_same_values_keeps_the_registered_one():
    registry = ProfileRegistry()
    seen = []

    class Observer:
        def _profile_changed(self, name):
            seen.append(name)

    observer = Observer()
    registry._attach(observer)
    driver = FakeDriver()
    first = ConnectionProfile("default", driver, params={"a": 1}, pool=PoolSettings(max_connections=2))
    registry.reset([first])
    registry.reset([ConnectionProfile("default", driver, params={"a": 1}, pool={"max_connections": 2})])
    assert registry.get("default") is first
    assert seen == []
    registry.reset([ConnectionProfile("default", driver, params={"a": 2})])
    assert registry.get("default") is not first
    assert seen == ["default"]
    assert first.same_as(first.replace())
    assert not first.same_as(first.replace(alias="other"))
    assert not first.same_as("default")


def test_a_change_of_the_default_pool_settings_retires_pools_of_kept_profiles_without_their_own():
    registry = ProfileRegistry()
    seen = []

    class Observer:
        def _profile_changed(self, name):
            seen.append(name)

    observer = Observer()
    registry._attach(observer)
    plain = ConnectionProfile("plain", FakeDriver())
    own = ConnectionProfile("own", FakeDriver(), pool=PoolSettings(max_connections=1))
    registry.reset([plain, own])
    registry.reset([plain, own])
    assert seen == []
    registry.reset([plain, own], pool=PoolSettings(max_connections=2))
    assert seen == ["plain"]


async def test_a_pool_retired_for_new_default_settings_is_rebuilt_with_them():
    prof = ConnectionProfile("default", FakeDriver())
    adorm.configure(profiles={"default": prof}, pool={"max_connections": 1})
    assert await create_session().execute(Op("first")) == "first"
    pool = adorm.settings.default_router().pool("default")
    assert pool.max_connections == 1
    adorm.configure(profiles={"default": prof}, pool={"max_connections": 3})
    await until(lambda: pool.closed)
    assert await create_session().execute(Op("second")) == "second"
    assert adorm.settings.default_router().pool("default").max_connections == 3


@pytest.mark.parametrize("attempt", REPEAT)
async def test_a_waiter_keeps_its_pool_across_a_configuration_with_the_same_values(attempt):  # noqa: ARG001
    driver = FakeDriver()
    adorm.configure(profiles={"default": {"driver": driver}}, pool={"max_connections": 1, "acquire_timeout": GUARD})
    holder = create_session("default", "holder")
    async with holder.atomic():
        await holder.execute(Op("held"))
        waiter = asyncio.create_task(create_session("default", "waiter").execute(Op("waited")))
        pool = adorm.settings.default_router().pool("default")
        await until(lambda: pool.waiting == 1)
        adorm.configure(profiles={"default": {"driver": driver}}, pool={"max_connections": 1, "acquire_timeout": GUARD})
        await asyncio.sleep(0)
        assert not pool.retired
    assert await waiter == "waited"
    assert adorm.settings.default_router().pool("default") is pool
    assert len(driver.connections) == 1


@pytest.mark.parametrize("through", ["run", "atomic"])
@pytest.mark.parametrize("attempt", REPEAT)
async def test_a_waiter_moves_to_the_new_pool_when_its_profile_changes(through, attempt):  # noqa: ARG001
    old, new = FakeDriver(), FakeDriver()
    settings = {"max_connections": 1, "acquire_timeout": GUARD}
    adorm.configure(profiles={"default": {"driver": old}}, pool=settings)

    async def wait():
        session = create_session("default", "waiter")
        if through == "run":
            return await session.execute(Op("waited"))
        async with session.atomic():
            return await session.execute(Op("waited"))

    holder = create_session("default", "holder")
    async with holder.atomic():
        await holder.execute(Op("held"))
        waiter = asyncio.create_task(wait())
        pool = adorm.settings.default_router().pool("default")
        await until(lambda: pool.waiting == 1)
        adorm.configure(profiles={"default": {"driver": new}}, pool=settings)
        assert await waiter == "waited"
        assert pool.retired
        # the holder's transaction is still open on the old slot
        assert await holder.execute(Op("still")) == "still"
    assert ["execute", "waited"] in [list(call[:2]) for call in new.connections[0].calls]
    assert len(old.connections) == 1
    await until(lambda: old.connections[0].closed)
    assert adorm.settings.default_router().pool("default").waiting == 0


@pytest.mark.parametrize("attempt", REPEAT)
async def test_a_waiter_on_a_removed_profile_raises_interface_error(attempt):  # noqa: ARG001
    adorm.configure(
        profiles={"default": {"driver": FakeDriver()}}, pool={"max_connections": 1, "acquire_timeout": GUARD}
    )
    holder = create_session("default", "holder")
    async with holder.atomic():
        await holder.execute(Op("held"))
        waiter = asyncio.create_task(create_session("default", "waiter").execute(Op("waited")))
        pool = adorm.settings.default_router().pool("default")
        await until(lambda: pool.waiting == 1)
        adorm.configure()
        with pytest.raises(InterfaceError, match="no connection profile is named 'default'"):
            await waiter
        assert await holder.execute(Op("still")) == "still"


@pytest.mark.parametrize("replacement", [None, "new"], ids=["removed", "redefined"])
async def test_a_provider_answer_from_a_replaced_configuration_is_discarded(replacement):
    release = asyncio.Event()
    asked = asyncio.Event()
    registry = ProfileRegistry()

    async def stale(name):
        asked.set()
        await release.wait()
        return ConnectionProfile(name, FakeDriver())

    registry.reset([], [stale])
    task = asyncio.create_task(registry.resolve("reports"))
    await asked.wait()
    current = ConnectionProfile("reports", FakeDriver())
    registry.reset([current] if replacement else [])
    release.set()
    if replacement:
        assert await task is current
    else:
        with pytest.raises(InterfaceError, match="no connection profile is named 'reports'"):
            await task
        assert registry.get("reports") is None
    assert registry.get("reports") is (current if replacement else None)


def test_settings_functions_use_the_global_registry():
    prof = adorm.settings.register_profile(profile("x"))
    assert adorm.settings.get_profile("x") is prof

    @adorm.settings.add_profile_provider
    def provider(name):  # noqa: ARG001
        return None

    assert adorm.settings.profiles.providers == (provider,)
    adorm.settings.remove_profile_provider(provider)
    assert adorm.settings.unregister_profile("x") is prof
    assert adorm.settings.profiles.is_empty()


# Routers built from profiles


async def test_router_creates_a_pool_from_the_profile_on_first_use():
    driver = FakeDriver()
    prof = profile(driver=driver, alias="replica", pool=PoolSettings(max_connections=2, acquire_timeout=4))
    router, _registry = routed(prof)
    assert router.pools == ()
    with pytest.raises(InterfaceError):
        router.pool("default")
    assert await router.run(session(), Op("q")) == "q"
    pool = router.pool("default")
    assert (pool.profile, pool.driver, pool.max_connections, pool.acquire_timeout) == (prof, driver, 2, 4.0)
    assert await router.get_pool("default") is pool


async def test_router_uses_the_registry_pool_settings_when_the_profile_has_none():
    router, _ = routed(
        profile(), max_connections=3, commit_timeout=None, release_timeout=7, purge_stale_connections_at=None
    )
    pool = await router.get_pool("default")
    assert (
        pool.max_connections,
        pool.acquire_timeout,
        pool.commit_timeout,
        pool.release_timeout,
        pool.purge_stale_connections_at,
    ) == (3, LONG, None, 7.0, None)


async def test_an_added_pool_wins_over_a_profile_of_the_same_name():
    router, _ = routed(profile())
    pool = router.add(Pool(FakeDriver(), profile()))
    assert await router.get_pool("default") is pool


async def test_unknown_profile_raises_interface_error():
    router, _ = routed(profile())
    with pytest.raises(InterfaceError, match="'missing'"):
        await router.run(session(using="missing"), Op())
    with pytest.raises(InterfaceError, match="no pool"):
        await Router().get_pool("missing")


@pytest.mark.parametrize("attempt", REPEAT)
async def test_concurrent_first_uses_create_one_pool_and_ask_the_provider_once(attempt):  # noqa: ARG001
    registry = ProfileRegistry(pool={"acquire_timeout": LONG})
    gate = asyncio.Event()
    calls = []

    async def provider(name):
        calls.append(name)
        await gate.wait()
        return profile(name)

    registry.add_provider(provider)
    router = Router(profiles=registry)
    tasks = [asyncio.create_task(router.run(session(f"s{i}", "tenant"), Op(f"q{i}"))) for i in range(5)]
    await until(lambda: calls)
    gate.set()
    assert await asyncio.gather(*tasks) == [f"q{i}" for i in range(5)]
    assert calls == ["tenant"]
    assert len(router.pools) == 1
    await router.close()


async def test_a_cancelled_pool_creator_leaves_the_creation_to_a_waiter():
    registry = ProfileRegistry(pool={"acquire_timeout": LONG})
    first_gate, calls = asyncio.Event(), []

    async def provider(name):
        calls.append(name)
        if len(calls) == 1:
            await first_gate.wait()  # never set: the first creator is cancelled here
        return profile(name)

    registry.add_provider(provider)
    router = Router(profiles=registry)
    creator = asyncio.create_task(router.get_pool("p"))
    await until(lambda: calls)
    waiter = asyncio.create_task(router.get_pool("p"))
    await asyncio.sleep(0)
    creator.cancel()
    with pytest.raises(asyncio.CancelledError):
        await creator
    pool = await asyncio.wait_for(waiter, GUARD)
    assert pool.name == "p"
    assert calls == ["p", "p"]


async def test_a_cancelled_waiter_does_not_disturb_the_creator():
    registry = ProfileRegistry()
    gate = asyncio.Event()

    async def provider(name):
        await gate.wait()
        return profile(name)

    registry.add_provider(provider)
    router = Router(profiles=registry)
    creator = asyncio.create_task(router.get_pool("p"))
    await asyncio.sleep(0)
    waiter = asyncio.create_task(router.get_pool("p"))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    gate.set()
    assert (await creator).name == "p"


async def test_a_provider_error_reaches_every_waiter_and_the_next_use_asks_again():
    registry = ProfileRegistry()
    gate, calls = asyncio.Event(), []

    async def provider(name):
        calls.append(name)
        await gate.wait()
        if len(calls) == 1:
            raise RuntimeError("vault unavailable")
        return profile(name)

    registry.add_provider(provider)
    router = Router(profiles=registry)
    tasks = [asyncio.create_task(router.get_pool("p")) for _ in range(3)]
    await until(lambda: calls)
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(r, RuntimeError) for r in results)
    assert (await router.get_pool("p")).name == "p"
    assert calls == ["p", "p"]


async def test_a_driver_that_cannot_be_created_raises_and_routes_nothing():
    router, _ = routed(ConnectionProfile("default", "tests.no_such_module.Driver"))
    with pytest.raises(ImportError):
        await router.run(session(), Op())
    assert router.pools == ()


async def test_unregistering_a_profile_retires_and_closes_its_pool():
    first = FakeDriver()
    router, registry = routed(profile(driver=first))
    held = await router.acquire(session("held"))
    idle_lease = await router.acquire(session("idle"))
    await idle_lease.release()
    old = router.pool("default")

    registry.unregister("default")
    assert router.pools == ()
    # the idle slot closes; the held one keeps serving its lease and closes on release
    await until(lambda: first.connections[1].closed)
    assert old.closed
    assert not first.connections[0].closed
    assert await held.call("execute", Op("still")) == "still"
    await held.release()
    assert first.connections[0].closed

    second = FakeDriver()
    registry.register(profile(driver=second))
    assert await router.run(session(), Op("new")) == "new"
    assert router.pool("default").driver is second
    assert len(second.connections) == 1


async def test_replacing_a_profile_retires_the_old_pool():
    router, registry = routed(profile())
    await router.run(session(), Op())
    old = router.pool("default")
    replacement = registry.register(profile(alias="replica"), replace=True)
    await until(lambda: old.closed)
    assert (await router.get_pool("default")).profile is replacement


async def test_registering_the_same_profile_again_keeps_the_pool():
    router, registry = routed(profile())
    prof = registry.get("default")
    pool = await router.get_pool("default")
    registry.register(prof, replace=True)
    await asyncio.sleep(0)
    assert router.pool("default") is pool
    assert not pool.closed


async def test_waiters_of_a_retired_pool_raise_interface_error_when_no_profile_remains():
    router, registry = routed(profile(pool=PoolSettings(max_connections=1, acquire_timeout=LONG)))
    held = await router.acquire(session("held"))
    waiter = asyncio.create_task(router.acquire(session("waiter")))
    await until(lambda: router.pool("default").waiting == 1)
    registry.unregister("default")
    with pytest.raises(InterfaceError, match="no connection profile is named 'default'"):
        await asyncio.wait_for(waiter, GUARD)
    await held.release()


async def test_waiters_of_a_retired_pool_acquire_from_the_replacement():
    router, registry = routed(profile(pool=PoolSettings(max_connections=1, acquire_timeout=LONG)))
    held = await router.acquire(session("held"))
    old = router.pool("default")
    waiter = asyncio.create_task(router.acquire(session("waiter")))
    await until(lambda: old.waiting == 1)
    replacement = registry.register(profile(alias="replica"), replace=True)
    lease = await asyncio.wait_for(waiter, GUARD)
    assert lease.pool is not old
    assert lease.pool.profile is replacement
    await lease.release()
    await held.release()
    await until(lambda: old.closed)


async def test_unregistering_from_another_thread_retires_on_the_router_loop():
    router, registry = routed(profile())
    pool = await router.get_pool("default")
    thread = threading.Thread(target=registry.unregister, args=("default",))
    thread.start()
    await asyncio.to_thread(thread.join)
    await until(lambda: pool.closed)
    assert router.pools == ()


async def test_a_profile_unregistered_while_its_pool_is_created_does_not_stay_routed():
    registry = ProfileRegistry()

    class Unregistering(FakeDriver):
        def __init__(self):
            super().__init__()
            registry.unregister("default")

    registry.register(ConnectionProfile("default", Unregistering))
    router = Router(profiles=registry)
    pool = await router.get_pool("default")
    assert router.pools == ()
    await until(lambda: pool.closed)


async def test_profile_changes_leave_added_pools_alone():
    router, registry = routed(profile("reports"))
    added = router.add(Pool(FakeDriver(), profile()))
    registry.register(profile(), replace=False)
    registry.unregister("default")
    await asyncio.sleep(0)
    assert router.pool("default") is added
    assert not added.closed


async def test_router_remove_closes_the_pool_and_a_profile_router_creates_a_new_one():
    router, _ = routed(profile())
    pool = await router.get_pool("default")
    assert await router.remove("default") is pool
    assert pool.closed
    assert await router.get_pool("default") is not pool
    with pytest.raises(InterfaceError):
        await Router().remove("default")


async def test_router_close_waits_for_retired_pools():
    router, registry = routed(profile())
    pool = await router.get_pool("default")
    connection_gate = asyncio.Event()
    lease = await router.acquire(session())
    await lease.release()
    pool.driver.connections[0].close_gate = connection_gate
    registry.unregister("default")
    closing = asyncio.create_task(router.close())
    await asyncio.sleep(0)
    assert not closing.done()
    connection_gate.set()
    await asyncio.wait_for(closing, GUARD)


async def test_a_closed_profile_router_refuses_operations_and_opens_nothing():
    driver = FakeDriver()
    router, _ = routed(profile(driver=driver))
    await router.close()
    with pytest.raises(InterfaceError, match="closed"):
        await router.run(session(), Op())
    with pytest.raises(InterfaceError, match="closed"):
        await router.acquire(session())
    assert all(pool.closed for pool in router.pools)
    assert driver.connections == []
    await router.close()


async def test_an_operation_resolving_its_profile_when_the_router_closes_fails_and_opens_nothing():
    registry = ProfileRegistry(pool={"acquire_timeout": LONG})
    driver = FakeDriver()
    gate, calls = asyncio.Event(), []

    async def provider(name):
        calls.append(name)
        await gate.wait()
        return profile(name, driver=driver)

    registry.add_provider(provider)
    router = Router(profiles=registry)
    operation = asyncio.create_task(router.run(session(using="late"), Op()))
    await until(lambda: calls)
    await asyncio.wait_for(router.close(), GUARD)
    gate.set()
    with pytest.raises(InterfaceError, match="closed"):
        await asyncio.wait_for(operation, GUARD)
    assert all(pool.closed for pool in router.pools)
    assert all(connection.closed for connection in driver.connections)
    with pytest.raises(InterfaceError, match="closed"):
        await router.run(session(using="late"), Op())
    await router.close()
    assert all(connection.closed for connection in driver.connections)


# The default router of the settings


async def test_without_profiles_there_is_no_default_runner():
    assert get_runner() is None
    with pytest.raises(InterfaceError, match="no runner"):
        await session().execute(Op())


async def test_configured_profiles_run_operations_on_the_default_router():
    driver = FakeDriver()
    adorm.configure(profiles={"default": {"driver": driver}}, pool={"acquire_timeout": LONG})
    router = get_runner()
    assert router is adorm.settings.default_router()
    assert router.profiles is adorm.settings.profiles
    assert await session().execute(Op("q")) == "q"
    async with session("tx").atomic():
        assert await session("tx").execute(Op("in-tx")) == "in-tx"
    assert [c[0] for c in driver.connections[0].calls] == ["execute", "begin", "execute", "commit"]
    await adorm.settings.close_pools()
    assert adorm.settings.default_router(create=False) is None
    assert driver.connections[0].closed


async def test_set_runner_takes_precedence_over_the_default_router():
    adorm.configure(profiles={"default": {"driver": FakeDriver()}})
    router = Router()
    set_runner(router)
    assert get_runner() is router


def test_no_default_runner_outside_an_event_loop():
    adorm.configure(profiles={"default": {"driver": FakeDriver()}})
    assert get_runner() is None
    with pytest.raises(RuntimeError):
        adorm.settings.default_router()


async def test_runtime_profiles_from_a_settings_provider():
    driver = FakeDriver()

    @adorm.settings.add_profile_provider
    def per_user(name):
        if name.startswith("user:"):
            return ConnectionProfile(name, driver, params={"role": name.removeprefix("user:")})
        return None

    assert await create_session("user:alice", "web").execute(Op("q")) == "q"
    assert adorm.settings.get_profile("user:alice").params["role"] == "alice"
    pool = adorm.settings.default_router().pool("user:alice")
    adorm.settings.unregister_profile("user:alice")
    await until(lambda: pool.closed)


async def test_default_routers_are_per_event_loop():
    adorm.configure(profiles={"default": {"driver": FakeDriver()}})
    here = adorm.settings.default_router()

    def other_loop():
        async def main():
            router = adorm.settings.default_router()
            await adorm.settings.close_pools()
            return router

        return asyncio.run(main())

    there = await asyncio.to_thread(other_loop)
    assert there is not here
    assert adorm.settings.default_router() is here


# Hooks


async def test_on_acquire_runs_before_the_first_request_and_on_release_after_the_last():
    hooks = Hooks()
    router, _ = routed(profile(hooks=hooks))
    lease = await router.acquire(session("a"))
    assert hooks.log == [("on_acquire", 1, "a")]
    await lease.call("execute", Op("q"))
    await lease.release()
    connection = router.pool("default").driver.connections[0]
    assert connection.calls == [("on_acquire", "a"), ("execute", "q"), ("on_release", "a")]
    assert router.pool("default").idle == 1


async def test_hooks_receive_the_target_connection_the_profile_and_the_session():
    seen = []

    class Target(FakeConnection):
        @property
        def target(self):
            return ("target", self.number)

    class Driver(FakeDriver):
        async def open_connection(self, profile, emit):  # noqa: ARG002
            connection = Target(self, emit, 1)
            self.connections.append(connection)
            return connection

    def on_acquire(connection, prof, sess):
        seen.append(("acquire", connection, prof.name, sess))

    def on_release(connection, prof, sess):
        seen.append(("release", connection, prof.name, sess))

    router, _ = routed(profile(driver=Driver(), on_acquire=on_acquire, on_release=on_release))
    await router.run(session("x"), Op())
    assert seen == [
        ("acquire", ("target", 1), "default", session("x")),
        ("release", ("target", 1), "default", session("x")),
    ]


async def test_on_acquire_runs_in_the_context_of_the_acquiring_task():
    hooks = Hooks()
    router, _ = routed(profile(hooks=hooks))
    request_var.set("request-1")
    await router.run(session(), Op())
    assert hooks.contexts[0] == "request-1"


async def test_hooks_run_for_every_session_that_gets_the_slot():
    hooks = Hooks()
    router, _ = routed(profile(hooks=hooks, pool=PoolSettings(max_connections=1, acquire_timeout=LONG)))
    for name in ("a", "b", "c"):
        await router.run(session(name), Op())
    assert hooks.log == [(w, 1, n) for n in ("a", "b", "c") for w in ("on_acquire", "on_release")]


async def test_the_next_session_gets_the_slot_only_after_on_release():
    hooks = Hooks()
    hooks.gates["on_release"] = asyncio.Event()
    router, _ = routed(profile(hooks=hooks, pool=PoolSettings(max_connections=1, acquire_timeout=LONG)))
    lease = await router.acquire(session("a"))
    waiter = asyncio.create_task(router.acquire(session("b")))
    await until(lambda: router.pool("default").waiting == 1)
    releasing = asyncio.create_task(lease.release())
    await hooks.entered["on_release"].wait()
    await asyncio.sleep(0)
    assert not waiter.done()
    hooks.gates["on_release"].set()
    await releasing
    second = await asyncio.wait_for(waiter, GUARD)
    assert hooks.log == [("on_acquire", 1, "a"), ("on_release", 1, "a"), ("on_acquire", 1, "b")]
    await second.release()


async def test_a_failing_on_acquire_raises_and_discards_the_slot():
    hooks = Hooks()
    hooks.errors["on_acquire"] = DatabaseError("permission denied to set role")
    router, _ = routed(profile(hooks=hooks))
    with pytest.raises(DatabaseError, match="permission denied"):
        await router.acquire(session())
    pool = router.pool("default")
    assert pool.driver.connections[0].closed
    assert (pool.size, pool.in_use) == (0, 0)
    assert [w for w, _n, _s in hooks.log] == ["on_acquire"]  # no on_release for a discarded slot


async def test_on_acquire_errors_are_mapped_by_the_connection():
    def on_acquire(connection, prof, sess):  # noqa: ARG001
        raise LookupError("no such role")

    router, _ = routed(profile(on_acquire=on_acquire))
    with pytest.raises(DatabaseError) as info:
        await router.run(session(), Op())
    assert isinstance(info.value.original, LookupError)


async def test_a_waiter_gets_a_fresh_connection_after_on_acquire_failed():
    hooks = Hooks()
    hooks.gates["on_acquire"] = asyncio.Event()
    hooks.errors["on_acquire"] = DatabaseError("boom")
    hooks.once.add("on_acquire")
    router, _ = routed(profile(hooks=hooks, pool=PoolSettings(max_connections=1, acquire_timeout=LONG)))
    first = asyncio.create_task(router.acquire(session("a")))
    await hooks.entered["on_acquire"].wait()
    second = asyncio.create_task(router.acquire(session("b")))
    await until(lambda: router.pool("default").waiting == 1)
    hooks.gates["on_acquire"].set()
    with pytest.raises(DatabaseError):
        await first
    lease = await asyncio.wait_for(second, GUARD)
    assert lease.slot.connection.number == 2
    await lease.release()


async def test_on_acquire_that_outlasts_the_acquire_timeout_raises_session_timeout():
    hooks = Hooks()
    hooks.gates["on_acquire"] = asyncio.Event()  # never set
    router, _ = routed(profile(hooks=hooks, pool=PoolSettings(acquire_timeout=LONG)))
    with pytest.raises(SessionTimeout, match="on_acquire of profile 'default' did not finish within 0.05 s") as info:
        await router.acquire(session(), timeout=0.05)
    assert not isinstance(info.value, DeadlockError)
    assert hooks.cancelled == ["on_acquire"]
    pool = router.pool("default")
    assert pool.size == 0
    assert pool.driver.connections[0].closed


async def test_on_acquire_timeout_while_holding_a_slot_is_a_deadlock():
    hooks = Hooks()
    router, registry = routed(profile("held"))
    registry.register(profile(hooks=hooks))
    hooks.gates["on_acquire"] = asyncio.Event()
    async with router.slot(session("h", "held")):
        with pytest.raises(DeadlockError) as info:
            await router.acquire(session(), timeout=0.05)
    assert info.value.held_sessions == (session("h", "held"),)
    assert str(info.value).startswith("on_acquire of profile 'default' did not finish within 0.05 s")
    assert "while holding" in str(info.value)


async def test_a_hook_raising_its_own_timeout_error_is_not_a_session_timeout():
    def on_acquire(connection, prof, sess):  # noqa: ARG001
        raise adorm.OperationTimeout("hook's own timeout")

    router, _ = routed(profile(on_acquire=on_acquire))
    with pytest.raises(adorm.OperationTimeout):
        await router.acquire(session())


@pytest.mark.parametrize("attempt", REPEAT)
async def test_cancelling_the_acquirer_during_on_acquire_leaks_no_slot(attempt):  # noqa: ARG001
    hooks = Hooks()
    hooks.gates["on_acquire"] = asyncio.Event()
    router, _ = routed(profile(hooks=hooks, pool=PoolSettings(max_connections=1, acquire_timeout=LONG)))
    task = asyncio.create_task(router.acquire(session("a")))
    await hooks.entered["on_acquire"].wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert hooks.cancelled == ["on_acquire"]
    pool = router.pool("default")
    await until(lambda: pool.size == 0)
    hooks.gates["on_acquire"].set()
    lease = await asyncio.wait_for(router.acquire(session("b")), GUARD)
    assert lease.slot.connection.number == 2
    await lease.release()


async def test_a_failing_on_release_drops_the_slot_and_logs(caplog):
    hooks = Hooks()
    hooks.errors["on_release"] = DatabaseError("cannot reset role")
    router, _ = routed(profile(hooks=hooks))
    with caplog.at_level(logging.ERROR, logger="adorm.executor"):
        await router.run(session("a"), Op())
    pool = router.pool("default")
    assert pool.size == 0
    assert pool.driver.connections[0].closed
    assert "on_release" in caplog.text
    del hooks.errors["on_release"]
    lease = await router.acquire(session("b"))
    assert lease.slot.connection.number == 2
    await lease.release()


async def test_on_release_that_outlasts_release_timeout_drops_the_slot(caplog):
    hooks = Hooks()
    hooks.gates["on_release"] = asyncio.Event()  # never set
    router, _ = routed(profile(hooks=hooks), release_timeout=0.05)
    with caplog.at_level(logging.ERROR, logger="adorm.executor"):
        await asyncio.wait_for(router.run(session(), Op()), GUARD)
    assert hooks.cancelled == ["on_release"]
    pool = router.pool("default")
    assert pool.size == 0
    assert "did not finish within" in caplog.text


@pytest.mark.parametrize("attempt", REPEAT)
async def test_on_release_completes_when_the_releasing_task_is_cancelled(attempt):  # noqa: ARG001
    hooks = Hooks()
    hooks.gates["on_release"] = asyncio.Event()
    router, _ = routed(profile(hooks=hooks, pool=PoolSettings(max_connections=1, acquire_timeout=LONG)))
    lease = await router.acquire(session("a"))
    releasing = asyncio.create_task(lease.release())
    await hooks.entered["on_release"].wait()
    releasing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await releasing
    assert hooks.cancelled == []
    hooks.gates["on_release"].set()
    pool = router.pool("default")
    await until(lambda: pool.idle == 1)
    assert not pool.driver.connections[0].closed


@pytest.mark.parametrize(
    "end",
    ["discard", "unusable", "in_transaction"],
)
async def test_on_release_does_not_run_for_a_slot_that_leaves_the_pool(end):
    hooks = Hooks()
    router, _ = routed(profile(hooks=hooks))
    lease = await router.acquire(session())
    if end == "discard":
        await lease.release(discard=True)
    elif end == "unusable":
        lease.slot.connection.usable = False
        await lease.release()
    else:
        await lease.call("begin")
        await lease.release()
    assert [w for w, _n, _s in hooks.log] == ["on_acquire"]
    assert router.pool("default").size == 0


async def test_transaction_hooks_wrap_begin_and_commit():
    hooks = Hooks()
    router, _ = routed(profile(hooks=hooks))
    set_runner(router)
    async with session("tx").atomic():
        await session("tx").execute(Op("q"))
    with pytest.raises(RuntimeError):
        async with session("tx").atomic():
            raise RuntimeError
    calls = router.pool("default").driver.connections[0].calls
    assert calls == [
        ("on_acquire", "tx"),
        ("begin", None),
        ("execute", "q"),
        ("commit", None),
        ("on_release", "tx"),
        ("on_acquire", "tx"),
        ("begin", None),
        ("rollback", None),
        ("on_release", "tx"),
    ]


async def test_an_aborted_transaction_skips_on_release():
    hooks = Hooks()
    router, _ = routed(profile(hooks=hooks), commit_timeout=0.05)
    set_runner(router)
    pool = await router.get_pool("default")
    with pytest.raises(TransactionClosed):
        async with session("tx").atomic():
            await until(lambda: pool.size == 0)
    assert [w for w, _n, _s in hooks.log] == ["on_acquire"]


async def test_atomic_entry_raises_the_on_acquire_error():
    hooks = Hooks()
    hooks.errors["on_acquire"] = DatabaseError("role does not exist")
    router, _ = routed(profile(hooks=hooks))
    set_runner(router)
    with pytest.raises(DatabaseError, match="role does not exist"):
        async with session().atomic():
            pytest.fail("the block must not run")
    assert router.pool("default").size == 0


@pytest.mark.parametrize("attempt", range(10))
async def test_hooks_never_interleave_on_one_connection_under_load(attempt):  # noqa: ARG001
    """Each connection alternates on_acquire / on_release for one session at a time, and every request of a session
    runs between its own hooks."""
    state: dict[int, str | None] = {}
    violations = []

    async def on_acquire(connection, prof, sess):  # noqa: ARG001
        if state.get(connection.number) is not None:
            violations.append(("acquire over", connection.number, state[connection.number]))
        await asyncio.sleep(0)
        state[connection.number] = sess.session_name

    async def on_release(connection, prof, sess):  # noqa: ARG001
        if state.get(connection.number) != sess.session_name:
            violations.append(("release of", connection.number, sess.session_name))
        await asyncio.sleep(0)
        state[connection.number] = None

    def check(name):
        def effect(connection):
            if state.get(connection.number) != name:
                violations.append(("request", connection.number, name, state.get(connection.number)))

        return effect

    router, _ = routed(
        profile(
            on_acquire=on_acquire,
            on_release=on_release,
            pool=PoolSettings(max_connections=3, acquire_timeout=LONG),
        )
    )
    set_runner(router)

    async def flow(i):
        name = f"flow-{i}"
        for j in range(5):
            if j % 2:
                async with session(name).atomic():
                    await session(name).execute(Op(effect=check(name)))
            else:
                await session(name).execute(Op(effect=check(name)))

    await asyncio.gather(*(flow(i) for i in range(12)))
    assert violations == []
    pool = router.pool("default")
    assert pool.idle == pool.size <= 3
    assert all(value is None for value in state.values())


# Threaded drivers


class ThreadedFake(ThreadedConnection):
    def connect_sync(self):
        self.thread_id = threading.get_ident()

    def execute_sync(self, operation):
        return operation.name

    def begin_sync(self):
        pass

    def commit_sync(self):
        pass

    def rollback_sync(self):
        pass

    def close_sync(self):
        pass


class ThreadedFakeDriver(ThreadedDriver):
    name = "threaded-fake"
    connection_class = ThreadedFake
    capabilities = Capabilities(operations={"op"})


async def test_threaded_hooks_run_on_the_connection_thread():
    threads = []

    def on_acquire(connection, prof, sess):  # noqa: ARG001
        threads.append(("acquire", threading.get_ident(), connection.thread_id))

    def on_release(connection, prof, sess):  # noqa: ARG001
        threads.append(("release", threading.get_ident(), connection.thread_id))

    router, _ = routed(profile(driver=ThreadedFakeDriver(), on_acquire=on_acquire, on_release=on_release))
    assert await router.run(session(), Op("q")) == "q"
    loop_thread = threading.get_ident()
    assert [t[0] for t in threads] == ["acquire", "release"]
    for _what, ran_on, connection_thread in threads:
        assert ran_on == connection_thread != loop_thread
    await router.close()


@pytest.mark.parametrize("what", ["connect", "on_acquire", "on_release"])
async def test_threaded_driver_refuses_coroutine_hooks(what):
    async def hook(*args):
        pass

    with pytest.raises(TypeError, match=f"{what} of profile 'default' is a coroutine function"):
        Pool(ThreadedFakeDriver(), profile(**{what: hook}))
    for driver in (ThreadedFakeDriver(), ThreadedFakeDriver):
        with pytest.raises(TypeError, match=f"{what} .* coroutine function"):
            profile(driver=driver, **{what: hook})
    # A driver given by import path is checked when its pool is created.
    router, _ = routed(profile(driver="tests.test_profiles.ThreadedFakeDriver", **{what: hook}))
    with pytest.raises(TypeError, match=f"{what} .* coroutine function"):
        await router.get_pool("default")


async def test_async_driver_accepts_coroutine_hooks():
    async def hook(*args):
        pass

    prof = profile(connect=hook, on_acquire=hook, on_release=hook)
    assert Pool(prof.get_driver(), prof).on_acquire is hook


async def test_a_threaded_hook_that_returns_an_awaitable_fails_the_acquisition():
    async def later():
        pass

    def on_acquire(connection, prof, sess):  # noqa: ARG001
        return later()

    router, _ = routed(profile(driver=ThreadedFakeDriver(), on_acquire=on_acquire))
    with pytest.raises(InterfaceError, match="returned an awaitable"):
        await router.acquire(session())
    assert router.pool("default").size == 0
    await router.close()


async def test_pool_accepts_profiles_without_hook_attributes():
    from tests.executor_fakes import Profile

    pool = Pool(FakeDriver(), Profile("default"))
    assert (pool.on_acquire, pool.on_release) == (None, None)

    class Bad:
        name = "default"
        alias = "default"
        params = {}  # noqa: RUF012
        connect = None
        on_acquire = 1

    with pytest.raises(TypeError, match="on_acquire"):
        Pool(FakeDriver(), Bad())


def test_profile_copies():
    import copy

    prof = profile(params={"a": 1})
    assert copy.copy(prof) is prof
    assert copy.deepcopy(prof) is prof


# The connect hook


class TargetConnection(FakeConnection):
    raw = None

    @property
    def target(self):
        return self.raw


class ConnectingDriver(FakeDriver):
    """Opens the target's connection with ``profile.connect`` when it is set."""

    async def open_connection(self, profile, emit):
        connection = TargetConnection(self, emit, len(self.connections) + 1)
        if profile.connect is not None:
            connection.raw = await profile.connect(profile)
        self.connections.append(connection)
        return connection


async def test_connect_opens_each_slot_and_hooks_receive_its_connection():
    opened = []
    seen = []

    async def connect(prof):
        opened.append(prof.name)
        return f"raw-{len(opened)}"

    async def on_acquire(connection, prof, sess):  # noqa: ARG001
        seen.append(connection)

    registry = ProfileRegistry(pool=PoolSettings(acquire_timeout=LONG))
    registry.register(ConnectionProfile("default", ConnectingDriver(), connect=connect, on_acquire=on_acquire))
    router = Router(profiles=registry)
    async with router.slot(session("a")), router.slot(session("b")):
        pass
    assert await router.run(session("c"), Op("q")) == "q"
    assert opened == ["default", "default"]
    assert seen[:2] == ["raw-1", "raw-2"]
    assert seen[2] in ("raw-1", "raw-2")
    await router.close()


async def test_connect_of_a_configured_profile_is_used_by_the_default_router():
    opened = []

    async def connect(prof):
        opened.append(prof.params["role"])
        return "raw"

    adorm.configure(profiles={"default": {"driver": ConnectingDriver(), "connect": connect, "params": {"role": "r"}}})
    assert await create_session().execute(Op("q")) == "q"
    assert opened == ["r"]


# Default routers and closed event loops


def threaded_closes():
    closed = []

    class Closing(ThreadedFake):
        def close_sync(self):
            closed.append(threading.get_ident())

    class Driver(ThreadedFakeDriver):
        connection_class = Closing

    return Driver(), closed


def run_in_new_loops(main, count, *, shut_down=True):
    """Run ``main`` in ``count`` new event loops on a separate thread; ``shut_down`` closes them as `asyncio.run()`
    does, otherwise they cancel their tasks and close without shutting down their asynchronous generators."""

    def runner():
        for _ in range(count):
            if shut_down:
                asyncio.run(main())
                continue
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(main())
                tasks = asyncio.all_tasks(loop)
                for task in tasks:
                    task.cancel()
                loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            finally:
                loop.close()

    thread = threading.Thread(target=runner)
    thread.start()
    thread.join(GUARD)


async def test_a_loop_shutting_down_closes_its_default_router():
    driver, closed = threaded_closes()
    adorm.configure(profiles={"default": {"driver": driver}})
    loops = []

    async def main():
        loops.append(asyncio.get_running_loop())
        assert await create_session().execute(Op("q")) == "q"

    run_in_new_loops(main, 2)
    assert len(closed) == 2
    assert not any(loop in adorm.settings._routers for loop in loops)


async def test_creating_a_router_drops_the_routers_of_loops_closed_without_shutdown(caplog):
    driver, closed = threaded_closes()
    adorm.configure(profiles={"default": {"driver": driver}, "async": {"driver": FakeDriver()}})
    loops = []

    async def main():
        loops.append(asyncio.get_running_loop())
        assert await create_session().execute(Op("q")) == "q"
        assert await create_session(using="async").execute(Op("q")) == "q"

    run_in_new_loops(main, 2, shut_down=False)
    assert loops[0] not in adorm.settings._routers
    await until(lambda: len(closed) == 1)
    assert "1 connection(s) of its default router are left open" in caplog.text
    adorm.settings.default_router()
    assert not any(loop in adorm.settings._routers for loop in loops)
    await until(lambda: len(closed) == 2)


async def test_close_pools_closes_the_router_once():
    adorm.configure(profiles={"default": {"driver": FakeDriver()}})
    router = adorm.settings.default_router()
    await create_session().execute(Op("q"))
    pool = router.pool("default")
    await adorm.settings.close_pools()
    assert pool.closed
    assert adorm.settings.default_router(create=False) is None
    assert adorm.settings.default_router() is not router
