"""Managers: declarations, binding, query set classes, ORM managers and driver methods."""

import asyncio
import copy
import datetime
import pickle
import re

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, ClassVar

import pytest

from adorm import (
    BoundModel,
    create_session,
    driver_method,
    InterfaceError,
    Manager,
    ManyToMany,
    Model,
    ModelMeta,
    NotSupportedError,
    ObjectDoesNotExist,
    QuerySet,
    ReverseForeignKey,
)
from adorm.executor import bind_transaction
from adorm.models import driver_methods, DriverMethod, find_driver_method, get_managers, model_schema, registry
from adorm.operations import Call, Count, Create, Delete, GetOrCreate, ModelRow, Operation, Select, Update
from adorm.query import Query

REPEAT = range(20)


class RentalQuerySet(QuerySet):
    def open(self) -> "RentalQuerySet":
        return self.filter(archived=False)

    def due_before(self, day: datetime.date) -> "RentalQuerySet":
        return self.filter(due__lt=day)

    @driver_method
    async def archive(self, reason: str = "") -> int:
        """Archive the loans."""

    @driver_method(queryset_only=True)
    async def renew(self, *, days: int) -> int: ...

    def _private(self) -> None: ...


class RentalManager(Manager):
    queryset_class = RentalQuerySet

    def get_queryset(self) -> RentalQuerySet:
        return super().get_queryset().filter(deleted=False)

    @driver_method
    async def lend(self, reader_id: int, book_id: str, /, *extra: int, days: int = 14, **options: str) -> "Rental": ...


class Rental(Model):
    _meta = ModelMeta("managertest.Rental")
    id: int | None = None
    reader_id: int = 0
    book_id: str = ""
    due: datetime.date | None = None
    archived: bool = False
    deleted: bool = False

    objects = RentalManager("objects")
    everything = Manager()
    deleted_loans = RentalQuerySet.as_manager("deleted_loans")


class Renter(Model):
    _meta = ModelMeta("managertest.Renter")
    id: int | None = None
    name: str = ""

    objects = Manager("objects")


class Bare(Model):
    _meta = ModelMeta("managertest.Bare")
    id: int | None = None


class StackQuerySet(QuerySet):
    @driver_method
    async def tidy(self) -> int: ...


class StackManager(Manager):
    queryset_class = StackQuerySet

    @driver_method
    async def stock(self, count: int) -> int: ...


class Stack(Model):
    """A model whose manager only has driver methods: its queries carry no ORM manager."""

    _meta = ModelMeta("managertest.Stack")
    id: int | None = None

    objects = StackManager(method_manager="objects")


class Borrower(Model):
    _meta = ModelMeta("managertest.Borrower")
    id: int | None = None

    loans = ReverseForeignKey("managertest.Rental", column="reader_id")
    favourites = ManyToMany(
        "managertest.Rental",
        through="managertest.Borrower_favourites",
        source_column="borrower_id",
        target_column="rental_id",
    )


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


def bound(executor: Executor, manager: str = "objects") -> tuple[BoundModel, Any]:
    session = create_session("default", f"managers-{id(executor)}")
    return session.bind(Rental, manager), bind_transaction(session, executor)


# Declarations


@contextmanager
def raises_in_set_name(expected: type[Exception], match: str) -> Iterator[None]:
    """Expect ``expected`` from ``__set_name__``; Python 3.11 wraps it in a RuntimeError."""
    with pytest.raises((expected, RuntimeError)) as caught:
        yield
    error = caught.value if isinstance(caught.value, expected) else caught.value.__cause__
    assert isinstance(error, expected)
    assert re.search(match, str(error))


def test_model_declares_a_plain_objects_manager():
    declared = get_managers(Bare)["objects"]
    assert type(declared) is Manager
    assert declared.name == "objects"
    assert declared.orm_name is None
    assert Bare.objects.orm_name is None
    assert Bare.objects.get_queryset().query == Query("managertest.Bare")


def test_managers_are_collected_by_name():
    assert list(get_managers(Rental)) == ["objects", "everything", "deleted_loans"]
    assert get_managers(Rental)["deleted_loans"].queryset_class is RentalQuerySet
    assert get_managers(Rental)["deleted_loans"].orm_name == "deleted_loans"


