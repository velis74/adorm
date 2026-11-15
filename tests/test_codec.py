import datetime
import decimal
import json
import threading
import uuid

from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Any, Literal

import pytest

from pydantic import BaseModel, ConfigDict, Field as PydanticField

from adorm.codec import (
    adapter_for,
    decode_node,
    decode_value,
    encode_node,
    encode_value,
    node_name,
    type_from_name,
    type_name,
    TYPES,
)
from adorm.query import (
    Avg,
    Case,
    Coalesce,
    Concat,
    Count,
    Exact,
    Exists,
    F,
    ForUpdate,
    Func,
    IsNull,
    Length,
    Lookup,
    Lower,
    Max,
    Min,
    Node,
    Now,
    OrderBy,
    OuterRef,
    Q,
    Query,
    register_lookup,
    Subquery,
    Sum,
    Trunc,
    TruncDate,
    TruncMonth,
    Upper,
    Value,
    When,
)
from tests.club_schema import Address, Level, SCHEMA

MEMBERS = Query("club.Member")
UTC = datetime.UTC
SCALARS = [
    True,
    False,
    0,
    -7,
    1.5,
    "",
    "Ann",
    decimal.Decimal("1.50"),
    datetime.datetime(2026, 11, 14, 12, 30, tzinfo=UTC),
    datetime.datetime(2026, 11, 14, 12, 30),
    datetime.date(2026, 11, 14),
    datetime.time(12, 30, 1),
    datetime.timedelta(days=1, seconds=5),
    uuid.UUID("12345678-1234-5678-1234-567812345678"),
    b"\x00\xff\xfe",
]


def roundtrip(data):
    """``data`` after a trip through strict JSON text."""
    return json.loads(json.dumps(data, allow_nan=False))


def same(a, b):
    """Equal values of the same types, at any depth."""
    if type(a) is not type(b):
        return False
    if isinstance(a, list | tuple):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, set | frozenset):
        return a == b
    return a == b


# type names


def test_type_names():
    for name, tp in TYPES.items():
        assert type_name(tp) == name
        assert type_from_name(name) is tp
    for tp in (list, tuple, set, frozenset, dict):
        assert type_from_name(type_name(tp)) is tp


@pytest.mark.parametrize("tp", [object, Level, int | None, list[int]])
def test_type_name_errors(tp):
    with pytest.raises(TypeError, match="no type name"):
        type_name(tp)


@pytest.mark.parametrize("name", ["object", "", None, 1, "Decimal"])
def test_type_from_name_errors(name):
    with pytest.raises(ValueError, match="unknown type name"):
        type_from_name(name)


# values without a schema type


@pytest.mark.parametrize("value", SCALARS, ids=repr)
def test_inferred_scalars_round_trip(value):
    data = roundtrip(encode_value(value))
    assert data["type"] == type_name(type(value))
    assert same(decode_value(data), value)


def test_inferred_types_keep_bool_int_and_float_apart():
    assert encode_value(True) == {"type": "bool", "value": True}
    assert encode_value(1) == {"type": "int", "value": 1}
    assert encode_value(1.0) == {"type": "float", "value": 1.0}
    assert encode_value(datetime.datetime(2026, 1, 1))["type"] == "datetime"
    assert encode_value(datetime.date(2026, 1, 1))["type"] == "date"


def test_inferred_containers_round_trip():
    value = {
        "list": [1, "a", None, [datetime.date(2026, 1, 1)]],
        "tuple": (decimal.Decimal("2.5"), b"\x01"),
        "set": {3, 1, 2},
        "frozenset": frozenset({"b", "a"}),
        "nested": {"uuid": uuid.UUID(int=5)},
    }
    data = roundtrip(encode_value(value))
    assert data["type"] == "dict"
    assert same(decode_value(data), value)


def test_inferred_sets_encode_deterministically():
    assert encode_value({3, 1, 2}) == encode_value({2, 3, 1})
    assert [item["value"] for item in encode_value({3, 1, 2})["value"]] == [1, 2, 3]


