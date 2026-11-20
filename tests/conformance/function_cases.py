"""Conformance cases of the database functions: every function of ``django.db.models.functions`` that is not a window
function, in annotations, conditions, orderings, ``values()``, updates and aggregates' arguments.

Importing the module registers the cases in `tests.conformance.cases.CASES`. A case built by `function_case()`
annotates three rows of one model with the function, orders the rows by it and, when it has a ``match``, filters the
rows whose value equals ``match``; its Django mirror runs the same query with Django's function of the same name.
Expressions are built inside the case, so a function adorm lacks fails only its own cases.
"""

from __future__ import annotations

import datetime
import hashlib
import importlib
import uuid

from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

import pytest

import adorm

from adorm import F, Value
from adorm.query import Concat, Count, Exact, Length, Lower, Max, Min, Sum, Upper
from tests.clubmodels.activities import Activity, Competition
from tests.clubmodels.billing import Invoice
from tests.clubmodels.clubs import Club, Member
from tests.conformance.cases import case, rows
from tests.library_data import in_thread

UTC = datetime.UTC

NO_MATCH = object()

# model key: (adorm model, Django model path, label field, labels of the rows a case reads)
MODELS: dict[str, tuple[Any, str, str, tuple[str, ...]]] = {
    "member": (Member, "tests.djangoproject.clubs.models:Member", "first_name", ("Ana", "Eva", "Lara")),
    "activity": (
        Activity,
        "tests.djangoproject.activities.models:Activity",
        "name",
        ("Morning sculling", "Spring regatta", "Crag day"),
    ),
    "invoice": (
        Invoice,
        "tests.djangoproject.billing.models:Invoice",
        "number",
        ("2026-0001", "2026-0004", "2026-0005"),
    ),
    "competition": (
        Competition,
        "tests.djangoproject.activities.models:Competition",
        "name",
        ("Spring regatta", "Regional bouldering cup"),
    ),
}


def django_model(model: str) -> Any:
    module, _, name = MODELS[model][1].partition(":")
    return getattr(importlib.import_module(module), name)


def vendor() -> str:
    from django.db import connection

    return connection.vendor


def install_pgcrypto() -> None:
    """Install pgcrypto, whose ``DIGEST()`` the SHA functions use on PostgreSQL."""
    from django.db import connection

    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")


def binary_collation() -> str:
    """The name of a collation that compares strings by their bytes on the test database."""
    return "binary" if vendor() == "sqlite" else "C"


class Side:
    """The function classes and helpers of one ORM, for building the same expression in adorm and in Django."""

    def __init__(self, module: str, value: Any, f: Any, cast_type: Callable[[Any], Any]) -> None:
        self._module = module
        self.Value = value
        self.F = f
        self.type = cast_type

    def __getattr__(self, name: str) -> Any:
        return getattr(importlib.import_module(self._module), name)


def _django_type(python_type: type) -> Any:
    from django.db import models

    fields = {
        int: models.IntegerField,
        str: models.TextField,
        float: models.FloatField,
        datetime.date: models.DateField,
        datetime.datetime: models.DateTimeField,
    }
    return fields[python_type]()


def _django_value(value: Any) -> Any:
    from django.db.models import Value as DjangoValue

    return DjangoValue(value)


def _django_f(name: str) -> Any:
    from django.db.models import F as DjangoF

    return DjangoF(name)


ADORM = Side("adorm.query", Value, F, lambda python_type: python_type)
DJANGO = Side("django.db.models.functions", _django_value, _django_f, _django_type)


def function_case(
    name: str,
    case_id: str,
    model: str,
    build: Callable[[Side], Any],
    *,
    match: Any = NO_MATCH,
    uses: tuple[str, ...] = (),
    expected: Any,
    on_vendor: Mapping[str, Any] | None = None,
    prepare: Callable[[], None] | None = None,
) -> None:
    """Register the case ``case_id``: ``build(side)`` annotated as ``value`` on the rows of ``model``, the rows ordered
    by it, and the rows whose value equals ``match``; ``expected`` is ``(annotated, ordered[, matched])``.
    ``prepare()`` runs with the Django ORM before the queries."""
    adorm_model, _, label, labels = MODELS[model]
    names = {"select", "clause.annotate", "lookup.in", f"function.{name}", *uses}
    if match is not NO_MATCH:
        names.add("lookup.exact")

    async def run():
        if prepare is not None:
            await in_thread(prepare)
        expression = build(ADORM)
        base = adorm_model.objects.filter(**{f"{label}__in": labels})
        annotated = await rows(base.annotate(value=expression).order_by("id").values_list(label, "value"))
        ordered = await rows(base.order_by(expression, "id").values_list(label, flat=True))
        if match is NO_MATCH:
            return annotated, ordered
        matched = await rows(base.filter(Exact(expression, match)).order_by("id").values_list(label, flat=True))
        return annotated, ordered, matched

    def mirror():
        from django.db.models.lookups import Exact as DjangoExact

        if prepare is not None:
            prepare()
        expression = build(DJANGO)
        base = django_model(model).objects.filter(**{f"{label}__in": labels})
        annotated = list(base.annotate(value=expression).order_by("id").values_list(label, "value"))
        ordered = list(base.order_by(expression, "id").values_list(label, flat=True))
        if match is NO_MATCH:
            return annotated, ordered
        matched = list(base.filter(DjangoExact(expression, match)).order_by("id").values_list(label, flat=True))
        return annotated, ordered, matched

    run.__name__ = f"scenario_{case_id}"
    case(*sorted(names), expected=expected, django=mirror, on_vendor=on_vendor)(run)


# Comparison and conversion functions

