"""The model base class: rows, deferred fields, relations, equality and save()."""

import asyncio
import copy
import datetime
import decimal
import gc
import pickle
import warnings

from typing import Annotated, Any, ClassVar

import pytest

from pydantic import Field, ValidationError

from adorm import (
    ForeignKey,
    GenericForeignKey,
    InterfaceError,
    ManyToMany,
    Model,
    ModelMeta,
    ObjectDoesNotExist,
    QuerySet,
    ReverseForeignKey,
)
from adorm.models import get_relations, model_schema, ModelRegistry, registry, RelatedObject, set_executor_resolver
from adorm.operations import Get, Operation, Save
from adorm.query import Q, Query


class Club(Model):
    _meta = ModelMeta("modeltest.Club")
    id: int | None = None
    name: str = ""
    founded: datetime.date | None = None


class Member(Model):
    _meta = ModelMeta("modeltest.Member")
    id: int | None = None
    name: str
    fee: Annotated[decimal.Decimal, Field(ge=0)] = decimal.Decimal("10")
    active: bool = True
    json_: str | None = Field(default=None, alias="json")
    club_id: int | None = None
    club = ForeignKey("modeltest.Club")
    sponsor_ref: int | None = None
    sponsor = ForeignKey("modeltest.Club", column="sponsor_ref")


class Payment(Model):
    _meta: ClassVar[ModelMeta] = ModelMeta("modeltest.Payment", pk="code")
    code: str | None = None
    amount: decimal.Decimal = decimal.Decimal(0)
    member_id: int | None = None
    member = ForeignKey("modeltest.Member")


class Named(Model):
    """An abstract model: no _meta."""

    name: str = ""


class Recorder:
    """An executor that records operations and answers from a queue of results (or raises them)."""

    def __init__(self, *results: Any) -> None:
        self.operations: list[Operation] = []
        self.results = list(results)

    async def execute(self, operation: Operation) -> Any:
        self.operations.append(operation)
        result = self.results.pop(0) if self.results else None
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture
def resolver():
    """Installs a resolver for the test and restores the previous one."""
    installed = []

    def install(fn):
        installed.append(set_executor_resolver(fn))

    yield install
    if installed:
        set_executor_resolver(installed[0])


def member_row(**overrides):
    row = {"id": 1, "name": "Ana", "fee": decimal.Decimal("12.5"), "active": True, "json_": None, "club_id": 7}
    row.update({"sponsor_ref": None})
    row.update(overrides)
    return row


# Class creation and the registry


def test_models_register_by_label():
    assert registry.get("modeltest.Member") is Member
    assert "modeltest.Club" in registry
    assert Member in list(registry)
    assert len(registry) >= 3


def test_abstract_model_is_not_registered():
    assert Named._meta is None
    assert Named not in list(registry)


def test_registry_lookup_of_unknown_label():
    with pytest.raises(LookupError, match="modeltest.Nope"):
        registry.get("modeltest.Nope")
    assert registry.model_schema("modeltest.Nope") is None


def test_meta_validates_label_and_pk():
    with pytest.raises(ValueError, match="model label"):
        ModelMeta("Member")
    with pytest.raises(ValueError, match="field name"):
        ModelMeta("a.B", pk="no-name")
    assert ModelMeta("a.B") == ModelMeta("a.B", pk="id")


def test_meta_must_be_a_class_variable():
    with pytest.raises(TypeError, match="ClassVar"):

        class Broken(Model):
            _meta: ModelMeta = ModelMeta("modeltest.Broken")
            id: int | None = None


def test_meta_must_be_a_model_meta():
    with pytest.raises(TypeError, match="is a ModelMeta"):

        class Broken(Model):
            _meta = "modeltest.Broken"
            id: int | None = None


def test_primary_key_must_be_a_field():
    with pytest.raises(TypeError, match="primary key 'code'"):

        class Broken(Model):
            _meta = ModelMeta("modeltest.Broken", pk="code")
            id: int | None = None

    assert "modeltest.Broken" not in registry


def test_foreign_key_column_must_be_a_field():
    with pytest.raises(TypeError, match="'owner_id', which is not a field"):

        class Broken(Model):
            _meta = ModelMeta("modeltest.Broken")
            id: int | None = None
            owner = ForeignKey("modeltest.Member")


def test_foreign_key_validates_target_and_column():
    with pytest.raises(ValueError, match="model label"):
        ForeignKey("Member")
    with pytest.raises(ValueError, match="field name"):
        ForeignKey("modeltest.Member", column="a b")


def test_second_class_with_the_same_label_is_refused():
    with pytest.raises(ValueError, match="already registered by tests.test_models.Club"):

        class Club(Model):  # a different qualified name: a local class
            _meta = ModelMeta("modeltest.Club")
            id: int | None = None

    assert registry.get("modeltest.Club").__module__ == __name__


