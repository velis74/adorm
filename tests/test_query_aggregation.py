"""Annotations, aliases, grouping and aggregation on `Query`, the `Aggregate` operation and `QuerySet.aggregate()`."""

import asyncio
import datetime
import decimal

import pytest

from adorm import (
    Avg,
    Count,
    create_session,
    F,
    Lower,
    Max,
    Model,
    ModelMeta,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    Sum,
    TruncMonth,
    Value,
)
from adorm.operations import Aggregate, First, Last, Operation, Update
from adorm.query import Query
from tests.club_schema import SCHEMA

PAYMENT = "club.Payment"


class Payment(Model):
    _meta = ModelMeta(PAYMENT)
    id: int | None = None
    member_id: int
    amount: decimal.Decimal
    paid_at: datetime.datetime


class Executor:
    """Records operations and answers each with the next queued result."""

    def __init__(self, *results):
        self.operations: list[Operation] = []
        self.results = list(results)

    async def execute(self, operation):
        self.operations.append(operation)
        return self.results.pop(0)


def payments(executor) -> QuerySet:
    return QuerySet(Payment, executor)


# alias()


def test_alias_adds_an_annotation_that_is_not_selected():
    query = Query(PAYMENT).alias(month=TruncMonth("paid_at")).filter(month__gte=datetime.date(2026, 1, 1))
    assert query.annotations == (("month", TruncMonth("paid_at")),)
    assert query.aliases == ("month",)
    assert set(query.constructs()) >= {"clause.alias", "function.TruncMonth"}
    assert "clause.annotate" not in set(query.constructs())
    both = query.annotate(double=F("amount") * 2)
    assert {"clause.alias", "clause.annotate"} <= set(both.constructs())
    assert both.aliases == ("month",)


def test_an_alias_cannot_be_selected():
    query = Query(PAYMENT).alias(month=TruncMonth("paid_at"))
    with pytest.raises(ValueError, match="cannot select the alias 'month'"):
        query.values("amount", "month")
    with pytest.raises(ValueError, match="cannot select the alias 'month'"):
        query.values_list("month", flat=True)
    assert query.values_list("amount", flat=True).select == ("amount",)


def test_alias_names_follow_annotation_rules():
    query = Query(PAYMENT).annotate(total=Sum("amount"))
    with pytest.raises(ValueError, match="already exists"):
        query.alias(total=Count("pk"))
    with pytest.raises(ValueError, match="not an annotation name"):
        query.alias(a__b=Count("pk"))
    with pytest.raises(TypeError, match="not an expression"):
        query.alias(n=1)
    assert query.alias() is query
    assert query.annotate() is query


def test_an_alias_does_not_change_a_combination_unless_both_have_it():
    annotated = Query(PAYMENT).annotate(n=Count("pk"))
    aliased = Query(PAYMENT).alias(n=Count("pk"))
    with pytest.raises(TypeError, match="same aliases"):
        annotated.filter(amount__gt=1) | aliased.filter(amount__lt=5)
    assert (aliased.filter(amount__gt=1) | aliased.filter(amount__lt=5)).aliases == ("n",)


# Grouping with values().annotate()


def test_values_then_an_aggregate_groups_by_the_selected_names():
    query = Query(PAYMENT).values("member_id").annotate(total=Sum("amount"), n=Count("*"))
    assert query.group_by == ("member_id",)
    assert query.select == ("member_id", "total", "n")
    assert query.is_grouped
    assert "clause.group_by" in set(query.constructs())


def test_annotate_then_values_does_not_group():
    query = Query(PAYMENT).annotate(total=Sum("amount")).values("member_id", "total")
    assert query.group_by is None
    assert "clause.group_by" not in set(query.constructs())


def test_an_annotation_before_values_is_a_grouping_column():
    query = (
        Query(PAYMENT)
        .annotate(month=TruncMonth("paid_at"))
        .values("month")
        .annotate(total=Sum("amount"))
        .order_by("month")
    )
    assert query.group_by == ("month",)
    assert query.select == ("month", "total")


def test_new_plain_annotations_of_the_grouping_call_join_the_group():
    query = Query(PAYMENT).values("member_id").annotate(day=TruncMonth("paid_at"), total=Sum("amount"))
    assert query.group_by == ("member_id", "day")


def test_an_annotation_referring_to_an_aggregate_does_not_join_the_group():
    query = Query(PAYMENT).values("member_id").annotate(total=Sum("amount")).annotate(double=F("total") * 2)
    assert query.group_by == ("member_id",)
    assert query.aggregate_names() == {"total", "double"}
    assert query.select == ("member_id", "total", "double")


