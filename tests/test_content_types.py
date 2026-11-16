"""Content type lookups by model and by id (`adorm.content_types`) and assigning instances to generic foreign keys:
validation without a database, and the cache and the writes on the executor with the memory driver (concurrent
lookups, cancellation, a cache cleared mid-lookup, transactions on one slot, several databases)."""

import asyncio
import copy
import pickle

from decimal import Decimal

import pytest

from pydantic import Field

import adorm

from adorm import content_types, GenericForeignKey
from adorm.exceptions import ObjectDoesNotExist
from adorm.models import Model, ModelMeta, natural_key
from adorm.operations import FullClean, Get, Save
from adorm.settings import default_router
from tests import memory_driver as memory
from tests.clubmodels.activities import Activity
from tests.clubmodels.billing import InvoiceLine
from tests.clubmodels.clubs import Membership
from tests.clubmodels.contenttypes import ContentType
from tests.generated.library import Book, BookProxy, Edition, Note
from tests.memory_data import executor, tables, until

REPEAT = range(5)
CONTENT_TYPES = "contenttypes.ContentType"


def content_type_rows(membership=1, activity=2):
    return [
        {"id": membership, "app_label": "clubs", "model": "membership"},
        {"id": activity, "app_label": "activities", "model": "activity"},
        {"id": 3, "app_label": "clubs", "model": "gone"},
    ]


def generic_tables(membership=1, activity=2):
    """`tables()` with content types and invoice lines billing a membership and an activity."""
    data = tables()
    data[CONTENT_TYPES] = content_type_rows(membership, activity)
    line = {"invoice_id": 1, "quantity": 1, "unit_price": Decimal("1.00")}
    data["billing.InvoiceLine"] = [
        {"id": 1, "content_type_id": membership, "object_id": 1, "description": "fee", **line},
        {"id": 2, "content_type_id": activity, "object_id": 2, "description": "regatta", **line},
    ]
    return data


@pytest.fixture
async def db():
    async with executor(memory.MemoryDatabase(generic_tables())) as database:
        yield database


@pytest.fixture
async def single_slot():
    async with executor(memory.MemoryDatabase(generic_tables()), max_connections=1, acquire_timeout=0.5) as database:
        yield database


@pytest.fixture
async def two_databases():
    """The profile ``default`` with membership content type 1, the profile ``other`` with 11; returns the session of
    ``other``."""
    first = memory.MemoryDatabase(generic_tables())
    second = memory.MemoryDatabase(generic_tables(membership=11, activity=12))
    profiles = {
        name: adorm.ConnectionProfile(
            name, memory.MemoryDriver(database), on_acquire=memory.on_acquire, on_release=memory.on_release
        )
        for name, database in (("default", first), ("other", second))
    }
    adorm.configure(profiles=profiles, pool={"max_connections": 2, "acquire_timeout": 2.0})
    try:
        yield adorm.create_session("other")
    finally:
        from adorm.settings import close_pools

        await close_pools()
        adorm.configure()


@pytest.fixture
def recorded(monkeypatch):
    operations = []
    execute = adorm.Session.execute

    async def recording(self, operation):
        operations.append(operation)
        return await execute(self, operation)

    monkeypatch.setattr(adorm.Session, "execute", recording)
    return operations


@pytest.fixture
def gate(monkeypatch):
    """Makes the memory driver's reads of content types wait for the returned ``release`` event; ``started`` is set
    when one waits."""
    release, started = asyncio.Event(), asyncio.Event()
    original = memory.MemoryConnection._execute

    async def _execute(self, operation):
        if getattr(operation, "model", None) == CONTENT_TYPES:
            started.set()
            await release.wait()
        return await original(self, operation)

    monkeypatch.setattr(memory.MemoryConnection, "_execute", _execute)
    return release, started


def pool():
    return default_router(create=False).pools[0]


def kinds(recorded):
    return [operation.kind for operation in recorded]


async def membership(pk=2):
    return await Membership.objects.get(pk=pk)


# Natural keys and model declarations


