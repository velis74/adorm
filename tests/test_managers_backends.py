"""Managers against Django: the operations of an ORM manager run through that manager, so its overrides apply.

`DjangoManagerExecutor` runs operations the way the driver contract asks: a query with a manager starts from that
manager's ``get_queryset()``, ``Create`` goes through the manager's ``create()``, and a ``Call`` runs a method the
registered model declares, on the manager or on the query set. It covers the constructs these tests use.
"""

import datetime

from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from django.apps import apps
from django.contrib.auth.models import User as DjangoUser

from adorm import create_session, NotSupportedError
from adorm.codegen import python_name
from adorm.exceptions import wrap_driver_error
from adorm.executor import bind_transaction
from adorm.models import find_driver_method
from adorm.operations import Call, Count, Create, Delete, ModelRow, Operation, Select
from adorm.query import Query
from tests.djangoproject.library import models as django_library
from tests.generated import auth, library

pytestmark = pytest.mark.django_db


def row(instance: Any) -> dict[str, Any]:
    return {python_name(f.attname): getattr(instance, f.attname) for f in instance._meta.concrete_fields}


def plain(value: Any) -> Any:
    from django.db import models

    if isinstance(value, models.Model):
        return ModelRow(value._meta.label, row(value))
    if isinstance(value, list | tuple):
        return type(value)(plain(item) for item in value)
    return value


class DjangoManagerExecutor:
    """Runs operations with the Django ORM in the calling thread."""

    def __init__(self) -> None:
        self.operations: list[Operation] = []

    @staticmethod
    def manager(query: Query, name: str | None = None) -> Any:
        model = apps.get_model(query.model)
        name = name or query.manager
        return getattr(model, name) if name is not None else model._base_manager

    def queryset(self, query: Query, manager: str | None = None) -> Any:
        queryset = self.manager(query, manager).all()
        if query.where.negated or query.where.connector != "AND":
            raise NotImplementedError(query.where)
        for lookup in query.where.children:
            queryset = queryset.filter(**{f"{lookup.lhs.name}__{lookup.lookup_name}": lookup.rhs})
        return queryset

    async def execute(self, operation: Operation) -> Any:
        self.operations.append(operation)
        try:
            return self.run(operation)
        except Exception as exc:
            raise wrap_driver_error(exc) from exc

    def run(self, operation: Operation) -> Any:
        if isinstance(operation, Count):
            return self.queryset(operation.query).count()
        if isinstance(operation, Select):
            return [row(instance) for instance in self.queryset(operation.query)]
        if isinstance(operation, Delete):
            return self.queryset(operation.query).delete()
        if isinstance(operation, Create):
            manager = self.manager(Query(operation.model, manager=operation.manager))
            return row(manager.create(**dict(operation.values)))
        if isinstance(operation, Call):
            find_driver_method(operation.model, operation.manager, operation.target, operation.method)
            if operation.target == "manager":
                target = self.manager(operation.query, operation.manager)
            else:
                target = self.queryset(operation.query, operation.manager)
            return plain(getattr(target, operation.method)(*operation.args, **dict(operation.kwargs)))
        raise NotImplementedError(operation.kind)


@pytest.fixture
def django_session():
    """Runs a test coroutine to completion without an event loop, so the executor's ORM calls run on the test's
    connection, inside the test's transaction. The executor never suspends, so neither does the coroutine."""
    executor = DjangoManagerExecutor()
    session = create_session("default", "managers-backend")

    def run(test: Callable[..., Coroutine[Any, Any, Any]]) -> Any:
        coroutine = test(session, executor)
        with bind_transaction(session, executor):
            try:
                coroutine.send(None)
            except StopIteration as stop:
                return stop.value
        coroutine.close()
        raise AssertionError("the test coroutine suspended")

    return run


@pytest.fixture
def book():
    return django_library.Book.objects.create(isbn="9780000000001", title="Dune", pages=412)


def test_create_goes_through_the_query_set_override(django_session):
    async def body(session, executor):  # noqa: ARG001
        reader = await session.Reader.create(name="Ann", email="ann@example.com", username="ann")
        assert isinstance(reader, library.Reader)
        assert reader.pk is not None
        stored = django_library.Reader.objects.get(pk=reader.pk)
        assert (stored.name, stored.email) == ("Ann", "ann@example.com")
        assert executor.operations[0].manager == "objects"

    django_session(body)


