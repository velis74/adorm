"""``await instance.refresh_from_db(fields=None)`` on every bundled driver: it reloads the instance's field values from
the database on the instance's session, inside that session's transaction when one is active, limits the reload to
``fields`` and leaves deferred fields deferred without them, clears cached relations as Django's ``refresh_from_db()``
does and raises the model's ``DoesNotExist`` when the row is gone. Another session's writes are made with the Django ORM
on a thread of its own."""

import datetime
import decimal

import pytest

import adorm

from adorm import ObjectDoesNotExist
from tests.clubmodels.activities import Activity, Competition
from tests.clubmodels.clubs import Club, Member
from tests.conformance.drivers import on_sqlite
from tests.djangoproject.activities import models as dj_activities
from tests.djangoproject.clubs import models as dj_clubs
from tests.library_data import in_thread

pytestmark = pytest.mark.django_db(transaction=True)

REPEAT = range(3)


@pytest.fixture(autouse=True)
def bundled(driver, pool):
    """Every test runs on a bundled driver's pool; the memory driver is skipped, as the Django ORM's writes do not
    reach it."""
    if driver.name == "memory":
        pytest.skip("the memory driver keeps its rows apart from the database the Django ORM writes")
    return pool


class RolledBackError(Exception):
    """Raised to roll a block back."""


def outside(fn):
    """``fn()`` with the Django ORM on a thread of its own: a write another session commits."""
    return in_thread(fn)


def update_club(pk, **values):
    return outside(lambda: dj_clubs.Club.objects.filter(pk=pk).update(**values))


async def test_refresh_shows_another_sessions_update(bundled):
    club = await Club.objects.get(slug="koper-sailing")
    for n in REPEAT:
        assert await update_club(club.pk, name=f"Koper {n}", city=f"Piran {n}", disciplines=["Kite", str(n)]) == 1
        assert club.name != f"Koper {n}"
        assert await club.refresh_from_db() is None
        assert (club.name, club.city, club.disciplines) == (f"Koper {n}", f"Piran {n}", ["Kite", str(n)])
        assert club.slug == "koper-sailing"
        assert (bundled.size, bundled.in_use) == (1, 0)


async def test_refresh_replaces_values_assigned_since_the_load():
    club = await Club.objects.get(slug="koper-sailing")
    club.city = "Unsaved"
    club.address = {"street": "Unsaved 1"}
    await club.refresh_from_db()
    assert (club.city, club.address) == ("Koper", {})
    assert await Club.objects.filter(city="Unsaved").count() == 0


async def test_fields_limit_what_is_reloaded():
    club = await Club.objects.get(slug="koper-sailing")
    await update_club(club.pk, name="Renamed", city="Izola")
    await club.refresh_from_db(fields=["city"])
    assert (club.name, club.city) == ("Koper Sailing Club", "Izola")
    await club.refresh_from_db(fields=("name",))
    assert (club.name, club.city) == ("Renamed", "Izola")


async def test_fields_take_any_iterable_of_names():
    club = await Club.objects.get(slug="koper-sailing")
    await update_club(club.pk, name="Renamed", city="Izola", founded=datetime.date(2010, 1, 2))
    await club.refresh_from_db(fields={"city", "founded"})
    assert (club.name, club.city, club.founded) == ("Koper Sailing Club", "Izola", datetime.date(2010, 1, 2))
    await club.refresh_from_db(fields=(name for name in ["name"]))
    assert club.name == "Renamed"


async def test_empty_fields_reload_nothing(recorded):
    club = await Club.objects.get(slug="koper-sailing")
    await update_club(club.pk, city="Izola")
    recorded.clear()
    await club.refresh_from_db(fields=[])
    assert club.city == "Koper"
    assert recorded == []


async def test_without_fields_deferred_fields_stay_deferred():
    club = await Club.objects.only("name").get(slug="koper-sailing")
    deferred = club.get_deferred_fields()
    assert "city" in deferred
    await update_club(club.pk, name="Renamed", city="Izola")
    await club.refresh_from_db()
    assert club.name == "Renamed"
    assert club.get_deferred_fields() == deferred
    with pytest.raises(AttributeError):
        _ = club.city


async def test_fields_load_deferred_fields():
    club = await Club.objects.defer("city", "address").get(slug="koper-sailing")
    assert {"city", "address"} <= club.get_deferred_fields()
    await update_club(club.pk, name="Renamed", city="Izola")
    await club.refresh_from_db(fields=["city"])
    assert club.city == "Izola"
    assert club.name == "Koper Sailing Club"
    assert "city" not in club.get_deferred_fields()
    assert "address" in club.get_deferred_fields()
    assert "city" in club.model_fields_set
    with pytest.raises(AttributeError):
        _ = club.address


