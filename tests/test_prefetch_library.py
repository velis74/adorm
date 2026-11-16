"""prefetch_related(), in_bulk(), generic foreign keys, children of existing parents and full_clean() on the drivers
that translate with Django (Django and asyncpg), over the library schema."""

import asyncio
import datetime

from decimal import Decimal

import pytest

from django.contrib.contenttypes.models import ContentType as DjangoContentType

import adorm

from adorm import ConnectionProfile, GenericPrefetch, Prefetch, prefetch_related_objects, ValidationError
from adorm.executor import Pool, Router, set_runner
from demo.models.contenttypes import ContentType
from tests.djangoproject.library import models as dj
from tests.generated.library import Author, Book, Edition, Loan, Note, Reader, Reservation, Tag, Tagging
from tests.library_data import DUNE, DUNE_2, in_thread, NOTE, PHYSICS, POEMS

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(params=["django", "asyncpg"])
def driver_class(request):
    """The driver class named by the parameter."""
    if request.param == "django":
        from adorm.drivers.django import DjangoDriver

        return DjangoDriver
    from adorm.drivers.asyncpg import AsyncpgDriver

    return AsyncpgDriver


@pytest.fixture
async def pool(driver_class):
    driver = driver_class()
    pool = Pool(driver, ConnectionProfile("default", driver), max_connections=2)
    router = Router([pool])
    previous = set_runner(router)
    try:
        yield pool
    finally:
        set_runner(previous)
        await router.close()


@pytest.fixture
def recorded(monkeypatch):
    """The operations sessions execute from now on."""
    operations = []
    execute = adorm.Session.execute

    async def recording(self, operation):
        operations.append(operation)
        return await execute(self, operation)

    monkeypatch.setattr(adorm.Session, "execute", recording)
    return operations


# prefetch_related()


async def test_prefetch_of_every_relation_kind_runs_one_operation_per_relation(library, pool, recorded):  # noqa: ARG001
    books = [
        book
        async for book in Book.objects.order_by("isbn").prefetch_related(
            "authors", "tagging_set__tag", "copy_notes", "edition", "loans"
        )
    ]
    kinds = [operation.kind for operation in recorded]
    # books, the authors' links and authors, taggings, tags, notes, editions, loans
    assert kinds == ["select"] * 8
    recorded.clear()
    by_isbn = {book.isbn: book for book in books}
    assert sorted([author.name async for author in by_isbn[PHYSICS].authors]) == ["Ann", "Cid"]
    assert await by_isbn[POEMS].authors.count() == 1
    assert await by_isbn[DUNE_2].authors.exists() is False
    taggings = [tagging async for tagging in by_isbn[DUNE].tagging_set]
    assert [(tagging.weight, (await tagging.tag).name) for tagging in taggings] == [(3, "rare")]
    assert [note.code async for note in by_isbn[DUNE].copy_notes] == [NOTE]
    assert (await by_isbn[DUNE_2].edition).number == 2
    with pytest.raises(Edition.DoesNotExist):
        await by_isbn[DUNE].edition
    # The loans query applies the manager's get_queryset(): the archived loan is left out.
    assert [loan.due async for loan in by_isbn[DUNE].loans] == [datetime.date(2026, 1, 1)]
    assert await by_isbn[POEMS].loans.count() == 0
    assert recorded == [], "the prefetched relations ran no operation"
    # A building method starts a new query.
    assert await by_isbn[PHYSICS].authors.filter(name="Ann").count() == 1
    assert [operation.kind for operation in recorded] == ["count"]


async def test_prefetch_sets_the_foreign_key_back_to_the_owner(library, pool, recorded):  # noqa: ARG001
    dune = await Book.objects.prefetch_related("tagging_set").get(isbn=DUNE)
    recorded.clear()
    (tagging,) = [tagging async for tagging in dune.tagging_set]
    assert await tagging.book is dune
    assert recorded == []


async def test_prefetch_with_a_queryset_and_to_attr(library, pool, recorded):  # noqa: ARG001
    ann = await Author.objects.prefetch_related(
        Prefetch("books", queryset=Book.objects.filter(in_print=True).order_by("-pages"), to_attr="in_print"),
        Prefetch("books", to_attr="every_book"),
    ).get(slug="ann")
    assert [book.title for book in ann.in_print] == ["Dune"]
    assert sorted(book.title for book in ann.every_book) == ["Dune", "Physics"]
    assert "books" not in ann._state.related, "to_attr leaves the relation's cache alone"
    assert ann.model_dump(include={"name"}) == {"name": "Ann"}
    recorded.clear()
    single = await Note.objects.prefetch_related(Prefetch("copy_", to_attr="book")).get(code=NOTE)
    assert single.book.title == "Dune"
    assert [operation.kind for operation in recorded] == ["get", "select"]


