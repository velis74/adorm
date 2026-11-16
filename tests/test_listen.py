"""adorm.listen(): a slot held for notifications, its events, its release and its failures, on a controllable driver."""

import asyncio

import pytest

import adorm

from adorm import create_session, DeadlockError, InterfaceError, NotSupportedError
from adorm.drivers import Capabilities, check_channel
from adorm.events import BackendEvent, CallbackEvent, CONNECTION_LOST, NOTICE, NOTIFY
from adorm.executor import CloseReason, Pool, Router, set_runner
from tests.executor_fakes import FakeConnection, FakeDriver, Op, Profile

REPEAT = range(20)
GUARD = 5


class ListeningConnection(FakeConnection):
    """Listens on channels; ``listen_gate`` holds ``listen()`` and ``listen_errors`` makes it raise per channel."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.channels: list[str] = []
        self.listen_gate: asyncio.Event | None = None
        self.listen_entered = asyncio.Event()
        self.listen_errors: dict[str, BaseException] = {}

    async def _listen(self, channel):
        self.calls.append(("listen", channel))
        self.listen_entered.set()
        if self.listen_gate is not None:
            await self.listen_gate.wait()
        error = self.listen_errors.get(channel)
        if error is not None:
            raise error
        self.channels.append(channel)

    async def _unlisten(self, channel):
        self.calls.append(("unlisten", channel))
        if channel in self.channels:  # UNLISTEN of a channel not listened on does nothing
            self.channels.remove(channel)

    def notify(self, channel, payload=""):
        """Deliver a notification, as the database does, when the connection listens on ``channel``."""
        if channel in self.channels:
            self.emit(BackendEvent(NOTIFY, channel, payload))


class ListeningDriver(FakeDriver):
    name = "fake-listening"
    capabilities = Capabilities(
        operations={"op"}, backend_events={NOTIFY, NOTICE, CONNECTION_LOST}, savepoints=True, max_channel_length=63
    )

    def __init__(self):
        super().__init__()
        self.listen_gate: asyncio.Event | None = None
        self.listen_errors: dict[str, BaseException] = {}

    async def open_connection(self, profile, emit):  # noqa: ARG002
        self.attempts += 1
        connection = ListeningConnection(self, emit, len(self.connections) + 1)
        connection.listen_gate = self.listen_gate
        connection.listen_errors = self.listen_errors
        self.connections.append(connection)
        return connection


@pytest.fixture
def setup():
    """``setup(driver=None, **pool)``: a router with one pool of the driver as adorm's runner."""
    routers = []
    previous = set_runner(None)

    def make(driver=None, **options):
        driver = driver or ListeningDriver()
        options.setdefault("acquire_timeout", GUARD)
        pool = Pool(driver, Profile("default"), **options)
        router = Router([pool])
        routers.append(router)
        set_runner(router)
        return router, pool, driver

    yield make
    set_runner(previous)


async def until(predicate):
    async with asyncio.timeout(GUARD):
        while not predicate():
            await asyncio.sleep(0)


def connection_of(listener) -> ListeningConnection:
    return listener.lease.slot.connection


# Channel names


@pytest.mark.parametrize("channel", ["orders", "Orders", 'a "quoted" name', "č" * 31, "x" * 1000])
def test_a_channel_name_is_used_as_given(channel):
    assert check_channel(channel) == channel


@pytest.mark.parametrize("channel", ["", "a\x00b"])
def test_an_invalid_channel_name_is_refused(channel):
    with pytest.raises(ValueError, match="non-empty"):
        check_channel(channel)


@pytest.mark.parametrize(
    ("channel", "valid"), [("x" * 63, True), ("č" * 31, True), ("x" * 64, False), ("č" * 32, False)]
)
def test_the_driver_sets_the_longest_channel_name(channel, valid):
    capabilities = Capabilities(max_channel_length=63)
    if valid:
        assert capabilities.check_channel(channel) == channel
    else:
        with pytest.raises(ValueError, match="longer than 63 bytes"):
            capabilities.check_channel(channel)
    assert Capabilities().check_channel(channel) == channel
    with pytest.raises(ValueError, match="non-empty"):
        capabilities.check_channel("")


def test_a_channel_name_is_a_string():
    with pytest.raises(TypeError):
        check_channel(b"orders")


