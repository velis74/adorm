"""Turns of transactions on a shared database session: the order of waiting flows, cancellation racing a grant, the
deadline, a transaction that awaits the flow waiting for it, and the router taking and giving up turns."""

import asyncio
import concurrent.futures
import contextvars
import threading

import pytest

from adorm import create_session, DeadlockError
from adorm.executor import Pool, Router, turns as turns_module
from adorm.executor.turns import awaits, TransactionTurns
from tests.executor_fakes import FakeDriver, Op, Profile

REPEAT = range(20)
LONG = 30
GUARD = 5


async def until(predicate):
    async with asyncio.timeout(GUARD):
        while not predicate():
            await asyncio.sleep(0)


def deadline(seconds=LONG):
    return asyncio.get_running_loop().time() + seconds


async def take(turns, exclusive, name="s", seconds=LONG):
    return await turns.take(
        exclusive=exclusive, session=create_session("default", name), deadline=deadline(seconds), timeout=seconds
    )


def kinds(turns):
    return [turn.exclusive for turn in turns.holders]


# Granting


async def test_a_transaction_holds_the_turn_alone_and_operations_together():
    turns = TransactionTurns("test")
    first, second = await take(turns, False), await take(turns, False)
    assert kinds(turns) == [False, False]
    transaction = asyncio.create_task(take(turns, True))
    await until(lambda: turns.waiting == 1)
    first.release()
    await asyncio.sleep(0)
    assert not transaction.done()
    second.release()
    held = await transaction
    assert turns.holders == (held,)
    assert held.held
    held.release()
    held.release()
    assert turns.holders == ()
    assert not held.held


async def test_turns_are_granted_in_the_order_asked_for():
    turns = TransactionTurns("test")
    for _ in REPEAT:
        await grant_in_order(turns)


async def grant_in_order(turns):
    """Seven flows wait behind a transaction; each is released once the next ones have their turn."""
    holder = await take(turns, True)
    order = []

    async def flow(n, exclusive):
        turn = await take(turns, exclusive, f"flow-{n}")
        order.append(n)
        return turn

    pattern = [False, True, False, False, True, True, False]
    tasks = []
    for n, exclusive in enumerate(pattern):
        tasks.append(asyncio.create_task(flow(n, exclusive)))
        await until(lambda n=n: turns.waiting == n + 1)
    holder.release()
    await until(lambda: order == [0])
    # A later operation does not pass the waiting transaction 1.
    assert kinds(turns) == [False]
    tasks[0].result().release()
    await until(lambda: order == [0, 1])
    tasks[1].result().release()
    await until(lambda: order == [0, 1, 2, 3])
    assert kinds(turns) == [False, False]
    tasks[2].result().release()
    tasks[3].result().release()
    await until(lambda: order == [0, 1, 2, 3, 4])
    tasks[4].result().release()
    await until(lambda: order == [0, 1, 2, 3, 4, 5])
    tasks[5].result().release()
    await until(lambda: order == list(range(7)))
    tasks[6].result().release()
    assert turns.holders == ()
    assert turns.waiting == 0


# Cancellation and the deadline


async def test_cancelling_a_waiting_flow_lets_the_next_one_go():
    turns = TransactionTurns("test")
    for _ in REPEAT:
        holder = await take(turns, False)
        transaction = asyncio.create_task(take(turns, True))
        operation = asyncio.create_task(take(turns, False))
        await until(lambda: turns.waiting == 2)
        # The waiting transaction kept the operation waiting; without it the operation joins the holder.
        transaction.cancel()
        with pytest.raises(asyncio.CancelledError):
            await transaction
        turn = await operation
        assert kinds(turns) == [False, False]
        holder.release()
        turn.release()
        assert turns.holders == ()
        assert turns.waiting == 0


async def test_a_turn_granted_to_a_flow_cancelled_before_it_resumed_goes_to_the_next():
    turns = TransactionTurns("test")
    for _ in REPEAT:
        holder = await take(turns, True)
        granted = asyncio.create_task(take(turns, True))
        follower = asyncio.create_task(take(turns, True))
        await until(lambda: turns.waiting == 2)
        holder.release()
        assert [turn.task for turn in turns.holders] == [granted]
        granted.cancel()  # in the same step: the grant has not reached the task
        with pytest.raises(asyncio.CancelledError):
            await granted
        turn = await follower
        assert turns.holders == (turn,)
        turn.release()


