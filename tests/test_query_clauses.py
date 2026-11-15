import pytest

from adorm.query import Count, F, ForUpdate, Lower, Q, Query

MEMBERS = Query("club.Member")


def test_new_query_defaults():
    assert MEMBERS.row_format == "model"
    assert MEMBERS.only_fields is None
    assert MEMBERS.deferred_fields == ()
    assert MEMBERS.select_related_fields is False
    assert not MEMBERS.is_none
    assert MEMBERS.for_update is None


@pytest.mark.parametrize(
    ("build", "select", "row_format"),
    [
        (lambda q: q.values(), (), "dict"),
        (lambda q: q.values("name", "club__name"), ("name", "club__name"), "dict"),
        (lambda q: q.values_list(), (), "tuple"),
        (lambda q: q.values_list("name", "age"), ("name", "age"), "tuple"),
        (lambda q: q.values_list("name", flat=True), ("name",), "flat"),
        (lambda q: q.values_list("name", "age", named=True), ("name", "age"), "named"),
        (lambda q: q.values_list("name").values("age"), ("age",), "dict"),
    ],
)
def test_values_and_values_list(build, select, row_format):
    q = build(MEMBERS)
    assert (q.select, q.row_format) == (select, row_format)


def test_values_list_errors():
    with pytest.raises(TypeError, match="mutually exclusive"):
        MEMBERS.values_list("name", flat=True, named=True)
    with pytest.raises(TypeError, match="single field"):
        MEMBERS.values_list("name", "age", flat=True)
    with pytest.raises(ValueError, match="field path"):
        MEMBERS.values_list("na me")


@pytest.mark.parametrize(
    ("build", "only", "deferred"),
    [
        (lambda q: q.only("name", "age"), ("name", "age"), ()),
        (lambda q: q.only("name", "name"), ("name",), ()),
        (lambda q: q.only(), (), ()),
        (lambda q: q.only("name").only("age"), ("age",), ()),
        (lambda q: q.defer("bio"), None, ("bio",)),
        (lambda q: q.defer("bio").defer("photo", "bio"), None, ("bio", "photo")),
        (lambda q: q.defer("bio").only("bio", "name"), ("name",), ()),
        (lambda q: q.only("name", "bio").defer("bio"), ("name",), ()),
        (lambda q: q.only("name").defer(None), None, ()),
        (lambda q: q.defer("bio").defer(None), None, ()),
    ],
)
def test_only_and_defer(build, only, deferred):
    q = build(MEMBERS)
    assert (q.only_fields, q.deferred_fields) == (only, deferred)


@pytest.mark.parametrize("method", ["only", "defer", "select_related"])
@pytest.mark.parametrize("values", [lambda q: q.values("name"), lambda q: q.values_list("name")])
def test_only_defer_and_select_related_after_values(method, values):
    with pytest.raises(TypeError, match=f"cannot call {method}"):
        getattr(values(MEMBERS), method)("name")


def test_only_and_defer_check_paths():
    with pytest.raises(ValueError, match="field path"):
        MEMBERS.only("a-b")
    with pytest.raises(TypeError, match="field path"):
        MEMBERS.defer(1)


@pytest.mark.parametrize(
    ("build", "expected"),
    [
        (lambda q: q.select_related(), True),
        (lambda q: q.select_related("club"), ("club",)),
        (lambda q: q.select_related("club").select_related("club__owner", "club"), ("club", "club__owner")),
        (lambda q: q.select_related().select_related("club"), ("club",)),
        (lambda q: q.select_related("club").select_related(), True),
        (lambda q: q.select_related("club").select_related(None), False),
    ],
)
def test_select_related(build, expected):
    assert build(MEMBERS).select_related_fields == expected


def test_none():
    q = MEMBERS.filter(name="a").none()
    assert q.is_none
    assert q.is_empty
    assert q.filter(age=1).is_none
    assert MEMBERS[2:2].is_empty
    assert not MEMBERS[2:2].is_none


def test_select_for_update():
    q = MEMBERS.select_for_update()
    assert q.for_update == ForUpdate()
    q = MEMBERS.select_for_update(nowait=True, of=("self", "club", "self"), no_key=True)
    assert q.for_update == ForUpdate(nowait=True, of=("self", "club"), no_key=True)
    assert q.for_update.of == ("self", "club")
    assert MEMBERS.select_for_update(skip_locked=True).for_update.skip_locked


def test_select_for_update_errors():
    with pytest.raises(ValueError, match="mutually exclusive"):
        MEMBERS.select_for_update(nowait=True, skip_locked=True)
    with pytest.raises(TypeError, match="not a string"):
        MEMBERS.select_for_update(of="club")
    with pytest.raises(ValueError, match="field path"):
        ForUpdate(of=["club name"])


