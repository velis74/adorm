"""The demo's executed endpoints in process, on the memory driver with a small data set."""

import asyncio
import datetime
import sys

import httpx
import pytest

import adorm

from adorm.settings import get_profile
from demo.api import data, drivers, main, memory
from demo.api.main import app, POOL
from demo.models.activities import Activity
from demo.models.billing import Invoice
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


class RowHolder:
    """A transaction of the session ``default/holder`` that locks rows with ``select_for_update()`` until
    `release()`."""

    def __init__(self) -> None:
        self.held = asyncio.Event()
        self.done = asyncio.Event()
        self.task: asyncio.Task | None = None

    async def hold(self, queryset_factory) -> None:
        async def run():
            with adorm.create_session("default", "holder").activate():
                async with adorm.atomic():
                    [row async for row in queryset_factory()]
                    self.held.set()
                    await self.done.wait()

        self.task = asyncio.create_task(run())
        await self.held.wait()

    async def release(self) -> None:
        self.done.set()
        if self.task is not None:
            await self.task


@pytest.fixture
async def holder(client):  # noqa: ARG001 - the executor of the app
    row_holder = RowHolder()
    try:
        yield row_holder
    finally:
        await row_holder.release()


def activity_one():
    return Activity.objects.select_for_update().filter(pk=1)


async def test_a_registration_locks_the_activity_with_select_for_update(database, client, monkeypatch):
    """While the transaction registers a member, it holds the activity's row lock and nothing else locks it."""
    reached = asyncio.Event()
    proceed = asyncio.Event()
    get_or_create = memory.MemoryConnection._get_or_create
    held: list[set] = []

    async def stall(self, operation):
        held.append({key for key, owner in database.locks.items() if owner is self})
        reached.set()
        await proceed.wait()
        return await get_or_create(self, operation)

    monkeypatch.setattr(memory.MemoryConnection, "_get_or_create", stall)
    request = asyncio.create_task(client.post("/api/data/activities/2/registrations", json={"member_ids": [2]}))
    await reached.wait()
    assert held == [{("activities.Activity", 2)}]
    proceed.set()
    body = (await request).json()
    assert body["result"] == {"registered": [2], "skipped": {}}
    assert calls(body)[:3] == ["acquire", "begin", "get activities.Activity"]
    assert "update activities.Activity" not in calls(body)
    assert database.locks == {}


async def test_a_registration_with_nowait_answers_409_while_the_activity_is_locked(database, client, holder):
    await holder.hold(activity_one)
    response = await client.post("/api/data/activities/1/registrations?nowait=true", json={"member_ids": [2]})
    assert response.status_code == 409
    body = response.json()
    assert (body["error"], body["sqlstate"]) == ("OperationalError", "55P03")
    assert "activities.Activity" in body["detail"]
    assert database.waiters == {}
    assert len(database.rows("activities.Attendance")) == 1
    await holder.release()
    response = await client.post("/api/data/activities/1/registrations?nowait=true", json={"member_ids": [2]})
    assert response.json()["result"]["registered"] == [2]


@pytest.mark.parametrize("attempt", REPEAT)
async def test_a_registration_waiting_for_the_lock_times_out_with_504(database, client, holder, attempt):  # noqa: ARG001
    await holder.hold(activity_one)
    response = await client.post(
        "/api/data/activities/1/registrations",
        params={"timeout": 0.05},
        json={"member_ids": [2]},
        headers={data.SESSION_HEADER: "late"},
    )
    assert response.status_code == 504
    body = response.json()
    assert (body["error"], body["phase"], body["operation"]) == (
        "OperationTimeout",
        "executing",
        "get activities.Activity",
    )
    assert (body["session"], body["outcome"]) == ("default/late", "unknown")
    assert database.waiters == {}
    assert len(database.rows("activities.Attendance")) == 1
    await until(lambda: _pool().in_use == 1)  # only the holder's slot
    await holder.release()
    assert database.locks == {}


