"""Prefetch lookups, generic foreign keys, in_bulk(), children of existing parents and full_clean(): building, encoding
and validation without a database, and prefetching on the executor with the memory driver (concurrent tasks,
transactions, cancellation)."""

import asyncio
import datetime
import pickle

from decimal import Decimal

import pytest

import adorm

from adorm import GenericForeignKey, GenericPrefetch, Prefetch, prefetch_related_objects
from adorm.models import get_generic_foreign_keys, get_relations, Model, ModelMeta, registry
from adorm.models.inheritance import child_values
from adorm.models.prefetch import normalize_lookups
from adorm.operations import CreateFromParent, FullClean, Get, InBulk, Operation, Select
from adorm.query import Query
from adorm.settings import default_router
from tests import memory_driver as memory
from tests.clubmodels.activities import Activity, Attendance, Competition
from tests.clubmodels.billing import InvoiceLine
from tests.clubmodels.clubs import Club, Member, Membership
from tests.memory_data import executor, tables, until

REPEAT = range(5)


# Prefetch lookups


def test_prefetch_paths():
    plain = Prefetch("memberships__member")
    assert (plain.prefetch_through, plain.prefetch_to) == ("memberships__member", "memberships__member")
    assert plain.levels() == 2
    stored = Prefetch("memberships__member", to_attr="people")
    assert stored.prefetch_to == "memberships__people"
    assert stored.current_prefetch_to(0) == "memberships"
    assert stored.current_prefetch_to(1) == "memberships__people"
    assert stored == Prefetch("memberships__member", Membership.objects.all(), to_attr="people")
    assert stored != plain
    assert len({stored, Prefetch("memberships__member", to_attr="people")}) == 1
    assert repr(stored) == "Prefetch('memberships__member', to_attr='people')"


@pytest.mark.parametrize(
    ("args", "kwargs", "error", "match"),
    [
        ((1,), {}, TypeError, "relation path"),
        (("a b",), {}, ValueError, "field path"),
        (("members",), {"to_attr": "a__b"}, ValueError, "not an attribute name"),
        (("members",), {"to_attr": 3}, ValueError, "not an attribute name"),
        (("members",), {"queryset": Member.objects.values("id")}, ValueError, "values"),
        (("members",), {"queryset": Member.objects.all()[:2]}, ValueError, "sliced"),
        (("members",), {"queryset": "Member"}, TypeError, "QuerySet or a Query"),
    ],
)
def test_prefetch_refuses(args, kwargs, error, match):
    with pytest.raises(error, match=match):
        Prefetch(*args, **kwargs)


def test_prefetch_takes_a_query_on_a_registered_model():
    lookup = Prefetch("memberships", Query("clubs.Membership").filter(active=True))
    ((query, model, nested),) = lookup.sources()
    assert (query.model, model, nested) == ("clubs.Membership", Membership, ())
    nested_lookup = Prefetch("memberships", Membership.objects.prefetch_related("member"))
    assert nested_lookup.sources()[0][2] == (Prefetch("member"),)


def test_generic_prefetch():
    lookup = GenericPrefetch("item", [Membership.objects.filter(active=True), Activity.objects.all()], to_attr="x")
    assert [model for _, model, _ in lookup.sources()] == [Membership, Activity]
    assert lookup.prefetch_to == "x"
    with pytest.raises(ValueError, match="different model"):
        GenericPrefetch("item", [Membership.objects.all(), Membership.objects.filter(active=True)])
    with pytest.raises(TypeError, match="iterable of query sets"):
        GenericPrefetch("item", "Membership")
    assert "GenericPrefetch('item'" in repr(lookup)


def test_prefetch_lookups_pickle():
    lookup = Prefetch("memberships", Query("clubs.Membership").filter(active=True), to_attr="active")
    copy = pickle.loads(pickle.dumps(lookup))
    assert (copy.prefetch_to, copy.queryset) == ("active", lookup.queryset)
    generic = pickle.loads(pickle.dumps(GenericPrefetch("item", [Query("clubs.Membership")])))
    assert generic.querysets == (Query("clubs.Membership"),)


