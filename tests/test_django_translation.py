"""Translation of adorm queries into Django query sets: no database connection is involved (pytest-django blocks
database access in these tests)."""

import contextvars
import threading
import uuid
import zoneinfo

import pytest

from django.db.models.functions import Lower as DjangoLower
from django.utils import timezone, translation

from adorm import (
    Aggregate,
    Case,
    Coalesce,
    Concat,
    Count,
    Exists,
    F,
    FieldError,
    Func,
    Length,
    Lower,
    NotSupportedError,
    Now,
    OuterRef,
    ProgrammingError,
    Q,
    Subquery,
    Sum,
    Trunc,
    TruncDate,
    TruncMonth,
    TruncWeek,
    Upper,
    Value,
    When,
)
from adorm.drivers.django import adopt_context_locals, map_django_error, model_row
from adorm.drivers.django.translation import (
    aggregation,
    django_model,
    field_kwargs,
    instance_row,
    lookup_kwargs,
    model_queryset,
    operation_model,
    orm_path,
    output_field,
    subquery,
    translate,
    validation_error,
)
from adorm.operations import ModelRow
from adorm.query import Query
from adorm.query.lookups import Exact
from tests.djangoproject.library import models as dj
from tests.generated import library

AUTHOR = "library.Author"
NOTE_CODE = uuid.UUID(int=5)
MENTOR_RATING = Query(AUTHOR).filter(pk=OuterRef("mentor")).values("rating")


@pytest.mark.parametrize(
    ("model", "path", "expected"),
    [
        (dj.Note, "json_", "json"),
        (dj.Note, "copy___title", "copy__title"),
        (dj.Note, "copy___tagging_set__tag__name", "copy__tagging__tag__name"),
        (dj.Note, "copy_id", "copy_id"),
        (dj.Book, "tagging_set__weight", "tagging__weight"),
        (dj.Book, "copy_notes__schema_", "copy_notes__schema"),
        (dj.Book, "edition__number", "edition__number"),
        (dj.Book, "authors__pupils__name", "authors__pupils__name"),
        (dj.Placement, "order_", "_order"),
        (dj.Placement, "book__schema_", "book__schema"),
        (dj.Edition, "schema_", "schema"),
        (dj.Edition, "pk", "pk"),
        (dj.Tagging, "book_id", "book_id"),
        (dj.Author, "nothing__name", "nothing__name"),
        (dj.Book, "extra__a__b", "extra__a__b"),
    ],
)
def test_orm_paths_rename_each_segment_on_the_model_it_reaches(model, path, expected):
    assert orm_path(model, path) == expected


@pytest.mark.parametrize("path", ["name__nothing", "name__lower", "pupils__name__year"])
def test_orm_paths_refuse_segments_after_a_scalar_field(path):
    with pytest.raises(FieldError, match=r"library\.Author\.name is neither a relation nor a JSON field") as caught:
        orm_path(dj.Author, path)
    assert caught.value.sqlstate is None


def test_a_path_starting_with_an_annotation_is_kept():
    assert orm_path(dj.Note, "json_", annotations=["json_"]) == "json_"


def test_django_models_by_label_including_auto_created_through_models():
    assert django_model("library.Book") is dj.Book
    assert django_model("library.Book_authors") is dj.Book.authors.through
    with pytest.raises(ProgrammingError, match="no Django model is installed as 'library.Nothing'"):
        django_model("library.Nothing")


def test_model_rows_select_the_adorm_fields_and_build_rows():
    translated = translate(Query(AUTHOR))
    assert translated.columns == ("id", "name", "slug", "born", "rating", "website", "account_id", "mentor_id")
    assert translated.queryset.model is dj.Author
    row = translated.row((1, "Ann", "ann", None, 5, "", None, None))
    assert row == {
        "id": 1,
        "name": "Ann",
        "slug": "ann",
        "born": None,
        "rating": 5,
        "website": "",
        "account_id": None,
        "mentor_id": None,
    }


def test_renamed_fields_are_selected_by_their_django_names():
    assert translate(Query("library.Note")).columns == (
        "code",
        "content_type_id",
        "object_id",
        "json",
        "copy_id",
        "schema",
        "model_dump",
        "score",
    )


