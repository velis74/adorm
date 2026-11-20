"""Own expressions on every driver: functions, lookups and transforms an application defines with SQL templates (one
template or one per database vendor), functions with a translation registered for the Django-based drivers, and the
built-in `UUID4` and `UUID7` on every database and Django version the suite runs with (`UUID7` refused on PostgreSQL
before 18, which has no function for it).

The SQL-based drivers (Django, native PostgreSQL) render templates and run registered translations and declare their
construct names; a vendor without a template and a driver without SQL (the test suite's memory driver) refuse the
operation with `NotSupportedError` before it reaches a connection."""

from __future__ import annotations

import asyncio
import datetime
import time
import uuid

import pytest

from django.db import connection

import adorm

from adorm import Value
from adorm.query import Concat, Exact, Func, Lookup, register_lookup, register_transform, Transform
from tests.clubmodels.clubs import Member

pytestmark = pytest.mark.django_db(transaction=True)

SQL_DRIVERS = ("django", "asyncpg")
"""The drivers that render SQL templates and run translations registered for the Django-based drivers."""

MEMBERS = [
    ("Ana", "Novak"),
    ("Bojan", "Kovač"),
    ("Cvetka", "Horvat"),
    ("David", "Krajnc"),
    ("Eva", "Zupančič"),
    ("Filip", "Potočnik"),
    ("Gaja", "Kos"),
    ("Hana", "Vidmar"),
    ("Igor", "Golob"),
    ("Jana", "Turk"),
    ("Klemen", "Kralj"),
    ("Lara", "Božič"),
]


class Initials(Func):
    """The first letters of two strings, one template over both arguments."""

    arity = 2
    default_output_type = str
    template = "SUBSTR(%(expressions)s, 1, 1)"
    arg_joiner = ", 1, 1) || SUBSTR("


class Upcase(Func):
    """A database function by name only, rendered with the default template."""

    arity = 1
    default_output_type = str
    function = "UPPER"


class VendorCase(Func):
    """Upper case on PostgreSQL, lower case on SQLite: one template per vendor."""

    arity = 1
    default_output_type = str
    templates = {"postgresql": "UPPER(%(expressions)s)", "sqlite": "LOWER(%(expressions)s)"}


class PostgresOnly(Func):
    arity = 1
    default_output_type = str
    templates = {"postgresql": "REVERSE(%(expressions)s)"}


class SqliteOnly(Func):
    arity = 1
    default_output_type = str
    templates = {"sqlite": "LOWER(%(expressions)s)"}


@register_lookup
class SameInitial(Lookup):
    """Strings that start with the same character."""

    lookup_name = "same_initial"
    template = "SUBSTR(%(lhs)s, 1, 1) = SUBSTR(%(rhs)s, 1, 1)"


@register_transform
class Initial(Transform):
    """The first character of a string."""

    lookup_name = "initial"
    template = "SUBSTR(%(expressions)s, 1, 1)"

    @classmethod
    def applies_to(cls, field_type):
        return field_type in (str, None)

    @classmethod
    def output_type(cls, field_type):  # noqa: ARG003 - the base class's signature
        return str


class Shout(Func):
    """A function without SQL of its own: the Django-based drivers run the translation `shout()` registers."""

    arity = 1
    default_output_type = str


def shout(node, translate):
    from django.db.models import TextField, Value as DjangoValue
    from django.db.models.functions import Concat as DjangoConcat, Upper as DjangoUpper

    (argument,) = node.get_source_expressions()
    return DjangoConcat(DjangoUpper(translate(argument)), DjangoValue("!"), output_field=TextField())


@register_transform
class Itself(Transform):
    """The value itself, of unknown type."""

    lookup_name = "itself"
    template = "%(expressions)s"


@register_transform
class Loud(Transform):
    """A transform without SQL of its own: the Django-based drivers run the translation `loud()` registers."""

    lookup_name = "loud"

    @classmethod
    def applies_to(cls, field_type):
        return field_type in (str, None)

    @classmethod
    def output_type(cls, field_type):  # noqa: ARG003 - the base class's signature
        return str


