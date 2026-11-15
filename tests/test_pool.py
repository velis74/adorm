"""The pool of slots and the router: acquisition, queueing, timeouts, deadlocks and the request/event queues."""

import asyncio

import pytest

from adorm import create_session, DeadlockError, InterfaceError, OperationalError, ProgrammingError, SessionTimeout
from adorm.drivers import is_refusal
from adorm.events import BackendEvent, CallbackEvent, ErrorEvent, ResultEvent
from adorm.executor import (
    held_leases,
    holding,
    Pool,
    Request,
    REQUEST_METHODS,
    Router,
    set_runner,
    SlotLease,
)
from tests.executor_fakes import FakeDriver, marker, Op, Profile

REPEAT = range(20)
# Long enough never to expire in a passing test; a test that expected an immediate outcome fails instead of hanging.
LONG = 30
GUARD = 5


def refusal():
    return OperationalError("sorry, too many clients already", sqlstate="53300")


def make(name="default", *, max_connections=0, acquire_timeout=LONG, router=None):
    driver = FakeDriver()
    pool = Pool(driver, Profile(name), max_connections=max_connections, acquire_timeout=acquire_timeout)
    router = router or Router()
    router.add(pool)
    return router, pool, driver


def session(name="s", using="default"):
    return create_session(using, name)


async def until(predicate):
    """Yield to the loop until ``predicate()`` holds."""
    async with asyncio.timeout(GUARD):
        while not predicate():
            await asyncio.sleep(0)


# Configuration


@pytest.mark.parametrize("value", [-1, 1.5, "2", True, None])
def test_max_connections_is_a_non_negative_integer(value):
    with pytest.raises(ValueError, match="max_connections"):
        Pool(FakeDriver(), Profile("default"), max_connections=value)


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), "1", True])
def test_acquire_timeout_is_positive_and_finite(value):
    with pytest.raises(ValueError, match="acquire_timeout"):
        Pool(FakeDriver(), Profile("default"), acquire_timeout=value)


def test_router_routes_one_pool_per_profile():
    router, pool, _ = make()
    assert router.pool("default") is pool
    assert router.pools == (pool,)
    with pytest.raises(ValueError, match="already routed"):
        router.add(Pool(FakeDriver(), Profile("default")))
    with pytest.raises(ValueError, match="already belongs"):
        Router().add(pool)
    with pytest.raises(TypeError):
        router.add("default")
    with pytest.raises(InterfaceError, match="'reports'"):
        router.pool("reports")


async def test_acquire_for_an_unknown_profile_raises():
    router, _, _ = make()
    with pytest.raises(InterfaceError, match="no pool"):
        await router.acquire(session(using="reports"))


async def test_acquire_timeout_argument_is_validated():
    router, _, _ = make()
    with pytest.raises(ValueError, match="timeout"):
        await router.acquire(session(), timeout=0)


def test_request_validates_the_method():
    assert Request("execute", [1]).args == (1,)
    assert "commit" in REQUEST_METHODS
    with pytest.raises(ValueError, match="unknown request method"):
        Request("drop_everything")
    assert Request("begin").request_id != Request("begin").request_id


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (OperationalError(sqlstate="53300"), True),
        (OperationalError(sqlstate="53200"), True),
        (OperationalError(sqlstate="08006"), False),
        (OperationalError(), False),
        (ProgrammingError(sqlstate="53300"), False),
    ],
)
def test_is_refusal(exc, expected):
    assert is_refusal(exc) is expected


# Acquisition and release


async def test_slots_open_on_demand_and_are_reused():
    router, pool, driver = make()
    assert pool.size == 0
    assert driver.connections == []
    lease = await router.acquire(session())
    assert isinstance(lease, SlotLease)
    assert (pool.size, pool.in_use, pool.idle) == (1, 1, 0)
    assert lease.session is session()
    assert lease.pool is pool
    assert lease.owner is asyncio.current_task()
    await lease.release()
    assert (pool.size, pool.in_use, pool.idle) == (1, 0, 1)
    again = await router.acquire(session("other"))
    assert again.slot is lease.slot
    assert len(driver.connections) == 1
    await again.release()


async def test_release_twice_does_nothing_and_a_released_lease_takes_no_requests():
    router, pool, _ = make()
    lease = await router.acquire(session())
    await lease.release()
    await lease.release()
    assert lease.released
    assert pool.idle == 1
    with pytest.raises(InterfaceError, match="cannot take requests"):
        lease.requests.put_nowait(Request("begin"))
    with pytest.raises(InterfaceError):
        await lease.call("execute", Op())


