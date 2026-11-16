"""The demo's aggregation endpoints and its endpoints on related objects in process, on the memory driver with a
small data set that has invoice lines: the operations each one runs, the 501 answers of what the driver refuses,
the slots prefetches take, and concurrent and cancelled writes of invoice lines."""

import asyncio

from decimal import Decimal

import httpx
import pytest

from demo.api import drivers, main, memory
from demo.api.main import app
from demo.models.billing import Invoice
from tests.demo.memory_data import generic_tables, until
from tests.demo.test_data import RowHolder

REPEAT = range(5)
LINE = {
    "item": {"model": "activities.Activity", "id": 1},
    "description": "Sculling",
    "quantity": 2,
    "unit_price": "5.00",
}


@pytest.fixture
def database(monkeypatch):
    """The memory driver as the demo's only driver, over `generic_tables()`."""
    database = memory.MemoryDatabase()
    monkeypatch.setattr(memory, "DATABASE", database)
    demo_driver = drivers.DemoDriver(
        "memory",
        "demo.api.memory:MemoryDriver",
        lambda: database.load(generic_tables()),
        on_acquire=memory.on_acquire,
        on_release=memory.on_release,
    )
    monkeypatch.setattr(drivers, "DRIVERS", (demo_driver,))
    monkeypatch.delenv(drivers.DRIVER_ENV, raising=False)
    return database


@pytest.fixture
async def client(database):  # noqa: ARG001 - the driver's database
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://demo") as http:
            yield http


def calls(body) -> list[str]:
    return [call["call"] for call in body["calls"]]


def pool():
    from adorm.settings import default_router

    return next(p for p in default_router(create=False).pools if p.name == "default")


def assert_no_slot_in_use() -> None:
    assert (pool().in_use, pool().waiting) == (0, 0)


# Refused by the memory driver


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/api/data/clubs/reports", {}),
        ("/api/data/clubs/rowing/report", {}),
        ("/api/data/memberships/fees", {}),
        ("/api/data/activities/schedule", {}),
        ("/api/data/invoices/outstanding", {"today": "2026-03-01"}),
        ("/api/data/invoices/months", {}),
        ("/api/data/clubs/in-bulk", {"slug": "rowing"}),
    ],
)
async def test_annotations_aggregates_and_in_bulk_by_slug_answer_501(client, path, params):
    response = await client.get(path, params=params)
    assert response.status_code == 501, response.text
    body = response.json()
    assert body["error"] == "NotSupportedError"
    assert body["detail"]
    assert_no_slot_in_use()


async def test_full_clean_answers_501(client, database):
    responses = [
        await client.post("/api/data/invoices/1/validation", json={"due": "2020-01-01"}),
        await client.post(
            "/api/data/memberships/validation",
            json={"club_id": 1, "member_id": 1, "since": "2026-01-01", "fee": "10.00"},
        ),
    ]
    assert [(r.status_code, r.json()["error"]) for r in responses] == [(501, "NotSupportedError")] * 2
    assert database.rows("billing.Invoice")[0]["due"].isoformat() == "2026-02-14"
    assert_no_slot_in_use()


@pytest.mark.parametrize("path", ["/api/data/invoices/1/validation", "/api/data/memberships/validation"])
async def test_an_exclude_naming_no_field_answers_422_before_any_operation(client, path):
    body = {} if "invoices" in path else {"club_id": 1, "member_id": 1, "since": "2026-01-01", "fee": "1"}
    response = await client.post(path, params={"exclude": ["nonsense"]}, json=body)
    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert error["loc"] == ["query", "exclude"]
    assert "'nonsense' is not a field" in error["msg"]


# Prefetching


async def test_a_roster_prefetches_one_operation_per_relation_and_level(client):
    body = (await client.get("/api/data/clubs/rowing/roster")).json()
    roster = body["result"]
    assert roster["club"]["slug"] == "rowing"
    assert "active_memberships" not in roster["club"]
    assert [(m["member"]["first_name"], m["role"], m["fee"]) for m in roster["members"]] == [
        ("Ana", "member", "120.00"),
        ("Bojan", "coach", "0.00"),
    ]
    assert [(a["activity"]["name"], a["participants"]) for a in roster["activities"]] == [
        ("Sculling", ["Ana Novak"]),
        ("Regatta", []),
    ]
    operations = [call for call in calls(body) if call not in ("acquire", "release")]
    assert operations == [
        "get clubs.Club",
        "select clubs.Membership",
        "select clubs.Member",
        "select activities.Activity",
        "select activities.Attendance",
        "select clubs.Member",
    ]
    assert calls(body).count("acquire") == len(operations)