def test_natural_key_of_a_model_an_instance_and_a_proxy():
    assert natural_key(Membership) == ("clubs", "membership")
    assert natural_key(Membership.from_row({"id": 1})) == ("clubs", "membership")
    assert natural_key(BookProxy) == ("library", "book")
    assert natural_key(BookProxy, for_concrete_model=False) == ("library", "bookproxy")
    assert natural_key(Edition) == ("library", "edition"), "a multi-table inheritance child has its own"
    with pytest.raises(TypeError, match="model class or instance"):
        natural_key("clubs.Membership")


def test_model_meta_concrete_model():
    assert BookProxy._meta.concrete_model == "library.Book"
    assert Book._meta.concrete_model is None
    with pytest.raises(ValueError, match="not a model label"):
        ModelMeta("tests.Proxy", concrete_model="book")
    with pytest.raises(ValueError, match="proxy of itself"):
        ModelMeta("tests.Proxy", concrete_model="tests.Proxy")


def test_generic_foreign_key_for_concrete_model():
    assert InvoiceLine.item.for_concrete_model is True
    relation = GenericForeignKey("owner_type", "owner_id", for_concrete_model=False)
    assert repr(relation) == "GenericForeignKey('owner_type', 'owner_id', for_concrete_model=False)"
    with pytest.raises(TypeError, match="for_concrete_model is a bool"):
        GenericForeignKey(for_concrete_model=1)


# Assignment without a database


class Keyed(Model):
    _meta = ModelMeta("tests.Keyed", pk="code")
    code: str | None = None


class Owned(Model):
    _meta = ModelMeta("tests.Owned")
    id: int | None = None
    owner_type_id: int | None = None
    owner_id: int | None = Field(default=None, ge=0)
    owner = GenericForeignKey("owner_type", "owner_id", for_concrete_model=False)


def test_assignment_converts_the_primary_key_to_the_object_id_field():
    note = Note.from_row({"code": "8f0e6f4e-1d1b-4c55-9d5c-0b9a1b0e7a10", "content_type_id": 1, "object_id": "x"})
    note.item = Membership.from_row({"id": 7})
    assert note.object_id == "7", "an integer key in a text field"
    line = InvoiceLine.from_row({"id": 1, "content_type_id": 1, "object_id": 1})
    line.item = Book.from_row({"isbn": "9780000000001"})
    assert line.object_id == 9780000000001, "a text key of digits in an integer field"
    with pytest.raises(ValueError, match=r"the primary key 'abc' does not fit InvoiceLine.object_id"):
        line.item = Keyed.from_row({"code": "abc"})
    assert line.object_id == 9780000000001, "a refused assignment changes nothing"


def test_assignment_refuses_what_is_not_a_stored_model_instance():
    line = InvoiceLine.from_row({"id": 1, "content_type_id": 1, "object_id": 1})
    with pytest.raises(TypeError, match="item takes a model instance, not str"):
        line.item = "clubs.Membership"
    with pytest.raises(TypeError, match="item takes a model instance, not Model"):
        line.item = Model()
    with pytest.raises(ValueError, match="cannot assign an unsaved clubs.Membership instance to item"):
        line.item = Membership.model_construct(id=None)
    assert (line.content_type_id, line.object_id) == (1, 1)


def test_constructor_keyword_conflicts_and_json_input():
    item = Membership.from_row({"id": 1})
    with pytest.raises(ValueError, match="give either item or object_id, not both"):
        InvoiceLine(invoice_id=1, item=item, object_id=1, description="x", unit_price=1)
    with pytest.raises(ValueError, match="give either item or content_type_id, not both"):
        InvoiceLine(invoice_id=1, item=item, content_type_id=1, description="x", unit_price=1)
    with pytest.raises(ValueError, match="cannot assign an unsaved"):
        InvoiceLine(invoice_id=1, item=Membership.model_construct(id=None), description="x", unit_price=1)
    with pytest.raises(ValueError, match="generic foreign key: give 'content_type_id' and 'object_id'"):
        InvoiceLine.model_validate_json('{"invoice_id": 1, "item": 1, "description": "x", "unit_price": 1}')
    with pytest.raises(ValueError, match="content_type_id"):
        InvoiceLine(invoice_id=1, item=None, description="x", unit_price=1)