async def test_lease_is_an_async_context_manager():
    router, pool, _ = make()
    async with await router.acquire(session()) as lease:
        assert await lease.call("execute", Op("x")) == "x"
    assert lease.released
    assert pool.idle == 1


async def test_router_slot_records_the_lease_as_held_for_the_block():
    router, pool, _ = make()
    assert held_leases() == ()
    async with router.slot(session()) as lease:
        assert held_leases() == (lease,)
        assert router.held_sessions() == (session(),)
    assert held_leases() == ()
    assert pool.idle == 1


async def test_holding_needs_a_lease():
    with pytest.raises(TypeError):
        with holding("lease"):
            pass


# Request and event queues


async def test_requests_run_in_order_and_events_carry_their_ids():
    router, _, driver = make()
    async with await router.acquire(session()) as lease:
        requests = [Request("begin"), Request("execute", (Op("a"),)), Request("execute", (Op("b"),)), Request("commit")]
        for request in requests:
            await lease.requests.put(request)
        events = [await lease.events.get() for _ in requests]
    assert [e.request_id for e in events] == [r.request_id for r in requests]
    assert [e.value for e in events] == [None, "a", "b", None]
    assert driver.connections[0].calls == [("begin", None), ("execute", "a"), ("execute", "b"), ("commit", None)]


async def test_a_failing_request_yields_an_error_event_and_the_next_one_still_runs():
    router, _, _ = make()
    error = ProgrammingError("syntax error")
    async with await router.acquire(session()) as lease:
        failing, after = Request("execute", (Op(error=error),)), Request("execute", (Op("after"),))
        lease.requests.put_nowait(failing)
        lease.requests.put_nowait(after)
        await lease.requests.join()
        assert lease.events.get_nowait() == ErrorEvent(failing.request_id, error)
        assert lease.events.get_nowait() == ResultEvent(after.request_id, "after")


async def test_call_returns_the_result_or_raises_the_error():
    router, _, _ = make()
    async with await router.acquire(session()) as lease:
        assert await lease.call("execute", Op(result=[{"id": 1}])) == [{"id": 1}]
        with pytest.raises(ProgrammingError, match="bad"):
            await lease.call("execute", Op(error=ProgrammingError("bad")))
        assert lease.events.empty()


async def test_unsupported_operation_is_an_error_of_the_request():
    router, _, _ = make()
    async with await router.acquire(session()) as lease:
        with pytest.raises(TypeError, match="takes Request"):
            lease.requests.put_nowait("begin")
        with pytest.raises(Exception, match="unsupported"):
            await lease.call("set_options", {"statement_timeout": 1}, False)


async def test_emitted_events_go_to_the_lease_while_held_and_to_the_pool_bus_otherwise():
    router, pool, driver = make()
    seen = []
    pool.events.subscribe(BackendEvent, seen.append)

    def notify(connection):
        connection.emit(BackendEvent("notify", "orders", "1"))

    lease = await router.acquire(session())
    result = Request("execute", (Op("x", effect=notify),))
    lease.requests.put_nowait(result)
    await lease.requests.join()
    assert lease.events.get_nowait() == BackendEvent("notify", "orders", "1")
    assert lease.events.get_nowait() == ResultEvent(result.request_id, "x")
    await lease.release()
    driver.connections[0].emit(BackendEvent("notify", "orders", "2"))
    assert seen == [BackendEvent("notify", "orders", "2")]


async def test_unread_callbacks_and_backend_events_go_to_the_pool_bus_on_release():
    router, pool, _ = make()
    seen = []
    pool.events.subscribe(CallbackEvent, seen.append)
    lease = await router.acquire(session())

    def callback(connection):
        connection.emit(CallbackEvent("on_commit", 7))

    await lease.call("execute", Op(effect=callback))
    lease.requests.put_nowait(Request("begin"))  # its unread ResultEvent is dropped
    await lease.release()
    assert seen == [CallbackEvent("on_commit", 7)]


async def test_requests_run_in_the_context_of_the_sender():
    router, _, driver = make()
    async with await router.acquire(session()) as lease:
        token = marker.set("first")
        try:
            await lease.call("execute", Op())
        finally:
            marker.reset(token)
        await lease.call("execute", Op())
    assert driver.connections[0].markers == ["first", None]


