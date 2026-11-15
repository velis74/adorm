import dataclasses
import datetime
import decimal
import json
import uuid

import pytest

from adorm import operations
from adorm.drivers import Capabilities, Operation as OperationProtocol
from adorm.exceptions import NotSupportedError
from adorm.operations import (
    Create,
    Delete,
    First,
    Get,
    GetOrCreate,
    Last,
    Operation,
    OPERATIONS,
    Save,
    Select,
    Update,
    UpdateOrCreate,
)
from adorm.query import Case, Coalesce, Count, Exists, F, Lower, Now, OuterRef, Q, Query, Subquery, Sum, Value, When
from tests.club_schema import Level, SCHEMA

MEMBERS = Query("club.Member")
JOINED = datetime.datetime(2026, 11, 14, 9, 30, tzinfo=datetime.UTC)
FILTERED = (
    MEMBERS.filter(Q(name__icontains="ann") | Q(club__name="Chess"), age__gte=18)
    .exclude(club=None)
    .annotate(paid=Sum("payment__amount", default=Value(decimal.Decimal(0))))
    .order_by("-paid", Lower("name"))
    .select_related("club")
)

OPERATION_EXAMPLES = [
    Select(FILTERED[:10]),
    Select(MEMBERS.values_list("name", flat=True).distinct()),
    Select(MEMBERS.only("name").defer("photo")),
    Select(MEMBERS.filter(name="a") | MEMBERS.filter(age__lt=3)),
    Select(MEMBERS.none()),
    Select(MEMBERS.filter(pk=1).select_for_update(nowait=True, of=["self"])),
    Get(MEMBERS.filter(pk=1)),
    First(FILTERED),
    Last(MEMBERS.order_by("joined")),
    operations.Count(MEMBERS.filter(joined__gte=JOINED)),
    operations.Exists(MEMBERS.filter(token=uuid.UUID(int=7))),
    Create(
        "club.Member",
        {
            "name": "Ann",
            "age": 30,
            "joined": JOINED,
            "fee": decimal.Decimal("12.50"),
            "photo": b"\x89PNG\x00",
            "token": uuid.UUID(int=1),
            "tags": ["chess", "go"],
            "status": "active",
            "club_id": None,
        },
    ),
    Create("club.Member", {"name": "Bob", "joined": Now()}),
    Save("club.Member", 5, {"name": "Ann", "age": 31}, update_fields=["age"]),
    Save("club.Member", None, {"name": "New"}, force_insert=True),
    Save("club.Member", 5, {"age": F("age") + 1}, force_update=True),
    Save("club.Payment", uuid.UUID(int=9), {"amount": decimal.Decimal("3.20")}),
    Update(MEMBERS.filter(club__name="Chess"), {"fee": F("fee") * decimal.Decimal("1.1"), "status": "left"}),
    Update(MEMBERS, {}),
    Delete(MEMBERS.filter(Exists(Query("club.Payment").filter(member=OuterRef("pk"), amount__lt=decimal.Decimal(1))))),
    Delete(MEMBERS.filter(pk=5)),
    operations.DeleteInstance("club.Member", 5),
    operations.DeleteInstance("club.Payment", uuid.UUID(int=9)),
    GetOrCreate(MEMBERS.filter(club_id=3), {"name": "Ann", "club__name": "Chess"}, {"age": 30}),
    GetOrCreate(MEMBERS, {"token": uuid.UUID(int=2), "club": None}, defaults={"fee": decimal.Decimal(1)}),
    GetOrCreate(
        MEMBERS,
        {"pk__in": [1, 2], "club__in": Query("club.Club").filter(name="x")},
        {"joined": Now()},
    ),
    UpdateOrCreate(MEMBERS, {"name": "Ann"}, {"age": 31}),
    UpdateOrCreate(
        MEMBERS.filter(club_id=3), {"token": uuid.UUID(int=3)}, {"age": 2}, {"name": "New", "joined": Now()}
    ),
]


def roundtrip(data):
    return json.loads(json.dumps(data, allow_nan=False))