def test_a_pending_content_type_reads_as_unset_and_the_instance_as_assigned():
    line = InvoiceLine(invoice_id=1, item=Membership.from_row({"id": 1}), description="x", unit_price=1)
    assert line.object_id == 1
    assert "content_type_id" not in line.model_fields_set
    with pytest.raises(AttributeError, match="content_type_id is unset until the next write: item was assigned"):
        line.content_type_id  # noqa: B018 - the access raises
    assert "content_type_id" not in line.model_dump()
    owned = Owned(owner=Membership.from_row({"id": 4}))
    assert owned.owner_id == 4
    with pytest.raises(AttributeError, match="unset until the next write"):
        owned.owner_type_id  # noqa: B018 - the access raises
    owned.owner = None
    assert (owned.owner_type_id, owned.owner_id) == (None, None)


async def test_awaiting_a_pending_assignment_gives_the_instance_without_an_operation():
    item = Membership.from_row({"id": 1})
    line = InvoiceLine(invoice_id=1, item=item, description="x", unit_price=1)
    assert await line.item is item


def test_copies_and_pickles_keep_a_pending_assignment():
    line = InvoiceLine(invoice_id=1, item=Membership.from_row({"id": 1}), description="x", unit_price=1)
    for clone in (copy.copy(line), copy.deepcopy(line), pickle.loads(pickle.dumps(line))):
        assert InvoiceLine.item.assigned(clone) == Membership.from_row({"id": 1})
        with pytest.raises(AttributeError, match="unset until the next write"):
            clone.content_type_id  # noqa: B018 - the access raises


def natural_key_of(operation):
    """The ``(app_label, model)`` a content type read filters on."""
    return tuple(lookup.rhs for lookup in operation.query.filters[0].children)


class Recorder:
    """An executor that answers content type reads with ``content_type`` and records every operation."""

    def __init__(self, content_type=5):
        self.content_type = content_type
        self.operations = []

    async def execute(self, operation):
        self.operations.append(operation)
        if isinstance(operation, Get) and operation.model == CONTENT_TYPES:
            return {"id": self.content_type}
        if isinstance(operation, FullClean):
            return {}
        if isinstance(operation, Save):
            return {"id": 9}
        raise AssertionError(f"unexpected {operation!r}")


async def test_save_and_full_clean_resolve_the_content_type_on_the_instance_executor():
    recorder = Recorder()
    line = InvoiceLine(invoice_id=1, description="x", unit_price=1, item=Membership.from_row({"id": 3}))
    line._state.executor = recorder
    await line.full_clean()
    assert isinstance(recorder.operations[0], Get)
    assert dict(recorder.operations[1].values)["content_type_id"] == 5
    assert line.content_type_id == 5
    line.item = Activity.from_row({"id": 2})
    await line.save()
    get, save = recorder.operations[2:]
    assert get.query.model == CONTENT_TYPES
    assert (dict(save.values)["content_type_id"], dict(save.values)["object_id"]) == (5, 2)
    assert content_types.cached_id(Activity, session=recorder) == 5


async def test_a_failed_resolution_leaves_the_assignment_pending():
    class Missing(Recorder):
        async def execute(self, operation):
            self.operations.append(operation)
            raise ObjectDoesNotExist("no row")

    line = InvoiceLine(invoice_id=1, description="x", unit_price=1, item=Membership.from_row({"id": 3}))
    line._state.executor = missing = Missing()
    with pytest.raises(ObjectDoesNotExist, match="no contenttypes.ContentType row names clubs.membership"):
        await line.save()
    assert [type(operation) for operation in missing.operations] == [Get], "nothing is written"
    with pytest.raises(AttributeError, match="unset until the next write"):
        line.content_type_id  # noqa: B018 - the access raises
    line._state.executor = recorder = Recorder(6)
    await line.save()
    assert dict(recorder.operations[-1].values)["content_type_id"] == 6


