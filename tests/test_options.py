"""Connection options in layers, the slot's memory of what it set, and ``adorm.timeout()``."""

import asyncio
import gc
import itertools
import threading
import warnings

import pytest

import adorm

from adorm import (
    cancellation_outcome,
    CancellationOutcome,
    ConnectionProfile,
    create_session,
    DeadlockError,
    NotSupportedError,
    OperationalError,
    OperationTimeout,
    SessionTimeout,
    TransactionClosed,
    TransactionManagementError,
    TransactionOutcome,
)
from adorm.executor import Pool, Router, set_runner, Timeout
from adorm.executor.options import (
    block_options,
    check_options,
    resolve_options,
    session_options,
    set_session_options,
)
from adorm.executor.slot import Request
from tests.executor_fakes import FakeDriver, Op, OptionsDriver, Profile

REPEAT = range(10)
LONG = 30
GUARD = 5
SHORT = 0.05
_names = itertools.count()


def unique_session(using="default"):
    return create_session(using, f"opt-{next(_names)}")


async def rolled_back_savepoint(session, **options):
    """Run an operation with ``options`` in a savepoint of ``session`` that rolls back."""
    try:
        async with session.atomic():
            with adorm.connection_options(**options):
                await session.execute(Op())
            raise KeyError
    except KeyError:
        pass


async def expiring(delay, body):
    """The `OperationTimeout` of ``adorm.timeout(delay)`` (or of ``delay``, a `Timeout`) around ``await body()``, with
    the block."""
    scope = delay if isinstance(delay, Timeout) else adorm.timeout(delay)
    with pytest.raises(OperationTimeout) as raised:
        async with scope:
            await body()
    return raised.value, scope


async def until(predicate):
    """Yield to the loop until ``predicate()`` holds."""
    async with asyncio.timeout(GUARD):
        while not predicate():
            await asyncio.sleep(0)


class Setup:
    def __init__(self):
        self.router = Router()

    def pool(self, *, driver=None, options=None, max_connections=0, acquire_timeout=LONG, commit_timeout=None, **hooks):
        driver = OptionsDriver() if driver is None else driver
        pool = Pool(
            driver,
            Profile("default", options=options or {}, **hooks),
            max_connections=max_connections,
            acquire_timeout=acquire_timeout,
            commit_timeout=commit_timeout,
        )
        self.router.add(pool)
        return pool, driver


@pytest.fixture
async def setup():
    built = Setup()
    previous = set_runner(built.router)
    try:
        yield built
    finally:
        set_runner(previous)
        await built.router.close()


async def hold_slot(session, holding, done):
    """Hold a slot of ``session`` in a transaction until ``done`` is set."""
    async with session.atomic():
        holding.set()
        await done.wait()


# Checking


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("acquire_timeout", 0),
        ("acquire_timeout", -1),
        ("acquire_timeout", float("inf")),
        ("acquire_timeout", True),
        ("acquire_timeout", None),
        ("acquire_timeout", "5"),
        ("commit_timeout", 0),
        ("commit_timeout", float("nan")),
        ("statement_timeout", -1),
        ("statement_timeout", float("inf")),
        ("statement_timeout", float("nan")),
        ("lock_timeout", False),
        ("lock_timeout", "2s"),
        ("idle_in_transaction_session_timeout", -0.5),
        ("statement_timeout", 2147483.648),
        ("lock_timeout", 3e6),
    ],
)
def test_invalid_values_are_refused(name, value):
    with pytest.raises(ValueError, match=name):
        check_options({name: value})
    with pytest.raises(ValueError, match=name), adorm.connection_options(**{name: value}):
        pass


def test_valid_values_are_kept():
    checked = check_options(
        {"acquire_timeout": 2, "commit_timeout": None, "statement_timeout": 0, "lock_timeout": None, "work_mem": "1MB"}
    )
    assert check_options({"lock_timeout": 2147483.647}) == {"lock_timeout": 2147483.647}
    assert checked == {
        "acquire_timeout": 2.0,
        "commit_timeout": None,
        "statement_timeout": 0,
        "lock_timeout": None,
        "work_mem": "1MB",
    }
    with pytest.raises(TypeError):
        checked["work_mem"] = "2MB"


