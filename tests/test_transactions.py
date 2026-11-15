"""Transactions: atomic() blocks, savepoints, the transaction in the context and the slot's commit deadline."""

import asyncio
import itertools
import warnings

import pytest

import adorm

from adorm import (
    Atomic,
    create_session,
    DeadlockError,
    FieldError,
    IntegrityError,
    InterfaceError,
    NotSupportedError,
    ObjectDoesNotExist,
    OperationalError,
    SessionTimeout,
    TransactionClosed,
    TransactionManagementError,
)
from adorm.executor import (
    active_transaction,
    bind_transaction,
    debug_enabled,
    get_runner,
    held_leases,
    Pool,
    Router,
    set_debug,
    set_runner,
    TransactionRunner,
)
from tests.executor_fakes import FakeDriver, Op, Profile

REPEAT = range(20)
LONG = 30
GUARD = 5
_names = itertools.count()


def unique_session(using="default"):
    return create_session(using, f"tx-{next(_names)}")


async def until(predicate):
    """Yield to the loop until ``predicate()`` holds."""
    async with asyncio.timeout(GUARD):
        while not predicate():
            await asyncio.sleep(0)


class Setup:
    def __init__(self):
        self.router = Router()

    def pool(self, name="default", *, max_connections=0, acquire_timeout=LONG, commit_timeout=None):
        driver = FakeDriver()
        pool = Pool(
            driver,
            Profile(name),
            max_connections=max_connections,
            acquire_timeout=acquire_timeout,
            commit_timeout=commit_timeout,
        )
        self.router.add(pool)
        return pool, driver


@pytest.fixture
async def setup():
    built = Setup()
    previous = set_runner(built.router)
    try:
        yield built
    finally:
        set_runner(previous)
        await built.router.close()


async def held_elsewhere(router, session):
    """A lease acquired by another task, so the current flow does not hold it."""
    return await asyncio.create_task(router.acquire(session))


def calls(driver, number=0):
    return driver.connections[number].calls


# Resolving the session


async def test_adorm_atomic_uses_the_current_session(setup):
    pool, driver = setup.pool()
    session = unique_session()
    with session.activate():
        block = adorm.atomic()
    assert isinstance(block, Atomic)
    assert block.session is session
    async with block as tx:
        assert tx is block
        assert active_transaction(session) is tx
        assert active_transaction(unique_session()) is None
        assert tx.lease.session is session
        assert await session.execute(Op("a")) == "a"
    assert active_transaction(session) is None
    assert calls(driver) == [("begin", None), ("execute", "a"), ("commit", None)]
    assert pool.idle == 1


async def test_session_atomic_uses_that_session(setup):
    _, driver = setup.pool()
    session = unique_session()
    async with session.atomic() as tx:
        assert tx.session is session
        assert active_transaction(adorm.current_session()) is None
        await session.execute(Op())
    assert [c[0] for c in calls(driver)] == ["begin", "execute", "commit"]


def test_atomic_needs_a_session():
    with pytest.raises(TypeError, match="Session"):
        Atomic("default")


async def test_operations_of_bound_models_find_the_transaction(setup):
    _, driver = setup.pool(max_connections=1)
    session = unique_session()
    async with session.atomic():
        # With one slot, an operation that acquired a slot of its own would wait for ever.
        assert await asyncio.wait_for(session.execute(Op("inside")), GUARD) == "inside"
    assert len(driver.connections) == 1


async def test_without_a_runner_atomic_raises_interface_error(setup):  # noqa: ARG001
    previous = set_runner(None)
    try:
        assert get_runner() is None
        with pytest.raises(InterfaceError, match="no runner"):
            async with unique_session().atomic():
                pass
    finally:
        set_runner(previous)


async def test_a_runner_without_slots_cannot_hold_a_transaction(setup):  # noqa: ARG001
    class Direct:
        async def run(self, session, operation):  # noqa: ARG002
            return operation

    previous = set_runner(Direct())
    try:
        with pytest.raises(InterfaceError, match="holds a slot"):
            async with unique_session().atomic():
                pass
    finally:
        set_runner(previous)


async def test_a_block_is_entered_once(setup):
    setup.pool()
    block = unique_session().atomic()
    async with block:
        pass
    with pytest.raises(TransactionManagementError, match="already entered"):
        async with block:
            pass


# Commit and rollback


async def test_an_exception_rolls_back_and_propagates(setup):
    pool, driver = setup.pool()
    session = unique_session()
    with pytest.raises(KeyError):
        async with session.atomic():
            await session.execute(Op())
            raise KeyError("boom")
    assert [c[0] for c in calls(driver)] == ["begin", "execute", "rollback"]
    assert pool.idle == 1
    assert held_leases() == ()


async def test_commit_ends_the_transaction_early(setup):
    pool, driver = setup.pool()
    session = unique_session()
    async with session.atomic() as tx:
        await session.execute(Op("a"))
        await tx.commit()
        assert tx.closed
        assert tx.lease.released
        assert pool.idle == 1
        with pytest.raises(TransactionClosed):
            await session.execute(Op("late"))
        with pytest.raises(TransactionClosed):
            await tx.commit()
    assert [c[0] for c in calls(driver)] == ["begin", "execute", "commit"]


async def test_an_exception_after_an_early_commit_rolls_back_nothing(setup):
    _, driver = setup.pool()
    with pytest.raises(KeyError):
        async with unique_session().atomic() as tx:
            await tx.commit()
            raise KeyError
    assert [c[0] for c in calls(driver)] == ["begin", "commit"]


