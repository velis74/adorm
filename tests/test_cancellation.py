"""Cancellation: the outcome of a cancelled request or operation, the transaction outcome of a cancelled block, and
the slot after each (returned to the pool or dropped, never leaked)."""

import asyncio
import gc
import itertools
import logging
import threading

import pytest

import adorm

from adorm import (
    cancellation_outcome,
    CancellationOutcome,
    create_session,
    IntegrityError,
    InterfaceError,
    OperationalError,
    TransactionOutcome,
)
from adorm.executor import Pool, Request, Router, set_runner
from adorm.executor.slot import cancelled_before_start
from tests.contract.test_threaded import FakeDriver as ThreadedFakeDriver, Op as ThreadedOp, TIMEOUT
from tests.executor_fakes import FakeDriver, Op, Profile

LONG = 30
GUARD = 5
_names = itertools.count()


def unique_session():
    return create_session("default", f"cancel-{next(_names)}")


async def until(predicate):
    """Yield to the loop until ``predicate()`` holds."""
    async with asyncio.timeout(GUARD):
        while not predicate():
            await asyncio.sleep(0)


class Setup:
    def __init__(self):
        self.router = Router()

    def pool(self, *, driver=None, max_connections=0, commit_timeout=None):
        driver = FakeDriver() if driver is None else driver
        pool = Pool(
            driver,
            Profile("default"),
            max_connections=max_connections,
            acquire_timeout=LONG,
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


def recording(awaitable, raised):
    """A task awaiting ``awaitable`` that appends the exception it raises to ``raised`` and re-raises it."""

    async def run():
        try:
            return await awaitable
        except BaseException as exc:
            raised.append(exc)
            raise

    return asyncio.create_task(run())


async def cancelled(task):
    """Cancel ``task`` and wait for it to end cancelled."""
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# cancellation_outcome()


def test_an_exception_no_cancelled_request_raised_has_no_outcome():
    assert cancellation_outcome(asyncio.CancelledError()) is None
    assert cancellation_outcome(ValueError("x")) is None
    assert not cancelled_before_start(asyncio.CancelledError())


def test_outcome_values():
    assert [o.value for o in CancellationOutcome] == ["not_started", "succeeded", "failed", "unknown"]
    assert [o.value for o in TransactionOutcome] == ["committed", "rolled_back", "unknown"]


def test_a_cause_chain_that_loops_ends():
    first, second = ValueError("a"), ValueError("b")
    first.__cause__, second.__cause__ = second, first
    assert cancellation_outcome(first) is None


# Requests on a lease


async def test_a_request_cancelled_while_queued_never_started(setup):
    _, driver = setup.pool()
    gate = asyncio.Event()
    lease = await setup.router.acquire(unique_session())
    raised = []
    blocking = asyncio.create_task(lease.call("execute", Op("first", gate=gate)))
    await until(lambda: driver.connections[0].running == 1)
    queued = recording(lease.call("execute", Op("second")), raised)
    await until(lambda: lease.requests.qsize() == 1)
    await cancelled(queued)
    assert cancellation_outcome(raised[0]) is CancellationOutcome.NOT_STARTED
    assert cancelled_before_start(raised[0])
    gate.set()
    assert await blocking == "first"
    await lease.release()
    assert driver.connections[0].calls == [("execute", "first")]


async def test_a_request_cancelled_while_running_has_an_unknown_outcome(setup):
    pool, driver = setup.pool()
    op = Op("slow", gate=asyncio.Event())
    lease = await setup.router.acquire(unique_session())
    raised = []
    task = recording(lease.call("execute", op), raised)
    await op.started.wait()
    await cancelled(task)
    assert cancellation_outcome(raised[0]) is CancellationOutcome.UNKNOWN
    await lease.release()
    # The connection reported the cancellation: whether a statement took effect stays unknown.
    assert cancellation_outcome(raised[0]) is CancellationOutcome.UNKNOWN
    assert driver.connections[0].cancelled == [op]
    assert lease.channel.awaited == 0
    assert (pool.size, pool.idle) == (1, 1)


@pytest.mark.parametrize(
    ("error", "outcome", "kept"),
    [
        (None, CancellationOutcome.SUCCEEDED, True),
        (IntegrityError("duplicate", sqlstate="23505"), CancellationOutcome.FAILED, True),
        (OperationalError("canceling statement", sqlstate="57014"), CancellationOutcome.FAILED, True),
        (InterfaceError("connection lost"), CancellationOutcome.UNKNOWN, False),
        (OperationalError("terminated", sqlstate="57P01"), CancellationOutcome.UNKNOWN, False),
    ],
)
async def test_a_request_finishing_after_its_cancellation_refines_the_outcome(setup, error, outcome, kept):
    pool, driver = setup.pool()
    gate = asyncio.Event()
    op = Op("slow", gate=gate, error=error, finishes_when_cancelled=True)
    lease = await setup.router.acquire(unique_session())
    raised = []
    task = recording(lease.call("execute", op), raised)
    await op.started.wait()
    await cancelled(task)
    assert cancellation_outcome(raised[0]) is CancellationOutcome.UNKNOWN
    connection = driver.connections[0]
    if not kept:
        connection.usable = False
    gate.set()
    await lease.release()
    assert cancellation_outcome(raised[0]) is outcome
    assert connection.cancelled == []
    assert (pool.size, pool.idle) == ((1, 1) if kept else (0, 0))


async def test_an_outcome_delivered_before_the_cancelled_caller_resumes_is_known_at_once(setup):
    _, driver = setup.pool()
    gate = asyncio.Event()
    op = Op("quick", gate=gate)
    lease = await setup.router.acquire(unique_session())
    raised = []
    task = recording(lease.call("execute", op), raised)
    await op.started.wait()
    gate.set()
    await until(lambda: lease.channel.awaited == 0)  # the slot delivered the result
    await cancelled(task)
    assert cancellation_outcome(raised[0]) is CancellationOutcome.SUCCEEDED
    await lease.release()
    assert driver.connections[0].cancelled == []


async def test_a_request_the_caller_did_not_cancel_still_raises_interface_error(setup):
    setup.pool()
    op = Op("slow", gate=asyncio.Event())
    lease = await setup.router.acquire(unique_session())
    task = asyncio.create_task(lease.call("execute", op))
    await op.started.wait()
    request = lease.slot._running[0]
    lease.requests.cancel(request)
    with pytest.raises(InterfaceError, match="cancelled by the slot"):
        await task
    await lease.release()


async def test_a_late_outcome_after_the_lease_ends_logs_no_unretrieved_exception(setup, caplog):
    pool, driver = setup.pool()
    gate = asyncio.Event()
    op = Op("slow", gate=gate, error=IntegrityError("late"), finishes_when_cancelled=True)
    lease = await setup.router.acquire(unique_session())
    raised = []
    task = recording(lease.call("execute", op), raised)
    await op.started.wait()
    await cancelled(task)
    gate.set()
    await lease.release()
    del task, raised
    gc.collect()
    await asyncio.sleep(0)
    assert "never retrieved" not in caplog.text
    assert pool.idle == 1
    assert driver.connections[0].calls == [("execute", "slow")]


# Operations outside a transaction


async def test_an_operation_cancelled_while_waiting_for_a_slot_never_started(setup):
    pool, _ = setup.pool(max_connections=1)
    lease = await setup.router.acquire(unique_session())
    raised = []
    task = recording(unique_session().execute(Op("waits")), raised)
    await until(lambda: pool.waiting == 1)
    await cancelled(task)
    assert cancellation_outcome(raised[0]) is CancellationOutcome.NOT_STARTED
    assert pool.waiting == 0
    await lease.release()
    assert (pool.size, pool.idle) == (1, 1)


@pytest.mark.parametrize("attempt", range(60))
async def test_an_operation_cancelled_racing_its_completion_reports_what_happened(setup, attempt):
    pool, driver = setup.pool(max_connections=1)
    applied = []
    gate = asyncio.Event()
    op = Op("write", gate=gate, effect=lambda _connection: applied.append(True))
    raised = []
    task = recording(unique_session().execute(op), raised)
    await op.started.wait()
    gate.set()
    for _ in range(attempt % 12):
        await asyncio.sleep(0)
    task.cancel()
    try:
        assert await task == "write"
        assert applied == [True]
    except asyncio.CancelledError:
        # Final: Router.run() has released the slot, which waited for the request to finish.
        outcome = cancellation_outcome(raised[0])
        if applied:
            assert outcome is CancellationOutcome.SUCCEEDED
        else:
            assert outcome is CancellationOutcome.UNKNOWN
            assert driver.connections[0].cancelled == [op]
    assert (pool.size, pool.in_use) == (1, 0)


async def test_the_race_produces_every_outcome(setup):
    """Across cancellation points 0 to 11 loop iterations after the operation may finish, the caller sees the result,
    a cancellation after the operation succeeded and a cancellation that reached it while it ran."""
    pool, _ = setup.pool(max_connections=1)
    seen = set()
    for attempt in range(48):
        gate = asyncio.Event()
        op = Op("write", gate=gate)
        raised = []
        task = recording(unique_session().execute(op), raised)
        await op.started.wait()
        if attempt % 12:
            gate.set()
        for _ in range(attempt % 12 - 1):
            await asyncio.sleep(0)
        task.cancel()
        try:
            seen.add(await task)
        except asyncio.CancelledError:
            seen.add(cancellation_outcome(raised[0]))
    assert seen == {"write", CancellationOutcome.SUCCEEDED, CancellationOutcome.UNKNOWN}
    assert (pool.size, pool.in_use) == (1, 0)


async def test_asyncio_timeout_keeps_the_outcome_on_its_cause(setup):
    setup.pool()
    op = Op("slow", gate=asyncio.Event())
    limits = []

    async def bounded():
        async with asyncio.timeout(LONG) as limit:
            limits.append(limit)
            return await unique_session().execute(op)

    task = asyncio.create_task(bounded())
    await op.started.wait()
    limits[0].reschedule(asyncio.get_running_loop().time())
    with pytest.raises(TimeoutError) as timed_out:
        await task
    # Python 3.11 puts the CancelledError on __context__, later versions on __cause__ as well.
    assert isinstance(timed_out.value.__cause__ or timed_out.value.__context__, asyncio.CancelledError)
    assert cancellation_outcome(timed_out.value) is CancellationOutcome.UNKNOWN


async def test_asyncio_timeout_before_a_slot_is_free_never_started(setup):
    pool, _ = setup.pool(max_connections=1)
    lease = await setup.router.acquire(unique_session())
    with pytest.raises(TimeoutError) as timed_out:
        async with asyncio.timeout(0.01):
            await unique_session().execute(Op("waits"))
    assert cancellation_outcome(timed_out.value) is CancellationOutcome.NOT_STARTED
    await lease.release()
    assert (pool.size, pool.waiting) == (1, 0)


# The threaded bridge


@pytest.fixture
async def threaded(setup):
    driver = ThreadedFakeDriver()
    pool, _ = setup.pool(driver=driver, max_connections=1)
    return pool, driver


@pytest.mark.parametrize("attempt", range(30))
async def test_a_threaded_call_completing_despite_its_cancellation_succeeded(threaded, attempt):  # noqa: ARG001
    pool, driver = threaded
    barrier = threading.Barrier(2, timeout=TIMEOUT)
    applied = threading.Event()

    def action(_connection):
        barrier.wait()
        applied.set()
        return "completed"

    raised = []
    task = recording(unique_session().execute(ThreadedOp(action)), raised)
    await asyncio.to_thread(barrier.wait)
    task.cancel()
    try:
        assert await task == "completed"
    except asyncio.CancelledError:
        # The call runs past the barrier, so it completes on its thread and the bridge reports that.
        assert cancellation_outcome(raised[0]) is CancellationOutcome.SUCCEEDED
    assert applied.is_set()
    assert (pool.size, pool.in_use, pool.idle) == (1, 0, 1)
    assert await unique_session().execute(ThreadedOp(lambda _c: "next")) == "next"


@pytest.mark.parametrize("attempt", range(10))
async def test_a_threaded_call_failing_despite_its_cancellation_failed(threaded, attempt):  # noqa: ARG001
    pool, driver = threaded
    running = threading.Event()

    def action(connection):
        running.set()
        assert connection.control.cancel_requested.wait(TIMEOUT)
        raise IntegrityError("duplicate", sqlstate="23505")

    raised = []
    task = recording(unique_session().execute(ThreadedOp(action)), raised)
    assert await asyncio.to_thread(running.wait, TIMEOUT)
    await cancelled(task)
    assert cancellation_outcome(raised[0]) is CancellationOutcome.FAILED
    assert (pool.size, pool.idle) == (1, 1)


async def test_a_threaded_call_cancelled_on_its_thread_is_unknown_and_the_slot_stays(threaded):
    pool, driver = threaded
    running = threading.Event()

    def action(connection):
        running.set()
        assert connection.control.cancel_requested.wait(TIMEOUT)
        raise OperationalError("canceling statement due to user request", sqlstate="57014")

    raised = []
    task = recording(unique_session().execute(ThreadedOp(action)), raised)
    assert await asyncio.to_thread(running.wait, TIMEOUT)
    await cancelled(task)
    assert cancellation_outcome(raised[0]) is CancellationOutcome.UNKNOWN
    assert len(driver.control.cancel_threads) == 1
    assert (pool.size, pool.idle) == (1, 1)


# Transactions


async def test_the_outcome_of_a_block(setup):
    _, driver = setup.pool()
    block = adorm.atomic()
    assert block.outcome is None
    async with block:
        await adorm.current_session().execute(Op())
        async with adorm.atomic() as savepoint:
            assert block.outcome is savepoint.outcome is None
        assert savepoint.outcome is None  # the transaction is still open
    assert block.outcome is savepoint.outcome is TransactionOutcome.COMMITTED

    with pytest.raises(ValueError, match="boom"):
        async with adorm.atomic() as failed:
            raise ValueError("boom")
    assert failed.outcome is TransactionOutcome.ROLLED_BACK

    async with adorm.atomic() as explicit:
        await explicit.rollback()
        assert explicit.outcome is TransactionOutcome.ROLLED_BACK


async def test_a_block_that_never_began_has_no_outcome(setup):
    pool, driver = setup.pool()
    driver.open_errors.append(OperationalError("refused", sqlstate="08006"))
    block = adorm.atomic()
    with pytest.raises(OperationalError):
        async with block:
            pass
    assert block.outcome is None
    assert pool.size == 0


@pytest.mark.parametrize("attempt", range(10))
async def test_cancelling_a_block_during_an_operation_rolls_back_and_returns_the_slot(setup, attempt):  # noqa: ARG001
    pool, driver = setup.pool()
    op = Op("slow", gate=asyncio.Event())
    block = adorm.atomic()
    raised = []

    async def run():
        async with block:
            await adorm.current_session().execute(op)

    task = recording(run(), raised)
    await op.started.wait()
    await cancelled(task)
    assert cancellation_outcome(raised[0]) is CancellationOutcome.UNKNOWN
    assert block.outcome is TransactionOutcome.ROLLED_BACK
    assert [c[0] for c in driver.connections[0].calls] == ["begin", "execute", "rollback"]
    assert (pool.size, pool.idle) == (1, 1)


@pytest.mark.parametrize("attempt", range(10))
async def test_a_commit_cancelled_in_the_connection_leaves_the_outcome_unknown(setup, attempt):  # noqa: ARG001
    pool, driver = setup.pool()
    block = adorm.atomic()
    raised = []
    gate = asyncio.Event()

    async def run():
        async with block:
            await adorm.current_session().execute(Op())
            connection = driver.connections[0]
            connection.gates["commit"] = gate

    task = recording(run(), raised)
    await until(lambda: driver.connections and "commit" in driver.connections[0].entered)
    await cancelled(task)
    assert block.outcome is TransactionOutcome.UNKNOWN
    assert cancellation_outcome(raised[0]) is CancellationOutcome.UNKNOWN
    assert driver.connections[0].finished == ["begin"]
    assert driver.connections[0].closed
    assert (pool.size, pool.in_use) == (0, 0)


@pytest.mark.parametrize("attempt", range(10))
async def test_a_commit_completing_despite_its_cancellation_is_committed(setup, attempt):  # noqa: ARG001
    pool, driver = setup.pool()
    block = adorm.atomic()
    gate = asyncio.Event()
    ran = []

    async def run():
        async with block:
            await adorm.current_session().execute(Op())
            block.on_commit(lambda: ran.append("callback"))
            connection = driver.connections[0]
            connection.gates["commit"] = gate
            connection.uncancellable.add("commit")

    task = asyncio.create_task(run())
    await until(lambda: driver.connections and "commit" in driver.connections[0].entered)
    task.cancel()
    await asyncio.sleep(0)
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert block.outcome is TransactionOutcome.COMMITTED
    # The commit stands, so its callbacks run, in a task of their own.
    await until(lambda: ran)
    for _ in range(5):
        await asyncio.sleep(0)
    assert ran == ["callback"]
    assert driver.connections[0].finished == ["begin", "commit"]
    assert (pool.size, pool.idle) == (1, 1)


@pytest.mark.parametrize(
    ("error", "outcome", "size"),
    [
        (InterfaceError("connection lost"), TransactionOutcome.UNKNOWN, 0),
        (OperationalError("terminated", sqlstate="57P01"), TransactionOutcome.UNKNOWN, 0),
        (IntegrityError("deferred check", sqlstate="23505"), TransactionOutcome.ROLLED_BACK, 0),
    ],
)
async def test_a_failing_commit(setup, error, outcome, size):
    pool, driver = setup.pool()
    block = adorm.atomic()

    async def run():
        async with block:
            await adorm.current_session().execute(Op())
            driver.connections[0].errors["commit"] = error

    with pytest.raises(type(error)):
        await run()
    assert block.outcome is outcome
    assert pool.size == size  # a commit that did not succeed leaves the connection in a transaction


async def test_cancelling_the_end_while_it_waits_for_pending_operations_rolls_back(setup):
    pool, driver = setup.pool()
    op = Op("slow", gate=asyncio.Event())
    block = adorm.atomic()
    raised = []

    async def run():
        async with block:
            asyncio.create_task(adorm.current_session().execute(op))
            await op.started.wait()

    task = recording(run(), raised)
    await op.started.wait()
    await until(lambda: block.closed)
    task.cancel()
    await asyncio.sleep(0)
    op.gate.set()  # the end returns the slot once the pending operation has finished
    with pytest.raises(asyncio.CancelledError):
        await task
    assert block.outcome is TransactionOutcome.ROLLED_BACK
    assert "commit" not in [c[0] for c in driver.connections[0].calls]
    assert pool.in_use == 0


async def test_a_transaction_the_slot_aborted_is_rolled_back(setup):
    setup.pool(commit_timeout=0.01)
    block = adorm.atomic()
    with pytest.raises(adorm.TransactionClosed):
        async with block:
            await until(lambda: block.lease.aborted)
    assert block.outcome is TransactionOutcome.ROLLED_BACK


async def test_an_operation_waiting_for_a_savepoint_of_another_task_never_started(setup):
    _, driver = setup.pool()
    entered = asyncio.Event()
    done = asyncio.Event()
    raised = []
    async with adorm.atomic():

        async def hold_savepoint():
            async with adorm.atomic():
                entered.set()
                await done.wait()

        holder = asyncio.create_task(hold_savepoint())
        await entered.wait()
        waiting = recording(adorm.current_session().execute(Op("waits")), raised)
        await asyncio.sleep(0)
        await cancelled(waiting)
        done.set()
        await holder
    assert cancellation_outcome(raised[0]) is CancellationOutcome.NOT_STARTED
    assert ("execute", "waits") not in driver.connections[0].calls


@pytest.mark.parametrize("attempt", range(40))
async def test_a_block_cancelled_racing_its_commit_reports_what_happened(setup, attempt):
    pool, driver = setup.pool()
    block = adorm.atomic()
    ended = asyncio.Event()

    async def run():
        async with block:
            await adorm.current_session().execute(Op())
            ended.set()

    task = asyncio.create_task(run())
    await ended.wait()
    for _ in range(attempt % 8):
        await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    committed = "commit" in driver.connections[0].finished
    if committed:
        assert block.outcome is TransactionOutcome.COMMITTED
    else:
        # UNKNOWN when the commit had reached the slot before the cancellation did.
        assert block.outcome in (TransactionOutcome.ROLLED_BACK, TransactionOutcome.UNKNOWN)
    assert pool.in_use == 0
    assert pool.size == (1 if committed else 0)


async def test_cancelling_a_queued_request_object_still_delivers_a_result_that_won(setup):
    """`RequestQueue.cancel()` is unaffected: a running request that finishes first delivers its result."""
    _, driver = setup.pool()
    gate = asyncio.Event()
    op = Op("slow", gate=gate, finishes_when_cancelled=True)
    async with await setup.router.acquire(unique_session()) as lease:
        request = Request("execute", (op,))
        lease.requests.put_nowait(request)
        await op.started.wait()
        assert lease.requests.cancel(request) is False
        gate.set()
        await lease.requests.join()
        event = lease.events.get_nowait()
    assert event.value == "slow"


async def test_callbacks_of_a_commit_whose_end_was_cancelled_run_outside_the_transaction(setup, caplog):
    _, driver = setup.pool()
    block = adorm.atomic()
    gate = asyncio.Event()
    seen = []

    def failing():
        seen.append(adorm.executor.active_transaction(adorm.current_session()))
        raise RuntimeError("callback failed")

    async def run():
        async with block:
            await adorm.current_session().execute(Op())
            block.on_commit(failing)
            block.on_commit(lambda: seen.append("not reached"))
            connection = driver.connections[0]
            connection.gates["commit"] = gate
            connection.uncancellable.add("commit")

    task = asyncio.create_task(run())
    await until(lambda: driver.connections and "commit" in driver.connections[0].entered)
    task.cancel()
    await asyncio.sleep(0)
    gate.set()
    with caplog.at_level(logging.ERROR, logger="adorm.executor"), pytest.raises(asyncio.CancelledError):
        await task
    await until(lambda: "on_commit callback failed" in caplog.text)
    assert seen == [None]
    assert block.outcome is TransactionOutcome.COMMITTED


# Outcome after the release


async def test_a_lost_connection_reported_while_the_release_is_cancelled_is_unknown(setup):
    pool, driver = setup.pool(max_connections=1)
    op = Op("write", gate=asyncio.Event(), error=InterfaceError("connection lost"))
    raised = []
    task = recording(unique_session().execute(op), raised)
    await op.started.wait()
    connection = driver.connections[0]
    connection.usable = False
    connection.close_gate = asyncio.Event()
    op.gate.set()
    await until(lambda: pool.in_use == 1 and connection.running == 0 and next(iter(pool._slots)).state == "finishing")
    task.cancel()
    await asyncio.sleep(0)
    connection.close_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancellation_outcome(raised[0]) is CancellationOutcome.UNKNOWN
    assert (pool.size, pool.in_use) == (0, 0)


@pytest.mark.parametrize("attempt", range(10))
async def test_a_second_cancellation_during_the_release_still_gives_the_final_outcome(setup, attempt):
    pool, driver = setup.pool(max_connections=1)
    gate = asyncio.Event()
    op = Op("write", gate=gate, finishes_when_cancelled=True)
    raised = []
    task = recording(unique_session().execute(op), raised)
    await op.started.wait()
    task.cancel()
    for _ in range(attempt % 5 + 1):
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The request finished after both cancellations; the raised error already says so.
    assert cancellation_outcome(raised[0]) is CancellationOutcome.SUCCEEDED
    assert (pool.size, pool.in_use, pool.idle) == (1, 0, 1)
    assert driver.connections[0].calls == [("execute", "write")]


# Savepoints and blocks ended by another task


async def test_a_rolled_back_savepoint_in_a_committed_transaction_is_rolled_back(setup):
    setup.pool()
    blocks = {}

    async def failing():
        async with adorm.atomic() as failed:
            blocks["failed"] = failed
            async with adorm.atomic() as inner:
                blocks["inner"] = inner
            raise ValueError("boom")

    async with adorm.atomic() as block:
        await adorm.current_session().execute(Op())
        with pytest.raises(ValueError, match="boom"):
            await failing()
        failed, inner = blocks["failed"], blocks["inner"]
        async with adorm.atomic() as explicit:
            await explicit.rollback()
        async with adorm.atomic() as kept:
            pass
    assert block.outcome is TransactionOutcome.COMMITTED
    assert kept.outcome is TransactionOutcome.COMMITTED
    assert failed.outcome is TransactionOutcome.ROLLED_BACK
    assert inner.outcome is TransactionOutcome.ROLLED_BACK  # released into a savepoint that rolled back
    assert explicit.outcome is TransactionOutcome.ROLLED_BACK


@pytest.mark.parametrize("sent", [True, False])
async def test_the_owner_learns_of_a_commit_another_task_cancelled(setup, sent):
    pool, driver = setup.pool()
    block = adorm.atomic()
    entered = asyncio.Event()
    committer = []
    op = Op("pending", gate=asyncio.Event())

    async def own():
        async with block:
            await adorm.current_session().execute(Op())
            if sent:
                driver.connections[0].gates["commit"] = asyncio.Event()
            else:
                asyncio.create_task(adorm.current_session().execute(op))
                await op.started.wait()
            committer.append(asyncio.create_task(block.commit()))
            entered.set()
            await until(lambda: committer[0].done())

    owner = asyncio.create_task(own())
    await entered.wait()
    if sent:
        await until(lambda: "commit" in driver.connections[0].entered)
    else:
        await until(lambda: block.closed)
    committer[0].cancel()
    await asyncio.sleep(0)
    op.gate.set()  # the cancelled end still waits for the pending operation before it returns the slot
    with pytest.raises(asyncio.CancelledError):
        await committer[0]
    with pytest.raises(adorm.TransactionManagementError, match="cancelled before it committed") as info:
        await owner
    expected = TransactionOutcome.UNKNOWN if sent else TransactionOutcome.ROLLED_BACK
    assert block.outcome is expected
    assert isinstance(info.value.__cause__, asyncio.CancelledError)
    assert pool.in_use == 0