def test_select_related_selects_the_related_columns_and_nests_rows():
    query = Query(AUTHOR).select_related("mentor__mentor").only("name", "mentor__name", "mentor__mentor__slug")
    translated = translate(query)
    assert translated.columns == (
        "id",
        "name",
        "mentor__id",
        "mentor__name",
        "mentor__mentor__id",
        "mentor__mentor__slug",
    )
    assert translated.rows([(3, "Cid", 2, "Bob", 1, "ann"), (1, "Ann", None, None, None, None)]) == [
        {"id": 3, "name": "Cid", "mentor": {"id": 2, "name": "Bob", "mentor": {"id": 1, "slug": "ann"}}},
        {"id": 1, "name": "Ann", "mentor": None},
    ]


def test_select_related_without_paths_follows_non_null_foreign_keys_only():
    columns = translate(Query("library.Tagging").select_related()).columns
    assert {column.split("__")[0] for column in columns if "__" in column} == {"book", "tag"}
    assert not any(column.startswith("copy__") for column in translate(Query("library.Note").select_related()).columns)


def test_annotations_follow_the_fields_of_a_model_row():
    translated = translate(Query(AUTHOR).only("name").annotate(lower=Lower("name")))
    assert translated.columns == ("id", "name", "lower")
    assert translated.row((1, "Ann", "ann")) == {"id": 1, "name": "Ann", "lower": "ann"}


def test_values_rows_are_keyed_by_the_selected_names():
    translated = translate(Query("library.Note").values("json_", "copy___title", "pk"))
    assert translated.columns == ("json", "copy__title", "pk")
    assert translated.row(("{}", "Dune", 7)) == {"json_": "{}", "copy___title": "Dune", "pk": 7}
    assert translate(Query(AUTHOR).values_list("pk", "id", flat=False)).columns == ("pk", "id")


def test_a_duplicated_path_is_selected_once():
    translated = translate(Query(AUTHOR).values("name", "mentor__name", "name"))
    assert translated.columns == ("name", "mentor__name")


def test_conditions_become_django_q_objects():
    query = Query(AUTHOR).filter(name="Ann") | Query(AUTHOR).exclude(mentor__name__in=["Bob", "Cid"])
    where = translate(query).queryset.query.where
    assert str(where).count("OR") == 1
    assert "NOT" in str(where)


def test_each_filter_is_a_django_filter_call_of_its_own():
    chained = translate(Query(AUTHOR).filter(books__title="a").filter(books__title="b")).queryset
    single = translate(Query(AUTHOR).filter(Q(books__title="a") & Q(books__title="b"))).queryset
    both = translate(Query(AUTHOR).filter(books__title="a") & Query(AUTHOR).filter(books__title="b")).queryset
    assert str(chained.query).count("JOIN") == str(both.query).count("JOIN") == 4
    assert str(single.query).count("JOIN") == 2


def test_typed_values_get_a_django_field():
    from django.db import models

    assert type(output_field(int)) is models.IntegerField
    assert type(output_field(dict)) is models.JSONField
    with pytest.raises(NotSupportedError, match="output type frozenset"):
        output_field(frozenset)


def test_a_query_in_an_in_lookup_becomes_a_django_query_set():
    inner = Query(AUTHOR).filter(rating__gt=3).values("mentor")
    queryset = subquery(inner)
    assert queryset.model is dj.Author
    assert queryset.query.values_select == ("mentor",)
    assert subquery(Query(AUTHOR)).query.values_select == ()


def test_a_lookup_on_an_expression_is_filtered_through_an_alias():
    queryset = translate(Query(AUTHOR).filter(Exact(Lower("name"), "ann"))).queryset
    assert isinstance(queryset.query.annotations["_adorm0"], DjangoLower)


class Unaccent(Func):
    arity = 1


class Median(Aggregate):
    pass


@pytest.mark.parametrize(
    ("query", "message"),
    [
        (Query(AUTHOR).annotate(n=Unaccent("name")), "Unaccent"),
        (Query(AUTHOR).annotate(n=Median("rating")), "Median"),
    ],
)
def test_nodes_without_a_translation_raise_not_supported(query, message):
    with pytest.raises(NotSupportedError, match=message):
        translate(query)


def test_an_outer_reference_outside_a_subquery_raises():
    with pytest.raises(ProgrammingError, match="outside a subquery"):
        translate(Query(AUTHOR).filter(rating=OuterRef("rating")))
    with pytest.raises(ProgrammingError, match="outside a subquery"):
        subquery(MENTOR_RATING)