async def test_a_roster_in_a_transaction_runs_every_prefetch_on_its_slot(client):
    body = (await client.get("/api/data/clubs/rowing/roster", params={"atomic": True})).json()
    assert calls(body)[:2] == ["acquire", "begin"]
    assert calls(body)[-2:] == ["commit", "release"]
    assert calls(body).count("acquire") == 1
    assert {call["slot"] for call in body["calls"]} == {1}


async def test_a_roster_of_a_missing_club_answers_404(client):
    response = await client.get("/api/data/clubs/nowhere/roster", params={"atomic": True})
    assert response.status_code == 404
    assert_no_slot_in_use()


@pytest.mark.parametrize("attempt", REPEAT)
async def test_concurrent_rosters_share_a_one_slot_pool_without_deadlock(database, monkeypatch, attempt):  # noqa: ARG001
    """Each prefetch operation takes the only slot and returns it; a roster in a transaction holds it throughout. The
    requests queue for it and every one finishes."""
    monkeypatch.setattr(main, "POOL", main.PoolSettings(max_connections=1, acquire_timeout=5))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://demo") as http:
            requests = [
                http.get("/api/data/clubs/rowing/roster", params={"atomic": index % 2 == 0}) for index in range(6)
            ]
            responses = await asyncio.gather(*requests)
            assert [r.status_code for r in responses] == [200] * 6
            assert all(r.json()["result"] == responses[0].json()["result"] for r in responses)
            assert (pool().size, pool().in_use, pool().waiting) == (1, 0, 0)


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/api/data/clubs/rowing/roster", {}),
        ("/api/data/clubs/rowing/roster", {"atomic": True}),
        ("/api/data/members/in-bulk", {"id": [1, 2]}),
        ("/api/data/invoices/1/lines", {}),
        ("/api/data/invoices/1/lines", {"prefetch": False}),
    ],
)
async def test_every_operation_of_a_read_runs_in_the_request_session(client, path, params):
    body = (await client.get(path, params=params, headers={"X-Adorm-Session": "alice"})).json()
    assert body["session"] == "default/alice"
    assert {call["session"] for call in body["calls"]} == {"default/alice"}


# Rows by key


async def test_members_in_bulk_by_primary_key(client):
    body = (await client.get("/api/data/members/in-bulk", params={"id": [4, 99, 1, 4]})).json()
    assert {pk: m["first_name"] for pk, m in body["result"]["found"].items()} == {"1": "Ana", "4": "David"}
    assert body["result"]["missing"] == [99]
    assert calls(body) == ["acquire", "in_bulk clubs.Member", "release"]


@pytest.mark.parametrize("params", [{}, {"id": "x"}, {"id": list(range(1, 102))}])
async def test_members_in_bulk_validates_the_keys(client, params):
    assert (await client.get("/api/data/members/in-bulk", params=params)).status_code == 422


# Generic foreign keys


async def test_invoice_lines_with_a_prefetch_of_the_generic_foreign_key(client):
    body = (await client.get("/api/data/invoices/1/lines")).json()
    assert [(line["model"], line["item"]["id"], line["club"]) for line in body["result"]] == [
        ("clubs.Membership", 1, "rowing"),
        ("activities.Activity", 2, "rowing"),
    ]
    assert "billed" not in body["result"][0]["line"]
    operations = [call for call in calls(body) if call not in ("acquire", "release")]
    assert operations == [
        "exists billing.Invoice",
        "select billing.InvoiceLine",
        "select contenttypes.ContentType",
        "select clubs.Membership",
        "select activities.Activity",
        "select clubs.Club",
        "select clubs.Club",
    ]


async def test_invoice_lines_without_a_prefetch_load_each_item_and_club(client):
    prefetched = (await client.get("/api/data/invoices/1/lines")).json()
    body = (await client.get("/api/data/invoices/1/lines", params={"prefetch": False})).json()
    assert body["result"] == prefetched["result"]
    operations = [call for call in calls(body) if call not in ("acquire", "release")]
    assert operations == [
        "exists billing.Invoice",
        "select billing.InvoiceLine",
        "get clubs.Membership",
        "get clubs.Club",
        "get activities.Activity",
        "get clubs.Club",
    ]


async def test_a_line_whose_item_is_gone_answers_null(database, client):
    tables = generic_tables()
    gone = {**tables["billing.InvoiceLine"][0], "id": 3, "object_id": 99, "description": "Gone"}
    tables["billing.InvoiceLine"].append(gone)
    database.load(tables)
    prefetched = await client.get("/api/data/invoices/1/lines")
    loaded = await client.get("/api/data/invoices/1/lines", params={"prefetch": False})
    assert (prefetched.status_code, loaded.status_code) == (200, 200), (prefetched.text, loaded.text)
    assert prefetched.json()["result"] == loaded.json()["result"]
    lines = prefetched.json()["result"]
    assert [line["model"] for line in lines] == ["clubs.Membership", "activities.Activity", None]
    assert (lines[2]["item"], lines[2]["club"]) == (None, None)
    assert_no_slot_in_use()


