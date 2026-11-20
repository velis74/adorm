"""Transforms in lookup keys without a database: the registry, key syntax with JSON indexes, and resolving keys with
transforms and JSON lookups against the club models."""

import datetime
import re

import pytest

from adorm import F, FieldError, Q
from adorm.query import (
    build_lookup,
    check_lookups,
    Exact,
    GreaterThan,
    GreaterThanOrEqual,
    In,
    IsNull,
    LessThan,
    LOOKUPS,
    Range,
    register_transform,
    resolve_lookup,
    split_lookup,
    Transform,
    TRANSFORMS,
)
from tests.clubmodels.activities import Activity
from tests.clubmodels.clubs import Club, Member

DATE_TRANSFORMS = ["year", "iso_year", "quarter", "month", "week", "week_day", "iso_week_day", "day"]
TIME_TRANSFORMS = ["hour", "minute", "second"]
DATETIME_TRANSFORMS = [*DATE_TRANSFORMS, *TIME_TRANSFORMS, "date", "time"]
JSON_LOOKUPS = ["has_key", "has_keys", "has_any_keys", "contains", "contained_by"]


@register_transform
class Initial(Transform):
    """A custom transform: the first letter of a text."""

    lookup_name = "test_initial"


# Registry


@pytest.mark.parametrize("name", DATETIME_TRANSFORMS)
def test_built_in_date_and_time_transforms_are_registered(name):
    assert issubclass(TRANSFORMS[name], Transform)


@pytest.mark.parametrize("name", JSON_LOOKUPS)
def test_json_lookups_are_registered(name):
    assert name in LOOKUPS


def test_the_transform_registry_is_read_only():
    with pytest.raises(TypeError):
        TRANSFORMS["year"] = Initial


def test_registering_a_transform_again_does_nothing():
    assert register_transform(Initial) is Initial
    assert TRANSFORMS["test_initial"] is Initial


def test_a_different_transform_under_a_taken_name_is_refused():
    class Year(Transform):
        lookup_name = "year"

    with pytest.raises(ValueError, match="'year'"):
        register_transform(Year)
    assert TRANSFORMS["year"] is not Year


@pytest.mark.parametrize("name", ["test__double", "1st", "with space", ""])
def test_a_transform_name_must_be_an_identifier_without_a_separator(name):
    class Bad(Transform):
        lookup_name = name

    with pytest.raises(ValueError, match=re.escape(repr(name))):
        register_transform(Bad)


def test_a_custom_transform_resolves_in_keys():
    resolved = resolve_lookup(Member, "first_name__test_initial")
    assert (resolved.model, resolved.field, resolved.lookup) == ("clubs.Member", "first_name", Exact)
    assert resolve_lookup(Member, "first_name__test_initial__in").lookup is In
    Member.objects.filter(first_name__test_initial="A")


def test_a_misspelt_custom_transform_is_suggested():
    with pytest.raises(FieldError, match=r"perhaps you meant 'test_initial'"):
        resolve_lookup(Member, "first_name__test_initil")


# Key syntax


@pytest.mark.parametrize(
    "key",
    ["tags__0", "address__items__0", "address__items__1__isnull", "address__items__0__name__icontains"],
)
def test_a_json_index_is_a_key_segment(key):
    value = True if key.endswith("isnull") else "x"
    build_lookup(key, value)
    (Member if key.startswith("tags") else Club).objects.filter(**{key: value})


def test_a_json_index_is_not_split_as_a_lookup():
    assert split_lookup("tags__0") == ("tags__0", "exact")


# Resolving date and time transforms


@pytest.mark.parametrize(
    ("key", "lookup"),
    [
        ("joined__year", Exact),
        ("joined__year__gte", GreaterThanOrEqual),
        ("birth_date__month__in", In),
        ("birth_date__year__range", Range),
        ("joined__date__lt", LessThan),
        ("joined__hour__isnull", IsNull),
        ("joined__time__gt", GreaterThan),
    ],
)
def test_transforms_resolve_before_a_lookup(key, lookup):
    resolved = resolve_lookup(Member, key)
    assert resolved.lookup is lookup
    assert resolved.model == "clubs.Member"
    assert resolved.field == key.split("__")[0]
    assert resolved.keys == ()


@pytest.mark.parametrize("name", DATETIME_TRANSFORMS)
def test_every_date_and_time_transform_applies_to_a_datetime_field(name):
    assert resolve_lookup(Member, f"joined__{name}").lookup is Exact


@pytest.mark.parametrize("name", DATE_TRANSFORMS)
def test_date_transforms_apply_to_a_date_field(name):
    assert resolve_lookup(Member, f"birth_date__{name}").lookup is Exact


@pytest.mark.parametrize("name", [*TIME_TRANSFORMS, "date", "time"])
def test_time_transforms_do_not_apply_to_a_date_field(name):
    with pytest.raises(FieldError, match=rf"'{name}'") as raised:
        resolve_lookup(Member, f"birth_date__{name}")
    assert "clubs.Member.birth_date" in str(raised.value)


def test_a_transform_after_relations():
    resolved = resolve_lookup(Member, "memberships__club__founded__year__lt")
    assert (resolved.model, resolved.field, resolved.lookup) == ("clubs.Club", "founded", LessThan)
    assert resolve_lookup(Activity, "club__founded__month").field == "founded"


