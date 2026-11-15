"""The demo API in process: every endpoint, the operations it builds and the driver matrix."""

import asyncio
import datetime
import decimal

import httpx
import pytest

from adorm.drivers import Capabilities, Driver
from adorm.models import ManyToMany, registry, ReverseForeignKey
from adorm.operations import Create, Get, GetOrCreate, Operation, Save, Select, Update
from adorm.settings import get_profile
from demo.api import drivers, operations
from demo.api.main import app
from demo.models import MODELS
from demo.models.clubs import Member


class FakeDriver(Driver):
    """A driver that supports selects of plain lookups only."""

    name = "fake"
    mode = "async"
    capabilities = Capabilities(operations={"select", "get"}, constructs={"lookup.exact"})

    async def open_connection(self, profile, emit):  # noqa: ARG002 - never opened
        raise NotImplementedError


class OtherDriver(FakeDriver):
    name = "other"
    capabilities = Capabilities(operations={"select"})


FAKE = drivers.DemoDriver("fake", f"{__name__}:FakeDriver")
MISSING = drivers.DemoDriver("missing", "adorm.drivers.nonexistent:Driver")


@pytest.fixture
def matrix(monkeypatch):
    """The demo's drivers replaced by an available fake driver and one whose module does not exist."""
    monkeypatch.setattr(drivers, "DRIVERS", (MISSING, FAKE))
    monkeypatch.delenv(drivers.DRIVER_ENV, raising=False)


@pytest.fixture
async def client(matrix):  # noqa: ARG001 - the real drivers stay out of in-process tests
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://demo") as http:
            yield http


def decoded(body) -> Operation:
    """The operation of an `OperationPlan` body; checks that the encoding round-trips."""
    operation = Operation.from_dict(body["operation"], registry)
    assert operation.to_dict(registry) == body["operation"]
    assert body["constructs"] == sorted(set(operation.constructs()))
    return operation


# Driver matrix


def test_available_drivers_are_the_importable_ones(matrix):  # noqa: ARG001
    assert drivers.available() == ["fake"]


def test_selected_driver_defaults_to_the_first_available(matrix):  # noqa: ARG001
    assert drivers.selected({}) == "fake"
    assert drivers.selected({drivers.DRIVER_ENV: "fake"}) == "fake"


def test_selected_driver_without_available_drivers_is_none(monkeypatch):
    monkeypatch.setattr(drivers, "DRIVERS", (MISSING,))
    assert drivers.selected({}) is None


def test_selecting_an_unknown_or_unavailable_driver_fails(matrix):  # noqa: ARG001
    with pytest.raises(LookupError, match="unknown driver 'nope'"):
        drivers.selected({drivers.DRIVER_ENV: "nope"})
    with pytest.raises(RuntimeError, match="adorm.drivers.nonexistent does not exist"):
        drivers.selected({drivers.DRIVER_ENV: "missing"})


def test_a_driver_needing_a_missing_package_is_not_available(monkeypatch):
    def import_module(_name):
        raise ModuleNotFoundError("No module named 'asyncpg'", name="asyncpg")

    monkeypatch.setattr(drivers.importlib, "import_module", import_module)
    assert drivers.get("asyncpg").unavailable_reason() == "adorm.drivers.asyncpg needs asyncpg, which is not installed"


def test_a_driver_whose_module_lacks_the_class_is_not_available():
    driver = drivers.DemoDriver("absent", f"{__name__}:NoSuchDriver")
    assert driver.unavailable_reason() == f"{__name__} does not define NoSuchDriver"
    assert not driver.is_available()


@pytest.mark.parametrize(
    "error",
    [ModuleNotFoundError("No module named 'adorm.missing'", name="adorm.missing"), ImportError("cannot import name")],
)
def test_other_import_errors_of_a_driver_propagate(monkeypatch, error):
    def import_module(_name):
        raise error

    monkeypatch.setattr(drivers.importlib, "import_module", import_module)
    with pytest.raises(ImportError):
        drivers.available()


def test_load_runs_setup_before_instantiating():
    calls = []
    driver = drivers.DemoDriver("fake", f"{__name__}:FakeDriver", lambda: calls.append("setup"))
    assert isinstance(driver.load(), FakeDriver)
    assert calls == ["setup"]


def test_the_demo_lists_django_asyncpg_and_memory():
    assert [d.name for d in drivers.DRIVERS] == ["django", "asyncpg", "memory"]
    assert drivers.get("asyncpg").module == "adorm.drivers.asyncpg"
    assert drivers.get("memory").is_available()


