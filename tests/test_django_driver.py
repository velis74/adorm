"""The Django driver on PostgreSQL: what is particular to it. The reads it shares with every driver are in
``tests/conformance``."""

import asyncio
import threading

import pytest

from django.db import connections

import adorm

from adorm import ConnectionProfile, NotSupportedError
from adorm.drivers.django import CAPABILITIES, DjangoConnection, DjangoDriver, READ_CAPABILITIES
from adorm.executor import Pool, Router, set_runner
from tests.generated.library import Author

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
async def executor():
    """``make(**pool)`` sets up a router with one pool of `DjangoDriver` slots for the profile ``default``."""
    routers = []

    def make(**kwargs):
        driver = DjangoDriver()
        pool = Pool(driver, ConnectionProfile("default", driver), **kwargs)
        router = Router([pool])
        routers.append(router)
        set_runner(router)
        return pool

    previous = set_runner(None)
    try:
        yield make
    finally:
        set_runner(previous)
        for router in routers:
            await router.close()


@pytest.fixture
async def pool(executor):
    return executor(max_connections=2)


async def test_every_orm_call_of_a_connection_runs_on_its_own_thread(library, executor, monkeypatch):  # noqa: ARG001
    executor(max_connections=1)
    threads = []
    original = DjangoConnection.execute_sync

    def execute_sync(self, operation):
        threads.append(threading.current_thread())
        return original(self, operation)

    monkeypatch.setattr(DjangoConnection, "execute_sync", execute_sync)
    await asyncio.gather(*(Author.objects.filter(rating__gte=n).count() for n in range(10)))
    assert len(set(threads)) == 1
    assert threads[0] is not threading.current_thread()
    assert threads[0].name.startswith("adorm-django-")


async def test_a_cancel_arriving_after_the_statement_does_not_reach_the_next_one(library, executor, monkeypatch):  # noqa: ARG001
    pool = executor(max_connections=1)
    returned, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    state = {"armed": False}
    execute, cancel = DjangoConnection.execute_sync, DjangoConnection.cancel_sync

    def execute_sync(self, operation):
        result = execute(self, operation)
        if state["armed"]:
            state["armed"] = False
            returned.set()
            release.wait(10)
        return result

    def cancel_sync(self):
        cancel(self)
        cancelled.set()

    monkeypatch.setattr(DjangoConnection, "execute_sync", execute_sync)
    monkeypatch.setattr(DjangoConnection, "cancel_sync", cancel_sync)
    assert await Author.objects.count() == 4
    for _ in range(20):
        for event in (returned, release, cancelled):
            event.clear()
        state["armed"] = True
        task = asyncio.create_task(Author.objects.count())
        assert await asyncio.to_thread(returned.wait, 10)
        task.cancel()
        assert await asyncio.to_thread(cancelled.wait, 10)  # the statement has ended: the cancel finds nothing to stop
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await Author.objects.filter(rating__gte=4).count() == 2
        assert (pool.size, pool.in_use) == (1, 0)


# Connections


async def open_pool(**profile):
    driver = DjangoDriver()
    pool = Pool(driver, ConnectionProfile("default", driver, **profile), max_connections=2)
    router = Router([pool])
    set_runner(router)
    return pool, router


async def test_profile_params_and_hooks_reach_the_slots_connection(library):  # noqa: ARG001
    shared = dict(connections.settings["default"])
    options = {**shared.get("OPTIONS", {}), "options": "-c application_name=adorm_reports"}
    seen = []

    def on_acquire(connection, profile, session):  # noqa: ARG001
        with connection.cursor() as cursor:
            cursor.execute("SHOW application_name")
            seen.append((cursor.fetchone()[0], threading.current_thread().name, connection.settings_dict["OPTIONS"]))

    previous = set_runner(None)
    pool, router = await open_pool(params={"OPTIONS": options}, on_acquire=on_acquire)
    try:
        assert await Author.objects.count() == 4
        async with adorm.atomic():
            assert await Author.objects.filter(rating__gte=4).count() == 2
    finally:
        set_runner(previous)
        await router.close()
    assert [name for name, _, _ in seen] == ["adorm_reports", "adorm_reports"]
    assert all(thread.startswith("adorm-django-") for _, thread, _ in seen)
    assert seen[0][2] == options
    assert connections.settings["default"] == shared
    assert pool.size == 0


async def test_the_connect_hook_opens_the_connection_django_configures(library):  # noqa: ARG001
    import psycopg

    opened, levels = [], []

    def connect(profile):
        opened.append(threading.current_thread().name)
        return psycopg.connect(**connections[profile.alias].get_connection_params())

    def on_acquire(connection, profile, session):  # noqa: ARG001
        levels.append(connection.connection.isolation_level)

    options = {
        **connections.settings["default"].get("OPTIONS", {}),
        "isolation_level": psycopg.IsolationLevel.SERIALIZABLE,
    }
    previous = set_runner(None)
    pool, router = await open_pool(params={"OPTIONS": options}, connect=connect, on_acquire=on_acquire)
    try:
        counts = await asyncio.gather(*(Author.objects.filter(rating__gte=n).count() for n in range(10)))
        assert counts == [4, 4, 3, 3, 2, 1, 0, 0, 0, 0]
        async with adorm.atomic():
            assert await Author.objects.filter(rating__gte=4).count() == 2
        assert len(opened) == pool.size
    finally:
        set_runner(previous)
        await router.close()
    assert all(thread.startswith("adorm-django-") for thread in opened)
    assert set(levels) == {psycopg.IsolationLevel.SERIALIZABLE}


async def test_a_database_other_than_postgresql_is_refused(library, monkeypatch):  # noqa: ARG001
    monkeypatch.setattr(type(connections["default"]), "vendor", "sqlite")
    previous = set_runner(None)
    pool, router = await open_pool()
    try:
        with pytest.raises(NotSupportedError, match="supports PostgreSQL databases, not 'sqlite'"):
            await Author.objects.count()
        assert (pool.size, pool.in_use) == (0, 0)
    finally:
        set_runner(previous)
        await router.close()


def test_capabilities():
    assert READ_CAPABILITIES.operations == {"select", "get", "first", "last", "count", "exists", "aggregate", "in_bulk"}
    assert CAPABILITIES.operations == READ_CAPABILITIES.operations | {
        "create",
        "create_from_parent",
        "full_clean",
        "save",
        "update",
        "delete",
        "delete_instance",
        "get_or_create",
        "update_or_create",
    }
    assert {
        "lookup.in",
        "function.Lower",
        "function.Trunc",
        "function.TruncMonth",
        "aggregate.Count",
        "aggregate.filter",
        "clause.alias",
        "clause.group_by",
        "expression.Case",
        "expression.Exists",
        "expression.OuterRef",
        "expression.Subquery",
        "expression.CombinedExpression",
    } <= READ_CAPABILITIES.constructs
    assert CAPABILITIES.constructs - READ_CAPABILITIES.constructs == {
        "clause.select_for_update",
        "clause.select_for_update.nowait",
        "clause.select_for_update.skip_locked",
        "clause.select_for_update.of",
        "clause.select_for_update.no_key",
    }
    assert CAPABILITIES.savepoints
    assert not READ_CAPABILITIES.savepoints
    assert CAPABILITIES.cancellation
    assert DjangoDriver.mode == "threaded"