def test_values_without_fields_does_not_group():
    query = Query(PAYMENT).values().annotate(total=Sum("amount"))
    assert query.group_by is None
    assert query.select == ()


def test_plain_annotations_after_values_add_columns_without_grouping():
    query = Query(PAYMENT).values("member_id").annotate(lower=Lower("member_id"))
    assert query.group_by is None
    assert query.select == ("member_id", "lower")


def test_an_aggregate_alias_after_values_groups_without_selecting():
    query = Query(PAYMENT).values("member_id").alias(n=Count("*")).filter(n__gt=1)
    assert query.group_by == ("member_id",)
    assert query.select == ("member_id",)


def test_grouping_survives_later_values_calls():
    grouped = Query(PAYMENT).values("member_id").annotate(total=Sum("amount"))
    assert grouped.values("total").group_by == ("member_id",)
    assert grouped.values_list("total", flat=True).group_by == ("member_id",)
    assert grouped.annotate(n=Count("*")).group_by == ("member_id",)


def test_a_subquerys_aggregates_do_not_make_an_aggregate():
    inner = Query(PAYMENT).filter(member_id=OuterRef("member_id")).values("member_id").annotate(s=Sum("amount"))
    query = Query(PAYMENT).values("member_id").annotate(member_total=Subquery(inner.values("s")))
    assert query.group_by is None
    assert query.aggregate_names() == frozenset()


def test_grouped_queries_need_an_ordering_for_first_and_last():
    grouped = Query(PAYMENT).values("member_id").annotate(total=Sum("amount"))
    for operation in (First, Last):
        with pytest.raises(TypeError, match="grouped query without an ordering"):
            operation(grouped)
        assert operation(grouped.order_by("member_id")).query.is_grouped


def test_a_grouped_query_cannot_be_updated():
    with pytest.raises(TypeError, match="cannot update a grouped query"):
        Update(Query(PAYMENT).values("member_id").annotate(total=Sum("amount")), {"amount": 1})


def test_grouping_and_aliases_round_trip_through_the_encoding():
    query = (
        Query(PAYMENT)
        .alias(month=TruncMonth("paid_at"))
        .annotate(month_shown=F("month"))
        .values("month_shown")
        .annotate(total=Sum("amount", filter=Q(amount__gt=1)))
        .filter(total__gt=decimal.Decimal("10"))
    )
    data = query.to_dict()
    assert data["aliases"] == ["month"]
    assert data["group_by"] == ["month_shown"]
    assert Query.from_dict(data) == query


def test_filters_record_how_many_annotations_preceded_them():
    query = (
        Query(PAYMENT)
        .filter(amount__gt=1)
        .annotate(n=Count("*"))
        .exclude(member_id=2)
        .alias(m=Max("amount"))
        .filter(m__lt=9)
    )
    assert query.filter_marks == (0, 1, 2)
    assert Query.from_dict(query.to_dict()) == query
    assert query != Query(PAYMENT).annotate(n=Count("*")).filter(amount__gt=1).exclude(member_id=2)


def test_combined_queries_keep_or_merge_the_filter_marks():
    base = Query(PAYMENT).annotate(n=Count("*"))
    before, after = Query(PAYMENT).filter(member_id=1).annotate(n=Count("*")), base.filter(member_id=2)
    assert (before & after).filter_marks == (0, 1)
    assert (before | after).filter_marks == (1,)
    assert (before | base).filter_marks == ()


def test_grouping_records_the_annotations_added_before_it():
    query = Query(PAYMENT).annotate(month=TruncMonth("paid_at")).values("month").annotate(total=Sum("amount"))
    assert query.group_mark == 1
    assert Query(PAYMENT).values("member_id").annotate(n=Count("*")).group_mark == 0


def test_plain_annotations_after_grouping_join_the_group():
    grouped = Query(PAYMENT).values("member_id").annotate(n=Count("*"))
    query = grouped.annotate(day=TruncMonth("paid_at"))
    assert query.group_by == ("member_id", "day")
    assert query.select == ("member_id", "n", "day")
    assert grouped.alias(day=TruncMonth("paid_at")).group_by == ("member_id",)
    assert Query.from_dict(query.to_dict()) == query


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"filter_marks": []}, "one per filter"),
        ({"filter_marks": [2]}, "one per filter"),
        ({"filter_marks": [-1]}, "one per filter"),
        ({"group_mark": None}, "group_mark is set exactly when group_by is"),
        ({"group_mark": 5}, "index of an annotation"),
    ],
)
def test_decoding_refuses_malformed_marks(change, message):
    query = Query(PAYMENT).filter(amount__gt=1).values("member_id").annotate(n=Count("*"))
    with pytest.raises(ValueError, match=message):
        Query.from_dict(query.to_dict() | change)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"aliases": ["nothing"]}, "aliases are names of annotations"),
        ({"aliases": ["n", "n"]}, "named once"),
        ({"group_by": "member_id"}, "group_by is a list"),
        ({"group_by": ["no such"]}, "not a field path"),
    ],
)
def test_decoding_refuses_malformed_aliases_and_grouping(change, message):
    data = Query(PAYMENT).alias(n=Count("*")).to_dict() | change
    with pytest.raises(ValueError, match=message):
        Query.from_dict(data)