def test_manager_on_an_instance_or_an_abstract_model_raises():
    with pytest.raises(AttributeError, match="objects is not accessible via Rental instances"):
        _ = Rental().objects
    with pytest.raises(AttributeError, match="everything is not accessible"):
        _ = Rental().everything

    class Abstract(Model):
        everything = Manager()

    with pytest.raises(AttributeError):
        _ = Abstract.everything
    with pytest.raises(AttributeError):
        _ = Abstract.objects


def test_subclasses_inherit_managers_bound_to_themselves():
    class LateRental(Rental):
        _meta = ModelMeta("managertest.LateRental")

    assert list(get_managers(LateRental)) == ["objects", "everything", "deleted_loans"]
    assert LateRental.objects.model is LateRental
    assert LateRental.objects.get_queryset().query.model == "managertest.LateRental"
    assert type(LateRental.objects) is RentalManager


def test_manager_names_must_not_clash_with_fields():
    class Named(Model):
        name = Manager()

    with pytest.raises(TypeError, match="managers named like fields or relations: name"):

        class Clash(Named):
            _meta = ModelMeta("managertest.Clash")
            id: int | None = None
            name: str = ""


def test_objects_must_stay_a_manager():
    with pytest.raises(TypeError, match=r"NoObjects.objects is a Manager"):

        class NoObjects(Model):
            _meta = ModelMeta("managertest.NoObjects")
            id: int | None = None
            objects: ClassVar[Any] = None


@pytest.mark.parametrize("name", ["", "_hidden", "not a name", 3])
def test_orm_name_is_a_public_identifier(name):
    with pytest.raises(ValueError, match="is not a manager name"):
        Manager(name)  # type: ignore[arg-type]


def test_a_manager_is_declared_under_one_name():
    shared = Manager()
    with raises_in_set_name(TypeError, "already 'first', not 'second'"):

        class Twice(Model):
            first = shared
            second = shared


def test_an_unbound_manager_has_no_queryset_and_does_not_pickle():
    declared = Manager("objects")
    with pytest.raises(TypeError, match="Manager is not bound"):
        declared.get_queryset()
    with pytest.raises(TypeError, match="cannot be pickled"):
        pickle.dumps(declared)
    with pytest.raises(AttributeError):
        _ = declared.anything
    assert repr(declared) == "<Manager 'objects' (not bound)>"
    assert declared == declared
    assert declared != Manager("objects")
    assert hash(declared) == hash(declared)


def test_from_queryset_and_as_manager_make_subclasses():
    manager_class = Manager.from_queryset(RentalQuerySet)
    assert issubclass(manager_class, Manager)
    assert manager_class.__name__ == "ManagerFromRentalQuerySet"
    assert manager_class.queryset_class is RentalQuerySet
    assert Manager.from_queryset(RentalQuerySet, "Loans").__name__ == "Loans"
    declared = RentalQuerySet.as_manager("objects")
    assert declared.queryset_class is RentalQuerySet
    assert declared.orm_name == "objects"
    with pytest.raises(TypeError, match="takes a QuerySet subclass"):
        Manager.from_queryset(dict)  # type: ignore[arg-type]


# Binding


def test_bound_managers_are_values():
    session = create_session("default", "managers-values")
    loans = session.bind(Rental)
    assert type(loans) is RentalManager
    assert isinstance(loans, BoundModel)
    assert loans == session.Rental == session.bind("managertest.Rental")
    assert loans != session.bind(Rental, "everything")
    assert hash(loans) == hash(session.Rental)
    assert copy.copy(loans) is loans
    assert copy.deepcopy(loans) is loans
    restored = pickle.loads(pickle.dumps(loans))
    assert restored == loans
    assert type(restored) is RentalManager
    everything = pickle.loads(pickle.dumps(session.bind(Rental, "everything")))
    assert everything.name == "everything"
    with pytest.raises(AttributeError, match="immutable"):
        loans.orm_name = "x"  # type: ignore[misc]
    assert repr(loans) == "<BoundModel managertest.Rental on Session('default', 'managers-values')>"
    assert repr(session.bind(Rental, "everything")).startswith("<BoundModel managertest.Rental.everything on")