async def test_release_waits_for_queued_requests():
    router, pool, driver = make()
    lease = await router.acquire(session())
    gate = asyncio.Event()
    lease.requests.put_nowait(Request("execute", (Op(gate=gate),)))
    lease.requests.put_nowait(Request("commit"))
    release = asyncio.create_task(lease.release())
    await until(lambda: driver.connections[0].running == 1)
    assert not release.done()
    assert pool.idle == 0
    gate.set()
    await release
    assert driver.connections[0].calls[-1] == ("commit", None)
    assert pool.idle == 1


@pytest.mark.parametrize("attempt", REPEAT)
async def test_cancelling_a_call_cancels_the_running_request_and_keeps_the_slot(attempt):  # noqa: ARG001
    router, pool, driver = make()
    lease = await router.acquire(session())
    op = Op(gate=asyncio.Event())
    call = asyncio.create_task(lease.call("execute", op))
    await op.started.wait()
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    await lease.release()
    assert driver.connections[0].cancelled == [op]
    assert lease.events.empty()
    assert pool.idle == 1
    assert pool.size == 1


async def test_cancelling_a_queued_call_means_it_never_runs():
    router, _, driver = make()
    async with await router.acquire(session()) as lease:
        first = Op("first", gate=asyncio.Event())
        running = asyncio.create_task(lease.call("execute", first))
        queued = asyncio.create_task(lease.call("execute", Op("queued")))
        await first.started.wait()
        await until(lambda: lease.requests.qsize() == 1)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        first.gate.set()
        assert await running == "first"
    assert driver.connections[0].calls == [("execute", "first")]


@pytest.mark.parametrize("attempt", REPEAT)
async def test_release_completes_when_the_releasing_task_is_cancelled(attempt):  # noqa: ARG001
    router, pool, driver = make()
    lease = await router.acquire(session())
    gate = asyncio.Event()
    lease.requests.put_nowait(Request("execute", (Op(gate=gate),)))
    release = asyncio.create_task(lease.release())
    await until(lambda: driver.connections[0].running == 1)
    release.cancel()
    with pytest.raises(asyncio.CancelledError):
        await release
    gate.set()
    await until(lambda: pool.idle == 1)
    assert pool.in_use == 0


async def test_a_connection_that_became_unusable_leaves_the_pool_on_release():
    router, pool, driver = make()
    lease = await router.acquire(session())

    def lose(connection):
        connection.usable = False

    with pytest.raises(OperationalError):
        await lease.call("execute", Op(error=OperationalError("server closed the connection"), effect=lose))
    await lease.release()
    assert driver.connections[0].closed
    assert pool.size == 0
    again = await router.acquire(session())
    assert again.slot.connection is driver.connections[1]
    await again.release()


async def test_release_with_discard_closes_the_connection():
    router, pool, driver = make()
    lease = await router.acquire(session())
    await lease.release(discard=True)
    assert driver.connections[0].closed
    assert pool.size == 0


async def test_an_idle_slot_that_became_unusable_is_not_handed_out():
    router, pool, driver = make()
    lease = await router.acquire(session())
    await lease.release()
    driver.connections[0].usable = False
    again = await router.acquire(session())
    assert again.slot.connection is driver.connections[1]
    await until(lambda: driver.connections[0].closed)
    assert pool.size == 1
    await again.release()


# Queueing


async def test_requests_wait_for_a_slot_in_arrival_order():
    router, pool, _ = make(max_connections=1)
    first = await router.acquire(session("first"))
    order = []

    async def wait(name):
        async with router.slot(session(name)):
            order.append(name)

    waiters = [asyncio.create_task(wait(f"w{i}")) for i in range(5)]
    await until(lambda: pool.waiting == 5)
    await first.release()
    await asyncio.gather(*waiters)
    assert order == [f"w{i}" for i in range(5)]
    assert (pool.size, pool.idle, pool.waiting) == (1, 1, 0)


@pytest.mark.parametrize("attempt", range(5))
async def test_many_tasks_share_a_bounded_pool_without_leaking_slots(attempt):  # noqa: ARG001
    router, pool, driver = make(max_connections=3)
    results = await asyncio.gather(*(router.run(session(f"t{i}"), Op(f"r{i}")) for i in range(60)))
    assert results == [f"r{i}" for i in range(60)]
    assert driver.peak <= 3
    assert len(driver.connections) == 3
    assert (pool.size, pool.idle, pool.in_use, pool.waiting) == (3, 3, 0, 0)