async def test_rollback_ends_the_transaction_early(setup):
    _, driver = setup.pool()
    session = unique_session()
    async with session.atomic() as tx:
        await session.execute(Op())
        await tx.rollback()
        with pytest.raises(TransactionClosed):
            await session.execute(Op())
    assert [c[0] for c in calls(driver)] == ["begin", "execute", "rollback"]


async def test_a_failing_commit_raises_from_the_block_and_discards_the_slot(setup):
    pool, driver = setup.pool()
    session = unique_session()
    with pytest.raises(OperationalError, match="lost"):
        async with session.atomic():
            driver.connections[0].errors["commit"] = OperationalError("lost")
    assert driver.connections[0].closed
    assert pool.size == 0


async def test_a_failing_begin_raises_at_entry_and_leaves_nothing_behind(setup):
    pool, driver = setup.pool()
    session = unique_session()
    lease = await setup.router.acquire(session)
    lease.slot.connection.errors["begin"] = OperationalError("no")
    await lease.release()
    entered = False
    with pytest.raises(OperationalError):
        async with session.atomic():
            entered = True
    assert not entered
    assert active_transaction(session) is None
    assert held_leases() == ()
    assert driver.connections[0].closed
    assert pool.size == 0


async def test_a_failing_rollback_does_not_hide_the_exception_of_the_block(setup, caplog):
    pool, driver = setup.pool()
    with pytest.raises(KeyError):
        async with unique_session().atomic():
            driver.connections[0].errors["rollback"] = OperationalError("gone")
            raise KeyError
    assert "rolling back" in caplog.text
    assert pool.size == 0


async def test_a_failing_explicit_rollback_raises(setup):
    _, driver = setup.pool()
    async with unique_session().atomic() as tx:
        driver.connections[0].errors["rollback"] = OperationalError("gone")
        with pytest.raises(OperationalError):
            await tx.rollback()


# A failed operation


async def test_a_database_error_caught_in_the_block_rolls_back_and_raises(setup):
    pool, driver = setup.pool()
    session = unique_session()
    with pytest.raises(TransactionManagementError, match="rolled back") as info:
        async with session.atomic():
            with pytest.raises(IntegrityError):
                await session.execute(Op(error=IntegrityError("duplicate")))
            with pytest.raises(TransactionManagementError, match="no further operations"):
                await session.execute(Op("after"))
    assert isinstance(info.value.__cause__, IntegrityError)
    assert [c[0] for c in calls(driver)] == ["begin", "execute", "rollback"]
    assert pool.idle == 1


@pytest.mark.parametrize(
    "error",
    [
        ObjectDoesNotExist("none"),
        NotSupportedError("unsupported"),
        ValueError("client side"),
        FieldError("unsupported lookup 'icontain'"),
        FieldError("Cannot resolve keyword 'nope' into field", original=KeyError("nope")),
    ],
)
async def test_errors_that_leave_the_transaction_intact_do_not_mark_it(setup, error):
    _, driver = setup.pool()
    session = unique_session()
    async with session.atomic():
        with pytest.raises(type(error)):
            await session.execute(Op(error=error))
        await session.execute(Op("after"))
    assert [c[0] for c in calls(driver)] == ["begin", "execute", "execute", "commit"]


async def test_a_field_error_from_the_database_marks_the_transaction(setup):
    _, driver = setup.pool()
    session = unique_session()
    with pytest.raises(TransactionManagementError):
        async with session.atomic():
            with pytest.raises(FieldError):
                await session.execute(Op(error=FieldError("column does not exist", sqlstate="42703")))
    assert calls(driver)[-1] == ("rollback", None)


async def test_field_errors_racing_operations_of_other_tasks_leave_the_transaction_intact(setup):
    pool, driver = setup.pool(max_connections=1)
    for _ in range(10):
        session = unique_session()
        async with session.atomic():
            results = await asyncio.gather(
                *(session.execute(Op(error=FieldError("bad key")) if n % 2 else Op(f"op{n}")) for n in range(20)),
                return_exceptions=True,
            )
            assert all(isinstance(result, FieldError) for result in results[1::2])
            await session.execute(Op("after"))
        assert calls(driver)[-1] == ("commit", None)
    assert (pool.size, pool.in_use) == (1, 0)


async def test_a_cancelled_operation_of_another_task_makes_the_end_roll_back(setup):
    _, driver = setup.pool()
    session = unique_session()
    op = Op(gate=asyncio.Event())
    with pytest.raises(TransactionManagementError):
        async with session.atomic():
            task = asyncio.create_task(session.execute(op))
            await op.started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert calls(driver)[-1] == ("rollback", None)


async def test_explicit_commit_after_a_failure_rolls_back_and_raises(setup):
    _, driver = setup.pool()
    session = unique_session()
    async with session.atomic() as tx:
        with pytest.raises(IntegrityError):
            await session.execute(Op(error=IntegrityError()))
        with pytest.raises(TransactionManagementError):
            await tx.commit()
    assert calls(driver)[-1] == ("rollback", None)


# Savepoints