async def test_proxy_instances_use_the_concrete_content_type_unless_the_relation_says_otherwise():
    recorder = Recorder()
    proxy = BookProxy.from_row({"isbn": "9780000000001"})
    line = InvoiceLine.from_row({"id": 1, "content_type_id": 1, "object_id": 1, "invoice_id": 1})
    line._state.executor = recorder
    line.item = proxy
    await line.save()
    assert natural_key_of(recorder.operations[0]) == ("library", "book")
    owned = Owned.from_row({"id": 1, "owner_type_id": None, "owner_id": None})
    owned._state.executor = recorder
    owned.owner = proxy
    await owned.save()
    assert natural_key_of(recorder.operations[2]) == ("library", "bookproxy")


# The cache on the memory driver


async def test_get_id_reads_once_and_get_model_and_get_for_model_use_the_cache(db, recorded):  # noqa: ARG001
    assert content_types.cached_id(Membership) is None
    assert await content_types.get_id(Membership) == 1
    assert kinds(recorded) == ["get"]
    assert content_types.cached_id(Membership) == 1
    assert await content_types.get_id(Membership.from_row({"id": 1})) == 1
    assert await content_types.get_model(1) is Membership
    row = await content_types.get_for_model(Membership)
    assert isinstance(row, ContentType)
    assert (row.pk, row.app_label, row.model) == (1, "clubs", "membership")
    assert row is not await content_types.get_for_model(Membership), "a new instance on every call"
    assert kinds(recorded) == ["get"]
    assert await content_types.get_model(2) is Activity
    assert content_types.cached_id(Activity) == 2, "reading by id caches the id by model"
    assert kinds(recorded) == ["get", "get"]
    assert await content_types.natural_keys([1, 2, None, 3]) == {
        1: ("clubs", "membership"),
        2: ("activities", "activity"),
        3: ("clubs", "gone"),
    }
    assert kinds(recorded) == ["get", "get", "get"]


async def test_several_uncached_ids_are_read_with_one_select(db, recorded):  # noqa: ARG001
    keys = await content_types.natural_keys([1, 2, 3, 99])
    assert sorted(keys) == [1, 2, 3]
    assert kinds(recorded) == ["select"]
    assert await content_types.natural_keys([3, 1]) == {3: ("clubs", "gone"), 1: ("clubs", "membership")}
    assert kinds(recorded) == ["select"]


async def test_a_missing_content_type_raises_and_is_not_cached(db, recorded):  # noqa: ARG001
    with pytest.raises(ContentType.DoesNotExist, match="no contenttypes.ContentType row names billing.invoiceline"):
        await content_types.get_id(InvoiceLine)
    with pytest.raises(ContentType.DoesNotExist):
        await content_types.get_id(InvoiceLine)
    assert kinds(recorded) == ["get", "get"], "a miss reads again"
    with pytest.raises(ContentType.DoesNotExist, match="no contenttypes.ContentType row has the primary key 99"):
        await content_types.get_model(99)
    with pytest.raises(LookupError, match="content type clubs.gone"):
        await content_types.get_model(3)
    with pytest.raises(LookupError, match="no model is registered under 'tests.Missing'"):
        await content_types.get_for_model(Membership, content_type_model="tests.Missing")


async def test_the_cache_belongs_to_the_profile(db):  # noqa: ARG001
    assert await content_types.get_id(Membership) == 1
    profile = adorm.settings.get_profile("default")
    adorm.settings.register_profile(profile.replace(), replace=True)
    assert content_types.cached_id(Membership) is None, "a replaced profile starts empty"
    assert await content_types.get_id(Membership) == 1
    content_types.clear()
    assert content_types.cached_id(Membership) is None


async def test_each_database_has_its_own_content_type_ids(two_databases):
    other = two_databases
    assert await content_types.get_id(Membership) == 1
    assert await content_types.get_id(Membership, session=other) == 11
    assert content_types.cached_id(Membership) == 1
    assert content_types.cached_id(Membership, session=other) == 11
    with other.activate():
        assert await asyncio.create_task(content_types.get_id(Activity)) == 12, "a task inherits the session"
        async with asyncio.TaskGroup() as group:
            task = group.create_task(content_types.get_model(12))
    assert task.result() is Activity
    assert content_types.cached_id(Activity) is None


