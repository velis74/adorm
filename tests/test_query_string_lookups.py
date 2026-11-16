"""Lookup keys built at run time: parsing, resolution against model schemas (`resolve_lookup`, `check_lookups`) and
the `FieldError` they raise."""

import datetime
import threading
import typing
import uuid

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from pydantic import BaseModel

import adorm

from adorm import FieldError, ProgrammingError
from adorm.codec import decode_node, encode_node
from adorm.models.registry import ModelRegistry
from adorm.query import (
    build_lookup,
    Case,
    check_lookups,
    Contains,
    Count,
    Exact,
    Exists,
    F,
    IContains,
    In,
    IRegex,
    IsNull,
    IStartsWith,
    Lookup,
    Lower,
    OuterRef,
    Q,
    Query,
    Range,
    register_lookup,
    resolve_lookup,
    ResolvedLookup,
    Subquery,
    Value,
    When,
)
from adorm.schema import ModelSchema, Schema
from demo.models import MODELS
from demo.models.clubs import Club, Member, Membership
from tests.club_schema import SCHEMA

MEMBERS = Query("club.Member")


class Settings(BaseModel):
    theme: str


EVENT = ModelSchema(
    "club.Event",
    "id",
    {
        "id": int,
        "range": int,
        "json_": dict[str, typing.Any],
        "settings": Settings | None,
        "payload": typing.Any,
        "starts": datetime.datetime,
        "host_id": int,
    },
    {"host": "club.Member", "venue": "venue.Hall"},
)
EVENTS = Schema([*SCHEMA, EVENT])


def resolve(key, label="club.Member", source=SCHEMA, **kwargs):
    return resolve_lookup(label, key, source=source, **kwargs)


# Building from keys made at run time


@pytest.mark.parametrize(
    ("field", "op", "value", "literal"),
    [
        ("name", "icontains", "an", Q(name__icontains="an")),
        ("age", "in", [3, 1], Q(age__in=[3, 1])),
        ("club__name", "istartswith", "ch", Q(club__name__istartswith="ch")),
        ("club", "isnull", True, Q(club__isnull=True)),
        ("name", "exact", None, Q(name__isnull=True)),
        ("joined", "range", (1, 2), Q(joined__range=(1, 2))),
    ],
)
def test_keys_built_at_run_time_build_the_literal_lookups(field, op, value, literal):
    key = f"{field}__{op}"
    assert Q(**{key: value}) == literal
    assert Q((key, value)) == literal
    assert MEMBERS.filter(**{key: value}) == MEMBERS.filter(literal)
    assert MEMBERS.exclude(**{key: value}) == MEMBERS.exclude(literal)
    assert MEMBERS.filter(**{key: value}).to_dict() == MEMBERS.filter(literal).to_dict()


def test_pairs_keep_their_order_and_join_with_the_connector():
    pairs = [("name__icontains", "a"), ("age__gte", 18), ("club__name", "Chess")]
    either = Q(*pairs, _connector=Q.OR)
    assert either.connector == Q.OR
    assert either.children == (IContains(F("name"), "a"), build_lookup("age__gte", 18), Exact(F("club__name"), "Chess"))
    assert Q(*sorted(pairs)) == Q(**dict(pairs))


def test_a_condition_built_in_a_loop_from_declared_filters():
    declared = {"name": ["icontains", "istartswith"], "age": ["gte"], "level": ["in"]}
    params = {"name__icontains": "an", "age__gte": 18, "level__in": ["a", "b"], "ignored": 1}
    condition = Q()
    for field, ops in declared.items():
        for op in ops:
            key = f"{field}__{op}"
            if key in params:
                condition &= Q((key, params[key]))
    assert condition == Q(("name__icontains", "an"), ("age__gte", 18), ("level__in", ["a", "b"]))


def test_a_dynamic_filter_round_trips_through_the_codec():
    query = MEMBERS.filter(**{"club__name__iexact": "chess", "token__in": [uuid.UUID(int=1)], "fee__lt": Decimal("5")})
    assert decode_node(encode_node(query, SCHEMA), SCHEMA) == query


