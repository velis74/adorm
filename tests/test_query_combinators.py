"""Set operations on `Query` and `QuerySet`: building, the clauses refused after them, encoding, the operations that
refuse a combined query, and their translation into Django query sets (no database connection)."""

import datetime
import decimal
import json

import pytest

from adorm import DatabaseError, NotSupportedError, ProgrammingError
from adorm.drivers.django.translation import subquery, translate
from adorm.operations import (
    Aggregate,
    Count as CountOperation,
    Delete,
    Exists as ExistsOperation,
    First,
    Get,
    GetOrCreate,
    InBulk,
    Last,
    Operation,
    Select,
    Update,
    UpdateOrCreate,
)
from adorm.query import CLAUSES, COMBINATORS, construct_names, Count, Exists, F, OuterRef, Query, Subquery
from adorm.query.lookups import Exact
from tests.club_schema import SCHEMA
from tests.test_queryset import Executor, Player, players

MEMBERS = Query("club.Member")
ADULTS = MEMBERS.filter(age__gte=18).order_by("name")
MASTERS = MEMBERS.filter(name__startswith="M").order_by("age")
AUTHOR = "library.Author"


def roundtrip(data):
    return json.loads(json.dumps(data, allow_nan=False))


# Building


@pytest.mark.parametrize("combinator", COMBINATORS)
def test_a_set_operation_combines_the_queries(combinator):
    combined = getattr(ADULTS, combinator)(MASTERS)
    assert combined.combinator == combinator
    assert combined.is_combined
    assert combined.combinator_all is False
    assert combined.model == "club.Member"
    # The first query keeps its ordering; an unsliced other query loses it.
    assert combined.combined_queries == (ADULTS, MASTERS.order_by())
    assert (combined.filters, combined.ordering, combined.select, combined.row_format) == ((), (), (), "model")
    assert not ADULTS.is_combined
    assert ADULTS.combinator is None
    assert ADULTS.combined_queries == ()


def test_a_sliced_other_query_keeps_its_ordering():
    youngest = MASTERS[:2]
    assert ADULTS.union(youngest).combined_queries == (ADULTS, youngest)
    distinct_on = MEMBERS.order_by("club_id", "age").distinct("club_id")
    assert ADULTS.union(distinct_on).combined_queries[1] == distinct_on


def test_union_all_keeps_duplicates_and_several_queries_combine_at_once():
    third = MEMBERS.filter(name="Ana")
    combined = ADULTS.union(MASTERS, third, all=True)
    assert combined.combinator_all is True
    assert len(combined.combined_queries) == 3
    assert combined != ADULTS.union(MASTERS, third)


def test_the_combined_rows_take_the_first_querys_row_format():
    names = MEMBERS.values_list("name", flat=True)
    combined = names.union(Query("club.Club").values_list("name", flat=True))
    assert (combined.select, combined.row_format) == (("name",), "flat")
    assert combined.combined_queries[1].model == "club.Club"


def test_set_operations_nest():
    inner = ADULTS.union(MASTERS)
    outer = inner.intersection(MEMBERS.filter(club_id=1))
    assert outer.combined_queries[0] == inner
    assert set(outer.constructs()) == {
        "clause.intersection",
        "clause.union",
        "lookup.gte",
        "lookup.startswith",
        "lookup.exact",
    }


def test_a_set_operation_is_immutable_and_compared_by_value():
    first = ADULTS.difference(MASTERS)
    second = ADULTS.difference(MASTERS)
    assert first == second
    assert hash(first) == hash(second)
    assert first != ADULTS.union(MASTERS)
    assert first != MASTERS.difference(ADULTS)
    with pytest.raises(AttributeError):
        first.combinator = "union"


def test_union_with_no_other_query_is_the_query_itself():
    assert ADULTS.union() is ADULTS
    assert ADULTS.union(all=True) is ADULTS


def test_intersection_and_difference_with_no_other_query_combine_the_query_alone():
    assert ADULTS.intersection().combined_queries == (ADULTS,)
    assert ADULTS.difference().combined_queries == (ADULTS,)