def test_reloaded_class_replaces_the_registered_one():
    own = ModelRegistry()

    def make():
        class Reloaded(Model):
            _meta = ModelMeta("modeltest.Reloaded")
            id: int | None = None

        return Reloaded

    first, second = make(), make()
    own.register(first)
    own.register(second)
    assert own.get("modeltest.Reloaded") is second
    registry.unregister("modeltest.Reloaded")
    registry.unregister("modeltest.Reloaded")  # unknown label: ignored
    assert "modeltest.Reloaded" not in registry


def test_subclass_of_a_model_keeps_meta_and_is_not_registered():
    class Extended(Member):
        def greeting(self) -> str:
            return f"Hello, {self.name}"

    assert Extended._meta is Member._meta
    assert registry.get("modeltest.Member") is Member
    assert set(get_relations(Extended)) == {"club", "sponsor"}
    assert Extended(name="Ana").greeting() == "Hello, Ana"


def test_relations_are_collected_and_overridable():
    assert get_relations(Member) == {"club": Member.club, "sponsor": Member.sponsor}
    assert get_relations(Named) == {}

    class WithoutSponsor(Member):
        sponsor: ClassVar[None] = None

    assert set(get_relations(WithoutSponsor)) == {"club"}


def test_abstract_base_can_declare_relations():
    class Owned(Model):
        club_id: int | None = None
        club = ForeignKey("modeltest.Club")

    class Team(Owned):
        _meta = ModelMeta("modeltest.Team")
        id: int | None = None

    try:
        assert set(get_relations(Team)) == {"club"}
        assert model_schema(Team).relations == {"club": "modeltest.Club"}
    finally:
        registry.unregister("modeltest.Team")


def test_model_schema():
    schema = model_schema(Member)
    assert schema.label == "modeltest.Member"
    assert schema.pk == "id"
    assert set(schema.fields) == {"id", "name", "fee", "active", "json_", "club_id", "sponsor_ref"}
    assert schema.fields["name"] is str
    assert schema.relations == {"club": "modeltest.Club", "sponsor": "modeltest.Club"}
    assert registry.model_schema("modeltest.Member") is registry.model_schema("modeltest.Member")
    with pytest.raises(TypeError, match="abstract"):
        model_schema(Named)


def test_model_schema_keeps_field_constraints():
    from pydantic import TypeAdapter

    adapter = TypeAdapter(model_schema(Member).fields["fee"])
    assert adapter.validate_python("1.5") == decimal.Decimal("1.5")
    with pytest.raises(ValueError, match="greater than or equal"):
        adapter.validate_python("-1")


def test_schema_of_an_incomplete_model_is_not_cached():
    source = ModelRegistry()

    class Early(Model):
        _meta = ModelMeta("modeltest.Early")
        id: int | None = None
        later: "Later | None" = None  # noqa: F821 - defined below

    try:
        source.register(Early)
        assert not isinstance(source.model_schema("modeltest.Early").fields["later"], type(int | None))

        class Later(Model):
            _meta = ModelMeta("modeltest.Later")
            id: int | None = None

        Early.model_rebuild()
        assert source.model_schema("modeltest.Early").fields["later"] == Later | None
        assert source.model_schema("modeltest.Early") is source.model_schema("modeltest.Early")
    finally:
        registry.unregister("modeltest.Early")
        registry.unregister("modeltest.Later")


def test_registry_types_operation_values():
    operation = Save("modeltest.Member", 3, {"fee": decimal.Decimal("1.50"), "club_id": 7})
    data = operation.to_dict(registry)
    assert data["values"]["fee"] == {"value": "1.50"}
    assert Operation.from_dict(data, registry) == operation


def test_every_model_has_its_own_exceptions():
    assert issubclass(Member.DoesNotExist, ObjectDoesNotExist)
    assert issubclass(Member.DoesNotExist, Model.DoesNotExist)
    assert Member.DoesNotExist is not Club.DoesNotExist


# Configuration and the JSON schema


def test_relations_are_not_in_the_json_schema():
    schema = Member.model_json_schema()
    assert "club" not in schema["properties"]
    assert "sponsor" not in schema["properties"]
    assert "club_id" in schema["properties"]


def test_alias_is_the_name_in_json_and_the_schema():
    assert "json" in Member.model_json_schema()["properties"]
    member = Member.model_validate({"name": "Ana", "json": "{}"})
    assert member.json_ == "{}"
    assert Member(name="Ana", json_="{}").json_ == "{}"
    assert member.model_dump()["json"] == "{}"
    assert "json_" not in member.model_dump()


def test_constructor_validates():
    with pytest.raises(ValueError, match="greater than or equal"):
        Member(name="Ana", fee=-1)


