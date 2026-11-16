"""``bulk_create()`` on every driver under the failures that matter in production: a failing row in a later batch,
cancellation while it waits and racing its completion, concurrent inserts of the same unique key with conflict
handling, tasks of one transaction inserting at once, and failures inside a savepoint. Every outcome is all rows or
none, and the pool stays usable."""

import asyncio
import contextlib
import datetime

import asyncpg
import pytest

import adorm

from adorm import cancellation_outcome, CancellationOutcome, DatabaseError, IntegrityError
from adorm.drivers import is_connection_error
from demo.models.activities import Tag
from demo.models.clubs import Club, Member
from tests.conformance.drivers import on_sqlite

pytestmark = pytest.mark.django_db(transaction=True)

REPEAT = range(3)
START = datetime.datetime(2026, 3, 2, 7, 0, tzinfo=datetime.UTC)
TAG_WAITING = (
    "SELECT pid FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND query LIKE '%activities_tag%'"
    " AND pid <> pg_backend_pid()"
)
MEMBER_WAITING = TAG_WAITING.replace("activities_tag", "clubs_member")
CLUB_WAITING = TAG_WAITING.replace("activities_tag", "clubs_club")


def sql_only(driver):
    if driver.name == "memory":
        pytest.skip("the memory driver has no table locks and checks no constraint but the primary key")


def clubs(prefix, count=3):
    return [
        Club(name=f"{prefix} {n}", slug=f"{prefix}-{n}", founded=datetime.date(2000, 1, 1), city="Piran")
        for n in range(count)
    ]


async def stored(prefix):
    return await Club.objects.filter(slug__istartswith=f"{prefix}-").count()


@pytest.fixture
async def catalogue(demo_data):
    """A connection of the test's own to the test database, which watches PostgreSQL's sessions; the test is skipped
    on SQLite."""
    if on_sqlite():
        pytest.skip("the test watches PostgreSQL's sessions")
    conn = await asyncpg.connect(demo_data)
    try:
        yield conn
    finally:
        await conn.close()


async def until_waiting(catalogue, query):
    async with asyncio.timeout(10):
        while await catalogue.fetchval(query) is None:
            await asyncio.sleep(0.01)


async def test_a_failing_row_in_a_later_batch_inserts_nothing_and_keeps_the_slot(driver, pool):  # noqa: ARG001
    existing = await Club.objects.get(slug="koper-sailing")
    for n in REPEAT:
        objs = [
            Club(pk=9100 + n * 10, name="A", slug=f"fail-{n}-a", founded=datetime.date(2000, 1, 1), city="Piran"),
            Club(pk=9101 + n * 10, name="B", slug=f"fail-{n}-b", founded=datetime.date(2000, 1, 1), city="Piran"),
            Club(pk=existing.pk, name="C", slug=f"fail-{n}-c", founded=datetime.date(2000, 1, 1), city="Piran"),
        ]
        with pytest.raises(IntegrityError) as raised:
            await Club.objects.bulk_create(objs, batch_size=1)
        assert raised.value.sqlstate == (None if on_sqlite() else "23505")
        assert await stored(f"fail-{n}") == 0
        assert all(obj._state.adding for obj in objs)
        assert (pool.size, pool.in_use) == (1, 0)
    assert await Club.objects.count() == 3


async def test_a_failing_bulk_create_inside_a_savepoint_leaves_the_transaction_usable(driver, pool):  # noqa: ARG001
    existing = await Club.objects.get(slug="koper-sailing")
    for n in REPEAT:
        async with adorm.atomic():
            await Club.objects.bulk_create(clubs(f"kept-{n}", 2))
            with pytest.raises(IntegrityError):
                async with adorm.atomic():
                    await Club.objects.bulk_create(
                        [
                            *clubs(f"lost-{n}", 2),
                            Club(pk=existing.pk, name="X", slug="x", founded=START.date(), city="Piran"),
                        ],
                        batch_size=1,
                    )
            assert await stored(f"lost-{n}") == 0
            await Club.objects.bulk_create(clubs(f"after-{n}", 1))
        assert (await stored(f"kept-{n}"), await stored(f"lost-{n}"), await stored(f"after-{n}")) == (2, 0, 1)
        assert pool.in_use == 0


