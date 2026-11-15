"""The demo's memory driver on the executor: queries, writes, transactions, row locks and the call trace."""

import asyncio
import datetime
import uuid

from decimal import Decimal

import pytest

import adorm

from adorm.drivers import Capabilities
from adorm.operations import Create, Select, Update
from adorm.query import F, Q, Query
from adorm.settings import default_router
from demo.api import memory
from demo.models.activities import Activity, Attendance, Competition
from demo.models.billing import Invoice, Payment
from demo.models.clubs import Club, Member, Membership
from tests.demo.memory_data import executor, START, tables, until

REPEAT = range(5)


@pytest.fixture
async def db():
    async with executor(memory.MemoryDatabase(tables())) as database:
        yield database


def ignore(_event):
    """An ``emit`` callable that drops events."""


def pool():
    return default_router(create=False).pools[0]


def other_session():
    return adorm.create_session("default", "other")


async def names(queryset):
    return [member.first_name async for member in queryset]


# Driver


def test_capabilities_name_what_the_driver_evaluates():
    caps = memory.MemoryDriver.capabilities
    assert isinstance(caps, Capabilities)
    assert {f"lookup.{name}" for name in memory.LOOKUPS} | {"lookup.isnull"} <= caps.constructs
    assert caps.savepoints
    assert caps.cancellation
    assert "call" not in caps.operations
    assert caps.connection_options == frozenset()


def test_a_driver_without_a_database_uses_the_module_database():
    assert memory.MemoryDriver().database is memory.DATABASE
    database = memory.MemoryDatabase()
    assert memory.MemoryDriver(database).database is database


async def test_a_profile_connect_hook_is_refused():
    profile = adorm.ConnectionProfile("default", memory.MemoryDriver(memory.MemoryDatabase()), connect=lambda p: p)
    with pytest.raises(adorm.InterfaceError, match="opens no target connection"):
        await profile.get_driver().open_connection(profile, ignore)


async def test_connections_are_numbered_in_the_order_they_open():
    driver = memory.MemoryDriver(memory.MemoryDatabase())
    first = await driver.open_connection(None, ignore)
    second = await driver.open_connection(None, ignore)
    assert (first.number, second.number) == (1, 2)
    await first.close()
    assert not first.is_usable()
    assert second.is_usable()
    with pytest.raises(adorm.InterfaceError, match="closed"):
        await first.begin()


def test_load_replaces_the_tables_and_restarts_the_sequences():
    database = memory.MemoryDatabase({"clubs.Club": tables()["clubs.Club"]})
    assert [row["slug"] for row in database.rows("clubs.Club")] == ["rowing", "sailing"]
    assert database.next_pk("clubs.Club") == 3
    database.load({"clubs.Member": []})
    assert list(database.tables) == ["clubs.Member"]
    assert database.next_pk("clubs.Member") == 1


# Reads


async def test_filters_with_lookups_q_and_negation(db):  # noqa: ARG001
    assert await names(Member.objects.filter(last_name="Novak").order_by("id")) == ["Ana", "David"]
    assert await names(Member.objects.filter(first_name__icontains="AN").order_by("id")) == ["Ana", "Bojan"]
    assert await names(Member.objects.filter(Q(level="advanced") | Q(id__in=[3, 4])).order_by("id")) == [
        "Bojan",
        "Cvetka",
        "David",
    ]
    assert await names(Member.objects.exclude(level="beginner").order_by("-id")) == ["Cvetka", "Bojan"]
    assert await names(Member.objects.filter(joined__range=(START - datetime.timedelta(days=2), START))) == [
        "Ana",
        "Bojan",
    ]
    assert await names(Member.objects.filter(email__endswith="@example.com", first_name__regex="^[AB]")) == [
        "Ana",
        "Bojan",
    ]
    assert await names(Member.objects.filter(tags__contains=["junior"])) == ["Ana"]


