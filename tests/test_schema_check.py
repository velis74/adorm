"""The schema check: schema hashes compared at a model's first use on a driver, and `adorm.check_schema()` on a
controllable fake driver."""

import asyncio
import itertools
import sys
import threading
import time

from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any

import pytest

import adorm

from adorm import (
    check_schema,
    create_session,
    Exists,
    F,
    ForeignKey,
    ManyToMany,
    Model,
    ModelMeta,
    NotSupportedError,
    OuterRef,
    SchemaCheckError,
    SchemaMismatchError,
    SchemaProblem,
    Subquery,
)
from adorm.checks import table_check
from adorm.drivers import Capabilities, operation_models
from adorm.executor import Pool, Router, set_runner
from adorm.models.registry import registry
from adorm.operations import CheckTable, Count, Select, Update
from adorm.query.query import Query
from tests.executor_fakes import FakeConnection, FakeDriver, Op, Profile

REPEAT = range(20)
GUARD = 5
_names = itertools.count()

CLUB_HASH = "c1c1c1c1c1c1c1c1"
MEMBER_HASH = "a2a2a2a2a2a2a2a2"


class Club(Model):
    _meta = ModelMeta("schemacheck.Club", schema_hash=CLUB_HASH)
    id: int | None = None
    name: str = ""
    members = ManyToMany(
        "schemacheck.Member", through="schemacheck.Club_members", source_column="club_id", target_column="member_id"
    )


class Member(Model):
    _meta = ModelMeta("schemacheck.Member", schema_hash=MEMBER_HASH, orm_names={"json_": "json"})
    id: int | None = None
    json_: str = ""
    club_id: int | None = None
    club = ForeignKey("schemacheck.Club")


class MemberProxy(Member):
    _meta = ModelMeta(
        "schemacheck.MemberProxy",
        schema_hash="b3b3b3b3b3b3b3b3",
        orm_names={"json_": "json"},
        concrete_model="schemacheck.Member",
    )


class Handwritten(Model):
    _meta = ModelMeta("schemacheck.Handwritten")
    id: int | None = None


@dataclass(eq=False)
class ModelOp(Op):
    """An `Op` on the model ``model``; ``query`` stands for its query."""

    model: str = Club._meta.label
    query: Any = None

    def nodes(self):
        return iter(()) if self.query is None else iter((self.query,))


def related(model, *paths):
    return ModelOp(model=model, query=Query(model).select_related(*paths))


class HashDriver(FakeDriver):
    """`FakeDriver` whose target has the schema hashes ``hashes`` by label; a label it lacks raises ``LookupError``.
    ``hash_calls`` counts the `schema_hash()` calls; ``hash_gate``, a ``threading.Event``, holds them; ``hash_error``
    makes them raise. ``tables`` are the `CheckTable` results by label."""

    name = "hashes"
    capabilities = Capabilities(operations={"op", "check_table"}, savepoints=True)

    def __init__(self, hashes=None) -> None:
        super().__init__()
        self.hashes = {Club._meta.label: CLUB_HASH, Member._meta.label: MEMBER_HASH} if hashes is None else hashes
        self.hash_calls: list[str] = []
        self.hash_gate: threading.Event | None = None
        self.hash_entered = threading.Event()
        self.hash_error: Exception | None = None
        self.tables: dict[str, list[dict[str, Any]]] = {}
        self.table_gate: asyncio.Event | None = None
        self.table_entered = asyncio.Event()
        self.table_errors: list[BaseException] = []

    def schema_hash(self, label: str) -> str | None:
        self.hash_calls.append(label)
        self.hash_entered.set()
        if self.hash_gate is not None:
            assert self.hash_gate.wait(GUARD)
        if self.hash_error is not None:
            raise self.hash_error
        return self.hashes[label]

    async def open_connection(self, profile, emit):
        connection = await super().open_connection(profile, emit)
        connection.__class__ = TableConnection
        return connection


class TableConnection(FakeConnection):
    async def _execute(self, operation):
        if operation.kind == "check_table":
            self.calls.append(("check_table", operation.model))
            driver = self.driver
            driver.table_entered.set()
            if driver.table_gate is not None:
                await driver.table_gate.wait()
            if driver.table_errors:
                raise driver.table_errors.pop(0)
            return [dict(problem) for problem in self.driver.tables.get(operation.model, [])]
        return await super()._execute(operation)


