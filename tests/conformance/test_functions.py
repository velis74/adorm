"""Database functions on the bundled drivers beyond the catalogue: the capabilities they declare, functions combined
with lookups, negation, subqueries and transactions, and the errors of a function the database cannot apply. Results
are compared with the Django ORM's for the same query."""

import datetime

import pytest

import adorm

from adorm import Value
from adorm.query import Exact, OuterRef, Subquery
from tests.clubmodels.activities import Activity, Competition
from tests.clubmodels.billing import Invoice
from tests.clubmodels.clubs import Member
from tests.library_data import in_thread

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("bundled", "router")]

FUNCTIONS = """Cast Collate Greatest Least NullIf JSONArray JSONObject Extract ExtractYear ExtractIsoYear ExtractQuarter
ExtractMonth ExtractWeek ExtractDay ExtractWeekDay ExtractIsoWeekDay ExtractHour ExtractMinute ExtractSecond Abs ACos
ASin ATan ATan2 Ceil Cos Cot Degrees Exp Floor Ln Log Mod Pi Power Radians Random Round Sign Sin Sqrt Tan Chr Left
LPad LTrim MD5 Ord Repeat Replace Reverse Right RPad RTrim SHA1 SHA224 SHA256 SHA384 SHA512 StrIndex Substr Trim
UUID4 UUID7""".split()


@pytest.fixture
def bundled(conformance_driver):
    """The name of the driver under test; the test suite's memory driver has no database functions."""
    if conformance_driver.name == "memory":
        pytest.skip("the memory driver has no database functions")
    return conformance_driver.name


async def rows(queryset):
    return [row async for row in queryset]


def test_function_names_are_all_distinct():
    assert len(FUNCTIONS) == len(set(FUNCTIONS))


async def test_bundled_drivers_declare_every_function(driver):
    missing = {f"function.{name}" for name in FUNCTIONS} - driver.capabilities.constructs
    assert missing == set()


async def test_string_lookups_apply_to_a_cast_to_str():
    from adorm.query import Cast

    def django():
        from django.db.models import TextField
        from django.db.models.functions import Cast as DjangoCast

        from tests.djangoproject.activities.models import Activity as DjangoActivity

        query = DjangoActivity.objects.annotate(c=DjangoCast("capacity", TextField())).filter(c__istartswith="1")
        return sorted(query.values_list("name", "c"))

    query = Activity.objects.annotate(c=Cast("capacity", str)).filter(c__istartswith="1")
    result = sorted(await rows(query.values_list("name", "c")))
    assert result == [("Bouldering basics", "10"), ("Crag day", "15"), ("Ergometer session", "12")]
    assert result == await in_thread(django)


async def test_date_lookups_apply_to_a_cast_to_date():
    from adorm.query import Cast

    def django():
        from django.db.models import DateField
        from django.db.models.functions import Cast as DjangoCast

        from tests.djangoproject.clubs.models import Member as DjangoMember

        query = DjangoMember.objects.annotate(day=DjangoCast("joined", DateField()))
        return sorted(query.filter(day__gte=datetime.date(2025, 9, 1)).values_list("first_name", flat=True))

    query = Member.objects.annotate(day=Cast("joined", datetime.date))
    result = sorted(await rows(query.filter(day__gte=datetime.date(2025, 9, 1)).values_list("first_name", flat=True)))
    assert result == ["Ana", "Bojan", "Cvetka", "David", "Eva"]
    assert result == await in_thread(django)


async def test_values_dicts_carry_function_annotations():
    from adorm.query import ExtractYear, JSONObject

    def django():
        from django.db.models.functions import ExtractYear as DjangoExtractYear, JSONObject as DjangoJSONObject

        from tests.djangoproject.clubs.models import Member as DjangoMember

        query = DjangoMember.objects.filter(first_name__in=["Ana", "Lara"]).annotate(
            y=DjangoExtractYear("birth_date"), card=DjangoJSONObject(name="first_name", born="birth_date")
        )
        return list(query.order_by("first_name").values("first_name", "y", "card"))

    query = Member.objects.filter(first_name__in=["Ana", "Lara"]).annotate(
        y=ExtractYear("birth_date"), card=JSONObject(name="first_name", born="birth_date")
    )
    result = await rows(query.order_by("first_name").values("first_name", "y", "card"))
    assert result == [
        {"first_name": "Ana", "y": 1970, "card": {"name": "Ana", "born": "1970-01-10"}},
        {"first_name": "Lara", "y": 2003, "card": {"name": "Lara", "born": "2003-12-10"}},
    ]
    assert result == await in_thread(django)


async def test_conditions_on_functions_combine_and_negate():
    from adorm.query import ExtractMonth, Mod

    def django():
        from django.db.models.functions import ExtractMonth as DjangoExtractMonth, Mod as DjangoMod
        from django.db.models.lookups import Exact as DjangoExact

        from tests.djangoproject.activities.models import Activity as DjangoActivity
        from tests.djangoproject.clubs.models import Member as DjangoMember

        month = DjangoExtractMonth("birth_date")
        edges = DjangoMember.objects.filter(DjangoExact(month, 1) | DjangoExact(month, 12))
        odd = DjangoActivity.objects.exclude(DjangoExact(DjangoMod("capacity", 2), 0))
        return sorted(edges.values_list("first_name", flat=True)), sorted(odd.values_list("name", flat=True))

    month = ExtractMonth("birth_date")
    edges = Member.objects.filter(Exact(month, 1) | Exact(month, 12))
    odd = Activity.objects.exclude(Exact(Mod("capacity", 2), 0))
    result = (
        sorted(await rows(edges.values_list("first_name", flat=True))),
        sorted(await rows(odd.values_list("name", flat=True))),
    )
    assert result == (["Ana", "Lara"], ["Crag day"])
    assert result == await in_thread(django)