function_case(
    "Cast",
    "function_cast_to_str",
    "activity",
    lambda fn: fn.Cast("capacity", fn.type(str)),
    match="30",
    expected=(
        [("Morning sculling", "8"), ("Spring regatta", "30"), ("Crag day", "15")],
        ["Crag day", "Spring regatta", "Morning sculling"],
        ["Spring regatta"],
    ),
)
function_case(
    "Cast",
    "function_cast_to_int",
    "activity",
    lambda fn: fn.Cast("price", fn.type(int)),
    match=15,
    expected=(
        [("Morning sculling", 0), ("Spring regatta", 25), ("Crag day", 15)],
        ["Morning sculling", "Crag day", "Spring regatta"],
        ["Crag day"],
    ),
)
function_case(
    "Cast",
    "function_cast_to_float",
    "invoice",
    lambda fn: fn.Cast("total", fn.type(float)),
    match=95.5,
    expected=(
        [("2026-0001", 120.0), ("2026-0004", 60.0), ("2026-0005", 95.5)],
        ["2026-0004", "2026-0005", "2026-0001"],
        ["2026-0005"],
    ),
)
function_case(
    "Cast",
    "function_cast_to_date",
    "member",
    lambda fn: fn.Cast("joined", fn.type(datetime.date)),
    match=datetime.date(2026, 3, 2),
    expected=(
        [
            ("Ana", datetime.date(2026, 3, 2)),
            ("Eva", datetime.date(2025, 9, 23)),
            ("Lara", datetime.date(2024, 12, 17)),
        ],
        ["Lara", "Eva", "Ana"],
        ["Ana"],
    ),
)
function_case(
    "Collate",
    "function_collate",
    "member",
    lambda fn: fn.Collate("last_name", binary_collation()),
    match="Novak",
    expected=([("Ana", "Novak"), ("Eva", "Zupančič"), ("Lara", "Božič")], ["Lara", "Ana", "Eva"], ["Ana"]),
)
function_case(
    "Greatest",
    "function_greatest",
    "member",
    lambda fn: fn.Greatest("birth_date", fn.Value(datetime.date(1980, 1, 1))),
    match=datetime.date(1980, 1, 1),
    expected=(
        [
            ("Ana", datetime.date(1980, 1, 1)),
            ("Eva", datetime.date(1982, 5, 10)),
            ("Lara", datetime.date(2003, 12, 10)),
        ],
        ["Ana", "Eva", "Lara"],
        ["Ana"],
    ),
)
function_case(
    "Least",
    "function_least",
    "member",
    lambda fn: fn.Least("birth_date", fn.Value(datetime.date(1980, 1, 1))),
    match=datetime.date(1980, 1, 1),
    expected=(
        [("Ana", datetime.date(1970, 1, 10)), ("Eva", datetime.date(1980, 1, 1)), ("Lara", datetime.date(1980, 1, 1))],
        ["Ana", "Eva", "Lara"],
        ["Eva", "Lara"],
    ),
)
function_case(
    "NullIf",
    "function_nullif",
    "activity",
    lambda fn: fn.NullIf("price", fn.Value(Decimal("0.00"))),
    match=Decimal("25.00"),
    expected=(
        [("Morning sculling", None), ("Spring regatta", Decimal("25.00")), ("Crag day", Decimal("15.00"))],
        ["Crag day", "Spring regatta", "Morning sculling"],
        ["Spring regatta"],
    ),
    # SQLite orders NULL first.
    on_vendor={
        "sqlite": (
            [("Morning sculling", None), ("Spring regatta", Decimal("25.00")), ("Crag day", Decimal("15.00"))],
            ["Morning sculling", "Crag day", "Spring regatta"],
            ["Spring regatta"],
        )
    },
)
function_case(
    "JSONArray",
    "function_jsonarray",
    "member",
    lambda fn: fn.JSONArray("first_name", "level"),
    expected=(
        [("Ana", ["Ana", "beginner"]), ("Eva", ["Eva", "advanced"]), ("Lara", ["Lara", "advanced"])],
        ["Ana", "Eva", "Lara"],
    ),
)
function_case(
    "JSONObject",
    "function_jsonobject",
    "member",
    lambda fn: fn.JSONObject(name="first_name", level="level"),
    expected=(
        [
            ("Ana", {"name": "Ana", "level": "beginner"}),
            ("Eva", {"name": "Eva", "level": "advanced"}),
            ("Lara", {"name": "Lara", "level": "advanced"}),
        ],
        ["Ana", "Eva", "Lara"],
    ),
)

# Date functions

function_case(
    "Extract",
    "function_extract",
    "member",
    lambda fn: fn.Extract("birth_date", "year"),
    match=1982,
    expected=([("Ana", 1970), ("Eva", 1982), ("Lara", 2003)], ["Ana", "Eva", "Lara"], ["Eva"]),
)
function_case(
    "Extract",
    "function_extract_hour_of_a_datetime",
    "activity",
    lambda fn: fn.Extract("starts_at", "hour"),
    match=7,
    expected=(
        [("Morning sculling", 7), ("Spring regatta", 7), ("Crag day", 7)],
        ["Morning sculling", "Spring regatta", "Crag day"],
        ["Morning sculling", "Spring regatta", "Crag day"],
    ),
)
for _name, _field, _match, _expected in [
    (
        "ExtractYear",
        "birth_date",
        2003,
        ([("Ana", 1970), ("Eva", 1982), ("Lara", 2003)], ["Ana", "Eva", "Lara"], ["Lara"]),
    ),
    (
        "ExtractIsoYear",
        "birth_date",
        1982,
        ([("Ana", 1970), ("Eva", 1982), ("Lara", 2003)], ["Ana", "Eva", "Lara"], ["Eva"]),
    ),
    ("ExtractQuarter", "birth_date", 2, ([("Ana", 1), ("Eva", 2), ("Lara", 4)], ["Ana", "Eva", "Lara"], ["Eva"])),
    ("ExtractMonth", "birth_date", 12, ([("Ana", 1), ("Eva", 5), ("Lara", 12)], ["Ana", "Eva", "Lara"], ["Lara"])),
    ("ExtractWeek", "birth_date", 19, ([("Ana", 2), ("Eva", 19), ("Lara", 50)], ["Ana", "Eva", "Lara"], ["Eva"])),
    (
        "ExtractDay",
        "birth_date",
        10,
        ([("Ana", 10), ("Eva", 10), ("Lara", 10)], ["Ana", "Eva", "Lara"], ["Ana", "Eva", "Lara"]),
    ),
    ("ExtractWeekDay", "birth_date", 2, ([("Ana", 7), ("Eva", 2), ("Lara", 4)], ["Eva", "Lara", "Ana"], ["Eva"])),
    ("ExtractIsoWeekDay", "birth_date", 1, ([("Ana", 6), ("Eva", 1), ("Lara", 3)], ["Eva", "Lara", "Ana"], ["Eva"])),
    ("ExtractYear", "joined", 2025, ([("Ana", 2026), ("Eva", 2025), ("Lara", 2024)], ["Lara", "Eva", "Ana"], ["Eva"])),
    ("ExtractMonth", "joined", 9, ([("Ana", 3), ("Eva", 9), ("Lara", 12)], ["Ana", "Eva", "Lara"], ["Eva"])),
    (
        "ExtractHour",
        "joined",
        7,
        ([("Ana", 7), ("Eva", 7), ("Lara", 7)], ["Ana", "Eva", "Lara"], ["Ana", "Eva", "Lara"]),
    ),
    (
        "ExtractMinute",
        "joined",
        0,
        ([("Ana", 0), ("Eva", 0), ("Lara", 0)], ["Ana", "Eva", "Lara"], ["Ana", "Eva", "Lara"]),
    ),
    (
        "ExtractSecond",
        "joined",
        0,
        ([("Ana", 0), ("Eva", 0), ("Lara", 0)], ["Ana", "Eva", "Lara"], ["Ana", "Eva", "Lara"]),
    ),
]:
    function_case(
        _name,
        f"function_{_name.lower()}_of_{_field}",
        "member",
        lambda fn, name=_name, field=_field: getattr(fn, name)(field),
        match=_match,
        expected=_expected,
    )

# Math functions