async def test_drivers_endpoint_reports_availability(matrix, client):  # noqa: ARG001
    response = await client.get("/api/drivers")
    assert response.status_code == 200
    assert response.json() == [
        {
            "name": "missing",
            "target": "adorm.drivers.nonexistent:Driver",
            "available": False,
            "active": False,
            "mode": None,
            "operations": None,
        },
        {
            "name": "fake",
            "target": f"{__name__}:FakeDriver",
            "available": True,
            "active": True,
            "mode": "async",
            "operations": ["get", "select"],
        },
    ]
    health = (await client.get("/api/health")).json()
    assert health["status"] == "ok"
    assert health["driver"] == "fake"


async def test_each_available_driver_reports_what_it_does_not_support(matrix, client):  # noqa: ARG001
    body = (await client.get("/api/operations/clubs/koper-sailing/report")).json()
    assert body["unsupported"]["fake"] == sorted(set(body["constructs"]) - {"lookup.exact"})
    body = (await client.post("/api/operations/attendances/4/cancel")).json()
    assert body["unsupported"]["fake"][0] == "update"


async def test_every_available_driver_is_loaded_and_becomes_a_profile(monkeypatch):
    calls = []
    monkeypatch.setattr(
        drivers,
        "DRIVERS",
        (
            drivers.DemoDriver("fake", f"{__name__}:FakeDriver", lambda: calls.append("fake")),
            MISSING,
            drivers.DemoDriver("other", f"{__name__}:OtherDriver", lambda: calls.append("other")),
        ),
    )
    monkeypatch.setenv(drivers.DRIVER_ENV, "other")
    async with app.router.lifespan_context(app):
        assert calls == ["fake", "other"]
        assert isinstance(app.state.drivers["fake"], FakeDriver)
        assert isinstance(app.state.drivers["other"], OtherDriver)
        assert get_profile("default").get_driver() is app.state.drivers["other"]
        assert get_profile("other").get_driver() is app.state.drivers["other"]
        assert get_profile("fake").get_driver() is app.state.drivers["fake"]
        assert get_profile("missing") is None
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://demo") as http:
            body = (await http.post("/api/operations/attendances/4/cancel")).json()
            listed = (await http.get("/api/drivers")).json()
    assert [get_profile(name) for name in ("default", "fake", "other")] == [None, None, None]
    assert body["unsupported"]["fake"][0] == body["unsupported"]["other"][0] == "update"
    assert [(d["name"], d["active"], d["mode"]) for d in listed] == [
        ("fake", False, "async"),
        ("missing", False, None),
        ("other", True, "async"),
    ]


def test_a_failing_setup_of_an_inactive_driver_stops_the_startup(monkeypatch):
    def broken():
        raise RuntimeError("no database")

    monkeypatch.setattr(
        drivers,
        "DRIVERS",
        (FAKE, drivers.DemoDriver("other", f"{__name__}:OtherDriver", broken)),
    )
    monkeypatch.delenv(drivers.DRIVER_ENV, raising=False)

    async def start():
        async with app.router.lifespan_context(app):
            pass

    with pytest.raises(RuntimeError, match="no database"):
        asyncio.run(start())
    assert get_profile("default") is None


async def test_without_drivers_operations_are_built_only(monkeypatch, client):
    monkeypatch.setattr(drivers, "DRIVERS", (MISSING,))
    async with app.router.lifespan_context(app):
        assert (await client.get("/api/health")).json()["driver"] is None
        body = (await client.get("/api/operations/members")).json()
    assert body["unsupported"] == {}


# Reads


async def test_member_search(client):
    response = await client.get(
        "/api/operations/members",
        params={"club": "ljubljana-rowing", "level": "beginner", "search": "nov", "joined_after": "2025-01-01"},
    )
    assert response.status_code == 200
    operation = decoded(response.json())
    assert operation == operations.member_search(
        club="ljubljana-rowing", level="beginner", search="nov", joined_after=datetime.date(2025, 1, 1)
    )
    assert isinstance(operation, Select)
    query = operation.query
    assert (query.model, query.low_mark, query.high_mark) == ("clubs.Member", 0, 20)
    assert "lookup.icontains" in response.json()["constructs"]


async def test_member_search_ordering_and_page(client):
    body = (await client.get("/api/operations/members", params={"ordering": "-joined", "offset": 40})).json()
    operation = decoded(body)
    assert operation == operations.member_search(ordering="-joined", offset=40)
    assert (operation.query.low_mark, operation.query.high_mark) == (40, 60)


