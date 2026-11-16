"""Set operations on every driver: concurrent combined reads, combined reads in tasks of one transaction, a failing
combined read, refusals that need no slot, cancellation racing completion and a connection lost mid-operation, and
combinations the conformance catalogue does not show (other models, subqueries, nesting)."""

import asyncio

import pytest

import adorm

from adorm import DatabaseError, Exists, NotSupportedError, OuterRef, ProgrammingError
from adorm.drivers import is_connection_error
from adorm.query import Count
from tests.clubmodels.clubs import Club, Member, Membership
from tests.conformance.cases import COMPOUND_SLICES
from tests.conformance.drivers import on_sqlite

pytestmark = pytest.mark.django_db(transaction=True)

REPEAT = range(5)
LEVELS = ["beginner", "intermediate", "advanced"]


def _levels(*levels):
    return [Member.objects.filter(level=level).values_list("first_name", flat=True) for level in levels]


def _combined(n):
    """One of three combined reads of member names, by ``n``, and its sorted result."""
    beginners, advanced = _levels("beginner", "advanced")
    coaches = Member.objects.filter(tags__contains=["coach"]).values_list("first_name", flat=True)
    if n % 3 == 0:
        return beginners.union(advanced), ["Ana", "Bojan", "David", "Eva", "Gaja", "Igor", "Jana", "Lara"]
    if n % 3 == 1:
        return advanced.intersection(coaches), ["Bojan", "Eva"]
    return advanced.difference(coaches), ["Igor", "Lara"]


async def _sorted(queryset):
    return sorted([row async for row in queryset])


def _club_union(slug="koper-sailing"):
    """A union of club slugs, which waits while a `TableLock` holds; it counts 1 row."""
    slugs = Club.objects.filter(slug=slug).values_list("slug", flat=True)
    return slugs.union(Club.objects.filter(city="Koper").values_list("slug", flat=True)).count()


def _mismatched():
    """A union whose queries select different numbers of columns: the database refuses it."""
    names = Member.objects.values_list("first_name")
    return names.union(Member.objects.values_list("first_name", "level"))


def _require(driver, *names):
    missing = set(names) - driver.capabilities.constructs
    if missing:
        pytest.skip(f"the {driver.name} driver does not support {', '.join(sorted(missing))}")


# Concurrency


async def test_concurrent_combined_reads_on_a_small_pool(driver, make_router):
    _require(driver, "lookup.contains")  # a JSON array containing a value
    pool = make_router(max_connections=3, acquire_timeout=10).pools[0]
    for _ in REPEAT:
        reads = [_combined(n) for n in range(30)]
        results = await asyncio.gather(*(_sorted(queryset) for queryset, _ in reads))
        assert results == [expected for _, expected in reads]
    assert pool.size <= 3
    assert pool.in_use == 0


async def test_combined_reads_in_tasks_of_one_transaction(driver, make_router):
    _require(driver, "lookup.contains")  # a JSON array containing a value
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]
    for _ in REPEAT:
        async with adorm.atomic():
            async with asyncio.TaskGroup() as group:
                reads = [_combined(n) for n in range(18)]
                tasks = [group.create_task(_sorted(queryset)) for queryset, _ in reads]
                counted = group.create_task(_club_union())
            assert pool.in_use == 1
        assert [task.result() for task in tasks] == [expected for _, expected in reads]
        assert counted.result() == 1
    assert (pool.size, pool.in_use) == (1, 0)


async def test_a_failing_combined_read_inside_a_transaction_rolls_back_and_frees_the_slot(driver, make_router):
    _require(driver, "lookup.contains")  # a JSON array containing a value
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]

    async def update_then_fail():
        async with adorm.atomic():
            await Club.objects.filter(slug="koper-sailing").update(city="Rolled back")
            await _sorted(_mismatched())

    for _ in REPEAT:
        with pytest.raises(ProgrammingError):
            await update_then_fail()
        assert pool.in_use == 0
        assert (await Club.objects.get(slug="koper-sailing")).city == "Koper"
    with pytest.raises(ProgrammingError):
        await _sorted(_mismatched())
    assert await _sorted(_combined(1)[0]) == ["Bojan", "Eva"]
    assert (pool.size, pool.in_use) == (1, 0)