def test_extra_values_are_allowed():
    member = Member(name="Ana", visits=3)
    assert member.visits == 3
    member.recent_payments = [Payment(code="p1")]
    assert member.model_dump()["visits"] == 3
    assert member.model_dump()["recent_payments"] == [{"code": "p1", "amount": decimal.Decimal(0), "member_id": None}]


# Rows


def test_from_row_builds_without_validation():
    member = Member.from_row(member_row(fee="not a number"))
    assert member.fee == "not a number"
    assert member.model_fields_set == {"id", "name", "fee", "active", "json_", "club_id", "sponsor_ref"}
    assert member._state.adding is False


def test_from_row_keeps_unknown_keys_as_extra_values():
    member = Member.from_row(member_row(payments_total=decimal.Decimal(30)))
    assert member.payments_total == 30
    assert member.__pydantic_extra__ == {"payments_total": decimal.Decimal(30)}


def test_from_row_needs_the_primary_key():
    with pytest.raises(ValueError, match="primary key 'id'"):
        Member.from_row({"name": "Ana"})


def test_abstract_model_has_no_rows():
    with pytest.raises(TypeError, match="abstract"):
        Named.from_row({"name": "x"})
    with pytest.raises(TypeError, match="abstract"):
        _ = Named().pk


def test_deferred_field_raises_attribute_error():
    member = Member.from_row({"id": 1, "club_id": None})
    with pytest.raises(AttributeError, match="Member.name is deferred"):
        _ = member.name
    assert not hasattr(member, "fee")
    assert getattr(member, "active", "missing") == "missing"
    assert member.get_deferred_fields() == {"name", "fee", "active", "json_", "sponsor_ref"}


def test_unknown_attribute_still_raises_attribute_error():
    member = Member.from_row(member_row())
    with pytest.raises(AttributeError, match="no_such_name"):
        _ = member.no_such_name


def test_assigning_a_deferred_field_loads_it():
    member = Member.from_row({"id": 1, "club_id": None})
    member.name = "Bea"
    assert member.name == "Bea"
    assert "name" in member.model_fields_set
    assert "name" not in member.get_deferred_fields()


def test_deferred_fields_are_left_out_of_dumps_and_repr():
    member = Member.from_row({"id": 1, "name": "Ana"})
    assert member.model_dump() == {"id": 1, "name": "Ana"}
    assert member.model_dump_json() == '{"id":1,"name":"Ana"}'
    assert repr(member) == "Member(id=1, name='Ana')"


def test_row_of_a_relation_fills_its_cache():
    executor = Recorder()
    member = Member.from_row(
        member_row(club={"id": 7, "name": "Chess"}, sponsor=None, sponsor_ref=None), executor=executor
    )
    club = member._state.related["club"]
    assert isinstance(club, Club)
    assert club.name == "Chess"
    assert club._state.executor is executor
    assert member._state.related["sponsor"] is None
    assert "club" not in (member.__pydantic_extra__ or {})


def test_nested_relation_rows():
    payment = Payment.from_row(
        {"code": "p1", "amount": 5, "member_id": 1, "member": member_row(club={"id": 7, "name": "Chess"})}
    )
    assert payment._state.related["member"]._state.related["club"].name == "Chess"


def test_relation_row_may_be_an_instance():
    club = Club.from_row({"id": 7, "name": "Chess"})
    member = Member.from_row(member_row(club=club))
    assert member._state.related["club"] is club


# Equality and hash


def test_equal_by_model_and_primary_key():
    a = Member.from_row(member_row(name="Ana"))
    b = Member.from_row(member_row(name="Changed"))
    assert a == b
    assert hash(a) == hash(b)
    assert len({a, b}) == 1
    assert a != Member.from_row(member_row(id=2))


def test_different_models_with_the_same_primary_key_differ():
    assert Member.from_row(member_row(id=7)) != Club.from_row({"id": 7, "name": "x"})
    assert hash(Member.from_row(member_row(id=7))) == hash(("modeltest.Member", 7))


def test_subclass_instance_equals_its_model_instance():
    class Extended(Member):
        pass

    assert Extended.from_row(member_row()) == Member.from_row(member_row())


def test_instances_without_primary_key():
    a, b = Member(name="Ana"), Member(name="Ana")
    assert a != b
    assert a == a  # noqa: PLR0124 - identity
    with pytest.raises(TypeError, match="unhashable"):
        hash(a)


def test_comparison_with_other_objects():
    member = Member.from_row(member_row())
    assert member != 1
    assert member.__eq__(1) is NotImplemented


def test_abstract_instances_compare_by_identity():
    a = Named(name="x")
    assert a == a  # noqa: PLR0124 - identity
    assert a != Named(name="x")
    assert hash(a) == object.__hash__(a)
    assert len({a, Named(name="x")}) == 2