@pytest.mark.parametrize("name", ["", "1x", "a-b", 3])
def test_option_names_are_identifiers(name):
    with pytest.raises(ValueError, match="identifier"):
        check_options({name: 1})


# Layers


def test_blocks_nest_and_restore():
    assert block_options() == {}
    with adorm.connection_options(statement_timeout=5, lock_timeout=2) as outer:
        assert outer == {"statement_timeout": 5, "lock_timeout": 2}
        with adorm.connection_options(lock_timeout=None, work_mem="1MB") as inner:
            assert inner == {"statement_timeout": 5, "lock_timeout": None, "work_mem": "1MB"}
            assert block_options() == inner
        assert block_options() == outer
    assert block_options() == {}


def test_a_refused_block_changes_nothing():
    with adorm.connection_options(statement_timeout=5):
        with pytest.raises(ValueError, match="lock_timeout"), adorm.connection_options(lock_timeout=-1):
            pass
        assert block_options() == {"statement_timeout": 5}


async def test_tasks_inherit_the_block():
    seen = {}

    async def read(key):
        seen[key] = dict(block_options())

    outside = asyncio.create_task(read("outside"))
    with adorm.connection_options(statement_timeout=5):
        inside = asyncio.create_task(read("inside"))
        async with asyncio.TaskGroup() as group:
            group.create_task(read("group"))
        await asyncio.gather(read("gathered"))
    await asyncio.gather(outside, inside)
    assert seen == {
        "outside": {},
        "inside": {"statement_timeout": 5},
        "group": {"statement_timeout": 5},
        "gathered": {"statement_timeout": 5},
    }


def test_session_options_belong_to_the_pair():
    session = unique_session()
    assert session.options == {}
    assert session.set_options(statement_timeout=5, acquire_timeout=1) == {"statement_timeout": 5, "acquire_timeout": 1}
    assert session.set_options(lock_timeout=2) == {"lock_timeout": 2}
    assert session_options(session) == {"lock_timeout": 2}
    key = session.key
    del session
    gc.collect()
    again = create_session(*key)
    assert again.options == {"lock_timeout": 2}
    assert create_session(key[0], key[1] + "-other").options == {}
    assert again.set_options() == {}
    assert again.options == {}
    with pytest.raises(ValueError, match="acquire_timeout"):
        again.set_options(acquire_timeout=0)
    assert again.options == {}


