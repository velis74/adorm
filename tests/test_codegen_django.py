"""Introspection of Django models (the ``library`` and ``auth`` test apps) into model descriptions."""

import datetime
import decimal
import warnings

import pytest

from django.contrib.postgres.fields import ArrayField, HStoreField
from django.core.validators import MaxLengthValidator, MinValueValidator, RegexValidator
from django.db import models
from django.test.utils import isolate_apps

from adorm.codegen import CodegenError, CodegenWarning, NO_DEFAULT, RelationSpec, TypeSpec
from adorm.codegen.django import field_spec, introspect, model_spec, schema_hash
from tests.djangoproject.library import models as library

# The library app has generic relations, which every introspection reports.
pytestmark = pytest.mark.filterwarnings("ignore::adorm.codegen.CodegenWarning")


def fields(model, **kwargs):
    return {field.name: field for field in model_spec(model, **kwargs).fields}


def relations(model, **kwargs):
    return {relation.name: relation for relation in model_spec(model, **kwargs).relations}


def bound(field, name="value"):
    """``field`` attached to a throwaway model, as Django introspection sees it."""
    with isolate_apps("tests.djangoproject.library"):
        type(
            "Holder",
            (models.Model,),
            {"__module__": __name__, name: field, "Meta": type("Meta", (), {"app_label": "library"})},
        )
    return field


@pytest.fixture
def no_codegen_warnings():
    with warnings.catch_warnings():
        warnings.simplefilter("error", CodegenWarning)
        yield


@pytest.mark.usefixtures("no_codegen_warnings")
def test_author_fields():
    spec = fields(library.Author)
    assert list(spec) == ["id", "name", "slug", "born", "rating", "website", "account_id", "mentor_id"]
    assert spec["id"].type == TypeSpec("int")
    assert spec["id"].db_filled
    assert spec["id"].primary_key
    assert spec["name"].description == "Full name as printed on the cover."
    assert dict(spec["name"].constraints) == {"max_length": 100}
    assert dict(spec["slug"].constraints) == {"max_length": 100, "pattern": "^[-a-zA-Z0-9_]+$"}
    assert spec["born"].nullable
    assert spec["born"].default is NO_DEFAULT
    assert spec["rating"].default == 3
    assert dict(spec["rating"].constraints) == {"ge": 0, "le": 5}
    assert spec["website"].default == ""
    assert spec["account_id"].nullable
    assert spec["account_id"].type == TypeSpec("int")


def test_book_fields():
    spec = fields(library.Book)
    assert spec["isbn"].primary_key
    assert not spec["isbn"].db_filled
    assert dict(spec["isbn"].constraints) == {"max_length": 13, "pattern": r"^\d{13}$"}
    assert spec["genre"].type == TypeSpec("typing.Literal", values=("fiction", "poetry", "science"))
    assert spec["genre"].default == "fiction"
    assert dict(spec["genre"].constraints) == {}
    assert dict(spec["pages"].constraints) == {"ge": 1}
    assert spec["price"].default == decimal.Decimal("0")
    assert isinstance(spec["price"].default, decimal.Decimal)
    assert dict(spec["price"].constraints) == {"max_digits": 8, "decimal_places": 2}
    assert spec["weight"].type == TypeSpec("float")
    assert spec["published"].db_filled, "timezone.now is not importable without Django"
    assert spec["added"].db_filled
    assert spec["reading_time"].type == TypeSpec("datetime.timedelta")
    assert spec["cover"].type == TypeSpec("bytes")
    assert spec["keywords"].type == TypeSpec("list", (TypeSpec("str"),))
    assert spec["keywords"].default_factory == "builtins.list"
    assert dict(spec["keywords"].constraints) == {"max_length": 10}
    assert spec["extra"].type == TypeSpec("typing.Any")
    assert spec["extra"].default_factory == "builtins.dict"
    assert spec["in_print"].default is True


