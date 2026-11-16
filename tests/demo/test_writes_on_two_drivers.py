"""The demo's write flows in process on the Django driver and on the native PostgreSQL driver, each one active in
turn, over the demo's data set in the test database: transactions with savepoints, `select_for_update()` with
``nowait`` and ``skip_locked``, ``lock_timeout``, `adorm.timeout()` and the client going away."""

import asyncio

from decimal import Decimal

import asyncpg
import httpx
import pytest

import adorm

from adorm.settings import default_router
from demo.api import data, drivers, instances, main
from demo.api.main import app

REPEAT = range(3)
DRIVERS = ("django", "asyncpg")
LOCK_WAITERS = (
    "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND datname = current_database()"
    " AND pid <> pg_backend_pid()"
)


@pytest.fixture(params=DRIVERS)
def active(request, monkeypatch):
    """The demo's Django and asyncpg drivers, the parameter active; skipped when either is not available."""
    entries = tuple(drivers.get(name) for name in DRIVERS)
    for entry in entries:
        reason = entry.unavailable_reason()
        if reason is not None:
            pytest.skip(f"the {entry.name} driver is not available: {reason}")
    monkeypatch.setattr(drivers, "DRIVERS", entries)
    monkeypatch.setenv(drivers.DRIVER_ENV, request.param)
    return request.param


class Database:
    """Connections of the test's own to the seeded test database: ``sql`` reads and writes, ``holder`` holds row
    locks in a transaction until `release()`, ``monitor`` counts the statements waiting for a lock."""

    def __init__(self, sql, holder, monitor):
        self.sql = sql
        self.holder = holder
        self.monitor = monitor
        self.transaction = None

    async def hold(self, statement: str, *args) -> None:
        self.transaction = self.holder.transaction()
        await self.transaction.start()
        await self.holder.execute(statement, *args)

    async def release(self) -> None:
        transaction, self.transaction = self.transaction, None
        if transaction is not None:
            await transaction.rollback()

    async def waiting(self, count: int) -> None:
        """Return once exactly ``count`` statements wait for a lock; fail after 10 seconds."""
        async with asyncio.timeout(10):
            while await self.monitor.fetchval(LOCK_WAITERS) != count:
                await asyncio.sleep(0.01)


@pytest.fixture
async def database(demo_data):
    connections = [await asyncpg.connect(demo_data) for _ in range(3)]
    db = Database(*connections)
    try:
        yield db
    finally:
        await db.release()
        for connection in connections:
            await connection.close()


@pytest.fixture
async def client(active, database):  # noqa: ARG001 - the active driver over the seeded database
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://demo", timeout=30) as http:
            yield http


def pool():
    router = default_router(create=False)
    return next(p for p in router.pools if p.name == "default")


async def no_slot_in_use() -> None:
    """Wait until the pool of ``default`` has no slot in use and no request waiting; fail after 10 seconds."""
    async with asyncio.timeout(10):
        while (pool().in_use, pool().waiting) != (0, 0):
            await asyncio.sleep(0.01)
    assert pool().size <= main.POOL.max_connections


class Activity:
    """An activity with two free places and the members not registered for it."""

    def __init__(self, pk: int, free_members: list[int]):
        self.pk = pk
        self.free_members = free_members


@pytest.fixture
async def activity(database) -> Activity:
    """The first activity, its capacity set to two more than its registrations."""
    pk = await database.sql.fetchval("SELECT min(id) FROM activities_activity")
    taken = await database.sql.fetchval(
        "SELECT count(*) FROM activities_attendance WHERE activity_id = $1 AND status <> 'cancelled'", pk
    )
    await database.sql.execute("UPDATE activities_activity SET capacity = $1 WHERE id = $2", taken + 2, pk)
    free = await database.sql.fetch(
        "SELECT id FROM clubs_member"
        " WHERE id NOT IN (SELECT member_id FROM activities_attendance WHERE activity_id = $1) ORDER BY id",
        pk,
    )
    return Activity(pk, [row["id"] for row in free])


