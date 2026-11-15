"""Test mode: the Django driver inside Django's ``TestCase`` (slots on the test's connection, transactions as
savepoints, overlapping transactions refused) and ``TransactionTestCase`` (connections of their own), under Django's
async test methods (the event loop on a thread of its own) and pytest-asyncio (the event loop on the test's thread).

The failures that matter: flows racing for the shared connection, a cancelled flow that waits for it or runs on it, a
failed operation that would break the test's transaction for every other flow, options that leak between slots,
transactions that overlap or outlive the one they began in, and slots that outlive their test."""

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
    IntegrityError,
    InterfaceError,
    NotSupportedError,
    OperationalError,
    TransactionClosed,
    TransactionManagementError,
    TransactionOverlapError,
)
from adorm.drivers.asyncpg import AsyncpgDriver
from adorm.drivers.django import DjangoConnection, DjangoDriver
from adorm.executor import CloseReason, Pool, Router, set_runner
from adorm.testing.django import (
    find_test_database,
    in_test_environment,
    open_testcase_atomics,
    QUERY_CANCELED,
    shared_connection_class,
    shared_test_connection,
    SharedConnectionMixin,
    SharedTestConnection,
    TestDatabase,
)
from tests.djangoproject.library import models as dj
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

    async def test_overlapping_transactions_raise(self):
        async with executor():
            for i in REPEAT:
                first, second = create_session("default", f"first-{i}"), create_session("default", f"second-{i}")
                entered, finish = asyncio.Event(), asyncio.Event()

                async def hold(session=first, entered=entered, finish=finish, i=i):
                    async with session.atomic():
                        await tags(session).create(name=f"first-{i}")
                        entered.set()
                        await finish.wait()

                task = asyncio.create_task(hold())
                await entered.wait()
                with pytest.raises(TransactionOverlapError):
                    async with second.atomic():
                        await tags(second).create(name="never")
                # An operation outside a transaction would run inside the open one, so it is refused too.
                with pytest.raises(TransactionOverlapError):
                    await tags(second).count()
                finish.set()
                await task
                async with second.atomic():
                    await tags(second).create(name=f"second-{i}")
            assert await tag_names() == sorted(["seed", *(f"{n}-{i}" for n in ("first", "second") for i in REPEAT)])

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
                with pytest.raises(TransactionOverlapError):
                    await tags().count()
                done.set()
                with pytest.raises(IntegrityError):
                    await task
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

    async def test_a_profile_with_session_hooks_cannot_share_the_test_connection(self):
        def hook(db, _profile, _session):
            with db.cursor() as cursor:
                cursor.execute("SET ROLE postgres")

        for hooks in ({"on_acquire": hook}, {"on_release": hook}):
            async with executor(**hooks) as pool:
                with pytest.raises(NotSupportedError, match="on_acquire or on_release"):
                    await tags().count()
                assert not pool._slots

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

    async def test_django_orm_code_takes_turns_and_respects_open_transactions(self):
        async with executor():
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
                # Outside it, it is refused while the transaction is open.
                entered, finish = asyncio.Event(), asyncio.Event()

                async def hold(entered=entered, finish=finish, i=i):
                    async with create_session("default", "holder").atomic():
                        await tags(create_session("default", "holder")).create(name=f"held-{i}")
                        entered.set()
                        await finish.wait()

                task = asyncio.create_task(hold())
                await first_of(entered.wait(), task)
                with pytest.raises(TransactionOverlapError):
                    await sync_to_async(names_on_test_thread)()
                finish.set()
                await task
                assert f"held-{i}" in await sync_to_async(names_on_test_thread)()
            assert await tags().filter(name__startswith="orm-").count() == 6 * len(REPEAT)
            assert await tags().filter(name__startswith="adorm-").count() == 6 * len(REPEAT)

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


def shared_of(pool) -> SharedTestConnection:
    return next(iter(pool._slots)).connection._shared


class ShowingConnection(DjangoConnection):
    """Records ``statement_timeout`` before each operation, by the ``opt-<label>`` the operation filters on."""

    seen: dict[str, list[str]] = {}

    def execute_sync(self, operation):
        label = re.search(r"opt-([a-z]+)", repr(operation))
        if label is not None:
            with self.db.cursor() as cursor:
                cursor.execute("SHOW statement_timeout")
                self.seen.setdefault(label.group(1), []).append(cursor.fetchone()[0])
        return super().execute_sync(operation)


class ShowingDriver(DjangoDriver):
    connection_class = ShowingConnection


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


async def test_on_the_test_thread_overlapping_transactions_raise(seeded):  # noqa: ARG001
    first, second = create_session("default", "first"), create_session("default", "second")
    async with executor():
        async with first.atomic():
            await tags(first).count()
            # A flow that does not inherit the transaction's context.
            with pytest.raises(TransactionOverlapError):
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
    assert statements == [
        "SET LOCAL statement_timeout = 5000",
        "SET LOCAL lock_timeout = 1000; SET LOCAL statement_timeout TO DEFAULT",
    ]
    statements.clear()
    shared.forget_options()
    shared.apply_options({})
    assert statements == ["SET LOCAL lock_timeout TO DEFAULT; SET LOCAL statement_timeout TO DEFAULT"]


# manage.py test --parallel: every worker's slots use that worker's test database.


def test_djangos_parallel_test_runner_is_unaffected():
    env = {
        **os.environ,
        "ADORM_TEST_DATABASE_NAME": "adorm_test_parallel",
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
