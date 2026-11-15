"""Sessions, the current session, bound models and the transaction lookup at execution time."""

import asyncio
import copy
import gc
import importlib
import pickle
import sys
import threading
import uuid

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

import adorm

from adorm import (
    BoundModel,
    create_session,
    current_session,
    InterfaceError,
    ManyToMany,
    Model,
    ModelMeta,
    QuerySet,
    ReverseForeignKey,
    Session,
    TransactionManagementError,
)
from adorm.executor import active_transaction, bind_transaction, Runner, set_runner, Transaction
from adorm.models import set_executor_resolver
from adorm.operations import Count, Get, Operation, Save, Select

REPEAT = range(20)


class Venue(Model):
    _meta = ModelMeta("sessiontest.Venue")
    id: int | None = None
    name: str = ""


class Booking(Model):
    _meta = ModelMeta("sessiontest.Booking")
    id: int | None = None
    hall_id: int | None = None


class Hall(Model):
    _meta = ModelMeta("sessiontest.Hall")
    id: int | None = None
    bookings = ReverseForeignKey("sessiontest.Booking", column="hall_id")
    guests = ManyToMany(
        "sessiontest.Booking", through="sessiontest.Hall_guests", source_column="hall_id", target_column="booking_id"
    )


class Abstract(Model):
    name: str = ""


def unique_session(using: str = "default") -> Session:
    return create_session(using, f"test-{uuid.uuid4().hex}")


class Recorder:
    """Records ``(target, session, operation)`` and returns ``result``; ``gate`` holds every call until set."""

    def __init__(self, name: str, log: list[tuple[str, Any, Operation]], result: Any = 0) -> None:
        self.name = name
        self.log = log
        self.result = result
        self.gate: asyncio.Event | None = None
        self.entered: asyncio.Event | None = None

    async def _record(self, session: Any, operation: Operation) -> Any:
        self.log.append((self.name, session, operation))
        if self.entered is not None:
            self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        return self.result


class FakeRunner(Recorder):
    async def run(self, session: Session, operation: Operation) -> Any:
        return await self._record(session, operation)


class FakeTransaction(Recorder):
    async def execute(self, operation: Operation) -> Any:
        return await self._record(None, operation)


@pytest.fixture
def log() -> list[tuple[str, Any, Operation]]:
    return []


@pytest.fixture
def runner(log):
    """A recording runner, installed for the test."""
    fake = FakeRunner("runner", log)
    previous = set_runner(fake)
    yield fake
    set_runner(previous)


# create_session()


def test_create_session_defaults_to_the_default_pair():
    session = create_session()
    assert session.key == ("default", "default")
    assert (session.using, session.session_name) == ("default", "default")
    assert session is create_session("default", "default")


def test_create_session_returns_the_open_session_of_the_pair():
    name = f"test-{uuid.uuid4().hex}"
    first = create_session(using="reports", session_name=name)
    assert create_session("reports", name) is first
    assert create_session("default", name) is not first
    assert create_session("reports", name + "x") is not first


@pytest.mark.parametrize("value", ["", None, 5, b"default"])
def test_create_session_needs_non_empty_names(value):
    with pytest.raises(ValueError, match="using is a non-empty string"):
        create_session(value)
    with pytest.raises(ValueError, match="session_name is a non-empty string"):
        create_session("default", value)


@pytest.mark.parametrize("attempt", REPEAT)
def test_create_session_from_many_threads_gives_one_session(attempt):  # noqa: ARG001 - repeated
    name = f"test-{uuid.uuid4().hex}"
    workers = 16
    barrier = threading.Barrier(workers)

    def create() -> Session:
        barrier.wait()
        return create_session("default", name)

    with ThreadPoolExecutor(workers) as pool:
        sessions = list(pool.map(lambda _: create(), range(workers)))
    assert all(s is sessions[0] for s in sessions)


def test_session_is_immutable_and_unique_under_copy_and_pickle():
    session = unique_session()
    with pytest.raises(AttributeError, match="immutable"):
        session.using = "other"
    assert copy.copy(session) is session
    assert copy.deepcopy(session) is session
    assert pickle.loads(pickle.dumps(session)) is session
    assert repr(session) == f"Session('default', {session.session_name!r})"