def test_registry():
    assert dict(OPERATIONS) == {
        "select": Select,
        "get": Get,
        "first": First,
        "last": Last,
        "count": operations.Count,
        "exists": operations.Exists,
        "delete": Delete,
        "delete_instance": operations.DeleteInstance,
        "update": Update,
        "create": Create,
        "save": Save,
        "get_or_create": GetOrCreate,
        "update_or_create": UpdateOrCreate,
        "call": operations.Call,
    }
    for kind, cls in OPERATIONS.items():
        assert cls.kind == kind


def test_kinds_are_unique():
    with pytest.raises(TypeError, match="already registered as Select"):

        class Duplicate(operations.Select):
            kind = "select"


@pytest.mark.parametrize("operation", OPERATION_EXAMPLES, ids=repr)
@pytest.mark.parametrize("schema", [None, SCHEMA], ids=["untyped", "typed"])
def test_round_trip(operation, schema):
    data = roundtrip(operation.to_dict(schema))
    assert data["operation"] == operation.kind
    restored = Operation.from_dict(data, schema)
    assert restored == operation
    assert hash(restored) == hash(operation)
    assert type(operation).from_dict(data, schema) == operation
    assert restored.to_dict(schema) == operation.to_dict(schema)


@pytest.mark.parametrize("operation", OPERATION_EXAMPLES, ids=repr)
def test_operations_satisfy_the_driver_protocol(operation):
    assert isinstance(operation, OperationProtocol)
    assert operation.model in ("club.Member", "club.Payment")
    capabilities = Capabilities(operations=OPERATIONS, constructs=set(operation.constructs()))
    capabilities.check(operation)


def test_capabilities_reject_missing_kinds_and_constructs():
    operation = Update(MEMBERS.filter(name__icontains="a"), {"name": Lower("name")})
    assert sorted(set(operation.constructs())) == ["function.Lower", "lookup.icontains"]
    with pytest.raises(NotSupportedError, match="'update' operation needs unsupported update, function.Lower"):
        Capabilities(operations={"select"}, constructs={"lookup.icontains"}).check(operation)


def test_constructs_include_every_node():
    operation = GetOrCreate(
        MEMBERS.filter(name__startswith="A").select_for_update(),
        {"age__gte": 3},
        {"joined": Now()},
    )
    assert sorted(set(operation.constructs())) == [
        "clause.select_for_update",
        "function.Now",
        "lookup.gte",
        "lookup.startswith",
    ]


def test_operations_are_immutable_and_compare_by_value():
    operation = Update(MEMBERS, {"age": 1})
    with pytest.raises(dataclasses.FrozenInstanceError):
        operation.values = ()  # type: ignore[misc]
    assert operation == Update(MEMBERS, [("age", 1)])
    assert operation != Update(MEMBERS, {"age": True})
    assert operation != Update(MEMBERS, {"age": 1.0})
    assert operation != Select(MEMBERS)
    assert Select(MEMBERS) != operations.Count(MEMBERS)
    assert {Select(MEMBERS): 1}[Select(Query("club.Member"))] == 1
    assert (Select(MEMBERS) == MEMBERS) is False


def test_values_are_copied():
    tags = ["a"]
    operation = Create("club.Member", {"tags": tags})
    tags.append("b")
    assert operation.values == (("tags", ["a"]),)


@pytest.mark.parametrize("cls", [Select, Get, First, Last, operations.Count, operations.Exists, Delete])
def test_query_operations_need_a_query(cls):
    with pytest.raises(TypeError, match="expected a Query, not str"):
        cls("club.Member")


@pytest.mark.parametrize(
    ("query", "message"),
    [
        (MEMBERS[:5], "slice"),
        (MEMBERS.distinct("name"), "distinct on fields"),
        (MEMBERS.values("name"), "values"),
        (MEMBERS.values_list("name", flat=True), "values"),
    ],
)
def test_delete_restrictions(query, message):
    with pytest.raises(TypeError, match=message):
        Delete(query)


def test_delete_allows_plain_distinct():
    assert Delete(MEMBERS.distinct()).query.is_distinct


