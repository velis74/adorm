"""`ModelMeta` schema hash and ORM names, and the `ReverseOneToOne` descriptor."""

import asyncio

import pytest

from pydantic import Field

from adorm import ForeignKey, Model, ModelMeta, ObjectDoesNotExist, ReverseOneToOne
from adorm.models import get_relations, model_schema, RelatedObject
from adorm.operations import Get
from adorm.query import Query


class Place(Model):
    _meta = ModelMeta("onetoone.Place", schema_hash="0123456789abcdef", orm_names={"json_": "json"})
    id: int | None = None
    name: str = ""
    json_: str = Field(default="", alias="json")
    restaurant = ReverseOneToOne("onetoone.Restaurant", column="place_id")


class Restaurant(Model):
    _meta = ModelMeta("onetoone.Restaurant", pk="place_id")
    place_id: int | None = None
    seats: int = 0
    place = ForeignKey("onetoone.Place")


class Recorder:
    def __init__(self, *results):
        self.operations = []
        self.results = list(results)

    async def execute(self, operation):
        self.operations.append(operation)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def test_model_meta_defaults():
    meta = ModelMeta("a.B")
    assert meta.schema_hash is None
    assert dict(meta.orm_names) == {}
    assert meta.orm_name("name") == "name"


def test_model_meta_takes_none_for_no_orm_names():
    assert ModelMeta("a.B", orm_names=None) == ModelMeta("a.B")
    assert dict(ModelMeta("a.B", orm_names=None).orm_names) == {}


class SignedPlace(Place):
    _meta = ModelMeta("onetoone.SignedPlace", pk="place_ptr_id", orm_names={"class_": "class"})
    place_ptr_id: int | None = None
    class_: str = Field(default="", alias="class")


class PlaceProxy(Place):
    _meta = ModelMeta("onetoone.PlaceProxy")


def test_subclasses_map_the_names_they_inherit():
    assert dict(SignedPlace._meta.orm_names) == {"json_": "json", "class_": "class"}
    assert SignedPlace._meta.orm_name("json_") == "json"
    assert PlaceProxy._meta.orm_name("json_") == "json"
    assert dict(Place._meta.orm_names) == {"json_": "json"}
    assert model_schema(PlaceProxy).orm_names == {"json_": "json"}


def test_a_subclass_may_map_an_inherited_name_differently():
    class Renamed(Place):
        _meta = ModelMeta("onetoone.Renamed", orm_names={"json_": "payload"})

    assert Renamed._meta.orm_name("json_") == "payload"


def test_model_meta_orm_names_are_read_only_and_map_names():
    meta = Place._meta
    assert meta.orm_name("json_") == "json"
    assert meta.orm_name("name") == "name"
    with pytest.raises(TypeError):
        meta.orm_names["x"] = "y"


def test_model_meta_copies_orm_names():
    names = {"json_": "json"}
    meta = ModelMeta("a.B", orm_names=names)
    names["copy_"] = "copy"
    assert dict(meta.orm_names) == {"json_": "json"}


def test_model_meta_compares_by_value():
    assert ModelMeta("a.B", orm_names={"x_": "x"}) == ModelMeta("a.B", orm_names={"x_": "x"})
    assert ModelMeta("a.B", schema_hash="1") != ModelMeta("a.B", schema_hash="2")
    assert hash(ModelMeta("a.B", schema_hash="1")) == hash(ModelMeta("a.B", schema_hash="1"))


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"schema_hash": ""}, ValueError),
        ({"schema_hash": 5}, ValueError),
        ({"orm_names": [("a", "b")]}, TypeError),
        ({"orm_names": {"not a name": "x"}}, ValueError),
        ({"orm_names": {"x_": ""}}, ValueError),
    ],
)
def test_model_meta_rejects_invalid_values(kwargs, error):
    with pytest.raises(error):
        ModelMeta("a.B", **kwargs)


def test_model_meta_arguments_after_pk_are_keyword_only():
    with pytest.raises(TypeError):
        ModelMeta("a.B", "id", "hash")


def test_orm_names_must_name_fields_or_relations():
    with pytest.raises(TypeError, match="names no field or relation: nothing"):

        class Broken(Model):
            _meta = ModelMeta("onetoone.Broken", orm_names={"nothing": "x"})
            id: int | None = None


def test_reverse_one_to_one_is_a_relation_outside_the_schema():
    assert get_relations(Place)["restaurant"] is Place.restaurant
    assert Place.restaurant.column == "place_id"
    assert repr(Place.restaurant) == "ReverseOneToOne('onetoone.Restaurant', column='place_id')"
    assert "restaurant" not in Place.model_json_schema()["properties"]
    assert model_schema(Place).relations["restaurant"] == "onetoone.Restaurant"


def test_reverse_one_to_one_rejects_an_invalid_column():
    with pytest.raises(ValueError, match="not a field name"):
        ReverseOneToOne("onetoone.Restaurant", column="no column")


async def test_reverse_one_to_one_loads_and_caches_the_related_row():
    executor = Recorder({"place_id": 3, "seats": 40})
    place = Place.from_row({"id": 3, "name": "Inn"}, executor=executor)
    related = place.restaurant
    assert isinstance(related, RelatedObject)
    assert executor.operations == []
    restaurant = await related
    assert isinstance(restaurant, Restaurant)
    assert restaurant.seats == 40
    assert executor.operations == [Get(Query("onetoone.Restaurant").filter(place_id=3))]
    assert await place.restaurant is restaurant
    assert len(executor.operations) == 1


async def test_reverse_one_to_one_raises_the_targets_does_not_exist():
    executor = Recorder(ObjectDoesNotExist("no row"))
    place = Place.from_row({"id": 3}, executor=executor)
    with pytest.raises(Restaurant.DoesNotExist):
        await place.restaurant


async def test_reverse_one_to_one_keeps_a_model_specific_error():
    executor = Recorder(Restaurant.DoesNotExist("gone"))
    place = Place.from_row({"id": 3}, executor=executor)
    with pytest.raises(Restaurant.DoesNotExist, match="gone"):
        await place.restaurant


async def test_reverse_one_to_one_uses_the_select_related_row():
    executor = Recorder()
    place = Place.from_row({"id": 3, "restaurant": {"place_id": 3, "seats": 12}}, executor=executor)
    restaurant = await place.restaurant
    assert restaurant.seats == 12
    assert executor.operations == []


async def test_reverse_one_to_one_cached_none_means_no_related_row():
    executor = Recorder()
    place = Place.from_row({"id": 3, "restaurant": None}, executor=executor)
    with pytest.raises(Restaurant.DoesNotExist):
        await place.restaurant
    assert executor.operations == []


async def test_reverse_one_to_one_needs_a_primary_key():
    with pytest.raises(ValueError, match="needs a primary key"):
        await Place(name="new").restaurant


async def test_concurrent_loads_of_a_reverse_one_to_one_each_return_the_row():
    gate = asyncio.Event()

    class Slow:
        def __init__(self):
            self.calls = 0

        async def execute(self, _operation):
            self.calls += 1
            await gate.wait()
            return {"place_id": 3, "seats": 5}

    executor = Slow()
    place = Place.from_row({"id": 3}, executor=executor)
    tasks = [asyncio.create_task(place.restaurant.relation.load(place)) for _ in range(3)]
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*tasks)
    assert all(isinstance(result, Restaurant) and result.pk == 3 for result in results)
    assert await place.restaurant in results