def test_direct_construction_returns_the_registered_session():
    session = unique_session()
    assert Session("default", session.session_name) is session
    assert Session() is create_session()
    with pytest.raises(ValueError, match="session_name is a non-empty string"):
        Session("default", "")


def test_unreferenced_sessions_are_freed():
    from adorm.executor import sessions

    name = f"test-{uuid.uuid4().hex}"
    first_id = id(create_session("default", name))
    gc.collect()
    assert ("default", name) not in sessions._sessions
    held = create_session("default", name)
    assert create_session("default", name) is held
    assert ("default", name) in sessions._sessions
    del first_id


# The current session


def test_current_session_defaults_to_the_default_session():
    assert current_session() is create_session("default", "default")


def test_activate_sets_the_current_session_for_the_block():
    outer, inner = unique_session(), unique_session("reports")
    with outer.activate() as active:
        assert active is outer
        assert current_session() is outer
        with inner.activate():
            assert current_session() is inner
        assert current_session() is outer
    assert current_session() is create_session()


def test_activate_restores_the_session_after_an_exception():
    session = unique_session()
    with pytest.raises(RuntimeError), session.activate():
        raise RuntimeError
    assert current_session() is create_session()


async def test_tasks_inherit_the_current_session():
    session = unique_session()
    seen = {}

    async def record(name: str) -> None:
        seen[name] = current_session()

    with session.activate():
        task = asyncio.create_task(record("task"))
        await asyncio.gather(record("gather"))
        async with asyncio.TaskGroup() as group:
            group.create_task(record("group"))
    await task
    await asyncio.create_task(record("outside"))
    assert seen == {"task": session, "gather": session, "group": session, "outside": create_session()}


async def test_a_task_keeps_its_session_after_the_block_ends():
    session = unique_session()
    leave, done = asyncio.Event(), asyncio.Event()
    seen = []

    async def worker() -> None:
        await leave.wait()
        seen.append(current_session())
        done.set()

    with session.activate():
        task = asyncio.create_task(worker())
    leave.set()
    await task
    assert seen == [session]


async def test_activate_in_a_task_does_not_leak_into_its_parent():
    session = unique_session()

    async def child() -> Session:
        with session.activate():
            await asyncio.sleep(0)
            return current_session()

    assert await asyncio.create_task(child()) is session
    assert current_session() is create_session()


@pytest.mark.parametrize("attempt", REPEAT)
async def test_concurrent_tasks_bind_to_their_own_sessions(attempt, runner, log):  # noqa: ARG001 - repeated
    sessions = [unique_session() for _ in range(8)]
    bound_all = asyncio.Event()
    waiting = 0

    async def flow(session: Session) -> BoundModel:
        nonlocal waiting
        with session.activate():
            bound = Venue.objects
            waiting += 1
            if waiting == len(sessions):
                bound_all.set()
            await bound_all.wait()
            await bound.count()
            return bound

    bound = await asyncio.gather(*(flow(s) for s in sessions))
    assert [b.session for b in bound] == sessions
    assert sorted(id(s) for _, s, _ in log) == sorted(id(s) for s in sessions)


# Bound models


def test_objects_binds_to_the_current_session():
    session = unique_session()
    assert Venue.objects == create_session().bind(Venue)
    with session.activate():
        bound = Venue.objects
    assert bound.session is session
    assert bound.model is Venue
    assert bound.objects is bound
    assert Venue.objects.session is create_session()


def test_session_attribute_binds_to_that_session():
    session = unique_session()
    bound = session.Venue
    assert isinstance(bound, BoundModel)
    assert bound != BoundModel(Venue, session)
    assert session.Venue.objects is not None
    assert session.Venue.objects.session is session
    assert session.bind(Venue) == bound
    assert session.bind("sessiontest.Venue") == bound