@pytest.mark.parametrize("empty", [MEMBERS.none(), MEMBERS[2:2]], ids=["none", "empty_slice"])
def test_an_empty_query_in_a_union_stays_and_an_empty_first_query_gives_way(empty):
    assert ADULTS.union(empty).combined_queries == (ADULTS, empty)
    assert ADULTS.union(empty, MASTERS).combined_queries == (ADULTS, empty, MASTERS.order_by())
    assert empty.union(ADULTS) is ADULTS
    assert empty.union(empty, ADULTS, MASTERS).combined_queries == (ADULTS, MASTERS.order_by())
    assert empty.union(empty) is empty
    assert empty.union() is empty


@pytest.mark.parametrize("empty", [MEMBERS.none(), MEMBERS[2:2]], ids=["none", "empty_slice"])
def test_an_empty_query_makes_an_intersection_empty(empty):
    assert ADULTS.intersection(MASTERS, empty) is empty
    assert empty.intersection(ADULTS) is empty


@pytest.mark.parametrize("empty", [MEMBERS.none(), MEMBERS[2:2]], ids=["none", "empty_slice"])
def test_a_difference_keeps_empty_other_queries_and_an_empty_first_query(empty):
    assert ADULTS.difference(empty, MASTERS).combined_queries == (ADULTS, empty, MASTERS.order_by())
    assert empty.difference(ADULTS) is empty


@pytest.mark.parametrize("combinator", COMBINATORS)
def test_a_set_operation_takes_queries_only(combinator):
    with pytest.raises(TypeError, match="combines queries, not str"):
        getattr(ADULTS, combinator)("club.Member")


# After a set operation


COMBINED = ADULTS.union(MASTERS)


@pytest.mark.parametrize(
    ("action", "build"),
    [
        ("filter", lambda q: q.filter(age=3)),
        ("exclude", lambda q: q.exclude(age=3)),
        ("annotate", lambda q: q.annotate(n=Count("pk"))),
        ("alias", lambda q: q.alias(n=Count("pk"))),
        ("distinct", lambda q: q.distinct()),
        ("only", lambda q: q.only("name")),
        ("defer", lambda q: q.defer("name")),
        ("select_related", lambda q: q.select_related("club")),
        ("select_for_update", lambda q: q.select_for_update()),
    ],
)
@pytest.mark.parametrize("combinator", COMBINATORS)
def test_clauses_after_a_set_operation_are_not_supported(combinator, action, build):
    combined = getattr(ADULTS, combinator)(MASTERS)
    with pytest.raises(NotSupportedError, match=rf"cannot call {action}\(\) after {combinator}\(\)"):
        build(combined)
    with pytest.raises(NotSupportedError, match=rf"after {combinator}\(\)"):
        build(combined.order_by("name")[:3].values("name"))


def test_the_and_and_or_operators_refuse_a_combined_query():
    with pytest.raises(TypeError, match=r"\| operator with a combined query"):
        COMBINED | ADULTS  # noqa: B018
    with pytest.raises(TypeError, match="& operator with a combined query"):
        ADULTS & COMBINED  # noqa: B018


def test_ordering_slicing_values_and_none_apply_to_the_combined_rows():
    q = COMBINED.order_by("-name")[2:5].values_list("name", "age")
    assert q.ordering[0].expression == F("name")
    assert q.ordering[0].descending
    assert (q.low_mark, q.high_mark) == (2, 5)
    assert (q.select, q.row_format) == (("name", "age"), "tuple")
    assert q.combined_queries == COMBINED.combined_queries
    assert COMBINED.none().is_empty
    assert COMBINED.none().is_combined
    with pytest.raises(TypeError, match="reorder"):
        q.order_by("name")


def test_construct_names_of_a_set_operation():
    assert {"clause.union", "clause.intersection", "clause.difference"} <= set(CLAUSES) <= construct_names()
    assert set(COMBINED.constructs()) == {"clause.union", "lookup.gte", "lookup.startswith"}
    assert "clause.difference" in set(MEMBERS.difference(MEMBERS).constructs())
    assert "path.nested" in set(MEMBERS.values("club__name").union(MEMBERS).constructs())


def test_repr_shows_the_combination():
    assert repr(MEMBERS.union(MEMBERS, all=True)) == (
        "Query('club.Member', union=(Query('club.Member'), Query('club.Member')), all=True)"
    )


# Encoding


