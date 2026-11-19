"""Every driver under connection options and ``adorm.timeout()``: a declared option limits a blocked operation in the
database and ends with its block, also inside transactions and savepoints; an undeclared one is refused before a slot
is taken; an expired timeout reports the phase and leaves the pool usable."""

import asyncio
import contextvars

import asyncpg
import pytest

import adorm

from adorm import (
    cancellation_outcome,
    CancellationOutcome,
    NotSupportedError,
    OperationalError,
    OperationTimeout,
)
from tests.clubmodels.clubs import Club, Member

pytestmark = pytest.mark.django_db(transaction=True)

REPEAT = range(3)
LIMIT = 0.2
LOCK_TIMEOUT = "55P03"
QUERY_CANCELED = "57014"


def declares(driver, option):
    if option not in driver.capabilities.connection_options:
        pytest.skip(f"the {driver.name} driver does not declare {option}")


async def blocked_fails(blocker, **options):
    """The error of a blocked operation run with ``options``."""
    with adorm.connection_options(**options), pytest.raises(OperationalError) as raised:
        await blocker.blocked("koper-sailing")
    return raised.value


async def waits_for_the_hold(blocker):
    """Run a blocked operation without options: it still waits after twice the limit the tests set, and succeeds
    once the hold is released."""
    task = asyncio.create_task(blocker.blocked("koper-sailing"))
    try:
        await blocker.waiting()
        await asyncio.sleep(LIMIT * 2)
        assert not task.done(), task
        await blocker.waiting()
    finally:
        await blocker.release()
    return await task


@pytest.mark.parametrize(
    ("option", "sqlstate"), [("lock_timeout", LOCK_TIMEOUT), ("statement_timeout", QUERY_CANCELED)]
)
async def test_an_option_limits_a_blocked_operation_and_ends_with_its_block(
    driver, make_router, blocker, option, sqlstate
):
    declares(driver, option)
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]
    for _ in REPEAT:
        await blocker.hold()
        error = await blocked_fails(blocker, **{option: LIMIT})
        assert error.sqlstate == sqlstate
        await waits_for_the_hold(blocker)
        assert (pool.size, pool.in_use) == (1, 0)


async def test_session_options_apply_to_every_operation(driver, make_router, blocker):
    declares(driver, "lock_timeout")
    make_router(max_connections=1, acquire_timeout=5)
    session = adorm.current_session()
    session.set_options(lock_timeout=LIMIT)
    try:
        for _ in REPEAT:
            await blocker.hold()
            assert (await blocked_fails(blocker)).sqlstate == LOCK_TIMEOUT
            with adorm.connection_options(lock_timeout=None):
                await waits_for_the_hold(blocker)
    finally:
        session.set_options()


async def test_profile_options_apply_until_a_session_or_a_block_replaces_them(driver, make_router, blocker):
    declares(driver, "lock_timeout")
    make_router(profile={"options": {"lock_timeout": LIMIT}}, max_connections=1, acquire_timeout=5)
    session = adorm.current_session()
    for _ in REPEAT:
        await blocker.hold()
        assert (await blocked_fails(blocker)).sqlstate == LOCK_TIMEOUT
        with adorm.connection_options(lock_timeout=None):
            await waits_for_the_hold(blocker)
        await blocker.hold()
        assert (await blocked_fails(blocker)).sqlstate == LOCK_TIMEOUT
        session.set_options(lock_timeout=None)
        try:
            await waits_for_the_hold(blocker)
        finally:
            session.set_options()


async def test_options_of_one_session_do_not_reach_the_next_sessions_transaction(driver, make_router):
    declares(driver, "idle_in_transaction_session_timeout")
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]
    other = adorm.create_session("default", "conformance-other")
    for _ in REPEAT:
        with adorm.connection_options(idle_in_transaction_session_timeout=LIMIT):
            assert await Club.objects.count() == 3
        with other.activate():
            async with adorm.atomic():
                await asyncio.sleep(LIMIT * 3)
                assert await Member.objects.count() == 12
        assert (pool.size, pool.in_use) == (1, 0)


async def test_local_options_end_with_a_rolled_back_savepoint(driver, make_router, blocker):
    declares(driver, "lock_timeout")
    if not driver.capabilities.savepoints:
        pytest.skip(f"the {driver.name} driver does not declare savepoints")
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]
    for _ in REPEAT:
        await blocker.hold()
        async with adorm.atomic():

            async def savepoint():
                async with adorm.atomic():
                    await blocked_fails(blocker, lock_timeout=LIMIT)

            with pytest.raises(adorm.TransactionManagementError):
                await savepoint()
            await waits_for_the_hold(blocker)
        assert (pool.size, pool.in_use) == (1, 0)


