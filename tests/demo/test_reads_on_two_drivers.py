"""The demo's read endpoints in process on the Django driver and the native PostgreSQL driver at once, over the
demo's data set in the test database: the ``X-Adorm-Driver`` header chooses the driver of a read."""

import asyncio
import datetime

from decimal import Decimal

import asyncpg
import httpx
import pytest

import adorm

from adorm.settings import default_router
from demo.api import data, drivers, main
from demo.api.main import app
from demo.models import MODELS

REPEAT = range(3)
DRIVERS = ("django", "asyncpg")
WAITING = (
    "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND query LIKE '%clubs_member%'"
    " AND pid <> pg_backend_pid()"
)


@pytest.fixture
def real_drivers(monkeypatch):
    """The demo's Django and asyncpg drivers, Django active; skipped when either is not available."""
    entries = tuple(drivers.get(name) for name in DRIVERS)
    for entry in entries:
        reason = entry.unavailable_reason()
        if reason is not None:
            pytest.skip(f"the {entry.name} driver is not available: {reason}")
    monkeypatch.setattr(drivers, "DRIVERS", entries)
    monkeypatch.delenv(drivers.DRIVER_ENV, raising=False)


@pytest.fixture
async def database(demo_data):
    """An asyncpg connection to the seeded test database."""
    connection = await asyncpg.connect(demo_data)
    try:
        yield connection
    finally:
        await connection.close()


@pytest.fixture
async def client(real_drivers, database):  # noqa: ARG001 - the drivers over the seeded database
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://demo", timeout=30) as http:
            yield http