@pytest.mark.parametrize(
    ("values", "error", "message"),
    [
        ({"club__name": "x"}, ValueError, "not a field name"),
        ({"": 1}, ValueError, "not a field name"),
        ([(1, 1)], ValueError, "not a field name"),
        ([("age", 1), ("age", 2)], ValueError, "given twice"),
        ({"age": Count("payment")}, TypeError, "aggregate"),
        ({"age": Q(age=1) & Q(name="a")}, None, None),
        ({"age": MEMBERS}, TypeError, "not an expression"),
        ({"age": object.__new__(type("Unhashable", (), {"__hash__": None}))}, TypeError, "cannot be compared"),
    ],
)
def test_update_values(values, error, message):
    if error is None:
        assert Update(MEMBERS, values).values[0][0] == "age"
        return
    with pytest.raises(error, match=message):
        Update(MEMBERS, values)


def test_update_restrictions():
    with pytest.raises(TypeError, match="slice"):
        Update(MEMBERS[:1], {"age": 1})
    with pytest.raises(TypeError, match="distinct on fields"):
        Update(MEMBERS.distinct("name"), {"age": 1})
    assert Update(MEMBERS.distinct(), {"age": 1}).query.is_distinct
    assert Update(MEMBERS.filter(name="a"), {"age": F("age") + 1}).values == (("age", F("age") + 1),)


def test_create_restrictions():
    with pytest.raises(ValueError, match="model label"):
        Create("Member", {})
    with pytest.raises(TypeError, match="cannot refer to a field"):
        Create("club.Member", {"age": F("age") + 1})
    with pytest.raises(TypeError, match="cannot refer to a field"):
        Create("club.Member", {"club_id": Subquery(Query("club.Club").filter(pk=OuterRef("pk")).values("pk"))})
    with pytest.raises(TypeError, match="aggregate"):
        Create("club.Member", {"age": Count("*")})
    assert Create("club.Member", {"name": Lower(Value("ANN"))}).values == (("name", Lower(Value("ANN"))),)
    newest = Subquery(Query("club.Club").order_by(F("founded").desc()).values("pk")[:1])
    assert Create("club.Member", {"club_id": newest}).values == (("club_id", newest),)


@pytest.mark.parametrize(
    ("kwargs", "error", "message"),
    [
        ({"model": "Member"}, ValueError, "model label"),
        ({"pk": F("id")}, TypeError, "primary key is a value"),
        ({"update_fields": "name"}, TypeError, "not a string"),
        ({"update_fields": ["fee"]}, ValueError, "without a value: 'fee'"),
        ({"force_insert": True, "force_update": True}, ValueError, "force_insert excludes"),
        ({"force_insert": True, "update_fields": ["name"]}, ValueError, "force_insert excludes"),
        ({"pk": None, "force_update": True}, ValueError, "needs a primary key"),
        ({"values": {"club__name": "x"}}, ValueError, "not a field name"),
        ({"pk": None, "values": {"age": F("age") + 1}}, TypeError, "cannot refer to a field"),
        ({"force_insert": True, "values": {"age": F("age") + 1}}, TypeError, "cannot refer to a field"),
    ],
)
def test_save_errors(kwargs, error, message):
    arguments = {"model": "club.Member", "pk": 1, "values": {"name": "a"}, **kwargs}
    with pytest.raises(error, match=message):
        Save(**arguments)


@pytest.mark.parametrize(
    ("kwargs", "error", "message"),
    [
        ({"model": "Member"}, ValueError, "model label"),
        ({"pk": None}, ValueError, "needs its primary key"),
        ({"pk": F("id")}, TypeError, "primary key is a value"),
    ],
)
def test_delete_instance_errors(kwargs, error, message):
    with pytest.raises(error, match=message):
        operations.DeleteInstance(**{"model": "club.Member", "pk": 1, **kwargs})


def test_last_refuses_a_sliced_query():
    with pytest.raises(TypeError, match="cannot reverse a query once a slice has been taken"):
        Last(MEMBERS[:3])
    assert First(MEMBERS.order_by("name")[:3]).query.is_sliced


def test_first_refuses_a_sliced_query_without_an_ordering():
    with pytest.raises(TypeError, match="sliced query without an ordering"):
        First(MEMBERS[:3])
    with pytest.raises(TypeError, match="sliced query without an ordering"):
        First(MEMBERS[2:])
    assert First(MEMBERS).query == MEMBERS