async def test_nested_atomic_is_a_savepoint_on_the_same_slot(setup):
    pool, driver = setup.pool(max_connections=1)
    session = unique_session()
    async with session.atomic() as outer:
        async with session.atomic() as inner:
            assert inner.parent is outer
            assert inner.root is outer
            assert inner.lease is outer.lease
            assert active_transaction(session) is inner
            await session.execute(Op("in"))
        assert active_transaction(session) is outer
        with session.activate():
            async with adorm.atomic():
                pass
    sp1, sp2 = inner.savepoint, "adorm_sp_2"
    assert calls(driver) == [
        ("begin", None),
        ("savepoint", sp1),
        ("execute", "in"),
        ("release_savepoint", sp1),
        ("savepoint", sp2),
        ("release_savepoint", sp2),
        ("commit", None),
    ]
    assert len(driver.connections) == 1
    assert pool.idle == 1


async def test_an_exception_in_a_savepoint_rolls_back_to_it_and_the_outer_block_commits(setup):
    _, driver = setup.pool()
    session = unique_session()
    async with session.atomic():
        with pytest.raises(KeyError):
            async with session.atomic() as inner:
                await session.execute(Op())
                raise KeyError
        await session.execute(Op("after"))
    name = inner.savepoint
    assert calls(driver) == [
        ("begin", None),
        ("savepoint", name),
        ("execute", "op"),
        ("rollback_to_savepoint", name),
        ("release_savepoint", name),
        ("execute", "after"),
        ("commit", None),
    ]


async def test_a_failure_inside_a_savepoint_does_not_mark_the_outer_block(setup):
    _, driver = setup.pool()
    session = unique_session()
    async with session.atomic():
        with pytest.raises(IntegrityError):
            async with session.atomic():
                await session.execute(Op(error=IntegrityError()))
        await session.execute(Op("after"))
    assert calls(driver)[-2:] == [("execute", "after"), ("commit", None)]


async def test_a_savepoint_that_exits_normally_after_a_failure_rolls_back_to_it_and_raises(setup):
    _, driver = setup.pool()
    session = unique_session()
    async with session.atomic():
        with pytest.raises(TransactionManagementError):
            async with session.atomic():
                with pytest.raises(IntegrityError):
                    await session.execute(Op(error=IntegrityError()))
        await session.execute(Op("after"))
    methods = [c[0] for c in calls(driver)]
    assert methods == [
        "begin",
        "savepoint",
        "execute",
        "rollback_to_savepoint",
        "release_savepoint",
        "execute",
        "commit",
    ]


async def test_savepoint_commit_and_rollback_end_it_early(setup):
    _, driver = setup.pool()
    session = unique_session()
    async with session.atomic():
        async with session.atomic() as first:
            await first.commit()
            with pytest.raises(TransactionClosed):
                await session.execute(Op())
        async with session.atomic() as second:
            await second.rollback()
    assert [c[0] for c in calls(driver)] == [
        "begin",
        "savepoint",
        "release_savepoint",
        "savepoint",
        "rollback_to_savepoint",
        "release_savepoint",
        "commit",
    ]


async def test_a_failing_savepoint_release_marks_the_outer_block(setup):
    _, driver = setup.pool()
    session = unique_session()
    with pytest.raises(TransactionManagementError):
        async with session.atomic():
            driver.connections[0].errors["release_savepoint"] = OperationalError("bad")
            with pytest.raises(OperationalError):
                async with session.atomic():
                    pass
    assert calls(driver)[-1] == ("rollback", None)


async def test_savepoints_need_a_driver_that_supports_them(setup):
    _, driver = setup.pool()
    driver.capabilities = type(driver.capabilities)(operations={"op"})
    session = unique_session()
    async with session.atomic():
        with pytest.raises(NotSupportedError, match="savepoints"):
            async with session.atomic():
                pass
        await session.execute(Op("still open"))
    assert calls(driver)[-1] == ("commit", None)


async def test_a_foreign_active_transaction_takes_no_nested_block(setup):
    setup.pool()
    session = unique_session()

    class Foreign:
        async def execute(self, operation):
            return operation

    with bind_transaction(session, Foreign()):
        with pytest.raises(TransactionManagementError, match="no nested"):
            async with session.atomic():
                pass


@pytest.mark.parametrize("attempt", REPEAT)
async def test_concurrent_savepoints_of_sibling_tasks_run_one_after_another(setup, attempt):  # noqa: ARG001
    _, driver = setup.pool()
    session = unique_session()
    gates = {name: asyncio.Event() for name in "ab"}
    ops = {name: Op(name, gate=gates[name]) for name in "ab"}

    async def child(name):
        async with session.atomic():
            await session.execute(ops[name])

    async with session.atomic():
        first = asyncio.create_task(child("a"))
        await ops["a"].started.wait()
        second = asyncio.create_task(child("b"))
        for _ in range(10):
            await asyncio.sleep(0)
        assert not ops["b"].started.is_set()
        assert [c[0] for c in calls(driver)].count("savepoint") == 1
        gates["a"].set()
        await first
        gates["b"].set()
        await second
    methods = [c[0] for c in calls(driver)]
    assert methods == ["begin"] + ["savepoint", "execute", "release_savepoint"] * 2 + ["commit"]


async def test_savepoints_in_a_task_group_all_complete(setup):
    _, driver = setup.pool(max_connections=1)
    session = unique_session()

    async def child(name):
        async with session.atomic():
            await session.execute(Op(name))

    async with session.atomic():
        async with asyncio.TaskGroup() as group:
            for name in "abcd":
                group.create_task(child(name))
    executed = sorted(name for method, name in calls(driver) if method == "execute")
    assert executed == list("abcd")
    assert calls(driver)[-1] == ("commit", None)