def test_a_nested_outer_reference_resolves_against_the_query_it_reaches():
    innermost = Query("library.Book").filter(pk=OuterRef(OuterRef("copy_")))
    note = Query("library.Note").filter(Exists(Query(AUTHOR).filter(Exists(innermost))))
    sql = str(translate(note).queryset.query)
    assert '"library_note"."copy_id"' in sql.split("EXISTS")[2]
    with pytest.raises(ProgrammingError, match="outside a subquery"):
        translate(Query(AUTHOR).filter(Exists(Query(AUTHOR).filter(pk=OuterRef(OuterRef("pk"))))))


def test_outer_references_resolve_against_the_enclosing_query():
    sql = str(translate(Query(AUTHOR).annotate(mentor_rating=Subquery(MENTOR_RATING))).queryset.query)
    inner = sql[sql.index("(SELECT") :]
    assert '= ("library_author"."mentor_id")' in inner
    note = Query("library.Note").filter(Exists(Query("library.Book").filter(pk=OuterRef("copy_"))))
    assert '"library_note"."copy_id"' in str(translate(note).queryset.query).split("EXISTS")[1]


def test_exists_and_its_negation():
    pupils = Query(AUTHOR).filter(mentor=OuterRef("pk"))
    plain = str(translate(Query(AUTHOR).filter(Exists(pupils))).queryset.query)
    negated = str(translate(Query(AUTHOR).filter(~Exists(pupils))).queryset.query)
    assert "WHERE EXISTS(" in plain
    assert "WHERE NOT EXISTS(" in negated


@pytest.mark.parametrize(
    ("node", "django_class", "kind"),
    [
        (Trunc("published", "month"), "Trunc", "month"),
        (Trunc("published", "date"), "TruncDate", None),
        (Trunc("published", "time"), "TruncTime", None),
        (TruncMonth("published"), "TruncMonth", "month"),
        (TruncWeek("published"), "TruncWeek", "week"),
        (TruncDate("published"), "TruncDate", None),
    ],
)
def test_truncations_become_djangos(node, django_class, kind):
    annotation = translate(Query("library.Book").annotate(t=node)).queryset.query.annotations["t"]
    assert type(annotation).__name__ == django_class
    if kind is not None:
        assert annotation.kind == kind


def test_aggregates_carry_distinct_filter_default_and_output_type():
    from django.db.models import Count as DjangoCount, FloatField, Q as DjangoQ, Sum as DjangoSum

    query = Query(AUTHOR).annotate(
        n=Count("books", distinct=True, filter=Q(books__pages__gt=100)),
        pages=Sum("books__pages", default=0, output_type=float),
        rows=Count("*"),
    )
    annotations = translate(query).queryset.query.annotations
    assert isinstance(annotations["n"], DjangoCount)
    assert annotations["n"].distinct
    assert isinstance(annotations["n"].filter, DjangoQ) or annotations["n"].filter is not None
    # Django wraps an aggregate with a default in Coalesce.
    assert type(annotations["pages"]).__name__ == "Coalesce"
    assert isinstance(annotations["pages"].source_expressions[0], DjangoSum)
    assert isinstance(annotations["pages"].output_field, FloatField)
    assert "COUNT(*)" in str(translate(query).queryset.query)


def test_case_coalesce_concat_length_and_now_translate():
    query = Query(AUTHOR).annotate(
        size=Case(When(rating__gte=4, then=Value("top")), default=Value("other"), output_type=str),
        label=Concat("name", Value(" / "), "slug", output_type=str),
        site=Coalesce("website", Value("")),
        n=Length("name"),
        at=Now(),
    )
    sql = str(translate(query).queryset.query)
    for fragment in ("CASE WHEN", "COALESCE(", "LENGTH(", "STATEMENT_TIMESTAMP()", "||"):
        assert fragment in sql


def test_a_lookup_on_an_expression_inside_an_annotation_is_aliased_first():
    query = Query(AUTHOR).annotate(top=Case(When(Exact(Lower("name"), "ann"), then=Value(True)), default=Value(False)))
    queryset = translate(query).queryset
    assert "_adorm0" in queryset.query.annotations
    assert "LOWER(" in str(queryset.query)