async def test_lines_of_a_missing_invoice_answer_404(client):
    assert (await client.get("/api/data/invoices/99/lines")).status_code == 404
    body = (await client.get("/api/data/invoices/3/lines")).json()
    assert body["result"] == []


async def test_adding_a_line_sets_the_content_type_and_raises_the_total(database, client):
    response = await client.post("/api/data/invoices/3/lines", json=LINE)
    assert response.status_code == 201, response.text
    body = response.json()
    line, invoice = body["result"]["line"], body["result"]["invoice"]
    assert (line["content_type_id"], line["object_id"], line["invoice_id"]) == (8, 1, 3)
    assert invoice["total"] == "90.00"
    assert database.rows("billing.Invoice")[2]["total"] == Decimal("90.00")
    assert calls(body) == [
        "acquire",
        "begin",
        "get billing.Invoice",
        "get activities.Activity",
        "get contenttypes.ContentType",
        "create billing.InvoiceLine",
        "save billing.Invoice",
        "commit",
        "release",
    ]
    again = (await client.post("/api/data/invoices/3/lines", json=LINE)).json()
    assert "get contenttypes.ContentType" not in calls(again)
    assert again["result"]["invoice"]["total"] == "100.00"


@pytest.mark.parametrize(
    ("pk", "change", "status"),
    [
        (2, {}, 409),
        (99, {}, 404),
        (1, {"item": {"model": "clubs.Membership", "id": 99}}, 404),
        (3, {"quantity": 1000, "unit_price": "999999.99"}, 409),
    ],
    ids=["paid_invoice", "missing_invoice", "missing_item", "total_beyond_digits"],
)
async def test_a_refused_line_stores_nothing(database, client, pk, change, status):
    response = await client.post(f"/api/data/invoices/{pk}/lines", json={**LINE, **change})
    assert response.status_code == status, response.text
    assert len(database.rows("billing.InvoiceLine")) == 2
    assert [row["total"] for row in database.rows("billing.Invoice")] == [Decimal("120.00")] * 2 + [Decimal("80.00")]
    assert_no_slot_in_use()


@pytest.mark.parametrize(
    "change",
    [
        {"item": {"model": "clubs.Club", "id": 1}},
        {"quantity": 0},
        {"quantity": 2**31},
        {"unit_price": "1.001"},
        {"extra": 1},
    ],
)
async def test_a_line_body_is_validated(client, change):
    assert (await client.post("/api/data/invoices/1/lines", json={**LINE, **change})).status_code == 422


@pytest.mark.parametrize("attempt", REPEAT)
async def test_concurrent_lines_of_one_invoice_add_up(database, client, attempt):  # noqa: ARG001
    """The invoice's row lock orders the transactions: no raise of the total is lost."""
    responses = await asyncio.gather(*(client.post("/api/data/invoices/3/lines", json=LINE) for _ in range(6)))
    assert [r.status_code for r in responses] == [201] * 6
    totals = sorted(Decimal(r.json()["result"]["invoice"]["total"]) for r in responses)
    assert totals == [Decimal(80 + 10 * n) for n in range(1, 7)]
    assert database.rows("billing.Invoice")[2]["total"] == Decimal("140.00")
    assert len(database.rows("billing.InvoiceLine")) == 8
    assert_no_slot_in_use()


@pytest.fixture
async def holder(client):  # noqa: ARG001 - the executor of the app
    row_holder = RowHolder()
    try:
        yield row_holder
    finally:
        await row_holder.release()


@pytest.mark.parametrize("attempt", REPEAT)
async def test_a_line_cancelled_while_waiting_for_the_invoice_lock_stores_nothing(database, client, holder, attempt):  # noqa: ARG001
    """A transaction of the test holds the invoice's row lock; the request waits for it and the client goes away."""
    await holder.hold(lambda: Invoice.objects.select_for_update().filter(pk=3))
    request = asyncio.create_task(client.post("/api/data/invoices/3/lines", json=LINE))
    await until(lambda: len(database.waiters) == 1)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert database.waiters == {}
    await until(lambda: pool().in_use == 1)
    await holder.release()
    assert len(database.rows("billing.InvoiceLine")) == 2
    assert database.rows("billing.Invoice")[2]["total"] == Decimal("80.00")
    assert database.locks == {}
    response = await client.post("/api/data/invoices/3/lines", json=LINE)
    assert response.json()["result"]["invoice"]["total"] == "90.00"
    assert_no_slot_in_use()