async def test_nested_prefetch_from_the_queryset_of_a_prefetch(library, pool):  # noqa: ARG001
    taggings = Tagging.objects.order_by("weight").prefetch_related("book__authors")
    rare = await Tag.objects.prefetch_related(Prefetch("taggings", queryset=taggings)).get(name="rare")
    books = [await tagging.book async for tagging in rare.taggings]
    assert [(book.title, [author.name async for author in book.authors]) for book in books] == [
        ("Poems", ["Bob"]),
        ("Dune", ["Ann"]),
    ]


async def test_prefetch_follows_a_to_attr_and_select_related(library, pool, recorded):  # noqa: ARG001
    authors = [
        author
        async for author in Author.objects.select_related("mentor")
        .order_by("name")
        .prefetch_related(Prefetch("pupils", to_attr="pupil_list"), "pupil_list__books", "mentor__books")
    ]
    by_name = {author.name: author for author in authors}
    assert [pupil.name for pupil in by_name["Ann"].pupil_list] == ["Bob"]
    recorded_kinds = [operation.kind for operation in recorded]
    # authors (with mentors), pupils, the pupils' book links and books, the mentors' book links and books
    assert recorded_kinds == ["select"] * 6
    recorded.clear()
    bob = by_name["Ann"].pupil_list[0]
    assert [book.title async for book in bob.books] == ["Poems"]
    assert sorted([book.title async for book in (await by_name["Bob"].mentor).books]) == ["Dune", "Physics"]
    assert recorded == []


async def test_prefetch_inside_a_transaction_uses_its_slot(library, pool, recorded):  # noqa: ARG001
    async with adorm.atomic():
        assert pool.in_use == 1
        books = [book async for book in Book.objects.prefetch_related("authors", "loans")]
        assert pool.in_use == 1
        await Author.objects.filter(slug="ann").update(name="Anne")
        dune = next(book for book in books if book.isbn == DUNE)
        refreshed = await Book.objects.prefetch_related("authors").get(isbn=DUNE)
        assert [a.name async for a in refreshed.authors] == ["Anne"], "the prefetch reads the transaction's writes"
        assert [a.name async for a in dune.authors] == ["Ann"], "a prefetched cache does not run again"
    assert pool.in_use == 0


async def test_prefetch_errors(library, pool):  # noqa: ARG001
    with pytest.raises(AttributeError, match="cannot find 'nothing' on Book"):
        await Book.objects.prefetch_related("nothing").first()
    with pytest.raises(ValueError, match="'title' does not resolve to a relation"):
        await Book.objects.prefetch_related("title").first()
    with pytest.raises(ValueError, match="conflicts with an attribute"):
        await Book.objects.prefetch_related(Prefetch("authors", to_attr="title")).first()
    with pytest.raises(ValueError, match="is on library.Tag, not on library.Author"):
        await Book.objects.prefetch_related(Prefetch("authors", queryset=Tag.objects.all())).first()
    with pytest.raises(ValueError, match="GenericPrefetch needs a generic foreign key"):
        await Book.objects.prefetch_related(GenericPrefetch("authors", [Author.objects.all()])).first()
    with pytest.raises(ValueError, match="use GenericPrefetch"):
        await Note.objects.prefetch_related(Prefetch("item", queryset=Book.objects.all())).first()


async def test_prefetch_related_objects_on_instances(library, pool, recorded):  # noqa: ARG001
    books = [book async for book in Book.objects.order_by("isbn")]
    recorded.clear()
    await prefetch_related_objects(books, "authors")
    await prefetch_related_objects(books, "authors")
    assert len(recorded) == 2, "a relation already cached is not read again"
    await prefetch_related_objects([], "authors")
    assert len(recorded) == 2


# Generic foreign keys


async def test_generic_foreign_key_loads_and_caches_the_object(library, pool, recorded):  # noqa: ARG001
    note = await Note.objects.get(code=NOTE)
    recorded.clear()
    book = await note.item
    assert isinstance(book, Book)
    assert book.isbn == DUNE
    assert [operation.kind for operation in recorded] == ["get", "get"], "the content type, then the book"
    assert await note.item is book
    assert len(recorded) == 2
    note.object_id = POEMS
    assert (await note.item).title == "Poems"
    assert len(recorded) == 3, "the content type is cached"
    book_type = note.content_type_id
    note.item = None
    assert (note.content_type_id, note.object_id) == (None, None)
    assert await note.item is None
    note.item = book
    assert (note.content_type_id, note.object_id) == (book_type, DUNE)
    assert await note.item is book
    assert len(recorded) == 3


async def test_generic_foreign_key_to_a_missing_row_is_none(library, pool):  # noqa: ARG001
    note = await Note.objects.get(code=NOTE)
    note.object_id = "9789999999999"
    assert await note.item is None


async def test_generic_foreign_key_uses_the_cached_content_type(library, pool, recorded):  # noqa: ARG001
    note = await Note.objects.select_related("content_type").get(code=NOTE)
    assert isinstance(await note.content_type, ContentType)
    recorded.clear()
    assert (await note.item).isbn == DUNE
    assert [operation.kind for operation in recorded] == ["get"]