async def test_an_invalid_channel_is_refused_before_a_slot_is_taken(setup):
    _router, pool, driver = setup()
    with pytest.raises(ValueError, match="non-empty"):
        adorm.listen("orders", "")
    async with adorm.listen("orders") as listener:
        with pytest.raises(ValueError, match="non-empty"):
            await listener.listen("")
        with pytest.raises(ValueError, match="longer than"):
            await listener.unlisten("x" * 64)
        assert listener.channels == ("orders",)
    assert driver.attempts == 1
    assert pool.in_use == 0


# Receiving


async def test_notifications_arrive_in_order_and_other_events_go_to_the_pool(setup):
    _router, pool, _driver = setup()
    seen = []
    pool.events.subscribe(BackendEvent, seen.append, name=NOTICE)
    callbacks = []
    pool.events.subscribe(CallbackEvent, callbacks.append)
    async with adorm.listen("orders", "invoices") as listener:
        connection = connection_of(listener)
        assert connection.listening == {"orders", "invoices"}
        notice = BackendEvent(NOTICE, "WARNING", {"message": "slow"})
        connection.notify("orders", "1")
        connection.emit(notice)
        connection.emit(CallbackEvent("cache_cleared"))
        connection.notify("invoices", "2")
        connection.notify("orders", "3")
        received = [await listener.get() for _ in range(2)]
        received.append(await anext(aiter(listener)))
        assert received == [
            BackendEvent(NOTIFY, "orders", "1"),
            BackendEvent(NOTIFY, "invoices", "2"),
            BackendEvent(NOTIFY, "orders", "3"),
        ]
        assert seen == [notice]
        assert callbacks == [CallbackEvent("cache_cleared")]


async def test_listen_and_unlisten_change_the_channels(setup):
    setup()
    async with adorm.listen("orders") as listener:
        connection = connection_of(listener)
        await listener.listen("invoices", "orders", "invoices")
        assert listener.channels == ("orders", "invoices")
        assert connection.listening == {"orders", "invoices"}
        await listener.unlisten("orders", "unknown")
        assert listener.channels == ("invoices",)
        assert connection.listening == {"invoices"}
        connection.notify("orders", "lost")
        connection.notify("invoices", "kept")
        assert await listener.get() == BackendEvent(NOTIFY, "invoices", "kept")
        assert [call for call in connection.calls if call[0] != "execute"] == [
            ("listen", "orders"),
            ("listen", "invoices"),
            ("unlisten", "orders"),
        ]


async def test_a_listener_is_entered_once_and_used_only_inside_its_block(setup):
    setup()
    listener = adorm.listen("orders")
    with pytest.raises(InterfaceError, match="not open"):
        await listener.get()
    async with listener:
        assert listener.lease is not None
        with pytest.raises(InterfaceError, match="already entered"):
            await listener.__aenter__()
    assert listener.lease is None
    with pytest.raises(InterfaceError, match="not open"):
        await listener.get()
    with pytest.raises(InterfaceError, match="not open"):
        await listener.listen("invoices")
    await listener.close()


async def test_a_listener_uses_the_given_or_the_current_session(setup):
    setup()
    other = create_session("default", "listener")
    async with adorm.listen("a", session=other) as listener:
        assert listener.lease.session is other
    with create_session("default", "current").activate() as current:
        async with adorm.listen("a") as listener:
            assert listener.lease.session is current


# The slot


async def test_the_slot_closes_while_its_connection_still_listens(setup):
    _router, pool, driver = setup()
    for _ in range(3):
        async with adorm.listen("orders") as listener:
            slot = listener.lease.slot
            assert (pool.size, pool.in_use) == (1, 1)
        assert slot.close_reason is CloseReason.LISTENING
        assert (pool.size, pool.in_use) == (0, 0)
    assert driver.attempts == 3
    assert all(connection.closed for connection in driver.connections)


async def test_the_slot_returns_to_the_pool_once_nothing_is_listened_on(setup):
    _router, pool, driver = setup()
    async with adorm.listen("orders") as listener:
        await listener.unlisten("orders")
        slot = listener.lease.slot
    assert slot.close_reason is None
    assert (pool.size, pool.idle) == (1, 1)
    assert await adorm.current_session().execute(Op("after")) == "after"
    assert driver.attempts == 1


async def test_a_driver_without_notifications_is_refused_before_a_slot_is_taken(setup):
    _router, pool, driver = setup(FakeDriver())
    with pytest.raises(NotSupportedError, match="'notify'") as raised:
        async with adorm.listen("orders"):
            pass
    assert raised.value.unsupported == ("notify",)
    assert driver.attempts == 0
    assert pool.size == 0


