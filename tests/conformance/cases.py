"""The conformance catalogue: operations over the club data set (``manage.py seed_clubs``) and their results.

Each `Case` names in ``uses`` every operation kind and construct name its operations report when they all run. A
driver that declares all of them must return ``expected``; a driver that lacks one must refuse with
`NotSupportedError` naming only names it lacks. ``refusals`` lists the drivers that may refuse the case at execution
instead, for a limit the operation's names cannot show (it depends on the schema or the rows), with a pattern of the
message (a query the database's backend refuses, such as a sliced query in a set operation on SQLite). ``on_vendor``
holds the result on a database where it differs, by Django's vendor name (SQLite sorts ``NULL`` first in ascending
order). ``django`` is a synchronous mirror of a write case in the Django ORM; it returns ``expected`` over the seeded
database, which holds the write cases to Django's results whatever the drivers support.
"""

from __future__ import annotations

import datetime
import uuid

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import adorm

from adorm import F, MultipleObjectsReturned, ObjectDoesNotExist, Prefetch, ProtectedError, Q, ValidationError, Value
from adorm.query import (
    Avg,
    Case as CaseWhen,
    Coalesce,
    Concat,
    Count,
    Exact,
    Exists,
    GreaterThan,
    Length,
    Lower,
    Max,
    Min,
    Now,
    OuterRef,
    resolve_lookup,
    Subquery,
    Sum,
    Trunc,
    TruncDate,
    TruncDay,
    TruncHour,
    TruncMinute,
    TruncMonth,
    TruncQuarter,
    TruncSecond,
    TruncTime,
    TruncWeek,
    TruncYear,
    Upper,
    When,
)
from tests.clubmodels.activities import Activity, Attendance, Competition, Tag
from tests.clubmodels.billing import Invoice, InvoiceLine, Payment
from tests.clubmodels.clubs import Club, Member, Membership

UTC = datetime.UTC
START = datetime.datetime(2026, 3, 2, 7, 0, tzinfo=UTC)
ADVANCED = ["Bojan", "Eva", "Igor", "Lara"]
SLUGS = ["koper-sailing", "ljubljana-rowing", "maribor-climbing"]


@dataclass(frozen=True)
class Case:
    id: str
    uses: frozenset[str]
    run: Callable[[], Awaitable[Any]]
    expected: Any
    refusals: Mapping[str, str] = field(default_factory=dict)
    django: Callable[[], Any] | None = None
    on_vendor: Mapping[str, Any] = field(default_factory=dict)

    def expected_on(self, vendor: str) -> Any:
        """The result on a database of Django's vendor ``vendor``."""
        return self.on_vendor.get(vendor, self.expected)


CASES: list[Case] = []

COMPOUND_SLICES = "LIMIT/OFFSET not allowed in subqueries of compound statements"
"""SQLite's refusal of a sliced query in a set operation, which Django raises before the query runs."""


def case(
    *uses: str,
    expected: Any,
    refusals: Mapping[str, str] | None = None,
    django: Callable[[], Any] | None = None,
    on_vendor: Mapping[str, Any] | None = None,
):
    def register(run: Callable[[], Awaitable[Any]]) -> Callable[[], Awaitable[Any]]:
        CASES.append(
            Case(run.__name__, frozenset(uses), run, expected, dict(refusals or {}), django, dict(on_vendor or {}))
        )
        return run

    return register


async def first_names(queryset) -> list[str]:
    return sorted([member.first_name async for member in queryset])


async def names(queryset) -> list[str]:
    return sorted([row.name async for row in queryset])


async def ordered(queryset, attribute: str = "first_name") -> list[Any]:
    return [getattr(row, attribute) async for row in queryset]


async def rows(queryset) -> list[Any]:
    return [row async for row in queryset]


# Reads: operations and row types


@case("select", expected=SLUGS)
async def select_every_row():
    return sorted([club.slug async for club in Club.objects.all()])


@case("select", "lookup.exact", expected=("Member", "Novak", "ana.0@example.com", datetime.date(1970, 1, 10)))
async def rows_are_model_instances():
    (ana,) = await rows(Member.objects.filter(first_name="Ana"))
    return type(ana).__name__, ana.last_name, ana.email, ana.birth_date


@case("select", "lookup.exact", expected=("beginner", START, ["junior"], set()))
async def datetime_json_and_choice_fields():
    (ana,) = await rows(Member.objects.filter(first_name="Ana"))
    return ana.level, ana.joined, ana.tags, ana.get_deferred_fields()


@case("get", "lookup.exact", expected=({"street": "Veslaška pot 1"}, datetime.date(1921, 5, 3)))
async def json_object_and_date_fields():
    club = await Club.objects.get(slug="ljubljana-rowing")
    return club.address, club.founded


@case(
    "get",
    "lookup.exact",
    expected=(datetime.timedelta(hours=8), Decimal("15.00"), 15, START + datetime.timedelta(days=6), "social"),
)
async def duration_decimal_and_integer_fields():
    crag = await Activity.objects.get(name="Crag day")
    return crag.duration, crag.price, crag.capacity, crag.starts_at, crag.kind


@case("first", expected=("SI00 0001", True, Decimal("120.00")))
async def uuid_primary_key():
    payment = await Payment.objects.order_by("reference").first()
    return payment.reference, isinstance(payment.code, uuid.UUID) and payment.pk == payment.code, payment.amount


@case("get", "lookup.exact", expected="Zupančič")
async def get_one_row():
    return (await Member.objects.get(email="eva.4@example.com")).last_name


@case("get", "lookup.exact", expected=(True, True))
async def get_without_a_row_raises_does_not_exist():
    try:
        await Member.objects.get(first_name="Nobody")
    except Member.DoesNotExist as exc:
        return isinstance(exc, ObjectDoesNotExist), "does not exist" in str(exc)
    return None


@case("get", "lookup.exact", expected=(True, True))
async def get_with_several_rows_raises_multiple_objects_returned():
    try:
        await Member.objects.get(level="advanced")
    except Member.MultipleObjectsReturned as exc:
        return isinstance(exc, MultipleObjectsReturned), "it returned 4" in str(exc)
    return None


@case("first", "last", expected=("Ana", "Lara"))
async def first_and_last_follow_the_ordering():
    ordered_members = Member.objects.order_by("birth_date")
    return (await ordered_members.first()).first_name, (await ordered_members.last()).first_name


@case("first", "last", expected=("Ana", "Lara"))
async def first_and_last_without_an_ordering_order_by_the_primary_key():
    return (await Member.objects.first()).first_name, (await Member.objects.last()).first_name


@case("first", "lookup.exact", expected=None)
async def first_without_a_row_is_none():
    return await Member.objects.filter(first_name="Nobody").first()


@case("count", "lookup.exact", expected=(13, 1, 0))
async def count():
    return (
        await Membership.objects.count(),
        await Membership.objects.filter(active=False).count(),
        await Membership.objects.filter(role="nobody").count(),
    )


@case("exists", "lookup.exact", expected=(True, False))
async def exists():
    return await Club.objects.filter(city="Koper").exists(), await Club.objects.filter(city="Rome").exists()


@case("count", "clause.manager", expected=3)
async def a_query_through_the_orm_manager():
    from adorm.operations import Count as CountOperation
    from adorm.query import Query

    return await adorm.create_session().execute(CountOperation(Query("clubs.Club", manager="objects")))


@case(expected=([], 0, False, None))
async def none_runs_no_operation():
    query = Member.objects.none()
    return await rows(query), await query.count(), await query.exists(), await query.first()


# Lookups


@case("select", "lookup.exact", expected=["Ana"])
async def lookup_exact():
    return await first_names(Member.objects.filter(first_name="Ana"))


@case("select", "lookup.iexact", expected=["Ana"])
async def lookup_iexact():
    return await first_names(Member.objects.filter(last_name__iexact="NOVAK"))


@case("select", "lookup.gt", expected=["Regional bouldering cup", "Spring regatta"])
async def lookup_gt():
    return await names(Activity.objects.filter(capacity__gt=15))


@case("select", "lookup.gte", expected=["Crag day", "Regional bouldering cup", "Spring regatta"])
async def lookup_gte():
    return await names(Activity.objects.filter(capacity__gte=15))


@case("select", "lookup.lt", expected=["Dinghy handling"])
async def lookup_lt():
    return await names(Activity.objects.filter(capacity__lt=8))


@case("select", "lookup.lte", expected=["Dinghy handling", "Morning sculling"])
async def lookup_lte():
    return await names(Activity.objects.filter(capacity__lte=8))


@case("select", "lookup.in", expected=(ADVANCED, []))
async def lookup_in():
    return (
        await first_names(Member.objects.filter(level__in=["advanced", "expert"])),
        await first_names(Member.objects.filter(level__in=[])),
    )


@case("select", "lookup.range", expected=["Eva", "Filip", "Gaja"])
async def lookup_range():
    return await first_names(
        Member.objects.filter(birth_date__range=(datetime.date(1980, 1, 1), datetime.date(1990, 12, 31)))
    )


@case("select", "lookup.isnull", expected=(["Spring regatta"], 6))
async def lookup_isnull():
    with_coach = await names(Activity.objects.filter(coach_id__isnull=False))
    return await names(Activity.objects.filter(coach_id__isnull=True)), len(with_coach)


@case("select", "lookup.contains", expected=(["Bojan", "Hana", "Jana"], ["Bojan", "Eva"], ["Eva", "Lara"]))
async def lookup_contains_on_text_and_json():
    return (
        await first_names(Member.objects.filter(first_name__contains="an")),
        await first_names(Member.objects.filter(tags__contains=["coach"])),
        await first_names(Member.objects.filter(tags__contains="board")),
    )


@case("select", "lookup.icontains", expected=["Ana"])
async def lookup_icontains():
    return await first_names(Member.objects.filter(last_name__icontains="NOV"))


@case("select", "lookup.startswith", expected=["Eva"])
async def lookup_startswith():
    return await first_names(Member.objects.filter(email__startswith="eva."))


@case("select", "lookup.istartswith", expected=["Klemen"])
async def lookup_istartswith():
    return await first_names(Member.objects.filter(first_name__istartswith="k"))


@case("select", "lookup.endswith", expected=["Eva", "Lara"])
async def lookup_endswith():
    return await first_names(Member.objects.filter(last_name__endswith="ič"))


@case("select", "lookup.iendswith", expected=["Filip"])
async def lookup_iendswith():
    return await first_names(Member.objects.filter(last_name__iendswith="NIK"))


@case("select", "lookup.regex", expected=["Ana", "Bojan", "Cvetka"])
async def lookup_regex():
    return await first_names(Member.objects.filter(first_name__regex="^[A-C]"))


@case("select", "lookup.iregex", expected=["Ana", "Bojan", "Cvetka"])
async def lookup_iregex():
    return await first_names(Member.objects.filter(first_name__iregex="^[a-c]"))


async def club_slugs(queryset) -> list[str]:
    return sorted([club.slug async for club in queryset])


@case(
    "select",
    "lookup.overlap",
    expected=(
        SLUGS,
        ["koper-sailing", "maribor-climbing"],
        ["ljubljana-rowing"],
        [],
        [],
        ["koper-sailing"],
        ["Dinghy", "Keelboat"],
    ),
)
async def lookup_overlap_on_an_array_field():
    (koper,) = await rows(Club.objects.filter(disciplines__overlap=("Keelboat", "Rowing", None)))
    return (
        await club_slugs(Club.objects.filter(disciplines__overlap=["Fitness", "Dinghy"])),
        await club_slugs(Club.objects.filter(disciplines__overlap=["Lead", "Keelboat"])),
        await club_slugs(Club.objects.filter(disciplines__overlap={"Coxed eights", "Sculling"})),
        await club_slugs(Club.objects.filter(disciplines__overlap=[])),
        await club_slugs(Club.objects.filter(disciplines__overlap=["fitness"])),
        await club_slugs(Club.objects.exclude(disciplines__overlap=["Fitness"])),
        koper.disciplines,
    )