async def test_a_negation_keeps_rows_whose_field_is_null_as_django_does(db):  # noqa: ARG001
    born_before = Member.objects.filter(birth_date__lt=datetime.date(2000, 1, 1))
    assert await names(born_before) == ["Cvetka"]
    assert await names(Member.objects.exclude(birth_date__lt=datetime.date(2000, 1, 1))) == ["Ana", "Bojan", "David"]
    # A NULL field on the right-hand side of a lookup counts the same.
    regatta = [activity.name async for activity in Activity.objects.filter(~Q(club_id__lt=F("coach_id")))]
    assert regatta == ["Regatta"]
    # Under two negations a NULL field leaves the lookup unknown again.
    assert await names(Member.objects.filter(~Q(~Q(birth_date__lt=datetime.date(2000, 1, 1))))) == ["Cvetka"]
    assert await names(Member.objects.filter(birth_date__isnull=True)) == ["Bojan", "David"]
    either = Member.objects.filter(Q(birth_date__isnull=True) | Q(birth_date__lt=datetime.date(2000, 1, 1)))
    assert await names(either.order_by("id")) == ["Bojan", "Cvetka", "David"]


async def test_values_that_do_not_compare_raise_a_programming_error(db):  # noqa: ARG001
    instant = datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC)
    with pytest.raises(adorm.ProgrammingError, match="operator does not exist") as lookup:
        await Member.objects.filter(birth_date__gt=instant).count()
    assert lookup.value.sqlstate == "42883"
    assert isinstance(lookup.value.__cause__, TypeError)
    with pytest.raises(adorm.ProgrammingError) as arithmetic:
        await Member.objects.filter(pk=1).update(level=F("level") - 1)
    assert arithmetic.value.sqlstate == "42883"


async def test_ordering_places_nulls_as_postgresql_does(db):  # noqa: ARG001
    assert await names(Member.objects.order_by("birth_date", "id")) == ["Cvetka", "Ana", "Bojan", "David"]
    assert await names(Member.objects.order_by("-birth_date", "id")) == ["Bojan", "David", "Ana", "Cvetka"]
    nulls_first = Member.objects.order_by(F("birth_date").asc(nulls_first=True), "id")
    assert await names(nulls_first) == ["Bojan", "David", "Cvetka", "Ana"]
    nulls_last = Member.objects.order_by(F("birth_date").desc(nulls_last=True), "-id")
    assert await names(nulls_last) == ["Ana", "Cvetka", "David", "Bojan"]


async def test_slices_values_and_distinct(db):  # noqa: ARG001
    assert await names(Member.objects.order_by("id")[1:3]) == ["Bojan", "Cvetka"]
    rows = [row async for row in Member.objects.filter(pk=1).values("first_name", "level")]
    assert rows == [{"first_name": "Ana", "level": "beginner"}]
    levels = [level async for level in Member.objects.order_by("level").values_list("level", flat=True).distinct()]
    assert levels == ["advanced", "beginner", "intermediate"]
    pairs = [pair async for pair in Membership.objects.order_by("id").values_list("club", "member")]
    assert pairs == [(1, 1), (1, 2), (1, 3)]


async def test_get_first_last_count_and_exists(db):  # noqa: ARG001
    assert (await Member.objects.get(pk=2)).first_name == "Bojan"
    with pytest.raises(Member.DoesNotExist):
        await Member.objects.get(pk=99)
    with pytest.raises(Member.MultipleObjectsReturned):
        await Member.objects.get(last_name="Novak")
    assert (await Member.objects.first()).id == 1
    assert (await Member.objects.last()).id == 4
    assert (await Member.objects.order_by("first_name").last()).first_name == "David"
    assert await Member.objects.filter(last_name="Nobody").first() is None
    assert await Member.objects.filter(last_name="Novak").count() == 2
    assert await Member.objects.filter(level="advanced").exists()
    assert not await Member.objects.filter(level="intermediate", tags__contains=["x"]).exists()


async def test_relations_load_through_their_columns(db):  # noqa: ARG001
    membership = await Membership.objects.get(pk=2)
    assert (await membership.club).slug == "rowing"
    assert (await membership.member).first_name == "Bojan"
    club = await Club.objects.get(slug="rowing")
    assert [m.id async for m in club.memberships.order_by("-id")] == [3, 2, 1]
    assert await Membership.objects.filter(club=1, member=2).count() == 1


async def test_relation_paths_and_annotations_are_not_supported(db):  # noqa: ARG001
    with pytest.raises(adorm.NotSupportedError, match="path.nested"):
        await Membership.objects.filter(club__slug="rowing").count()
    with pytest.raises(adorm.NotSupportedError, match="does not follow relations"):
        await Club.objects.filter(memberships=1).count()
    with pytest.raises(adorm.NotSupportedError, match="clause.annotate"):
        [row async for row in Member.objects.annotate(name=F("first_name"))]
    with pytest.raises(adorm.NotSupportedError, match="function.Lower"):
        await Member.objects.filter(first_name=adorm.Lower("last_name")).count()
    with pytest.raises(adorm.ProgrammingError, match="no field 'nickname'"):
        await Member.objects.filter(nickname="x").count()