ENCODED = [
    COMBINED,
    ADULTS.union(MASTERS, all=True).order_by("-name")[:3],
    MEMBERS.filter(fee__gt=decimal.Decimal("1.5")).intersection(
        MEMBERS.filter(joined__lt=datetime.datetime(2026, 1, 1))
    ),
    MEMBERS.values_list("name", flat=True).difference(Query("club.Club").values_list("name", flat=True)),
    COMBINED.difference(MEMBERS.filter(name="Ana")).values("name"),
    COMBINED.none(),
    MEMBERS.filter(Exists(Query("club.Payment").union(Query("club.Payment").filter(amount__gt=decimal.Decimal(5))))),
]


@pytest.mark.parametrize("query", ENCODED)
@pytest.mark.parametrize("schema", [None, SCHEMA], ids=["untyped", "typed"])
def test_combined_queries_round_trip(query, schema):
    data = roundtrip(query.to_dict(schema))
    assert Query.from_dict(data, schema) == query


def test_the_combined_queries_are_encoded_by_their_own_models_types():
    query = MEMBERS.filter(fee__gt=decimal.Decimal("1.5")).union(Query("club.Club").filter(founded="2001-02-03"))
    data = query.to_dict(SCHEMA)
    member, club = data["combined_queries"]
    assert member["filters"][0]["children"][0]["rhs"] == {"value": "1.5"}
    assert club["filters"][0]["children"][0]["rhs"] == {"value": "2001-02-03"}
    restored = Query.from_dict(roundtrip(data), SCHEMA)
    assert restored.combined_queries[1].filters[0].children[0].rhs == datetime.date(2001, 2, 3)


def test_an_outer_reference_in_a_combined_query_resolves_against_the_enclosing_query():
    # "joined" is a field of club.Member, the enclosing query, and not of club.Payment.
    referring = Query("club.Payment").filter(Exact(OuterRef("joined"), "2026-01-01T00:00:00"))
    query = MEMBERS.filter(Exists(referring.union(Query("club.Payment").filter(amount__gt=5))))
    restored = Query.from_dict(roundtrip(query.to_dict(SCHEMA)), SCHEMA)
    part = restored.filters[0].children[0].query.combined_queries[0]
    assert part.filters[0].children[0].rhs == datetime.datetime(2026, 1, 1)


def _encoded(**changes):
    data = roundtrip(COMBINED.to_dict())
    data.update(changes)
    return data


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (_encoded(combinator="unite"), "unknown combinator"),
        (_encoded(combinator=None), "need a combinator"),
        (_encoded(combined_queries=[]), "has combined_queries"),
        (_encoded(combined_queries={"node": "Query"}), "combined queries of a query are a list"),
        (_encoded(combined_queries=[{"node": "F", "name": "a"}]), "list of queries"),
        (_encoded(combinator="intersection", combinator_all=True), "combinator_all applies to a union"),
        (_encoded(combinator_all="yes"), "combinator_all is a bool"),
        (_encoded(model="club.Club"), "model of its first query"),
        (_encoded(manager="objects"), "no manager of its own"),
        (_encoded(is_distinct=True), "no is_distinct of its own"),
        (_encoded(filters=roundtrip(ADULTS.to_dict())["filters"], filter_marks=[0]), "no filters of its own"),
    ],
)
def test_decoding_refuses_an_invalid_combination(data, message):
    with pytest.raises(ValueError, match=message):
        Query.from_dict(data)


def test_a_plain_query_decodes_without_a_combination():
    data = roundtrip(MEMBERS.to_dict())
    assert (data["combinator"], data["combinator_all"], data["combined_queries"]) == (None, False, [])
    with pytest.raises(ValueError, match="need a combinator"):
        Query.from_dict({**data, "combined_queries": [roundtrip(MEMBERS.to_dict())]})


# Operations


@pytest.mark.parametrize(
    "operation",
    [Select, Get, First, Last, CountOperation, ExistsOperation],
    ids=lambda cls: cls.kind,
)
def test_reads_take_a_combined_query(operation):
    built = operation(COMBINED.order_by("name"))
    assert "clause.union" in set(built.constructs())
    assert Operation.from_dict(roundtrip(built.to_dict(SCHEMA)), SCHEMA) == built


@pytest.mark.parametrize(
    ("action", "build"),
    [
        ("aggregate", lambda q: Aggregate(q, {"n": Count("pk")})),
        ("update", lambda q: Update(q, {"name": "x"})),
        ("delete", Delete),
        ("in_bulk", InBulk),
        ("get_or_create", lambda q: GetOrCreate(q, {"name": "x"})),
        ("update_or_create", lambda q: UpdateOrCreate(q, {"name": "x"})),
    ],
)
def test_operations_refuse_a_combined_query(action, build):
    with pytest.raises(NotSupportedError, match=rf"cannot call {action}\(\) after union\(\)"):
        build(COMBINED)