async def test_a_reloaded_field_is_saved_afterwards():
    club = await Club.objects.only("name").get(slug="koper-sailing")
    await update_club(club.pk, city="Izola")
    await club.refresh_from_db(fields=["city"])
    club.name = "Renamed"
    await club.save()
    stored = await Club.objects.get(pk=club.pk)
    assert (stored.name, stored.city) == ("Renamed", "Izola")


async def test_null_values_are_reloaded_both_ways():
    member = await Member.objects.get(first_name="Ana")
    birth_date = member.birth_date
    assert birth_date is not None
    await outside(lambda: dj_clubs.Member.objects.filter(pk=member.pk).update(birth_date=None))
    await member.refresh_from_db()
    assert member.birth_date is None
    await outside(lambda: dj_clubs.Member.objects.filter(pk=member.pk).update(birth_date=birth_date))
    await member.refresh_from_db(fields=["birth_date"])
    assert member.birth_date == birth_date


async def test_reloaded_values_match_the_django_orms():
    member = await Member.objects.get(first_name="Eva")
    joined = datetime.datetime(2020, 2, 3, 4, 5, 6, tzinfo=datetime.UTC)
    await outside(
        lambda: dj_clubs.Member.objects.filter(pk=member.pk).update(
            last_name="Zupan", level="beginner", joined=joined, tags={"roles": ["coach"]}, birth_date=None
        )
    )
    await member.refresh_from_db()

    def stored():
        row = dj_clubs.Member.objects.get(pk=member.pk)
        return {name: getattr(row, name) for name in Member.model_fields}

    expected = await outside(stored)
    assert {name: getattr(member, name) for name in Member.model_fields} == expected
    assert member.joined == joined


async def test_an_instance_the_application_built_is_loaded():
    stored = await Club.objects.get(slug="koper-sailing")
    club = Club(pk=stored.pk, name="Stale", slug="stale", founded=datetime.date(2000, 1, 1), city="Nowhere")
    await club.refresh_from_db()
    assert club.model_dump() == stored.model_dump()


async def test_a_deleted_row_raises_does_not_exist(bundled):
    club = await Club.objects.get(slug="koper-sailing")
    before = club.model_dump()

    def delete():
        dj_clubs.Club.objects.filter(pk=club.pk).delete()

    await outside(delete)
    for _ in REPEAT:
        with pytest.raises(Club.DoesNotExist, match="Club matching query does not exist") as raised:
            await club.refresh_from_db()
        assert isinstance(raised.value, ObjectDoesNotExist)
        with pytest.raises(Club.DoesNotExist):
            await club.refresh_from_db(fields=["city"])
        assert club.model_dump() == before
        assert (bundled.size, bundled.in_use) == (1, 0)
    assert await Club.objects.count() == 2


async def test_unknown_field_names_are_refused(bundled):
    club = await Club.objects.get(slug="koper-sailing")
    with pytest.raises(ValueError, match="nonexistent"):
        await club.refresh_from_db(fields=["nonexistent"])
    with pytest.raises(ValueError, match="__"):
        await club.refresh_from_db(fields=["memberships__role"])
    with pytest.raises(TypeError):
        await club.refresh_from_db(fields="city")
    assert club.city == "Koper"
    assert (bundled.size, bundled.in_use) == (1, 0)


async def update_and_refresh(club, city):
    """Update the club's city and refresh it in a transaction, then roll the transaction back."""
    async with adorm.atomic():
        assert await Club.objects.filter(pk=club.pk).update(city=city) == 1
        await club.refresh_from_db()
        assert club.city == city
        raise RolledBackError


async def delete_and_refresh(club):
    """Delete the club's row and refresh the club in a transaction, then roll the transaction back."""
    async with adorm.atomic():
        await Club.objects.filter(pk=club.pk).delete()
        with pytest.raises(Club.DoesNotExist):
            await club.refresh_from_db()
        raise RolledBackError


async def test_refresh_inside_a_transaction_reads_the_transactions_view(bundled):
    club = await Club.objects.get(slug="koper-sailing")
    for n in REPEAT:
        with pytest.raises(RolledBackError):
            await update_and_refresh(club, f"Inside {n}")
        await club.refresh_from_db()
        assert club.city == "Koper"
        assert (bundled.size, bundled.in_use) == (1, 0)


async def test_a_row_the_transaction_deleted_is_gone_until_the_rollback(bundled):
    club = await Club.objects.get(slug="koper-sailing")
    with pytest.raises(RolledBackError):
        await delete_and_refresh(club)
    await club.refresh_from_db()
    assert club.city == "Koper"
    assert (bundled.size, bundled.in_use) == (1, 0)


