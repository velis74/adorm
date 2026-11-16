"""Every driver reports the violated constraint on `IntegrityError` with the same values: a duplicate key, a key
another transaction inserted while the write waited for it, and a deferred foreign key that fails at ``COMMIT``. The
pool stays usable after each. On SQLite, which reports the table and the columns of a key only, the test of
`tests.conformance.test_sqlite` replaces those that read PostgreSQL's catalogue."""

import asyncio
import contextlib
import datetime
import decimal

import asyncpg
import pytest

import adorm

from adorm import IntegrityError
from tests.clubmodels.clubs import Club, Membership
from tests.conformance.drivers import on_sqlite, WAITING

pytestmark = pytest.mark.django_db(transaction=True)

REPEAT = range(3)


def sql_only(driver):
    if driver.name == "memory":
        pytest.skip("the memory driver checks no constraint but the primary key")


@pytest.fixture
async def catalogue(club_data):
    """A connection of the test's own to the test database, which reads PostgreSQL's catalogue; the test is skipped on
    SQLite."""
    if on_sqlite():
        pytest.skip("the test reads PostgreSQL's catalogue")
    conn = await asyncpg.connect(club_data)
    try:
        yield conn
    finally:
        await conn.close()


async def unique_constraint(conn, table, column):
    return await conn.fetchval(
        "SELECT c.conname FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid"
        " AND a.attnum = ALL (c.conkey) WHERE c.conrelid = $1::regclass AND c.contype = 'u' AND a.attname = $2",
        table,
        column,
    )


async def foreign_key(conn, table, column):
    return await conn.fetchval(
        "SELECT c.conname FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid"
        " AND a.attnum = ALL (c.conkey) WHERE c.conrelid = $1::regclass AND c.contype = 'f' AND a.attname = $2",
        table,
        column,
    )


def new_club(**fields):
    values = {"name": "Twin", "slug": "twin", "founded": datetime.date(2001, 5, 1), "city": "Piran"} | fields
    return Club.objects.create(**values)


async def test_duplicate_primary_key(driver, pool):
    existing = await Club.objects.get(slug="koper-sailing")
    for _ in REPEAT:
        with pytest.raises(IntegrityError) as raised:
            await new_club(pk=existing.pk)
        exc = raised.value
        assert (exc.columns, exc.model, exc.fields) == (("id",), "clubs.Club", ("id",))
        assert (pool.size, pool.in_use) == (1, 0)
        if on_sqlite():
            assert (exc.sqlstate, exc.table_name, exc.key) == (None, "clubs_club", {})
            continue
        assert exc.sqlstate == "23505"
        assert exc.key == {"id": str(existing.pk)}
        assert exc.detail == f"Key (id)=({existing.pk}) already exists."
        assert (exc.model, exc.fields) == ("clubs.Club", ("id",))
        if driver.name != "memory":
            assert (exc.constraint_name, exc.table_name, exc.schema_name) == ("clubs_club_pkey", "clubs_club", "public")
        assert (pool.size, pool.in_use) == (1, 0)
    assert await Club.objects.filter(slug="twin").count() == 0


async def test_duplicate_unique_field_in_a_transaction(driver, pool, catalogue):
    sql_only(driver)
    constraint = await unique_constraint(catalogue, "clubs_club", "slug")
    for n in REPEAT:
        async with adorm.atomic():
            with pytest.raises(IntegrityError) as raised:
                async with adorm.atomic():
                    await new_club(name=f"Twin {n}", slug="koper-sailing")
            assert await Club.objects.filter(slug="koper-sailing").count() == 1
        exc = raised.value
        assert (exc.sqlstate, exc.constraint_name, exc.table_name) == ("23505", constraint, "clubs_club")
        assert exc.key == {"slug": "koper-sailing"}
        assert (exc.model, exc.fields) == ("clubs.Club", ("slug",))
        assert (pool.size, pool.in_use) == (1, 0)


async def test_duplicate_composite_key(driver, pool):
    sql_only(driver)
    existing = await Membership.objects.order_by("pk").first()
    with pytest.raises(IntegrityError) as raised:
        await Membership.objects.create(
            club_id=existing.club_id,
            member_id=existing.member_id,
            since=datetime.date(2026, 1, 1),
            fee=decimal.Decimal(0),
        )
    exc = raised.value
    assert (exc.model, exc.fields) == ("clubs.Membership", ("club_id", "member_id"))
    if on_sqlite():
        assert (exc.table_name, exc.columns, exc.constraint_name) == (
            "clubs_membership",
            ("club_id", "member_id"),
            None,
        )
        return
    assert (exc.constraint_name, exc.table_name) == ("clubs_membership_unique", "clubs_membership")
    assert exc.key == {"club_id": str(existing.club_id), "member_id": str(existing.member_id)}
    assert (exc.model, exc.fields) == ("clubs.Membership", ("club_id", "member_id"))
    assert (pool.size, pool.in_use) == (1, 0)


async def test_deferred_foreign_key_fails_at_commit(driver, pool, catalogue):
    sql_only(driver)
    constraint = await foreign_key(catalogue, "clubs_membership", "club_id")
    member = await Membership.objects.order_by("pk").first()
    for _ in REPEAT:
        with pytest.raises(IntegrityError) as raised:
            async with adorm.atomic():
                await Membership.objects.create(
                    club_id=999999, member_id=member.member_id, since=datetime.date(2026, 1, 1), fee=decimal.Decimal(0)
                )
        exc = raised.value
        assert (exc.sqlstate, exc.constraint_name, exc.table_name) == ("23503", constraint, "clubs_membership")
        assert exc.key == {"club_id": "999999"}
        assert (exc.model, exc.fields) == ("clubs.Membership", ("club_id",))
        assert exc.detail == 'Key (club_id)=(999999) is not present in table "clubs_club".'
        assert (pool.size, pool.in_use) == (0, 0)  # a failed commit drops the slot
        assert await Membership.objects.filter(club_id=999999).count() == 0


@pytest.mark.parametrize("winner_commits", [True, False])
async def test_a_write_waiting_for_a_concurrent_insert_of_the_same_key(driver, make_router, catalogue, winner_commits):
    """The second insert of a key waits for the transaction that inserted it first: it fails with the key when that
    transaction commits and succeeds when it rolls back."""
    sql_only(driver)
    pool = make_router(max_connections=2, acquire_timeout=5).pools[0]
    constraint = await unique_constraint(catalogue, "clubs_club", "slug")
    for n in REPEAT:
        slug = f"race-{winner_commits}-{n}"
        inserted = asyncio.Event()
        finish = asyncio.Event()

        async def first(slug=slug, inserted=inserted, finish=finish):
            with pytest.raises(RuntimeError) if not winner_commits else contextlib.nullcontext():
                async with adorm.atomic():
                    await new_club(name=f"First {slug}", slug=slug)
                    inserted.set()
                    await finish.wait()
                    if not winner_commits:
                        raise RuntimeError("roll back")

        holder = asyncio.create_task(first())
        await inserted.wait()
        second = asyncio.create_task(new_club(name=f"Second {slug}", slug=slug))
        async with asyncio.timeout(10):
            while await catalogue.fetchval(WAITING) is None:
                await asyncio.sleep(0.01)
        assert not second.done()
        finish.set()
        await holder
        if winner_commits:
            with pytest.raises(IntegrityError) as raised:
                await second
            assert (raised.value.constraint_name, raised.value.key) == (constraint, {"slug": slug})
        else:
            assert (await second).slug == slug
        assert await Club.objects.filter(slug=slug).count() == 1
        assert pool.in_use == 0