@case(
    "select",
    "lookup.overlap",
    expected=(SLUGS, ["koper-sailing", "maribor-climbing"], ["koper-sailing"], []),
)
async def lookup_overlap_with_expressions():
    return (
        await club_slugs(Club.objects.filter(disciplines__overlap=F("disciplines"))),
        await club_slugs(Club.objects.filter(disciplines__overlap=Value(["Lead", "Dinghy"]))),
        await club_slugs(Club.objects.filter(disciplines__overlap=[F("city"), "Dinghy"])),
        await club_slugs(Club.objects.filter(disciplines__overlap=["x", F("city")])),
    )


@case(
    "select",
    "lookup.overlap",
    "lookup.exact",
    "expression.Exists",
    "expression.OuterRef",
    expected=["ljubljana-rowing", "maribor-climbing"],
)
async def lookup_overlap_with_the_outer_row():
    others = Club.objects.exclude(pk=OuterRef("pk")).filter(disciplines__overlap=OuterRef("disciplines"))
    return await club_slugs(Club.objects.filter(Exists(others)))


@case("select", "lookup.overlap", "expression.Subquery", expected=["ljubljana-rowing", "maribor-climbing"])
async def lookup_overlap_with_a_query():
    return await club_slugs(Club.objects.filter(disciplines__overlap=Competition.objects.values("discipline")))


# Conditions


@case("select", "lookup.exact", expected=["Ana", *ADVANCED])
async def q_or():
    return await first_names(Member.objects.filter(Q(level="advanced") | Q(first_name="Ana")))


@case("select", "lookup.exact", "lookup.contains", expected=["Filip"])
async def q_negated_and_combined():
    return await first_names(Member.objects.filter(~Q(level="beginner"), tags__contains=["junior"]))


@case("select", "lookup.exact", expected=["Cvetka", "Filip", "Hana", "Klemen"])
async def exclude_calls_chain():
    return await first_names(Member.objects.exclude(level="beginner").exclude(level="advanced"))


@case("select", "lookup.lt", expected=["Regional bouldering cup"])
async def exclude_keeps_rows_whose_column_is_null():
    return await names(Competition.objects.exclude(entry_deadline__lt=datetime.date(2026, 4, 1)))


@case("count", "lookup.gte", expected=(8, 5))
async def negated_condition_keeps_rows_whose_column_is_null():
    return (
        await Attendance.objects.filter(~Q(checked_in_at__gte=START)).count(),
        await Attendance.objects.filter(~~Q(checked_in_at__gte=START)).count(),
    )


@case("select", "lookup.lt", expected=["Dinghy handling"])
async def lookup_against_a_field():
    return await names(Activity.objects.filter(capacity__lt=F("price")))


@case(
    "select",
    "lookup.gt",
    "expression.CombinedExpression",
    expected=["Ergometer session", "Morning sculling", "Regional bouldering cup"],
)
async def lookup_against_arithmetic():
    return await names(Activity.objects.filter(capacity__gt=F("price") * 2))


@case("get", "count", "lookup.exact", "lookup.in", expected=(3, 3, 3))
async def a_model_instance_stands_for_its_primary_key():
    koper = await Club.objects.get(slug="koper-sailing")
    return (
        await Membership.objects.filter(club=koper).count(),
        await Membership.objects.filter(club_id=koper.pk).count(),
        await Membership.objects.filter(club__in=[koper]).count(),
    )


# Lookup keys built at run time

MEMBER_FILTERS = [
    ("level", "in", ["advanced", "intermediate"]),
    ("first_name", "istartswith", "e"),
    ("birth_date", "gte", datetime.date(1980, 1, 1)),
]
"""Declared filters as a dynamic filter form or a viewset produces them: field, lookup name, value."""


@case(
    "select",
    "lookup.in",
    "lookup.istartswith",
    "lookup.gte",
    expected=(["Eva"], ["Bojan", "Cvetka", "Eva", "Filip", "Gaja", "Hana", "Igor", "Jana", "Klemen", "Lara"]),
)
async def lookup_keys_built_at_run_time():
    conditions = {f"{field}__{op}": value for field, op, value in MEMBER_FILTERS}
    return (
        await first_names(Member.objects.filter(**conditions)),
        await first_names(Member.objects.filter(Q(*conditions.items(), _connector=Q.OR))),
    )


@case("select", "lookup.exact", "lookup.icontains", "path.nested", expected=["Igor"])
async def lookup_keys_built_at_run_time_cross_relations():
    path = "__".join(["memberships", "club", "city"])
    return await first_names(Member.objects.filter(**{f"{path}__icontains": "kop", "memberships__role": "coach"}))


@case("get", "count", "lookup.exact", "lookup.in", "lookup.lt", "lookup.isnull", expected=[3, 10, 0, 13])
async def lookups_a_relation_takes_built_at_run_time():
    koper = await Club.objects.get(slug="koper-sailing")
    filters = [("club", "in", [koper]), ("club", "lt", koper.pk), ("club", "isnull", True), ("club", "isnull", False)]
    counts = []
    for name, op, value in filters:
        key = f"{name}__{op}"
        resolve_lookup(Membership, key)
        counts.append(await Membership.objects.filter(**{key: value}).count())
    return counts


# Ordering, slicing, distinct


@case(
    "select",
    expected=[
        "Lara",
        "Igor",
        "Eva",
        "Bojan",
        "Jana",
        "Gaja",
        "David",
        "Ana",
        "Klemen",
        "Hana",
        "Filip",
        "Cvetka",
    ],  # fmt: skip
)
async def order_by_several_fields():
    return await ordered(Member.objects.order_by("level", "-first_name"))


ACTIVITIES_BY_COACH = [
    "Ergometer session",
    "Morning sculling",
    "Bouldering basics",
    "Crag day",
    "Regional bouldering cup",
    "Dinghy handling",
]

SQLITE_NULL_ORDER = (
    ["Spring regatta", *ACTIVITIES_BY_COACH],
    ["Spring regatta", *ACTIVITIES_BY_COACH],
    ["Dinghy handling", "Bouldering basics", "Crag day", "Regional bouldering cup", "Ergometer session"],
)
"""`order_by_nulls_first_and_last` on SQLite, which sorts ``NULL`` first in ascending and last in descending order."""


@case(
    "select",
    expected=(
        ["Spring regatta", *ACTIVITIES_BY_COACH],
        [*ACTIVITIES_BY_COACH, "Spring regatta"],
        ["Spring regatta", "Dinghy handling", "Bouldering basics", "Crag day", "Regional bouldering cup"],
    ),
    on_vendor={"sqlite": SQLITE_NULL_ORDER},
)
async def order_by_nulls_first_and_last():
    return (
        await ordered(Activity.objects.order_by(F("coach_id").asc(nulls_first=True), "name"), "name"),
        await ordered(Activity.objects.order_by("coach_id", "name"), "name"),
        (await ordered(Activity.objects.order_by("-coach_id", "name"), "name"))[:5],
    )


@case("select", "count", expected=(["Cvetka", "David", "Eva"], ["Klemen", "Lara"], 5, 2))
async def slicing():
    by_name = Member.objects.order_by("first_name")
    return (
        await ordered(by_name[2:5]),
        await ordered(by_name[10:]),
        await by_name[:5].count(),
        await by_name[10:].count(),
    )


@case("select", "count", "clause.distinct", expected=(["board", "coach", "member"], 3))
async def distinct():
    roles = [role async for role in Membership.objects.values_list("role", flat=True).distinct().order_by("role")]
    return roles, await Membership.objects.values("role").distinct().count()


@case("select", "clause.distinct_on", expected=[Decimal("120.00"), Decimal("95.50"), Decimal("150.00")])
async def distinct_on():
    query = Membership.objects.order_by("club_id", "-fee").distinct("club_id").values_list("fee", flat=True)
    return [fee async for fee in query]


# Set operations


def _django_union_of_values_lists():
    from tests.djangoproject.clubs.models import Member as DjangoMember

    advanced = DjangoMember.objects.filter(level="advanced").values_list("level", flat=True)
    beginners = DjangoMember.objects.filter(level="beginner").values_list("level", flat=True)
    return list(advanced.union(beginners).order_by("level")), advanced.union(beginners, all=True).count()


@case(
    "select",
    "count",
    "lookup.exact",
    "clause.union",
    expected=(["advanced", "beginner"], 8),
    django=_django_union_of_values_lists,
)
async def union_of_values_lists():
    advanced = Member.objects.filter(level="advanced").values_list("level", flat=True)
    beginners = Member.objects.filter(level="beginner").values_list("level", flat=True)
    return await rows(advanced.union(beginners).order_by("level")), await advanced.union(beginners, all=True).count()


def _django_union_of_model_rows():
    from tests.djangoproject.clubs.models import Member as DjangoMember

    juniors = DjangoMember.objects.filter(tags__contains=["junior"])
    query = DjangoMember.objects.filter(level="advanced").union(juniors).order_by("-first_name")[:3]
    return [member.first_name for member in query]


@case(
    "select",
    "lookup.exact",
    "lookup.contains",
    "clause.union",
    expected=["Lara", "Jana", "Igor"],
    django=_django_union_of_model_rows,
)
async def union_of_model_rows():
    juniors = Member.objects.filter(tags__contains=["junior"])
    return await ordered(Member.objects.filter(level="advanced").union(juniors).order_by("-first_name")[:3])


def _django_intersection():
    from tests.djangoproject.clubs.models import Member as DjangoMember

    query = DjangoMember.objects.filter(level="advanced").intersection(
        DjangoMember.objects.filter(tags__contains=["coach"])
    )
    return sorted(member.first_name for member in query), query.exists()


@case(
    "select",
    "exists",
    "lookup.exact",
    "lookup.contains",
    "clause.intersection",
    expected=(["Bojan", "Eva"], True),
    django=_django_intersection,
)
async def intersection():
    query = Member.objects.filter(level="advanced").intersection(Member.objects.filter(tags__contains=["coach"]))
    return await first_names(query), await query.exists()


def _django_difference_then_values_list():
    from tests.djangoproject.clubs.models import Member as DjangoMember

    board = DjangoMember.objects.filter(tags__contains=["board"])
    query = DjangoMember.objects.filter(level="advanced").difference(board)
    return list(query.values_list("first_name", flat=True).order_by("first_name"))


@case(
    "select",
    "lookup.exact",
    "lookup.contains",
    "clause.difference",
    expected=["Bojan", "Igor"],
    django=_django_difference_then_values_list,
)
async def difference_then_values_list():
    query = Member.objects.filter(level="advanced").difference(Member.objects.filter(tags__contains=["board"]))
    return await rows(query.values_list("first_name", flat=True).order_by("first_name"))


def _django_union_reduced_to_one_query():
    from tests.djangoproject.clubs.models import Member as DjangoMember

    levels = DjangoMember.objects.order_by("level").values_list("level", flat=True)
    union = levels[:6].union(DjangoMember.objects.none()).order_by("-level")
    union_all = levels[:6].union(DjangoMember.objects.none(), all=True).order_by("-level")
    # Django 5.2 keeps the ordering of the single unsliced query, which PostgreSQL refuses; adorm drops it.
    unsliced = levels.order_by().union(DjangoMember.objects.none()).order_by("-level")
    return list(union[:5]), list(union_all[:5]), list(unsliced)


@case(
    "select",
    "clause.union",
    expected=(
        ["beginner", "advanced"],
        ["beginner", "beginner", "advanced", "advanced", "advanced"],
        ["intermediate"] * 4 + ["beginner"] * 4 + ["advanced"] * 4,
    ),
    django=_django_union_reduced_to_one_query,
    refusals={"django": COMPOUND_SLICES},
)
async def union_reduced_to_one_query():
    levels = Member.objects.order_by("level").values_list("level", flat=True)
    union = levels[:6].union(Member.objects.none()).order_by("-level")
    union_all = levels[:6].union(Member.objects.none(), all=True).order_by("-level")
    unsliced = levels.union(Member.objects.none()).order_by("-level")
    return await rows(union[:5]), await rows(union_all[:5]), await rows(unsliced)