@pytest.mark.parametrize("key", ["", "name__", "__name", "a____b", "name__0", "name-x", "name__in__"])
def test_malformed_keys_raise_value_error(key):
    with pytest.raises(ValueError, match="not a field path"):
        Q(**{key: 1})
    with pytest.raises(ValueError, match="not a field path"):
        resolve(key)


def test_a_pair_needs_a_string_key():
    with pytest.raises(TypeError):
        Q((1, "x"))


# resolve_lookup


@pytest.mark.parametrize(
    ("key", "path", "lookup", "model", "field", "field_type"),
    [
        ("name", ("name",), Exact, "club.Member", "name", str),
        ("name__icontains", ("name",), IContains, "club.Member", "name", str),
        ("pk__in", ("pk",), In, "club.Member", "id", int),
        ("club", ("club",), Exact, "club.Member", "club", int),
        ("club__isnull", ("club",), IsNull, "club.Member", "club", int),
        ("club__pk", ("club", "pk"), Exact, "club.Club", "id", int),
        ("club__owner__name__istartswith", ("club", "owner", "name"), IStartsWith, "club.Member", "name", str),
        ("payment__code", ("payment", "code"), Exact, "club.Payment", "code", uuid.UUID),
        ("payment", ("payment",), Exact, "club.Member", "payment", uuid.UUID),
        ("joined__exact", ("joined",), Exact, "club.Member", "joined", datetime.datetime),
    ],
)
def test_resolve_lookup(key, path, lookup, model, field, field_type):
    resolved = resolve(key)
    assert resolved == ResolvedLookup(key, path, lookup, model, field, field_type)


@pytest.mark.parametrize(
    ("key", "field", "keys", "lookup"),
    [
        ("json___icontains", "json_", (), IContains),
        ("json___theme", "json_", ("theme",), Exact),
        ("settings__theme__iregex", "settings", ("theme",), IRegex),
        ("payload__a__b__contains", "payload", ("a", "b"), Contains),
    ],
)
def test_keys_inside_json_and_record_fields(key, field, keys, lookup):
    resolved = resolve(key, "club.Event", EVENTS)
    assert (resolved.model, resolved.field, resolved.keys, resolved.lookup) == ("club.Event", field, keys, lookup)
    assert resolved.field_type is (EVENT.fields[field] if not keys else None)


def test_a_field_named_like_a_lookup():
    assert resolve("range", "club.Event", EVENTS).field == "range"
    assert resolve("range__range", "club.Event", EVENTS).lookup is Range
    with pytest.raises(
        FieldError, match=r"unsupported lookup 'rnage' for club\.Event\.range; perhaps you meant 'range'"
    ):
        resolve("range__rnage", "club.Event", EVENTS)
    assert resolve("range__exact", "club.Event", EVENTS).lookup is Exact
    assert resolve("range__gt", "club.Event", EVENTS).field_type is int


@pytest.mark.parametrize(
    ("key", "message"),
    [
        ("nmae", r"club\.Member has no field or relation 'nmae'; perhaps you meant 'name'"),
        ("nmae__icontains", r"club\.Member has no field or relation 'nmae'; perhaps you meant 'name'"),
        ("club__nmae", r"club\.Club has no field or relation 'nmae'; perhaps you meant 'name'"),
        ("zzz", r"^club\.Member has no field or relation 'zzz'$"),
        ("name__icontain", r"unsupported lookup 'icontain' for club\.Member\.name; perhaps you meant 'icontains'"),
        ("name__zzz", r"^unsupported lookup 'zzz' for club\.Member\.name$"),
        ("joined__year__gte", r"club\.Member\.joined is neither a relation nor a JSON field; 'year' cannot follow"),
        ("name__a__b", r"club\.Member\.name is neither a relation nor a JSON field; 'a__b' cannot follow"),
        ("club__name__exact__exact", r"'exact' cannot follow"),
        ("club_id__name", r"unsupported lookup 'name' for club\.Member\.club_id"),
        ("name__overlap", r"^unsupported lookup 'overlap' for club\.Member\.name$"),
    ],
)
def test_unresolvable_keys_raise_field_error(key, message):
    with pytest.raises(FieldError, match=message) as raised:
        resolve(key)
    assert isinstance(raised.value, ProgrammingError)
    assert raised.value.key == key
    assert raised.value.model in ("club.Member", "club.Club")
    assert (raised.value.sqlstate, raised.value.original) == (None, None)