function_case(
    "Abs",
    "function_abs",
    "activity",
    lambda fn: fn.Abs(fn.F("price") - 20),
    match=Decimal("5.00"),
    uses=("expression.CombinedExpression",),
    expected=(
        [("Morning sculling", Decimal("20.00")), ("Spring regatta", Decimal("5.00")), ("Crag day", Decimal("5.00"))],
        ["Spring regatta", "Crag day", "Morning sculling"],
        ["Spring regatta", "Crag day"],
    ),
)
function_case(
    "ACos",
    "function_acos",
    "activity",
    lambda fn: fn.ACos(fn.Cos("capacity")),
    uses=("function.Cos",),
    expected=(
        [
            ("Morning sculling", 1.7168146928204135),
            ("Spring regatta", 1.4159265358979325),
            ("Crag day", 2.433629385640827),
        ],
        ["Spring regatta", "Morning sculling", "Crag day"],
    ),
)
function_case(
    "ASin",
    "function_asin",
    "activity",
    lambda fn: fn.ASin(fn.Sin("capacity")),
    uses=("function.Sin",),
    expected=(
        [
            ("Morning sculling", 1.4247779607693798),
            ("Spring regatta", -1.4159265358979327),
            ("Crag day", 0.7079632679489661),
        ],
        ["Spring regatta", "Crag day", "Morning sculling"],
    ),
)
function_case(
    "ATan",
    "function_atan",
    "activity",
    lambda fn: fn.ATan("capacity"),
    expected=(
        [
            ("Morning sculling", 1.446441332248135),
            ("Spring regatta", 1.5374753309166493),
            ("Crag day", 1.5042281630190728),
        ],
        ["Morning sculling", "Crag day", "Spring regatta"],
    ),
)
function_case(
    "ATan2",
    "function_atan2",
    "activity",
    lambda fn: fn.ATan2("capacity", 10),
    expected=(
        [
            ("Morning sculling", 0.6747409422235526),
            ("Spring regatta", 1.2490457723982544),
            ("Crag day", 0.982793723247329),
        ],
        ["Morning sculling", "Crag day", "Spring regatta"],
    ),
)
function_case(
    "Ceil",
    "function_ceil",
    "invoice",
    lambda fn: fn.Ceil("total"),
    match=Decimal(96),
    expected=(
        [("2026-0001", Decimal("120")), ("2026-0004", Decimal("60")), ("2026-0005", Decimal("96"))],
        ["2026-0004", "2026-0005", "2026-0001"],
        ["2026-0005"],
    ),
)
function_case(
    "Cos",
    "function_cos",
    "activity",
    lambda fn: fn.Cos("capacity"),
    expected=(
        [
            ("Morning sculling", -0.14550003380861354),
            ("Spring regatta", 0.15425144988758405),
            ("Crag day", -0.7596879128588213),
        ],
        ["Crag day", "Morning sculling", "Spring regatta"],
    ),
)
function_case(
    "Cot",
    "function_cot",
    "activity",
    lambda fn: fn.Cot("capacity"),
    expected=(
        [
            ("Morning sculling", -0.1470650639494805),
            ("Spring regatta", -0.15611995216165922),
            ("Crag day", -1.1682333052318372),
        ],
        ["Crag day", "Spring regatta", "Morning sculling"],
    ),
)
function_case(
    "Degrees",
    "function_degrees",
    "activity",
    lambda fn: fn.Degrees("capacity"),
    expected=(
        [
            ("Morning sculling", 458.3662361046586),
            ("Spring regatta", 1718.8733853924696),
            ("Crag day", 859.4366926962348),
        ],
        ["Morning sculling", "Crag day", "Spring regatta"],
    ),
)
function_case(
    "Exp",
    "function_exp",
    "activity",
    lambda fn: fn.Exp("capacity"),
    expected=(
        [
            ("Morning sculling", 2980.9579870417283),
            ("Spring regatta", 10686474581524.463),
            ("Crag day", 3269017.3724721107),
        ],
        ["Morning sculling", "Crag day", "Spring regatta"],
    ),
)
function_case(
    "Floor",
    "function_floor",
    "invoice",
    lambda fn: fn.Floor("total"),
    match=Decimal(95),
    expected=(
        [("2026-0001", Decimal("120")), ("2026-0004", Decimal("60")), ("2026-0005", Decimal("95"))],
        ["2026-0004", "2026-0005", "2026-0001"],
        ["2026-0005"],
    ),
)
function_case(
    "Ln",
    "function_ln",
    "activity",
    lambda fn: fn.Ln("capacity"),
    expected=(
        [
            ("Morning sculling", 2.0794415416798357),
            ("Spring regatta", 3.4011973816621555),
            ("Crag day", 2.70805020110221),
        ],
        ["Morning sculling", "Crag day", "Spring regatta"],
    ),
)
function_case(
    "Log",
    "function_log",
    "activity",
    lambda fn: fn.Log(2, "capacity"),
    match=3.0,
    expected=(
        [("Morning sculling", 3.0), ("Spring regatta", 4.906890595608519), ("Crag day", 3.9068905956085187)],
        ["Morning sculling", "Crag day", "Spring regatta"],
        ["Morning sculling"],
    ),
)
function_case(
    "Mod",
    "function_mod",
    "activity",
    lambda fn: fn.Mod("capacity", 7),
    match=1,
    expected=(
        [("Morning sculling", 1.0), ("Spring regatta", 2.0), ("Crag day", 1.0)],
        ["Morning sculling", "Crag day", "Spring regatta"],
        ["Morning sculling", "Crag day"],
    ),
)
function_case(
    "Pi",
    "function_pi",
    "activity",
    lambda fn: fn.Pi(),
    expected=(
        [
            ("Morning sculling", 3.141592653589793),
            ("Spring regatta", 3.141592653589793),
            ("Crag day", 3.141592653589793),
        ],
        ["Morning sculling", "Spring regatta", "Crag day"],
    ),
)
function_case(
    "Power",
    "function_power",
    "activity",
    lambda fn: fn.Power("capacity", 2),
    match=225,
    expected=(
        [("Morning sculling", 64.0), ("Spring regatta", 900.0), ("Crag day", 225.0)],
        ["Morning sculling", "Crag day", "Spring regatta"],
        ["Crag day"],
    ),
)
function_case(
    "Radians",
    "function_radians",
    "activity",
    lambda fn: fn.Radians("capacity"),
    expected=(
        [
            ("Morning sculling", 0.13962634015954636),
            ("Spring regatta", 0.5235987755982988),
            ("Crag day", 0.2617993877991494),
        ],
        ["Morning sculling", "Crag day", "Spring regatta"],
    ),
)
function_case(
    "Round",
    "function_round",
    "invoice",
    lambda fn: fn.Round("total"),
    match=Decimal(96),
    expected=(
        [("2026-0001", Decimal("120")), ("2026-0004", Decimal("60")), ("2026-0005", Decimal("96"))],
        ["2026-0004", "2026-0005", "2026-0001"],
        ["2026-0005"],
    ),
)
function_case(
    "Round",
    "function_round_with_precision",
    "invoice",
    lambda fn: fn.Round(fn.F("total") / 7, 2),
    match=Decimal("13.64"),
    uses=("expression.CombinedExpression",),
    expected=(
        [("2026-0001", Decimal("17.14")), ("2026-0004", Decimal("8.57")), ("2026-0005", Decimal("13.64"))],
        ["2026-0004", "2026-0005", "2026-0001"],
        ["2026-0005"],
    ),
    # Django divides whole decimals as integers on SQLite.
    on_vendor={
        "sqlite": (
            [("2026-0001", Decimal("17")), ("2026-0004", Decimal("8")), ("2026-0005", Decimal("13.64"))],
            ["2026-0004", "2026-0005", "2026-0001"],
            ["2026-0005"],
        )
    },
)
function_case(
    "Sign",
    "function_sign",
    "activity",
    lambda fn: fn.Sign(fn.F("price") - 20),
    match=-1,
    uses=("expression.CombinedExpression",),
    expected=(
        [("Morning sculling", Decimal("-1")), ("Spring regatta", Decimal("1")), ("Crag day", Decimal("-1"))],
        ["Morning sculling", "Crag day", "Spring regatta"],
        ["Morning sculling", "Crag day"],
    ),
)
function_case(
    "Sin",
    "function_sin",
    "activity",
    lambda fn: fn.Sin("capacity"),
    expected=(
        [
            ("Morning sculling", 0.9893582466233818),
            ("Spring regatta", -0.9880316240928618),
            ("Crag day", 0.6502878401571168),
        ],
        ["Spring regatta", "Crag day", "Morning sculling"],
    ),
)
function_case(
    "Sqrt",
    "function_sqrt",
    "activity",
    lambda fn: fn.Sqrt("capacity"),
    expected=(
        [
            ("Morning sculling", 2.8284271247461903),
            ("Spring regatta", 5.477225575051661),
            ("Crag day", 3.872983346207417),
        ],
        ["Morning sculling", "Crag day", "Spring regatta"],
    ),
)
function_case(
    "Tan",
    "function_tan",
    "activity",
    lambda fn: fn.Tan("capacity"),
    expected=(
        [
            ("Morning sculling", -6.799711455220379),
            ("Spring regatta", -6.405331196646276),
            ("Crag day", -0.8559934009085187),
        ],
        ["Morning sculling", "Spring regatta", "Crag day"],
    ),
)

