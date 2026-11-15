import datetime

import pytest

from adorm.query import (
    build_lookup,
    Contains,
    EndsWith,
    Exact,
    Exists,
    F,
    GreaterThan,
    GreaterThanOrEqual,
    IContains,
    IEndsWith,
    IExact,
    In,
    IRegex,
    IsNull,
    IStartsWith,
    LessThan,
    LessThanOrEqual,
    Lookup,
    LOOKUPS,
    Lower,
    OuterRef,
    Q,
    Query,
    Range,
    Regex,
    register_lookup,
    split_lookup,
    StartsWith,
    Subquery,
    Value,
)

MEMBERS = Query("club.Member")
BUILT_IN = {
    "exact": Exact,
    "iexact": IExact,
    "gt": GreaterThan,
    "gte": GreaterThanOrEqual,
    "lt": LessThan,
    "lte": LessThanOrEqual,
    "in": In,
    "range": Range,
    "isnull": IsNull,
    "startswith": StartsWith,
    "istartswith": IStartsWith,
    "endswith": EndsWith,
    "iendswith": IEndsWith,
    "contains": Contains,
    "icontains": IContains,
    "regex": Regex,
    "iregex": IRegex,
}


def test_built_in_lookups_are_registered():
    assert {name: LOOKUPS[name] for name in BUILT_IN} == BUILT_IN


def test_registry_is_read_only():
    with pytest.raises(TypeError):
        LOOKUPS["x"] = Exact


@pytest.mark.parametrize(("name", "cls"), BUILT_IN.items())
def test_construct_names(name, cls):
    assert cls.construct == f"lookup.{name}"


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("name", ("name", "exact")),
        ("name__exact", ("name", "exact")),
        ("name__icontains", ("name", "icontains")),
        ("club__name", ("club__name", "exact")),
        ("club__owner__name__startswith", ("club__owner__name", "startswith")),
        ("pk__in", ("pk", "in")),
        ("club__isnull", ("club", "isnull")),
        # a single segment is always a field
        ("range", ("range", "exact")),
        ("in", ("in", "exact")),
        # a field named like a lookup takes an explicit lookup
        ("range__exact", ("range", "exact")),
        ("period__range__in", ("period__range", "in")),
        # an unknown last segment is a field
        ("joined__year", ("joined__year", "exact")),
    ],
)
def test_split_lookup(key, expected):
    assert split_lookup(key) == expected


@pytest.mark.parametrize("key", ["", "a__", "__in", "a____in", "a b"])
def test_split_lookup_rejects_bad_keys(key):
    with pytest.raises(ValueError, match="not a field path"):
        split_lookup(key)


def test_build_lookup():
    assert build_lookup("club__name__icontains", "x") == IContains(F("club__name"), "x")
    assert build_lookup("age", 3) == Exact(F("age"), 3)
    assert build_lookup("age__gt", F("min_age")) == GreaterThan(F("age"), F("min_age"))


@pytest.mark.parametrize("key", ["club", "club__exact", "name__iexact"])
def test_none_with_exact_becomes_isnull(key):
    assert build_lookup(key, None) == IsNull(F(key.split("__")[0]), True)


@pytest.mark.parametrize("name", sorted(set(BUILT_IN) - {"exact", "iexact", "isnull"}))
def test_none_is_refused(name):
    with pytest.raises((ValueError, TypeError)):
        build_lookup(f"a__{name}", None)


def test_exact_class_refuses_none():
    with pytest.raises(ValueError, match="use isnull"):
        Exact("a", None)


def test_lhs_can_be_an_expression():
    lookup = Exact(Lower("name"), "ann")
    assert lookup.lhs == Lower(F("name"))
    assert set(lookup.constructs()) == {"lookup.exact", "function.Lower"}


def test_rhs_expression():
    lookup = GreaterThan("end", F("start"))
    assert lookup.rhs == F("start")
    assert list(lookup.walk()) == [lookup, F("end"), F("start")]


def test_rhs_query_becomes_subquery():
    inner = MEMBERS.filter(pk=OuterRef("leader")).values("club")
    assert Exact("club", inner).rhs == Subquery(inner)


def test_rhs_other_node_is_refused():
    with pytest.raises(TypeError, match="not a value"):
        Exact("a", F("b").asc())


def test_lookup_is_conditional_and_boolean():
    lookup = Exact("a", 1)
    assert lookup.conditional
    assert lookup.output_type is bool
    assert repr(lookup) == "Exact(F('a'), 1)"


def test_lookup_base_class_cannot_be_built():
    with pytest.raises(TypeError, match="base class"):
        Lookup("a", 1)


# in