async def test_a_wait_past_the_deadline_raises_deadlock_error():
    turns = TransactionTurns("the test connection")
    for _ in REPEAT:
        holder = await asyncio.create_task(take(turns, True, "holder"))  # a task of its own, which awaits nothing
        follower = asyncio.create_task(take(turns, False, "follower"))
        await until(lambda: turns.waiting == 1)
        with pytest.raises(DeadlockError, match="stayed held for 0.01 s") as raised:
            await take(turns, True, "late", seconds=0.01)
        assert raised.value.session == create_session("default", "late")
        assert raised.value.timeout == 0.01
        assert raised.value.held_sessions == ()
        assert "the test connection" in str(raised.value)
        assert not str(raised.value).endswith("; ")
        assert turns.waiting == 1
        holder.release()
        (await follower).release()
        assert turns.holders == ()
        assert turns.waiting == 0


# Deadlocks


@pytest.fixture
def no_periodic_check(monkeypatch):
    """Leave only the check when a wait starts."""
    monkeypatch.setattr(turns_module, "DEADLOCK_CHECK_INTERVAL", LONG)


async def test_a_transaction_awaiting_the_waiting_flow_fails_the_wait_at_once(no_periodic_check):  # noqa: ARG001
    turns = TransactionTurns("test")
    loop = asyncio.get_running_loop()
    for _ in REPEAT:
        started = loop.time()
        holder = await take(turns, True)
        holder_task = asyncio.current_task()
        assert holder.task is holder_task
        # The holder awaits the waiting task: the check when the wait starts finds it.
        with pytest.raises(DeadlockError, match="awaits"):
            await asyncio.create_task(take(turns, True, "awaited"), context=contextvars.Context())
        # Through gather().
        results = await asyncio.gather(take(turns, False, "gathered"), asyncio.sleep(0), return_exceptions=True)
        assert isinstance(results[0], DeadlockError)
        assert turns.holders == (holder,)
        assert turns.waiting == 0
        assert loop.time() - started < 1
        holder.release()


async def test_a_transaction_that_starts_awaiting_a_waiting_flow_later_fails_its_wait():
    turns = TransactionTurns("test")
    holder = await take(turns, True)
    waiter = asyncio.create_task(take(turns, False, "waiter"))
    await until(lambda: turns.waiting == 1)
    # The wait has a deadline of LONG seconds; the check every DEADLOCK_CHECK_INTERVAL ends it first.
    async with asyncio.timeout(GUARD):
        with pytest.raises(DeadlockError):
            await waiter
    holder.release()


async def test_a_turn_granted_to_a_transaction_that_then_awaits_a_waiting_flow_fails_that_wait():
    turns = TransactionTurns("test")
    holder = await take(turns, False)
    started, awaited = asyncio.Event(), asyncio.Event()

    async def transaction():
        turn = await take(turns, True, "transaction")
        started.set()
        try:
            await awaited.wait()
            with pytest.raises(DeadlockError):
                await waiter
        finally:
            turn.release()

    owner = asyncio.create_task(transaction())
    await until(lambda: turns.waiting == 1)
    waiter = asyncio.create_task(take(turns, False, "waiter"))
    await until(lambda: turns.waiting == 2)
    awaited.set()
    await asyncio.sleep(0)
    holder.release()
    await owner
    assert turns.holders == ()
    assert turns.waiting == 0


def test_the_periodic_check_runs_on_each_event_loop_the_turns_serve():
    """A ``TestCase`` test runs each ``async_to_sync()`` call on a loop of its own: a check left scheduled on a closed
    loop does not stop the checks on the next one."""
    turns = TransactionTurns("test")

    async def short_wait():
        holder = await take(turns, True)
        waiter = asyncio.create_task(take(turns, False, "waiter"))
        await until(lambda: turns.waiting == 1)
        holder.release()
        (await waiter).release()

    async def late_await():
        loop = asyncio.get_running_loop()
        holder = await take(turns, True)
        waiter = asyncio.create_task(take(turns, False, "waiter"))
        await until(lambda: turns.waiting == 1)
        started = loop.time()
        async with asyncio.timeout(GUARD):
            with pytest.raises(DeadlockError):
                await waiter
        holder.release()
        return loop.time() - started

    for _ in range(3):
        asyncio.run(short_wait())
        assert asyncio.run(late_await()) < 1


def test_a_turn_left_held_on_a_closed_loop_does_not_block_the_next_loop():
    turns = TransactionTurns("test")

    async def leave_held():
        return await take(turns, True)

    abandoned = asyncio.run(leave_held())

    async def next_transaction():
        async with asyncio.timeout(GUARD):
            turn = await take(turns, True)
        abandoned.release()
        assert turns.holders == (turn,)
        turn.release()
        assert turns.holders == ()

    asyncio.run(next_transaction())


