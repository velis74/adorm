"""Transforms in lookup keys, ``F`` paths, ``values()`` and ``order_by()`` on every driver: date and time parts of
date and datetime fields, keys and indexes inside JSON fields, and the JSON lookups ``has_key``, ``has_keys``,
``has_any_keys``, ``contains`` and ``contained_by``. Each scenario runs the same query in adorm and in the Django ORM
over the club data set and compares the rows; the Django driver and the native PostgreSQL driver run every scenario, a
driver that does not declare a transform refuses it with `NotSupportedError`. Unknown transforms and lookups raise
`FieldError` with a suggestion, and leave the slot free."""

from __future__ import annotations

import asyncio
import datetime

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from django.db import connection, NotSupportedError as DjangoNotSupportedError
from django.db.models import Count as DjangoCount, F as DjangoF, Q as DjangoQ

import adorm

from adorm import F, FieldError, NotSupportedError, Q
from adorm.query import Count
from tests.clubmodels.activities import Activity, Attendance
from tests.clubmodels.clubs import Club, Member
from tests.djangoproject.activities import models as dj_activities
from tests.djangoproject.clubs import models as dj_clubs
from tests.library_data import in_thread

pytestmark = pytest.mark.django_db(transaction=True)

UTC = datetime.UTC
TRANSFORMING_DRIVERS = ("django", "asyncpg")
"""The drivers that run every scenario; another driver may refuse one with `NotSupportedError`."""

ANA_JOINED = datetime.datetime(2026, 3, 2, 23, 30, 15, tzinfo=UTC)
ADDRESSES = {
    "ljubljana-rowing": {
        "street": "Veslaška pot 1",
        "owner": {"name": "Ana", "since": 2019},
        "items": ["boat", "oar"],
        "a": 1,
        "rating": 5,
        "year": 2020,
    },
    "maribor-climbing": {
        "street": "Stenska ulica 4",
        "owner": {"name": "Bojan"},
        "items": ["rope"],
        "a": 2,
        "note": None,
    },
    "koper-sailing": {},
}
"""The clubs' addresses for these scenarios: nested objects, a list, numbers, a JSON ``null``, a key named like a
transform, and an empty object."""


def _extend_club_data() -> None:
    """Club addresses with nested JSON, a member without a birth date and a member who joined late in the evening."""
    from django.db import transaction

    with transaction.atomic():
        for slug, address in ADDRESSES.items():
            dj_clubs.Club.objects.filter(slug=slug).update(address=address)
        dj_clubs.Member.objects.filter(first_name="Klemen").update(birth_date=None)
        dj_clubs.Member.objects.filter(first_name="Ana").update(joined=ANA_JOINED)


@pytest.fixture
async def club_data(club_data):
    """The club data set with `_extend_club_data()` applied, before any driver reads it."""
    await in_thread(_extend_club_data)
    return club_data


ADORM = SimpleNamespace(Q=Q, F=F, Count=Count)
DJANGO = SimpleNamespace(Q=DjangoQ, F=DjangoF, Count=DjangoCount)

MODELS = {
    "Member": (Member, dj_clubs.Member, "first_name"),
    "Club": (Club, dj_clubs.Club, "slug"),
    "Activity": (Activity, dj_activities.Activity, "name"),
    "Attendance": (Attendance, dj_activities.Attendance, "id"),
}


@dataclass(frozen=True)
class Scenario:
    """``run()`` reads with adorm, ``django()`` reads the same with the Django ORM; both return plain values.
    ``expected`` is the result over the extended club data set on PostgreSQL (``None``: not stated)."""

    id: str
    run: Callable[[], Awaitable[Any]]
    django: Callable[[], Any]
    expected: Any = None


SCENARIOS: list[Scenario] = []


