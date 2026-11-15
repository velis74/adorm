"""The driver contract on real backends: Django ORM (threaded) and asyncpg (async)."""

import asyncio
import threading
import uuid

import asyncpg
import pytest

from django.core.exceptions import SynchronousOnlyOperation
from django.db import DatabaseError as DjangoDatabaseError

from adorm.events import BackendEvent
from adorm.exceptions import IntegrityError, InterfaceError, NotSupportedError, OperationalError, ProgrammingError
from tests.contract.drivers import AsyncpgDriver, DjangoDirectDriver, DjangoDriver, Listen, Profile, RawQuery, Recorder
from tests.djangoproject.settings import TEST_DATABASE_NAME

TABLE = "contract_item"


@pytest.fixture
async def monitor(test_database_url):
    """A separate connection for setting up tables and watching the connections under test."""
    conn = await asyncpg.connect(test_database_url)
    await conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
    await conn.execute(f"CREATE TABLE {TABLE} (id serial PRIMARY KEY, name text UNIQUE NOT NULL)")
    try:
        yield conn
    finally:
        await conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
        await conn.close()


@pytest.fixture(params=["django", "asyncpg"])
def backend(request, test_database_url, django_db_blocker):
    if request.param == "django":
        with django_db_blocker.unblock():
            yield DjangoDriver(), Profile("default", alias="default")
    else:
        yield AsyncpgDriver(), Profile("default", params={"dsn": test_database_url})


@pytest.fixture
async def opened(backend, monitor):  # noqa: ARG001 - needs the table
    driver, profile = backend
    recorder = Recorder()
    conn = await driver.open_connection(profile, recorder)
    try:
        yield driver, conn, recorder
    finally:
        await conn.close()


async def names(monitor):
    return [r["name"] for r in await monitor.fetch(f"SELECT name FROM {TABLE} ORDER BY name")]


async def insert(conn, name):
    return await conn.execute(RawQuery(f"INSERT INTO {TABLE} (name) VALUES ($1)", (name,)))


ACTIVE = "SELECT count(*) FROM pg_stat_activity WHERE state = 'active' AND query LIKE $1 AND pid <> pg_backend_pid()"


async def wait_until_active(monitor, marker, timeout=10):
    async with asyncio.timeout(timeout):
        while not await monitor.fetchval(ACTIVE, f"%{marker}%"):
            await asyncio.sleep(0.01)


async def wait_until_idle(monitor, marker, timeout=10):
    async with asyncio.timeout(timeout):
        while await monitor.fetchval(ACTIVE, f"%{marker}%"):
            await asyncio.sleep(0.01)


def sleeper():
    marker = f"m{uuid.uuid4().hex}"
    return marker, RawQuery(f"SELECT pg_sleep(30) AS {marker}")


async def test_connection_reaches_test_database(opened):
    _, conn, _ = opened
    rows = await conn.execute(RawQuery("SELECT current_database() AS name"))
    assert rows[0]["name"].startswith(TEST_DATABASE_NAME)


async def test_rows_are_dicts_and_writes_return_counts(opened):
    _, conn, _ = opened
    assert await insert(conn, "a") == 1
    assert await conn.execute(RawQuery(f"SELECT name FROM {TABLE}")) == [{"name": "a"}]
    assert await conn.execute(RawQuery(f"UPDATE {TABLE} SET name = name || 'x'")) == 1


async def test_commit_persists(opened, monitor):
    _, conn, _ = opened
    await conn.begin()
    await insert(conn, "a")
    assert await names(monitor) == []
    await conn.commit()
    assert await names(monitor) == ["a"]


async def test_rollback_discards(opened, monitor):
    _, conn, _ = opened
    await conn.begin()
    await insert(conn, "a")
    await conn.rollback()
    assert await names(monitor) == []
    assert conn.is_usable()


async def test_savepoints(opened, monitor):
    _, conn, _ = opened
    await conn.begin()
    await insert(conn, "outer")
    await conn.savepoint("s1")
    await insert(conn, "discarded")
    await conn.rollback_to_savepoint("s1")
    await conn.savepoint("s2")
    await insert(conn, "kept")
    await conn.release_savepoint("s2")
    await conn.commit()
    assert await names(monitor) == ["kept", "outer"]


async def test_integrity_error_is_mapped_and_keeps_connection(opened):
    _, conn, _ = opened
    await insert(conn, "a")
    with pytest.raises(IntegrityError) as info:
        await insert(conn, "a")
    assert info.value.sqlstate == "23505"
    assert info.value.original is not None
    assert conn.is_usable()
    assert await conn.execute(RawQuery("SELECT 1 AS one")) == [{"one": 1}]


async def test_error_in_transaction_then_rollback(opened, monitor):
    _, conn, _ = opened
    await conn.begin()
    await insert(conn, "a")
    with pytest.raises(ProgrammingError) as info:
        await conn.execute(RawQuery("SELECT * FROM no_such_table"))
    assert info.value.sqlstate == "42P01"
    await conn.rollback()
    assert await names(monitor) == []
    assert await conn.execute(RawQuery("SELECT 1 AS one")) == [{"one": 1}]