def test_other_managers_are_attributes_of_a_bound_model():
    session = create_session("default", "managers-attributes")
    everything = session.Rental.everything
    assert everything.name == "everything"
    assert everything.session is session
    assert everything.orm_name is None
    assert everything.objects == session.Rental
    loans = session.Rental
    assert loans.objects is loans
    with pytest.raises(AttributeError, match="has no manager 'missing'"):
        session.bind(Rental, "missing")
    with pytest.raises(TypeError, match="needs a Model subclass"):
        session.bind(int)  # type: ignore[arg-type]


def test_bound_model_constructor_binds_a_manager_without_manager_logic():
    session = create_session()
    bare = BoundModel(Rental, session)
    assert (bare.orm_name, bare.method_manager, bare.queryset_class) == (None, None, QuerySet)
    assert bare.get_queryset().query == Query("managertest.Rental")
    assert bare != Rental.objects
    assert bare == BoundModel(Rental, session)
    assert hash(bare) == hash(BoundModel(Rental, session))
    assert not hasattr(bare, "lend")
    assert pickle.loads(pickle.dumps(Rental.objects)) == Rental.objects


def test_model_access_binds_to_the_current_session():
    session = create_session("default", "managers-current")
    with session.activate():
        assert Rental.objects.session is session
        assert Rental.everything.session is session
        assert Rental.deleted_loans.session is session
    assert Rental.objects.session is create_session()


# Query sets


def test_get_queryset_carries_the_orm_manager_and_the_client_override():
    loans = Rental.objects
    queryset = loans.all()
    assert type(queryset) is RentalQuerySet
    assert queryset.query == Query("managertest.Rental", manager="objects").filter(deleted=False)
    assert Rental.everything.all().query == Query("managertest.Rental")
    assert type(Rental.everything.all()) is QuerySet
    assert Rental.deleted_loans.all().query == Query("managertest.Rental", manager="deleted_loans")


def test_building_methods_keep_the_query_set_class():
    queryset = Rental.objects.filter(reader_id=1).order_by("due")[:5]
    assert type(queryset) is RentalQuerySet
    assert type(Rental.objects.open().due_before(datetime.date(2026, 1, 1)).exclude(id=3)) is RentalQuerySet
    assert type(Rental.objects.all() | Rental.objects.open()) is RentalQuerySet
    restored = pickle.loads(pickle.dumps(Rental.objects.open()))
    assert type(restored) is RentalQuerySet
    assert restored.query == Rental.objects.open().query


def test_manager_forwards_public_query_set_methods():
    day = datetime.date(2026, 1, 1)
    assert Rental.objects.open().query == Rental.objects.all().filter(archived=False).query
    assert Rental.objects.due_before(day).query == Rental.objects.all().due_before(day).query
    assert Rental.deleted_loans.open().query == Query("managertest.Rental", manager="deleted_loans").filter(
        archived=False
    )
    assert callable(Rental.objects.archive)
    with pytest.raises(AttributeError, match="no attribute 'renew'"):
        _ = Rental.objects.renew
    with pytest.raises(AttributeError):
        _ = Rental.objects._private
    with pytest.raises(AttributeError, match="no attribute 'missing'"):
        _ = Rental.objects.missing
    with pytest.raises(AttributeError, match="no attribute 'open'"):
        _ = Rental.everything.open


async def test_every_operation_starts_from_get_queryset():
    executor = Executor(1, [], {"id": 1}, 2, (1, {}), ({"id": 1}, True))
    loans, binding = bound(executor)
    with binding:
        await loans.count()
        _ = [loan async for loan in loans.open()]
        await loans.get(pk=1)
        await loans.update(archived=True)
        await loans.delete()
        await loans.get_or_create(reader_id=1)
    base = Query("managertest.Rental", manager="objects").filter(deleted=False)
    assert executor.operations == [
        Count(base),
        Select(base.filter(archived=False)),
        executor.operations[2],
        Update(base, {"archived": True}),
        Delete(base),
        GetOrCreate(base, {"reader_id": 1}),
    ]
    assert executor.operations[2].query == base.filter(pk=1)
    assert all("clause.manager" in set(op.constructs()) for op in executor.operations)