def test_aliases_are_not_selected():
    query = Query(AUTHOR).alias(n=Count("books")).filter(n__gt=1).annotate(m=F("rating") + 1)
    translated = translate(query)
    assert "n" not in translated.columns
    assert "m" in translated.columns
    assert set(translated.queryset.query.annotation_select) == {"m"}
    assert "HAVING" in str(translated.queryset.query)
    values = translate(query.values()).columns
    assert "n" not in values
    assert "m" in values


def test_grouping_puts_aggregates_after_values():
    query = (
        Query("library.Book")
        .annotate(month=TruncMonth("published"), shout=Upper("title"))
        .values("month")
        .annotate(n=Count("pk"), pages=Sum("pages"), double=F("pages") * 2)
    )
    assert query.group_by == ("month",)
    django_query = translate(query).queryset.query
    assert isinstance(django_query.group_by, tuple)
    sql = str(django_query)
    assert sql.endswith("GROUP BY 1")
    assert sql.index("DATE_TRUNC") < sql.index("FROM")
    assert "UPPER" not in sql
    assert translate(query).columns == ("month", "n", "pages", "double")


def test_a_grouped_subquery_keeps_its_grouping():
    totals = Query("library.Book").filter(authors=OuterRef("pk")).values("genre").annotate(n=Sum("pages")).values("n")
    queryset = translate(Query(AUTHOR).annotate(pages=Subquery(totals[:1]))).queryset
    assert "GROUP BY" in str(queryset.query)


def test_aggregation_translates_against_the_querys_annotations():
    from django.db.models import Sum as DjangoSum

    query = Query(AUTHOR).alias(n=Count("books")).filter(rating__gte=1)
    queryset, expressions = aggregation(query, [("total", Sum("n")), ("lowered", Count(Lower("name")))])
    assert set(expressions) == {"total", "lowered"}
    assert isinstance(expressions["total"], DjangoSum)
    assert "n" in queryset.query.annotations


def test_select_for_update_locks_with_its_options():
    query = translate(Query(AUTHOR).select_for_update(nowait=True, of=["self", "mentor"], no_key=True)).queryset.query
    assert (query.select_for_update, query.select_for_update_nowait, query.select_for_no_key_update) == (True,) * 3
    assert query.select_for_update_of == ("self", "mentor")
    skip = model_queryset(Query("library.Note").select_for_update(skip_locked=True, of=["copy_"])).query
    assert (skip.select_for_update_skip_locked, skip.select_for_update_of) == (True, ("copy",))


def test_arithmetic_becomes_a_django_combined_expression():
    from django.db.models import IntegerField
    from django.db.models.expressions import CombinedExpression

    annotation = translate(Query(AUTHOR).annotate(n=(F("rating") + 1) % 3)).queryset.query.annotations["n"]
    assert isinstance(annotation, CombinedExpression)
    assert annotation.connector == "%%"
    assert isinstance(annotation.lhs, CombinedExpression)
    assert annotation.lhs.connector == "+"
    typed = translate(Query(AUTHOR).annotate(n=F("rating").__mul__(2))).queryset.query.annotations["n"]
    assert typed.connector == "*"
    combined = field_kwargs(AUTHOR, {"rating": F("rating") * 2})["rating"]
    assert isinstance(combined, CombinedExpression)
    from adorm.query.expressions import CombinedExpression as AdormCombined

    with_type = field_kwargs(AUTHOR, {"rating": AdormCombined(F("rating"), "-", 1, output_type=int)})["rating"]
    assert isinstance(with_type.output_field, IntegerField)


def test_model_queryset_selects_no_columns():
    queryset = model_queryset(Query(AUTHOR).filter(rating__gt=3).order_by("name"))
    assert queryset.model is dj.Author
    assert queryset.query.values_select == ()
    assert queryset.query.where


def test_field_kwargs_use_django_names_and_the_values_instances_stand_for():
    book = library.Book.from_row({"isbn": "9780000000001"})
    kwargs = field_kwargs("library.Note", {"json_": "{}", "schema_": "v3", "copy_id": book})
    assert kwargs == {"json": "{}", "schema": "v3", "copy_id": "9780000000001"}


