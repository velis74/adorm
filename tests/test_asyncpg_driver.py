"""The native PostgreSQL driver: what is particular to it. The reads it shares with every driver are in
``tests/conformance``."""

import asyncio
import datetime
import threading

import asyncpg
import pytest

from django.db import connections

import adorm

from adorm import ConnectionProfile, InterfaceError, NotSupportedError, OperationalError, ProgrammingError
from adorm.drivers import is_connection_error
from adorm.drivers.asyncpg import (
    AsyncpgConnection,
    AsyncpgDriver,
    CAPABILITIES,
    connect_arguments,
    ISOLATION_LEVELS,
    server_version_num,
)
from adorm.drivers.django import CAPABILITIES as DJANGO_CAPABILITIES, READ_CAPABILITIES
from adorm.drivers.sql import POSTGRESQL_OPTIONS
from adorm.executor import CloseReason, Pool, Router, set_runner
from adorm.operations import Count, Select
from adorm.query import Query
from tests.djangoproject.library import models as dj
from tests.generated.library import Author, Book
from tests.library_data import in_thread

pytestmark = pytest.mark.django_db(transaction=True)

ENTRY = {"NAME": "lib", "USER": "app", "PASSWORD": "secret", "HOST": "db.example", "PORT": "6432", "OPTIONS": {}}


# Connection arguments


def test_connect_arguments_from_a_databases_entry():
    assert connect_arguments(ENTRY) == {
        "database": "lib",
        "user": "app",
        "password": "secret",
        "host": "db.example",
        "port": 6432,
    }
    assert connect_arguments({"NAME": None}) == {"database": "postgres"}
    assert connect_arguments({"NAME": "", "USER": "", "HOST": "", "PORT": ""}) == {}
    assert connect_arguments({"NAME": "lib"}, time_zone="UTC") == {
        "database": "lib",
        "server_settings": {"TimeZone": "UTC"},
    }


def test_connect_arguments_from_options():
    options = {
        "options": "-c application_name=reports --statement-timeout=5s -csearch_path=app",
        "connect_timeout": 3,
        "sslmode": "require",
        "application_name": "adorm",
        "assume_role": "reader",
        "isolation_level": 4,
        "server_side_binding": True,
        "pool": {"min_size": 2},
        "prepare_threshold": None,
        "cursor_factory": object,
    }
    arguments = connect_arguments({"NAME": "lib", "OPTIONS": options}, time_zone="Europe/Ljubljana")
    assert arguments == {
        "database": "lib",
        "timeout": 3.0,
        "server_settings": {
            "application_name": "reports",
            "statement_timeout": "5s",
            "search_path": "app",
            "TimeZone": "Europe/Ljubljana",
        },
        "dsn": "postgresql://?sslmode=require&application_name=adorm",
    }


@pytest.mark.parametrize("options", ["-c", "-c broken", "--x", "plain", "-c =1"])
def test_connect_arguments_refuse_libpq_options_other_than_settings(options):
    with pytest.raises(NotSupportedError, match="only -c name=value"):
        connect_arguments({"NAME": "lib", "OPTIONS": {"options": options}})


@pytest.mark.parametrize(
    "key", ["keepalives", "tcp_user_timeout", "gssencmode", "hostaddr", "fallback_application_name"]
)
def test_connect_arguments_refuse_libpq_parameters_asyncpg_does_not_read(key):
    # asyncpg would send them to the server as settings, which refuses the connection
    with pytest.raises(NotSupportedError, match=repr(key)):
        connect_arguments({"NAME": "lib", "OPTIONS": {key: "1", "sslmode": "disable"}})


@pytest.mark.parametrize("level", [0, 5, "serializable", None])
def test_connect_arguments_refuse_an_invalid_isolation_level(level):
    with pytest.raises(ProgrammingError, match="invalid transaction isolation level"):
        connect_arguments({"NAME": "lib", "OPTIONS": {"isolation_level": level}})


def test_isolation_levels_follow_psycopg():
    from psycopg import IsolationLevel

    assert {int(level): ISOLATION_LEVELS[int(level)] for level in IsolationLevel} == {
        level.value: level.name.replace("_", " ") for level in IsolationLevel
    }


def test_server_version_num():
    from asyncpg.serverversion import split_server_version_string

    assert server_version_num(split_server_version_string("17.6 (Debian 17.6-1.pgdg13+1)")) == 170006
    assert server_version_num(split_server_version_string("14.0")) == 140000
    assert server_version_num(split_server_version_string("9.6.24")) == 90624