async def test_generic_prefetch_with_querysets_and_to_attr(library, pool, recorded):  # noqa: ARG001
    book_type = await in_thread(lambda: DjangoContentType.objects.get_for_model(dj.Book).id)
    reader_type = await in_thread(lambda: DjangoContentType.objects.get_for_model(dj.Reader).id)
    reader = await Reader.objects.get(name="Rea")
    await Note.objects.create(content_type_id=reader_type, object_id=str(reader.pk))
    await Note.objects.create(content_type_id=book_type, object_id=PHYSICS)
    recorded.clear()
    notes = [
        note
        async for note in Note.objects.order_by("object_id").prefetch_related(
            GenericPrefetch("item", [Book.objects.filter(in_print=True)], to_attr="target")
        )
    ]
    # notes, content types, books, readers
    assert [operation.kind for operation in recorded] == ["select"] * 4
    targets = {note.object_id: note.target for note in notes}
    assert targets[DUNE].title == "Dune"
    assert targets[PHYSICS] is None, "the books' query set leaves the book out"
    assert isinstance(targets[str(reader.pk)], Reader)
    recorded.clear()
    plain = [note async for note in Note.objects.order_by("object_id").prefetch_related("item")]
    assert {type(await note.item).__name__ for note in plain} == {"Book", "Reader"}
    assert len(recorded) == 3, "notes, books, readers: the content types are cached"


# in_bulk()


async def test_in_bulk_by_primary_key_and_unique_field(library, pool):  # noqa: ARG001
    bulk = await Book.objects.in_bulk([DUNE, POEMS, "9780000000999"])
    assert sorted(bulk) == [DUNE, POEMS]
    assert bulk[DUNE].title == "Dune"
    assert set(await Book.objects.filter(in_print=True).in_bulk()) == {DUNE, POEMS, DUNE_2}
    by_slug = await Author.objects.in_bulk(["ann", "bob"], field_name="slug")
    assert {slug: author.name for slug, author in by_slug.items()} == {"ann": "Ann", "bob": "Bob"}
    assert await Author.objects.in_bulk([]) == {}
    prefetched = await Author.objects.prefetch_related("books").in_bulk(field_name="slug")
    assert sorted([b.title async for b in prefetched["ann"].books]) == ["Dune", "Physics"]


async def test_in_bulk_refuses_a_field_that_is_not_unique(library, pool):  # noqa: ARG001
    with pytest.raises(adorm.ProgrammingError, match="must be a unique field but 'name' isn't"):
        await Author.objects.in_bulk(["Ann"], field_name="name")
    by_name = await Author.objects.distinct("name").order_by("name").in_bulk(["Ann"], field_name="name")
    assert list(by_name) == ["Ann"]
    with pytest.raises(TypeError, match="slice"):
        await Author.objects.all()[:2].in_bulk()


async def test_in_bulk_by_a_one_to_one_relation_keys_by_its_column(library, pool):  # noqa: ARG001
    from django.contrib.auth.models import User

    def link():
        user = User.objects.create(username="ann")
        dj.Author.objects.filter(slug="ann").update(account=user)
        return user.pk

    user_pk = await in_thread(link)
    for field_name in ("account", "account_id"):
        bulk = await Author.objects.in_bulk([user_pk, user_pk + 1], field_name=field_name)
        assert {key: author.slug for key, author in bulk.items()} == {user_pk: "ann"}
    every = await Author.objects.filter(account__isnull=False).in_bulk(field_name="account")
    assert list(every) == [user_pk]
    with pytest.raises(adorm.ProgrammingError, match="'mentor_id' isn't"):
        await Author.objects.in_bulk([1], field_name="mentor")


async def test_in_bulk_of_more_ids_than_a_statement_carries(library, pool):  # noqa: ARG001
    authors = [author.pk async for author in Author.objects.all()]
    ids = [*range(10**6, 10**6 + 40_000), *authors]
    bulk = await Author.objects.in_bulk(ids)
    assert sorted(bulk) == sorted(authors)


async def test_prefetch_of_more_keys_than_a_statement_carries(library, pool, recorded):  # noqa: ARG001
    real = [author async for author in Author.objects.order_by("name")]
    unknown = [Author.from_row({"id": pk}) for pk in range(10**6, 10**6 + 40_000)]
    recorded.clear()
    await prefetch_related_objects([*unknown, *real], "pupils", "books")
    # pupils: 5 batches; books: 5 batches of links, then the targets of the authors that have books
    assert [operation.kind for operation in recorded] == ["select"] * 11
    assert [[p.name for p in author._state.related["pupils"]] for author in real] == [["Bob"], ["Cid"], [], []]
    assert [sorted(b.title for b in author._state.related["books"]) for author in real] == [
        ["Dune", "Physics"],
        ["Poems"],
        ["Physics"],
        [],
    ]
    assert all(author._state.related["pupils"] == () for author in unknown)