async def test_a_registration_waiting_for_a_slot_times_out_in_that_phase(database, monkeypatch):
    monkeypatch.setattr(main, "POOL", adorm.PoolSettings(max_connections=1, acquire_timeout=5))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://demo") as http:
            row_holder = RowHolder()
            await row_holder.hold(activity_one)
            try:
                response = await http.post(
                    "/api/data/activities/2/registrations", params={"timeout": 0.05}, json={"member_ids": [2]}
                )
                assert _pool().waiting == 0
            finally:
                await row_holder.release()
            assert _pool().in_use == 0
    assert response.status_code == 504
    body = response.json()
    assert (body["phase"], body["operation"], body["outcome"]) == ("waiting_for_slot", "begin", "not_started")
    assert len(database.rows("activities.Attendance")) == 1


async def test_the_schema_check_is_refused_by_the_memory_driver_with_501(database, client):  # noqa: ARG001
    response = await client.get("/api/data/schema")
    assert response.status_code == 501
    body = response.json()
    assert body["error"] == "NotSupportedError"
    assert "'check_table'" in body["detail"]
    assert _pool().in_use == 0


async def test_lock_timeout_is_refused_by_the_memory_driver_with_501(database, client):
    response = await client.post("/api/data/activities/2/registrations?lock_timeout=1", json={"member_ids": [2]})
    assert response.status_code == 501
    body = response.json()
    assert body["error"] == "NotSupportedError"
    assert "lock_timeout" in body["detail"]
    assert len(database.rows("activities.Attendance")) == 1
    assert database.locks == {}
    assert _pool().in_use == 0


@pytest.mark.parametrize(
    "params", [{"timeout": 0}, {"timeout": 61}, {"lock_timeout": -1}, {"nowait": "maybe"}, {"timeout": "x"}]
)
async def test_registration_limits_are_validated(client, params):
    response = await client.post("/api/data/activities/2/registrations", params=params, json={"member_ids": [2]})
    assert response.status_code == 422


@pytest.mark.parametrize("attempt", REPEAT)
async def test_a_cancelled_registration_waiting_for_the_lock_stores_nothing(database, client, holder, attempt):  # noqa: ARG001
    await holder.hold(activity_one)
    request = asyncio.create_task(client.post("/api/data/activities/1/registrations", json={"member_ids": [2]}))
    await until(lambda: len(database.waiters) == 1)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert database.waiters == {}
    await until(lambda: _pool().in_use == 1)
    await holder.release()
    assert len(database.rows("activities.Attendance")) == 1
    assert database.locks == {}
    assert _pool().in_use == 0


@pytest.mark.parametrize("attempt", REPEAT)
async def test_a_registration_cancelled_after_its_writes_rolls_all_of_them_back(database, client, monkeypatch, attempt):  # noqa: ARG001
    """The client goes away while the second member's registration runs: the first member's savepoint was released
    but the transaction rolls back, so neither registration is stored."""
    reached = asyncio.Event()
    get_or_create = memory.MemoryConnection._get_or_create

    async def stall_on_member_3(self, operation):
        if dict(operation.lookups).get("member_id") == 3:
            reached.set()
            await asyncio.Event().wait()
        return await get_or_create(self, operation)

    monkeypatch.setattr(memory.MemoryConnection, "_get_or_create", stall_on_member_3)
    request = asyncio.create_task(client.post("/api/data/activities/2/registrations", json={"member_ids": [2, 3]}))
    await reached.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    await until(lambda: _pool().in_use == 0)
    assert [row["member_id"] for row in database.rows("activities.Attendance")] == [1]
    assert database.locks == {}


# Settlements


def open_invoices(database, count):
    """Replace the invoices with ``count`` open ones, due a day apart from the last to the first."""
    base = dict(database.tables["billing.Invoice"][1])
    database.tables["billing.Invoice"].clear()
    for pk in range(1, count + 1):
        due = base["due"] + datetime.timedelta(days=count - pk)
        database.tables["billing.Invoice"][pk] = base | {"id": pk, "number": f"2026-{pk:04d}", "due": due}