def test_pk_property():
    payment = Payment(amount=1)
    assert payment.pk is None
    payment.pk = "p1"
    assert payment.code == "p1"
    assert "code" in payment.model_fields_set


# Copies


def test_copy_has_its_own_relation_cache():
    executor = Recorder()
    member = Member.from_row(member_row(club={"id": 7, "name": "Chess"}), executor=executor)
    shallow = copy.copy(member)
    deep = copy.deepcopy(member)
    for clone in (shallow, deep, member.model_copy(), member.model_copy(deep=True)):
        assert clone._state is not member._state
        assert clone._state.related is not member._state.related
        assert clone._state.executor is executor
        assert clone._state.adding is False
    assert shallow._state.related["club"] is member._state.related["club"]
    assert deep._state.related["club"] is not member._state.related["club"]


def test_pickle_drops_the_executor():
    member = Member.from_row(member_row(club={"id": 7, "name": "Chess"}), executor=Recorder())
    restored = pickle.loads(pickle.dumps(member))
    assert restored == member
    assert restored._state.executor is None
    assert restored._state.adding is False
    assert restored._state.related["club"].name == "Chess"
    assert "adding=False" in repr(restored._state)


# Relations


def test_reverse_foreign_key_returns_a_query_set_of_related_rows():
    class Team(Model):
        _meta = ModelMeta("modeltest.Team")
        id: int | None = None
        players = ReverseForeignKey("modeltest.Member", column="club_id")

    assert Team.players.column == "club_id"
    assert repr(Team.players) == "ReverseForeignKey('modeltest.Member', column='club_id')"
    executor = Recorder()
    players = Team.from_row({"id": 4}, executor=executor).players
    assert isinstance(players, QuerySet)
    assert (players.model, players.executor) == (Member, executor)
    assert players.query == Query("modeltest.Member").filter(club_id=4)
    assert model_schema(Team).relations == {"players": "modeltest.Member"}
    with pytest.raises(ValueError, match="needs a primary key"):
        _ = Team().players
    with pytest.raises(ValueError, match="is not a field name"):
        ReverseForeignKey("modeltest.Member", column="club id")
    registry.unregister("modeltest.Team")


def test_many_to_many_returns_a_query_set_of_related_rows_through_the_link_model():
    class Event(Model):
        _meta = ModelMeta("modeltest.Event")
        id: int | None = None
        guests = ManyToMany(
            "modeltest.Member", through="modeltest.Event_guests", source_column="event_id", target_column="member_id"
        )

    links = Query("modeltest.Event_guests").filter(event_id=2).values("member_id")
    executor = Recorder()
    guests = Event.from_row({"id": 2}, executor=executor).guests
    assert (guests.model, guests.executor) == (Member, executor)
    assert guests.query == Query("modeltest.Member").filter(pk__in=links)
    assert Event.guests.through == "modeltest.Event_guests"
    assert repr(Event.guests) == (
        "ManyToMany('modeltest.Member', through='modeltest.Event_guests', source_column='event_id', "
        "target_column='member_id')"
    )
    with pytest.raises(ValueError, match="needs a primary key"):
        _ = Event().guests
    with pytest.raises(ValueError, match="is not a model label"):
        ManyToMany("modeltest.Member", through="guests", source_column="event_id", target_column="member_id")
    with pytest.raises(ValueError, match="is not a field name"):
        ManyToMany("modeltest.Member", through="modeltest.G", source_column="1", target_column="member_id")
    registry.unregister("modeltest.Event")


def test_relation_on_the_class_is_the_descriptor():
    assert isinstance(Member.club, ForeignKey)
    assert Member.club.name == "club"
    assert Member.club.column == "club_id"
    assert Member.sponsor.column == "sponsor_ref"
    assert Member.club.target_model() is Club
    assert repr(Member.club) == "ForeignKey('modeltest.Club')"


async def test_await_loads_the_related_row_once():
    executor = Recorder({"id": 7, "name": "Chess"})
    member = Member.from_row(member_row(club_id=7), executor=executor)
    related = member.club
    assert isinstance(related, RelatedObject)
    assert repr(related) == "<RelatedObject Member.club>"
    club = await related
    assert isinstance(club, Club)
    assert club.name == "Chess"
    assert club._state.executor is executor
    assert executor.operations == [Get(Query("modeltest.Club").filter(pk=7))]
    assert await member.club is club
    assert len(executor.operations) == 1


async def test_cached_row_from_select_related_needs_no_query():
    executor = Recorder()
    member = Member.from_row(member_row(club={"id": 7, "name": "Chess"}), executor=executor)
    assert (await member.club).name == "Chess"
    assert executor.operations == []