def test_normalize_lookups():
    first = normalize_lookups((), ["memberships", Prefetch("memberships"), "memberships__member"])
    assert [lookup.prefetch_to for lookup in first] == ["memberships", "memberships__member"]
    prefixed = normalize_lookups(first, ["club"], "memberships__member")
    assert prefixed[-1].prefetch_through == "memberships__member__club"
    with pytest.raises(ValueError, match="given twice"):
        normalize_lookups(first, [Prefetch("memberships", Membership.objects.filter(active=True))])
    with pytest.raises(TypeError, match="relation path or a Prefetch"):
        normalize_lookups((), [3])


def test_query_set_prefetch_related():
    session = adorm.create_session()
    queryset = session.bind(Club).prefetch_related("memberships").prefetch_related("memberships", "activities")
    assert [lookup.prefetch_to for lookup in queryset.prefetch_lookups] == ["memberships", "activities"]
    assert queryset.filter(city="Koper").order_by("name").prefetch_lookups == queryset.prefetch_lookups
    assert queryset.prefetch_related(None).prefetch_lookups == ()
    assert "prefetch_related=" in repr(queryset)
    copy = pickle.loads(pickle.dumps(queryset))
    assert copy.prefetch_lookups == queryset.prefetch_lookups
    with pytest.raises(TypeError, match="after prefetch_related"):
        queryset.values("name")
    with pytest.raises(TypeError, match="after prefetch_related"):
        queryset.values_list("name")
    with pytest.raises(TypeError, match="after values"):
        session.bind(Club).values("name").prefetch_related("memberships")
    with pytest.raises(ValueError, match="given twice"):
        queryset.prefetch_related(Prefetch("memberships", Membership.objects.filter(active=True)))


# Generic foreign keys


def test_generic_foreign_key_descriptor():
    item = get_generic_foreign_keys(InvoiceLine)["item"]
    assert isinstance(item, GenericForeignKey)
    assert InvoiceLine.item is item
    assert "item" not in get_relations(InvoiceLine)
    assert "item" not in registry.model_schema("billing.InvoiceLine").relations
    assert (item.ct_column(InvoiceLine), item.fk_field) == ("content_type_id", "object_id")
    assert item.content_type_model(InvoiceLine) == "contenttypes.ContentType"
    assert repr(item) == "GenericForeignKey('content_type', 'object_id')"
    assert "item" not in InvoiceLine.model_json_schema()["properties"]


def test_generic_foreign_key_values_and_assignment():
    line = InvoiceLine.from_row({"id": 1, "content_type_id": 4, "object_id": 7, "invoice_id": 1})
    assert get_generic_foreign_keys(InvoiceLine)["item"].values(line) == (4, 7)
    line.item = None
    assert (line.content_type_id, line.object_id) == (None, None)
    with pytest.raises(TypeError, match="item takes a model instance, not str"):
        line.item = "line"
    deferred = InvoiceLine.from_row({"id": 1, "content_type_id": 4})
    with pytest.raises(AttributeError, match="needs 'object_id', which is deferred"):
        deferred.item  # noqa: B018 - the access raises
    with pytest.raises(ValueError, match="generic foreign key: give 'content_type_id' and 'object_id'"):
        InvoiceLine.model_validate_json('{"invoice_id": 1, "item": 1, "description": "x", "unit_price": 1}')


