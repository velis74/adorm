import pytest

from adorm.query import (
    as_query,
    Count,
    Exact,
    Exists,
    F,
    IContains,
    Lower,
    OrderBy,
    OuterRef,
    Q,
    Query,
    Subquery,
    Sum,
)

MEMBERS = Query("club.Member")


def test_new_query():
    assert MEMBERS.model == "club.Member"
    assert MEMBERS.where == Q()
    assert MEMBERS.annotations == ()
    assert MEMBERS.ordering == ()
    assert MEMBERS.select == ()
    assert not MEMBERS.is_distinct
    assert MEMBERS.distinct_fields == ()
    assert (MEMBERS.low_mark, MEMBERS.high_mark) == (0, None)
    assert not MEMBERS.is_sliced
    assert not MEMBERS.is_empty
    assert list(MEMBERS.constructs()) == []
    assert repr(MEMBERS) == "Query('club.Member')"


@pytest.mark.parametrize("label", ["Member", "club.", ".Member", "a.b.c", "", 1, None, "club.Mem ber"])
def test_model_label(label):
    with pytest.raises(ValueError, match="model label"):
        Query(label)


def test_every_method_returns_a_new_query_and_leaves_the_original():
    steps = [
        lambda q: q.filter(name="a"),
        lambda q: q.exclude(name="a"),
        lambda q: q.annotate(n=Count("*")),
        lambda q: q.order_by("name"),
        lambda q: q.values("name"),
        lambda q: q.distinct(),
        lambda q: q[1:2],
    ]
    for step in steps:
        before = Query("club.Member")
        after = step(before)
        assert after is not before
        assert after != before
        assert before == Query("club.Member")


def test_filter_chains_with_and():
    q = MEMBERS.filter(name__icontains="ann").filter(Q(age__gte=18) | Q(guest=True))
    assert q.where == Q(name__icontains="ann") & (Q(age__gte=18) | Q(guest=True))
    assert q.where.children[0] == IContains(F("name"), "ann")


def test_filter_with_conditional_expressions():
    exists = Exists(Query("club.Payment").filter(member=OuterRef("pk")))
    q = MEMBERS.filter(exists, active=True)
    assert q.where == Q(exists, active=True)
    assert set(q.constructs()) == {"expression.Exists", "expression.OuterRef", "lookup.exact"}


def test_empty_filter_and_exclude():
    assert MEMBERS.filter() == MEMBERS
    assert MEMBERS.exclude() == MEMBERS
    assert MEMBERS.filter(Q()) == MEMBERS
    assert MEMBERS.exclude(Q()) == MEMBERS
    assert MEMBERS.filter(Q(a=1)) == MEMBERS.filter(a=1)
    assert MEMBERS.exclude(Q(a=1)) == MEMBERS.exclude(a=1)


def test_exclude_negates():
    q = MEMBERS.filter(active=True).exclude(name="x", age=3)
    assert q.where == Q(active=True) & ~Q(name="x", age=3)
    assert q.where.children[1].negated


def test_annotate():
    q = MEMBERS.annotate(lower=Lower("name")).annotate(n=Count("payments"))
    assert q.annotations == (("lower", Lower("name")), ("n", Count("payments")))
    assert set(q.constructs()) == {"function.Lower", "aggregate.Count"}
    assert q.filter(n__gt=1).where == Q(n__gt=1)


def test_annotate_errors():
    with pytest.raises(ValueError, match="already exists"):
        MEMBERS.annotate(n=Count("*")).annotate(n=Sum("a"))
    with pytest.raises(TypeError, match="not an expression"):
        MEMBERS.annotate(n="name")
    with pytest.raises(ValueError, match="annotation name"):
        MEMBERS.annotate(a__b=Count("*"))


def test_order_by():
    q = MEMBERS.order_by("-joined", "name", Lower("nick"), F("age").desc(nulls_last=True))
    assert q.ordering == (
        OrderBy(F("joined"), descending=True),
        OrderBy(F("name")),
        OrderBy(Lower("nick")),
        OrderBy(F("age"), descending=True, nulls_last=True),
    )
    assert q.order_by("name").ordering == (OrderBy(F("name")),)
    assert q.order_by().ordering == ()
    assert "function.Lower" in set(q.constructs())


@pytest.mark.parametrize("key", ["?", "-", "a__", 1, Q(a=1).children[0].__class__])
def test_order_by_errors(key):
    with pytest.raises((TypeError, ValueError)):
        MEMBERS.order_by(key)


def test_values():
    assert MEMBERS.values("name", "club__name").select == ("name", "club__name")
    assert MEMBERS.values("name").values().select == ()
    with pytest.raises(ValueError, match="not a field path"):
        MEMBERS.values("a b")


