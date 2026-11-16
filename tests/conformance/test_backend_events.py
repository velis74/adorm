"""Every driver's backend events: notifications through ``adorm.listen()`` (exact channel names, order, bursts, channel
changes racing arrivals, cancelled waits, the slot's release, a full pool, a lost connection) and the server's notices
of a statement, delivered before its outcome."""

import asyncio
import contextlib

import asyncpg
import pytest

import adorm

from adorm import DeadlockError, InterfaceError
from adorm.events import BackendEvent, CONNECTION_LOST, NOTICE, NOTIFY
from adorm.executor import CloseReason
from tests.clubmodels.clubs import Club

pytestmark = pytest.mark.django_db(transaction=True)

REPEAT = range(3)
GUARD = 10
CHANNELS = ("orders", "Orders", 'a "quoted", spaced channel', "kanal-č")


def declares(driver, name):
    if name not in driver.capabilities.backend_events:
        pytest.skip(f"the {driver.name} driver does not declare the backend event {name!r}")


async def next_events(listener, count):
    async with asyncio.timeout(GUARD):
        return [await listener.get() for _ in range(count)]


# Notifications


@pytest.mark.usefixtures("router")
async def test_notifications_arrive_on_their_exact_channels_in_order(driver, notifier):
    declares(driver, NOTIFY)
    for _ in REPEAT:
        async with adorm.listen(*CHANNELS) as listener:
            for index, channel in enumerate(CHANNELS):
                await notifier.notify(channel, f"{index}", "")
            await notifier.notify("not-listened", "x")
            await notifier.notify("orders", "ü → ✓")
            events = await next_events(listener, 2 * len(CHANNELS) + 1)
        expected = [BackendEvent(NOTIFY, c, p) for i, c in enumerate(CHANNELS) for p in (f"{i}", "")]
        assert events == [*expected, BackendEvent(NOTIFY, "orders", "ü → ✓")]


@pytest.mark.usefixtures("router")
async def test_a_burst_of_notifications_arrives_complete_and_in_order(driver, notifier):
    declares(driver, NOTIFY)
    payloads = [str(n) for n in range(500)]
    async with adorm.listen("burst") as listener:
        await notifier.notify("burst", *payloads)
        events = await next_events(listener, len(payloads))
    assert [event.payload for event in events] == payloads


@pytest.mark.usefixtures("router")
async def test_unlisten_stops_a_channel_and_listen_adds_one(driver, notifier):
    declares(driver, NOTIFY)
    async with adorm.listen("orders") as listener:
        await listener.listen("invoices")
        await listener.unlisten("orders")
        await notifier.notify("orders", "dropped")
        await notifier.notify("invoices", "kept")
        assert await next_events(listener, 1) == [BackendEvent(NOTIFY, "invoices", "kept")]
        assert listener.channels == ("invoices",)


@pytest.mark.parametrize("repeat", REPEAT)
@pytest.mark.usefixtures("router")
async def test_notifications_racing_channel_changes_all_arrive(driver, notifier, repeat):  # noqa: ARG001
    declares(driver, NOTIFY)
    count = 60
    async with adorm.listen("steady") as listener:

        async def send():
            for n in range(count):
                await notifier.notify("steady", str(n))

        sender = asyncio.create_task(send())
        try:
            async with asyncio.timeout(GUARD):
                while not sender.done():
                    await listener.listen("changing")
                    await listener.unlisten("changing")
        finally:
            await sender
        events = await next_events(listener, count)
    assert [event.payload for event in events if event.channel == "steady"] == [str(n) for n in range(count)]


@pytest.mark.parametrize("repeat", REPEAT)
@pytest.mark.usefixtures("router")
async def test_a_cancelled_wait_loses_no_notification(driver, notifier, repeat):  # noqa: ARG001
    declares(driver, NOTIFY)
    async with adorm.listen("orders") as listener:
        for n in range(5):
            waiting = asyncio.create_task(listener.get())
            await notifier.notify("orders", str(n))
            waiting.cancel()
            try:
                event = await waiting
            except asyncio.CancelledError:
                event = (await next_events(listener, 1))[0]
            assert event.payload == str(n)


async def test_the_listeners_slot_leaves_the_pool_and_operations_go_on(driver, pool, notifier):
    declares(driver, NOTIFY)
    async with adorm.listen("orders") as listener:
        slot = listener.lease.slot
        assert await Club.objects.filter(slug="koper-sailing").count() == 1
    assert slot.close_reason is CloseReason.LISTENING
    assert pool.in_use == 0
    # A notification sent now reaches no connection of the pool.
    await notifier.notify("orders", "late")
    assert await Club.objects.filter(slug="koper-sailing").count() == 1
    async with adorm.listen("orders") as listener:
        await listener.unlisten("orders")
        slot = listener.lease.slot
    assert slot.close_reason is None
    assert slot in pool._slots


async def test_a_full_pool_inside_the_listener_is_a_deadlock(driver, make_router):
    declares(driver, NOTIFY)
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]
    async with adorm.listen("orders"):
        with pytest.raises(DeadlockError):
            async with asyncio.timeout(GUARD):
                await Club.objects.count()
    assert await Club.objects.filter(slug="koper-sailing").count() == 1
    assert pool.in_use == 0