def test_generic_foreign_key_columns_are_fields():
    with pytest.raises(TypeError, match=r"Broken.owner is stored in 'owner_type_id', which is not a field"):

        class Broken(Model):
            _meta = ModelMeta("tests.Broken")
            id: int | None = None
            owner_id: int
            owner = GenericForeignKey("owner_type", "owner_id")

    class Tag(Model):
        _meta = ModelMeta("tests.GenericTag")
        id: int | None = None
        owner_type_id: int | None = None
        owner_id: str | None = None
        owner = GenericForeignKey("owner_type", "owner_id")

    try:
        owner = get_generic_foreign_keys(Tag)["owner"]
        assert owner.content_type_model(Tag) == "contenttypes.ContentType"
        assert owner.ct_column(Tag) == "owner_type_id"
    finally:
        registry.unregister("tests.GenericTag")


def test_registry_finds_the_model_of_a_content_type():
    assert registry.get_for_content_type("clubs", "membership") is Membership
    assert registry.get_for_content_type("activities", "Competition") is Competition
    with pytest.raises(LookupError, match="content type clubs.nothing"):
        registry.get_for_content_type("clubs", "nothing")


# Operations


def roundtrip(operation: Operation) -> Operation:
    data = operation.to_dict(registry)
    assert Operation.from_dict(data, registry) == operation
    return data


def test_in_bulk_operation():
    query = Query("clubs.Club")
    operation = InBulk(query, ids=[3, 1, 3])
    assert operation.ids == (3, 1)
    assert operation.select_query() == query.filter(pk__in=[3, 1])
    assert set(operation.constructs()) == {"lookup.in"}
    assert roundtrip(operation) == {
        "operation": "in_bulk",
        "query": query.to_dict(),
        "field_name": "pk",
        "ids": [{"value": 3}, {"value": 1}],
    }
    every = InBulk(query, "slug")
    assert every.select_query() is query
    assert list(every.constructs()) == []
    roundtrip(every)
    for args, error, match in [
        ((query[:2],), TypeError, "slice"),
        ((query.values("id"),), TypeError, "values"),
        ((query, "club__name"), ValueError, "field name"),
        ((query.only("name"), "slug"), TypeError, "defers"),
        ((query.defer("slug"), "slug"), TypeError, "defers"),
        ((query, "pk", "abc"), TypeError, "not a string"),
        ((query, "pk", [adorm.F("id")]), TypeError, "expression"),
    ]:
        with pytest.raises(error, match=match):
            InBulk(*args)
    assert InBulk(query.only("name"), "pk").field_name == "pk"


def test_create_from_parent_operation():
    operation = CreateFromParent("activities.Competition", 7, {"discipline": "Sprint"})
    assert roundtrip(operation) == {
        "operation": "create_from_parent",
        "model": "activities.Competition",
        "parent_pk": {"value": 7},
        "values": {"discipline": {"value": "Sprint"}},
    }
    with pytest.raises(ValueError, match="parent's primary key"):
        CreateFromParent("activities.Competition", None)
    with pytest.raises(TypeError, match="value, not an expression"):
        CreateFromParent("activities.Competition", adorm.F("id"))
    with pytest.raises(TypeError, match="cannot refer to a field"):
        CreateFromParent("activities.Competition", 1, {"discipline": adorm.F("name")})


def test_full_clean_operation():
    operation = FullClean(
        "billing.Invoice", 3, {"due": datetime.date(2026, 1, 1)}, exclude=["club", "club"], adding=False
    )
    assert operation.exclude == ("club",)
    data = roundtrip(operation)
    assert data["values"] == {"due": {"value": "2026-01-01"}}
    assert (data["validate_unique"], data["validate_constraints"], data["adding"]) == (True, True, False)
    assert FullClean("billing.Invoice", None).adding is True
    with pytest.raises(TypeError, match="not a string"):
        FullClean("billing.Invoice", None, exclude="due")
    with pytest.raises(ValueError, match="not a field name"):
        FullClean("billing.Invoice", None, exclude=["a b"])
    with pytest.raises(TypeError, match="expression"):
        FullClean("billing.Invoice", adorm.F("id"))
    with pytest.raises(TypeError):
        FullClean("billing.Invoice", 1, {"due": adorm.Value(1)})
    with pytest.raises(ValueError, match="flag|bool"):
        Operation.from_dict({**data, "adding": "yes"}, registry)


