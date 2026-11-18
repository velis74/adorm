"""`adorm.testing.wait_for_lock()` and `wait_for_operation()` on every driver: an operation waiting for a lock that
another session holds is reported with its blocker, also inside a transaction, and the slot of an operation cancelled
while it waits goes back to the pool."""

import asyncio
import datetime

from decimal import Decimal

import asyncpg
import pytest

import adorm

from adorm import ConnectionProfile, TransactionOutcome
from adorm.testing import running_operations, wait_for_lock, wait_for_operation
from tests.clubmodels.clubs import Member, Membership
from tests.conformance.drivers import on_sqlite

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _monitored():
    """These tests read the database's sessions, which SQLite does not have."""
    if on_sqlite():
        pytest.skip("SQLite has no database sessions to monitor")


REPEAT = range(3)


async def test_a_waiting_operation_is_reported_with_the_session_that_blocks_it(make_router, blocker):
    pool = make_router(max_connections=2, acquire_timeout=5).pools[0]
    for _ in REPEAT:
        await blocker.hold()
        task = asyncio.create_task(blocker.blocked("koper-sailing"))
        wait = await wait_for_lock(task)
        assert wait.running.sender is task
        assert wait.running.running
        assert wait.operation.kind in ("count", "update")
        assert blocker.holder_pid() in wait.blocking_pids
        assert wait.running.backend_pid == wait.activity.pid == pool.leases()[0].slot.connection.backend_pid
        await blocker.release()
        await task
        assert pool.in_use == 0
        assert running_operations() == ()


@pytest.mark.parametrize("repeat", REPEAT)
async def test_cancelling_an_operation_once_it_waits_returns_its_slot(make_router, blocker, repeat):  # noqa: ARG001
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]
    await blocker.hold()
    task = asyncio.create_task(blocker.blocked("koper-sailing"))
    await wait_for_lock(task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await blocker.idle()
    assert pool.in_use == 0
    await blocker.release()
    async with asyncio.timeout(5):
        await blocker.blocked("koper-sailing")


async def test_an_operation_inside_a_transaction_is_reported_and_rolled_back_when_cancelled(make_router, blocker):
    pool = make_router(max_connections=2, acquire_timeout=5).pools[0]
    outcomes = []

    async def flow():
        try:
            async with adorm.atomic() as tx:
                await Member.objects.count()
                await asyncio.create_task(blocker.blocked("koper-sailing"))  # a task of its own inside the block
        finally:
            outcomes.append(tx.outcome)

    for _ in REPEAT:
        await blocker.hold()
        task = asyncio.create_task(flow())
        wait = await wait_for_lock()
        assert wait.running.sender is not task  # sent by the task inside the block
        assert wait.running.session is wait.running.slot.lease.session
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await blocker.release()
        await blocker.transactions_ended()
        assert pool.in_use == 0
    assert outcomes == [TransactionOutcome.ROLLED_BACK] * len(REPEAT)


async def test_an_operation_is_seen_once_it_reaches_the_driver(make_router, blocker):
    make_router(max_connections=1, acquire_timeout=5)
    await blocker.hold()
    task = asyncio.create_task(blocker.blocked("koper-sailing"))
    running = await wait_for_operation(task)
    assert running.sender is task
    assert isinstance(running.backend_pid, int)
    await blocker.release()
    await task


async def test_a_monitor_reads_the_sessions_of_the_database(driver, blocker):
    await blocker.hold()
    async with await driver.open_monitor(ConnectionProfile("default", driver)) as monitor:
        holder = blocker.holder_pid()
        activity = await monitor.activity([holder, 2_000_000_000])
        assert list(activity) == [holder]
        assert activity[holder].pid == holder
        assert not activity[holder].waiting_for_lock
    await monitor.close()  # a second close does nothing
    await blocker.release()


async def test_a_commit_waiting_for_a_lock_is_reported(conformance_driver, make_router, club_data):
    """A deferred foreign key takes its lock on the referenced row at ``COMMIT``."""
    if conformance_driver.name == "memory":
        pytest.skip("the memory driver checks no foreign key at commit")
    pool = make_router(max_connections=2, acquire_timeout=5).pools[0]
    holder = await asyncpg.connect(club_data)
    try:
        club_id = await holder.fetchval("SELECT id FROM clubs_club WHERE slug = 'koper-sailing'")
        member_id = await holder.fetchval(
            "SELECT id FROM clubs_member WHERE id NOT IN (SELECT member_id FROM clubs_membership WHERE club_id = $1)"
            " ORDER BY id LIMIT 1",
            club_id,
        )
        for _ in REPEAT:
            await Membership.objects.filter(club_id=club_id, member_id=member_id).delete()
            async with holder.transaction():
                await holder.execute("SELECT id FROM clubs_club WHERE id = $1 FOR UPDATE", club_id)

                async def join():
                    async with adorm.atomic():
                        await Membership.objects.create(
                            club_id=club_id, member_id=member_id, since=datetime.date(2026, 1, 1), fee=Decimal(0)
                        )

                task = asyncio.create_task(join())
                wait = await wait_for_lock(task, timeout=5)
                assert wait.request.method == "commit"
                assert wait.operation is None
                assert wait.running.sender is task
                assert holder.get_server_pid() in wait.blocking_pids
            await task
            assert pool.in_use == 0
            assert await Membership.objects.filter(club_id=club_id, member_id=member_id).count() == 1
    finally:
        await holder.close()