@pytest.mark.parametrize("attempt", range(5))
async def test_tasks_of_one_transaction_insert_at_once_on_its_slot(driver, make_router, attempt):  # noqa: ARG001
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]
    async with adorm.atomic():
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(Club.objects.bulk_create(clubs(f"task-{n}", 3))) for n in range(5)]
        created = [club for task in tasks for club in task.result()]
        assert len({club.pk for club in created}) == 15
        assert pool.in_use == 1
    assert sum([await stored(f"task-{n}") for n in range(5)]) == 15
    assert (pool.size, pool.in_use) == (1, 0)


async def test_concurrent_bulk_creates_on_a_small_pool(driver, make_router):  # noqa: ARG001
    pool = make_router(max_connections=2, acquire_timeout=5).pools[0]
    results = await asyncio.gather(*(Club.objects.bulk_create(clubs(f"flow-{n}", 4), batch_size=3) for n in range(8)))
    assert len({club.pk for result in results for club in result}) == 32
    assert sum([await stored(f"flow-{n}") for n in range(8)]) == 32
    assert pool.in_use == 0
    assert pool.size <= 2


async def test_cancelling_a_waiting_bulk_create_inserts_nothing(driver, make_router, blocker):
    sql_only(driver)
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]
    for n in REPEAT:
        await blocker.hold()
        task = asyncio.create_task(Club.objects.bulk_create(clubs(f"wait-{n}"), batch_size=1))
        await blocker.waiting()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await blocker.idle()
        assert pool.in_use == 0
        await blocker.release()
        await blocker.transactions_ended()
        assert await stored(f"wait-{n}") == 0
    assert len(await Club.objects.bulk_create(clubs("after"), batch_size=1)) == 3
    assert await stored("after") == 3


@pytest.mark.parametrize("outer", [False, True], ids=["own transaction", "in atomic"])
@pytest.mark.parametrize("mode", ["terminate", "cancel"])
async def test_a_bulk_create_lost_after_its_first_batch_stores_nothing(
    driver, make_router, catalogue, demo_data, mode, outer
):
    """The connection lost or the call cancelled while the second batch waits on a row another transaction inserted:
    the first batch's row is not stored, the slot is returned and the next call works."""
    sql_only(driver)
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]
    holder = await asyncpg.connect(demo_data)
    try:
        for n in REPEAT:
            prefix = f"lost-{mode}-{outer}-{n}"
            first, blocked, last = (9600 + n * 10 + k for k in range(3))
            transaction = holder.transaction()
            await transaction.start()
            await holder.execute(
                "INSERT INTO clubs_club (id, name, slug, founded, city, address, disciplines)"
                " VALUES ($1, 'Held', $2, '2000-01-01', 'Piran', '{}', '{}')",
                blocked,
                f"held-{prefix}",
            )
            objs = [
                Club(pk=pk, name=prefix, slug=f"{prefix}-{k}", founded=datetime.date(2000, 1, 1), city="Piran")
                for k, pk in enumerate((first, blocked, last))
            ]

            async def insert(objs=objs):
                if outer:
                    async with adorm.atomic():
                        await Club.objects.bulk_create(objs, batch_size=1)
                else:
                    await Club.objects.bulk_create(objs, batch_size=1)

            task = asyncio.create_task(insert())
            async with asyncio.timeout(10):
                while (pid := await catalogue.fetchval(CLUB_WAITING)) is None:
                    await asyncio.sleep(0.01)
            if mode == "terminate":
                await catalogue.execute("SELECT pg_terminate_backend($1)", pid)
                with pytest.raises(DatabaseError) as lost:
                    await task
                assert is_connection_error(lost.value)
            else:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            await transaction.rollback()
            async with asyncio.timeout(5):
                while await catalogue.fetchval(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                    " AND xact_start IS NOT NULL AND pid <> pg_backend_pid() AND pid <> $1",
                    holder.get_server_pid(),
                ):
                    await asyncio.sleep(0.01)
            assert await stored(prefix) == 0
            assert pool.in_use == 0
            assert len(await Club.objects.bulk_create(clubs(f"next-{prefix}"), batch_size=1)) == 3
            assert await stored(f"next-{prefix}") == 3
            assert pool.in_use == 0
    finally:
        await holder.close()


async def _recorded(awaitable, raised):
    try:
        return await awaitable
    except BaseException as exc:
        raised.append(exc)
        raise