async def test_prefetch_and_in_bulk_in_batches_return_what_one_operation_does(library, pool, monkeypatch):  # noqa: ARG001
    from adorm.drivers import asyncpg as asyncpg_driver
    from adorm.models import prefetch

    def snapshot(books):
        return [
            (
                book.isbn,
                sorted(a.name for a in book._state.related["authors"]),
                [t.weight for t in book._state.related["tagging_set"]],
                [getattr(t._state.related.get("tag"), "name", None) for t in book._state.related["tagging_set"]],
            )
            for book in books
        ]

    lookups = ("authors", Prefetch("tagging_set", queryset=Tagging.objects.order_by("-weight")), "tagging_set__tag")
    expected = snapshot([book async for book in Book.objects.order_by("isbn").prefetch_related(*lookups)])
    ids = [DUNE, POEMS, PHYSICS, "9780000000999"]
    expected_bulk = sorted(await Book.objects.in_bulk(ids))
    monkeypatch.setattr(prefetch, "BATCH_SIZE", 1)
    monkeypatch.setattr(asyncpg_driver.driver, "IN_BULK_BATCH_SIZE", 1)
    assert snapshot([book async for book in Book.objects.order_by("isbn").prefetch_related(*lookups)]) == expected
    assert sorted(await Book.objects.in_bulk(ids)) == expected_bulk == sorted([DUNE, POEMS, PHYSICS])


# Children of existing parents


async def test_create_a_child_of_an_existing_parent(library, pool):  # noqa: ARG001
    physics = await Book.objects.get(isbn=PHYSICS)
    edition = await Edition.objects.create_from_parent(physics, released=datetime.date(2021, 3, 4), number=3)
    assert (edition.isbn, edition.book_ptr_id, edition.title, edition.pages, edition.number) == (
        PHYSICS,
        PHYSICS,
        "Physics",
        900,
        3,
    )
    assert edition.released == datetime.date(2021, 3, 4)
    stored = await in_thread(lambda: dj.Edition.objects.values_list("number", "book_ptr__title").get(pk=PHYSICS))
    assert stored == (3, "Physics")
    assert await Book.objects.count() == 4, "the parent row is not written again"
    with pytest.raises(adorm.IntegrityError):
        await Edition.objects.create_from_parent(PHYSICS, released=datetime.date(2021, 3, 4))


async def test_create_a_child_takes_the_parent_key_and_default_values(library, pool):  # noqa: ARG001
    edition = await Edition.objects.create_from_parent(POEMS, released=datetime.date(2022, 1, 1))
    assert (edition.number, edition.genre) == (2, "poetry")


async def test_create_a_child_errors(library, pool):  # noqa: ARG001
    with pytest.raises(Book.DoesNotExist):
        await Edition.objects.create_from_parent("9789999999999", released=datetime.date(2022, 1, 1))
    with pytest.raises(ValueError, match="'title' is an inherited field"):
        await Edition.objects.create_from_parent(POEMS, released=datetime.date(2022, 1, 1), title="x")
    with pytest.raises(ValueError, match="'released': .*valid date"):
        await Edition.objects.create_from_parent(POEMS, released="not a date")
    with pytest.raises(TypeError, match="not a multi-table inheritance child"):
        await Book.objects.create_from_parent(POEMS)
    author = await Author.objects.get(slug="ann")
    with pytest.raises(TypeError, match="takes a library.Book instance"):
        await Edition.objects.create_from_parent(author, released=datetime.date(2022, 1, 1))
    assert await Edition.objects.count() == 1


async def test_create_a_child_rolls_back_with_the_transaction(library, pool):  # noqa: ARG001
    async with adorm.atomic() as transaction:
        await Edition.objects.create_from_parent(POEMS, released=datetime.date(2022, 1, 1))
        assert await Edition.objects.count() == 2
        await transaction.rollback()
    assert await Edition.objects.count() == 1


# full_clean()


async def test_full_clean_reports_field_errors_by_adorm_name(library, pool):  # noqa: ARG001
    book = Book(isbn="9780000000777", title="x", pages=1)
    book.isbn = "12"
    book.pages = 0
    book.schema_ = "x" * 11
    with pytest.raises(ValidationError) as raised:
        await book.full_clean()
    errors = raised.value.message_dict
    assert set(errors) == {"isbn", "pages", "schema_"}
    await book.full_clean(exclude=["isbn", "pages", "schema_"])


async def test_full_clean_unique_checks_leave_the_instance_out(library, pool):  # noqa: ARG001
    ann = await Author.objects.get(slug="ann")
    await ann.full_clean()
    ann.slug = "bob"
    with pytest.raises(ValidationError) as raised:
        await ann.full_clean()
    assert list(raised.value.message_dict) == ["slug"]
    await ann.full_clean(validate_unique=False)
    await ann.full_clean(exclude=["slug"])
    new = Author(name="New", slug="ann")
    with pytest.raises(ValidationError, match="slug"):
        await new.full_clean()


async def test_full_clean_of_a_partly_loaded_instance_reads_the_row(library, pool):  # noqa: ARG001
    ann = await Author.objects.only("rating").get(slug="ann")
    ann.rating = 9
    with pytest.raises(ValidationError) as raised:
        await ann.full_clean()
    assert list(raised.value.message_dict) == ["rating"]
    ann.rating = 4
    await ann.full_clean()
    assert ann.get_deferred_fields() == {"name", "slug", "born", "website", "account_id", "mentor_id"}


