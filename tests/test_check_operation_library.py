"""``Operation.models`` over the generated library models on the drivers that translate with Django (Django and
asyncpg): many-to-many through tables, multi-table inheritance, proxies, prefetch queries, driver methods and the
models a delete reaches through Django's ``on_delete`` (`ModelMeta.delete_models`)."""

from __future__ import annotations

import datetime

from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import pytest

import adorm

from adorm import ConnectionProfile
from adorm.executor import Pool, Router, set_runner
from tests.generated.auth import User
from tests.generated.library import (
    Author,
    Book,
    BookProxy,
    Edition,
    Fine,
    Loan,
    Note,
    Placement,
    Reader,
    Reservation,
    Shelf,
    Tag,
)
from tests.library_data import DUNE, NOTE

pytestmark = pytest.mark.django_db(transaction=True)

AUTHOR = "library.Author"
BOOK = "library.Book"
BOOK_AUTHORS = "library.Book_authors"
BOOK_PROXY = "library.BookProxy"
EDITION = "library.Edition"
TAG = "library.Tag"
TAGGING = "library.Tagging"
NOTE_ = "library.Note"
SHELF = "library.Shelf"
PLACEMENT = "library.Placement"
READER = "library.Reader"
LOAN = "library.Loan"
FINE = "library.Fine"
RESERVATION = "library.Reservation"
CONTENT_TYPE = "contenttypes.ContentType"

BOOK_DELETE = frozenset({BOOK, BOOK_AUTHORS, EDITION, TAGGING, NOTE_, PLACEMENT, LOAN, FINE, RESERVATION, CONTENT_TYPE})
"""What deleting books reaches: the CASCADE targets recursively (a loan's fines are RESTRICT, so read), the generic
relation's notes and the content types it looks up."""

USER_DELETE = frozenset({"auth.User", "auth.User_groups", "auth.User_user_permissions", AUTHOR})
"""What deleting users reaches: the auto-created through tables, and the authors whose ``account`` is SET_NULL, not
recursed (so not the authors' book links)."""


class RefusedError(Exception):
    pass


_refuse: ContextVar[Callable[[Any], bool] | None] = ContextVar("refuse", default=None)


@contextmanager
def refusing(predicate: Callable[[Any], bool] = lambda _operation: True):
    token = _refuse.set(predicate)
    try:
        yield
    finally:
        _refuse.reset(token)


class Hook:
    def __init__(self) -> None:
        self.operations: list[Any] = []

    def __call__(self, operation, session) -> None:  # noqa: ARG002 - the hook's signature
        self.operations.append(operation)
        predicate = _refuse.get()
        if predicate is not None and predicate(operation):
            raise RefusedError(operation.kind)


@pytest.fixture(params=["django", "asyncpg"])
def driver_class(request):
    from django.db import connection

    if connection.vendor == "sqlite":
        pytest.skip("the SQLite settings install no library app")
    if request.param == "django":
        from adorm.drivers.django import DjangoDriver

        return DjangoDriver
    from adorm.drivers.asyncpg import AsyncpgDriver

    return AsyncpgDriver


@pytest.fixture
def hook():
    return Hook()


@pytest.fixture
async def pool(driver_class, hook):
    driver = driver_class()
    pool = Pool(driver, ConnectionProfile("default", driver, check_operation=hook), max_connections=2)
    router = Router([pool])
    previous = set_runner(router)
    try:
        yield pool
    finally:
        set_runner(previous)
        await router.close()


async def listed(queryset) -> list[Any]:
    return [row async for row in queryset]


async def models_of(hook: Hook, make: Callable[[], Any]) -> frozenset[str] | None:
    """``Operation.models`` of the one operation ``make()`` sends, refused before it reaches the database."""
    hook.operations.clear()
    with refusing(), pytest.raises(RefusedError):
        await make()
    (operation,) = hook.operations
    return operation.models


# Reads


