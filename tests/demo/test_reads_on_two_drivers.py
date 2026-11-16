"""The demo's read endpoints in process on the Django driver and the native PostgreSQL driver at once, over the
demo's data set in the test database: the ``X-Adorm-Driver`` header chooses the driver of a read."""

import asyncio

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
