"""Test mode: the Django driver inside Django's ``TestCase`` (slots on the test's connection, transactions as
savepoints, overlapping transactions taking turns) and ``TransactionTestCase`` (connections of their own), under
Django's async test methods (the event loop on a thread of its own) and pytest-asyncio (the event loop on the test's
thread).

The failures that matter: flows racing for the shared connection, a cancelled flow that waits for it or runs on it, a
failed operation that would break the test's transaction for every other flow, options that leak between slots,
transactions that overlap, wait for each other or outlive the one they began in, and slots that outlive their
test."""

import asyncio
import contextvars
import os
import re
import subprocess
import sys
import threading

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest

from asgiref.sync import async_to_sync, sync_to_async
from django.db import connections, transaction
from django.db.models.signals import post_save
from django.test import TestCase, TransactionTestCase

import adorm

from adorm import (
    ConnectionProfile,
    create_session,
    DeadlockError,
    IntegrityError,
    InterfaceError,
    NotSupportedError,
    OperationalError,
    TransactionClosed,
    TransactionManagementError,
    TransactionOverlapError,
)
from adorm.drivers.asyncpg import AsyncpgDriver
from adorm.drivers.django import backend_pid, DjangoConnection, DjangoDriver
from adorm.events import BackendEvent, NOTICE
from adorm.executor import CloseReason, Pool, Router, set_runner
from adorm.testing import running_operations, wait_for_lock, wait_for_operation
from adorm.testing.django import (
    find_test_database,
    in_test_environment,
    open_testcase_atomics,
    QUERY_CANCELED,
    shared_connection_class,
    shared_test_connection,
    shared_transaction_turns,
    SharedConnectionMixin,
    SharedTestConnection,
    TestDatabase,
)
from tests.djangoproject.library import models as dj
from tests.djangoproject.settings import PARALLEL_DATABASE
from tests.generated.library import Reader, Tag

REPEAT = range(3)
ROOT = Path(__file__).parent.parent


@asynccontextmanager
async def executor(driver=None, **kwargs):
    """A router with one pool for the profile ``default`` as the runner of operations; yields the pool."""
    driver = driver or DjangoDriver()
    hooks = {key: kwargs.pop(key) for key in ("on_acquire", "on_release") if key in kwargs}
    kwargs.setdefault("max_connections", 4)
    kwargs.setdefault("acquire_timeout", 5)
    pool = Pool(driver, ConnectionProfile("default", driver, **hooks), **kwargs)
    router = Router([pool])
    previous = set_runner(router)
    try:
        yield pool
    finally:
        set_runner(previous)
        await router.close()


@asynccontextmanager
async def outside():
    """A connection of the test's own to the test database, outside every transaction of the test."""
    entry = connections.settings["default"]
    conn = await asyncpg.connect(
        host=entry["HOST"] or None,
        port=int(entry["PORT"] or 5432),
        user=entry["USER"] or None,
        password=entry["PASSWORD"] or None,
        database=entry["NAME"],
    )
    try:
        yield conn
    finally:
        await conn.close()


async def waiting_for_lock(monitor):
    """Return once a backend of the test database waits for a lock."""
    async with asyncio.timeout(10):
        while not await monitor.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND datname = current_database()"
        ):
            await asyncio.sleep(0.01)


async def first_of(waited, task):
    """Wait for ``waited``; raise what ``task`` raises if it ends first."""
    waiter = asyncio.ensure_future(waited)
    await asyncio.wait({waiter, task}, return_when=asyncio.FIRST_COMPLETED)
    if not waiter.done():
        waiter.cancel()
        task.result()
        raise AssertionError(f"{task!r} ended first")


async def until(condition):
    async with asyncio.timeout(10):
        while not condition():
            await asyncio.sleep(0.005)


def tags(session=None):
    return (session or adorm.current_session()).bind(Tag)


async def tag_names(session=None):
    return sorted([tag.name async for tag in tags(session).all()])


def names_on_test_thread():
    """The tags as the test's own thread sees them."""
    return sorted(dj.Tag.objects.values_list("name", flat=True))


async def committed_names():
    async with outside() as conn:
        return sorted(r["name"] for r in await conn.fetch("SELECT name FROM library_tag"))


def wrapper_of_test():
    return connections["default"]


# Under Django's TestCase, with async test methods: the event loop runs on a thread of its own.


class SharedConnectionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        dj.Tag.objects.create(name="seed")

    async def test_slots_share_the_test_connection(self):
        wrapper = await sync_to_async(wrapper_of_test)()
        async with executor() as pool:
            counts = await asyncio.gather(*(tags().filter(name="seed").count() for _ in range(8)))
            assert counts == [1] * 8
            assert len(pool._slots) == 4
            for slot in pool._slots:
                assert isinstance(slot.connection, SharedConnectionMixin)
                assert isinstance(slot.connection, DjangoConnection)
                assert slot.connection.db is wrapper
                assert slot.connection.thread is not threading.main_thread()
            assert len(wrapper.execute_wrappers) == 1
        assert not wrapper.allow_thread_sharing
        assert wrapper.execute_wrappers == []

    async def assert_writes_stay_in_the_test(self, name):
        async with executor():
            await tags().create(name=name)
            assert await tag_names() == sorted(["seed", name])
        assert await sync_to_async(names_on_test_thread)() == sorted(["seed", name])
        assert await committed_names() == []

    # Each test sees only its own write: the other's was rolled back with its test.

    async def test_writes_stay_in_the_test_a(self):
        await self.assert_writes_stay_in_the_test("aa")

    async def test_writes_stay_in_the_test_b(self):
        await self.assert_writes_stay_in_the_test("bb")

    async def test_a_transaction_is_a_savepoint_in_the_test_transaction(self):
        wrapper = await sync_to_async(wrapper_of_test)()
        base = list(wrapper.savepoint_ids)
        async with executor():
            async with adorm.atomic():
                await tags().create(name="kept")
                assert wrapper.savepoint_ids[: len(base)] == base
                assert len(wrapper.savepoint_ids) == len(base) + 1
            assert wrapper.savepoint_ids == base
            with pytest.raises(KeyError):
                async with adorm.atomic():
                    await tags().create(name="dropped")
                    raise KeyError
            assert await tag_names() == ["kept", "seed"]
        assert await sync_to_async(names_on_test_thread)() == ["kept", "seed"]
        assert await committed_names() == []
        assert wrapper.in_atomic_block
        assert len(open_testcase_atomics(wrapper)) == 2

    async def test_overlapping_transactions_wait_for_the_open_one(self):
        async with executor():
            turns = await turns_of()
            for i in REPEAT:
                first, second = create_session("default", f"first-{i}"), create_session("default", f"second-{i}")
                entered, finish = asyncio.Event(), asyncio.Event()
                seen = []

                async def hold(session=first, entered=entered, finish=finish, i=i):
                    async with session.atomic():
                        await tags(session).create(name=f"first-{i}")
                        entered.set()
                        await finish.wait()

                async def follow(session=second, seen=seen, i=i):
                    async with session.atomic():
                        seen.extend(await tag_names(session))
                        await tags(session).create(name=f"second-{i}")

                task = asyncio.create_task(hold())
                await first_of(entered.wait(), task)
                waiter = asyncio.create_task(follow())
                # An operation outside a transaction would run inside the open one: it waits too.
                count = asyncio.create_task(tags(second).filter(name__startswith="first-").count())
                await until(lambda: turns.waiting == 2)
                assert not waiter.done()
                assert not count.done()
                finish.set()
                await asyncio.gather(task, waiter)
                assert f"first-{i}" in seen
                assert await count == i + 1
            assert await tag_names() == sorted(["seed", *(f"{n}-{i}" for n in ("first", "second") for i in REPEAT)])
            await idle(turns)

    async def test_racing_transactions_run_one_after_the_other(self):
        async with executor():
            for i in REPEAT:
                log = []

                async def flow(n, i=i, log=log):
                    session = create_session("default", f"race-{i}-{n}")
                    async with session.atomic():
                        log.append(("begin", n))
                        await tags(session).create(name=f"race-{i}-{n}")
                        count = await tags(session).filter(name__startswith=f"race-{i}-").count()
                        log.append(("end", n))
                        return count

                async def read(i=i):
                    return await tags(create_session("default", "reader")).filter(name__startswith=f"race-{i}-").count()

                results = await asyncio.gather(flow(0), flow(1), read(), flow(2), read())
                # Each transaction saw the writes of those before it and none of a later one.
                assert sorted(results[i] for i in (0, 1, 3)) == [1, 2, 3]
                assert all(0 <= results[i] <= 3 for i in (2, 4))
                assert [kind for kind, _ in log] == ["begin", "end"] * 3
                assert all(log[k][1] == log[k + 1][1] for k in range(0, 6, 2))
            assert await tags().filter(name__startswith="race-").count() == 3 * len(REPEAT)

    async def test_waiting_flows_run_in_the_order_they_came(self):
        holder = create_session("default", "holder")
        async with executor():
            turns = await turns_of()
            for i in REPEAT:
                entered, finish = asyncio.Event(), asyncio.Event()
                order = []

                async def hold(entered=entered, finish=finish, order=order):
                    async with holder.atomic():
                        await tags(holder).count()
                        entered.set()
                        await finish.wait()
                        order.append("holder")

                async def transaction(n, order=order, i=i):
                    session = create_session("default", f"tx-{n}")
                    async with session.atomic():
                        order.append(f"tx-{n}")
                        await tags(session).create(name=f"tx-{i}-{n}")

                async def operation(n, order=order):
                    count = await tags(create_session("default", f"op-{n}")).count()
                    order.append(f"op-{n}")
                    return count

                task = asyncio.create_task(hold())
                await first_of(entered.wait(), task)
                flows = []
                for n, start in enumerate([transaction, operation, operation, transaction, operation]):
                    flows.append(asyncio.create_task(start(n)))
                    await until(lambda n=n: turns.waiting == n + 1)
                finish.set()
                await asyncio.gather(task, *flows)
                # Operations that came together run together; none passes a transaction that came before it.
                assert order[:2] == ["holder", "tx-0"]
                assert sorted(order[2:4]) == ["op-1", "op-2"]
                assert order[4:] == ["tx-3", "op-4"]
                assert await flows[4] == 1 + 2 * (i + 1)
            await idle(turns)

    async def test_cancelling_a_flow_that_waits_for_an_open_transaction(self):
        holder = create_session("default", "holder")
        async with executor() as pool:
            turns = await turns_of()
            for i in REPEAT:
                entered, finish = asyncio.Event(), asyncio.Event()

                async def hold(entered=entered, finish=finish, i=i):
                    async with holder.atomic():
                        await tags(holder).create(name=f"held-{i}")
                        entered.set()
                        await finish.wait()

                async def never(i=i):
                    async with create_session("default", "never").atomic():
                        await tags(create_session("default", "never")).create(name=f"never-{i}")

                task = asyncio.create_task(hold())
                await first_of(entered.wait(), task)
                waiting = [asyncio.create_task(never()), asyncio.create_task(tags().create(name=f"never-op-{i}"))]
                after = asyncio.create_task(tags().create(name=f"after-{i}"))
                await until(lambda: turns.waiting == 3)
                for flow in waiting:
                    flow.cancel()
                for flow in waiting:
                    with pytest.raises(asyncio.CancelledError) as raised:
                        await flow
                    assert adorm.cancellation_outcome(raised.value) is adorm.CancellationOutcome.NOT_STARTED
                assert turns.waiting == 1
                assert not after.done()
                finish.set()
                await asyncio.gather(task, after)
            assert await tag_names() == sorted(["seed", *(f"{n}-{i}" for n in ("held", "after") for i in REPEAT)])
            await idle(turns)
            assert all(not slot.lease for slot in pool._slots)

    async def test_a_wait_for_an_open_transaction_times_out_as_a_deadlock(self):
        holder = create_session("default", "holder")
        async with executor(acquire_timeout=0.2):
            turns = await turns_of()
            for i in REPEAT:
                entered, finish = asyncio.Event(), asyncio.Event()

                async def hold(entered=entered, finish=finish, i=i):
                    async with holder.atomic():
                        await tags(holder).create(name=f"held-{i}")
                        entered.set()
                        await finish.wait()
                        await tags(holder).create(name=f"held-late-{i}")

                task = asyncio.create_task(hold())
                await first_of(entered.wait(), task)
                waiter = create_session("default", "waiter")
                with pytest.raises(DeadlockError, match="stayed held for 0.2 s") as raised:
                    await enter(waiter)
                assert (raised.value.session, raised.value.timeout, raised.value.held_sessions) == (waiter, 0.2, ())
                with pytest.raises(DeadlockError):
                    await tags(waiter).count()
                # A time limit of the flow ends the wait first, as a wait for a slot.
                with adorm.connection_options(acquire_timeout=5):
                    with pytest.raises(adorm.OperationTimeout) as limited:
                        async with adorm.timeout(0.1):
                            await tags(waiter).count()
                assert limited.value.phase == "waiting_for_slot"
                assert turns.waiting == 0
                finish.set()
                await task
            assert await tag_names() == sorted(["seed", *(f"held{n}-{i}" for n in ("", "-late") for i in REPEAT)])

    async def test_a_transaction_that_awaits_a_flow_waiting_for_it_is_a_deadlock(self):
        outer, other = create_session("default", "outer"), create_session("default", "other")
        async with executor(acquire_timeout=60):
            turns = await turns_of()
            for i in REPEAT:
                async with asyncio.timeout(10), outer.atomic():
                    await tags(outer).create(name=f"outer-{i}")
                    # Awaited directly, through gather() and through a task in between.
                    with pytest.raises(DeadlockError, match="awaits") as raised:
                        await asyncio.create_task(enter(other), context=contextvars.Context())
                    assert raised.value.session == other
                    results = await asyncio.gather(
                        asyncio.create_task(tags(other).count(), context=contextvars.Context()),
                        tags(outer).filter(name=f"outer-{i}").count(),
                        return_exceptions=True,
                    )
                    assert isinstance(results[0], DeadlockError)
                    assert results[1] == 1

                    async def middle():
                        await asyncio.create_task(enter(other), context=contextvars.Context())

                    with pytest.raises(DeadlockError):
                        await asyncio.create_task(middle())
                    # A flow that waits first and is awaited afterwards.
                    late = asyncio.create_task(enter(other), context=contextvars.Context())
                    await until(lambda: turns.waiting == 1)
                    with pytest.raises(DeadlockError):
                        await late
                    await tags(outer).create(name=f"outer-late-{i}")
                await idle(turns)
            assert await tag_names() == sorted(["seed", *(f"outer{n}-{i}" for n in ("", "-late") for i in REPEAT)])

    async def test_a_task_that_outlives_its_transaction_takes_turns_afterwards(self):
        outer, holder = create_session("default", "outer"), create_session("default", "holder")
        async with executor():
            turns = await turns_of()
            for i in REPEAT:
                created, go_on, entered, finish = asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()
                seen = []

                async def outliving(created=created, go_on=go_on, seen=seen, i=i):
                    # Inside the transaction: nested in it, committed with it.
                    await tags(create_session("default", "inside")).create(name=f"inside-{i}")
                    created.set()
                    await go_on.wait()
                    with pytest.raises(TransactionClosed):
                        await tags(outer).count()
                    # Outside the ended transaction: its work waits for the open one.
                    await tags(create_session("default", "late")).create(name=f"late-{i}")
                    async with create_session("default", "late").atomic():
                        seen.extend(await tag_names(create_session("default", "late")))

                async def hold(entered=entered, finish=finish, i=i):
                    async with holder.atomic():
                        await tags(holder).create(name=f"held-{i}")
                        entered.set()
                        await finish.wait()

                async with outer.atomic():
                    await tags(outer).create(name=f"outer-{i}")
                    task = asyncio.create_task(outliving())
                    await first_of(created.wait(), task)
                holding = asyncio.create_task(hold())
                await first_of(entered.wait(), holding)
                go_on.set()
                await until(lambda: turns.waiting == 1)
                assert not task.done()
                finish.set()
                await asyncio.gather(holding, task)
                assert {f"outer-{i}", f"inside-{i}", f"held-{i}", f"late-{i}"} <= set(seen)
            await idle(turns)

    async def test_a_transaction_begun_inside_another_is_nested(self):
        outer, inner = create_session("default", "outer"), create_session("default", "inner")
        async with executor():
            async with outer.atomic():
                await tags(outer).create(name="o1")
                async with inner.atomic():
                    await tags(inner).create(name="i1")
                # A task inherits the outer transaction: its own transaction nests in it.
                task_done = asyncio.Event()

                async def child():
                    async with inner.atomic():
                        await tags(inner).create(name="i2")
                    task_done.set()

                await asyncio.create_task(child())
                assert task_done.is_set()
                with pytest.raises(KeyError):
                    async with inner.atomic():
                        await tags(inner).create(name="dropped")
                        raise KeyError
                await tags(outer).create(name="o2")
            assert await tag_names() == ["i1", "i2", "o1", "o2", "seed"]

    async def test_an_inner_transaction_that_outlives_the_outer_one_raises_in_both(self):
        outer, inner = create_session("default", "outer"), create_session("default", "inner")
        async with executor() as pool:
            for _ in REPEAT:
                opened, go_on = asyncio.Event(), asyncio.Event()

                async def late(opened=opened, go_on=go_on):
                    async with inner.atomic():
                        await tags(inner).create(name="inner")
                        opened.set()
                        await go_on.wait()
                        await tags(inner).create(name="late")

                with pytest.raises(TransactionOverlapError):
                    async with outer.atomic():
                        await tags(outer).create(name="outer")
                        task = asyncio.create_task(late())
                        await opened.wait()
                go_on.set()
                with pytest.raises(TransactionOverlapError):
                    await task
                assert await tag_names() == ["seed"]
                assert pool._slots
                assert not shared_of(pool).transactions

    async def test_a_failed_operation_leaves_the_test_transaction_usable(self):
        async with executor():
            for i in REPEAT:
                with pytest.raises(IntegrityError):
                    await tags().create(name="seed")
                await tags().create(name=f"after-{i}")
            assert await tag_names() == ["after-0", "after-1", "after-2", "seed"]

    async def test_a_failed_transaction_leaves_the_shared_connection_usable(self):
        failing, nested = create_session("default", "failing"), create_session("default", "nested")
        async with executor():
            turns = await turns_of()
            for i in REPEAT:
                failed, done = asyncio.Event(), asyncio.Event()

                async def fail(failed=failed, done=done, i=i):
                    async with failing.atomic():
                        await tags(failing).create(name=f"lost-{i}")
                        try:
                            await tags(failing).create(name="seed")
                        except IntegrityError:
                            # Rolled back to its savepoint at once: a flow inside it goes on.
                            child = asyncio.create_task(tags(nested).create(name=f"nested-{i}"))
                            await child
                            names = await asyncio.create_task(tag_names(nested))
                            assert names == sorted(["seed", f"nested-{i}", *(f"after-{n}" for n in range(i))])
                            failed.set()
                            await done.wait()
                            with pytest.raises(TransactionManagementError):
                                await tags(failing).count()
                            raise

                task = asyncio.create_task(fail())
                await first_of(failed.wait(), task)
                count = asyncio.create_task(tags().count())
                await until(lambda: turns.waiting == 1)
                done.set()
                with pytest.raises(IntegrityError):
                    await task
                assert await count == 1 + i
                await tags().create(name=f"after-{i}")
            assert await tag_names() == ["after-0", "after-1", "after-2", "seed"]

    async def test_an_abandoned_transaction_is_aborted_and_frees_the_shared_connection(self):
        stuck, other = create_session("default", "stuck"), create_session("default", "other")
        async with executor(commit_timeout=0.2) as pool:
            for i in REPEAT:
                written, release = asyncio.Event(), asyncio.Event()

                async def abandon(written=written, release=release, i=i):
                    async with stuck.atomic():
                        await tags(stuck).create(name=f"stuck-{i}")
                        written.set()
                        await release.wait()
                        await tags(stuck).count()

                task = asyncio.create_task(abandon())
                await first_of(written.wait(), task)
                shared = shared_of(pool)
                await until(lambda shared=shared: not shared.transactions)
                async with other.atomic():
                    await tags(other).create(name=f"other-{i}")
                release.set()
                with pytest.raises(TransactionClosed):
                    await task
            assert await tag_names() == ["other-0", "other-1", "other-2", "seed"]

    async def test_a_failed_savepoint_lets_the_transaction_go_on(self):
        async with executor():
            async with adorm.atomic():
                await tags().create(name="x1")
                with pytest.raises(IntegrityError):
                    async with adorm.atomic():
                        await tags().create(name="x2")
                        await tags().create(name="seed")
                await tags().create(name="x3")
            assert await tag_names() == ["seed", "x1", "x3"]

    async def test_concurrent_flows_take_turns_on_the_shared_connection(self):
        async with executor() as pool:
            for i in REPEAT:

                async def flow(n, i=i):
                    session = create_session("default", f"flow-{n}")
                    await tags(session).create(name=f"t-{i}-{n}")
                    return await tags(session).filter(name__startswith=f"t-{i}-").count()

                counts = await asyncio.gather(*(flow(n) for n in range(12)))
                assert all(1 <= count <= 12 for count in counts)
                assert await tags().filter(name__startswith=f"t-{i}-").count() == 12
                # Flows inside a transaction take turns with it.
                session = create_session("default", "transaction")
                async with session.atomic():
                    await tags(session).create(name=f"tx-{i}")
                    counts = await asyncio.gather(*(flow(n + 100) for n in range(12)))
                    assert all(13 <= count <= 24 for count in counts)
                    assert await tags(session).filter(name__startswith=f"t-{i}-").count() == 24
            assert pool.size == 4
            assert shared_of(pool).owner is None

    async def test_cancelling_a_flow_that_waits_for_the_shared_connection(self):
        async with executor(max_connections=1) as pool:
            await tags().count()
            shared = shared_of(pool)
            connection = next(iter(pool._slots)).connection
            for i in REPEAT:
                holder = SimpleNamespace(_cancel_waiting=None, _current=None)
                shared.acquire(holder)
                try:
                    task = asyncio.create_task(tags().create(name=f"never-{i}"))
                    await until(lambda: connection._current is not None)
                    task.cancel()
                    async with asyncio.timeout(5):
                        with pytest.raises(asyncio.CancelledError):
                            await task
                finally:
                    shared.release(holder)
                assert await tag_names() == ["seed"]
            assert pool.size == 1

    async def test_cancelling_a_running_statement_frees_the_shared_connection(self):
        async with executor() as pool, outside() as holder, outside() as monitor:
            for i in REPEAT:
                lock = holder.transaction()
                await lock.start()
                await holder.execute("LOCK TABLE library_reader IN ACCESS EXCLUSIVE MODE")
                try:
                    blocked = asyncio.create_task(create_session("default", "blocked").bind(Reader).count())
                    await waiting_for_lock(monitor)
                    # Every other flow waits behind the running statement.
                    other = asyncio.create_task(tags().filter(name="seed").count())
                    await until(lambda: len(pool._slots) == 2 and all(s.connection._current for s in pool._slots))
                    assert not other.done()
                    blocked.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await blocked
                    assert await other == 1
                finally:
                    await lock.rollback()
                # The cancelled statement was rolled back to its savepoint: the test's transaction is usable.
                await tags().create(name=f"after-{i}")
            # Read last: the read's lock on the table lasts until the test's transaction ends.
            assert await create_session("default", "after").bind(Reader).count() == 0
            assert await tag_names() == ["after-0", "after-1", "after-2", "seed"]

    async def test_a_statement_waiting_for_a_lock_on_the_shared_connection_is_reported(self):
        test_pid = await sync_to_async(lambda: backend_pid(wrapper_of_test()))()
        async with executor() as pool, outside() as holder:
            for _ in REPEAT:
                lock = holder.transaction()
                await lock.start()
                await holder.execute("LOCK TABLE library_reader IN ACCESS EXCLUSIVE MODE")
                try:
                    blocked = asyncio.create_task(create_session("default", "blocked").bind(Reader).count())
                    assert (await wait_for_operation(blocked)).sender is blocked
                    wait = await wait_for_lock(blocked)
                    assert wait.running.sender is blocked
                    assert wait.running.backend_pid == wait.activity.pid == test_pid
                    assert wait.blocking_pids == (holder.get_server_pid(),)
                    blocked.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await blocked
                finally:
                    await lock.rollback()
            assert pool.in_use == 0
            await tags().create(name="after")
            assert await tag_names() == ["after", "seed"]

    async def test_a_statement_waiting_for_the_shared_connection_is_not_reported_as_in_the_database(self):
        async with executor() as pool, outside() as holder:
            for _ in REPEAT:
                lock = holder.transaction()
                await lock.start()
                await holder.execute("LOCK TABLE library_reader IN ACCESS EXCLUSIVE MODE")
                try:
                    blocked = asyncio.create_task(create_session("default", "blocked").bind(Reader).count())
                    await wait_for_lock(blocked)
                    queued = asyncio.create_task(tags(create_session("default", "queued")).count())
                    while not running_operations(queued):
                        await asyncio.sleep(0.001)
                    [running] = running_operations(queued)
                    assert not running.in_session
                    assert not running.reached_session
                    # The blocked statement holds the test connection; the queued one only waits for it.
                    with pytest.raises(TimeoutError):
                        await wait_for_operation(queued, timeout=0.2)
                    with pytest.raises(TimeoutError):
                        await wait_for_lock(queued, timeout=0.2)
                    # Cancelled, the blocked statement's savepoint and its table lock are rolled back.
                    blocked.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await blocked
                    assert await queued == 1
                    assert running.reached_session
                    assert not running.running
                finally:
                    await lock.rollback()
            assert pool.in_use == 0

    async def test_connection_options_do_not_leak_between_slots(self):
        seen = ShowingConnection.seen = {}

        def count(label, session_name=None):
            return tags(create_session("default", session_name or label)).filter(name=f"opt-{label}").count()

        async with executor(driver=ShowingDriver()):
            with adorm.connection_options(statement_timeout=5):
                await count("five")
            await count("plain")
            for _ in REPEAT:
                # Concurrent slots, each with its own options.

                async def with_options():
                    with adorm.connection_options(statement_timeout=3):
                        await count("three")

                async def without():
                    await count("none")

                await asyncio.gather(*(f() for f in (with_options, without) * 3))
                # Options set inside a transaction end with it.
                with adorm.connection_options(statement_timeout=7):
                    async with create_session("default", "seven").atomic():
                        with adorm.connection_options(statement_timeout=9):
                            await count("nine", "seven")
                        await count("seven")
                await count("after")
        assert seen["five"] == ["5s"]
        assert seen["plain"] == ["0"]
        assert seen["three"] == ["3s"] * 3 * len(REPEAT)
        assert seen["none"] == ["0"] * 3 * len(REPEAT)
        assert seen["nine"] == ["9s"] * len(REPEAT)
        assert seen["seven"] == ["7s"] * len(REPEAT)
        assert seen["after"] == ["0"] * len(REPEAT)

    async def test_options_a_hook_returns_apply_to_each_operation_of_the_lease_only(self):
        seen = ShowingConnection.seen = {}
        user = contextvars.ContextVar("test_user", default=None)

        def on_acquire(_db, _profile, _session):
            return None if user.get() is None else {APP_USER: user.get()}

        async def count(label, name=None):
            token = user.set(name)
            try:
                return await tags(create_session("default", f"{label}-{name}")).filter(name=f"opt-{label}").count()
            finally:
                user.reset(token)

        async with executor(driver=ShowingDriver(setting=APP_USER), on_acquire=on_acquire) as pool:
            for _ in REPEAT:
                await asyncio.gather(*(count("users", name) for name in ("alice", "bob", None) * 3))
                assert await sync_to_async(seen_on_test_connection)() in (None, "")
                token = user.set("carol")
                try:
                    async with create_session("default", "tx").atomic():
                        await count("tx", "carol")
                        with adorm.connection_options(**{APP_USER: "dave"}):
                            await count("block")
                        await count("tx", "carol")
                finally:
                    user.reset(token)
            # NULL before the option is first set on the test's connection, '' after a reset: both mean no user.
            users = [value or "" for value in seen["users"]]
            assert sorted(users) == sorted(["alice", "bob", ""] * 3 * len(REPEAT))
            assert seen["tx"] == ["carol"] * 2 * len(REPEAT)
            assert seen["block"] == ["dave"] * len(REPEAT)
            assert all(not slot.connection.ran_custom_sql for slot in pool._slots)

    async def test_a_driver_default_is_restored_after_another_slot_changes_it(self):
        seen = ShowingConnection.seen = {}

        def start_with_base():
            # As Django assumes OPTIONS["assume_role"] when it connects: a session-level setting.
            with connections["default"].cursor() as cursor:
                cursor.execute(f"SET {APP_USER} = 'base'")

        async def count(label, session_name):
            return await tags(create_session("default", session_name)).filter(name=f"opt-{label}").count()

        async def alice(name):
            with adorm.connection_options(**{APP_USER: "alice"}):
                await count("alice", name)

        await sync_to_async(start_with_base)()
        async with executor(driver=DefaultsDriver(setting=APP_USER)):
            for i in REPEAT:
                await alice(f"alice-{i}")
                await count("plain", f"alice-{i}")
                await count("plain", f"other-{i}")
                assert await sync_to_async(seen_on_test_connection)() == "base"
                await asyncio.gather(alice(f"alice-c{i}"), *(count("plain", f"plain-{i}-{n}") for n in range(6)))
                assert await sync_to_async(seen_on_test_connection)() == "base"
        assert seen["alice"] == ["alice"] * 2 * len(REPEAT)
        assert seen["plain"] == ["base"] * 8 * len(REPEAT)

    async def test_a_hook_option_the_database_refuses_fails_that_acquisition_only(self):
        """As on a connection of its own: the acquisition fails and the slot leaves the pool; the test's transaction,
        other sessions and Django code of the test go on."""
        user = contextvars.ContextVar("test_user", default=None)

        def on_acquire(_db, _profile, _session):
            return {"role": "adorm_test_no_such_role"} if user.get() == "ghost" else None

        async def as_ghost():
            token = user.set("ghost")
            try:
                return await tags(create_session("default", "ghost")).count()
            finally:
                user.reset(token)

        async with executor(on_acquire=on_acquire) as pool:
            before = await tags().count()
            for _ in REPEAT:
                with pytest.raises(adorm.DatabaseError, match="adorm_test_no_such_role"):
                    await as_ghost()
                assert await tags(create_session("default", "other")).count() == before
                assert len(await sync_to_async(names_on_test_thread)()) == before
            assert all(not slot.connection.ran_custom_sql for slot in pool._slots)

    async def test_django_code_runs_with_the_defaults_once_the_last_slot_closes(self):
        def on_acquire(_db, _profile, _session):
            return {APP_USER: "alice"}

        for _ in REPEAT:
            async with executor(driver=ShowingDriver(setting=APP_USER), on_acquire=on_acquire):
                ShowingConnection.seen = {}
                await tags().filter(name="opt-hook").count()
                assert ShowingConnection.seen["hook"] == ["alice"]
            assert await sync_to_async(seen_on_test_connection)() in (None, "")

    async def test_sql_a_hook_runs_on_the_shared_connection_is_refused(self):
        def hook(db, _profile, _session):
            with db.cursor() as cursor:
                cursor.execute(f"SET {APP_USER} = 'alice'")

        async with executor(on_acquire=hook) as pool:
            for _ in REPEAT:
                with pytest.raises(NotSupportedError, match="ran SQL on the shared connection"):
                    await tags().count()
                assert not pool._slots
        async with executor(on_release=hook) as pool:
            for _ in REPEAT:
                async with adorm.atomic() as tx:
                    await tags().count()
                    slot = tx.lease.slot
                await asyncio.wait_for(slot.wait_closed(), 10)
                assert slot.close_reason is CloseReason.HOOK_FAILED
                assert await sync_to_async(seen_on_test_connection)() in (None, "")
            assert pool.size == 0

    async def test_django_code_racing_a_hook_is_not_the_hooks_sql(self):
        gated = threading.Event()
        hook_runs = threading.Event()
        django_waits = threading.Event()

        def hook(_db, _profile, _session):
            if gated.is_set():
                hook_runs.set()
                assert django_waits.wait(10)

        async with executor(on_acquire=hook) as pool:
            await tags().count()
            shared = shared_of(pool)
            acquire = shared.acquire

            def watching_acquire(connection):
                if not isinstance(connection, SharedConnectionMixin):
                    django_waits.set()
                return acquire(connection)

            shared.acquire = watching_acquire
            gated.set()
            for i in REPEAT:
                hook_runs.clear()
                django_waits.clear()
                operation = asyncio.ensure_future(tags(create_session("default", f"racing-{i}")).count())
                await first_of(until(hook_runs.is_set), operation)
                # Django's statement starts while the hook's execute wrapper is installed and waits for the hook.
                django = asyncio.ensure_future(sync_to_async(names_on_test_thread)())
                await operation
                await django
            assert all(not slot.connection.ran_custom_sql for slot in pool._slots)

    def test_capture_on_commit_callbacks_sees_djangos_callbacks_unchanged(self):
        calls = []

        def receiver(instance, **_kwargs):
            transaction.on_commit(lambda: calls.append(instance.name))

        async def write(suffix):
            async with executor():
                await tags().create(name=f"alone-{suffix}")
                async with adorm.atomic():
                    await tags().create(name=f"committed-{suffix}")
                with pytest.raises(KeyError):
                    async with adorm.atomic():
                        await tags().create(name=f"rolled-back-{suffix}")
                        raise KeyError

        post_save.connect(receiver, sender=dj.Tag, weak=False)
        try:
            for i in REPEAT:
                with self.captureOnCommitCallbacks() as callbacks:
                    async_to_sync(write)(f"kept-{i}")
                assert len(callbacks) == 2
                assert calls == []
                with self.captureOnCommitCallbacks(execute=True) as callbacks:
                    async_to_sync(write)(f"run-{i}")
                assert len(callbacks) == 2
                assert calls == [f"alone-run-{i}", f"committed-run-{i}"]
                calls.clear()
        finally:
            post_save.disconnect(receiver, sender=dj.Tag)

    async def test_an_outer_transaction_cannot_operate_while_a_nested_one_is_open(self):
        outer, inner = create_session("default", "outer"), create_session("default", "inner")
        async with executor():
            # The second name would fail: a failure inside the inner transaction's savepoint would break that one.
            for i, refused in enumerate([f"lost-{n}" for n in REPEAT] + ["seed"] * len(REPEAT)):
                opened, go_on = asyncio.Event(), asyncio.Event()
                seen = []

                async def child(opened=opened, go_on=go_on, seen=seen, i=i):
                    async with inner.atomic():
                        await tags(inner).create(name=f"inner-{i}")
                        opened.set()
                        await go_on.wait()
                        await tags(inner).create(name=f"inner-late-{i}")
                        seen.extend(await tag_names(inner))

                # The refused operation fails the outer block, which then rolls back: nothing is lost silently.
                with pytest.raises(TransactionManagementError, match="failed") as raised:
                    async with outer.atomic():
                        await tags(outer).create(name=f"outer-{i}")
                        task = asyncio.create_task(child())
                        try:
                            await first_of(opened.wait(), task)
                            with pytest.raises(TransactionOverlapError):
                                await tags(outer).create(name=refused)
                        finally:
                            go_on.set()
                        await task  # the inner transaction goes on unaffected
                assert not isinstance(raised.value, TransactionOverlapError)
                assert seen == sorted(["seed", f"outer-{i}", f"inner-{i}", f"inner-late-{i}"])
                assert await tag_names() == ["seed"]

    async def test_closing_the_slot_of_an_outer_transaction_rolls_back_the_nested_one(self):
        outer, inner = create_session("default", "outer"), create_session("default", "inner")
        async with executor() as pool:
            for i in REPEAT:
                opened, go_on = asyncio.Event(), asyncio.Event()

                async def child(opened=opened, go_on=go_on, i=i):
                    async with inner.atomic():
                        await tags(inner).create(name=f"inner-{i}")
                        opened.set()
                        await go_on.wait()
                        await tags(inner).create(name=f"inner-late-{i}")

                with pytest.raises(InterfaceError):
                    async with outer.atomic() as tx:
                        await tags(outer).create(name=f"outer-{i}")
                        task = asyncio.create_task(child())
                        try:
                            await first_of(opened.wait(), task)
                            # As when the slot closes its connection after losing it.
                            await tx.lease.slot.connection.close()
                        finally:
                            go_on.set()
                        with pytest.raises(TransactionOverlapError):
                            await task
                assert await tag_names() == ["seed"]
                assert not shared_of(pool).transactions

    async def test_django_orm_code_takes_turns_and_waits_for_open_transactions(self):
        async with executor():
            turns = await turns_of()
            for i in REPEAT:

                async def orm_write(n, i=i):
                    await sync_to_async(dj.Tag.objects.create)(name=f"orm-{i}-{n}")

                async def adorm_write(n, i=i):
                    await tags(create_session("default", f"flow-{n}")).create(name=f"adorm-{i}-{n}")

                await asyncio.gather(*(write(n) for n in range(6) for write in (orm_write, adorm_write)))
                assert await tags().filter(name__endswith=f"-{i}-0").count() == 2
                # Inside an adorm transaction's context, Django code joins its savepoint.
                with pytest.raises(KeyError):
                    async with adorm.atomic():
                        await sync_to_async(dj.Tag.objects.create)(name=f"joined-{i}")
                        assert f"joined-{i}" in await tag_names()
                        raise KeyError
                assert f"joined-{i}" not in await sync_to_async(names_on_test_thread)()
                # Outside it, it waits until the transaction has ended.
                entered, finish = asyncio.Event(), asyncio.Event()

                async def hold(entered=entered, finish=finish, i=i):
                    async with create_session("default", "holder").atomic():
                        await tags(create_session("default", "holder")).create(name=f"held-{i}")
                        entered.set()
                        await finish.wait()
                        await tags(create_session("default", "holder")).create(name=f"held-late-{i}")

                task = asyncio.create_task(hold())
                await first_of(entered.wait(), task)
                reading = asyncio.create_task(sync_to_async(names_on_test_thread)())
                await until(lambda: turns.waiting == 1)
                assert not reading.done()
                finish.set()
                await task
                names = await reading
                assert {f"held-{i}", f"held-late-{i}"} <= set(names)
                await idle(turns)
            assert await tags().filter(name__startswith="orm-").count() == 6 * len(REPEAT)
            assert await tags().filter(name__startswith="adorm-").count() == 6 * len(REPEAT)

    async def test_django_orm_code_racing_an_adorm_transaction_runs_after_it(self):
        writer = create_session("default", "writer")
        async with executor():
            turns = await turns_of()
            for i in REPEAT:
                entered, finish = asyncio.Event(), asyncio.Event()

                async def transaction(entered=entered, finish=finish, i=i):
                    async with writer.atomic():
                        await tags(writer).create(name=f"tx-{i}")
                        entered.set()
                        await finish.wait()

                async def orm(entered=entered, i=i):
                    await entered.wait()
                    await sync_to_async(dj.Tag.objects.create)(name=f"orm-{i}")
                    return await sync_to_async(lambda: dj.Tag.objects.filter(name__endswith=f"-{i}").count())()

                async def release(finish=finish):
                    await until(lambda: turns.waiting == 1)
                    finish.set()

                results = await asyncio.gather(transaction(), orm(), release())
                assert results[1] == 2
                await idle(turns)

    async def test_django_orm_code_waiting_past_the_timeout_raises_deadlock_error(self):
        holder = create_session("default", "holder")
        async with executor(acquire_timeout=0.2):
            turns = await turns_of()
            for i in range(3):
                entered, finish = asyncio.Event(), asyncio.Event()

                async def hold(entered=entered, finish=finish, i=i):
                    async with holder.atomic():
                        await tags(holder).create(name=f"held-{i}")
                        entered.set()
                        await finish.wait()

                task = asyncio.create_task(hold())
                await first_of(entered.wait(), task)
                with pytest.raises(DeadlockError, match="statement of Django code") as raised:
                    await sync_to_async(names_on_test_thread)()
                assert raised.value.timeout == 0.2
                assert "TransactionTestCase" in str(raised.value)
                assert turns.waiting == 0
                finish.set()
                await task
                await idle(turns)
            assert await tag_names() == sorted(["seed", *(f"held-{i}" for i in range(3))])

    async def test_a_slot_that_outlives_its_test_leaves_the_pool(self):
        wrapper = await sync_to_async(wrapper_of_test)()
        async with executor(max_connections=1) as pool:
            await tags().count()
            (slot,) = pool._slots
            connection = slot.connection
            # As after the test has ended: other TestCase atomic blocks on the test's connection.
            connection._test = TestDatabase("default", wrapper, (object(),))
            assert not connection.is_usable()
            assert await tags().count() == 1
            assert slot.close_reason is CloseReason.UNUSABLE
            assert slot not in pool._slots
        assert wrapper.connection is not None
        assert not wrapper.allow_thread_sharing
        assert await sync_to_async(names_on_test_thread)() == ["seed"]

    async def test_a_direct_driver_shares_the_test_connection(self):
        wrapper = await sync_to_async(wrapper_of_test)()
        async with executor(driver=DirectDjangoDriver(), max_connections=1) as pool:
            async with adorm.atomic():
                await tags().create(name="direct")
            assert await tag_names() == ["direct", "seed"]
            assert next(iter(pool._slots)).connection.db is wrapper
        assert not wrapper.allow_thread_sharing
        assert await sync_to_async(names_on_test_thread)() == ["direct", "seed"]

    async def test_notices_reach_the_slot_that_holds_the_shared_connection(self):
        await sync_to_async(create_raising_trigger)()
        async with executor() as pool:
            seen = []
            pool.events.subscribe(BackendEvent, seen.append, name=NOTICE)
            for _ in REPEAT:
                seen.clear()
                await asyncio.gather(*(tags().filter(name="seed").update(name="seed") for _ in range(4)))
                assert [event.payload["message"] for event in seen] == ["tags updated"] * 4
        await sync_to_async(drop_raising_trigger)()

    async def test_listening_on_the_shared_connection_is_refused(self):
        async with executor(max_connections=1) as pool:
            with pytest.raises(NotSupportedError, match="TransactionTestCase") as raised:
                async with adorm.listen("orders"):
                    pytest.fail("entered")
            assert raised.value.unsupported == ("notify",)
            # Refused before a slot is taken.
            assert (pool.size, pool.in_use) == (0, 0)
            assert await tags().count() == 1

    async def test_the_native_postgresql_driver_refuses_to_run(self):
        async with executor(driver=AsyncpgDriver()):
            with pytest.raises(NotSupportedError, match="TransactionTestCase"):
                await tags().count()

    async def test_a_profile_with_params_cannot_share_the_test_connection(self):
        driver = DjangoDriver()
        pool = Pool(driver, ConnectionProfile("default", driver, params={"USER": "someone-else"}))
        router = Router([pool])
        previous = set_runner(router)
        try:
            with pytest.raises(NotSupportedError, match="params"):
                await tags().count()
        finally:
            set_runner(previous)
            await router.close()