async def test_a_settlement_pays_open_invoices_earliest_due_first(database, client):
    open_invoices(database, 3)
    response = await client.post("/api/data/invoices/settlements", params={"limit": 2})
    assert response.status_code == 200
    body = response.json()
    assert [(p["invoice_id"], p["amount"], p["reference"]) for p in body["result"]["payments"]] == [
        (3, "120.00", "settled by default"),
        (2, "120.00", "settled by default"),
    ]
    assert calls(body) == [
        "acquire",
        "begin",
        "select billing.Invoice",
        "create billing.Payment",
        "save billing.Invoice",
        "create billing.Payment",
        "save billing.Invoice",
        "commit",
        "release",
    ]
    assert [row["status"] for row in database.rows("billing.Invoice")] == ["open", "paid", "paid"]
    again = (await client.post("/api/data/invoices/settlements")).json()["result"]["payments"]
    assert [p["invoice_id"] for p in again] == [1]
    assert (await client.post("/api/data/invoices/settlements")).json()["result"]["payments"] == []
    assert len(database.rows("billing.Payment")) == 3


async def test_a_settlement_skips_an_invoice_another_transaction_locked(database, client, holder):
    open_invoices(database, 2)
    await holder.hold(lambda: Invoice.objects.select_for_update().filter(pk=2))
    payments = (await client.post("/api/data/invoices/settlements")).json()["result"]["payments"]
    assert [p["invoice_id"] for p in payments] == [1]
    assert database.waiters == {}
    await holder.release()
    assert database.tables["billing.Invoice"][2]["status"] == "open"


@pytest.mark.parametrize("attempt", REPEAT)
async def test_concurrent_settlements_pay_each_invoice_once(database, client, monkeypatch, attempt):  # noqa: ARG001
    """Three settlements of at most two invoices each hold their locks until all three have selected: each takes
    the invoices the others have not locked, and none waits."""
    open_invoices(database, 5)
    selected = asyncio.Barrier(3)
    commit = memory.MemoryConnection.commit

    async def commit_together(self):
        if self.session.endswith("/s0") or self.session.endswith("/s1") or self.session.endswith("/s2"):
            await selected.wait()
        await commit(self)

    monkeypatch.setattr(memory.MemoryConnection, "commit", commit_together)
    responses = await asyncio.gather(
        *(
            client.post("/api/data/invoices/settlements", params={"limit": 2}, headers={data.SESSION_HEADER: f"s{n}"})
            for n in range(3)
        )
    )
    paid = [[p["invoice_id"] for p in response.json()["result"]["payments"]] for response in responses]
    assert sorted(len(ids) for ids in paid) == [1, 2, 2]
    assert sorted(pk for ids in paid for pk in ids) == [1, 2, 3, 4, 5]
    assert sorted(row["invoice_id"] for row in database.rows("billing.Payment")) == [1, 2, 3, 4, 5]
    assert {row["status"] for row in database.rows("billing.Invoice")} == {"paid"}
    assert database.locks == {}
    assert database.waiters == {}
    assert _pool().in_use == 0


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
        "sqlstate": "40P01",
        "phase": None,
        "operation": None,
        "outcome": None,
    }


def _pool(using="default"):
    """The pool of the profile ``using``; ``None`` before its first use."""
    from adorm.settings import default_router

    router = default_router(create=False)
    if router is None:
        return None
    return next((pool for pool in router.pools if pool.name == using), None)


# Schema


