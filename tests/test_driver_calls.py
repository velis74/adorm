"""The ORM manager of a query, the `Call` operation, `ModelRow` and the drivers' check of declared methods."""

import asyncio
import datetime
import decimal
import json

from typing import Any

import pytest

from adorm import create_session, driver_method, Manager, Model, ModelMeta, NotSupportedError, Q, QuerySet
from adorm.drivers import Capabilities, Driver, DriverConnection
from adorm.executor import Pool, Router, set_runner
from adorm.models import registry
from adorm.operations import Call, Create, ModelRow, Operation, Select
from adorm.query import Query
from adorm.schema import MethodSchema, ModelSchema, Schema
from tests.executor_fakes import Profile

REPEAT = range(20)


class VoucherQuerySet(QuerySet):
    @driver_method
    async def close(self, when: datetime.date, *, fee: decimal.Decimal = decimal.Decimal("0")) -> int: ...


class VoucherManager(Manager):
    queryset_class = VoucherQuerySet

    @driver_method
    async def issue(self, holder: str, *tags: str, **extra: int) -> "Voucher": ...


class Voucher(Model):
    _meta = ModelMeta("calltest.Voucher")
    id: int | None = None
    holder: str = ""
    price: decimal.Decimal = decimal.Decimal("1")

    objects = VoucherManager("objects")


def roundtrip(data: Any) -> Any:
    return json.loads(json.dumps(data))


# Query.manager


def test_query_manager_is_part_of_the_value():
    query = Query("calltest.Voucher", manager="objects")
    assert query.manager == "objects"
    assert query.filter(holder="A").manager == "objects"
    assert query != Query("calltest.Voucher")
    assert "clause.manager" in set(query.constructs())
    assert "clause.manager" not in set(Query("calltest.Voucher").constructs())
    assert repr(query) == "Query('calltest.Voucher', manager='objects')"


@pytest.mark.parametrize("name", ["", "_base_manager", "two words", 1])
def test_query_manager_is_a_public_identifier(name):
    with pytest.raises(ValueError, match="is not a manager name"):
        Query("calltest.Voucher", manager=name)


def test_queries_of_different_managers_do_not_combine():
    with pytest.raises(TypeError, match="combined queries need the same manager"):
        _ = Query("calltest.Voucher", manager="objects") | Query("calltest.Voucher")


def test_query_manager_survives_encoding():
    query = Query("calltest.Voucher", manager="objects").filter(Q(holder="A") | Q(price__gt=2))
    data = Select(query).to_dict()
    assert data["query"]["manager"] == "objects"
    assert Operation.from_dict(roundtrip(data)) == Select(query)
    data["query"]["manager"] = "_hidden"
    with pytest.raises(ValueError, match="is not a manager name"):
        Operation.from_dict(data)


# Create with a manager


def test_create_with_a_manager_encodes_extra_values_by_inferred_type():
    schema = Schema([ModelSchema("calltest.Voucher", "id", {"id": int, "price": decimal.Decimal})])
    operation = Create("calltest.Voucher", {"price": "2.50", "username": "ann"}, manager="objects")
    assert "clause.manager" in set(operation.constructs())
    data = operation.to_dict(schema)
    assert data == {
        "operation": "create",
        "model": "calltest.Voucher",
        "values": {"price": {"value": "2.50"}, "username": {"type": "str", "value": "ann"}},
        "manager": "objects",
    }
    restored = Operation.from_dict(roundtrip(data), schema)
    assert restored == Create("calltest.Voucher", {"price": decimal.Decimal("2.50"), "username": "ann"}, "objects")
    with pytest.raises(ValueError, match="has no field or relation 'username'"):
        Create("calltest.Voucher", {"username": "ann"}).to_dict(schema)
    with pytest.raises(ValueError, match="is not a manager name"):
        Create("calltest.Voucher", {}, manager="_x")
    data["manager"] = 3
    with pytest.raises(ValueError, match="the manager is a name or null"):
        Operation.from_dict(data, schema)


def test_create_without_a_manager_has_no_manager_key():
    assert "manager" not in Create("calltest.Voucher", {"holder": "A"}).to_dict()


# Call