def same_rows(
    id: str,  # noqa: A002 - the scenario's id
    model: str,
    build: Callable[[Any], Any],
    expected: Any = None,
    *,
    attribute: str | None = "",
    keep_order: bool = False,
) -> None:
    """A scenario reading ``build(Model.objects)`` in adorm and ``build(DjangoModel.objects)`` in Django; a ``build``
    with a second argument gets `ADORM` or `DJANGO`, whose ``Q``, ``F`` and ``Count`` belong to its ORM. Each row
    becomes its ``attribute`` (the model's name field by default; ``None``: the row itself, for ``values()`` and
    ``values_list()``); the rows are sorted unless ``keep_order``."""
    adorm_model, django_model, default = MODELS[model]
    name = default if attribute == "" else attribute
    with_expressions = build.__code__.co_argcount == 2

    def project(row: Any) -> Any:
        return row if name is None else getattr(row, name)

    def finish(values: list[Any]) -> list[Any]:
        return values if keep_order else sorted(values, key=repr)

    async def run() -> list[Any]:
        query = build(adorm_model.objects, ADORM) if with_expressions else build(adorm_model.objects)
        return finish([project(row) async for row in query])

    def django() -> list[Any]:
        query = build(django_model.objects, DJANGO) if with_expressions else build(django_model.objects)
        return finish([project(row) for row in query])

    SCENARIOS.append(Scenario(id, run, django, expected))


def scenario(django: Callable[[], Any], expected: Any = None):
    """Registers an adorm coroutine function as a scenario mirrored by ``django``."""

    def register(run: Callable[[], Awaitable[Any]]) -> Callable[[], Awaitable[Any]]:
        SCENARIOS.append(Scenario(run.__name__, run, django, expected))
        return run

    return register


# Date transforms of a date field (birth_date: the 10th of month i + 1 of year 1970 + 3i; Klemen's is NULL)

same_rows("year", "Member", lambda o: o.filter(birth_date__year=1982), ["Eva"])
same_rows("year_gte", "Member", lambda o: o.filter(birth_date__year__gte=1994), ["Igor", "Jana", "Lara"])
same_rows("year_lt", "Member", lambda o: o.filter(birth_date__year__lt=1975), ["Ana", "Bojan"])
same_rows("year_range", "Member", lambda o: o.filter(birth_date__year__range=(1980, 1990)), ["Eva", "Filip", "Gaja"])
same_rows("year_in", "Member", lambda o: o.filter(birth_date__year__in=[1970, 2003, 1800]), ["Ana", "Lara"])
same_rows("year_in_nothing", "Member", lambda o: o.filter(birth_date__year__in=[]), [])
same_rows("year_none_is_null", "Member", lambda o: o.filter(birth_date__year=None), ["Klemen"])
same_rows("year_isnull", "Member", lambda o: o.filter(birth_date__year__isnull=True), ["Klemen"])
same_rows(
    "year_isnull_false",
    "Member",
    lambda o: o.filter(birth_date__year__isnull=False),
    ["Ana", "Bojan", "Cvetka", "David", "Eva", "Filip", "Gaja", "Hana", "Igor", "Jana", "Lara"],
)
same_rows("year_no_row", "Member", lambda o: o.filter(birth_date__year=1500), [])
same_rows("month", "Member", lambda o: o.filter(birth_date__month=5), ["Eva"])
same_rows(
    "exclude_month_in",
    "Member",
    lambda o: o.exclude(birth_date__month__in=[1, 2]),
    ["Cvetka", "David", "Eva", "Filip", "Gaja", "Hana", "Igor", "Jana", "Klemen", "Lara"],
)
same_rows(
    "exclude_year_gte_keeps_null",
    "Member",
    lambda o: o.exclude(birth_date__year__gte=1980),
    ["Ana", "Bojan", "Cvetka", "David", "Klemen"],
)
same_rows("quarter", "Member", lambda o: o.filter(birth_date__quarter=2), ["David", "Eva", "Filip"])
same_rows(
    "day",
    "Member",
    lambda o: o.filter(birth_date__day=10),
    ["Ana", "Bojan", "Cvetka", "David", "Eva", "Filip", "Gaja", "Hana", "Igor", "Jana", "Lara"],
)
same_rows("week_of_a_date", "Member", lambda o: o.filter(birth_date__week=2))
same_rows("week_day_of_a_date", "Member", lambda o: o.filter(birth_date__week_day=1))
same_rows("iso_week_day_of_a_date", "Member", lambda o: o.filter(birth_date__iso_week_day=7))
same_rows("iso_year_of_a_date", "Member", lambda o: o.filter(birth_date__iso_year__lt=1980))
same_rows(
    "year_or_month",
    "Member",
    lambda o, x: o.filter(x.Q(birth_date__year=1970) | x.Q(birth_date__month=12)),
    ["Ana", "Lara"],
)
same_rows(
    "two_transforms_of_one_field",
    "Member",
    lambda o: o.filter(birth_date__year__gte=1980, birth_date__month__lte=7),
    ["Eva", "Filip", "Gaja"],
)