@pytest.mark.parametrize(
    "params",
    [{"ordering": "email"}, {"level": "expert"}, {"limit": 0}, {"limit": 101}, {"offset": -1}, {"joined_after": "x"}],
)
async def test_member_search_rejects_invalid_parameters(client, params):
    assert (await client.get("/api/operations/members", params=params)).status_code == 422


async def test_club_report(client):
    body = (await client.get("/api/operations/clubs/koper-sailing/report")).json()
    operation = decoded(body)
    assert isinstance(operation, Get)
    assert operation == operations.club_report("koper-sailing")
    for name in ("aggregate.Count", "aggregate.Sum", "aggregate.filter", "expression.Exists", "expression.Case"):
        assert name in body["constructs"]


async def test_club_report_rejects_a_malformed_slug(client):
    assert (await client.get("/api/operations/clubs/no%20slug/report")).status_code == 422


async def test_activity_schedule(client):
    params = {"club": "maribor-climbing", "kind": "training", "starts_after": "2026-03-01T00:00:00+00:00"}
    body = (await client.get("/api/operations/activities", params=params)).json()
    operation = decoded(body)
    starts = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
    assert operation == operations.activity_schedule(club="maribor-climbing", kind="training", starts_after=starts)
    assert operation.query.select_related_fields == ("club", "coach")
    assert "function.TruncMonth" in body["constructs"]


async def test_outstanding_invoices(client):
    body = (await client.get("/api/operations/invoices/outstanding", params={"today": "2026-03-01"})).json()
    operation = decoded(body)
    assert operation == operations.outstanding_by_club(datetime.date(2026, 3, 1))
    assert operation.query.row_format == "dict"
    assert (await client.get("/api/operations/invoices/outstanding")).status_code == 422


# Writes


async def test_create_member_validates_with_the_adorm_model(client):
    payload = {"first_name": "Mia", "last_name": "Kos", "email": "mia@example.com", "joined": "2026-01-01T09:00:00Z"}
    response = await client.post("/api/operations/members", json=payload)
    assert response.status_code == 201
    operation = decoded(response.json())
    assert isinstance(operation, Create)
    values = dict(operation.values)
    assert values["joined"] == datetime.datetime(2026, 1, 1, 9, tzinfo=datetime.UTC)
    assert values["level"] == "beginner"
    assert values["tags"] == []
    assert "id" not in values


@pytest.mark.parametrize(
    "change",
    [{"level": "expert"}, {"joined": "yesterday"}, {"first_name": "x" * 61}, {"nickname": "M"}, {"email": None}],
)
async def test_create_member_rejects_invalid_input(client, change):
    payload = {"first_name": "Mia", "last_name": "Kos", "email": "mia@example.com", "joined": "2026-01-01T09:00:00Z"}
    response = await client.post("/api/operations/members", json={**payload, **change})
    assert response.status_code == 422


async def test_change_member_saves_only_the_given_fields(client):
    response = await client.patch("/api/operations/members/3", json={"level": "advanced", "tags": ["coach"]})
    operation = decoded(response.json())
    assert isinstance(operation, Save)
    assert operation == Save(
        "clubs.Member", 3, {"level": "advanced", "tags": ["coach"]}, update_fields=("level", "tags")
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"joined": "2026-01-01T00:00:00Z"},
        {"level": "expert"},
        {"first_name": None},
        {"email": None},
        {"level": None},
        {"tags": None},
    ],
)
async def test_change_member_rejects_empty_or_invalid_changes(client, payload):
    response = await client.patch("/api/operations/members/3", json=payload)
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


async def test_change_member_clears_the_nullable_birth_date(client):
    operation = decoded((await client.patch("/api/operations/members/3", json={"birth_date": None})).json())
    assert operation == Save("clubs.Member", 3, {"birth_date": None}, update_fields=("birth_date",))


async def test_register_attendance(client):
    response = await client.post("/api/operations/activities/2/attendances", json={"member_id": 5})
    operation = decoded(response.json())
    assert isinstance(operation, GetOrCreate)
    assert dict(operation.lookups) == {"activity_id": 2, "member_id": 5}
    assert dict(operation.defaults) == {"status": "registered"}


async def test_cancel_attendance(client):
    operation = decoded((await client.post("/api/operations/attendances/4/cancel")).json())
    assert isinstance(operation, Update)
    assert dict(operation.values) == {"status": "cancelled", "checked_in_at": None}