def test_bulk_delete_is_refused_by_the_query_set(django_session):
    async def body(session, executor):  # noqa: ARG001
        django_library.LoanPolicy.objects.bulk_create(
            [django_library.LoanPolicy(name="short", max_days=7), django_library.LoanPolicy(name="long")]
        )
        with pytest.raises(NotSupportedError, match="loan policies are deleted one at a time"):
            await session.LoanPolicy.delete()
        assert django_library.LoanPolicy.objects.count() == 2

    django_session(body)


def test_rows_come_from_the_manager_get_queryset(django_session, book):
    async def body(session, executor):  # noqa: ARG001
        reader = django_library.Reader.objects.create(name="Bo")
        due = datetime.date(2026, 3, 1)
        django_library.Loan.all_loans.create(reader=reader, book=book, due=due, archived=True)
        django_library.Loan.all_loans.create(reader=reader, book=book, due=due)
        assert await session.Loan.count() == 1
        assert await session.Loan.all_loans.count() == 2
        assert [loan.archived async for loan in session.Loan.all()] == [False]
        assert sorted([loan.archived async for loan in session.Loan.all_loans.all()]) == [False, True]

    django_session(body)


def test_driver_methods_run_on_the_manager_and_the_query_set(django_session, book):
    async def body(session, executor):  # noqa: ARG001
        reader = django_library.Reader.objects.create(name="Cy")
        loan = await session.Loan.lend(reader.pk, book.pk, days=3)
        assert isinstance(loan, library.Loan)
        assert (loan.reader_id, loan.book_id, loan.archived) == (reader.pk, book.pk, False)
        assert loan.due > datetime.date(2000, 1, 1)
        django_library.Loan.objects.create(reader=reader, book=book, due=datetime.date(2001, 1, 1))
        archived = await session.Loan.overdue(datetime.date(2002, 1, 1)).archive("late")
        assert archived == 1
        assert list(django_library.Loan.all_loans.filter(archived=True).values_list("note", flat=True)) == ["late"]
        assert await session.Loan.count() == 1

    django_session(body)


def test_reverse_relations_apply_the_target_manager(django_session, book):
    async def body(session, executor):  # noqa: ARG001
        reader = django_library.Reader.objects.create(name="Di")
        django_library.Loan.all_loans.create(reader=reader, book=book, due=datetime.date(2001, 1, 1), archived=True)
        django_library.Loan.all_loans.create(reader=reader, book=book, due=datetime.date(2001, 1, 1))
        django_library.Loan.all_loans.create(reader=reader, book=book, due=datetime.date(2030, 1, 1))
        owner = library.Reader.from_row({"id": reader.pk, "name": "Di"}, executor=session)
        assert await owner.loans.count() == 2
        assert await owner.loans.overdue(datetime.date(2002, 1, 1)).count() == 1
        assert await owner.loans.overdue(datetime.date(2002, 1, 1)).archive("late") == 1
        assert await owner.loans.count() == 1
        assert django_library.Loan.all_loans.filter(reader=reader).count() == 3

    django_session(body)


def test_exposed_methods_leave_plain_queries_off_the_orm_manager(django_session):
    async def body(session, executor):
        DjangoUser.objects.create(username="bo")
        assert await session.User.filter(username="bo").count() == 1
        assert executor.operations[-1].query.manager is None
        assert "clause.manager" not in set(executor.operations[-1].constructs())

    django_session(body)


def test_third_party_manager_method_exposed_by_the_setting(django_session):
    async def body(session, executor):  # noqa: ARG001
        user = await session.User.create_user("ann", email="ann@example.com", password="secret")  # noqa: S106
        assert isinstance(user, auth.User)
        assert user.username == "ann"
        stored = DjangoUser.objects.get(pk=user.pk)
        assert stored.check_password("secret")
        assert stored.password != "secret"  # noqa: S105 - the hash Django stored

    django_session(body)


def test_undeclared_methods_do_not_run(django_session):
    async def body(session, executor):  # noqa: ARG001
        with pytest.raises(NotSupportedError, match="declares no driver method 'overdue'"):
            await session.execute(
                Call(Query("library.Loan", manager="objects"), "overdue", "queryset", ("2026-01-01",))
            )
        with pytest.raises(NotSupportedError, match="declares no driver method 'create_superuser'"):
            await session.execute(Call(Query("auth.User", manager="objects"), "create_superuser", "manager", ("root",)))
        assert len(executor.operations) == 2
        assert not DjangoUser.objects.filter(username="root").exists()

    django_session(body)