def test_none_has_no_type():
    assert encode_value(None) == {"value": None}
    assert encode_value(None, int) == {"value": None}
    assert decode_value({"value": None}) is None
    assert decode_value({"value": None}, int) is None


@pytest.mark.parametrize("value", [object(), Level.MASTER, Address(street="a", city="b"), 1 + 2j])
def test_value_without_a_type(value):
    with pytest.raises(TypeError, match="needs a type"):
        encode_value(value)


def test_dict_without_a_type_needs_string_keys():
    with pytest.raises(TypeError, match="string keys"):
        encode_value({1: "a"})


# values with a schema type


@pytest.mark.parametrize(
    ("hint", "value", "encoded", "decoded"),
    [
        (int, 5, 5, 5),
        (int, "5", 5, 5),
        (str, "Ann", "Ann", "Ann"),
        (datetime.date, "2026-11-14", "2026-11-14", datetime.date(2026, 11, 14)),
        (datetime.date, datetime.date(2026, 11, 14), "2026-11-14", datetime.date(2026, 11, 14)),
        (decimal.Decimal, 3, "3", decimal.Decimal(3)),
        (decimal.Decimal, decimal.Decimal("1.50"), "1.50", decimal.Decimal("1.50")),
        (bytes, b"\x00\xff", "AP8=", b"\x00\xff"),
        (bytes | None, b"\xfe", "/g==", b"\xfe"),
        (list[bytes], [b"\x00", b"\x01"], ["AA==", "AQ=="], [b"\x00", b"\x01"]),
        (dict[str, bytes], {"a": b"\x00"}, {"a": "AA=="}, {"a": b"\x00"}),
        (tuple[int, ...], [1, "2"], [1, 2], (1, 2)),
        (uuid.UUID, uuid.UUID(int=1), str(uuid.UUID(int=1)), uuid.UUID(int=1)),
        (Level, Level.MASTER, "master", Level.MASTER),
        (Level, "beginner", "beginner", Level.BEGINNER),
        (Literal["active", "left"], "left", "left", "left"),
        (Address, {"street": "a", "city": "b"}, {"street": "a", "city": "b"}, Address(street="a", city="b")),
        (Address | None, Address(street="a", city="b"), {"street": "a", "city": "b"}, Address(street="a", city="b")),
        (
            datetime.datetime,
            datetime.datetime(2026, 1, 1, 8, tzinfo=datetime.timezone(datetime.timedelta(hours=1))),
            "2026-01-01T08:00:00+01:00",
            datetime.datetime(2026, 1, 1, 7, tzinfo=UTC),
        ),
        (datetime.timedelta, datetime.timedelta(hours=2), "PT2H", datetime.timedelta(hours=2)),
    ],
)
def test_typed_values(hint, value, encoded, decoded):
    data = encode_value(value, hint)
    assert data == {"value": encoded}
    assert decode_value(roundtrip(data), hint) == decoded


@pytest.mark.parametrize(
    ("hint", "value"),
    [(int, "abc"), (int, 1.5), (datetime.date, "14. 11. 2026"), (Level, "expert"), (str, 5), (bytes, 5)],
)
def test_invalid_typed_values(hint, value):
    with pytest.raises(ValueError, match="is not a valid"):
        encode_value(value, hint)


def test_typed_value_decodes_only_with_its_type():
    with pytest.raises(ValueError, match="no type"):
        decode_value({"value": 5})
    with pytest.raises(ValueError, match="is not a valid"):
        decode_value({"value": "abc"}, int)
    with pytest.raises(ValueError, match="base64"):
        decode_value({"value": 5}, bytes)


def test_any_hint_infers_the_type():
    assert encode_value(5, Any) == {"type": "int", "value": 5}


def test_the_named_type_wins_over_the_hint():
    assert decode_value({"type": "str", "value": "5"}, int) == "5"


@pytest.mark.parametrize(
    "data",
    [
        5,
        {"type": "int"},
        {"type": "nope", "value": 1},
        {"type": "int", "value": "x"},
        {"type": "int", "value": True},
        {"type": "list", "value": 5},
        {"type": "dict", "value": [1]},
        {"type": "bytes", "value": "***"},
    ],
)
def test_malformed_values(data):
    with pytest.raises(ValueError):  # noqa: PT011 - each case fails with its own message
        decode_value(data)