async def test_a_label_without_a_table_is_not_supported(db):
    del db.tables["clubs.Club"]
    with pytest.raises(adorm.NotSupportedError, match="no table for clubs.Club"):
        await Club.objects.count()
    with pytest.raises(adorm.ProgrammingError, match="no adorm model is registered"):
        await adorm.current_session().execute(Select(Query("nowhere.Thing")))


# Writes


async def test_create_fills_defaults_and_the_primary_key(db):
    member = await Member.objects.create(first_name="Eva", last_name="Kos", email="eva@x", joined=START)
    assert member.id == 5
    assert db.rows("clubs.Member")[-1] == {
        "id": 5,
        "first_name": "Eva",
        "last_name": "Kos",
        "email": "eva@x",
        "birth_date": None,
        "level": "beginner",
        "joined": START,
        "tags": [],
    }
    payment = await Payment.objects.create(invoice_id=1, amount=Decimal("5.00"), paid_at=START)
    assert isinstance(payment.code, uuid.UUID)
    assert db.rows("billing.Payment") == [
        {"code": payment.code, "invoice_id": 1, "amount": Decimal("5.00"), "paid_at": START, "reference": ""}
    ]


async def test_create_without_a_required_value_or_with_a_taken_key_fails(db):  # noqa: ARG001
    session = adorm.current_session()
    with pytest.raises(adorm.IntegrityError, match="'email' has no value") as missing:
        await session.execute(Create("clubs.Member", {"first_name": "X", "last_name": "Y", "joined": START}))
    assert missing.value.sqlstate == "23502"
    with pytest.raises(adorm.IntegrityError, match="primary key 1 exists") as taken:
        await session.execute(Create("clubs.Club", {"id": 1, "name": "X", "slug": "x", "founded": START.date(),
                                                    "city": "Y"}))  # fmt: skip
    assert taken.value.sqlstate == "23505"


async def test_save_updates_inserts_and_honours_update_fields(db):
    member = await Member.objects.get(pk=1)
    member.level = "advanced"
    member.first_name = "Anica"
    await member.save(update_fields=["level"])
    assert db.tables["clubs.Member"][1]["level"] == "advanced"
    assert db.tables["clubs.Member"][1]["first_name"] == "Ana"
    await member.save()
    assert db.tables["clubs.Member"][1]["first_name"] == "Anica"
    new = Member(first_name="Iva", last_name="Turk", email="iva@x", joined=START)
    await new.save()
    assert new.id == 5
    assert db.tables["clubs.Member"][5]["first_name"] == "Iva"
    ghost = Member(id=40, first_name="Gal", last_name="Kralj", email="gal@x", joined=START)
    await ghost.save()
    assert 40 in db.tables["clubs.Member"]
    stale = Member(id=41, first_name="Ola", last_name="Kralj", email="ola@x", joined=START)
    with pytest.raises(adorm.DatabaseError, match="update_fields did not affect any rows"):
        await stale.save(update_fields=["first_name"])
    with pytest.raises(adorm.DatabaseError, match="Forced update did not affect any rows"):
        await stale.save(force_update=True)
    with pytest.raises(adorm.IntegrityError):
        await ghost.save(force_insert=True)


async def test_update_evaluates_field_arithmetic(db):
    assert await Activity.objects.filter(kind="training").update(capacity=F("capacity") * 3 + 1) == 1
    assert db.tables["activities.Activity"][1]["capacity"] == 7
    assert await Activity.objects.filter(coach_id__isnull=True).update(coach_id=None, price=F("price") - 5) == 1
    assert db.tables["activities.Activity"][2]["price"] == Decimal("20.00")