class MemberLock:
    """An exclusive lock on the members' table, held on a connection of its own; reads of members wait for it."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.holder: asyncpg.Connection | None = None
        self.monitor: asyncpg.Connection | None = None
        self.transaction = None

    async def hold(self) -> None:
        self.holder = await asyncpg.connect(self.url)
        self.monitor = await asyncpg.connect(self.url)
        self.transaction = self.holder.transaction()
        await self.transaction.start()
        await self.holder.execute("LOCK TABLE clubs_member IN ACCESS EXCLUSIVE MODE")

    async def release(self) -> None:
        transaction, self.transaction = self.transaction, None
        if transaction is not None:
            await transaction.rollback()

    async def waiting(self, count: int) -> None:
        """Return once exactly ``count`` statements wait for the lock; fail after 10 seconds."""
        async with asyncio.timeout(10):
            while await self.monitor.fetchval(WAITING) != count:
                await asyncio.sleep(0.01)

    async def close(self) -> None:
        await self.release()
        for connection in (self.holder, self.monitor):
            if connection is not None:
                await connection.close()


@pytest.fixture
async def member_lock(demo_data):
    lock = MemberLock(demo_data)
    await lock.hold()
    try:
        yield lock
    finally:
        await lock.close()


def on(driver: str) -> dict[str, str]:
    return {data.DRIVER_HEADER: driver}


def pool(using: str):
    router = default_router(create=False)
    return next((p for p in router.pools if p.name == using), None) if router is not None else None


def assert_no_slot_in_use() -> None:
    for p in default_router(create=False).pools:
        assert (p.name, p.in_use, p.waiting) == (p.name, 0, 0)
        assert p.size <= main.POOL.max_connections


async def read_paths(database) -> list[tuple[str, dict]]:
    member = await database.fetchval("SELECT min(id) FROM clubs_member")
    invoice = await database.fetchval(
        "SELECT min(invoice_id) FROM billing_payment"
    )  # an invoice with payments, so the reverse relation has rows
    return [
        ("/api/data/members", {"limit": 100}),
        ("/api/data/members", {"search": "NOV", "ordering": "-joined"}),
        ("/api/data/members", {"level": "beginner", "ordering": "name", "offset": 1, "limit": 3}),
        (f"/api/data/members/{member}", {}),
        (f"/api/data/invoices/{invoice}", {}),
        ("/api/data/clubs/ljubljana-rowing/summary", {}),
        ("/api/data/clubs/ljubljana-rowing/summary", {"atomic": True}),
        *AGGREGATE_PATHS,
        ("/api/data/clubs/ljubljana-rowing/roster", {}),
        ("/api/data/clubs/maribor-climbing/roster", {"atomic": True}),
        ("/api/data/members/in-bulk", {"id": [member, member + 1, 999999]}),
        ("/api/data/clubs/in-bulk", {"slug": ["koper-sailing", "nowhere", "ljubljana-rowing"]}),
        (f"/api/data/invoices/{invoice}/lines", {}),
        (f"/api/data/invoices/{invoice}/lines", {"prefetch": False}),
    ]


AGGREGATE_PATHS = [
    ("/api/data/clubs/reports", {}),
    ("/api/data/clubs/reports", {"min_active_members": 4}),
    ("/api/data/clubs/koper-sailing/report", {}),
    ("/api/data/memberships/fees", {}),
    ("/api/data/memberships/fees", {"club": "maribor-climbing"}),
    ("/api/data/memberships/fees", {"club": "nowhere"}),
    ("/api/data/activities/schedule", {}),
    ("/api/data/activities/schedule", {"kind": "training", "starts_after": "2026-03-03T00:00:00Z"}),
    ("/api/data/invoices/outstanding", {"today": "2026-03-01"}),
    ("/api/data/invoices/months", {}),
    ("/api/data/invoices/months", {"club": "koper-sailing"}),
]


async def test_every_read_answers_the_same_on_both_drivers(client, database):
    for path, params in await read_paths(database):
        default = (await client.get(path, params=params)).json()
        assert (default["session"], default["driver"]) == ("default/default", "django"), path
        for driver in DRIVERS:
            response = await client.get(path, params=params, headers=on(driver) | {data.SESSION_HEADER: "reader"})
            assert response.status_code == 200, (driver, path, response.text)
            body = response.json()
            assert (body["session"], body["driver"], body["calls"]) == (f"{driver}/reader", driver, []), path
            assert body["result"] == default["result"], (driver, path)
    assert sorted(p.name for p in default_router(create=False).pools) == ["asyncpg", "default", "django"]
    assert_no_slot_in_use()


async def test_the_reads_match_the_database(client, database):
    members = await database.fetch("SELECT id, first_name FROM clubs_member ORDER BY last_name, first_name, id")
    expected = await database.fetchrow(
        "SELECT (SELECT count(*) FROM clubs_membership m WHERE m.club_id = c.id AND m.active),"
        " (SELECT count(*) FROM activities_activity a WHERE a.club_id = c.id),"
        " (SELECT count(*) FROM billing_invoice i WHERE i.club_id = c.id AND i.status = 'open')"
        " FROM clubs_club c WHERE c.slug = 'ljubljana-rowing'"
    )
    for driver in DRIVERS:
        listed = (await client.get("/api/data/members", params={"limit": 100}, headers=on(driver))).json()["result"]
        assert [(m["id"], m["first_name"]) for m in listed] == [tuple(row) for row in members]
        summary = (await client.get("/api/data/clubs/ljubljana-rowing/summary", headers=on(driver))).json()["result"]
        assert (summary["active_memberships"], summary["activities"], summary["open_invoices"]) == tuple(expected)
        missing = await client.get("/api/data/members/999999", headers=on(driver))
        assert missing.status_code == 404
        assert missing.json() == {"detail": "Member matching query does not exist."}


@pytest.mark.parametrize("driver", DRIVERS)
async def test_the_schema_matches_on_both_drivers(client, driver):
    response = await client.get("/api/data/schema", headers={data.DRIVER_HEADER: driver})
    assert response.status_code == 200
    body = response.json()
    assert body["driver"] == driver
    assert body["result"]["problems"] == []
    assert body["result"]["models"] == [model._meta.label for model in MODELS]


async def test_compare_reads_the_page_on_both_drivers(client, database):
    body = (await client.get("/api/data/compare/members", params={"limit": 100})).json()
    assert body["same"] is True
    assert [(read["driver"], read["session"], read["error"]) for read in body["reads"]] == [
        ("django", "django/default", None),
        ("asyncpg", "asyncpg/default", None),
    ]
    assert len(body["reads"][0]["result"]) == await database.fetchval("SELECT count(*) FROM clubs_member")
    assert_no_slot_in_use()


@pytest.mark.parametrize("attempt", REPEAT)
async def test_compare_runs_both_reads_at_once(client, member_lock, attempt):  # noqa: ARG001
    """Both reads wait for the lock at the same time: they run concurrently on the two drivers."""
    request = asyncio.create_task(client.get("/api/data/compare/members"))
    try:
        await member_lock.waiting(2)
    finally:
        await member_lock.release()
    body = (await request).json()
    assert body["same"] is True
    assert_no_slot_in_use()


async def test_a_full_pool_of_one_driver_does_not_hold_up_the_other(real_drivers, database, member_lock, monkeypatch):  # noqa: ARG001
    monkeypatch.setattr(main, "POOL", adorm.PoolSettings(max_connections=2, acquire_timeout=1))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://demo", timeout=30) as http:
            blocked = [asyncio.create_task(http.get("/api/data/members", headers=on("django"))) for _ in range(2)]
            try:
                await member_lock.waiting(2)
                late = await http.get("/api/data/members", headers=on("django") | {data.SESSION_HEADER: "late"})
                invoice = await database.fetchval("SELECT min(id) FROM billing_invoice")
                other = await http.get(f"/api/data/invoices/{invoice}", headers=on("asyncpg"))
                default = await http.get(f"/api/data/invoices/{invoice}")
            finally:
                await member_lock.release()
            responses = await asyncio.gather(*blocked)
            assert_no_slot_in_use()
    assert late.status_code == 503
    assert (late.json()["error"], late.json()["session"]) == ("SessionTimeout", "django/late")
    assert (other.status_code, other.json()["driver"]) == (200, "asyncpg")
    assert (default.status_code, default.json()["session"]) == (200, "default/default")
    assert [response.status_code for response in responses] == [200, 200]


@pytest.mark.parametrize("driver", DRIVERS)
@pytest.mark.parametrize("attempt", REPEAT)
async def test_a_cancelled_read_cancels_its_statement(client, member_lock, driver, attempt):  # noqa: ARG001
    """The client goes away while the read waits in the database: the statement is cancelled while the lock is still
    held, and the slot goes back to the pool or leaves it."""
    request = asyncio.create_task(client.get("/api/data/members/1", headers=on(driver)))
    try:
        await member_lock.waiting(1)
    finally:
        request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    await member_lock.waiting(0)
    async with asyncio.timeout(10):
        while pool(driver).in_use:
            await asyncio.sleep(0.01)
    await member_lock.release()
    response = await client.get("/api/data/compare/members")
    assert response.json()["same"] is True
    assert_no_slot_in_use()


@pytest.mark.parametrize("attempt", REPEAT)
async def test_concurrent_reads_on_both_drivers_leave_no_slot_behind(client, database, attempt):  # noqa: ARG001
    """More concurrent reads than the pools have slots, on three profiles of two drivers, transactions included:
    each transaction's tasks share its one connection."""
    paths = await read_paths(database)
    headers = [{}, on("django"), on("asyncpg")]
    requests = [(path, params, header) for path, params in paths for header in headers] * 2
    responses = await asyncio.gather(
        *(client.get(path, params=params, headers=header) for path, params, header in requests)
    )
    assert [response.status_code for response in responses] == [200] * len(requests), [
        response.text for response in responses if response.status_code != 200
    ]
    results: dict[str, list] = {}
    for (path, params, _), response in zip(requests, responses, strict=True):
        results.setdefault(f"{path} {sorted(params.items())}", []).append(response.json()["result"])
    for key, found in results.items():
        assert all(result == found[0] for result in found), key
    assert_no_slot_in_use()


