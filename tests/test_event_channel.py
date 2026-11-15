"""The event channel from a slot to the client: outcomes, cancellation, callbacks, on_commit and encoded events."""

import asyncio
import contextvars
import itertools
import json
import logging

import pytest

import adorm

from adorm import (
    create_session,
    IntegrityError,
    InterfaceError,
    OperationalError,
    TransactionClosed,
    TransactionManagementError,
)
from adorm.drivers import Capabilities, Driver, DriverConnection
from adorm.events import (
    BackendEvent,
    CallbackEvent,
    CancelledEvent,
    ErrorEvent,
    Event,
    ON_COMMIT,
    ON_ROLLBACK,
    OUTCOME_EVENTS,
    RESERVED_CALLBACKS,
    ResultEvent,
)
from adorm.executor import active_transaction, bind_transaction, Pool, Request, Router, set_runner, Slot
from adorm.executor.slot import cancelled_before_start
from tests.executor_fakes import FakeDriver, Op, Profile

REPEAT = range(20)
LONG = 30
GUARD = 5
_names = itertools.count()
flavour: contextvars.ContextVar[str | None] = contextvars.ContextVar("event_channel_flavour", default=None)


def unique_session():
    return create_session("default", f"ev-{next(_names)}")


async def until(predicate):
    """Yield to the loop until ``predicate()`` holds."""
    async with asyncio.timeout(GUARD):
        while not predicate():
            await asyncio.sleep(0)


class Setup:
    def __init__(self):
        self.router = Router()
        self.pool = Pool(FakeDriver(), Profile("default"), acquire_timeout=LONG, commit_timeout=None)
        self.driver = self.pool.driver
        self.router.add(self.pool)

    def calls(self, number=0):
        return self.driver.connections[number].calls


@pytest.fixture
async def setup():
    built = Setup()
    previous = set_runner(built.router)
    try:
        yield built
    finally:
        set_runner(previous)
        await built.router.close()


@pytest.fixture(params=["in-process", "encoded"])
def path(request, monkeypatch):
    """Runs a test twice: events reach the client channel directly, and after a round trip through JSON."""
    if request.param == "encoded":

        def deliver(self, lease, event):  # noqa: ARG001
            lease.channel.deliver(Event.from_dict(json.loads(json.dumps(event.to_dict()))))

        monkeypatch.setattr(Slot, "_deliver", deliver)
    return request.param


def outcomes_of(events):
    return [e for e in events if isinstance(e, (ResultEvent, ErrorEvent, CancelledEvent))]


def drain(queue):
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


# Outcomes and cancellation on the request queue


async def test_every_request_has_exactly_one_outcome_in_order(setup, path):  # noqa: ARG001
    async with await setup.router.acquire(unique_session()) as lease:
        requests = [
            Request("execute", (Op("a"),)),
            Request("execute", (Op(error=IntegrityError("dup", sqlstate="23505")),)),
            Request("execute", (Op(result=[{"id": 1, "name": "Ana"}]),)),
        ]
        for request in requests:
            lease.requests.put_nowait(request)
        await lease.requests.join()
        events = drain(lease.events)
    assert [e.request_id for e in events] == [r.request_id for r in requests]
    assert events[0] == ResultEvent(requests[0].request_id, "a")
    assert type(events[1].error) is IntegrityError
    assert events[1].error.sqlstate == "23505"
    assert events[2] == ResultEvent(requests[2].request_id, [{"id": 1, "name": "Ana"}])


async def test_cancelling_a_queued_request_yields_a_cancelled_event_and_it_never_runs(setup, path):  # noqa: ARG001
    """The CancelledEvent of a queued request arrives at once, ahead of the outcome of the request before it."""
    async with await setup.router.acquire(unique_session()) as lease:
        first = Op("first", gate=asyncio.Event())
        running, queued = Request("execute", (first,)), Request("execute", (Op("queued"),))
        lease.requests.put_nowait(running)
        lease.requests.put_nowait(queued)
        await first.started.wait()
        assert lease.requests.cancel(queued) is True
        assert lease.events.get_nowait() == CancelledEvent(queued.request_id, started=False)
        first.gate.set()
        await lease.requests.join()
        assert lease.events.get_nowait() == ResultEvent(running.request_id, "first")
    assert setup.calls() == [("execute", "first")]