@pytest.mark.parametrize(
    "key", ["club__icontains", "club__startswith", "club__range", "payment__regex", "club__owner__contains"]
)
def test_a_relation_refuses_lookups_it_does_not_take(key):
    with pytest.raises(FieldError, match=r"unsupported lookup '\w+' for the relation club\.\w+\.\w+; a relation takes"):
        resolve(key)
    with pytest.raises(FieldError, match="for the relation"):
        check(Q((key, ("a", "b") if key.endswith("range") else "a")))


@pytest.mark.parametrize(
    ("key", "lookup"),
    [
        ("club__in", In),
        ("club__gte", adorm.query.GreaterThanOrEqual),
        ("club__lt", adorm.query.LessThan),
        ("payment__isnull", IsNull),
        ("club__owner__exact", Exact),
    ],
)
def test_a_relation_takes_the_lookups_whose_on_relations_is_true(key, lookup):
    assert lookup.on_relations
    assert resolve(key).lookup is lookup


def test_a_registered_lookup_opts_in_to_relations():
    class SameAs(Lookup):
        lookup_name = "same_as_test"
        on_relations = True

    register_lookup(SameAs)
    try:
        assert resolve("club__same_as_test").field == "club"
    finally:
        from adorm.query.lookups import _LOOKUPS

        del _LOOKUPS["same_as_test"]


@pytest.mark.parametrize(
    "path", [["name"], ["club", "owner", "name"], ["nmae"], ["joined", "year"], ["club", "pk"], ["tags", "first"]]
)
def test_the_codec_and_resolve_lookup_resolve_a_path_alike(path):
    from adorm.schema import resolve_field_type

    key = "__".join(path)
    try:
        expected = resolve(f"{key}__exact").field_type
    except FieldError as exc:
        with pytest.raises(FieldError, match=str(exc).split(";")[0]):
            resolve_field_type(SCHEMA, "club.Member", path)
    else:
        assert resolve_field_type(SCHEMA, "club.Member", path) == expected


def test_a_driver_lacking_a_construct_reports_an_unresolvable_key_first():
    from adorm.operations import Count as CountOperation
    from demo.api.memory import MemoryDriver

    driver = MemoryDriver()
    assert "path.nested" not in driver.capabilities.constructs
    with pytest.raises(FieldError, match=r"unsupported lookup 'nope' for clubs\.Member\.first_name"):
        driver.check(CountOperation(Member.objects.filter(first_name__nope="x").query))
    with pytest.raises(adorm.NotSupportedError, match="path.nested"):
        driver.check(CountOperation(Membership.objects.filter(club__name="x").query))


def test_a_model_the_source_does_not_know_ends_the_check():
    assert resolve("anything__icontains", "other.Thing") == ResolvedLookup(
        "anything__icontains", ("anything",), IContains, None, None
    )
    venue = resolve("venue__anything__deeper__gt", "club.Event", EVENTS)
    assert (venue.model, venue.field, venue.field_type, venue.lookup.lookup_name) == (None, None, None, "gt")
    assert resolve("venue", "club.Event", EVENTS) == ResolvedLookup("venue", ("venue",), Exact, "club.Event", "venue")


def test_annotation_names_and_types():
    names = resolve("n__gt", annotations=["n"])
    assert (names.model, names.field, names.field_type, names.lookup.lookup_name) == (None, None, None, "gt")
    typed = resolve("n__in", annotations={"n": int})
    assert (typed.field_type, typed.value_type) == (int, int)
    nested = resolve("data__key__icontains", annotations={"data": dict})
    assert (nested.keys, nested.field_type) == (("key",), None)
    with pytest.raises(FieldError):
        resolve("m__gt", annotations={"n": int})


