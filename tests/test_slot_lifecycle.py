"""The slot lifecycle: nothing on acquisition, the end of a lease that closes the slot (an unclean end, a connection
error, custom SQL), the connection's settling after its last request, and idle slots purged after
``purge_stale_connections_at``."""

import asyncio
import itertools

import pytest

import adorm

from adorm import create_session, IntegrityError, OperationalError, TransactionClosed
from adorm.events import CallbackEvent, ON_COMMIT
from adorm.executor import CloseReason, Pool, Router, set_runner
from tests.executor_fakes import FakeConnection, FakeDriver, Op, Profile

REPEAT = range(20)
LONG = 30
GUARD = 5
_names = itertools.count()


def session():
    return create_session("default", f"life-{next(_names)}")


async def until(predicate):
    """Yield to the loop until ``predicate()`` holds."""
    async with asyncio.timeout(GUARD):
        while not predicate():
            await asyncio.sleep(0)


def lost():
    return OperationalError("terminating connection due to administrator command", sqlstate="57P01")


def custom_sql(connection: FakeConnection) -> None:
    connection.mark_custom_sql()


class Setup:
    def __init__(self):
        self.router = Router()

    def pool(self, driver=None, profile=None, **settings):
        driver = driver or FakeDriver()
        settings.setdefault("acquire_timeout", LONG)
        pool = Pool(driver, profile or Profile("default"), **settings)
        self.router.add(pool)
        return pool, driver


class Clock:
    """The running loop's clock, advanced only by `advance()`."""

    def __init__(self, loop):
        self.now = loop.time()

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
async def clock(monkeypatch):
    loop = asyncio.get_running_loop()
    fake = Clock(loop)
    monkeypatch.setattr(loop, "time", fake.time)
    return fake


@pytest.fixture
async def setup():
    built = Setup()
    previous = set_runner(built.router)
    try:
        yield built
    finally:
        set_runner(previous)
        await built.router.close()


# Acquisition


async def test_acquiring_and_releasing_a_slot_does_nothing_to_its_connection(setup):
    pool, driver = setup.pool(max_connections=1)
    for _ in range(10):
        lease = await setup.router.acquire(session())
        await lease.release()
    assert len(driver.connections) == 1
    connection = driver.connections[0]
    assert (connection.calls, connection.option_calls, connection.closed) == ([], [], False)
    assert (pool.size, pool.idle) == (1, 1)


async def test_a_reused_slot_runs_only_the_requests_of_its_leases(setup):
    _, driver = setup.pool(max_connections=1)
    for name in ("a", "b", "c"):
        assert await setup.router.run(session(), Op(name)) == name
    assert driver.connections[0].calls == [("execute", "a"), ("execute", "b"), ("execute", "c")]


# Ends that close the slot


async def test_a_lease_ended_inside_a_transaction_closes_the_slot(setup):
    pool, driver = setup.pool(max_connections=1)
    lease = await setup.router.acquire(session())
    slot = lease.slot
    await lease.call("begin")
    await lease.call("execute", Op("write"))
    await lease.release()  # neither commit nor rollback
    assert driver.connections[0].closed
    assert slot.close_reason is CloseReason.UNCLEAN_END
    assert pool.size == 0
    assert await setup.router.run(session(), Op("next")) == "next"
    assert len(driver.connections) == 2


@pytest.mark.parametrize("method", ["commit", "rollback"])
async def test_a_failed_end_of_a_transaction_closes_the_slot(setup, method):
    _, driver = setup.pool(max_connections=1)
    lease = await setup.router.acquire(session())
    await lease.call("begin")
    driver.connections[0].errors[method] = IntegrityError("deferred constraint", sqlstate="23503")
    with pytest.raises(IntegrityError):
        await lease.call(method)
    await lease.release()
    assert lease.slot.close_reason is CloseReason.UNCLEAN_END


@pytest.mark.parametrize("method", ["commit", "rollback"])
async def test_a_clean_end_of_a_transaction_keeps_the_slot(setup, method):
    pool, driver = setup.pool(max_connections=1)
    async with await setup.router.acquire(session()) as lease:
        await lease.call("begin")
        await lease.call("execute", Op("write"))
        await lease.call(method)
    assert lease.slot.close_reason is None
    assert (pool.size, pool.idle, driver.connections[0].closed) == (1, 1, False)


