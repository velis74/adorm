"""The demo's executed endpoints in process, on the memory driver with a small data set."""

import asyncio

import httpx
import pytest

import adorm

from adorm.settings import get_profile
from demo.api import data, drivers, main, memory
from demo.api.main import app, POOL
from tests.demo.memory_data import tables, until

REPEAT = range(5)
PAYMENT = {"amount": "120.00", "reference": "SI00 123", "paid_at": "2026-03-01T10:00:00Z"}
ENROLMENT = {"first_name": "Eva", "last_name": "Kos", "email": "eva@example.com", "fee": "95.50"}


@pytest.fixture
def database(monkeypatch):
    """The memory driver as the demo's only driver; its setup loads the small data set into a fresh database."""
    database = memory.MemoryDatabase()
    monkeypatch.setattr(memory, "DATABASE", database)
    demo_driver = drivers.DemoDriver(
        "memory",
        "demo.api.memory:MemoryDriver",
        lambda: database.load(tables()),
        on_acquire=memory.on_acquire,
        on_release=memory.on_release,
    )
    monkeypatch.setattr(drivers, "DRIVERS", (demo_driver,))
    monkeypatch.delenv(drivers.DRIVER_ENV, raising=False)
    data.OUTBOX.clear()
    yield database
    data.OUTBOX.clear()


@pytest.fixture
async def client(database):  # noqa: ARG001 - the driver's database
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://demo") as http:
            yield http


def calls(body) -> list[str]:
    return [call["call"] for call in body["calls"]]


# Set-up


async def test_the_active_driver_becomes_the_default_profile(database, client):
    profile = get_profile("default")
    assert profile.get_driver().database is database
    assert profile.on_acquire is memory.on_acquire
    assert profile.pool is None
    assert (await client.get("/api/health")).json()["driver"] == "memory"
    assert len(database.rows("clubs.Member")) == 4
    assert POOL.max_connections == 4


async def test_shutdown_closes_the_pools_and_removes_the_profile(database):  # noqa: ARG001
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://demo") as http:
            assert (await http.get("/api/data/members")).status_code == 200
            state = (await http.get("/api/executor")).json()
            assert state["pools"] == [{"using": "default", "limit": 4, "size": 1, "idle": 1, "in_use": 0, "waiting": 0}]
    assert get_profile("default") is None


async def test_without_a_driver_the_data_endpoints_answer_503(monkeypatch):
    monkeypatch.setattr(drivers, "DRIVERS", (drivers.DemoDriver("missing", "adorm.drivers.nonexistent:Driver"),))
    monkeypatch.delenv(drivers.DRIVER_ENV, raising=False)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://demo") as http:
            response = await http.get("/api/data/members")
            assert response.status_code == 503
            assert response.json() == {"detail": "no driver is active: the data endpoints execute operations on one"}
            assert (await http.get("/api/executor")).json() == {"driver": None, "pools": []}
    assert get_profile("default") is None


# Reads


async def test_list_members_runs_one_select_on_its_own_slot(client):
    response = await client.get("/api/data/members", params={"search": "NOV", "ordering": "-joined"})
    assert response.status_code == 200
    body = response.json()
    assert [member["first_name"] for member in body["result"]] == ["Ana", "David"]
    assert body["result"][0]["birth_date"] == "2001-01-10"
    assert body["session"] == "default/default"
    assert body["calls"] == [
        {"slot": 1, "session": "default/default", "call": call}
        for call in ("acquire", "select clubs.Member", "release")
    ]


async def test_list_members_filters_orders_and_pages(client):
    body = (await client.get("/api/data/members", params={"level": "beginner"})).json()
    assert [member["id"] for member in body["result"]] == [1, 4]
    body = (await client.get("/api/data/members", params={"ordering": "name", "offset": 1, "limit": 2})).json()
    assert [member["last_name"] for member in body["result"]] == ["Kovač", "Novak"]


@pytest.mark.parametrize("params", [{"level": "expert"}, {"limit": 0}, {"limit": 101}, {"ordering": "email"}])
async def test_list_members_rejects_invalid_parameters(client, params):
    assert (await client.get("/api/data/members", params=params)).status_code == 422