async def test_full_clean_keys_a_foreign_key_error_by_the_relation(library, pool):  # noqa: ARG001
    loan = Loan(reader_id=1, book_id=DUNE, due=datetime.date(2026, 1, 1))
    loan.book_id = None
    with pytest.raises(ValidationError) as raised:
        await loan.full_clean()
    assert "book" in raised.value.message_dict


async def test_full_clean_sets_the_cleaned_values(library, pool):  # noqa: ARG001
    reader = await Reader.objects.get(name="Rea")
    reader.email = "rea@example.com"
    await reader.full_clean()
    assert reader.email == "rea@example.com"
    book = await Book.objects.get(isbn=DUNE)
    book.price = "3.5"
    await book.full_clean()
    assert book.price == Decimal("3.5")


@pytest.mark.parametrize("attempt", range(5))
async def test_concurrent_full_cleans_check_their_own_instances(library, pool, attempt):  # noqa: ARG001
    from django.db import connections

    before = getattr(connections._connections, "default", None)
    authors = [author async for author in Author.objects.order_by("slug")]
    for author in authors:
        author.slug = "ann"
    results = await asyncio.gather(*(author.full_clean() for author in authors), return_exceptions=True)
    assert [type(result).__name__ for result in results] == ["NoneType"] + ["ValidationError"] * 3
    assert all(list(result.message_dict) == ["slug"] for result in results[1:])
    assert getattr(connections._connections, "default", None) is before


async def test_full_clean_inside_a_transaction_sees_its_writes(library, pool):  # noqa: ARG001
    async with adorm.atomic() as transaction:
        await Author.objects.create(name="Eve", slug="eve")
        with pytest.raises(ValidationError, match="slug"):
            await Author(name="Eve 2", slug="eve").full_clean()
        await transaction.rollback()
    await Author(name="Eve 2", slug="eve").full_clean()


async def test_full_clean_exclude_names_are_checked(library, pool):  # noqa: ARG001
    with pytest.raises(ValueError, match="'nothing' is not a field"):
        await Book(isbn=DUNE, title="x", pages=1).full_clean(exclude=["nothing"])


# full_clean() on a profile whose alias is not Django's default


SECOND = "second"


TWO_ALIASES = pytest.mark.django_db(transaction=True, databases=["default", SECOND])


@pytest.fixture
async def two_pools(driver_class):
    """A router with the profiles ``default`` (alias ``default``) and ``second`` (the test settings' alias ``second``,
    the test database with connections of its own)."""
    pools = {}
    for name, alias in (("default", "default"), (SECOND, SECOND)):
        driver = driver_class()
        pools[name] = Pool(driver, ConnectionProfile(name, driver, alias=alias), max_connections=2)
    router = Router(list(pools.values()))
    previous = set_runner(router)
    try:
        yield pools
    finally:
        set_runner(previous)
        await router.close()


class Elsewhere:
    """A project router that sends every query to an alias Django does not know."""

    def db_for_read(self, model, **hints):  # noqa: ARG002
        return "nowhere"

    db_for_write = db_for_read


@TWO_ALIASES
@pytest.mark.parametrize("attempt", range(3))
async def test_full_clean_on_a_second_alias_sees_its_transactions_writes(library, two_pools, attempt):  # noqa: ARG001
    second = adorm.create_session(SECOND, f"clean-{attempt}")
    async with second.atomic() as transaction:
        eve = await second.Author.create(name="Eve", slug="eve")
        with second.activate():
            with pytest.raises(ValidationError) as raised:
                await Author(name="Eve 2", slug="eve").full_clean()
            assert list(raised.value.message_dict) == ["slug"]
            await Author(name="Kim", slug="kim", mentor_id=eve.id).full_clean()
        # The default profile's connections do not see the uncommitted row.
        await Author(name="Eve 2", slug="eve").full_clean()
        with pytest.raises(ValidationError) as raised:
            await Author(name="Kim", slug="kim", mentor_id=eve.id).full_clean()
        assert list(raised.value.message_dict) == ["mentor"]
        await transaction.rollback()
    assert all(pool.in_use == 0 for pool in two_pools.values())


@TWO_ALIASES
@pytest.mark.parametrize("attempt", range(3))
async def test_concurrent_full_cleans_on_two_aliases_check_their_own_transactions(library, two_pools, attempt):  # noqa: ARG001
    """Each flow writes a row only its open transaction sees and validates a duplicate of the other's row as well."""

    async def flow(using, mine, theirs):
        session = adorm.create_session(using, f"race-{attempt}")
        async with session.atomic() as transaction:
            await session.Author.create(name=mine.title(), slug=mine)
            outcomes = []
            with session.activate():
                for _ in range(5):
                    for slug in (mine, theirs):
                        try:
                            await Author(name="X", slug=slug).full_clean()
                        except ValidationError:
                            outcomes.append((slug, "taken"))
                        else:
                            outcomes.append((slug, "free"))
                        await asyncio.sleep(0)
            await transaction.rollback()
        return outcomes

    first, second = await asyncio.gather(flow("default", "ivy", "jan"), flow(SECOND, "jan", "ivy"))
    assert first == [("ivy", "taken"), ("jan", "free")] * 5
    assert second == [("jan", "taken"), ("ivy", "free")] * 5
    assert all(pool.in_use == 0 for pool in two_pools.values())
    assert await Author.objects.filter(slug__in=["ivy", "jan"]).count() == 0