async def test_pay_invoice_builds_a_create_and_an_update(client):
    payload = {"amount": "120.00", "reference": "SI00 1", "paid_at": "2026-02-01T12:00:00+01:00"}
    response = await client.post("/api/operations/invoices/7/payments", json=payload)
    first, second = (decoded(body) for body in response.json())
    assert isinstance(first, Create)
    assert dict(first.values)["amount"] == decimal.Decimal("120.00")
    assert isinstance(second, Update)
    assert dict(second.values) == {"status": "paid"}


@pytest.mark.parametrize("amount", ["0", "-1", "1.234", "abc"])
async def test_pay_invoice_rejects_invalid_amounts(client, amount):
    payload = {"amount": amount, "paid_at": "2026-02-01T12:00:00Z"}
    assert (await client.post("/api/operations/invoices/7/payments", json=payload)).status_code == 422


async def test_delete_draft_invoices(client):
    operation = decoded((await client.delete("/api/operations/clubs/koper-sailing/draft-invoices")).json())
    assert operation == operations.delete_draft_invoices("koper-sailing")


@pytest.mark.parametrize("path", ["/api/operations/attendances/0/cancel", "/api/operations/invoices/x/payments"])
async def test_primary_keys_are_positive_integers(client, path):
    assert (await client.post(path, json={})).status_code == 422


# Operation codec and models


async def test_decode_round_trips_every_built_operation(client):
    body = (await client.get("/api/operations/clubs/ljubljana-rowing/report")).json()
    again = await client.post("/api/operations/decode", json=body["operation"])
    assert again.status_code == 200
    assert again.json() == body


@pytest.mark.parametrize(
    "data",
    [
        {"operation": "nope"},
        {"operation": "create", "model": "clubs.Member", "values": {"nickname": {"value": "x"}}},
        {"operation": "create", "model": "clubs.Member", "values": {"joined": {"value": "yesterday"}}},
        {"operation": "select"},
    ],
)
async def test_decode_rejects_invalid_operations(client, data):
    response = await client.post("/api/operations/decode", json=data)
    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert (error["type"], error["loc"]) == ("value_error", ["body"])
    assert error["msg"]


async def test_create_member_reports_unknown_fields_in_the_validation_shape(client):
    payload = {"first_name": "Mia", "last_name": "Kos", "email": "m@example.com", "joined": "2026-01-01T09:00:00Z"}
    response = await client.post("/api/operations/members", json={**payload, "nickname": "M"})
    assert response.status_code == 422
    assert [(e["type"], e["loc"]) for e in response.json()["detail"]] == [("extra_forbidden", ["body", "nickname"])]


async def test_models_endpoint_serves_json_schemas(client):
    schemas = (await client.get("/api/models")).json()
    assert list(schemas) == [model._meta.label for model in MODELS]
    member = schemas["clubs.Member"]
    assert member["required"] == ["first_name", "last_name", "email", "joined"]
    assert "memberships" not in member["properties"]


def test_demo_models_are_registered():
    for model in MODELS:
        assert registry.get(model._meta.label) is model
    assert registry.model_schema("billing.Payment").pk == "code"
    assert Member.__adorm_relations__["memberships"].target == "clubs.Membership"
    assert isinstance(Member.memberships, ReverseForeignKey)
    assert isinstance(Member.clubs, ManyToMany)


async def test_openapi_documents_every_endpoint(client):
    schema = (await client.get("/openapi.json")).json()
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            assert operation.get("summary"), f"{method} {path} has no summary"
            assert operation.get("description"), f"{method} {path} has no description"
            if "422" in operation["responses"]:
                content = operation["responses"]["422"]["content"]["application/json"]
                assert content["schema"] == {"$ref": "#/components/schemas/HTTPValidationError"}
    for name, model in schema["components"]["schemas"].items():
        for field, definition in model.get("properties", {}).items():
            if name in {"ValidationError", "HTTPValidationError"} or name in {m.__name__ for m in MODELS}:
                continue
            assert definition.get("description"), f"{name}.{field} has no description"


async def test_openapi_says_when_an_endpoint_answers_422_beyond_validation(client):
    paths = (await client.get("/openapi.json")).json()["paths"]
    for path, method in [
        ("/api/operations/members", "post"),
        ("/api/operations/members/{pk}", "patch"),
        ("/api/operations/decode", "post"),
    ]:
        assert paths[path][method]["responses"]["422"]["description"].startswith("Validation error: the request")