def test_save_normalises_update_fields():
    save = Save("club.Member", 1, {"name": "a", "age": 2}, update_fields=iter(["age", "name", "age"]))
    assert save.update_fields == ("age", "name")
    assert Save("club.Member", 1, {"name": "a"}, update_fields=[]).update_fields == ()
    assert Save("club.Member", 1, {"name": "a"}).update_fields is None


def test_get_or_create_prepares_lookups():
    clubs = Query("club.Club").filter(name="x")
    operation = GetOrCreate(MEMBERS, {"club": None, "pk__in": [2, None, 1], "club__in": clubs, "name": "Ann"})
    assert operation.lookups == (
        ("club", None),
        ("pk__in", (2, 1)),
        ("club__in", Subquery(clubs)),
        ("name", "Ann"),
    )
    assert operation.defaults == ()


@pytest.mark.parametrize(
    ("kwargs", "error", "message"),
    [
        ({"lookups": {"age__isnull": 1}}, ValueError, "isnull"),
        ({"lookups": {"age__gt": None}}, None, None),
        ({"lookups": {"na me": 1}}, ValueError, "field path"),
        ({"lookups": [(1, 1)]}, TypeError, "lookup key is a string"),
        ({"lookups": [("age", 1), ("age", 2)]}, ValueError, "given twice"),
        ({"defaults": {"club__name": "x"}}, ValueError, "not a field name"),
        ({"defaults": {"age": F("age")}}, TypeError, "cannot refer to a field"),
        ({"defaults": {"age": Count("*")}}, TypeError, "aggregate"),
        ({"query": MEMBERS[:3]}, TypeError, "slice"),
        ({"query": "club.Member"}, TypeError, "expected a Query"),
    ],
)
def test_get_or_create_errors(kwargs, error, message):
    arguments = {"query": MEMBERS, **kwargs}
    if error is None:
        assert GetOrCreate(**arguments)
        return
    with pytest.raises(error, match=message):
        GetOrCreate(**arguments)


def test_update_or_create_keeps_create_defaults_apart():
    operation = UpdateOrCreate(MEMBERS, {"name": "Ann"}, {"age": 31}, {"age": 1, "joined": Now()})
    assert operation.kind == "update_or_create"
    assert operation.model == "club.Member"
    assert operation.defaults == (("age", 31),)
    assert operation.create_defaults == (("age", 1), ("joined", Now()))
    assert sorted(set(operation.constructs())) == ["function.Now", "lookup.exact"]
    assert UpdateOrCreate(MEMBERS, {"name": "Ann"}).create_defaults is None
    assert UpdateOrCreate(MEMBERS, {"name": "Ann"}, create_defaults={}).create_defaults == ()
    assert not isinstance(operation, GetOrCreate)
    assert operation.to_dict(SCHEMA)["create_defaults"] == {"age": {"value": 1}, "joined": Now().to_dict()}
    assert UpdateOrCreate(MEMBERS, {"name": "Ann"}).to_dict()["create_defaults"] is None


@pytest.mark.parametrize(
    ("kwargs", "error", "message"),
    [
        ({"create_defaults": {"age": F("age") + 1}}, TypeError, "cannot refer to a field"),
        ({"create_defaults": {"club__name": "x"}}, ValueError, "not a field name"),
        ({"defaults": {"age": F("age")}}, TypeError, "cannot refer to a field"),
        ({"query": MEMBERS[:1]}, TypeError, "slice"),
    ],
)
def test_update_or_create_errors(kwargs, error, message):
    with pytest.raises(error, match=message):
        UpdateOrCreate(**{"query": MEMBERS, **kwargs})


def test_create_is_typed_by_the_model():
    operation = Create(
        "club.Member",
        {"name": "Ann", "fee": "12.50", "joined": "2026-11-14T09:30:00Z", "photo": b"\x00", "level": "master"},
    )
    assert operation.to_dict(SCHEMA) == {
        "operation": "create",
        "model": "club.Member",
        "values": {
            "name": {"value": "Ann"},
            "fee": {"value": "12.50"},
            "joined": {"value": "2026-11-14T09:30:00Z"},
            "photo": {"value": "AA=="},
            "level": {"value": "master"},
        },
    }
    restored = Operation.from_dict(roundtrip(operation.to_dict(SCHEMA)), SCHEMA)
    assert dict(restored.values) == {
        "name": "Ann",
        "fee": decimal.Decimal("12.50"),
        "joined": JOINED.replace(minute=30),
        "photo": b"\x00",
        "level": Level.MASTER,
    }