# Timeouts


async def test_acquisition_times_out_with_session_timeout():
    router, pool, _ = make(max_connections=1, acquire_timeout=0.05)
    held = asyncio.Event()
    done = asyncio.Event()

    async def holder():
        async with router.slot(session("holder")):
            held.set()
            await done.wait()

    task = asyncio.create_task(holder())
    await held.wait()
    with pytest.raises(SessionTimeout) as info:
        await router.acquire(session("late"))
    assert type(info.value) is SessionTimeout
    assert info.value.session is session("late")
    assert info.value.timeout == 0.05
    assert pool.waiting == 0
    done.set()
    await task
    assert pool.idle == 1


async def test_per_call_timeout_overrides_the_pool_default():
    router, _, _ = make(max_connections=1)
    holder = await asyncio.create_task(router.acquire(session("holder")))
    with pytest.raises(SessionTimeout) as info:
        async with asyncio.timeout(GUARD):
            await router.acquire(session("x"), timeout=0.05)
    assert type(info.value) is SessionTimeout
    assert info.value.timeout == 0.05
    await holder.release()


async def test_timeout_while_holding_a_slot_of_another_pool_is_a_deadlock_error():
    router, p1, _ = make("one", max_connections=1, acquire_timeout=0.05)
    make("two", router=router)
    held, done = asyncio.Event(), asyncio.Event()

    async def holder():
        async with router.slot(session("h", "one")):
            held.set()
            await done.wait()

    task = asyncio.create_task(holder())
    await held.wait()
    async with router.slot(session("mine", "two")):
        with pytest.raises(DeadlockError) as info:
            await router.acquire(session("mine", "one"))
    assert info.value.held_sessions == (session("mine", "two"),)
    assert "while holding" in str(info.value)
    assert p1.waiting == 0
    done.set()
    await task


async def test_a_child_task_inherits_the_held_slot_of_its_parent():
    router, _, _ = make("one", max_connections=1, acquire_timeout=0.05)
    make("two", router=router)
    held, done = asyncio.Event(), asyncio.Event()

    async def holder():
        async with router.slot(session("h", "one")):
            held.set()
            await done.wait()

    other = asyncio.create_task(holder())
    await held.wait()
    async with router.slot(session("parent", "two")):
        child = asyncio.create_task(router.acquire(session("child", "one")))
        with pytest.raises(DeadlockError) as info:
            await child
    assert info.value.held_sessions == (session("parent", "two"),)
    done.set()
    await other


async def test_a_slow_connection_open_times_out_and_frees_its_capacity():
    router, pool, driver = make(max_connections=1, acquire_timeout=0.05)
    driver.open_gate = asyncio.Event()
    with pytest.raises(SessionTimeout):
        await router.acquire(session())
    assert pool.size == 0
    driver.open_gate.set()
    lease = await router.acquire(session())
    await lease.release()


async def test_an_outer_asyncio_timeout_removes_the_waiter():
    router, pool, _ = make(max_connections=1)
    lease = await router.acquire(session("holder"))

    async def late():
        async with asyncio.timeout(0.05):
            await router.acquire(session("late"))

    with pytest.raises(TimeoutError) as info:
        await asyncio.create_task(late())
    assert not isinstance(info.value, SessionTimeout)
    assert pool.waiting == 0
    await lease.release()
    assert pool.idle == 1


@pytest.mark.parametrize("attempt", REPEAT)
async def test_a_slot_handed_to_a_waiter_that_is_cancelled_meanwhile_is_not_lost(attempt):  # noqa: ARG001
    router, pool, _ = make(max_connections=1)
    lease = await router.acquire(session("holder"))
    waiter = asyncio.create_task(router.acquire(session("waiter")))
    await until(lambda: pool.waiting == 1)
    returned = pool._returned

    def hand_over_then_cancel(slot):
        # The waiter is cancelled after the slot became its result and before it resumes.
        pool._returned = returned
        returned(slot)
        waiter.cancel()

    pool._returned = hand_over_then_cancel
    await lease.release()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert (pool.idle, pool.in_use) == (1, 0)
    again = await router.acquire(session("again"), timeout=GUARD)
    await again.release()


