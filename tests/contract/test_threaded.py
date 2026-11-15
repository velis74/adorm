"""ThreadedConnection: the bridge between the event loop and a connection's own thread."""

import asyncio
import contextvars
import gc
import logging
import threading
import time
import warnings

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import pytest

from adorm.drivers import Capabilities, ThreadedConnection, ThreadedDriver
from adorm.events import BackendEvent, CallbackEvent
from adorm.exceptions import (
    DatabaseError,
    IntegrityError,
    InterfaceError,
    NotSupportedError,
    ObjectDoesNotExist,
    OperationalError,
)
from tests.contract.drivers import Profile, Recorder

TIMEOUT = 10  # upper bound for waits that end on an event; never reached when the code works


@dataclass(frozen=True)
class Op:
    action: Callable[["FakeConnection"], Any]
    kind: str = "fake"

    def constructs(self) -> Iterable[str]:
        return ()


class SqlError(Exception):
    def __init__(self, sqlstate):
        super().__init__(f"sqlstate {sqlstate}")
        self.sqlstate = sqlstate


@dataclass
class Control:
    """Knobs and observations shared between a test and its fake connection."""

    fail_connect: BaseException | None = None
    fail_close: BaseException | None = None
    cancel_gate: threading.Event | None = None
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    cancel_threads: list[int] = field(default_factory=list)
    calls: list[tuple[str, int]] = field(default_factory=list)
    closed: threading.Event = field(default_factory=threading.Event)


class FakeConnection(ThreadedConnection):
    @property
    def control(self) -> Control:
        return self.driver.control

    def record(self, name):
        self.control.calls.append((name, threading.get_ident()))

    def connect_sync(self):
        self.record("connect")
        if self.control.fail_connect is not None:
            raise self.control.fail_connect

    def execute_sync(self, operation):
        self.record("execute")
        return operation.action(self)

    def begin_sync(self):
        self.record("begin")

    def commit_sync(self):
        self.record("commit")
        self.emit(CallbackEvent("on_commit"))

    def rollback_sync(self):
        self.record("rollback")

    def savepoint_sync(self, name):
        self.record(f"savepoint {name}")

    def set_options_sync(self, options, local):
        self.record(f"options {sorted(options.items())} local={local}")

    def cancel_sync(self):
        self.control.cancel_threads.append(threading.get_ident())
        self.control.cancel_requested.set()
        if self.control.cancel_gate is not None:
            assert self.control.cancel_gate.wait(TIMEOUT)

    def close_sync(self):
        self.record("close")
        self.control.closed.set()
        if self.control.fail_close is not None:
            raise self.control.fail_close


class FakeDriver(ThreadedDriver):
    name = "fake"
    connection_class = FakeConnection
    capabilities = Capabilities(
        operations={"fake"}, connection_options={"statement_timeout"}, savepoints=True, cancellation=True
    )

    def __init__(self, cancel_grace=TIMEOUT):
        self.control = Control()
        self.cancel_grace = cancel_grace


@pytest.fixture
async def fake():
    driver = FakeDriver()
    recorder = Recorder()
    conn = await driver.open_connection(Profile("default"), recorder)
    try:
        yield conn, driver.control, recorder
    finally:
        await conn.close()
        conn.thread.join(TIMEOUT)
        assert not conn.thread.is_alive()


def wait_for_cancel(conn: FakeConnection) -> Any:
    """An action that runs until the connection's cancel_sync is called, then fails like a cancelled query."""
    assert conn.control.cancel_requested.wait(TIMEOUT)
    raise SqlError("57014")


async def started(event: threading.Event) -> None:
    assert await asyncio.to_thread(event.wait, TIMEOUT)


# Threads


async def test_every_call_runs_on_the_connection_thread(fake):
    conn, control, _ = fake
    await conn.execute(Op(lambda _: None))
    await conn.begin()
    await conn.savepoint("s")
    await conn.set_options({"statement_timeout": 1}, local=True)
    await conn.commit()
    await conn.rollback()
    threads = {ident for _, ident in control.calls}
    assert threads == {conn.thread.ident}
    assert conn.thread.ident != threading.get_ident()
    assert conn.thread.name.startswith("adorm-fake-")
    assert [name for name, _ in control.calls] == [
        "connect",
        "execute",
        "begin",
        "savepoint s",
        "options [('statement_timeout', 1)] local=True",
        "commit",
        "rollback",
    ]