# Aggregates and related objects


async def test_every_read_runs_on_the_profile_the_header_names(real_drivers, database):  # noqa: ARG001
    """Only the named driver's pool is created: no operation of a read runs on the session that was current when the
    request arrived."""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://demo", timeout=30) as http:
            for path, params in await read_paths(database):
                response = await http.get(path, params=params, headers=on("asyncpg"))
                assert response.status_code == 200, (path, response.text)
                assert [p.name for p in default_router(create=False).pools] == ["asyncpg"], path
            assert_no_slot_in_use()


@pytest.mark.parametrize("driver", DRIVERS)
async def test_the_aggregates_match_the_database(client, database, driver):
    reports = (await client.get("/api/data/clubs/reports", headers=on(driver))).json()["result"]
    expected = await database.fetch(
        "SELECT c.slug,"
        " (SELECT count(*) FROM clubs_membership m WHERE m.club_id = c.id AND m.active) AS active,"
        " (SELECT coalesce(sum(fee), 0) FROM clubs_membership m WHERE m.club_id = c.id AND m.active) AS fees,"
        " (SELECT count(*) FROM billing_invoice i WHERE i.club_id = c.id AND i.status = 'open') AS open,"
        " EXISTS (SELECT 1 FROM activities_activity a WHERE a.club_id = c.id AND a.kind = 'competition') AS comp"
        " FROM clubs_club c ORDER BY active DESC, c.slug"
    )
    assert [
        (r["club"]["slug"], r["active_members"], Decimal(r["fees"]), r["open_invoices"], r["has_competitions"])
        for r in reports
    ] == [tuple(row) for row in expected]
    assert [r["size"] for r in reports] == [
        "large" if row["active"] >= 5 else "medium" if row["active"] >= 3 else "small" for row in expected
    ]
    having = (await client.get("/api/data/clubs/reports", params={"min_active_members": 4}, headers=on(driver))).json()
    assert [r["club"]["slug"] for r in having["result"]] == [row["slug"] for row in expected if row["active"] >= 4]

    fees = (await client.get("/api/data/memberships/fees", headers=on(driver))).json()["result"]
    row = await database.fetchrow(
        "SELECT count(*), count(DISTINCT club_id), count(*) FILTER (WHERE fee > 0), sum(fee), avg(fee), min(fee),"
        " max(fee) FROM clubs_membership WHERE active"
    )
    assert (fees["memberships"], fees["clubs"], fees["paying"]) == tuple(row[:3])
    assert [Decimal(fees[name]) for name in ("total", "average", "lowest", "highest")] == list(row[3:])
    assert Decimal(fees["spread"]) == row[6] - row[5]

    months = (await client.get("/api/data/invoices/months", headers=on(driver))).json()["result"]
    rows = await database.fetch(
        "SELECT date_trunc('month', issued)::date, count(*), sum(total), count(*) FILTER (WHERE status = 'open'),"
        " coalesce(sum(total) FILTER (WHERE status = 'paid'), 0) FROM billing_invoice GROUP BY 1 ORDER BY 1"
    )
    assert [
        (datetime.date.fromisoformat(m["month"]), m["invoices"], Decimal(m["billed"]), m["open"], Decimal(m["paid"]))
        for m in months
    ] == [tuple(row) for row in rows]

    schedule = (await client.get("/api/data/activities/schedule", headers=on(driver))).json()["result"]
    rows = await database.fetch(
        "SELECT a.id, c.name, a.capacity - count(t.id) FILTER (WHERE t.status <> 'cancelled')"
        " FROM activities_activity a JOIN clubs_club c ON c.id = a.club_id"
        " LEFT JOIN activities_attendance t ON t.activity_id = a.id GROUP BY a.id, c.name ORDER BY a.starts_at, a.id"
    )
    assert [(s["activity"]["id"], s["club"], s["free_places"]) for s in schedule] == [tuple(row) for row in rows]