class NoHashDriver(FakeDriver):
    name = "no-hashes"
    capabilities = Capabilities(operations={"op"}, savepoints=True)


@pytest.fixture
async def make_router():
    routers = []

    def make(driver, *, max_connections=0, acquire_timeout=GUARD, **pool):
        router = Router(
            [
                Pool(
                    driver,
                    Profile("default"),
                    max_connections=max_connections,
                    acquire_timeout=acquire_timeout,
                    **pool,
                )
            ]
        )
        routers.append(router)
        set_runner(router)
        return router

    previous = set_runner(None)
    try:
        yield make
    finally:
        set_runner(previous)
        for router in routers:
            await router.close()


def session():
    return create_session("default", f"schema-{next(_names)}")


def mismatched(driver, label=Member._meta.label):
    driver.hashes[label] = "ffffffffffffffff"
    return driver


# The schema hash at use


async def test_a_mismatch_raises_for_the_mismatched_model_only(make_router):
    driver = mismatched(HashDriver())
    make_router(driver)
    s = session()
    assert await s.execute(ModelOp("club")) == "club"
    with pytest.raises(SchemaMismatchError) as raised:
        await s.execute(ModelOp("member", model=Member._meta.label))
    error = raised.value
    assert (error.model, error.expected, error.actual, error.driver) == (
        Member._meta.label,
        MEMBER_HASH,
        "ffffffffffffffff",
        "hashes",
    )
    assert str(error) == (
        f"the adorm model schemacheck.Member was generated with schema hash {MEMBER_HASH}, the hashes driver's model"
        " has ffffffffffffffff: run adorm generate"
    )
    assert isinstance(error, adorm.ProgrammingError)
    assert error.sqlstate is None
    assert await s.execute(ModelOp("club again")) == "club again"
    assert [call for connection in driver.connections for call in connection.calls] == [
        ("execute", "club"),
        ("execute", "club again"),
    ]


async def test_a_mismatch_is_raised_before_waiting_for_a_slot(make_router):
    driver = mismatched(HashDriver())
    router = make_router(driver, max_connections=1, acquire_timeout=60)
    pool = router.pools[0]
    held = await asyncio.create_task(router.acquire(session()))
    try:
        async with asyncio.timeout(GUARD):
            with pytest.raises(SchemaMismatchError):
                await session().execute(ModelOp(model=Member._meta.label))
        assert pool.in_use == 1
    finally:
        await held.release()
    assert pool.in_use == 0


@pytest.mark.parametrize("attempt", REPEAT)
async def test_concurrent_first_uses_compute_the_hash_once_and_leak_no_slot(make_router, attempt):  # noqa: ARG001
    driver = mismatched(HashDriver())
    router = make_router(driver, max_connections=2)
    s = session()
    labels = [Club._meta.label, Member._meta.label] * 25
    results = await asyncio.gather(
        *(s.execute(ModelOp(str(index), model=label)) for index, label in enumerate(labels)), return_exceptions=True
    )
    for index, (label, result) in enumerate(zip(labels, results, strict=True)):
        if label == Club._meta.label:
            assert result == str(index)
        else:
            assert isinstance(result, SchemaMismatchError)
    assert sorted(driver.hash_calls) == [Club._meta.label, Member._meta.label]
    assert router.pools[0].in_use == 0
    assert all(not connection.closed for connection in driver.connections)


def _racing(thread: threading.Thread) -> bool:
    """Whether ``thread`` runs `HashDriver.schema_hash()` or waits inside `Driver._target_schema_hash()`."""
    frame = sys._current_frames().get(thread.ident)
    names = []
    while frame is not None:
        names.append(frame.f_code.co_name)
        frame = frame.f_back
    return "schema_hash" in names or (bool(names) and names[0] == "_target_schema_hash")