@pytest.mark.usefixtures("pool")
async def test_many_to_many_paths_include_the_through_table(hook):
    assert await models_of(hook, lambda: Book.objects.filter(authors__name="Ann").count()) == {
        BOOK,
        AUTHOR,
        BOOK_AUTHORS,
    }
    assert await models_of(hook, lambda: Author.objects.filter(books__title="Dune").count()) == {
        AUTHOR,
        BOOK,
        BOOK_AUTHORS,
    }
    assert await models_of(hook, lambda: Book.objects.filter(tags__name="rare").count()) == {BOOK, TAG, TAGGING}
    assert await models_of(hook, lambda: Tag.objects.filter(books__authors__slug="ann").count()) == {
        TAG,
        TAGGING,
        BOOK,
        BOOK_AUTHORS,
        AUTHOR,
    }


@pytest.mark.usefixtures("pool")
async def test_self_relations_and_forward_paths(hook):
    assert await models_of(hook, lambda: Author.objects.filter(mentor__mentor__name="Ann").count()) == {AUTHOR}
    assert await models_of(hook, lambda: listed(Fine.objects.select_related("loan__reader"))) == {FINE, LOAN, READER}
    # Without arguments: the non-null foreign keys only.
    assert await models_of(hook, lambda: listed(Loan.objects.select_related())) == {LOAN, READER, BOOK}
    assert await models_of(hook, lambda: listed(Author.objects.select_related())) == {AUTHOR}


@pytest.mark.usefixtures("pool")
async def test_a_path_through_a_relation_adorm_does_not_know_has_no_models(library, hook):  # noqa: ARG001
    assert await models_of(hook, lambda: Book.objects.filter(reviews__code=NOTE).count()) is None
    # Django resolves these paths and joins the tables: a foreign key to a field other than the primary key (not
    # generated) and a reverse relation by its query name.
    for make in (
        lambda: Placement.objects.filter(shelf__code="A1").count(),
        lambda: Book.objects.filter(tagging__tag__name="rare").count(),
    ):
        assert await models_of(hook, make) is None
        assert await make() >= 0


@pytest.mark.usefixtures("pool")
async def test_inheritance_includes_the_parent_and_a_proxy_its_concrete_model(hook):
    assert await models_of(hook, lambda: Edition.objects.count()) == {EDITION, BOOK}
    assert await models_of(hook, lambda: Edition.objects.filter(authors__name="Ann").count()) == {
        EDITION,
        BOOK,
        AUTHOR,
        BOOK_AUTHORS,
    }
    assert await models_of(hook, lambda: Book.objects.filter(edition__number=2).count()) == {BOOK, EDITION}
    assert await models_of(hook, lambda: BookProxy.objects.count()) == {BOOK_PROXY, BOOK}


# Writes


@pytest.mark.usefixtures("pool")
async def test_writes_of_a_child_include_its_parent(hook):
    def edition():
        return Edition.objects.create(isbn="9780000000099", title="Never", pages=1, released=datetime.date(2026, 1, 1))

    assert await models_of(hook, edition) == {EDITION, BOOK}
    assert await models_of(hook, lambda: Edition.objects.filter(number=2).update(title="Never")) == {EDITION, BOOK}
    unsaved = Edition(isbn="9780000000099", title="Never", pages=1, released=datetime.date(2026, 1, 1))
    assert await models_of(hook, unsaved.full_clean) == {EDITION, BOOK}


@pytest.mark.usefixtures("pool")
async def test_full_clean_reads_the_targets_of_the_foreign_keys(hook):
    reservation = Reservation(
        reader_id=1, book_id=DUNE, starts=datetime.date(2026, 1, 1), ends=datetime.date(2026, 1, 2)
    )
    assert await models_of(hook, reservation.full_clean) == {RESERVATION, READER, BOOK}
    note = Note(content_type_id=1, object_id=DUNE)
    assert await models_of(hook, note.full_clean) == {NOTE_, CONTENT_TYPE, BOOK}


@pytest.mark.usefixtures("pool")
async def test_check_schema_reads_the_through_tables(hook):
    models = await models_of(hook, lambda: adorm.check_schema([Book]))
    assert {BOOK, BOOK_AUTHORS} <= models


# Deletes


def test_generated_models_carry_the_models_a_delete_reaches():
    assert Book._meta.delete_models == BOOK_DELETE
    assert User._meta.delete_models == USER_DELETE
    assert Loan._meta.delete_models == {LOAN, FINE}
    assert Reader._meta.delete_models == {READER, LOAN, FINE, RESERVATION}
    assert Author._meta.delete_models == {AUTHOR, BOOK_AUTHORS}
    assert Tag._meta.delete_models == {TAG, TAGGING}
    assert Shelf._meta.delete_models == {SHELF, PLACEMENT}