@pytest.mark.parametrize(
    ("lookups", "expected"),
    [
        ({"name": "Ann"}, {"name": "Ann"}),
        ({"pk": 3}, {"pk": 3}),
        ({"mentor": None}, {"mentor_id": None}),
        ({"mentor_id": 2}, {"mentor_id": 2}),
        ({"name__iexact": "ann"}, {"name__iexact": "ann"}),
        ({"mentor__name": "Bob"}, {"mentor__name": "Bob"}),
        ({"mentor__name": None}, {"mentor__name": None}),
        ({"rating__in": [1, 2]}, {"rating__in": (1, 2)}),
    ],
)
def test_lookup_kwargs_keep_plain_keys_creatable(lookups, expected):
    assert lookup_kwargs(Query(AUTHOR), lookups.items()) == expected


def test_lookup_kwargs_turn_an_instance_into_its_key():
    mentor = library.Author.from_row({"id": 7})
    assert lookup_kwargs(Query(AUTHOR), [("mentor", mentor)]) == {"mentor_id": 7}
    note = lookup_kwargs(Query("library.Note"), [("copy_", library.Book.from_row({"isbn": "9780000000001"}))])
    assert note == {"copy_id": "9780000000001"}


def test_instance_rows_use_adorm_names_and_leave_out_what_is_not_loaded():
    from django.db.models import F as DjangoF

    note = dj.Note(code=NOTE_CODE, object_id="x", json="{}", schema="v3", content_type_id=1)
    row = instance_row(note)
    assert row["json_"] == "{}"
    assert row["schema_"] == "v3"
    assert row["code"] == NOTE_CODE
    assert "json" not in row
    partial = dj.Author.from_db("default", ["id", "name"], [1, "Ann"])
    partial.rating = DjangoF("rating") + 1
    assert instance_row(partial) == {"id": 1, "name": "Ann"}
    assert model_row(partial) == ModelRow("library.Author", {"id": 1, "name": "Ann"})


def test_an_unknown_manager_is_a_programming_error():
    with pytest.raises(ProgrammingError, match="no Django manager 'nothing'"):
        translate(Query(AUTHOR, manager="nothing"))
    assert translate(Query("library.Loan", manager="objects")).queryset.query.where


def test_ordering_and_distinct_on():
    query = Query(AUTHOR).order_by(F("born").asc(nulls_last=True), Lower("name").desc()).distinct("born")
    queryset = translate(query).queryset
    assert queryset.query.distinct_fields == ("born",)
    assert len(queryset.query.order_by) == 2


def test_django_errors_map_to_adorm_errors():
    from django.core.exceptions import FieldDoesNotExist, FieldError, MultipleObjectsReturned, ValidationError
    from django.db import transaction

    from adorm import exceptions

    not_found = map_django_error(dj.Author.DoesNotExist("Author matching query does not exist."))
    assert type(not_found) is exceptions.ObjectDoesNotExist
    assert str(not_found) == "Author matching query does not exist."
    assert type(map_django_error(MultipleObjectsReturned("two"))) is exceptions.MultipleObjectsReturned
    field = map_django_error(FieldError("no such field"))
    assert type(field) is exceptions.FieldError
    assert isinstance(field, exceptions.ProgrammingError)
    assert isinstance(field.original, FieldError)
    assert (field.sqlstate, field.model, field.key) == (None, None, None)
    assert type(map_django_error(FieldDoesNotExist("Author has no field named 'nope'"))) is exceptions.FieldError
    assert map_django_error(ValidationError({"code": ["bad"]})).message_dict == {"code": ["bad"]}
    single = map_django_error(ValidationError("“%(value)s” is not valid", code="invalid", params={"value": "x"}))
    assert (single.messages, single.code) == (["“x” is not valid"], "invalid")
    assert map_django_error(ValidationError(["a", "b"])).messages == ["a", "b"]
    tme = map_django_error(transaction.TransactionManagementError("nested"))
    assert type(tme) is exceptions.TransactionManagementError
    assert type(map_django_error(KeyError("x"))) is exceptions.DatabaseError


def _errors(error):
    """``{key: [(message, code, params), ...]}`` of an adorm ``ValidationError`` built from a dict."""
    return {key: [(e.message, e.code, e.params) for e in errors] for key, errors in error.error_dict.items()}