def test_capabilities():
    assert CAPABILITIES.operations == DJANGO_CAPABILITIES.operations
    assert CAPABILITIES.operations > READ_CAPABILITIES.operations
    assert CAPABILITIES.constructs == DJANGO_CAPABILITIES.constructs
    assert "clause.select_for_update.skip_locked" in CAPABILITIES.constructs
    assert CAPABILITIES.cancellation
    assert CAPABILITIES.savepoints
    assert CAPABILITIES.connection_options == POSTGRESQL_OPTIONS == DJANGO_CAPABILITIES.connection_options
    assert (AsyncpgDriver.name, AsyncpgDriver.mode) == ("asyncpg", "async")


# Pools


async def open_pool(**profile):
    driver = AsyncpgDriver()
    pool = Pool(driver, ConnectionProfile("default", driver, **profile), max_connections=2)
    router = Router([pool])
    set_runner(router)
    return pool, router


@pytest.fixture
async def run_pool():
    """``await make(**profile)`` sets up a router with a pool of `AsyncpgDriver` slots for the profile ``default``."""
    routers = []

    async def make(**profile):
        pool, router = await open_pool(**profile)
        routers.append(router)
        return pool

    previous = set_runner(None)
    try:
        yield make
    finally:
        set_runner(previous)
        for router in routers:
            await router.close()


async def test_every_call_runs_on_the_event_loop_without_threads(library, run_pool, monkeypatch):  # noqa: ARG001
    pool = await run_pool()
    await asyncio.gather(*(Author.objects.count() for _ in range(4)))
    assert pool.size == 2
    threads = set()
    original = AsyncpgConnection._read

    async def read(self, operation):
        threads.add(threading.get_ident())
        return await original(self, operation)

    def no_thread(*_args, **_kwargs):
        raise AssertionError("a read used a thread")

    # Undone before the loop closes, which may start a thread to shut down its default executor.
    with monkeypatch.context() as patched:
        patched.setattr(AsyncpgConnection, "_read", read)
        patched.setattr(asyncio.get_running_loop(), "run_in_executor", no_thread)
        patched.setattr(threading.Thread, "start", no_thread)
        for _ in range(3):
            counts = await asyncio.gather(*(Author.objects.filter(rating__gte=n).count() for n in range(10)))
            assert counts == [4, 4, 3, 3, 2, 1, 0, 0, 0, 0]
            async with adorm.atomic():
                assert await Author.objects.order_by("name").first() is not None
    assert threads == {threading.get_ident()}
    assert (pool.size, pool.in_use) == (2, 0)


async def test_profile_params_and_hooks_reach_the_slots_connection(library, run_pool):  # noqa: ARG001
    shared = dict(connections.settings["default"])
    seen = []

    async def on_acquire(connection, profile, session):  # noqa: ARG001
        settings = connection.get_settings()  # reported by the server, without a statement
        seen.append((settings.application_name, settings.TimeZone))

    def on_release(connection, profile, session):  # noqa: ARG001
        seen.append(type(connection))

    params = {"OPTIONS": {"options": "-c application_name=adorm_reports"}, "TIME_ZONE": "Europe/Ljubljana"}
    pool = await run_pool(params=params, on_acquire=on_acquire, on_release=on_release)
    published = datetime.datetime(2024, 3, 1, 12, 30, tzinfo=datetime.UTC)
    await in_thread(lambda: dj.Book.objects.filter(isbn="9780000000001").update(published=published))
    book = await Book.objects.get(isbn="9780000000001")
    assert book.published == published
    assert (str(book.published.tzinfo), book.published.hour) == ("Europe/Ljubljana", 13)
    async with adorm.atomic():
        assert await Author.objects.filter(rating__gte=4).count() == 2
    assert seen == [("adorm_reports", "Europe/Ljubljana"), asyncpg.Connection] * 2
    assert connections.settings["default"] == shared
    assert pool.size == 1


@pytest.mark.parametrize("hook", ["on_acquire", "on_release"])
async def test_sql_a_profile_hook_runs_is_custom_and_the_slot_leaves_the_pool(library, run_pool, hook):  # noqa: ARG001
    async def run_sql(connection, profile, session):  # noqa: ARG001
        await connection.execute("SET application_name TO 'adorm hook'")

    pool = await run_pool(**{hook: run_sql})
    for _ in range(3):
        async with adorm.atomic() as tx:
            assert await Author.objects.count() == 4
            slot = tx.lease.slot
        await asyncio.wait_for(slot.wait_closed(), 10)
        assert slot.close_reason is CloseReason.CUSTOM_SQL
    assert pool.size == 0