def loud(lhs):
    from django.db.models.functions import Upper as DjangoUpper

    return DjangoUpper(lhs)


_registered: set[type] = set()


def register_shout() -> None:
    """Register the translation of `Shout` once, after the drivers' modules are imported."""
    from adorm.drivers.django import register_translation

    if Shout not in _registered:
        register_translation(Shout)(shout)
        _registered.add(Shout)


def vendor() -> str:
    return connection.vendor


def constructs(pool) -> frozenset[str]:
    return pool.capabilities.constructs


async def rows(queryset) -> list:
    return [row async for row in queryset]


async def assert_refused(awaitable, construct: str, driver_name: str, pool) -> None:
    """``awaitable`` raises the capability refusal naming ``construct``, before it takes a slot, and the pool serves
    the next operation."""
    with pytest.raises(adorm.NotSupportedError) as raised:
        await awaitable
    assert raised.value.driver == driver_name
    assert construct in raised.value.unsupported
    assert construct in str(raised.value)
    assert pool.in_use == 0
    assert await Member.objects.count() == 12


@pytest.fixture
def sql_driver(conformance_driver):
    if conformance_driver.name not in SQL_DRIVERS:
        pytest.skip(f"the {conformance_driver.name} driver has no SQL")
    return conformance_driver.name


@pytest.fixture
def driver_name(conformance_driver):
    return conformance_driver.name


# Functions with a template


@pytest.mark.usefixtures("club_data", "sql_driver")
async def test_a_function_with_one_template_runs_on_every_sql_driver(pool):
    assert "function.Initials" in constructs(pool)
    query = Member.objects.annotate(i=Initials("first_name", "last_name")).values_list("first_name", "i")
    result = sorted(await rows(query))
    assert result == sorted((first, first[0] + last[0]) for first, last in MEMBERS)


@pytest.mark.usefixtures("club_data", "sql_driver", "router")
async def test_a_function_with_a_template_in_a_filter_and_an_ordering():
    initials = Initials("first_name", "last_name")
    matched = await rows(Member.objects.filter(Exact(initials, "EZ")).values_list("first_name", flat=True))
    assert matched == ["Eva"]
    three = Member.objects.filter(first_name__in=["Lara", "Ana", "Klemen"])
    ordered = await rows(three.order_by(initials.desc()).values_list("first_name", flat=True))
    assert ordered == ["Lara", "Klemen", "Ana"]
    ordered = await rows(three.annotate(i=initials).order_by("i").values_list("i", flat=True))
    assert ordered == ["AN", "KK", "LB"]


@pytest.mark.usefixtures("club_data", "sql_driver")
async def test_a_function_by_name_uses_the_default_template(pool):
    assert "function.Upcase" in constructs(pool)
    query = Member.objects.filter(first_name__in=["Ana", "Eva"]).annotate(u=Upcase("first_name"))
    assert sorted(await rows(query.values_list("u", flat=True))) == ["ANA", "EVA"]


@pytest.mark.usefixtures("club_data", "sql_driver", "router")
async def test_a_function_whose_arguments_are_expressions():
    query = Member.objects.filter(first_name="Ana").annotate(u=Upcase(Concat("first_name", Value("-x"))))
    assert await rows(query.values_list("u", flat=True)) == ["ANA-X"]


@pytest.mark.usefixtures("club_data", "sql_driver", "router")
async def test_a_template_function_in_an_update():
    changed = await Member.objects.filter(first_name="Ana").update(last_name=Upcase("last_name"))
    assert changed == 1
    assert (await Member.objects.get(first_name="Ana")).last_name == "NOVAK"


@pytest.mark.usefixtures("club_data", "sql_driver")
async def test_per_vendor_templates_pick_the_vendor_s_sql(pool):
    assert "function.VendorCase" in constructs(pool)
    query = Member.objects.filter(first_name="Ana").annotate(v=VendorCase("first_name"))
    expected = {"postgresql": "ANA", "sqlite": "ana"}[vendor()]
    assert await rows(query.values_list("v", flat=True)) == [expected]