async def test_create_on_another_session_writes_that_database_s_content_type(two_databases):
    other = two_databases
    item = await membership()
    assert await content_types.get_id(Membership) == 1, "cached for the current session"
    line = await other.bind(InvoiceLine).objects.create(invoice_id=1, item=item, description="x", unit_price=1)
    assert line.content_type_id == 11
    stored = await other.bind(InvoiceLine).objects.get(pk=line.pk)
    assert (stored.content_type_id, stored.object_id) == (11, 2)
    lines = await other.bind(InvoiceLine).objects.bulk_create(
        [InvoiceLine(invoice_id=1, item=item, description=str(n), unit_price=1) for n in range(2)]
    )
    assert [line.content_type_id for line in lines] == [11, 11]


@pytest.mark.parametrize("attempt", REPEAT)
async def test_a_clear_during_a_lookup_keeps_its_result_out_of_the_cache(db, gate, attempt):  # noqa: ARG001
    release, started = gate
    lookup = asyncio.create_task(content_types.get_id(Membership))
    await started.wait()
    content_types.clear()
    release.set()
    assert await lookup == 1
    assert content_types.cached_id(Membership) is None
    assert await content_types.get_id(Membership) == 1
    assert content_types.cached_id(Membership) == 1
    assert pool().in_use == 0


@pytest.mark.parametrize("attempt", REPEAT)
async def test_cancelling_one_of_concurrent_lookups_leaves_the_others(db, gate, recorded, attempt):  # noqa: ARG001
    release, started = gate
    first = asyncio.create_task(content_types.get_id(Membership))
    second = asyncio.create_task(content_types.get_id(Membership))
    await started.wait()
    await until(lambda: kinds(recorded) == ["get", "get"])
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert await second == 1, "each lookup runs its own operation"
    assert content_types.cached_id(Membership) == 1
    await until(lambda: pool().in_use == 0)


@pytest.mark.parametrize("attempt", REPEAT)
async def test_cancelling_a_save_while_it_resolves_the_content_type(db, gate, recorded, attempt):  # noqa: ARG001
    release, started = gate
    line = await InvoiceLine.objects.get(pk=1)
    line.item = await Activity.objects.get(pk=2)
    recorded.clear()
    saving = asyncio.create_task(line.save())
    await started.wait()
    saving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await saving
    assert kinds(recorded) == ["get"], "nothing is written"
    with pytest.raises(AttributeError, match="unset until the next write"):
        line.content_type_id  # noqa: B018 - the access raises
    await until(lambda: pool().in_use == 0)
    release.set()
    await line.save()
    stored = await InvoiceLine.objects.get(pk=1)
    assert (stored.content_type_id, stored.object_id) == (2, 2)


@pytest.mark.parametrize("attempt", REPEAT)
async def test_lookups_and_assignments_in_a_transaction_use_its_slot(single_slot, attempt):  # noqa: ARG001
    async with adorm.atomic():
        item = await membership()
        line = await InvoiceLine.objects.create(invoice_id=1, item=item, description="x", unit_price=1)
        async with asyncio.TaskGroup() as group:
            ids = group.create_task(content_types.get_id(Activity))
            model = group.create_task(content_types.get_model(1))
        assert (ids.result(), model.result()) == (2, Membership)
        assert pool().in_use == 1
    stored = await InvoiceLine.objects.get(pk=line.pk)
    assert (stored.content_type_id, stored.object_id) == (1, 2)
    assert pool().in_use == 0


# Assignment on the memory driver


async def test_assigning_an_uncached_model_writes_its_content_type_on_save(db, recorded):  # noqa: ARG001
    line = await InvoiceLine.objects.get(pk=1)
    item = await membership()
    recorded.clear()
    line.item = item
    assert await line.item is item
    assert kinds(recorded) == []
    await line.save()
    assert kinds(recorded) == ["get", "save"]
    assert (line.content_type_id, line.object_id) == (1, 2)
    stored = await InvoiceLine.objects.get(pk=1)
    assert (stored.content_type_id, stored.object_id) == (1, 2)
    assert await stored.item == item