async def test_a_lost_client_inside_a_transaction_closes_the_slot(setup):
    pool, driver = setup.pool(max_connections=1, commit_timeout=0.01)
    lease = await setup.router.acquire(session())
    slot = lease.slot
    await lease.call("begin")
    # The client sends nothing more: the slot rolls back and leaves the pool on its own.
    await until(lambda: slot.close_reason is not None)
    assert slot.close_reason is CloseReason.ABORTED
    assert driver.connections[0].calls == [("begin", None), ("rollback", None)]
    with pytest.raises(TransactionClosed):
        await lease.call("commit")
    await until(lambda: pool.size == 0)
    assert await setup.router.run(session(), Op("next")) == "next"
    assert driver.connections[1].calls == [("execute", "next")]


async def test_a_connection_error_closes_the_slot_although_the_connection_reports_itself_usable(setup):
    pool, driver = setup.pool(max_connections=1)
    with pytest.raises(OperationalError):
        await setup.router.run(session(), Op(error=lost()))
    connection = driver.connections[0]
    assert connection.usable  # the driver did not notice; the slot did
    assert connection.closed
    assert pool.size == 0
    assert await setup.router.run(session(), Op("next")) == "next"
    assert len(driver.connections) == 2


async def test_a_connection_error_inside_a_transaction_closes_the_slot_after_its_rollback(setup):
    pool, driver = setup.pool(max_connections=1)
    with pytest.raises(OperationalError):
        async with adorm.atomic():
            await adorm.current_session().execute(Op(error=lost()))
    assert driver.connections[0].calls[-1] == ("rollback", None)
    assert driver.connections[0].closed
    assert pool.size == 0


@pytest.mark.parametrize(
    "error",
    [IntegrityError("duplicate key", sqlstate="23505"), OperationalError("canceling statement", sqlstate="57014")],
)
async def test_a_statement_error_keeps_the_slot(setup, error):
    pool, driver = setup.pool(max_connections=1)
    with pytest.raises(type(error)):
        await setup.router.run(session(), Op(error=error))
    assert (pool.size, pool.idle, driver.connections[0].closed) == (1, 1, False)


async def test_a_driver_error_detector_that_fails_counts_as_a_lost_connection(setup, caplog):
    class Failing(FakeConnection):
        def is_connection_lost(self, exc):  # noqa: ARG002
            raise RuntimeError("broken detector")

    class Driver(FakeDriver):
        async def open_connection(self, profile, emit):  # noqa: ARG002
            connection = Failing(self, emit, len(self.connections) + 1)
            self.connections.append(connection)
            return connection

    pool, driver = setup.pool(Driver(), max_connections=1)
    with pytest.raises(IntegrityError):
        await setup.router.run(session(), Op(error=IntegrityError("duplicate key", sqlstate="23505")))
    assert driver.connections[0].closed
    assert "is_connection_lost failed" in caplog.text
    assert pool.size == 0


async def test_custom_sql_closes_the_slot_after_a_clean_end(setup):
    pool, driver = setup.pool(max_connections=1)
    assert await setup.router.run(session(), Op("raw", effect=custom_sql)) == "raw"
    assert driver.connections[0].closed
    assert pool.size == 0
    assert await setup.router.run(session(), Op("next")) == "next"
    assert not driver.connections[1].ran_custom_sql


async def test_custom_sql_inside_a_transaction_commits_then_closes_the_slot(setup):
    pool, driver = setup.pool(max_connections=1)
    committed = []
    async with adorm.atomic() as tx:
        tx.on_commit(lambda: committed.append(True))
        await adorm.current_session().execute(Op("raw", effect=custom_sql))
        await adorm.current_session().execute(Op("after"))
    assert committed == [True]
    assert driver.connections[0].finished == ["begin", "commit"]
    assert driver.connections[0].closed
    assert pool.size == 0