# Date and time transforms of a datetime field (joined: 2026-03-02 07:00 UTC minus 40 days per member; Ana's is
# 2026-03-02 23:30:15 UTC)

same_rows("date", "Member", lambda o: o.filter(joined__date=datetime.date(2026, 3, 2)), ["Ana"])
same_rows("date_lt", "Member", lambda o: o.filter(joined__date__lt=datetime.date(2025, 6, 1)))
same_rows(
    "date_range",
    "Member",
    lambda o: o.filter(joined__date__range=(datetime.date(2025, 12, 1), datetime.date(2026, 2, 1))),
)
same_rows("datetime_year", "Member", lambda o: o.filter(joined__year=2025))
same_rows("datetime_month_in", "Member", lambda o: o.exclude(joined__month__in=[1, 2]))
same_rows("hour", "Member", lambda o: o.filter(joined__hour=23), ["Ana"])
same_rows("hour_in", "Member", lambda o: o.filter(joined__hour__in=[7, 8]))
same_rows("minute", "Member", lambda o: o.filter(joined__minute=30), ["Ana"])
same_rows("second_gt", "Member", lambda o: o.filter(joined__second__gt=0), ["Ana"])
same_rows("time", "Member", lambda o: o.filter(joined__time=datetime.time(23, 30, 15)), ["Ana"])
same_rows("time_lt", "Member", lambda o: o.filter(joined__time__lt=datetime.time(12)))
same_rows("week", "Member", lambda o: o.filter(joined__week=10))
same_rows("week_day", "Member", lambda o: o.filter(joined__week_day=2))
same_rows("iso_week_day", "Member", lambda o: o.filter(joined__iso_week_day__in=[1, 7]))
same_rows("iso_year", "Member", lambda o: o.filter(joined__iso_year=2025))
same_rows("quarter_of_a_datetime", "Member", lambda o: o.filter(joined__quarter=1))
same_rows("day_of_a_datetime", "Member", lambda o: o.filter(joined__day__gte=15))

# Transforms after relations and of nullable datetimes

same_rows(
    "across_a_relation",
    "Member",
    lambda o: o.filter(memberships__club__founded__year__lt=1950),
    ["Ana", "Bojan", "Cvetka", "David", "Lara"],
)
same_rows("week_day_of_activities", "Activity", lambda o: o.filter(starts_at__week_day__in=[2, 3]))
same_rows(
    "activities_of_a_club_by_date",
    "Activity",
    lambda o: o.filter(club__slug="ljubljana-rowing", starts_at__date__gte=datetime.date(2026, 3, 3)),
)
same_rows("checked_in_hour", "Attendance", lambda o: o.filter(checked_in_at__hour=7))
same_rows("checked_in_date_isnull", "Attendance", lambda o: o.filter(checked_in_at__date__isnull=True))
same_rows("checked_in_date_exclude", "Attendance", lambda o: o.exclude(checked_in_at__date=datetime.date(2026, 3, 2)))
same_rows(
    "member_by_attendance_date",
    "Member",
    lambda o: o.filter(attendances__checked_in_at__date=datetime.date(2026, 3, 2)).distinct(),
    ["Ana", "Cvetka"],
)