def test_decoding_refuses_a_selected_alias():
    data = Query(PAYMENT).alias(n=Count("*")).to_dict() | {"select": ["n"], "row_format": "dict"}
    with pytest.raises(ValueError, match="not selected"):
        Query.from_dict(data)


# The Aggregate operation


def test_the_aggregate_operation_names_its_constructs():
    operation = Aggregate(Query(PAYMENT).filter(amount__gt=1), {"total": Sum("amount", filter=Q(member_id=1))})
    assert operation.kind == "aggregate"
    assert operation.model == PAYMENT
    assert operation.aggregations == (("total", Sum("amount", filter=Q(member_id=1))),)
    assert set(operation.constructs()) == {"lookup.gt", "lookup.exact", "aggregate.Sum", "aggregate.filter"}


@pytest.mark.parametrize(
    ("aggregations", "error", "message"),
    [
        ({}, TypeError, "at least one"),
        ({"n": F("amount")}, TypeError, "not an aggregate expression"),
        ({"n": 5}, TypeError, "not an aggregate expression"),
        ({"n": Subquery(Query(PAYMENT).annotate(s=Sum("amount")).values("s"))}, TypeError, "not an aggregate"),
        ({"a b": Count("*")}, ValueError, "not an aggregation name"),
        ({"": Count("*")}, ValueError, "not an aggregation name"),
        ({"x;": Count("*")}, ValueError, "not an aggregation name"),
        ({"a--b": Count("*")}, ValueError, "not an aggregation name"),
        ({"a/*b": Count("*")}, ValueError, "not an aggregation name"),
        ({"a*/b": Count("*")}, ValueError, "not an aggregation name"),
        ({"a#b": Count("*")}, ValueError, "not an aggregation name"),
        ({"a`b": Count("*")}, ValueError, "not an aggregation name"),
        ({"a[b]": Count("*")}, ValueError, "not an aggregation name"),
        ({"a\x07b": Count("*")}, ValueError, "not an aggregation name"),
        ([("n", Count("*")), ("n", Sum("amount"))], ValueError, "given twice"),
    ],
)
def test_the_aggregate_operation_refuses(aggregations, error, message):
    with pytest.raises(error, match=message):
        Aggregate(Query(PAYMENT), aggregations)


def test_aggregations_may_combine_aggregates_and_name_paths():
    operation = Aggregate(Query(PAYMENT), {"amount__sum": Sum("amount"), "spread": Max("amount") - Avg("amount")})
    assert [name for name, _ in operation.aggregations] == ["amount__sum", "spread"]


def test_the_aggregate_operation_round_trips_with_typed_values():
    query = Query("club.Member").annotate(paid=Sum("payment__amount", default=decimal.Decimal("0")))
    operation = Aggregate(query, {"rich": Count("*", filter=Q(paid__gt=decimal.Decimal("100.50"))), "top": Max("paid")})
    data = operation.to_dict(SCHEMA)
    assert data["aggregations"][0][0] == "rich"
    decoded = Operation.from_dict(data, SCHEMA)
    assert decoded == operation
    (_, rich), _ = decoded.aggregations
    assert type(rich.filter.children[0].rhs) is decimal.Decimal


def test_decoding_refuses_malformed_aggregations():
    data = Aggregate(Query(PAYMENT), {"n": Count("*")}).to_dict()
    with pytest.raises(ValueError, match="list of"):
        Aggregate.from_dict(data | {"aggregations": {"n": 1}})
    with pytest.raises(ValueError, match="pair"):
        Aggregate.from_dict(data | {"aggregations": [["n"]]})
    with pytest.raises(ValueError, match="not an aggregate"):
        Aggregate.from_dict(data | {"aggregations": [["n", F("amount").to_dict()]]})


# Default names


def test_default_aliases():
    assert Sum("amount").default_alias == "amount__sum"
    assert Count("member__club__name").default_alias == "member__club__name__count"
    for expression in (Count("*"), Sum(F("amount") * 2), Max(OuterRef("amount")), Avg(Lower("name"))):
        with pytest.raises(TypeError, match="no default name"):
            expression.default_alias  # noqa: B018 - the property raises