async def test_cancelling_a_running_request_cancels_it_in_the_connection(setup, path):  # noqa: ARG001
    async with await setup.router.acquire(unique_session()) as lease:
        op = Op(gate=asyncio.Event())
        request = Request("execute", (op,))
        lease.requests.put_nowait(request)
        await op.started.wait()
        assert lease.requests.cancel(request) is False
        await lease.requests.join()
        assert drain(lease.events) == [CancelledEvent(request.request_id, started=True)]
        assert await lease.call("execute", Op("next")) == "next"
    assert setup.driver.connections[0].cancelled == [op]
    assert setup.pool.idle == 1


async def test_cancelling_a_finished_request_does_nothing(setup):
    async with await setup.router.acquire(unique_session()) as lease:
        request = Request("execute", (Op("done"),))
        lease.requests.put_nowait(request)
        await lease.requests.join()
        assert lease.requests.cancel(request) is False
        assert drain(lease.events) == [ResultEvent(request.request_id, "done")]


@pytest.mark.parametrize("attempt", REPEAT)
async def test_cancel_racing_completion_yields_exactly_one_outcome(setup, attempt):
    """The gate opens, then ``attempt % 5`` loop iterations pass before the cancel: every interleaving of the
    driver call finishing and the cancellation arriving ends in one outcome, and the slot stays usable."""
    async with await setup.router.acquire(unique_session()) as lease:
        op = Op("finished", gate=asyncio.Event())
        request = Request("execute", (op,))
        lease.requests.put_nowait(request)
        await op.started.wait()
        op.gate.set()
        for _ in range(attempt % 5):
            await asyncio.sleep(0)
        lease.requests.cancel(request)
        await lease.requests.join()
        events = outcomes_of(drain(lease.events))
        assert len(events) == 1
        assert events[0] in (ResultEvent(request.request_id, "finished"), CancelledEvent(request.request_id, True))
        assert await lease.call("execute", Op("after")) == "after"
    assert setup.pool.idle == 1


@pytest.mark.parametrize("attempt", REPEAT)
async def test_a_cancelled_call_leaves_nothing_awaited_on_the_channel(setup, path, attempt):  # noqa: ARG001
    async with await setup.router.acquire(unique_session()) as lease:
        first = Op("first", gate=asyncio.Event())
        running = asyncio.create_task(lease.call("execute", first))
        queued = asyncio.create_task(lease.call("execute", Op("queued")))
        await first.started.wait()
        await until(lambda: lease.requests.qsize() == 1)
        queued.cancel()
        running.cancel()
        with pytest.raises(asyncio.CancelledError) as unsent:
            await queued
        with pytest.raises(asyncio.CancelledError) as sent:
            await running
        assert cancelled_before_start(unsent.value)
        assert not cancelled_before_start(sent.value)
        await lease.requests.join()
        assert lease.channel.awaited == 0
        assert lease.events.empty()
    assert setup.calls() == [("execute", "first")]


# Callbacks the slot delivers


async def test_a_successful_commit_or_rollback_delivers_its_callback_before_the_outcome(setup, path):  # noqa: ARG001
    async with await setup.router.acquire(unique_session()) as lease:
        requests = [Request("begin"), Request("commit"), Request("begin"), Request("rollback")]
        for request in requests:
            lease.requests.put_nowait(request)
        await lease.requests.join()
        events = drain(lease.events)
    ids = [r.request_id for r in requests]
    assert events == [
        ResultEvent(ids[0], None),
        CallbackEvent(ON_COMMIT),
        ResultEvent(ids[1], None),
        ResultEvent(ids[2], None),
        CallbackEvent(ON_ROLLBACK),
        ResultEvent(ids[3], None),
    ]


async def test_a_failed_commit_delivers_no_callback(setup):
    lease = await setup.router.acquire(unique_session())
    await lease.call("begin")
    lease.slot.connection.errors["commit"] = OperationalError("lost")
    with pytest.raises(OperationalError):
        await lease.call("commit")
    assert lease.events.empty()
    await lease.release()


