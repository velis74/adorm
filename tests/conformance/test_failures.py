"""Every driver under the failures that matter in production: refusals racing reads for slots and transactions,
concurrent tasks on one transaction, cancellation of a waiting operation (also racing its completion) and connections
lost mid-operation. Each driver's `Blocker` makes an operation wait in the database deterministically."""

import asyncio

import pytest

import adorm

from adorm import DatabaseError, DeadlockError, Exists, NotSupportedError, OuterRef
from adorm.drivers import is_connection_error
from adorm.query import Length
from demo.models.clubs import Club, Member, Membership

pytestmark = pytest.mark.django_db(transaction=True)

REPEAT = range(5)


def refused(driver):
    """A query set whose read the driver refuses: the first of a few that uses a construct it does not declare."""
    candidates = [
        Member.objects.filter(Exists(Membership.objects.filter(member_id=OuterRef("pk")))),
        Member.objects.annotate(n=Length("first_name")),
        Member.objects.filter(memberships__role="coach"),
    ]
    for queryset in candidates:
        if driver.capabilities.unsupported(_select(queryset)):
            return queryset
    raise AssertionError(f"the {driver.name} driver declares every construct of the candidates")


def _select(queryset):
    from adorm.operations import Select

    return Select(queryset.query)


async def read(queryset):
    return [row async for row in queryset]


# Refusals


async def test_a_refusal_does_not_wait_for_a_slot(driver, make_router):
    pool = make_router(max_connections=1, acquire_timeout=30).pools[0]
    query = refused(driver)
    holding = asyncio.Event()
    done = asyncio.Event()

    async def hold_the_only_slot():
        async with adorm.atomic():
            assert await Club.objects.count() == 3
            holding.set()
            await done.wait()

    holder = asyncio.create_task(hold_the_only_slot())
    try:
        await holding.wait()
        for _ in REPEAT:
            async with asyncio.timeout(5):
                with pytest.raises(NotSupportedError) as raised:
                    await query.count()
            assert raised.value.driver == driver.name
            assert pool.waiting == 0
    finally:
        done.set()
        await holder
    assert (pool.size, pool.in_use) == (1, 0)


async def test_concurrent_refusals_open_no_slot(driver, pool):
    query = refused(driver)
    for _ in REPEAT:
        results = await asyncio.gather(*(query.count() for _ in range(20)), return_exceptions=True)
        assert all(isinstance(result, NotSupportedError) for result in results)
    assert pool.size == 0
    assert await Club.objects.count() == 3


async def test_a_refusal_inside_a_transaction_leaves_it_usable(driver, pool):
    query = refused(driver)
    for _ in REPEAT:
        async with adorm.atomic():
            with pytest.raises(NotSupportedError):
                await read(query)
            assert await Club.objects.filter(city="Koper").count() == 1
            with pytest.raises(NotSupportedError):
                await query.first()
            assert await Member.objects.count() == 12
    assert (pool.size, pool.in_use) == (1, 0)


async def test_refusals_and_reads_racing_in_one_transaction(driver, pool):
    query = refused(driver)
    for _ in REPEAT:
        async with adorm.atomic():
            results = await asyncio.gather(
                *(Member.objects.filter(level="advanced").count() if n % 2 else query.count() for n in range(20)),
                return_exceptions=True,
            )
            assert results[1::2] == [4] * 10
            assert all(isinstance(result, NotSupportedError) for result in results[::2])
            assert await Club.objects.count() == 3
    assert (pool.size, pool.in_use) == (1, 0)


# Concurrency


async def test_concurrent_reads_on_a_small_pool(make_router):
    pool = make_router(max_connections=3, acquire_timeout=10).pools[0]
    levels = ["beginner", "intermediate", "advanced"]
    for _ in REPEAT:
        counts = await asyncio.gather(*(Member.objects.filter(level=levels[n % 3]).count() for n in range(40)))
        assert counts == [[4, 4, 4][n % 3] for n in range(40)]
    assert pool.size <= 3
    assert pool.in_use == 0


async def test_tasks_inherit_the_transactions_slot(make_router):
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]
    for _ in REPEAT:
        async with adorm.atomic():
            async with asyncio.TaskGroup() as group:
                counts = [group.create_task(Membership.objects.filter(active=n % 2 == 0).count()) for n in range(20)]
                first = group.create_task(Member.objects.order_by("first_name").first())
            gathered = await asyncio.gather(*(Club.objects.filter(city=city).count() for city in ("Koper", "Rome")))
            assert pool.in_use == 1
        assert [task.result() for task in counts] == [[12, 1][n % 2] for n in range(20)]
        assert first.result().first_name == "Ana"
        assert gathered == [1, 0]
    assert (pool.size, pool.in_use) == (1, 0)