def test_child_values():
    link, parent, values = child_values(Competition, {"discipline": "Sprint", "entry_deadline": "2026-05-01"})
    assert (link.name, parent) == ("activity_ptr", Activity)
    assert values == {"discipline": "Sprint", "entry_deadline": datetime.date(2026, 5, 1)}
    with pytest.raises(ValueError, match="'name' is an inherited field"):
        child_values(Competition, {"name": "x"})
    with pytest.raises(ValueError, match="'pk' is an inherited field"):
        child_values(Competition, {"pk": 1})
    with pytest.raises(ValueError, match="'nothing' is not a field"):
        child_values(Competition, {"nothing": 1})
    with pytest.raises(ValueError, match="'discipline'"):
        child_values(Competition, {"discipline": "x" * 61})
    with pytest.raises(TypeError, match="not a multi-table inheritance child"):
        child_values(Activity, {})


async def test_full_clean_checks_exclude_names_before_running():
    line = InvoiceLine(invoice_id=1, content_type_id=1, object_id=1, description="x", unit_price=1)
    with pytest.raises(ValueError, match="'nothing' is not a field"):
        await line.full_clean(exclude=["nothing"])
    with pytest.raises(TypeError, match="not a string"):
        await line.full_clean(exclude="description")


# Prefetching on the executor (memory driver)


def generic_tables():
    """`tables()` with content types and invoice lines billing a membership and an activity."""
    data = tables()
    data["contenttypes.ContentType"] = [
        {"id": 1, "app_label": "clubs", "model": "membership"},
        {"id": 2, "app_label": "activities", "model": "activity"},
        {"id": 3, "app_label": "clubs", "model": "gone"},
    ]
    line = {"invoice_id": 1, "quantity": 1, "unit_price": Decimal("1.00")}
    data["billing.InvoiceLine"] = [
        {"id": 1, "content_type_id": 1, "object_id": 1, "description": "fee", **line},
        {"id": 2, "content_type_id": 2, "object_id": 2, "description": "regatta", **line},
        {"id": 3, "content_type_id": 1, "object_id": 99, "description": "missing", **line},
        {"id": 4, "content_type_id": 1, "object_id": 3, "description": "fee", **line},
    ]
    return data


@pytest.fixture
async def db():
    async with executor(memory.MemoryDatabase(generic_tables())) as database:
        yield database


def pool():
    return default_router(create=False).pools[0]


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
    """``gate(label)`` makes the memory driver's selects of ``label`` wait until the returned event is set; the second
    event is set when such a select starts waiting."""

    def install(label):
        release, started = asyncio.Event(), asyncio.Event()
        original = memory.MemoryConnection._execute

        async def _execute(self, operation):
            if isinstance(operation, Select) and operation.model == label:
                started.set()
                await release.wait()
            return await original(self, operation)

        monkeypatch.setattr(memory.MemoryConnection, "_execute", _execute)
        return release, started

    return install


async def members_of(club):
    return sorted([(await membership.member).first_name async for membership in club.memberships])


async def test_prefetch_on_the_memory_driver(db, recorded):  # noqa: ARG001
    rowing = await Club.objects.prefetch_related("memberships__member", "activities").get(slug="rowing")
    assert [operation.kind for operation in recorded] == ["get", "select", "select", "select"]
    recorded.clear()
    assert await members_of(rowing) == ["Ana", "Bojan", "Cvetka"]
    assert sorted([activity.name async for activity in rowing.activities]) == ["Regatta", "Sculling"]
    assert recorded == []


async def test_first_and_last_prefetch_too(db, recorded):  # noqa: ARG001
    first = await Club.objects.order_by("slug").prefetch_related("memberships").first()
    last = await Club.objects.order_by("slug").prefetch_related("memberships").last()
    assert [operation.kind for operation in recorded] == ["first", "select", "last", "select"]
    assert (await first.memberships.count(), await last.memberships.count()) == (3, 0)
    assert await Club.objects.filter(slug="none").prefetch_related("memberships").first() is None
    assert [operation.kind for operation in recorded][-1] == "first"


