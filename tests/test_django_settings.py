"""The Django app ``adorm.contrib.django``: adorm's configuration from the ``ADORM`` setting and reconfiguration when a
test overrides it."""

import asyncio
import importlib
import json
import os
import subprocess
import sys

from pathlib import Path

import pytest

from django.conf import settings as django_settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connection as django_connection
from django.test import override_settings
from django.test.signals import setting_changed

import adorm
import adorm.settings

from adorm import ConnectionProfile, create_session, PoolSettings
from adorm.contrib.django import (
    configure_from_settings,
    DEFAULT_DRIVER,
    default_profiles,
    ensure_configured,
    settings_configuration,
)
from adorm.contrib.django.apps import AdormConfig
from adorm.drivers.django import DjangoDriver
from adorm.executor import debug_enabled, set_debug, set_runner
from adorm.settings import import_object
from tests.executor_fakes import FakeDriver, Op

REPEAT = range(10)
GUARD = 5
ROOT = Path(__file__).parent.parent
NOT_CALLABLE = 42


def on_acquire(connection, profile, session):  # noqa: ARG001
    return None


def on_release(connection, profile, session):  # noqa: ARG001
    return None


async def async_hook(connection, profile, session):  # noqa: ARG001
    return None


def first_provider(name):  # noqa: ARG001
    return None


def second_provider(name):  # noqa: ARG001
    return None


async def until(predicate):
    async with asyncio.timeout(GUARD):
        while not predicate():
            await asyncio.sleep(0)


@pytest.fixture(autouse=True)
async def clean_settings():
    adorm.configure()
    previous = set_runner(None)
    yield
    setting_changed.disconnect(dispatch_uid="adorm.contrib.django.reconfigure")
    await adorm.settings.close_pools()
    adorm.configure()
    set_runner(previous)
    set_debug(None)


def app_config() -> AdormConfig:
    return AdormConfig("adorm.contrib.django", importlib.import_module("adorm.contrib.django"))


def snapshot():
    registry = adorm.settings.profiles
    return {p.name: p for p in registry}, registry.providers, registry.pool


# Reading the setting


def test_without_the_setting_every_database_alias_is_a_django_driver_profile():
    assert default_profiles() == {
        "default": {"driver": DEFAULT_DRIVER, "alias": "default"},
        "second": {"driver": DEFAULT_DRIVER, "alias": "second"},
    }
    configure_from_settings()
    registry = adorm.settings.profiles
    assert {p.name: (p.driver, p.alias) for p in registry} == {
        "default": (DEFAULT_DRIVER, "default"),
        "second": (DEFAULT_DRIVER, "second"),
    }
    assert registry.providers == ()
    assert registry.pool == PoolSettings()


def test_ensure_configured_configures_unless_the_app_is_installed(monkeypatch):
    from django.apps import apps

    monkeypatch.setattr(apps, "is_installed", lambda name: name == "adorm.contrib.django")
    ensure_configured()
    assert list(adorm.settings.profiles) == []
    monkeypatch.setattr(apps, "is_installed", lambda _name: False)
    ensure_configured()
    assert [profile.name for profile in adorm.settings.profiles] == ["default", "second"]


@override_settings(ADORM=None)
def test_a_none_setting_is_an_empty_one():
    configure_from_settings()
    assert sorted(p.name for p in adorm.settings.profiles) == ["default", "second"]


@override_settings(ADORM={"pool": {"max_connections": 10, "acquire_timeout": 10, "commit_timeout": 30}, "debug": True})
def test_pool_and_debug_without_profiles_keep_the_default_profiles():
    configure_from_settings()
    registry = adorm.settings.profiles
    assert sorted(p.name for p in registry) == ["default", "second"]
    assert registry.pool == PoolSettings(max_connections=10, acquire_timeout=10, commit_timeout=30)
    assert debug_enabled() is True


def test_explicit_profiles_replace_the_default_ones():
    driver = FakeDriver()
    reports = ConnectionProfile("reports", driver, pool=PoolSettings(max_connections=2))
    adorm_setting = {"profiles": {"default": {"driver": driver, "alias": "second"}, "reports": reports}}
    with override_settings(ADORM=adorm_setting):
        configure_from_settings()
    assert adorm.settings.get_profile("reports") is reports
    assert adorm.settings.get_profile("default").alias == "second"
    assert adorm.settings.get_profile("second") is None
    with override_settings(ADORM={"profiles": {}}):
        configure_from_settings()
    assert len(adorm.settings.profiles) == 0