# Transforms in F paths, values(), values_list(), order_by() and grouping

same_rows(
    "annotate_a_transform",
    "Member",
    lambda o, x: o.annotate(born=x.F("birth_date__year")).filter(born__gte=2000).values_list("first_name", "born"),
    [("Lara", 2003)],
    attribute=None,
)
same_rows(
    "transform_compared_with_a_transform",
    "Member",
    lambda o, x: o.filter(joined__month=x.F("birth_date__month")),
)
same_rows(
    "transform_compared_with_arithmetic",
    "Member",
    lambda o, x: o.filter(birth_date__year__lt=x.F("joined__year") - 40),
)
same_rows(
    "values_of_a_transform",
    "Member",
    lambda o: o.order_by("first_name").values("first_name", "birth_date__year"),
    attribute=None,
    keep_order=True,
)
same_rows(
    "values_list_of_a_transform",
    "Member",
    lambda o: o.values_list("joined__month", flat=True).distinct().order_by("joined__month"),
    attribute=None,
    keep_order=True,
)
same_rows(
    "order_by_a_transform",
    "Member",
    lambda o: o.order_by("-joined__hour", "-first_name"),
    keep_order=True,
)
same_rows(
    "order_by_a_transform_with_nulls",
    "Member",
    lambda o: o.order_by("birth_date__month", "first_name"),
    keep_order=True,
)
same_rows(
    "order_by_a_transform_across_a_relation",
    "Activity",
    lambda o: o.order_by("club__founded__year", "starts_at__day", "name"),
    keep_order=True,
)
same_rows(
    "distinct_on_a_datetime_transform",
    "Member",
    lambda o: o.order_by("joined__year", "first_name").distinct("joined__year").values_list("first_name", flat=True),
    attribute=None,
    keep_order=True,
)
same_rows(
    "distinct_on_seconds_of_a_datetime",
    "Member",
    lambda o: (
        o.order_by("joined__second", "first_name").distinct("joined__second").values_list("first_name", flat=True)
    ),
    attribute=None,
    keep_order=True,
)
same_rows(
    "group_by_a_transform",
    "Member",
    lambda o, x: (
        o.values("joined__year").annotate(n=x.Count("pk")).order_by("joined__year").values_list("joined__year", "n")
    ),
    attribute=None,
    keep_order=True,
)

# JSON key and index transforms

same_rows("json_nested_key", "Club", lambda o: o.filter(address__owner__name="Ana"), ["ljubljana-rowing"])
same_rows(
    "json_nested_key_icontains", "Club", lambda o: o.filter(address__owner__name__icontains="BO"), ["maribor-climbing"]
)
same_rows("json_list_index", "Club", lambda o: o.filter(address__items__0="boat"), ["ljubljana-rowing"])
same_rows("json_list_index_missing", "Club", lambda o: o.filter(address__items__1__isnull=True))
same_rows("json_key_present", "Club", lambda o: o.filter(address__owner__isnull=False))
same_rows("json_number_gt", "Club", lambda o: o.filter(address__a__gt=1), ["maribor-climbing"])
same_rows("json_number_in", "Club", lambda o: o.filter(address__a__in=[1, 3]), ["ljubljana-rowing"])
same_rows("json_key_named_like_a_transform", "Club", lambda o: o.filter(address__year=2020), ["ljubljana-rowing"])
same_rows("json_null_value", "Club", lambda o: o.filter(address__note=None))
same_rows("json_exclude_null_value", "Club", lambda o: o.exclude(address__note=None))
same_rows("json_null_value_by_iexact", "Club", lambda o: o.filter(address__note__iexact=None))
same_rows("json_missing_key", "Club", lambda o: o.filter(address__note__isnull=True))
same_rows("json_exclude_nested_key", "Club", lambda o: o.exclude(address__owner__name="Ana"))
same_rows("json_list_first_item", "Member", lambda o: o.filter(tags__0="coach"), ["Bojan", "Eva"])
same_rows("json_list_second_item", "Member", lambda o: o.filter(tags__1="board"), ["Eva"])
same_rows("json_empty_list", "Member", lambda o: o.filter(tags__0__isnull=True))
same_rows(
    "json_values_of_keys",
    "Club",
    lambda o: o.order_by("slug").values_list("slug", "address__owner__name"),
    [("koper-sailing", None), ("ljubljana-rowing", "Ana"), ("maribor-climbing", "Bojan")],
    attribute=None,
    keep_order=True,
)
same_rows(
    "json_annotate_a_key",
    "Club",
    lambda o, x: o.annotate(items=x.F("address__items")).order_by("slug").values_list("slug", "items"),
    attribute=None,
    keep_order=True,
)
same_rows("json_order_by_a_key", "Club", lambda o: o.order_by("address__a", "slug"), keep_order=True)
same_rows(
    "json_order_by_a_key_descending", "Club", lambda o: o.order_by("-address__owner__name", "slug"), keep_order=True
)