def test_renamed_fields_and_relations():
    model = model_spec(library.Note)
    spec = {field.name: field for field in model.fields}
    assert spec["json_"].orm_name == "json"
    assert spec["json_"].alias == "json"
    assert spec["schema_"].alias == "schema"
    assert spec["model_dump_"].alias == "model_dump"
    assert spec["copy_id"].alias is None
    assert spec["code"].default_factory == "uuid.uuid4"
    assert spec["score"].default == decimal.Decimal("2.5")
    copy = {relation.name: relation for relation in model.relations}["copy_"]
    assert (copy.orm_name, copy.column, copy.target) == ("copy", "copy_id", "library.Book")
    assert model.orm_names == {"json_": "json", "schema_": "schema", "model_dump_": "model_dump", "copy_": "copy"}


def test_forward_and_reverse_relations():
    author = relations(library.Author)
    assert (author["mentor"].kind, author["mentor"].target, author["mentor"].column) == (
        "foreign_key",
        "library.Author",
        "mentor_id",
    )
    assert (author["account"].kind, author["account"].target) == ("foreign_key", "auth.User")
    assert (author["pupils"].kind, author["pupils"].column) == ("reverse_foreign_key", "mentor_id")
    books = author["books"]
    assert (books.kind, books.reverse, books.through) == ("many_to_many", True, "library.Book_authors")
    assert (books.source_column, books.target_column) == ("author_id", "book_id")
    book = relations(library.Book)
    assert (book["authors"].source_column, book["authors"].target_column, book["authors"].reverse) == (
        "book_id",
        "author_id",
        False,
    )
    assert (book["tags"].through, book["tags"].source_column, book["tags"].target_column) == (
        "library.Tagging",
        "book_id",
        "tag_id",
    )
    assert (book["edition"].kind, book["edition"].target, book["edition"].column) == (
        "reverse_one_to_one",
        "library.Edition",
        "book_ptr_id",
    )
    assert book["tagging_set"].column == "book_id", "the field name, not db_column"
    assert book["copy_notes"].column == "copy_id"
    assert "reviews" not in book


def test_multi_table_inheritance_child_derives_from_its_parent():
    model = model_spec(library.Edition)
    assert model.base == "library.Book"
    assert model.pk == "book_ptr_id"
    assert [field.name for field in model.fields] == ["book_ptr_id", "number", "released"]
    ptr = model.fields[0]
    assert ptr.type == TypeSpec("str")
    assert ptr.db_filled
    assert [relation.name for relation in model.relations] == ["book_ptr"]


def test_multi_table_child_of_a_parent_left_out_is_flattened():
    model = model_spec(library.Edition, included={"library.Edition"})
    assert model.base is None
    assert [field.name for field in model.fields][:3] == ["isbn", "title", "genre"]
    assert "authors" in {relation.name for relation in model.relations}


def test_proxy_derives_from_the_model_it_stands_for():
    model = model_spec(library.BookProxy)
    assert (model.base, model.pk, model.fields, model.relations) == ("library.Book", "isbn", (), ())
    flat = model_spec(library.BookProxy, included={"library.BookProxy"})
    assert flat.base is None
    assert "title" in {field.name for field in flat.fields}


def test_reverse_relations_from_models_left_out_are_skipped():
    user = {relation.name for relation in model_spec(library.Author._meta.get_field("account").related_model).relations}
    assert "author" in user
    narrow = model_spec(library.Author._meta.get_field("account").related_model, included={"auth.User"})
    assert "author" not in {relation.name for relation in narrow.relations}
    assert "groups" in {relation.name for relation in narrow.relations}


def test_choices_with_integers():
    spec = fields(library.Tagging)
    assert spec["weight"].type == TypeSpec("typing.Literal", values=(1, 2, 3))
    assert spec["weight"].default == 1
    assert spec["book_id"].type == TypeSpec("str")


def test_docstrings():
    assert model_spec(library.Author).doc.strip() == "A person who writes books."
    assert model_spec(library.Tag).doc is None, "Django's generated docstring is left out"


def test_generic_foreign_keys_are_generated_and_generic_relations_reported():
    model = model_spec(library.Note)
    (item,) = [relation for relation in model.relations if relation.name == "item"]
    assert item == RelationSpec(
        "generic_foreign_key", "item", "item", "contenttypes.ContentType", column="object_id", ct_field="content_type"
    )
    assert not item.is_forward
    assert "item" not in model.orm_names
    with pytest.warns(CodegenWarning, match=r"library\.Book\.reviews: GenericRelation is not generated"):
        model_spec(library.Book)