@pytest.mark.parametrize("attempt", REPEAT)
async def test_cancelling_a_waiter_frees_its_place(attempt):  # noqa: ARG001
    router, pool, _ = make(max_connections=1)
    lease = await router.acquire(session("holder"))
    first = asyncio.create_task(router.acquire(session("first")))
    second = asyncio.create_task(router.acquire(session("second")))
    await until(lambda: pool.waiting == 2)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await lease.release()
    got = await second
    assert got.session is session("second")
    await got.release()


# Deadlocks raised at once


async def test_waiting_for_a_slot_of_a_pool_whose_only_slot_the_flow_holds_raises_at_once():
    router, pool, _ = make(max_connections=1)
    async with router.slot(session("outer")):
        with pytest.raises(DeadlockError) as info:
            async with asyncio.timeout(GUARD):
                await router.acquire(session("inner"))
    assert info.value.held_sessions == (session("outer"),)
    assert info.value.session is session("inner")
    assert info.value.timeout == LONG
    assert "deadlock" in str(info.value)
    assert (pool.waiting, pool.idle) == (0, 1)


@pytest.mark.parametrize("attempt", REPEAT)
async def test_two_flows_waiting_for_each_others_slots_get_a_deadlock_error_at_once(attempt):  # noqa: ARG001
    router, _, _ = make("one", max_connections=1)
    make("two", max_connections=1, router=router)
    both_hold = asyncio.Barrier(2)

    async def flow(mine, theirs):
        async with router.slot(session("f", mine)):
            await both_hold.wait()
            async with router.slot(session("f", theirs)):
                return "done"

    async with asyncio.timeout(GUARD):
        a = asyncio.create_task(flow("one", "two"))
        b = asyncio.create_task(flow("two", "one"))
        results = await asyncio.gather(a, b, return_exceptions=True)
    errors = [r for r in results if isinstance(r, DeadlockError)]
    assert len(errors) == 1
    assert results.count("done") == 1
    assert len(errors[0].held_sessions) == 1


async def test_a_cycle_of_three_flows_is_detected():
    router = Router()
    for name in ("a", "b", "c"):
        make(name, max_connections=1, router=router)
    all_hold = asyncio.Barrier(3)

    async def flow(mine, theirs):
        async with router.slot(session("f", mine)):
            await all_hold.wait()
            async with router.slot(session("f", theirs)):
                return "done"

    async with asyncio.timeout(GUARD):
        results = await asyncio.gather(flow("a", "b"), flow("b", "c"), flow("c", "a"), return_exceptions=True)
    assert sum(isinstance(r, DeadlockError) for r in results) == 1
    assert results.count("done") == 2


async def test_no_immediate_deadlock_while_the_holder_is_still_running():
    router, _, _ = make("one", max_connections=1)
    make("two", router=router)
    held, done = asyncio.Event(), asyncio.Event()

    async def holder():
        async with router.slot(session("h", "one")):
            held.set()
            await done.wait()

    task = asyncio.create_task(holder())
    await held.wait()
    async with router.slot(session("mine", "two")):
        waiting = asyncio.create_task(router.acquire(session("mine", "one")))
        await until(lambda: router.pool("one").waiting == 1)
        assert not waiting.done()
        done.set()
        lease = await waiting
        await lease.release()
    await task


async def test_no_immediate_deadlock_while_the_pool_can_still_open_a_slot():
    router, pool, _ = make()
    async with router.slot(session("outer")):
        async with router.slot(session("inner")) as inner:
            assert pool.size == 2
            assert inner.session is session("inner")


async def test_no_immediate_deadlock_while_a_slot_is_opening():
    router, pool, driver = make(max_connections=2)
    async with router.slot(session("outer")):
        driver.open_gate = asyncio.Event()
        opening = asyncio.create_task(router.acquire(session("opening")))
        await until(lambda: pool._opening == 1)
        waiting = asyncio.create_task(router.acquire(session("third")))
        await until(lambda: pool.waiting == 1)
        assert not waiting.done()
        driver.open_gate.set()
        second = await opening
        await second.release()
        third = await waiting
        await third.release()


# Opening connections