async def test_a_lost_connection_ends_the_listener(driver, pool, notifier):
    declares(driver, NOTIFY)
    declares(driver, CONNECTION_LOST)
    for _ in REPEAT:
        async with adorm.listen("orders") as listener:
            slot = listener.lease.slot
            await notifier.notify("orders", "before")
            assert (await next_events(listener, 1))[0].payload == "before"
            waiters = [asyncio.create_task(listener.get()) for _ in range(2)]
            await notifier.terminate(slot.connection.backend_pid)
            async with asyncio.timeout(GUARD):
                errors = await asyncio.gather(*waiters, return_exceptions=True)
            assert all(isinstance(error, InterfaceError) and "lost" in str(error) for error in errors), errors
        assert slot.close_reason in (CloseReason.UNUSABLE, CloseReason.CONNECTION_ERROR)
        assert pool.in_use == 0
    assert await Club.objects.filter(slug="koper-sailing").count() == 1


async def test_a_connection_lost_while_not_listening_is_reported_at_most_once(driver, router, pool, notifier):
    declares(driver, CONNECTION_LOST)
    lost = []
    pool.events.subscribe(BackendEvent, lost.append, name=CONNECTION_LOST)
    session = adorm.current_session()
    for _ in REPEAT:
        lost.clear()
        async with router.slot(session) as lease:
            pid = lease.slot.connection.backend_pid
        await notifier.terminate(pid)
        # The next operation may run on the ended connection and fail with a connection error; the slot then leaves.
        with contextlib.suppress(adorm.OperationalError, InterfaceError):
            await Club.objects.filter(slug="koper-sailing").count()
        assert await Club.objects.filter(slug="koper-sailing").count() == 1
        assert len(lost) <= 1
        assert all(isinstance(event.payload, str) for event in lost)


# Notices


@pytest.fixture
async def raising_trigger(driver, club_data):
    """A trigger on clubs that sends a NOTICE and a WARNING with detail and hint for every updating statement; the
    test is skipped for a driver that does not declare `NOTICE`."""
    declares(driver, NOTICE)
    conn = await asyncpg.connect(club_data)
    await conn.execute(
        """
        CREATE FUNCTION adorm_test_notices() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE NOTICE 'clubs updated' USING DETAIL = 'statement level', HINT = 'none needed';
            RAISE WARNING 'check the clubs' USING ERRCODE = '01000';
            RETURN NULL;
        END $$;
        CREATE TRIGGER adorm_test_notices AFTER UPDATE ON clubs_club
            FOR EACH STATEMENT EXECUTE FUNCTION adorm_test_notices();
        """
    )
    try:
        yield
    finally:
        await conn.execute(
            "DROP TRIGGER IF EXISTS adorm_test_notices ON clubs_club; DROP FUNCTION IF EXISTS adorm_test_notices()"
        )
        await conn.close()


NOTICES = [
    BackendEvent(
        NOTICE,
        "NOTICE",
        {
            "severity": "NOTICE",
            "sqlstate": "00000",
            "message": "clubs updated",
            "detail": "statement level",
            "hint": "none needed",
        },
    ),
    BackendEvent(
        NOTICE,
        "WARNING",
        {"severity": "WARNING", "sqlstate": "01000", "message": "check the clubs", "detail": None, "hint": None},
    ),
]


async def test_notices_of_an_operation_reach_the_pools_subscribers(driver, router, pool, raising_trigger):  # noqa: ARG001
    declares(driver, NOTICE)
    seen = []
    pool.events.subscribe(BackendEvent, seen.append, name=NOTICE)
    for _ in REPEAT:
        seen.clear()
        assert await Club.objects.filter(slug="koper-sailing").update(city="Koper") == 1
        assert seen == NOTICES
    warnings = []
    pool.events.subscribe(BackendEvent, warnings.append, name=NOTICE, channel="WARNING")
    async with adorm.atomic():
        await Club.objects.filter(slug="koper-sailing").update(city="Koper")
    assert warnings == NOTICES[1:]


async def test_notices_arrive_before_the_outcome_of_their_request(driver, router, raising_trigger):  # noqa: ARG001
    declares(driver, NOTICE)
    from adorm.operations import Update

    operation = Update(Club.objects.filter(slug="koper-sailing").query, {"city": "Koper"})
    async with router.slot(adorm.current_session()) as lease:
        for _ in REPEAT:
            assert await lease.call("execute", operation) == 1
            assert [lease.events.get_nowait() for _ in range(lease.events.qsize())] == NOTICES


async def test_a_direct_django_connection_receives_notifications_between_calls(conformance_driver, notifier):
    if conformance_driver.name != "django":
        pytest.skip("a direct connection is the Django driver's")
    from adorm import ConnectionProfile
    from adorm.drivers.django import DjangoDriver
    from adorm.executor import Pool, Router, set_runner

    class DirectDjangoDriver(DjangoDriver):
        direct = True

    driver = DirectDjangoDriver()
    if NOTIFY not in driver.capabilities_for(ConnectionProfile("default", driver)).backend_events:
        pytest.skip("the database delivers no notifications")
    router = Router([Pool(driver, ConnectionProfile("default", driver), max_connections=1, acquire_timeout=5)])
    previous = set_runner(router)
    try:
        async with adorm.listen("orders") as listener:
            await notifier.notify("orders", "1", "2")
            assert [event.payload for event in await next_events(listener, 2)] == ["1", "2"]
            await listener.listen("invoices")
            await notifier.notify("invoices", "3")
            assert [event.payload for event in await next_events(listener, 1)] == ["3"]
    finally:
        set_runner(previous)
        await router.close()
