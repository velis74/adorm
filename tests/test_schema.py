import datetime
import decimal
import uuid

import pytest

from adorm import FieldError
from adorm.schema import ModelSchema, resolve_field_type, Schema, SchemaSource
from tests.club_schema import CLUB, MEMBER, SCHEMA


def test_model_schema_copies_its_mappings():
    fields = {"id": int, "name": str}
    relations = {"club": "club.Club"}
    model = ModelSchema("club.Member", "id", fields, relations)
    fields["age"] = int
    relations["owner"] = "club.Member"
    assert dict(model.fields) == {"id": int, "name": str}
    assert dict(model.relations) == {"club": "club.Club"}
    with pytest.raises(TypeError):
        model.fields["age"] = int  # type: ignore[index]


@pytest.mark.parametrize(
    ("args", "error", "message"),
    [
        (("Member", "id", {"id": int}), ValueError, "model label"),
        (("club.Member", "pk", {"id": int}), ValueError, "primary key"),
        (("club.Member", "id", {"id": int}, {"club": "Club"}), ValueError, "model label"),
        (("club.Member", "id", {"id": int, "club": int}, {"club": "club.Club"}), ValueError, "same names: club"),
    ],
)
def test_model_schema_errors(args, error, message):
    with pytest.raises(error, match=message):
        ModelSchema(*args)


def test_model_schema_orm_names():
    names = {"json_": "json", "copy_": "copy"}
    model = ModelSchema("club.Note", "id", {"id": int, "json_": str}, {"copy_": "club.Member"}, orm_names=names)
    names["id"] = "pk"
    assert dict(model.orm_names) == {"json_": "json", "copy_": "copy"}
    assert dict(ModelSchema("club.Note", "id", {"id": int}).orm_names) == {}
    with pytest.raises(ValueError, match="orm_names of club.Note names no field or relation: nothing"):
        ModelSchema("club.Note", "id", {"id": int}, orm_names={"nothing": "x"})


def test_schema_registry():
    schema = Schema([CLUB])
    assert isinstance(schema, SchemaSource)
    assert schema.model_schema("club.Club") is CLUB
    assert schema.model_schema("club.Member") is None
    assert "club.Club" in schema
    assert len(schema) == 1
    schema.add(MEMBER)
    assert list(schema) == [CLUB, MEMBER]
    replacement = ModelSchema("club.Club", "id", {"id": int})
    schema.add(replacement)
    assert schema.model_schema("club.Club") is replacement
    with pytest.raises(TypeError, match="ModelSchema"):
        schema.add({"label": "club.Club"})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("label", "path", "expected"),
    [
        ("club.Member", ["name"], str),
        ("club.Member", ["pk"], int),
        ("club.Member", ["club_id"], int | None),
        ("club.Member", ["club"], int),
        ("club.Member", ["club", "founded"], datetime.date),
        ("club.Member", ["club", "pk"], int),
        ("club.Member", ["club", "owner", "joined"], datetime.datetime),
        ("club.Member", ["payment"], uuid.UUID),
        ("club.Member", ["payment", "amount"], decimal.Decimal),
        ("club.Club", ["member", "payment", "pk"], uuid.UUID),
        ("club.Missing", ["name"], None),
        ("club.Member", [], None),
    ],
)
def test_resolve_field_type(label, path, expected):
    assert resolve_field_type(SCHEMA, label, path) == expected


@pytest.mark.parametrize(
    ("path", "message"),
    [
        (["missing"], "club.Member has no field or relation 'missing'"),
        (["club", "missing"], "club.Club has no"),
        (["name", "length"], "unsupported transform 'length' for club.Member.name"),
    ],
)
def test_resolve_field_type_rejects_unknown_fields_of_known_models(path, message):
    with pytest.raises(FieldError, match=message):
        resolve_field_type(SCHEMA, "club.Member", path)


def test_resolve_field_type_with_unknown_targets():
    schema = Schema([MEMBER])
    assert resolve_field_type(schema, "club.Member", ["club"]) is None
    assert resolve_field_type(schema, "club.Member", ["club", "name"]) is None
    assert resolve_field_type(None, "club.Member", ["name"]) is None


def test_any_object_with_model_schema_is_a_source():
    class Source:
        def model_schema(self, label):
            return MEMBER if label == "club.Member" else None

    source = Source()
    assert isinstance(source, SchemaSource)
    assert resolve_field_type(source, "club.Member", ["age"]) is int
