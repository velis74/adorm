"""The pool and router on real backends: Django ORM (threaded) and asyncpg (async)."""

import asyncio
import secrets
import threading

from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest

from adorm import create_session, OperationalError
from adorm.executor import Pool, Router
from tests.contract.drivers import AsyncpgDriver, DjangoDriver, Profile, RawQuery

ROLE = "adorm_test_limited"


class RefusalAwareDjangoDriver(DjangoDriver):
    """psycopg reports a refused connection without a SQLSTATE; PostgreSQL's message names the cause."""

    def is_refusal(self, exc):
        return super().is_refusal(exc) or (
            isinstance(exc, OperationalError) and "too many connections" in str(exc.original)
        )


@pytest.fixture
async def limited_role(test_database_url):
    """A login role allowed one connection; yields its password."""
    password = secrets.token_hex(16)
    conn = await asyncpg.connect(test_database_url)
    await conn.execute(f"DROP ROLE IF EXISTS {ROLE}")
    await conn.execute(f"CREATE ROLE {ROLE} LOGIN PASSWORD '{password}' CONNECTION LIMIT 1")
    try:
        yield password
    finally:
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = $1 AND pid <> pg_backend_pid()",
            ROLE,
        )
        await conn.execute(f"DROP ROLE IF EXISTS {ROLE}")
        await conn.close()


def as_role(url, password):
    parts = urlsplit(url)
    netloc = f"{ROLE}:{quote(password)}@{parts.hostname}:{parts.port or 5432}"
    return urlunsplit(parts._replace(netloc=netloc))


@pytest.fixture(params=["django", "asyncpg"])
def backend(request, test_database_url, django_db_blocker):
    """``make(max_connections, password=None)`` returns a router with one pool on the backend."""

    def make(max_connections=0, password=None):
        if request.param == "django":
            params = {} if password is None else {"USER": ROLE, "PASSWORD": password}
            pool = Pool(RefusalAwareDjangoDriver(), Profile("default", params=params), max_connections=max_connections)
        else:
            dsn = test_database_url if password is None else as_role(test_database_url, password)
            pool = Pool(AsyncpgDriver(), Profile("default", params={"dsn": dsn}), max_connections=max_connections)
        return Router([pool])

    with django_db_blocker.unblock():
        yield request.param, make


@pytest.fixture
async def router_of(backend):
    kind, make = backend
    made = []

    def build(*args, **kwargs):
        router = make(*args, **kwargs)
        made.append(router)
        return router

    yield kind, build
    for router in made:
        await router.close()


PID = RawQuery("SELECT pg_backend_pid() AS pid")


async def test_a_bounded_pool_serves_concurrent_operations_on_its_connections(router_of):
    _, build = router_of
    router = build(max_connections=2)
    rows = await asyncio.gather(*(router.run(create_session("default", f"s{i}"), PID) for i in range(12)))
    pids = {r[0]["pid"] for r in rows}
    pool = router.pool("default")
    assert len(pids) <= 2
    assert pool.size <= 2
    assert pool.idle == pool.size


async def test_each_threaded_slot_runs_on_its_own_thread(router_of):
    kind, build = router_of
    if kind != "django":
        pytest.skip("only a threaded driver has slot threads")
    router = build(max_connections=2)
    first = await router.acquire(create_session("default", "a"))
    second = await router.acquire(create_session("default", "b"))
    threads = {first.slot.connection.thread, second.slot.connection.thread}
    assert len(threads) == 2
    assert threading.current_thread() not in threads
    await first.release()
    await second.release()


async def test_a_transaction_on_a_lease_is_isolated_until_commit(router_of, test_database_url):
    _, build = router_of
    router = build(max_connections=2)
    monitor = await asyncpg.connect(test_database_url)
    await monitor.execute("DROP TABLE IF EXISTS pool_item")
    await monitor.execute("CREATE TABLE pool_item (name text)")
    try:
        async with router.slot(create_session("default", "writer")) as lease:
            await lease.call("begin")
            await lease.call("execute", RawQuery("INSERT INTO pool_item VALUES ('a')"))
            other = await router.run(
                create_session("default", "reader"), RawQuery("SELECT count(*) AS n FROM pool_item")
            )
            assert other == [{"n": 0}]
            await lease.call("commit")
        assert await monitor.fetchval("SELECT count(*) FROM pool_item") == 1
    finally:
        await monitor.execute("DROP TABLE IF EXISTS pool_item")
        await monitor.close()


@pytest.mark.parametrize("attempt", range(3))
async def test_a_database_refusal_queues_the_request_until_a_slot_returns(router_of, limited_role, attempt):  # noqa: ARG001
    _, build = router_of
    router = build(max_connections=0, password=limited_role)
    pool = router.pool("default")
    first = await router.acquire(create_session("default", "first"))
    second = asyncio.create_task(router.run(create_session("default", "second"), PID))
    async with asyncio.timeout(10):
        while pool.waiting != 1:
            await asyncio.sleep(0.01)
    assert pool.limit == 1
    pid = (await first.call("execute", PID))[0]["pid"]
    await first.release()
    assert (await second)[0]["pid"] == pid
    assert pool.size == 1