async def test_order_by_a_function_of_a_nullable_field_places_nulls_as_asked():
    from adorm.query import ExtractDay

    def django():
        from django.db.models.functions import ExtractDay as DjangoExtractDay

        from tests.djangoproject.activities.models import Competition as DjangoCompetition

        day = DjangoExtractDay("entry_deadline")
        return (
            list(DjangoCompetition.objects.order_by(day.asc(nulls_first=True)).values_list("name", flat=True)),
            list(DjangoCompetition.objects.order_by(day.asc(nulls_last=True)).values_list("name", flat=True)),
        )

    day = ExtractDay("entry_deadline")
    result = (
        await rows(Competition.objects.order_by(day.asc(nulls_first=True)).values_list("name", flat=True)),
        await rows(Competition.objects.order_by(day.asc(nulls_last=True)).values_list("name", flat=True)),
    )
    assert result == (["Regional bouldering cup", "Spring regatta"], ["Spring regatta", "Regional bouldering cup"])
    assert result == await in_thread(django)


async def test_functions_inside_a_subquery():
    from adorm.query import ExtractMonth

    def django():
        from django.db.models import OuterRef as DjangoOuterRef, Subquery as DjangoSubquery
        from django.db.models.functions import ExtractMonth as DjangoExtractMonth

        from tests.djangoproject.billing.models import Invoice as DjangoInvoice
        from tests.djangoproject.clubs.models import Member as DjangoMember

        latest = (
            DjangoInvoice.objects.filter(member=DjangoOuterRef("pk"))
            .annotate(m=DjangoExtractMonth("issued"))
            .order_by("-issued", "-number")
            .values("m")[:1]
        )
        query = DjangoMember.objects.filter(first_name__in=["Ana", "Bojan", "David"]).annotate(m=DjangoSubquery(latest))
        return list(query.order_by("first_name").values_list("first_name", "m"))

    latest = (
        Invoice.objects.filter(member=OuterRef("pk"))
        .annotate(m=ExtractMonth("issued"))
        .order_by("-issued", "-number")
        .values("m")[:1]
    )
    query = Member.objects.filter(first_name__in=["Ana", "Bojan", "David"]).annotate(m=Subquery(latest))
    result = await rows(query.order_by("first_name").values_list("first_name", "m"))
    assert result == [("Ana", 3), ("Bojan", None), ("David", 1)]
    assert result == await in_thread(django)


async def test_an_update_with_functions_is_undone_with_its_transaction():
    from adorm.query import Left, LPad

    class UndoError(Exception):
        pass

    async def change_then_fail():
        async with adorm.atomic():
            changed = await Member.objects.filter(first_name="Ana").update(
                last_name=LPad(Left("last_name", 1), 3, Value("*"))
            )
            seen = (await Member.objects.get(first_name="Ana")).last_name
            raise UndoError(changed, seen)

    with pytest.raises(UndoError) as raised:
        await change_then_fail()
    assert raised.value.args == (1, "**N")
    assert (await Member.objects.get(first_name="Ana")).last_name == "Novak"


async def test_an_update_computes_each_row_from_its_own_values():
    from adorm.query import Power, Round

    def django():
        from tests.djangoproject.activities.models import Activity as DjangoActivity

        return sorted(DjangoActivity.objects.values_list("name", "capacity"))

    changed = await Activity.objects.filter(capacity__lt=10).update(capacity=Round(Power("capacity", 2)))
    assert changed == 2
    result = sorted(await rows(Activity.objects.values_list("name", "capacity")))
    assert dict(result)["Morning sculling"] == 64
    assert dict(result)["Dinghy handling"] == 36
    assert dict(result)["Crag day"] == 15
    assert result == await in_thread(django)


async def test_an_expression_of_functions_and_fields():
    from adorm.query import ExtractYear

    def django():
        from django.db.models.functions import ExtractYear as DjangoExtractYear

        from tests.djangoproject.clubs.models import Member as DjangoMember

        age = DjangoExtractYear("joined") - DjangoExtractYear("birth_date")
        query = DjangoMember.objects.annotate(age=age).filter(age__gte=50)
        return sorted(query.values_list("first_name", "age"))

    age = ExtractYear("joined") - ExtractYear("birth_date")
    query = Member.objects.annotate(age=age).filter(age__gte=50)
    result = sorted(await rows(query.values_list("first_name", "age")))
    assert result == [("Ana", 56), ("Bojan", 53)]
    assert result == await in_thread(django)


async def test_a_time_component_of_a_date_is_refused():
    from adorm.query import ExtractHour

    with pytest.raises((ValueError, adorm.DatabaseError), match="hour"):
        await rows(Member.objects.annotate(h=ExtractHour("birth_date")).values_list("h", flat=True))
    # The refusal leaves the pool serving the next query.
    assert await Member.objects.count() == 12