async def test_a_delete_of_a_referenced_row_is_refused_and_deletes_nothing(db):
    assert await Invoice.objects.filter(status="draft").delete() == (1, {"billing.Invoice": 1})
    # Member 4 is no longer referenced, member 2 still is (a membership, an activity's coach).
    with pytest.raises(adorm.NotSupportedError, match="does not cascade deletes: .* references clubs.Member 2"):
        await Member.objects.filter(pk__in=[4, 2]).delete()
    assert [row["id"] for row in db.rows("clubs.Member")] == [1, 2, 3, 4]
    async with adorm.atomic():
        with pytest.raises(adorm.NotSupportedError):
            await Member.objects.filter(pk__in=[4, 2]).delete()
        # The refusal changed nothing, so the transaction goes on.
        assert await Member.objects.filter(pk=4).delete() == (1, {"clubs.Member": 1})
    assert [row["id"] for row in db.rows("clubs.Member")] == [1, 2, 3]


@pytest.fixture
def competition(db):
    """Activity 2 is also a competition: the child's row repeats the parent's fields."""
    db.tables["activities.Competition"] = {
        2: {**db.tables["activities.Activity"][2], "activity_ptr_id": 2, "discipline": "Eights", "entry_deadline": None}
    }
    return db


async def test_updates_reach_the_rows_shared_through_multi_table_inheritance(competition):
    db = competition
    assert await Activity.objects.filter(pk=2).update(capacity=F("capacity") + 1) == 1
    assert db.tables["activities.Competition"][2]["capacity"] == 6
    assert await Competition.objects.filter(pk=2).update(discipline="Pairs", price=Decimal("1.00")) == 1
    assert db.tables["activities.Activity"][2]["price"] == Decimal("1.00")
    assert "discipline" not in db.tables["activities.Activity"][2]
    async with adorm.atomic():
        regatta = await Competition.objects.get(pk=2)
        regatta.capacity = 9
        await regatta.save(update_fields=["capacity"])
        assert (await Activity.objects.get(pk=2)).capacity == 9
        assert db.tables["activities.Activity"][2]["capacity"] == 6
    assert db.tables["activities.Activity"][2]["capacity"] == 9
    # Activity 1 has no competition row.
    assert await Activity.objects.filter(pk=1).update(capacity=3) == 1
    assert list(db.tables["activities.Competition"]) == [2]


async def test_inserting_and_deleting_multi_table_rows_is_refused(competition):
    db = competition
    with pytest.raises(adorm.NotSupportedError, match="multi-table inheritance child of activities.Activity"):
        await Competition.objects.create(
            club_id=1, name="Cup", starts_at=START, duration=datetime.timedelta(hours=1), capacity=4, discipline="X"
        )
    with pytest.raises(adorm.NotSupportedError, match="multi-table inheritance child"):
        await Competition.objects.filter(pk=2).delete()
    with pytest.raises(adorm.NotSupportedError, match="activities.Competition.activity_ptr_id references"):
        await Activity.objects.filter(pk=2).delete()
    assert list(db.tables["activities.Competition"]) == [2]
    assert list(db.tables["activities.Activity"]) == [1, 2]
    assert await Competition.objects.filter(pk=99).delete() == (0, {})


async def test_delete_counts_the_rows_without_cascading(db):
    assert await Invoice.objects.filter(status="draft").delete() == (1, {"billing.Invoice": 1})
    assert await Invoice.objects.filter(status="draft").delete() == (0, {})
    assert await Club.objects.filter(pk=2).delete() == (1, {"clubs.Club": 1})
    assert [row["id"] for row in db.rows("billing.Invoice")] == [1, 2]


async def test_get_or_create(db):
    attendance, created = await Attendance.objects.get_or_create(activity_id=1, member_id=1)
    assert (attendance.id, created) == (1, False)
    attendance, created = await Attendance.objects.get_or_create(
        activity_id=1, member_id=2, defaults={"status": "attended"}
    )
    assert created
    assert attendance.status == "attended"
    assert db.tables["activities.Attendance"][attendance.id]["member_id"] == 2


# Transactions


async def test_commit_publishes_and_rollback_discards(db):
    async with adorm.atomic():
        await Member.objects.filter(pk=1).update(level="advanced")
        assert db.tables["clubs.Member"][1]["level"] == "beginner"
        assert (await Member.objects.get(pk=1)).level == "advanced"
    assert db.tables["clubs.Member"][1]["level"] == "advanced"
    with pytest.raises(RuntimeError):
        async with adorm.atomic():
            eva = await Member.objects.create(first_name="Eva", last_name="Kos", email="eva@x", joined=START)
            await Member.objects.filter(pk=4).update(level="advanced")
            await Member.objects.filter(pk=eva.pk).delete()
            raise RuntimeError
    assert [row["id"] for row in db.rows("clubs.Member")] == [1, 2, 3, 4]
    assert db.tables["clubs.Member"][4]["level"] == "beginner"
    created = await Member.objects.create(first_name="Eva", last_name="Kos", email="eva@x", joined=START)
    assert created.id == 6  # a rollback does not return sequence values