async def test_awaits_follows_tasks_and_gather():
    blocker = asyncio.Event()

    async def wait():
        await blocker.wait()

    target = asyncio.create_task(wait())

    async def via():
        await target

    direct = asyncio.create_task(via())

    async def through_gather():
        await asyncio.gather(asyncio.sleep(LONG), direct)

    gathered = asyncio.create_task(through_gather())
    other = asyncio.create_task(wait())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert awaits(target, target)
    assert awaits(direct, target)
    assert awaits(gathered, target)
    assert not awaits(other, target)
    assert not awaits(target, direct)
    blocker.set()
    gathered.cancel()
    await asyncio.gather(target, other, gathered, return_exceptions=True)


@pytest.mark.parametrize(
    "wait",
    [
        lambda target: asyncio.wait_for(target, LONG),
        lambda target: asyncio.shield(target),
        lambda target: asyncio.wait([target]),
    ],
    ids=["wait_for", "shield", "wait"],
)
async def test_awaits_follows_wait_helpers(wait):
    blocker = asyncio.Event()

    async def work():
        await blocker.wait()

    target = asyncio.create_task(work())
    other = asyncio.create_task(work())

    async def via():
        await wait(target)

    waiter = asyncio.create_task(via())

    async def outer():
        await waiter

    parent = asyncio.create_task(outer())
    for _ in range(3):
        await asyncio.sleep(0)
    assert awaits(waiter, target)
    assert awaits(parent, target)
    assert not awaits(waiter, other)
    assert not awaits(target, waiter)
    blocker.set()
    await asyncio.gather(target, other, waiter, parent)


@pytest.mark.parametrize("helper", ["wait_for", "shield"])
async def test_a_transaction_awaiting_the_waiting_flow_through_a_helper_fails_the_wait_at_once(
    no_periodic_check,  # noqa: ARG001
    helper,
):
    turns = TransactionTurns("test")
    loop = asyncio.get_running_loop()
    holder = await take(turns, True)
    started = loop.time()
    waiter = asyncio.create_task(take(turns, False, "awaited"), context=contextvars.Context())
    wrapped = asyncio.wait_for(waiter, LONG) if helper == "wait_for" else asyncio.shield(waiter)
    with pytest.raises(DeadlockError, match="awaits"):
        await wrapped
    assert loop.time() - started < 1
    holder.release()


async def test_the_advice_ends_every_deadlock_message():
    turns = TransactionTurns("test", advice="use separate connections")
    holder = await take(turns, True)
    with pytest.raises(DeadlockError, match="; use separate connections$"):
        await asyncio.create_task(take(turns, False, seconds=0.01))
    holder.release()


# Waiting from a thread


async def in_thread(fn):
    return await asyncio.to_thread(fn)


async def test_a_thread_waits_for_the_open_transaction_and_releases_its_turn():
    turns = TransactionTurns("test")
    for _ in range(5):
        holder = await take(turns, True)
        taken = threading.Event()

        def statement(taken=taken):
            turn = turns.take_blocking("statement")
            taken.set()
            assert turn is not None
            assert turn.session is None
            assert turn.task is None
            turn.release()
            return turn

        thread = asyncio.create_task(in_thread(statement))
        await until(lambda: turns.waiting == 1)
        assert not taken.is_set()
        holder.release()
        turn = await thread
        await until(lambda turn=turn: not turn.held)
        assert turns.holders == ()


async def test_a_thread_waiting_past_the_timeout_raises_deadlock_error():
    turns = TransactionTurns("test")
    holder = await take(turns, True, seconds=0.05)
    with pytest.raises(DeadlockError, match="stayed held for 0.05 s while a statement waited") as raised:
        await in_thread(lambda: turns.take_blocking("statement"))
    assert raised.value.timeout == 0.05
    assert raised.value.session is None
    with pytest.raises(DeadlockError, match="stayed held for 0.01 s"):
        await in_thread(lambda: turns.take_blocking("statement", timeout=0.01))
    assert turns.waiting == 0
    holder.release()