async def test_a_slot_that_ran_custom_sql_skips_the_on_release_hook(setup):
    released = []
    profile = Profile("default", on_release=lambda *args: released.append(args))
    pool, driver = setup.pool(profile=profile, max_connections=1)
    await setup.router.run(session(), Op("raw", effect=custom_sql))
    assert released == []
    assert driver.connections[0].closed
    await setup.router.run(session(), Op("plain"))
    assert len(released) == 1
    assert pool.idle == 1


async def test_a_custom_sql_lease_keeps_its_slot_until_released(setup):
    _, driver = setup.pool(max_connections=1)
    async with await setup.router.acquire(session()) as lease:
        await lease.call("execute", Op("raw", effect=custom_sql))
        events = []
        lease.channel.on(ON_COMMIT, lambda event: events.append(event))
        await lease.call("begin")
        assert await lease.call("execute", Op("same connection")) == "same connection"
        await lease.call("commit")
        assert events == [CallbackEvent(ON_COMMIT)]
        assert not driver.connections[0].closed
    assert driver.connections[0].closed


@pytest.mark.parametrize("ending", ["discard", "custom_sql", "connection_error"])
async def test_the_close_reason_names_the_first_cause(setup, ending):
    _, driver = setup.pool(max_connections=1)
    lease = await setup.router.acquire(session())
    await lease.call("execute", Op("raw", effect=custom_sql))
    if ending == "connection_error":
        with pytest.raises(OperationalError):
            await lease.call("execute", Op(error=lost()))
    await lease.release(discard=ending == "discard")
    expected = {
        "discard": CloseReason.DISCARDED,
        "custom_sql": CloseReason.CUSTOM_SQL,
        "connection_error": CloseReason.CONNECTION_ERROR,
    }
    assert lease.slot.close_reason is expected[ending]


@pytest.mark.parametrize("attempt", REPEAT)
async def test_queued_flows_get_a_fresh_connection_after_a_custom_sql_slot_leaves(setup, attempt):  # noqa: ARG001
    pool, driver = setup.pool(max_connections=1)
    gate = asyncio.Event()
    first = asyncio.create_task(setup.router.run(session(), Op("raw", gate=gate, effect=custom_sql)))
    await until(lambda: driver.connections and driver.connections[0].running == 1)
    waiting = [asyncio.create_task(setup.router.run(session(), Op(f"w{i}"))) for i in range(5)]
    await until(lambda: pool.waiting == 5)
    gate.set()
    assert await first == "raw"
    assert await asyncio.gather(*waiting) == [f"w{i}" for i in range(5)]
    assert len(driver.connections) == 2
    assert driver.connections[0].closed
    assert not driver.connections[1].closed
    assert driver.peak == 1
    assert (pool.size, pool.idle, pool.in_use) == (1, 1, 0)


@pytest.mark.parametrize("attempt", REPEAT)
async def test_cancelling_a_flow_whose_custom_sql_completes_still_closes_the_slot(setup, attempt):  # noqa: ARG001
    pool, driver = setup.pool(max_connections=1)
    gate = asyncio.Event()
    op = Op("raw", gate=gate, effect=custom_sql, finishes_when_cancelled=True)
    task = asyncio.create_task(setup.router.run(session(), op))
    await op.started.wait()
    task.cancel()
    await asyncio.sleep(0)
    gate.set()  # the statement completes although its flow was cancelled
    with pytest.raises(asyncio.CancelledError):
        await task
    await until(lambda: pool.size == 0)
    assert driver.connections[0].ran_custom_sql
    assert driver.connections[0].closed
    assert await setup.router.run(session(), Op("next")) == "next"
    assert (pool.size, pool.in_use) == (1, 0)


@pytest.mark.parametrize("attempt", REPEAT)
async def test_concurrent_flows_never_reuse_a_closed_slot(setup, attempt):  # noqa: ARG001
    pool, driver = setup.pool(max_connections=3)

    async def flow(i):
        if i % 3 == 0:
            op = Op(f"raw{i}", effect=custom_sql)
        elif i % 3 == 1:
            op = Op(f"lost{i}", error=lost())
        else:
            op = Op(f"plain{i}")
        try:
            return await setup.router.run(session(), op)
        except OperationalError:
            return None

    await asyncio.gather(*(flow(i) for i in range(30)))
    for connection in driver.connections:
        executed = [name for method, name in connection.calls if method == "execute"]
        # A connection that ran custom SQL or lost its connection served nothing after it.
        for index, name in enumerate(executed):
            if name.startswith(("raw", "lost")):
                assert index == len(executed) - 1
                assert connection.closed
    assert pool.in_use == 0
    assert pool.size == pool.idle <= 3


