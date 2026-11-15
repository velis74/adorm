"""The generated models checked in under ``tests/generated``: up to date with the Django models, and how they behave."""

import json
import warnings

from pathlib import Path

import pytest

from adorm import ForeignKey, ManyToMany, Model, ReverseForeignKey, ReverseOneToOne
from adorm.codegen import check, CodegenWarning, plan, python_name
from adorm.codegen.django import introspect, schema_hash
from adorm.models import get_relations, model_schema, registry
from adorm.operations import Get, Save
from adorm.query import F, Query
from adorm.query.lookups import split_lookup
from tests.djangoproject.library import models as django_library
from tests.generated import auth, library, MODELS

GENERATED = Path(__file__).parent / "generated"
COMMAND = "adorm generate --settings tests.djangoproject.settings -o tests/generated --line-length 120 auth library"


def test_checked_in_models_are_up_to_date():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", CodegenWarning)
        files = plan(introspect(["auth", "library"]), line_length=120)
    assert check(GENERATED, files) == [], f"run {COMMAND}"


def test_every_generated_model_is_registered():
    assert [cls._meta.label for cls in MODELS] == [
        "auth.Permission",
        "auth.Group",
        "auth.User",
        "library.Author",
        "library.Tag",
        "library.Book",
        "library.Edition",
        "library.BookProxy",
        "library.Tagging",
        "library.Note",
        "library.Shelf",
        "library.Placement",
        "library.Reader",
        "library.LoanPolicy",
        "library.Loan",
    ]
    for cls in MODELS:
        assert issubclass(cls, Model)
        assert registry.get(cls._meta.label) is cls


@pytest.mark.filterwarnings("ignore::adorm.codegen.CodegenWarning")
def test_schema_hashes_match_the_django_models():
    from django.apps import apps

    for cls in MODELS:
        assert cls._meta.schema_hash == schema_hash(apps.get_model(cls._meta.label))


def test_renamed_fields_use_the_suffixed_name_in_code_and_the_original_in_json():
    note = library.Note(content_type_id=1, object_id="7", json="{}", schema="v2", model_dump=True)
    assert (note.json_, note.schema_, note.model_dump_) == ("{}", "v2", True)
    assert library.Note(content_type_id=1, object_id="7", json_="x").json_ == "x"
    dumped = note.model_dump()
    assert dumped["json"] == "{}"
    assert dumped["model_dump"] is True
    assert "json_" not in dumped
    assert json.loads(note.model_dump_json())["schema"] == "v2"
    properties = library.Note.model_json_schema()["properties"]
    assert {"json", "schema", "model_dump"} <= set(properties)
    assert not {"json_", "schema_", "model_dump_", "copy_"} & set(properties)
    assert library.Note.model_validate_json('{"content_type_id": 1, "object_id": "7", "json": "a"}').json_ == "a"


def test_renamed_names_keep_pydantic_methods_working():
    note = library.Note(content_type_id=1, object_id="7")
    assert callable(note.model_dump)
    assert callable(library.Note.model_validate)
    assert isinstance(library.Note.copy_, ForeignKey)
    assert library.Note.copy_.column == "copy_id"


def test_orm_names_map_the_renamed_names():
    meta = library.Note._meta
    assert meta.orm_name("json_") == "json"
    assert meta.orm_name("copy_") == "copy"
    assert meta.orm_name("object_id") == "object_id"


def test_children_and_proxies_map_inherited_renamed_names():
    for cls in (library.Book, library.Edition, library.BookProxy):
        assert cls._meta.orm_name("schema_") == "schema", cls.__name__
    assert model_schema(library.BookProxy).orm_names == {"schema_": "schema"}
    edition = library.Edition(isbn="9780000000001", title="Dune", pages=1, released="2026-01-01", schema="x")
    assert edition.schema_ == "x"