@pytest.mark.parametrize("attempt", range(10))
def test_threads_racing_a_first_use_compute_the_hash_once(attempt):  # noqa: ARG001
    driver = mismatched(HashDriver())
    driver.hash_gate = threading.Event()
    barrier = threading.Barrier(8)
    outcomes: list[Any] = []

    def check():
        barrier.wait(GUARD)
        try:
            driver.check(ModelOp(model=Member._meta.label))
        except SchemaMismatchError as exc:
            outcomes.append(exc.actual)
        else:
            outcomes.append(None)

    threads = [threading.Thread(target=check) for _ in range(8)]
    for thread in threads:
        thread.start()
    assert driver.hash_entered.wait(GUARD)
    # Every thread is inside schema_hash() or waits to look the hash up; only then may the first one finish.
    deadline = time.monotonic() + GUARD
    while not all(_racing(thread) for thread in threads):
        assert time.monotonic() < deadline
        time.sleep(0.001)
    driver.hash_gate.set()
    for thread in threads:
        thread.join(GUARD)
    assert outcomes == ["ffffffffffffffff"] * 8
    assert driver.hash_calls == [Member._meta.label]


async def test_a_model_the_target_lacks_is_a_mismatch_and_is_looked_up_once(make_router):
    driver = HashDriver({Club._meta.label: CLUB_HASH})
    make_router(driver)
    for _ in range(3):
        with pytest.raises(SchemaMismatchError, match="the target has no model schemacheck.Member") as raised:
            await session().execute(ModelOp(model=Member._meta.label))
        assert raised.value.actual is None
    assert driver.hash_calls == [Member._meta.label]


async def test_an_error_computing_the_hash_is_reported_and_kept(make_router):
    driver = HashDriver()
    driver.hash_error = RuntimeError("composite primary keys are not supported")
    make_router(driver)
    for _ in range(2):
        with pytest.raises(SchemaMismatchError, match="RuntimeError: composite primary keys are not supported"):
            await session().execute(ModelOp())
    assert driver.hash_calls == [Club._meta.label]


async def test_unchecked_models_and_drivers(make_router):
    driver = HashDriver({})
    make_router(driver)
    s = session()
    assert await s.execute(ModelOp("hand", model=Handwritten._meta.label)) == "hand"
    assert await s.execute(ModelOp("unregistered", model="schemacheck.Unknown")) == "unregistered"
    assert await s.execute(Op("no model")) == "no model"
    assert driver.hash_calls == []
    plain = NoHashDriver()
    plain.check(ModelOp(model=Member._meta.label))


async def test_the_client_hash_is_compared_at_every_use(make_router, monkeypatch):
    driver = HashDriver()
    make_router(driver)
    s = session()
    assert await s.execute(ModelOp("before")) == "before"
    monkeypatch.setattr(Club, "_meta", replace(Club._meta, schema_hash="0000000000000000"))
    with pytest.raises(SchemaMismatchError) as raised:
        await s.execute(ModelOp())
    assert raised.value.expected == "0000000000000000"
    assert driver.hash_calls == [Club._meta.label]


async def test_select_related_targets_are_checked(make_router):
    driver = mismatched(HashDriver(), Club._meta.label)
    make_router(driver)
    s = session()
    assert await s.execute(ModelOp("plain", model=Member._meta.label)) == "plain"
    with pytest.raises(SchemaMismatchError) as raised:
        await s.execute(related(Member._meta.label, "club"))
    assert raised.value.model == Club._meta.label


def test_operation_models():
    member = Member._meta.label
    assert operation_models(Op()) == ()
    assert operation_models(ModelOp(model=member)) == (member,)
    assert operation_models(related(member, "club", "club__members", "nothing", "club__nothing__more")) == (
        member,
        Club._meta.label,
    )
    assert operation_models(related("schemacheck.Unknown", "club")) == ("schemacheck.Unknown",)
    assert operation_models(ModelOp(model=member, query=Query(member).select_related())) == (member,)
    assert operation_models(SimpleNamespace(model=member)) == (member,)