@pytest.mark.parametrize("driver", DRIVERS)
async def test_the_related_objects_match_the_database(client, database, driver):
    roster = (await client.get("/api/data/clubs/ljubljana-rowing/roster", headers=on(driver))).json()["result"]
    members = await database.fetch(
        "SELECT m.member_id FROM clubs_membership m JOIN clubs_club c ON c.id = m.club_id"
        " WHERE c.slug = 'ljubljana-rowing' AND m.active ORDER BY m.since, m.id"
    )
    assert [m["member"]["id"] for m in roster["members"]] == [row[0] for row in members]
    participants = await database.fetch(
        "SELECT a.id, count(t.id) FROM activities_activity a JOIN clubs_club c ON c.id = a.club_id"
        " LEFT JOIN activities_attendance t ON t.activity_id = a.id WHERE c.slug = 'ljubljana-rowing'"
        " GROUP BY a.id, a.starts_at ORDER BY a.starts_at, a.id"
    )
    assert [(a["activity"]["id"], len(a["participants"])) for a in roster["activities"]] == [
        tuple(row) for row in participants
    ]

    lines = await database.fetch(
        "SELECT l.invoice_id, l.id, ct.app_label || '.' || ct.model, l.object_id FROM billing_invoiceline l"
        " JOIN django_content_type ct ON ct.id = l.content_type_id ORDER BY l.invoice_id, l.id LIMIT 6"
    )
    for invoice in sorted({row[0] for row in lines}):
        for prefetch in (True, False):
            body = await client.get(
                f"/api/data/invoices/{invoice}/lines", params={"prefetch": prefetch}, headers=on(driver)
            )
            assert [(b["line"]["id"], b["model"].lower(), b["item"]["id"]) for b in body.json()["result"]] == [
                (row[1], row[2], row[3]) for row in lines if row[0] == invoice
            ]
    clubs = await client.get(
        "/api/data/clubs/in-bulk", params={"slug": ["nowhere", "koper-sailing"]}, headers=on(driver)
    )
    assert list(clubs.json()["result"]["found"]) == ["koper-sailing"]
    assert clubs.json()["result"]["missing"] == ["nowhere"]
    assert_no_slot_in_use()