async def test_an_empty_result_runs_no_prefetch(db, recorded):  # noqa: ARG001
    assert [club async for club in Club.objects.filter(slug="none").prefetch_related("memberships")] == []
    assert [club async for club in Club.objects.none().prefetch_related("memberships")] == []
    assert [operation.kind for operation in recorded] == ["select"]


async def test_generic_foreign_keys_on_the_memory_driver(db, recorded):  # noqa: ARG001
    lines = [line async for line in InvoiceLine.objects.order_by("id")]
    recorded.clear()
    membership = await lines[0].item
    assert (type(membership), membership.pk, membership.fee) == (Membership, 1, Decimal("120.00"))
    assert (type(await lines[1].item), (await lines[1].item).name) == (Activity, "Regatta")
    assert await lines[2].item is None
    # an object for each line, a content type the first time it is seen; the second await of a line reads the cache
    assert [operation.kind for operation in recorded] == ["get"] * 5
    recorded.clear()
    prefetched = [
        line async for line in InvoiceLine.objects.order_by("id").prefetch_related(Prefetch("item", to_attr="billed"))
    ]
    # lines, memberships, activities: the content types are cached
    assert [operation.kind for operation in recorded] == ["select"] * 3
    assert [type(line.billed).__name__ for line in prefetched] == ["Membership", "Activity", "NoneType", "Membership"]
    generic = [
        line
        async for line in InvoiceLine.objects.order_by("id").prefetch_related(
            GenericPrefetch("item", [Membership.objects.filter(active=True)]), "item__club"
        )
    ]
    assert [None if (item := await line.item) is None else item.pk for line in generic] == [1, 2, None, None]
    assert (await (await generic[1].item).club).slug == "rowing"


async def test_a_content_type_of_an_unregistered_model_raises_lookup_error(db):  # noqa: ARG001
    line = await InvoiceLine.objects.get(pk=1)
    line.content_type_id = 3
    with pytest.raises(LookupError, match="content type clubs.gone"):
        await line.item
    with pytest.raises(LookupError, match="content type clubs.gone"):
        await prefetch_related_objects([line], "item")


@pytest.mark.parametrize("attempt", REPEAT)
async def test_concurrent_awaits_of_one_generic_foreign_key_agree(db, attempt):  # noqa: ARG001
    line = await InvoiceLine.objects.get(pk=1)
    items = await asyncio.gather(*(line.item for _ in range(10)))
    assert {item.pk for item in items} == {1}
    assert await line.item in items


@pytest.mark.parametrize("attempt", REPEAT)
async def test_concurrent_prefetches_share_one_slot_without_deadlock(attempt):  # noqa: ARG001
    async with executor(memory.MemoryDatabase(generic_tables()), max_connections=1, acquire_timeout=2.0):

        async def read():
            clubs = [club async for club in Club.objects.order_by("slug").prefetch_related("memberships__member")]
            return [await members_of(club) for club in clubs]

        results = await asyncio.gather(*(read() for _ in range(20)))
        assert results == [[["Ana", "Bojan", "Cvetka"], []]] * 20
        assert (pool().in_use, pool().size) == (0, 1)


@pytest.mark.parametrize("attempt", REPEAT)
async def test_prefetch_in_tasks_of_a_transaction_uses_its_slot(attempt):  # noqa: ARG001
    async with executor(memory.MemoryDatabase(generic_tables()), max_connections=1, acquire_timeout=0.5):
        async with adorm.atomic():
            await Membership.objects.filter(pk=3).update(active=True)
            async with asyncio.TaskGroup() as group:
                tasks = [
                    group.create_task(Club.objects.prefetch_related("memberships").get(slug="rowing")) for _ in range(5)
                ]
            for task in tasks:
                assert sorted([m.active async for m in task.result().memberships]) == [True, True, True]
            assert pool().in_use == 1
        assert pool().in_use == 0


