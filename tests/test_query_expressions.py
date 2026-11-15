import copy
import datetime
import pickle

import pytest

import adorm

from adorm.query import (
    Aggregate,
    Avg,
    Case,
    Coalesce,
    CombinedExpression,
    Concat,
    Count,
    Exact,
    Exists,
    F,
    Func,
    IsNull,
    Length,
    Lower,
    Max,
    Min,
    Now,
    OrderBy,
    OuterRef,
    Q,
    Query,
    Star,
    Subquery,
    Sum,
    Trunc,
    TruncDate,
    TruncMonth,
    TruncTime,
    TruncYear,
    Upper,
    Value,
    When,
)
from adorm.query.expressions import check_field_path, make_hashable, Node

MEMBERS = Query("club.Member")


# Immutability and value semantics


@pytest.mark.parametrize(
    "node",
    [
        F("name"),
        Value(1),
        Lower("name"),
        Count("*"),
        Q(name="x"),
        Case(When(active=True, then=1)),
        Exists(MEMBERS),
        MEMBERS,
        F("a") + 1,
        F("a").desc(),
    ],
    ids=repr,
)
def test_nodes_are_immutable(node):
    field = node._fields[0]
    with pytest.raises(AttributeError, match="immutable"):
        setattr(node, field, None)
    with pytest.raises(AttributeError, match="immutable"):
        delattr(node, field)
    with pytest.raises(AttributeError, match="immutable"):
        node.extra = 1


@pytest.mark.parametrize(
    "build",
    [
        lambda: F("club__name"),
        lambda: Value([1, {"a": {2, 3}}]),
        lambda: Lower(F("name")).desc(nulls_last=True),
        lambda: Sum("amount", filter=Q(paid=True), default=0),
        lambda: Q(name__icontains="a") | ~Q(age__lt=18, tags__in=[1, 2]),
        lambda: Case(When(Q(a=1) | Q(b=2), then=Value("x")), default="name", output_type=str),
        lambda: Exists(MEMBERS.filter(club=OuterRef("pk"))),
        lambda: MEMBERS.filter(name="x").annotate(n=Count("id")).order_by("-n")[1:5],
        lambda: TruncMonth("joined") - F("x") * 2,
    ],
)
def test_equal_builds_are_equal_and_hash_alike(build):
    a, b = build(), build()
    assert a is not b
    assert a == b
    assert hash(a) == hash(b)
    assert len({a, b}) == 1


def test_inequality_by_class_and_value():
    assert F("a") != F("b")
    assert F("a") != OuterRef("a")
    assert Lower("a") != Upper("a")
    assert Value(1) != Value(True)
    assert Value(1) != Value(1.0)
    assert Value(1) != Value(1, output_type=int)
    assert Count("a") != Count("a", distinct=True)
    assert Q(a=1) != ~Q(a=1)
    assert (F("a") == "a") is False


def test_nodes_survive_copy_and_pickle():
    node = MEMBERS.filter(Q(name__icontains="a") | Exists(MEMBERS.filter(pk=OuterRef("pk")))).annotate(n=Count("*"))
    for clone in (copy.copy(node), copy.deepcopy(node), pickle.loads(pickle.dumps(node))):
        assert clone == node
        assert hash(clone) == hash(node)


def test_input_lists_are_not_shared():
    values = [1, 2]
    q = Q(pk__in=values)
    values.append(3)
    assert q.children[0].rhs == (1, 2)


@pytest.mark.parametrize(
    "build",
    [
        lambda v: Q(tags__contains=v),
        lambda v: Q(data__exact=v),
        lambda v: Q(span__range=(v, v)),
        lambda v: Value(v),
        lambda v: When(active=True, then=v),
    ],
)
@pytest.mark.parametrize("make", [lambda: [1, [2]], lambda: {"a": [1]}])
def test_mutable_values_are_copied(build, make):
    original = make()
    node = build(original)
    expected, expected_hash = build(make()), hash(build(make()))
    if isinstance(original, list):
        original[1].append(3)
        original.append(4)
    else:
        original["a"].append(2)
        original["b"] = 3
    assert node == expected
    assert hash(node) == expected_hash