def create_raising_trigger() -> None:
    """A trigger raising a NOTICE for every update of tags, created in the test's transaction."""
    with connections["default"].cursor() as cursor:
        cursor.execute(
            "CREATE FUNCTION adorm_test_tag_notice() RETURNS trigger LANGUAGE plpgsql AS $$"
            " BEGIN RAISE NOTICE 'tags updated'; RETURN NULL; END $$;"
            f" CREATE TRIGGER adorm_test_tag_notice AFTER UPDATE ON {dj.Tag._meta.db_table}"
            " FOR EACH STATEMENT EXECUTE FUNCTION adorm_test_tag_notice()"
        )


def drop_raising_trigger() -> None:
    with connections["default"].cursor() as cursor:
        cursor.execute(
            f"DROP TRIGGER adorm_test_tag_notice ON {dj.Tag._meta.db_table}; DROP FUNCTION adorm_test_tag_notice()"
        )


async def turns_of(alias="default"):
    """The turns of the test's shared connection of ``alias``."""
    turns = await shared_transaction_turns(alias)
    assert turns is not None
    return turns


async def idle(turns):
    """Return once no turn is held or waits: the last slot has been returned."""
    await until(lambda: not turns.holders and not turns.waiting)


def shared_of(pool) -> SharedTestConnection:
    return next(iter(pool._slots)).connection._shared