def test_adapters_are_cached():
    assert adapter_for(int) is adapter_for(int)
    assert adapter_for(list[bytes]) is adapter_for(list[bytes])


# nodes

NODES = [
    F("club__name"),
    OuterRef("pk"),
    Value(1),
    Value("x", output_type=str),
    Value(None),
    Value([1, {"a": datetime.date(2026, 1, 1)}]),
    F("fee") * 2 + Value(decimal.Decimal("0.5")),
    -F("age"),
    F("name").desc(nulls_last=True),
    Lower("name"),
    Upper(Lower("name")),
    Length("name"),
    Coalesce("nick", "name", Value("?")),
    Concat("first", Value(" "), "last"),
    Now(),
    Trunc("joined", "month"),
    TruncMonth("joined"),
    TruncDate("joined"),
    Count("*"),
    Count("payment", distinct=True, filter=Q(payment__amount__gt=10)),
    Sum("payment__amount", default=0),
    Avg("age", distinct=True),
    Min("age"),
    Max("joined"),
    Case(When(age__lt=18, then=Value("junior")), When(Q(age__gt=65), then="name"), default=Value("senior")),
    Q(name="a") | ~Q(age__in=[1, 2], club__isnull=False),
    Q(),
    Q(joined__range=(datetime.datetime(2026, 1, 1), F("club__founded"))),
    Q(name__regex=r"^A", tags__contains=["x"], token=uuid.UUID(int=3)),
    Exists(Query("club.Payment").filter(member=OuterRef("pk"))),
    ~Exists(Query("club.Payment")),
    Subquery(Query("club.Payment").filter(member=OuterRef("pk")).order_by("-paid_at").values("paid_at")[:1]),
    Exact(Lower("name"), "ann"),
    ForUpdate(nowait=True, of=("self",)),
    OrderBy(Lower("name"), descending=True, nulls_first=True),
    MEMBERS,
    MEMBERS.filter(name__icontains="a", club__in=Query("club.Club").filter(name="x"))
    .exclude(age__gte=F("club__owner__age"))
    .annotate(n=Count("payment"), m=F("age") + 1)
    .filter(n__gt=1, m__lt=Value(5))
    .order_by("-n", Lower("name"))
    .values_list("name", "n", named=True)
    .distinct("name")[5:10],
    MEMBERS.only("name").select_related("club").select_for_update(skip_locked=True, no_key=True).none(),
    MEMBERS.defer("photo").select_related(),
]


@pytest.mark.parametrize("node", NODES, ids=repr)
@pytest.mark.parametrize("schema", [None, SCHEMA], ids=["untyped", "typed"])
def test_nodes_round_trip(node, schema):
    data = roundtrip(node.to_dict(schema))
    restored = Node.from_dict(data, schema)
    assert restored == node
    assert hash(restored) == hash(node)
    assert repr(restored) == repr(node)
    assert type(node).from_dict(data, schema) == node
    assert encode_node(node, schema) == node.to_dict(schema)
    assert decode_node(data, schema) == node


def test_node_shape():
    assert Lower("name").to_dict() == {
        "node": "Lower",
        "expressions": [{"node": "F", "name": "name"}],
        "output_type": None,
    }
    assert Q(age__gt=3).to_dict(SCHEMA) == {
        "node": "Q",
        "children": [{"node": "lookup.gt", "lhs": {"node": "F", "name": "age"}, "rhs": {"type": "int", "value": 3}}],
        "connector": "AND",
        "negated": False,
    }


def test_lookups_are_typed_by_the_queried_model():
    data = MEMBERS.filter(age__gt="3", joined__date__gte=1).to_dict(SCHEMA)
    lookups = {c["lhs"]["name"]: c["rhs"] for c in data["filters"][0]["children"]}
    assert lookups == {"age": {"value": 3}, "joined__date": {"type": "int", "value": 1}}