# Text functions

function_case(
    "Chr",
    "function_chr",
    "activity",
    lambda fn: fn.Chr(fn.F("capacity") + 60),
    match="Z",
    uses=("expression.CombinedExpression",),
    expected=(
        [("Morning sculling", "D"), ("Spring regatta", "Z"), ("Crag day", "K")],
        ["Morning sculling", "Crag day", "Spring regatta"],
        ["Spring regatta"],
    ),
)
function_case(
    "Left",
    "function_left",
    "member",
    lambda fn: fn.Left("last_name", 3),
    match="Bož",
    expected=([("Ana", "Nov"), ("Eva", "Zup"), ("Lara", "Bož")], ["Lara", "Ana", "Eva"], ["Lara"]),
)
function_case(
    "LPad",
    "function_lpad",
    "member",
    lambda fn: fn.LPad("first_name", 5, fn.Value(".")),
    match="..Eva",
    expected=([("Ana", "..Ana"), ("Eva", "..Eva"), ("Lara", ".Lara")], ["Ana", "Eva", "Lara"], ["Eva"]),
)
function_case(
    "LPad",
    "function_lpad_to_a_shorter_length_with_the_default_fill",
    "member",
    lambda fn: fn.LPad("last_name", 4),
    match="Zupa",
    expected=([("Ana", "Nova"), ("Eva", "Zupa"), ("Lara", "Boži")], ["Lara", "Ana", "Eva"], ["Eva"]),
)
function_case(
    "LTrim",
    "function_ltrim",
    "member",
    lambda fn: fn.LTrim(fn.Concat(fn.Value("  "), "first_name", fn.Value(" "))),
    match="Ana ",
    uses=("function.Concat",),
    expected=([("Ana", "Ana "), ("Eva", "Eva "), ("Lara", "Lara ")], ["Ana", "Eva", "Lara"], ["Ana"]),
)
function_case(
    "RTrim",
    "function_rtrim",
    "member",
    lambda fn: fn.RTrim(fn.Concat(fn.Value(" "), "first_name", fn.Value("  "))),
    match=" Ana",
    uses=("function.Concat",),
    expected=([("Ana", " Ana"), ("Eva", " Eva"), ("Lara", " Lara")], ["Ana", "Eva", "Lara"], ["Ana"]),
)
function_case(
    "Trim",
    "function_trim",
    "member",
    lambda fn: fn.Trim(fn.Concat(fn.Value("  "), "first_name", fn.Value("  "))),
    match="Ana",
    uses=("function.Concat",),
    expected=([("Ana", "Ana"), ("Eva", "Eva"), ("Lara", "Lara")], ["Ana", "Eva", "Lara"], ["Ana"]),
)
EMAILS = {"Ana": "ana.0@example.com", "Eva": "eva.4@example.com", "Lara": "lara.11@example.com"}


def _digests(algorithm: str) -> tuple[list[tuple[str, str]], list[str]]:
    """The hex digests of the members' e-mail addresses, and the members in the order of their digests."""
    digests = [(name, hashlib.new(algorithm, email.encode()).hexdigest()) for name, email in EMAILS.items()]
    return digests, [name for name, _ in sorted(digests, key=lambda item: item[1])]


for _name in ["MD5", "SHA1", "SHA224", "SHA256", "SHA384", "SHA512"]:
    function_case(
        _name,
        f"function_{_name.lower()}",
        "member",
        lambda fn, name=_name: getattr(fn, name)("email"),
        expected=_digests(_name.lower()),
        prepare=install_pgcrypto,
    )