async def test_options_from_a_hook_are_not_custom_sql(library, run_pool):  # noqa: ARG001
    def on_acquire(connection, profile, session):  # noqa: ARG001
        return {"adorm_test.app_user": "alice", "statement_timeout": 7}

    pool = await run_pool(on_acquire=on_acquire, on_release=lambda *_: None)
    for _ in range(3):
        async with adorm.atomic() as tx:
            assert await Author.objects.count() == 4
            conn = tx.lease.slot.connection.conn
            # The slot runs nothing while the block awaits this.
            assert await conn.fetchval("SELECT current_setting('adorm_test.app_user')") == "alice"
            assert await conn.fetchval("SHOW statement_timeout") == "7s"
    (slot,) = pool._slots
    assert slot.close_reason is None
    assert not slot.connection.ran_custom_sql


async def test_libpq_options_reach_asyncpg_through_its_dsn(library, run_pool):  # noqa: ARG001
    seen = []

    async def on_acquire(connection, profile, session):  # noqa: ARG001
        seen.append(await connection.fetchval("SHOW application_name"))

    options = {"sslmode": "disable", "application_name": "adorm_dsn", "connect_timeout": 5}
    await run_pool(params={"OPTIONS": options}, on_acquire=on_acquire)
    assert await Author.objects.count() == 4
    assert seen == ["adorm_dsn"]


async def test_the_connect_hook_opens_the_connection_and_the_driver_configures_it(library, test_database_url, run_pool):  # noqa: ARG001
    opened, levels = [], []

    async def connect(profile):
        opened.append(profile.name)
        # Another session time zone: the driver sets Django's.
        return await asyncpg.connect(test_database_url, server_settings={"TimeZone": "America/New_York"})

    def on_acquire(connection, profile, session):  # noqa: ARG001
        levels.append(connection)

    user = connections.settings["default"]["USER"]
    options = {**connections.settings["default"].get("OPTIONS", {}), "isolation_level": 4, "assume_role": user}
    pool = await run_pool(params={"OPTIONS": options}, connect=connect, on_acquire=on_acquire)
    counts = await asyncio.gather(*(Author.objects.filter(rating__gte=n).count() for n in range(10)))
    assert counts == [4, 4, 3, 3, 2, 1, 0, 0, 0, 0]
    assert len(opened) == pool.size
    conn = levels[-1]
    assert await conn.fetchval("SHOW TimeZone") == "UTC"
    assert await conn.fetchval("SHOW role") == user
    assert pool.options == {"role": user}
    async with adorm.atomic():
        assert await Author.objects.filter(rating__gte=4).count() == 2
        conn = levels[-1]
        # The slot runs nothing while the block awaits this; the transaction is the slot's.
        assert await conn.fetchval("SHOW transaction_isolation") == "serializable"
    assert await conn.fetchval("SHOW transaction_isolation") == "read committed"


async def test_a_connect_hook_returning_something_else_is_refused(library, run_pool):  # noqa: ARG001
    pool = await run_pool(connect=lambda profile: None)  # noqa: ARG005
    with pytest.raises(InterfaceError, match="not a connection"):
        await Author.objects.count()
    assert (pool.size, pool.in_use) == (0, 0)


async def test_a_database_other_than_postgresql_is_refused(library, run_pool):  # noqa: ARG001
    pool = await run_pool(params={"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"})
    with pytest.raises(NotSupportedError, match="supports PostgreSQL databases, not 'sqlite'"):
        await Author.objects.count()
    assert (pool.size, pool.in_use) == (0, 0)


async def test_an_unknown_alias_is_refused(library, run_pool):  # noqa: ARG001
    pool = await run_pool(alias="nowhere")
    with pytest.raises(ProgrammingError, match="DATABASES has no entry 'nowhere'"):
        await Author.objects.count()
    assert (pool.size, pool.in_use) == (0, 0)


async def test_a_failed_connect_leaves_no_slot(library, run_pool):  # noqa: ARG001
    pool = await run_pool(params={"NAME": "adorm_test_no_such_database"})
    for _ in range(3):
        with pytest.raises(OperationalError) as failed:
            await Author.objects.count()
        assert failed.value.sqlstate == "3D000"
        assert (pool.size, pool.in_use) == (0, 0)


async def test_the_compiler_uses_the_servers_version(library, test_database_url):  # noqa: ARG001
    driver = AsyncpgDriver()
    connection = await driver.open_connection(ConnectionProfile("default", driver), lambda _event: None)
    try:
        assert connection.compiler.server_version == int(await connection.conn.fetchval("SHOW server_version_num"))
    finally:
        await connection.close()


# A connection on its own


