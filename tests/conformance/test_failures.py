"""Every driver under the failures that matter in production: refusals racing reads for slots and transactions,
concurrent tasks on one transaction, cancellation of a waiting operation (also racing its completion) and connections
lost mid-operation. Each driver's `Blocker` makes an operation wait in the database deterministically."""

import asyncio

import pytest

import adorm

from adorm import (
    cancellation_outcome,
    CancellationOutcome,
    DatabaseError,
    DeadlockError,
    Exists,
    FieldError,
    NotSupportedError,
    OuterRef,
    TransactionOutcome,
)
from adorm.drivers import is_connection_error
from adorm.query import check_lookups, Count, Func, Length, Q, resolve_lookup
from demo.models.clubs import Club, Member, Membership

pytestmark = pytest.mark.django_db(transaction=True)

REPEAT = range(5)


class Unaccent(Func):
    """A function no driver declares."""

    arity = 1


def refused(driver):
    """A query set whose read the driver refuses: the first of a few that uses a construct it does not declare."""
    candidates = [
        Member.objects.filter(Exists(Membership.objects.filter(member_id=OuterRef("pk")))),
        Member.objects.annotate(n=Length("first_name")),
        Member.objects.filter(memberships__role="coach"),
        Member.objects.annotate(n=Unaccent("first_name")),
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


# Lookup keys built at run time

UNRESOLVED = ["frist_name__icontains", "nickname", "first_name__nope", "birth_date__year", "memberships__regex"]
"""Keys a dynamic filter may build that the model does not resolve: a misspelled field, an unknown field, an unknown
lookup name, a transform after a scalar field and a lookup a relation does not take; every driver reports them as
`FieldError`."""


def unresolved(n):
    """An unresolvable dynamic filter."""
    return Member.objects.filter(**{UNRESOLVED[n % len(UNRESOLVED)]: "x"})


async def test_an_unresolvable_key_raises_field_error_and_keeps_the_slot(make_router):
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]
    for n in range(3 * len(REPEAT)):
        with pytest.raises(FieldError):
            await unresolved(n).count()
        assert (pool.size, pool.in_use) == (1, 0)
        assert await Member.objects.filter(**{"level__in": ["advanced"]}).count() == 4
    assert pool.size == 1


async def test_unresolvable_keys_inside_a_transaction_leave_it_usable(pool):
    for n in REPEAT:
        async with adorm.atomic():
            with pytest.raises(FieldError):
                await unresolved(n).count()
            assert await Club.objects.filter(**{"slug__exact": "koper-sailing"}).update(city=f"Koper {n}") == 1
            with pytest.raises(FieldError):
                await unresolved(n + 1).first()
        assert (await Club.objects.get(slug="koper-sailing")).city == f"Koper {n}"
    assert (pool.size, pool.in_use) == (1, 0)


async def test_unresolvable_and_valid_dynamic_filters_racing_in_one_transaction(pool):
    for _ in REPEAT:
        async with adorm.atomic():
            results = await asyncio.gather(
                *(
                    Member.objects.filter(**{"level__iexact": "ADVANCED"}).count() if n % 2 else unresolved(n).count()
                    for n in range(20)
                ),
                return_exceptions=True,
            )
            assert results[1::2] == [4] * 10
            assert all(isinstance(result, FieldError) for result in results[::2])
            assert await Club.objects.count() == 3
    assert (pool.size, pool.in_use) == (1, 0)


async def test_checking_keys_first_needs_no_slot(make_router):
    pool = make_router(max_connections=1, acquire_timeout=30).pools[0]
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
        for key in UNRESOLVED:
            with pytest.raises(FieldError):
                resolve_lookup(Member, key)
            with pytest.raises(FieldError):
                check_lookups(Member.objects.all(), Q((key, "x")) | Q(level="advanced"))
        assert pool.waiting == 0
    finally:
        done.set()
        await holder
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


async def _recorded(awaitable, raised):
    try:
        return await awaitable
    except BaseException as exc:
        raised.append(exc)
        raise


async def _city(slug="koper-sailing"):
    return (await Club.objects.get(slug=slug)).city