@pytest.mark.parametrize("build", [lambda v: Q(a=v), lambda v: Value(v), lambda v: Q(a__in=[v])])
def test_uncomparable_values_fail_when_built(build):
    with pytest.raises(TypeError, match="bytearray is not a supported query value"):
        build(bytearray(b"a"))


def test_make_hashable():
    assert make_hashable({"a": [1, {2}]}) == make_hashable({"a": [1, {2}]})
    assert make_hashable([1]) == make_hashable((1,))
    assert make_hashable(1) != make_hashable(True)
    node = F("a")
    assert make_hashable(node) is node
    with pytest.raises(TypeError):
        make_hashable(bytearray(b"x"))


def test_unhashable_values_still_compare():
    assert Value({"a": [1]}) == Value({"a": [1]})
    assert hash(Value({"a": [1]})) == hash(Value({"a": [1]}))


# F and field paths


def test_f_path():
    assert F("club__owner__name").path == ("club", "owner", "name")
    assert F("pk").path == ("pk",)
    assert repr(F("name")) == "F('name')"
    assert repr(OuterRef("pk")) == "OuterRef('pk')"


@pytest.mark.parametrize("name", ["", "a__", "__a", "a____b", "a b", "a-b", "1a", "a__1"])
def test_invalid_field_paths(name):
    with pytest.raises(ValueError, match="not a field path"):
        F(name)


def test_field_path_must_be_a_string():
    with pytest.raises(TypeError):
        F(1)
    assert check_field_path("a__b") == "a__b"


def test_outer_ref_construct():
    assert list(OuterRef("pk").constructs()) == ["expression.OuterRef"]
    assert list(F("pk").constructs()) == []


# Value and Star


def test_value_wraps_literals_only():
    assert Value("x").value == "x"
    with pytest.raises(TypeError):
        Value(F("a"))
    assert repr(Value(1, output_type=int)) == "Value(1, output_type=int)"
    assert repr(Star()) == "Star()"


# Arithmetic


@pytest.mark.parametrize(
    ("expression", "connector"),
    [
        (F("a") + 1, "+"),
        (F("a") - 1, "-"),
        (F("a") * 1, "*"),
        (F("a") / 1, "/"),
        (F("a") % 1, "%"),
        (F("a") ** 1, "^"),
    ],
)
def test_arithmetic(expression, connector):
    assert expression == CombinedExpression(F("a"), connector, Value(1))
    assert list(expression.constructs()) == ["expression.CombinedExpression"]


def test_reverse_arithmetic_and_negation():
    assert 1 + F("a") == CombinedExpression(Value(1), "+", F("a"))
    assert 2 - F("a") == CombinedExpression(Value(2), "-", F("a"))
    assert 2 * F("a") == CombinedExpression(Value(2), "*", F("a"))
    assert 2 / F("a") == CombinedExpression(Value(2), "/", F("a"))
    assert 2 % F("a") == CombinedExpression(Value(2), "%", F("a"))
    assert 2 ** F("a") == CombinedExpression(Value(2), "^", F("a"))
    assert -F("a") == CombinedExpression(F("a"), "*", Value(-1))


def test_arithmetic_strings_are_values():
    assert (F("a") + "x").rhs == Value("x")
    assert (F("a") + F("b")).rhs == F("b")


def test_unknown_connector():
    with pytest.raises(ValueError, match="connector"):
        CombinedExpression(F("a"), "&", F("b"))


def test_arithmetic_with_a_query_node_is_refused():
    with pytest.raises(TypeError, match="not an expression"):
        F("a") + MEMBERS


# Ordering


def test_order_by():
    assert F("a").asc() == OrderBy(F("a"))
    assert F("a").desc(nulls_first=True) == OrderBy(F("a"), descending=True, nulls_first=True)
    assert OrderBy("name").expression == F("name")
    with pytest.raises(ValueError, match="mutually exclusive"):
        F("a").asc(nulls_first=True, nulls_last=True)
    with pytest.raises(ValueError, match="True or None"):
        F("a").asc(nulls_first=False)
    with pytest.raises(TypeError):
        OrderBy(F("a").asc())