# JSON lookups

same_rows("has_key", "Club", lambda o: o.filter(address__has_key="owner"), ["ljubljana-rowing", "maribor-climbing"])
same_rows("has_key_nested", "Club", lambda o: o.filter(address__owner__has_key="since"), ["ljubljana-rowing"])
same_rows("has_key_of_a_null_value", "Club", lambda o: o.filter(address__has_key="note"), ["maribor-climbing"])
same_rows("exclude_has_key", "Club", lambda o: o.exclude(address__has_key="owner"), ["koper-sailing"])
same_rows("has_keys", "Club", lambda o: o.filter(address__has_keys=["owner", "items"]))
same_rows("has_keys_partly", "Club", lambda o: o.filter(address__has_keys=["owner", "note"]), ["maribor-climbing"])
same_rows(
    "has_any_keys",
    "Club",
    lambda o: o.filter(address__has_any_keys=["note", "rating"]),
    ["ljubljana-rowing", "maribor-climbing"],
)
same_rows("has_any_keys_none_present", "Club", lambda o: o.filter(address__has_any_keys=["nothing", "else"]), [])
same_rows("json_contains", "Club", lambda o: o.filter(address__contains={"a": 1}), ["ljubljana-rowing"])
same_rows(
    "json_contains_nested",
    "Club",
    lambda o: o.filter(address__contains={"owner": {"name": "Bojan"}}),
    ["maribor-climbing"],
)
same_rows(
    "json_contains_empty_object",
    "Club",
    lambda o: o.filter(address__contains={}),
    ["koper-sailing", "ljubljana-rowing", "maribor-climbing"],
)
same_rows("json_contains_of_a_key", "Club", lambda o: o.filter(address__owner__contains={"name": "Ana"}))
same_rows("json_exclude_contains", "Club", lambda o: o.exclude(address__contains={"a": 1}))
same_rows(
    "json_contained_by",
    "Club",
    lambda o: o.filter(
        address__contained_by={
            "street": "Stenska ulica 4",
            "owner": {"name": "Bojan"},
            "items": ["rope"],
            "a": 2,
            "note": None,
            "extra": 0,
        }
    ),
    ["koper-sailing", "maribor-climbing"],
)
same_rows("json_list_contained_by", "Member", lambda o: o.filter(tags__contained_by=["junior", "coach"]))
same_rows("json_list_contains", "Member", lambda o: o.filter(tags__contains=["coach", "board"]), ["Eva"])
same_rows(
    "json_lookups_combined", "Club", lambda o, x: o.filter(x.Q(address__has_key="rating") | x.Q(address__a__gte=2))
)


# Terminal methods with transforms


def _django_get_count_exists():
    members = dj_clubs.Member.objects
    return (
        members.get(birth_date__year=1982).first_name,
        members.filter(joined__hour=7).count(),
        members.filter(birth_date__year=1500).exists(),
        dj_clubs.Club.objects.filter(address__has_key="owner").count(),
    )


