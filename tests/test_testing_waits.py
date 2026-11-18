"""`adorm.testing`: waiting until an operation has been sent to the database or waits there for a lock, over a driver
whose monitor the test controls; the races between the wait and the operation's own end, the task that never sends
it, timeouts and cancellation, and no monitor or callback left behind."""

import asyncio
import itertools

import pytest

from adorm import create_session, IntegrityError, NotSupportedError
from adorm.drivers import BackendActivity, DatabaseMonitor
from adorm.executor import on_request_started, Pool, Router, set_runner
from adorm.executor.slot import _watchers
from adorm.testing import LockWait, running_operations, wait_for_lock, wait_for_operation
from tests.executor_fakes import FakeConnection, FakeDriver, Op, Profile

GUARD = 5
REPEAT = range(10)
_names = itertools.count()


def unique_session():
    return create_session("default", f"waits-{next(_names)}")


class PidConnection(FakeConnection):
    @property
    def backend_pid(self):
        return 1000 + self.number


class SharedSessionConnection(PidConnection):
    """Shares its database session: a call has it only while ``driver.holder`` is this connection."""

    @property
    def holds_session(self):
        return self.driver.holder is self

    @property
    def session_holds(self):
        return self.driver.holds.get(self, 0)

    def take(self):
        self.driver.holder = self
        self.driver.holds[self] = self.session_holds + 1


class FakeMonitor(DatabaseMonitor):
    """Reads ``driver.activity``; ``driver.read_gate`` holds a read once ``driver.reading`` is set."""

    def __init__(self, driver):
        self.driver = driver
        self.closed = False

    async def activity(self, pids):
        pids = list(pids)
        self.driver.reads.append(pids)
        self.driver.reading.set()
        if self.driver.on_read is not None:
            self.driver.on_read(len(self.driver.reads))
        if self.driver.read_gate is not None:
            await self.driver.read_gate.wait()
        return {pid: self.driver.activity[pid] for pid in pids if pid in self.driver.activity}

    async def close(self):
        self.closed = True


class MonitoredDriver(FakeDriver):
    name = "fake-monitored"

    def __init__(self):
        super().__init__()
        self.activity: dict[int, BackendActivity] = {}
        self.monitors: list[FakeMonitor] = []
        self.reads: list[list[int]] = []
        self.reading = asyncio.Event()
        self.read_gate: asyncio.Event | None = None
        self.on_read = None

    async def open_connection(self, profile, emit):  # noqa: ARG002
        connection = PidConnection(self, emit, len(self.connections) + 1)
        self.connections.append(connection)
        return connection

    async def open_monitor(self, profile):  # noqa: ARG002
        monitor = FakeMonitor(self)
        self.monitors.append(monitor)
        return monitor

    def lock(self, pid, *blockers):
        self.activity[pid] = BackendActivity(pid, "active", "Lock", "transactionid", blockers)


class SharedSessionDriver(MonitoredDriver):
    name = "fake-shared"

    def __init__(self):
        super().__init__()
        self.holder = None
        self.holds = {}

    async def open_connection(self, profile, emit):  # noqa: ARG002
        connection = SharedSessionConnection(self, emit, len(self.connections) + 1)
        self.connections.append(connection)
        return connection


class PidOnlyDriver(FakeDriver):
    """Reports backend pids but has no monitor."""

    name = "fake-pid-only"

    async def open_connection(self, profile, emit):  # noqa: ARG002
        connection = PidConnection(self, emit, len(self.connections) + 1)
        self.connections.append(connection)
        return connection


@pytest.fixture
async def router():
    router = Router()
    previous = set_runner(router)
    try:
        yield router
    finally:
        set_runner(previous)
        await router.close()
    assert not _watchers, "a wait left its callback registered"


def add_pool(router, driver, name="default"):
    pool = Pool(driver, Profile(name), acquire_timeout=GUARD, commit_timeout=None)
    router.add(pool)
    return pool