async def test_connections_have_their_own_threads():
    driver = FakeDriver()
    first = await driver.open_connection(Profile("default"), Recorder())
    second = await driver.open_connection(Profile("default"), Recorder())
    try:
        assert first.thread is not second.thread
        idents = await asyncio.gather(*(c.execute(Op(lambda _: threading.get_ident())) for c in (first, second)))
        assert idents == [first.thread.ident, second.thread.ident]
    finally:
        await first.close()
        await second.close()


@pytest.mark.parametrize("repeat", range(5))
async def test_concurrent_calls_are_serialised_on_one_thread(fake, repeat):  # noqa: ARG001
    conn, _, _ = fake
    running = 0
    overlaps = 0

    def action(_conn):
        nonlocal running, overlaps
        running += 1
        overlaps += running > 1
        result = threading.get_ident()
        running -= 1
        return result

    results = await asyncio.gather(*(conn.execute(Op(action)) for _ in range(200)))
    assert set(results) == {conn.thread.ident}
    assert overlaps == 0


# Results, events, errors


async def test_result_is_returned(fake):
    conn, _, _ = fake
    assert await conn.execute(Op(lambda _: [{"id": 1}])) == [{"id": 1}]


async def test_events_arrive_on_the_loop_thread_before_the_result(fake):
    conn, _, recorder = fake
    order = []
    recorder.on_event = lambda event: order.append(event.payload)

    def action(c):
        for i in range(100):
            c.emit(BackendEvent("notify", "ch", i))
        return "done"

    result = await conn.execute(Op(action))
    order.append(result)
    assert order == [*range(100), "done"]
    assert set(recorder.threads) == {threading.get_ident()}


async def test_callback_event_from_commit(fake):
    conn, _, recorder = fake
    await conn.begin()
    await conn.commit()
    assert recorder.events == [CallbackEvent("on_commit")]


async def test_driver_error_is_mapped_with_original(fake):
    conn, _, _ = fake
    error = SqlError("23505")
    with pytest.raises(IntegrityError) as info:
        await conn.execute(Op(lambda _: (_ for _ in ()).throw(error)))
    assert info.value.original is error
    assert info.value.__cause__ is error
    assert info.value.sqlstate == "23505"
    assert conn.is_usable()


async def test_adorm_error_passes_unchanged(fake):
    conn, _, _ = fake
    error = ObjectDoesNotExist("missing")
    with pytest.raises(ObjectDoesNotExist) as info:
        await conn.execute(Op(lambda _: (_ for _ in ()).throw(error)))
    assert info.value is error


@pytest.mark.parametrize(
    ("error", "usable"),
    [
        (OSError("connection reset"), False),
        (SqlError("08006"), False),
        (SqlError("57P01"), False),
        (SqlError("57014"), True),
        (SqlError("40001"), True),
        (ValueError("bad"), True),
    ],
)
async def test_connection_errors_make_connection_unusable(fake, error, usable):
    conn, _, _ = fake

    def fail(_conn):
        raise error

    with pytest.raises(DatabaseError):
        await conn.execute(Op(fail))
    assert conn.is_usable() is usable


async def test_base_exception_on_thread_becomes_interface_error(fake):
    conn, _, _ = fake

    def interrupt(_conn):
        raise KeyboardInterrupt

    with pytest.raises(InterfaceError) as info:
        await conn.execute(Op(interrupt))
    assert isinstance(info.value.original, KeyboardInterrupt)
    assert not conn.is_usable()
    assert conn.thread.is_alive()


async def test_map_error_override_runs_on_thread():
    seen = []

    class Mapping(FakeConnection):
        def map_error(self, exc):
            seen.append(threading.get_ident())
            return IntegrityError("mapped", original=exc)

    class Driver(FakeDriver):
        connection_class = Mapping

    conn = await Driver().open_connection(Profile("default"), Recorder())
    try:
        with pytest.raises(IntegrityError, match="mapped"):
            await conn.execute(Op(lambda _: 1 / 0))
        assert seen == [conn.thread.ident]
    finally:
        await conn.close()