async def test_a_handler_on_the_lease_channel_runs_for_callbacks_the_connection_emits(setup):
    seen = []
    async with await setup.router.acquire(unique_session()) as lease:
        lease.channel.on("cache_cleared", seen.append)
        await lease.call("execute", Op(effect=lambda c: c.emit(CallbackEvent("cache_cleared", ["k"]))))
        assert lease.events.empty()
    assert seen == [CallbackEvent("cache_cleared", ["k"])]


@pytest.mark.parametrize("name", [ON_COMMIT, ON_ROLLBACK])
async def test_a_reserved_callback_emitted_by_the_connection_is_dropped(setup, caplog, name):
    seen = []
    setup.pool.events.subscribe(Event, seen.append)
    async with await setup.router.acquire(unique_session()) as lease:
        with caplog.at_level(logging.ERROR, logger="adorm.executor"):
            await lease.call("execute", Op(effect=lambda c: c.emit(CallbackEvent(name))))
        assert lease.events.empty()
    setup.driver.connections[0].emit(CallbackEvent(name))
    assert seen == []
    assert "only the slot delivers" in caplog.text


async def test_an_outcome_event_emitted_by_the_connection_is_dropped(setup, caplog):
    seen = []
    setup.pool.events.subscribe(Event, seen.append)
    async with await setup.router.acquire(unique_session()) as lease:
        rid = []
        emitted = [
            lambda: ResultEvent(rid[0], "forged"),
            lambda: ErrorEvent(rid[0], OperationalError("forged")),
            lambda: CancelledEvent(rid[0], started=True),
        ]

        def forge(connection):
            for make in emitted:
                connection.emit(make())

        op = Op("own", gate=asyncio.Event(), effect=forge)
        call = asyncio.create_task(lease.call("execute", op))
        await op.started.wait()
        rid.append(next(iter(lease.channel._pending)))
        with caplog.at_level(logging.ERROR, logger="adorm.executor"):
            op.gate.set()
            assert await call == "own"
        assert lease.events.empty()
    assert seen == []
    assert caplog.text.count("emits only callbacks and backend events") == 3


async def test_a_cancelled_event_the_caller_did_not_ask_for_raises_interface_error(setup):
    async with await setup.router.acquire(unique_session()) as lease:
        op = Op("late", gate=asyncio.Event())
        call = asyncio.create_task(lease.call("execute", op))
        await op.started.wait()
        request_id = next(iter(lease.channel._pending))
        lease.channel.deliver(CancelledEvent(request_id, started=True))
        with pytest.raises(InterfaceError, match="cancelled by the slot"):
            await call
        op.gate.set()
        await lease.requests.join()
        assert drain(lease.events) == [ResultEvent(request_id, "late")]


async def test_events_after_release_go_to_the_pool_bus(setup):
    seen = []
    setup.pool.events.subscribe(Event, seen.append)
    lease = await setup.router.acquire(unique_session())
    lease.slot.connection.emit(BackendEvent("notify", "held"))
    await lease.release()
    assert lease.channel.closed
    setup.driver.connections[0].emit(BackendEvent("notify", "idle"))
    assert seen == [BackendEvent("notify", "held"), BackendEvent("notify", "idle")]


async def test_an_aborted_transaction_delivers_on_rollback_to_the_pool_bus():
    router = Router()
    pool = Pool(FakeDriver(), Profile("default"), acquire_timeout=LONG, commit_timeout=0.02)
    router.add(pool)
    seen = []
    pool.events.subscribe(CallbackEvent, seen.append)
    try:
        lease = await router.acquire(unique_session())
        await lease.call("begin")
        await until(lambda: pool.size == 0)
        assert lease.aborted
        assert seen == [CallbackEvent(ON_ROLLBACK)]
        await lease.release()
    finally:
        await router.close()


# on_commit


async def test_on_commit_runs_after_the_commit_outside_the_transaction(setup, path):  # noqa: ARG001
    session = unique_session()
    seen = []

    async def callback():
        seen.append(("callback", active_transaction(session), setup.pool.idle))
        seen.append(("query", await session.execute(Op("after commit"))))

    async with session.atomic() as tx:
        tx.on_commit(callback)
        await session.execute(Op("inside"))
        assert seen == []
    assert seen == [("callback", None, 1), ("query", "after commit")]
    calls = [c for n in range(len(setup.driver.connections)) for c in setup.calls(n)]
    assert calls == [("begin", None), ("execute", "inside"), ("commit", None), ("execute", "after commit")]