async def test_an_operation_through_an_ended_savepoint_raises_transaction_closed(setup):
    setup.pool()
    session = unique_session()
    async with session.atomic():
        async with session.atomic():
            context_task = asyncio.create_task(asyncio.sleep(0))
            inherited = active_transaction(session)
        await context_task
        with pytest.raises(TransactionClosed):
            await inherited.execute(Op())
        await session.execute(Op("outer still open"))


async def test_a_savepoint_left_open_by_another_task_is_closed_with_the_outer_block(setup):
    _, driver = setup.pool()
    session = unique_session()
    inside, leave = asyncio.Event(), asyncio.Event()
    outcome = []

    async def straggler():
        try:
            async with session.atomic():
                inside.set()
                await leave.wait()
                await session.execute(Op("late"))
        except TransactionClosed as exc:
            outcome.append(exc)

    async with session.atomic():
        task = asyncio.create_task(straggler())
        await inside.wait()
    leave.set()
    await task
    assert len(outcome) == 1
    assert [c[0] for c in calls(driver)] == ["begin", "savepoint", "rollback_to_savepoint", "commit"]


async def test_a_forced_savepoint_that_exits_normally_raises_transaction_closed(setup):
    setup.pool()
    session = unique_session()
    inside, leave = asyncio.Event(), asyncio.Event()

    async def straggler():
        async with session.atomic():
            inside.set()
            await leave.wait()

    async with session.atomic():
        task = asyncio.create_task(straggler())
        await inside.wait()
    leave.set()
    with pytest.raises(TransactionClosed, match="enclosing"):
        await task


# Tasks inheriting the transaction


@pytest.mark.parametrize("spawn", ["create_task", "task_group", "gather"])
async def test_tasks_inheriting_the_context_run_in_the_transaction(setup, spawn):
    pool, driver = setup.pool(max_connections=1)
    session = unique_session()
    async with session.atomic():
        ops = [session.execute(Op(f"t{i}")) for i in range(3)]
        if spawn == "create_task":
            for task in [asyncio.create_task(op) for op in ops]:
                await task
        elif spawn == "task_group":
            async with asyncio.TaskGroup() as group:
                for op in ops:
                    group.create_task(op)
        else:
            await asyncio.gather(*ops)
    assert len(driver.connections) == 1
    assert sorted(name for method, name in calls(driver) if method == "execute") == ["t0", "t1", "t2"]
    assert calls(driver)[-1] == ("commit", None)
    assert pool.idle == 1


@pytest.mark.parametrize("attempt", REPEAT)
async def test_exit_waits_for_operations_already_sent_then_commits(setup, attempt):  # noqa: ARG001
    _, driver = setup.pool()
    session = unique_session()
    op = Op("slow", gate=asyncio.Event())
    async with session.atomic():
        task = asyncio.create_task(session.execute(op))
        await op.started.wait()
        asyncio.get_running_loop().call_soon(op.gate.set)
    assert task.done()
    assert await task == "slow"
    assert [c[0] for c in calls(driver)] == ["begin", "execute", "commit"]


@pytest.mark.parametrize("attempt", REPEAT)
async def test_the_block_does_not_commit_before_a_pending_operation_finishes(setup, attempt):  # noqa: ARG001
    _, driver = setup.pool()
    session = unique_session()
    op = Op("slow", gate=asyncio.Event())

    blocks = []

    async def block():
        async with session.atomic() as tx:
            blocks.append(tx)
            task = asyncio.create_task(session.execute(op))
            await op.started.wait()
        return task

    outer = asyncio.create_task(block())
    await op.started.wait()
    await until(lambda: blocks[0].closed)
    for _ in range(10):
        await asyncio.sleep(0)
    assert ("commit", None) not in calls(driver)
    assert not outer.done()
    op.gate.set()
    task = await outer
    assert await task == "slow"
    assert calls(driver)[-1] == ("commit", None)


@pytest.mark.parametrize("attempt", REPEAT)
async def test_an_operation_from_a_task_that_outlives_the_block_raises_transaction_closed(setup, attempt):  # noqa: ARG001
    _, driver = setup.pool()
    session = unique_session()
    go = asyncio.Event()

    async def later():
        await go.wait()
        return await session.execute(Op("late"))

    async with session.atomic():
        task = asyncio.create_task(later())
    go.set()
    with pytest.raises(TransactionClosed):
        await task
    assert ("execute", "late") not in calls(driver)


@pytest.mark.parametrize("attempt", REPEAT)
async def test_an_operation_racing_the_exit_either_runs_before_the_commit_or_raises(setup, attempt):
    _, driver = setup.pool()
    session = unique_session()

    async def racer():
        for _ in range(attempt % 4):
            await asyncio.sleep(0)
        return await session.execute(Op("race"))

    async with session.atomic():
        task = asyncio.create_task(racer())
    try:
        await task
    except TransactionClosed:
        assert ("execute", "race") not in calls(driver)
    else:
        assert calls(driver).index(("execute", "race")) < calls(driver).index(("commit", None))
    assert calls(driver)[-1] == ("commit", None)


async def test_debug_mode_warns_about_live_tasks_that_used_the_transaction(setup):
    setup.pool()
    session = unique_session()
    loop = asyncio.get_running_loop()
    ran, stop = asyncio.Event(), asyncio.Event()

    async def worker():
        await session.execute(Op())
        ran.set()
        await stop.wait()

    loop.set_debug(True)
    try:
        with pytest.warns(RuntimeWarning, match="still running: the-worker"):
            async with session.atomic():
                task = asyncio.create_task(worker(), name="the-worker")
                await ran.wait()
    finally:
        loop.set_debug(False)
        stop.set()
        await task