async def attendances(database, activity: Activity) -> list[int]:
    rows = await database.sql.fetch(
        "SELECT member_id FROM activities_attendance WHERE activity_id = $1 AND member_id = ANY($2::int[])"
        " ORDER BY member_id",
        activity.pk,
        activity.free_members,
    )
    return [row["member_id"] for row in rows]


async def bounded(request, params):
    """Await ``request``; fail 10 seconds after the request's own ``timeout``, so a missed lock limit fails the test."""
    async with asyncio.timeout(10 + params.get("timeout", 0)):
        return await request


async def register(client, activity: Activity, members, session="default", **params):
    return await bounded(
        client.post(
            f"/api/data/activities/{activity.pk}/registrations",
            params=params,
            json={"member_ids": list(members)},
            headers={data.SESSION_HEADER: session},
        ),
        params,
    )


async def lock_activity(database, activity: Activity) -> None:
    await database.hold("SELECT id FROM activities_activity WHERE id = $1 FOR UPDATE", activity.pk)


# Registrations: a transaction, a savepoint per member, the activity's row locked


async def test_a_full_registration_rolls_back_to_its_savepoint(active, client, database, activity):
    a, b, c = activity.free_members[:3]
    response = await register(client, activity, [a, b, c, 999999])
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["driver"], body["session"]) == (active, "default/default")
    assert body["result"] == {"registered": [a, b], "skipped": {str(c): "full", "999999": "unknown member"}}
    assert await attendances(database, activity) == [a, b]
    again = (await register(client, activity, [a])).json()["result"]
    assert again == {"registered": [], "skipped": {str(a): "already registered"}}
    await no_slot_in_use()


@pytest.mark.parametrize("attempt", REPEAT)
async def test_concurrent_registrations_never_overfill_the_activity(client, database, activity, attempt):  # noqa: ARG001
    """Four requests for the two free places: `select_for_update()` makes them run one after another, so exactly two
    register and two find the activity full."""
    members = activity.free_members[:4]
    responses = await asyncio.gather(
        *(register(client, activity, [member], session=f"r{member}") for member in members)
    )
    assert [response.status_code for response in responses] == [200] * 4
    results = [response.json()["result"] for response in responses]
    registered = sorted(member for result in results for member in result["registered"])
    assert len(registered) == 2
    assert sorted(reason for result in results for reason in result["skipped"].values()) == ["full", "full"]
    assert await attendances(database, activity) == registered
    await no_slot_in_use()


async def test_nowait_answers_409_while_another_transaction_locks_the_activity(client, database, activity):
    await lock_activity(database, activity)
    response = await register(client, activity, activity.free_members[:1], nowait=True)
    assert response.status_code == 409
    body = response.json()
    assert (body["error"], body["sqlstate"]) == ("OperationalError", "55P03")
    await no_slot_in_use()
    await database.release()
    assert await attendances(database, activity) == []
    response = await register(client, activity, activity.free_members[:1], nowait=True)
    assert response.json()["result"]["registered"] == activity.free_members[:1]


async def test_lock_timeout_answers_409_after_waiting_that_long(client, database, activity):
    await lock_activity(database, activity)
    response = await register(client, activity, activity.free_members[:1], lock_timeout=0.2)
    assert response.status_code == 409
    body = response.json()
    assert (body["error"], body["sqlstate"]) == ("OperationalError", "55P03")
    assert "lock timeout" in body["detail"]
    await database.waiting(0)
    await no_slot_in_use()
    await database.release()
    assert await attendances(database, activity) == []
    # The SET LOCAL ended with the transaction: the next registration waits without a limit.
    await lock_activity(database, activity)
    request = asyncio.create_task(register(client, activity, activity.free_members[:1]))
    await database.waiting(1)
    await database.release()
    assert (await request).json()["result"]["registered"] == activity.free_members[:1]