def test_a_chained_transform():
    assert resolve_lookup(Member, "joined__date__year__gte").lookup is GreaterThanOrEqual


@pytest.mark.parametrize(
    ("key", "value_type"),
    [
        ("joined__year", int),
        ("joined__year__in", int),
        ("birth_date__month__gte", int),
        ("joined__hour", int),
        ("joined__date", datetime.date),
        ("joined__date__range", datetime.date),
        ("joined__time", datetime.time),
        ("joined__year__isnull", bool),
    ],
)
def test_a_transform_types_the_right_hand_side(key, value_type):
    assert resolve_lookup(Member, key).value_type is value_type


# Resolving JSON keys and lookups


@pytest.mark.parametrize(
    ("key", "keys", "lookup"),
    [
        ("address__owner__name", ("owner", "name"), Exact),
        ("address__owner__name__icontains", ("owner", "name"), LOOKUPS["icontains"]),
        ("address__items__0", ("items", "0"), Exact),
        ("address__items__1__isnull", ("items", "1"), IsNull),
        ("address__year", ("year",), Exact),
        ("address__has_key", (), LOOKUPS["has_key"]),
        ("address__owner__has_key", ("owner",), LOOKUPS["has_key"]),
        ("address__has_keys", (), LOOKUPS["has_keys"]),
        ("address__has_any_keys", (), LOOKUPS["has_any_keys"]),
        ("address__contains", (), LOOKUPS["contains"]),
        ("address__contained_by", (), LOOKUPS["contained_by"]),
        ("tags__0", ("0",), Exact),
        ("tags__contained_by", (), LOOKUPS["contained_by"]),
    ],
)
def test_json_keys_and_lookups_resolve(key, keys, lookup):
    resolved = resolve_lookup(Club if key.startswith("address") else Member, key)
    assert (resolved.field, resolved.keys, resolved.lookup) == (key.split("__")[0], keys, lookup)


def test_has_key_takes_a_key_name():
    assert resolve_lookup(Club, "address__has_key").value_type is str


@pytest.mark.parametrize("name", ["has_key", "has_keys", "has_any_keys", "contained_by"])
def test_json_lookups_do_not_apply_to_a_text_field(name):
    with pytest.raises(FieldError, match=rf"'{name}'") as raised:
        resolve_lookup(Member, f"first_name__{name}")
    assert "clubs.Member.first_name" in str(raised.value)


# Errors with suggestions


@pytest.mark.parametrize(
    ("key", "facts"),
    [
        ("joined__yeer", ["'yeer'", "clubs.Member.joined", "perhaps you meant 'year'"]),
        ("joined__yeer__gte", ["'yeer'", "perhaps you meant 'year'"]),
        ("joined__iso_wek_day", ["'iso_wek_day'", "perhaps you meant 'iso_week_day'"]),
        ("birth_date__year__gtee", ["'gtee'", "perhaps you meant 'gte'"]),
        ("first_name__year", ["'year'", "clubs.Member.first_name"]),
        ("joined__year__club", ["'club'"]),
    ],
)
def test_an_unknown_transform_or_lookup_is_refused_with_a_suggestion(key, facts):
    with pytest.raises(FieldError) as raised:
        resolve_lookup(Member, key)
    for fact in facts:
        assert fact in str(raised.value)


def test_a_misspelt_json_lookup_is_a_key_inside_the_value():
    assert resolve_lookup(Club, "address__has_kye").keys == ("has_kye",)


def test_check_lookups_accepts_transforms_and_json_lookups():
    check_lookups(
        Member,
        Q(joined__year=2026) | Q(birth_date__month__in=[1, 2]) | Q(tags__0="coach") | Q(tags__contained_by=["x"]),
    )
    check_lookups(Member, GreaterThan(F("joined__year"), F("birth_date__year")))
    check_lookups(Club, Q(address__has_keys=["owner"]) & ~Q(address__owner__name="Ana"))


@pytest.mark.parametrize(
    "condition",
    [
        Q(joined__yeer=2026),
        Q(level="advanced") | Q(birth_date__hour=7),
        GreaterThan(F("joined__yeer"), 2000),
        Q(joined__year=F("birth_date__yeer")),
    ],
)
def test_check_lookups_refuses_an_unknown_transform(condition):
    with pytest.raises(FieldError):
        check_lookups(Member, condition)


@pytest.mark.parametrize(
    ("key", "value", "kind"),
    [
        ("joined__yeer", 2026, "lookup"),
        ("joined__yeer__exact", 2026, "transform"),
        ("joined__yeer__isnull", True, "transform"),
        ("joined__yeer__gte", 2026, "transform"),
    ],
)
def test_check_lookups_names_the_unknown_segment_as_resolve_lookup_does(key, value, kind):
    with pytest.raises(FieldError) as resolved:
        resolve_lookup(Member, key)
    with pytest.raises(FieldError) as checked:
        check_lookups(Member, Q(**{key: value}))
    assert str(checked.value) == str(resolved.value)
    assert str(checked.value).startswith(f"unsupported {kind} 'yeer'")