async def test_no_warning_for_finished_tasks_or_outside_debug_mode(setup):
    setup.pool()
    session = unique_session()
    ran, stop = asyncio.Event(), asyncio.Event()

    async def worker():
        await session.execute(Op())
        ran.set()
        await stop.wait()

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        asyncio.get_running_loop().set_debug(True)
        try:
            async with session.atomic():
                await asyncio.create_task(session.execute(Op()))
        finally:
            asyncio.get_running_loop().set_debug(False)
        async with session.atomic():
            task = asyncio.create_task(worker())
            await ran.wait()
    stop.set()
    await task


# Taking the slot at entry


async def test_the_slot_is_taken_at_entry_and_a_timeout_raises_there(setup):
    pool, _ = setup.pool(max_connections=1, acquire_timeout=0.01)
    holder = await held_elsewhere(setup.router, unique_session())
    entered = False
    session = unique_session()
    with pytest.raises(SessionTimeout) as info:
        async with session.atomic():
            entered = True
    assert not isinstance(info.value, DeadlockError)
    assert info.value.session is session
    assert not entered
    assert active_transaction(session) is None
    await holder.release()
    assert pool.idle == 1


async def test_a_transaction_waiting_for_a_second_slot_of_its_pool_gets_a_deadlock_error_at_once(setup):
    setup.pool(max_connections=1)
    first, second = unique_session(), unique_session()
    async with first.atomic():
        with pytest.raises(DeadlockError) as info:
            async with asyncio.timeout(GUARD), second.atomic():
                pass
        assert info.value.held_sessions == (first,)
        await first.execute(Op("still usable"))


async def test_a_transaction_holding_a_slot_gets_a_deadlock_error_on_timeout(setup):
    setup.pool(max_connections=1)
    setup.pool("reports", max_connections=1, acquire_timeout=0.01)
    reports = unique_session("reports")
    blocker = await held_elsewhere(setup.router, reports)
    first = unique_session()
    try:
        async with first.atomic():
            with pytest.raises(DeadlockError) as info:
                async with reports.atomic():
                    pass
            assert info.value.held_sessions == (first,)
    finally:
        await blocker.release()


@pytest.mark.parametrize("attempt", REPEAT)
async def test_two_transactions_waiting_for_each_others_pools_get_a_deadlock_error(setup, attempt):  # noqa: ARG001
    setup.pool("one", max_connections=1)
    setup.pool("two", max_connections=1)
    one, two = unique_session("one"), unique_session("two")
    both = asyncio.Barrier(2)

    async def flow(mine, theirs):
        async with mine.atomic():
            await both.wait()
            async with theirs.atomic():
                return "inner"

    async with asyncio.timeout(GUARD):
        results = await asyncio.gather(flow(one, two), flow(two, one), return_exceptions=True)
    assert sorted(type(r).__name__ for r in results) == ["DeadlockError", "str"]


async def test_a_nested_block_of_another_session_takes_its_own_slot(setup):
    pool, driver = setup.pool(max_connections=2)
    first, second = unique_session(), unique_session()
    async with first.atomic() as outer:
        async with second.atomic() as inner:
            assert inner.parent is None
            assert inner.lease.slot is not outer.lease.slot
            await second.execute(Op("second"))
        await first.execute(Op("first"))
    assert len(driver.connections) == 2
    assert pool.idle == 2