@pytest.mark.parametrize(
    ("field", "expected_type", "constraints"),
    [
        (models.SmallAutoField(primary_key=True), TypeSpec("int"), {}),
        (models.PositiveBigIntegerField(), TypeSpec("int"), {"ge": 0}),
        (models.TextField(), TypeSpec("str"), {}),
        (models.EmailField(), TypeSpec("str"), {"max_length": 254}),
        (models.SlugField(allow_unicode=True), TypeSpec("str"), {"max_length": 50, "pattern": r"^[-\w]+$"}),
        (models.GenericIPAddressField(), TypeSpec("str"), {"max_length": 39}),
        (models.FileField(), TypeSpec("str"), {"max_length": 100}),
        (models.TimeField(), TypeSpec("datetime.time"), {}),
        (models.UUIDField(), TypeSpec("uuid.UUID"), {}),
        (models.BinaryField(max_length=8), TypeSpec("bytes"), {"max_length": 8}),
        (HStoreField(), TypeSpec("dict", (TypeSpec("str"), TypeSpec("str", nullable=True))), {}),
        (ArrayField(models.IntegerField(null=True)), TypeSpec("list", (TypeSpec("int", nullable=True),)), {}),
        (
            ArrayField(ArrayField(models.DateField()), size=3),
            TypeSpec("list", (TypeSpec("list", (TypeSpec("datetime.date"),)),)),
            {"max_length": 3},
        ),
        (models.IntegerField(validators=[MinValueValidator(1), MinValueValidator(4)]), TypeSpec("int"), {"ge": 4}),
        (models.CharField(max_length=9, validators=[MaxLengthValidator(5)]), TypeSpec("str"), {"max_length": 5}),
        (
            models.DecimalField(
                max_digits=5, decimal_places=2, validators=[MinValueValidator(decimal.Decimal("0.01"))]
            ),
            TypeSpec("decimal.Decimal"),
            {"ge": decimal.Decimal("0.01"), "max_digits": 5, "decimal_places": 2},
        ),
        (models.IntegerField(validators=[MinValueValidator(lambda: 1)]), TypeSpec("int"), {}),
        (
            models.CharField(max_length=5, validators=[RegexValidator("x", inverse_match=True)]),
            TypeSpec("str"),
            {"max_length": 5},
        ),
        (models.CharField(max_length=5, validators=[RegexValidator("x", flags=2)]), TypeSpec("str"), {"max_length": 5}),
    ],
)
def test_field_types_and_constraints(field, expected_type, constraints):
    spec = field_spec(bound(field))
    assert spec.type == expected_type
    assert dict(spec.constraints) == constraints


def test_a_pattern_pydantic_cannot_compile_is_reported():
    field = bound(models.CharField(max_length=5, validators=[RegexValidator(r"(?<=a)b")]))
    with pytest.warns(CodegenWarning, match="the pattern of RegexValidator is not generated"):
        spec = field_spec(field)
    assert "pattern" not in spec.constraints


def test_an_unknown_field_type_becomes_any():
    class Point(models.Field):
        def db_type(self, connection):  # noqa: ARG002 - Django's signature
            return "point"

    with pytest.warns(CodegenWarning, match="the Django type Point is generated as typing.Any"):
        spec = field_spec(bound(Point()))
    assert spec.type == TypeSpec("typing.Any")