async def test_refresh_runs_on_the_instances_session(bundled):
    other = adorm.create_session("default", "refresh-other")
    mine = await Club.objects.get(slug="koper-sailing")
    theirs = await other.bind(Club).get(slug="koper-sailing")
    for n in REPEAT:
        async with other.atomic():
            assert await other.bind(Club).filter(pk=theirs.pk).update(city=f"Other {n}") == 1
            await theirs.refresh_from_db()
            assert theirs.city == f"Other {n}"
            if not on_sqlite():
                await mine.refresh_from_db()
                assert mine.city != f"Other {n}"
        await mine.refresh_from_db()
        assert mine.city == f"Other {n}"
    assert bundled.in_use == 0


async def test_a_foreign_key_cache_is_cleared_when_its_column_is_reloaded():
    activity = await Activity.objects.select_related("coach").get(name="Morning sculling")
    coach = await activity.coach
    await outside(lambda: dj_clubs.Member.objects.filter(pk=coach.pk).update(first_name="Bob"))
    await activity.refresh_from_db(fields=["name"])
    assert await activity.coach is coach
    await activity.refresh_from_db()
    reloaded = await activity.coach
    assert reloaded is not coach
    assert reloaded.first_name == "Bob"


async def test_a_changed_foreign_key_is_followed():
    activity = await Activity.objects.select_related("coach").get(name="Morning sculling")
    assert (await activity.coach).first_name == "Bojan"
    eva = await Member.objects.get(first_name="Eva")
    await outside(lambda: dj_activities.Activity.objects.filter(pk=activity.pk).update(coach_id=eva.pk))
    await activity.refresh_from_db(fields=["coach_id"])
    assert activity.coach_id == eva.pk
    assert (await activity.coach).first_name == "Eva"
    await outside(lambda: dj_activities.Activity.objects.filter(pk=activity.pk).update(coach_id=None))
    await activity.refresh_from_db()
    assert activity.coach_id is None
    assert await activity.coach is None


async def test_a_reverse_one_to_one_cache_is_cleared_without_fields():
    activity = await Activity.objects.get(name="Spring regatta")
    competition = await activity.competition
    await outside(
        lambda: dj_activities.Competition.objects.filter(pk=competition.pk).update(discipline="Single sculls")
    )
    await activity.refresh_from_db(fields=["name"])
    assert await activity.competition is competition
    await activity.refresh_from_db()
    assert (await activity.competition).discipline == "Single sculls"


async def test_a_reverse_one_to_one_cache_is_cleared_when_named():
    activity = await Activity.objects.get(name="Spring regatta")
    competition = await activity.competition
    await outside(lambda: dj_activities.Competition.objects.filter(pk=competition.pk).update(discipline="Fours"))
    await activity.refresh_from_db(fields=["name", "competition"])
    assert (await activity.competition).discipline == "Fours"


async def test_prefetched_rows_are_cleared_without_fields():
    club = await Club.objects.prefetch_related("memberships").get(slug="koper-sailing")
    assert await club.memberships.count() == 3

    def drop_one():
        dj_clubs.Membership.objects.filter(club_id=club.pk, role="member").order_by("pk").first().delete()

    await outside(drop_one)
    await club.refresh_from_db(fields=["city"])
    assert await club.memberships.count() == 3
    await club.refresh_from_db()
    assert await club.memberships.count() == 2
    assert len([m async for m in club.memberships]) == 2


async def test_naming_a_prefetched_relation_clears_only_it():
    club = await Club.objects.prefetch_related("memberships").get(slug="koper-sailing")
    await outside(lambda: dj_clubs.Membership.objects.filter(club_id=club.pk).update(active=False))
    await update_club(club.pk, city="Izola")
    await club.refresh_from_db(fields=["memberships"])
    assert club.city == "Koper"
    assert [m.active async for m in club.memberships] == [False, False, False]


async def test_a_multi_table_child_reloads_its_parents_and_its_own_fields():
    competition = await Competition.objects.get(name="Regional bouldering cup")
    deadline = datetime.date(2026, 4, 1)

    def change():
        dj_activities.Activity.objects.filter(pk=competition.pk).update(name="Cup", price=decimal.Decimal("9.50"))
        dj_activities.Competition.objects.filter(pk=competition.pk).update(entry_deadline=deadline)

    await outside(change)
    await competition.refresh_from_db()
    assert (competition.name, competition.price, competition.entry_deadline) == (
        "Cup",
        decimal.Decimal("9.50"),
        deadline,
    )
    await outside(lambda: dj_activities.Competition.objects.filter(pk=competition.pk).update(entry_deadline=None))
    await competition.refresh_from_db(fields=["entry_deadline"])
    assert competition.entry_deadline is None
    assert competition.name == "Cup"