@pytest.mark.parametrize("attempt", REPEAT)
async def test_cancelling_a_block_waiting_for_its_slot_leaks_nothing(setup, attempt):  # noqa: ARG001
    pool, _ = setup.pool(max_connections=1)
    holder = await held_elsewhere(setup.router, unique_session())
    session = unique_session()

    async def block():
        async with session.atomic():
            pass

    task = asyncio.create_task(block())
    await until(lambda: pool.waiting == 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pool.waiting == 0
    await holder.release()
    assert pool.idle == 1


# Cancellation


@pytest.mark.parametrize("attempt", REPEAT)
async def test_cancelling_the_block_during_an_operation_rolls_back_and_returns_the_slot(setup, attempt):  # noqa: ARG001
    pool, driver = setup.pool()
    session = unique_session()
    op = Op(gate=asyncio.Event())

    async def block():
        async with session.atomic():
            await session.execute(op)

    task = asyncio.create_task(block())
    await op.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    connection = driver.connections[0]
    assert connection.cancelled == [op]
    assert connection.calls[-1] == ("rollback", None)
    assert pool.idle == 1
    assert not connection.closed


@pytest.mark.parametrize("attempt", REPEAT)
async def test_cancelling_the_block_during_begin_discards_the_slot(setup, attempt):  # noqa: ARG001
    pool, driver = setup.pool()
    session = unique_session()
    lease = await setup.router.acquire(session)
    connection = lease.slot.connection
    connection.gates["begin"] = asyncio.Event()
    await lease.release()

    async def block():
        async with session.atomic():
            pass

    task = asyncio.create_task(block())
    await until(lambda: "begin" in connection.entered)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert connection.closed
    assert pool.size == 0
    assert active_transaction(session) is None


@pytest.mark.parametrize("attempt", REPEAT)
async def test_cancelling_the_block_during_commit_does_not_leak_the_slot(setup, attempt):  # noqa: ARG001
    pool, driver = setup.pool()
    session = unique_session()
    commit_gate = asyncio.Event()

    async def block():
        async with session.atomic() as tx:
            tx.lease.slot.connection.gates["commit"] = commit_gate

    task = asyncio.create_task(block())
    await until(lambda: driver.connections and "commit" in driver.connections[0].entered)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert driver.connections[0].closed
    assert pool.size == 0
    assert held_leases() == ()


@pytest.mark.parametrize("attempt", REPEAT)
async def test_cancelling_the_block_while_it_waits_for_pending_operations(setup, attempt):
    pool, driver = setup.pool()
    session = unique_session()
    op = Op(gate=asyncio.Event())
    started, blocks = [], []

    async def block():
        async with session.atomic() as tx:
            blocks.append(tx)
            started.append(asyncio.create_task(session.execute(op)))
            await op.started.wait()

    task = asyncio.create_task(block())
    await op.started.wait()
    await until(lambda: blocks[0].closed)
    task.cancel()
    for _ in range(attempt % 3):
        await asyncio.sleep(0)
    op.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await started[0] == "op"
    await until(lambda: pool.size == 0)
    assert ("commit", None) not in calls(driver)
    assert driver.connections[0].closed


@pytest.mark.parametrize("attempt", REPEAT)
async def test_cancelling_a_savepoint_block_during_an_operation_rolls_back_to_the_savepoint(setup, attempt):  # noqa: ARG001
    pool, driver = setup.pool()
    session = unique_session()
    op = Op(gate=asyncio.Event())

    async def inner():
        async with session.atomic():
            await session.execute(op)

    async with session.atomic():
        task = asyncio.create_task(inner())
        await op.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await session.execute(Op("after"))
    assert [c[0] for c in calls(driver)] == [
        "begin",
        "savepoint",
        "execute",
        "rollback_to_savepoint",
        "release_savepoint",
        "execute",
        "commit",
    ]
    assert pool.idle == 1


async def test_a_savepoint_whose_rollback_fails_marks_the_outer_block(setup):
    _, driver = setup.pool()
    session = unique_session()
    with pytest.raises(TransactionManagementError):
        async with session.atomic():
            driver.connections[0].errors["rollback_to_savepoint"] = OperationalError("x")
            with pytest.raises(KeyError):
                async with session.atomic():
                    raise KeyError
            with pytest.raises(TransactionManagementError):
                await session.execute(Op())
    assert calls(driver)[-1] == ("rollback", None)


async def test_an_explicit_savepoint_rollback_that_fails_raises_and_marks_the_outer_block(setup):
    _, driver = setup.pool()
    session = unique_session()
    with pytest.raises(TransactionManagementError):
        async with session.atomic():
            async with session.atomic() as savepoint:
                driver.connections[0].errors["rollback_to_savepoint"] = OperationalError("gone")
                with pytest.raises(OperationalError, match="gone"):
                    await savepoint.rollback()
                with pytest.raises(TransactionClosed):
                    await session.execute(Op())
            with pytest.raises(TransactionManagementError):
                await session.execute(Op())
    assert calls(driver)[-1] == ("rollback", None)
    assert ("release_savepoint", "adorm_sp_1") not in calls(driver)


@pytest.mark.parametrize("attempt", REPEAT)
async def test_cancelling_a_savepoint_while_it_ends_marks_the_outer_block(setup, attempt):  # noqa: ARG001
    _, driver = setup.pool()
    session = unique_session()

    async def inner():
        async with session.atomic():
            driver.connections[0].gates["release_savepoint"] = asyncio.Event()

    with pytest.raises(TransactionManagementError, match="rolled back"):
        async with session.atomic():
            task = asyncio.create_task(inner())
            await until(lambda: "release_savepoint" in driver.connections[0].entered)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert calls(driver)[-1] == ("rollback", None)


# The slot's commit deadline


def test_commit_timeout_is_validated():
    with pytest.raises(ValueError, match="commit_timeout"):
        Pool(FakeDriver(), Profile("default"), commit_timeout=0)
    assert Pool(FakeDriver(), Profile("default")).commit_timeout == 60.0
    assert Pool(FakeDriver(), Profile("default"), commit_timeout=None).commit_timeout is None
    assert Pool(FakeDriver(), Profile("default"), commit_timeout=2).commit_timeout == 2.0


@pytest.mark.parametrize("attempt", range(5))
async def test_the_slot_aborts_a_transaction_whose_commit_does_not_arrive(setup, attempt, caplog):  # noqa: ARG001
    pool, driver = setup.pool(max_connections=1, commit_timeout=0.02)
    session = unique_session()
    never = asyncio.Event()

    async def lost_client():
        async with session.atomic():
            await session.execute(Op("work"))
            await never.wait()

    lost = asyncio.create_task(lost_client())
    await until(lambda: driver.connections and driver.connections[0].closed)
    assert calls(driver) == [("begin", None), ("execute", "work"), ("rollback", None)]
    assert "no request within" in caplog.text
    await until(lambda: pool.size == 0)
    # the pool serves another session with a fresh connection
    other = unique_session()
    assert await asyncio.wait_for(other.execute(Op("next")), GUARD) == "next"
    assert len(driver.connections) == 2
    lost.cancel()
    with pytest.raises(asyncio.CancelledError):
        await lost


async def test_after_an_abort_the_block_raises_transaction_closed(setup):
    _, driver = setup.pool(commit_timeout=0.02)
    session = unique_session()
    with pytest.raises(TransactionClosed, match="no request within"):
        async with session.atomic() as tx:
            await until(lambda: tx.lease.aborted)
            await until(lambda: driver.connections[0].closed)
            with pytest.raises(TransactionClosed):
                await session.execute(Op())


async def test_after_an_abort_an_exception_of_the_block_propagates(setup):
    _, driver = setup.pool(commit_timeout=0.02)
    with pytest.raises(KeyError):
        async with unique_session().atomic() as tx:
            await until(lambda: tx.lease.aborted)
            raise KeyError
    assert calls(driver).count(("rollback", None)) == 1


async def test_after_an_abort_the_commit_raises_transaction_closed(setup):
    _, _ = setup.pool(commit_timeout=0.02)
    with pytest.raises(TransactionClosed):
        async with unique_session().atomic() as tx:
            await until(lambda: tx.lease.aborted)


async def test_a_running_operation_does_not_count_against_the_commit_deadline(setup):
    _, driver = setup.pool(commit_timeout=0.02)
    session = unique_session()
    op = Op("long", gate=asyncio.Event())
    async with session.atomic() as tx:
        task = asyncio.create_task(session.execute(op))
        await op.started.wait()
        await asyncio.sleep(0.1)  # five deadlines while the operation runs
        assert not tx.lease.aborted
        op.gate.set()
        assert await task == "long"
        await session.execute(Op("next"))
    assert calls(driver)[-1] == ("commit", None)


async def test_no_deadline_outside_a_transaction(setup):
    pool, driver = setup.pool(commit_timeout=0.01)
    lease = await setup.router.acquire(unique_session())
    await asyncio.sleep(0.05)
    assert not lease.aborted
    await lease.call("execute", Op())
    await lease.release()
    assert pool.idle == 1
    assert not driver.connections[0].closed


async def test_a_failing_rollback_on_abort_still_ends_the_lease(setup, caplog):
    pool, driver = setup.pool(commit_timeout=0.02)
    lease = await setup.router.acquire(unique_session())
    lease.slot.connection.errors["rollback"] = OperationalError("gone")
    await lease.call("begin")
    await until(lambda: lease.aborted)
    await until(lambda: pool.size == 0)
    assert "rolling back the abandoned transaction" in caplog.text
    with pytest.raises(TransactionClosed):
        await lease.call("execute", Op())
    await lease.release()
    assert driver.connections[0].closed


# Errors raised by the server, savepoints of other tasks, ends in other tasks


async def test_a_not_supported_error_from_the_server_marks_the_block(setup):
    _, driver = setup.pool()
    session = unique_session()
    with pytest.raises(TransactionManagementError, match="rolled back"):
        async with session.atomic():
            with pytest.raises(NotSupportedError):
                await session.execute(Op(error=NotSupportedError("feature", sqlstate="0A000")))
    assert calls(driver)[-1] == ("rollback", None)


@pytest.mark.parametrize("attempt", REPEAT)
async def test_an_outer_operation_of_another_task_waits_for_an_open_savepoint(setup, attempt):
    _, driver = setup.pool()
    session = unique_session()
    inside, leave = asyncio.Event(), asyncio.Event()

    async def savepoint_task():
        with pytest.raises(KeyError):
            async with session.atomic():
                await session.execute(Op("inner"))
                inside.set()
                await leave.wait()
                raise KeyError

    async def outer_task():
        for _ in range(attempt % 3):
            await asyncio.sleep(0)
        return await session.execute(Op("outer"))

    async with session.atomic():
        first = asyncio.create_task(savepoint_task())
        await inside.wait()
        second = asyncio.create_task(outer_task())
        for _ in range(10):
            await asyncio.sleep(0)
        assert ("execute", "outer") not in calls(driver)
        leave.set()
        await first
        assert await second == "outer"
    methods = [c[0] if c[0] != "execute" else c[1] for c in calls(driver)]
    assert methods == [
        "begin",
        "savepoint",
        "inner",
        "rollback_to_savepoint",
        "release_savepoint",
        "outer",
        "commit",
    ]


@pytest.mark.parametrize("nested", ["savepoint", "operation"])
async def test_waiting_for_a_savepoint_that_waits_for_the_waiter_raises_deadlock_error(setup, nested):
    setup.pool(acquire_timeout=0.05)
    session = unique_session()

    async def worker():
        if nested == "savepoint":
            async with session.atomic():
                pass
        else:
            await session.execute(Op())

    async with session.atomic():
        task = asyncio.create_task(worker())  # created before the savepoint it then waits for
        with pytest.raises(DeadlockError) as info:
            async with session.atomic():
                await asyncio.wait_for(task, GUARD)
        assert info.value.held_sessions == (session,)
        await session.execute(Op("outer still open"))


async def test_work_of_a_savepoint_left_open_by_another_task_is_not_committed(setup):
    _, driver = setup.pool()
    session = unique_session()
    inside, leave = asyncio.Event(), asyncio.Event()

    async def straggler():
        with pytest.raises(KeyError):
            async with session.atomic():
                await session.execute(Op("unfinished"))
                inside.set()
                await leave.wait()
                raise KeyError

    async with session.atomic():
        await session.execute(Op("outer"))
        task = asyncio.create_task(straggler())
        await inside.wait()
    leave.set()
    await task
    assert calls(driver) == [
        ("begin", None),
        ("execute", "outer"),
        ("savepoint", "adorm_sp_1"),
        ("execute", "unfinished"),
        ("rollback_to_savepoint", "adorm_sp_1"),
        ("commit", None),
    ]


async def test_a_savepoint_releases_without_the_work_of_a_savepoint_left_open_inside_it(setup):
    _, driver = setup.pool()
    session = unique_session()
    inside, leave = asyncio.Event(), asyncio.Event()

    async def straggler():
        async with session.atomic():
            await session.execute(Op("unfinished"))
            inside.set()
            await leave.wait()

    async with session.atomic():
        async with session.atomic():
            task = asyncio.create_task(straggler())
            await inside.wait()
        await session.execute(Op("outer"))
    leave.set()
    with pytest.raises(TransactionClosed):
        await task
    assert calls(driver) == [
        ("begin", None),
        ("savepoint", "adorm_sp_1"),
        ("savepoint", "adorm_sp_2"),
        ("execute", "unfinished"),
        ("rollback_to_savepoint", "adorm_sp_2"),
        ("release_savepoint", "adorm_sp_1"),
        ("execute", "outer"),
        ("commit", None),
    ]


async def test_a_failing_rollback_of_a_savepoint_left_open_rolls_the_transaction_back(setup):
    _, driver = setup.pool()
    session = unique_session()
    inside, leave = asyncio.Event(), asyncio.Event()

    async def straggler():
        async with session.atomic():
            inside.set()
            await leave.wait()

    with pytest.raises(TransactionManagementError, match="rolled back"):
        async with session.atomic():
            task = asyncio.create_task(straggler())
            await inside.wait()
            driver.connections[0].errors["rollback_to_savepoint"] = OperationalError("gone")
    leave.set()
    with pytest.raises(TransactionClosed):
        await task
    assert calls(driver)[-1] == ("rollback", None)


@pytest.mark.parametrize("method", ["commit", "rollback"])
async def test_an_end_failing_in_another_task_raises_from_the_owner_block(setup, method):
    _, driver = setup.pool()
    session = unique_session()
    with pytest.raises(OperationalError, match="lost"):
        async with session.atomic() as tx:
            connection = driver.connections[0]
            connection.gates[method] = asyncio.Event()
            connection.errors[method] = OperationalError("lost")
            ender = asyncio.create_task(getattr(tx, method)())
            await until(lambda: method in connection.entered)
            asyncio.get_running_loop().call_soon(connection.gates[method].set)
    with pytest.raises(OperationalError):
        await ender


async def test_an_end_in_another_task_completes_before_the_owner_block_exits(setup):
    pool, driver = setup.pool()
    session = unique_session()
    async with session.atomic() as tx:
        connection = driver.connections[0]
        connection.gates["commit"] = asyncio.Event()
        ender = asyncio.create_task(tx.commit())
        await until(lambda: "commit" in connection.entered)
        asyncio.get_running_loop().call_soon(connection.gates["commit"].set)
    assert ender.done()
    await ender
    assert pool.idle == 1


@pytest.mark.parametrize("attempt", REPEAT)
async def test_an_operation_cancelled_while_queued_leaves_the_block_intact(setup, attempt):  # noqa: ARG001
    _, driver = setup.pool()
    session = unique_session()
    first = Op("first", gate=asyncio.Event())
    async with session.atomic():
        running = asyncio.create_task(session.execute(first))
        await first.started.wait()
        queued = asyncio.create_task(session.execute(Op("queued")))
        for _ in range(5):
            await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        first.gate.set()
        assert await running == "first"
        assert await session.execute(Op("after")) == "after"
    assert [c[1] for c in calls(driver) if c[0] == "execute"] == ["first", "after"]
    assert calls(driver)[-1] == ("commit", None)


async def test_a_rollback_on_abort_that_hangs_is_cancelled_and_the_slot_leaves(setup, caplog):
    pool, _ = setup.pool(commit_timeout=0.02)
    lease = await setup.router.acquire(unique_session())
    connection = lease.slot.connection
    connection.gates["rollback"] = asyncio.Event()  # never set
    await lease.call("begin")
    await until(lambda: lease.aborted)
    await until(lambda: pool.size == 0)
    assert "did not finish" in caplog.text
    assert connection.closed
    await lease.release()


# The runner and the debug switch


def test_the_router_is_a_transaction_runner():
    assert isinstance(Router(), TransactionRunner)


@pytest.mark.parametrize(("switch", "loop_debug", "warns"), [(True, False, True), (False, True, False)])
async def test_set_debug_overrides_the_loop_debug_mode(setup, switch, loop_debug, warns):
    setup.pool()
    session = unique_session()
    ran, stop = asyncio.Event(), asyncio.Event()

    async def worker():
        await session.execute(Op())
        ran.set()
        await stop.wait()

    loop = asyncio.get_running_loop()
    previous = set_debug(switch)
    loop.set_debug(loop_debug)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            async with session.atomic():
                task = asyncio.create_task(worker(), name="the-worker")
                await ran.wait()
        assert any("the-worker" in str(w.message) for w in caught) is warns
    finally:
        loop.set_debug(False)
        set_debug(previous)
        stop.set()
        await task


def test_set_debug_takes_a_bool_or_none():
    with pytest.raises(TypeError):
        set_debug("yes")
    previous = set_debug(None)
    assert set_debug(previous) is None
    assert debug_enabled() is False
