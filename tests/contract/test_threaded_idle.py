"""ThreadedConnection between calls: watching ``idle_fileno()``, waking for calls, failures while idle and closing."""

import asyncio
import socket
import threading

import pytest

from adorm.drivers import Capabilities, ThreadedConnection, ThreadedDriver
from adorm.events import BackendEvent, CONNECTION_LOST, NOTIFY
from tests.contract.drivers import Profile, Recorder

TIMEOUT = 10
REPEAT = range(10)


class Op:
    kind = "fake"

    def __init__(self, action=None):
        self.action = action

    def constructs(self):
        return ()


class IdleConnection(ThreadedConnection):
    """Watches the reading end of a socket pair while ``watching``; each byte read is a notification. ``fail_idle``
    makes ``idle_sync()`` raise."""

    def connect_sync(self):
        self.reader, self.writer = socket.socketpair()
        self.reader.setblocking(False)
        self.watching = False
        self.fail_idle: BaseException | None = None
        self.idle_threads: set[int] = set()
        self.idle_runs = 0

    def execute_sync(self, operation):
        return operation.action(self) if operation.action is not None else threading.get_ident()

    def begin_sync(self):
        pass

    def commit_sync(self):
        pass

    def rollback_sync(self):
        pass

    def listen_sync(self, channel):  # noqa: ARG002
        self.watching = True

    def unlisten_sync(self, channel):  # noqa: ARG002
        self.watching = False

    def idle_fileno(self):
        return self.reader.fileno() if self.watching else None

    def idle_sync(self):
        self.idle_runs += 1
        self.idle_threads.add(threading.get_ident())
        if self.fail_idle is not None:
            raise self.fail_idle
        try:
            data = self.reader.recv(65536)
        except BlockingIOError:
            return
        for byte in data:
            self.emit(BackendEvent(NOTIFY, "bytes", str(byte)))

    def close_sync(self):
        self.reader.close()
        self.writer.close()


class IdleDriver(ThreadedDriver):
    name = "fake-idle"
    connection_class = IdleConnection
    capabilities = Capabilities(operations={"fake"}, backend_events={NOTIFY, CONNECTION_LOST})

    def __init__(self, direct=False):
        self.direct = direct


@pytest.fixture(params=[False, True], ids=["thread", "direct"])
async def idle(request):
    recorder = Recorder()
    conn = await IdleDriver(direct=request.param).open_connection(Profile("default"), recorder)
    try:
        yield conn, recorder
    finally:
        await conn.close()
        if not request.param:
            conn.thread.join(TIMEOUT)
            assert not conn.thread.is_alive()


async def received(recorder, count):
    async with asyncio.timeout(TIMEOUT):
        while len(recorder.events) < count:
            recorder.arrived.clear()
            await recorder.arrived.wait()
    return recorder.events


async def test_data_arriving_between_calls_is_read_on_the_connection_thread(idle):
    conn, recorder = idle
    await conn.listen("bytes")
    conn.writer.send(bytes([1, 2]))
    events = await received(recorder, 2)
    assert events == [BackendEvent(NOTIFY, "bytes", "1"), BackendEvent(NOTIFY, "bytes", "2")]
    assert conn.idle_threads == {conn.thread.ident}
    assert set(recorder.threads) == {threading.get_ident()}


def watch_state(conn):
    """On the connection's thread, between calls: the watched descriptor, ``idle_sync()`` runs and the unread data."""
    return conn._watched, conn.idle_runs, conn.reader.recv(16)


async def until(predicate):
    async with asyncio.timeout(TIMEOUT):
        while not predicate():
            await asyncio.sleep(0)


async def test_nothing_is_watched_unless_the_connection_asks(idle):
    conn, recorder = idle
    conn.writer.send(b"\x01")
    assert await conn.execute(Op(watch_state)) == (None, 0, b"\x01")
    await conn.listen("bytes")
    conn.writer.send(b"\x02")
    assert await received(recorder, 1) == [BackendEvent(NOTIFY, "bytes", "2")]


@pytest.mark.parametrize("repeat", REPEAT)
async def test_calls_run_promptly_while_the_thread_watches(idle, repeat):  # noqa: ARG001
    conn, _recorder = idle
    await conn.listen("bytes")
    async with asyncio.timeout(TIMEOUT):
        for _ in range(200):
            assert await conn.execute(Op()) == conn.thread.ident


@pytest.mark.parametrize("repeat", REPEAT)
async def test_data_racing_calls_is_all_read_in_order(idle, repeat):  # noqa: ARG001
    conn, recorder = idle
    await conn.listen("bytes")
    count = 300
    stop = threading.Event()

    def write():
        for value in range(count):
            conn.writer.send(bytes([value % 256]))
            if value % 7 == 0:
                stop.wait(0.0001)

    writer = threading.Thread(target=write)
    writer.start()
    try:
        async with asyncio.timeout(TIMEOUT):
            while writer.is_alive():
                await conn.execute(Op())
                await asyncio.sleep(0)  # a direct call does not yield to the loop
    finally:
        writer.join(TIMEOUT)
    events = await received(recorder, count)
    assert [event.payload for event in events] == [str(value % 256) for value in range(count)]


async def test_unlistening_stops_the_watch(idle):
    conn, recorder = idle
    await conn.listen("bytes")
    await conn.unlisten("bytes")
    assert conn.listening == frozenset()
    conn.writer.send(b"\x01")
    # The call after unlisten() runs once the thread has asked idle_fileno() again.
    assert await conn.execute(Op(watch_state)) == (None, 0, b"\x01")
    assert recorder.events == []


async def test_a_failure_while_idle_is_reported_once_and_ends_the_watch(idle, caplog):
    conn, recorder = idle
    await conn.listen("bytes")
    conn.fail_idle = OSError("socket broke")
    conn.writer.send(b"\x01")
    events = await received(recorder, 1)
    assert events == [BackendEvent(CONNECTION_LOST, payload=events[0].payload)]
    assert "socket broke" in events[0].payload
    assert not conn.is_usable()
    await until(lambda: conn._watched is None)
    conn.writer.send(b"\x02")
    assert conn.reader.recv(16) == b"\x01\x02"  # idle_sync() failed before it read
    assert conn.idle_runs == 1
    assert len(recorder.events) == 1
    assert "failed while idle" in caplog.text


@pytest.mark.parametrize("repeat", REPEAT)
async def test_closing_while_the_thread_watches_stops_it(repeat):  # noqa: ARG001
    conn = await IdleDriver().open_connection(Profile("default"), Recorder())
    await conn.listen("bytes")
    if repeat % 2:
        await asyncio.sleep(0.001)
    await asyncio.wait_for(conn.close(), TIMEOUT)
    conn.thread.join(TIMEOUT)
    assert not conn.thread.is_alive()
    assert conn._waker is None
    assert conn._selector is None


async def test_a_direct_connection_is_watched_by_the_event_loop():
    recorder = Recorder()
    conn = await IdleDriver(direct=True).open_connection(Profile("default"), recorder)
    loop = asyncio.get_running_loop()
    try:
        await conn.listen("bytes")
        fd = conn.reader.fileno()
        conn.writer.send(b"\x07")
        await received(recorder, 1)
        assert conn.idle_threads == {threading.get_ident()}
        await conn.unlisten("bytes")
        assert not loop.remove_reader(fd)
        await conn.listen("bytes")
    finally:
        await conn.close()
    assert not loop.remove_reader(fd)