@pytest.mark.usefixtures("club_data")
async def test_a_vendor_without_a_template_refuses_before_anything_runs(sql_driver, pool):
    present, absent = (PostgresOnly, SqliteOnly) if vendor() == "postgresql" else (SqliteOnly, PostgresOnly)
    assert f"function.{present.__name__}" in constructs(pool)
    assert f"function.{absent.__name__}" not in constructs(pool)
    query = Member.objects.filter(first_name="Ana").annotate(v=absent("first_name"))
    await assert_refused(rows(query), f"function.{absent.__name__}", sql_driver, pool)
    applied = await rows(Member.objects.filter(first_name="Ana").annotate(v=present("first_name")).values_list("v"))
    assert applied == ([("anA",)] if present is PostgresOnly else [("ana",)])


@pytest.mark.usefixtures("club_data")
async def test_a_template_function_defined_after_the_drivers_are_imported_is_declared(sql_driver, pool):
    # A class of its own for each driver under test: its construct name is new to the driver.
    late = type(f"LateUpcase{sql_driver.capitalize()}", (Func,), {"arity": 1, "function": "UPPER"})
    assert f"function.{late.__name__}" in constructs(pool)
    query = Member.objects.filter(first_name="Eva").annotate(u=late("first_name"))
    assert await rows(query.values_list("u", flat=True)) == ["EVA"]


@pytest.mark.usefixtures("club_data")
async def test_a_driver_without_sql_refuses_a_template_function(driver_name, pool):
    if driver_name in SQL_DRIVERS:
        pytest.skip("the driver renders templates")
    assert "function.Initials" not in constructs(pool)
    query = Member.objects.annotate(i=Initials("first_name", "last_name"))
    await assert_refused(rows(query), "function.Initials", driver_name, pool)


# Lookups and transforms with a template


@pytest.mark.usefixtures("club_data", "sql_driver")
async def test_an_own_lookup_in_a_filter(pool):
    assert "lookup.same_initial" in constructs(pool)
    result = await rows(Member.objects.filter(first_name__same_initial="Gregor").values_list("first_name", flat=True))
    assert result == ["Gaja"]
    excluded = await rows(Member.objects.exclude(last_name__same_initial="Kranj").values_list("last_name", flat=True))
    assert sorted(excluded) == sorted(last for _, last in MEMBERS if not last.startswith("K"))


@pytest.mark.usefixtures("club_data", "sql_driver", "router")
async def test_an_own_lookup_as_a_condition_object():
    result = await rows(Member.objects.filter(SameInitial("last_name", "Zebra")).values_list("first_name", flat=True))
    assert result == ["Eva"]


@pytest.mark.usefixtures("club_data")
async def test_a_driver_without_sql_refuses_an_own_lookup(driver_name, pool):
    if driver_name in SQL_DRIVERS:
        pytest.skip("the driver renders templates")
    await assert_refused(
        rows(Member.objects.filter(first_name__same_initial="Gregor")), "lookup.same_initial", driver_name, pool
    )


@pytest.mark.usefixtures("club_data", "sql_driver")
async def test_an_own_transform_in_a_lookup_key_and_in_values(pool):
    assert "transform.initial" in constructs(pool)
    result = await rows(Member.objects.filter(last_name__initial="K").values_list("first_name", flat=True))
    assert sorted(result) == ["Bojan", "David", "Gaja", "Klemen"]
    initials = await rows(
        Member.objects.filter(first_name__in=["Ana", "Lara"]).order_by("first_name").values_list("first_name__initial")
    )
    assert initials == [("A",), ("L",)]


# Translations registered for the Django-based drivers


@pytest.mark.usefixtures("club_data", "sql_driver")
async def test_a_registered_translation_runs_on_both_django_based_drivers(pool):
    register_shout()
    assert "function.Shout" in constructs(pool)
    query = Member.objects.filter(first_name__in=["Ana", "Eva"]).annotate(s=Shout("first_name"))
    assert sorted(await rows(query.values_list("first_name", "s"))) == [("Ana", "ANA!"), ("Eva", "EVA!")]


