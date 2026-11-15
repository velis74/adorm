"""Translation of adorm queries into Django query sets: no database connection is involved (pytest-django blocks
database access in these tests)."""

import contextvars
import threading
import zoneinfo

import pytest

from django.db.models.functions import Lower as DjangoLower
from django.utils import timezone, translation

from adorm import Count, Exists, F, Length, Lower, NotSupportedError, OuterRef, ProgrammingError, Q, Subquery
from adorm.drivers.django import adopt_context_locals, map_django_error
from adorm.drivers.django.translation import django_model, orm_path, output_field, subquery, translate
from adorm.query import Query
from adorm.query.lookups import Exact
from tests.djangoproject.library import models as dj
from tests.generated import library  # noqa: F401 - registers the adorm models

AUTHOR = "library.Author"
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
        (dj.Author, "name__nothing", "name__nothing"),
    ],
)
def test_orm_paths_rename_each_segment_on_the_model_it_reaches(model, path, expected):
    assert orm_path(model, path) == expected


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


@pytest.mark.parametrize(
    ("query", "message"),
    [
        (Query(AUTHOR).annotate(n=Count("books")), "Count"),
        (Query(AUTHOR).annotate(n=Length("name")), "Length"),
        (
            Query(AUTHOR).filter(rating=Subquery(Query(AUTHOR).filter(pk=OuterRef("mentor")).values("rating"))),
            "OuterRef",
        ),
        (Query(AUTHOR).filter(Exists(Query(AUTHOR))), "Exists"),
        (Query(AUTHOR).select_for_update(), "select_for_update"),
    ],
)
def test_nodes_without_a_translation_raise_not_supported(query, message):
    with pytest.raises(NotSupportedError, match=message):
        translate(query)


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
    from django.core.exceptions import FieldError, MultipleObjectsReturned, ValidationError
    from django.db import transaction

    from adorm import exceptions

    not_found = map_django_error(dj.Author.DoesNotExist("Author matching query does not exist."))
    assert type(not_found) is exceptions.ObjectDoesNotExist
    assert str(not_found) == "Author matching query does not exist."
    assert type(map_django_error(MultipleObjectsReturned("two"))) is exceptions.MultipleObjectsReturned
    field = map_django_error(FieldError("no such field"))
    assert type(field) is exceptions.ProgrammingError
    assert isinstance(field.original, FieldError)
    assert map_django_error(ValidationError({"code": ["bad"]})).message_dict == {"code": ["bad"]}
    single = map_django_error(ValidationError("“%(value)s” is not valid", code="invalid", params={"value": "x"}))
    assert (single.messages, single.code) == (["“x” is not valid"], "invalid")
    assert map_django_error(ValidationError(["a", "b"])).messages == ["a", "b"]
    tme = map_django_error(transaction.TransactionManagementError("nested"))
    assert type(tme) is exceptions.TransactionManagementError
    assert type(map_django_error(KeyError("x"))) is exceptions.DatabaseError


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