@override_settings(
    ADORM={
        "profiles": {
            "default": {
                "driver": DEFAULT_DRIVER,
                "on_acquire": "tests.test_django_settings.on_acquire",
                "on_release": "tests.test_django_settings:on_release",
                "connect": None,
            }
        },
        "providers": ["tests.test_django_settings.second_provider", first_provider],
    }
)
def test_hooks_and_providers_are_imported_from_their_paths():
    configuration = settings_configuration()
    profile = configuration["profiles"]["default"]
    assert profile.on_acquire is on_acquire
    assert profile.on_release is on_release
    assert profile.connect is None
    assert configuration["providers"] == [second_provider, first_provider]
    configure_from_settings()
    assert adorm.settings.profiles.providers == (second_provider, first_provider)


def content_types_key(profile, session):  # noqa: ARG001 - the hook's signature
    return None


@override_settings(
    ADORM={
        "profiles": {
            "default": {"driver": DEFAULT_DRIVER, "content_types_key": "tests.test_django_settings.content_types_key"}
        }
    }
)
def test_content_types_key_is_imported_from_its_path():
    assert settings_configuration()["profiles"]["default"].content_types_key is content_types_key


def test_import_object():
    assert import_object("tests.test_django_settings.on_acquire") is on_acquire
    assert import_object("tests.test_django_settings:on_release") is on_release
    with pytest.raises(ValueError, match="a hook path"):
        import_object("on_acquire", "hook")
    with pytest.raises(ImportError, match="has no hook 'missing'"):
        import_object("tests.test_django_settings.missing", "hook")
    with pytest.raises(ImportError):
        import_object("tests.no_such_module.hook")


def profile_with(**changes):
    return {"profiles": {"default": {"driver": FakeDriver(), **changes}}}


INVALID = [
    pytest.param(["default"], "ADORM is a mapping", id="not-a-mapping"),
    pytest.param({"profile": {}}, r"unknown keys \['profile'\]", id="unknown-key"),
    pytest.param({"profiles": ["default"]}, r"ADORM\['profiles'\] is a mapping", id="profiles-not-a-mapping"),
    pytest.param({"profiles": {"default": {"driver": 1}}}, r"\['default'\]: driver is", id="bad-driver"),
    pytest.param({"profiles": {"default": "x"}}, r"\['default'\]: a connection profile is", id="bad-profile"),
    pytest.param(
        {"profiles": {"default": ConnectionProfile("other", FakeDriver())}}, "registered under", id="profile-name"
    ),
    pytest.param(profile_with(alias=""), r"\['default'\]: alias is", id="empty-alias"),
    pytest.param(
        profile_with(on_acquire="tests.no_such_module.hook"), r"\['on_acquire'\]: No module", id="missing-module"
    ),
    pytest.param(
        profile_with(on_release="tests.test_django_settings.missing"), r"\['on_release'\]: module", id="missing-name"
    ),
    pytest.param(profile_with(connect="connect"), r"\['connect'\]: a hook path is", id="malformed-path"),
    pytest.param(
        profile_with(on_acquire="tests.test_django_settings.NOT_CALLABLE"), "a hook is a callable", id="not-callable"
    ),
    pytest.param(
        {"profiles": {"default": {"driver": DjangoDriver, "on_acquire": "tests.test_django_settings.async_hook"}}},
        r"\['default'\]: .*coroutine",
        id="async-hook-on-threaded-driver",
    ),
    pytest.param(
        {"profiles": {"default": {"driver": "adorm.drivers.django.DjangoDriverTypo"}}},
        r"\['default'\]\['driver'\]: module 'adorm.drivers.django' has no driver 'DjangoDriverTypo'",
        id="misspelled-driver-path",
    ),
    pytest.param(
        {"profiles": {"default": {"driver": "adorm.no_such_module.Driver"}}},
        r"\['default'\]\['driver'\]: No module",
        id="driver-module-missing",
    ),
    pytest.param(
        {"profiles": {"default": {"driver": "tests.test_django_settings.on_acquire"}}},
        r"\['default'\]: driver is",
        id="driver-path-not-a-driver",
    ),
    pytest.param(
        {"profiles": {"default": {"driver": DEFAULT_DRIVER, "on_acquire": "tests.test_django_settings.async_hook"}}},
        r"\['default'\]: .*coroutine",
        id="async-hook-on-threaded-driver-path",
    ),
    pytest.param(
        {"profiles": {"default": {"driver": DEFAULT_DRIVER, "options": {"work_mem": "1MB"}}}},
        r"\['default'\]: unsupported connection options: work_mem",
        id="option-the-driver-does-not-declare",
    ),
    pytest.param(
        {"profiles": {"default": ConnectionProfile("default", DEFAULT_DRIVER, options={"work_mem": "1MB"})}},
        r"\['default'\]: unsupported connection options: work_mem",
        id="profile-instance-with-driver-path",
    ),
    pytest.param(
        {"profiles": {"default": {"driver": DEFAULT_DRIVER, "covered_receivers": "myapp.signals.on_saved"}}},
        r"\['default'\]: covered_receivers of profile 'default' is a list",
        id="covered-receivers-string",
    ),
    pytest.param({"providers": "tests.test_django_settings.first_provider"}, "is a list", id="providers-string"),
    pytest.param({"providers": [42]}, r"\['providers'\]\[0\]: a profile provider is", id="provider-not-callable"),
    pytest.param({"providers": ["tests.no_such_module.p"]}, r"\['providers'\]\[0\]", id="provider-missing"),
    pytest.param({"pool": {"max_connections": -1}}, r"ADORM\['pool'\]: max_connections", id="bad-pool"),
    pytest.param({"pool": {"max_conections": 1}}, r"ADORM\['pool'\]", id="pool-typo"),
    pytest.param({"pool": "big"}, r"ADORM\['pool'\]", id="pool-not-a-mapping"),
    pytest.param({"debug": "yes"}, r"ADORM\['debug'\]", id="bad-debug"),
]