@pytest.fixture
async def connection(library, locker):  # noqa: ARG001
    """An open `AsyncpgConnection` of the profile ``default``, closed afterwards."""
    driver = AsyncpgDriver()
    conn = await driver.open_connection(ConnectionProfile("default", driver), lambda _event: None)
    try:
        yield conn
    finally:
        await conn.close()


COUNT = Count(Query("library.Author"))


async def test_cancel_from_another_thread_stops_the_running_statement(connection, locker):
    lock, unlock, waiting, _ = locker
    for _ in range(3):
        await lock()
        task = asyncio.create_task(connection.execute(COUNT))
        await waiting()
        await asyncio.to_thread(connection.cancel)
        with pytest.raises(OperationalError) as cancelled:
            await task
        assert cancelled.value.sqlstate == "57014"
        await waiting(present=False)
        await unlock()
        # The server may still hold the statement's transaction, so the slot must not reuse the connection.
        assert not connection.is_usable()


async def test_cancel_without_a_running_statement_does_nothing(connection):
    connection.cancel()
    await asyncio.to_thread(connection.cancel)
    for _ in range(5):
        await asyncio.sleep(0)
    assert await connection.execute(COUNT) == 4


async def test_a_cancel_arriving_after_its_statement_does_not_reach_the_next_one(connection, locker, monkeypatch):
    lock, unlock, waiting, _ = locker
    delayed = []
    loop = asyncio.get_running_loop()

    class Loop:
        """The connection's loop with ``call_soon_threadsafe()`` held back until the test releases it."""

        def call_soon_threadsafe(self, callback, *args):
            delayed.append((callback, args))

    monkeypatch.setattr(connection, "_loop", Loop())
    for _ in range(5):
        await lock()
        first = asyncio.create_task(connection.execute(COUNT))
        await waiting()
        connection.cancel()  # held back while the statement runs
        await unlock()
        assert await first == 4
        await lock()
        second = asyncio.create_task(connection.execute(COUNT))
        await waiting()
        for callback, args in delayed:
            loop.call_soon(callback, *args)
        delayed.clear()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not second.done()  # the late cancel found its own statement finished
        await unlock()
        assert await second == 4


@pytest.mark.parametrize("attempt", range(10))
async def test_a_cancelled_caller_cancels_its_statement_and_gives_the_connection_up(connection, locker, attempt):
    lock, unlock, waiting, _ = locker
    await lock()
    task = asyncio.create_task(connection.execute(COUNT))
    await waiting()
    if attempt % 2:
        await unlock()
        for _ in range(attempt % 4):
            await asyncio.sleep(0)
    task.cancel()
    try:
        finished = await task == 4
    except asyncio.CancelledError:
        finished = False
    if not attempt % 2:
        await waiting(present=False)
        await unlock()
    # A statement cancelled before it finished may leave its transaction open on the server.
    assert connection.is_usable() is finished
    if finished:
        assert await connection.execute(COUNT) == 4


@pytest.mark.parametrize("attempt", range(10))
async def test_a_terminated_backend_makes_the_connection_unusable(connection, locker, attempt):  # noqa: ARG001
    # asyncpg reports the next statement after an idle backend ends as an error or as InternalClientError
    _, _, _, monitor = locker
    pid = connection.conn.get_server_pid()
    await monitor.execute("SELECT pg_terminate_backend($1)", pid)
    async with asyncio.timeout(10):
        while await monitor.fetchval("SELECT count(*) FROM pg_stat_activity WHERE pid = $1", pid):
            await asyncio.sleep(0.01)
    with pytest.raises((OperationalError, InterfaceError)) as lost:
        await connection.execute(COUNT)
    assert is_connection_error(lost.value)
    assert not connection.is_usable()


async def test_a_statement_error_keeps_the_connection(connection):
    with pytest.raises(adorm.DataError) as invalid:
        await connection.execute(Select(Query("library.Author").filter(name__regex="(")))
    assert invalid.value.sqlstate == "2201B"
    assert connection.is_usable()
    assert await connection.execute(COUNT) == 4


async def test_a_transaction_ends_with_commit_or_rollback(connection):
    await connection.begin()
    assert connection.conn.is_in_transaction()
    with pytest.raises(adorm.DataError):
        await connection.execute(Select(Query("library.Author").filter(name__regex="(")))
    await connection.rollback()
    assert not connection.conn.is_in_transaction()
    await connection.begin()
    assert await connection.execute(COUNT) == 4
    await connection.commit()
    assert not connection.conn.is_in_transaction()
    assert connection.is_usable()


async def test_close_twice_and_use_after_close(connection):
    await connection.close()
    await connection.close()
    assert not connection.is_usable()
    with pytest.raises(InterfaceError, match="closed"):
        await connection.execute(COUNT)