@pytest.mark.parametrize(
    ("condition", "rhs"),
    [
        (Q(name="Ann"), {"value": "Ann"}),
        (Q(pk=5), {"value": 5}),
        (Q(club=5), {"value": 5}),
        (Q(club__founded__gte=datetime.date(2026, 1, 1)), {"value": "2026-01-01"}),
        (Q(club__founded__gte="2026-01-01"), {"value": "2026-01-01"}),
        (Q(fee__lt=10), {"value": "10"}),
        (Q(photo=b"\x00"), {"value": "AA=="}),
        (Q(level="master"), {"value": "master"}),
        (Q(payment=uuid.UUID(int=2)), {"value": str(uuid.UUID(int=2))}),
        (Q(club__address={"street": "a", "city": "b"}), {"value": {"street": "a", "city": "b"}}),
        (Q(club__isnull=True), {"value": True}),
        (Q(age__regex="^1"), {"value": "^1"}),
        (Q(name__icontains="ann"), {"value": "ann"}),
        (Q(pk__in=[1, "2"]), {"items": [{"value": 1}, {"value": 2}]}),
        (Q(fee__range=(1, F("age"))), {"items": [{"value": "1"}, {"node": "F", "name": "age"}]}),
        (Q(tags__contains=("a",)), {"value": ["a"]}),
        (Q(tags__contains=["a"]), {"value": ["a"]}),
        (Q(club__address__contains={"city": "b"}), {"type": "dict", "value": {"city": {"type": "str", "value": "b"}}}),
        (Q(age__icontains="1a"), {"value": "1a"}),
        (Q(club__founded__startswith="2024"), {"value": "2024"}),
        (Q(("token__startswith", "abc")), {"value": "abc"}),
        (Q(level__iexact="MASTER"), {"value": "MASTER"}),
        (Q(status__icontains="act"), {"value": "act"}),
        (Q(name__endswith="n"), {"value": "n"}),
        (Q(club__address__name="x"), {"type": "str", "value": "x"}),
    ],
)
def test_lookup_values(condition, rhs):
    data = MEMBERS.filter(condition).to_dict(SCHEMA)
    assert data["filters"][0]["children"][0]["rhs"] == rhs
    restored = Query.from_dict(roundtrip(data), SCHEMA)
    assert restored.to_dict(SCHEMA) == data


def test_invalid_lookup_value_for_its_field():
    with pytest.raises(ValueError, match="is not a valid int"):
        MEMBERS.filter(age="abc").to_dict(SCHEMA)
    assert MEMBERS.filter(age="abc").to_dict()  # without a schema the type is inferred


def test_annotations_type_lookups_on_them():
    q = MEMBERS.annotate(n=Count("payment"), total=Sum("payment__amount")).filter(n__gt=1, total__gt=5)
    data = q.to_dict(SCHEMA)
    rhs = {c["lhs"]["name"]: c["rhs"] for c in data["filters"][0]["children"]}
    assert rhs == {"n": {"value": 1}, "total": {"type": "int", "value": 5}}
    assert Query.from_dict(roundtrip(data), SCHEMA) == q


def test_an_annotation_shadows_a_field():
    q = MEMBERS.annotate(age=Length("name") * 2).filter(age__gt=3)
    data = q.to_dict(SCHEMA)
    assert data["filters"][0]["children"][0]["rhs"] == {"type": "int", "value": 3}
    assert Query.from_dict(roundtrip(data), SCHEMA) == q


def test_subqueries_resolve_against_their_own_model_and_outer_refs_against_the_outer():
    inner = Query("club.Payment").filter(Exact(OuterRef("joined"), "2026-01-01T00:00:00"), amount__gt=5)
    q = MEMBERS.filter(Exists(inner))
    data = q.to_dict(SCHEMA)
    children = data["filters"][0]["children"][0]["query"]["filters"][0]["children"]
    assert [c["rhs"] for c in children] == [{"value": "2026-01-01T00:00:00"}, {"value": "5"}]
    assert Query.from_dict(roundtrip(data), SCHEMA) == MEMBERS.filter(
        Exists(
            Query("club.Payment").filter(
                Exact(OuterRef("joined"), datetime.datetime(2026, 1, 1)), amount__gt=decimal.Decimal(5)
            )
        )
    )


