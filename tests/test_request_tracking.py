"""Requests running on slots: `Slot.running`, `running_requests()` and `on_request_started()` follow a request from the
start of its driver connection call to its end, whatever ends it, and never cross event loops."""

import asyncio
import itertools
import logging
import threading

import pytest

import adorm

from adorm import cancellation_outcome, CancellationOutcome, create_session, IntegrityError
from adorm.executor import on_request_started, Pool, Router, running_requests, RunningRequest, set_runner
from tests.executor_fakes import FakeDriver, Op, OptionsDriver, Profile

GUARD = 5
REPEAT = range(10)
_names = itertools.count()


def unique_session():
    return create_session("default", f"tracking-{next(_names)}")


async def until(predicate):
    async with asyncio.timeout(GUARD):
        while not predicate():
            await asyncio.sleep(0)


@pytest.fixture
async def router():
    router = Router()
    previous = set_runner(router)
    try:
        yield router
    finally:
        set_runner(previous)
        await router.close()


def add_pool(router, driver=None, **kwargs):
    driver = FakeDriver() if driver is None else driver
    pool = Pool(driver, Profile("default"), acquire_timeout=GUARD, commit_timeout=None, **kwargs)
    router.add(pool)
    return pool, driver


async def test_a_running_operation_is_reported_until_it_ends(router):
    pool, _driver = add_pool(router)
    session = unique_session()
    op = Op("slow", gate=asyncio.Event())
    started = []
    stop = on_request_started(started.append)
    try:
        task = asyncio.create_task(session.execute(op))
        await op.started.wait()
        (running,) = running_requests()
        assert started == [running]
        assert isinstance(running, RunningRequest)
        assert running.operation is op
        assert running.request.method == "execute"
        assert running.session is session
        assert running.sender is task
        assert running.slot.running is running
        assert running.running
        assert running.backend_pid is None  # the fake connection does not report one
        op.gate.set()
        assert await task == "slow"
        assert running_requests() == ()
        assert not running.running
        assert running.slot.running is None
    finally:
        stop()


@pytest.mark.parametrize("ending", ["error", "cancelled", "slot closed"])
async def test_a_request_is_no_longer_running_however_it_ends(router, ending):
    pool, _driver = add_pool(router)
    op = Op("slow", gate=asyncio.Event(), error=IntegrityError("duplicate") if ending == "error" else None)
    task = asyncio.create_task(unique_session().execute(op))
    await op.started.wait()
    (running,) = running_requests()
    if ending == "error":
        op.gate.set()
        with pytest.raises(IntegrityError):
            await task
    elif ending == "cancelled":
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as raised:
            await task
        assert cancellation_outcome(raised.value) is CancellationOutcome.UNKNOWN
    else:
        await running.slot.close()
        with pytest.raises(adorm.InterfaceError):
            await task
    assert running_requests() == ()
    assert not running.running


async def test_a_request_cancelled_while_queued_is_never_reported(router):
    add_pool(router, max_connections=1)
    session = unique_session()
    started = []
    stop = on_request_started(started.append)
    try:
        async with session.atomic():
            first = Op("first", gate=asyncio.Event())
            running_first = asyncio.create_task(session.execute(first))
            await first.started.wait()
            queued_op = Op("queued")
            queued = asyncio.create_task(session.execute(queued_op))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            queued.cancel()
            with pytest.raises(asyncio.CancelledError) as raised:
                await queued
            assert cancellation_outcome(raised.value) is CancellationOutcome.NOT_STARTED
            first.gate.set()
            await running_first
        assert [r.operation for r in started if r.operation is not None] == [first]
        assert not queued_op.started.is_set()
    finally:
        stop()


async def test_a_call_starts_only_once_its_connection_options_are_set(router):
    pool, driver = add_pool(router, driver=OptionsDriver())
    session = unique_session()
    lease = await router.acquire(session)
    connection = lease.slot.connection
    connection.gates["set_options"] = gate = asyncio.Event()
    started = []
    stop = on_request_started(started.append)
    try:
        op = Op("timed")
        task = asyncio.create_task(lease.call("execute", op, options={"statement_timeout": 5}))
        await connection.entered.setdefault("set_options", asyncio.Event()).wait()
        assert running_requests() == ()
        assert started == []
        assert lease.slot.running is None
        gate.set()
        assert await task == "timed"
        assert [r.operation for r in started] == [op]
    finally:
        stop()
        await lease.release()


async def test_a_request_cancelled_while_its_options_are_set_never_starts(router):
    pool, driver = add_pool(router, driver=OptionsDriver())
    lease = await router.acquire(unique_session())
    connection = lease.slot.connection
    connection.gates["set_options"] = asyncio.Event()
    started = []
    stop = on_request_started(started.append)
    try:
        op = Op("timed")
        task = asyncio.create_task(lease.call("execute", op, options={"statement_timeout": 5}))
        await connection.entered.setdefault("set_options", asyncio.Event()).wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as raised:
            await task
        await lease.release()  # the outcome is final once the slot has finished the request
        assert cancellation_outcome(raised.value) is CancellationOutcome.NOT_STARTED
        assert started == []
        assert running_requests() == ()
        assert not op.started.is_set()
    finally:
        stop()
        await lease.release()