@pytest.mark.parametrize("attempt", REPEAT)
async def test_cancelling_a_prefetch_leaves_no_slot_and_no_partial_cache(db, gate, attempt):  # noqa: ARG001
    clubs = [club async for club in Club.objects.order_by("slug")]
    release, started = gate("clubs.Member")
    task = asyncio.create_task(prefetch_related_objects(clubs, "memberships__member"))
    await started.wait()
    assert pool().in_use == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await until(lambda: pool().in_use == 0)
    rowing = clubs[0]
    memberships = rowing._state.related["memberships"]
    assert all("member" not in membership._state.related for membership in memberships)
    release.set()
    await prefetch_related_objects(clubs, "memberships__member")
    assert await members_of(rowing) == ["Ana", "Bojan", "Cvetka"]
    assert pool().in_use == 0


@pytest.mark.parametrize("attempt", REPEAT)
async def test_a_prefetch_racing_a_commit_reads_after_it(db, gate, attempt):  # noqa: ARG001
    release, started = gate("clubs.Membership")
    reader = asyncio.create_task(Club.objects.prefetch_related("memberships").get(slug="sailing"))
    await started.wait()
    async with adorm.create_session("default", "writer").atomic():
        await adorm.create_session("default", "writer").Membership.create(
            club_id=2, member_id=4, role="member", since=datetime.date(2026, 1, 1), fee=Decimal("1.00")
        )
    release.set()
    sailing = await reader
    assert [m.member_id async for m in sailing.memberships] == [4]


async def test_in_bulk_on_the_memory_driver(db, recorded):  # noqa: ARG001
    bulk = await Member.objects.in_bulk([1, 3, 42])
    assert {pk: member.first_name for pk, member in bulk.items()} == {1: "Ana", 3: "Cvetka"}
    assert await Member.objects.in_bulk([]) == {}
    assert len(await Member.objects.filter(level="beginner").in_bulk()) == 2
    assert [operation.kind for operation in recorded] == ["in_bulk", "in_bulk"]
    with pytest.raises(adorm.NotSupportedError, match="unique constraints"):
        await Member.objects.in_bulk(["ana@example.com"], field_name="email")
    with pytest.raises(adorm.TransactionManagementError, match="select_for_update"):
        await Member.objects.select_for_update().in_bulk([1])
    async with adorm.atomic():
        assert list(await Member.objects.select_for_update().in_bulk([1])) == [1]
    assert await Member.objects.none().in_bulk([1]) == {}


async def test_operations_the_memory_driver_does_not_declare_are_refused(db):  # noqa: ARG001
    with pytest.raises(adorm.NotSupportedError, match="create_from_parent"):
        await Competition.objects.create_from_parent(1, discipline="Sprint")
    attendance = await Attendance.objects.get(pk=1)
    with pytest.raises(adorm.NotSupportedError, match="full_clean"):
        await attendance.full_clean()
    assert pool().in_use == 0


async def test_prefetched_rows_serve_the_relation_query_set(db):  # noqa: ARG001
    rowing = await Club.objects.prefetch_related("memberships").get(slug="rowing")
    queryset = rowing.memberships
    assert (await queryset.count(), await queryset.exists()) == (3, True)
    assert isinstance(queryset, adorm.QuerySet)
    sailing = await Club.objects.prefetch_related("memberships").get(slug="sailing")
    assert (await sailing.memberships.count(), await sailing.memberships.exists()) == (0, False)
    assert [m async for m in sailing.memberships] == []