def test_operation_models_follow_every_query_and_relation_path():
    club, member = Club._meta.label, Member._meta.label
    cases = [
        Select(Member.objects.filter(club__name="x").query),
        Select(Member.objects.exclude(club__members__json="x").query),
        Count(Member.objects.annotate(n=F("club__name")).query),
        Select(Member.objects.order_by("club__name").query),
        Select(Member.objects.values("club__name").query),
        Select(Member.objects.only("club__name").query),
        Select(Member.objects.distinct("club__name").query),
        Select(Member.objects.filter(club_id__in=Club.objects.values("id")).query),
        Select(
            Member.objects.annotate(name=Subquery(Club.objects.filter(pk=OuterRef("club_id")).values("name"))).query
        ),
        Select(Member.objects.filter(Exists(Club.objects.filter(pk=OuterRef("club_id")))).query),
        Select(Member.objects.values("id").union(Club.objects.values("id")).query),
        Update(Member.objects.all().query, {"json": F("club__name")}),
    ]
    for operation in cases:
        assert operation_models(operation) == (member, club), operation
    # An OuterRef path resolves in the outer query: Club -> members (Member), not in the inner Member query.
    inner = Member.objects.filter(json=OuterRef("members__json"))
    assert operation_models(Select(Club.objects.filter(Exists(inner)).query)) == (club, member)
    assert operation_models(Select(Member.objects.filter(json="x").query)) == (member,)


async def test_a_mismatched_model_reached_through_a_filter_subquery_or_union_raises(make_router):
    driver = mismatched(HashDriver(), Club._meta.label)
    make_router(driver)
    s = session()
    for query in (
        Member.objects.filter(club__name="x").query,
        Member.objects.filter(Exists(Club.objects.filter(pk=OuterRef("club_id")))).query,
        Member.objects.values("id").union(Club.objects.values("id")).query,
    ):
        with pytest.raises(SchemaMismatchError) as raised:
            await s.execute(ModelOp(model=Member._meta.label, query=query))
        assert raised.value.model == Club._meta.label
    assert await s.execute(ModelOp("plain", model=Member._meta.label, query=Member.objects.all().query)) == "plain"
    assert driver.connections[0].calls == [("execute", "plain")]


@pytest.mark.parametrize("attempt", REPEAT)
async def test_a_mismatch_inside_a_transaction_leaves_it_usable(make_router, attempt):  # noqa: ARG001
    driver = mismatched(HashDriver())
    make_router(driver)
    s = session()
    async with s.atomic():
        release = asyncio.Event()

        async def matching():
            await release.wait()
            return await s.execute(ModelOp("club"))

        async def failing():
            try:
                return await s.execute(ModelOp("member", model=Member._meta.label))
            finally:
                release.set()

        results = await asyncio.gather(matching(), failing(), return_exceptions=True)
        assert results[0] == "club"
        assert isinstance(results[1], SchemaMismatchError)
        assert await s.execute(ModelOp("after")) == "after"
    (connection,) = driver.connections
    assert connection.calls == [("begin", None), ("execute", "club"), ("execute", "after"), ("commit", None)]
    assert not connection.closed


# check_schema()


def problem(kind, table=None, column=None, field=None, expected=None, actual=None):
    return {"kind": kind, "table": table, "column": column, "field": field, "expected": expected, "actual": actual}


async def test_check_schema_returns_nothing_when_the_tables_match(make_router):
    driver = HashDriver()
    make_router(driver)
    s = session()
    assert await check_schema([Club, Member._meta.label], session=s) == ()
    assert driver.connections[0].calls == [("check_table", Club._meta.label), ("check_table", Member._meta.label)]


async def test_check_schema_uses_the_current_session_and_every_registered_model(make_router, monkeypatch):
    driver = HashDriver()
    make_router(driver)
    monkeypatch.setattr(registry, "_models", {m._meta.label: m for m in (Club, Member, Handwritten)})
    with session().activate():
        assert await check_schema() == ()
    labels = [call[1] for call in driver.connections[0].calls]
    assert labels == [Club._meta.label, Member._meta.label, Handwritten._meta.label]


def test_table_check_names_orm_fields_and_many_to_many_relations():
    assert table_check(Member) == CheckTable(Member._meta.label, ("id", "json", "club_id"), ())
    assert table_check(Club) == CheckTable(Club._meta.label, ("id", "name"), ("members",))