# Functions


def test_function_strings_are_field_references():
    assert Lower("name").expressions == (F("name"),)
    assert Concat("first", Value(" "), "last").expressions == (F("first"), Value(" "), F("last"))
    assert Coalesce("nick", "name", Value("?")).expressions[2] == Value("?")


def test_function_non_string_literals_become_values():
    assert Coalesce("n", 0).expressions == (F("n"), Value(0))


@pytest.mark.parametrize(
    ("node", "output_type"),
    [
        (Lower("a"), None),
        (Upper("a"), None),
        (Length("a"), int),
        (Now(), datetime.datetime),
        (TruncMonth("a"), None),
        (TruncDate("a"), datetime.date),
        (TruncTime("a"), datetime.time),
        (Trunc("a", "date"), datetime.date),
        (Count("a"), int),
        (Sum("a"), None),
        (Lower("a", output_type=str), str),
    ],
)
def test_output_types(node, output_type):
    assert node.output_type is output_type


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: Lower(), "takes 1 argument"),
        (lambda: Lower("a", "b"), "takes 1 argument"),
        (lambda: Now("a"), "takes 0 argument"),
        (lambda: Coalesce("a"), "at least 2"),
        (lambda: Concat("a"), "at least 2"),
        (lambda: Func("a"), "base class"),
        (lambda: Aggregate("a"), "base class"),
    ],
)
def test_function_arity(build, message):
    with pytest.raises(TypeError, match=message):
        build()


def test_function_construct_names():
    assert list(Lower("a").constructs()) == ["function.Lower"]
    assert list(Lower(Upper("a")).constructs()) == ["function.Lower", "function.Upper"]
    assert list(TruncMonth("a").constructs()) == ["function.TruncMonth"]
    assert list(Trunc("a", "month").constructs()) == ["function.Trunc"]


def test_custom_function():
    class Unaccent(Func):
        arity = 1

    assert Unaccent.construct == "function.Unaccent"
    assert list(Unaccent("name").constructs()) == ["function.Unaccent"]


def test_trunc():
    assert TruncMonth("joined") == TruncMonth(F("joined"))
    assert TruncMonth("joined").kind == "month"
    assert TruncYear("joined") != Trunc("joined", "year")
    assert repr(TruncMonth("joined")) == "TruncMonth(F('joined'))"
    with pytest.raises(ValueError, match="truncation kind"):
        Trunc("joined", "decade")


def test_function_repr():
    assert repr(Lower("name")) == "Lower(F('name'))"
    assert repr(Count("*", filter=Q(a=1))) == "Count(Star(), filter=<Q: (AND: Exact(F('a'), 1))>)"


# Aggregates


def test_count_star():
    assert Count("*").expressions == (Star(),)
    assert Count("id").expressions == (F("id"),)
    with pytest.raises(TypeError, match="distinct"):
        Count("*", distinct=True)


@pytest.mark.parametrize("cls", [Count, Sum, Avg])
def test_distinct_allowed(cls):
    assert cls("a", distinct=True).distinct is True
    assert "aggregate.distinct" in set(cls("a", distinct=True).constructs())


@pytest.mark.parametrize("cls", [Min, Max])
def test_distinct_refused(cls):
    with pytest.raises(TypeError, match="does not allow distinct"):
        cls("a", distinct=True)


def test_aggregate_filter_and_default():
    total = Sum("amount", filter=Q(paid=True), default=0)
    assert total.filter == Q(paid=True)
    assert total.default == Value(0)
    assert set(total.constructs()) == {"aggregate.Sum", "aggregate.filter", "lookup.exact"}
    assert Sum("a", default="b").default == F("b")
    assert Sum("a", filter=Exact("paid", True)).filter == Exact(F("paid"), True)


def test_aggregate_filter_must_be_a_condition():
    with pytest.raises(TypeError, match="condition"):
        Sum("amount", filter=F("paid"))
    with pytest.raises(TypeError, match="condition"):
        Sum("amount", filter={"paid": True})


def test_count_has_no_default():
    with pytest.raises(TypeError):
        Count("a", default=0)