@pytest.mark.parametrize("attempt", REPEAT)
async def test_a_timeout_cancels_the_waiting_statement_and_stores_nothing(client, database, activity, attempt):  # noqa: ARG001
    assert (await client.get(f"/api/data/members/{activity.free_members[0]}")).status_code == 200  # a warm slot
    await lock_activity(database, activity)
    response = await register(client, activity, activity.free_members[:1], session="late", timeout=1)
    assert response.status_code == 504
    body = response.json()
    assert (body["error"], body["phase"], body["operation"]) == (
        "OperationTimeout",
        "executing",
        "get activities.Activity",
    )
    assert body["session"] == "default/late"
    assert body["outcome"] in ("unknown", "failed")
    await database.waiting(0)  # cancelled in the database while the lock is still held
    await no_slot_in_use()
    await database.release()
    assert await attendances(database, activity) == []
    assert (await register(client, activity, activity.free_members[:1])).json()["result"]["registered"]


@pytest.mark.parametrize("attempt", REPEAT)
async def test_a_timeout_waiting_for_a_slot_reports_that_phase(active, database, activity, monkeypatch, attempt):  # noqa: ARG001
    """The pool's only slot is held by a registration waiting for the activity's lock: a second request's timeout
    expires while it waits for the slot, before anything reaches the database."""
    monkeypatch.setattr(main, "POOL", adorm.PoolSettings(max_connections=1, acquire_timeout=10))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://demo", timeout=30) as http:
            await lock_activity(database, activity)
            first = asyncio.create_task(register(http, activity, activity.free_members[:1], session="first"))
            await database.waiting(1)
            late = await register(http, activity, activity.free_members[1:2], session="late", timeout=0.2)
            assert pool().waiting == 0
            await database.release()
            assert (await first).json()["result"]["registered"] == activity.free_members[:1]
            await no_slot_in_use()
    assert late.status_code == 504
    body = late.json()
    assert (body["phase"], body["operation"], body["outcome"], body["session"]) == (
        "waiting_for_slot",
        "begin",
        "not_started",
        "default/late",
    )
    assert await attendances(database, activity) == activity.free_members[:1]


@pytest.mark.parametrize("attempt", REPEAT)
async def test_a_client_going_away_cancels_the_waiting_statement(client, database, activity, attempt):  # noqa: ARG001
    await lock_activity(database, activity)
    request = asyncio.create_task(register(client, activity, activity.free_members[:1]))
    try:
        await database.waiting(1)
    finally:
        request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    await database.waiting(0)
    await no_slot_in_use()
    await database.release()
    assert await attendances(database, activity) == []


@pytest.mark.parametrize("attempt", REPEAT)
async def test_a_client_going_away_after_a_registration_rolls_it_back(client, database, activity, attempt):  # noqa: ARG001
    """Another transaction has inserted, not yet committed, the second member's attendance: the request registers
    the first member, then its insert of the second waits on the unique index. The client goes away; the whole
    transaction rolls back, the first member's released savepoint included."""
    a, b = activity.free_members[:2]
    await database.hold(
        "INSERT INTO activities_attendance (activity_id, member_id, status) VALUES ($1, $2, 'registered')",
        activity.pk,
        b,
    )
    request = asyncio.create_task(register(client, activity, [a, b]))
    try:
        await database.waiting(1)
    finally:
        request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    await database.waiting(0)
    await no_slot_in_use()
    await database.release()
    assert await attendances(database, activity) == []


# Settlements: select_for_update(skip_locked=True)


async def open_invoices(database) -> list[tuple[int, str]]:
    rows = await database.sql.fetch("SELECT id, total FROM billing_invoice WHERE status = 'open' ORDER BY due, id")
    return [(row["id"], f"{row['total']:.2f}") for row in rows]


async def settle(client, session="default", **params):
    return await bounded(
        client.post("/api/data/invoices/settlements", params=params, headers={data.SESSION_HEADER: session}), params
    )