async def test_failing_map_error_falls_back_to_default(caplog):
    class Broken(FakeConnection):
        def map_error(self, exc):
            raise RuntimeError("broken mapper") from exc

    class Driver(FakeDriver):
        connection_class = Broken

    conn = await Driver().open_connection(Profile("default"), Recorder())
    try:
        with caplog.at_level(logging.ERROR, logger="adorm.drivers"), pytest.raises(IntegrityError):
            await conn.execute(Op(lambda _: (_ for _ in ()).throw(SqlError("23505"))))
        assert "map_error failed" in caplog.text
    finally:
        await conn.close()


# Capabilities


async def test_unsupported_operation_is_refused_before_the_thread(fake):
    conn, control, _ = fake

    @dataclass(frozen=True)
    class Other:
        kind: str = "select"

        def constructs(self):
            return ["function.Lower"]

    with pytest.raises(NotSupportedError, match="select, function.Lower"):
        await conn.execute(Other())
    with pytest.raises(NotSupportedError, match="lock_timeout"):
        await conn.set_options({"lock_timeout": 1}, local=False)
    assert [name for name, _ in control.calls] == ["connect"]


async def test_default_hooks_refuse_savepoints_and_options():
    class Minimal(FakeConnection):
        savepoint_sync = ThreadedConnection.savepoint_sync
        set_options_sync = ThreadedConnection.set_options_sync

    class Driver(FakeDriver):
        connection_class = Minimal

    conn = await Driver().open_connection(Profile("default"), Recorder())
    try:
        for call in (conn.savepoint("s"), conn.release_savepoint("s"), conn.rollback_to_savepoint("s")):
            with pytest.raises(NotSupportedError, match="savepoints"):
                await call
        with pytest.raises(NotSupportedError, match="statement_timeout"):
            await conn.set_options({"statement_timeout": 1}, local=False)
        await conn.set_options({}, local=False)
        assert conn.is_usable()
    finally:
        await conn.close()


async def test_savepoints_not_declared_are_refused_before_the_thread():
    class Driver(FakeDriver):
        capabilities = Capabilities(operations={"fake"})

    driver = Driver()
    conn = await driver.open_connection(Profile("default"), Recorder())
    try:
        for call in (conn.savepoint("s"), conn.release_savepoint("s"), conn.rollback_to_savepoint("s")):
            with pytest.raises(NotSupportedError, match="savepoints"):
                await call
        assert [name for name, _ in driver.control.calls] == ["connect"]
    finally:
        await conn.close()


# Connection loss


async def test_is_connection_lost_override_keeps_the_connection():
    class SQLiteLike(FakeConnection):
        def is_connection_lost(self, exc):
            return isinstance(exc, InterfaceError)

    class Driver(FakeDriver):
        connection_class = SQLiteLike

    class StatementError(Exception):
        """A PEP 249 OperationalError without a SQLSTATE, as sqlite3 raises for "no such table"."""

    StatementError.__name__ = "OperationalError"

    def fail(_conn):
        raise StatementError("no such table: missing")

    conn = await Driver().open_connection(Profile("default"), Recorder())
    try:
        with pytest.raises(OperationalError) as info:
            await conn.execute(Op(fail))
        assert info.value.sqlstate is None
        assert conn.is_usable()
        assert await conn.execute(Op(lambda _: 1)) == 1
    finally:
        await conn.close()


async def test_operational_error_without_sqlstate_is_lost_by_default(fake):
    conn, _, _ = fake
    error = type("OperationalError", (Exception,), {})("server closed the connection")

    def fail(_conn):
        raise error

    with pytest.raises(OperationalError):
        await conn.execute(Op(fail))
    assert not conn.is_usable()


async def test_failing_is_connection_lost_gives_up_the_connection(caplog):
    class Broken(FakeConnection):
        def is_connection_lost(self, exc):
            raise RuntimeError("broken") from exc

    class Driver(FakeDriver):
        connection_class = Broken

    conn = await Driver().open_connection(Profile("default"), Recorder())
    try:
        with caplog.at_level(logging.ERROR, logger="adorm.drivers"), pytest.raises(IntegrityError):
            await conn.execute(Op(lambda _: (_ for _ in ()).throw(SqlError("23505"))))
        assert "is_connection_lost failed" in caplog.text
        assert not conn.is_usable()
    finally:
        await conn.close()


# Context


request_id = contextvars.ContextVar("request_id", default="unset")