APP_USER = "adorm_test.app_user"


class ShowingConnection(DjangoConnection):
    """Records the driver's ``setting`` before each operation, by the ``opt-<label>`` the operation filters on."""

    seen: dict[str, list[str]] = {}

    def execute_sync(self, operation):
        label = re.search(r"opt-([a-z]+)", repr(operation))
        if label is not None:
            with self.db.cursor() as cursor:
                cursor.execute("SELECT current_setting(%s, true)", [self.driver.setting])
                self.seen.setdefault(label.group(1), []).append(cursor.fetchone()[0])
        return super().execute_sync(operation)


class ShowingDriver(DjangoDriver):
    connection_class = ShowingConnection

    def __init__(self, setting="statement_timeout"):
        self.setting = setting


class DefaultsDriver(ShowingDriver):
    """Its connections start with ``APP_USER`` set to ``base``, as Django's start with ``OPTIONS["assume_role"]``."""

    def connection_defaults(self, profile):  # noqa: ARG002
        return {APP_USER: "base"}


def seen_on_test_connection():
    """``APP_USER`` as a statement of Django code on the test's connection sees it."""
    with connections["default"].cursor() as cursor:
        cursor.execute(f"SELECT current_setting('{APP_USER}', true)")
        return cursor.fetchone()[0]