async def test_a_settlement_pays_the_earliest_due_invoices_in_full(client, database):
    before = await open_invoices(database)
    response = await settle(client, limit=3)
    assert response.status_code == 200, response.text
    payments = response.json()["result"]["payments"]
    assert [(p["invoice_id"], p["amount"], p["reference"]) for p in payments] == [
        (pk, total, "settled by default") for pk, total in before[:3]
    ]
    assert await open_invoices(database) == before[3:]
    stored = await database.sql.fetch(
        "SELECT invoice_id FROM billing_payment WHERE reference = 'settled by default' ORDER BY invoice_id"
    )
    assert [row["invoice_id"] for row in stored] == sorted(pk for pk, _ in before[:3])
    await no_slot_in_use()


async def test_a_settlement_skips_an_invoice_another_transaction_locked(client, database):
    before = await open_invoices(database)
    await database.hold("SELECT id FROM billing_invoice WHERE id = $1 FOR UPDATE", before[0][0])
    response = await settle(client, limit=2, timeout=10)
    assert response.status_code == 200, response.text
    assert [p["invoice_id"] for p in response.json()["result"]["payments"]] == [pk for pk, _ in before[1:3]]
    await database.release()
    assert (await open_invoices(database))[0] == before[0]
    await no_slot_in_use()


@pytest.mark.parametrize("attempt", REPEAT)
async def test_concurrent_settlements_pay_every_invoice_at_most_once(client, database, attempt):  # noqa: ARG001
    before = dict(await open_invoices(database))
    payments_before = await database.sql.fetchval("SELECT count(*) FROM billing_payment")
    responses = await asyncio.gather(*(settle(client, session=f"s{n}", limit=3, timeout=20) for n in range(4)))
    assert [response.status_code for response in responses] == [200] * 4
    paid = [p["invoice_id"] for response in responses for p in response.json()["result"]["payments"]]
    assert len(paid) == len(set(paid))
    assert set(paid) <= set(before)
    assert await database.sql.fetchval("SELECT count(*) FROM billing_payment") == payments_before + len(paid)
    duplicates = await database.sql.fetchval(
        "SELECT count(*) FROM (SELECT invoice_id FROM billing_payment GROUP BY invoice_id HAVING count(*) > 1) d"
    )
    assert duplicates == 0
    still_open = {pk for pk, _ in await open_invoices(database)}
    assert still_open == set(before) - set(paid)
    await no_slot_in_use()


# Invoice lines: a generic foreign key written in a transaction that locks the invoice

LINE = {"description": "Extra session", "quantity": 2, "unit_price": "12.50"}


async def open_invoice(database) -> tuple[int, Decimal]:
    row = await database.sql.fetchrow("SELECT id, total FROM billing_invoice WHERE status = 'open' ORDER BY id LIMIT 1")
    return row["id"], row["total"]


async def test_adding_a_line_stores_the_content_type_and_raises_the_total(active, client, database):
    invoice, total = await open_invoice(database)
    activity = await database.sql.fetchval("SELECT min(id) FROM activities_activity")
    body = {**LINE, "item": {"model": "activities.Activity", "id": activity}}
    response = await client.post(f"/api/data/invoices/{invoice}/lines", json=body)
    assert response.status_code == 201, response.text
    result = response.json()["result"]
    assert result["invoice"]["total"] == str(total + Decimal("25.00"))
    row = await database.sql.fetchrow(
        "SELECT ct.app_label, ct.model, l.object_id FROM billing_invoiceline l"
        " JOIN django_content_type ct ON ct.id = l.content_type_id WHERE l.id = $1",
        result["line"]["id"],
    )
    assert tuple(row) == ("activities", "activity", activity)
    assert await database.sql.fetchval("SELECT total FROM billing_invoice WHERE id = $1", invoice) == total + 25
    lines = (await client.get(f"/api/data/invoices/{invoice}/lines", headers={data.DRIVER_HEADER: active})).json()
    assert (lines["result"][-1]["model"], lines["result"][-1]["item"]["id"]) == ("activities.Activity", activity)
    await no_slot_in_use()