async def test_a_write_cancelled_racing_completion_reports_its_effect(make_router, blocker):
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]
    seen = set()
    try:
        for attempt in range(120):
            city = f"Race {attempt}"
            await blocker.hold()
            raised = []
            write = Club.objects.filter(slug="koper-sailing").update(city=city)
            task = asyncio.create_task(_recorded(write, raised))
            await blocker.waiting()
            await blocker.release()
            for _ in range(attempt % 15 if attempt < 90 else (attempt % 15) ** 3):
                if task.done():
                    break
                await asyncio.sleep(0)
            task.cancel()
            try:
                assert await task == 1
                outcome = None
            except asyncio.CancelledError:
                outcome = cancellation_outcome(raised[0])
            seen.add(outcome)
            assert pool.in_use == 0
            await blocker.transactions_ended()
            applied = await _city() == city
            if outcome in (None, CancellationOutcome.SUCCEEDED):
                assert applied, outcome
            elif outcome is not CancellationOutcome.UNKNOWN:
                assert not applied, outcome
    finally:
        await Club.objects.filter(slug="koper-sailing").update(city="Koper")
    assert None in seen
    assert seen - {None}, "no attempt was cancelled"
    assert seen <= {None, CancellationOutcome.SUCCEEDED, CancellationOutcome.UNKNOWN}
    assert pool.size <= 1


async def test_a_block_cancelled_racing_its_commit_reports_the_outcome(make_router, blocker):
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]
    seen = set()
    try:
        for attempt in range(80):
            city = f"Commit {attempt}"
            block = adorm.atomic()
            ended = asyncio.Event()

            async def run(block=block, city=city, ended=ended):
                async with block:
                    await Club.objects.filter(slug="koper-sailing").update(city=city)
                    ended.set()

            task = asyncio.create_task(run())
            await ended.wait()
            for _ in range(attempt % 20 if attempt < 60 else (attempt % 20) ** 3):
                if task.done():
                    break
                await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            outcome = block.outcome
            seen.add(outcome)
            assert outcome is not None
            assert pool.in_use == 0
            await blocker.transactions_ended()
            applied = await _city() == city
            if outcome is TransactionOutcome.COMMITTED:
                assert applied
            elif outcome is TransactionOutcome.ROLLED_BACK:
                assert not applied
    finally:
        await Club.objects.filter(slug="koper-sailing").update(city="Koper")
    assert TransactionOutcome.COMMITTED in seen
    assert seen - {TransactionOutcome.COMMITTED}, "no commit was cancelled"
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


# Aggregation


def _aggregated(driver, slug="koper-sailing"):
    """An aggregate over the clubs: it waits while a `TableLock` holds; skips a driver without ``aggregate``."""
    if "aggregate" not in driver.capabilities.operations:
        pytest.skip(f"the {driver.name} driver does not run aggregate operations")
    return Club.objects.filter(slug=slug).aggregate(n=Count("*"), cities=Count("city", distinct=True))


async def test_an_aggregate_cancelled_racing_completion_leaves_the_slot_usable(driver, make_router, blocker):
    _aggregated(driver).close()
    if not blocker.lost_connections:
        pytest.skip("the blocker does not hold a table lock aggregates wait for")
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]
    outcomes = set()
    for attempt in range(60):
        await blocker.hold()
        task = asyncio.create_task(_aggregated(driver))
        await blocker.waiting()
        await blocker.release()
        for _ in range(attempt % 15 if attempt < 45 else (attempt % 15) ** 3):
            if task.done():
                break
            await asyncio.sleep(0)
        task.cancel()
        try:
            outcomes.add(tuple(sorted((await task).items())))
        except asyncio.CancelledError:
            outcomes.add("cancelled")
        assert pool.in_use == 0
        await blocker.transactions_ended()
    async with asyncio.timeout(5):
        assert await _aggregated(driver) == {"n": 1, "cities": 1}
    assert outcomes == {(("cities", 1), ("n", 1)), "cancelled"}
    assert pool.size == 1


async def test_concurrent_aggregates_in_tasks_of_one_transaction(driver, make_router):
    _aggregated(driver).close()
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]
    slugs = ["koper-sailing", "ljubljana-rowing", "maribor-climbing", "nowhere"]
    for _ in REPEAT:
        async with adorm.atomic():
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(_aggregated(driver, slugs[n % 4])) for n in range(24)]
                counted = group.create_task(Membership.objects.aggregate(rows=Count("*")))
            assert pool.in_use == 1
        assert [task.result()["n"] for task in tasks] == [[1, 1, 1, 0][n % 4] for n in range(24)]
        assert counted.result() == {"rows": 13}
    assert (pool.size, pool.in_use) == (1, 0)


async def _update_then_fail():
    async with adorm.atomic():
        await Club.objects.filter(slug="koper-sailing").update(city="Rolled back")
        await Club.objects.aggregate(n=Count("no_such_field"))


async def test_a_failing_aggregate_inside_a_transaction_rolls_back_and_frees_the_slot(driver, make_router):
    _aggregated(driver).close()
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]
    for _ in REPEAT:
        with pytest.raises(adorm.DatabaseError):
            await _update_then_fail()
        assert pool.in_use == 0
        assert (await Club.objects.get(slug="koper-sailing")).city == "Koper"
    assert pool.size <= 1