def test_session_attribute_errors():
    session = unique_session()
    with pytest.raises(AttributeError, match="no registered model is named 'Nothing'"):
        session.Nothing  # noqa: B018
    with pytest.raises(AttributeError):
        session._private  # noqa: B018
    with pytest.raises(LookupError):
        session.bind("sessiontest.Nothing")


def test_session_attribute_with_an_ambiguous_name(request):
    class Venue(Model):  # same class name, another label
        _meta = ModelMeta("othersessiontest.Venue")
        id: int | None = None

    request.addfinalizer(lambda: adorm.models.registry.unregister("othersessiontest.Venue"))
    session = unique_session()
    with pytest.raises(AttributeError, match=r"several models \(othersessiontest.Venue, sessiontest.Venue\)"):
        session.Venue  # noqa: B018
    assert session.bind("othersessiontest.Venue").model is Venue


def test_objects_is_not_available_on_instances_or_abstract_models():
    with pytest.raises(AttributeError):
        Venue(name="Chess").objects  # noqa: B018
    with pytest.raises(AttributeError):
        Abstract.objects  # noqa: B018
    with pytest.raises(TypeError, match="abstract"):
        BoundModel(Abstract, create_session())
    with pytest.raises(TypeError, match="Model subclass"):
        BoundModel(int, create_session())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="Session"):
        BoundModel(Venue, "default")  # type: ignore[arg-type]


def test_objects_is_not_a_field():
    assert "objects" not in Venue.model_fields
    assert "objects" not in Venue.model_json_schema()["properties"]
    assert "objects" not in Venue(name="Chess").model_dump()


def test_bound_model_is_immutable_and_hashable():
    session = unique_session()
    bound = session.Venue
    with pytest.raises(AttributeError, match="immutable"):
        bound.session = create_session()
    assert hash(bound) == hash(session.bind(Venue))
    assert {bound, session.Venue} == {bound}
    assert bound != create_session().Venue
    assert repr(bound) == f"<BoundModel sessiontest.Venue on {session!r}>"


def test_bound_model_copies_and_pickles():
    session = unique_session()
    bound = session.Venue
    assert copy.copy(bound) is bound
    assert copy.deepcopy(bound) is bound
    assert copy.deepcopy({"bound": bound})["bound"] is bound
    restored = pickle.loads(pickle.dumps(bound))
    assert restored == bound
    assert restored.model is Venue
    assert restored.session is session


def test_binding_takes_no_slot(runner, log):
    session = unique_session()
    with session.activate():
        bound = Venue.objects
        queryset = bound.filter(name="Chess").order_by("name")[:5]
    assert isinstance(queryset, QuerySet)
    assert queryset.executor is session
    assert log == []
    assert runner.log == []