async def test_a_turn_granted_as_the_threads_wait_times_out_is_released(monkeypatch):
    """The thread's wait ends with `DeadlockError` at the moment the loop grants the turn: the turn is given up."""
    turns = TransactionTurns("test")
    run = asyncio.run_coroutine_threadsafe

    class LateFuture:
        """Times out only once the loop has finished the wait."""

        def __init__(self, future):
            self.future = future
            self.timed_out = False

        def result(self, timeout=None):
            if self.timed_out:
                return self.future.result(timeout)
            self.timed_out = True
            concurrent.futures.wait([self.future], GUARD)
            raise concurrent.futures.TimeoutError

        def __getattr__(self, name):
            return getattr(self.future, name)

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", lambda coro, loop: LateFuture(run(coro, loop)))
    for _ in range(5):
        holder = await take(turns, True)
        thread = asyncio.create_task(in_thread(lambda: turns.take_blocking("statement")))
        await until(lambda: turns.waiting == 1)
        holder.release()
        with pytest.raises(DeadlockError, match="stopped answering"):
            await thread
        await until(lambda: turns.holders == ())
        assert turns.waiting == 0


async def test_a_thread_cannot_wait_without_a_loop_or_on_the_loop_thread():
    turns = TransactionTurns("test")
    assert await in_thread(lambda: turns.take_blocking("statement")) is None
    holder = await take(turns, True)
    assert turns.take_blocking("statement") is None
    holder.release()


# The router


class SharingDriver(FakeDriver):
    """A `FakeDriver` whose connections share one database session."""

    def __init__(self):
        super().__init__()
        self.turns = TransactionTurns("the fake session")

    async def transaction_turns(self, profile):  # noqa: ARG002
        return self.turns


def make(max_connections=4):
    driver = SharingDriver()
    pool = Pool(driver, Profile("default"), max_connections=max_connections, acquire_timeout=LONG)
    return Router([pool]), pool, driver


async def test_the_router_takes_turns_for_transactions_and_operations():
    router, _pool, driver = make()
    turns = driver.turns
    first, second = create_session("default", "first"), create_session("default", "second")
    for _ in range(5):
        gate = asyncio.Event()
        running = asyncio.create_task(router.run(first, Op("held", gate=gate)))
        await until(lambda: turns.holders and not turns.holders[0].exclusive)

        async def transaction():
            async with router.slot(second) as lease:
                assert turns.holders[0].exclusive
                assert lease._turn is turns.holders[0]
                # A flow inside the transaction's context takes no turn.
                assert await router.run(first, Op("nested")) == "nested"
                async with router.slot(first) as inner:
                    assert inner._turn is None
                assert len(turns.holders) == 1
            return lease

        task = asyncio.create_task(transaction())
        await until(lambda: turns.waiting == 1)
        gate.set()
        assert await running == "held"
        lease = await task
        assert lease._turn is None
        assert turns.holders == ()
    # A lease from acquire() takes no turn.
    lease = await router.acquire(first)
    assert lease._turn is None
    assert turns.holders == ()
    await lease.release()
    await router.close()


@pytest.mark.parametrize("attempt", REPEAT)
async def test_a_task_of_a_transaction_that_ends_while_it_waits_for_a_slot_takes_a_turn(attempt):  # noqa: ARG001
    """A task created inside the transaction takes no turn; when the transaction ends before the task has a slot, the
    task takes its own turn, and a third flow waits for it."""
    router, pool, driver = make(max_connections=2)
    turns = driver.turns
    blocker = await router.acquire(create_session("default", "blocker"))
    child_has_lease, child_done = asyncio.Event(), asyncio.Event()
    child = None

    async def child_transaction():
        async with router.slot(create_session("default", "child")) as lease:
            child_has_lease.set()
            await child_done.wait()
            return lease._turn

    async with router.slot(create_session("default", "x")):
        child = asyncio.create_task(child_transaction())
        await until(lambda: pool.waiting == 1)
    await child_has_lease.wait()
    assert len(turns.holders) == 1
    assert turns.holders[0].exclusive
    assert turns.holders[0].session == create_session("default", "child")
    third = asyncio.create_task(router.run(create_session("default", "third"), Op("third")))
    await until(lambda: turns.waiting == 1)
    assert not third.done()
    child_done.set()
    assert (await child) is not None
    assert await third == "third"
    assert turns.holders == ()
    await blocker.release()
    await router.close()


async def test_a_failed_acquisition_gives_up_its_turn():
    router, _pool, driver = make()
    driver.open_errors.append(OSError("refused"))
    with pytest.raises(OSError, match="refused"):
        await router.run(create_session("default", "s"), Op())
    assert driver.turns.holders == ()
    assert driver.turns.waiting == 0
    assert await router.run(create_session("default", "s"), Op("again")) == "again"
    assert driver.turns.holders == ()
    await router.close()


async def test_a_turn_is_given_up_when_the_slot_closes_while_leased():
    router, _pool, driver = make()
    async with router.slot(create_session("default", "s")) as lease:
        assert driver.turns.holders == (lease._turn,)
        await lease.slot.close()
    assert driver.turns.holders == ()
    await router.close()