def test_aggregates_do_not_nest():
    with pytest.raises(TypeError, match="cannot aggregate an aggregate"):
        Sum(Count("a"))
    with pytest.raises(TypeError, match="cannot aggregate an aggregate"):
        Max(Count("a") + 1)
    with pytest.raises(TypeError, match="cannot contain an aggregate"):
        Sum("a", filter=Q(n__gt=Count("b")))
    with pytest.raises(TypeError, match="cannot contain an aggregate"):
        Sum("a", default=Count("b"))


def test_empty_aggregate_filter_is_no_filter():
    count = Count("id", filter=Q())
    assert count.filter is None
    assert count == Count("id")
    assert "aggregate.filter" not in set(count.constructs())


def test_when_rejects_an_empty_q():
    with pytest.raises(ValueError, match="empty Q"):
        When(Q(), then=1)


def test_aggregate_over_a_subquery_with_an_aggregate():
    inner = MEMBERS.annotate(n=Count("*")).values("n")
    assert Max(Subquery(inner)).contains_aggregate


def test_contains_aggregate():
    assert Count("a").contains_aggregate
    assert (Sum("a") / Count("a")).contains_aggregate
    assert Coalesce(Sum("a"), 0).contains_aggregate
    assert not (F("a") + 1).contains_aggregate
    assert not Lower("a").contains_aggregate
    assert not Subquery(MEMBERS.annotate(n=Count("*")).values("n")).contains_aggregate
    assert Case(When(a=1, then=Count("b"))).contains_aggregate


# Case / When


def test_when_conditions():
    assert When(active=True, then=1).condition == Q(active=True)
    assert When(Q(a=1), then=1).condition == Q(a=1)
    assert When(Q(a=1), b=2, then=1).condition == Q(Q(a=1), b=2)
    assert When(Exact("a", 1), then=1).condition == Exact(F("a"), 1)
    assert When(Exists(MEMBERS), then=1).condition == Exists(MEMBERS)


def test_when_then_strings_are_fields():
    assert When(a=1, then="name").result == F("name")
    assert When(a=1, then=Value("name")).result == Value("name")
    assert When(a=1).result == Value(None)


@pytest.mark.parametrize("condition", [None, F("a"), "a", 1, Value(True)])
def test_when_needs_a_condition(condition):
    with pytest.raises(TypeError):
        When(condition, then=1)


def test_case():
    case = Case(When(age__lt=18, then=Value("minor")), default=Value("adult"), output_type=str)
    assert case.cases == (When(age__lt=18, then=Value("minor")),)
    assert case.default == Value("adult")
    assert case.output_type is str
    assert Case(When(a=1, then=1)).default == Value(None)
    assert set(case.constructs()) == {"expression.Case", "lookup.lt"}
    with pytest.raises(TypeError, match="When"):
        Case(Q(a=1))


# Subquery / Exists


class QuerySetLike:
    """An object carrying a ``query`` attribute, as a queryset does."""

    def __init__(self, query):
        self.query = query


def test_subquery():
    inner = MEMBERS.filter(club=OuterRef("pk")).order_by("-joined").values("name")[:1]
    sub = Subquery(inner, output_type=str)
    assert sub.query == inner
    assert sub.output_type is str
    assert Subquery(QuerySetLike(inner)) == Subquery(inner)
    assert set(sub.constructs()) == {"expression.Subquery", "lookup.exact", "expression.OuterRef"}


def test_subquery_selects_one_column():
    with pytest.raises(ValueError, match="one column"):
        Subquery(MEMBERS.values("a", "b"))


@pytest.mark.parametrize("value", [None, "club.Member", F("a"), QuerySetLike("x")])
def test_subquery_needs_a_query(value):
    with pytest.raises(TypeError, match="expected a query"):
        Subquery(value)


def test_exists():
    exists = Exists(MEMBERS.values("a", "b"))
    assert exists.output_type is bool
    assert exists.conditional
    assert not exists.negated
    assert (~exists).negated
    assert ~~exists == exists
    assert list(exists.constructs()) == ["expression.Exists"]


# Q