def test_a_query_target_resolves_its_annotation_names():
    query = MEMBERS.annotate(paid=Count("payment")).alias(hidden=Value(1))
    assert resolve_lookup(query, "paid__gte", source=SCHEMA).field is None
    assert resolve_lookup(query, "hidden", source=SCHEMA).field is None
    assert resolve_lookup(query, "name", source=SCHEMA).field == "name"
    assert resolve_lookup(query, "paid__gte", source=SCHEMA, annotations={"paid": int}).field_type is int
    with pytest.raises(FieldError):
        resolve_lookup(query, "unpaid", source=SCHEMA)


def test_the_model_registry_is_the_default_source():
    resolved = resolve_lookup(Member, "memberships__club__city__icontains")
    assert (resolved.model, resolved.field, resolved.lookup) == ("clubs.Club", "city", IContains)
    assert resolve_lookup("clubs.Member", "clubs__slug").model == "clubs.Club"
    assert resolve_lookup(Club, "address__street").keys == ("street",)
    with pytest.raises(FieldError, match="perhaps you meant 'clubs'"):
        resolve_lookup(Member, "club__slug")


def test_a_query_set_target_needs_no_session():
    class NoExecutor:
        async def execute(self, operation):
            raise AssertionError(f"resolving ran {operation!r}")

    from adorm.query import QuerySet

    queryset = QuerySet(Member, NoExecutor()).annotate(n=Count("memberships"))
    assert resolve_lookup(queryset, "n__lt").field is None
    check_lookups(queryset, Q(n__lt=3, memberships__role="coach"))
    with pytest.raises(FieldError, match="perhaps you meant 'role'"):
        check_lookups(queryset, Q(n__lt=3, memberships__rol="coach"))


@pytest.mark.parametrize("target", [1, None, object(), "nope", Member.model_construct(id=1)])
def test_resolve_lookup_takes_a_model_a_label_or_a_query(target):
    with pytest.raises((TypeError, ValueError)):
        resolve_lookup(target, "name")


@pytest.mark.parametrize(
    ("key", "value_type"),
    [
        ("name__icontains", str),
        ("age__icontains", str),
        ("age__in", int),
        ("age__range", int),
        ("age__isnull", bool),
        ("token__startswith", str),
        ("age__regex", str),
        ("tags__contains", list[str]),
        ("tags__overlap", str),
        ("joined__lte", datetime.datetime),
    ],
)
def test_value_type(key, value_type):
    assert resolve(key).value_type == value_type


@pytest.mark.parametrize(
    ("key", "message"),
    [
        ("payload__overlap", r"^unsupported lookup 'overlap' for club\.Event\.payload$"),
        ("json___overlap", r"^unsupported lookup 'overlap' for club\.Event\.json_$"),
        ("json___a__overlap", r"^unsupported lookup 'overlap' for club\.Event\.json___a$"),
        ("settings__theme__overlap", r"^unsupported lookup 'overlap' for club\.Event\.settings__theme$"),
        ("starts__overlap", r"^unsupported lookup 'overlap' for club\.Event\.starts$"),
    ],
)
def test_overlap_applies_only_to_an_array_field(key, message):
    with pytest.raises(FieldError, match=message) as raised:
        resolve(key, "club.Event", EVENTS)
    assert (raised.value.model, raised.value.key) == ("club.Event", key)
    with pytest.raises(FieldError, match=message):
        check_lookups("club.Event", Q((key, ["x"])) | Q(range=1), source=EVENTS)


def test_overlap_on_an_annotation():
    assert resolve("n__overlap", annotations=["n"]).value_type is None
    assert resolve("n__overlap", annotations={"n": list[int] | None}).value_type is int
    for annotations in ({"n": typing.Any}, {"n": int}, ["n"]):
        key = "n__overlap" if annotations != ["n"] else "n__a__overlap"
        with pytest.raises(FieldError, match="unsupported lookup 'overlap' for the annotation"):
            resolve(key, annotations=annotations)