async def test_with_max_connections_zero_a_refusal_caps_the_pool_and_requests_wait():
    router, pool, driver = make(max_connections=0)
    first = await router.acquire(session("1"))
    second = await router.acquire(session("2"))
    driver.open_errors = [refusal()]
    third = asyncio.create_task(router.acquire(session("3")))
    await until(lambda: pool.waiting == 1)
    assert pool.limit == 2
    await first.release()
    lease = await third
    assert lease.slot is first.slot
    assert len(driver.connections) == 2
    await lease.release()
    await second.release()


async def test_a_refusal_with_no_slot_to_wait_for_raises():
    router, pool, driver = make(max_connections=0)
    driver.open_errors = [refusal()]
    with pytest.raises(OperationalError) as info:
        await router.acquire(session())
    assert info.value.sqlstate == "53300"
    assert pool.size == 0


async def test_a_refusal_cap_is_lifted_when_a_slot_leaves_the_pool():
    router, pool, driver = make(max_connections=0)
    first = await router.acquire(session("1"))
    driver.open_errors = [refusal()]
    waiting = asyncio.create_task(router.acquire(session("2")))
    await until(lambda: pool.waiting == 1)
    assert pool.limit == 1
    await first.release(discard=True)
    lease = await waiting
    assert lease.slot.connection is driver.connections[1]
    assert pool.limit is None
    await lease.release()


async def test_a_refusal_with_max_connections_zero_is_a_certain_deadlock_for_the_holder():
    router, pool, driver = make(max_connections=0)
    async with router.slot(session("outer")):
        driver.open_errors = [refusal()]
        with pytest.raises(DeadlockError):
            async with asyncio.timeout(GUARD):
                await router.acquire(session("inner"))
    assert pool.idle == 1


async def test_an_open_error_propagates_and_frees_the_capacity():
    router, pool, driver = make(max_connections=1)
    driver.open_errors = [OperationalError("connection refused")]
    with pytest.raises(OperationalError, match="connection refused"):
        await router.acquire(session())
    assert pool.size == 0
    lease = await router.acquire(session())
    await lease.release()


async def test_a_waiter_opens_a_connection_when_a_slot_leaves_the_pool():
    router, pool, driver = make(max_connections=1)
    lease = await router.acquire(session("holder"))
    waiting = asyncio.create_task(router.acquire(session("waiter")))
    await until(lambda: pool.waiting == 1)
    await lease.release(discard=True)
    got = await waiting
    assert got.slot.connection is driver.connections[1]
    await got.release()


async def test_a_waiter_that_fails_to_open_raises_and_passes_capacity_on():
    router, pool, driver = make(max_connections=1)
    lease = await router.acquire(session("holder"))
    first = asyncio.create_task(router.acquire(session("first")))
    second = asyncio.create_task(router.acquire(session("second")))
    await until(lambda: pool.waiting == 2)
    driver.open_errors = [OperationalError("connection refused")]
    await lease.release(discard=True)
    with pytest.raises(OperationalError):
        await first
    got = await second
    await got.release()
    assert pool.size == 1


# Runner and closing


async def test_router_runs_session_operations_as_the_runner():
    router, pool, _ = make()
    previous = set_runner(router)
    try:
        assert await session().execute(Op("ran")) == "ran"
    finally:
        set_runner(previous)
    assert pool.idle == 1


async def test_run_releases_the_slot_after_an_error_and_after_cancellation():
    router, pool, _ = make(max_connections=1)
    with pytest.raises(ProgrammingError):
        await router.run(session(), Op(error=ProgrammingError("bad")))
    op = Op(gate=asyncio.Event())
    task = asyncio.create_task(router.run(session(), op))
    await op.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (pool.idle, pool.in_use) == (1, 0)


async def test_close_fails_waiters_closes_idle_slots_and_held_ones_on_release():
    router, pool, driver = make(max_connections=1)
    idle_router, idle_pool, idle_driver = make()
    await (await idle_router.acquire(session())).release()
    lease = await router.acquire(session("holder"))
    waiting = asyncio.create_task(router.acquire(session("waiter")))
    await until(lambda: pool.waiting == 1)
    await asyncio.gather(router.close(), idle_router.close())
    with pytest.raises(InterfaceError, match="closed"):
        await waiting
    assert idle_driver.connections[0].closed
    assert idle_pool.size == 0
    assert not driver.connections[0].closed
    await lease.release()
    assert driver.connections[0].closed
    assert pool.size == 0
    with pytest.raises(InterfaceError, match="closed"):
        await router.acquire(session())
    await router.close()