# Settling


class SettlingConnection(FakeConnection):
    """A connection that still works after a request's outcome until ``settle_gate`` is set; ``after`` runs then."""

    def __init__(self, *args):
        super().__init__(*args)
        self.settle_gate: asyncio.Event | None = None
        self.after = None

    @property
    def settled(self):
        return self.settle_gate is None or self.settle_gate.is_set()

    async def settle(self):
        if self.settle_gate is not None:
            await self.settle_gate.wait()
            if self.after is not None:
                self.after(self)


class SettlingDriver(FakeDriver):
    async def open_connection(self, profile, emit):  # noqa: ARG002
        connection = SettlingConnection(self, emit, len(self.connections) + 1)
        self.connections.append(connection)
        return connection


@pytest.mark.parametrize("after", [None, custom_sql])
async def test_the_release_does_not_wait_for_the_connection_to_settle(setup, after):
    pool, driver = setup.pool(SettlingDriver(), max_connections=1)
    lease = await setup.router.acquire(session())
    connection = driver.connections[0]
    connection.settle_gate, connection.after = asyncio.Event(), after
    await lease.call("execute", Op("a"))
    async with asyncio.timeout(GUARD):
        await lease.release()
    assert pool.in_use == 1  # not handed out before the connection settled
    waiting = asyncio.create_task(setup.router.run(session(), Op("b")))
    await until(lambda: pool.waiting == 1)
    connection.settle_gate.set()
    assert await waiting == "b"
    if after is None:
        assert len(driver.connections) == 1
        assert lease.slot.close_reason is None
    else:
        assert len(driver.connections) == 2
        assert lease.slot.close_reason is CloseReason.CUSTOM_SQL


async def test_a_connection_that_does_not_settle_in_time_leaves_the_pool(setup, caplog):
    pool, driver = setup.pool(SettlingDriver(), max_connections=1, release_timeout=0.01)
    lease = await setup.router.acquire(session())
    driver.connections[0].settle_gate = asyncio.Event()
    await lease.release()
    await until(lambda: lease.slot.close_reason is not None)
    assert lease.slot.close_reason is CloseReason.UNSETTLED
    assert "did not settle" in caplog.text
    await until(lambda: pool.size == 0)


async def test_a_settle_timeout_gives_way_to_the_end_of_the_lease(setup):
    _, driver = setup.pool(SettlingDriver(), max_connections=1, release_timeout=0.01)
    lease = await setup.router.acquire(session())
    driver.connections[0].settle_gate = asyncio.Event()
    await lease.call("execute", Op("raw", effect=custom_sql))
    await lease.release()
    await until(lambda: lease.slot.close_reason is not None)
    assert lease.slot.close_reason is CloseReason.CUSTOM_SQL


@pytest.mark.parametrize("attempt", range(5))
@pytest.mark.parametrize("closing", ["pool", "router"])
async def test_closing_waits_for_a_released_slot_that_is_settling(setup, closing, attempt):  # noqa: ARG001
    pool, driver = setup.pool(SettlingDriver(), max_connections=1, release_timeout=LONG)
    lease = await setup.router.acquire(session())
    connection = driver.connections[0]
    connection.settle_gate = asyncio.Event()
    await lease.call("execute", Op("a"))
    await lease.release()
    closer = asyncio.create_task(pool.close() if closing == "pool" else setup.router.close())
    for _ in range(10):
        await asyncio.sleep(0)
    assert not closer.done()
    assert not connection.closed
    connection.settle_gate.set()
    async with asyncio.timeout(GUARD):
        await closer
    assert connection.closed
    assert lease.slot.close_reason is CloseReason.POOL_CLOSED
    assert pool.size == 0