async def test_check_schema_reports_problems_with_adorm_names(make_router):
    driver = HashDriver()
    driver.tables[Member._meta.label] = [
        problem("missing_column", "member", "json", "json"),
        problem("column_type", "member", "club_id", "club_id", "integer", "text"),
        problem("required_column", "member", "nickname", actual="text"),
    ]
    make_router(driver)
    problems = await check_schema([Club, Member], session=session())
    assert problems == (
        SchemaProblem(Member._meta.label, "missing_column", "member", "json", "json_"),
        SchemaProblem(Member._meta.label, "column_type", "member", "club_id", "club_id", "integer", "text"),
        SchemaProblem(Member._meta.label, "required_column", "member", "nickname", actual="text"),
    )
    assert [str(p) for p in problems] == [
        "schemacheck.Member.json_: the column member.json does not exist",
        "schemacheck.Member.club_id: the column member.club_id is text, the model expects integer",
        "schemacheck.Member: the column member.nickname (text) is NOT NULL without a default;"
        " the model does not set it",
    ]


async def test_a_mismatched_model_is_a_problem_and_its_tables_are_not_read(make_router):
    driver = mismatched(HashDriver())
    driver.hashes.pop(Club._meta.label)
    make_router(driver)
    problems = await check_schema([Club, Member, Handwritten], session=session())
    assert problems == (
        SchemaProblem(
            Club._meta.label, "schema_hash", expected=CLUB_HASH, detail="the target has no model schemacheck.Club"
        ),
        SchemaProblem(Member._meta.label, "schema_hash", expected=MEMBER_HASH, actual="ffffffffffffffff"),
    )
    assert str(problems[0]) == (
        "schemacheck.Club: cannot be compared with the driver's model: the target has no model schemacheck.Club"
    )
    assert str(problems[1]) == (
        f"schemacheck.Member: generated with schema hash {MEMBER_HASH}, the driver's model has ffffffffffffffff"
    )
    assert driver.connections[0].calls == [("check_table", Handwritten._meta.label)]


async def test_a_table_shared_by_several_models_is_reported_once(make_router):
    driver = HashDriver({**HashDriver().hashes, MemberProxy._meta.label: MemberProxy._meta.schema_hash})
    for label in (Member._meta.label, MemberProxy._meta.label):
        driver.tables[label] = [
            problem("missing_table", "member"),
            problem("unknown_field", "member", field="json"),
            problem("unknown_field", "member", field="id"),
        ]
    make_router(driver)
    problems = await check_schema([MemberProxy, Member, MemberProxy], session=session())
    assert problems == (
        SchemaProblem(MemberProxy._meta.label, "missing_table", "member"),
        SchemaProblem(MemberProxy._meta.label, "unknown_field", "member", field="json_"),
        SchemaProblem(MemberProxy._meta.label, "unknown_field", "member", field="id"),
    )
    assert len(driver.connections[0].calls) == 2


async def test_raise_on_problems(make_router):
    driver = HashDriver()
    driver.tables[Club._meta.label] = [problem("missing_table", "club")]
    make_router(driver)
    assert await check_schema([Member], session=session(), raise_on_problems=True) == ()
    with pytest.raises(SchemaCheckError) as raised:
        await check_schema([Club, Member], session=session(), raise_on_problems=True)
    assert raised.value.problems == (SchemaProblem(Club._meta.label, "missing_table", "club"),)
    assert str(raised.value) == (
        "the database does not match the models (1 problem):\n  schemacheck.Club: the table club does not exist"
    )


async def test_check_schema_runs_in_the_sessions_transaction(make_router):
    driver = HashDriver()
    make_router(driver, max_connections=1)
    s = session()
    async with s.atomic():
        assert await check_schema([Club, Member], session=s) == ()
    (connection,) = driver.connections
    assert [call[0] for call in connection.calls] == ["begin", "check_table", "check_table", "commit"]


async def test_check_schema_refuses_what_it_cannot_check(make_router):
    make_router(NoHashDriver())
    with pytest.raises(NotSupportedError, match="'check_table'"):
        await check_schema([Club], session=session())
    with pytest.raises(LookupError):
        await check_schema(["schemacheck.Unknown"], session=session())
    with pytest.raises(TypeError, match="model classes with _meta"):
        await check_schema([Model], session=session())