def test_outer_ref_without_an_outer_query_is_untyped():
    data = MEMBERS.filter(Exact(OuterRef("age"), 3)).to_dict(SCHEMA)
    assert data["filters"][0]["children"][0]["rhs"] == {"type": "int", "value": 3}


def test_value_output_type_types_the_value():
    assert Value("5", output_type=int).to_dict() == {"node": "Value", "value": {"value": 5}, "output_type": "int"}
    assert Value(b"\x00", output_type=bytes).to_dict()["value"] == {"value": "AA=="}
    with pytest.raises(ValueError, match="is not a valid int"):
        Value("x", output_type=int).to_dict()
    with pytest.raises(TypeError, match="no type name"):
        Value(1, output_type=Level).to_dict()


def test_expression_output_type_types_a_lookup():
    assert Exact(Length("name"), "3").to_dict()["rhs"] == {"value": 3}
    assert Exact(Lower("name"), 3).to_dict()["rhs"] == {"type": "int", "value": 3}


class Reverse(Func):
    """A function defined outside adorm."""

    arity = 1


@register_lookup
class Similar(Lookup):
    lookup_name = "similar_test"


class Unregistered(Exact):
    pass


def test_node_names():
    assert node_name(Exact) == "lookup.exact"
    assert node_name(Lower) == "Lower"
    assert node_name(Query) == "Query"
    assert node_name(Reverse) == "tests.test_codec.Reverse"
    assert node_name(Similar) == "lookup.similar_test"
    assert node_name(Unregistered) == "tests.test_codec.Unregistered"


@pytest.mark.parametrize(
    "node",
    [Reverse("name"), Q(name__similar_test="Ann"), Unregistered("name", "Ann"), MEMBERS.order_by(Reverse("name"))],
    ids=repr,
)
def test_nodes_defined_outside_adorm_round_trip(node):
    data = roundtrip(node.to_dict(SCHEMA))
    assert Node.from_dict(data, SCHEMA) == node


def test_ambiguous_node_names():
    def make():
        class Twin(Func):
            arity = 1

        return Twin

    first, second = make(), make()
    assert node_name(first) == node_name(second)
    with pytest.raises(ValueError, match="more than one class"):
        Node.from_dict(first("a").to_dict())


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([], "mapping with 'node'"),
        ({"name": "a"}, "mapping with 'node'"),
        ({"node": "Nope"}, "unknown node"),
        ({"node": "os.system"}, "unknown node"),
        ({"node": "F"}, "has no 'name'"),
        ({"node": "F", "name": "a b"}, "field path"),
        ({"node": "Q", "children": [], "connector": "XOR", "negated": False}, "connector"),
        ({"node": "CombinedExpression", "lhs": None, "connector": "&&", "rhs": None, "output_type": None}, "connector"),
        ({"node": "Value", "value": 1, "output_type": None}, "encoded value"),
        ({"node": "Value", "value": {"value": 1}, "output_type": "object"}, "unknown type name"),
        ({"node": "F", "name": 1.5}, "cannot decode"),
        ({"node": "lookup.in", "lhs": {"node": "F", "name": "a"}, "rhs": {"type": "int", "value": 1}}, "sequence"),
        ({"node": "ForUpdate", "nowait": True, "skip_locked": True, "of": [], "no_key": False}, "mutually exclusive"),
    ],
)
def test_malformed_nodes(data, message):
    with pytest.raises(ValueError, match=message):
        Node.from_dict(data)