@pytest.mark.usefixtures("club_data", "sql_driver", "router")
async def test_a_translation_translates_the_node_s_arguments():
    register_shout()
    query = Member.objects.filter(first_name="Ana").annotate(
        s=Shout(Concat("first_name", Value(" "), Initials("first_name", "last_name")))
    )
    assert await rows(query.values_list("s", flat=True)) == ["ANA AN!"]


@pytest.mark.usefixtures("club_data")
async def test_a_translation_registered_after_the_drivers_are_imported_is_declared(sql_driver, pool):
    from adorm.drivers.django import register_translation

    late = type(f"LateShout{sql_driver.capitalize()}", (Func,), {"arity": 1, "default_output_type": str})
    assert f"function.{late.__name__}" not in constructs(pool)
    register_translation(late)(shout)
    assert f"function.{late.__name__}" in constructs(pool)
    query = Member.objects.filter(first_name="Jana").annotate(s=late("first_name"))
    assert await rows(query.values_list("s", flat=True)) == ["JANA!"]


@pytest.mark.usefixtures("club_data")
async def test_a_function_without_a_template_or_translation_is_refused(driver_name, pool):
    unknown = type(f"Untranslated{driver_name.capitalize()}", (Func,), {"arity": 1})
    assert f"function.{unknown.__name__}" not in constructs(pool)
    query = Member.objects.annotate(s=unknown("first_name"))
    await assert_refused(rows(query), f"function.{unknown.__name__}", driver_name, pool)


@pytest.mark.usefixtures("club_data")
async def test_the_memory_driver_refuses_a_registered_translation(driver_name, pool):
    if driver_name in SQL_DRIVERS:
        pytest.skip("the driver runs translations registered for the Django-based drivers")
    register_shout()
    assert "function.Shout" not in constructs(pool)
    await assert_refused(rows(Member.objects.annotate(s=Shout("first_name"))), "function.Shout", driver_name, pool)


@pytest.mark.usefixtures("club_data", "sql_driver")
async def test_a_registered_translation_of_a_transform_in_a_lookup_key_and_in_values(pool):
    from adorm.drivers.django import register_translation

    if Loud not in _registered:
        register_translation(Loud)(loud)
        _registered.add(Loud)
    assert "transform.loud" in constructs(pool)
    result = await rows(Member.objects.filter(first_name__loud="ANA").values_list("last_name", flat=True))
    assert result == ["Novak"]
    names = await rows(Member.objects.filter(first_name="Eva").values_list("first_name__loud__initial", flat=True))
    assert names == ["E"]


@pytest.mark.usefixtures("club_data", "sql_driver")
async def test_a_built_in_transform_after_an_own_transform_is_refused(pool):
    with pytest.raises(adorm.NotSupportedError, match="'year' only before a transform with SQL of its own"):
        await Member.objects.filter(birth_date__itself__year=1970).count()
    assert pool.in_use == 0


# UUID4 and UUID7 on every database, Django and Python version


def timestamp_ms(value: uuid.UUID) -> int:
    """The Unix time in milliseconds in the first 48 bits of a version 7 UUID."""
    return value.int >> 80


def now_ms() -> int:
    return time.time_ns() // 1_000_000


async def uuid7_in_database() -> bool:
    """Whether the database generates version 7 UUIDs: SQLite (a Python function on the connection) and PostgreSQL 18
    or later. PostgreSQL before 18 has no such function, and UUID7 is refused there."""
    from tests.library_data import in_thread

    return vendor() == "sqlite" or await in_thread(lambda: connection.pg_version >= 180000)


async def assert_uuid7_refused(awaitable, pool) -> None:
    """``awaitable`` raises `NotSupportedError` naming UUID7, and the pool serves the next operation."""
    with pytest.raises(adorm.NotSupportedError, match="UUID7"):
        await awaitable
    assert pool.in_use == 0
    assert await Member.objects.count() == 12