async def test_openapi_documents_the_data_endpoints(client):
    paths = (await client.get("/openapi.json")).json()["paths"]
    data_paths = [path for path in paths if path.startswith("/api/data/")]
    assert len(data_paths) == 23
    refused_by_memory = [
        ("/api/data/clubs/reports", "get"),
        ("/api/data/clubs/{slug}/report", "get"),
        ("/api/data/memberships/fees", "get"),
        ("/api/data/activities/schedule", "get"),
        ("/api/data/invoices/outstanding", "get"),
        ("/api/data/invoices/months", "get"),
        ("/api/data/clubs/in-bulk", "get"),
        ("/api/data/invoices/{pk}/validation", "post"),
        ("/api/data/memberships/validation", "post"),
    ]
    for path, method in refused_by_memory:
        response = paths[path][method]["responses"]["501"]
        assert response["content"]["application/json"]["schema"]["$ref"].endswith("/ExecutorError"), path
    for path in ("/api/data/activities/{pk}/registrations", "/api/data/invoices/settlements"):
        responses = paths[path]["post"]["responses"]
        assert {"409", "501", "503", "504"} <= set(responses), path
        assert responses["504"]["content"]["application/json"]["schema"]["$ref"].endswith("/ExecutorError")
    registration = paths["/api/data/activities/{pk}/registrations"]["post"]
    assert {p["name"] for p in registration["parameters"] if p["in"] == "query"} == {
        "nowait",
        "timeout",
        "lock_timeout",
    }
    member = paths["/api/data/members/{pk}"]["get"]
    assert {"404", "503"} <= set(member["responses"])
    headers = {p["name"] for p in member["parameters"] if p["in"] == "header"}
    assert headers == {data.SESSION_HEADER, data.DRIVER_HEADER}
    assert "409" in paths["/api/data/invoices/{pk}/payments"]["post"]["responses"]
    assert "503" in paths["/api/data/outbox"]["get"]["responses"]
    assert "503" in paths["/api/data/compare/members"]["get"]["responses"]
    for path, methods in paths.items():
        if path.startswith("/api/data/") and path not in {"/api/data/outbox", "/api/data/compare/members"}:
            for method, operation in methods.items():
                assert {"409", "503"} <= set(operation["responses"]), path
                names = {p["name"] for p in operation.get("parameters", ()) if p["in"] == "header"}
                assert (data.DRIVER_HEADER in names) == (method == "get"), path
                if method == "get":
                    assert data.DRIVER_HEADER in operation["responses"]["422"]["description"], path


# Choosing the driver of a read


_second: memory.MemoryDatabase | None = None


class SecondDriver(memory.MemoryDriver):
    """A memory driver over a database of its own, the demo's second driver in these tests."""

    name = "second"

    def __init__(self) -> None:
        super().__init__(_second)


@pytest.fixture
def two_drivers(monkeypatch, database):
    """The memory driver (active) and `SecondDriver`, each over its own copy of the small data set."""
    second = memory.MemoryDatabase()
    monkeypatch.setattr(sys.modules[__name__], "_second", second)
    demo_drivers = (
        drivers.get("memory"),
        drivers.DemoDriver(
            "second",
            f"{__name__}:SecondDriver",
            lambda: second.load(tables()),
            on_acquire=memory.on_acquire,
            on_release=memory.on_release,
        ),
    )
    monkeypatch.setattr(drivers, "DRIVERS", demo_drivers)
    return database, second


@pytest.fixture
async def two_clients(two_drivers):  # noqa: ARG001 - the drivers' databases
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://demo") as http:
            yield http


def on(driver: str) -> dict[str, str]:
    return {data.DRIVER_HEADER: driver}


async def test_every_loaded_driver_is_a_profile_named_after_it(two_drivers, two_clients):  # noqa: ARG001
    first, second = two_drivers
    assert get_profile("default").get_driver().database is first
    assert get_profile("memory").get_driver() is get_profile("default").get_driver()
    assert get_profile("second").get_driver().database is second


@pytest.mark.parametrize(
    ("path", "call"),
    [
        ("/api/data/members", "select clubs.Member"),
        ("/api/data/members/2", "get clubs.Member"),
        ("/api/data/invoices/1", "get billing.Invoice"),
        ("/api/data/clubs/rowing/summary", "get clubs.Club"),
    ],
)
async def test_a_read_runs_on_the_profile_the_driver_header_names(two_clients, path, call):
    default = (await two_clients.get(path)).json()
    assert (default["session"], default["driver"]) == ("default/default", "memory")
    for driver in ("memory", "second"):
        body = (await two_clients.get(path, headers=on(driver) | {data.SESSION_HEADER: "alice"})).json()
        assert (body["session"], body["driver"]) == (f"{driver}/alice", driver)
        assert {entry["session"] for entry in body["calls"]} == {f"{driver}/alice"}
        assert call in calls(body)
        assert body["result"] == default["result"]
    state = (await two_clients.get("/api/executor")).json()
    assert {pool["using"]: pool["in_use"] for pool in state["pools"]} == {"default": 0, "memory": 0, "second": 0}