def test_a_default_refers_to_a_field_through_an_outer_reference_in_a_combined_query():
    referring = Query("club.Payment").filter(member_id=OuterRef("pk")).values("amount")
    plain = Query("club.Payment").values("amount")
    with pytest.raises(TypeError, match="cannot refer to a field"):
        GetOrCreate(MEMBERS, {"name": "x"}, {"fee": Subquery(referring.union(plain)[:1])})
    # The ordering of the combined rows names their columns, not fields of the written row.
    GetOrCreate(MEMBERS, {"name": "x"}, {"fee": Subquery(plain.union(plain).order_by("amount")[:1])})


# Query sets


async def test_a_query_set_set_operation_combines_the_queries():
    executor = Executor([{"id": 1, "name": "Ana"}], 4)
    cheap = players(executor).filter(fee__lt=5)
    named = players(executor).filter(name__startswith="A")
    combined = cheap.union(named, all=True)
    assert type(combined) is type(cheap)
    assert combined.query == cheap.query.union(named.query, all=True)
    assert cheap.intersection(named).query == cheap.query.intersection(named.query)
    assert cheap.difference(named).query == cheap.query.difference(named.query)
    rows = [row async for row in combined.order_by("name")]
    assert rows == [Player(id=1, name="Ana")]
    assert await combined.count() == 4
    assert [type(op) for op in executor.operations] == [Select, CountOperation]
    assert executor.operations[0].query == combined.order_by("name").query


async def test_query_set_rows_after_values_list_of_a_combination():
    executor = Executor([{"name": "Ana"}, {"name": "Bor"}])
    qs = players(executor).union(players(executor).filter(fee__gt=1)).values_list("name", flat=True)
    assert [row async for row in qs] == ["Ana", "Bor"]


def test_query_set_set_operations_take_query_sets_of_one_session():
    executor = Executor()
    with pytest.raises(TypeError, match="combines query sets, not Query"):
        players(executor).union(Query("querysettest.Player"))
    with pytest.raises(TypeError, match="different sessions"):
        players(executor).intersection(players(Executor()))


@pytest.mark.parametrize("make_empty", [lambda qs: qs.none(), lambda qs: qs[1:1]], ids=["none", "empty_slice"])
def test_query_set_set_operations_with_an_empty_query_set_return_the_query_set_that_decides(make_empty):
    executor = Executor()
    empty = make_empty(players(executor))
    some = players(executor).filter(fee__gt=1)
    assert empty.union(some) is some
    assert empty.union(empty).query == empty.query
    assert some.intersection(empty) is empty
    assert empty.intersection(some).query == empty.query
    assert empty.difference(some).query == empty.query


async def test_query_set_methods_refused_after_a_set_operation_run_no_operation():
    executor = Executor()
    combined = players(executor).union(players(executor))
    for build in (
        lambda: combined.filter(name="Ana"),
        lambda: combined.prefetch_related("team"),
        lambda: combined.select_related("team"),
    ):
        with pytest.raises(NotSupportedError):
            build()
    for call in (
        combined.get(name="Ana"),
        combined.aggregate(n=Count("pk")),
        combined.update(name="x"),
        combined.delete(),
        combined.in_bulk([1]),
        combined.get_or_create(name="x"),
    ):
        with pytest.raises(NotSupportedError):
            await call
    assert executor.operations == []


async def test_query_set_get_without_conditions_runs_on_a_combination():
    executor = Executor({"id": 1, "name": "Ana"})
    combined = players(executor).union(players(executor).filter(fee__gt=1))
    assert await combined.get() == Player(id=1, name="Ana")
    assert executor.operations == [Get(combined.query)]


async def test_an_empty_combination_runs_no_operation():
    executor = Executor()
    combined = players(executor).union(players(executor)).none()
    assert [row async for row in combined] == []
    assert await combined.count() == 0
    assert executor.operations == []


# Translation into Django query sets


def test_union_translates_into_a_django_union():
    query = Query(AUTHOR).filter(rating__gt=3).union(Query(AUTHOR).filter(name="Bob"), all=True).order_by("-name")
    translated = translate(query.values_list("name", flat=True)[:2])
    sql = str(translated.queryset.query)
    assert "UNION ALL" in sql
    assert sql.endswith("ORDER BY 1 DESC LIMIT 2")
    assert translated.row(("Ann",)) == {"name": "Ann"}