def manager_query() -> Query:
    return Query("calltest.Voucher", manager="objects")


def test_call_is_a_value():
    call = Call(manager_query().filter(holder="A"), "close", "queryset", (datetime.date(2026, 1, 2),), {"fee": 3})
    assert call == Call(
        manager_query().filter(holder="A"), "close", args=(datetime.date(2026, 1, 2),), kwargs=[("fee", 3)]
    )
    assert hash(call) == hash(
        Call(manager_query().filter(holder="A"), "close", "queryset", (datetime.date(2026, 1, 2),), {"fee": 3})
    )
    assert call.kind == "call"
    assert call.model == "calltest.Voucher"
    assert call.key == ("objects", "queryset", "close")
    assert set(call.constructs()) == {"clause.manager", "lookup.exact"}
    tags = ["a"]
    copied = Call(manager_query(), "issue", "manager", (tags,)).args[0]
    tags.append("b")
    assert copied == ["a"]


@pytest.mark.parametrize(
    ("arguments", "error", "message"),
    [
        ((Query("calltest.Voucher"), "close"), TypeError, "needs an ORM manager"),
        ((Query("calltest.Voucher"), "close", "queryset", (), (), "_x"), ValueError, "is not a manager name"),
        ((manager_query(), "close", "queryset", (), (), "other"), ValueError, "runs on the manager 'objects'"),
        ((manager_query(), "close", "queryset", (Voucher(id=1),)), TypeError, "pass its primary key"),
        ((manager_query(), "close", "queryset", (), {"v": Voucher(id=1)}), TypeError, "pass its primary key"),
        ((manager_query(), "_close"), ValueError, "is not a public method name"),
        ((manager_query(), "not a name"), ValueError, "is not a public method name"),
        ((manager_query(), "close", "model"), ValueError, "the target is 'manager' or 'queryset'"),
        ((manager_query().filter(id=1), "issue", "manager"), ValueError, "takes the manager's plain query"),
        ((manager_query(), "close", "queryset", (Q(id=1),)), TypeError, "an argument is a plain value"),
        ((manager_query(), "close", "queryset", (), {"1x": 1}), ValueError, "is not an argument name"),
        ((manager_query(), "close", "queryset", (), [("a", 1), ("a", 2)]), ValueError, "is given twice"),
        (("calltest.Voucher", "close"), TypeError, "expected a Query"),
    ],
)
def test_call_validation(arguments, error, message):
    with pytest.raises(error, match=message):
        Call(*arguments)


def test_call_with_a_manager_and_a_plain_query():
    call = Call(Query("calltest.Voucher").filter(holder="A"), "close", manager="objects")
    assert call.manager == "objects"
    assert call.key == ("objects", "queryset", "close")
    assert set(call.constructs()) == {"lookup.exact"}
    assert call != Call(manager_query().filter(holder="A"), "close")
    data = call.to_dict(registry)
    assert data["manager"] == "objects"
    assert data["query"].get("manager") is None
    assert Operation.from_dict(roundtrip(data), registry) == call
    issued = Call(Query("calltest.Voucher"), "issue", "manager", ("Ann",), manager="objects")
    assert Operation.from_dict(roundtrip(issued.to_dict(registry)), registry) == issued


def test_create_with_a_manager_refuses_model_instances():
    with pytest.raises(TypeError, match="the value of 'holder' is a plain value, not a Voucher instance"):
        Create("calltest.Voucher", {"holder": Voucher(id=2)}, manager="objects")


def test_call_encodes_arguments_by_the_method_schema():
    schema = registry
    call = Call(
        manager_query().filter(holder="A"),
        "close",
        "queryset",
        (datetime.date(2026, 1, 2),),
        {"fee": decimal.Decimal("2.5")},
    )
    data = call.to_dict(schema)
    assert data["args"] == [{"value": "2026-01-02"}]
    assert data["kwargs"] == {"fee": {"value": "2.5"}}
    assert (data["method"], data["target"]) == ("close", "queryset")
    assert Operation.from_dict(roundtrip(data), schema) == call
    issued = Call(manager_query(), "issue", "manager", ("Ann", "x", "y"), {"seats": 2})
    data = issued.to_dict(schema)
    assert data["args"] == [{"value": "Ann"}, {"value": "x"}, {"value": "y"}]
    assert data["kwargs"] == {"seats": {"value": 2}}
    assert Operation.from_dict(roundtrip(data), schema) == issued