async def finish(*tasks):
    await asyncio.gather(*tasks, return_exceptions=True)


# wait_for_operation


async def test_an_operation_already_running_is_returned_at_once(router):
    add_pool(router, FakeDriver())
    op = Op("slow", gate=asyncio.Event())
    task = asyncio.create_task(unique_session().execute(op))
    await op.started.wait()
    running = await wait_for_operation(task)
    assert running.operation is op
    assert running.sender is task
    assert running.running
    assert running_operations(task) == (running,)
    op.gate.set()
    await task


@pytest.mark.parametrize("repeat", REPEAT)
async def test_an_operation_that_ends_at_once_is_still_returned(router, repeat):  # noqa: ARG001
    add_pool(router, FakeDriver())
    op = Op("quick")
    waiting = asyncio.create_task(wait_for_operation())
    await asyncio.sleep(0)  # the wait is registered before the operation is sent
    assert await unique_session().execute(op) == "quick"
    running = await waiting
    assert running.operation is op
    assert not running.running


async def test_the_operation_of_the_given_task_is_awaited_not_an_earlier_one(router):
    add_pool(router, FakeDriver())
    first = Op("first", gate=asyncio.Event())
    other = asyncio.create_task(unique_session().execute(first))
    await first.started.wait()
    mine_gate = asyncio.Event()
    second = Op("second", gate=asyncio.Event())

    async def mine():
        await mine_gate.wait()
        return await unique_session().execute(second)

    task = asyncio.create_task(mine())
    waiting = asyncio.create_task(wait_for_operation(task))
    await asyncio.sleep(0)
    assert not waiting.done()
    mine_gate.set()
    running = await waiting
    assert running.operation is second
    assert running.sender is task
    first.gate.set()
    second.gate.set()
    await finish(other, task)


async def test_a_session_selects_the_operation(router):
    add_pool(router, FakeDriver())
    one, two = unique_session(), unique_session()
    op_one, op_two = Op("one", gate=asyncio.Event()), Op("two", gate=asyncio.Event())
    tasks = [asyncio.create_task(one.execute(op_one)), asyncio.create_task(two.execute(op_two))]
    running = await wait_for_operation(session=two)
    assert running.operation is op_two
    assert running.session is two
    op_one.gate.set()
    op_two.gate.set()
    await finish(*tasks)


async def test_transaction_calls_do_not_count_as_operations(router):
    driver = FakeDriver()
    add_pool(router, driver)
    session = unique_session()
    begin_gate = asyncio.Event()

    async def transaction():
        async with session.atomic():
            await session.execute(Op("inside"))

    def hold_begin(running):
        if running.request.method == "begin":
            running.slot.connection.gates["begin"] = begin_gate

    stop = on_request_started(hold_begin)
    try:
        task = asyncio.create_task(transaction())
        with pytest.raises(TimeoutError, match="within 0.05 s"):
            await wait_for_operation(session=session, timeout=0.05)
    finally:
        stop()
    begin_gate.set()
    await task


async def test_a_task_that_finishes_without_an_operation_raises_runtime_error(router):
    add_pool(router, FakeDriver())

    async def fails():
        raise IntegrityError("refused before sending")

    task = asyncio.create_task(fails())
    with pytest.raises(RuntimeError, match="raised IntegrityError") as raised:
        await wait_for_operation(task)
    assert isinstance(raised.value.__cause__, IntegrityError)

    returned = asyncio.create_task(asyncio.sleep(0))
    with pytest.raises(RuntimeError, match="returned before an operation"):
        await wait_for_operation(returned)

    cancelled = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(RuntimeError, match="was cancelled"):
        await wait_for_operation(cancelled)


async def test_no_operation_within_the_timeout_raises_timeout_error(router):
    add_pool(router, FakeDriver())
    with pytest.raises(TimeoutError, match="no operation was sent to the database within 0.01 s"):
        await wait_for_operation(timeout=0.01)