def test_enum_values_need_a_schema_type():
    operation = Update(MEMBERS, {"level": Level.MASTER})
    assert operation.to_dict(SCHEMA)["values"] == {"level": {"value": "master"}}
    assert Operation.from_dict(operation.to_dict(SCHEMA), SCHEMA) == operation
    with pytest.raises(TypeError, match="a Level value needs a type"):
        operation.to_dict()


def test_untyped_values_carry_their_type():
    data = Create("club.Member", {"name": "Ann", "fee": decimal.Decimal("1.5"), "extra": {"a": [1]}}).to_dict()
    assert data["values"] == {
        "name": {"type": "str", "value": "Ann"},
        "fee": {"type": "decimal", "value": "1.5"},
        "extra": {"type": "dict", "value": {"a": {"type": "list", "value": [{"type": "int", "value": 1}]}}},
    }


def test_models_unknown_to_the_schema_carry_their_types():
    data = Create("club.Guest", {"nickname": "A", "age": 3}).to_dict(SCHEMA)
    assert data["values"] == {"nickname": {"type": "str", "value": "A"}, "age": {"type": "int", "value": 3}}


@pytest.mark.parametrize(
    "operation",
    [
        Create("club.Member", {"nickname": "A"}),
        Update(MEMBERS, {"nmae": "x"}),
        Save("club.Member", 1, {"nmae": "x"}),
        GetOrCreate(MEMBERS, {"nmae": "x"}),
        GetOrCreate(MEMBERS, {"name": "x"}, {"nmae": "x"}),
        Select(MEMBERS.filter(club__nmae="x")),
    ],
    ids=repr,
)
def test_fields_unknown_to_the_schema_of_a_known_model_are_refused(operation):
    with pytest.raises(ValueError, match="has no field or relation 'nmae'|has no field or relation 'nickname'"):
        operation.to_dict(SCHEMA)
    assert operation.to_dict()


def test_save_pk_is_typed_by_the_primary_key():
    data = Save("club.Payment", str(uuid.UUID(int=9)), {"amount": 1}).to_dict(SCHEMA)
    assert data["pk"] == {"value": str(uuid.UUID(int=9))}
    assert data["values"] == {"amount": {"value": "1"}}
    restored = Operation.from_dict(roundtrip(data), SCHEMA)
    assert restored.pk == uuid.UUID(int=9)
    assert Save("club.Member", None, {"name": "a"}).to_dict(SCHEMA)["pk"] == {"value": None}


def test_update_values_are_typed_by_the_queried_model():
    data = Update(MEMBERS.filter(age__gt="1"), {"age": "2", "fee": F("fee") + 1}).to_dict(SCHEMA)
    assert data["values"]["age"] == {"value": 2}
    assert data["values"]["fee"]["node"] == "CombinedExpression"
    assert data["query"]["filters"][0]["children"][0]["rhs"] == {"value": 1}


def test_get_or_create_lookups_are_typed_by_their_lookup():
    operation = GetOrCreate(
        MEMBERS,
        {"age__in": ["1", 2], "club__founded__gte": "2026-01-01", "name__regex": "^A", "photo__isnull": False},
        {"fee": 5},
    )
    data = operation.to_dict(SCHEMA)
    assert data["lookups"] == [
        ["age__in", {"items": [{"value": 1}, {"value": 2}]}],
        ["club__founded__gte", {"value": "2026-01-01"}],
        ["name__regex", {"value": "^A"}],
        ["photo__isnull", {"value": False}],
    ]
    assert data["defaults"] == {"fee": {"value": "5"}}
    assert "create_defaults" not in data


