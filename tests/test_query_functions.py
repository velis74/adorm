"""The database functions of ``django.db.models.functions`` that are not window functions, as adorm nodes: exports,
construct names, arguments, result types, encoding and the errors their constructors raise."""

import datetime
import json
import uuid

from decimal import Decimal

import pytest

import adorm
import adorm.query

from adorm.query import F, Func, Node, Sum, Value

COMPARISON = ["Cast", "Collate", "Greatest", "Least", "NullIf"]
JSON = ["JSONArray", "JSONObject"]
EXTRACTS = [
    "ExtractYear",
    "ExtractIsoYear",
    "ExtractQuarter",
    "ExtractMonth",
    "ExtractWeek",
    "ExtractDay",
    "ExtractWeekDay",
    "ExtractIsoWeekDay",
    "ExtractHour",
    "ExtractMinute",
    "ExtractSecond",
]
MATH = [
    "Abs",
    "ACos",
    "ASin",
    "ATan",
    "ATan2",
    "Ceil",
    "Cos",
    "Cot",
    "Degrees",
    "Exp",
    "Floor",
    "Ln",
    "Log",
    "Mod",
    "Pi",
    "Power",
    "Radians",
    "Random",
    "Round",
    "Sign",
    "Sin",
    "Sqrt",
    "Tan",
]
HASHES = ["MD5", "SHA1", "SHA224", "SHA256", "SHA384", "SHA512"]
TEXT = [
    "Chr",
    "Left",
    "LPad",
    "LTrim",
    *HASHES,
    "Ord",
    "Repeat",
    "Replace",
    "Reverse",
    "Right",
    "RPad",
    "RTrim",
    "StrIndex",
    "Substr",
    "Trim",
]
UUIDS = ["UUID4", "UUID7"]
NAMES = [*COMPARISON, *JSON, "Extract", *EXTRACTS, *MATH, *TEXT, *UUIDS]


def fn(name: str):
    return getattr(adorm.query, name)


# One instance of every function, built as a user writes it.
EXAMPLES = {
    "Cast": lambda: fn("Cast")("capacity", str),
    "Collate": lambda: fn("Collate")("last_name", "C"),
    "Greatest": lambda: fn("Greatest")("issued", "due"),
    "Least": lambda: fn("Least")("issued", "due", Value(datetime.date(2026, 1, 1))),
    "NullIf": lambda: fn("NullIf")("price", Value(Decimal(0))),
    "JSONArray": lambda: fn("JSONArray")("first_name", "level"),
    "JSONObject": lambda: fn("JSONObject")(name="first_name", level="level"),
    "Extract": lambda: fn("Extract")("birth_date", "year"),
    **{name: (lambda name=name: fn(name)("joined")) for name in EXTRACTS},
    "ATan2": lambda: fn("ATan2")("capacity", 10),
    "Log": lambda: fn("Log")(2, "capacity"),
    "Mod": lambda: fn("Mod")("capacity", 7),
    "Pi": lambda: fn("Pi")(),
    "Power": lambda: fn("Power")("capacity", 2),
    "Random": lambda: fn("Random")(),
    "Round": lambda: fn("Round")("total", 2),
    **{
        name: (lambda name=name: fn(name)("capacity"))
        for name in MATH
        if name not in {"ATan2", "Log", "Mod", "Pi", "Power", "Random", "Round"}
    },
    "Chr": lambda: fn("Chr")("capacity"),
    "Left": lambda: fn("Left")("last_name", 3),
    "LPad": lambda: fn("LPad")("first_name", 5, Value(".")),
    "LTrim": lambda: fn("LTrim")("first_name"),
    **{name: (lambda name=name: fn(name)("email")) for name in HASHES},
    "Ord": lambda: fn("Ord")("last_name"),
    "Repeat": lambda: fn("Repeat")("first_name", 2),
    "Replace": lambda: fn("Replace")("last_name", Value("č"), Value("c")),
    "Reverse": lambda: fn("Reverse")("first_name"),
    "Right": lambda: fn("Right")("last_name", 2),
    "RPad": lambda: fn("RPad")("first_name", 6, Value("*")),
    "RTrim": lambda: fn("RTrim")("first_name"),
    "StrIndex": lambda: fn("StrIndex")("last_name", Value("o")),
    "Substr": lambda: fn("Substr")("last_name", 2, 3),
    "Trim": lambda: fn("Trim")("first_name"),
    "UUID4": lambda: fn("UUID4")(),
    "UUID7": lambda: fn("UUID7")(),
}