function_case(
    "Ord",
    "function_ord",
    "member",
    lambda fn: fn.Ord("last_name"),
    match=90,
    expected=([("Ana", 78), ("Eva", 90), ("Lara", 66)], ["Lara", "Ana", "Eva"], ["Eva"]),
)
function_case(
    "Repeat",
    "function_repeat",
    "member",
    lambda fn: fn.Repeat("first_name", 2),
    match="EvaEva",
    expected=([("Ana", "AnaAna"), ("Eva", "EvaEva"), ("Lara", "LaraLara")], ["Ana", "Eva", "Lara"], ["Eva"]),
)
function_case(
    "Replace",
    "function_replace",
    "member",
    lambda fn: fn.Replace("last_name", fn.Value("č"), fn.Value("c")),
    match="Zupancic",
    expected=([("Ana", "Novak"), ("Eva", "Zupancic"), ("Lara", "Božic")], ["Lara", "Ana", "Eva"], ["Eva"]),
)
function_case(
    "Replace",
    "function_replace_with_the_default_replacement",
    "member",
    lambda fn: fn.Replace("last_name", fn.Value("a")),
    match="Novk",
    expected=([("Ana", "Novk"), ("Eva", "Zupnčič"), ("Lara", "Božič")], ["Lara", "Ana", "Eva"], ["Ana"]),
)
function_case(
    "Reverse",
    "function_reverse",
    "member",
    lambda fn: fn.Reverse("first_name"),
    match="araL",
    expected=([("Ana", "anA"), ("Eva", "avE"), ("Lara", "araL")], ["Ana", "Lara", "Eva"], ["Lara"]),
)
function_case(
    "Right",
    "function_right",
    "member",
    lambda fn: fn.Right("last_name", 2),
    match="ak",
    expected=([("Ana", "ak"), ("Eva", "ič"), ("Lara", "ič")], ["Ana", "Eva", "Lara"], ["Ana"]),
)
function_case(
    "RPad",
    "function_rpad",
    "member",
    lambda fn: fn.RPad("first_name", 6, fn.Value("*")),
    match="Lara**",
    expected=([("Ana", "Ana***"), ("Eva", "Eva***"), ("Lara", "Lara**")], ["Ana", "Eva", "Lara"], ["Lara"]),
)
function_case(
    "StrIndex",
    "function_strindex",
    "member",
    lambda fn: fn.StrIndex("last_name", fn.Value("o")),
    match=0,
    expected=([("Ana", 2), ("Eva", 0), ("Lara", 2)], ["Eva", "Ana", "Lara"], ["Eva"]),
)
function_case(
    "Substr",
    "function_substr",
    "member",
    lambda fn: fn.Substr("last_name", 2, 3),
    match="ova",
    expected=([("Ana", "ova"), ("Eva", "upa"), ("Lara", "oži")], ["Ana", "Lara", "Eva"], ["Ana"]),
)
function_case(
    "Substr",
    "function_substr_to_the_end",
    "member",
    lambda fn: fn.Substr("last_name", 3),
    match="pančič",
    expected=([("Ana", "vak"), ("Eva", "pančič"), ("Lara", "žič")], ["Eva", "Ana", "Lara"], ["Eva"]),
)


# Functions whose values differ between runs


def _django_random():
    from django.db.models.functions import Random as DjangoRandom

    from tests.djangoproject.clubs.models import Member as DjangoMember

    values = list(DjangoMember.objects.annotate(r=DjangoRandom()).values_list("r", flat=True))
    shuffled = list(DjangoMember.objects.order_by(DjangoRandom()).values_list("first_name", flat=True))
    return _random_outcome(values, shuffled)


def _random_outcome(values: list[Any], shuffled: list[str]) -> tuple[int, bool, list[str]]:
    in_range = all(isinstance(value, float) and 0 <= value < 1 for value in values)
    return len(values), in_range and len(set(values)) > 1, sorted(shuffled)


MEMBER_NAMES = sorted(
    ["Ana", "Bojan", "Cvetka", "David", "Eva", "Filip", "Gaja", "Hana", "Igor", "Jana", "Klemen", "Lara"]
)


@case("select", "clause.annotate", "function.Random", expected=(12, True, MEMBER_NAMES), django=_django_random)
async def scenario_function_random():
    from adorm.query import Random

    values = await rows(Member.objects.annotate(r=Random()).values_list("r", flat=True))
    shuffled = await rows(Member.objects.order_by(Random()).values_list("first_name", flat=True))
    return _random_outcome(values, shuffled)


def _django_function(name: str) -> Any:
    """Django's function ``name``; the mirror is skipped when the installed Django has none."""
    from django.db.models import functions

    if not hasattr(functions, name):
        pytest.skip(f"the installed Django has no {name}")
    return getattr(functions, name)


def _uuid_outcome(values: list[Any], version: int) -> tuple[int, bool, int]:
    ok = all(isinstance(value, uuid.UUID) and value.version == version for value in values)
    return len(values), ok, len(set(values))


def _django_uuid4():
    django_uuid4 = _django_function("UUID4")

    from tests.djangoproject.clubs.models import Member as DjangoMember

    return _uuid_outcome(list(DjangoMember.objects.annotate(u=django_uuid4()).values_list("u", flat=True)), 4)


@case("select", "clause.annotate", "function.UUID4", expected=(12, True, 12), django=_django_uuid4)
async def scenario_function_uuid4():
    from adorm.query import UUID4

    return _uuid_outcome(await rows(Member.objects.annotate(u=UUID4()).values_list("u", flat=True)), 4)


def _uuid7_supported() -> bool:
    from django.db import connection

    return bool(getattr(connection.features, "supports_uuid7_function", False))


def _django_uuid7():
    django_uuid7 = _django_function("UUID7")

    from tests.djangoproject.clubs.models import Member as DjangoMember

    if not _uuid7_supported():
        return 12, True, 12
    return _uuid_outcome(list(DjangoMember.objects.annotate(u=django_uuid7()).values_list("u", flat=True)), 7)


@case("select", "clause.annotate", "function.UUID7", expected=(12, True, 12), django=_django_uuid7)
async def scenario_function_uuid7():
    """Version 7 UUIDs where the database has the function; elsewhere the database's refusal is a
    `NotSupportedError` naming UUID7 (a capability refusal propagates)."""
    from adorm.query import UUID7

    supported = await in_thread(_uuid7_supported)
    try:
        values = await rows(Member.objects.annotate(u=UUID7()).values_list("u", flat=True))
    except adorm.NotSupportedError as exc:
        if exc.unsupported or supported:
            raise
        return 12, "UUID7" in str(exc), 12
    return _uuid_outcome(values, 7)


# Functions in the places a query uses expressions


def _django_annotation_in_a_keyword_lookup():
    from django.db.models.functions import ExtractYear as DjangoExtractYear

    from tests.djangoproject.clubs.models import Member as DjangoMember

    query = DjangoMember.objects.annotate(y=DjangoExtractYear("birth_date")).filter(y__gte=2000)
    return sorted(query.values_list("first_name", "y"))


@case(
    "select",
    "clause.annotate",
    "lookup.gte",
    "function.ExtractYear",
    expected=[("Klemen", 2000), ("Lara", 2003)],
    django=_django_annotation_in_a_keyword_lookup,
)
async def scenario_function_annotation_in_a_keyword_lookup():
    from adorm.query import ExtractYear

    query = Member.objects.annotate(y=ExtractYear("birth_date")).filter(y__gte=2000)
    return sorted([(member.first_name, member.y) for member in await rows(query)])


def _django_function_annotation_on_a_fetched_instance():
    from django.db.models import TextField
    from django.db.models.functions import Cast as DjangoCast, ExtractMonth as DjangoExtractMonth

    from tests.djangoproject.clubs.models import Member as DjangoMember

    eva = DjangoMember.objects.annotate(
        month=DjangoExtractMonth("birth_date"), born=DjangoCast("birth_date", TextField())
    ).get(first_name="Eva")
    return eva.month, eva.born