def test_order_with_respect_to_field_is_renamed_and_filled_by_the_target():
    placement = library.Placement(shelf_id="A1", book_id="9780000000001")
    assert placement.order_ is None
    assert library.Placement._meta.orm_name("order_") == "_order"
    assert library.Placement(shelf_id="A1", book_id="9780000000001", _order=2).model_dump()["_order"] == 2
    assert "shelf" not in get_relations(library.Placement), "a foreign key to a non-primary-key field"
    assert "placements" not in get_relations(library.Shelf)
    assert isinstance(get_relations(library.Book)["placements"], ReverseForeignKey)


def test_queries_use_the_suffixed_names():
    query = library.Note.objects.filter(json_="x", copy___title="Dune").query
    assert query == Query("library.Note").filter(json_="x", copy___title="Dune")
    encoded = Get(query).to_dict(registry)
    assert Get.from_dict(json.loads(json.dumps(encoded)), registry) == Get(query)


def test_relation_descriptors():
    relations = get_relations(library.Book)
    assert isinstance(relations["authors"], ManyToMany)
    assert isinstance(relations["edition"], ReverseOneToOne)
    assert isinstance(relations["copy_notes"], ReverseForeignKey)
    assert isinstance(get_relations(auth.User)["author"], ReverseOneToOne)
    assert isinstance(get_relations(library.Author)["account"], ForeignKey)
    assert model_schema(library.Author).relations["pupils"] == "library.Author"
    book = library.Book.from_row({"isbn": "9780000000001", "title": "Dune"})
    assert book.copy_notes.query == Query("library.Note").filter(copy_id="9780000000001")
    assert book.tags.query == Query("library.Tag").filter(
        pk__in=Query("library.Tagging").filter(book_id="9780000000001").values("tag_id")
    )


def test_multi_table_child_and_proxy_derive_from_the_parent():
    assert issubclass(library.Edition, library.Book)
    assert issubclass(library.BookProxy, library.Book)
    assert library.Edition._meta.pk == "book_ptr_id"
    assert "title" in library.Edition.model_fields
    assert library.BookProxy._meta.label == "library.BookProxy"
    assert library.BookProxy.model_fields.keys() == library.Book.model_fields.keys()


def test_fields_follow_the_django_fields():
    fields = library.Book.model_fields
    assert set(fields) == {python_name(f.attname) for f in django_library.Book._meta.concrete_fields}
    assert fields["genre"].default == "fiction"
    assert library.Book(isbn="9780000000001", title="Dune", pages=1).price == 0
    with pytest.raises(ValueError, match="pattern|String should match"):
        library.Book(isbn="12", title="Dune", pages=1)
    with pytest.raises(ValueError, match="greater than or equal to 1"):
        library.Book(isbn="9780000000001", title="Dune", pages=0)


async def test_saving_a_generated_model_writes_the_suffixed_names():
    class Recorder:
        def __init__(self):
            self.operations = []

        async def execute(self, operation):
            self.operations.append(operation)
            return {"code": "6f1f7d1a-0000-4000-8000-000000000000"}

    executor = Recorder()
    note = library.Note.from_row(
        {"code": "6f1f7d1a-0000-4000-8000-000000000000", "content_type_id": 1, "object_id": "7"}, executor=executor
    )
    note.json_ = "text"
    await note.save()
    (operation,) = executor.operations
    assert isinstance(operation, Save)
    assert dict(operation.values)["json_"] == "text"


@pytest.mark.parametrize(
    ("key", "path", "lookup"),
    [
        ("json_", ("json_",), "exact"),
        ("json___icontains", ("json_",), "icontains"),
        ("copy___title", ("copy_", "title"), "exact"),
        ("copy___schema___in", ("copy_", "schema_"), "in"),
    ],
)
def test_lookup_paths_with_renamed_names(key, path, lookup):
    built_path, built_lookup = split_lookup(key)
    assert (F(built_path).path, built_lookup) == (path, lookup)


@pytest.mark.parametrize("path", ["a____b", "a__", "__a", "a___"])
def test_paths_with_a_double_underscore_inside_a_segment_are_refused(path):
    with pytest.raises(ValueError, match="not a field path"):
        F(path)