@pytest.mark.usefixtures("pool")
async def test_deletes_reach_their_cascades(library, hook):  # noqa: ARG001
    book = await Book.objects.get(isbn=DUNE)
    assert await models_of(hook, book.delete) == BOOK_DELETE
    assert await models_of(hook, Book.objects.filter(isbn=DUNE).delete) == BOOK_DELETE
    assert await models_of(hook, Book.objects.filter(authors__name="Ann").delete) == BOOK_DELETE | {AUTHOR}
    assert await models_of(hook, User.objects.filter(username="nobody").delete) == USER_DELETE
    assert await models_of(hook, Loan.all_loans.filter(book_id=DUNE).delete) == {LOAN, FINE}
    edition = await models_of(hook, Edition.objects.all().delete)
    assert {EDITION, BOOK, BOOK_AUTHORS, TAGGING, NOTE_, LOAN, FINE, RESERVATION, PLACEMENT} <= edition


@pytest.mark.usefixtures("pool")
async def test_a_refused_delete_leaves_every_row(library, hook):
    dune = Book.objects.filter(isbn=DUNE)
    with refusing(lambda operation: LOAN in (operation.models or {LOAN})), pytest.raises(RefusedError):
        await dune.delete()
    with refusing(lambda operation: LOAN in (operation.models or {LOAN})), pytest.raises(RefusedError):
        await (await dune.get()).delete()
    assert await dune.count() == 1
    assert await Loan.all_loans.filter(book_id=DUNE).count() == 1
    assert await Author.objects.filter(pk=library["ann"], books__isbn=DUNE).count() == 1
    assert await Note.objects.filter(code=NOTE).count() == 1
    assert hook.operations


# Prefetch and related objects


@pytest.mark.usefixtures("pool")
async def test_each_prefetch_query_is_checked_on_its_own(library, hook):  # noqa: ARG001
    def models_per_operation() -> list[list[str]]:
        return sorted(sorted(operation.models) for operation in hook.operations)

    hook.operations.clear()
    (book,) = await listed(Book.objects.filter(isbn=DUNE).prefetch_related("authors"))
    assert [author.name async for author in book.authors] == ["Ann"]
    assert models_per_operation() == sorted([[BOOK], [BOOK_AUTHORS], [AUTHOR]])
    hook.operations.clear()
    await listed(Book.objects.filter(isbn=DUNE).prefetch_related("loans"))
    assert models_per_operation() == sorted([[BOOK], [LOAN]])
    hook.operations.clear()
    await listed(Loan.objects.filter(book_id=DUNE).prefetch_related("book"))
    assert models_per_operation() == sorted([[LOAN], [BOOK]])


@pytest.mark.usefixtures("pool")
async def test_a_refused_prefetch_query_fails_the_whole_call(library, hook):  # noqa: ARG001
    got = None
    with refusing(lambda operation: operation.models == {AUTHOR}), pytest.raises(RefusedError):
        got = await listed(Book.objects.filter(isbn=DUNE).prefetch_related("authors"))
    assert got is None
    with refusing(lambda operation: operation.models == {BOOK_AUTHORS}), pytest.raises(RefusedError):
        got = await listed(Book.objects.filter(isbn=DUNE).prefetch_related("authors"))
    assert got is None


@pytest.mark.usefixtures("pool")
async def test_generic_foreign_key_loads_are_checked(library, hook):  # noqa: ARG001
    note = await Note.objects.get(code=NOTE)
    hook.operations.clear()
    item = await note.item
    assert item.isbn == DUNE
    assert hook.operations
    assert all(operation.models is not None for operation in hook.operations)
    assert BOOK in frozenset().union(*(operation.models for operation in hook.operations))


# Driver methods


@pytest.mark.usefixtures("pool")
async def test_a_driver_method_has_no_models(library, hook):
    assert await models_of(hook, lambda: Loan.objects.lend(library["reader"], DUNE)) is None
    assert await Loan.all_loans.filter(book_id=DUNE).count() == 1, "the refused method did not run"