def test_module_level_bound_model_keeps_the_import_time_session(tmp_path, monkeypatch):
    module = f"bound_at_import_{uuid.uuid4().hex}"
    (tmp_path / f"{module}.py").write_text(
        "from tests.test_sessions import Venue\n\nclubs = Venue.objects\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    session = unique_session()
    with session.activate():
        imported = importlib.import_module(module)
    try:
        with unique_session().activate():
            assert imported.clubs.session is session
            assert Venue.objects.session is not session
    finally:
        sys.modules.pop(module, None)


@pytest.mark.usefixtures("runner")
async def test_module_level_bound_model_runs_on_the_import_time_session(log):
    session = unique_session()
    with session.activate():
        clubs = Venue.objects
    with unique_session().activate():
        await clubs.count()
    assert log[0][1] is session


# Execution: runner and transaction lookup


async def test_operations_outside_a_transaction_go_to_the_runner(runner, log):
    session = unique_session()
    runner.result = 3
    assert await session.Venue.count() == 3
    assert log == [("runner", session, Count(session.Venue.all().query))]


async def test_reading_rows_for_update_needs_a_transaction(runner, log):
    session = unique_session()
    locked = session.Venue.select_for_update()

    async def listed():
        return [v async for v in locked]

    for read in (lambda: locked.get(pk=1), locked.first, locked.last, listed):
        with pytest.raises(TransactionManagementError, match="outside of a transaction"):
            await read()
    assert log == []
    runner.result = 2
    assert await locked.count() == 2  # no row is read, so none is locked
    with bind_transaction(session, FakeTransaction("tx", log, result=[])):
        assert [v async for v in locked] == []
    assert [entry[0] for entry in log] == ["runner", "tx"]


async def test_without_a_runner_execution_raises_interface_error():
    previous = set_runner(None)
    try:
        with pytest.raises(InterfaceError, match="no runner executes operations outside a transaction"):
            await unique_session().Venue.count()
    finally:
        set_runner(previous)


def test_set_runner_checks_and_returns_the_previous_one(runner):
    with pytest.raises(TypeError, match="async run"):
        set_runner(object())  # type: ignore[arg-type]
    other = FakeRunner("other", [])
    assert set_runner(other) is runner
    assert set_runner(runner) is other
    assert isinstance(runner, Runner)


@pytest.mark.usefixtures("runner")
async def test_the_transaction_is_looked_up_when_the_operation_runs(log):
    session = unique_session()
    bound = session.Venue  # bound before the transaction exists
    queryset = bound.filter(name="Chess")
    transaction = FakeTransaction("tx", log, result=1)
    with bind_transaction(session, transaction) as bound_tx:
        assert bound_tx is transaction
        assert active_transaction(session) is transaction
        await bound.count()
        await queryset.count()
    await bound.count()
    assert [target for target, _, _ in log] == ["tx", "tx", "runner"]
    assert active_transaction(session) is None


@pytest.mark.usefixtures("runner")
async def test_a_transaction_of_another_session_is_not_used(log):
    session, other = unique_session(), unique_session()
    with bind_transaction(other, FakeTransaction("other-tx", log)):
        await session.Venue.count()
        await other.Venue.count()
    assert [(target, s) for target, s, _ in log] == [("runner", session), ("other-tx", None)]


@pytest.mark.usefixtures("runner")
async def test_same_pair_shares_the_transaction(log):
    name = f"test-{uuid.uuid4().hex}"
    with bind_transaction(create_session("default", name), FakeTransaction("tx", log)):
        await create_session("default", name).Venue.count()
    assert log[0][0] == "tx"


@pytest.mark.usefixtures("runner")
async def test_nested_bindings_restore_the_outer_transaction(log):
    session = unique_session()
    outer, inner = FakeTransaction("outer", log), FakeTransaction("inner", log)
    with bind_transaction(session, outer):
        with bind_transaction(session, inner):
            await session.Venue.count()
        await session.Venue.count()
    assert [target for target, _, _ in log] == ["inner", "outer"]


def test_bind_transaction_checks_the_transaction():
    with pytest.raises(TypeError, match="async execute"), bind_transaction(unique_session(), object()):
        pass
    assert isinstance(FakeTransaction("tx", []), Transaction)


@pytest.mark.usefixtures("runner")
async def test_current_session_operations_use_its_transaction(log):
    session = unique_session()
    with session.activate(), bind_transaction(session, FakeTransaction("tx", log)):
        await Venue.objects.count()
    with bind_transaction(session, FakeTransaction("tx2", log)):
        await Venue.objects.count()  # the default session has no transaction
    assert [target for target, _, _ in log] == ["tx", "runner"]


@pytest.mark.usefixtures("runner")
async def test_tasks_inherit_the_transaction(log):
    session = unique_session()
    bound = session.Venue
    with bind_transaction(session, FakeTransaction("tx", log)):
        task = asyncio.create_task(bound.count())
        await asyncio.gather(bound.count())
        async with asyncio.TaskGroup() as group:
            group.create_task(bound.count())
    await task
    await asyncio.create_task(bound.count())
    assert [target for target, _, _ in log] == ["tx", "tx", "tx", "runner"]


@pytest.mark.parametrize("attempt", REPEAT)
async def test_a_task_outliving_the_block_still_finds_its_transaction(attempt, runner, log):  # noqa: ARG001
    # The transaction decides what a late operation does (TransactionClosed); the lookup must not fall back to a
    # new slot.
    session = unique_session()
    transaction = FakeTransaction("tx", log)
    proceed = asyncio.Event()

    async def late() -> None:
        await proceed.wait()
        await session.Venue.count()

    with bind_transaction(session, transaction):
        task = asyncio.create_task(late())
    await session.Venue.count()
    proceed.set()
    await task
    assert sorted(target for target, _, _ in log) == ["runner", "tx"]
    assert log[-1][0] == "tx"


@pytest.mark.parametrize("attempt", REPEAT)
async def test_an_operation_in_flight_keeps_its_target(attempt, runner, log):  # noqa: ARG001 - repeated
    # The target is chosen when the operation starts; the block ending meanwhile does not move it.
    session = unique_session()
    transaction = FakeTransaction("tx", log)
    transaction.gate, transaction.entered = asyncio.Event(), asyncio.Event()
    with bind_transaction(session, transaction):
        task = asyncio.create_task(session.Venue.count())
        async with asyncio.timeout(5):
            await transaction.entered.wait()
    await session.Venue.count()
    transaction.gate.set()
    await task
    assert [target for target, _, _ in log] == ["tx", "runner"]


@pytest.mark.parametrize("attempt", REPEAT)
async def test_cancellation_while_executing_leaves_no_binding(attempt, runner, log):  # noqa: ARG001 - repeated
    session = unique_session()
    runner.gate, runner.entered = asyncio.Event(), asyncio.Event()

    async def flow() -> None:
        with session.activate(), bind_transaction(session, FakeTransaction("tx", log)):
            await create_session().Venue.count()

    after_cancel: list[tuple[Session, Any]] = []

    async def wrapper() -> None:
        # The bindings are checked in the task whose context flow() changed.
        try:
            await flow()
        except asyncio.CancelledError:
            after_cancel.append((current_session(), active_transaction(session)))
            raise

    task = asyncio.create_task(wrapper())
    async with asyncio.timeout(5):
        await runner.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert after_cancel == [(create_session(), None)]


async def test_errors_of_the_target_propagate(runner):
    async def fail(_session: Session, _operation: Operation) -> Any:
        raise adorm.OperationalError("connection lost")

    runner.run = fail
    with pytest.raises(adorm.OperationalError, match="connection lost"):
        await unique_session().Venue.count()


# Instances


async def test_instances_without_an_executor_use_the_current_session(runner, log):
    session = unique_session()
    runner.result = {"id": 4}
    club = Venue(name="Chess")
    with session.activate():
        await club.save()
    assert club._state.executor is session
    assert isinstance(log[0][2], Save)
    assert log[0][1] is session


async def test_loaded_instances_keep_their_session_and_find_its_transaction(runner, log):
    session = unique_session()
    runner.result = {"id": 1, "name": "Chess"}
    club = await session.Venue.get(pk=1)
    assert club._state.executor is session
    with unique_session().activate(), bind_transaction(session, FakeTransaction("tx", log, result={})):
        await club.save()
    assert [(target, type(op)) for target, _, op in log] == [("runner", Get), ("tx", Save)]


async def test_related_rows_run_on_the_owner_session_and_its_transaction(runner, log):
    session = unique_session()
    runner.result = {"id": 1}
    hall = await session.Hall.get(pk=1)
    runner.result = [{"id": 5, "hall_id": 1}]
    with unique_session().activate():
        bookings = [b async for b in hall.bookings]
        assert bookings[0]._state.executor is session
        with bind_transaction(session, FakeTransaction("tx", log, result=2)):
            assert await hall.bookings.count() == 2
            assert await hall.guests.count() == 2
    assert [(target, s, type(op)) for target, s, op in log] == [
        ("runner", session, Get),
        ("runner", session, Select),
        ("tx", None, Count),
        ("tx", None, Count),
    ]


def test_the_session_resolver_is_installed():
    previous = set_executor_resolver(None)
    try:
        assert previous is not None
        session = unique_session()
        with session.activate():
            assert previous(Venue) is session
    finally:
        set_executor_resolver(previous)