# Under Django's TransactionTestCase: slots with connections of their own.


class OwnConnectionsTests(TransactionTestCase):
    async def test_slots_have_connections_of_their_own(self):
        wrapper = await sync_to_async(wrapper_of_test)()
        first, second = create_session("default", "first"), create_session("default", "second")
        async with executor() as pool:
            for i in REPEAT:
                entered, finish = asyncio.Event(), asyncio.Event()

                async def hold(entered=entered, finish=finish, i=i):
                    async with first.atomic():
                        await tags(first).create(name=f"first-{i}")
                        entered.set()
                        await finish.wait()

                task = asyncio.create_task(hold())
                await entered.wait()
                # Concurrent transactions are isolated, not refused.
                async with second.atomic():
                    assert await tags(second).filter(name=f"first-{i}").count() == 0
                    await tags(second).create(name=f"second-{i}")
                finish.set()
                await task
            for slot in pool._slots:
                assert type(slot.connection) is DjangoConnection
                assert slot.connection.db is not wrapper
                assert slot.connection.db.settings_dict["NAME"] == wrapper.settings_dict["NAME"]
        expected = sorted(f"{n}-{i}" for n in ("first", "second") for i in REPEAT)
        assert await committed_names() == expected

    async def test_a_flow_waiting_for_a_row_lock_is_reported_on_both_drivers(self):
        first, second = create_session("default", "holder"), create_session("default", "renamer")
        for driver in (DjangoDriver(), AsyncpgDriver()):
            async with executor(driver=driver) as pool:
                tag = await tags().create(name=f"tag-{driver.name}")
                for i in REPEAT:
                    holding, release = asyncio.Event(), asyncio.Event()
                    holder_pids = []

                    async def hold(holding=holding, release=release, holder_pids=holder_pids, pk=tag.pk, pool=pool):
                        async with first.atomic():
                            await tags(first).select_for_update().get(pk=pk)
                            lease = next(lease for lease in pool.leases() if lease.session is first)
                            holder_pids.append(lease.slot.connection.backend_pid)
                            holding.set()
                            await release.wait()

                    async def rename(i=i, pk=tag.pk, name=driver.name):
                        async with second.atomic():
                            row = await tags(second).select_for_update().get(pk=pk)
                            row.name = f"{name}-{i}"
                            await row.save()

                    holder = asyncio.create_task(hold())
                    await holding.wait()
                    renamer = asyncio.create_task(rename())
                    wait = await wait_for_lock(renamer)
                    assert wait.running.sender is renamer
                    assert wait.running.session is second
                    assert wait.blocking_pids == tuple(holder_pids)
                    if i % 2:
                        renamer.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await renamer
                        assert pool.in_use == 1  # the holder's slot only
                        release.set()
                        await holder
                    else:
                        release.set()
                        await holder
                        await renamer
                    expected = f"{driver.name}-{i - i % 2}"
                    assert (await tags().get(pk=tag.pk)).name == expected
                assert pool.in_use == 0

    async def test_the_native_postgresql_driver_runs(self):
        async with executor(driver=AsyncpgDriver()):
            await tags().create(name="native")
            assert await tags().count() == 1
        assert await committed_names() == ["native"]