def test_call_without_a_method_schema_infers_argument_types():
    call = Call(manager_query(), "close", "queryset", (datetime.date(2026, 1, 2),), {"fee": 3})
    data = call.to_dict()
    assert data["args"] == [{"type": "date", "value": "2026-01-02"}]
    assert data["kwargs"] == {"fee": {"type": "int", "value": 3}}
    assert Operation.from_dict(roundtrip(data)) == call
    with pytest.raises(TypeError, match="needs a type"):
        Call(manager_query(), "close", "queryset", (object(),)).to_dict()


def test_call_values_are_checked_against_the_schema_types():
    with pytest.raises(ValueError, match="is not a valid date"):
        Call(manager_query(), "close", "queryset", ("soon",)).to_dict(registry)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"args": {}}, "the arguments are a list and a mapping"),
        ({"kwargs": []}, "the arguments are a list and a mapping"),
        ({"method": "_x"}, "is not a public method name"),
        ({"target": "model"}, "the target is"),
        ({"manager": None}, "the manager is a name"),
    ],
)
def test_malformed_calls(change, message):
    data = {**Call(manager_query(), "close").to_dict(), **change}
    with pytest.raises(ValueError, match=message):
        Operation.from_dict(data)


def test_method_schema_validation():
    with pytest.raises(ValueError, match="is not a \\(manager, target, method\\) key"):
        ModelSchema("calltest.Voucher", "id", {"id": int}, methods={("objects", "close"): MethodSchema()})
    with pytest.raises(ValueError, match="the target of"):
        ModelSchema("calltest.Voucher", "id", {"id": int}, methods={("objects", "model", "close"): MethodSchema()})
    with pytest.raises(TypeError, match="is a MethodSchema"):
        ModelSchema("calltest.Voucher", "id", {"id": int}, methods={("objects", "queryset", "close"): {}})


# ModelRow


def test_model_row():
    row = ModelRow("calltest.Voucher", {"id": 1, "tags": ["a"]})
    assert row == ModelRow("calltest.Voucher", {"id": 1, "tags": ["a"]})
    assert hash(row) == hash(ModelRow("calltest.Voucher", {"id": 1, "tags": ["a"]}))
    assert row.row["id"] == 1
    with pytest.raises(TypeError):
        row.row["id"] = 2  # type: ignore[index]
    with pytest.raises(ValueError, match="is not a model label"):
        ModelRow("Voucher", {})
    with pytest.raises(TypeError, match="a row is a mapping"):
        ModelRow("calltest.Voucher", [])  # type: ignore[arg-type]


# The driver contract runs declared methods only


class CallConnection(DriverConnection):
    def __init__(self, driver: "CallDriver") -> None:
        super().__init__(driver)
        self.calls: list[Call] = []
        self.gate: asyncio.Event | None = None

    async def _execute(self, operation: Any) -> Any:
        self.calls.append(operation)
        if self.gate is not None:
            await self.gate.wait()
        if getattr(operation, "method", None) == "issue":
            return ModelRow("calltest.Voucher", {"id": len(self.calls), "holder": operation.args[0]})
        return len(self.calls)

    async def begin(self) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass

    def cancel(self) -> None:
        pass

    def is_usable(self) -> bool:
        return True

    async def close(self) -> None:
        pass


class CallDriver(Driver):
    name = "calls"
    mode = "async"
    capabilities = Capabilities(operations={"call"}, constructs={"clause.manager", "lookup.exact"})

    def __init__(self) -> None:
        self.connections: list[CallConnection] = []

    async def open_connection(self, profile: Any, emit: Any) -> CallConnection:  # noqa: ARG002
        connection = CallConnection(self)
        self.connections.append(connection)
        return connection


@pytest.fixture
async def router():
    built = Router()
    driver = CallDriver()
    pool = Pool(driver, Profile("default"), max_connections=1, acquire_timeout=5)
    built.add(pool)
    previous = set_runner(built)
    try:
        yield built, pool, driver
    finally:
        set_runner(previous)
        await built.close()


