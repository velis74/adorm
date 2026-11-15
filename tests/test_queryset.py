"""`QuerySet`: building without I/O, one operation per terminal method, result shaping."""

import asyncio
import copy
import decimal
import pickle
import uuid

from typing import Any

import pytest

from adorm import (
    create_session,
    F,
    ForeignKey,
    Lower,
    Model,
    ModelMeta,
    MultipleObjectsReturned,
    ObjectDoesNotExist,
    Q,
    QuerySet,
)
from adorm.executor import bind_transaction
from adorm.operations import Count, Create, Delete, Exists, First, Get, GetOrCreate, Last, Operation, Select, Update
from adorm.query import Query


class Team(Model):
    _meta = ModelMeta("querysettest.Team")
    id: int | None = None
    name: str = ""


class Player(Model):
    _meta = ModelMeta("querysettest.Player")
    id: int | None = None
    name: str
    fee: decimal.Decimal = decimal.Decimal("10")
    team_id: int | None = None
    team = ForeignKey("querysettest.Team")


class Executor:
    """Records operations and answers with the queued results; an exception result is raised."""

    def __init__(self, *results: Any) -> None:
        self.operations: list[Operation] = []
        self.results = list(results)

    async def execute(self, operation: Operation) -> Any:
        self.operations.append(operation)
        result = self.results.pop(0) if self.results else None
        if isinstance(result, BaseException):
            raise result
        return result


def players(executor: Executor) -> QuerySet:
    return QuerySet(Player, executor)


# Construction and building


def test_queryset_defaults_to_every_row():
    qs = players(Executor())
    assert qs.query == Query("querysettest.Player")
    assert qs.model is Player


def test_queryset_checks_the_model_and_query():
    class Abstract(Model):
        name: str = ""

    with pytest.raises(TypeError, match="abstract"):
        QuerySet(Abstract, Executor())
    with pytest.raises(ValueError, match="querysettest.Team is not a query on querysettest.Player"):
        QuerySet(Player, Executor(), Query("querysettest.Team"))


def test_building_returns_new_querysets_without_io():
    executor = Executor()
    base = players(executor)
    built = (
        base.filter(Q(name__icontains="a") | Q(fee__gt=5), team_id=1)
        .exclude(name="X")
        .annotate(lower=Lower("name"))
        .order_by("-fee", "name")
        .select_related("team")
        .only("name", "fee")
        .distinct()[2:10]
    )
    assert executor.operations == []
    assert base.query == Query("querysettest.Player")
    assert (
        built.query
        == (
            Query("querysettest.Player")
            .filter(Q(name__icontains="a") | Q(fee__gt=5), team_id=1)
            .exclude(name="X")
            .annotate(lower=Lower("name"))
            .order_by("-fee", "name")
            .select_related("team")
            .only("name", "fee")
            .distinct()[2:10]
        )
    )
    assert built.executor is executor
    assert base.all() is not base
    assert base.all().query == base.query


def test_every_building_method_maps_to_the_query():
    qs = players(Executor())
    query = Query("querysettest.Player")
    assert qs.values("name").query == query.values("name")
    assert qs.values_list("name", flat=True).query == query.values_list("name", flat=True)
    assert qs.defer("fee").query == query.defer("fee")
    assert qs.none().query == query.none()
    assert qs.select_for_update(nowait=True, of=["self"]).query == query.select_for_update(nowait=True, of=["self"])
    assert qs.distinct("name").query == query.distinct("name")


def test_queryset_is_immutable():
    qs = players(Executor())
    with pytest.raises(AttributeError, match="immutable"):
        qs.query = Query("querysettest.Player")


def test_combining_querysets():
    executor = Executor()
    a, b = players(executor).filter(name="A"), players(executor).filter(name="B")
    assert (a | b).query == a.query | b.query
    assert (a & b).query == a.query & b.query
    with pytest.raises(TypeError, match="different sessions"):
        a | players(Executor())  # noqa: B018
    with pytest.raises(TypeError):
        a | a.query  # noqa: B018


def test_queryset_is_a_subquery():
    teams = QuerySet(Team, Executor()).filter(name__startswith="A")
    qs = players(Executor()).filter(team_id__in=teams.values("id"))
    assert qs.query == Query("querysettest.Player").filter(
        team_id__in=Query("querysettest.Team").filter(name__startswith="A").values("id")
    )


def test_integer_index_is_refused():
    with pytest.raises(TypeError, match="takes a slice"):
        players(Executor())[0]  # type: ignore[index]


# Terminal methods


async def test_iteration_builds_instances_bound_to_the_executor():
    executor = Executor(
        [{"id": 1, "name": "Ana", "team_id": 3, "team": {"id": 3, "name": "Red"}, "lower": "ana"}, {"id": 2}]
    )
    qs = players(executor).select_related("team")
    rows = [p async for p in qs]
    assert executor.operations == [Select(qs.query)]
    assert [p.pk for p in rows] == [1, 2]
    assert rows[0].lower == "ana"
    assert rows[0]._state.executor is executor
    assert (await rows[0].team).name == "Red"
    with pytest.raises(AttributeError, match="deferred"):
        rows[1].name  # noqa: B018