@pytest.mark.parametrize(("value", "message"), INVALID)
def test_an_invalid_setting_raises_improperly_configured_and_changes_nothing(value, message):
    adorm.configure(profiles={"kept": {"driver": FakeDriver()}}, pool={"acquire_timeout": 3})
    before = snapshot()
    with override_settings(ADORM=value), pytest.raises(ImproperlyConfigured, match=message):
        configure_from_settings()
    assert snapshot() == before


def test_a_driver_path_is_checked_but_stays_a_path():
    with override_settings(ADORM={"profiles": {"default": {"driver": DEFAULT_DRIVER, "on_acquire": on_acquire}}}):
        configure_from_settings()
    assert adorm.settings.get_profile("default").driver == DEFAULT_DRIVER


def test_a_hook_module_that_fails_to_import_raises_improperly_configured(tmp_path, monkeypatch):
    (tmp_path / "adorm_failing_hooks.py").write_text("raise RuntimeError('settings not ready')\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    adorm.configure(profiles={"kept": {"driver": FakeDriver()}})
    before = snapshot()
    value = profile_with(on_acquire="adorm_failing_hooks.on_acquire")
    with override_settings(ADORM=value), pytest.raises(ImproperlyConfigured, match=r"\['on_acquire'\]: settings not"):
        configure_from_settings()
    assert snapshot() == before


async def test_an_invalid_setting_leaves_running_pools_alone():
    driver = FakeDriver()
    adorm.configure(profiles={"default": {"driver": driver}})
    assert await create_session().execute(Op("before")) == "before"
    pool = adorm.settings.default_router().pool("default")
    with override_settings(ADORM={"profiles": {"default": {"driver": 1}}}), pytest.raises(ImproperlyConfigured):
        configure_from_settings()
    await asyncio.sleep(0)
    assert adorm.settings.default_router().pool("default") is pool
    assert not pool.closed
    assert await create_session().execute(Op("after")) == "after"
    assert len(driver.connections) == 1


# The app


def test_the_app_configures_adorm_when_ready():
    driver = FakeDriver()
    with override_settings(ADORM={"profiles": {"default": {"driver": driver}}, "pool": {"max_connections": 3}}):
        app_config().ready()
        assert adorm.settings.get_profile("default").get_driver() is driver
        assert adorm.settings.profiles.pool == PoolSettings(max_connections=3)


@override_settings(ADORM={"profiles": {"default": {"driver": 1}}})
def test_ready_raises_improperly_configured_for_an_invalid_setting():
    with pytest.raises(ImproperlyConfigured, match="driver is"):
        app_config().ready()


def test_overriding_the_setting_reconfigures_and_restores():
    first, second = FakeDriver(), FakeDriver()
    with override_settings(ADORM={"profiles": {"default": {"driver": first}}}):
        app_config().ready()
        with override_settings(ADORM={"profiles": {"other": {"driver": second}}}):
            assert [p.name for p in adorm.settings.profiles] == ["other"]
            assert adorm.settings.get_profile("other").get_driver() is second
        assert [p.name for p in adorm.settings.profiles] == ["default"]
        assert adorm.settings.get_profile("default").get_driver() is first


@pytest.mark.filterwarnings("ignore:Overriding setting DATABASES")
def test_overriding_databases_reconfigures_the_default_profiles():
    databases = {**django_settings.DATABASES, "third": django_settings.DATABASES["second"]}
    app_config().ready()
    assert sorted(p.name for p in adorm.settings.profiles) == ["default", "second"]
    with override_settings(DATABASES=databases):
        assert sorted(p.name for p in adorm.settings.profiles) == ["default", "second", "third"]
    assert sorted(p.name for p in adorm.settings.profiles) == ["default", "second"]


@pytest.mark.filterwarnings("ignore:Overriding setting DATABASES")
def test_overriding_databases_keeps_explicit_profiles():
    databases = {**django_settings.DATABASES, "third": django_settings.DATABASES["second"]}
    with override_settings(ADORM={"profiles": {"only": {"driver": FakeDriver()}}}):
        app_config().ready()
        before = snapshot()
        with override_settings(DATABASES=databases):
            assert snapshot() == before


def test_overriding_another_setting_does_not_reconfigure():
    with override_settings(ADORM={"profiles": {"default": {"driver": FakeDriver()}}}):
        app_config().ready()
        before = snapshot()
        with override_settings(TIME_ZONE="Europe/Ljubljana", DEBUG=True):
            assert snapshot() == before


def test_ready_twice_connects_one_receiver():
    with override_settings(ADORM={"profiles": {"default": {"driver": FakeDriver()}}}):
        app_config().ready()
        app_config().ready()
        calls = []
        original = adorm.configure

        def counting(**kwargs):
            calls.append(kwargs)
            original(**kwargs)

        adorm.configure = counting
        try:
            with override_settings(ADORM={"profiles": {}}):
                pass
        finally:
            adorm.configure = original
        assert len(calls) == 2  # entering and leaving the override


def test_an_invalid_override_raises_and_keeps_the_settings_of_the_outer_one():
    with override_settings(ADORM={"profiles": {"default": {"driver": FakeDriver()}}}):
        app_config().ready()
        with pytest.raises(ImproperlyConfigured), override_settings(ADORM={"pool": "big"}):
            pass
        assert [p.name for p in adorm.settings.profiles] == ["default"]


@pytest.mark.parametrize("from_thread", [False, True], ids=["loop-thread", "other-thread"])
@pytest.mark.parametrize("attempt", REPEAT)
async def test_an_override_during_a_transaction_lets_it_commit_on_its_slot(from_thread, attempt):  # noqa: ARG001
    old, new = FakeDriver(), FakeDriver()
    outer = override_settings(ADORM={"profiles": {"default": {"driver": old}}})
    outer.enable()
    try:
        app_config().ready()
        inner = override_settings(ADORM={"profiles": {"default": {"driver": new}}})
        session = create_session("default", "tx")
        async with session.atomic():
            assert await session.execute(Op("in-tx")) == "in-tx"
            if from_thread:
                await asyncio.to_thread(inner.enable)
            else:
                inner.enable()
            # the transaction keeps its slot on the old driver
            assert await session.execute(Op("still")) == "still"
            assert await create_session("default", "outside").execute(Op("new")) == "new"
        assert [c[0] for c in old.connections[0].calls] == ["begin", "execute", "execute", "commit"]
        assert [c[0] for c in new.connections[0].calls] == ["execute"]
        await until(lambda: old.connections[0].closed)
        assert not new.connections[0].closed
        inner.disable()
        assert await create_session().execute(Op("restored")) == "restored"
        assert len(old.connections) == 2
        await until(lambda: new.connections[0].closed)
    finally:
        outer.disable()


async def test_an_override_of_only_the_pool_rebuilds_pools_of_profiles_without_their_own():
    driver = FakeDriver()
    prof = ConnectionProfile("default", driver)
    outer = override_settings(ADORM={"profiles": {"default": prof}, "pool": {"max_connections": 1}})
    outer.enable()
    try:
        app_config().ready()
        assert await create_session().execute(Op("first")) == "first"
        pool = adorm.settings.default_router().pool("default")
        with override_settings(ADORM={"profiles": {"default": prof}, "pool": {"max_connections": 4}}):
            await until(lambda: pool.closed)
            assert await create_session().execute(Op("second")) == "second"
            assert adorm.settings.default_router().pool("default").max_connections == 4
        assert await create_session().execute(Op("third")) == "third"
        assert adorm.settings.default_router().pool("default").max_connections == 1
    finally:
        outer.disable()


@pytest.mark.parametrize("attempt", REPEAT)
async def test_an_override_with_the_same_values_keeps_the_pool_and_its_waiters(attempt):  # noqa: ARG001
    driver = FakeDriver()
    value = {"profiles": {"default": {"driver": driver}}, "pool": {"max_connections": 1, "acquire_timeout": GUARD}}
    outer = override_settings(ADORM=value)
    outer.enable()
    try:
        app_config().ready()
        holder = create_session("default", "holder")
        async with holder.atomic():
            await holder.execute(Op("held"))
            pool = adorm.settings.default_router().pool("default")
            waiter = asyncio.create_task(create_session("default", "waiter").execute(Op("waited")))
            await until(lambda: pool.waiting == 1)
            with override_settings(ADORM=dict(value)):
                await asyncio.sleep(0)
                assert adorm.settings.default_router().pool("default") is pool
        assert await waiter == "waited"
        assert not pool.closed
        assert len(driver.connections) == 1
    finally:
        outer.disable()


# A process that sets Django up with the app


SCRIPT = """
import asyncio, json
import django

django.setup()

import adorm
from adorm.settings import default_router
from tests.djangoproject import adorm_hooks
from tests.generated.library import Tag


async def main():
    count = await Tag.objects.count()
    pool = default_router().pool("default")
    timed_out = False
    async with adorm.atomic():
        await Tag.objects.count()
        try:
            await adorm.create_session("default", "second").Tag.objects.count()
        except adorm.SessionTimeout:
            timed_out = True
    reports = await adorm.create_session("reports", "nightly").Tag.objects.count()
    await adorm.settings.close_pools()
    return {
        "count": count,
        "reports": reports,
        "timed_out": timed_out,
        "max_connections": pool.max_connections,
        "acquire_timeout": pool.acquire_timeout,
        "calls": adorm_hooks.calls,
    }


print(json.dumps(asyncio.run(main())))
"""


@pytest.mark.django_db(transaction=True)
def test_django_setup_configures_adorm_from_the_setting_in_a_new_process():
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "tests.djangoproject.adorm_app_settings",
        "ADORM_APP_DATABASE_NAME": django_connection.settings_dict["NAME"],
    }
    result = subprocess.run(  # noqa: S603 - the interpreter running the suite
        [sys.executable, "-c", SCRIPT], capture_output=True, text=True, cwd=ROOT, env=env, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr[-3000:]
    outcome = json.loads(result.stdout.strip().splitlines()[-1])
    assert outcome["count"] == 0
    assert outcome["reports"] == 0
    assert outcome["timed_out"] is True
    assert (outcome["max_connections"], outcome["acquire_timeout"]) == (1, 0.5)
    assert outcome["calls"] == [
        ["on_acquire", "default", "default"],
        ["on_release", "default", "default"],
        ["on_acquire", "default", "default"],
        ["on_release", "default", "default"],
        ["on_acquire", "reports", "nightly"],
    ]


def test_django_setup_fails_on_an_invalid_setting_in_a_new_process(tmp_path):
    (tmp_path / "broken_settings.py").write_text(
        "from tests.djangoproject.adorm_app_settings import *  # noqa: F403\n"
        "ADORM = {'profiles': {'default': {'driver': 'adorm.drivers.django.DjangoDriver',"
        " 'on_acquire': 'tests.no_such_module.hook'}}}\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "broken_settings",
        "PYTHONPATH": os.pathsep.join([str(tmp_path), str(ROOT)]),
    }
    result = subprocess.run(  # noqa: S603 - the interpreter running the suite
        [sys.executable, "-c", "import django; django.setup()"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert "ADORM['profiles']['default']['on_acquire']" in result.stderr