@scenario(_django_get_count_exists, ("Eva", 11, False, 2))
async def get_count_and_exists():
    return (
        (await Member.objects.get(birth_date__year=1982)).first_name,
        await Member.objects.filter(joined__hour=7).count(),
        await Member.objects.filter(birth_date__year=1500).exists(),
        await Club.objects.filter(address__has_key="owner").count(),
    )


def _django_update_by_a_transform():
    from django.db import transaction

    with transaction.atomic():
        updated = dj_clubs.Member.objects.filter(birth_date__year__lt=1975).update(level="intermediate")
        result = (
            updated,
            sorted(dj_clubs.Member.objects.filter(level="intermediate").values_list("first_name", flat=True)),
        )
        transaction.set_rollback(True)
    return result


@scenario(_django_update_by_a_transform, (2, ["Ana", "Bojan", "Cvetka", "Filip", "Hana", "Klemen"]))
async def update_by_a_transform():
    async with adorm.atomic() as tx:
        updated = await Member.objects.filter(birth_date__year__lt=1975).update(level="intermediate")
        names = sorted(
            [name async for name in Member.objects.filter(level="intermediate").values_list("first_name", flat=True)]
        )
        await tx.rollback()
    return updated, names


def _django_aggregate_over_a_json_key():
    return dj_clubs.Club.objects.filter(address__items__0__isnull=False).aggregate(n=DjangoCount("pk"))


@scenario(_django_aggregate_over_a_json_key, {"n": 2})
async def aggregate_over_a_json_key():
    return await Club.objects.filter(address__items__0__isnull=False).aggregate(n=Count("pk"))


async def run_on(driver, read: Callable[[], Awaitable[Any]]) -> Any:
    """``read()`` on ``driver``; a driver outside `TRANSFORMING_DRIVERS` may refuse it, which skips the test."""
    try:
        return await read()
    except NotSupportedError as exc:
        if driver.name in TRANSFORMING_DRIVERS:
            raise
        pytest.skip(f"the {driver.name} driver refuses: {exc}")


async def django_result(read: Callable[[], Any]) -> Any:
    try:
        return await in_thread(read)
    except DjangoNotSupportedError as exc:
        pytest.skip(f"Django refuses the scenario on {connection.vendor}: {exc}")


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.id for s in SCENARIOS])
async def test_same_rows_as_django(driver, pool, scenario):
    expected = await django_result(scenario.django)
    assert await run_on(driver, scenario.run) == expected
    assert pool.in_use == 0


@pytest.mark.parametrize("scenario", [s for s in SCENARIOS if s.expected is not None], ids=lambda s: s.id)
@pytest.mark.usefixtures("club_data")
async def test_django_returns_the_stated_rows(scenario):
    """The stated results hold the scenarios to rows that tell the transforms apart, not to an empty or full table."""
    if connection.vendor != "postgresql":
        pytest.skip("the stated results are PostgreSQL's")
    assert await in_thread(scenario.django) == scenario.expected


def test_scenario_ids_are_unique():
    ids = [s.id for s in SCENARIOS]
    assert len(ids) == len(set(ids))


async def test_transforms_in_tasks_of_one_transaction(driver, pool):
    async def read():
        async with adorm.atomic():
            return await asyncio.gather(
                *(
                    Member.objects.filter(birth_date__year__gte=1994).count()
                    if n % 3 == 0
                    else Club.objects.filter(address__has_key="owner").count()
                    if n % 3 == 1
                    else Member.objects.filter(joined__hour=23).count()
                    for n in range(12)
                )
            )

    assert await run_on(driver, read) == [3, 2, 1] * 4
    assert pool.in_use == 0


async def test_datetime_transforms_are_computed_in_the_current_time_zone(driver, pool):
    from django.utils import timezone

    async def read():
        with timezone.override("Asia/Tokyo"):
            return [
                await Member.objects.filter(first_name="Ana", joined__hour=8).count(),
                await Member.objects.filter(first_name="Ana", joined__date=datetime.date(2026, 3, 3)).count(),
                await Member.objects.filter(first_name="Ana", joined__day=3).count(),
            ]

    assert await run_on(driver, read) == [1, 1, 1]
    assert pool.in_use == 0