def test_q_kwargs_are_lookups_in_sorted_order():
    q = Q(name__icontains="ann", age__gte=18)
    assert [type(c).__name__ for c in q.children] == ["GreaterThanOrEqual", "IContains"]
    assert q == Q(age__gte=18, name__icontains="ann")
    assert q.connector == Q.AND
    assert not q.negated


def test_q_positional_arguments():
    assert Q(("name", "x")) == Q(name="x")
    assert Q(Exact("name", "x")).children == (Exact(F("name"), "x"),)
    assert Q(Q(a=1)) == Q(a=1)
    assert Q(Q(a=1, b=2), c=3) == Q(a=1, b=2, c=3)
    assert Q(Q(a=1) | Q(b=2)).children == (Q(a=1) | Q(b=2),)
    assert Q(~Q(a=1)).children == (~Q(a=1),)
    assert not Q(Q())
    assert not Q(Q(), ~Q())
    for bad in (F("a"), "a", 1, ("a",), Value(True)):
        with pytest.raises(TypeError):
            Q(bad)


def test_empty_q():
    assert not Q()
    assert len(Q()) == 0
    assert Q() & Q(a=1) == Q(a=1)
    assert Q(a=1) | Q() == Q(a=1)
    assert Q() & Q() == Q()


def test_q_and_flattens_same_connector():
    q = Q(a=1) & Q(b=2) & Q(c=3)
    assert q.connector == Q.AND
    assert q.children == Q(a=1, b=2, c=3).children


def test_q_or():
    q = Q(a=1) | Q(b=2) | Q(c=3)
    assert q.connector == Q.OR
    assert len(q) == 3


def test_q_mixed_connectors_nest():
    q = (Q(a=1) | Q(b=2)) & Q(c=3)
    assert q.connector == Q.AND
    assert q.children == (Q(a=1) | Q(b=2), Exact(F("c"), 3))


def test_q_negation():
    q = ~Q(a=1, b=2)
    assert q.negated
    assert q.children == Q(a=1, b=2).children
    assert ~~Q(a=1) == Q(a=1)
    combined = Q(c=3) & q
    assert combined.children == (Exact(F("c"), 3), q)
    assert repr(q) == "<Q: NOT (AND: Exact(F('a'), 1), Exact(F('b'), 2))>"


def test_q_combines_with_conditional_expressions():
    exists = Exists(MEMBERS)
    assert Q(a=1) & exists == Q(Exact("a", 1), exists)
    assert exists | Q(a=1) == Q(exists, Exact("a", 1), _connector=Q.OR)
    assert Exact("a", 1) & Exact("b", 2) == Q(a=1, b=2)
    assert ~Exact("a", 1) == ~Q(a=1)


def test_q_refuses_non_conditions():
    with pytest.raises(TypeError, match="condition"):
        Q(a=1) & F("b")
    with pytest.raises(TypeError, match="condition"):
        Q(a=1) | {"b": 2}
    with pytest.raises(TypeError, match="condition"):
        F("b") & Q(a=1)
    with pytest.raises(TypeError, match="condition"):
        ~F("b")


def test_q_unknown_connector():
    with pytest.raises(ValueError, match="connector"):
        Q(a=1, _connector="XOR")


def test_q_constructs_and_walk():
    q = Q(name__icontains="a") | ~Q(IsNull("club", True))
    assert set(q.constructs()) == {"lookup.icontains", "lookup.isnull"}
    assert F("club") in set(q.walk())


def test_q_is_an_expression():
    assert isinstance(Q(), adorm.Expression)
    assert Q().output_type is bool
    assert Q(a=Count("b")).contains_aggregate
    assert not Q(a=1).contains_aggregate


def test_walk_order():
    node = Lower(Concat("a", "b"))
    assert list(node.walk()) == [node, Concat("a", "b"), F("a"), F("b")]


def test_node_repr_default():
    class Thing(Node):
        _fields = ("x",)

        def __init__(self, x):
            self._init(x=x)

    assert repr(Thing(1)) == "Thing(x=1)"
    assert Thing(1) == Thing(1)
    assert Thing(1) != 1