@case(
    "get",
    "clause.annotate",
    "lookup.exact",
    "function.ExtractMonth",
    "function.Cast",
    expected=(5, "1982-05-10"),
    django=_django_function_annotation_on_a_fetched_instance,
)
async def scenario_function_annotation_on_a_fetched_instance():
    from adorm.query import Cast, ExtractMonth

    eva = await Member.objects.annotate(month=ExtractMonth("birth_date"), born=Cast("birth_date", str)).get(
        first_name="Eva"
    )
    return eva.month, eva.born


def _django_function_in_an_alias():
    from django.db.models.functions import Left as DjangoLeft

    from tests.djangoproject.clubs.models import Member as DjangoMember

    return sorted(
        DjangoMember.objects.alias(initial=DjangoLeft("last_name", 1))
        .filter(initial="K")
        .values_list("first_name", flat=True)
    )


@case(
    "select",
    "clause.alias",
    "lookup.exact",
    "function.Left",
    expected=["Bojan", "David", "Gaja", "Klemen"],
    django=_django_function_in_an_alias,
)
async def scenario_function_in_an_alias():
    from adorm.query import Left

    query = Member.objects.alias(initial=Left("last_name", 1)).filter(initial="K")
    return sorted(await rows(query.values_list("first_name", flat=True)))


def _django_order_by_a_function_descending():
    from django.db.models.functions import ExtractMonth as DjangoExtractMonth

    from tests.djangoproject.clubs.models import Member as DjangoMember

    return list(
        DjangoMember.objects.order_by(DjangoExtractMonth("birth_date").desc()).values_list("first_name", flat=True)
    )[:3]


@case(
    "select",
    "function.ExtractMonth",
    expected=["Lara", "Klemen", "Jana"],
    django=_django_order_by_a_function_descending,
)
async def scenario_function_order_by_descending():
    from adorm.query import ExtractMonth

    return (
        await rows(Member.objects.order_by(ExtractMonth("birth_date").desc()).values_list("first_name", flat=True))
    )[:3]


def _django_grouping_by_a_function():
    from django.db.models import Count as DjangoCount
    from django.db.models.functions import ExtractQuarter as DjangoExtractQuarter

    from tests.djangoproject.clubs.models import Member as DjangoMember

    groups = DjangoMember.objects.annotate(q=DjangoExtractQuarter("birth_date")).values("q")
    return list(groups.annotate(n=DjangoCount("pk")).order_by("q").values_list("q", "n"))


@case(
    "select",
    "clause.annotate",
    "clause.group_by",
    "aggregate.Count",
    "function.ExtractQuarter",
    expected=[(1, 3), (2, 3), (3, 3), (4, 3)],
    django=_django_grouping_by_a_function,
)
async def scenario_function_grouping_by_a_function():
    from adorm.query import ExtractQuarter

    groups = Member.objects.annotate(q=ExtractQuarter("birth_date")).values("q")
    return await rows(groups.annotate(n=Count("pk")).order_by("q").values_list("q", "n"))


def _django_function_as_an_aggregate_argument():
    from django.db.models import Max as DjangoMax, Min as DjangoMin, Sum as DjangoSum
    from django.db.models.functions import ExtractYear as DjangoExtractYear, Power as DjangoPower

    from tests.djangoproject.activities.models import Activity as DjangoActivity
    from tests.djangoproject.clubs.models import Member as DjangoMember

    years = DjangoMember.objects.aggregate(
        oldest=DjangoMin(DjangoExtractYear("birth_date")), youngest=DjangoMax(DjangoExtractYear("birth_date"))
    )
    squares = DjangoActivity.objects.aggregate(squares=DjangoSum(DjangoPower("capacity", 2)))
    return years, squares


@case(
    "aggregate",
    "aggregate.Min",
    "aggregate.Max",
    "aggregate.Sum",
    "function.ExtractYear",
    "function.Power",
    expected=({"oldest": 1970, "youngest": 2003}, {"squares": 3069.0}),
    django=_django_function_as_an_aggregate_argument,
)
async def scenario_function_as_an_aggregate_argument():
    from adorm.query import ExtractYear, Power

    years = await Member.objects.aggregate(
        oldest=Min(ExtractYear("birth_date")), youngest=Max(ExtractYear("birth_date"))
    )
    squares = await Activity.objects.aggregate(squares=Sum(Power("capacity", 2)))
    return years, squares


def _django_function_aggregate_over_no_rows():
    from django.db.models import Max as DjangoMax
    from django.db.models.functions import ExtractYear as DjangoExtractYear

    from tests.djangoproject.clubs.models import Member as DjangoMember

    return DjangoMember.objects.filter(first_name="Nobody").aggregate(y=DjangoMax(DjangoExtractYear("birth_date")))


@case(
    "aggregate",
    "lookup.exact",
    "aggregate.Max",
    "function.ExtractYear",
    expected={"y": None},
    django=_django_function_aggregate_over_no_rows,
)
async def scenario_function_aggregate_over_no_rows():
    from adorm.query import ExtractYear

    return await Member.objects.filter(first_name="Nobody").aggregate(y=Max(ExtractYear("birth_date")))


def _django_function_in_an_aggregate_over_a_relation():
    from django.db.models import Min as DjangoMin
    from django.db.models.functions import ExtractYear as DjangoExtractYear

    from tests.djangoproject.clubs.models import Club as DjangoClub

    query = DjangoClub.objects.annotate(oldest=DjangoMin(DjangoExtractYear("memberships__member__birth_date")))
    return sorted(query.values_list("slug", "oldest"))


@case(
    "select",
    "clause.annotate",
    "path.nested",
    "aggregate.Min",
    "function.ExtractYear",
    expected=[("koper-sailing", 1994), ("ljubljana-rowing", 1970), ("maribor-climbing", 1976)],
    django=_django_function_in_an_aggregate_over_a_relation,
)
async def scenario_function_in_an_aggregate_over_a_relation():
    from adorm.query import ExtractYear

    query = Club.objects.annotate(oldest=Min(ExtractYear("memberships__member__birth_date")))
    return sorted(await rows(query.values_list("slug", "oldest")))


def _django_update_with_functions():
    from django.db.models import Value as DjangoValue
    from django.db.models.functions import Ceil as DjangoCeil, Replace as DjangoReplace

    from tests.djangoproject.billing.models import Invoice as DjangoInvoice
    from tests.djangoproject.clubs.models import Member as DjangoMember

    renamed = DjangoMember.objects.filter(first_name="Eva").update(
        last_name=DjangoReplace("last_name", DjangoValue("č"), DjangoValue("c"))
    )
    rounded = DjangoInvoice.objects.filter(number="2026-0005").update(total=DjangoCeil("total"))
    return (
        renamed,
        DjangoMember.objects.get(first_name="Eva").last_name,
        rounded,
        DjangoInvoice.objects.get(number="2026-0005").total,
    )