async def test_each_driver_reads_its_own_database(two_drivers, two_clients):
    first, second = two_drivers
    first.tables["clubs.Member"][1]["first_name"] = "Changed"
    assert (await two_clients.get("/api/data/members/1")).json()["result"]["first_name"] == "Changed"
    assert (await two_clients.get("/api/data/members/1", headers=on("memory"))).json()["result"]["first_name"] == (
        "Changed"
    )
    assert (await two_clients.get("/api/data/members/1", headers=on("second"))).json()["result"]["first_name"] == "Ana"
    assert second.tables["clubs.Member"][1]["first_name"] == "Ana"


@pytest.mark.parametrize("name", ["nope", "", "default"])
async def test_a_driver_header_naming_no_available_driver_answers_422(two_clients, name):
    response = await two_clients.get("/api/data/members/1", headers=on(name))
    assert response.status_code == 422
    (error,) = response.json()["detail"]
    assert error["loc"] == ["header", data.DRIVER_HEADER]
    assert error["msg"] == f"{name!r} is not an available driver; the available drivers are memory, second"


async def test_writes_run_on_the_active_driver_whatever_the_driver_header(two_drivers, two_clients):
    first, second = two_drivers
    response = await two_clients.patch("/api/data/members/1", json={"level": "advanced"}, headers=on("second"))
    assert response.status_code == 200
    body = response.json()
    assert (body["session"], body["driver"]) == ("default/default", "memory")
    assert first.tables["clubs.Member"][1]["level"] == "advanced"
    assert second.tables["clubs.Member"][1]["level"] == "beginner"


async def test_a_driver_that_is_not_available_is_no_profile(monkeypatch, database):  # noqa: ARG001
    monkeypatch.setattr(
        drivers, "DRIVERS", (drivers.get("memory"), drivers.DemoDriver("missing", "adorm.drivers.nonexistent:Driver"))
    )
    async with app.router.lifespan_context(app):
        assert get_profile("missing") is None
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://demo") as http:
            assert (await http.get("/api/data/members", headers=on("missing"))).status_code == 422
            body = (await http.get("/api/data/compare/members")).json()
    assert [read["driver"] for read in body["reads"]] == ["memory"]
    assert body["same"] is True


async def test_compare_reads_the_same_page_on_every_driver(two_clients):
    body = (await two_clients.get("/api/data/compare/members", params={"search": "nov"})).json()
    assert body["same"] is True
    assert [(read["driver"], read["session"], read["error"]) for read in body["reads"]] == [
        ("memory", "memory/default", None),
        ("second", "second/default", None),
    ]
    assert [member["first_name"] for member in body["reads"][0]["result"]] == ["Ana", "David"]
    assert body["reads"][0]["result"] == body["reads"][1]["result"]


async def test_compare_reports_a_difference_between_the_databases(two_drivers, two_clients):
    first, _ = two_drivers
    first.tables["clubs.Member"][2]["last_name"] = "Zupan"
    body = (await two_clients.get("/api/data/compare/members", params={"ordering": "name"})).json()
    assert body["same"] is False
    assert [m["last_name"] for m in body["reads"][0]["result"]][-1] == "Zupan"
    assert "Zupan" not in [m["last_name"] for m in body["reads"][1]["result"]]


async def test_a_failing_read_of_one_driver_does_not_stop_the_other(two_clients, monkeypatch):
    original = memory.MemoryConnection._execute

    async def failing(self, operation):
        if isinstance(self.driver, SecondDriver):
            raise adorm.OperationalError("server closed the connection unexpectedly")
        return await original(self, operation)

    monkeypatch.setattr(memory.MemoryConnection, "_execute", failing)
    response = await two_clients.get("/api/data/compare/members")
    assert response.status_code == 200
    body = response.json()
    assert body["same"] is False
    assert len(body["reads"][0]["result"]) == 4
    assert body["reads"][1] == {
        "driver": "second",
        "session": "second/default",
        "result": None,
        "error": "OperationalError: server closed the connection unexpectedly",
    }
    assert [_pool(using).in_use for using in ("memory", "second")] == [0, 0]