# QuerySet.aggregate()


async def test_aggregate_sends_one_operation_and_returns_a_dict():
    executor = Executor({"amount__sum": decimal.Decimal("5"), "n": 2})
    result = await payments(executor).filter(member_id=1).aggregate(Sum("amount"), n=Count("*"))
    assert result == {"amount__sum": decimal.Decimal("5"), "n": 2}
    (operation,) = executor.operations
    assert operation == Aggregate(Query(PAYMENT).filter(member_id=1), {"amount__sum": Sum("amount"), "n": Count("*")})


async def test_aggregate_refuses_bad_arguments_without_an_operation():
    executor = Executor()
    with pytest.raises(TypeError, match="positional argument"):
        await payments(executor).aggregate(F("amount"))
    with pytest.raises(TypeError, match="no default name"):
        await payments(executor).aggregate(Count("*"))
    with pytest.raises(ValueError, match="given twice"):
        await payments(executor).aggregate(Sum("amount"), amount__sum=Max("amount"))
    with pytest.raises(ValueError, match="given twice"):
        await payments(executor).aggregate(Sum("amount"), Sum("amount", filter=Q(member_id=1)))
    with pytest.raises(TypeError, match="distinct on fields"):
        await payments(executor).order_by("member_id").distinct("member_id").aggregate(n=Count("*"))
    assert executor.operations == []


async def test_aggregate_without_arguments_is_an_empty_dict():
    executor = Executor()
    assert await payments(executor).aggregate() == {}
    assert executor.operations == []


def test_the_aggregate_operation_refuses_a_query_distinct_on_fields():
    with pytest.raises(TypeError, match="distinct on fields"):
        Aggregate(Query(PAYMENT).order_by("member_id").distinct("member_id"), {"n": Count("*")})
    assert Aggregate(Query(PAYMENT).distinct(), {"n": Count("*")}).query.is_distinct


def test_aggregation_names_may_contain_double_underscores_and_dashes():
    operation = Aggregate(Query(PAYMENT), {"fee__sum": Sum("amount"), "a-b": Count("*"), "a/b": Count("*")})
    assert [name for name, _ in operation.aggregations] == ["fee__sum", "a-b", "a/b"]


async def test_aggregate_of_an_empty_query_runs_no_operation():
    executor = Executor()
    result = (
        await payments(executor)
        .none()
        .aggregate(
            n=Count("*"), total=Sum("amount"), zero=Sum("amount", default=0), top=Max("amount", default=Value(7))
        )
    )
    assert result == {"n": 0, "total": None, "zero": 0, "top": 7}
    assert await payments(executor)[3:3].aggregate(n=Count("pk", filter=Q(amount__gt=1))) == {"n": 0}
    assert executor.operations == []


async def test_aggregate_of_an_empty_query_runs_the_operation_when_only_the_database_knows():
    executor = Executor({"total": None}, {"spread": None})
    assert await payments(executor).none().aggregate(total=Sum("amount", default="member_id")) == {"total": None}
    assert await payments(executor).none().aggregate(spread=Max("amount") - Avg("amount")) == {"spread": None}
    assert [operation.kind for operation in executor.operations] == ["aggregate", "aggregate"]


async def test_aggregate_and_alias_through_the_bound_model(monkeypatch):
    from adorm import Session

    recorded = []

    async def execute(self, operation):
        recorded.append((self, operation))
        return {"n": 3}

    monkeypatch.setattr(Session, "execute", execute)
    session = create_session("default", "aggregation-test")
    bound = session.bind(Payment)
    assert bound.alias(month=TruncMonth("paid_at")).query.aliases == ("month",)
    assert await bound.aggregate(n=Count("*")) == {"n": 3}
    ((used, operation),) = recorded
    assert used is session
    assert operation == Aggregate(Query(PAYMENT), {"n": Count("*")})


async def test_concurrent_aggregates_in_tasks_share_one_executor_and_keep_their_results():
    class SlowExecutor(Executor):
        async def execute(self, operation):
            self.operations.append(operation)
            # Answer in reverse order of arrival: each task must still get its own result.
            await asyncio.sleep(0)
            ((name, _),) = operation.aggregations
            return {name: int(name.removeprefix("n"))}

    executor = SlowExecutor()
    results = await asyncio.gather(*(payments(executor).aggregate(**{f"n{i}": Count("*")}) for i in range(50)))
    assert results == [{f"n{i}": i} for i in range(50)]
    assert len(executor.operations) == 50