@pytest.mark.usefixtures("setup")
async def test_on_commit_callbacks_run_in_registration_order_sync_and_async():
    session = unique_session()
    seen = []

    async def later(tag):
        await asyncio.sleep(0)
        seen.append(tag)

    async with session.atomic() as tx:
        tx.on_commit(lambda: seen.append("sync"))
        tx.on_commit(lambda: later("async"))
        await adorm.on_commit(lambda: seen.append("module"), session=session)
    assert seen == ["sync", "async", "module"]


@pytest.mark.usefixtures("setup")
async def test_on_commit_callbacks_see_the_context_of_the_block():
    session = unique_session()
    seen = []
    token = flavour.set("vanilla")
    try:
        async with session.atomic() as tx:
            tx.on_commit(lambda: seen.append(flavour.get()))
    finally:
        flavour.reset(token)
    assert seen == ["vanilla"]


@pytest.mark.parametrize("end", ["exception", "rollback", "failed operation"])
async def test_on_commit_callbacks_are_dropped_when_the_transaction_rolls_back(setup, end):
    session = unique_session()
    seen = []

    async def flow():
        async with session.atomic() as tx:
            tx.on_commit(lambda: seen.append("ran"))
            if end == "exception":
                raise RuntimeError("abort")
            if end == "rollback":
                await tx.rollback()
                raise RuntimeError("after rollback")
            with pytest.raises(IntegrityError):
                await session.execute(Op(error=IntegrityError("dup")))

    with pytest.raises((RuntimeError, TransactionManagementError)):
        await flow()
    assert seen == []
    assert setup.calls()[-1] == ("rollback", None)


@pytest.mark.usefixtures("setup")
async def test_savepoint_callbacks_move_up_on_release_and_drop_on_rollback():
    session = unique_session()
    seen = []
    async with session.atomic():
        async with session.atomic() as kept:
            kept.on_commit(lambda: seen.append("kept"))

        async def dropped_savepoint():
            async with session.atomic() as dropped:
                dropped.on_commit(lambda: seen.append("dropped"))
                async with session.atomic() as inner:
                    inner.on_commit(lambda: seen.append("inner of dropped"))
                raise RuntimeError("roll back the savepoint")

        with pytest.raises(RuntimeError):
            await dropped_savepoint()
        await adorm.on_commit(lambda: seen.append("outer"), session=session)
        assert seen == []
    assert seen == ["kept", "outer"]


@pytest.mark.usefixtures("setup")
async def test_adorm_on_commit_registers_in_the_innermost_block():
    session = unique_session()
    seen = []

    async def savepoint():
        async with adorm.atomic():
            await adorm.on_commit(lambda: seen.append("savepoint"))
            raise RuntimeError

    with session.activate():
        async with adorm.atomic():
            with pytest.raises(RuntimeError):
                await savepoint()
            await adorm.on_commit(lambda: seen.append("transaction"))
    assert seen == ["transaction"]


async def test_adorm_on_commit_without_a_transaction_runs_now(setup):  # noqa: ARG001
    seen = []

    async def callback():
        seen.append("async")

    await adorm.on_commit(lambda: seen.append("sync"), session=unique_session())
    await adorm.on_commit(callback)
    assert seen == ["sync", "async"]
    with pytest.raises(RuntimeError):
        await adorm.on_commit(lambda: (_ for _ in ()).throw(RuntimeError("raised")))
    await adorm.on_commit(lambda: (_ for _ in ()).throw(RuntimeError("logged")), robust=True)


async def test_adorm_on_commit_refuses_a_transaction_that_is_not_an_atomic_block():
    class Plain:
        async def execute(self, operation):
            return operation

    session = unique_session()
    with bind_transaction(session, Plain()):
        with pytest.raises(TransactionManagementError, match="runs no callbacks"):
            await adorm.on_commit(print, session=session)


@pytest.mark.usefixtures("setup")
async def test_on_commit_needs_a_callable_and_an_open_block():
    session = unique_session()
    async with session.atomic() as tx:
        with pytest.raises(TypeError, match="callable"):
            tx.on_commit("not callable")
        with pytest.raises(TypeError, match="callable"):
            await adorm.on_commit(None)
        await tx.commit()
        with pytest.raises(TransactionClosed):
            tx.on_commit(print)