async def test_a_second_session_inside_a_transaction_on_a_full_pool_is_a_deadlock(make_router):
    pool = make_router(max_connections=1, acquire_timeout=30).pools[0]
    other = adorm.create_session("default", "other")
    for _ in REPEAT:
        async with adorm.atomic():
            async with asyncio.timeout(5):
                with pytest.raises(DeadlockError) as raised:
                    await other.bind(Club).count()
            assert raised.value.held_sessions == (adorm.current_session(),)
            assert await Club.objects.count() == 3
    assert (pool.size, pool.in_use) == (1, 0)


# Cancellation


async def test_cancelling_a_waiting_operation_returns_its_slot(make_router, blocker):
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]
    assert await Club.objects.count() == 3
    for _ in range(3):
        await blocker.hold()
        task = asyncio.create_task(blocker.blocked("koper-sailing"))
        await blocker.waiting()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await blocker.idle()  # the operation no longer waits, though the hold lasts
        assert pool.in_use == 0
        await blocker.release()
        async with asyncio.timeout(5):
            await blocker.blocked("koper-sailing")
    assert pool.size == 1


async def test_cancellation_racing_completion_leaves_the_slot_usable(make_router, blocker):
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]
    outcomes = set()
    for attempt in range(150):
        await blocker.hold()
        task = asyncio.create_task(blocker.blocked("koper-sailing"))
        await blocker.waiting()
        await blocker.release()
        # Cancel after 0 to 14 loop iterations, then after up to 2744, or once the operation has finished.
        for _ in range(attempt % 15 if attempt < 120 else (attempt % 15) ** 3):
            if task.done():
                break
            await asyncio.sleep(0)
        task.cancel()
        try:
            outcomes.add(await task)
        except asyncio.CancelledError:
            outcomes.add("cancelled")
        assert pool.in_use == 0
        # The released slot keeps no transaction and no lock open, before anything else runs on it.
        await blocker.transactions_ended()
        async with asyncio.timeout(5):
            await blocker.hold()
        await blocker.release()
    async with asyncio.timeout(5):
        assert await Club.objects.filter(slug="koper-sailing").count() == 1
    assert outcomes == {1, "cancelled"}
    assert pool.size == 1


async def test_cancelling_an_operation_inside_a_transaction_rolls_it_back(make_router, blocker):
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]

    async def in_transaction():
        async with adorm.atomic():
            assert await Member.objects.count() == 12
            return await blocker.blocked("koper-sailing")

    for _ in REPEAT:
        await blocker.hold()
        task = asyncio.create_task(in_transaction())
        await blocker.waiting()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await blocker.release()
        assert pool.in_use == 0
        await blocker.transactions_ended()
        async with asyncio.timeout(5):
            async with adorm.atomic():
                assert await blocker.blocked("koper-sailing") == 1
    assert pool.size <= 1


# Lost connections


async def test_a_connection_lost_mid_operation_is_dropped(make_router, blocker):
    if not blocker.lost_connections:
        pytest.skip("the driver has no server connection to lose")
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]
    for _ in range(3):
        await blocker.hold()
        task = asyncio.create_task(blocker.blocked("koper-sailing"))
        await blocker.terminate(await blocker.waiting())
        with pytest.raises(DatabaseError) as lost:
            await task
        assert is_connection_error(lost.value)
        assert (pool.size, pool.in_use) == (0, 0)
        await blocker.release()
        assert await blocker.blocked("koper-sailing") == 1
        assert pool.size == 1


async def test_a_connection_lost_inside_a_transaction_ends_it(make_router, blocker):
    if not blocker.lost_connections:
        pytest.skip("the driver has no server connection to lose")
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]

    async def in_transaction():
        async with adorm.atomic():
            return await blocker.blocked("koper-sailing")

    for _ in range(3):
        await blocker.hold()
        task = asyncio.create_task(in_transaction())
        await blocker.terminate(await blocker.waiting())
        with pytest.raises(DatabaseError) as lost:
            await task
        assert is_connection_error(lost.value)
        await blocker.release()
        assert (pool.size, pool.in_use) == (0, 0)
        await blocker.transactions_ended()
        async with adorm.atomic():
            assert await Club.objects.count() == 3
        assert (pool.size, pool.in_use) == (1, 0)