def assert_version(values: list, version: int) -> None:
    assert len(values) == 12
    assert len(set(values)) == 12
    for value in values:
        assert isinstance(value, uuid.UUID)
        assert value.version == version
        assert value.variant == uuid.RFC_4122


@pytest.mark.usefixtures("club_data", "sql_driver")
async def test_both_uuid_functions_are_declared(pool):
    assert {"function.UUID4", "function.UUID7"} <= constructs(pool)


@pytest.mark.usefixtures("club_data", "sql_driver", "router")
async def test_uuid4_gives_a_version_4_uuid_per_row():
    from adorm.query import UUID4

    assert_version(await rows(Member.objects.annotate(u=UUID4()).values_list("u", flat=True)), 4)


@pytest.mark.usefixtures("club_data", "sql_driver")
async def test_uuid7_gives_a_version_7_uuid_per_row_with_the_current_time(pool):
    from adorm.query import UUID7

    if not await uuid7_in_database():
        await assert_uuid7_refused(rows(Member.objects.annotate(u=UUID7())), pool)
        return
    before = now_ms()
    values = await rows(Member.objects.annotate(u=UUID7()).values_list("u", flat=True))
    after = now_ms()
    assert_version(values, 7)
    # The database runs on this machine; its clock may differ by a little.
    assert all(before - 1000 <= timestamp_ms(value) <= after + 1000 for value in values)


@pytest.mark.usefixtures("club_data", "sql_driver", "router")
async def test_uuid7_values_of_later_milliseconds_sort_later():
    from adorm.query import UUID7

    if not await uuid7_in_database():
        pytest.skip("PostgreSQL before 18 refuses UUID7")
    query = Member.objects.filter(first_name="Ana").annotate(u=UUID7()).values_list("u", flat=True)
    (first,) = await rows(query)
    passed = now_ms()
    async with asyncio.timeout(5):
        while now_ms() <= passed:
            await asyncio.sleep(0.001)
    (second,) = await rows(query)
    assert timestamp_ms(first) < timestamp_ms(second)
    assert first < second


@pytest.mark.usefixtures("club_data", "sql_driver", "router")
async def test_uuid_functions_in_an_update():
    from adorm.query import Cast, UUID4, UUID7

    functions = ((UUID4, 4), (UUID7, 7)) if await uuid7_in_database() else ((UUID4, 4),)
    for function, version in functions:
        changed = await Member.objects.filter(first_name__in=["Ana", "Eva"]).update(last_name=Cast(function(), str))
        assert changed == 2
        stored = await rows(Member.objects.filter(first_name__in=["Ana", "Eva"]).values_list("last_name", flat=True))
        assert {uuid.UUID(value).version for value in stored} == {version}
        assert len(set(stored)) == 2


@pytest.mark.usefixtures("club_data", "sql_driver")
async def test_uuid7_with_a_shift_moves_its_timestamp_or_is_refused_by_name(pool):
    from adorm.query import UUID7

    shift = datetime.timedelta(days=1)
    query = Member.objects.filter(first_name="Ana").annotate(u=UUID7(shift)).values_list("u", flat=True)
    if not await uuid7_in_database():
        await assert_uuid7_refused(rows(query), pool)
        return
    before = now_ms()
    refusal = None
    try:
        values = await rows(query)
    except adorm.NotSupportedError as exc:
        refusal = exc
    if refusal is not None:
        assert "shift" in str(refusal)
        assert pool.in_use == 0
        assert await Member.objects.count() == 12
        return
    after = now_ms()
    (value,) = values
    assert value.version == 7
    assert value.variant == uuid.RFC_4122
    day = 86_400_000
    assert before + day - 1000 <= timestamp_ms(value) <= after + day + 1000


@pytest.mark.usefixtures("club_data")
async def test_the_memory_driver_refuses_the_uuid_functions(driver_name, pool):
    if driver_name in SQL_DRIVERS:
        pytest.skip("the driver runs the UUID functions")
    from adorm.query import UUID7

    await assert_refused(rows(Member.objects.annotate(u=UUID7())), "function.UUID7", driver_name, pool)