async def test_uncommitted_writes_are_invisible_to_other_sessions(db):  # noqa: ARG001
    inside = asyncio.Event()
    finish = asyncio.Event()

    async def writer():
        async with adorm.atomic():
            await Member.objects.create(first_name="Eva", last_name="Kos", email="eva@x", joined=START)
            inside.set()
            await finish.wait()

    task = asyncio.create_task(writer())
    await inside.wait()
    with other_session().activate():
        assert await Member.objects.count() == 4
    finish.set()
    await task
    assert await Member.objects.count() == 5


async def test_a_savepoint_rolls_back_only_its_writes(db):
    with memory.tracing() as calls:
        async with adorm.atomic():
            await Member.objects.filter(pk=1).update(level="advanced")
            with pytest.raises(ValueError, match="undo"):
                async with adorm.atomic():
                    await Member.objects.filter(pk=2).update(level="beginner")
                    await Member.objects.create(first_name="Eva", last_name="Kos", email="eva@x", joined=START)
                    raise ValueError("undo")
            async with adorm.atomic():
                await Member.objects.filter(pk=3).update(level="advanced")
    assert [row["level"] for row in db.rows("clubs.Member")] == ["advanced", "advanced", "advanced", "beginner"]
    sequence = [entry.call for entry in calls]
    assert sequence[:4] == ["acquire", "begin", "update clubs.Member", "savepoint adorm_sp_1"]
    assert "rollback to savepoint adorm_sp_1" in sequence
    assert sequence[-3:] == ["release savepoint adorm_sp_2", "commit", "release"]


async def test_savepoint_calls_need_a_transaction():
    connection = await memory.MemoryDriver(memory.MemoryDatabase()).open_connection(None, ignore)
    with pytest.raises(adorm.ProgrammingError, match="no transaction is open"):
        await connection.savepoint("s1")
    await connection.begin()
    with pytest.raises(adorm.ProgrammingError, match="already open"):
        await connection.begin()
    with pytest.raises(adorm.ProgrammingError, match="savepoint 's9' does not exist"):
        await connection.rollback_to_savepoint("s9")
    await connection.rollback()
    with pytest.raises(adorm.ProgrammingError, match="no transaction is open"):
        await connection.commit()


# Row locks: concurrent transactions


@pytest.mark.parametrize("attempt", REPEAT)
async def test_a_conflicting_update_waits_for_the_commit_and_rechecks_the_row(db, attempt):  # noqa: ARG001
    """Two transactions pay the same open invoice: the second update waits for the first to commit, then finds the
    invoice paid and changes nothing."""
    locked = asyncio.Event()
    release = asyncio.Event()

    async def first():
        async with adorm.atomic():
            assert await Invoice.objects.filter(pk=1, status="open").update(status="paid") == 1
            locked.set()
            await release.wait()

    async def second():
        await locked.wait()
        with other_session().activate():
            async with adorm.atomic():
                return await Invoice.objects.filter(pk=1, status="open").update(status="paid")

    tasks = [asyncio.create_task(first()), asyncio.create_task(second())]
    await until(lambda: len(db.waiters) == 1)
    assert not tasks[1].done()
    release.set()
    _, changed = await asyncio.gather(*tasks)
    assert changed == 0
    assert db.tables["billing.Invoice"][1]["status"] == "paid"
    assert db.locks == {}
    assert db.waiters == {}
    assert pool().in_use == 0


@pytest.mark.parametrize("attempt", REPEAT)
async def test_a_wait_for_a_rolled_back_lock_proceeds(db, attempt):  # noqa: ARG001
    locked = asyncio.Event()
    release = asyncio.Event()

    async def first():
        with pytest.raises(LookupError):
            async with adorm.atomic():
                await Invoice.objects.filter(pk=1).update(status="void")
                locked.set()
                await release.wait()
                raise LookupError

    async def second():
        await locked.wait()
        with other_session().activate():
            return await Invoice.objects.filter(pk=1, status="open").update(status="paid")

    tasks = [asyncio.create_task(first()), asyncio.create_task(second())]
    await until(lambda: len(db.waiters) == 1)
    release.set()
    _, changed = await asyncio.gather(*tasks)
    assert changed == 1  # the autocommit update waited, then saw the open invoice
    assert db.tables["billing.Invoice"][1]["status"] == "paid"