async def test_a_failing_listen_releases_the_slot(setup):
    _router, pool, driver = setup()
    driver.listen_errors["invoices"] = adorm.ProgrammingError("refused")
    for _ in range(3):
        with pytest.raises(adorm.ProgrammingError, match="refused"):
            async with adorm.listen("orders", "invoices"):
                pytest.fail("entered")
        # "orders" was listened on, so the slot leaves the pool.
        assert (pool.size, pool.in_use) == (0, 0)
    driver.listen_errors.clear()
    async with adorm.listen("invoices") as listener:
        assert listener.channels == ("invoices",)


@pytest.mark.parametrize("repeat", REPEAT)
async def test_cancelling_the_entry_while_it_listens_leaks_no_slot(setup, repeat):  # noqa: ARG001
    _router, pool, driver = setup(max_connections=1)
    driver.listen_gate = asyncio.Event()
    entered = []

    async def enter():
        async with adorm.listen("orders"):
            entered.append(True)

    task = asyncio.create_task(enter())
    await until(lambda: driver.connections and driver.connections[-1].listen_entered.is_set())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not entered
    # The cancelled LISTEN may have reached the database: the connection counts as listening and is closed.
    assert driver.connections[-1].listening == {"orders"}
    await until(lambda: pool.size == 0)
    assert pool.in_use == 0
    driver.listen_gate = None
    assert await adorm.current_session().execute(Op("after")) == "after"


async def test_the_listeners_flow_holds_its_slot(setup):
    setup(max_connections=1)
    session = adorm.current_session()
    async with adorm.listen("orders"):
        with pytest.raises(DeadlockError) as raised:
            await session.execute(Op("needs another slot"))
        assert raised.value.held_sessions == (session,)
    assert await session.execute(Op("after")) == "after"


# Waiting and its failures


@pytest.mark.parametrize("repeat", REPEAT)
async def test_a_cancelled_wait_loses_no_notification(setup, repeat):
    setup()
    async with adorm.listen("orders") as listener:
        connection = connection_of(listener)
        waiting = asyncio.create_task(listener.get())
        if repeat % 2:
            await asyncio.sleep(0)
        # The notification arrives in the same loop iteration as the cancellation.
        connection.notify("orders", "1")
        waiting.cancel()
        try:
            first = [await waiting]
        except asyncio.CancelledError:
            first = []
        connection.notify("orders", "2")
        rest = [await listener.get() for _ in range(2 - len(first))]
        assert [event.payload for event in first + rest] == ["1", "2"]


async def test_a_lost_connection_ends_the_listener_after_the_notifications_received_before(setup):
    _router, pool, _driver = setup()
    async with adorm.listen("orders") as listener:
        connection = connection_of(listener)
        waiting = asyncio.create_task(listener.get())
        await asyncio.sleep(0)
        connection.notify("orders", "1")
        connection.notify("orders", "2")
        connection.usable = False
        connection.emit(BackendEvent(CONNECTION_LOST, payload="terminated"))
        assert (await waiting).payload == "1"
        assert (await listener.get()).payload == "2"
        with pytest.raises(InterfaceError, match="terminated"):
            await listener.get()
        with pytest.raises(InterfaceError, match="terminated"):
            await listener.get()
        with pytest.raises(InterfaceError, match="terminated"):
            await listener.listen("invoices")
        slot = listener.lease.slot
    assert slot.close_reason is CloseReason.UNUSABLE
    assert (pool.size, pool.in_use) == (0, 0)


async def test_a_closed_slot_wakes_a_waiting_listener(setup):
    _router, pool, _driver = setup()
    async with adorm.listen("orders") as listener:
        slot = listener.lease.slot
        waiting = asyncio.create_task(listener.get())
        await asyncio.sleep(0)
        await slot.close()
        with pytest.raises(InterfaceError, match="closed"):
            async with asyncio.timeout(GUARD):
                await waiting
    assert (pool.size, pool.in_use) == (0, 0)


async def test_an_iterating_listener_ends_with_the_error(setup):
    setup()
    received = []
    async with adorm.listen("orders") as listener:
        connection = connection_of(listener)
        for payload in ("1", "2"):
            connection.notify("orders", payload)
        connection.emit(BackendEvent(CONNECTION_LOST, payload="gone"))

        async def iterate():
            async for event in listener:
                received.append(event.payload)

        with pytest.raises(InterfaceError, match="gone"):
            await iterate()
    assert received == ["1", "2"]