def _django_set_operations_of_one_query():
    from tests.djangoproject.clubs.models import Member as DjangoMember

    levels = DjangoMember.objects.values_list("level", flat=True)
    return (
        sorted(levels.difference(DjangoMember.objects.none())),
        sorted(levels.intersection()),
        sorted(levels.union(levels[2:2])[:20]),
    )


@case(
    "select",
    "clause.intersection",
    "clause.difference",
    "clause.union",
    expected=(
        ["advanced"] * 4 + ["beginner"] * 4 + ["intermediate"] * 4,
        ["advanced"] * 4 + ["beginner"] * 4 + ["intermediate"] * 4,
        ["advanced", "beginner", "intermediate"],
    ),
    django=_django_set_operations_of_one_query,
    refusals={"django": COMPOUND_SLICES},
)
async def set_operations_of_one_query():
    levels = Member.objects.values_list("level", flat=True)
    return (
        sorted(await rows(levels.difference(Member.objects.none()))),
        sorted(await rows(levels.intersection())),
        sorted(await rows(levels.union(levels[2:2])[:20])),
    )


# Row formats and loaded fields


@case(
    "select",
    "lookup.exact",
    expected=(
        [{"name": "Koper Sailing Club", "city": "Koper"}],
        (["id", "name"], ["endurance", "indoor", "outdoor", "technique", "youth"]),
    ),
)
async def values():
    tags = await rows(Tag.objects.order_by("name").values())
    return (
        await rows(Club.objects.filter(slug="koper-sailing").values("name", "city")),
        (sorted(tags[0]), [tag["name"] for tag in tags]),
    )


@case(
    "select",
    "lookup.contains",
    "lookup.exact",
    expected=(
        [("Bojan", "advanced"), ("Eva", "advanced")],
        ["Bojan", "Eva"],
        ("Bojan", "advanced"),
        [("member", False)],
    ),
)
async def values_list():
    coaches = Member.objects.filter(tags__contains=["coach"]).order_by("first_name")
    named = (await rows(coaches.values_list("first_name", "level", named=True)))[0]
    return (
        await rows(coaches.values_list("first_name", "level")),
        await rows(coaches.values_list("first_name", flat=True)),
        (named.first_name, named.level),
        await rows(Membership.objects.filter(active=False).values_list("role", "active")),
    )


@case(
    "get",
    "lookup.exact",
    "clause.only",
    expected=("Ana", ["birth_date", "email", "joined", "last_name", "level", "tags"]),
)
async def only():
    ana = await Member.objects.only("first_name").get(first_name="Ana")
    return ana.first_name, sorted(ana.get_deferred_fields())


@case("get", "lookup.exact", "clause.defer", expected=("Novak", ["joined", "tags"], True))
async def defer():
    ana = await Member.objects.defer("tags", "joined").get(first_name="Ana")
    try:
        ana.tags  # noqa: B018 - the access raises
    except AttributeError:
        deferred_access_raises = True
    else:
        deferred_access_raises = False
    return ana.last_name, sorted(ana.get_deferred_fields()), deferred_access_raises


# Relations


@case("get", "lookup.exact", expected=("Ljubljana Rowing Club", "Lara"))
async def foreign_keys_load_on_await():
    board = await Membership.objects.get(role="board")
    return (await board.club).name, (await board.member).first_name


@case("get", "lookup.exact", "clause.select_related", expected="Ljubljana Rowing Club")
async def select_related_loads_the_relation_with_the_row():
    board = await Membership.objects.select_related("club").get(role="board")
    return (await board.club).name


@case("get", "count", "lookup.exact", expected=(3, 2))
async def reverse_foreign_keys_are_queries():
    koper = await Club.objects.get(slug="koper-sailing")
    return await koper.memberships.count(), await koper.memberships.filter(role="member").count()


@case(
    "get",
    "select",
    "lookup.exact",
    "lookup.in",
    "expression.Subquery",
    expected=["Ljubljana Rowing Club", "Maribor Climbing Club"],
)
async def many_to_many_relations_are_queries():
    cvetka = await Member.objects.get(first_name="Cvetka")
    return await names(cvetka.clubs)


@case("select", "lookup.exact", "path.nested", expected=["Igor", "Jana", "Klemen"])
async def lookups_across_relations():
    return await first_names(Member.objects.filter(memberships__club__slug="koper-sailing"))


@case(
    "get",
    "select",
    "lookup.exact",
    expected=(["Igor", "Jana", "Klemen"], ["Ljubljana Rowing Club", "Maribor Climbing Club"]),
    refusals={"memory": "does not follow relations"},
)
async def conditions_on_a_reverse_or_many_to_many_relation_name():
    koper = await Club.objects.get(slug="koper-sailing")
    cvetka = await Member.objects.get(first_name="Cvetka")
    return await first_names(Member.objects.filter(clubs=koper)), await names(Club.objects.filter(members=cvetka))


@case("select", "path.nested", expected=["Ana", "Bojan", "Cvetka", "Cvetka", "David", "Eva", "Filip"])
async def order_by_a_related_field():
    query = Membership.objects.order_by("member__first_name", "club__slug")
    return (await rows(query.values_list("member__first_name", flat=True)))[:7]


@case("select", "clause.distinct", "path.nested", expected=["Koper", "Ljubljana", "Maribor"])
async def values_of_a_related_field():
    query = Activity.objects.values_list("club__city", flat=True).distinct().order_by("club__city")
    return await rows(query)


@case("select", "lookup.exact", "path.nested", expected={"street": "Veslaška pot 1"})
async def lookups_into_json_keys():
    (club,) = await rows(Club.objects.filter(address__street="Veslaška pot 1"))
    return club.address


@case(
    "select",
    "count",
    "lookup.exact",
    expected=(
        [
            ("Regional bouldering cup", "Bouldering", None),
            ("Spring regatta", "Coxed eights", datetime.date(2026, 3, 12)),
        ],
        2,
    ),
)
async def multi_table_inheritance_children_read_their_parents_fields():
    competitions = await rows(Competition.objects.order_by("name"))
    return (
        [(c.name, c.discipline, c.entry_deadline) for c in competitions],
        await Activity.objects.filter(kind="competition").count(),
    )


@case("get", "lookup.exact", expected="Coxed eights")
async def reverse_one_to_one_loads_on_await():
    regatta = await Activity.objects.get(name="Spring regatta")
    return (await regatta.competition).discipline


@case("count", "lookup.exact", "path.nested", expected=(10, 9))
async def generic_relation_content_types():
    lines = InvoiceLine.objects
    return (
        await lines.filter(content_type__model="membership").count(),
        await lines.filter(content_type__model="activity").count(),
    )


# Subqueries


@case("select", "lookup.in", "lookup.exact", "expression.Subquery", expected=["Ana", "Cvetka", "Filip", "Gaja", "Jana"])
async def a_query_as_an_in_subquery():
    attended = Attendance.objects.filter(status="attended").values("member_id")
    return await first_names(Member.objects.filter(pk__in=attended))


def _django_a_correlated_subquery():
    from django.db.models import OuterRef as DjangoOuterRef, Subquery as DjangoSubquery

    from tests.djangoproject.clubs.models import Member as DjangoMember, Membership as DjangoMembership

    coach = DjangoMembership.objects.filter(member_id=DjangoOuterRef("pk"), role="coach").values("member_id")
    return sorted(DjangoMember.objects.filter(pk__in=DjangoSubquery(coach)).values_list("first_name", flat=True))


@case(
    "select",
    "lookup.in",
    "lookup.exact",
    "expression.Subquery",
    "expression.OuterRef",
    expected=["Bojan", "Eva", "Igor"],
    django=_django_a_correlated_subquery,
)
async def a_correlated_subquery():
    coach = Membership.objects.filter(member_id=OuterRef("pk"), role="coach").values("member_id")
    return await first_names(Member.objects.filter(pk__in=Subquery(coach)))


def _django_exists_subquery():
    from django.db.models import Exists as DjangoExists, OuterRef as DjangoOuterRef

    from tests.djangoproject.clubs.models import Member as DjangoMember, Membership as DjangoMembership

    board = DjangoMembership.objects.filter(member_id=DjangoOuterRef("pk"), role="board")
    return sorted(DjangoMember.objects.filter(DjangoExists(board)).values_list("first_name", flat=True))


@case(
    "select",
    "lookup.exact",
    "expression.Exists",
    "expression.OuterRef",
    expected=["Lara"],
    django=_django_exists_subquery,
)
async def exists_subquery():
    board = Membership.objects.filter(member_id=OuterRef("pk"), role="board")
    return await first_names(Member.objects.filter(Exists(board)))


# Annotations and functions


@case("select", "lookup.exact", "clause.annotate", expected=[("Ana", "Ana", 1)])
async def annotations_of_fields_and_values():
    query = Member.objects.annotate(given=F("first_name"), one=Value(1)).filter(first_name="Ana")
    return [(m.first_name, m.given, m.one) for m in await rows(query)]


@case("select", "lookup.exact", "clause.annotate", "function.Upper", expected=[("Ana", "NOVAK")])
async def function_upper():
    query = Member.objects.annotate(upper=Upper("last_name")).filter(upper="NOVAK")
    return [(m.first_name, m.upper) for m in await rows(query)]


@case("select", "lookup.exact", "function.Lower", expected=["Ana"])
async def function_lower_in_a_condition():
    return await first_names(Member.objects.filter(Exact(Lower("last_name"), "novak")))


@case(
    "select",
    "function.Lower",
    expected=["Lara", "Igor", "Cvetka", "Gaja", "Bojan", "David", "Klemen", "Ana", "Filip", "Jana", "Hana", "Eva"],
)
async def order_by_a_function():
    return await ordered(Member.objects.order_by(Lower("last_name")))


def _django_function_coalesce():
    from django.db.models import Value as DjangoValue
    from django.db.models.functions import Coalesce as DjangoCoalesce
    from django.db.models.lookups import Exact as DjangoExact

    from tests.djangoproject.activities.models import Activity as DjangoActivity

    condition = DjangoExact(DjangoCoalesce("coach_id", DjangoValue(0)), 0)
    return sorted(DjangoActivity.objects.filter(condition).values_list("name", flat=True))


@case("select", "lookup.exact", "function.Coalesce", expected=["Spring regatta"], django=_django_function_coalesce)
async def function_coalesce():
    return await names(Activity.objects.filter(Exact(Coalesce("coach_id", Value(0)), 0)))


def _django_function_concat():
    from django.db.models import Value as DjangoValue
    from django.db.models.functions import Concat as DjangoConcat
    from django.db.models.lookups import Exact as DjangoExact

    from tests.djangoproject.clubs.models import Member as DjangoMember

    condition = DjangoExact(DjangoConcat("first_name", DjangoValue(" "), "last_name"), "Ana Novak")
    return sorted(DjangoMember.objects.filter(condition).values_list("first_name", flat=True))


@case("select", "lookup.exact", "function.Concat", expected=["Ana"], django=_django_function_concat)
async def function_concat():
    return await first_names(Member.objects.filter(Exact(Concat("first_name", Value(" "), "last_name"), "Ana Novak")))


def _django_function_length():
    from django.db.models.functions import Length as DjangoLength
    from django.db.models.lookups import Exact as DjangoExact

    from tests.djangoproject.clubs.models import Member as DjangoMember

    condition = DjangoExact(DjangoLength("first_name"), 3)
    return sorted(DjangoMember.objects.filter(condition).values_list("first_name", flat=True))


@case("select", "lookup.exact", "function.Length", expected=["Ana", "Eva"], django=_django_function_length)
async def function_length():
    return await first_names(Member.objects.filter(Exact(Length("first_name"), 3)))


def _django_function_now():
    from django.db.models import F as DjangoF
    from django.db.models.functions import Now as DjangoNow
    from django.db.models.lookups import GreaterThan as DjangoGreaterThan

    from tests.djangoproject.activities.models import Activity as DjangoActivity

    return DjangoActivity.objects.filter(DjangoGreaterThan(DjangoF("starts_at"), DjangoNow())).count()


@case("count", "lookup.gt", "function.Now", expected=0, django=_django_function_now)
async def function_now():
    return await Activity.objects.filter(GreaterThan(F("starts_at"), Now())).count()