@pytest.mark.parametrize(
    ("field", "default", "factory", "db_filled"),
    [
        (models.IntegerField(default=4), 4, None, False),
        (models.CharField(max_length=5, default="x"), "x", None, False),
        (models.CharField(max_length=5), NO_DEFAULT, None, False),
        (models.CharField(max_length=5, blank=True), "", None, False),
        (models.CharField(max_length=5, blank=True, null=True), NO_DEFAULT, None, False),
        (models.CharField(max_length=5, blank=True, choices=[("a", "A")]), NO_DEFAULT, None, False),
        (models.BinaryField(blank=True), NO_DEFAULT, None, False),
        (models.DateField(default=datetime.date(2026, 1, 1)), datetime.date(2026, 1, 1), None, False),
        (models.DateField(default=datetime.date.today), NO_DEFAULT, None, True),
        (models.UUIDField(default=__import__("uuid").uuid4), NO_DEFAULT, "uuid.uuid4", False),
        (models.JSONField(default=list), NO_DEFAULT, "builtins.list", False),
        (models.JSONField(default={"a": 1}), {"a": 1}, None, False),
        (models.JSONField(default={1: "a"}), NO_DEFAULT, None, True),
        (models.IntegerField(db_default=5), NO_DEFAULT, None, True),
        (models.DateTimeField(auto_now=True), NO_DEFAULT, None, True),
        (models.DecimalField(max_digits=4, decimal_places=1, default=1.5), decimal.Decimal("1.5"), None, False),
        (models.CharField(max_length=5, choices=[("a", "A")], default=library.Genre.POETRY), "poetry", None, False),
    ],
)
def test_defaults(field, default, factory, db_filled):
    spec = field_spec(bound(field))
    assert spec.default == default
    assert type(spec.default) is type(default)
    assert spec.default_factory == factory
    assert spec.db_filled is db_filled


def test_choices_that_a_literal_cannot_hold_keep_the_base_type():
    field = bound(models.DateField(choices=[(datetime.date(2026, 1, 1), "new year")]))
    assert field_spec(field).type == TypeSpec("datetime.date")


def test_generated_field_is_filled_by_the_database():
    field = models.GeneratedField(expression=models.F("value"), output_field=models.IntegerField(), db_persist=True)
    with isolate_apps("tests.djangoproject.library"):

        class WithGenerated(models.Model):
            value = models.IntegerField()
            double = field

            class Meta:
                app_label = "library"

    spec = field_spec(WithGenerated._meta.get_field("double"))
    assert spec.db_filled
    assert spec.type == TypeSpec("int")


@isolate_apps("tests.djangoproject.library")
def test_composite_primary_keys_are_refused():
    class Pair(models.Model):
        pk = models.CompositePrimaryKey("left", "right")
        left = models.IntegerField()
        right = models.IntegerField()

        class Meta:
            app_label = "library"

    with pytest.raises(CodegenError, match="library.Pair: composite primary keys are not supported"):
        model_spec(Pair)


def test_schema_hash_is_the_one_in_the_spec_and_stable():
    for model in (library.Author, library.Book, library.Edition, library.BookProxy, library.Note):
        assert schema_hash(model) == model_spec(model).schema_hash
        assert schema_hash(model) == model_spec(model, included=set()).schema_hash, "independent of the selection"
    assert len({schema_hash(model) for model in (library.Book, library.BookProxy, library.Edition)}) == 3


def thing_hash(**field_kwargs):
    with isolate_apps("tests.djangoproject.library"):

        class Thing(models.Model):
            title = models.CharField(**field_kwargs)

            class Meta:
                app_label = "library"

    return schema_hash(Thing)


def test_schema_hash_follows_the_columns():
    plain = thing_hash(max_length=10)
    assert thing_hash(max_length=10) == plain
    assert thing_hash(max_length=10, help_text="Doc", default="a", blank=True) == plain
    assert thing_hash(max_length=11) != plain
    assert thing_hash(max_length=10, null=True) != plain
    assert thing_hash(max_length=10, choices=[("a", "A")]) != plain


def test_introspect_selects_apps_and_models():
    apps = introspect(["library", "auth"])
    assert [app.label for app in apps] == ["library", "auth"]
    assert [model.label for model in apps[0].models] == [
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
        "library.Fine",
        "library.Reservation",
    ]
    assert [model.label for model in apps[1].models] == ["auth.Permission", "auth.Group", "auth.User"]
    assert "library.Book_authors" not in {model.label for app in introspect() for model in app.models}


def test_introspect_defaults_to_every_app_in_installed_apps_order():
    assert [app.label for app in introspect()] == ["contenttypes", "auth", "library", "clubs", "activities", "billing"]