async def test_assigning_a_cached_model_sets_the_content_type_at_once(db, recorded):  # noqa: ARG001
    await content_types.get_id(Activity)
    line = await InvoiceLine.objects.get(pk=1)
    recorded.clear()
    line.item = Activity.from_row({"id": 2})
    assert (line.content_type_id, line.object_id) == (2, 2)
    await line.save()
    assert kinds(recorded) == ["save"]


async def test_assigning_a_field_afterwards_drops_the_pending_content_type(db, recorded):  # noqa: ARG001
    line = await InvoiceLine.objects.get(pk=1)
    line.item = await Activity.objects.get(pk=2)
    line.object_id = 1
    with pytest.raises(AttributeError, match="needs 'content_type_id', which is deferred"):
        line.item  # noqa: B018 - the access raises
    line.content_type_id = 1
    recorded.clear()
    await line.save()
    assert kinds(recorded) == ["save"]
    assert await (await InvoiceLine.objects.get(pk=1)).item == await membership(1)


async def test_update_fields_with_the_relation_name_writes_both_fields(db):  # noqa: ARG001
    line = await InvoiceLine.objects.get(pk=1)
    line.item = await Activity.objects.get(pk=2)
    line.description = "changed"
    await line.save(update_fields=["item"])
    stored = await InvoiceLine.objects.get(pk=1)
    assert (stored.content_type_id, stored.object_id, stored.description) == (2, 2, "fee")


async def test_create_and_bulk_create_resolve_the_content_type(db, recorded):  # noqa: ARG001
    item = await membership()
    recorded.clear()
    created = await InvoiceLine.objects.create(invoice_id=1, item=item, description="x", unit_price=1)
    assert kinds(recorded) == ["get", "create"]
    lines = await InvoiceLine.objects.bulk_create(
        [
            InvoiceLine(invoice_id=1, item=await Activity.objects.get(pk=n), description="y", unit_price=1)
            for n in (1, 2)
        ]
    )
    assert [line.content_type_id for line in [created, *lines]] == [1, 2, 2]
    stored = [(line.content_type_id, line.object_id) async for line in InvoiceLine.objects.filter(pk__gt=2)]
    assert sorted(stored) == [(1, 2), (2, 1), (2, 2)]


# Get-or-create and update-or-create defaults


async def test_get_or_create_and_update_or_create_take_an_instance_in_defaults(db, recorded):  # noqa: ARG001
    item = await membership()
    activity = await Activity.objects.get(pk=1)
    recorded.clear()
    line, created = await InvoiceLine.objects.get_or_create(
        description="new", defaults={"invoice_id": 1, "item": item, "unit_price": 1}
    )
    assert created
    assert kinds(recorded) == ["get", "get_or_create"], "the content type is read before the operation"
    assert (line.content_type_id, line.object_id) == (1, 2)
    updated, created = await InvoiceLine.objects.update_or_create(description="new", defaults={"item": activity})
    assert not created
    assert (updated.content_type_id, updated.object_id) == (2, 1)
    made, created = await InvoiceLine.objects.update_or_create(
        description="other",
        defaults={"item": activity},
        create_defaults={"invoice_id": 1, "item": item, "unit_price": 1},
    )
    assert created
    assert (made.content_type_id, made.object_id) == (1, 2)
    with pytest.raises(ValueError, match="give either item or object_id, not both"):
        await InvoiceLine.objects.get_or_create(description="x", defaults={"item": item, "object_id": 1})
    with pytest.raises(ValueError, match="give either item or content_type, not both"):
        await InvoiceLine.objects.update_or_create(description="x", defaults={"item": item, "content_type": 1})
    with pytest.raises(TypeError, match="item takes a model instance"):
        await InvoiceLine.objects.get_or_create(description="x", defaults={"item": 1})


# Assignments that change while a write resolves them


class GatedRecorder(Recorder):
    """A `Recorder` whose content type reads wait for ``release``; ``started`` is set when one waits, ``waiting``
    counts them."""

    def __init__(self, content_type=5):
        super().__init__(content_type)
        self.release, self.started = asyncio.Event(), asyncio.Event()
        self.waiting = 0

    async def execute(self, operation):
        if isinstance(operation, Get) and operation.model == CONTENT_TYPES:
            self.waiting += 1
            self.started.set()
            await self.release.wait()
        return await super().execute(operation)