def test_every_function_has_an_example():
    assert sorted(EXAMPLES) == sorted(NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_function_is_exported_from_adorm_and_adorm_query(name):
    assert getattr(adorm, name) is getattr(adorm.query, name)
    assert issubclass(getattr(adorm.query, name), Func)


@pytest.mark.parametrize("name", NAMES)
def test_construct_name_is_function_and_the_class_name(name):
    node = EXAMPLES[name]()
    assert type(node).construct == f"function.{name}"
    assert f"function.{name}" in set(node.constructs())


@pytest.mark.parametrize("name", NAMES)
def test_encoding_round_trips_through_json(name):
    node = EXAMPLES[name]()
    data = json.loads(json.dumps(node.to_dict()))
    assert Node.from_dict(data) == node
    assert type(node).from_dict(data) == node


@pytest.mark.parametrize("name", NAMES)
def test_nodes_are_immutable_and_equal_by_value(name):
    node = EXAMPLES[name]()
    assert node == EXAMPLES[name]()
    assert hash(node) == hash(EXAMPLES[name]())
    with pytest.raises(AttributeError):
        node.output_type = int


def test_string_arguments_are_field_references_and_other_values_become_values():
    left = fn("Left")("last_name", 3)
    assert F("last_name") in list(left.walk())
    assert Value(3) in list(left.walk())
    assert F("first_name") in list(fn("JSONObject")(name="first_name").walk())


def test_extract_lookup_name_and_collation_are_not_field_references():
    assert F("year") not in list(fn("Extract")("birth_date", "year").walk())
    assert F("C") not in list(fn("Collate")("last_name", "C").walk())


def test_extract_with_a_lookup_name_is_another_node_than_the_subclass():
    extract = fn("Extract")("birth_date", "year")
    year = fn("ExtractYear")("birth_date")
    assert extract != year
    assert set(extract.constructs()) == {"function.Extract"}
    assert set(year.constructs()) == {"function.ExtractYear"}
    assert extract != fn("Extract")("birth_date", "month")


def test_arguments_that_are_not_expressions_distinguish_nodes():
    assert fn("Cast")("capacity", str) != fn("Cast")("capacity", int)
    assert fn("Collate")("last_name", "C") != fn("Collate")("last_name", "POSIX")
    assert fn("Round")("total", 1) != fn("Round")("total", 2)
    assert fn("JSONObject")(a="first_name") != fn("JSONObject")(b="first_name")
    assert fn("Substr")("last_name", 2) != fn("Substr")("last_name", 2, 3)


def test_functions_nest_and_report_every_construct():
    node = fn("Upper")(fn("Left")(fn("Trim")("last_name"), 3))
    assert set(node.constructs()) == {"function.Upper", "function.Left", "function.Trim"}


@pytest.mark.parametrize(
    ("name", "output_type"),
    [
        *[(name, int) for name in EXTRACTS],
        ("Ord", int),
        ("StrIndex", int),
        *[(name, str) for name in HASHES],
        ("Chr", str),
        ("Pi", float),
        ("Random", float),
        ("UUID4", uuid.UUID),
        ("UUID7", uuid.UUID),
    ],
)
def test_output_type_of_functions_with_a_fixed_result_type(name, output_type):
    assert EXAMPLES[name]().output_type is output_type


def test_extract_with_a_lookup_name_has_an_int_result():
    assert fn("Extract")("birth_date", "iso_week_day").output_type is int


@pytest.mark.parametrize("python_type", [int, str, float, datetime.date, datetime.datetime])
def test_cast_output_type_is_the_python_type_given(python_type):
    cast = fn("Cast")("capacity", python_type)
    assert cast.output_type is python_type


@pytest.mark.parametrize(
    "lookup_name",
    ["year", "iso_year", "quarter", "month", "week", "day", "week_day", "iso_week_day", "hour", "minute", "second"],
)
def test_extract_accepts_every_lookup_name_of_its_subclasses(lookup_name):
    assert fn("Extract")("joined", lookup_name) == fn("Extract")("joined", lookup_name)


# Errors


def test_extract_rejects_an_unknown_lookup_name():
    with pytest.raises(ValueError, match="fortnight"):
        fn("Extract")("joined", "fortnight")


def test_extract_needs_a_lookup_name():
    with pytest.raises(TypeError):
        fn("Extract")("joined")


def test_cast_needs_an_output_type():
    with pytest.raises(TypeError):
        fn("Cast")("capacity")


def test_cast_rejects_a_type_adorm_cannot_name():
    class Opaque:
        pass

    with pytest.raises(TypeError):
        fn("Cast")("capacity", Opaque)


@pytest.mark.parametrize("collation", ["", "C; DROP TABLE x", 'de"x', "a b"])
def test_collate_rejects_a_collation_that_is_not_a_name(collation):
    with pytest.raises(ValueError, match="collation"):
        fn("Collate")("last_name", collation)


def test_substr_position_starts_at_one():
    with pytest.raises(ValueError, match="pos"):
        fn("Substr")("last_name", 0)


@pytest.mark.parametrize("name", ["LPad", "RPad"])
def test_padding_length_cannot_be_negative(name):
    with pytest.raises(ValueError, match="length"):
        fn(name)("first_name", -1)


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: fn("Pi")(1), "takes 0 argument"),
        (lambda: fn("Random")(1), "takes 0 argument"),
        (lambda: fn("UUID4")(1), "takes 0 argument"),
        (lambda: fn("Abs")(), "takes 1 argument"),
        (lambda: fn("Abs")("a", "b"), "takes 1 argument"),
        (lambda: fn("ExtractYear")(), "missing 1 required positional argument"),
        (lambda: fn("MD5")("a", "b"), "takes 1 argument"),
        (lambda: fn("Reverse")("a", "b"), "takes 1 argument"),
        (lambda: fn("ATan2")("a"), "takes 2 argument"),
        (lambda: fn("Log")("a"), "takes 2 argument"),
        (lambda: fn("Mod")("a"), "takes 2 argument"),
        (lambda: fn("Power")("a"), "takes 2 argument"),
        (lambda: fn("NullIf")("a"), "takes 2 argument"),
        (lambda: fn("Greatest")("a"), "at least 2"),
        (lambda: fn("Least")("a"), "at least 2"),
    ],
)
def test_function_arity(build, message):
    with pytest.raises(TypeError, match=message):
        build()


def test_functions_without_arguments_build_empty_json():
    assert list(fn("JSONArray")().get_source_expressions()) == []
    assert list(fn("JSONObject")().get_source_expressions()) == []


def test_aggregate_of_a_function_has_no_default_alias():
    with pytest.raises(TypeError):
        _ = Sum(fn("Abs")("fee")).default_alias


def test_function_as_an_aggregate_argument_is_not_an_aggregate_itself():
    assert not fn("ExtractYear")("joined").contains_aggregate
    assert Sum(fn("Power")("capacity", 2)).contains_aggregate
    assert fn("Round")(Sum("total"), 1).contains_aggregate