@case(
    "update",
    "get",
    "lookup.exact",
    "function.Replace",
    "function.Ceil",
    expected=(1, "Zupancic", 1, Decimal("96.00")),
    django=_django_update_with_functions,
)
async def scenario_function_update_with_functions():
    from adorm.query import Ceil, Replace

    renamed = await Member.objects.filter(first_name="Eva").update(
        last_name=Replace("last_name", Value("č"), Value("c"))
    )
    rounded = await Invoice.objects.filter(number="2026-0005").update(total=Ceil("total"))
    return (
        renamed,
        (await Member.objects.get(first_name="Eva")).last_name,
        rounded,
        (await Invoice.objects.get(number="2026-0005")).total,
    )


def _django_functions_nested():
    from django.db.models import TextField, Value as DjangoValue
    from django.db.models.functions import (
        Cast as DjangoCast,
        Concat as DjangoConcat,
        ExtractDay as DjangoExtractDay,
        Left as DjangoLeft,
        Lower as DjangoLower,
        LPad as DjangoLPad,
        Upper as DjangoUpper,
    )

    from tests.djangoproject.clubs.models import Member as DjangoMember

    code = DjangoConcat(
        DjangoUpper(DjangoLeft("last_name", 3)),
        DjangoValue("-"),
        DjangoLPad(DjangoCast(DjangoExtractDay("birth_date"), TextField()), 3, DjangoValue("0")),
    )
    query = (
        DjangoMember.objects.annotate(code=code).filter(first_name__in=["Ana", "Lara"]).order_by(DjangoLower("code"))
    )
    return list(query.values_list("code", flat=True))


@case(
    "select",
    "clause.annotate",
    "lookup.in",
    "function.Concat",
    "function.Upper",
    "function.Left",
    "function.LPad",
    "function.Cast",
    "function.ExtractDay",
    "function.Lower",
    expected=["BOŽ-010", "NOV-010"],
    # SQLite's UPPER() changes ASCII letters only.
    on_vendor={"sqlite": ["BOž-010", "NOV-010"]},
    django=_django_functions_nested,
)
async def scenario_function_nested_functions():
    from adorm.query import Cast, ExtractDay, Left, LPad

    code = Concat(Upper(Left("last_name", 3)), Value("-"), LPad(Cast(ExtractDay("birth_date"), str), 3, Value("0")))
    query = Member.objects.annotate(code=code).filter(first_name__in=["Ana", "Lara"]).order_by(Lower("code"))
    return await rows(query.values_list("code", flat=True))


# NULL and empty inputs


def _django_functions_of_null():
    from django.db.models import F as DjangoF, TextField, Value as DjangoValue
    from django.db.models.functions import (
        Abs,
        Cast,
        ExtractYear,
        Left,
        LPad,
        MD5,
        Mod,
        Power,
        Reverse,
        Round,
        Sqrt,
        StrIndex,
    )

    from tests.djangoproject.activities.models import Activity as DjangoActivity

    query = DjangoActivity.objects.filter(name="Spring regatta").annotate(
        a=Abs("coach_id"),
        m=Mod("coach_id", 2),
        p=Power("coach_id", 2),
        s=Sqrt("coach_id"),
        r=Round("coach_id"),
        y=ExtractYear("coach__birth_date"),
        c=Cast("coach_id", TextField()),
        left=Left("coach__first_name", 1),
        pad=LPad("coach__first_name", 3, DjangoValue(".")),
        rev=Reverse("coach__first_name"),
        md5=MD5("coach__first_name"),
        index=StrIndex(DjangoF("coach__first_name"), DjangoValue("a")),
    )
    return list(query.values_list("a", "m", "p", "s", "r", "y", "c", "left", "pad", "rev", "md5", "index"))


@case(
    "select",
    "clause.annotate",
    "lookup.exact",
    "path.nested",
    "function.Abs",
    "function.Mod",
    "function.Power",
    "function.Sqrt",
    "function.Round",
    "function.ExtractYear",
    "function.Cast",
    "function.Left",
    "function.LPad",
    "function.Reverse",
    "function.MD5",
    "function.StrIndex",
    expected=[(None,) * 12],
    django=_django_functions_of_null,
)
async def scenario_function_of_null_is_null():
    from adorm.query import Abs, Cast, ExtractYear, Left, LPad, MD5, Mod, Power, Reverse, Round, Sqrt, StrIndex

    query = Activity.objects.filter(name="Spring regatta").annotate(
        a=Abs("coach_id"),
        m=Mod("coach_id", 2),
        p=Power("coach_id", 2),
        s=Sqrt("coach_id"),
        r=Round("coach_id"),
        y=ExtractYear("coach__birth_date"),
        c=Cast("coach_id", str),
        left=Left("coach__first_name", 1),
        pad=LPad("coach__first_name", 3, Value(".")),
        rev=Reverse("coach__first_name"),
        md5=MD5("coach__first_name"),
        index=StrIndex(F("coach__first_name"), Value("a")),
    )
    return await rows(query.values_list("a", "m", "p", "s", "r", "y", "c", "left", "pad", "rev", "md5", "index"))


def _django_null_arguments_of_comparisons():
    from django.db.models import Value as DjangoValue
    from django.db.models.functions import Greatest, Least, NullIf

    from tests.djangoproject.activities.models import Competition as DjangoCompetition

    deadline = DjangoValue(datetime.date(2026, 3, 1))
    query = DjangoCompetition.objects.order_by("name").annotate(
        greatest=Greatest("entry_deadline", deadline),
        least=Least("entry_deadline", deadline),
        same=NullIf("discipline", DjangoValue("Bouldering")),
    )
    return list(query.values_list("name", "greatest", "least", "same"))


@case(
    "select",
    "clause.annotate",
    "function.Greatest",
    "function.Least",
    "function.NullIf",
    expected=[
        ("Regional bouldering cup", datetime.date(2026, 3, 1), datetime.date(2026, 3, 1), None),
        ("Spring regatta", datetime.date(2026, 3, 12), datetime.date(2026, 3, 1), "Coxed eights"),
    ],
    # SQLite returns NULL when any argument of GREATEST or LEAST is NULL.
    on_vendor={
        "sqlite": [
            ("Regional bouldering cup", None, None, None),
            ("Spring regatta", datetime.date(2026, 3, 12), datetime.date(2026, 3, 1), "Coxed eights"),
        ]
    },
    django=_django_null_arguments_of_comparisons,
)
async def scenario_function_null_arguments_of_comparisons():
    from adorm.query import Greatest, Least, NullIf

    deadline = Value(datetime.date(2026, 3, 1))
    query = Competition.objects.order_by("name").annotate(
        greatest=Greatest("entry_deadline", deadline),
        least=Least("entry_deadline", deadline),
        same=NullIf("discipline", Value("Bouldering")),
    )
    return await rows(query.values_list("name", "greatest", "least", "same"))