async def test_get_member_and_a_missing_one(client):
    body = (await client.get("/api/data/members/2")).json()
    assert body["result"]["tags"] == ["coach"]
    assert calls(body) == ["acquire", "get clubs.Member", "release"]
    response = await client.get("/api/data/members/99")
    assert response.status_code == 404
    assert response.json() == {"detail": "Member matching query does not exist."}


async def test_get_invoice_with_its_payments_through_the_reverse_relation(client):
    body = (await client.get("/api/data/invoices/1")).json()
    assert body["result"]["invoice"]["number"] == "2026-0001"
    assert body["result"]["payments"] == []
    assert calls(body) == ["acquire", "get billing.Invoice", "release", "acquire", "select billing.Payment", "release"]
    assert (await client.get("/api/data/invoices/99")).status_code == 404


# Sessions


@pytest.mark.parametrize("path", ["/api/data/members/1", "/api/data/members"])
async def test_the_session_header_names_the_session(client, path):
    response = await client.get(path, headers={data.SESSION_HEADER: "alice"})
    body = response.json()
    assert body["session"] == "default/alice"
    assert {call["session"] for call in body["calls"]} == {"default/alice"}


@pytest.mark.parametrize("name", ["", "a b", "x" * 41, "a/b"])
async def test_an_invalid_session_name_is_rejected(client, name):
    response = await client.get("/api/data/members/1", headers={data.SESSION_HEADER: name})
    assert response.status_code == 422


# Writes in transactions


async def test_change_member_saves_only_the_given_fields_in_a_transaction(database, client):
    response = await client.patch("/api/data/members/3", json={"level": "advanced", "birth_date": None})
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["level"] == "advanced"
    assert body["result"]["birth_date"] is None
    assert calls(body) == ["acquire", "begin", "get clubs.Member", "save clubs.Member", "commit", "release"]
    row = database.tables["clubs.Member"][3]
    assert (row["level"], row["birth_date"], row["first_name"]) == ("advanced", None, "Cvetka")


async def test_change_member_rejects_bad_bodies_and_missing_members(database, client):
    assert (await client.patch("/api/data/members/1", json={})).status_code == 422
    assert (await client.patch("/api/data/members/1", json={"level": None})).status_code == 422
    response = await client.patch("/api/data/members/99", json={"level": "advanced"})
    assert response.status_code == 404
    assert database.locks == {}


async def test_enrolment_creates_member_and_membership_and_queues_on_commit(database, client):
    response = await client.post("/api/data/clubs/rowing/members", json=ENROLMENT)
    assert response.status_code == 201
    body = response.json()
    member, membership = body["result"]["member"], body["result"]["membership"]
    assert (member["id"], member["level"]) == (5, "beginner")
    assert (membership["club_id"], membership["member_id"], membership["fee"]) == (1, 5, "95.50")
    assert calls(body) == [
        "acquire",
        "begin",
        "get clubs.Club",
        "create clubs.Member",
        "create clubs.Membership",
        "commit",
        "release",
    ]
    assert database.tables["clubs.Membership"][4]["member_id"] == 5
    outbox = (await client.get("/api/data/outbox")).json()
    assert [message["text"] for message in outbox] == ["Welcome to Rowing, Eva!"]


async def test_a_failed_enrolment_writes_nothing_and_queues_nothing(database, client, monkeypatch):
    assert (await client.post("/api/data/clubs/nowhere/members", json=ENROLMENT)).status_code == 404

    insert = memory.MemoryConnection._insert

    async def refuse_memberships(self, label, values):
        if label == "clubs.Membership":
            raise adorm.IntegrityError("membership refused", sqlstate="23505")
        return await insert(self, label, values)

    monkeypatch.setattr(memory.MemoryConnection, "_insert", refuse_memberships)
    with pytest.raises(adorm.IntegrityError, match="membership refused"):
        await client.post("/api/data/clubs/rowing/members", json=ENROLMENT)
    assert len(database.rows("clubs.Member")) == 4
    assert len(database.rows("clubs.Membership")) == 3
    assert (await client.get("/api/data/outbox")).json() == []