def _truncated(function, value):
    async def run():
        return await Activity.objects.filter(Exact(function, value)).count()

    return run


def _django_truncated(name, kind, value):
    def mirror():
        from django.db.models import functions
        from django.db.models.lookups import Exact as DjangoExact

        from tests.djangoproject.activities.models import Activity as DjangoActivity

        function = getattr(functions, name)
        truncated = function("starts_at", kind) if kind else function("starts_at")
        return DjangoActivity.objects.filter(DjangoExact(truncated, value)).count()

    return mirror


TRUNCATIONS = [
    ("Trunc", "month", Trunc("starts_at", "month"), datetime.datetime(2026, 3, 1, tzinfo=UTC), 7),
    ("TruncYear", None, TruncYear("starts_at"), datetime.datetime(2026, 1, 1, tzinfo=UTC), 7),
    ("TruncQuarter", None, TruncQuarter("starts_at"), datetime.datetime(2026, 1, 1, tzinfo=UTC), 7),
    ("TruncMonth", None, TruncMonth("starts_at"), datetime.datetime(2026, 3, 1, tzinfo=UTC), 7),
    ("TruncWeek", None, TruncWeek("starts_at"), datetime.datetime(2026, 3, 2, tzinfo=UTC), 5),
    ("TruncDay", None, TruncDay("starts_at"), datetime.datetime(2026, 3, 2, tzinfo=UTC), 1),
    ("TruncHour", None, TruncHour("starts_at"), datetime.datetime(2026, 3, 2, 7, tzinfo=UTC), 1),
    ("TruncMinute", None, TruncMinute("starts_at"), datetime.datetime(2026, 3, 2, 7, tzinfo=UTC), 1),
    ("TruncSecond", None, TruncSecond("starts_at"), datetime.datetime(2026, 3, 2, 7, tzinfo=UTC), 1),
    ("TruncDate", None, TruncDate("starts_at"), datetime.date(2026, 3, 2), 1),
    ("TruncTime", None, TruncTime("starts_at"), datetime.time(7, 0), 7),
]
for name, kind, function, value, matching in TRUNCATIONS:
    run = _truncated(function, value)
    run.__name__ = f"function_{name.lower()}"
    mirror = _django_truncated(name, kind, value)
    case("count", "lookup.exact", f"function.{name}", expected=matching, django=mirror)(run)


def _django_case_when():
    from django.db.models import Case as DjangoCase, Value as DjangoValue, When as DjangoWhen

    from tests.djangoproject.clubs.models import Member as DjangoMember

    senior = DjangoCase(
        DjangoWhen(birth_date__lt=datetime.date(1980, 1, 1), then=DjangoValue(True)), default=DjangoValue(False)
    )
    return sorted(DjangoMember.objects.annotate(senior=senior).filter(senior=True).values_list("first_name", flat=True))


@case(
    "select",
    "lookup.exact",
    "lookup.lt",
    "clause.annotate",
    "expression.Case",
    expected=["Ana", "Bojan", "Cvetka", "David"],
    django=_django_case_when,
)
async def case_when():
    senior = CaseWhen(When(birth_date__lt=datetime.date(1980, 1, 1), then=Value(True)), default=Value(False))
    return await first_names(Member.objects.annotate(senior=senior).filter(senior=True))


# Aggregates


def _django_by_slug(aggregate) -> list[tuple[str, Any]]:
    from tests.djangoproject.clubs.models import Club as DjangoClub

    return list(DjangoClub.objects.annotate(value=aggregate).order_by("slug").values_list("slug", "value"))


async def _by_slug(query) -> list[tuple[str, Any]]:
    return await rows(query.order_by("slug").values_list("slug", "value"))


def _django_aggregate_count():
    from django.db.models import Count as DjangoCount

    return _django_by_slug(DjangoCount("memberships"))


@case(
    "select",
    "clause.annotate",
    "aggregate.Count",
    expected=[("koper-sailing", 3), ("ljubljana-rowing", 5), ("maribor-climbing", 5)],
    django=_django_aggregate_count,
)
async def aggregate_count():
    return await _by_slug(Club.objects.annotate(value=Count("memberships")))


def _django_aggregate_sum():
    from django.db.models import Sum as DjangoSum

    return _django_by_slug(DjangoSum("memberships__fee"))


@case(
    "select",
    "clause.annotate",
    "path.nested",
    "aggregate.Sum",
    expected=[
        ("koper-sailing", Decimal("300.00")),
        ("ljubljana-rowing", Decimal("420.00")),
        ("maribor-climbing", Decimal("382.00")),
    ],
    django=_django_aggregate_sum,
)
async def aggregate_sum():
    return await _by_slug(Club.objects.annotate(value=Sum("memberships__fee")))


def _django_aggregate_avg():
    from django.db.models import Avg as DjangoAvg

    from tests.djangoproject.clubs.models import Club as DjangoClub

    query = DjangoClub.objects.annotate(value=DjangoAvg("memberships__fee")).filter(slug="koper-sailing")
    return [float(value) for value in query.values_list("value", flat=True)]


@case(
    "select",
    "lookup.exact",
    "clause.annotate",
    "path.nested",
    "aggregate.Avg",
    expected=[100.0],
    django=_django_aggregate_avg,
)
async def aggregate_avg():
    query = Club.objects.annotate(value=Avg("memberships__fee")).filter(slug="koper-sailing")
    return [float(value) async for value in query.values_list("value", flat=True)]


def _django_aggregate_min_and_max():
    from django.db.models import Max as DjangoMax, Min as DjangoMin

    return _django_by_slug(DjangoMin("activities__capacity")), _django_by_slug(DjangoMax("activities__capacity"))


@case(
    "select",
    "clause.annotate",
    "path.nested",
    "aggregate.Min",
    "aggregate.Max",
    expected=(
        [("koper-sailing", 6), ("ljubljana-rowing", 8), ("maribor-climbing", 10)],
        [("koper-sailing", 6), ("ljubljana-rowing", 30), ("maribor-climbing", 40)],
    ),
    django=_django_aggregate_min_and_max,
)
async def aggregate_min_and_max():
    return (
        await _by_slug(Club.objects.annotate(value=Min("activities__capacity"))),
        await _by_slug(Club.objects.annotate(value=Max("activities__capacity"))),
    )


def _django_aggregate_distinct():
    from django.db.models import Count as DjangoCount

    return _django_by_slug(DjangoCount("memberships__role", distinct=True))


@case(
    "select",
    "clause.annotate",
    "path.nested",
    "aggregate.Count",
    "aggregate.distinct",
    expected=[("koper-sailing", 2), ("ljubljana-rowing", 3), ("maribor-climbing", 2)],
    django=_django_aggregate_distinct,
)
async def aggregate_distinct():
    return await _by_slug(Club.objects.annotate(value=Count("memberships__role", distinct=True)))


def _django_aggregate_filter():
    from django.db.models import Count as DjangoCount, Q as DjangoQ

    return _django_by_slug(DjangoCount("memberships", filter=DjangoQ(memberships__active=False)))


@case(
    "select",
    "lookup.exact",
    "clause.annotate",
    "path.nested",
    "aggregate.Count",
    "aggregate.filter",
    expected=[("koper-sailing", 0), ("ljubljana-rowing", 1), ("maribor-climbing", 0)],
    django=_django_aggregate_filter,
)
async def aggregate_filter():
    inactive = Count("memberships", filter=Q(memberships__active=False))
    return await _by_slug(Club.objects.annotate(value=inactive))


# Aggregation over the rows, grouping and aliases

FEES = Decimal("1102.00")


def _django_aggregate_operation():
    from django.db.models import Count as DjangoCount, Q as DjangoQ, Sum as DjangoSum

    from tests.djangoproject.clubs.models import Membership as DjangoMembership

    return DjangoMembership.objects.aggregate(
        DjangoSum("fee"),
        rows=DjangoCount("*"),
        coach_fees=DjangoSum("fee", filter=DjangoQ(role="coach")),
        paying=DjangoCount("pk", filter=DjangoQ(fee__gt=0)),
    )


@case(
    "aggregate",
    "lookup.exact",
    "lookup.gt",
    "aggregate.Sum",
    "aggregate.Count",
    "aggregate.filter",
    expected={"fee__sum": FEES, "rows": 13, "coach_fees": Decimal("0.00"), "paying": 10},
    django=_django_aggregate_operation,
)
async def aggregate_operation():
    return await Membership.objects.aggregate(
        Sum("fee"),
        rows=Count("*"),
        coach_fees=Sum("fee", filter=Q(role="coach")),
        paying=Count("pk", filter=Q(fee__gt=0)),
    )


def _django_aggregate_without_rows():
    from django.db.models import Count as DjangoCount, Max as DjangoMax, Sum as DjangoSum

    from tests.djangoproject.clubs.models import Membership as DjangoMembership

    return DjangoMembership.objects.filter(role="treasurer").aggregate(
        n=DjangoCount("*"), total=DjangoSum("fee"), zero=DjangoSum("fee", default=0), top=DjangoMax("fee")
    )


@case(
    "aggregate",
    "lookup.exact",
    "aggregate.Count",
    "aggregate.Sum",
    "aggregate.Max",
    expected={"n": 0, "total": None, "zero": 0, "top": None},
    django=_django_aggregate_without_rows,
)
async def aggregate_without_rows():
    return await Membership.objects.filter(role="treasurer").aggregate(
        n=Count("*"), total=Sum("fee"), zero=Sum("fee", default=0), top=Max("fee")
    )


def _django_aggregate_over_a_slice_and_annotations():
    from django.db.models import Avg as DjangoAvg, Count as DjangoCount, Max as DjangoMax

    from tests.djangoproject.clubs.models import Club as DjangoClub, Membership as DjangoMembership

    top = DjangoMembership.objects.order_by("-fee", "pk")[:3].aggregate(DjangoMax("fee"), n=DjangoCount("*"))
    clubs = DjangoClub.objects.annotate(headcount=DjangoCount("memberships")).aggregate(DjangoAvg("headcount"))
    return top, round(clubs["headcount__avg"], 6)


@case(
    "aggregate",
    "clause.annotate",
    "aggregate.Max",
    "aggregate.Count",
    "aggregate.Avg",
    expected=({"fee__max": Decimal("150.00"), "n": 3}, 4.333333),
    django=_django_aggregate_over_a_slice_and_annotations,
)
async def aggregate_over_a_slice_and_annotations():
    top = await Membership.objects.order_by("-fee", "pk")[:3].aggregate(Max("fee"), n=Count("*"))
    clubs = await Club.objects.annotate(headcount=Count("memberships")).aggregate(Avg("headcount"))
    return top, round(clubs["headcount__avg"], 6)


def _django_values_annotate_groups_rows():
    from django.db.models import Count as DjangoCount, Sum as DjangoSum

    from tests.djangoproject.clubs.models import Membership as DjangoMembership

    groups = DjangoMembership.objects.values("role").annotate(n=DjangoCount("*"), fees=DjangoSum("fee"))
    return list(groups.order_by("role"))


@case(
    "select",
    "clause.annotate",
    "clause.group_by",
    "aggregate.Count",
    "aggregate.Sum",
    expected=[
        {"role": "board", "n": 1, "fees": Decimal("60.00")},
        {"role": "coach", "n": 3, "fees": Decimal("0.00")},
        {"role": "member", "n": 9, "fees": Decimal("1042.00")},
    ],
    django=_django_values_annotate_groups_rows,
)
async def values_annotate_groups_rows():
    groups = Membership.objects.values("role").annotate(n=Count("*"), fees=Sum("fee"))
    return await rows(groups.order_by("role"))


def _django_grouping_by_a_truncated_month():
    from django.db.models import Count as DjangoCount, Sum as DjangoSum
    from django.db.models.functions import TruncMonth as DjangoTruncMonth

    from tests.djangoproject.billing.models import Invoice as DjangoInvoice

    months = DjangoInvoice.objects.annotate(month=DjangoTruncMonth("issued")).values("month")
    return list(
        months.annotate(n=DjangoCount("pk"), total=DjangoSum("total"))
        .order_by("month")
        .values_list("month", "n", "total")
    )