async def test_unsupported_operation_and_options(opened):
    driver, conn, _ = opened

    class Select:
        kind = "select"

        def constructs(self):
            return ["lookup.icontains"]

    with pytest.raises(NotSupportedError, match="select, lookup.icontains"):
        await conn.execute(Select())
    with pytest.raises(NotSupportedError, match="work_mem"):
        await conn.set_options({"work_mem": "1MB"}, local=False)
    assert driver.capabilities.backend_events == {"notify"}


async def test_session_options(opened):
    _, conn, _ = opened
    await conn.set_options({"statement_timeout": 5}, local=False)
    assert await conn.execute(RawQuery("SHOW statement_timeout")) == [{"statement_timeout": "5s"}]


async def test_local_options_end_with_transaction(opened):
    _, conn, _ = opened
    await conn.set_options({"lock_timeout": 2}, local=False)
    await conn.begin()
    await conn.set_options({"lock_timeout": 0.5}, local=True)
    assert await conn.execute(RawQuery("SHOW lock_timeout")) == [{"lock_timeout": "500ms"}]
    await conn.commit()
    assert await conn.execute(RawQuery("SHOW lock_timeout")) == [{"lock_timeout": "2s"}]


async def test_statement_timeout_is_operational_error(opened):
    _, conn, _ = opened
    await conn.set_options({"statement_timeout": 0.05}, local=False)
    with pytest.raises(OperationalError) as info:
        await conn.execute(RawQuery("SELECT pg_sleep(5)"))
    assert info.value.sqlstate == "57014"
    assert conn.is_usable()


@pytest.mark.parametrize("repeat", range(3))
async def test_task_cancellation_cancels_running_statement(opened, monitor, repeat):  # noqa: ARG001
    _, conn, _ = opened
    marker, query = sleeper()
    task = asyncio.create_task(conn.execute(query))
    await wait_until_active(monitor, marker)
    task.cancel()
    async with asyncio.timeout(10):
        with pytest.raises(asyncio.CancelledError):
            await task
    assert conn.is_usable()
    assert await conn.execute(RawQuery("SELECT 1 AS one")) == [{"one": 1}]
    await wait_until_idle(monitor, marker)


async def test_task_cancellation_in_transaction_then_rollback(opened, monitor):
    _, conn, _ = opened
    await conn.begin()
    await insert(conn, "a")
    marker, query = sleeper()
    task = asyncio.create_task(conn.execute(query))
    await wait_until_active(monitor, marker)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await conn.rollback()
    assert await names(monitor) == []
    assert conn.is_usable()


async def test_cancel_from_another_thread(opened, monitor):
    _, conn, _ = opened
    marker, query = sleeper()
    task = asyncio.create_task(conn.execute(query))
    await wait_until_active(monitor, marker)
    canceller = threading.Thread(target=conn.cancel)
    canceller.start()
    with pytest.raises(OperationalError) as info:
        async with asyncio.timeout(10):
            await task
    canceller.join()
    assert info.value.sqlstate == "57014"
    assert conn.is_usable()
    assert await conn.execute(RawQuery("SELECT 1 AS one")) == [{"one": 1}]


async def test_cancel_when_idle_does_nothing(opened):
    _, conn, _ = opened
    conn.cancel()
    await asyncio.sleep(0)
    assert await conn.execute(RawQuery("SELECT 1 AS one")) == [{"one": 1}]


async def test_notify_arrives_as_backend_event_on_loop_thread(opened, monitor):
    _, conn, recorder = opened
    channel = f"c{uuid.uuid4().hex}"
    await conn.execute(Listen(channel))
    await monitor.execute("SELECT pg_notify($1, 'hello')", channel)
    async with asyncio.timeout(10):
        while not recorder.events:
            # A threaded connection receives notifications while it executes a statement.
            await conn.execute(RawQuery("SELECT 1 AS one"))
            await asyncio.sleep(0.01)
    assert recorder.events == [BackendEvent("notify", channel, "hello")]
    assert recorder.threads == [threading.get_ident()]


async def test_connection_lost_marks_unusable(opened, monitor):
    _, conn, _ = opened
    pid = (await conn.execute(RawQuery("SELECT pg_backend_pid() AS pid")))[0]["pid"]
    await monitor.execute("SELECT pg_terminate_backend($1)", pid)

    async def until_error():
        async with asyncio.timeout(10):
            while True:
                await conn.execute(RawQuery("SELECT 1 AS one"))
                await asyncio.sleep(0.01)

    with pytest.raises((OperationalError, InterfaceError)):
        await until_error()
    assert not conn.is_usable()


async def test_close_ends_backend_and_rejects_calls(opened, monitor):
    _, conn, _ = opened
    pid = (await conn.execute(RawQuery("SELECT pg_backend_pid() AS pid")))[0]["pid"]
    await conn.close()
    await conn.close()
    assert not conn.is_usable()
    with pytest.raises(InterfaceError):
        await conn.execute(RawQuery("SELECT 1"))
    async with asyncio.timeout(10):
        while await monitor.fetchval("SELECT count(*) FROM pg_stat_activity WHERE pid = $1", pid):
            await asyncio.sleep(0.01)