# Under pytest-django's database access without transaction=True (a TestCase): the event loop runs on the test's
# own thread.


@pytest.fixture
def seeded(db):  # noqa: ARG001 - the test's database access
    dj.Tag.objects.create(name="seed")
    return connections["default"]


class DirectDjangoDriver(DjangoDriver):
    direct = True


@pytest.mark.parametrize("driver_class", [DjangoDriver, DirectDjangoDriver])
async def test_on_the_test_thread_slots_share_its_connection(seeded, driver_class):
    async with executor(driver=driver_class()) as pool:
        assert await tag_names() == ["seed"]
        async with adorm.atomic():
            await tags().create(name="in-transaction")
        with pytest.raises(IntegrityError):
            await tags().create(name="seed")
        await tags().create(name="after")
        assert await tag_names() == ["after", "in-transaction", "seed"]
        for slot in pool._slots:
            assert slot.connection.db is seeded
    assert await committed_names() == []
    assert seeded.in_atomic_block


async def test_on_the_test_thread_a_transaction_awaiting_an_overlapping_one_is_a_deadlock(seeded):  # noqa: ARG001
    first, second = create_session("default", "first"), create_session("default", "second")
    async with executor(acquire_timeout=60):
        for _ in REPEAT:
            async with asyncio.timeout(10), first.atomic():
                await tags(first).count()
                # A flow that does not inherit the transaction's context waits for it, and the transaction awaits it.
                with pytest.raises(DeadlockError) as raised:
                    await asyncio.create_task(enter(second), context=contextvars.Context())
                assert raised.value.session == second
                assert raised.value.timeout == 60
                assert await tags(first).count() == 1
        # Once the transaction has ended, the same flow runs.
        await asyncio.create_task(enter(second), context=contextvars.Context())