@case(
    "select",
    "clause.annotate",
    "clause.group_by",
    "function.TruncMonth",
    "aggregate.Count",
    "aggregate.Sum",
    expected=[
        (datetime.date(2026, 1, 1), 10, Decimal("1102.00")),
        (datetime.date(2026, 3, 1), 9, Decimal("165.00")),
    ],
    django=_django_grouping_by_a_truncated_month,
)
async def grouping_by_a_truncated_month():
    months = Invoice.objects.annotate(month=TruncMonth("issued")).values("month")
    return await rows(
        months.annotate(n=Count("pk"), total=Sum("total")).order_by("month").values_list("month", "n", "total")
    )


def _django_aggregate_over_groups():
    from django.db.models import Count as DjangoCount, Max as DjangoMax

    from tests.djangoproject.clubs.models import Membership as DjangoMembership

    groups = DjangoMembership.objects.values("club_id").annotate(n=DjangoCount("*"))
    return groups.aggregate(largest=DjangoMax("n"), groups=DjangoCount("club_id"))


@case(
    "aggregate",
    "clause.annotate",
    "clause.group_by",
    "aggregate.Count",
    "aggregate.Max",
    expected={"largest": 5, "groups": 3},
    django=_django_aggregate_over_groups,
)
async def aggregate_over_groups():
    groups = Membership.objects.values("club_id").annotate(n=Count("*"))
    return await groups.aggregate(largest=Max("n"), groups=Count("club_id"))


def _django_alias_filters_without_selecting():
    from django.db.models import Count as DjangoCount

    from tests.djangoproject.clubs.models import Club as DjangoClub

    query = DjangoClub.objects.alias(headcount=DjangoCount("memberships")).filter(headcount__gt=3).order_by("slug")
    return [(club.slug, hasattr(club, "headcount")) for club in query]


@case(
    "select",
    "lookup.gt",
    "clause.alias",
    "aggregate.Count",
    expected=[("ljubljana-rowing", False), ("maribor-climbing", False)],
    django=_django_alias_filters_without_selecting,
)
async def alias_filters_without_selecting():
    query = Club.objects.alias(headcount=Count("memberships")).filter(headcount__gt=3).order_by("slug")
    return [(club.slug, hasattr(club, "headcount")) for club in await rows(query)]


def _django_annotations_of_functions_and_cases():
    from django.db.models import Case as DjangoCase, Value as DjangoValue, When as DjangoWhen
    from django.db.models.functions import Coalesce as DjangoCoalesce, Concat as DjangoConcat

    from tests.djangoproject.activities.models import Activity as DjangoActivity

    query = DjangoActivity.objects.annotate(
        label=DjangoConcat("name", DjangoValue(" ("), "kind", DjangoValue(")")),
        coach_name=DjangoCoalesce("coach__first_name", DjangoValue("nobody")),
        size=DjangoCase(
            DjangoWhen(capacity__gte=20, then=DjangoValue("large")),
            DjangoWhen(capacity__gte=10, then=DjangoValue("medium")),
            default=DjangoValue("small"),
        ),
    )
    return list(query.order_by("starts_at").values_list("label", "coach_name", "size")[:3])


@case(
    "select",
    "lookup.gte",
    "path.nested",
    "clause.annotate",
    "function.Concat",
    "function.Coalesce",
    "expression.Case",
    expected=[
        ("Morning sculling (training)", "Bojan", "small"),
        ("Bouldering basics (training)", "Eva", "medium"),
        ("Ergometer session (training)", "Bojan", "medium"),
    ],
    django=_django_annotations_of_functions_and_cases,
)
async def annotations_of_functions_and_cases():
    query = Activity.objects.annotate(
        label=Concat("name", Value(" ("), "kind", Value(")")),
        coach_name=Coalesce("coach__first_name", Value("nobody")),
        size=CaseWhen(
            When(capacity__gte=20, then=Value("large")),
            When(capacity__gte=10, then=Value("medium")),
            default=Value("small"),
        ),
    )
    return await rows(query.order_by("starts_at").values_list("label", "coach_name", "size")[:3])


def _django_a_subquery_of_grouped_sums():
    from django.db.models import OuterRef as DjangoOuterRef, Subquery as DjangoSubquery, Sum as DjangoSum

    from tests.djangoproject.billing.models import Invoice as DjangoInvoice
    from tests.djangoproject.clubs.models import Member as DjangoMember

    billed = (
        DjangoInvoice.objects.filter(member_id=DjangoOuterRef("pk"))
        .values("member_id")
        .annotate(total=DjangoSum("total"))
        .values("total")
    )
    query = DjangoMember.objects.annotate(billed=DjangoSubquery(billed)).filter(first_name__in=["Ana", "Bojan"])
    return list(query.order_by("first_name").values_list("first_name", "billed"))


@case(
    "select",
    "lookup.exact",
    "lookup.in",
    "clause.annotate",
    "clause.group_by",
    "aggregate.Sum",
    "expression.Subquery",
    "expression.OuterRef",
    expected=[("Ana", Decimal("145.00")), ("Bojan", None)],
    django=_django_a_subquery_of_grouped_sums,
)
async def a_subquery_of_grouped_sums():
    billed = (
        Invoice.objects.filter(member_id=OuterRef("pk"))
        .values("member_id")
        .annotate(total=Sum("total"))
        .values("total")
    )
    query = Member.objects.annotate(billed=Subquery(billed)).filter(first_name__in=["Ana", "Bojan"])
    return await rows(query.order_by("first_name").values_list("first_name", "billed"))


def _django_filter_then_annotate_counts_the_matched_rows():
    from django.db.models import Count as DjangoCount

    from tests.djangoproject.clubs.models import Club as DjangoClub

    query = DjangoClub.objects.filter(memberships__role="member").annotate(n=DjangoCount("memberships"))
    return list(query.order_by("slug").values_list("slug", "n"))


@case(
    "select",
    "lookup.exact",
    "path.nested",
    "clause.annotate",
    "aggregate.Count",
    expected=[("koper-sailing", 2), ("ljubljana-rowing", 3), ("maribor-climbing", 4)],
    django=_django_filter_then_annotate_counts_the_matched_rows,
)
async def filter_then_annotate_counts_the_matched_rows():
    query = Club.objects.filter(memberships__role="member").annotate(n=Count("memberships"))
    return await rows(query.order_by("slug").values_list("slug", "n"))


def _django_annotate_then_filter_joins_again():
    from django.db.models import Count as DjangoCount

    from tests.djangoproject.clubs.models import Club as DjangoClub

    query = DjangoClub.objects.annotate(n=DjangoCount("memberships")).filter(memberships__role="member")
    return list(query.order_by("slug").values_list("slug", "n"))


@case(
    "select",
    "lookup.exact",
    "path.nested",
    "clause.annotate",
    "aggregate.Count",
    expected=[("koper-sailing", 6), ("ljubljana-rowing", 15), ("maribor-climbing", 20)],
    django=_django_annotate_then_filter_joins_again,
)
async def annotate_then_filter_joins_again():
    query = Club.objects.annotate(n=Count("memberships")).filter(memberships__role="member")
    return await rows(query.order_by("slug").values_list("slug", "n"))


def _django_filter_then_grouped_count():
    from django.db.models import Count as DjangoCount

    from tests.djangoproject.clubs.models import Club as DjangoClub

    query = DjangoClub.objects.filter(memberships__active=True).values("slug").annotate(n=DjangoCount("memberships"))
    return list(query.order_by("slug").values_list("slug", "n"))


@case(
    "select",
    "lookup.exact",
    "path.nested",
    "clause.annotate",
    "clause.group_by",
    "aggregate.Count",
    expected=[("koper-sailing", 3), ("ljubljana-rowing", 4), ("maribor-climbing", 5)],
    django=_django_filter_then_grouped_count,
)
async def filter_then_grouped_count():
    query = Club.objects.filter(memberships__active=True).values("slug").annotate(n=Count("memberships"))
    return await rows(query.order_by("slug").values_list("slug", "n"))


def _django_annotation_after_grouping_joins_the_group():
    from django.db.models import Count as DjangoCount, F as DjangoF

    from tests.djangoproject.clubs.models import Membership as DjangoMembership

    query = DjangoMembership.objects.values("role").annotate(n=DjangoCount("*")).annotate(c=DjangoF("club__slug"))
    return list(query.order_by("role", "c").values_list("role", "c", "n"))


@case(
    "select",
    "path.nested",
    "clause.annotate",
    "clause.group_by",
    "aggregate.Count",
    expected=[
        ("board", "ljubljana-rowing", 1),
        ("coach", "koper-sailing", 1),
        ("coach", "ljubljana-rowing", 1),
        ("coach", "maribor-climbing", 1),
        ("member", "koper-sailing", 2),
        ("member", "ljubljana-rowing", 3),
        ("member", "maribor-climbing", 4),
    ],
    django=_django_annotation_after_grouping_joins_the_group,
)
async def annotation_after_grouping_joins_the_group():
    query = Membership.objects.values("role").annotate(n=Count("*")).annotate(c=F("club__slug"))
    return await rows(query.order_by("role", "c").values_list("role", "c", "n"))


def _django_grouped_count_exists_and_get():
    from django.db.models import Count as DjangoCount

    from tests.djangoproject.clubs.models import Membership as DjangoMembership

    groups = DjangoMembership.objects.values("role").annotate(n=DjangoCount("*"))
    large = groups.filter(n__gte=3)
    return (
        large.count(),
        large.exists(),
        groups.filter(n__gte=100).exists(),
        groups.filter(n__gte=100).count(),
        groups.get(role="board"),
    )


@case(
    "count",
    "exists",
    "get",
    "lookup.exact",
    "lookup.gte",
    "clause.annotate",
    "clause.group_by",
    "aggregate.Count",
    expected=(2, True, False, 0, {"role": "board", "n": 1}),
    django=_django_grouped_count_exists_and_get,
)
async def grouped_count_exists_and_get():
    groups = Membership.objects.values("role").annotate(n=Count("*"))
    large = groups.filter(n__gte=3)
    return (
        await large.count(),
        await large.exists(),
        await groups.filter(n__gte=100).exists(),
        await groups.filter(n__gte=100).count(),
        await groups.get(role="board"),
    )


def _django_first_and_last_of_ordered_groups():
    from django.db.models import Count as DjangoCount

    from tests.djangoproject.clubs.models import Membership as DjangoMembership

    groups = DjangoMembership.objects.values("club__slug").annotate(n=DjangoCount("*")).order_by("n", "club__slug")
    return groups.first(), groups.last()


@case(
    "first",
    "last",
    "path.nested",
    "clause.annotate",
    "clause.group_by",
    "aggregate.Count",
    expected=({"club__slug": "koper-sailing", "n": 3}, {"club__slug": "maribor-climbing", "n": 5}),
    django=_django_first_and_last_of_ordered_groups,
)
async def first_and_last_of_ordered_groups():
    groups = Membership.objects.values("club__slug").annotate(n=Count("*")).order_by("n", "club__slug")
    return await groups.first(), await groups.last()


def _django_a_nested_outer_ref():
    from django.db.models import Exists as DjangoExists, OuterRef as DjangoOuterRef

    from tests.djangoproject.billing.models import Invoice as DjangoInvoice
    from tests.djangoproject.clubs.models import Club as DjangoClub, Membership as DjangoMembership

    invoiced = DjangoInvoice.objects.filter(
        member_id=DjangoOuterRef("member_id"), club_id=DjangoOuterRef(DjangoOuterRef("pk")), status="open"
    )
    memberships = DjangoMembership.objects.filter(club_id=DjangoOuterRef("pk"), role="board")
    memberships = memberships.filter(DjangoExists(invoiced))
    return sorted(DjangoClub.objects.filter(DjangoExists(memberships)).values_list("slug", flat=True))