async def test_the_driver_decides_what_is_a_refusal():
    router, pool, driver = make(max_connections=0)
    driver.is_refusal = lambda exc: "too many" in str(exc)
    first = await router.acquire(session("1"))
    driver.open_errors = [OperationalError("too many connections for role")]
    waiting = asyncio.create_task(router.acquire(session("2")))
    await until(lambda: pool.waiting == 1)
    await first.release()
    await (await waiting).release()


async def test_a_failing_refusal_check_counts_as_no_refusal(caplog):
    router, pool, driver = make(max_connections=0)

    def broken(exc):  # noqa: ARG001
        raise RuntimeError("bug")

    driver.is_refusal = broken
    first = await router.acquire(session("1"))
    driver.open_errors = [refusal()]
    with pytest.raises(OperationalError):
        await router.acquire(session("2"))
    assert "is_refusal failed" in caplog.text
    assert pool.limit is None
    await first.release()


async def test_a_request_with_wrong_arguments_fails_alone():
    router, pool, _ = make()
    async with await router.acquire(session()) as lease:
        with pytest.raises(TypeError):
            await lease.call("commit", "unexpected")
        assert await lease.call("execute", Op("still served")) == "still served"
    assert pool.idle == 1


# Transactions and failures on a slot


async def test_a_lease_released_inside_a_transaction_discards_the_slot():
    router, pool, driver = make()
    lease = await router.acquire(session())
    await lease.call("begin")
    await lease.release()
    assert driver.connections[0].closed
    assert pool.size == 0
    again = await router.acquire(session())
    assert again.slot.connection is driver.connections[1]
    assert driver.connections[1].calls == []
    await again.release()


@pytest.mark.parametrize("end", ["commit", "rollback"])
async def test_a_lease_that_ends_its_transaction_returns_the_slot(end):
    router, pool, driver = make()
    async with await router.acquire(session()) as lease:
        await lease.call("begin")
        await lease.call(end)
    assert not driver.connections[0].closed
    assert pool.idle == 1


async def test_a_cancelled_request_inside_a_transaction_discards_the_slot():
    router, pool, driver = make()
    lease = await router.acquire(session())
    await lease.call("begin")
    op = Op(gate=asyncio.Event())
    call = asyncio.create_task(lease.call("execute", op))
    await op.started.wait()
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    await lease.release()
    assert lease._calls == {}
    assert driver.connections[0].closed
    assert pool.size == 0


async def test_a_driver_method_that_returns_no_coroutine_fails_its_request():
    router, pool, driver = make()
    async with await router.acquire(session()) as lease:
        lease.slot.connection.set_options = lambda options, local: None  # noqa: ARG005
        with pytest.raises(InterfaceError, match="did not return a coroutine"):
            async with asyncio.timeout(GUARD):
                await lease.call("set_options", {}, False)
        assert await lease.call("execute", Op("still served")) == "still served"
    assert pool.idle == 1
    assert not driver.connections[0].closed


async def test_a_failing_slot_fails_its_queued_requests_and_leaves_the_pool(monkeypatch):
    router, pool, driver = make()
    lease = await router.acquire(session())

    async def broken(*args):  # noqa: ARG001
        raise RuntimeError("bug")

    monkeypatch.setattr(lease.slot, "_run", broken)
    first, second = Request("execute", (Op("first"),)), Request("execute", (Op("second"),))
    lease.requests.put_nowait(first)
    lease.requests.put_nowait(second)
    async with asyncio.timeout(GUARD):
        events = [await lease.events.get(), await lease.events.get()]
        await lease.release()
    assert [event.request_id for event in events] == [first.request_id, second.request_id]
    assert all(isinstance(event, ErrorEvent) and isinstance(event.error, InterfaceError) for event in events)
    assert driver.connections[0].closed
    assert pool.size == 0


async def test_a_request_the_driver_cancels_itself_has_an_error_outcome():
    router, pool, _ = make()
    async with await router.acquire(session()) as lease:
        with pytest.raises(InterfaceError, match="cancelled by the driver"):
            async with asyncio.timeout(GUARD):
                await lease.call("execute", Op(error=asyncio.CancelledError()))
        request = Request("execute", (Op(error=asyncio.CancelledError()),))
        lease.requests.put_nowait(request)
        async with asyncio.timeout(GUARD):
            event = await lease.events.get()
        assert isinstance(event, ErrorEvent)
        assert event.request_id == request.request_id
        assert isinstance(event.error, InterfaceError)
    assert pool.idle == 1