async def test_a_bulk_create_cancelled_racing_completion_inserts_every_batch_or_none(driver, make_router, blocker):
    sql_only(driver)
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]
    seen = set()
    for attempt in range(120):
        prefix = f"race{attempt}"
        await blocker.hold()
        raised = []
        # Three statements in one transaction of the driver's own.
        task = asyncio.create_task(_recorded(Club.objects.bulk_create(clubs(prefix), batch_size=1), raised))
        await blocker.waiting()
        await blocker.release()
        if attempt % 10 == 9:
            # Every tenth attempt lets the statements finish before the cancellation.
            await asyncio.wait({task}, timeout=5)
        for _ in range(attempt % 15 if attempt < 90 else (attempt % 15) ** 3):
            if task.done():
                break
            await asyncio.sleep(0)
        task.cancel()
        try:
            assert len(await task) == 3
            outcome = None
        except asyncio.CancelledError:
            outcome = cancellation_outcome(raised[0])
        seen.add(outcome)
        assert pool.in_use == 0
        await blocker.transactions_ended()
        count = await stored(prefix)
        assert count in (0, 3)
        if outcome in (None, CancellationOutcome.SUCCEEDED):
            assert count == 3, outcome
        elif outcome is not CancellationOutcome.UNKNOWN:
            assert count == 0, outcome
    assert None in seen
    assert seen - {None}, "no attempt was cancelled"
    assert pool.size <= 1


@pytest.mark.parametrize("winner_commits", [True, False])
async def test_ignoring_conflicts_waits_for_a_concurrent_insert_of_the_same_key(
    driver, make_router, catalogue, winner_commits
):
    """A row whose unique key another transaction inserted waits for it: it is skipped when that transaction commits
    and inserted when it rolls back; the other rows are inserted either way."""
    sql_only(driver)
    pool = make_router(max_connections=2, acquire_timeout=5).pools[0]
    for n in REPEAT:
        name = f"race-{winner_commits}-{n}"
        inserted, finish = asyncio.Event(), asyncio.Event()

        async def first(name=name, inserted=inserted, finish=finish):
            with contextlib.suppress(RuntimeError):
                async with adorm.atomic():
                    await Tag.objects.bulk_create([Tag(name=name)])
                    inserted.set()
                    await finish.wait()
                    if not winner_commits:
                        raise RuntimeError("roll back")

        holder = asyncio.create_task(first())
        await inserted.wait()
        second = asyncio.create_task(
            Tag.objects.bulk_create([Tag(name=f"other-{name}"), Tag(name=name)], ignore_conflicts=True)
        )
        await until_waiting(catalogue, TAG_WAITING)
        assert not second.done()
        finish.set()
        await holder
        assert [tag.pk for tag in await second] == [None, None]
        assert await Tag.objects.filter(name__in=[name, f"other-{name}"]).count() == 2
        assert pool.in_use == 0


@pytest.mark.parametrize("winner_commits", [True, False])
async def test_updating_conflicts_waits_for_a_concurrent_insert_of_the_same_key(
    driver, make_router, catalogue, winner_commits
):
    """An upsert of a unique key another transaction inserted waits for it: it updates that row when the transaction
    commits and inserts its own when it rolls back."""
    sql_only(driver)
    pool = make_router(max_connections=2, acquire_timeout=5).pools[0]
    for n in REPEAT:
        email = f"race-{winner_commits}-{n}@example.com"
        inserted, finish = asyncio.Event(), asyncio.Event()

        def member(first_name, level, email=email):
            return Member(first_name=first_name, last_name="Race", email=email, joined=START, level=level)

        async def first(inserted=inserted, finish=finish, member=member):
            with contextlib.suppress(RuntimeError):
                async with adorm.atomic():
                    await Member.objects.create(**member("First", "beginner").model_dump(exclude={"id"}))
                    inserted.set()
                    await finish.wait()
                    if not winner_commits:
                        raise RuntimeError("roll back")

        holder = asyncio.create_task(first())
        await inserted.wait()
        second = asyncio.create_task(
            Member.objects.bulk_create(
                [member("Second", "advanced")], update_conflicts=True, update_fields=["level"], unique_fields=["email"]
            )
        )
        await until_waiting(catalogue, MEMBER_WAITING)
        finish.set()
        await holder
        (upserted,) = await second
        row = await Member.objects.get(email=email)
        assert upserted.pk == row.pk
        assert (row.first_name, row.level) == ("First" if winner_commits else "Second", "advanced")
        assert pool.in_use == 0
