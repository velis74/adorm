"""The Django driver on SQLite (``--ds=tests.djangoproject.settings_sqlite``): WAL for readers beside the writer, one
writer at a time from ``BEGIN IMMEDIATE`` on, waits for the write lock bounded by the busy timeout and ended by a
cancellation, ``interrupt()`` of a running statement also when it races the statement's start, and SQLite's errors
leaving the slot in the pool. Skipped on other databases."""

import asyncio
import contextvars
import datetime
import decimal
import sqlite3
import threading
import unittest

from types import SimpleNamespace

import pytest

from django.test import TestCase

import adorm

from adorm import (
    cancellation_outcome,
    CancellationOutcome,
    ConnectionProfile,
    IntegrityError,
    OperationalError,
    TransactionManagementError,
)
from adorm.drivers.django import DjangoDriver
from adorm.drivers.django.vendors import PROGRESS_STEPS, SQLITE_CAPABILITIES, TRANSACTION_ENDED
from adorm.executor import Pool, Router, set_runner
from adorm.operations import CheckTable
from adorm.testing import wait_for_operation
from tests.clubmodels.clubs import Club, Member, Membership
from tests.conformance.drivers import on_sqlite, SQLiteWriteLock, TrackedDjangoDriver
from tests.library_data import in_thread

pytestmark = [pytest.mark.django_db(transaction=True)]

REPEAT = range(5)
FOUNDED = datetime.date(2001, 2, 3)


@pytest.fixture
async def sqlite(club_data):
    """``make(**params)``: a router with one pool of a `TrackedDjangoDriver` on the SQLite test database, the
    profile's ``params`` applied, as adorm's runner; returns the pool. Returns the database's path as ``path``."""
    if not on_sqlite():
        pytest.skip("the test database is not SQLite")
    routers = []
    previous = set_runner(None)

    def make(**params) -> Pool:
        driver = TrackedDjangoDriver()
        pool = Pool(driver, ConnectionProfile("default", driver, params=params), max_connections=4, acquire_timeout=5)
        router = Router([pool])
        routers.append(router)
        set_runner(router)
        return pool

    make.path = club_data.removeprefix("sqlite:///")
    try:
        yield make
    finally:
        set_runner(previous)
        for router in routers:
            await router.close()


@pytest.fixture
async def write_lock(sqlite):
    """``write_lock(pool)``: the `SQLiteWriteLock` on the pool's driver, released afterwards."""
    locks = []

    def make(pool: Pool) -> SQLiteWriteLock:
        lock = SQLiteWriteLock(pool.driver, f"sqlite:///{sqlite.path}")
        locks.append(lock)
        return lock

    try:
        yield make
    finally:
        for lock in locks:
            await lock.close()


def other_writer_is_refused(path: str) -> bool:
    """Whether a connection of its own cannot take the write lock now."""
    other = sqlite3.connect(path, isolation_level=None, timeout=0)
    try:
        other.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        return "locked" in str(exc)
    else:
        other.execute("ROLLBACK")
        return False
    finally:
        other.close()


async def test_the_pool_has_sqlites_capabilities_and_the_database_runs_in_wal_mode(sqlite):
    pool = sqlite()
    assert pool.capabilities == SQLITE_CAPABILITIES
    assert await Club.objects.count() == 3
    (connection,) = pool.driver.connections
    assert connection.vendor.name == "sqlite"
    assert connection.backend_pid is None

    def journal_mode() -> str:
        db = sqlite3.connect(sqlite.path)
        try:
            return db.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            db.close()

    assert await in_thread(journal_mode) == "wal"
    assert pool.driver.database_key(pool.profile)[0] == "sqlite"
    with pytest.raises(adorm.NotSupportedError, match="monitors PostgreSQL databases only"):
        await pool.driver.open_monitor(pool.profile)


async def test_a_transaction_holds_the_write_lock_from_its_start_and_readers_go_on(sqlite):
    sqlite()
    async with adorm.atomic():
        # Nothing written yet: BEGIN IMMEDIATE took the write lock.
        assert await in_thread(lambda: other_writer_is_refused(sqlite.path))
        await Club.objects.filter(slug="koper-sailing").update(city="Held")
        # A reader outside the transaction, on another slot, sees the committed state.
        reader = asyncio.create_task(Club.objects.filter(city="Held").count(), context=contextvars.Context())
        assert await reader == 0
    assert not await in_thread(lambda: other_writer_is_refused(sqlite.path))
    assert await Club.objects.filter(city="Held").count() == 1