async def test_refused_methods_after_a_set_operation_need_no_slot(make_router, recorded):
    pool = make_router(max_connections=1, acquire_timeout=30).pools[0]
    combined, _ = _combined(0)
    holding = asyncio.Event()
    done = asyncio.Event()

    async def hold_the_only_slot():
        async with adorm.atomic():
            await Club.objects.count()
            holding.set()
            await done.wait()

    holder = asyncio.create_task(hold_the_only_slot())
    try:
        await holding.wait()
        before = len(recorded)
        async with asyncio.timeout(1):
            with pytest.raises(NotSupportedError, match=r"after union\(\)"):
                combined.filter(level="beginner")
            for call in (
                lambda: combined.aggregate(n=Count("pk")),
                lambda: combined.update(level="beginner"),
                lambda: combined.delete(),
                lambda: combined.get(first_name="Ana"),
            ):
                with pytest.raises(NotSupportedError, match=r"after union\(\)"):
                    await call()
        assert len(recorded) == before
    finally:
        done.set()
        await holder
    assert pool.in_use == 0


# Cancellation and lost connections


async def test_a_combined_read_cancelled_racing_completion_leaves_the_slot_usable(make_router, blocker):
    if not blocker.lost_connections:
        pytest.skip("the blocker does not hold a table lock reads wait for")
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]
    outcomes = set()
    for attempt in range(60):
        await blocker.hold()
        task = asyncio.create_task(_club_union())
        await blocker.waiting()
        await blocker.release()
        # Cancel after 0 to 14 loop iterations, then after up to 2744, or once the operation has finished.
        for _ in range(attempt % 15 if attempt < 45 else (attempt % 15) ** 3):
            if task.done():
                break
            await asyncio.sleep(0)
        task.cancel()
        try:
            outcomes.add(await task)
        except asyncio.CancelledError:
            outcomes.add("cancelled")
        assert pool.in_use == 0
        await blocker.transactions_ended()
    async with asyncio.timeout(5):
        assert await _club_union() == 1
    assert outcomes == {1, "cancelled"}
    assert pool.size == 1


async def test_cancelling_a_waiting_combined_read_inside_a_transaction_rolls_it_back(make_router, blocker):
    if not blocker.lost_connections:
        pytest.skip("the blocker does not hold a table lock reads wait for")
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]
    started = asyncio.Event()

    async def update_then_wait():
        async with adorm.atomic():
            await Member.objects.filter(first_name="Ana").update(level="advanced")
            started.set()
            return await _club_union()

    for _ in range(3):
        started.clear()
        await blocker.hold()
        task = asyncio.create_task(update_then_wait())
        await started.wait()
        await blocker.waiting()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await blocker.release()
        assert pool.in_use == 0
        await blocker.transactions_ended()
        assert (await Member.objects.get(first_name="Ana")).level == "beginner"
    assert pool.size <= 1


async def test_a_connection_lost_mid_combined_read_is_dropped(make_router, blocker):
    if not blocker.lost_connections:
        pytest.skip("the driver has no server connection to lose")
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]
    for _ in range(3):
        await blocker.hold()
        task = asyncio.create_task(_club_union())
        await blocker.terminate(await blocker.waiting())
        with pytest.raises(DatabaseError) as lost:
            await task
        assert is_connection_error(lost.value)
        assert (pool.size, pool.in_use) == (0, 0)
        await blocker.release()
        assert await _club_union() == 1
        assert pool.size == 1


# Combinations


async def test_queries_of_different_models_combine_column_by_column(router):  # noqa: ARG001
    cities = Club.objects.values_list("city", flat=True)
    names = Member.objects.filter(level="advanced").values_list("first_name", flat=True)
    assert await _sorted(cities.union(names)) == ["Bojan", "Eva", "Igor", "Koper", "Lara", "Ljubljana", "Maribor"]
    assert await cities.union(names, all=True).count() == 7


async def test_set_operations_nest_and_order_slice_and_count_the_combined_rows(driver, router):  # noqa: ARG001
    _require(driver, "lookup.contains")  # a JSON array containing a value
    beginners, intermediate, advanced = _levels(*LEVELS)
    coaches = Member.objects.filter(tags__contains=["coach"]).values_list("first_name", flat=True)
    nested = beginners.union(advanced).difference(coaches)
    assert await _sorted(nested) == ["Ana", "David", "Gaja", "Igor", "Jana", "Lara"]
    ordered = nested.union(intermediate).order_by("-first_name")
    assert [name async for name in ordered[1:4]] == ["Klemen", "Jana", "Igor"]
    assert await ordered.count() == 10
    assert await ordered[8:].count() == 2
    assert await ordered.exists()
    assert not await beginners.intersection(advanced).exists()
    assert await ordered.first() == "Lara"


async def test_model_rows_of_a_combination_are_instances(driver, router):  # noqa: ARG001
    _require(driver, "lookup.contains")  # a JSON array containing a value
    coaches = Member.objects.filter(tags__contains=["coach"])
    members = Member.objects.filter(level="advanced").intersection(coaches).order_by("first_name")
    found = [member async for member in members]
    assert [(type(member), member.first_name, member.level) for member in found] == [
        (Member, "Bojan", "advanced"),
        (Member, "Eva", "advanced"),
    ]
    with pytest.raises(Member.MultipleObjectsReturned):
        await members.get()
    assert (await members[:1].get()).first_name == "Bojan"