@case(
    "select",
    "lookup.exact",
    "expression.Exists",
    "expression.OuterRef",
    expected=["ljubljana-rowing"],
    django=_django_a_nested_outer_ref,
)
async def a_nested_outer_ref():
    invoiced = Invoice.objects.filter(member_id=OuterRef("member_id"), club_id=OuterRef(OuterRef("pk")), status="open")
    memberships = Membership.objects.filter(club_id=OuterRef("pk"), role="board").filter(Exists(invoiced))
    return sorted(await rows(Club.objects.filter(Exists(memberships)).values_list("slug", flat=True)))


# Row locks


def _django_select_for_update():
    from tests.djangoproject.clubs.models import Member as DjangoMember

    query = DjangoMember.objects.select_for_update().filter(first_name="Ana")
    return sorted(member.first_name for member in query)


@case("select", "lookup.exact", "clause.select_for_update", expected=["Ana"], django=_django_select_for_update)
async def select_for_update():
    async with adorm.atomic():
        return await first_names(Member.objects.select_for_update().filter(first_name="Ana"))


def _django_select_for_update_options():
    from tests.djangoproject.clubs.models import Member as DjangoMember

    query = DjangoMember.objects.select_for_update(nowait=True, of=["self"], no_key=True).filter(first_name="Ana")
    return sorted(member.first_name for member in query)


@case(
    "select",
    "lookup.exact",
    "clause.select_for_update",
    "clause.select_for_update.nowait",
    "clause.select_for_update.of",
    "clause.select_for_update.no_key",
    expected=["Ana"],
    django=_django_select_for_update_options,
)
async def select_for_update_options():
    async with adorm.atomic():
        query = Member.objects.select_for_update(nowait=True, of=["self"], no_key=True).filter(first_name="Ana")
        return await first_names(query)


def _django_select_for_update_skip_locked():
    from tests.djangoproject.clubs.models import Member as DjangoMember

    query = DjangoMember.objects.select_for_update(skip_locked=True).filter(first_name="Ana")
    return sorted(member.first_name for member in query)


@case(
    "select",
    "lookup.exact",
    "clause.select_for_update",
    "clause.select_for_update.skip_locked",
    expected=["Ana"],
    django=_django_select_for_update_skip_locked,
)
async def select_for_update_skip_locked():
    async with adorm.atomic():
        return await first_names(Member.objects.select_for_update(skip_locked=True).filter(first_name="Ana"))


# Writes


def _django_create():
    from tests.djangoproject.activities.models import Tag as DjangoTag

    tag = DjangoTag.objects.create(name="night")
    stored = list(DjangoTag.objects.filter(pk=tag.pk).values_list("name", flat=True))
    return isinstance(tag.pk, int), DjangoTag.objects.filter(name="night").exists(), stored


@case("create", "select", "exists", "lookup.exact", expected=(True, True, ["night"]), django=_django_create)
async def create():
    tag = await Tag.objects.create(name="night")
    stored = [name async for name in Tag.objects.filter(pk=tag.pk).values_list("name", flat=True)]
    return isinstance(tag.pk, int), await Tag.objects.filter(name="night").exists(), stored


def _django_create_fills_defaults_and_a_generated_primary_key():
    from tests.djangoproject.billing.models import Invoice as DjangoInvoice, Payment as DjangoPayment

    invoice = DjangoInvoice.objects.get(number="2026-0003")
    payment = DjangoPayment.objects.create(invoice_id=invoice.pk, amount=Decimal("5.00"), paid_at=START)
    stored = DjangoPayment.objects.get(code=payment.code)
    return isinstance(payment.code, uuid.UUID), stored.amount, stored.reference


@case(
    "get",
    "create",
    "lookup.exact",
    expected=(True, Decimal("5.00"), ""),
    django=_django_create_fills_defaults_and_a_generated_primary_key,
)
async def create_fills_defaults_and_a_generated_primary_key():
    invoice = await Invoice.objects.get(number="2026-0003")
    payment = await Payment.objects.create(invoice_id=invoice.pk, amount=Decimal("5.00"), paid_at=START)
    stored = await Payment.objects.get(code=payment.code)
    return isinstance(payment.code, uuid.UUID), stored.amount, stored.reference


def _django_save_updates_the_loaded_fields():
    from tests.djangoproject.clubs.models import Member as DjangoMember

    ana = DjangoMember.objects.only("first_name", "level").get(first_name="Ana")
    ana.level = "advanced"
    ana.save()
    stored = DjangoMember.objects.get(first_name="Ana")
    return stored.level, stored.last_name, stored.email


@case(
    "get",
    "save",
    "lookup.exact",
    "clause.only",
    expected=("advanced", "Novak", "ana.0@example.com"),
    django=_django_save_updates_the_loaded_fields,
)
async def save_updates_the_loaded_fields():
    ana = await Member.objects.only("first_name", "level").get(first_name="Ana")
    ana.level = "advanced"
    await ana.save()
    stored = await Member.objects.get(first_name="Ana")
    return stored.level, stored.last_name, stored.email


def _django_save_with_update_fields_writes_only_those():
    from tests.djangoproject.clubs.models import Member as DjangoMember

    ana = DjangoMember.objects.get(first_name="Ana")
    ana.level = "advanced"
    ana.last_name = "Novak-Kos"
    ana.save(update_fields=["last_name"])
    ana.last_name = "Novak"
    ana.save(update_fields=["last_name"])
    stored = DjangoMember.objects.get(first_name="Ana")
    return stored.last_name, stored.level


@case(
    "get",
    "save",
    "lookup.exact",
    expected=("Novak", "beginner"),
    django=_django_save_with_update_fields_writes_only_those,
)
async def save_with_update_fields_writes_only_those():
    ana = await Member.objects.get(first_name="Ana")
    ana.level = "advanced"
    ana.last_name = "Novak-Kos"
    await ana.save(update_fields=["last_name"])
    ana.last_name = "Novak"
    await ana.save(update_fields=["last_name"])
    stored = await Member.objects.get(first_name="Ana")
    return stored.last_name, stored.level


def _django_save_inserts_a_new_instance():
    from tests.djangoproject.activities.models import Tag as DjangoTag

    tag = DjangoTag(name="dawn")
    tag.save()
    return tag.pk is not None, DjangoTag.objects.get(pk=tag.pk).name


@case("save", "get", "lookup.exact", expected=(True, "dawn"), django=_django_save_inserts_a_new_instance)
async def save_inserts_a_new_instance():
    tag = Tag(name="dawn")
    await tag.save()
    return tag.pk is not None, (await Tag.objects.get(pk=tag.pk)).name


def _django_update():
    from tests.djangoproject.clubs.models import Membership as DjangoMembership

    changed = DjangoMembership.objects.filter(active=False).update(active=True)
    unchanged = DjangoMembership.objects.filter(role="nobody").update(active=False)
    return changed, DjangoMembership.objects.filter(active=True).count(), unchanged


@case("update", "count", "lookup.exact", expected=(1, 13, 0), django=_django_update)
async def update():
    changed = await Membership.objects.filter(active=False).update(active=True)
    unchanged = await Membership.objects.filter(role="nobody").update(active=False)
    return changed, await Membership.objects.filter(active=True).count(), unchanged


def _django_update_with_arithmetic():
    from django.db.models import F as DjangoF

    from tests.djangoproject.activities.models import Activity as DjangoActivity

    changed = DjangoActivity.objects.filter(name="Crag day").update(capacity=DjangoF("capacity") + 5)
    return changed, DjangoActivity.objects.get(name="Crag day").capacity


@case(
    "update",
    "get",
    "lookup.exact",
    "expression.CombinedExpression",
    expected=(1, 20),
    django=_django_update_with_arithmetic,
)
async def update_with_arithmetic():
    changed = await Activity.objects.filter(name="Crag day").update(capacity=F("capacity") + 5)
    return changed, (await Activity.objects.get(name="Crag day")).capacity


def _django_delete():
    from tests.djangoproject.billing.models import Payment as DjangoPayment

    deleted = DjangoPayment.objects.filter(reference="SI00 0001").delete()
    return deleted, DjangoPayment.objects.count()


@case("delete", "count", "lookup.exact", expected=((1, {"billing.Payment": 1}), 6), django=_django_delete)
async def delete():
    deleted = await Payment.objects.filter(reference="SI00 0001").delete()
    return deleted, await Payment.objects.count()


def _django_delete_cascades():
    from tests.djangoproject.billing.models import Invoice as DjangoInvoice, InvoiceLine as DjangoInvoiceLine

    deleted = DjangoInvoice.objects.filter(number="2026-0001").delete()
    return deleted, DjangoInvoice.objects.count(), DjangoInvoiceLine.objects.count()


@case(
    "delete",
    "count",
    "lookup.exact",
    expected=((3, {"billing.Invoice": 1, "billing.InvoiceLine": 1, "billing.Payment": 1}), 18, 18),
    refusals={"memory": "does not cascade"},
    django=_django_delete_cascades,
)
async def delete_cascades():
    deleted = await Invoice.objects.filter(number="2026-0001").delete()
    return deleted, await Invoice.objects.count(), await InvoiceLine.objects.count()


def _django_get_or_create():
    from tests.djangoproject.activities.models import Tag as DjangoTag

    found, created = DjangoTag.objects.get_or_create(name="indoor")
    new, created_new = DjangoTag.objects.get_or_create(name="night")
    again, created_again = DjangoTag.objects.get_or_create(name="night")
    return (created, found.name), (created_new, new.name), (created_again, again.name)


@case(
    "get_or_create",
    "lookup.exact",
    expected=((False, "indoor"), (True, "night"), (False, "night")),
    django=_django_get_or_create,
)
async def get_or_create():
    found, created = await Tag.objects.get_or_create(name="indoor")
    new, created_new = await Tag.objects.get_or_create(name="night")
    again, created_again = await Tag.objects.get_or_create(name="night")
    return (created, found.name), (created_new, new.name), (created_again, again.name)


def _django_update_or_create():
    from tests.djangoproject.clubs.models import Member as DjangoMember

    ana, created = DjangoMember.objects.update_or_create(email="ana.0@example.com", defaults={"level": "advanced"})
    new, created_new = DjangoMember.objects.update_or_create(
        email="new@example.com",
        defaults={"level": "advanced"},
        create_defaults={"first_name": "Nina", "last_name": "Nova", "joined": START, "level": "intermediate"},
    )
    stored = DjangoMember.objects.get(email="ana.0@example.com")
    return (created, ana.level, ana.last_name), (created_new, new.level, new.first_name), stored.level


@case(
    "update_or_create",
    "get",
    "lookup.exact",
    expected=((False, "advanced", "Novak"), (True, "intermediate", "Nina"), "advanced"),
    django=_django_update_or_create,
)
async def update_or_create():
    ana, created = await Member.objects.update_or_create(email="ana.0@example.com", defaults={"level": "advanced"})
    new, created_new = await Member.objects.update_or_create(
        email="new@example.com",
        defaults={"level": "advanced"},
        create_defaults={"first_name": "Nina", "last_name": "Nova", "joined": START, "level": "intermediate"},
    )
    stored = await Member.objects.get(email="ana.0@example.com")
    return (created, ana.level, ana.last_name), (created_new, new.level, new.first_name), stored.level


def _django_delete_an_instance():
    from tests.djangoproject.billing.models import Payment as DjangoPayment

    payment = DjangoPayment.objects.get(reference="SI00 0001")
    deleted = payment.delete()
    return deleted, payment.pk, DjangoPayment.objects.count()


@case(
    "get",
    "delete_instance",
    "count",
    "lookup.exact",
    expected=((1, {"billing.Payment": 1}), None, 6),
    django=_django_delete_an_instance,
)
async def delete_an_instance():
    payment = await Payment.objects.get(reference="SI00 0001")
    deleted = await payment.delete()
    return deleted, payment.pk, await Payment.objects.count()


def _django_delete_protected():
    from django.db.models import ProtectedError as DjangoProtectedError

    from tests.djangoproject.billing.models import Invoice as DjangoInvoice
    from tests.djangoproject.clubs.models import Member as DjangoMember

    try:
        DjangoMember.objects.filter(first_name="Ana").delete()
    except DjangoProtectedError as exc:
        objects = exc.protected_objects
        protected = sorted(o.number for o in objects), all(isinstance(o, DjangoInvoice) for o in objects)
    return protected, DjangoMember.objects.count()