async def test_sync_methods_see_a_copy_of_the_callers_context(fake):
    conn, _, _ = fake
    request_id.set("r1")

    def action(_conn):
        seen = request_id.get()
        request_id.set("changed on the thread")
        return seen

    assert await conn.execute(Op(action)) == "r1"
    assert request_id.get() == "r1"


class PreparingConnection(FakeConnection):
    def prepare_call_sync(self):
        self.record(f"prepare {request_id.get()}")
        request_id.set(f"prepared {request_id.get()}")
        if request_id.get() == "prepared fail":
            raise SqlError("23505")


class PreparingDriver(FakeDriver):
    connection_class = PreparingConnection


async def test_prepare_call_sync_runs_before_every_call_in_its_context():
    driver = PreparingDriver()
    conn = await driver.open_connection(Profile("default"), Recorder())
    try:
        request_id.set("r1")
        assert await conn.execute(Op(lambda _conn: request_id.get())) == "prepared r1"
        await conn.begin()
        assert request_id.get() == "r1"
        assert [name for name, _ in driver.control.calls] == [
            "prepare unset",
            "connect",
            "prepare r1",
            "execute",
            "prepare r1",
            "begin",
        ]
        assert {ident for _, ident in driver.control.calls} == {conn.thread.ident}
        request_id.set("fail")
        with pytest.raises(IntegrityError):
            await conn.execute(Op(lambda _conn: "never"))
        assert driver.control.calls[-1][0] == "prepare fail"
        assert conn.is_usable()
    finally:
        await conn.close()


# Cancellation


@pytest.mark.parametrize("repeat", range(5))
async def test_cancelling_the_task_cancels_the_running_call(fake, repeat):  # noqa: ARG001
    conn, control, _ = fake
    running = threading.Event()

    def action(c):
        running.set()
        return wait_for_cancel(c)

    task = asyncio.create_task(conn.execute(Op(action)))
    await started(running)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(control.cancel_threads) == 1
    assert control.cancel_threads[0] not in (conn.thread.ident, threading.get_ident())
    assert conn.is_usable()
    assert await conn.execute(Op(lambda _: "next")) == "next"


async def test_cancel_waits_for_the_call_to_finish(fake):
    conn, _, _ = fake
    running = threading.Event()
    finish = threading.Event()
    finished = threading.Event()

    def action(c):
        running.set()
        assert c.control.cancel_requested.wait(TIMEOUT)
        assert finish.wait(TIMEOUT)
        finished.set()

    task = asyncio.create_task(conn.execute(Op(action)))
    await started(running)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


async def test_repeated_cancellation_while_draining(fake):
    conn, _, _ = fake
    running = threading.Event()
    finish = threading.Event()

    def action(c):
        running.set()
        assert finish.wait(TIMEOUT)
        return wait_for_cancel(c)

    task = asyncio.create_task(conn.execute(Op(action)))
    await started(running)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await conn.execute(Op(lambda _: 1)) == 1


async def test_cancelling_a_queued_call_never_runs_it(fake):
    conn, control, _ = fake
    running = threading.Event()
    release = threading.Event()
    ran = []

    def first(_conn):
        running.set()
        assert release.wait(TIMEOUT)
        return "first"

    head = asyncio.create_task(conn.execute(Op(first)))
    await started(running)
    queued = asyncio.create_task(conn.execute(Op(lambda _: ran.append("queued"))))
    await asyncio.sleep(0)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    release.set()
    assert await head == "first"
    assert await conn.execute(Op(lambda _: "after")) == "after"
    assert ran == []
    assert control.cancel_threads == []


@pytest.mark.parametrize("repeat", range(30))
async def test_cancellation_racing_completion(fake, repeat):  # noqa: ARG001
    conn, _, _ = fake
    barrier = threading.Barrier(2, timeout=TIMEOUT)

    def action(_conn):
        barrier.wait()
        return "completed"

    task = asyncio.create_task(conn.execute(Op(action)))
    await asyncio.to_thread(barrier.wait)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        if task.cancel():
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await task == "completed"
        gc.collect()
    assert conn.is_usable()
    assert await conn.execute(Op(lambda _: "next")) == "next"