@pytest.mark.parametrize("attempt", REPEAT)
async def test_save_and_full_clean_resolving_one_assignment_concurrently(attempt):  # noqa: ARG001
    gated = GatedRecorder()
    line = InvoiceLine.from_row({"id": 1, "content_type_id": 1, "object_id": 1, "invoice_id": 1})
    line._state.executor = gated
    line.item = Activity.from_row({"id": 2})
    saving = asyncio.create_task(line.save())
    cleaning = asyncio.create_task(line.full_clean())
    await until(lambda: gated.waiting == 2)
    gated.release.set()
    await asyncio.gather(saving, cleaning)
    assert InvoiceLine.item.assigned(line) is None
    writes = [dict(operation.values) for operation in gated.operations if isinstance(operation, Save | FullClean)]
    assert len(writes) == 2
    assert all((values["content_type_id"], values["object_id"]) == (5, 2) for values in writes)
    assert (line.content_type_id, line.object_id) == (5, 2)


@pytest.mark.parametrize("attempt", REPEAT)
async def test_reassigning_while_a_save_resolves_stores_the_new_instance(db, gate, attempt):  # noqa: ARG001
    release, started = gate
    line = await InvoiceLine.objects.get(pk=2)
    line.item = await Activity.objects.get(pk=2)
    saving = asyncio.create_task(line.save())
    await started.wait()
    line.item = await membership(1)
    release.set()
    await saving
    stored = await InvoiceLine.objects.get(pk=2)
    assert (stored.content_type_id, stored.object_id) == (1, 1), "the membership's content type and id"
    assert await stored.item == await membership(1)


@pytest.mark.parametrize("attempt", REPEAT)
async def test_setting_the_fields_while_a_save_resolves_keeps_them(db, gate, attempt):  # noqa: ARG001
    release, started = gate
    line = await InvoiceLine.objects.get(pk=2)
    line.item = await Activity.objects.get(pk=1)
    saving = asyncio.create_task(line.save())
    await started.wait()
    line.content_type_id, line.object_id = 1, 1
    release.set()
    await saving
    stored = await InvoiceLine.objects.get(pk=2)
    assert (stored.content_type_id, stored.object_id) == (1, 1)


@pytest.mark.parametrize("attempt", REPEAT)
async def test_assigning_none_while_a_save_resolves_stores_none(attempt):  # noqa: ARG001
    gated = GatedRecorder()
    owned = Owned.from_row({"id": 1, "owner_type_id": None, "owner_id": None})
    owned._state.executor = gated
    owned.owner = Membership.from_row({"id": 4})
    saving = asyncio.create_task(owned.save())
    await gated.started.wait()
    owned.owner = None
    gated.release.set()
    await saving
    save = gated.operations[-1]
    assert isinstance(save, Save)
    assert (dict(save.values)["owner_type_id"], dict(save.values)["owner_id"]) == (None, None)
    assert (owned.owner_type_id, owned.owner_id) == (None, None)


# The cache shared by profiles of one database


class NamedMemoryDriver(memory.MemoryDriver):
    def database_key(self, profile):  # noqa: ARG002 - every profile names the same database
        return ("memory", id(self.database))


async def test_profiles_whose_driver_names_one_database_share_the_cache():
    database = memory.MemoryDatabase(generic_tables())
    driver = NamedMemoryDriver(database)
    profiles = {
        name: adorm.ConnectionProfile(name, driver, on_acquire=memory.on_acquire, on_release=memory.on_release)
        for name in ("default", "user_7")
    }
    adorm.configure(profiles=profiles, pool={"max_connections": 2, "acquire_timeout": 2.0})
    try:
        user = adorm.create_session("user_7")
        assert await content_types.get_id(Membership) == 1
        assert content_types.cached_id(Membership, session=user) == 1, "shared with the profile user_7"
        profile = adorm.settings.get_profile("default")
        adorm.settings.register_profile(profile.replace(), replace=True)
        assert content_types.cached_id(Membership) == 1, "a replaced profile naming the same database keeps it"
    finally:
        from adorm.settings import close_pools

        await close_pools()
        adorm.configure()