async def test_values_rows():
    executor = Executor(
        [{"name": "Ana", "fee": 1}],
        [{"name": "Ana", "fee": 1}],
        [{"name": "Ana"}],
        [{"name": "Ana", "fee": 1}],
        [{"name": "Ana", "fee": 1}],
    )
    qs = players(executor)
    assert [r async for r in qs.values("name", "fee")] == [{"name": "Ana", "fee": 1}]
    assert [r async for r in qs.values_list("fee", "name")] == [(1, "Ana")]
    assert [r async for r in qs.values_list("name", flat=True)] == ["Ana"]
    named = [r async for r in qs.values_list("name", "fee", named=True)]
    assert named[0].name == "Ana"
    assert named[0].fee == 1
    assert [r async for r in qs.values_list()] == [("Ana", 1)]


async def test_get():
    executor = Executor({"id": 1, "name": "Ana"})
    qs = players(executor).filter(fee__gt=1)
    player = await qs.get(name="Ana")
    assert executor.operations == [Get(qs.query.filter(name="Ana"))]
    assert player.name == "Ana"
    assert player._state.executor is executor


async def test_get_maps_errors_to_the_model_exceptions():
    executor = Executor(ObjectDoesNotExist("none"), MultipleObjectsReturned("many"))
    qs = players(executor)
    with pytest.raises(Player.DoesNotExist, match="none") as caught:
        await qs.get(pk=1)
    assert isinstance(caught.value.__cause__, ObjectDoesNotExist)
    with pytest.raises(Player.MultipleObjectsReturned, match="many"):
        await qs.get()


async def test_errors_of_another_model_pass_unchanged():
    missing, several = Team.DoesNotExist("team missing"), Team.MultipleObjectsReturned("teams")
    qs = players(Executor(missing, several))
    with pytest.raises(Team.DoesNotExist) as caught:
        await qs.get_or_create(name="x")
    assert caught.value is missing
    assert not isinstance(caught.value, Player.DoesNotExist)
    with pytest.raises(Team.MultipleObjectsReturned) as caught_many:
        await qs.get(pk=1)
    assert caught_many.value is several


async def test_model_specific_errors_pass_unchanged():
    error = Player.DoesNotExist("as is")
    executor = Executor(error)
    with pytest.raises(Player.DoesNotExist) as caught:
        await players(executor).get(pk=1)
    assert caught.value is error


async def test_first_and_last():
    executor = Executor({"id": 1, "name": "Ana"}, None, {"id": 9}, None)
    qs = players(executor).order_by("name")
    assert (await qs.first()).pk == 1
    assert await qs.first() is None
    assert (await qs.last()).pk == 9
    assert await qs.last() is None
    assert executor.operations == [First(qs.query), First(qs.query), Last(qs.query), Last(qs.query)]


async def test_count_exists_update_delete():
    executor = Executor(5, True, 2, (3, {"querysettest.Player": 3}))
    qs = players(executor).filter(team_id=1)
    assert await qs.count() == 5
    assert await qs.exists() is True
    assert await qs.update(fee=F("fee") + 1) == 2
    assert await qs.delete() == (3, {"querysettest.Player": 3})
    assert executor.operations == [
        Count(qs.query),
        Exists(qs.query),
        Update(qs.query, {"fee": F("fee") + 1}),
        Delete(qs.query),
    ]


async def test_update_and_delete_refuse_what_the_operation_refuses():
    qs = players(Executor())
    with pytest.raises(TypeError, match="at least one"):
        await qs.update()
    with pytest.raises(TypeError, match="slice"):
        await qs[:3].update(fee=1)
    with pytest.raises(TypeError, match="slice"):
        await qs[:3].delete()
    with pytest.raises(TypeError, match="slice"):
        await qs[:3].last()


async def test_none_runs_no_operation():
    executor = Executor()
    qs = players(executor).none()
    assert [p async for p in qs] == []
    assert await qs.count() == 0
    assert await qs.exists() is False
    assert await qs.first() is None
    assert await qs.last() is None
    assert await qs.update(fee=1) == 0
    assert await qs.delete() == (0, {})
    with pytest.raises(Player.DoesNotExist):
        await qs.get()
    assert executor.operations == []


async def test_create_and_get_or_create_run_their_operation_after_none():
    executor = Executor({"id": 3}, ({"id": 4, "name": "Bo"}, True))
    qs = players(executor).none()
    assert (await qs.create(name="Ana")).pk == 3
    player, created = await qs.get_or_create(name="Bo")
    assert (player.pk, created) == (4, True)
    assert executor.operations == [
        Create("querysettest.Player", {"name": "Ana"}),
        GetOrCreate(qs.query, {"name": "Bo"}, {}),
    ]