async def test_prefetch_runs_on_the_given_executor(db):  # noqa: ARG001
    clubs = [club async for club in Club.objects.all()]
    calls = []

    class Recording:
        async def execute(self, operation):
            calls.append(operation)
            return await adorm.create_session().execute(operation)

    await prefetch_related_objects(clubs, "activities", executor=Recording())
    assert [type(call) for call in calls] == [Select]


@pytest.fixture
def gate_reads(monkeypatch):
    """``gate_reads(label)`` makes the memory driver's reads (`Get` and `Select`) of ``label`` wait until the returned
    event is set; the second event is set when such a read starts waiting."""

    def install(label):
        release, started = asyncio.Event(), asyncio.Event()
        original = memory.MemoryConnection._execute

        async def _execute(self, operation):
            if isinstance(operation, Get | Select) and operation.model == label:
                started.set()
                await release.wait()
            return await original(self, operation)

        monkeypatch.setattr(memory.MemoryConnection, "_execute", _execute)
        return release, started

    return install


@pytest.mark.parametrize("attempt", REPEAT)
@pytest.mark.parametrize("prefetch", [False, True], ids=["await", "prefetch"])
async def test_a_generic_foreign_key_changed_during_its_load_is_not_cached_for_the_new_key(
    db,  # noqa: ARG001
    gate_reads,
    prefetch,
    attempt,  # noqa: ARG001
):
    line = await InvoiceLine.objects.get(pk=1)
    release, started = gate_reads("clubs.Membership")
    load = prefetch_related_objects([line], "item") if prefetch else line.item
    task = asyncio.ensure_future(load)
    await started.wait()
    line.object_id = 3
    release.set()
    loaded = await task
    if not prefetch:
        assert loaded.pk == 1, "the load returns the row of the key it read"
    assert (await line.item).pk == 3
    line.object_id = 1
    assert (await line.item).pk == 1


def cached(club):
    """The primary keys of what the prefetches cached on ``club``."""
    related = club._state.related
    memberships = related.get("memberships", ())
    members = [getattr(m._state.related.get("member"), "pk", None) for m in memberships]
    return [m.pk for m in memberships], members, [a.pk for a in related.get("activities", ())]


@pytest.mark.parametrize("relation", ["memberships", "memberships__member", "activities"])
async def test_a_prefetch_reads_its_keys_in_batches(db, recorded, monkeypatch, relation):  # noqa: ARG001
    from adorm.models import prefetch

    expected = [club async for club in Club.objects.order_by("slug")]
    await prefetch_related_objects(expected, relation)
    clubs = [club async for club in Club.objects.order_by("slug")]
    recorded.clear()
    monkeypatch.setattr(prefetch, "BATCH_SIZE", 1)
    await prefetch_related_objects(clubs, relation)
    assert len(recorded) >= len(clubs), "an operation per club"
    assert [cached(club) for club in clubs] == [cached(club) for club in expected]
    assert any(cached(club)[0] or cached(club)[2] for club in clubs)


async def test_a_many_to_many_prefetch_in_batches_keeps_the_query_set_order(db, recorded, monkeypatch):  # noqa: ARG001
    from adorm.models import prefetch

    for activity_id, member_id in [(1, 2), (1, 3), (2, 4), (2, 2)]:
        await Attendance.objects.create(activity_id=activity_id, member_id=member_id)
    ordered = Member.objects.order_by("-first_name")
    activities = [a async for a in Activity.objects.order_by("id")]
    recorded.clear()
    # Activity 1 has members 1-3, activity 2 members 2 and 4: one group each with three targets per Select.
    monkeypatch.setattr(prefetch, "BATCH_SIZE", 3)
    await prefetch_related_objects(activities, Prefetch("participants", queryset=ordered))
    assert [[m.first_name for m in a._state.related["participants"]] for a in activities] == [
        ["Cvetka", "Bojan", "Ana"],
        ["David", "Bojan"],
    ]
    # the links (one batch of the two activities), then the targets of each activity
    assert [operation.model for operation in recorded] == ["activities.Attendance", "clubs.Member", "clubs.Member"]
