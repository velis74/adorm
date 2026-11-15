"""Transactions on real backends: Django ORM (threaded) and asyncpg (async)."""

import asyncio
import itertools

import asyncpg
import pytest

from adorm import create_session, IntegrityError, TransactionClosed
from adorm.executor import Pool, Router, set_runner
from tests.contract.drivers import AsyncpgDriver, DjangoDriver, Profile, RawQuery

TABLE = "tx_item"
_names = itertools.count()


def session():
    return create_session("default", f"backend-tx-{next(_names)}")


def insert(name):
    return RawQuery(f"INSERT INTO {TABLE} (name) VALUES ($1)", (name,))


@pytest.fixture(params=["django", "asyncpg"])
def make_pool(request, test_database_url, django_db_blocker):
    def make(**kwargs):
        if request.param == "django":
            return Pool(DjangoDriver(), Profile("default"), **kwargs)
        return Pool(AsyncpgDriver(), Profile("default", params={"dsn": test_database_url}), **kwargs)

    with django_db_blocker.unblock():
        yield make


@pytest.fixture
async def monitor(test_database_url):
    """A connection outside adorm; ``rows()`` returns the committed names."""
    conn = await asyncpg.connect(test_database_url)
    await conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
    await conn.execute(f"CREATE TABLE {TABLE} (name text PRIMARY KEY)")

    async def rows():
        return sorted(r["name"] for r in await conn.fetch(f"SELECT name FROM {TABLE}"))

    try:
        yield rows
    finally:
        await conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
        await conn.close()


@pytest.fixture
async def runner(make_pool):
    routers = []

    def build(**kwargs):
        router = Router([make_pool(**kwargs)])
        routers.append(router)
        set_runner(router)
        return router

    previous = set_runner(None)
    try:
        yield build
    finally:
        set_runner(previous)
        for router in routers:
            await router.close()


async def test_commit_on_exit_is_visible_to_other_connections_only_then(runner, monitor):
    runner(max_connections=2)
    s = session()
    async with s.atomic():
        await s.execute(insert("a"))
        assert await monitor() == []
        other = await create_session("default", "reader").execute(RawQuery(f"SELECT count(*) AS n FROM {TABLE}"))
        assert other == [{"n": 0}]
    assert await monitor() == ["a"]


async def test_an_exception_rolls_back(runner, monitor):
    router = runner()
    s = session()
    with pytest.raises(KeyError):
        async with s.atomic():
            await s.execute(insert("a"))
            raise KeyError
    assert await monitor() == []
    assert router.pool("default").idle == 1


async def test_a_savepoint_rolls_back_alone(runner, monitor):
    runner()
    s = session()
    async with s.atomic():
        await s.execute(insert("a"))
        with pytest.raises(IntegrityError):
            async with s.atomic():
                await s.execute(insert("b"))
                await s.execute(insert("a"))
        await s.execute(insert("c"))
    assert await monitor() == ["a", "c"]


async def test_early_commit_is_visible_before_the_block_ends(runner, monitor):
    runner()
    s = session()
    async with s.atomic() as tx:
        await s.execute(insert("a"))
        await tx.commit()
        assert await monitor() == ["a"]
        with pytest.raises(TransactionClosed):
            await s.execute(insert("b"))
    assert await monitor() == ["a"]


async def test_operations_of_inheriting_tasks_are_committed_with_the_block(runner, monitor):
    router = runner(max_connections=1)
    s = session()
    async with s.atomic():
        await asyncio.gather(*(s.execute(insert(name)) for name in "abc"))
    assert await monitor() == ["a", "b", "c"]
    assert router.pool("default").size == 1


async def test_an_abandoned_transaction_is_rolled_back_by_the_slot(runner, monitor):
    router = runner(max_connections=1, commit_timeout=0.2)
    s = session()
    never = asyncio.Event()

    async def lost_client():
        async with s.atomic():
            await s.execute(insert("a"))
            await never.wait()

    lost = asyncio.create_task(lost_client())
    pool = router.pool("default")
    async with asyncio.timeout(10):
        while not (pool.size == 0 and pool.leases() == []):
            await asyncio.sleep(0.01)
    assert await monitor() == []
    await session().execute(insert("b"))
    assert await monitor() == ["b"]
    lost.cancel()
    with pytest.raises(asyncio.CancelledError):
        await lost


@pytest.mark.parametrize("attempt", range(3))
async def test_cancelling_a_block_during_a_long_statement_rolls_back_and_frees_the_slot(runner, monitor, attempt):  # noqa: ARG001
    router = runner(max_connections=1)
    s = session()
    started = asyncio.Event()

    async def block():
        async with s.atomic():
            await s.execute(insert("a"))
            started.set()
            await s.execute(RawQuery("SELECT pg_sleep(30)"))

    task = asyncio.create_task(block())
    await started.wait()
    await asyncio.sleep(0.05)
    task.cancel()
    async with asyncio.timeout(10):
        with pytest.raises(asyncio.CancelledError):
            await task
    assert await monitor() == []
    async with asyncio.timeout(10):
        await session().execute(insert("b"))
    assert await monitor() == ["b"]
    assert router.pool("default").size == 1


async def test_on_commit_callbacks_see_the_committed_rows_and_rollbacks_drop_them(runner, monitor):
    runner(max_connections=1)
    s = session()
    seen = []

    async def callback():
        seen.append(await monitor())
        seen.append(await s.execute(RawQuery(f"SELECT count(*) AS n FROM {TABLE}")))

    async with s.atomic() as tx:
        await s.execute(insert("a"))
        tx.on_commit(callback)
    assert seen == [["a"], [{"n": 1}]]

    async def rolled_back():
        async with s.atomic() as tx:
            await s.execute(insert("b"))
            tx.on_commit(lambda: seen.append("dropped"))
            raise RuntimeError("roll back")

    with pytest.raises(RuntimeError):
        await rolled_back()
    assert seen == [["a"], [{"n": 1}]]
    assert await monitor() == ["a"]
