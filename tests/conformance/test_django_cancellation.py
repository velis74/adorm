"""The Django driver on every database: a cancellation that lands between two statements of one operation stops the
operation's later statements."""

import asyncio
import datetime
import threading

import pytest

import adorm

from adorm import cancellation_outcome, CancellationOutcome, ConnectionProfile, TransactionManagementError
from adorm.executor import Pool, Router, set_runner
from tests.clubmodels.clubs import Club
from tests.conformance.drivers import TrackedDjangoDriver
from tests.library_data import in_thread

pytestmark = [pytest.mark.django_db(transaction=True)]

REPEAT = range(5)
FOUNDED = datetime.date(2001, 2, 3)


@pytest.fixture
async def django_pool(club_data):  # noqa: ARG001 - seeds the database
    """A router with one pool of a `TrackedDjangoDriver` on the test database, as adorm's runner."""
    driver = TrackedDjangoDriver()
    pool = Pool(driver, ConnectionProfile("default", driver), max_connections=2, acquire_timeout=5)
    router = Router([pool])
    previous = set_runner(router)
    try:
        yield pool
    finally:
        set_runner(previous)
        await router.close()


def pause_between_statements(connection, first: str, *, before: bool = False):
    """An execute wrapper that pauses after (``before``: before) the first statement starting with ``first`` until the
    running call is cancelled (``cancel_sync()`` has returned); returns the wrapper and the event set once it
    pauses."""
    paused = threading.Event()
    cancelled = threading.Event()
    cancel_sync = connection.cancel_sync

    def cancel_and_report() -> None:
        cancel_sync()
        cancelled.set()

    connection.cancel_sync = cancel_and_report

    def pause(sql) -> None:
        if sql.lstrip().upper().startswith(first) and not paused.is_set():
            paused.set()
            assert cancelled.wait(10)

    def wrapper(execute, sql, params, many, context):
        if before:
            pause(sql)
        result = execute(sql, params, many, context)
        if not before:
            pause(sql)
        return result

    return wrapper, paused


@pytest.mark.parametrize("repeat", REPEAT)
async def test_a_cancellation_between_the_statements_of_a_call_stops_the_next_one(django_pool, repeat):  # noqa: ARG001
    """The cancellation reaches an idle server session and is lost there; the next statement does not run."""
    pool = django_pool
    assert await Club.objects.count() == 3
    (connection,) = pool.driver.connections
    wrapper, paused = pause_between_statements(connection, "SELECT")
    connection.db.execute_wrappers.append(wrapper)
    try:
        task = asyncio.create_task(
            Club.objects.get_or_create(slug="new-club", defaults={"name": "New", "city": "Ptuj", "founded": FOUNDED})
        )
        assert await in_thread(lambda: paused.wait(10))
        task.cancel()
        async with asyncio.timeout(5):
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        connection.db.execute_wrappers.remove(wrapper)
        del connection.cancel_sync
    assert (pool.size, pool.in_use) == (1, 0)
    assert await Club.objects.filter(slug="new-club").count() == 0


@pytest.mark.parametrize("repeat", REPEAT)
async def test_a_cancellation_after_the_last_write_of_a_call_reports_its_success(django_pool, repeat):  # noqa: ARG001
    """``RELEASE SAVEPOINT`` of Django's own block still runs: the cancelled call completed and reports
    `SUCCEEDED`. The nested block that ran it rolls back, and the enclosing one goes on and commits."""
    pool = django_pool
    assert await Club.objects.count() == 3
    (connection,) = pool.driver.connections
    wrapper, paused = pause_between_statements(connection, "RELEASE", before=True)
    connection.db.execute_wrappers.append(wrapper)
    try:
        async with adorm.atomic():
            with pytest.raises(TransactionManagementError):  # noqa: PT012 - the nested block's exit raises
                async with adorm.atomic():
                    task = asyncio.create_task(
                        Club.objects.get_or_create(
                            slug="new-club", defaults={"name": "New", "city": "Ptuj", "founded": FOUNDED}
                        )
                    )
                    assert await in_thread(lambda: paused.wait(10))
                    task.cancel()
                    async with asyncio.timeout(5):
                        with pytest.raises(asyncio.CancelledError) as raised:
                            await task
            assert cancellation_outcome(raised.value) is CancellationOutcome.SUCCEEDED
            await Club.objects.filter(slug="koper-sailing").update(city="Celje")
    finally:
        connection.db.execute_wrappers.remove(wrapper)
        del connection.cancel_sync
    assert (pool.size, pool.in_use) == (1, 0)
    assert await Club.objects.filter(slug="new-club").count() == 0
    assert await Club.objects.filter(city="Celje").count() == 1