@pytest.mark.parametrize(("combinator", "keyword"), [("intersection", "INTERSECT"), ("difference", "EXCEPT")])
def test_intersection_and_difference_translate_into_their_sql(combinator, keyword):
    query = getattr(Query(AUTHOR), combinator)(Query(AUTHOR).filter(rating=1))
    sql = str(translate(query).queryset.query)
    assert f" {keyword} " in sql
    assert "UNION" not in sql


def test_combined_model_rows_are_built_as_the_first_querys_rows():
    query = Query(AUTHOR).only("name").union(Query(AUTHOR).only("name"))
    translated = translate(query)
    assert translated.columns == ("id", "name")
    assert translated.row((1, "Ann")) == {"id": 1, "name": "Ann"}


def test_values_after_a_set_operation_select_the_names_in_every_query_of_model_rows():
    query = Query(AUTHOR).union(Query(AUTHOR).filter(rating=1), Query(AUTHOR).values_list("slug")).values("name")
    translated = translate(query)
    sql = str(translated.queryset.query)
    assert sql.count('"library_author"."name"') == 2
    assert sql.count('"library_author"."slug"') == 1
    assert translated.row(("Ann",)) == {"name": "Ann"}


def test_values_after_a_set_operation_key_the_rows_column_by_column():
    query = Query(AUTHOR).values_list("slug").union(Query(AUTHOR).values_list("name")).values_list("label")
    assert translate(query).row(("ann",)) == {"label": "ann"}
    with pytest.raises(ProgrammingError, match="names 2 columns, the combined queries select 1"):
        translate(Query(AUTHOR).values_list("slug").union(Query(AUTHOR).values_list("name")).values_list("a", "b"))


def test_a_combined_subquery_selects_the_primary_key_of_model_rows():
    combined = Query(AUTHOR).filter(rating=5).union(Query(AUTHOR).filter(mentor=OuterRef("pk")))
    sql = str(translate(Query(AUTHOR).filter(Exists(combined))).queryset.query)
    inner = sql.split("EXISTS", 1)[1]
    assert "UNION" in inner
    assert '"library_author"."name"' not in inner
    assert "UNION" in str(subquery(Query(AUTHOR).union(Query(AUTHOR)).values("slug")).query)


def test_an_empty_combination_translates_into_an_empty_query_set():
    query = Query(AUTHOR).union(Query(AUTHOR).filter(rating=1)).none()
    assert translate(query).queryset.query.is_empty()


def test_a_union_whose_other_queries_are_empty_stays_a_union_after_ordering_and_slicing():
    first = Query(AUTHOR).order_by("name")[:3]
    query = first.union(Query(AUTHOR).none()).order_by("slug")[:2]
    sql = str(translate(query.values_list("slug", flat=True)).queryset.query)
    assert " UNION " in sql
    assert sql.endswith("ORDER BY 1 ASC LIMIT 2")


@pytest.mark.parametrize("combinator", ["intersection", "difference"])
def test_a_set_operation_of_one_query_translates_into_its_select(combinator):
    query = getattr(Query(AUTHOR).values_list("name", flat=True), combinator)()
    sql = str(translate(query).queryset.query)
    assert "INTERSECT" not in sql
    assert "EXCEPT" not in sql


def test_ordering_by_a_renamed_column_orders_by_the_first_querys_column():
    combined = Query(AUTHOR).values_list("slug").union(Query(AUTHOR).values_list("name"))
    sql = str(translate(combined.values_list("label", flat=True).order_by("-label")).queryset.query)
    assert sql.endswith("ORDER BY 1 DESC")
    nested = combined.union(Query(AUTHOR).values_list("name")).values_list("tag").order_by("tag")
    assert str(translate(nested).queryset.query).endswith("ORDER BY 1 ASC")


def test_ordering_by_a_name_the_combined_rows_lack_raises_database_error():
    combined = Query(AUTHOR).values_list("slug").union(Query(AUTHOR).values_list("name")).values_list("label")
    with pytest.raises(DatabaseError, match="ORDER BY term does not match any column") as info:
        translate(combined.order_by("slug"))
    assert type(info.value) is DatabaseError