def test_value_type_of_a_registered_lookup():
    class LongerThan(Lookup):
        lookup_name = "longer_than_test"

        @classmethod
        def rhs_type(cls, field_type):  # noqa: ARG003 - a length for any field
            return int

    register_lookup(LongerThan)
    try:
        resolved = resolve("club__name__longer_than_test")
        assert (resolved.field, resolved.lookup, resolved.value_type) == ("name", LongerThan, int)
        check_lookups("club.Member", Q(**{"name__longer_than_test": 3}), source=SCHEMA)
    finally:
        from adorm.query.lookups import _LOOKUPS

        del _LOOKUPS["longer_than_test"]
    with pytest.raises(FieldError, match="unsupported lookup 'longer_than_test'"):
        resolve("name__longer_than_test")


# check_lookups


def check(condition, label="club.Member", **kwargs):
    check_lookups(label, condition, source=SCHEMA, **kwargs)


def test_check_lookups_accepts_resolvable_conditions():
    check(Q(name__icontains="a") | ~Q(club__owner__age__gte=F("age") + 1))
    check(Exact(Lower("name"), "ann") & Q(payment__amount__lt=F("fee")))
    check(Exact(Case(When(age__lt=18, then=Value("junior")), default=Value("senior")), "junior"))
    check(Q(Exists(Query("club.Payment").filter(member=OuterRef("pk"), nowhere=OuterRef("nowhere")))))
    check(Q(age__in=Query("other.Thing").filter(zzz=1).values("zzz")))
    check(Q(club=Subquery(Query("club.Club").filter(name=OuterRef("name")).values("pk")[:1])))
    check(Q(paid__gt=0, name="a"), annotations=["paid"])


@pytest.mark.parametrize(
    ("condition", "message"),
    [
        (Q(nmae="a"), "no field or relation 'nmae'"),
        (Q(name__icontain="a"), "unsupported lookup 'icontain'"),
        (Q(name="a") | Q(club__nmae="x"), r"club\.Club has no field or relation 'nmae'"),
        (~Q(~Q(age__gte=F("aeg"))), "no field or relation 'aeg'"),
        (Exact(Lower("nmae"), "x"), "no field or relation 'nmae'"),
        (Q(age__gt=F("age") * F("club__fe")), r"club\.Club has no field or relation 'fe'"),
        (Exact(Case(When(nmae="a", then=Value(1)), default=Value(0)), 1), "'nmae'"),
        (Q(joined__year=2026), r"unsupported lookup 'year' for club\.Member\.joined"),
        (IContains(F("joined__year"), "x"), r"'year' cannot follow"),
        (Q(name=None, nmae=None), "'nmae'"),
    ],
)
def test_check_lookups_raises_for_the_first_unresolvable_reference(condition, message):
    with pytest.raises(FieldError, match=message):
        check(condition)


def test_check_lookups_needs_a_condition():
    with pytest.raises(TypeError, match="expected a condition"):
        check({"name": "a"})


# Concurrency


def test_concurrent_resolution_in_threads_on_a_fresh_registry():
    registry = ModelRegistry()
    for model in MODELS:
        registry.register(model)
    keys = ["memberships__club__city__icontains", "clubs__slug__in", "first_name", "pk__gte", "tags__contains"]
    barrier = threading.Barrier(8)

    def worker(n):
        barrier.wait()
        resolved = [resolve_lookup(Member, keys[(n + i) % len(keys)], source=registry) for i in range(200)]
        errors = 0
        for i in range(50):
            try:
                resolve_lookup(Membership, "member__frist_name" if i % 2 else "club__nmae", source=registry)
            except FieldError:
                errors += 1
        return resolved, errors

    for _ in range(5):
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(worker, range(8)))
        expected = {key: resolve_lookup(Member, key) for key in keys}
        for n, (resolved, errors) in enumerate(results):
            assert errors == 50
            assert resolved == [expected[keys[(n + i) % len(keys)]] for i in range(200)]


def test_field_error_is_exported():
    assert adorm.FieldError is FieldError
    assert issubclass(FieldError, adorm.ProgrammingError)