async def test_a_cancelled_wait_leaves_no_callback(router):
    add_pool(router, FakeDriver())
    waiting = asyncio.create_task(wait_for_operation())
    await asyncio.sleep(0)
    assert _watchers
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert not _watchers


async def test_an_operation_waiting_for_a_shared_session_is_returned_once_it_has_it(router):
    driver = SharedSessionDriver()
    add_pool(router, driver)
    op = Op("shared", gate=asyncio.Event())
    task = asyncio.create_task(unique_session().execute(op))
    await op.started.wait()
    [running] = running_operations(task)
    assert not running.in_session
    assert not running.reached_session
    with pytest.raises(TimeoutError):
        await wait_for_operation(task, timeout=0.05, interval=0)
    waiting = asyncio.create_task(wait_for_operation(task, interval=0))
    await asyncio.sleep(0.01)
    assert not waiting.done()
    driver.connections[0].take()
    assert await waiting is running
    assert running.in_session
    op.gate.set()
    await task
    assert running.reached_session
    assert not running.in_session


@pytest.mark.parametrize("repeat", REPEAT)
async def test_an_operation_that_ended_without_its_shared_session_is_not_returned(router, repeat):  # noqa: ARG001
    driver = SharedSessionDriver()
    add_pool(router, driver)
    session = unique_session()
    first = Op("never held", gate=asyncio.Event())
    waiting = asyncio.create_task(wait_for_operation(session=session, interval=0))
    await asyncio.sleep(0)
    task = asyncio.create_task(session.execute(first))
    await first.started.wait()
    first.gate.set()
    await task
    assert not waiting.done()
    second = Op("held", gate=asyncio.Event())
    task = asyncio.create_task(session.execute(second))
    await second.started.wait()
    driver.connections[0].take()
    assert (await waiting).operation is second
    second.gate.set()
    await task


# wait_for_lock


async def test_a_lock_wait_is_reported_with_its_blockers(router):
    driver = MonitoredDriver()
    add_pool(router, driver)
    op = Op("locked", gate=asyncio.Event())
    task = asyncio.create_task(unique_session().execute(op))
    await op.started.wait()
    driver.lock(1001, 4242)
    wait = await wait_for_lock(task)
    assert isinstance(wait, LockWait)
    assert wait.operation is op
    assert wait.running.sender is task
    assert wait.blocking_pids == (4242,)
    assert wait.activity.wait_event == "transactionid"
    assert [m.closed for m in driver.monitors] == [True]
    op.gate.set()
    await task


async def test_a_commit_waiting_for_a_lock_is_reported(router):
    driver = MonitoredDriver()
    add_pool(router, driver)
    session = unique_session()
    inside, proceed = asyncio.Event(), asyncio.Event()

    async def transaction():
        async with session.atomic():
            await session.execute(Op("inside"))
            driver.connections[0].gates["commit"] = asyncio.Event()
            inside.set()
            await proceed.wait()

    task = asyncio.create_task(transaction())
    await inside.wait()
    driver.lock(1001, 4242)
    # the poll interval exceeds the timeout: only the commit's start wakes the wait
    waiting = asyncio.create_task(wait_for_lock(task, timeout=GUARD, interval=2 * GUARD))
    await asyncio.sleep(0)
    proceed.set()
    try:
        wait = await waiting
    finally:
        driver.connections[0].gates["commit"].set()
    assert wait.request.method == "commit"
    assert wait.request is wait.running.request
    assert wait.operation is None
    assert wait.running.sender is task
    assert wait.blocking_pids == (4242,)
    await task