async def test_a_second_transaction_waits_for_the_first_ones_commit(sqlite):
    pool = sqlite()
    first_open = asyncio.Event()
    first_may_commit = asyncio.Event()

    async def first():
        async with adorm.atomic():
            await Club.objects.filter(slug="koper-sailing").update(city="First")
            first_open.set()
            await first_may_commit.wait()

    async def second():
        async with adorm.create_session(session_name="second").atomic():
            return await Club.objects.filter(city="First").count()

    for _ in REPEAT:
        first_open.clear()
        first_may_commit.clear()
        task = asyncio.create_task(first())
        await first_open.wait()
        waiting = asyncio.create_task(second())
        lock = SQLiteWriteLock(pool.driver, "")
        await lock.waiting()
        first_may_commit.set()
        await task
        # The second transaction began after the first one committed, so it sees its write.
        assert await waiting == 1
        await Club.objects.filter(city="First").update(city="Koper")


async def test_a_write_waits_for_the_write_lock_and_then_runs(sqlite, write_lock):
    pool = sqlite()
    lock = write_lock(pool)
    for _ in REPEAT:
        await lock.hold()
        task = asyncio.create_task(lock.blocked("koper-sailing"))
        connection = await lock.waiting()
        assert connection.waiting_for_write_lock
        assert await Club.objects.filter(slug="koper-sailing").count() == 1  # readers go on
        await lock.release()
        assert await task == 1
        assert not connection.waiting_for_write_lock


async def test_a_write_waiting_past_the_busy_timeout_fails_and_keeps_its_slot(sqlite, write_lock):
    pool = sqlite(OPTIONS={"timeout": 0.2})
    lock = write_lock(pool)
    assert await Club.objects.count() == 3
    await lock.hold()
    with pytest.raises(OperationalError, match="database is locked") as raised:
        await lock.blocked("koper-sailing")
    assert raised.value.sqlstate is None
    assert raised.value.connection_lost is False
    await lock.release()
    assert pool.size == 1
    assert await lock.blocked("koper-sailing") == 1
    assert pool.size == 1


@pytest.mark.parametrize("repeat", range(3))
async def test_cancelling_a_wait_for_the_write_lock_ends_it_at_once(sqlite, write_lock, repeat):  # noqa: ARG001
    pool = sqlite(OPTIONS={"timeout": 60})
    lock = write_lock(pool)
    pool.driver.cancel_grace = 30
    await lock.hold()
    task = asyncio.create_task(lock.blocked("koper-sailing"))
    connection = await lock.waiting()
    task.cancel()
    async with asyncio.timeout(5):  # far below the busy timeout and the cancel grace
        with pytest.raises(asyncio.CancelledError) as raised:
            await task
    assert cancellation_outcome(raised.value) is CancellationOutcome.UNKNOWN
    assert not connection.waiting_for_write_lock
    await lock.release()
    assert (pool.size, pool.in_use) == (1, 0)
    assert await Club.objects.filter(city="Blocked").count() == 0
    assert await lock.blocked("koper-sailing") == 1


async def test_cancelling_a_transaction_waiting_for_the_write_lock_leaves_no_transaction(sqlite, write_lock):
    pool = sqlite(OPTIONS={"timeout": 60})
    lock = write_lock(pool)
    await lock.hold()

    async def block():
        async with adorm.atomic():
            await Club.objects.filter(slug="koper-sailing").update(city="Blocked")

    task = asyncio.create_task(block())
    await lock.waiting()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await lock.release()
    assert await lock.open_transactions() == 0
    assert not await in_thread(lambda: other_writer_is_refused(sqlite.path))
    assert await Club.objects.filter(city="Blocked").count() == 0
    assert pool.in_use == 0