@pytest.mark.parametrize("attempt", REPEAT)
async def test_a_lock_cycle_raises_a_deadlock_error_in_the_closing_transaction(db, attempt):  # noqa: ARG001
    both_locked = asyncio.Barrier(2)

    async def lock_then_take(first_pk, second_pk, session_name):
        with adorm.create_session("default", session_name).activate():
            async with adorm.atomic():
                await Member.objects.filter(pk=first_pk).update(level="advanced")
                await both_locked.wait()
                await Member.objects.filter(pk=second_pk).update(level="intermediate")

    one = asyncio.create_task(lock_then_take(1, 2, "one"))
    await until(lambda: both_locked.n_waiting == 1)
    two = asyncio.create_task(lock_then_take(2, 1, "two"))
    results = await asyncio.gather(one, two, return_exceptions=True)
    errors = [result for result in results if isinstance(result, BaseException)]
    assert len(errors) == 1
    assert isinstance(errors[0], adorm.OperationalError)
    assert errors[0].sqlstate == "40P01"
    winner = 1 if results[0] is None else 2
    loser = 3 - winner
    assert db.tables["clubs.Member"][winner]["level"] == "advanced"
    assert db.tables["clubs.Member"][loser]["level"] == "intermediate"
    assert db.locks == {}
    assert db.waiters == {}
    assert pool().in_use == 0


@pytest.mark.parametrize("attempt", REPEAT)
async def test_cancelling_a_lock_wait_rolls_back_and_frees_the_slot(db, attempt):  # noqa: ARG001
    locked = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with adorm.atomic():
            await Member.objects.filter(pk=1).update(level="advanced")
            locked.set()
            await release.wait()

    async def waiter():
        with other_session().activate():
            async with adorm.atomic():
                await Member.objects.create(first_name="Eva", last_name="Kos", email="eva@x", joined=START)
                await Member.objects.filter(pk=1).update(level="intermediate")

    held = asyncio.create_task(holder())
    await locked.wait()
    waiting = asyncio.create_task(waiter())
    await until(lambda: len(db.waiters) == 1)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert db.waiters == {}
    release.set()
    await held
    assert db.tables["clubs.Member"][1]["level"] == "advanced"
    assert [row["first_name"] for row in db.rows("clubs.Member")] == ["Ana", "Bojan", "Cvetka", "David"]
    assert db.locks == {}
    await until(lambda: pool().in_use == 0)


@pytest.mark.parametrize("attempt", REPEAT)
async def test_an_insert_of_a_taken_primary_key_waits_and_then_fails(db, attempt):  # noqa: ARG001
    """Two transactions insert the same explicit primary key: the second waits for the first one's row lock and,
    after the commit, raises instead of replacing the committed row."""
    inserted = asyncio.Event()
    release = asyncio.Event()

    def club(name):
        return Create("clubs.Club", {"id": 9, "name": name, "slug": name, "founded": START.date(), "city": "Bled"})

    async def first():
        async with adorm.atomic():
            await adorm.current_session().execute(club("first"))
            inserted.set()
            await release.wait()

    async def second():
        await inserted.wait()
        with other_session().activate():
            async with adorm.atomic():
                await adorm.current_session().execute(club("second"))

    tasks = [asyncio.create_task(first()), asyncio.create_task(second())]
    await until(lambda: len(db.waiters) == 1)
    release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert results[0] is None
    assert isinstance(results[1], adorm.IntegrityError)
    assert results[1].sqlstate == "23505"
    assert db.tables["clubs.Club"][9]["name"] == "first"
    assert db.locks == {}


def _member_update(pk, level):
    return Update(Query("clubs.Member").filter(pk=pk), {"level": level})