def test_mapped_validation_errors_keep_each_code_and_params():
    from django.core.exceptions import ValidationError

    exc = ValidationError(
        {
            "name": [
                ValidationError("This field cannot be blank.", code="blank"),
                ValidationError("At least %(limit_value)d.", code="min_length", params={"limit_value": 2}),
            ],
            "__all__": ValidationError("Constraint “x” is violated.", code="x_violated"),
        }
    )
    mapped = map_django_error(exc)
    assert _errors(mapped) == {
        "name": [
            ("This field cannot be blank.", "blank", None),
            ("At least %(limit_value)d.", "min_length", {"limit_value": 2}),
        ],
        "__all__": [("Constraint “x” is violated.", "x_violated", None)],
    }
    assert mapped.message_dict["name"] == ["This field cannot be blank.", "At least 2."]
    assert mapped.__cause__ is exc
    listed = map_django_error(ValidationError([ValidationError("a", code="one"), "b"]))
    assert [(e.message, e.code) for e in listed.error_list] == [("a", "one"), ("b", None)]


def test_mapped_validation_errors_turn_lazy_messages_and_django_objects_into_plain_values():
    from django.core.exceptions import ValidationError
    from django.utils.text import format_lazy
    from django.utils.translation import gettext_lazy

    tag = dj.Tag(id=1, name="rare")
    params = {"model": tag, "model_class": dj.Tag, "field_label": gettext_lazy("Name"), "unique_check": ("name",)}
    mapped = map_django_error(
        ValidationError(
            {"name": [ValidationError(gettext_lazy("%(field_label)s taken"), code="unique", params=params)]}
        )
    )
    error = mapped.error_dict["name"][0]
    assert type(error.message) is str
    assert error.params == {
        "model": "library.Tag",
        "model_class": "library.Tag",
        "field_label": "Name",
        "unique_check": ("name",),
    }
    assert type(error.params["field_label"]) is str
    assert mapped.messages == ["Name taken"]
    single = map_django_error(ValidationError(format_lazy("{} is invalid", "x"), code="invalid"))
    assert (type(single.message), single.code) == (str, "invalid")


def test_validation_errors_of_a_model_are_keyed_by_adorm_names():
    from django.core.exceptions import ValidationError

    exc = ValidationError(
        {
            "copy": [ValidationError("too high", code="max_value", params={"limit_value": 32767})],
            "reader": [ValidationError("missing", code="invalid", params={"field": "id", "value": 9})],
            "__all__": [
                ValidationError(
                    "%(model_name)s with this %(field_labels)s already exists.",
                    code="unique",
                    params={
                        "model_name": "Reservation",
                        "field_labels": "Book and Copy",
                        "unique_check": ("book", "copy"),
                    },
                )
            ],
        }
    )
    error = validation_error("library.Reservation", exc)
    assert _errors(error) == {
        "copy_": [("too high", "max_value", {"limit_value": 32767})],
        "reader": [("missing", "invalid", {"field": "id", "value": 9})],
        "__all__": [
            (
                "%(model_name)s with this %(field_labels)s already exists.",
                "unique",
                {"model_name": "Reservation", "field_labels": "Book and Copy", "unique_check": ("book", "copy_")},
            )
        ],
    }
    assert error.message_dict["__all__"] == ["Reservation with this Book and Copy already exists."]
    # Errors under a foreign key's name and under its column share the relation's key.
    note = validation_error("library.Note", ValidationError({"copy": ["bad"], "copy_id": ["worse"], "json": ["x"]}))
    assert note.message_dict == {"copy_": ["bad", "worse"], "json_": ["x"]}
    unknown = validation_error(None, ValidationError({"copy": ["bad"]}))
    assert unknown.message_dict == {"copy": ["bad"]}
    with pytest.raises(ProgrammingError):
        django_model("nowhere.Thing")
    assert validation_error("nowhere.Thing", ValidationError({"copy": ["bad"]})).message_dict == {"copy": ["bad"]}


def test_the_model_of_an_operation():
    from adorm import operations

    assert operation_model(operations.Create(model="library.Tag", values=())) == "library.Tag"
    assert operation_model(operations.Count(Query("library.Book"))) == "library.Book"
    assert operation_model(object()) is None