async def test_a_failing_settle_decision_still_closes_the_slot(setup, monkeypatch):
    pool, driver = setup.pool(SettlingDriver(), max_connections=1)
    lease = await setup.router.acquire(session())
    connection = driver.connections[0]
    connection.settle_gate = asyncio.Event()
    await lease.call("execute", Op("a"))
    await lease.release()

    def broken():
        raise RuntimeError("decision failed")

    monkeypatch.setattr(lease.slot, "_end_reason", broken)
    finisher = lease.slot._finisher
    connection.settle_gate.set()
    with pytest.raises(RuntimeError, match="decision failed"):
        await finisher
    assert connection.closed
    assert lease.slot.close_reason is CloseReason.UNUSABLE
    assert pool.size == 0


# Stale idle slots


async def test_an_idle_slot_leaves_the_pool_after_purge_stale_connections_at(setup):
    pool, driver = setup.pool(purge_stale_connections_at=0.01)
    lease = await setup.router.acquire(session())
    slot = lease.slot
    await lease.release()
    assert pool.idle == 1
    await until(lambda: driver.connections[0].closed)
    assert slot.close_reason is CloseReason.STALE
    assert (pool.size, pool.idle) == (0, 0)
    assert await setup.router.run(session(), Op("next")) == "next"
    assert len(driver.connections) == 2


async def test_a_held_slot_is_never_purged(setup):
    pool, driver = setup.pool(purge_stale_connections_at=0.01)
    held = await setup.router.acquire(session())
    other = await setup.router.acquire(session())
    await other.release()
    await until(lambda: other.slot.close_reason is CloseReason.STALE)
    assert held.slot.close_reason is None
    assert not held.slot.connection.closed
    await held.release()
    assert pool.idle == 1


async def test_a_request_never_takes_a_slot_past_its_idle_age(setup):
    # The purge timer may run late on a busy loop; acquisition checks the age itself.
    pool, driver = setup.pool(purge_stale_connections_at=LONG)
    lease = await setup.router.acquire(session())
    slot = lease.slot
    await lease.release()
    slot.idle_since -= 2 * LONG
    assert await setup.router.run(session(), Op("next")) == "next"
    await until(lambda: driver.connections[0].closed)
    assert driver.connections[0].calls == []
    assert slot.close_reason is CloseReason.STALE
    assert pool.size == 1


async def test_a_request_takes_the_slot_returned_last(setup):
    pool, _ = setup.pool(purge_stale_connections_at=LONG)
    first = await setup.router.acquire(session())
    second = await setup.router.acquire(session())
    await first.release()
    await second.release()
    for _ in range(3):
        async with await setup.router.acquire(session()) as lease:
            assert lease.slot is second.slot
    assert first.slot.idle_since < second.slot.idle_since
    assert pool.idle == 2


async def test_under_a_smaller_load_the_unused_slots_age_out(setup, clock):
    pool, driver = setup.pool(purge_stale_connections_at=LONG)
    leases = [await setup.router.acquire(session()) for _ in range(3)]
    for lease in leases:
        await lease.release()
    busy = leases[-1].slot
    # One flow at a time keeps reusing the slot returned last; the other two reach their idle age.
    for _ in range(3):
        clock.advance(LONG / 3)
        assert await setup.router.run(session(), Op("tick")) == "tick"
        assert busy.close_reason is None
    await until(lambda: sum(c.closed for c in driver.connections) == 2)
    assert {lease.slot.close_reason for lease in leases[:2]} == {CloseReason.STALE}
    assert busy.close_reason is None
    assert pool.size == 1


async def test_the_purge_closes_exactly_the_slots_past_their_idle_age(setup, clock):
    pool, driver = setup.pool(purge_stale_connections_at=LONG)
    leases = [await setup.router.acquire(session()) for _ in range(3)]
    for step, lease in enumerate(leases):
        await lease.release()
        clock.advance(10 if step < 2 else 0)
    # Idle for 20, 10 and 0 seconds.
    clock.advance(LONG - 20 - 1e-6)
    slots = [lease.slot for lease in leases]
    pool._purge()
    assert list(pool._idle) == slots
    clock.advance(1e-6)
    pool._purge()
    assert list(pool._idle) == slots[1:]
    await until(lambda: slots[0].close_reason is CloseReason.STALE)
    clock.advance(10)
    await until(lambda: slots[1].close_reason is CloseReason.STALE)  # the timer
    assert list(pool._idle) == slots[2:]
    assert slots[2].close_reason is None
    await until(lambda: [c.closed for c in driver.connections] == [True, True, False])