async def test_cancel_sync_blocks_the_next_call():
    driver = FakeDriver()
    driver.control.cancel_gate = threading.Event()
    conn = await driver.open_connection(Profile("default"), Recorder())
    running = threading.Event()
    returned = threading.Event()
    second_started = threading.Event()

    def first(c):
        running.set()
        assert c.control.cancel_requested.wait(TIMEOUT)
        returned.set()
        return "first"

    try:
        head = asyncio.create_task(conn.execute(Op(first)))
        await started(running)
        second = asyncio.create_task(conn.execute(Op(lambda _: second_started.set())))
        await asyncio.sleep(0)
        canceller = threading.Thread(target=conn.cancel)
        canceller.start()
        await started(returned)
        # The first call has returned but cancel_sync has not: the thread must not start the second call.
        assert not await asyncio.to_thread(second_started.wait, 0.05)
        driver.control.cancel_gate.set()
        canceller.join(TIMEOUT)
        assert await head == "first"
        await second
        assert second_started.is_set()
    finally:
        driver.control.cancel_gate.set()
        await conn.close()


async def test_cancel_from_another_thread_reaches_the_running_call(fake):
    conn, control, _ = fake
    running = threading.Event()

    def action(c):
        running.set()
        return wait_for_cancel(c)

    task = asyncio.create_task(conn.execute(Op(action)))
    await started(running)
    canceller = threading.Thread(target=conn.cancel)
    canceller.start()
    with pytest.raises(OperationalError) as info:
        await task
    canceller.join(TIMEOUT)
    assert info.value.sqlstate == "57014"
    assert control.cancel_threads == [canceller.ident]
    assert conn.is_usable()


async def test_cancel_on_the_loop_does_not_block_it():
    driver = FakeDriver()
    driver.control.cancel_gate = threading.Event()
    conn = await driver.open_connection(Profile("default"), Recorder())
    running = threading.Event()

    def action(c):
        running.set()
        return wait_for_cancel(c)

    try:
        task = asyncio.create_task(conn.execute(Op(action)))
        await started(running)
        started_at = time.monotonic()
        conn.cancel()
        assert time.monotonic() - started_at < 1
        ticks = 0
        for _ in range(5):
            await asyncio.sleep(0.01)
            ticks += 1
        assert ticks == 5
        assert not task.done()
        driver.control.cancel_gate.set()
        with pytest.raises(OperationalError) as info:
            await task
        assert info.value.sqlstate == "57014"
        assert driver.control.cancel_threads[0] not in (conn.thread.ident, threading.get_ident())
        assert conn.is_usable()
    finally:
        driver.control.cancel_gate.set()
        await conn.close()


async def test_cancellation_does_not_depend_on_the_default_executor():
    loop = asyncio.get_running_loop()
    driver = FakeDriver(cancel_grace=TIMEOUT)
    conn = await driver.open_connection(Profile("default"), Recorder())
    executor_free = threading.Event()
    running = threading.Event()

    def action(c):
        running.set()
        return wait_for_cancel(c)

    busy = [loop.run_in_executor(None, executor_free.wait, TIMEOUT) for _ in range(64)]
    try:
        task = asyncio.create_task(conn.execute(Op(action)))
        async with asyncio.timeout(TIMEOUT / 2):
            # started() needs the default executor, which is saturated.
            while not running.is_set():
                await asyncio.sleep(0.001)
        task.cancel()
        async with asyncio.timeout(TIMEOUT / 2):
            with pytest.raises(asyncio.CancelledError):
                await task
        assert len(driver.control.cancel_threads) == 1
        assert conn.is_usable()
    finally:
        executor_free.set()
        await asyncio.gather(*busy)
        await conn.close()


async def test_cancel_when_idle_does_nothing(fake):
    conn, control, _ = fake
    conn.cancel()
    assert control.cancel_threads == []
    assert await conn.execute(Op(lambda _: 1)) == 1


async def test_failing_cancel_sync_is_logged(fake, caplog):
    conn, _, _ = fake
    running = threading.Event()
    finish = threading.Event()

    def broken_cancel():
        finish.set()
        raise RuntimeError("cancel failed")

    conn.cancel_sync = broken_cancel

    def action(_conn):
        running.set()
        assert finish.wait(TIMEOUT)

    task = asyncio.create_task(conn.execute(Op(action)))
    await started(running)
    with caplog.at_level(logging.ERROR, logger="adorm.drivers"):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert "cancelling the running fake call failed" in caplog.text
    assert conn.is_usable()