async def test_create_with_an_orm_manager_sends_the_values_that_are_not_fields():
    executor = Executor({"id": 5}, {"id": 6})
    session = create_session("default", "managers-create")
    with bind_transaction(session, executor):
        reader = await session.Renter.create(name="Ann", username="ann")
        plain = await session.Bare.create(username="bob")
    assert executor.operations == [
        Create("managertest.Renter", {"name": "Ann", "username": "ann"}, manager="objects"),
        Create("managertest.Bare", {}),
    ]
    assert reader.pk == 5
    assert reader.model_extra == {"username": "ann"}
    assert plain.model_extra == {"username": "bob"}


# Driver methods


async def test_queryset_driver_method_sends_a_call_with_the_query():
    executor = Executor(3)
    loans, binding = bound(executor)
    with binding:
        assert await loans.open().archive("lost") == 3
    query = Query("managertest.Rental", manager="objects").filter(deleted=False).filter(archived=False)
    assert executor.operations == [Call(query, "archive", "queryset", ("lost",))]


async def test_manager_forwarded_driver_method_runs_on_get_queryset():
    executor = Executor(2)
    loans, binding = bound(executor)
    with binding:
        assert await loans.archive(reason="moved") == 2
    query = Query("managertest.Rental", manager="objects").filter(deleted=False)
    assert executor.operations == [Call(query, "archive", "queryset", ("moved",))]


async def test_manager_driver_method_sends_the_plain_manager_query():
    row = ModelRow("managertest.Rental", {"id": 9, "reader_id": 1, "book_id": "b"})
    executor = Executor(row)
    loans, binding = bound(executor)
    with binding:
        loan = await loans.lend(1, "b", 7, 8, days=3, colour="red")
    assert executor.operations == [
        Call(
            Query("managertest.Rental", manager="objects"),
            "lend",
            "manager",
            (1, "b", 7, 8),
            (("days", 3), ("colour", "red")),
        )
    ]
    assert isinstance(loan, Rental)
    assert loan.pk == 9
    assert loan.get_deferred_fields() == {"due", "archived", "deleted"}
    assert loan._state.adding is False
    assert loan._state.executor is loans.session


async def test_driver_method_arguments_are_checked_before_sending():
    executor = Executor()
    loans, binding = bound(executor)
    with binding:
        with pytest.raises(TypeError, match=r"lend\(\): missing a required argument: 'book_id'"):
            await loans.lend(1)
        with pytest.raises(TypeError, match=r"archive\(\): too many positional arguments"):
            await loans.archive("a", "b")
        with pytest.raises(TypeError, match=r"renew\(\): too many positional arguments"):
            await loans.all().renew(3)
    assert executor.operations == []


async def test_driver_method_needs_an_orm_manager():
    executor = Executor()
    session = create_session("default", "managers-no-orm")
    with bind_transaction(session, executor):
        await RentalQuerySet(Rental, session).archive()
        assert executor.operations == [Call(Query("managertest.Rental"), "archive", manager="objects")]
        with pytest.raises(TypeError, match="no manager of Bare runs the driver method archive"):
            await RentalQuerySet(Bare, session).archive()

        class Orphan(Manager):
            @driver_method
            async def ping(self) -> None: ...

        class Pinged(Model):
            _meta = ModelMeta("managertest.Pinged")
            id: int | None = None
            objects = Orphan()

        with pytest.raises(TypeError, match="the manager objects has no ORM manager to run ping on"):
            await session.Pinged.ping()