@pytest.fixture
async def many_members(sqlite):  # noqa: ARG001 - on SQLite only
    """2000 members more, each in every club."""

    def add() -> None:
        from tests.djangoproject.clubs import models as dj

        joined = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        members = dj.Member.objects.bulk_create(
            dj.Member(first_name="M", last_name=str(n), email=f"m{n}@example.com", joined=joined) for n in range(2000)
        )
        since = datetime.date(2026, 1, 1)
        dj.Membership.objects.bulk_create(
            dj.Membership(club=club, member=member, since=since, fee=0)
            for club in dj.Club.objects.all()
            for member in members
        )

    await in_thread(add)


SLOW_PATH = "memberships__member__memberships__club__memberships__member__memberships__club__memberships__active"
"""A filter over a join of about 10^11 rows with ``many_members``."""


def slow_count():
    return Club.objects.filter(**{SLOW_PATH: True}).count()


@pytest.mark.parametrize("repeat", range(3))
async def test_cancelling_a_running_statement_interrupts_it(sqlite, many_members, repeat):  # noqa: ARG001
    pool = sqlite()
    pool.driver.cancel_grace = 30
    assert await Member.objects.count() > 2000
    task = asyncio.create_task(slow_count())
    await wait_for_operation(task)
    task.cancel()
    async with asyncio.timeout(5):
        with pytest.raises(asyncio.CancelledError):
            await task
    assert (pool.size, pool.in_use) == (1, 0)
    assert await Club.objects.count() == 3


async def test_a_cancellation_racing_the_start_of_the_statement_stops_it(sqlite, many_members):  # noqa: ARG001
    """The cancellation reaches the call at every point from its start to the statement's: interrupt() before the
    statement runs is lost, and the statement raises before it runs."""
    pool = sqlite()
    pool.driver.cancel_grace = 30
    assert await Club.objects.count() == 3
    for spins in range(12):
        task = asyncio.create_task(slow_count())
        for _ in range(spins):
            await asyncio.sleep(0)
        task.cancel()
        async with asyncio.timeout(5):
            with pytest.raises(asyncio.CancelledError):
                await task
        assert (pool.size, pool.in_use) == (1, 0)
    assert await Club.objects.count() == 3


@pytest.mark.parametrize("repeat", range(3))
async def test_a_cancelled_write_in_a_nested_block_rolls_back_the_whole_transaction(
    sqlite,
    many_members,  # noqa: ARG001
    repeat,  # noqa: ARG001
):
    """SQLite rolls back the whole transaction after an interrupted write: the enclosing block cannot go on."""
    pool = sqlite()
    pool.driver.cancel_grace = 30
    assert await Club.objects.count() == 3

    (connection,) = pool.driver.connections
    raw = connection.db.connection
    running = threading.Event()

    def progress() -> bool:
        running.set()
        return connection._cancelled()

    async def inner():
        async with adorm.atomic():
            await Club.objects.filter(**{SLOW_PATH: True}).update(city="Inner")

    async def outer():
        async with adorm.atomic():
            await Club.objects.filter(slug="koper-sailing").update(city="Outer")
            # Reports the update's statement running; SAVEPOINT ends before the handler's first call.
            raw.set_progress_handler(progress, PROGRESS_STEPS)
            task = asyncio.create_task(inner())
            assert await in_thread(lambda: running.wait(10))
            task.cancel()
            async with asyncio.timeout(5):
                with pytest.raises(asyncio.CancelledError):
                    await task
            with pytest.raises(TransactionManagementError, match=TRANSACTION_ENDED):
                await Club.objects.filter(slug="koper-sailing").update(city="After")

    try:
        with pytest.raises(TransactionManagementError, match=TRANSACTION_ENDED):
            await outer()
    finally:
        raw.set_progress_handler(connection._cancelled, PROGRESS_STEPS)
    assert (pool.size, pool.in_use) == (1, 0)
    assert await Club.objects.filter(city__in=["Outer", "Inner", "After"]).count() == 0
    assert not await in_thread(lambda: other_writer_is_refused(sqlite.path))