async def test_none_keeps_idle_slots_until_the_pool_closes(setup):
    pool, driver = setup.pool(purge_stale_connections_at=None)
    lease = await setup.router.acquire(session())
    await lease.release()
    lease.slot.idle_since -= 10_000
    assert await setup.router.run(session(), Op("again")) == "again"
    assert len(driver.connections) == 1
    assert pool._purge_handle is None
    await pool.close()
    assert lease.slot.close_reason is CloseReason.POOL_CLOSED


async def test_closing_the_pool_cancels_the_purge(setup):
    pool, driver = setup.pool(purge_stale_connections_at=LONG)
    lease = await setup.router.acquire(session())
    await lease.release()
    handle = pool._purge_handle
    assert handle is not None
    await pool.close()
    assert handle.cancelled()
    assert pool._purge_handle is None
    assert lease.slot.close_reason is CloseReason.POOL_CLOSED
    assert driver.connections[0].closed


@pytest.mark.parametrize("attempt", REPEAT)
async def test_a_purge_racing_an_acquisition_never_hands_out_a_stale_slot(setup, clock, attempt):  # noqa: ARG001
    pool, driver = setup.pool(purge_stale_connections_at=LONG)
    first = await setup.router.acquire(session())
    second = await setup.router.acquire(session())
    await first.release()
    await second.release()
    stale, fresh = first.slot, second.slot
    stale.idle_since = clock.now - LONG  # exactly at its idle age
    fresh.idle_since = clock.now - LONG + 0.5
    pool._schedule_purge()  # due now, together with the requests
    used = []

    def check(connection):
        used.append((connection, connection.closed))

    results = await asyncio.gather(*(setup.router.run(session(), Op(f"op{i}", effect=check)) for i in range(3)))
    assert results == ["op0", "op1", "op2"]
    assert [closed for _, closed in used] == [False] * 3
    assert stale.connection not in [connection for connection, _ in used]
    assert stale.connection.calls == []
    assert fresh.connection in [connection for connection, _ in used]
    await until(lambda: stale.connection.closed)
    assert stale.close_reason is CloseReason.STALE
    assert fresh.close_reason is None
    assert pool.in_use == 0


# A slot closed while leased


@pytest.mark.parametrize("run", REPEAT)
async def test_a_slot_closed_inside_its_block_ends_the_block_and_frees_the_capacity(setup, run):  # noqa: ARG001
    pool, _ = setup.pool(max_connections=1)
    flow = session()

    async def block():
        async with flow.atomic() as tx:
            assert await flow.execute(Op("a")) == "a"
            await tx.lease.slot.close()

    async with asyncio.timeout(GUARD):
        with pytest.raises(adorm.InterfaceError, match="serves no requests"):
            await block()
    assert pool.size == 0
    async with asyncio.timeout(GUARD):
        assert await flow.execute(Op("b")) == "b"
    assert pool.size == 1


@pytest.mark.parametrize("run", REPEAT)
async def test_a_slot_closed_while_a_request_runs_answers_it_and_the_queued_ones(setup, run):  # noqa: ARG001
    pool, _ = setup.pool(max_connections=1)
    flow = session()
    gate = asyncio.Event()
    running = Op("running", gate=gate)
    tasks = []

    async def block():
        async with flow.atomic() as tx:
            tasks.extend([asyncio.create_task(flow.execute(running)), asyncio.create_task(flow.execute(Op("queued")))])
            await running.started.wait()
            await until(lambda: tx.lease.requests.qsize() == 1)
            await tx.lease.slot.close()
            await asyncio.gather(*tasks, return_exceptions=True)

    async with asyncio.timeout(GUARD):
        with pytest.raises(adorm.TransactionManagementError, match="is closed"):
            await block()
    for task in tasks:
        with pytest.raises(adorm.InterfaceError, match="is closed"):
            task.result()
    assert pool.size == 0
    async with asyncio.timeout(GUARD):
        assert await flow.execute(Op("next")) == "next"