async def test_changed_column_reloads_the_relation():
    executor = Recorder({"id": 8, "name": "Go"})
    member = Member.from_row(member_row(club={"id": 7, "name": "Chess"}), executor=executor)
    member.club_id = 8
    assert (await member.club).name == "Go"
    assert executor.operations == [Get(Query("modeltest.Club").filter(Q(pk=8)))]


async def test_null_column_gives_none_without_a_query():
    executor = Recorder()
    member = Member.from_row(member_row(club_id=None, club={"id": 7, "name": "stale"}), executor=executor)
    assert await member.club is None
    assert await member.sponsor is None
    assert executor.operations == []


async def test_cached_none_with_a_value_reloads():
    executor = Recorder({"id": 7, "name": "Chess"})
    member = Member.from_row(member_row(club_id=None, club=None), executor=executor)
    member.club_id = 7
    assert (await member.club).name == "Chess"


def test_relation_with_deferred_column_raises_attribute_error():
    member = Member.from_row({"id": 1, "name": "Ana"})
    with pytest.raises(AttributeError, match="needs 'club_id', which is deferred"):
        _ = member.club


async def test_relation_load_after_its_column_was_deleted():
    member = Member.from_row(member_row())
    related = member.club
    del member.__dict__["club_id"]
    with pytest.raises(AttributeError, match="deferred"):
        await related


def test_assigning_an_instance_sets_the_column():
    club = Club.from_row({"id": 7, "name": "Chess"})
    member = Member.from_row({"id": 1, "club_id": None})
    member.club = club
    assert member.club_id == 7
    assert "club_id" in member.model_fields_set
    assert member._state.related["club"] is club
    member.sponsor = club
    assert member.sponsor_ref == 7


async def test_assigned_instance_is_returned_without_a_query():
    executor = Recorder()
    member = Member.from_row(member_row(club_id=None), executor=executor)
    club = Club.from_row({"id": 9, "name": "Go"})
    member.club = club
    assert await member.club is club
    assert executor.operations == []


async def test_assigning_none_clears_the_column():
    member = Member.from_row(member_row(club={"id": 7, "name": "Chess"}))
    member.club = None
    assert member.club_id is None
    assert await member.club is None


def test_assigning_a_wrong_model_or_value():
    member = Member(name="Ana")
    with pytest.raises(TypeError, match=r"takes a modeltest.Club instance, not Member"):
        member.club = Member.from_row(member_row())
    with pytest.raises(TypeError, match="not dict"):
        member.club = {"id": 7}
    with pytest.raises(TypeError, match="not Named"):
        member.club = Named()


def test_assigning_an_unsaved_instance():
    member = Member(name="Ana")
    with pytest.raises(ValueError, match="unsaved modeltest.Club"):
        member.club = Club(name="new")
    assert member.club_id is None


def test_relation_in_the_constructor():
    club = Club.from_row({"id": 7, "name": "Chess"})
    member = Member(name="Ana", club=club)
    assert member.club_id == 7
    assert member.model_fields_set == {"name", "club_id"}
    assert member.__pydantic_extra__ == {}
    assert "club" not in member.model_dump()


class Ticket(Model):
    _meta = ModelMeta("modeltest.Ticket")
    id: int | None = None
    club_id: int
    club = ForeignKey("modeltest.Club")


def test_relation_in_the_constructor_fills_a_required_column():
    club = Club.from_row({"id": 7, "name": "Chess"})
    ticket = Ticket(club=club)
    assert ticket.club_id == 7
    assert ticket.model_fields_set == {"club_id"}
    assert ticket._state.related["club"] is club


def test_relation_in_the_constructor_caches_none():
    member = Member(name="Ana", club=None)
    assert member.club_id is None
    assert member._state.related["club"] is None
    assert member.model_fields_set == {"name", "club_id"}


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"id": 7}, "not dict"),
        (7, "not int"),
        (Named(name="x"), "not Named"),
        (Club(name="new"), "unsaved modeltest.Club"),
    ],
)
def test_relation_in_validated_input_takes_a_saved_instance(value, message):
    with pytest.raises(ValidationError, match=message):
        Member.model_validate({"name": "Ana", "club": value})


def test_relation_and_its_column_together_are_refused():
    club = Club.from_row({"id": 7, "name": "Chess"})
    with pytest.raises(ValidationError, match="either club or club_id"):
        Member(name="Ana", club=club, club_id=7)


@pytest.mark.parametrize("club", ["5", '{"id": 5}', "null"])
def test_relation_in_json_input_is_a_validation_error(club):
    with pytest.raises(ValidationError, match="club is a relation: give its column 'club_id'"):
        Member.model_validate_json(f'{{"name": "Ana", "club": {club}}}')