def test_schema_problem_kinds_and_messages():
    with pytest.raises(ValueError, match="unknown schema problem kind"):
        SchemaProblem("a.B", "missing")
    label = "a.B"
    assert str(SchemaProblem(label, "missing_table", "b")) == "a.B: the table b does not exist"
    assert str(SchemaProblem(label, "nullability", "b", "c", "c", "NOT NULL", "NULL")) == (
        "a.B.c: the column b.c is NULL, the model expects NOT NULL"
    )
    assert str(SchemaProblem(label, "unknown_field", "b", field="c")) == "a.B.c: the driver's model has no such field"
    assert str(SchemaCheckError(problems=[SchemaProblem(label, "missing_table", "b")] * 2)).startswith(
        "the database does not match the models (2 problems):"
    )


def test_registry_schema_hashes(monkeypatch):
    monkeypatch.setattr(registry, "_models", {m._meta.label: m for m in (Club, Member, Handwritten)})
    assert registry.schema_hashes() == {Club._meta.label: CLUB_HASH, Member._meta.label: MEMBER_HASH}


def test_the_client_hashes_can_be_given_to_the_check():
    driver = HashDriver()
    member = Member._meta.label
    driver.check(ModelOp(model=member), schema_hashes={member: MEMBER_HASH})
    driver.check(ModelOp(model=member), schema_hashes={})
    with pytest.raises(SchemaMismatchError) as raised:
        driver.check(ModelOp(model=member), schema_hashes={member: "0000000000000000"})
    assert (raised.value.expected, raised.value.actual) == ("0000000000000000", MEMBER_HASH)
    # A label only the client registered is checked against the driver too.
    with pytest.raises(SchemaMismatchError, match="the target has no model schemacheck.Remote"):
        driver.check(ModelOp(model="schemacheck.Remote"), schema_hashes={"schemacheck.Remote": "1111111111111111"})


# The start-up check of a pool


@pytest.fixture
def checked_models(monkeypatch):
    monkeypatch.setattr(registry, "_models", {m._meta.label: m for m in (Club, Member, Handwritten)})


def calls_of(driver, kind):
    return [call for connection in driver.connections for call in connection.calls if call[0] == kind]


ALL_CHECKED = [("check_table", label) for label in (Club._meta.label, Member._meta.label, Handwritten._meta.label)]


async def settle():
    for _ in range(20):
        await asyncio.sleep(0)


def test_check_schema_settings_are_validated():
    from adorm.settings import PoolSettings

    assert PoolSettings(check_schema="warn").as_kwargs()["check_schema"] == "warn"
    for value in (True, 0, "", "error", None):
        with pytest.raises(ValueError, match="check_schema is False, 'warn' or 'raise'"):
            PoolSettings(check_schema=value)
        with pytest.raises(ValueError, match="check_schema is False, 'warn' or 'raise'"):
            Pool(HashDriver(), Profile("default"), check_schema=value)


async def test_no_start_up_check_by_default(make_router, checked_models):  # noqa: ARG001
    driver = HashDriver()
    driver.tables[Club._meta.label] = [problem("missing_table", "club")]
    make_router(driver)
    assert await session().execute(ModelOp("op")) == "op"
    assert calls_of(driver, "check_table") == []


@pytest.mark.parametrize("attempt", REPEAT)
async def test_concurrent_first_acquisitions_wait_for_one_start_up_check(make_router, checked_models, attempt):  # noqa: ARG001
    driver = HashDriver()
    driver.table_gate = asyncio.Event()
    router = make_router(driver, max_connections=2, check_schema="warn")
    pool = router.pools[0]

    async def in_transaction(index):
        s = session()
        async with s.atomic():
            return await s.execute(ModelOp(f"tx{index}"))

    tasks = [asyncio.create_task(session().execute(ModelOp(f"op{index}"))) for index in range(10)]
    tasks += [asyncio.create_task(in_transaction(index)) for index in range(3)]
    tasks.append(asyncio.create_task(router.acquire(session())))
    await asyncio.wait_for(driver.table_entered.wait(), GUARD)
    await settle()
    assert calls_of(driver, "execute") == calls_of(driver, "begin") == []
    assert (pool.size, pool.in_use) == (1, 1)
    driver.table_gate.set()
    results = await asyncio.wait_for(asyncio.gather(*tasks), GUARD)
    await results.pop().release()
    assert results == [f"op{index}" for index in range(10)] + [f"tx{index}" for index in range(3)]
    assert calls_of(driver, "check_table") == ALL_CHECKED
    assert pool.in_use == 0
    assert await session().execute(ModelOp("later")) == "later"
    assert calls_of(driver, "check_table") == ALL_CHECKED