@pytest.mark.usefixtures("setup")
async def test_explicit_commit_runs_the_callbacks_before_it_returns():
    session = unique_session()
    seen = []
    async with session.atomic() as tx:
        tx.on_commit(lambda: seen.append("ran"))
        await tx.commit()
        assert seen == ["ran"]
        assert active_transaction(session) is tx
    assert seen == ["ran"]


async def test_a_failing_callback_is_raised_after_the_commit_and_stops_the_rest(setup):
    session = unique_session()
    seen = []

    def broken():
        raise ValueError("callback failed")

    async def flow():
        async with session.atomic() as tx:
            tx.on_commit(lambda: seen.append("first"))
            tx.on_commit(broken)
            tx.on_commit(lambda: seen.append("never"))

    with pytest.raises(ValueError, match="callback failed"):
        await flow()
    assert seen == ["first"]
    assert setup.calls()[-1] == ("commit", None)
    assert active_transaction(session) is None
    assert setup.pool.idle == 1


@pytest.mark.usefixtures("setup")
async def test_a_robust_callback_failure_is_logged_and_the_rest_run(caplog):
    session = unique_session()
    seen = []

    async def broken():
        raise ValueError("robust failure")

    with caplog.at_level(logging.ERROR, logger="adorm.executor"):
        async with session.atomic() as tx:
            tx.on_commit(broken, robust=True)
            tx.on_commit(lambda: seen.append("still runs"))
    assert seen == ["still runs"]
    assert "robust failure" in caplog.text


async def test_callbacks_run_only_when_the_slot_confirms_the_commit(setup, monkeypatch):
    original = Slot._deliver

    def without_commit_callback(self, lease, event):
        if event != CallbackEvent(ON_COMMIT):
            original(self, lease, event)

    monkeypatch.setattr(Slot, "_deliver", without_commit_callback)
    session = unique_session()
    seen = []
    async with session.atomic() as tx:
        tx.on_commit(lambda: seen.append("ran"))
    assert setup.calls()[-1] == ("commit", None)
    assert seen == []


async def test_callbacks_are_dropped_when_the_slot_aborts_the_transaction():
    router = Router()
    pool = Pool(FakeDriver(), Profile("default"), acquire_timeout=LONG, commit_timeout=0.02)
    router.add(pool)
    previous = set_runner(router)
    session = unique_session()
    seen = []

    async def flow():
        async with session.atomic() as tx:
            tx.on_commit(lambda: seen.append("ran"))
            await until(lambda: tx.lease.aborted)

    try:
        with pytest.raises(TransactionClosed):
            await flow()
    finally:
        set_runner(previous)
        await router.close()
    assert seen == []


@pytest.mark.parametrize("attempt", REPEAT)
async def test_callbacks_registered_by_concurrent_tasks_each_run_once(setup, attempt):  # noqa: ARG001
    session = unique_session()
    seen = []

    async def worker(number):
        await session.execute(Op(f"w{number}"))
        await adorm.on_commit(lambda: seen.append(number), session=session)

    async with session.atomic():
        await asyncio.gather(*(worker(n) for n in range(5)))
        async with asyncio.TaskGroup() as group:
            for n in range(5, 10):
                group.create_task(worker(n))
    assert sorted(seen) == list(range(10))


@pytest.mark.parametrize("attempt", REPEAT)
async def test_cancelling_the_end_while_callbacks_run_cancels_them_and_keeps_the_slot_free(setup, attempt):  # noqa: ARG001
    session = unique_session()
    entered, gate = asyncio.Event(), asyncio.Event()
    finished = []

    async def slow():
        entered.set()
        try:
            await gate.wait()
        finally:
            finished.append("cleaned up")

    async def flow():
        async with session.atomic() as tx:
            tx.on_commit(slow)

    task = asyncio.create_task(flow())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished == ["cleaned up"]
    assert setup.calls()[-1] == ("commit", None)
    assert setup.pool.idle == 1
    assert setup.pool.in_use == 0