async def test_connections_are_independent(backend, monitor):  # noqa: ARG001 - needs the table
    driver, profile = backend
    first = await driver.open_connection(profile, Recorder())
    second = await driver.open_connection(profile, Recorder())
    try:
        query = RawQuery("SELECT pg_backend_pid() AS pid")
        assert (await first.execute(query)) != (await second.execute(query))
        await first.begin()
        await insert(first, "a")
        assert await second.execute(RawQuery(f"SELECT name FROM {TABLE}")) == []
        await first.commit()
        assert await second.execute(RawQuery(f"SELECT name FROM {TABLE}")) == [{"name": "a"}]
    finally:
        await first.close()
        await second.close()


async def test_django_calls_run_on_one_dedicated_thread(test_database_url, django_db_blocker):  # noqa: ARG001
    with django_db_blocker.unblock():
        conn = await DjangoDriver().open_connection(Profile("default"), Recorder())
        try:
            assert conn.thread is not threading.current_thread()
            assert conn.thread.name.startswith("adorm-django-raw-")
            await conn.execute(RawQuery("SELECT 1"))
            # Django refuses to use a connection from a thread other than the one that created it.
            with pytest.raises(DjangoDatabaseError, match="thread"):
                conn.db.validate_thread_sharing()
        finally:
            await conn.close()
        conn.thread.join(timeout=10)
        assert not conn.thread.is_alive()


@pytest.mark.parametrize("backend_name", ["django", "asyncpg"])
async def test_open_failure_is_mapped(test_database_url, django_db_blocker, backend_name):
    if backend_name == "django":
        driver, profile = DjangoDriver(), Profile("default", params={"NAME": "adorm_no_such_database"})
    else:
        bad = test_database_url.rsplit("/", 1)[0] + "/adorm_no_such_database"
        driver, profile = AsyncpgDriver(), Profile("default", params={"dsn": bad})
    before = set(threading.enumerate())
    with django_db_blocker.unblock(), pytest.raises(OperationalError) as info:
        await driver.open_connection(profile, Recorder())
    assert info.value.original is not None
    new = [t for t in threading.enumerate() if t not in before and t.name.startswith("adorm-")]
    for thread in new:
        thread.join(10)
    assert not any(t.is_alive() for t in new)


async def test_profile_connect_hook_runs_on_the_connection_thread(test_database_url, django_db_blocker):  # noqa: ARG001
    threads = []

    def connect(profile):
        from django.db import connections

        threads.append(threading.get_ident())
        wrapper = connections[profile.alias]
        return type(wrapper).get_new_connection(wrapper, wrapper.get_connection_params())

    with django_db_blocker.unblock():
        conn = await DjangoDriver().open_connection(Profile("default", connect=connect), Recorder())
        try:
            assert await conn.execute(RawQuery("SELECT 1 AS one")) == [{"one": 1}]
            assert threads == [conn.thread.ident]
        finally:
            await conn.close()


async def test_profile_connect_hook_is_awaited_by_async_driver(test_database_url):
    calls = []

    def connect(profile):
        calls.append(profile.name)
        return asyncpg.connect(test_database_url)

    conn = await AsyncpgDriver().open_connection(Profile("hooked", connect=connect), Recorder())
    try:
        assert await conn.execute(RawQuery("SELECT 1 AS one")) == [{"one": 1}]
        assert calls == ["hooked"]
    finally:
        await conn.close()


def run_in_own_thread(scenario):
    """Run ``scenario()`` in a fresh thread with its own event loop, as a Celery worker task would."""
    outcome = {}

    def target():
        try:
            outcome["result"] = asyncio.run(scenario())
        except BaseException as exc:  # noqa: BLE001 - re-raised in the test
            outcome["error"] = exc

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(30)
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


async def test_django_direct_runs_on_the_loop_thread(test_database_url, monitor, django_db_blocker):
    marker, query = sleeper()

    def cancel_when_active(conn):
        async def watch():
            watcher = await asyncpg.connect(test_database_url)
            try:
                await wait_until_active(watcher, marker)
            finally:
                await watcher.close()

        asyncio.run(watch())
        conn.cancel()

    async def scenario():
        conn = await DjangoDirectDriver().open_connection(Profile("default"), Recorder())
        try:
            assert conn.thread is threading.current_thread()
            await conn.begin()
            await insert(conn, "a")
            await conn.commit()
            with pytest.raises(IntegrityError):
                await insert(conn, "a")
            assert conn.is_usable()
            # Outside adorm's calls Django still refuses use from an event loop.
            with pytest.raises(SynchronousOnlyOperation):
                conn.db.cursor()
            canceller = threading.Thread(target=cancel_when_active, args=(conn,))
            canceller.start()
            with pytest.raises(OperationalError) as info:
                await conn.execute(query)
            canceller.join(10)
            assert info.value.sqlstate == "57014"
            assert conn.is_usable()
        finally:
            await conn.close()

    with django_db_blocker.unblock():
        await asyncio.to_thread(run_in_own_thread, scenario)
    assert await names(monitor) == ["a"]