def test_malformed_trunc_kind():
    data = Trunc("joined", "month").to_dict()
    data["kind"] = "century"
    with pytest.raises(ValueError, match="truncation kind"):
        Node.from_dict(data)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"model": "Member"}, "model label"),
        ({"filters": [{"node": "F", "name": "a"}]}, "filters are non-empty Q nodes"),
        ({"ordering": [{"node": "F", "name": "a"}]}, "ordering"),
        ({"for_update": {"node": "F", "name": "a"}}, "for_update"),
        ({"row_format": "xml"}, "row format"),
        ({"low_mark": 5, "high_mark": 2}, "slice"),
        ({"select": ["a b"]}, "field path"),
        ({"select_related_fields": ["a-b"]}, "field path"),
        ({"annotations": [["a__b", {"node": "Now", "expressions": [], "output_type": "datetime"}]]}, "annotation"),
        ({"annotations": [["a", {"node": "Q", "children": [], "connector": "AND", "negated": False}], "x"]}, "pair"),
        ({"annotations": [["a", {"node": "OrderBy", "expression": {"node": "F", "name": "a"}}]]}, "descending"),
        ({"annotations": {}}, "list"),
    ],
)
def test_malformed_queries(change, message):
    data = {**MEMBERS.to_dict(), **change}
    with pytest.raises(ValueError, match=message):
        Query.from_dict(data)


def test_from_dict_checks_the_class():
    with pytest.raises(ValueError, match="expected a Query node, got F"):
        Query.from_dict(F("a").to_dict())
    with pytest.raises(ValueError, match="expected a Lookup node, got Q"):
        Lookup.from_dict(Q().to_dict())
    assert isinstance(Lookup.from_dict(Exact("a", 1).to_dict()), Exact)


def test_nodes_with_non_json_attributes_are_refused():
    class Weird(Func):
        _fields = ("expressions", "output_type", "factor")

        def __init__(self, *args, factor):
            super().__init__(*args)
            self._init(factor=factor)

    with pytest.raises(TypeError, match="cannot encode a float"):
        Weird("a", factor=1.5).to_dict()


def test_isnull_from_none_round_trips():
    q = MEMBERS.filter(club=None)
    assert q.where.children == (IsNull("club", True),)
    assert Query.from_dict(roundtrip(q.to_dict(SCHEMA)), SCHEMA) == q


def test_decoding_typed_data_without_the_schema_fails():
    data = MEMBERS.filter(age=3).to_dict(SCHEMA)
    with pytest.raises(ValueError, match="schema it was encoded with"):
        Query.from_dict(data)


def test_concurrent_encoding_from_threads_gives_one_result():
    q = NODES[-3]
    barrier = threading.Barrier(8)

    def encode(_):
        barrier.wait()
        return [json.dumps(q.to_dict(SCHEMA)) for _ in range(20)]

    with ThreadPoolExecutor(8) as pool:
        results = {text for batch in pool.map(encode, range(8)) for text in batch}
    assert len(results) == 1
    assert Query.from_dict(json.loads(results.pop()), SCHEMA) == q


# bytes, non-finite floats


class Blob(BaseModel):
    blob: bytes


class Base64Blob(BaseModel):
    model_config = ConfigDict(ser_json_bytes="base64", val_json_bytes="base64")
    blob: bytes


@pytest.mark.parametrize(
    "hint", [Annotated[bytes, PydanticField(max_length=10)], Annotated[bytes, PydanticField(max_length=10)] | None]
)
def test_annotated_bytes_are_base64(hint):
    data = roundtrip(encode_value(b"\xff\x00", hint))
    assert data == {"value": "/wA="}
    assert decode_value(data, hint) == b"\xff\x00"
    with pytest.raises(ValueError, match="is not a valid"):
        encode_value(b"\x00" * 11, hint)


def test_bytes_in_models_follow_the_model_config():
    assert decode_value(roundtrip(encode_value(Blob(blob=b"abc"), Blob)), Blob) == Blob(blob=b"abc")
    with pytest.raises(ValueError, match="ser_json_bytes='base64'"):
        encode_value(Blob(blob=b"\xff"), Blob)
    data = roundtrip(encode_value(Base64Blob(blob=b"\xff"), Base64Blob))
    assert decode_value(data, Base64Blob) == Base64Blob(blob=b"\xff")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("hint", [float, None, list[float]])
def test_non_finite_floats_are_refused(value, hint):
    with pytest.raises(ValueError, match="not JSON compliant"):
        encode_value([value] if hint == list[float] else value, hint)