async def test_a_slot_timeout_of_one_driver_is_reported_in_its_read(two_drivers, monkeypatch):  # noqa: ARG001
    monkeypatch.setattr(main, "POOL", adorm.PoolSettings(max_connections=1, acquire_timeout=0.05))
    hold = asyncio.Event()
    original = memory.MemoryConnection.begin

    async def slow_begin(self):
        await original(self)
        if isinstance(self.driver, SecondDriver):
            await hold.wait()

    monkeypatch.setattr(memory.MemoryConnection, "begin", slow_begin)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://demo") as http:
            holding = asyncio.create_task(
                http.get("/api/data/clubs/rowing/summary", params={"atomic": True}, headers=on("second"))
            )
            await until(lambda: (pool := _pool("second")) is not None and pool.in_use == 1)
            body = (await http.get("/api/data/compare/members")).json()
            hold.set()
            assert (await holding).status_code == 200
            assert _pool("second").in_use == 0
    assert len(body["reads"][0]["result"]) == 4
    assert body["reads"][1]["result"] is None
    assert body["reads"][1]["error"].startswith("SessionTimeout: ")
    assert body["same"] is False


@pytest.mark.parametrize("attempt", REPEAT)
async def test_compare_reads_on_both_drivers_at_once(two_clients, monkeypatch, attempt):  # noqa: ARG001
    """Each read waits until both are executing: the reads run concurrently, not one after the other."""
    executing: set[str] = set()
    both = asyncio.Event()
    original = memory.MemoryConnection._execute

    async def rendezvous(self, operation):
        if operation.kind == "select":
            executing.add(self.driver.name)
            if len(executing) == 2:
                both.set()
            await both.wait()
        return await original(self, operation)

    monkeypatch.setattr(memory.MemoryConnection, "_execute", rendezvous)
    try:
        async with asyncio.timeout(5):
            body = (await two_clients.get("/api/data/compare/members")).json()
    finally:
        both.set()
    assert executing == {"memory", "second"}
    assert body["same"] is True


@pytest.mark.parametrize("attempt", REPEAT)
async def test_a_cancelled_comparison_returns_both_slots(two_drivers, two_clients, monkeypatch, attempt):  # noqa: ARG001
    """The client goes away while both reads execute: both are cancelled and both slots go back to their pools."""
    executing: set[str] = set()
    never = asyncio.Event()
    original = memory.MemoryConnection._execute

    async def stuck(self, operation):
        if operation.kind == "select":
            executing.add(self.driver.name)
            await never.wait()
        return await original(self, operation)

    monkeypatch.setattr(memory.MemoryConnection, "_execute", stuck)
    request = asyncio.create_task(two_clients.get("/api/data/compare/members"))
    try:
        await until(lambda: executing == {"memory", "second"})
    finally:
        request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    await until(lambda: all((pool := _pool(u)) is not None and pool.in_use == 0 for u in ("memory", "second")))
    monkeypatch.setattr(memory.MemoryConnection, "_execute", original)
    body = (await two_clients.get("/api/data/compare/members")).json()
    assert body["same"] is True
    assert [_pool(using).in_use for using in ("memory", "second")] == [0, 0]


@pytest.mark.parametrize("attempt", REPEAT)
async def test_concurrent_reads_on_both_drivers_leave_no_slot_behind(two_clients, attempt):  # noqa: ARG001
    """More concurrent reads than either pool has slots, spread over three profiles of two drivers."""
    paths = ["/api/data/members", "/api/data/members/3", "/api/data/invoices/1", "/api/data/clubs/rowing/summary"]
    headers = [{}, on("memory"), on("second")]
    requests = [(path, header) for n in range(4) for path in paths for header in headers[n % 3 :] + headers[: n % 3]]
    responses = await asyncio.gather(*(two_clients.get(path, headers=header) for path, header in requests))
    assert [response.status_code for response in responses] == [200] * len(requests)
    results: dict[str, list] = {}
    for (path, _), response in zip(requests, responses, strict=True):
        results.setdefault(path, []).append(response.json()["result"])
    for path, found in results.items():
        assert all(result == found[0] for result in found), path
    for using in ("default", "memory", "second"):
        assert (_pool(using).in_use, _pool(using).waiting) == (0, 0)
        assert _pool(using).size <= main.POOL.max_connections