async def test_a_commit_waiting_for_a_lock_when_the_wait_begins_is_reported(router):
    driver = MonitoredDriver()
    add_pool(router, driver)
    session = unique_session()
    driver.lock(1001, 4242)
    entered = asyncio.Event()

    async def transaction():
        async with session.atomic():
            await session.execute(Op("inside"))
            connection = driver.connections[0]
            connection.gates["commit"] = asyncio.Event()
            connection.entered["commit"] = entered

    task = asyncio.create_task(transaction())
    await entered.wait()
    assert running_operations(task) == ()
    try:
        wait = await wait_for_lock(task, timeout=GUARD)
    finally:
        driver.connections[0].gates["commit"].set()
    assert wait.request.method == "commit"
    assert wait.operation is None
    assert wait.running.sender is task
    await task


async def test_the_wait_polls_until_the_operation_waits(router):
    driver = MonitoredDriver()
    add_pool(router, driver)
    op = Op("later", gate=asyncio.Event())
    task = asyncio.create_task(unique_session().execute(op))
    await op.started.wait()
    driver.activity[1001] = BackendActivity(1001, "active")

    def on_read(count):
        if count == 3:
            driver.lock(1001, 7)

    driver.on_read = on_read
    wait = await wait_for_lock(task, interval=0)
    assert len(driver.reads) == 3
    assert wait.blocking_pids == (7,)
    assert len(driver.monitors) == 1  # opened once, reused by every read
    op.gate.set()
    await task


async def test_an_operation_sent_after_the_wait_began_is_found(router):
    driver = MonitoredDriver()
    add_pool(router, driver)
    driver.lock(1001, 9)
    send = asyncio.Event()
    op = Op("sent later", gate=asyncio.Event())

    async def flow():
        await send.wait()
        return await unique_session().execute(op)

    task = asyncio.create_task(flow())
    waiting = asyncio.create_task(wait_for_lock(task, interval=GUARD))  # only the start wakes the wait
    await asyncio.sleep(0)
    send.set()
    wait = await waiting
    assert wait.operation is op
    op.gate.set()
    await task


@pytest.mark.parametrize("repeat", REPEAT)
async def test_a_lock_wait_of_an_operation_that_ended_during_the_read_is_not_reported(router, repeat):  # noqa: ARG001
    driver = MonitoredDriver()
    add_pool(router, driver)
    first = Op("first", gate=asyncio.Event())
    task = asyncio.create_task(unique_session().execute(first))
    await first.started.wait()
    driver.lock(1001, 5)
    driver.read_gate = asyncio.Event()
    waiting = asyncio.create_task(wait_for_lock(task, timeout=0.2, interval=0))
    await driver.reading.wait()
    # The operation ends while the monitor reads; what the read saw may belong to it or to a later request.
    first.gate.set()
    await task
    driver.read_gate.set()
    with pytest.raises(RuntimeError, match="returned before a request"):
        await waiting
    assert all(m.closed for m in driver.monitors)


async def test_a_lock_wait_of_a_shared_session_is_reported_only_for_the_call_holding_it(router):
    driver = SharedSessionDriver()
    add_pool(router, driver)
    holding, queued = Op("holding", gate=asyncio.Event()), Op("queued", gate=asyncio.Event())
    first = asyncio.create_task(unique_session().execute(holding))
    second = asyncio.create_task(unique_session().execute(queued))
    await holding.started.wait()
    await queued.started.wait()
    first_connection, second_connection = (running.slot.connection for running in running_operations())
    first_connection.take()
    # Both connections report one pid, as on the shared test connection of Django's TestCase.
    driver.lock(first_connection.backend_pid, 77)
    driver.lock(second_connection.backend_pid, 77)
    assert (await wait_for_lock(first)).operation is holding
    with pytest.raises(TimeoutError):
        await wait_for_lock(second, timeout=0.05, interval=0)
    second_connection.take()
    assert (await wait_for_lock(second)).operation is queued
    holding.gate.set()
    queued.gate.set()
    await finish(first, second)


