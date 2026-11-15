"""The ORM-independent model description of `adorm generate` and its schema hash."""

import dataclasses
import decimal
import pickle

import pytest

from adorm.codegen import (
    AppSpec,
    compute_schema_hash,
    FieldSpec,
    ModelSpec,
    NO_DEFAULT,
    python_name,
    RelationSpec,
    TypeSpec,
)


@pytest.mark.parametrize(
    ("orm_name", "expected"),
    [
        ("name", "name"),
        ("club_id", "club_id"),
        ("model_dump", "model_dump_"),
        ("model_validate", "model_validate_"),
        ("model_fields", "model_fields_"),
        ("json", "json_"),
        ("copy", "copy_"),
        ("schema", "schema_"),
        ("dict", "dict_"),
        ("save", "save_"),
        ("objects", "objects_"),
        ("pk", "pk_"),
        ("from_row", "from_row_"),
        ("DoesNotExist", "DoesNotExist_"),
        ("class", "class_"),
        ("model_dumpling", "model_dumpling"),
        ("_order", "order_"),
        ("__order", "order_"),
        ("_json", "json_"),
    ],
)
def test_python_name(orm_name, expected):
    assert python_name(orm_name) == expected


@pytest.mark.parametrize("orm_name", ["", "two words", "1st", "_", "___", "_1st", "__dunder__", "a__b", None])
def test_python_name_rejects_names_a_field_cannot_take(orm_name):
    with pytest.raises(ValueError, match="identifier|underscores|field path"):
        python_name(orm_name)


@pytest.mark.parametrize(
    ("spec", "text"),
    [
        (TypeSpec("int"), "int"),
        (TypeSpec("int", nullable=True), "int | None"),
        (TypeSpec("typing.Any", nullable=True), "Any"),
        (TypeSpec("datetime.date"), "datetime.date"),
        (TypeSpec("list", (TypeSpec("str", nullable=True),)), "list[str | None]"),
        (TypeSpec("typing.Literal", values=("a", 1)), "Literal['a', 1]"),
    ],
)
def test_type_spec_text(spec, text):
    assert str(spec) == text


@pytest.mark.parametrize(
    "kwargs",
    [{"name": "int", "values": (1,)}, {"name": "typing.Literal"}, {"name": "nosuchtype"}, {"name": "print"}],
)
def test_type_spec_rejects_invalid_types(kwargs):
    with pytest.raises(ValueError, match="Literal|builtin"):
        TypeSpec(**kwargs)


def test_field_spec_orders_constraints_and_aliases_renamed_fields():
    field = FieldSpec("json_", "json", TypeSpec("str"), constraints={"max_length": 5, "ge": 1})
    assert list(field.constraints) == ["ge", "max_length"]
    assert field.alias == "json"
    assert FieldSpec("name", "name", TypeSpec("str")).alias is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "not a name"},
        {"constraints": {"multiple_of": 2}},
        {"default": 1, "default_factory": "builtins.int"},
    ],
)
def test_field_spec_rejects_invalid_values(kwargs):
    values = {"name": "a", "orm_name": "a", "type": TypeSpec("int"), **kwargs}
    with pytest.raises(ValueError, match="identifier|unknown constraints|both a default"):
        FieldSpec(**values)


def test_relation_spec_needs_its_columns():
    with pytest.raises(ValueError, match="needs a column"):
        RelationSpec("foreign_key", "club", "club", "a.Club")
    with pytest.raises(ValueError, match="needs through"):
        RelationSpec("many_to_many", "tags", "tags", "a.Tag", through="a.T")


def test_relation_spec_forward_and_reverse():
    assert RelationSpec("foreign_key", "c", "c", "a.B", column="c_id").is_forward
    assert not RelationSpec("reverse_foreign_key", "cs", "cs", "a.B", column="c_id").is_forward
    m2m = RelationSpec("many_to_many", "t", "t", "a.T", through="a.X", source_column="s", target_column="t")
    assert m2m.is_forward
    assert not dataclasses.replace(m2m, reverse=True).is_forward


def test_model_spec_rejects_duplicate_names_and_lists_orm_names():
    field = FieldSpec("copy_", "copy", TypeSpec("int"))
    relation = RelationSpec("foreign_key", "json_", "json", "a.B", column="json_id")
    model = ModelSpec("a.M", "M", "id", (FieldSpec("id", "id", TypeSpec("int")), field), (relation,), "h")
    assert model.orm_names == {"copy_": "copy", "json_": "json"}
    with pytest.raises(ValueError, match="several fields or relations named copy_"):
        ModelSpec("a.M", "M", "id", (field, field), (), "h")


@pytest.mark.parametrize("label", ["not-valid", "class", "1app"])
def test_app_spec_label_names_a_module(label):
    with pytest.raises(ValueError, match="cannot name a module"):
        AppSpec(label, ())


ID = FieldSpec("id", "id", TypeSpec("int"), db_filled=True, primary_key=True)
NAME = FieldSpec("name", "name", TypeSpec("str"), constraints={"max_length": 20})
CLUB = RelationSpec("foreign_key", "club", "club", "a.Club", column="club_id")


def schema_hash(fields=(ID, NAME), relations=(CLUB,), **kwargs):
    return compute_schema_hash(kwargs.pop("label", "a.Member"), kwargs.pop("pk", "id"), fields, relations, **kwargs)


def test_schema_hash_is_sixteen_hex_digits_and_stable():
    first = schema_hash()
    assert len(first) == 16
    assert int(first, 16) >= 0
    assert schema_hash() == first


@pytest.mark.parametrize(
    "changed",
    [
        {"label": "a.Other"},
        {"pk": "name"},
        {"parents": ("a.Person",)},
        {"fields": (ID,)},
        {"fields": (ID, dataclasses.replace(NAME, type=TypeSpec("int")))},
        {"fields": (ID, dataclasses.replace(NAME, nullable=True))},
        {"fields": (ID, dataclasses.replace(NAME, constraints={"max_length": 21}))},
        {"fields": (ID, dataclasses.replace(NAME, name="name_", orm_name="name"))},
        {"relations": ()},
        {"relations": (dataclasses.replace(CLUB, target="a.Team"),)},
    ],
)
def test_schema_hash_changes_with_the_data_contract(changed):
    assert schema_hash(**changed) != schema_hash()


@pytest.mark.parametrize(
    "changed",
    [
        {"fields": (ID, dataclasses.replace(NAME, default="x"))},
        {"fields": (ID, dataclasses.replace(NAME, description="Shown in the schema"))},
        {"fields": (ID, dataclasses.replace(NAME, db_filled=True))},
        {"relations": (CLUB, RelationSpec("reverse_foreign_key", "notes", "notes", "a.Note", column="member_id"))},
    ],
)
def test_schema_hash_ignores_what_rows_do_not_carry(changed):
    assert schema_hash(**changed) == schema_hash()


def test_schema_hash_treats_equal_decimal_constraints_alike():
    a = dataclasses.replace(NAME, type=TypeSpec("decimal.Decimal"), constraints={"ge": decimal.Decimal("0.5")})
    b = dataclasses.replace(NAME, type=TypeSpec("decimal.Decimal"), constraints={"ge": decimal.Decimal("0.5")})
    assert schema_hash(fields=(ID, a)) == schema_hash(fields=(ID, b))


def test_no_default_is_a_singleton_that_pickles():
    assert pickle.loads(pickle.dumps(NO_DEFAULT)) is NO_DEFAULT
    assert repr(NO_DEFAULT) == "NO_DEFAULT"