async def test_local_options_stay_after_a_released_savepoint_until_their_block_ends(driver, make_router, blocker):
    declares(driver, "lock_timeout")
    if not driver.capabilities.savepoints:
        pytest.skip(f"the {driver.name} driver does not declare savepoints")
    make_router(max_connections=1, acquire_timeout=5)
    for _ in REPEAT:
        async with adorm.atomic():
            with adorm.connection_options(lock_timeout=LIMIT):
                async with adorm.atomic():
                    assert await Member.objects.count() == 12  # the blocker's lock is on clubs
                await blocker.hold()

                async def savepoint():
                    async with adorm.atomic():
                        await blocked_fails(blocker)

                with pytest.raises(adorm.TransactionManagementError):
                    await savepoint()
            await waits_for_the_hold(blocker)


async def test_a_local_option_does_not_outlive_its_transaction(driver, make_router, blocker):
    declares(driver, "lock_timeout")
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]
    for _ in REPEAT:
        async with adorm.atomic():
            with adorm.connection_options(lock_timeout=LIMIT):
                assert await Club.objects.count() == 3
        await blocker.hold()
        await waits_for_the_hold(blocker)
        assert pool.size == 1


async def test_an_undeclared_option_is_refused_before_a_slot_is_taken(driver, make_router):
    candidates = {"statement_timeout": LIMIT, "lock_timeout": LIMIT, "work_mem": "1MB"}
    undeclared = {
        name: value for name, value in candidates.items() if name not in driver.capabilities.connection_options
    }
    assert "work_mem" in undeclared
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]
    for _ in REPEAT:
        with adorm.connection_options(**undeclared), pytest.raises(NotSupportedError) as raised:
            await Club.objects.count()
        assert raised.value.unsupported == tuple(sorted(undeclared))
    assert pool.size == 0
    with adorm.connection_options(**dict.fromkeys(undeclared)):
        assert await Club.objects.count() == 3


async def test_a_timeout_while_executing_leaves_the_pool_usable(make_router, blocker):
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]
    for _ in REPEAT:
        await blocker.hold()
        with pytest.raises(OperationTimeout) as raised:
            async with adorm.timeout(LIMIT):
                await blocker.blocked("koper-sailing")
        await blocker.release()
        error = raised.value
        assert error.phase == "executing"
        assert error.operation.kind in {"count", "update"}
        assert error.session is adorm.current_session()
        assert cancellation_outcome(error) in {CancellationOutcome.UNKNOWN, CancellationOutcome.FAILED}
        await blocker.idle()
        assert await Club.objects.count() == 3
        assert pool.in_use == 0


async def test_a_timeout_while_waiting_for_a_slot(driver, make_router):  # noqa: ARG001
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]
    holding, done = asyncio.Event(), asyncio.Event()

    async def hold():
        async with adorm.atomic():
            assert await Club.objects.count() == 3
            holding.set()
            await done.wait()

    holder = asyncio.create_task(hold())
    await holding.wait()
    try:
        for _ in REPEAT:
            with pytest.raises(OperationTimeout) as raised:
                async with adorm.timeout(LIMIT):
                    await Club.objects.filter(city="Koper").count()
            assert raised.value.phase == "waiting_for_slot"
            assert raised.value.operation.kind == "count"
            assert cancellation_outcome(raised.value) is CancellationOutcome.NOT_STARTED
            assert pool.waiting == 0
    finally:
        done.set()
        await holder
    assert await Club.objects.count() == 3


async def show(tx, *names):
    """The values of the settings ``names`` on the connection of ``tx``'s slot, read without custom SQL: on the
    ``asyncpg.Connection`` directly, on Django's psycopg connection past its execute wrappers."""
    sql = "SELECT " + ", ".join(f"current_setting('{name}', true)" for name in names)
    connection = tx.lease.slot.connection
    if isinstance(connection.target, asyncpg.Connection):
        return tuple(await connection.target.fetchrow(sql))
    return await connection.call_hook(lambda db, *_: tuple(db.connection.execute(sql).fetchone()))


async def test_options_are_set_and_reset_on_the_connection(driver, make_router):
    declares(driver, "lock_timeout")
    make_router(max_connections=1)
    for _ in REPEAT:
        with adorm.connection_options(statement_timeout=3, lock_timeout=0.25):
            assert await Club.objects.count() == 3
            async with adorm.atomic() as tx:
                assert await show(tx, "statement_timeout", "lock_timeout") == ("3s", "250ms")
                with adorm.connection_options(statement_timeout=4):
                    assert await Club.objects.count() == 3
                    assert await show(tx, "statement_timeout", "lock_timeout") == ("4s", "250ms")
                assert await Club.objects.count() == 3
                assert await show(tx, "statement_timeout", "lock_timeout") == ("3s", "250ms")
        async with adorm.atomic() as tx:
            assert await show(tx, "statement_timeout", "lock_timeout") == ("0", "0")