def test_pk_keyword_sets_the_primary_key_field():
    assert Club(pk=5, name="x").id == 5
    assert Payment(pk="p1").code == "p1"
    assert Club.from_row({"pk": 5, "name": "x"}).id == 5
    with pytest.raises(ValidationError, match="either pk or id"):
        Club(pk=5, id=6)
    with pytest.raises(ValueError, match="either pk or id"):
        Club.from_row({"pk": 5, "id": 6})


@pytest.mark.parametrize("name", ["save", "get_deferred_fields", "model_config", "_state"])
def test_extra_value_may_not_shadow_a_class_attribute(name):
    with pytest.raises(ValidationError, match=f"'{name}' is an attribute of Named"):
        Named.model_validate({name: 4})
    with pytest.raises(ValueError, match=f"'{name}' is an attribute of Member"):
        Member.from_row({"id": 1, name: 4})


def test_pk_of_an_abstract_model_is_not_an_extra_value():
    with pytest.raises(ValidationError, match="'pk' is an attribute of Named"):
        Named(pk=4)


def test_from_row_accepts_an_alias():
    member = Member.from_row({"id": 1, "json": "j"})
    assert member.json_ == "j"
    assert "json_" in member.model_fields_set
    assert "json_" not in member.get_deferred_fields()


def test_from_row_checks_the_type_of_a_related_instance():
    with pytest.raises(TypeError, match="takes a modeltest.Club instance, not Payment"):
        Member.from_row(member_row(club=Payment.from_row({"code": "p1"})))


async def test_missing_related_row_raises_the_targets_does_not_exist():
    executor = Recorder(ObjectDoesNotExist("no row"))
    member = Member.from_row(member_row(), executor=executor)
    with pytest.raises(Club.DoesNotExist, match="no row") as info:
        await member.club
    assert isinstance(info.value.__cause__, ObjectDoesNotExist)
    assert "club" not in member._state.related


async def test_targets_own_does_not_exist_passes_through():
    error = Club.DoesNotExist("no club")
    member = Member.from_row(member_row(), executor=Recorder(error))
    with pytest.raises(Club.DoesNotExist) as info:
        await member.club
    assert info.value is error


async def test_unregistered_target():
    class Loose(Model):
        _meta = ModelMeta("modeltest.Loose")
        id: int | None = None
        thing_id: int | None = None
        thing = ForeignKey("modeltest.Unknown")

    try:
        loose = Loose.from_row({"id": 1, "thing_id": 2}, executor=Recorder())
        with pytest.raises(LookupError, match="modeltest.Unknown"):
            await loose.thing
    finally:
        registry.unregister("modeltest.Loose")


def test_relation_not_awaited_leaves_no_coroutine_behind():
    member = Member.from_row(member_row(), executor=Recorder())
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        for _ in range(3):
            member.club  # noqa: B018 - reading without awaiting
        gc.collect()


async def test_concurrent_loads_of_one_relation():
    """Both awaits run while the first load is still executing; each gets a club and the cache keeps one."""
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    class Gate:
        async def execute(self, _operation):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return {"id": 7, "name": f"Chess {calls}"}

    for _ in range(20):
        started.clear()
        release.clear()
        calls = 0
        member = Member.from_row(member_row(), executor=Gate())
        first = asyncio.ensure_future(member.club)
        await started.wait()
        second = asyncio.ensure_future(member.club)
        await asyncio.sleep(0)
        release.set()
        a, b = await asyncio.gather(first, second)
        assert a == b
        assert member._state.related["club"] in (a, b)
        assert (await member.club) is member._state.related["club"]


async def test_cancelled_load_leaves_the_cache_empty():
    started = asyncio.Event()

    class Hang:
        async def execute(self, _operation):
            started.set()
            await asyncio.Event().wait()

    for _ in range(10):
        started.clear()
        member = Member.from_row(member_row(), executor=Hang())
        task = asyncio.ensure_future(member.club)
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert "club" not in member._state.related


# Executors


async def test_instance_without_an_executor(resolver):
    resolver(None)
    member = Member.from_row(member_row())
    with pytest.raises(InterfaceError, match="no executor is available for modeltest.Member"):
        await member.club
    with pytest.raises(InterfaceError):
        await Member(name="Ana").save()


async def test_resolver_supplies_an_executor(resolver):
    executor = Recorder({"id": 7, "name": "Chess"})
    seen = []

    def resolve(model):
        seen.append(model)
        return executor

    resolver(resolve)
    member = Member.from_row(member_row())
    assert (await member.club)._state.executor is executor
    assert seen == [Member]


async def test_own_executor_wins_over_the_resolver(resolver):
    resolver(lambda _model: pytest.fail("the resolver is not asked"))
    own = Recorder({"id": 7, "name": "Chess"})
    member = Member.from_row(member_row(), executor=own)
    await member.club
    assert len(own.operations) == 1