async def test_driver_method_results_turn_model_rows_into_instances():
    rows = [ModelRow("managertest.Rental", {"id": 1}), ModelRow("managertest.Renter", {"id": 2, "name": "A"})]
    executor = Executor(rows, (rows[0], True), {"plain": 1}, ModelRow("managertest.Unknown", {"id": 1}))
    loans, binding = bound(executor)
    with binding:
        loan, reader = await loans.archive()
        assert (type(loan), type(reader), reader.name) == (Rental, Renter, "A")
        pair = await loans.archive()
        assert isinstance(pair, tuple)
        assert type(pair[0]) is Rental
        assert pair[1] is True
        assert await loans.archive() == {"plain": 1}
        executor.results.insert(0, {"k": [rows[1]], "plain": 2})
        keyed = await loans.archive()
        assert type(keyed["k"][0]) is Renter
        assert keyed["plain"] == 2
        with pytest.raises(InterfaceError, match="returned a row of managertest.Unknown, which is not registered"):
            await loans.archive()


async def test_driver_method_errors_become_the_model_exceptions():
    executor = Executor(ObjectDoesNotExist("gone"), ValueError("boom"))
    loans, binding = bound(executor)
    with binding:
        with pytest.raises(Rental.DoesNotExist, match="gone"):
            await loans.lend(1, "b")
        with pytest.raises(ValueError, match="boom"):
            await loans.archive()


def test_driver_method_declaration_errors():
    def sync(self): ...

    async def _hidden(self): ...

    async def no_self(*args): ...

    with pytest.raises(TypeError, match="declared with async def"):
        driver_method(sync)
    with pytest.raises(ValueError, match="is public"):
        driver_method(_hidden)
    with pytest.raises(TypeError, match="needs a self parameter"):
        driver_method(no_self)
    with raises_in_set_name(ValueError, "declare it under its own name"):

        class Renamed(QuerySet):
            other = driver_method(RentalQuerySet.__dict__["archive"].func)


async def test_driver_method_outside_a_manager_or_query_set_raises():
    class Elsewhere:
        archive = RentalQuerySet.__dict__["archive"]

    with pytest.raises(TypeError, match="belongs to a manager or a query set"):
        await Elsewhere().archive()


def test_driver_method_is_a_descriptor_with_the_declaration():
    method = RentalQuerySet.archive
    assert isinstance(method, DriverMethod)
    assert method.name == "archive"
    assert method.__doc__ == "Archive the loans."
    assert method.__qualname__ == "RentalQuerySet.archive"
    assert repr(method) == "<driver method RentalQuerySet.archive>"
    assert RentalQuerySet.renew.queryset_only is True


def test_driver_method_schema_types_the_parameters():
    schema = RentalManager.lend.schema
    assert schema.positional == (int, str)
    assert dict(schema.keyword) == {"days": int}
    assert (schema.var_positional, schema.var_keyword) == (int, str)
    assert schema.arg_type(0) is int
    assert schema.arg_type(5) is int
    assert schema.kwarg_type("days") is int
    assert schema.kwarg_type("colour") is str

    class Strings(QuerySet):
        @driver_method
        async def run(self, day: "datetime.date", other: "Missing", plain) -> None: ...  # noqa: F821

    assert Strings.run.schema.positional == (datetime.date, None, None)


def test_driver_methods_and_the_model_schema():
    methods = driver_methods(Rental)
    assert set(methods) == {
        ("objects", "manager", "lend"),
        ("objects", "queryset", "archive"),
        ("objects", "queryset", "renew"),
        ("deleted_loans", "queryset", "archive"),
        ("deleted_loans", "queryset", "renew"),
    }
    assert methods[("objects", "manager", "lend")] is RentalManager.lend
    schema = model_schema(Rental)
    assert set(schema.methods) == set(methods)
    assert schema.methods[("objects", "queryset", "archive")].kwarg_type("reason") is str
    assert registry.model_schema("managertest.Rental").methods.keys() == schema.methods.keys()
    assert driver_methods(Bare) == {}


async def test_manager_with_only_driver_methods_sends_plain_queries():
    assert (Stack.objects.orm_name, Stack.objects.method_manager) == (None, "objects")
    assert repr(get_managers(Stack)["objects"]) == "<StackManager method_manager='objects' (not bound)>"
    executor = Executor(4, 2, 1)
    session = create_session("default", "managers-stack")
    with bind_transaction(session, executor):
        assert await session.Stack.count() == 4
        assert await session.Stack.stock(3) == 2
        assert await session.Stack.filter(id=1).tidy() == 1
    query = Query("managertest.Stack")
    assert executor.operations == [
        Count(query),
        Call(query, "stock", "manager", (3,), manager="objects"),
        Call(query.filter(id=1), "tidy", manager="objects"),
    ]
    assert all("clause.manager" not in set(op.constructs()) for op in executor.operations)
    assert set(driver_methods(Stack)) == {("objects", "manager", "stock"), ("objects", "queryset", "tidy")}
    assert find_driver_method("managertest.Stack", "objects", "manager", "stock") is StackManager.stock