def test_protected_and_restricted_errors_carry_model_rows():
    from django.db.models import ProtectedError, RestrictedError

    from adorm import exceptions

    ann = dj.Author(id=1, name="Ann", slug="ann", rating=5)
    tag = dj.Tag.from_db("default", ["id"], [4])
    protected = map_django_error(ProtectedError("referenced", {ann}))
    assert type(protected) is exceptions.ProtectedError
    assert str(protected) == "referenced"
    assert protected.protected_objects == (ModelRow("library.Author", instance_row(ann)),)
    assert protected.protected_objects[0].row["name"] == "Ann"
    assert isinstance(protected.original, ProtectedError)
    assert protected.__cause__ is protected.original
    restricted = map_django_error(RestrictedError("restricted", [tag]))
    assert type(restricted) is exceptions.RestrictedError
    assert restricted.restricted_objects == (ModelRow("library.Tag", {"id": 4}),)


def test_adopted_context_locals_carry_the_language_and_time_zone_to_another_thread():
    seen = []

    def read():
        before = translation.get_language()
        adopt_context_locals()
        seen.append((before, translation.get_language(), str(timezone.get_current_timezone())))

    with translation.override("sl"), timezone.override(zoneinfo.ZoneInfo("Europe/Ljubljana")):
        context = contextvars.copy_context()
    thread = threading.Thread(target=context.run, args=(read,))
    thread.start()
    thread.join()
    assert seen[0][1:] == ("sl", "Europe/Ljubljana")
    assert seen[0][0] != "sl"  # asgiref hides another thread's values until they are adopted


def test_only_asgiref_storage_is_re_tagged():
    other = contextvars.ContextVar("asgiref.local")
    unrelated = contextvars.ContextVar("unrelated")
    other.set({"value": 1})
    tagged = type(
        "Tagged", (), {"__init__": lambda self, thread_id, data: vars(self).update(thread_id=thread_id, data=data)}
    )
    unrelated.set(tagged(0, {"value": 2}))
    adopt_context_locals()
    assert other.get() == {"value": 1}
    assert unrelated.get().thread_id == 0


# pinned_alias()


class _Elsewhere:
    def db_for_read(self, model, **hints):  # noqa: ARG002
        return "elsewhere"

    db_for_write = db_for_read


@pytest.fixture
def project_routers(monkeypatch):
    """Django's routers: one project router that sends every query to ``elsewhere``."""
    from django.db import router

    monkeypatch.setattr(router, "routers", [_Elsewhere()])
    return router


def test_a_pinned_alias_routes_reads_and_writes_ahead_of_the_projects_routers(project_routers):
    from adorm.drivers.django.translation import pinned_alias
    from tests.djangoproject.library import models as dj

    before = project_routers.routers
    with pinned_alias("second"):
        assert project_routers.db_for_read(dj.Author) == "second"
        assert project_routers.db_for_write(dj.Author, instance=dj.Author()) == "second"
        with pinned_alias("third"):
            assert project_routers.db_for_read(dj.Author) == "third"
        assert project_routers.db_for_read(dj.Author) == "second"
    assert project_routers.db_for_read(dj.Author) == "elsewhere"
    assert len(before) == 1  # replaced, not changed in place, while other threads may iterate it
    pinned = project_routers.routers
    with pinned_alias("second"):
        assert project_routers.routers is pinned


def test_a_pinned_alias_stays_in_its_thread(project_routers):
    from adorm.drivers.django.translation import pinned_alias
    from tests.djangoproject.library import models as dj

    inside, release = threading.Event(), threading.Event()
    seen = []

    def pinned():
        with pinned_alias("second"):
            inside.set()
            release.wait(5)
            seen.append(project_routers.db_for_read(dj.Author))

    thread = threading.Thread(target=pinned)
    thread.start()
    try:
        assert inside.wait(5)
        assert project_routers.db_for_read(dj.Author) == "elsewhere"
        context = contextvars.copy_context()
        assert context.run(project_routers.db_for_read, dj.Author) == "elsewhere"
    finally:
        release.set()
        thread.join()
    assert seen == ["second"]


def test_a_pinned_alias_survives_djangos_router_reset(project_routers):
    from django.db.utils import ConnectionRouter

    from adorm.drivers.django.translation import pinned_alias
    from tests.djangoproject.library import models as dj

    with pinned_alias("second"):
        pass
    project_routers.routers = ConnectionRouter().routers  # what override_settings(DATABASE_ROUTERS=...) does
    with pinned_alias("second"):
        assert project_routers.db_for_read(dj.Author) == "second"
    assert project_routers.db_for_read(dj.Author) == "default"