def _django_empty_inputs():
    from django.db.models import Value as DjangoValue
    from django.db.models.functions import JSONArray, JSONObject, Left, MD5, Reverse, StrIndex, Trim

    from tests.djangoproject.clubs.models import Member as DjangoMember

    query = DjangoMember.objects.filter(first_name="Ana").annotate(
        array=JSONArray(),
        object=JSONObject(),
        left=Left(DjangoValue(""), 3),
        rev=Reverse(DjangoValue("")),
        trim=Trim(DjangoValue("   ")),
        md5=MD5(DjangoValue("")),
        index=StrIndex(DjangoValue(""), DjangoValue("a")),
    )
    return list(query.values_list("array", "object", "left", "rev", "trim", "md5", "index"))


@case(
    "select",
    "clause.annotate",
    "lookup.exact",
    "function.JSONArray",
    "function.JSONObject",
    "function.Left",
    "function.Reverse",
    "function.Trim",
    "function.MD5",
    "function.StrIndex",
    expected=[([], {}, "", "", "", "d41d8cd98f00b204e9800998ecf8427e", 0)],
    django=_django_empty_inputs,
)
async def scenario_function_of_empty_inputs():
    from adorm.query import JSONArray, JSONObject, Left, MD5, Reverse, StrIndex, Trim

    query = Member.objects.filter(first_name="Ana").annotate(
        array=JSONArray(),
        object=JSONObject(),
        left=Left(Value(""), 3),
        rev=Reverse(Value("")),
        trim=Trim(Value("   ")),
        md5=MD5(Value("")),
        index=StrIndex(Value(""), Value("a")),
    )
    return await rows(query.values_list("array", "object", "left", "rev", "trim", "md5", "index"))


def _django_function_over_no_rows():
    from django.db.models.functions import Reverse as DjangoReverse

    from tests.djangoproject.clubs.models import Member as DjangoMember

    query = DjangoMember.objects.filter(first_name="Nobody").annotate(r=DjangoReverse("last_name"))
    return list(query.order_by(DjangoReverse("first_name")).values_list("r", flat=True))


@case(
    "select", "clause.annotate", "lookup.exact", "function.Reverse", expected=[], django=_django_function_over_no_rows
)
async def scenario_function_over_no_rows():
    from adorm.query import Reverse

    query = Member.objects.filter(first_name="Nobody").annotate(r=Reverse("last_name"))
    return await rows(query.order_by(Reverse("first_name")).values_list("r", flat=True))


# Result types


def _type_names(row: tuple[Any, ...]) -> list[str]:
    return [type(value).__name__ for value in row]


def _django_result_types():
    from django.db.models import TextField, Value as DjangoValue
    from django.db.models.functions import (
        Cast,
        ExtractYear,
        Greatest,
        JSONArray,
        JSONObject,
        MD5,
        NullIf,
        Ord,
        Pi,
        Round,
        Sqrt,
        StrIndex,
    )

    uuid4 = _django_function("UUID4")

    from tests.djangoproject.activities.models import Activity as DjangoActivity

    query = DjangoActivity.objects.filter(name="Crag day").annotate(
        year=ExtractYear("starts_at"),
        text=Cast("capacity", TextField()),
        rounded=Round("price"),
        root=Sqrt("capacity"),
        later=Greatest("starts_at", DjangoValue(START_OF_2026)),
        md5=MD5("name"),
        array=JSONArray("name"),
        object=JSONObject(name="name"),
        code=uuid4(),
        index=StrIndex("name", DjangoValue("a")),
        ord=Ord("name"),
        pi=Pi(),
        kept=NullIf("price", DjangoValue(Decimal(0))),
    )
    fields = (
        "year",
        "text",
        "rounded",
        "root",
        "later",
        "md5",
        "array",
        "object",
        "code",
        "index",
        "ord",
        "pi",
        "price",
    )
    return [_type_names(row) for row in query.values_list(*fields)]


START_OF_2026 = datetime.datetime(2026, 1, 1, tzinfo=UTC)


@case(
    "select",
    "clause.annotate",
    "lookup.exact",
    "function.ExtractYear",
    "function.Cast",
    "function.Round",
    "function.Sqrt",
    "function.Greatest",
    "function.MD5",
    "function.JSONArray",
    "function.JSONObject",
    "function.UUID4",
    "function.StrIndex",
    "function.Ord",
    "function.Pi",
    "function.NullIf",
    expected=[
        [
            "int",
            "str",
            "Decimal",
            "float",
            "datetime",
            "str",
            "list",
            "dict",
            "UUID",
            "int",
            "int",
            "float",
            "Decimal",
        ]
    ],
    django=_django_result_types,
)
async def scenario_function_result_types():
    from adorm.query import (
        Cast,
        ExtractYear,
        Greatest,
        JSONArray,
        JSONObject,
        MD5,
        NullIf,
        Ord,
        Pi,
        Round,
        Sqrt,
        StrIndex,
        UUID4,
    )

    query = Activity.objects.filter(name="Crag day").annotate(
        year=ExtractYear("starts_at"),
        text=Cast("capacity", str),
        rounded=Round("price"),
        root=Sqrt("capacity"),
        later=Greatest("starts_at", Value(START_OF_2026)),
        md5=MD5("name"),
        array=JSONArray("name"),
        object=JSONObject(name="name"),
        code=UUID4(),
        index=StrIndex("name", Value("a")),
        ord=Ord("name"),
        pi=Pi(),
        kept=NullIf("price", Value(Decimal(0))),
    )
    fields = (
        "year",
        "text",
        "rounded",
        "root",
        "later",
        "md5",
        "array",
        "object",
        "code",
        "index",
        "ord",
        "pi",
        "price",
    )
    return [_type_names(row) for row in await rows(query.values_list(*fields))]


def _django_conditions_compare_with_function_results():
    from django.db.models import TextField
    from django.db.models.functions import Cast, Length as DjangoLength, Lower as DjangoLower, SHA256
    from django.db.models.lookups import Exact as DjangoExact, GreaterThan as DjangoGreaterThan

    from tests.djangoproject.clubs.models import Member as DjangoMember

    install_pgcrypto()
    digest = DjangoMember.objects.filter(DjangoExact(SHA256(DjangoLower("email")), _SHA256_OF_ANA)).values_list(
        "first_name", flat=True
    )
    long_text = DjangoMember.objects.filter(DjangoGreaterThan(DjangoLength(Cast("birth_date", TextField())), 9)).count()
    return list(digest), long_text


_SHA256_OF_ANA = hashlib.sha256(b"ana.0@example.com").hexdigest()


@case(
    "select",
    "count",
    "lookup.exact",
    "lookup.gt",
    "function.SHA256",
    "function.Lower",
    "function.Length",
    "function.Cast",
    expected=(["Ana"], 12),
    django=_django_conditions_compare_with_function_results,
)
async def scenario_function_conditions_compare_with_function_results():
    from adorm.query import Cast, GreaterThan, SHA256

    await in_thread(install_pgcrypto)
    digest = await rows(
        Member.objects.filter(Exact(SHA256(Lower("email")), _SHA256_OF_ANA)).values_list("first_name", flat=True)
    )
    long_text = await Member.objects.filter(GreaterThan(Length(Cast("birth_date", str)), 9)).count()
    return digest, long_text