@pytest.mark.parametrize("repeat", range(3))
async def test_select_for_update_in_a_transaction_makes_other_writers_wait(sqlite, repeat):  # noqa: ARG001
    """``BEGIN IMMEDIATE`` holds the write lock: a write of another flow waits for the commit of the transaction that
    read rows with ``select_for_update()``."""
    pool = sqlite()
    assert await Club.objects.count() == 3
    write = None
    async with adorm.atomic():
        club = await Club.objects.select_for_update(of=["self"]).get(slug="koper-sailing")
        assert await in_thread(lambda: other_writer_is_refused(sqlite.path))
        write = asyncio.create_task(
            Club.objects.filter(slug="koper-sailing").update(city="Second"), context=contextvars.Context()
        )
        async with asyncio.timeout(10):
            while not any(connection.waiting_for_write_lock for connection in list(pool.driver.connections)):
                await asyncio.sleep(0.01)
        assert not write.done()
        await Club.objects.filter(pk=club.pk).update(city="First")
    async with asyncio.timeout(10):
        assert await write == 1
    assert await Club.objects.filter(slug="koper-sailing").values_list("city", flat=True).get() == "Second"
    with pytest.raises(TransactionManagementError, match="outside of a transaction"):
        await Club.objects.select_for_update().get(slug="koper-sailing")


async def test_errors_leave_the_slot_in_the_pool(sqlite):
    pool = sqlite()
    assert await Club.objects.count() == 3
    for _ in REPEAT:
        with pytest.raises(IntegrityError) as raised:
            await Club.objects.create(
                name="Other",
                slug="koper-sailing",
                founded=datetime.date(2000, 1, 1),
                city="Koper",
                address={},
                disciplines=[],
            )
        error = raised.value
        assert (error.table_name, error.columns, error.model, error.fields) == (
            "clubs_club",
            ("slug",),
            "clubs.Club",
            ("slug",),
        )
        assert error.sqlstate is None
        assert error.connection_lost is False
    assert pool.size == 1
    async with adorm.atomic():
        with pytest.raises(IntegrityError):
            async with adorm.atomic():
                await Club.objects.filter(slug="koper-sailing").update(slug="ljubljana-rowing")
        assert await Club.objects.filter(slug="koper-sailing").update(city="Saved") == 1
    assert await Club.objects.filter(city="Saved").count() == 1
    assert pool.size == 1


async def test_lists_and_json_round_trip(sqlite):
    sqlite()
    club = await Club.objects.create(
        name="Nov",
        slug="nov",
        founded=datetime.date(2001, 2, 3),
        city="Nova Gorica",
        address={"street": "Ul. 1"},
        disciplines=["A"],
    )
    again = await Club.objects.get(pk=club.pk)
    assert (again.address, again.disciplines) == ({"street": "Ul. 1"}, ["A"])
    await Club.objects.filter(pk=club.pk).delete()


async def test_a_deferred_foreign_key_fails_at_commit_and_rolls_back(sqlite):
    pool = sqlite()
    member = await Member.objects.order_by("pk").first()
    for _ in REPEAT:
        with pytest.raises(IntegrityError) as raised:
            async with adorm.atomic():
                await Membership.objects.create(
                    club_id=999999, member_id=member.pk, since=FOUNDED, fee=decimal.Decimal(0)
                )
        assert (raised.value.detail, raised.value.table_name) == ("FOREIGN KEY constraint failed", None)
        assert await Membership.objects.filter(club_id=999999).count() == 0
        assert not await in_thread(lambda: other_writer_is_refused(sqlite.path))
    assert pool.in_use == 0


@pytest.mark.parametrize(
    "name", [":memory:", "file:shared?mode=memory&cache=shared", "file::memory:", "file::memory:?cache=shared"]
)
async def test_an_in_memory_database_is_refused(sqlite, name):
    pool = sqlite(NAME=name)
    with pytest.raises(adorm.NotSupportedError, match="needs a SQLite database file"):
        await Club.objects.count()
    assert pool.size == 0
    assert pool.driver.database_key(pool.profile) is None