async def test_a_line_of_a_missing_item_stores_nothing(client, database):
    invoice, total = await open_invoice(database)
    before = await database.sql.fetchval("SELECT count(*) FROM billing_invoiceline")
    body = {**LINE, "item": {"model": "clubs.Membership", "id": 999999}}
    response = await client.post(f"/api/data/invoices/{invoice}/lines", json=body)
    assert response.status_code == 404
    assert await database.sql.fetchval("SELECT count(*) FROM billing_invoiceline") == before
    assert await database.sql.fetchval("SELECT total FROM billing_invoice WHERE id = $1", invoice) == total
    await no_slot_in_use()


@pytest.mark.parametrize("attempt", REPEAT)
async def test_concurrent_lines_of_one_invoice_add_up(client, database, attempt):  # noqa: ARG001
    """The invoice's row lock orders the transactions: no raise of the total is lost."""
    invoice, total = await open_invoice(database)
    membership = await database.sql.fetchval("SELECT min(id) FROM clubs_membership")
    body = {**LINE, "item": {"model": "clubs.Membership", "id": membership}}
    responses = await asyncio.gather(
        *(
            bounded(
                client.post(f"/api/data/invoices/{invoice}/lines", json=body, headers={data.SESSION_HEADER: f"l{n}"}),
                {},
            )
            for n in range(6)
        )
    )
    assert [response.status_code for response in responses] == [201] * 6, [r.text for r in responses]
    totals = sorted(Decimal(r.json()["result"]["invoice"]["total"]) for r in responses)
    assert totals == [total + 25 * n for n in range(1, 7)]
    assert await database.sql.fetchval("SELECT total FROM billing_invoice WHERE id = $1", invoice) == total + 150
    await no_slot_in_use()


@pytest.mark.parametrize("attempt", REPEAT)
async def test_a_line_waiting_for_the_invoice_lock_is_cancelled_with_its_statement(client, database, attempt):  # noqa: ARG001
    invoice, total = await open_invoice(database)
    await database.hold("SELECT id FROM billing_invoice WHERE id = $1 FOR UPDATE", invoice)
    membership = await database.sql.fetchval("SELECT min(id) FROM clubs_membership")
    body = {**LINE, "item": {"model": "clubs.Membership", "id": membership}}
    request = asyncio.create_task(client.post(f"/api/data/invoices/{invoice}/lines", json=body))
    try:
        await database.waiting(1)
    finally:
        request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    await database.waiting(0)
    await database.release()
    await no_slot_in_use()
    assert await database.sql.fetchval("SELECT total FROM billing_invoice WHERE id = $1", invoice) == total


PRICE = Decimal("999999.99")


@pytest.mark.parametrize(
    ("quantity", "status"), [(2**31, 422), (1000, 409)], ids=["quantity_beyond_column", "total_beyond_digits"]
)
async def test_a_line_the_columns_cannot_hold_stores_nothing(client, database, quantity, status):
    invoice, total = await open_invoice(database)
    before = await database.sql.fetchval("SELECT count(*) FROM billing_invoiceline")
    membership = await database.sql.fetchval("SELECT min(id) FROM clubs_membership")
    body = {
        **LINE,
        "quantity": quantity,
        "unit_price": str(PRICE),
        "item": {"model": "clubs.Membership", "id": membership},
    }
    response = await client.post(f"/api/data/invoices/{invoice}/lines", json=body)
    assert response.status_code == status, response.text
    assert await database.sql.fetchval("SELECT count(*) FROM billing_invoiceline") == before
    assert await database.sql.fetchval("SELECT total FROM billing_invoice WHERE id = $1", invoice) == total
    if status == 409:  # a 422 answers before any operation
        await no_slot_in_use()