def test_distinct():
    assert MEMBERS.distinct().is_distinct
    assert list(MEMBERS.distinct().constructs()) == ["clause.distinct"]
    on = MEMBERS.distinct("club")
    assert on.distinct_fields == ("club",)
    assert list(on.constructs()) == ["clause.distinct_on"]


@pytest.mark.parametrize(
    ("slices", "marks"),
    [
        ([slice(None, 10)], (0, 10)),
        ([slice(5, None)], (5, None)),
        ([slice(5, 10)], (5, 10)),
        ([slice(5, 10), slice(1, 3)], (6, 8)),
        ([slice(5, 10), slice(2, None)], (7, 10)),
        ([slice(5, 10), slice(None, 20)], (5, 10)),
        ([slice(5, None), slice(None, 3)], (5, 8)),
        ([slice(5, 10), slice(8, None)], (10, 10)),
        ([slice(3, 3)], (3, 3)),
    ],
)
def test_slicing(slices, marks):
    q = MEMBERS
    for s in slices:
        q = q[s]
    assert (q.low_mark, q.high_mark) == marks
    assert q.is_sliced
    assert q.is_empty == (marks[1] is not None and marks[1] <= marks[0])


@pytest.mark.parametrize(
    ("key", "error"),
    [
        (0, TypeError),
        ("a", TypeError),
        (slice(0, 10, 2), ValueError),
        (slice(-1, None), ValueError),
        (slice(None, -1), ValueError),
        (slice(1.5, None), TypeError),
        (slice(True, None), TypeError),
    ],
)
def test_slicing_errors(key, error):
    with pytest.raises(error):
        MEMBERS[key]


def test_no_filter_or_reorder_after_slicing():
    sliced = MEMBERS[:5]
    with pytest.raises(TypeError, match="filter"):
        sliced.filter(a=1)
    with pytest.raises(TypeError, match="filter"):
        sliced.exclude(a=1)
    with pytest.raises(TypeError, match="reorder"):
        sliced.order_by("a")
    with pytest.raises(TypeError, match="make distinct"):
        sliced.distinct()
    with pytest.raises(TypeError, match="make distinct"):
        sliced.distinct("a")
    assert sliced.values("a").select == ("a",)


def test_as_query():
    class QuerySetLike:
        query = MEMBERS

    assert as_query(MEMBERS) is MEMBERS
    assert as_query(QuerySetLike()) is MEMBERS
    assert as_query("club.Member") is None
    assert as_query(object()) is None
    assert as_query(Subquery(MEMBERS)) is None


def test_nested_subqueries():
    payments = Query("club.Payment").filter(member=OuterRef("pk")).values("member")
    total = Query("club.Payment").filter(member=OuterRef("pk")).annotate(t=Sum("amount")).values("t")
    q = (
        MEMBERS.filter(pk__in=payments)
        .annotate(total=Subquery(total))
        .filter(club__in=Query("club.Club").filter(Exists(Query("club.Event").filter(club=OuterRef("pk")))))
    )
    assert set(q.constructs()) == {
        "lookup.in",
        "lookup.exact",
        "expression.Subquery",
        "expression.Exists",
        "expression.OuterRef",
        "aggregate.Sum",
    }
    assert not q.annotations[0][1].contains_aggregate


def test_repr():
    q = MEMBERS.filter(a=1).annotate(n=Count("*")).order_by("-n").values("n").distinct()[2:4]
    text = repr(q)
    assert text.startswith("Query('club.Member', filters=(<Q: (AND: Exact(F('a'), 1))>,)")
    assert "ordering=" in text
    assert "select=('n',)" in text
    assert "annotations=" in text
    assert "is_distinct=True" in text
    assert text.endswith("slice=[2:4])")
    assert repr(MEMBERS[3:]).endswith("slice=[3:])")


def test_query_is_hashable_and_usable_as_a_key():
    cache = {MEMBERS.filter(a=1): "x"}
    assert cache[Query("club.Member").filter(a=1)] == "x"
    assert Exact(F("a"), 1) == MEMBERS.filter(a=1).where.children[0]


def test_concurrent_building_from_a_shared_query():
    import threading

    base = MEMBERS.filter(active=True)
    barrier = threading.Barrier(8)
    results: dict[int, Query] = {}

    def build(i):
        barrier.wait()
        q = base
        for step in range(50):
            q = q.filter(**{f"f{step}": i}).order_by(f"-f{step}")
        results[i] = q

    threads = [threading.Thread(target=build, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert base == MEMBERS.filter(active=True)
    for i, q in results.items():
        assert q.where.children == (Exact(F("active"), True), *(Exact(F(f"f{step}"), i) for step in range(50)))
        assert q.ordering == (OrderBy(F("f49"), descending=True),)