def test_copy_and_pickle_keep_model_executor_and_query():
    session = create_session("default", f"test-{uuid.uuid4().hex}")
    qs = session.Player.filter(name="Ana").order_by("id")
    assert copy.copy(qs) is qs
    assert copy.deepcopy(qs) is qs
    assert copy.deepcopy([qs])[0] is qs
    restored = pickle.loads(pickle.dumps(qs))
    assert (restored.model, restored.executor, restored.query) == (Player, session, qs.query)


async def test_create_validates_and_inserts():
    executor = Executor({"id": 7, "fee": decimal.Decimal(10)})
    team = Team.from_row({"id": 3, "name": "Red"})
    player = await players(executor).create(name="Ana", team=team)
    assert executor.operations == [Create("querysettest.Player", {"name": "Ana", "team_id": 3})]
    assert player.pk == 7
    assert player.fee == decimal.Decimal(10)
    assert player._state.adding is False
    assert player._state.executor is executor
    assert await player.team is team


async def test_create_with_a_primary_key_and_invalid_values():
    executor = Executor({"id": 5})
    await players(executor).create(id=5, name="Ana")
    assert executor.operations == [Create("querysettest.Player", {"id": 5, "name": "Ana"})]
    with pytest.raises(ValueError, match="fee"):
        await players(executor).create(name="Ana", fee="abc")
    assert len(executor.operations) == 1


async def test_get_or_create():
    executor = Executor(({"id": 1, "name": "Ana"}, True), ({"id": 1, "name": "Ana"}, 0))
    qs = players(executor)
    player, created = await qs.get_or_create(name="Ana", defaults={"fee": 5})
    assert created is True
    assert player.pk == 1
    assert player._state.executor is executor
    assert executor.operations[0] == GetOrCreate(qs.query, {"name": "Ana"}, {"fee": 5})
    assert (await qs.get_or_create(name="Ana"))[1] is False


@pytest.mark.parametrize("attempt", range(20))
async def test_concurrent_terminal_methods_share_nothing(attempt):  # noqa: ARG001 - repeated
    # Several tasks run terminal methods of one query set at once; each gets its own result.
    release = asyncio.Event()
    started = 0

    class Slow:
        async def execute(self, operation: Operation) -> Any:
            nonlocal started
            started += 1
            if started == 5:
                release.set()
            await release.wait()
            return operation

    qs = players(Slow())
    results = await asyncio.gather(*(qs.filter(fee=n).count() for n in range(5)))
    assert results == [Count(qs.filter(fee=n).query) for n in range(5)]


# Bound models delegate to their query set


async def test_bound_model_methods_run_on_its_session():
    session = create_session("default", f"test-{uuid.uuid4().hex}")
    executor = Executor(
        2, True, {"id": 1}, {"id": 1}, {"id": 2}, [{"id": 3}], 1, (1, {}), {"id": 4}, ({"id": 1}, False)
    )
    bound = session.Player
    with bind_transaction(session, executor):
        assert await bound.count() == 2
        assert await bound.exists() is True
        assert (await bound.get(pk=1)).pk == 1
        assert (await bound.first()).pk == 1
        assert (await bound.last()).pk == 2
        assert [p.pk async for p in bound] == [3]
        assert await bound.update(fee=1) == 1
        assert await bound.delete() == (1, {})
        assert (await bound.create(name="X")).pk == 4
        assert (await bound.get_or_create(name="X"))[1] is False
    assert [op.kind for op in executor.operations] == [
        "count",
        "exists",
        "get",
        "first",
        "last",
        "select",
        "update",
        "delete",
        "create",
        "get_or_create",
    ]
    query = Query("querysettest.Player")
    assert bound.all().executor is session
    assert bound.filter(name="A").query == query.filter(name="A")
    assert bound.exclude(name="A").query == query.exclude(name="A")
    assert bound.annotate(n=Lower("name")).query == query.annotate(n=Lower("name"))
    assert bound.order_by("name").query == query.order_by("name")
    assert bound.values("name").query == query.values("name")
    assert bound.values_list("name", flat=True).query == query.values_list("name", flat=True)
    assert bound.only("name").query == query.only("name")
    assert bound.defer("name").query == query.defer("name")
    assert bound.select_related("team").query == query.select_related("team")
    assert bound.distinct().query == query.distinct()
    assert bound.none().query == query.none()
    assert bound.select_for_update(skip_locked=True).query == query.select_for_update(skip_locked=True)
    with bind_transaction(session, executor):
        assert await bound.execute(Count(query)) is None
    assert executor.operations[-1] == Count(query)


def test_repr():
    executor = Executor()
    assert repr(players(executor).filter(name="A")).startswith("<QuerySet Query('querysettest.Player'")