@pytest.mark.parametrize("attempt", REPEAT)
async def test_concurrent_lines_never_raise_the_total_beyond_its_digits(client, database, attempt):  # noqa: ARG001
    """Each line fits the total alone, two do not: under the invoice's lock exactly one is stored."""
    invoice, total = await open_invoice(database)
    before = await database.sql.fetchval("SELECT count(*) FROM billing_invoiceline")
    quantity = int((instances.MAX_TOTAL - total) // PRICE)
    membership = await database.sql.fetchval("SELECT min(id) FROM clubs_membership")
    body = {
        **LINE,
        "quantity": quantity,
        "unit_price": str(PRICE),
        "item": {"model": "clubs.Membership", "id": membership},
    }
    responses = await asyncio.gather(
        *(
            bounded(
                client.post(f"/api/data/invoices/{invoice}/lines", json=body, headers={data.SESSION_HEADER: f"o{n}"}),
                {},
            )
            for n in range(2)
        )
    )
    assert sorted(response.status_code for response in responses) == [201, 409], [r.text for r in responses]
    assert await database.sql.fetchval("SELECT count(*) FROM billing_invoiceline") == before + 1
    stored = await database.sql.fetchval("SELECT total FROM billing_invoice WHERE id = $1", invoice)
    assert stored == total + quantity * PRICE
    await no_slot_in_use()


# full_clean(): Django's validation of an instance, on the session's slot


async def test_validating_invoice_changes_reports_django_errors_and_stores_nothing(active, client, database):
    invoice = await database.sql.fetchrow("SELECT id, number, issued FROM billing_invoice ORDER BY id LIMIT 1")
    other = await database.sql.fetchval("SELECT number FROM billing_invoice WHERE id <> $1 LIMIT 1", invoice["id"])
    changes = {"number": other, "due": "2000-01-01", "total": "123456789012.50"}
    response = await client.post(f"/api/data/invoices/{invoice['id']}/validation", json=changes)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["driver"] == active
    result = body["result"]
    assert result["valid"] is False
    assert {name: [(e["code"], e["message"]) for e in errors] for name, errors in result["errors"].items()} == {
        "number": [("unique", "Invoice with this Number already exists.")],
        "total": [("max_digits", "Ensure that there are no more than 10 digits in total.")],
        "due": [(None, "The due date cannot be before the issue date.")],
    }
    assert result["errors"]["total"][0]["params"] == {"max": 10, "value": 123456789012.5}
    assert result["instance"]["number"] == other
    assert (
        await database.sql.fetchval("SELECT number FROM billing_invoice WHERE id = $1", invoice["id"])
        == (invoice["number"])
    )
    excluded = await client.post(
        f"/api/data/invoices/{invoice['id']}/validation", params={"exclude": ["number", "total"]}, json=changes
    )
    assert list(excluded.json()["result"]["errors"]) == ["due"]
    valid = (await client.post(f"/api/data/invoices/{invoice['id']}/validation", json={})).json()["result"]
    assert (valid["valid"], valid["errors"]) == (True, {})
    await no_slot_in_use()


async def test_validating_a_membership_checks_the_constraint_and_the_fee(client, database):
    existing = await database.sql.fetchrow("SELECT club_id, member_id FROM clubs_membership ORDER BY id LIMIT 1")
    body = {**dict(existing), "since": "2026-01-01", "fee": "-1"}
    result = (await client.post("/api/data/memberships/validation", json=body)).json()["result"]
    assert result["valid"] is False
    assert {name: [e["code"] for e in errors] for name, errors in result["errors"].items()} == {
        "fee": ["min_value"],
        "__all__": ["unique_together"],
    }
    assert result["errors"]["__all__"][0]["params"]["unique_check"] == ["club", "member"]
    free = await database.sql.fetchval(
        "SELECT min(id) FROM clubs_member WHERE id NOT IN (SELECT member_id FROM clubs_membership WHERE club_id = $1)",
        existing["club_id"],
    )
    fine = {**body, "member_id": free, "fee": "10.00"}
    result = (await client.post("/api/data/memberships/validation", json=fine)).json()["result"]
    assert (result["valid"], result["errors"]) == (True, {})
    assert (
        await database.sql.fetchval(
            "SELECT count(*) FROM clubs_membership WHERE member_id = $1 AND club_id = $2", free, existing["club_id"]
        )
        == 0
    )
    await no_slot_in_use()