async def test_concurrent_waiters_share_the_notifications(setup):
    setup()
    async with adorm.listen("orders") as listener:
        connection = connection_of(listener)
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(listener.get()) for _ in range(3)]
            await asyncio.sleep(0)
            for payload in "abc":
                connection.notify("orders", payload)
        assert sorted(task.result().payload for task in tasks) == ["a", "b", "c"]


@pytest.mark.parametrize("repeat", REPEAT)
async def test_a_lost_connection_ends_every_waiting_get(setup, repeat):  # noqa: ARG001
    setup()
    async with adorm.listen("orders") as listener:
        connection = connection_of(listener)
        waiters = [asyncio.create_task(listener.get()) for _ in range(3)]
        await asyncio.sleep(0)
        connection.notify("orders", "1")
        connection.usable = False
        connection.emit(BackendEvent(CONNECTION_LOST, payload="terminated"))
        async with asyncio.timeout(GUARD):
            results = await asyncio.gather(*waiters, return_exceptions=True)
        assert [r.payload for r in results if isinstance(r, BackendEvent)] == ["1"]
        errors = [r for r in results if not isinstance(r, BackendEvent)]
        assert len(errors) == 2
        assert all(isinstance(e, InterfaceError) and "terminated" in str(e) for e in errors)


@pytest.mark.parametrize("repeat", REPEAT)
async def test_leaving_the_block_ends_a_get_of_another_task(setup, repeat):  # noqa: ARG001
    _router, pool, _driver = setup()
    async with adorm.listen("orders") as listener:
        connection = connection_of(listener)
        waiters = [asyncio.create_task(listener.get()) for _ in range(2)]
        await asyncio.sleep(0)
        connection.notify("orders", "unread")
    for waiter in waiters:
        with pytest.raises(InterfaceError, match="not open"):
            async with asyncio.timeout(GUARD):
                await waiter
    assert pool.in_use == 0


@pytest.mark.parametrize("repeat", REPEAT)
async def test_closing_the_router_ends_a_waiting_get(setup, repeat):  # noqa: ARG001
    router, pool, _driver = setup()
    async with adorm.listen("orders") as listener:
        connection = connection_of(listener)
        connection.notify("orders", "1")
        waiting = asyncio.create_task(listener.get())
        if repeat % 2:
            await asyncio.sleep(0)
        await router.close()
        assert (await waiting).payload == "1"
        with pytest.raises(InterfaceError, match="pool 'default' is closed"):
            async with asyncio.timeout(GUARD):
                await listener.get()
        slot = listener.lease.slot
    assert slot.close_reason is CloseReason.POOL_CLOSED
    assert pool.in_use == 0


@pytest.mark.parametrize("repeat", REPEAT)
async def test_a_cancelled_listen_can_be_unlistened(setup, repeat):  # noqa: ARG001
    _router, pool, driver = setup()
    async with adorm.listen("orders") as listener:
        connection = connection_of(listener)
        connection.listen_gate = asyncio.Event()
        task = asyncio.create_task(listener.listen("b"))
        await until(lambda: ("listen", "b") in connection.calls)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The cancelled LISTEN may have reached the database.
        assert connection.listening == {"orders", "b"}
        assert listener.channels == ("orders", "b")
        connection.listen_gate = None
        await listener.unlisten("b", "orders")
        assert connection.listening == frozenset()
        assert listener.channels == ()
        slot = listener.lease.slot
    assert slot.close_reason is None
    assert (pool.size, pool.idle) == (1, 1)
    assert driver.attempts == 1


async def test_a_too_long_channel_is_refused_before_a_slot_is_taken(setup):
    _router, pool, driver = setup()
    with pytest.raises(ValueError, match="longer than 63 bytes"):
        async with adorm.listen("x" * 64):
            pytest.fail("entered")
    assert driver.attempts == 0
    assert pool.size == 0


async def test_the_django_driver_declares_no_backend_events_with_psycopg_2(setup, monkeypatch):
    from adorm.drivers.django import driver as django_driver, vendors

    assert django_driver.DjangoDriver().capabilities.backend_events == {NOTIFY, NOTICE, CONNECTION_LOST}
    monkeypatch.setattr(vendors, "_is_psycopg3", lambda: False)
    _router, pool, _driver = setup(django_driver.DjangoDriver(), max_connections=1)
    assert pool.driver.capabilities.backend_events == frozenset()
    with pytest.raises(NotSupportedError, match="'notify'"):
        async with adorm.listen("orders"):
            pytest.fail("entered")
    assert (pool.size, pool.in_use) == (0, 0)