async def test_a_combination_in_a_subquery(driver, router):  # noqa: ARG001
    _require(driver, "lookup.contains", "expression.Subquery", "lookup.in")
    advanced = Member.objects.filter(level="advanced").values("pk")
    juniors = Member.objects.filter(tags__contains=["junior"]).values("pk")
    members = Member.objects.filter(pk__in=advanced.union(juniors)).values_list("first_name", flat=True)
    assert await _sorted(members) == ["Ana", "Bojan", "Eva", "Filip", "Igor", "Jana", "Lara"]


async def test_an_outer_reference_in_a_combined_exists_refers_to_the_enclosing_query(driver, router):  # noqa: ARG001
    _require(driver, "expression.Exists", "expression.OuterRef")
    coach = Membership.objects.filter(member_id=OuterRef("pk"), role="coach")
    board = Membership.objects.filter(member_id=OuterRef("pk"), role="board")
    members = Member.objects.filter(Exists(coach.union(board))).values_list("first_name", flat=True)
    assert await _sorted(members) == ["Bojan", "Eva", "Igor", "Lara"]


async def test_first_and_last_of_unordered_combined_rows_order_by_the_primary_key(router):  # noqa: ARG001
    members = Member.objects.filter(level="advanced").union(Member.objects.filter(level="beginner"))
    assert (await members.first()).first_name == "Ana"
    assert (await members.last()).first_name == "Lara"
    names = members.values_list("first_name", flat=True)
    with pytest.raises(DatabaseError, match="ORDER BY term") as info:
        await names.first()
    assert type(info.value) is DatabaseError
    assert await names.order_by("first_name").first() == "Ana"


async def test_ordering_by_a_column_named_after_the_set_operation(router):  # noqa: ARG001
    advanced = Member.objects.filter(level="advanced").values_list("first_name")
    combined = advanced.union(Club.objects.values_list("city")).values_list("label", flat=True)
    expected = ["Bojan", "Eva", "Igor", "Koper", "Lara", "Ljubljana", "Maribor"]
    assert [name async for name in combined.order_by("label")] == expected
    assert [name async for name in combined.order_by("-label")[:2]] == ["Maribor", "Ljubljana"]
    nested = combined.union(Member.objects.filter(level="beginner").values_list("first_name")).values_list("tag")
    assert [row async for row in nested.order_by("tag")[:2]] == [("Ana",), ("Bojan",)]
    with pytest.raises(DatabaseError, match="ORDER BY term does not match any column") as info:
        await combined.order_by("first_name").first()
    assert type(info.value) is DatabaseError


async def test_empty_slices_follow_the_rules_of_empty_queries(router):  # noqa: ARG001
    beginners, advanced = _levels("beginner", "advanced")
    with pytest.raises(TypeError, match="slice"):
        beginners[0:0].union(advanced[:2]).order_by("first_name")
    with pytest.raises(TypeError, match="slice"):
        beginners.intersection(advanced[1:1]).order_by("first_name")
    if on_sqlite():
        with pytest.raises(NotSupportedError, match=COMPOUND_SLICES):
            await _sorted(beginners.union(advanced[3:3])[:10])
        return
    assert [name async for name in beginners.intersection(advanced[1:1])] == []
    assert await _sorted(beginners.union(advanced[3:3])[:10]) == ["Ana", "David", "Gaja", "Jana"]
    assert await _sorted(beginners.difference(advanced[3:3])) == ["Ana", "David", "Gaja", "Jana"]


async def test_a_set_operation_of_one_sliced_query_keeps_its_clauses_apart(router):  # noqa: ARG001
    (beginners,) = _levels("beginner")
    first_two = beginners.order_by("first_name")[:2]
    if on_sqlite():
        with pytest.raises(NotSupportedError, match=COMPOUND_SLICES):
            await _sorted(first_two.difference(Member.objects.none()))
        return
    with pytest.raises(ProgrammingError, match="multiple ORDER BY clauses not allowed"):
        await _sorted(first_two.union(Member.objects.none()).order_by("-first_name"))
    with pytest.raises(ProgrammingError, match="multiple LIMIT clauses not allowed"):
        await _sorted(first_two.intersection()[:1])
    assert [name async for name in first_two.union(Member.objects.none()).order_by("-first_name")[:5]] == [
        "David",
        "Ana",
    ]
    assert [name async for name in first_two.difference(Member.objects.none())] == ["Ana", "David"]