async def test_paying_an_open_invoice_stores_the_payment_and_marks_it_paid(database, client):
    response = await client.post("/api/data/invoices/1/payments", json=PAYMENT)
    assert response.status_code == 201
    body = response.json()
    assert body["result"]["reference"] == "SI00 123"
    assert calls(body) == [
        "acquire",
        "begin",
        "exists billing.Invoice",
        "create billing.Payment",
        "update billing.Invoice",
        "commit",
        "release",
    ]
    assert database.tables["billing.Invoice"][1]["status"] == "paid"
    detail = (await client.get("/api/data/invoices/1")).json()["result"]
    assert [payment["code"] for payment in detail["payments"]] == [body["result"]["code"]]


async def test_paying_an_invoice_that_is_not_open_rolls_the_payment_back(database, client):
    response = await client.post("/api/data/invoices/2/payments", json=PAYMENT)
    assert response.status_code == 409
    assert response.json() == {"detail": "invoice 2 is not open"}
    assert database.rows("billing.Payment") == []
    assert database.locks == {}
    assert (await client.post("/api/data/invoices/99/payments", json=PAYMENT)).status_code == 404


@pytest.mark.parametrize("attempt", REPEAT)
async def test_concurrent_payments_of_one_invoice_store_exactly_one(database, client, attempt):  # noqa: ARG001
    """The second transaction's update waits for the first one's row lock, then finds the invoice paid."""
    responses = await asyncio.gather(
        *(
            client.post("/api/data/invoices/1/payments", json=PAYMENT, headers={data.SESSION_HEADER: f"s{n}"})
            for n in range(3)
        )
    )
    assert sorted(response.status_code for response in responses) == [201, 409, 409]
    assert len(database.rows("billing.Payment")) == 1
    assert database.locks == {}
    assert database.waiters == {}
    state = (await client.get("/api/executor")).json()
    assert state["pools"][0]["in_use"] == 0


async def test_registrations_roll_back_a_full_registration_to_its_savepoint(database, client):
    response = await client.post("/api/data/activities/1/registrations", json={"member_ids": [1, 2, 3, 99, 2]})
    assert response.status_code == 200
    body = response.json()
    assert body["result"] == {
        "registered": [2],
        "skipped": {"1": "already registered", "3": "full", "99": "unknown member"},
    }
    sequence = calls(body)
    assert sequence[:3] == ["acquire", "begin", "get activities.Activity"]
    assert sequence.count("rollback to savepoint adorm_sp_3") == 1
    assert sequence[-2:] == ["commit", "release"]
    members = sorted(row["member_id"] for row in database.rows("activities.Attendance"))
    assert members == [1, 2]
    assert (await client.post("/api/data/activities/99/registrations", json={"member_ids": [1]})).status_code == 404
    assert (await client.post("/api/data/activities/1/registrations", json={"member_ids": []})).status_code == 422


@pytest.mark.parametrize("attempt", REPEAT)
async def test_concurrent_registrations_do_not_overfill_the_activity(database, client, attempt):  # noqa: ARG001
    """Activity 1 has one place left: two requests registering different members queue on the activity's row lock,
    and the second finds it full."""
    responses = await asyncio.gather(
        *(
            client.post(
                "/api/data/activities/1/registrations",
                json={"member_ids": [member_id]},
                headers={data.SESSION_HEADER: f"r{member_id}"},
            )
            for member_id in (3, 4)
        )
    )
    results = [response.json()["result"] for response in responses]
    assert sorted(len(result["registered"]) for result in results) == [0, 1]
    assert sorted(reason for result in results for reason in result["skipped"].values()) == ["full"]
    assert len(database.rows("activities.Attendance")) == 2
    assert database.locks == {}


# Concurrent reads


async def test_summary_without_a_transaction_reads_on_a_slot_per_task(client):
    body = (await client.get("/api/data/clubs/rowing/summary")).json()
    assert body["result"] | {"club": None} == {
        "club": None,
        "active_memberships": 2,
        "activities": 2,
        "open_invoices": 1,
    }
    assert body["result"]["club"]["slug"] == "rowing"
    assert "begin" not in calls(body)
    assert calls(body).count("acquire") == 4
    assert calls(body).count("release") == 4