@case(
    "delete",
    "count",
    "lookup.exact",
    expected=((["2026-0001", "2026-0011"], True), 12),
    refusals={"memory": "does not cascade"},
    django=_django_delete_protected,
)
async def delete_protected():
    try:
        await Member.objects.filter(first_name="Ana").delete()
    except ProtectedError as exc:
        objects = exc.protected_objects
        protected = sorted(o.number for o in objects), all(isinstance(o, Invoice) for o in objects)
    return protected, await Member.objects.count()


def _django_catch_a_protected_error_inside_a_transaction():
    from django.db import transaction
    from django.db.models import ProtectedError as DjangoProtectedError

    from tests.djangoproject.clubs.models import Club as DjangoClub, Member as DjangoMember

    refused = False
    with transaction.atomic():
        try:
            DjangoMember.objects.filter(first_name="Ana").delete()
        except DjangoProtectedError:
            refused = True
        DjangoClub.objects.filter(slug="koper-sailing").update(city="Capodistria")
    return refused, DjangoClub.objects.filter(city="Capodistria").count(), DjangoMember.objects.count()


@case(
    "delete",
    "update",
    "count",
    "lookup.exact",
    expected=(True, 1, 12),
    refusals={"memory": "does not cascade"},
    django=_django_catch_a_protected_error_inside_a_transaction,
)
async def catch_a_protected_error_inside_a_transaction():
    refused = False
    async with adorm.atomic():
        try:
            await Member.objects.filter(first_name="Ana").delete()
        except ProtectedError:
            refused = True
        await Club.objects.filter(slug="koper-sailing").update(city="Capodistria")
    return refused, await Club.objects.filter(city="Capodistria").count(), await Member.objects.count()


def _django_save_with_arithmetic():
    from django.db.models import F as DjangoF

    from tests.djangoproject.activities.models import Activity as DjangoActivity

    crag = DjangoActivity.objects.get(name="Crag day")
    crag.capacity = DjangoF("capacity") + 1
    crag.save(update_fields=["capacity"])
    return DjangoActivity.objects.get(name="Crag day").capacity


@case(
    "get",
    "save",
    "lookup.exact",
    "expression.CombinedExpression",
    expected=16,
    django=_django_save_with_arithmetic,
)
async def save_with_arithmetic():
    crag = await Activity.objects.get(name="Crag day")
    crag.capacity = F("capacity") + 1
    await crag.save(update_fields=["capacity"])
    return (await Activity.objects.get(name="Crag day")).capacity


def _django_update_a_parent_row_of_a_multi_table_child():
    from tests.djangoproject.activities.models import Activity as DjangoActivity, Competition as DjangoCompetition

    changed = DjangoActivity.objects.filter(name="Spring regatta").update(capacity=99)
    return changed, DjangoCompetition.objects.get(name="Spring regatta").capacity


@case(
    "get",
    "update",
    "lookup.exact",
    expected=(1, 99),
    django=_django_update_a_parent_row_of_a_multi_table_child,
)
async def update_a_parent_row_of_a_multi_table_child():
    changed = await Activity.objects.filter(name="Spring regatta").update(capacity=99)
    return changed, (await Competition.objects.get(name="Spring regatta")).capacity


def _django_update_a_multi_table_child():
    from tests.djangoproject.activities.models import Competition as DjangoCompetition

    changed = DjangoCompetition.objects.filter(name="Spring regatta").update(discipline="Rowing")
    regatta = DjangoCompetition.objects.get(name="Spring regatta")
    return changed, regatta.discipline, regatta.capacity


@case(
    "get",
    "update",
    "lookup.exact",
    expected=(1, "Rowing", 30),
    django=_django_update_a_multi_table_child,
)
async def update_a_multi_table_child():
    changed = await Competition.objects.filter(name="Spring regatta").update(discipline="Rowing")
    regatta = await Competition.objects.get(name="Spring regatta")
    return changed, regatta.discipline, regatta.capacity


def _django_create_a_multi_table_child():
    from tests.djangoproject.activities.models import Activity as DjangoActivity, Competition as DjangoCompetition
    from tests.djangoproject.clubs.models import Club as DjangoClub

    koper = DjangoClub.objects.get(slug="koper-sailing")
    DjangoCompetition.objects.create(
        club_id=koper.pk,
        name="Autumn regatta",
        kind="competition",
        starts_at=START,
        duration=datetime.timedelta(hours=4),
        capacity=12,
        discipline="Laser",
    )
    return DjangoActivity.objects.count(), DjangoCompetition.objects.count()


@case(
    "get",
    "create",
    "count",
    "lookup.exact",
    expected=(8, 3),
    refusals={"memory": "multi-table inheritance child"},
    django=_django_create_a_multi_table_child,
)
async def create_a_multi_table_child():
    koper = await Club.objects.get(slug="koper-sailing")
    await Competition.objects.create(
        club_id=koper.pk,
        name="Autumn regatta",
        kind="competition",
        starts_at=START,
        duration=datetime.timedelta(hours=4),
        capacity=12,
        discipline="Laser",
    )
    return await Activity.objects.count(), await Competition.objects.count()


def _django_bulk_create():
    from tests.djangoproject.activities.models import Tag as DjangoTag

    tags = DjangoTag.objects.bulk_create([DjangoTag(name="night"), DjangoTag(name="dawn")])
    stored = sorted(DjangoTag.objects.filter(pk__in=[tag.pk for tag in tags]).values_list("name", flat=True))
    return [isinstance(tag.pk, int) for tag in tags], [tag.name for tag in tags], stored


@case(
    "bulk_create",
    "select",
    "lookup.in",
    expected=([True, True], ["night", "dawn"], ["dawn", "night"]),
    django=_django_bulk_create,
)
async def bulk_create():
    tags = await Tag.objects.bulk_create([Tag(name="night"), Tag(name="dawn")])
    stored = sorted(
        [name async for name in Tag.objects.filter(pk__in=[tag.pk for tag in tags]).values_list("name", flat=True)]
    )
    return [isinstance(tag.pk, int) for tag in tags], [tag.name for tag in tags], stored


def _django_bulk_create_fills_defaults_and_generated_primary_keys():
    from tests.djangoproject.billing.models import Invoice as DjangoInvoice, Payment as DjangoPayment

    invoice = DjangoInvoice.objects.get(number="2026-0003")
    given = uuid.UUID("00000000-0000-4000-8000-000000000063")
    payments = DjangoPayment.objects.bulk_create(
        [
            DjangoPayment(invoice_id=invoice.pk, amount=Decimal("5.00"), paid_at=START),
            DjangoPayment(code=given, invoice_id=invoice.pk, amount=Decimal("6.00"), paid_at=START, reference="r"),
        ]
    )
    stored = [DjangoPayment.objects.get(code=payment.code) for payment in payments]
    return (
        isinstance(payments[0].code, uuid.UUID),
        payments[1].code == given,
        [(payment.amount, payment.reference) for payment in stored],
    )


@case(
    "get",
    "bulk_create",
    "lookup.exact",
    expected=(True, True, [(Decimal("5.00"), ""), (Decimal("6.00"), "r")]),
    django=_django_bulk_create_fills_defaults_and_generated_primary_keys,
)
async def bulk_create_fills_defaults_and_generated_primary_keys():
    invoice = await Invoice.objects.get(number="2026-0003")
    given = uuid.UUID("00000000-0000-4000-8000-000000000063")
    payments = await Payment.objects.bulk_create(
        [
            Payment(invoice_id=invoice.pk, amount=Decimal("5.00"), paid_at=START),
            Payment(code=given, invoice_id=invoice.pk, amount=Decimal("6.00"), paid_at=START, reference="r"),
        ]
    )
    stored = [await Payment.objects.get(code=payment.code) for payment in payments]
    return (
        isinstance(payments[0].code, uuid.UUID),
        payments[1].code == given,
        [(payment.amount, payment.reference) for payment in stored],
    )


def _django_bulk_create_in_batches_inserts_all_rows_or_none():
    from django.db import IntegrityError as DjangoIntegrityError, transaction

    from tests.djangoproject.activities.models import Tag as DjangoTag

    existing = DjangoTag.objects.get(name="indoor")
    DjangoTag.objects.bulk_create([DjangoTag(name=f"batch {n}") for n in range(5)], batch_size=2)
    failed = False
    try:
        with transaction.atomic():
            DjangoTag.objects.bulk_create(
                [DjangoTag(pk=9001, name="a"), DjangoTag(pk=9002, name="b"), DjangoTag(pk=existing.pk, name="c")],
                batch_size=2,
            )
    except DjangoIntegrityError:
        failed = True
    batches = DjangoTag.objects.filter(name__startswith="batch ").count()
    return batches, failed, DjangoTag.objects.filter(pk=9001).count()


@case(
    "get",
    "bulk_create",
    "count",
    "lookup.exact",
    "lookup.startswith",
    expected=(5, True, 0),
    django=_django_bulk_create_in_batches_inserts_all_rows_or_none,
)
async def bulk_create_in_batches_inserts_all_rows_or_none():
    existing = await Tag.objects.get(name="indoor")
    await Tag.objects.bulk_create([Tag(name=f"batch {n}") for n in range(5)], batch_size=2)
    failed = False
    try:
        # The first batch is inserted before the second fails on the existing primary key.
        await Tag.objects.bulk_create(
            [Tag(pk=9001, name="a"), Tag(pk=9002, name="b"), Tag(pk=existing.pk, name="c")], batch_size=2
        )
    except adorm.IntegrityError:
        failed = True
    batches = await Tag.objects.filter(name__startswith="batch ").count()
    return batches, failed, await Tag.objects.filter(pk=9001).count()


def _django_bulk_create_ignoring_conflicts():
    from tests.djangoproject.activities.models import Tag as DjangoTag

    tags = DjangoTag.objects.bulk_create([DjangoTag(name="indoor"), DjangoTag(name="night")], ignore_conflicts=True)
    return [tag.pk for tag in tags], DjangoTag.objects.filter(name__in=["indoor", "night"]).count()


@case(
    "bulk_create",
    "count",
    "lookup.in",
    "clause.on_conflict.ignore",
    expected=([None, None], 2),
    django=_django_bulk_create_ignoring_conflicts,
)
async def bulk_create_ignoring_conflicts():
    tags = await Tag.objects.bulk_create([Tag(name="indoor"), Tag(name="night")], ignore_conflicts=True)
    return [tag.pk for tag in tags], await Tag.objects.filter(name__in=["indoor", "night"]).count()


def _django_bulk_create_updating_conflicts():
    from tests.djangoproject.clubs.models import Member as DjangoMember

    ana = DjangoMember.objects.get(email="ana.0@example.com")
    members = DjangoMember.objects.bulk_create(
        [
            DjangoMember(first_name="X", last_name="Y", email="ana.0@example.com", joined=START, level="advanced"),
            DjangoMember(first_name="Nina", last_name="Nova", email="nina@example.com", joined=START),
        ],
        update_conflicts=True,
        update_fields=["level"],
        unique_fields=["email"],
    )
    stored = DjangoMember.objects.get(email="ana.0@example.com")
    return members[0].pk == ana.pk, isinstance(members[1].pk, int), (stored.first_name, stored.level)


@case(
    "get",
    "bulk_create",
    "lookup.exact",
    "clause.on_conflict.update",
    expected=(True, True, ("Ana", "advanced")),
    django=_django_bulk_create_updating_conflicts,
)
async def bulk_create_updating_conflicts():
    ana = await Member.objects.get(email="ana.0@example.com")
    members = await Member.objects.bulk_create(
        [
            Member(first_name="X", last_name="Y", email="ana.0@example.com", joined=START, level="advanced"),
            Member(first_name="Nina", last_name="Nova", email="nina@example.com", joined=START),
        ],
        update_conflicts=True,
        update_fields=["level"],
        unique_fields=["email"],
    )
    stored = await Member.objects.get(email="ana.0@example.com")
    return members[0].pk == ana.pk, isinstance(members[1].pk, int), (stored.first_name, stored.level)