def test_invalid_values_for_their_fields():
    with pytest.raises(ValueError, match="is not a valid int"):
        Create("club.Member", {"age": "abc"}).to_dict(SCHEMA)
    with pytest.raises(ValueError, match="is not a valid Level"):
        Update(MEMBERS, {"level": "expert"}).to_dict(SCHEMA)
    with pytest.raises(TypeError, match="needs a type"):
        Create("club.Member", {"age": object()}).to_dict()


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([], "mapping"),
        ({}, "unknown operation None"),
        ({"operation": "drop_table"}, "unknown operation 'drop_table'"),
        ({"operation": "select"}, "has no 'query'"),
        ({"operation": "select", "query": {"node": "F", "name": "a"}}, "is a Query"),
        ({"operation": "create", "model": "club.Member"}, "has no 'values'"),
        ({"operation": "create", "model": "club.Member", "values": []}, "mapping of field names"),
        ({"operation": "create", "model": "Member", "values": {}}, "model label"),
        (
            {
                "operation": "save",
                "model": "club.Member",
                "pk": {"value": None},
                "values": {},
                "update_fields": "name",
                "force_insert": False,
                "force_update": False,
            },
            "update_fields is a list",
        ),
        ({"operation": "get_or_create", "query": MEMBERS.to_dict(), "lookups": {}}, "list of"),
        ({"operation": "get_or_create", "query": MEMBERS.to_dict(), "lookups": [["a"]]}, "pair"),
        ({**UpdateOrCreate(MEMBERS).to_dict(), "create_defaults": []}, "create defaults are a mapping"),
        (
            {key: v for key, v in UpdateOrCreate(MEMBERS).to_dict().items() if key != "create_defaults"},
            "create_defaults",
        ),
        ({**Save("club.Member", 1, {}).to_dict(), "force_insert": "yes"}, "force_insert is a bool"),
        ({**Save("club.Member", 1, {}).to_dict(), "force_update": 1}, "force_update is a bool"),
        ({**Select(MEMBERS).to_dict(), "operation": "last", "query": MEMBERS[:2].to_dict()}, "invalid last operation"),
    ],
)
def test_malformed_operations(data, message):
    with pytest.raises(ValueError, match=message):
        Operation.from_dict(data)


def test_from_dict_checks_the_class():
    data = Select(MEMBERS).to_dict()
    with pytest.raises(ValueError, match="expected a Update operation, got 'select'"):
        Update.from_dict(data)
    assert isinstance(Operation.from_dict(data), Select)


def test_typed_data_needs_its_schema_to_decode():
    data = Create("club.Member", {"age": 3}).to_dict(SCHEMA)
    with pytest.raises(ValueError, match="schema it was encoded with"):
        Operation.from_dict(data)


def test_decoded_operations_are_validated_again():
    data = Create("club.Member", {"age": 3}).to_dict()
    data["values"]["club__name"] = {"type": "str", "value": "x"}
    with pytest.raises(ValueError, match="not a field name"):
        Operation.from_dict(data)
    data = Delete(MEMBERS).to_dict()
    data["query"]["high_mark"] = 5
    with pytest.raises(ValueError, match="invalid delete operation: .*slice"):
        Operation.from_dict(data)


def test_values_inside_expressions_are_typed_by_their_field():
    level = Case(When(age__gt=5, then=Value(Level.MASTER)), default=Value(Level.BEGINNER))
    operation = Update(MEMBERS, {"level": level, "status": Coalesce(F("status"), Value("left"))})
    data = operation.to_dict(SCHEMA)
    assert data["values"]["level"]["cases"][0]["result"]["value"] == {"value": "master"}
    assert data["values"]["level"]["default"]["value"] == {"value": "beginner"}
    assert data["values"]["status"]["expressions"][1]["value"] == {"value": "left"}
    assert Operation.from_dict(json.loads(json.dumps(data)), SCHEMA) == operation
    with pytest.raises(ValueError, match="is not a valid Literal"):
        Update(MEMBERS, {"status": Coalesce(F("status"), Value("gone"))}).to_dict(SCHEMA)
    with pytest.raises(TypeError, match="a Level value needs a type"):
        operation.to_dict()


def test_get_or_create_lookups_on_annotations_are_typed_by_the_annotation():
    operation = GetOrCreate(MEMBERS.annotate(lv=F("level")), {"lv": Level.MASTER, "name": "Ann"})
    data = operation.to_dict(SCHEMA)
    assert data["lookups"][0] == ["lv", {"value": "master"}]
    assert Operation.from_dict(json.loads(json.dumps(data)), SCHEMA) == operation