def test_introspect_excludes_apps_and_models():
    apps = introspect(exclude=["contenttypes", "clubs", "activities", "billing", "library.Note", "library.Edition"])
    assert [app.label for app in apps] == ["auth", "library"]
    labels = {model.label for model in apps[1].models}
    assert "library.Note" not in labels
    assert "library.Edition" not in labels
    book = next(model for model in apps[1].models if model.label == "library.Book")
    assert "edition" not in {relation.name for relation in book.relations}
    assert introspect(["contenttypes"], exclude=["contenttypes.ContentType"]) == ()


def test_introspect_refuses_an_unknown_app():
    with pytest.raises(CodegenError, match="no installed Django app has the label 'shop'"):
        introspect(["shop"])


def test_relations_to_a_field_other_than_the_primary_key_are_reported_and_left_out():
    with pytest.warns(
        CodegenWarning,
        match=r"library\.Placement\.shelf: the relation references library\.Shelf\.code, not a primary key",
    ):
        placement = model_spec(library.Placement)
    spec = {field.name: field for field in placement.fields}
    assert spec["shelf_id"].type == TypeSpec("str"), "the column keeps the type of the referenced field"
    assert "shelf" not in {relation.name for relation in placement.relations}
    assert "book" in {relation.name for relation in placement.relations}
    with pytest.warns(CodegenWarning, match=r"library\.Shelf\.placements: the relation references library\.Shelf"):
        shelf = model_spec(library.Shelf)
    assert shelf.relations == ()


@isolate_apps("tests.djangoproject.library")
def test_many_to_many_through_a_foreign_key_to_another_field_is_left_out():
    class Code(models.Model):
        code = models.CharField(max_length=5, unique=True)

        class Meta:
            app_label = "library"

    class Holder(models.Model):
        codes = models.ManyToManyField(Code, through="Link")

        class Meta:
            app_label = "library"

    class Link(models.Model):
        holder = models.ForeignKey(Holder, on_delete=models.CASCADE)
        code = models.ForeignKey(Code, on_delete=models.CASCADE, to_field="code")

        class Meta:
            app_label = "library"

    with pytest.warns(CodegenWarning, match=r"library\.Holder\.codes: the relation references library\.Code\.code"):
        holder = model_spec(Holder, included={"library.Holder", "library.Code", "library.Link"})
    assert "codes" not in {relation.name for relation in holder.relations}
    with pytest.warns(CodegenWarning, match=r"library\.Code\.holder_set: the relation references library\.Code\.code"):
        code = model_spec(Code, included={"library.Holder", "library.Code", "library.Link"})
    assert "holder_set" not in {relation.name for relation in code.relations}


def test_order_with_respect_to_gives_a_renamed_field_the_target_fills_in():
    placement = model_spec(library.Placement)
    order = {field.name: field for field in placement.fields}["order_"]
    assert (order.orm_name, order.alias, order.type) == ("_order", "_order", TypeSpec("int"))
    assert order.db_filled
    assert placement.orm_names == {"order_": "_order"}


def test_forward_relations_to_models_not_generated_are_reported():
    with pytest.warns(CodegenWarning, match=r"library\.Note\.content_type: contenttypes\.ContentType not generated"):
        note = model_spec(library.Note, included={"library.Note", "library.Book"})
    assert "content_type" in {relation.name for relation in note.relations}, "the descriptor is still generated"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", CodegenWarning)
        model_spec(library.Book, included={"library.Book", "library.Author", "library.Tag", "library.Tagging"})
    assert not [w for w in caught if "not generated, so" in str(w.message)], "an auto-created through is not reported"
    with pytest.warns(CodegenWarning, match=r"library\.Book\.tags: library\.Tagging not generated"):
        model_spec(library.Book, included={"library.Book", "library.Author", "library.Tag"})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", CodegenWarning)
        model_spec(library.Note)
    assert not [w for w in caught if "not generated, so" in str(w.message)], "None includes every model"


@isolate_apps("tests.djangoproject.library")
def test_names_that_give_one_python_name_twice_are_an_error():
    class Clash(models.Model):
        order = models.ForeignKey(library.Book, on_delete=models.CASCADE)
        order_ = models.IntegerField()

        class Meta:
            app_label = "library"
            order_with_respect_to = "order"

    with pytest.raises(CodegenError, match=r"library\.Clash: .*several fields or relations named order_"):
        model_spec(Clash)