def _django_bulk_create_ignores_conflict_fields_without_update_conflicts():
    from tests.djangoproject.activities.models import Tag as DjangoTag

    tags = DjangoTag.objects.bulk_create(
        [DjangoTag(name="upsert off")], update_conflicts=False, update_fields=["name"], unique_fields=["name"]
    )
    ignored = DjangoTag.objects.bulk_create(
        [DjangoTag(name="upsert off")], ignore_conflicts=True, unique_fields=["name"]
    )
    return isinstance(tags[0].pk, int), ignored[0].pk, DjangoTag.objects.filter(name="upsert off").count()


@case(
    "bulk_create",
    "count",
    "lookup.exact",
    "clause.on_conflict.ignore",
    expected=(True, None, 1),
    django=_django_bulk_create_ignores_conflict_fields_without_update_conflicts,
)
async def bulk_create_ignores_conflict_fields_without_update_conflicts():
    tags = await Tag.objects.bulk_create(
        [Tag(name="upsert off")], update_conflicts=False, update_fields=["name"], unique_fields=["name"]
    )
    ignored = await Tag.objects.bulk_create([Tag(name="upsert off")], ignore_conflicts=True, unique_fields=["name"])
    return isinstance(tags[0].pk, int), ignored[0].pk, await Tag.objects.filter(name="upsert off").count()


# Prefetching, in_bulk(), generic foreign keys


@case(
    "select",
    "lookup.in",
    expected=[
        ("koper-sailing", ["Igor", "Jana", "Klemen"]),
        ("ljubljana-rowing", ["Ana", "Bojan", "Cvetka", "David", "Lara"]),
        ("maribor-climbing", ["Cvetka", "Eva", "Filip", "Gaja", "Hana"]),
    ],
)
async def prefetch_a_reverse_and_a_forward_relation():
    clubs = [club async for club in Club.objects.order_by("slug").prefetch_related("memberships__member")]
    return [
        (club.slug, sorted([(await membership.member).first_name async for membership in club.memberships]))
        for club in clubs
    ]


@case("select", "lookup.exact", "lookup.in", expected=["Ana", "Cvetka", "David"])
async def prefetch_a_many_to_many_relation_through_a_model():
    (sculling,) = [a async for a in Activity.objects.filter(name="Morning sculling").prefetch_related("participants")]
    return sorted([member.first_name async for member in sculling.participants])


@case(
    "select",
    "lookup.exact",
    "lookup.in",
    expected=[
        ("Bouldering basics", ["Filip", "Gaja"]),
        ("Crag day", []),
        ("Dinghy handling", ["Jana"]),
        ("Ergometer session", []),
        ("Morning sculling", ["Ana", "Cvetka"]),
        ("Regional bouldering cup", []),
        ("Spring regatta", []),
    ],
)
async def prefetch_with_a_query_set_into_an_attribute():
    attended = Attendance.objects.filter(status="attended").order_by("member_id")
    activities = Activity.objects.order_by("name").prefetch_related(
        Prefetch("attendances", queryset=attended, to_attr="attended"), "attended__member"
    )
    return [
        (activity.name, [(await attendance.member).first_name for attendance in activity.attended])
        async for activity in activities
    ]


@case("first", "get", "lookup.exact", expected=("Membership", Decimal("120.00"), "Ana", "Ana"))
async def generic_foreign_key_object():
    line = await InvoiceLine.objects.order_by("id").first()
    item = await line.item
    again = await line.item
    return type(item).__name__, item.fee, (await item.member).first_name, (await again.member).first_name


@case("get", "lookup.exact", expected=("Membership", True, "membership", "Membership"))
async def content_type_of_a_model_cached():
    import tests.clubmodels.contenttypes  # noqa: F401 - registers the content type model

    content_type = await adorm.content_types.get_id(Membership)
    row = await adorm.content_types.get_for_model(Membership)
    model = await adorm.content_types.get_model(content_type)
    return model.__name__, row.pk == content_type, row.model, (await adorm.content_types.get_model(row.pk)).__name__


def _django_generic_foreign_key_assignment():
    from tests.djangoproject.activities.models import Activity as DjangoActivity
    from tests.djangoproject.billing.models import Invoice as DjangoInvoice, InvoiceLine as DjangoInvoiceLine
    from tests.djangoproject.clubs.models import Membership as DjangoMembership

    invoice = DjangoInvoice.objects.get(number="2026-0003")
    membership = DjangoMembership.objects.order_by("id").first()
    created = DjangoInvoiceLine.objects.create(
        invoice=invoice, item=membership, description="Fee", unit_price=Decimal("10.00")
    )
    activity = DjangoActivity.objects.order_by("id").first()
    saved = DjangoInvoiceLine(invoice=invoice, item=membership, description="Activity", unit_price=Decimal("4.00"))
    saved.item = activity
    saved.save()
    first = DjangoInvoiceLine.objects.get(pk=created.pk).item
    second = DjangoInvoiceLine.objects.get(pk=saved.pk).item
    return type(first).__name__, first.pk == membership.pk, type(second).__name__, second.pk == activity.pk


@case(
    "first",
    "get",
    "create",
    "save",
    "lookup.exact",
    expected=("Membership", True, "Activity", True),
    django=_django_generic_foreign_key_assignment,
)
async def generic_foreign_key_assignment():
    invoice = await Invoice.objects.get(number="2026-0003")
    membership = await Membership.objects.order_by("id").first()
    created = await InvoiceLine.objects.create(
        invoice_id=invoice.pk, item=membership, description="Fee", unit_price=Decimal("10.00")
    )
    activity = await Activity.objects.order_by("id").first()
    saved = InvoiceLine(invoice_id=invoice.pk, item=membership, description="Activity", unit_price=Decimal("4.00"))
    saved.item = activity
    await saved.save()
    first = await (await InvoiceLine.objects.get(pk=created.pk)).item
    second = await (await InvoiceLine.objects.get(pk=saved.pk)).item
    return type(first).__name__, first.pk == membership.pk, type(second).__name__, second.pk == activity.pk


def _django_generic_foreign_key_in_defaults():
    from tests.djangoproject.activities.models import Activity as DjangoActivity
    from tests.djangoproject.billing.models import Invoice as DjangoInvoice, InvoiceLine as DjangoInvoiceLine
    from tests.djangoproject.clubs.models import Membership as DjangoMembership

    invoice = DjangoInvoice.objects.get(number="2026-0003")
    membership = DjangoMembership.objects.order_by("id").first()
    activity = DjangoActivity.objects.order_by("id").first()
    line, created = DjangoInvoiceLine.objects.get_or_create(
        description="Defaults", defaults={"invoice": invoice, "item": membership, "unit_price": Decimal("3.00")}
    )
    first = DjangoInvoiceLine.objects.get(pk=line.pk).item
    _, created_again = DjangoInvoiceLine.objects.update_or_create(description="Defaults", defaults={"item": activity})
    second = DjangoInvoiceLine.objects.get(pk=line.pk).item
    return created, type(first).__name__, created_again, type(second).__name__, second.pk == activity.pk


@case(
    "first",
    "get",
    "get_or_create",
    "update_or_create",
    "lookup.exact",
    expected=(True, "Membership", False, "Activity", True),
    django=_django_generic_foreign_key_in_defaults,
)
async def generic_foreign_key_in_defaults():
    invoice = await Invoice.objects.get(number="2026-0003")
    membership = await Membership.objects.order_by("id").first()
    activity = await Activity.objects.order_by("id").first()
    line, created = await InvoiceLine.objects.get_or_create(
        description="Defaults", defaults={"invoice_id": invoice.pk, "item": membership, "unit_price": Decimal("3.00")}
    )
    first = await (await InvoiceLine.objects.get(pk=line.pk)).item
    _, created_again = await InvoiceLine.objects.update_or_create(description="Defaults", defaults={"item": activity})
    second = await (await InvoiceLine.objects.get(pk=line.pk)).item
    return created, type(first).__name__, created_again, type(second).__name__, second.pk == activity.pk


@case(
    "select",
    "lookup.in",
    expected=(
        [("Activity", 9), ("Membership", 10)],
        [("koper-sailing", 4), ("ljubljana-rowing", 7), ("maribor-climbing", 8)],
    ),
)
async def prefetch_across_a_generic_foreign_key_into_an_attribute():
    lines = [
        line
        async for line in InvoiceLine.objects.order_by("id").prefetch_related(
            Prefetch("item", to_attr="billed"), "billed__club"
        )
    ]
    kinds: dict[str, int] = {}
    clubs: dict[str, int] = {}
    for line in lines:
        kinds[type(line.billed).__name__] = kinds.get(type(line.billed).__name__, 0) + 1
        slug = (await line.billed.club).slug
        clubs[slug] = clubs.get(slug, 0) + 1
    return sorted(kinds.items()), sorted(clubs.items())


@case("in_bulk", "lookup.in", expected=(SLUGS, True, ["koper-sailing"]))
async def in_bulk_by_primary_key():
    every = await Club.objects.in_bulk()
    koper = next(pk for pk, club in every.items() if club.slug == "koper-sailing")
    some = await Club.objects.in_bulk([koper, 0])
    return (
        sorted(club.slug for club in every.values()),
        all(pk == club.pk for pk, club in every.items()),
        [club.slug for club in some.values()],
    )


@case(
    "in_bulk",
    "lookup.in",
    expected={"koper-sailing": "Koper Sailing Club"},
    refusals={"memory": "unique constraints"},
)
async def in_bulk_by_a_unique_field():
    found = await Club.objects.in_bulk(["koper-sailing", "nowhere"], field_name="slug")
    return {slug: club.name for slug, club in found.items()}


def _django_create_a_multi_table_child_of_an_existing_parent():
    from tests.djangoproject.activities.models import Activity as DjangoActivity, Competition as DjangoCompetition

    crag = DjangoActivity.objects.get(name="Crag day")
    DjangoCompetition(activity_ptr=crag, discipline="Climbing").save_base(raw=True)
    competition = DjangoCompetition.objects.get(name="Crag day")
    return (
        DjangoActivity.objects.count(),
        DjangoCompetition.objects.count(),
        competition.name,
        competition.discipline,
        competition.capacity,
    )


@case(
    "get",
    "create_from_parent",
    "count",
    "lookup.exact",
    expected=(7, 3, "Crag day", "Climbing", 15),
    django=_django_create_a_multi_table_child_of_an_existing_parent,
)
async def create_a_multi_table_child_of_an_existing_parent():
    crag = await Activity.objects.get(name="Crag day")
    competition = await Competition.objects.create_from_parent(crag, discipline="Climbing")
    return (
        await Activity.objects.count(),
        await Competition.objects.count(),
        competition.name,
        competition.discipline,
        competition.capacity,
    )


@case(
    "get",
    "full_clean",
    "lookup.exact",
    expected=(
        {"due": ["The due date cannot be before the issue date."]},
        [(None, None)],
        {"__all__": ["Membership with this Club and Member already exists."]},
        [("unique_together", ("club", "member"))],
        None,
    ),
)
async def full_clean_runs_the_model_clean_and_constraints():
    invoice = await Invoice.objects.get(number="2026-0001")
    await invoice.full_clean()
    invoice.due = datetime.date(2025, 1, 1)
    try:
        await invoice.full_clean()
    except ValidationError as exc:
        clean = exc.message_dict
        clean_codes = [(e.code, e.params) for e in exc.error_dict["due"]]
    board = await Membership.objects.get(role="board")
    copy = Membership(club_id=board.club_id, member_id=board.member_id, since=datetime.date(2026, 1, 1), fee=1)
    try:
        await copy.full_clean()
    except ValidationError as exc:
        unique = exc.message_dict
        unique_codes = [(e.code, e.params["unique_check"]) for e in exc.error_dict["__all__"]]
    return clean, clean_codes, unique, unique_codes, await copy.full_clean(exclude=["member"])


# The schema check


@case("check_table", expected=((), ()))
async def check_table_finds_the_club_tables_as_the_models_expect():
    from adorm.checks import table_check

    club = await adorm.create_session().execute(table_check(Club))
    membership = await adorm.create_session().execute(table_check(Membership))
    return tuple(club), tuple(membership)