def test_set_executor_resolver_returns_the_previous_one(resolver):
    def first(_model):
        return None

    resolver(first)
    assert set_executor_resolver(None) is first
    assert set_executor_resolver(first) is None


# save()


async def test_save_new_instance_writes_assigned_fields_only():
    executor = Recorder({"id": 5, "active": False})
    member = Member(name="Ana", fee=decimal.Decimal(3))
    member._state.executor = executor
    await member.save()
    assert executor.operations == [Save("modeltest.Member", None, {"name": "Ana", "fee": decimal.Decimal(3)})]
    assert member.id == 5
    assert member.active is False
    assert member.model_fields_set == {"name", "fee", "id", "active"}
    assert member._state.adding is False


async def test_save_uses_and_keeps_the_resolved_executor(resolver):
    executor = Recorder({"id": 5})
    resolver(lambda _model: executor)
    member = Member(name="Ana")
    await member.save()
    assert member._state.executor is executor


async def test_save_loaded_instance_writes_loaded_and_assigned_fields():
    executor = Recorder({"id": 1})
    member = Member.from_row({"id": 1, "name": "Ana", "club_id": 7}, executor=executor)
    member.active = False
    await member.save()
    assert executor.operations == [
        Save("modeltest.Member", 1, {"name": "Ana", "active": False, "club_id": 7}),
    ]


async def test_save_ignores_extra_values():
    executor = Recorder({"id": 1})
    member = Member.from_row(member_row(visits=3), executor=executor)
    member.more = 4
    await member.save()
    assert {name for name, _ in executor.operations[0].values} == {
        "name",
        "fee",
        "active",
        "json_",
        "club_id",
        "sponsor_ref",
    }


async def test_save_writes_by_field_name_not_alias():
    executor = Recorder({"id": 1})
    member = Member.from_row({"id": 1, "json_": "{}"}, executor=executor)
    await member.save()
    assert executor.operations[0].values == (("json_", "{}"),)


async def test_save_with_a_primary_key_other_than_id():
    executor = Recorder({"code": "p1"})
    payment = Payment(code="p1", amount=decimal.Decimal(2))
    payment._state.executor = executor
    await payment.save(force_insert=True)
    assert executor.operations == [Save("modeltest.Payment", "p1", {"amount": decimal.Decimal(2)}, force_insert=True)]


async def test_save_update_fields():
    executor = Recorder({"id": 1})
    member = Member.from_row(member_row(), executor=executor)
    member.name = "Bea"
    await member.save(update_fields=["name", "club", "pk"])
    operation = executor.operations[0]
    assert operation.update_fields == ("name", "club_id")
    assert operation.pk == 1


async def test_save_update_fields_writes_an_unset_default():
    executor = Recorder({"id": 1})
    member = Member(id=1, name="Ana")
    member._state.executor = executor
    await member.save(update_fields=["active"])
    assert executor.operations[0] == Save(
        "modeltest.Member", 1, {"name": "Ana", "active": True}, update_fields=["active"]
    )


@pytest.mark.parametrize("fields", [[], ["pk"], ["id"]])
async def test_save_with_nothing_to_update_does_nothing(fields):
    executor = Recorder()
    member = Member.from_row(member_row(), executor=executor)
    await member.save(update_fields=fields)
    assert executor.operations == []


@pytest.mark.parametrize(
    ("fields", "error", "match"),
    [
        ("name", TypeError, "not a string"),
        (["nope"], ValueError, "'nope' is not a field of modeltest.Member"),
        (["fee"], ValueError, "'fee', which is deferred"),
    ],
)
async def test_save_update_fields_errors(fields, error, match):
    executor = Recorder()
    member = Member.from_row({"id": 1, "name": "Ana"}, executor=executor)
    with pytest.raises(error, match=match):
        await member.save(update_fields=fields)
    assert executor.operations == []


async def test_save_update_fields_without_a_primary_key():
    member = Member(name="Ana")
    member._state.executor = Recorder()
    with pytest.raises(ValueError, match="without a primary key value"):
        await member.save(update_fields=["name"])


async def test_save_force_flags_are_checked_by_the_operation():
    member = Member(name="Ana")
    member._state.executor = Recorder()
    with pytest.raises(ValueError, match="force_update needs a primary key"):
        await member.save(force_update=True)
    with pytest.raises(ValueError, match="force_insert excludes"):
        await Member.from_row(member_row(), executor=Recorder()).save(force_insert=True, force_update=True)


async def test_save_force_update():
    executor = Recorder({"id": 1})
    member = Member.from_row({"id": 1, "name": "Ana"}, executor=executor)
    await member.save(force_update=True)
    assert executor.operations[0].force_update is True