async def test_a_lock_owner_that_starts_a_new_transaction_waits_for_a_waiter_that_has_not_resumed():
    """``b`` commits while ``a`` waits for its lock, then wants the row ``a`` holds before ``a`` resumes: ``b``
    waits for ``a``, without a deadlock."""
    database = memory.MemoryDatabase(tables())
    driver = memory.MemoryDriver(database)
    a = await driver.open_connection(None, ignore)
    b = await driver.open_connection(None, ignore)
    await b.begin()
    await b.execute(_member_update(1, "advanced"))
    await a.begin()
    await a.execute(_member_update(2, "advanced"))

    async def a_flow():
        await a.execute(_member_update(1, "intermediate"))
        await a.commit()

    async def b_flow():
        await b.commit()
        await b.begin()
        await b.execute(_member_update(2, "intermediate"))
        await b.commit()

    a_task = asyncio.create_task(a_flow())
    await until(lambda: len(database.waiters) == 1)
    await asyncio.gather(a_task, asyncio.create_task(b_flow()))
    assert database.tables["clubs.Member"][1]["level"] == "intermediate"
    assert database.tables["clubs.Member"][2]["level"] == "intermediate"
    assert database.locks == {}
    assert database.waiters == {}


async def test_rolling_back_to_a_savepoint_releases_the_locks_taken_after_it():
    database = memory.MemoryDatabase(tables())
    driver = memory.MemoryDriver(database)
    holder = await driver.open_connection(None, ignore)
    other = await driver.open_connection(None, ignore)
    await holder.begin()
    await holder.execute(_member_update(2, "advanced"))
    await holder.savepoint("s1")
    await holder.execute(_member_update(1, "advanced"))
    waiting = asyncio.create_task(other.execute(_member_update(1, "intermediate")))
    await until(lambda: len(database.waiters) == 1)
    await holder.rollback_to_savepoint("s1")
    async with asyncio.timeout(5):
        assert await waiting == 1
    assert database.locks == {("clubs.Member", 2): holder}
    assert database.tables["clubs.Member"][1]["level"] == "intermediate"
    await holder.commit()
    assert database.tables["clubs.Member"][2]["level"] == "advanced"
    assert database.locks == {}


async def test_closing_a_connection_in_a_transaction_releases_its_locks():
    database = memory.MemoryDatabase(tables())
    driver = memory.MemoryDriver(database)
    connection = await driver.open_connection(None, ignore)
    await connection.begin()
    await connection.execute(Update(Query("clubs.Member").filter(pk=1), {"level": "advanced"}))
    assert database.locks == {("clubs.Member", 1): connection}
    await connection.close()
    assert database.locks == {}
    assert database.tables["clubs.Member"][1]["level"] == "beginner"


# Trace


async def test_tracing_collects_the_calls_of_its_context_and_of_tasks_created_in_it(db):  # noqa: ARG001
    with memory.tracing() as calls:
        async with adorm.atomic():
            async with asyncio.TaskGroup() as group:
                group.create_task(Member.objects.count())
                group.create_task(Club.objects.count())
    assert [entry.call for entry in calls][:2] == ["acquire", "begin"]
    assert sorted(entry.call for entry in calls[2:4]) == ["count clubs.Club", "count clubs.Member"]
    assert [entry.call for entry in calls][4:] == ["commit", "release"]
    assert {entry.slot for entry in calls} == {1}
    assert {entry.session for entry in calls} == {"default/default"}


@pytest.mark.parametrize("attempt", REPEAT)
async def test_concurrent_flows_keep_their_traces_apart(db, attempt):  # noqa: ARG001
    gate = asyncio.Barrier(2)

    async def flow(session_name, model):
        with adorm.create_session("default", session_name).activate(), memory.tracing() as calls:
            async with adorm.atomic():
                await gate.wait()
                await model.objects.count()
        return calls

    first, second = await asyncio.gather(flow("a", Member), flow("b", Club))
    assert [entry.call for entry in first] == ["acquire", "begin", "count clubs.Member", "commit", "release"]
    assert [entry.call for entry in second] == ["acquire", "begin", "count clubs.Club", "commit", "release"]
    assert {entry.session for entry in first} == {"default/a"}
    assert {entry.session for entry in second} == {"default/b"}
    assert {entry.slot for entry in first}.isdisjoint({entry.slot for entry in second})


async def test_calls_outside_tracing_are_not_recorded(db):  # noqa: ARG001
    with memory.tracing() as calls:
        pass
    await Member.objects.count()
    assert calls == []