async def test_options_a_hook_returns_hold_for_its_lease_and_a_block_restores_them(driver, make_router):
    declares(driver, "statement_timeout")

    def on_acquire(_connection, _profile, _session):
        return {"statement_timeout": 7}

    pool = make_router(profile={"on_acquire": on_acquire}, max_connections=1).pools[0]
    for _ in REPEAT:
        async with adorm.atomic() as tx:
            assert await show(tx, "statement_timeout") == ("7s",)
            with adorm.connection_options(statement_timeout=3):
                assert await Club.objects.count() == 3
                assert await show(tx, "statement_timeout") == ("3s",)
            assert await Club.objects.count() == 3
            assert await show(tx, "statement_timeout") == ("7s",)
        with adorm.connection_options(statement_timeout=3):
            async with adorm.atomic() as tx:
                assert await show(tx, "statement_timeout") == ("3s",)
        with adorm.connection_options(statement_timeout=None):
            async with adorm.atomic() as tx:
                assert await show(tx, "statement_timeout") == ("0",)
        async with adorm.atomic() as tx:
            assert await show(tx, "statement_timeout") == ("7s",)
    (slot,) = pool._slots
    assert not slot.connection.ran_custom_sql


async def test_a_custom_option_from_a_hook_reaches_the_connection(driver, make_router):
    if not driver.capabilities.custom_options:
        pytest.skip(f"the {driver.name} driver does not declare custom options")
    user = contextvars.ContextVar("conformance_user")

    def on_acquire(_connection, _profile, _session):
        return {"adorm_test.app_user": user.get()}

    make_router(profile={"on_acquire": on_acquire}, max_connections=1)

    async def who(name):
        token = user.set(name)
        try:
            async with adorm.atomic() as tx:
                return (await show(tx, "adorm_test.app_user"))[0]
        finally:
            user.reset(token)

    for _ in REPEAT:
        assert [await who(name) for name in ("alice", "bob", None, "alice")] == ["alice", "bob", "", "alice"]


TENANT_SCHEMA = "adorm_test_tenant"


@pytest.fixture
async def tenant_schema(driver, club_data):
    """A schema with a clubs' table holding the club in Koper only; dropped afterwards."""
    declares(driver, "search_path")
    conn = await asyncpg.connect(club_data)
    try:
        await conn.execute(
            f"DROP SCHEMA IF EXISTS {TENANT_SCHEMA} CASCADE; CREATE SCHEMA {TENANT_SCHEMA}; "
            f"CREATE TABLE {TENANT_SCHEMA}.clubs_club (LIKE public.clubs_club INCLUDING ALL); "
            f"INSERT INTO {TENANT_SCHEMA}.clubs_club SELECT * FROM public.clubs_club WHERE city = 'Koper'"
        )
        yield TENANT_SCHEMA
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {TENANT_SCHEMA} CASCADE")
        await conn.close()


async def test_search_path_resolves_tables_in_its_schemas_and_ends_with_its_block(make_router, tenant_schema):
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]
    for _ in REPEAT:
        with adorm.connection_options(search_path=[tenant_schema, "public"]):
            assert await Club.objects.count() == 1
        assert await Club.objects.count() == 3
        async with adorm.atomic():
            with adorm.connection_options(search_path=(tenant_schema, "public")):
                assert await Club.objects.count() == 1
            assert await Club.objects.count() == 3
        assert await Club.objects.count() == 3
    assert (pool.size, pool.in_use) == (1, 0)


async def test_search_path_from_an_on_acquire_hook(make_router, tenant_schema):
    tenant: contextvars.ContextVar[str | None] = contextvars.ContextVar("tenant", default=None)

    def on_acquire(connection, profile, session):  # noqa: ARG001 - the hook's signature
        schema = tenant.get()
        return None if schema is None else {"search_path": (schema, "public")}

    make_router(profile={"on_acquire": on_acquire}, max_connections=1, acquire_timeout=5)
    for _ in REPEAT:
        token = tenant.set(tenant_schema)
        try:
            assert await Club.objects.count() == 1
            async with adorm.atomic():
                assert await Club.objects.count() == 1
        finally:
            tenant.reset(token)
        assert await Club.objects.count() == 3