@pytest.mark.parametrize("values", [[1, 2], (1, 2), range(1, 3), (v for v in (1, 2)), {1: "a", 2: "b"}.keys()])
def test_in_takes_iterables(values):
    assert In("pk", values).rhs == (1, 2)


def test_in_keeps_expressions_and_drops_none():
    lookup = In("a", [1, None, F("b")])
    assert lookup.rhs == (1, F("b"))
    assert F("b") in list(lookup.walk())


def test_in_prepares_each_value():
    inner = Query("club.Club").values("id")
    lookup = In("club", [1, inner])
    assert lookup.rhs == (1, Subquery(inner))
    assert "expression.Subquery" in set(lookup.constructs())
    for bad in (Q(b=1), Exact("b", 1), F("a").asc(), Exists(inner)):
        with pytest.raises(TypeError):
            In("a", [bad])


def test_in_sorts_sets():
    first, second = {"x", "y", "z"}, set()
    for value in ("z", "y", "x"):
        second.add(value)
    assert In("a", first) == In("a", second)
    assert In("a", {"x", "y"}).rhs == ("x", "y")
    assert len(In("a", {1, "x"}).rhs) == 2


def test_in_empty():
    assert In("pk", []).rhs == ()


@pytest.mark.parametrize("value", ["abc", b"abc", 1, None, {"a": 1}, F("a"), Value([1])])
def test_in_refuses_non_iterables(value):
    with pytest.raises(TypeError, match="iterable of values or a query"):
        In("pk", value)


def test_in_subquery():
    clubs = Query("club.Club").filter(active=True).values("id")
    lookup = build_lookup("club__in", clubs)
    assert lookup.rhs == Subquery(clubs)
    assert set(lookup.constructs()) == {"lookup.in", "expression.Subquery", "lookup.exact"}
    assert In("club", Subquery(clubs)).rhs == Subquery(clubs)


def test_in_subquery_from_a_queryset_like_object():
    class QuerySetLike:
        query = Query("club.Club")

    assert In("club", QuerySetLike()).rhs == Subquery(Query("club.Club"))


def test_in_subquery_without_values_selects_the_primary_key():
    assert In("club", Query("club.Club")).rhs.query.select == ()


def test_in_subquery_with_several_columns_is_refused():
    with pytest.raises(ValueError, match="one column"):
        In("club", Query("club.Club").values("id", "name"))


def test_in_refuses_exists():
    with pytest.raises(TypeError):
        In("club", Exists(Query("club.Club")))


# range


def test_range():
    start, end = datetime.date(2026, 1, 1), datetime.date(2026, 12, 31)
    assert Range("joined", [start, end]).rhs == (start, end)
    assert Range("n", (F("a"), 3)).rhs == (F("a"), 3)


@pytest.mark.parametrize(
    ("value", "error"), [((1,), ValueError), ((1, 2, 3), ValueError), ("ab", TypeError), (1, TypeError)]
)
def test_range_takes_two_values(value, error):
    with pytest.raises(error):
        Range("n", value)


def test_range_refuses_none_bounds():
    with pytest.raises(ValueError, match="None"):
        Range("n", (None, 1))


# isnull


@pytest.mark.parametrize("value", [True, False])
def test_isnull(value):
    assert IsNull("club", value).rhs is value


@pytest.mark.parametrize("value", [1, 0, "yes", None])
def test_isnull_takes_a_bool(value):
    with pytest.raises(ValueError, match="True or False"):
        IsNull("club", value)


# regex


def test_regex():
    assert Regex("name", r"^A.*n$").rhs == r"^A.*n$"
    assert IRegex("name", F("pattern")).rhs == F("pattern")
    with pytest.raises(TypeError, match="pattern string"):
        Regex("name", 1)


# registration


def test_register_custom_lookup():
    class Unaccent(Lookup):
        lookup_name = "unaccent_test"

    try:
        assert register_lookup(Unaccent) is Unaccent
        assert register_lookup(Unaccent) is Unaccent
        assert split_lookup("name__unaccent_test") == ("name", "unaccent_test")
        assert Q(name__unaccent_test="a").children == (Unaccent(F("name"), "a"),)
        assert Unaccent.construct == "lookup.unaccent_test"
    finally:
        from adorm.query.lookups import _LOOKUPS

        del _LOOKUPS["unaccent_test"]


def test_register_conflicting_lookup():
    class Other(Lookup):
        lookup_name = "exact"

    with pytest.raises(ValueError, match="already registered"):
        register_lookup(Other)


@pytest.mark.parametrize("name", [None, "", "a b", "a__b", 1])
def test_register_needs_a_valid_name(name):
    cls = type("Bad", (Lookup,), {"lookup_name": name})
    with pytest.raises(ValueError, match="lookup_name"):
        register_lookup(cls)