@pytest.mark.usefixtures("setup")
async def test_operations_in_a_callback_do_not_reach_the_ended_transaction():
    session = unique_session()
    seen = []

    async def callback():
        seen.append(await session.execute(Op("own slot")))

    async with session.atomic() as tx:
        tx.on_commit(callback)
        await tx.commit()
        with pytest.raises(TransactionClosed):
            await session.execute(Op("too late"))
    assert seen == ["own slot"]


async def test_a_lost_channel_fails_the_awaited_call(setup):
    lease = await setup.router.acquire(unique_session())
    op = Op(gate=asyncio.Event())
    call = asyncio.create_task(lease.call("execute", op))
    await op.started.wait()
    lease.channel.close()
    with pytest.raises(InterfaceError, match="no outcome"):
        await call
    op.gate.set()
    await lease.release()


# A remote-style driver


class RemoteConnection(DriverConnection):
    """Runs each call on a lease of a "server" pool and carries the server slot's events back as JSON: the outcome
    becomes the method's return value or error, other events go to ``emit``, the server's own commit and rollback
    callbacks stay behind."""

    def __init__(self, driver, emit, server):
        super().__init__(driver)
        self.emit = emit
        self.server = server
        self.closed = False

    async def _call(self, method, *args):
        request = Request(method, args)
        self.server.requests.put_nowait(request)
        while True:
            event = Event.from_dict(json.loads(json.dumps((await self.server.events.get()).to_dict())))
            if isinstance(event, OUTCOME_EVENTS):
                assert event.request_id == request.request_id
                if isinstance(event, ResultEvent):
                    return event.value
                if isinstance(event, ErrorEvent):
                    raise event.error
                raise asyncio.CancelledError
            if isinstance(event, CallbackEvent) and event.name in RESERVED_CALLBACKS:
                continue
            self.emit(event)

    async def _execute(self, operation):
        return await self._call("execute", operation)

    async def begin(self):
        await self._call("begin")

    async def commit(self):
        await self._call("commit")

    async def rollback(self):
        await self._call("rollback")

    def cancel(self):
        pass

    def is_usable(self):
        return not self.closed

    async def close(self):
        self.closed = True
        await self.server.release()


class RemoteDriver(Driver):
    name = "remote-fake"
    mode = "async"
    capabilities = Capabilities(operations={"op"}, savepoints=True)

    def __init__(self, server_router):
        self.server_router = server_router

    async def open_connection(self, profile, emit):  # noqa: ARG002
        return RemoteConnection(self, emit, await self.server_router.acquire(unique_session()))


async def test_a_remote_style_connection_yields_one_outcome_and_one_on_commit_per_request():
    server = Setup()
    client = Router()
    client.add(Pool(RemoteDriver(server.router), Profile("default"), acquire_timeout=LONG, commit_timeout=None))
    previous = set_runner(client)
    try:

        def effects(connection):
            connection.emit(BackendEvent("notify", "orders", "42"))
            connection.emit(CallbackEvent("cache_cleared", ["club:1"]))

        async with await client.acquire(unique_session()) as lease:
            requests = [
                Request("begin"),
                Request("execute", (Op("row", effect=effects),)),
                Request("execute", (Op(error=IntegrityError("dup", sqlstate="23505")),)),
                Request("commit"),
            ]
            for request in requests:
                lease.requests.put_nowait(request)
            await lease.requests.join()
            events = drain(lease.events)
        ids = [r.request_id for r in requests]
        assert [e.request_id for e in outcomes_of(events)] == ids
        assert events[:4] == [
            ResultEvent(ids[0], None),
            BackendEvent("notify", "orders", "42"),
            CallbackEvent("cache_cleared", ["club:1"]),
            ResultEvent(ids[1], "row"),
        ]
        assert type(events[4].error) is IntegrityError
        assert events[4].error.sqlstate == "23505"
        assert events[5:] == [CallbackEvent(ON_COMMIT), ResultEvent(ids[3], None)]

        session = unique_session()
        ran = []
        async with session.atomic() as tx:
            tx.on_commit(lambda: ran.append("committed"))
            assert await session.execute(Op("inside")) == "inside"
        assert ran == ["committed"]
    finally:
        set_runner(previous)
        await client.close()
        await server.router.close()