async def enter(session):
    async with session.atomic():
        pass


# The parts of test mode.


def test_the_suite_runs_in_djangos_test_environment():
    assert in_test_environment()


def test_outside_the_test_environment_nothing_is_looked_for(monkeypatch):
    from django.test.utils import _TestState

    monkeypatch.delattr(_TestState, "saved_data")
    assert not in_test_environment()
    assert asyncio.run(find_test_database("default")) is None


async def test_an_alias_without_a_database_has_no_test_database():
    assert await find_test_database("missing") is None


class FakeWrapper:
    def __init__(self, *blocks, cursor=None):
        self.atomic_blocks = list(blocks)
        self.cursor = cursor


def fake_wrapper(*blocks):
    return FakeWrapper(*blocks)


def test_a_test_database_is_current_while_its_testcase_blocks_are_open():
    test_block, own_block = SimpleNamespace(_from_testcase=True), SimpleNamespace(_from_testcase=False)
    wrapper = fake_wrapper(test_block, own_block)
    test = TestDatabase("default", wrapper, open_testcase_atomics(wrapper))
    assert test.atomics == (test_block,)
    assert test.in_test_case
    assert test.current()
    wrapper.atomic_blocks.pop()
    assert test.current()  # the test's own block does not count
    wrapper.atomic_blocks[0] = SimpleNamespace(_from_testcase=True)
    assert not test.current()
    wrapper.atomic_blocks.clear()
    assert not test.current()
    outside_case = TestDatabase("default", wrapper, ())
    assert not outside_case.in_test_case
    assert outside_case.current()
    wrapper.atomic_blocks.append(test_block)
    assert not outside_case.current()