async def test_connection_refuses_methods_the_model_does_not_declare(router):
    _, pool, driver = router
    session = create_session("default", "calls-undeclared")
    with pytest.raises(NotSupportedError, match="declares no driver method 'delete_everything'"):
        await session.execute(Call(manager_query(), "delete_everything"))
    with pytest.raises(NotSupportedError, match="declares no driver method 'close' on the manager"):
        await session.execute(Call(manager_query(), "close", "manager"))
    with pytest.raises(NotSupportedError, match="no model is registered under 'calltest.Unknown'"):
        await session.execute(Call(Query("calltest.Unknown", manager="objects"), "close"))
    assert all(not connection.calls for connection in driver.connections)
    assert pool.idle == len(driver.connections)


async def test_connection_runs_declared_methods_and_the_result_becomes_an_instance(router):
    _, pool, driver = router
    session = create_session("default", "calls-declared")
    ticket = await session.Voucher.issue("Ann", "front")
    assert isinstance(ticket, Voucher)
    assert (ticket.pk, ticket.holder) == (1, "Ann")
    assert await session.Voucher.filter(holder="Ann").close(datetime.date(2026, 1, 2)) == 2
    assert [call.method for call in driver.connections[0].calls] == ["issue", "close"]
    assert pool.idle == 1


class Ledger(Model):
    """Driver methods only: the manager routes nothing through the ORM manager."""

    _meta = ModelMeta("calltest.Ledger")
    id: int | None = None
    holder: str = ""

    objects = VoucherManager(method_manager="objects")


class NativeDriver(CallDriver):
    name = "native"
    capabilities = Capabilities(operations={"count"}, constructs={"lookup.exact"})


async def test_a_driver_without_managers_runs_plain_queries_of_a_model_with_driver_methods():
    built = Router()
    driver = NativeDriver()
    built.add(Pool(driver, Profile("default"), max_connections=1, acquire_timeout=5))
    previous = set_runner(built)
    try:
        session = create_session("default", "calls-native")
        assert await session.Ledger.filter(holder="Ann").count() == 1
        with pytest.raises(NotSupportedError, match="call"):
            await session.Ledger.issue("Ann")
        with pytest.raises(NotSupportedError, match="clause.manager"):
            await session.Voucher.count()
        assert [type(op).__name__ for op in driver.connections[0].calls] == ["Count"]
    finally:
        set_runner(previous)
        await built.close()


async def test_connection_refuses_a_call_kind_that_is_not_a_call():
    class Fake:
        kind = "call"

        def constructs(self):
            return ()

    driver = CallDriver()
    connection = await driver.open_connection(Profile("default"), None)
    with pytest.raises(NotSupportedError, match="is an adorm.operations.Call, not Fake"):
        await connection.execute(Fake())
    assert connection.calls == []


@pytest.mark.parametrize("attempt", REPEAT)
async def test_driver_methods_inside_atomic_share_its_slot(router, attempt):  # noqa: ARG001
    _, pool, driver = router
    session = create_session("default", "calls-atomic")
    async with session.atomic():
        results = await asyncio.gather(
            session.Voucher.issue("A"),
            session.Voucher.filter(holder="A").close(datetime.date(2026, 1, 1)),
            session.Voucher.issue("B"),
        )
    assert len(driver.connections) == 1
    assert sorted(call.method for call in driver.connections[0].calls) == ["close", "issue", "issue"]
    assert {type(result) for result in results} == {Voucher, int}
    assert pool.idle == 1


@pytest.mark.parametrize("attempt", REPEAT)
async def test_cancelled_driver_method_returns_the_slot(router, attempt):  # noqa: ARG001
    _, pool, driver = router
    session = create_session("default", "calls-cancel")
    await session.Voucher.issue("warm")
    connection = driver.connections[0]
    connection.gate = asyncio.Event()
    task = asyncio.create_task(session.Voucher.issue("held"))
    while len(connection.calls) < 2:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    connection.gate = None
    assert (await session.Voucher.issue("after")).holder == "after"
    assert pool.size <= 1