# typing of values inside expressions


def test_values_in_case_and_coalesce_take_the_type_of_their_position():
    case = Case(When(age__gt=5, then=Value("master")), default=Value(Level.BEGINNER))
    query = MEMBERS.filter(level=case, status=Coalesce("status", Value("left")))
    data = query.to_dict(SCHEMA)
    rhs = {c["lhs"]["name"]: c["rhs"] for c in data["filters"][0]["children"]}
    assert rhs["level"]["cases"][0]["result"]["value"] == {"value": "master"}
    assert rhs["level"]["default"]["value"] == {"value": "beginner"}
    assert rhs["status"]["expressions"][1]["value"] == {"value": "left"}
    restored = Query.from_dict(roundtrip(data), SCHEMA)
    assert restored.where.children[0].rhs.cases[0].result == Value(Level.MASTER)
    with pytest.raises(TypeError, match="a Level value needs a type"):
        query.to_dict()


def test_case_output_type_wins_over_the_position():
    data = MEMBERS.filter(name=Case(When(age=1, then=Value("5")), default=Value("6"), output_type=int)).to_dict(SCHEMA)
    assert data["filters"][0]["children"][0]["rhs"]["default"]["value"] == {"value": 6}


def test_arithmetic_operands_keep_their_inferred_types():
    data = MEMBERS.filter(joined__gt=F("joined") - datetime.timedelta(days=1)).to_dict(SCHEMA)
    assert data["filters"][0]["children"][0]["rhs"]["rhs"]["value"] == {"type": "timedelta", "value": "P1D"}


def test_output_types_are_checked_when_built():
    for build in (
        lambda: Value(1, output_type=Level),
        lambda: Coalesce("a", "b", output_type=list[int]),
        lambda: Case(default=1, output_type=Level),
        lambda: Subquery(MEMBERS.values("pk"), output_type=Address),
        lambda: F("a").__add__(1).__class__("a", "+", 1, output_type=dict[str, int]),
    ):
        with pytest.raises(TypeError, match="no type name"):
            build()


def test_annotations_are_typed_by_their_expression():
    query = MEMBERS.annotate(lv=F("level"), club_ref=Coalesce("club_id", Value(0))).filter(
        lv=Level.MASTER, club_ref="3"
    )
    data = query.to_dict(SCHEMA)
    assert [c["rhs"] for c in data["filters"][0]["children"]] == [{"value": 3}, {"value": "master"}]
    assert Query.from_dict(roundtrip(data), SCHEMA).to_dict(SCHEMA) == data


def test_subquery_is_typed_by_the_field_it_selects():
    payments = Query("club.Payment").filter(member=OuterRef("pk")).values("paid_at")[:1]
    data = MEMBERS.filter(Exact(Subquery(payments), "2026-01-01T00:00:00")).to_dict(SCHEMA)
    assert data["filters"][0]["children"][0]["rhs"] == {"value": "2026-01-01T00:00:00"}


# structure of decoded nodes