async def test_save_without_a_result():
    executor = Recorder(None)
    member = Member.from_row({"id": 1, "name": "Ana"}, executor=executor)
    await member.save()
    assert member._state.adding is False


async def test_save_with_a_result_that_is_not_a_mapping():
    member = Member(name="Ana")
    member._state.executor = Recorder([1])
    with pytest.raises(TypeError, match="mapping of field values, not list"):
        await member.save()


async def test_save_result_ignores_unknown_names():
    executor = Recorder({"id": 4, "whatever": 1})
    member = Member(name="Ana")
    member._state.executor = executor
    await member.save()
    assert member.id == 4
    assert "whatever" not in member.model_fields_set
    assert not member.__pydantic_extra__


async def test_failed_save_leaves_the_instance_unchanged(resolver):
    executor = Recorder(ObjectDoesNotExist("boom"))
    resolver(lambda _model: executor)
    member = Member(name="Ana")
    with pytest.raises(ObjectDoesNotExist):
        await member.save()
    assert member.id is None
    assert member._state.adding is True
    assert member._state.executor is None
    assert member.model_fields_set == {"name"}


async def test_cancelled_save_leaves_the_instance_unchanged():
    started = asyncio.Event()

    class Hang:
        async def execute(self, _operation):
            started.set()
            await asyncio.Event().wait()

    for _ in range(10):
        started.clear()
        member = Member(name="Ana")
        member._state.executor = Hang()
        task = asyncio.ensure_future(member.save())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert member.id is None
        assert member._state.adding is True
        assert member.model_fields_set == {"name"}


async def test_saved_instance_is_hashable_and_equal_to_its_row():
    member = Member(name="Ana")
    member._state.executor = Recorder({"id": 11})
    await member.save()
    assert member == Member.from_row(member_row(id=11))
    assert {member: 1}[Member.from_row(member_row(id=11))] == 1


async def test_saved_operation_encodes_with_the_registry():
    executor = Recorder({"id": 1})
    member = Member.from_row(member_row(), executor=executor)
    await member.save()
    operation = executor.operations[0]
    assert Operation.from_dict(operation.to_dict(registry), registry) == operation


def test_validating_an_instance_returns_it():
    member = Member(name="Ana")
    assert Member.model_validate(member) is member


# refresh_from_db()


class Tagged(Model):
    _meta = ModelMeta("modeltest.Tagged")
    id: int | None = None
    label: str = ""
    target_type_id: int | None = None
    target_id: int | None = None
    target = GenericForeignKey("target_type", "target_id")


async def test_refresh_from_db_without_a_primary_key_raises_without_an_operation():
    recorder = Recorder()
    club = Club(name="Unsaved")
    club._state.executor = recorder
    with pytest.raises(Club.DoesNotExist, match="Club matching query does not exist"):
        await club.refresh_from_db()
    assert recorder.operations == []
    assert club.name == "Unsaved"


async def test_refresh_from_db_reads_the_base_query_and_limits_it_only_for_deferred_or_named_fields():
    recorder = Recorder(member_row(name="Bea"), member_row(name="Cene"), {"id": 1, "club_id": 8})
    member = Member.from_row(member_row(), executor=recorder)
    await member.refresh_from_db()
    full = recorder.operations[0]
    assert isinstance(full, Get)
    assert (full.query.manager, full.query.only_fields) == (None, None)
    assert member.name == "Bea"
    del member.__dict__["fee"]
    await member.refresh_from_db()
    assert "fee" not in recorder.operations[1].query.only_fields
    assert member.get_deferred_fields() == {"fee"}
    await member.refresh_from_db(fields=["club", "pk"])
    assert recorder.operations[2].query.only_fields == ("club_id", "id")
    assert (member.name, member.club_id) == ("Cene", 8)


async def test_refresh_from_db_clears_a_generic_foreign_key_and_its_pending_assignment():
    row = {"id": 1, "label": "x", "target_type_id": 3, "target_id": 4}
    recorder = Recorder(row, row, row, row)
    tagged = Tagged.from_row(row, executor=recorder)
    club = Club.from_row({"id": 4})
    Tagged.target.cache(tagged, club)
    await tagged.refresh_from_db(fields=["label"])
    assert Tagged.target.cached(tagged) is club
    await tagged.refresh_from_db()
    assert "target" not in tagged._state.related
    Tagged.target.cache(tagged, club)
    await tagged.refresh_from_db(fields=["target"])
    assert recorder.operations[-1].query.only_fields == ("target_type_id", "target_id")
    assert "target" not in tagged._state.related
    tagged.target = Member.from_row(member_row(id=4))
    assert "target_type_id" not in tagged.__dict__
    await tagged.refresh_from_db()
    assert tagged._state.generic == {}
    assert tagged.target_type_id == 3, "a content type a pending assignment left unset is reloaded"