async def test_call_ignoring_cancellation_gives_up_the_connection(caplog):
    driver = FakeDriver(cancel_grace=0.01)
    conn = await driver.open_connection(Profile("default"), Recorder())
    running = threading.Event()
    release = threading.Event()

    def stubborn(_conn):
        running.set()
        assert release.wait(TIMEOUT)
        raise SqlError("23505")

    task = asyncio.create_task(conn.execute(Op(stubborn)))
    await started(running)
    with caplog.at_level(logging.WARNING, logger="adorm.drivers"):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert "did not finish within" in caplog.text
    assert not conn.is_usable()
    with pytest.raises(InterfaceError, match="still running a cancelled call"):
        await conn.execute(Op(lambda _: 1))
    await conn.close()
    assert not driver.control.closed.is_set()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        release.set()
        assert await asyncio.to_thread(driver.control.closed.wait, TIMEOUT)
        conn.thread.join(TIMEOUT)
        await asyncio.sleep(0)
        gc.collect()
    assert not conn.thread.is_alive()


# Opening and closing


async def test_close_runs_on_thread_and_stops_it(fake):
    conn, control, _ = fake
    await conn.close()
    await conn.close()
    conn.thread.join(TIMEOUT)
    assert not conn.thread.is_alive()
    assert [name for name, _ in control.calls].count("close") == 1
    assert control.calls[-1] == ("close", conn.thread.ident)
    assert not conn.is_usable()
    with pytest.raises(InterfaceError, match="closed"):
        await conn.execute(Op(lambda _: 1))


async def test_close_error_is_logged_not_raised(caplog):
    driver = FakeDriver()
    driver.control.fail_close = SqlError("08006")
    conn = await driver.open_connection(Profile("default"), Recorder())
    with caplog.at_level(logging.ERROR, logger="adorm.drivers"):
        await conn.close()
    assert "closing fake connection failed" in caplog.text
    conn.thread.join(TIMEOUT)
    assert not conn.thread.is_alive()


async def test_connect_failure_is_mapped_closes_and_stops_thread():
    driver = FakeDriver()
    driver.control.fail_connect = OSError("refused")
    before = set(threading.enumerate())
    with pytest.raises(OperationalError) as info:
        await driver.open_connection(Profile("default"), Recorder())
    assert isinstance(info.value.original, OSError)
    assert await asyncio.to_thread(driver.control.closed.wait, TIMEOUT)
    assert [name for name, _ in driver.control.calls] == ["connect", "close"]
    new = [t for t in threading.enumerate() if t not in before and t.name.startswith("adorm-fake-")]
    for thread in new:
        thread.join(TIMEOUT)
    assert not any(t.is_alive() for t in new)


async def test_cancelling_open_connection_during_connect():
    connecting = threading.Event()

    class SlowConnect(FakeConnection):
        def connect_sync(self):
            self.record("connect")
            connecting.set()
            assert self.control.cancel_requested.wait(TIMEOUT)
            raise SqlError("57014")

    class Driver(FakeDriver):
        connection_class = SlowConnect

    driver = Driver()
    before = set(threading.enumerate())
    task = asyncio.create_task(driver.open_connection(Profile("default"), Recorder()))
    await started(connecting)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(driver.control.closed.wait, TIMEOUT)
    assert [name for name, _ in driver.control.calls] == ["connect", "close"]
    new = [t for t in threading.enumerate() if t not in before and t.name.startswith("adorm-fake-")]
    for thread in new:
        thread.join(TIMEOUT)
    assert not any(t.is_alive() for t in new)