async def test_a_lock_wait_read_while_the_shared_session_changed_hands_is_not_reported(router):
    driver = SharedSessionDriver()
    add_pool(router, driver)
    op = Op("held", gate=asyncio.Event())
    task = asyncio.create_task(unique_session().execute(op))
    await op.started.wait()
    connection = driver.connections[0]
    connection.take()
    driver.lock(connection.backend_pid, 3)

    def on_read(count):
        if count == 1:
            # Released and taken again during the read: what it saw may belong to another call.
            driver.holder = None
            connection.take()

    driver.on_read = on_read
    wait = await wait_for_lock(task, interval=0)
    assert len(driver.reads) == 2
    assert wait.operation is op
    op.gate.set()
    await task


async def test_a_driver_without_backend_pids_is_not_supported(router):
    add_pool(router, FakeDriver())
    op = Op("slow", gate=asyncio.Event())
    task = asyncio.create_task(unique_session().execute(op))
    await op.started.wait()
    with pytest.raises(NotSupportedError, match="does not report the database session"):
        await wait_for_lock(task)
    op.gate.set()
    await task


async def test_a_driver_without_a_monitor_is_not_supported(router):
    add_pool(router, PidOnlyDriver())
    op = Op("slow", gate=asyncio.Event())
    task = asyncio.create_task(unique_session().execute(op))
    await op.started.wait()
    with pytest.raises(NotSupportedError, match="cannot monitor"):
        await wait_for_lock(task)
    op.gate.set()
    await task


async def test_a_task_that_finishes_without_waiting_raises_and_closes_the_monitor(router):
    driver = MonitoredDriver()
    add_pool(router, driver)
    op = Op("not locked", gate=asyncio.Event())
    task = asyncio.create_task(unique_session().execute(op))
    await op.started.wait()
    driver.activity[1001] = BackendActivity(1001, "active")
    waiting = asyncio.create_task(wait_for_lock(task, interval=GUARD))
    await driver.reading.wait()
    op.gate.set()
    with pytest.raises(RuntimeError, match="returned before a request"):
        await waiting
    assert [m.closed for m in driver.monitors] == [True]


async def test_no_lock_wait_within_the_timeout_raises_timeout_error(router):
    driver = MonitoredDriver()
    add_pool(router, driver)
    op = Op("free", gate=asyncio.Event())
    task = asyncio.create_task(unique_session().execute(op))
    await op.started.wait()
    with pytest.raises(TimeoutError, match="waited for a lock within 0.05 s"):
        await wait_for_lock(task, timeout=0.05)
    assert [m.closed for m in driver.monitors] == [True]
    op.gate.set()
    await task


async def test_a_wait_cancelled_during_a_read_closes_the_monitor(router):
    driver = MonitoredDriver()
    add_pool(router, driver)
    op = Op("slow", gate=asyncio.Event())
    task = asyncio.create_task(unique_session().execute(op))
    await op.started.wait()
    driver.read_gate = asyncio.Event()
    waiting = asyncio.create_task(wait_for_lock(task))
    await driver.reading.wait()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert [m.closed for m in driver.monitors] == [True]
    op.gate.set()
    await task


async def test_each_pool_gets_a_monitor_of_its_own(router):
    first, second = MonitoredDriver(), MonitoredDriver()
    add_pool(router, first)
    add_pool(router, second, name="other")
    one = create_session("default", f"waits-{next(_names)}")
    other = create_session("other", f"waits-{next(_names)}")
    ops = [Op("one", gate=asyncio.Event()), Op("other", gate=asyncio.Event())]
    tasks = [asyncio.create_task(one.execute(ops[0])), asyncio.create_task(other.execute(ops[1]))]
    await asyncio.gather(ops[0].started.wait(), ops[1].started.wait())
    second.lock(1001, 3)
    wait = await wait_for_lock(interval=0)
    assert wait.operation is ops[1]
    assert wait.running.session is other
    assert len(first.monitors) == len(second.monitors) == 1
    assert first.reads
    assert second.reads
    assert all(m.closed for m in first.monitors + second.monitors)
    for op in ops:
        op.gate.set()
    await finish(*tasks)