def test_one_shared_connection_per_test():
    block = SimpleNamespace(_from_testcase=True)
    wrapper = fake_wrapper(block)
    shared = shared_test_connection(TestDatabase("default", wrapper, (block,)))
    assert shared_test_connection(TestDatabase("default", wrapper, (block,))) is shared
    other = SimpleNamespace(_from_testcase=True)
    assert shared_test_connection(TestDatabase("default", wrapper, (other,))) is not shared


def test_the_shared_connection_class_extends_the_drivers_class():
    class Custom(DjangoConnection):
        pass

    cls = shared_connection_class(Custom)
    assert issubclass(cls, SharedConnectionMixin)
    assert issubclass(cls, Custom)
    assert shared_connection_class(Custom) is cls
    with pytest.raises(TypeError):
        shared_connection_class(object)


def fake_connection():
    return SimpleNamespace(_cancel_waiting=None, _current=None)


def test_a_cancelled_wait_for_the_shared_connection_raises_a_cancellation():
    shared = SharedTestConnection(TestDatabase("default", fake_wrapper(), ()))
    owner, waiter = fake_connection(), fake_connection()
    for _ in REPEAT:
        shared.acquire(owner)
        waiter._current = call = object()
        outcome = {}
        waiting = threading.Event()

        def wait(waiter=waiter, outcome=outcome, waiting=waiting):
            waiting.set()
            try:
                shared.acquire(waiter)
            except OperationalError as exc:
                outcome["error"] = exc
            else:
                outcome["acquired"] = True

        thread = threading.Thread(target=wait)
        thread.start()
        assert waiting.wait(5)
        shared.cancel(waiter, cancel=lambda: pytest.fail("the waiter does not run a statement"))
        thread.join(5)
        assert not thread.is_alive()
        assert outcome["error"].sqlstate == QUERY_CANCELED
        assert shared.owner is owner
        # A cancellation of an earlier call does not reach the next one.
        assert waiter._cancel_waiting is None
        waiter._cancel_waiting, waiter._current = call, object()
        shared.release(owner)
        shared.acquire(waiter)
        assert shared.owner is waiter
        shared.release(waiter)


def test_cancelling_the_owner_cancels_its_statement():
    shared = SharedTestConnection(TestDatabase("default", fake_wrapper(), ()))
    owner = fake_connection()
    cancelled = []
    shared.acquire(owner)
    shared.cancel(owner, cancel=lambda: cancelled.append(True))
    assert cancelled == [True]
    assert owner._cancel_waiting is None
    shared.release(owner)


class RecordingCursor:
    def __init__(self, statements):
        self.statements = statements

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql):
        self.statements.append(sql)


def test_options_are_set_locally_and_only_when_they_differ():
    statements = []
    wrapper = FakeWrapper(cursor=lambda: RecordingCursor(statements))
    shared = SharedTestConnection(TestDatabase("default", wrapper, ()))
    shared.apply_options({})
    assert statements == []
    shared.apply_options({"statement_timeout": 5})
    shared.apply_options({"statement_timeout": 5})
    shared.apply_options({"lock_timeout": 1})
    set_options = [statement for statement in statements if "SAVEPOINT" not in statement]
    assert set_options == [
        "SET LOCAL statement_timeout = 5000",
        "SET LOCAL lock_timeout = 1000; SET LOCAL statement_timeout TO DEFAULT",
    ]
    # Each in a savepoint of its own, which a refused value rolls back.
    assert statements[:3] == [
        "SAVEPOINT adorm_options",
        "SET LOCAL statement_timeout = 5000",
        "RELEASE SAVEPOINT adorm_options",
    ]
    statements.clear()
    shared.forget_options()
    shared.apply_options({})
    assert statements == [
        "SAVEPOINT adorm_options",
        "SET LOCAL lock_timeout TO DEFAULT; SET LOCAL statement_timeout TO DEFAULT",
        "RELEASE SAVEPOINT adorm_options",
    ]


# manage.py test --parallel: every worker's slots use that worker's test database.


def test_djangos_parallel_test_runner_is_unaffected():
    env = {
        **os.environ,
        "ADORM_TEST_DATABASE_NAME": PARALLEL_DATABASE,
        "DJANGO_SETTINGS_MODULE": "tests.djangoproject.settings",
        "PYTHONPATH": str(ROOT),
    }
    result = subprocess.run(  # noqa: S603 - fixed arguments
        [
            sys.executable,
            "-m",
            "django",
            "test",
            "tests.testmode.parallel_cases",
            "--parallel",
            "2",
            "--noinput",
            "--verbosity",
            "2",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-4000:]
    assert "Cloning test database for alias 'default'" in output
    assert "Ran 4 tests" in output
    assert "OK" in output