def no_second_zed(monkeypatch):
    """Give `dj.Author` a ``clean()`` that queries its default manager without ``using()``."""
    from django.core.exceptions import ValidationError as DjangoValidationError

    def clean(self):  # noqa: ARG001
        if dj.Author.objects.filter(name="Zed").exists():
            raise DjangoValidationError("Zed is taken.", code="zed_taken")

    monkeypatch.setattr(dj.Author, "clean", clean)


@TWO_ALIASES
@pytest.mark.parametrize("attempt", range(3))
@pytest.mark.parametrize("routed", [False, True], ids=["django-routers", "project-router"])
async def test_full_clean_runs_the_models_clean_queries_on_the_profiles_alias(
    library,  # noqa: ARG001
    two_pools,
    monkeypatch,
    attempt,
    routed,
):
    from django.db import router

    no_second_zed(monkeypatch)
    if routed:
        monkeypatch.setattr(router, "routers", [Elsewhere()])
    second = adorm.create_session(SECOND, f"model-clean-{attempt}")
    async with second.atomic() as transaction:
        await second.Author.create(name="Zed", slug="zed")
        with second.activate():
            with pytest.raises(ValidationError, match="Zed is taken"):
                await Author(name="Ann 2", slug="ann-2").full_clean()
        # The default profile's connections do not see the uncommitted row.
        await Author(name="Ann 2", slug="ann-2").full_clean()
        await transaction.rollback()
    with second.activate():
        await Author(name="Ann 2", slug="ann-2").full_clean()
    assert all(pool.in_use == 0 for pool in two_pools.values())


@TWO_ALIASES
async def test_full_clean_runs_on_the_profiles_alias_whatever_the_projects_routers(library, two_pools, monkeypatch):
    from django.db import router

    await reserve(library["reader"], copy=3)
    elsewhere = Elsewhere()
    monkeypatch.setattr(router, "routers", [elsewhere])
    for using in ("default", SECOND):
        with adorm.create_session(using, "routed").activate():
            with pytest.raises(ValidationError) as raised:
                await Author(name="New", slug="ann").full_clean()
            assert list(raised.value.message_dict) == ["slug"]
            clash = Reservation(reader_id=library["reader"], book_id=DUNE, copy_=3, starts=TODAY, ends=TODAY)
            with pytest.raises(ValidationError, match="Book and Copy already exists"):
                await clash.full_clean()
            loan = Loan(reader_id=library["reader"], book_id="9780000000000", due=TODAY)
            with pytest.raises(ValidationError) as raised:
                await loan.full_clean()
            assert list(raised.value.message_dict) == ["book"]
    # Outside full_clean() the project's routers decide, here and on the slots' threads.
    assert router.db_for_read(dj.Author) == "nowhere"
    assert router.routers[1:] == [elsewhere]
    assert all(pool.in_use == 0 for pool in two_pools.values())


# Codes and params of validation errors


def errors(error):
    """``{key: [(code, params), ...]}`` of an adorm ``ValidationError`` built from a dict."""
    return {key: [(e.code, e.params) for e in found] for key, found in error.error_dict.items()}


TODAY = datetime.date(2026, 3, 1)
YESTERDAY = datetime.date(2026, 2, 28)


async def reserve(reader, copy=1):
    """A stored reservation of ``copy`` of Dune, made with the Django ORM."""
    return await in_thread(
        lambda: dj.Reservation.objects.create(reader_id=reader, book_id=DUNE, copy=copy, starts=TODAY, ends=TODAY).pk
    )


async def test_full_clean_errors_keep_their_codes_and_params(library, pool):  # noqa: ARG001
    book = Book(isbn="9780000000777", title="x", pages=1)
    book.isbn = "12"
    book.pages = 0
    book.schema_ = "x" * 11
    with pytest.raises(ValidationError) as raised:
        await book.full_clean()
    assert errors(raised.value) == {
        "isbn": [("invalid", {"value": "12"})],
        "pages": [("min_value", {"limit_value": 1, "show_value": 0, "value": 0})],
        "schema_": [("max_length", {"limit_value": 10, "show_value": 11, "value": "x" * 11})],
    }
    assert raised.value.message_dict["schema_"] == ["Ensure this value has at most 10 characters (it has 11)."]
    assert all(type(e.message) is str for found in raised.value.error_dict.values() for e in found)