def test_reverse_relations_start_from_the_target_objects_manager():
    session = create_session("default", "managers-relations")
    borrower = Borrower.from_row({"id": 4}, executor=session)
    loans = borrower.loans
    assert type(loans) is RentalQuerySet
    assert loans.query == Query("managertest.Rental", manager="objects").filter(deleted=False).filter(reader_id=4)
    assert loans.open().query == loans.query.filter(archived=False)
    favourites = borrower.favourites
    assert type(favourites) is RentalQuerySet
    links = Query("managertest.Borrower_favourites").filter(borrower_id=4).values("rental_id")
    expected = Query("managertest.Rental", manager="objects").filter(deleted=False).filter(pk__in=links)
    assert favourites.query == expected
    other = Borrower.from_row({"id": 4}, executor=Executor()).loans
    assert type(other) is RentalQuerySet
    assert other.query == Query("managertest.Rental", manager="objects").filter(reader_id=4)


def test_find_driver_method_accepts_only_declared_methods():
    assert find_driver_method("managertest.Rental", "objects", "manager", "lend") is RentalManager.lend
    assert find_driver_method("managertest.Rental", "deleted_loans", "queryset", "archive") is RentalQuerySet.archive
    with pytest.raises(NotSupportedError, match="declares no driver method 'lend' on the queryset"):
        find_driver_method("managertest.Rental", "objects", "queryset", "lend")
    with pytest.raises(NotSupportedError, match="declares no driver method 'open'"):
        find_driver_method("managertest.Rental", "objects", "queryset", "open")
    with pytest.raises(NotSupportedError, match="declares no driver method"):
        find_driver_method("managertest.Rental", "everything", "queryset", "archive")
    with pytest.raises(NotSupportedError, match="declares no driver method"):
        find_driver_method("managertest.Rental", None, "queryset", "archive")
    with pytest.raises(NotSupportedError, match="no model is registered under 'managertest.Nothing'"):
        find_driver_method("managertest.Nothing", "objects", "manager", "lend")


# Concurrency: driver methods share the transaction of the context like every operation


class GatedExecutor:
    """Holds every operation until ``release`` is set; records operations in arrival order."""

    def __init__(self) -> None:
        self.operations: list[Operation] = []
        self.release = asyncio.Event()
        self.arrived = asyncio.Event()

    async def execute(self, operation: Operation) -> Any:
        self.operations.append(operation)
        if len(self.operations) == 3:
            self.arrived.set()
        await self.release.wait()
        return len(self.operations)


@pytest.mark.parametrize("attempt", REPEAT)
async def test_driver_methods_in_tasks_use_the_inherited_transaction(attempt):  # noqa: ARG001
    executor = GatedExecutor()
    session = create_session("default", "managers-tasks")
    with bind_transaction(session, executor):
        tasks = [
            asyncio.create_task(session.Rental.archive("a")),
            asyncio.create_task(session.Rental.lend(1, "b")),
            asyncio.create_task(session.Rental.open().archive()),
        ]
    await asyncio.wait_for(executor.arrived.wait(), 5)
    executor.release.set()
    assert sorted(await asyncio.gather(*tasks)) == [3, 3, 3]
    assert sorted(op.method for op in executor.operations) == ["archive", "archive", "lend"]


@pytest.mark.parametrize("attempt", REPEAT)
async def test_cancelling_a_driver_method_call_cancels_the_operation(attempt):  # noqa: ARG001
    executor = GatedExecutor()
    session = create_session("default", "managers-cancel")
    with bind_transaction(session, executor):
        task = asyncio.create_task(session.Rental.archive())
    while not executor.operations:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(executor.operations) == 1