async def test_the_sender_is_the_task_that_sent_the_operation_inside_a_transaction(router):
    add_pool(router)
    session = unique_session()
    gates = [asyncio.Event(), asyncio.Event()]
    ops = [Op(f"child-{i}", gate=gate) for i, gate in enumerate(gates)]
    async with session.atomic():
        children = [asyncio.create_task(session.execute(op)) for op in ops]
        await ops[0].started.wait()
        (running,) = running_requests()  # one slot: the second child's operation is queued
        assert running.sender is children[0]
        assert running.session is session
        gates[0].set()
        await ops[1].started.wait()
        (running,) = running_requests()
        assert running.sender is children[1]
        gates[1].set()
        await asyncio.gather(*children)
    assert running_requests() == ()


async def test_transaction_calls_are_reported_without_an_operation(router):
    add_pool(router)
    session = unique_session()
    started = []
    stop = on_request_started(started.append)
    try:
        async with session.atomic():
            await session.execute(Op("inside"))
    finally:
        stop()
    assert [r.request.method for r in started] == ["begin", "execute", "commit"]
    assert [r.operation for r in started if r.operation is not None][0].name == "inside"
    assert all(r.operation is None for r in started if r.request.method != "execute")


async def test_a_failing_callback_is_logged_and_the_request_still_runs(router, caplog):
    add_pool(router)

    def fail(_running):
        raise RuntimeError("callback failed")

    seen = []
    stop_failing = on_request_started(fail)
    stop_seen = on_request_started(seen.append)
    try:
        with caplog.at_level(logging.ERROR, logger="adorm.executor"):
            assert await unique_session().execute(Op("runs")) == "runs"
    finally:
        stop_failing()
        stop_seen()
    assert len(seen) == 1
    assert "callback failed" in caplog.text


async def test_a_stopped_callback_is_no_longer_called(router):
    add_pool(router)
    seen = []
    stop = on_request_started(seen.append)
    await unique_session().execute(Op("one"))
    stop()
    stop()  # a second call does nothing
    await unique_session().execute(Op("two"))
    assert [r.operation.name for r in seen] == ["one"]


def test_a_callback_needs_a_running_loop_and_must_be_callable():
    with pytest.raises(RuntimeError):
        on_request_started(print)
    with pytest.raises(RuntimeError):
        running_requests()


async def test_a_callback_must_be_callable():
    with pytest.raises(TypeError):
        on_request_started("not callable")  # type: ignore[arg-type]


@pytest.mark.parametrize("repeat", REPEAT)
async def test_concurrent_operations_are_each_reported_once_and_none_is_left(router, repeat):  # noqa: ARG001
    pool, driver = add_pool(router, max_connections=3)
    started = []
    stop = on_request_started(started.append)
    gate = asyncio.Event()
    ops = [Op(f"op-{i}", gate=gate) for i in range(12)]
    try:
        tasks = [asyncio.create_task(unique_session().execute(op)) for op in ops]
        await until(lambda: len(running_requests()) == 3)
        assert {r.slot for r in running_requests()} == {lease.slot for lease in pool.leases()}
        tasks[0].cancel()  # cancellation racing the others' completion
        gate.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        stop()
    operations = [r.operation for r in started]
    assert len(operations) == len(set(map(id, operations)))
    assert set(map(id, operations)) <= set(map(id, ops))
    assert running_requests() == ()
    assert pool.in_use == 0


async def test_requests_of_another_event_loop_are_not_seen(router):
    add_pool(router)
    other_started = threading.Event()
    release_other = threading.Event()
    other_seen = []
    errors = []

    async def other_loop():
        other = Router()
        other.add(Pool(FakeDriver(), Profile("default"), acquire_timeout=GUARD))
        stop = on_request_started(other_seen.append)
        try:
            lease = await other.acquire(unique_session())
            gate = asyncio.Event()
            task = asyncio.create_task(lease.call("execute", Op("other", gate=gate)))
            while not running_requests():
                await asyncio.sleep(0)
            other_started.set()
            await asyncio.get_running_loop().run_in_executor(None, release_other.wait)
            gate.set()
            await task
            await lease.release()
        finally:
            stop()
            await other.close()

    def run_other():
        try:
            asyncio.run(other_loop())
        except BaseException as exc:
            errors.append(exc)
            other_started.set()

    thread = threading.Thread(target=run_other)
    thread.start()
    seen = []
    stop = on_request_started(seen.append)
    try:
        assert await asyncio.get_running_loop().run_in_executor(None, other_started.wait, GUARD)
        assert running_requests() == ()
        await unique_session().execute(Op("mine"))
    finally:
        stop()
        release_other.set()
        await asyncio.get_running_loop().run_in_executor(None, thread.join, GUARD)
    assert errors == []
    assert [r.operation.name for r in seen] == ["mine"]
    assert [r.operation.name for r in other_seen] == ["other"]