async def test_full_clean_unique_and_blank_errors_name_their_code(library, pool):  # noqa: ARG001
    with pytest.raises(ValidationError) as raised:
        await Tag(name="rare").full_clean()
    assert errors(raised.value) == {
        "name": [
            (
                "unique",
                {
                    "model": "library.Tag",
                    "model_class": "library.Tag",
                    "model_name": "Tag",
                    "unique_check": ("name",),
                    "field_label": "Name",
                },
            )
        ]
    }
    assert raised.value.message_dict == {"name": ["Tag with this Name already exists."]}
    blank = Tag(name="new")
    blank.name = ""
    with pytest.raises(ValidationError) as raised:
        await blank.full_clean()
    assert errors(raised.value) == {"name": [("blank", None)]}
    short = Tag(name="new")
    short.name = "x"
    with pytest.raises(ValidationError) as raised:
        await short.full_clean()
    assert errors(raised.value) == {"name": [("min_length", {"limit_value": 2, "show_value": 1, "value": "x"})]}


async def test_full_clean_constraint_errors_keep_the_violation_code(library, pool):  # noqa: ARG001
    await reserve(library["reader"])
    clash = Reservation(reader_id=library["reader"], book_id=DUNE, copy_=1, starts=TODAY, ends=YESTERDAY)
    with pytest.raises(ValidationError) as raised:
        await clash.full_clean()
    assert errors(raised.value) == {
        "__all__": [
            ("ends_before_start", None),
            (
                "unique_together",
                {
                    "model": "library.Reservation",
                    "model_class": "library.Reservation",
                    "model_name": "Reservation",
                    "unique_check": ("book", "copy_"),
                    "field_labels": "Book and Copy",
                },
            ),
        ]
    }
    assert raised.value.message_dict == {
        "__all__": ["A reservation cannot end before it starts.", "Reservation with this Book and Copy already exists."]
    }
    clash.copy_, clash.ends = 2, TODAY
    await clash.full_clean()


async def test_full_clean_messages_follow_the_callers_language(library, pool):  # noqa: ARG001
    from django.utils import translation

    with translation.override("sl"):
        expected = str(dj.Tag._meta.get_field("name").error_messages["blank"])
        tag = Tag(name="new")
        tag.name = ""
        with pytest.raises(ValidationError) as raised:
            await tag.full_clean()
    assert expected != "This field cannot be blank."
    assert raised.value.message_dict == {"name": [expected]}
    assert raised.value.error_dict["name"][0].code == "blank"


@pytest.mark.parametrize("attempt", range(5))
async def test_concurrent_full_cleans_keep_the_params_of_their_own_instance(library, pool, attempt):  # noqa: ARG001
    books = []
    for length in range(11, 17):
        book = Book(isbn="9780000000777", title="x", pages=1)
        book.schema_ = "x" * length
        books.append(book)
    results = await asyncio.gather(*(book.full_clean() for book in books), return_exceptions=True)
    assert [errors(result)["schema_"][0][1]["show_value"] for result in results] == list(range(11, 17))
    assert pool.in_use == 0


@pytest.mark.parametrize("driver_class", ["django"], indirect=True)
async def test_a_save_that_validates_reports_the_same_errors_as_full_clean(library, pool):  # noqa: ARG001
    await reserve(library["reader"])
    values = {"reader_id": library["reader"], "book_id": DUNE, "copy_": 1, "starts": TODAY, "ends": YESTERDAY}
    with pytest.raises(ValidationError) as cleaned:
        await Reservation(**values).full_clean()
    with pytest.raises(ValidationError) as created:
        await Reservation.objects.create(**values)
    with pytest.raises(ValidationError) as saved:
        await Reservation(**values).save()
    assert errors(created.value) == errors(saved.value) == errors(cleaned.value)
    assert set(errors(created.value)) == {"__all__"}
    with pytest.raises(ValidationError) as field:
        await Reservation.objects.create(**{**values, "copy_": 40000, "book_id": "9780000000999", "ends": TODAY})
    assert errors(field.value) == {
        "book": [("invalid", {"model": "book", "pk": "9780000000999", "field": "isbn", "value": "9780000000999"})],
        "copy_": [("max_value", {"limit_value": 32767, "show_value": 40000, "value": 40000})],
    }
    assert await Reservation.objects.count() == 1


@pytest.mark.parametrize("driver_class", ["django"], indirect=True)
@pytest.mark.parametrize("attempt", range(3))
async def test_failed_validating_saves_release_their_slots(library, pool, attempt):  # noqa: ARG001
    values = {"reader_id": library["reader"], "book_id": DUNE, "starts": TODAY, "ends": YESTERDAY}
    results = await asyncio.gather(
        *(Reservation.objects.create(copy_=copy, **values) for copy in range(1, 7)), return_exceptions=True
    )
    assert all(errors(result) == {"__all__": [("ends_before_start", None)]} for result in results)
    assert pool.in_use == 0
    async with adorm.atomic():
        await Reservation.objects.create(**{**values, "copy_": 1, "ends": TODAY})
    assert pool.in_use == 0