async def test_closing_while_a_call_runs_waits_for_it(fake):
    conn, control, _ = fake
    running = threading.Event()
    release = threading.Event()

    def action(_conn):
        running.set()
        assert release.wait(TIMEOUT)
        return "done"

    task = asyncio.create_task(conn.execute(Op(action)))
    await started(running)
    closing = asyncio.create_task(conn.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    assert await task == "done"
    await closing
    assert [name for name, _ in control.calls][-2:] == ["execute", "close"]


async def test_no_threads_leak_after_many_connections():
    driver = FakeDriver()
    before = {t.ident for t in threading.enumerate()}
    for _ in range(20):
        conn = await driver.open_connection(Profile("default"), Recorder())
        await conn.execute(Op(lambda _: 1))
        await conn.close()
        conn.thread.join(TIMEOUT)
    leaked = [t for t in threading.enumerate() if t.ident not in before and t.name.startswith("adorm-fake-")]
    assert leaked == []


def test_event_after_loop_closed_is_dropped(caplog):
    recorder = Recorder()
    holder = {}

    async def open_and_leave():
        holder["conn"] = await FakeDriver().open_connection(Profile("default"), recorder)

    loop = asyncio.new_event_loop()
    loop.run_until_complete(open_and_leave())
    loop.close()
    conn = holder["conn"]
    with caplog.at_level(logging.WARNING, logger="adorm.drivers"):
        conn.emit(BackendEvent("notify"))
    assert "dropping" in caplog.text
    assert recorder.events == []
    conn._requests.put(None)
    conn.thread.join(TIMEOUT)


# Direct execution


class DirectDriver(FakeDriver):
    direct = True


@pytest.fixture
async def direct():
    driver = DirectDriver()
    recorder = Recorder()
    conn = await driver.open_connection(Profile("default"), recorder)
    try:
        yield conn, driver.control, recorder
    finally:
        await conn.close()


async def test_direct_calls_run_on_the_loop_thread(direct):
    conn, control, _ = direct
    await conn.execute(Op(lambda _: None))
    await conn.begin()
    await conn.commit()
    assert conn.thread is threading.current_thread()
    assert {ident for _, ident in control.calls} == {threading.get_ident()}
    assert [name for name, _ in control.calls] == ["connect", "execute", "begin", "commit"]
    assert conn.is_usable()


async def test_direct_calls_see_no_running_loop(direct):
    conn, _, _ = direct

    def action(_conn):
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        return "sync"

    assert await conn.execute(Op(action)) == "sync"
    assert asyncio.get_running_loop() is not None


async def test_direct_events_arrive_before_the_result(direct):
    conn, _, recorder = direct
    order = []
    recorder.on_event = lambda event: order.append(event.payload)

    def action(c):
        for i in range(3):
            c.emit(BackendEvent("notify", "ch", i))
        return "done"

    order.append(await conn.execute(Op(action)))
    assert order == [0, 1, 2, "done"]
    assert set(recorder.threads) == {threading.get_ident()}


async def test_direct_errors_are_mapped(direct):
    conn, _, _ = direct
    request_id.set("r2")

    def fail(_conn):
        assert request_id.get() == "r2"
        raise SqlError("23505")

    with pytest.raises(IntegrityError) as info:
        await conn.execute(Op(fail))
    assert info.value.sqlstate == "23505"
    assert conn.is_usable()
    with pytest.raises(OperationalError):
        await conn.execute(Op(lambda _: (_ for _ in ()).throw(SqlError("08006"))))
    assert not conn.is_usable()


async def test_direct_call_is_cancelled_from_another_thread(direct):
    conn, control, _ = direct

    def action(c):
        canceller = threading.Thread(target=c.cancel)
        canceller.start()
        try:
            return wait_for_cancel(c)
        finally:
            canceller.join(TIMEOUT)

    with pytest.raises(OperationalError) as info:
        await conn.execute(Op(action))
    assert info.value.sqlstate == "57014"
    assert len(control.cancel_threads) == 1
    assert control.cancel_threads[0] != threading.get_ident()
    assert conn.is_usable()


async def test_direct_close_and_connect_failure():
    driver = DirectDriver()
    conn = await driver.open_connection(Profile("default"), Recorder())
    await conn.close()
    await conn.close()
    assert [name for name, _ in driver.control.calls] == ["connect", "close"]
    assert not conn.is_usable()
    with pytest.raises(InterfaceError, match="closed"):
        await conn.execute(Op(lambda _: 1))

    failing = DirectDriver()
    failing.control.fail_connect = OSError("refused")
    with pytest.raises(OperationalError):
        await failing.open_connection(Profile("default"), Recorder())
    assert [name for name, _ in failing.control.calls] == ["connect", "close"]


async def test_direct_close_error_is_logged(caplog):
    driver = DirectDriver()
    driver.control.fail_close = SqlError("08006")
    conn = await driver.open_connection(Profile("default"), Recorder())
    with caplog.at_level(logging.ERROR, logger="adorm.drivers"):
        await conn.close()
    assert "closing fake connection failed" in caplog.text