@pytest.mark.parametrize("driver", DRIVERS)
async def test_a_line_whose_item_is_gone_answers_null(client, database, driver):
    invoice, content_type = await database.fetchrow(
        "SELECT invoice_id, content_type_id FROM billing_invoiceline ORDER BY id LIMIT 1"
    )
    gone = await database.fetchval(
        "INSERT INTO billing_invoiceline (invoice_id, content_type_id, object_id, description, quantity, unit_price)"
        " VALUES ($1, $2, 999999, 'Gone', 1, 1) RETURNING id",
        invoice,
        content_type,
    )
    answers = []
    for prefetch in (True, False):
        response = await client.get(
            f"/api/data/invoices/{invoice}/lines", params={"prefetch": prefetch}, headers=on(driver)
        )
        assert response.status_code == 200, response.text
        answers.append(response.json()["result"])
    assert answers[0] == answers[1]
    dangling = next(line for line in answers[0] if line["line"]["id"] == gone)
    assert (dangling["model"], dangling["item"], dangling["club"]) == (None, None, None)
    assert all(line["item"] is not None for line in answers[0] if line["line"]["id"] != gone)
    assert_no_slot_in_use()


@pytest.mark.parametrize("driver", DRIVERS)
@pytest.mark.parametrize("atomic", [False, True])
@pytest.mark.parametrize("attempt", REPEAT)
async def test_a_roster_cancelled_while_a_prefetch_waits_frees_its_slot(client, member_lock, driver, atomic, attempt):  # noqa: ARG001
    """The prefetch of the members waits for the table lock, after the club and its memberships are read; the client
    goes away. The waiting statement is cancelled while the lock is still held and no slot stays in use."""
    request = asyncio.create_task(
        client.get("/api/data/clubs/ljubljana-rowing/roster", params={"atomic": atomic}, headers=on(driver))
    )
    try:
        await member_lock.waiting(1)
    finally:
        request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    await member_lock.waiting(0)
    async with asyncio.timeout(10):
        while pool(driver).in_use:
            await asyncio.sleep(0.01)
    await member_lock.release()
    response = await client.get("/api/data/clubs/ljubljana-rowing/roster", headers=on(driver))
    assert response.status_code == 200
    assert_no_slot_in_use()