@pytest.mark.parametrize("driver_class", ["django"], indirect=True)
@pytest.mark.parametrize("attempt", range(3))
async def test_a_save_racing_an_uncommitted_duplicate_fails_in_the_database(library, pool, database, attempt):  # noqa: ARG001
    await database.hold(
        "INSERT INTO library_reservation (reader_id, book_id, copy, starts, ends) VALUES ($1, $2, 1, $3, $3)",
        library["reader"],
        DUNE,
        TODAY,
    )
    values = {"reader_id": library["reader"], "book_id": DUNE, "copy_": 1, "starts": TODAY, "ends": TODAY}
    # The duplicate is not visible to the validation; the insert waits for the holder's transaction.
    task = asyncio.create_task(Reservation.objects.create(**values))
    await database.waiting()
    transaction, database.transaction = database.transaction, None
    await transaction.commit()
    with pytest.raises(adorm.IntegrityError) as raised:
        await task
    assert raised.value.sqlstate == "23505"
    with pytest.raises(ValidationError) as again:
        await Reservation.objects.create(**values)
    assert [e.code for e in again.value.error_dict["__all__"]] == ["unique_together"]
    assert pool.in_use == 0


# Cancellation and lost connections on the asyncpg driver


@pytest.fixture
async def asyncpg_pool():
    from adorm.drivers.asyncpg import AsyncpgDriver

    driver = AsyncpgDriver()
    pool = Pool(driver, ConnectionProfile("default", driver), max_connections=1, acquire_timeout=5)
    router = Router([pool])
    previous = set_runner(router)
    try:
        yield pool
    finally:
        set_runner(previous)
        await router.close()


def django_connections():
    from django.db import connections

    return dict(vars(connections._connections))


@pytest.mark.parametrize("attempt", range(3))
async def test_cancelling_a_full_clean_mid_statement_releases_the_slot(library, asyncpg_pool, database, attempt):  # noqa: ARG001
    before = django_connections()
    ann = await Author.objects.get(slug="ann")
    ann.slug = "bob"
    await database.hold("LOCK TABLE library_author IN ACCESS EXCLUSIVE MODE")
    task = asyncio.create_task(ann.full_clean())
    await database.waiting()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await database.release()
    assert asyncpg_pool.in_use == 0
    assert django_connections() == before
    with pytest.raises(ValidationError, match="slug"):
        await ann.full_clean()


@pytest.mark.parametrize("attempt", range(3))
async def test_a_connection_lost_during_full_clean_is_dropped(library, asyncpg_pool, database, attempt):  # noqa: ARG001
    from adorm.drivers import is_connection_error

    before = django_connections()
    ann = await Author.objects.get(slug="ann")
    await database.hold("LOCK TABLE library_author IN ACCESS EXCLUSIVE MODE")
    task = asyncio.create_task(ann.full_clean())
    await database.monitor.execute("SELECT pg_terminate_backend($1)", await database.waiting())
    with pytest.raises(adorm.DatabaseError) as lost:
        await task
    assert is_connection_error(lost.value)
    await database.release()
    assert (asyncpg_pool.size, asyncpg_pool.in_use) == (0, 0)
    assert django_connections() == before
    await ann.full_clean()


@pytest.mark.parametrize("attempt", range(3))
async def test_a_connection_lost_during_a_prefetch_caches_nothing(library, asyncpg_pool, database, attempt):  # noqa: ARG001
    from adorm.drivers import is_connection_error

    authors = [author async for author in Author.objects.order_by("name")]
    await database.hold("LOCK TABLE library_book IN ACCESS EXCLUSIVE MODE")
    task = asyncio.create_task(prefetch_related_objects(authors, "pupils", "books"))
    await database.monitor.execute("SELECT pg_terminate_backend($1)", await database.waiting())
    with pytest.raises(adorm.DatabaseError) as lost:
        await task
    assert is_connection_error(lost.value)
    await database.release()
    assert (asyncpg_pool.size, asyncpg_pool.in_use) == (0, 0)
    assert all("books" not in author._state.related for author in authors)
    assert all("pupils" in author._state.related for author in authors), "the relation read before stays cached"
    await prefetch_related_objects(authors, "books")
    assert sorted(book.title for book in authors[0]._state.related["books"]) == ["Dune", "Physics"]


@pytest.mark.parametrize("attempt", range(3))
async def test_cancelling_a_child_insert_writes_nothing_and_releases_the_slot(
    library,  # noqa: ARG001
    asyncpg_pool,
    database,
    attempt,  # noqa: ARG001
):
    await database.hold("LOCK TABLE library_edition IN ACCESS EXCLUSIVE MODE")
    task = asyncio.create_task(Edition.objects.create_from_parent(POEMS, released=datetime.date(2022, 1, 1)))
    await database.waiting()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await database.release()
    assert asyncpg_pool.in_use == 0
    assert await Edition.objects.count() == 1
    assert await Book.objects.count() == 4
    edition = await Edition.objects.create_from_parent(POEMS, released=datetime.date(2022, 1, 1))
    assert edition.title == "Poems"