def test_session_options_are_thread_safe():
    sessions = [unique_session() for _ in range(8)]
    barrier = threading.Barrier(len(sessions))

    def work(session, n):
        barrier.wait()
        for i in range(200):
            set_session_options(session, {"statement_timeout": n + i})
        session.set_options(statement_timeout=n)

    threads = [threading.Thread(target=work, args=(s, n)) for n, s in enumerate(sessions)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [s.options for s in sessions] == [{"statement_timeout": n} for n in range(len(sessions))]
    for session in sessions:
        session.set_options()


async def test_layers_resolve_last_wins():
    pool = Pool(
        OptionsDriver(),
        Profile("default", options={"statement_timeout": 30, "lock_timeout": 10}),
        acquire_timeout=7,
        commit_timeout=60,
    )
    session = unique_session()
    resolved = resolve_options(session, pool)
    assert (resolved.acquire_timeout, resolved.commit_timeout) == (7, 60)
    assert resolved.database == {"statement_timeout": 30, "lock_timeout": 10}
    session.set_options(statement_timeout=20, acquire_timeout=3)
    try:
        resolved = resolve_options(session, pool)
        assert (resolved.acquire_timeout, resolved.database) == (3, {"statement_timeout": 20, "lock_timeout": 10})
        with adorm.connection_options(statement_timeout=5, lock_timeout=None, commit_timeout=None):
            resolved = resolve_options(session, pool)
            assert (resolved.acquire_timeout, resolved.commit_timeout) == (3, None)
            assert resolved.database == {"statement_timeout": 5}
        assert resolve_options(unique_session(), pool).database == {"statement_timeout": 30, "lock_timeout": 10}
    finally:
        session.set_options()


def test_profile_options_are_checked():
    with pytest.raises(ValueError, match="pool setting"):
        ConnectionProfile("p", OptionsDriver(), options={"acquire_timeout": 5})
    with pytest.raises(ValueError, match="statement_timeout"):
        ConnectionProfile("p", OptionsDriver(), options={"statement_timeout": -5})
    with pytest.raises(NotSupportedError) as raised:
        ConnectionProfile("p", FakeDriver(), options={"statement_timeout": 5})
    assert raised.value.unsupported == ("statement_timeout",)
    with pytest.raises(NotSupportedError):
        ConnectionProfile("p", FakeDriver, options={"statement_timeout": 5})
    profile = ConnectionProfile("p", "tests.executor_fakes.FakeDriver", options={"statement_timeout": 5})
    assert profile.options == {"statement_timeout": 5}
    with pytest.raises(NotSupportedError):
        Pool(profile.get_driver(), profile)
    assert ConnectionProfile("p", FakeDriver(), options={"statement_timeout": None}).options == {
        "statement_timeout": None
    }
    assert ConnectionProfile("p", OptionsDriver()).options == {}


def test_request_options_are_frozen():
    options = {"statement_timeout": 5}
    request = Request("execute", (Op(),), options=options)
    options["lock_timeout"] = 1
    assert request.options == {"statement_timeout": 5}
    assert Request("execute").options is None
    with pytest.raises(TypeError):
        Request("execute", options=[("a", 1)])


# The slot sets what differs


async def test_the_slot_sends_set_only_on_a_difference(setup):
    pool, driver = setup.pool(max_connections=1)
    session = unique_session()
    await session.execute(Op())
    connection = driver.connections[0]
    assert connection.option_calls == []
    with adorm.connection_options(statement_timeout=5):
        for _ in REPEAT:
            await session.execute(Op())
    assert connection.option_calls == [({"statement_timeout": 5}, False)]
    with adorm.connection_options(statement_timeout=5, lock_timeout=2):
        await session.execute(Op())
    with adorm.connection_options(statement_timeout=1, lock_timeout=2):
        await session.execute(Op())
    await session.execute(Op())
    await session.execute(Op())
    assert connection.option_calls[1:] == [
        ({"lock_timeout": 2}, False),
        ({"statement_timeout": 1}, False),
        ({"lock_timeout": None, "statement_timeout": None}, False),
    ]
    assert pool.leases() == []
    assert pool._slots.pop().options == {}


async def test_profile_options_are_set_once_per_connection(setup):
    pool, driver = setup.pool(options={"statement_timeout": 30}, max_connections=2)
    session = unique_session()
    for _ in REPEAT:
        await session.execute(Op())
    gate = asyncio.Event()
    ops = [Op(gate=gate), Op(gate=gate)]
    tasks = [asyncio.create_task(session.execute(op)) for op in ops]
    await until(lambda: all(op.started.is_set() for op in ops))
    gate.set()
    await asyncio.gather(*tasks)
    assert [c.option_calls for c in driver.connections] == [[({"statement_timeout": 30}, False)]] * 2


async def test_operations_on_one_slot_from_concurrent_blocks_get_their_own_options(setup):
    _, driver = setup.pool(max_connections=1)
    session = unique_session()
    seen = []

    async def run(value):
        with adorm.connection_options(statement_timeout=value):
            await session.execute(Op(effect=lambda c: seen.append((value, dict(c.driver_options)))))

    original = driver.open_connection

    async def opening(profile, emit):
        connection = await original(profile, emit)
        connection.driver_options = {}
        set_options = connection._set_options

        async def tracking(options, local):
            await set_options(options, local)
            connection.driver_options.update(options)

        connection._set_options = tracking
        return connection

    driver.open_connection = opening
    for _ in REPEAT:
        await asyncio.gather(*(run(value) for value in (1, 2, 3, 2, 1)))
    assert all(value == options["statement_timeout"] for value, options in seen)
    assert len(seen) == 5 * len(REPEAT)


async def test_inside_a_transaction_options_are_local(setup):
    pool, driver = setup.pool(max_connections=1)
    session = unique_session()
    with adorm.connection_options(statement_timeout=5):
        await session.execute(Op())
    connection = driver.connections[0]
    async with session.atomic():
        with adorm.connection_options(statement_timeout=5):
            await session.execute(Op())
        with adorm.connection_options(statement_timeout=1):
            await session.execute(Op())
            assert pool._slots.copy().pop().options == {"statement_timeout": 1}
        with adorm.connection_options(statement_timeout=5):
            await session.execute(Op())
        await session.execute(Op())
    with adorm.connection_options(statement_timeout=5):
        await session.execute(Op())
    assert connection.option_calls == [
        ({"statement_timeout": 5}, False),
        ({"statement_timeout": None}, False),  # before BEGIN, outside the block
        ({"statement_timeout": 5}, True),
        ({"statement_timeout": 1}, True),
        ({"statement_timeout": 5}, True),
        ({"statement_timeout": None}, True),
        ({"statement_timeout": 5}, False),
    ]
    assert pool._slots.copy().pop().options == {"statement_timeout": 5}


@pytest.mark.parametrize("end", ["commit", "rollback"])
async def test_local_options_end_with_the_transaction(setup, end):
    _, driver = setup.pool(max_connections=1)
    session = unique_session()
    async with session.atomic() as tx:
        with adorm.connection_options(lock_timeout=1):
            await session.execute(Op())
        await getattr(tx, end)()
    with adorm.connection_options(lock_timeout=1):
        await session.execute(Op())
    before_end = [({"lock_timeout": None}, True)] if end == "commit" else []  # the COMMIT runs outside the block
    assert driver.connections[0].option_calls == [
        ({"lock_timeout": 1}, True),
        *before_end,
        ({"lock_timeout": 1}, False),
    ]


async def test_a_rollback_to_a_savepoint_restores_the_options(setup):
    _, driver = setup.pool(max_connections=1)
    session = unique_session()
    async with session.atomic():
        with adorm.connection_options(statement_timeout=1):
            await session.execute(Op())
            await rolled_back_savepoint(session, statement_timeout=2, lock_timeout=3)
            await session.execute(Op())
            async with session.atomic():
                with adorm.connection_options(statement_timeout=4):
                    await session.execute(Op())
            with adorm.connection_options(statement_timeout=4):
                await session.execute(Op())
    assert driver.connections[0].option_calls == [
        ({"statement_timeout": 1}, True),
        ({"lock_timeout": 3, "statement_timeout": 2}, True),
        ({"statement_timeout": 4}, True),
        ({"statement_timeout": None}, True),  # before the COMMIT, outside the block
    ]


async def test_a_failed_setting_is_sent_again(setup):
    pool, driver = setup.pool(max_connections=1)
    session = unique_session()
    await session.execute(Op())
    connection = driver.connections[0]
    connection.errors["set_options"] = OperationalError("invalid value", sqlstate="22023")
    op = Op("never")
    with adorm.connection_options(statement_timeout=5), pytest.raises(OperationalError):
        await session.execute(op)
    assert not op.started.is_set()
    assert pool._slots.copy().pop().options == {}
    del connection.errors["set_options"]
    with adorm.connection_options(statement_timeout=5):
        await session.execute(Op())
        await session.execute(Op())
    assert connection.option_calls == [({"statement_timeout": 5}, False)] * 2


async def test_a_failed_setting_inside_a_transaction_fails_the_block(setup):
    pool, driver = setup.pool(max_connections=1)
    session = unique_session()

    async def transaction():
        async with session.atomic():
            await session.execute(Op())
            driver.connections[0].errors["set_options"] = OperationalError("invalid value", sqlstate="22023")
            with adorm.connection_options(lock_timeout=1), pytest.raises(OperationalError):
                await session.execute(Op())

    with pytest.raises(TransactionManagementError):
        await transaction()
    assert ("rollback", None) in driver.connections[0].calls
    assert (pool.size, pool.in_use) == (1, 0)


@pytest.mark.parametrize("attempt", REPEAT)
async def test_cancelling_while_options_are_set_sends_them_again(setup, attempt):  # noqa: ARG001
    pool, driver = setup.pool(max_connections=1)
    session = unique_session()
    await session.execute(Op())
    connection = driver.connections[0]
    connection.gates["set_options"] = asyncio.Event()
    op = Op("never")

    async def run():
        with adorm.connection_options(statement_timeout=5):
            return await session.execute(op)

    task = asyncio.create_task(run())
    await connection.entered.setdefault("set_options", asyncio.Event()).wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    assert cancellation_outcome(raised.value) is CancellationOutcome.NOT_STARTED
    assert not op.started.is_set()
    assert (pool.size, pool.in_use) == (1, 0)
    del connection.gates["set_options"]
    with adorm.connection_options(statement_timeout=5):
        await session.execute(Op())
    assert connection.option_calls == [({"statement_timeout": 5}, False)] * 2


@pytest.mark.parametrize("attempt", REPEAT)
async def test_a_transaction_begins_with_its_own_options_not_those_the_previous_lease_left(setup, attempt):  # noqa: ARG001
    _, driver = setup.pool(max_connections=1)
    first, second = unique_session(), unique_session()
    with adorm.connection_options(statement_timeout=0.05, lock_timeout=1):
        await first.execute(Op())
    connection = driver.connections[0]
    connection.gates["begin"] = asyncio.Event()

    async def empty_transaction():
        async with second.atomic():
            pass

    task = asyncio.create_task(empty_transaction())
    await connection.entered.setdefault("begin", asyncio.Event()).wait()
    assert connection.option_calls[-1] == ({"lock_timeout": None, "statement_timeout": None}, False)
    connection.gates["begin"].set()
    await task
    assert connection.calls[-2:] == [("begin", None), ("commit", None)]
    assert len(connection.option_calls) == 2


async def test_the_commit_runs_with_the_options_around_it(setup):
    _, driver = setup.pool(max_connections=1)
    session = unique_session()
    with adorm.connection_options(statement_timeout=2):
        async with session.atomic():
            with adorm.connection_options(statement_timeout=None):
                await session.execute(Op())
    assert driver.connections[0].option_calls == [
        ({"statement_timeout": 2}, False),
        ({"statement_timeout": None}, True),
        ({"statement_timeout": 2}, True),
    ]
    assert driver.connections[0].calls[-1] == ("commit", None)


@pytest.mark.parametrize("attempt", REPEAT)
async def test_on_acquire_runs_with_the_sessions_options_and_hooks_make_the_slot_set_them_again(setup, attempt):  # noqa: ARG001
    seen = []

    def on_acquire(connection, _profile, _session):
        seen.append(list(connection.option_calls))

    _, driver = setup.pool(max_connections=1, on_acquire=on_acquire, on_release=lambda *_: None)
    session = unique_session()
    with adorm.connection_options(lock_timeout=1):
        await session.execute(Op())
        await session.execute(Op())
    await session.execute(Op())
    assert seen == [
        [({"lock_timeout": 1}, False)],
        [({"lock_timeout": 1}, False)] * 3,
        [({"lock_timeout": 1}, False)] * 4 + [({"lock_timeout": None}, False)],
    ]
    # Each hook may have changed the option, so each operation sets it again.
    assert driver.connections[0].option_calls == [({"lock_timeout": 1}, False)] * 4 + [({"lock_timeout": None}, False)]


async def test_cancelling_at_any_point_leaves_no_unawaited_call(setup):
    pool, _ = setup.pool(max_connections=1)
    session = unique_session()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for steps in range(12):
            for options in ({}, {"statement_timeout": steps + 1}):

                async def run(options=options):
                    with adorm.connection_options(**options):
                        return await session.execute(Op())

                task = asyncio.create_task(run())
                for _ in range(steps):
                    await asyncio.sleep(0)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await until(lambda: pool.in_use == 0)
        gc.collect()
    assert [str(w.message) for w in caught if "never awaited" in str(w.message)] == []


# Unsupported options


async def test_an_unsupported_option_raises_before_a_slot_is_acquired(setup):
    pool, _ = setup.pool(driver=FakeDriver(), max_connections=1)
    session = unique_session()
    holding, done = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(hold_slot(unique_session(), holding, done))
    await holding.wait()
    try:
        for _ in REPEAT:
            async with asyncio.timeout(GUARD):
                with adorm.connection_options(statement_timeout=5), pytest.raises(NotSupportedError) as raised:
                    await session.execute(Op())
            assert raised.value.unsupported == ("statement_timeout",)
            assert raised.value.driver == "fake"
            assert pool.waiting == 0
    finally:
        done.set()
        await holder
    with adorm.connection_options(statement_timeout=None):
        assert await session.execute(Op("plain")) == "plain"


async def test_an_unsupported_option_leaves_the_transaction_usable(setup):
    pool, driver = setup.pool(driver=FakeDriver(), max_connections=1)
    session = unique_session()
    try:
        async with session.atomic():
            session.set_options(lock_timeout=1)
            for _ in REPEAT:
                with pytest.raises(NotSupportedError):
                    await session.execute(Op())
            session.set_options()
            assert await session.execute(Op("after")) == "after"
    finally:
        session.set_options()
    assert driver.connections[0].finished[-1] == "commit"
    assert (pool.size, pool.in_use) == (1, 0)


async def test_an_unsupported_option_at_the_end_rolls_the_transaction_back(setup):
    pool, driver = setup.pool(driver=FakeDriver(), max_connections=1)
    session = unique_session()

    async def transaction():
        async with session.atomic():
            await session.execute(Op())
            session.set_options(lock_timeout=1)

    try:
        with pytest.raises(TransactionManagementError) as raised:
            await transaction()
    finally:
        session.set_options()
    assert isinstance(raised.value.__cause__, NotSupportedError)
    assert driver.connections[0].finished[-1] == "rollback"
    assert (pool.size, pool.in_use) == (1, 0)


# adorm's own options


async def test_acquire_timeout_from_a_block_and_a_session(setup):
    pool, _ = setup.pool(max_connections=1, acquire_timeout=LONG)
    holding, done = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(hold_slot(unique_session(), holding, done))
    await holding.wait()
    session = unique_session()
    try:
        for _ in range(3):
            async with asyncio.timeout(GUARD):
                with adorm.connection_options(acquire_timeout=SHORT), pytest.raises(SessionTimeout) as raised:
                    await session.execute(Op())
            assert raised.value.timeout == SHORT
            session.set_options(acquire_timeout=SHORT * 2)
            async with asyncio.timeout(GUARD):
                with pytest.raises(SessionTimeout) as raised:
                    await session.execute(Op())
            assert raised.value.timeout == SHORT * 2
            async with asyncio.timeout(GUARD):
                with pytest.raises(SessionTimeout) as raised:
                    async with session.atomic():
                        pass
            assert raised.value.timeout == SHORT * 2
            with adorm.connection_options(acquire_timeout=LONG):
                async with asyncio.timeout(GUARD):
                    with pytest.raises(SessionTimeout) as raised:
                        await setup.router.acquire(session, timeout=SHORT)
            assert raised.value.timeout == SHORT
            session.set_options()
        assert pool.waiting == 0
    finally:
        session.set_options()
        done.set()
        await holder


async def test_acquire_timeout_while_holding_a_slot_is_a_deadlock(setup):
    setup.pool(max_connections=1, acquire_timeout=LONG)
    outer, inner = unique_session(), unique_session()
    async with outer.atomic():
        async with asyncio.timeout(GUARD):
            with adorm.connection_options(acquire_timeout=SHORT), pytest.raises(DeadlockError) as raised:
                await inner.execute(Op())
    assert raised.value.held_sessions == (outer,)


async def test_waiting_for_a_savepoint_uses_the_acquire_timeout_in_effect(setup):
    setup.pool(max_connections=1, acquire_timeout=LONG)
    session = unique_session()
    async with session.atomic():
        opened, done = asyncio.Event(), asyncio.Event()

        async def savepoint():
            async with session.atomic():
                opened.set()
                await done.wait()

        task = asyncio.create_task(savepoint())
        await opened.wait()

        async def outer_operation():
            with adorm.connection_options(acquire_timeout=SHORT):
                await session.execute(Op())

        async with asyncio.timeout(GUARD):
            with pytest.raises(DeadlockError) as raised:
                await asyncio.create_task(outer_operation())
        assert raised.value.timeout == SHORT
        done.set()
        await task


async def test_commit_timeout_is_taken_when_the_transaction_starts(setup):
    pool, driver = setup.pool(max_connections=1, commit_timeout=None)
    session = unique_session()
    blocks = []

    async def abandoned():
        with adorm.connection_options(commit_timeout=SHORT):
            async with session.atomic() as tx:
                blocks.append(tx)
                assert tx.lease.commit_timeout == SHORT
                rolled_back = tx.lease.slot.connection.entered.setdefault("rollback", asyncio.Event())
                async with asyncio.timeout(GUARD):
                    await rolled_back.wait()
                with pytest.raises(TransactionClosed):
                    await session.execute(Op())

    for _ in range(3):
        with pytest.raises(TransactionClosed):
            await abandoned()
        assert blocks[-1].outcome is TransactionOutcome.ROLLED_BACK
        await until(lambda: pool.size == 0)
    async with session.atomic() as tx:
        with adorm.connection_options(commit_timeout=SHORT):
            assert tx.lease.commit_timeout is None
            await session.execute(Op())
    assert tx.outcome is TransactionOutcome.COMMITTED
    assert len(driver.connections) == 4


# adorm.timeout()


async def test_timeout_while_waiting_for_a_slot(setup):
    pool, _ = setup.pool(max_connections=1)
    holding, done = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(hold_slot(unique_session(), holding, done))
    await holding.wait()
    session = unique_session()
    try:
        for _ in REPEAT:
            op = Op("waits")
            error, _ = await expiring(SHORT, lambda op=op: session.execute(op))
            assert isinstance(error, TimeoutError)
            assert (error.phase, error.operation, error.session, error.timeout) == (
                "waiting_for_slot",
                op,
                session,
                SHORT,
            )
            assert cancellation_outcome(error) is CancellationOutcome.NOT_STARTED
            assert not op.started.is_set()
            assert pool.waiting == 0
    finally:
        done.set()
        await holder
    assert await session.execute(Op("later")) == "later"


async def test_timeout_while_executing(setup):
    pool, driver = setup.pool(max_connections=1)
    session = unique_session()
    for _ in REPEAT:
        op = Op("runs", gate=asyncio.Event())
        error, _ = await expiring(SHORT, lambda op=op: session.execute(op))
        assert (error.phase, error.operation, error.session) == ("executing", op, session)
        assert cancellation_outcome(error) is CancellationOutcome.UNKNOWN
        assert driver.connections[0].cancelled[-1] is op
        assert (pool.size, pool.in_use) == (1, 0)


@pytest.mark.parametrize("finishes_when_cancelled", [True, False])
async def test_timeout_reports_the_outcome_of_the_interrupted_operation(setup, finishes_when_cancelled):
    pool, driver = setup.pool(max_connections=1)
    session = unique_session()
    for steps in range(12):
        gate = asyncio.Event()
        op = Op("finishes", gate=gate, finishes_when_cancelled=finishes_when_cancelled)
        scope = adorm.timeout(LONG)

        async def expire_then_finish(op=op, gate=gate, scope=scope, steps=steps):
            await op.started.wait()
            scope.reschedule(asyncio.get_running_loop().time())
            await until(scope.expired)
            for _ in range(steps):
                await asyncio.sleep(0)
            gate.set()

        racer = asyncio.create_task(expire_then_finish())
        error, _ = await expiring(scope, lambda op=op: session.execute(op))
        await racer
        assert error.phase == "executing"
        cancelled = op in driver.connections[0].cancelled
        expected = CancellationOutcome.UNKNOWN if cancelled else CancellationOutcome.SUCCEEDED
        assert cancellation_outcome(error) is expected
        if finishes_when_cancelled:
            assert expected is CancellationOutcome.SUCCEEDED
        assert (pool.size, pool.in_use) == (1, 0)


async def test_timeout_names_the_operation_of_a_child_task(setup):
    setup.pool(max_connections=2)
    session = unique_session()
    for gather in (True, False):
        quick, slow = Op("quick"), Op("slow", gate=asyncio.Event())

        async def both(gather=gather, quick=quick, slow=slow):
            if gather:
                await asyncio.gather(session.execute(quick), session.execute(slow))
            else:
                async with asyncio.TaskGroup() as group:
                    group.create_task(session.execute(quick))
                    group.create_task(session.execute(slow))

        error, _ = await expiring(SHORT, both)
        assert (error.phase, error.operation) == ("executing", slow)
        assert cancellation_outcome(error) is CancellationOutcome.UNKNOWN


async def test_timeout_names_an_operation_still_running_in_a_detached_task(setup):
    setup.pool(max_connections=1)
    session = unique_session()
    op = Op("detached", gate=asyncio.Event())
    tasks = []

    async def detach():
        tasks.append(asyncio.create_task(session.execute(op)))
        await op.started.wait()
        await asyncio.sleep(LONG)

    error, scope = await expiring(SHORT, detach)
    assert (error.phase, error.operation) == ("executing", op)
    op.gate.set()
    assert await tasks[0] == "detached"
    assert scope._active == []


async def test_timeout_without_an_operation(setup):
    setup.pool()
    error, _ = await expiring(SHORT, lambda: asyncio.sleep(LONG))
    assert (error.phase, error.operation, error.session) == (None, None, None)
    assert str(error) == f"operation timed out after {SHORT:g} s"


async def test_timeout_that_does_not_expire_changes_nothing(setup):
    setup.pool()
    session = unique_session()
    async with adorm.timeout(LONG) as scope:
        assert await session.execute(Op("fast")) == "fast"
    assert not scope.expired()
    with pytest.raises(KeyError):
        async with adorm.timeout(LONG):
            raise KeyError
    with pytest.raises(TimeoutError) as raised:
        async with adorm.timeout(LONG):
            raise TimeoutError("own")
    assert type(raised.value) is TimeoutError
    async with adorm.timeout(None) as unlimited:
        assert unlimited.when() is None
        await session.execute(Op())
    assert scope._active == []
    assert unlimited._active == []


async def test_nested_timeouts(setup):
    setup.pool()
    session = unique_session()
    scopes = []

    async def nested(inner_delay, op):
        async with adorm.timeout(inner_delay) as inner:
            scopes.append(inner)
            await session.execute(op)

    op = Op("slow", gate=asyncio.Event())
    async with adorm.timeout(LONG) as outer:
        with pytest.raises(OperationTimeout) as raised:
            await nested(SHORT, op)
    assert scopes[-1].expired()
    assert not outer.expired()
    assert (raised.value.timeout, raised.value.operation) == (SHORT, op)
    op = Op("slower", gate=asyncio.Event())
    error, outer = await expiring(SHORT, lambda: nested(LONG, op))
    assert outer.expired()
    assert not scopes[-1].expired()
    assert (error.timeout, error.operation) == (SHORT, op)


async def test_timeout_inside_a_transaction(setup):
    pool, driver = setup.pool(max_connections=1)
    session = unique_session()
    holding, done = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(hold_slot(unique_session(), holding, done))
    await holding.wait()

    async def empty():
        async with session.atomic():
            pass

    error, _ = await expiring(SHORT, empty)
    assert (error.phase, error.operation, error.session) == ("waiting_for_slot", "begin", session)
    done.set()
    await holder
    gate = asyncio.Event()
    for connection in driver.connections:
        connection.gates["commit"] = gate
    blocks = []

    async def committing():
        async with session.atomic() as tx:
            blocks.append(tx)
            await session.execute(Op())

    error, _ = await expiring(SHORT, committing)
    assert (error.phase, error.operation) == ("executing", "commit")
    assert blocks[0].outcome is TransactionOutcome.UNKNOWN
    await until(lambda: pool.in_use == 0)


async def test_the_timeout_api():
    with pytest.raises(ValueError, match="number of seconds"):
        adorm.timeout("5")
    with pytest.raises(ValueError, match="number of seconds"):
        adorm.timeout(float("nan"))
    scope = adorm.timeout(LONG)
    assert isinstance(scope, Timeout)
    assert scope.when() is None
    assert not scope.expired()
    with pytest.raises(RuntimeError):
        scope.reschedule(1)
    assert "not entered" in repr(scope)
    loop = asyncio.get_running_loop()

    async def rescheduled():
        async with scope:
            assert scope.when() == pytest.approx(loop.time() + LONG, abs=1)
            scope.reschedule(loop.time() + SHORT)
            await asyncio.sleep(LONG)

    with pytest.raises(OperationTimeout):
        await rescheduled()
    assert scope.expired()
    assert "expired" in repr(scope)
    with pytest.raises(RuntimeError):
        async with scope:
            pass


async def test_tracking_leaves_nothing_behind(setup):
    setup.pool(max_connections=2)
    session = unique_session()
    async with adorm.timeout(LONG) as scope:
        for _ in REPEAT:
            await session.execute(Op())
            with pytest.raises(KeyError):
                await session.execute(Op(error=KeyError("x")))
            async with session.atomic():
                await asyncio.gather(*(session.execute(Op()) for _ in range(5)))
            task = asyncio.create_task(session.execute(Op(gate=asyncio.Event())))
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert scope._active == []
