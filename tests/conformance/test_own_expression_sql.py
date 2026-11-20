"""SQL templates and registrations on the SQL drivers beyond the scenarios: parameters of an argument named twice in a
template, a lookup template that names the right-hand side first, a SQLite function registered while a connection is
open, the refusals of the translation, and adorm's own version 7 UUIDs."""

from __future__ import annotations

import datetime
import time
import uuid

import pytest

from django.db import connection

import adorm

from adorm import Value
from adorm.query import Func, Lookup, register_lookup
from tests.clubmodels.clubs import Member

pytestmark = pytest.mark.django_db(transaction=True)


class Doubled(Func):
    arity = 1
    default_output_type = str
    template = "(%(expressions)s || '-' || %(expressions)s)"


class PrefixOf(Lookup):
    """The right-hand side is a prefix of the left-hand side; the template names the right-hand side first."""

    lookup_name = "prefix_of_value"
    template = "%(rhs)s = SUBSTR(%(lhs)s, 1, LENGTH(%(rhs)s))"


@register_lookup
class SameFirstLetter(Lookup):
    lookup_name = "same_first_letter"
    template = "SUBSTR(%(lhs)s, 1, 1) = SUBSTR(%(rhs)s, 1, 1)"


@register_lookup
class Equals(Lookup):
    """Equality written as a template: the right-hand side is prepared by the left-hand side's field."""

    lookup_name = "equals_by_template"
    template = "%(lhs)s = %(rhs)s"


@pytest.fixture
def sql_driver(conformance_driver):
    if conformance_driver.name == "memory":
        pytest.skip("the memory driver has no SQL")
    return conformance_driver.name


async def rows(queryset) -> list:
    return [row async for row in queryset]


@pytest.mark.usefixtures("club_data", "sql_driver", "router")
async def test_an_argument_named_twice_in_a_template_binds_its_parameters_twice():
    query = Member.objects.filter(first_name="Ana").annotate(d=Doubled(Value("x")))
    assert await rows(query.values_list("d", flat=True)) == ["x-x"]


@pytest.mark.usefixtures("club_data", "sql_driver", "router")
async def test_a_lookup_template_binds_parameters_in_the_order_it_names_them():
    result = await rows(Member.objects.filter(PrefixOf("first_name", "Kle")).values_list("first_name", flat=True))
    assert result == ["Klemen"]


@pytest.mark.usefixtures("club_data", "sql_driver", "router")
async def test_get_or_create_refuses_a_lookup_with_a_template():
    with pytest.raises(adorm.NotSupportedError, match="get_or_create"):
        await Member.objects.get_or_create(first_name__same_first_letter="Gregor", defaults={"last_name": "X"})


@pytest.mark.usefixtures("club_data", "sql_driver", "router")
async def test_a_lookup_template_compares_a_json_field_with_a_list():
    result = await rows(Member.objects.filter(tags__equals_by_template=["junior"]).values_list("first_name", flat=True))
    assert sorted(result) == ["Ana", "Filip", "Jana"]


@pytest.mark.usefixtures("club_data", "sql_driver", "router")
async def test_a_lookup_template_converts_a_string_for_an_integer_field():
    ana = await Member.objects.get(first_name="Ana")
    result = await rows(Member.objects.filter(id__equals_by_template=str(ana.id)).values_list("first_name", flat=True))
    assert result == ["Ana"]


@pytest.mark.usefixtures("club_data", "sql_driver", "router")
async def test_a_lookup_template_compares_a_date_field_with_a_date():
    day = datetime.date(1970, 1, 10)
    result = await rows(Member.objects.filter(birth_date__equals_by_template=day).values_list("first_name", flat=True))
    assert result == ["Ana"]


@pytest.mark.usefixtures("club_data", "router")
async def test_a_sqlite_function_registered_while_a_connection_is_open_is_there_for_its_next_call(sql_driver):
    from adorm.drivers.django import register_sqlite_function

    if connection.vendor != "sqlite":
        pytest.skip("SQLite only")
    name = f"late_{sql_driver}_{time.monotonic_ns()}"
    late = type("LateSqliteFunction", (Func,), {"arity": 1, "default_output_type": str, "function": name})
    assert await Member.objects.count() == 12  # the slot's connection is open
    register_sqlite_function(name, 1, lambda text: f"<{text}>")
    query = Member.objects.filter(first_name="Ana").annotate(v=late("first_name"))
    assert await rows(query.values_list("v", flat=True)) == ["<Ana>"]


def test_register_translation_refuses_a_class_without_a_construct_name_and_a_string_of_vendors():
    from adorm.drivers.django import register_translation
    from adorm.query import Aggregate

    with pytest.raises(TypeError):
        register_translation(Aggregate)
    with pytest.raises(TypeError):
        register_translation(Doubled, vendors="postgresql")


def test_a_template_that_is_not_a_string_is_refused_when_the_class_is_defined():
    with pytest.raises(TypeError, match="templates"):
        type("BadTemplates", (Func,), {"templates": {"postgresql": 1}})
    with pytest.raises(TypeError, match="template"):
        type("BadTemplate", (Lookup,), {"lookup_name": "bad_template", "template": ["x"]})


@pytest.mark.parametrize("own", [True, False], ids=["own", "stdlib-or-own"])
def test_uuid7_is_a_version_7_uuid_with_the_current_time(own):
    from adorm.uuids import _uuid7

    generate = _uuid7 if own else adorm.uuid7
    before = time.time_ns() // 1_000_000
    values = [generate() for _ in range(100)]
    after = time.time_ns() // 1_000_000
    assert len(set(values)) == 100
    for value in values:
        assert isinstance(value, uuid.UUID)
        assert value.version == 7
        assert value.variant == uuid.RFC_4122
        assert before <= value.int >> 80 <= after