# Unknown transforms and lookups

UNKNOWN = [
    ("joined__yeer", 2026, ["unsupported lookup 'yeer'", "clubs.Member.joined", "perhaps you meant 'year'"]),
    ("joined__yeer__gte", 2026, ["'yeer'", "perhaps you meant 'year'"]),
    ("joined__yeer__exact", 2026, ["unsupported transform 'yeer'", "perhaps you meant 'year'"]),
    ("joined__yeer__isnull", True, ["unsupported transform 'yeer'", "perhaps you meant 'year'"]),
    ("birth_date__year__gtee", 2000, ["'gtee'", "perhaps you meant 'gte'"]),
    ("joined__date__yeer", 2026, ["'yeer'"]),
    ("birth_date__hour", 7, ["'hour'", "clubs.Member.birth_date"]),
    ("first_name__year", 2026, ["'year'", "clubs.Member.first_name"]),
    ("first_name__has_key", "x", ["'has_key'", "clubs.Member.first_name"]),
    ("joined__year__club", 1, ["'club'"]),
    ("address__has_kye", "x", None),
]
"""Keys that do not resolve, with a value and the facts their `FieldError` message states (``None``: a JSON key path,
which the message need not describe)."""


@pytest.mark.parametrize(("key", "value", "facts"), UNKNOWN, ids=[key for key, _, _ in UNKNOWN])
async def test_an_unknown_transform_or_lookup_raises_field_error(driver, pool, key, value, facts):
    model = Club if key.startswith("address") else Member
    if facts is None:
        # "has_kye" is read as a key inside the JSON field, as in Django: a filter that matches no club.
        assert await run_on(driver, lambda: model.objects.filter(**{key: value}).count()) == 0
        return
    with pytest.raises(FieldError) as raised:
        await model.objects.filter(**{key: value}).count()
    for fact in facts:
        assert fact in str(raised.value)
    assert pool.in_use == 0
    assert await Member.objects.filter(birth_date__isnull=True).count() == 1


@pytest.mark.parametrize(
    ("build", "facts"),
    [
        (lambda: Member.objects.annotate(y=F("joined__yeer")), ["'yeer'", "perhaps you meant 'year'"]),
        (lambda: Member.objects.filter(joined__year=F("birth_date__yeer")), ["'yeer'", "perhaps you meant 'year'"]),
        (lambda: Member.objects.order_by("joined__yeer"), ["'yeer'", "perhaps you meant 'year'"]),
        (lambda: Member.objects.order_by("-joined__yeer"), ["'yeer'", "perhaps you meant 'year'"]),
        (lambda: Member.objects.values("joined__yeer"), ["'yeer'", "perhaps you meant 'year'"]),
        (lambda: Member.objects.values_list("first_name__year", flat=True), ["'year'", "clubs.Member.first_name"]),
    ],
    ids=["annotate", "f_on_the_right", "order_by", "order_by_descending", "values", "values_list"],
)
async def test_an_unknown_transform_in_a_field_path_raises_field_error(driver, pool, build, facts):  # noqa: ARG001
    with pytest.raises(FieldError) as raised:
        [row async for row in build()]
    for fact in facts:
        assert fact in str(raised.value)
    assert pool.in_use == 0


async def test_an_unknown_transform_in_a_transaction_leaves_it_usable(driver, pool):
    async with adorm.atomic():
        with pytest.raises(FieldError):
            await Member.objects.filter(joined__yeer=2026).count()
        assert await Club.objects.filter(slug="koper-sailing").update(city="Piran") == 1
        if driver.name in TRANSFORMING_DRIVERS:
            assert await Member.objects.filter(birth_date__year=1982).count() == 1
    assert (await Club.objects.get(slug="koper-sailing")).city == "Piran"
    assert pool.in_use == 0