@pytest.mark.parametrize("attempt", REPEAT)
async def test_summary_in_a_transaction_shares_its_slot_with_the_tasks(client, attempt):  # noqa: ARG001
    body = (await client.get("/api/data/clubs/rowing/summary", params={"atomic": True})).json()
    assert body["result"]["active_memberships"] == 2
    assert {call["slot"] for call in body["calls"]} == {body["calls"][0]["slot"]}
    sequence = calls(body)
    assert sequence[:3] == ["acquire", "begin", "get clubs.Club"]
    assert sorted(sequence[3:6]) == ["count activities.Activity", "count billing.Invoice", "count clubs.Membership"]
    assert sequence[6:] == ["commit", "release"]


async def test_summary_of_a_missing_club(client):
    assert (await client.get("/api/data/clubs/nowhere/summary")).status_code == 404
    assert (await client.get("/api/data/clubs/nowhere/summary", params={"atomic": True})).status_code == 404


@pytest.mark.parametrize("attempt", REPEAT)
async def test_concurrent_requests_queue_for_the_pool_and_leave_no_slot_behind(database, client, attempt):  # noqa: ARG001
    """More concurrent transactions than the pool has slots: the extra ones wait for a slot."""
    hold = asyncio.Event()
    original = memory.MemoryConnection.begin

    async def slow_begin(self):
        await original(self)
        await hold.wait()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(memory.MemoryConnection, "begin", slow_begin)
        requests = [
            asyncio.create_task(client.patch(f"/api/data/members/{1 + n % 4}", json={"level": "advanced"}))
            for n in range(6)
        ]
        await until(lambda: (pool := _pool()) is not None and pool.in_use == 4 and pool.waiting == 2)
        hold.set()
        responses = await asyncio.gather(*requests)
    assert [response.status_code for response in responses] == [200] * 6
    assert _pool().in_use == 0
    assert _pool().size == 4
    assert database.locks == {}


async def test_a_request_that_gets_no_slot_in_time_answers_503(database, monkeypatch):  # noqa: ARG001
    monkeypatch.setattr(main, "POOL", adorm.PoolSettings(max_connections=1, acquire_timeout=0.05))
    hold = asyncio.Event()
    original = memory.MemoryConnection.begin

    async def slow_begin(self):
        await original(self)
        await hold.wait()

    monkeypatch.setattr(memory.MemoryConnection, "begin", slow_begin)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://demo") as http:
            holding = asyncio.create_task(http.patch("/api/data/members/1", json={"level": "advanced"}))
            await until(lambda: (pool := _pool()) is not None and pool.in_use == 1)
            response = await http.get("/api/data/members", headers={data.SESSION_HEADER: "late"})
            hold.set()
            assert (await holding).status_code == 200
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "SessionTimeout"
    assert body["session"] == "default/late"
    assert body["held_sessions"] == []
    assert "default" in body["detail"]


async def test_a_deadlock_answers_409(client, monkeypatch):
    async def deadlock(self, query, values):  # noqa: ARG001
        raise adorm.OperationalError("deadlock detected", sqlstate="40P01")

    monkeypatch.setattr(memory.MemoryConnection, "_update", deadlock)
    response = await client.patch("/api/data/members/1", json={"level": "advanced"})
    assert response.status_code == 409
    assert response.json() == {
        "detail": "deadlock detected",
        "error": "OperationalError",
        "session": None,
        "held_sessions": [],
    }


def _pool():
    from adorm.settings import default_router

    router = default_router(create=False)
    return router.pools[0] if router is not None and router.pools else None


# Schema


async def test_openapi_documents_the_data_endpoints(client):
    paths = (await client.get("/openapi.json")).json()["paths"]
    data_paths = [path for path in paths if path.startswith("/api/data/")]
    assert len(data_paths) == 8
    member = paths["/api/data/members/{pk}"]["get"]
    assert {"404", "503"} <= set(member["responses"])
    header = next(p for p in member["parameters"] if p["in"] == "header")
    assert header["name"] == data.SESSION_HEADER
    assert "409" in paths["/api/data/invoices/{pk}/payments"]["post"]["responses"]
    assert "503" in paths["/api/data/outbox"]["get"]["responses"]
    for path, methods in paths.items():
        if path.startswith("/api/data/") and path != "/api/data/outbox":
            for operation in methods.values():
                assert {"409", "503"} <= set(operation["responses"]), path