F_A = {"node": "F", "name": "a"}
V_1 = {"node": "Value", "value": {"type": "int", "value": 1}, "output_type": None}
EXACT = {"node": "lookup.exact", "lhs": F_A, "rhs": {"type": "int", "value": 1}}
ORDER = {"node": "OrderBy", "expression": F_A, "descending": False, "nulls_first": None, "nulls_last": None}


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"node": "Subquery", "query": F_A, "output_type": None}, "holds a Query"),
        ({"node": "Exists", "query": F_A, "negated": False}, "holds a Query"),
        ({"node": "Exists", "query": MEMBERS.to_dict(), "negated": "no"}, "negated is a bool"),
        ({"node": "Q", "children": [F_A], "connector": "AND", "negated": False}, "children of a Q are conditions"),
        ({"node": "Q", "children": [ORDER], "connector": "AND", "negated": False}, "children of a Q"),
        ({"node": "Q", "children": [EXACT], "connector": "AND", "negated": 0}, "negated is a bool"),
        ({**EXACT, "lhs": ORDER}, "lhs of a lookup is an expression"),
        ({**EXACT, "rhs": ORDER}, "not a value for the exact lookup"),
        ({"node": "lookup.isnull", "lhs": F_A, "rhs": {"type": "int", "value": 1}}, "True or False"),
        ({"node": "lookup.range", "lhs": F_A, "rhs": {"items": [{"type": "int", "value": 1}]}}, "two values"),
        ({"node": "lookup.regex", "lhs": F_A, "rhs": {"type": "int", "value": 1}}, "pattern string"),
        ({"node": "Case", "cases": [F_A], "default": V_1, "output_type": None}, "Case takes When nodes"),
        ({"node": "Case", "cases": [], "default": ORDER, "output_type": None}, "default of a Case"),
        ({"node": "When", "condition": F_A, "result": V_1}, "condition of a When"),
        ({"node": "When", "condition": EXACT, "result": ORDER}, "result of a When"),
        ({**ORDER, "nulls_first": True, "nulls_last": True}, "mutually exclusive"),
        ({**ORDER, "descending": "yes"}, "descending is a bool"),
        ({**ORDER, "nulls_first": False}, "True or None"),
        ({**ORDER, "expression": ORDER}, "orders by an expression"),
        ({"node": "CombinedExpression", "lhs": F_A, "connector": "+", "rhs": ORDER, "output_type": None}, "operands"),
        ({"node": "Lower", "expressions": [F_A, F_A], "output_type": None}, "takes 1 argument"),
        ({"node": "Coalesce", "expressions": [F_A], "output_type": None}, "at least 2"),
        ({"node": "Lower", "expressions": [ORDER], "output_type": None}, "arguments of Lower are expressions"),
        ({"node": "Func", "expressions": [], "output_type": None}, "base class"),
        (
            {
                "node": "Aggregate",
                "expressions": [F_A],
                "distinct": False,
                "filter": None,
                "default": None,
                "output_type": None,
            },
            "base class",
        ),
        (
            {
                "node": "Min",
                "expressions": [F_A],
                "distinct": True,
                "filter": None,
                "default": None,
                "output_type": None,
            },
            "does not allow distinct",
        ),
        (
            {
                "node": "Count",
                "expressions": [F_A],
                "distinct": False,
                "filter": None,
                "default": V_1,
                "output_type": "int",
            },
            "does not allow default",
        ),
        (
            {
                "node": "Sum",
                "expressions": [F_A],
                "distinct": False,
                "filter": F_A,
                "default": None,
                "output_type": None,
            },
            "filter of Sum is a condition",
        ),
        (
            {
                "node": "Sum",
                "expressions": [Max("a").to_dict()],
                "distinct": False,
                "filter": None,
                "default": None,
                "output_type": None,
            },
            "cannot contain an aggregate",
        ),
        ({"node": "F", "name": 5}, "a field path is a string"),
        ({"node": "ForUpdate", "nowait": "x", "skip_locked": False, "of": [], "no_key": False}, "bools"),
    ],
)
def test_structurally_invalid_nodes(data, message):
    with pytest.raises(ValueError, match=message):
        Node.from_dict(data)


def test_a_condition_may_be_the_lhs_of_a_lookup():
    lookup = Exact(Q(age__gt=1), True)
    assert Node.from_dict(roundtrip(lookup.to_dict())) == lookup


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"low_mark": "x"}, "slice marks are integers"),
        ({"low_mark": True}, "slice marks are integers"),
        ({"high_mark": 2.5}, "slice marks are integers|cannot decode"),
        ({"is_distinct": "x"}, "is_distinct and is_none are bools"),
        ({"is_none": 1}, "is_distinct and is_none are bools"),
        ({"select_related_fields": "abc"}, "select_related_fields is a bool"),
        ({"select": "abc"}, "select is a list"),
        ({"only_fields": "abc"}, "only_fields is a list"),
        ({"row_format": "flat", "select": ["a", "b"]}, "flat needs a single field"),
    ],
)
def test_query_attributes_of_the_wrong_type(change, message):
    with pytest.raises(ValueError, match=message):
        Query.from_dict({**MEMBERS.to_dict(), **change})