# Refusals below the configured limit, closing and queue position


async def test_a_refusal_below_max_connections_caps_the_pool_and_the_request_waits():
    router, pool, driver = make(max_connections=5)
    held = await router.acquire(session("held"))
    driver.open_errors = [refusal() for _ in range(100)]
    with pytest.raises(SessionTimeout) as info:
        await asyncio.create_task(router.acquire(session("waiter"), timeout=0.2))
    assert not isinstance(info.value, DeadlockError)
    assert driver.attempts == 2
    assert pool.limit == 1
    waiting = asyncio.create_task(router.acquire(session("waiter")))
    await until(lambda: pool.waiting == 1)
    await held.release()
    lease = await waiting
    assert lease.slot is held.slot
    assert driver.attempts == 2
    await lease.release()


async def test_a_refused_opener_keeps_its_place_in_the_queue():
    router, pool, driver = make(max_connections=2)
    held = await router.acquire(session("held"))
    driver.open_gate = asyncio.Event()
    first = asyncio.create_task(router.acquire(session("first")))
    await until(lambda: pool._opening == 1)
    second = asyncio.create_task(router.acquire(session("second")))
    await until(lambda: pool.waiting == 1)
    driver.open_errors = [refusal()]
    driver.open_gate.set()
    await until(lambda: pool.waiting == 2)
    await held.release()
    lease = await first
    assert lease.session is session("first")
    assert not second.done()
    await lease.release()
    await (await second).release()


async def test_close_waits_for_slots_being_discarded():
    router, pool, driver = make()
    await (await router.acquire(session())).release()
    stale = driver.connections[0]
    stale.usable = False
    stale.close_gate = asyncio.Event()
    lease = await router.acquire(session())
    await lease.release()
    closing = asyncio.create_task(router.close())
    await until(lambda: driver.connections[1].closed)
    assert not closing.done()
    stale.close_gate.set()
    await closing
    assert stale.closed


async def test_a_slot_that_finishes_opening_after_close_is_closed():
    router, pool, driver = make()
    driver.open_gate = asyncio.Event()
    opening = asyncio.create_task(router.acquire(session()))
    await until(lambda: pool._opening == 1)
    await router.close()
    driver.open_gate.set()
    with pytest.raises(InterfaceError, match="closed"):
        await opening
    assert driver.connections[0].closed
    assert pool.size == 0


# Deadlock victims


@pytest.mark.parametrize("attempt", range(5))
async def test_the_deadlock_victim_is_a_holder_not_a_waiter_without_slots(attempt):  # noqa: ARG001
    router, one, one_driver = make("one", max_connections=0)
    make("two", max_connections=1, router=router)
    a_holds, b_holds, release_b = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def flow_a():
        async with router.slot(session("a", "one")):
            a_holds.set()
            await release_b.wait()
            async with router.slot(session("a", "two")):
                return "done"

    async def flow_b():
        async with router.slot(session("b", "two")):
            b_holds.set()
            await release_b.wait()
            async with router.slot(session("b", "one")):
                return "done"

    async with asyncio.timeout(GUARD):
        a = asyncio.create_task(flow_a())
        await a_holds.wait()
        b = asyncio.create_task(flow_b())
        await b_holds.wait()
        one_driver.open_gate = asyncio.Event()
        release_b.set()
        await until(lambda: one._opening == 1 and router.pool("two").waiting == 1)
        x = asyncio.create_task(router.acquire(session("x", "one")))
        await until(lambda: one._opening == 2)
        one_driver.open_errors = [refusal(), refusal()]
        one_driver.open_gate.set()
        results = await asyncio.gather(a, b, return_exceptions=True)
        lease = await x
    assert results[0] == "done"
    assert isinstance(results[1], DeadlockError)
    assert results[1].held_sessions == (session("b", "two"),)
    assert results[1].session is session("b", "one")
    await lease.release()


async def test_a_lease_another_task_releases_is_no_immediate_deadlock():
    router, pool, _ = make(max_connections=1)
    lease = await router.acquire(session("handed"))

    async def helper():
        await asyncio.sleep(0)
        await lease.release()

    task = asyncio.create_task(helper())
    async with asyncio.timeout(GUARD):
        again = await router.acquire(session("again"), timeout=LONG)
    assert again.slot is lease.slot
    await again.release()
    await task