async def test_the_schema_check_compares_columns_types_and_nullability(sqlite, tmp_path):
    path = tmp_path / "drifted.sqlite3"

    def drift() -> None:
        db = sqlite3.connect(path)
        try:
            db.execute(
                "CREATE TABLE clubs_club (id integer NOT NULL PRIMARY KEY, name varchar(50) NOT NULL,"
                " founded date NULL, city varchar(100) NOT NULL, address text NOT NULL, disciplines text NOT NULL,"
                " nickname TEXT NOT NULL)"
            )
            db.commit()
        finally:
            db.close()

    await in_thread(drift)
    sqlite(NAME=str(path))
    fields = ("id", "name", "slug", "founded", "city", "address", "disciplines")
    problems = await adorm.create_session().execute(CheckTable("clubs.Club", fields))
    found = {(problem["kind"], problem["column"]): (problem["expected"], problem["actual"]) for problem in problems}
    assert found == {
        ("column_type", "name"): ("varchar(100)", "varchar(50)"),
        ("missing_column", "slug"): (None, None),
        ("nullability", "founded"): ("NOT NULL", "NULL"),
        ("required_column", "nickname"): (None, "TEXT"),
    }
    missing = await adorm.create_session().execute(CheckTable("clubs.Member", ("id",)))
    assert [problem["kind"] for problem in missing] == ["missing_table"]


class SharedConnectionOnSQLiteTests(TestCase):
    """Django's ``TestCase`` on SQLite: the slots share the test's connection."""

    @classmethod
    def setUpClass(cls):
        if not on_sqlite():
            raise unittest.SkipTest("the test database is not SQLite")
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        from tests.djangoproject.clubs import models as dj

        joined = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        clubs = [
            dj.Club.objects.create(name=f"C{n}", slug=f"c{n}", founded=FOUNDED, city="X", address={}, disciplines=[])
            for n in range(3)
        ]
        members = dj.Member.objects.bulk_create(
            dj.Member(first_name="M", last_name=str(n), email=f"m{n}@example.com", joined=joined) for n in range(2000)
        )
        dj.Membership.objects.bulk_create(
            dj.Membership(club=club, member=member, since=FOUNDED, fee=0) for club in clubs for member in members
        )

    async def run_on_pool(self, test, **kwargs):
        driver = DjangoDriver()
        driver.cancel_grace = 30
        pool = Pool(driver, ConnectionProfile("default", driver), acquire_timeout=5, **kwargs)
        router = Router([pool])
        previous = set_runner(router)
        try:
            await test(pool)
        finally:
            set_runner(previous)
            await router.close()

    async def test_cancelling_a_call_waiting_for_the_shared_connection(self):
        async def test(pool):
            assert await Club.objects.count() == 3
            (slot,) = pool._slots
            connection = slot.connection
            for _ in REPEAT:
                holder = SimpleNamespace(_cancel_waiting=None, _current=None)
                connection._shared.acquire(holder)
                try:
                    task = asyncio.create_task(Club.objects.count())
                    async with asyncio.timeout(10):
                        while connection._current is None:
                            await asyncio.sleep(0.005)
                    task.cancel()
                    async with asyncio.timeout(5):
                        with pytest.raises(asyncio.CancelledError):
                            await task
                finally:
                    connection._shared.release(holder)
            assert pool.size == 1
            assert await Club.objects.count() == 3

        await self.run_on_pool(test, max_connections=1)

    async def test_a_cancellation_racing_the_start_of_the_statement_stops_it(self):
        async def test(pool):
            assert await Club.objects.count() == 3
            for spins in range(12):
                task = asyncio.create_task(slow_count())
                for _ in range(spins):
                    await asyncio.sleep(0)
                task.cancel()
                async with asyncio.timeout(5):
                    with pytest.raises(asyncio.CancelledError):
                        await task
                assert (pool.size, pool.in_use) == (1, 0)
            # A cancelled read leaves the test's transaction usable.
            assert await Club.objects.count() == 3

        await self.run_on_pool(test, max_connections=1)


async def test_a_query_django_refuses_before_any_sql_leaves_the_transaction_usable(sqlite):
    sqlite()
    async with adorm.atomic():
        await Club.objects.filter(slug="koper-sailing").update(city="Kept")
        with pytest.raises(adorm.NotSupportedError, match="clause.combinator.slice") as raised:
            await Club.objects.order_by("name")[:1].union(Club.objects.all()).count()
        assert raised.value.original is None
        assert await Club.objects.filter(city="Kept").count() == 1
    assert await Club.objects.filter(city="Kept").count() == 1