def test_new_clauses_leave_the_original():
    steps = [
        lambda q: q.values_list("name"),
        lambda q: q.only("name"),
        lambda q: q.defer("name"),
        lambda q: q.select_related("club"),
        lambda q: q.none(),
        lambda q: q.select_for_update(),
    ]
    for step in steps:
        before = Query("club.Member")
        after = step(before)
        assert after != before
        assert before == Query("club.Member")


def test_or_combines_conditions():
    a = MEMBERS.filter(name="a")
    b = MEMBERS.filter(age__gt=3)
    assert (a | b).where == Q(name="a") | Q(age__gt=3)
    assert (a & b).where == Q(name="a") & Q(age__gt=3)


def test_or_with_an_unfiltered_query_returns_all_rows():
    a = MEMBERS.filter(name="a")
    assert (a | MEMBERS).where == Q()
    assert (MEMBERS | a).where == Q()
    assert (MEMBERS & a).where == Q(name="a")


def test_combining_with_none():
    a = MEMBERS.filter(name="a")
    empty = MEMBERS.filter(age=1).none()
    assert (a | empty) is a
    assert (empty | a) is a
    assert (a & empty) is empty
    assert (empty & a) is empty


def test_combined_ordering_and_select_related():
    a = MEMBERS.order_by("name").select_related("club")
    b = MEMBERS.select_related("club__owner")
    assert (a | b).ordering == a.ordering
    assert (b | a).ordering == a.ordering
    assert (a | b.order_by("-age")).ordering == MEMBERS.order_by("-age").ordering
    assert (a | b).select_related_fields == ("club", "club__owner")
    assert (a | MEMBERS.select_related()).select_related_fields is True
    assert (MEMBERS | MEMBERS).select_related_fields is False


@pytest.mark.parametrize(
    ("other", "message"),
    [
        (Query("club.Club"), "cannot combine a query on club.(Member|Club)"),
        (MEMBERS[:3], "slice"),
        (MEMBERS.distinct(), "is_distinct"),
        (MEMBERS.distinct("name"), "is_distinct"),
        (MEMBERS.values("name"), "select"),
        (MEMBERS.values_list(), "row_format"),
        (MEMBERS.annotate(n=Count("*")), "annotations"),
        (MEMBERS.only("name"), "only_fields"),
        (MEMBERS.defer("name"), "deferred_fields"),
        (MEMBERS.select_for_update(), "for_update"),
    ],
)
def test_combining_incompatible_queries(other, message):
    with pytest.raises(TypeError, match=message):
        MEMBERS.filter(name="a") | other
    with pytest.raises(TypeError, match=message):
        other & MEMBERS.filter(name="a")


def test_combining_with_something_else():
    with pytest.raises(TypeError):
        MEMBERS | Q(name="a")


def test_new_constructs():
    q = (
        MEMBERS.only("name")
        .defer("bio")
        .select_related("club")
        .select_for_update(nowait=True, of=["self"], no_key=True)
        .filter(name__icontains="a")
    )
    assert sorted(set(q.constructs())) == [
        "clause.only",
        "clause.select_for_update",
        "clause.select_for_update.no_key",
        "clause.select_for_update.nowait",
        "clause.select_for_update.of",
        "clause.select_related",
        "lookup.icontains",
    ]
    assert list(MEMBERS.defer("bio").constructs()) == ["clause.defer"]
    assert list(MEMBERS.select_for_update(skip_locked=True).constructs()) == [
        "clause.select_for_update",
        "clause.select_for_update.skip_locked",
    ]
    assert list(MEMBERS.none().values_list("name", flat=True).constructs()) == []


def test_for_update_is_a_child_node():
    q = MEMBERS.order_by(Lower("name")).select_for_update()
    assert q.for_update in list(q.walk())


def test_repr_of_new_clauses():
    q = MEMBERS.select_related("club").values_list("name", flat=True).none()
    assert repr(q) == (
        "Query('club.Member', select=('name',), select_related_fields=('club',), row_format='flat', is_none=True)"
    )
    assert "only_fields=('name',)" in repr(MEMBERS.only("name"))
    assert "deferred_fields=('bio',)" in repr(MEMBERS.defer("bio"))
    assert "for_update=ForUpdate(" in repr(MEMBERS.select_for_update())


def test_equal_queries_built_differently():
    assert MEMBERS.only("a", "b") == MEMBERS.only("a", "b", "a")
    assert MEMBERS.select_related("a").select_related("b") == MEMBERS.select_related("a", "b")
    assert MEMBERS.order_by(F("a").desc()) == MEMBERS.order_by("-a")