async def test_warn_logs_the_problems_once_and_serves_the_acquisitions(make_router, checked_models, caplog):  # noqa: ARG001
    driver = mismatched(HashDriver())
    driver.tables[Club._meta.label] = [problem("missing_table", "club")]
    make_router(driver, check_schema="warn")
    with caplog.at_level("WARNING", logger="adorm.executor"):
        assert await asyncio.gather(*(session().execute(ModelOp(str(i))) for i in range(5))) == list("01234")
        assert await session().execute(ModelOp("again")) == "again"
    records = [record for record in caplog.records if record.name == "adorm.executor"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert message.startswith("pool 'default': the database does not match the models (2 problems):")
    assert "schemacheck.Club: the table club does not exist" in message
    assert f"schemacheck.Member: generated with schema hash {MEMBER_HASH}" in message


@pytest.mark.parametrize("attempt", REPEAT)
async def test_raise_refuses_every_acquisition_while_problems_are_found(make_router, checked_models, attempt):  # noqa: ARG001
    driver = HashDriver()
    driver.tables[Club._meta.label] = [problem("missing_table", "club")]
    router = make_router(driver, max_connections=1, check_schema="raise")
    pool = router.pools[0]
    results = await asyncio.gather(*(session().execute(ModelOp(str(i))) for i in range(5)), return_exceptions=True)
    expected = (SchemaProblem(Club._meta.label, "missing_table", "club"),)
    assert all(isinstance(result, SchemaCheckError) and result.problems == expected for result in results)
    with pytest.raises(SchemaCheckError):
        async with session().atomic():
            pass
    with pytest.raises(SchemaCheckError):
        await router.acquire(session())
    assert calls_of(driver, "check_table") == ALL_CHECKED
    assert calls_of(driver, "execute") == calls_of(driver, "begin") == []
    assert (pool.size, pool.in_use) == (1, 0)


async def test_a_matching_schema_raises_nothing(make_router, checked_models):  # noqa: ARG001
    driver = HashDriver()
    make_router(driver, check_schema="raise")
    assert await session().execute(ModelOp("op")) == "op"
    assert calls_of(driver, "check_table") == ALL_CHECKED


async def test_a_failed_start_up_check_is_raised_and_run_again(make_router, checked_models):  # noqa: ARG001
    driver = HashDriver()
    driver.table_errors.append(adorm.OperationalError("the server closed the connection"))
    router = make_router(driver, check_schema="raise")
    results = await asyncio.gather(*(session().execute(ModelOp(str(i))) for i in range(3)), return_exceptions=True)
    assert all(isinstance(result, adorm.OperationalError) for result in results)
    assert router.pools[0].in_use == 0
    assert await session().execute(ModelOp("op")) == "op"
    assert calls_of(driver, "check_table") == [ALL_CHECKED[0], *ALL_CHECKED]


async def test_a_driver_without_check_table_refuses_the_start_up_check(make_router, checked_models):  # noqa: ARG001
    driver = NoHashDriver()
    router = make_router(driver, check_schema="warn")
    for _ in range(2):
        with pytest.raises(NotSupportedError, match="'check_table'"):
            await session().execute(Op("op"))
    assert router.pools[0].in_use == 0


async def test_waiting_for_the_start_up_check_times_out_and_cancels_without_a_leak(make_router, checked_models):  # noqa: ARG001
    driver = HashDriver()
    driver.table_gate = asyncio.Event()
    router = make_router(driver, max_connections=1, check_schema="raise")
    pool = router.pools[0]
    with adorm.connection_options(acquire_timeout=0.01):
        with pytest.raises(adorm.SessionTimeout, match="the schema check of pool 'default' did not finish"):
            await session().execute(ModelOp("timed out"))
    cancelled = asyncio.create_task(session().execute(ModelOp("cancelled")))
    await settle()
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert (pool.in_use, calls_of(driver, "check_table")) == (1, [ALL_CHECKED[0]])
    driver.table_gate.set()
    assert await session().execute(ModelOp("op")) == "op"
    assert calls_of(driver, "check_table") == ALL_CHECKED
    assert calls_of(driver, "execute") == [("execute", "op")]
    assert pool.in_use == 0
