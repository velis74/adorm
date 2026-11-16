"""Writes and transactions of the native PostgreSQL driver: Django's write logic without Django's model methods and
signals, compared with the Django ORM's own results, savepoints, row locks, the transactions the driver opens for
writes of several statements, and the failures that matter in production: lock waits, races between flows, commits
that fail, cancellation and lost connections. The results the driver shares with every driver are in
``tests/conformance``."""

import asyncio
import contextvars
import datetime
import threading

from decimal import Decimal

import pytest

from django.apps import apps
from django.db import connection as django_connection, models, transaction
from django.db.models.signals import post_delete, post_save, pre_delete, pre_save

import adorm

from adorm import (
    ConnectionProfile,
    DatabaseError,
    F,
    IntegrityError,
    NotSupportedError,
    OperationalError,
    ProgrammingError,
    ProtectedError,
    RestrictedError,
    TransactionClosed,
    TransactionManagementError,
)
from adorm.drivers import is_connection_error
from adorm.drivers.asyncpg import AsyncpgConnection, AsyncpgDriver
from adorm.drivers.asyncpg.writes import Writer
from adorm.executor import Pool, Router, set_runner
from adorm.operations import Create, Select
from adorm.query import Query
from tests.djangoproject.library import models as dj
from tests.generated.library import (
    Author,
    Book,
    Edition,
    Fine,
    Loan,
    LoanPolicy,
    Note,
    Placement,
    Reader,
    Shelf,
    Tag,
    Tagging,
)
from tests.library_data import DUNE, DUNE_2, in_thread, PHYSICS, POEMS

pytestmark = pytest.mark.django_db(transaction=True)

REPEAT = range(3)
REQUEST = contextvars.ContextVar("request", default=None)
VOLATILE = frozenset({"added", "published", "date_joined", "last_login"})


@pytest.fixture
async def executor():
    """``make(**pool)`` sets up a router with one pool of `AsyncpgDriver` slots for the profile ``default``."""
    routers = []

    def make(**kwargs):
        driver = AsyncpgDriver()
        pool = Pool(driver, ConnectionProfile("default", driver), **kwargs)
        router = Router([pool])
        routers.append(router)
        set_runner(router)
        return pool

    previous = set_runner(None)
    try:
        yield make
    finally:
        set_runner(previous)
        for router in routers:
            await router.close()


@pytest.fixture
async def pool(executor):
    return executor(max_connections=2, acquire_timeout=5)


@pytest.fixture
async def connection(library):  # noqa: ARG001
    """An open `AsyncpgConnection` of the profile ``default``, closed afterwards."""
    driver = AsyncpgDriver()
    opened = await driver.open_connection(ConnectionProfile("default", driver), lambda _event: None)
    try:
        yield opened
    finally:
        await opened.close()


def tables_now():
    """Every row of the library tables, read with Django's connection, without columns whose values depend on the
    time of the write."""
    tables = sorted(m._meta.db_table for m in apps.get_app_config("library").get_models(include_auto_created=True))
    state = {}
    with django_connection.cursor() as cursor:
        for table in tables:
            cursor.execute(f'SELECT * FROM "{table}" ORDER BY 1')  # noqa: S608 - names from the app registry
            names = [column.name for column in cursor.description]
            state[table] = [
                {n: v for n, v in zip(names, row, strict=True) if n not in VOLATILE} for row in cursor.fetchall()
            ]
    return state


async def django_outcome(fn):
    """What ``fn()`` returns or raises with the Django ORM and the tables it leaves, in a transaction rolled back;
    deferred constraints are checked as a commit would check them."""

    def run():
        with transaction.atomic():
            try:
                with transaction.atomic():
                    result = ("ok", fn())
                    with django_connection.cursor() as cursor:
                        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            except Exception as exc:
                result = ("error", type(exc).__name__)
            state = tables_now()
            transaction.set_rollback(True)
        return result, state

    return await in_thread(run)


async def adorm_outcome(coroutine):
    """What awaiting ``coroutine`` returns or raises, named by the Django exception it stands for, and the tables
    after it."""
    try:
        result = ("ok", await coroutine)
    except (ProtectedError, RestrictedError, IntegrityError) as exc:
        result = ("error", type(exc).__name__)
    return result, await in_thread(tables_now)


async def tag_names():
    return sorted([tag.name async for tag in Tag.objects.all()])


async def rating(slug):
    return (await Author.objects.get(slug=slug)).rating


# Saving and creating


async def test_saving_writes_the_fields_the_instance_has(library, pool):
    ann = Author.from_row({"id": library["ann"], "name": "Ann"})  # loaded without the other fields
    ann.name = "Annie"
    await ann.save()
    stored = await Author.objects.get(slug="ann")
    assert (stored.name, stored.rating, stored.born) == ("Annie", 5, datetime.date(1970, 1, 1))
    bob = await Author.objects.only("rating").get(slug="bob")
    bob.rating = 1
    await bob.save()
    assert (await Author.objects.get(slug="bob")).name == "Bob"
    assert await rating("bob") == 1
    assert pool.in_use == 0


async def test_saving_a_missing_row_inserts_it_with_the_targets_defaults(library, pool):  # noqa: ARG001
    zed = Author(id=900, name="Zed", slug="zed")
    await zed.save()
    stored = await Author.objects.get(pk=900)
    assert (stored.name, stored.rating, stored.website) == ("Zed", 3, "")
    assert zed.rating == 3
    tag = Tag(id=950, name="fresh")
    await tag.save()
    tag.name = "fresher"
    await tag.save()
    assert await tag_names() == ["fresher", "rare"]


async def test_forced_and_limited_saves_never_insert(library, pool):
    with pytest.raises(DatabaseError, match="Forced update did not affect any rows"):
        await Author(id=901, name="Ghost", slug="ghost").save(force_update=True)
    ghost = Author.from_row({"id": 902, "name": "Ghost", "slug": "ghost"})
    with pytest.raises(DatabaseError, match="did not affect any rows"):
        await ghost.save(update_fields=["name"])
    assert not await Author.objects.filter(slug="ghost").exists()
    with pytest.raises(IntegrityError) as duplicate:
        await Author(id=library["ann"], name="Twin", slug="twin").save(force_insert=True)
    assert duplicate.value.sqlstate == "23505"
    assert pool.in_use == 0


async def test_save_with_update_fields_and_arithmetic(library, pool):  # noqa: ARG001
    ann = await Author.objects.get(pk=library["ann"])
    ann.name, ann.rating = "Changed", 1
    await ann.save(update_fields=["rating"])
    stored = await Author.objects.get(pk=library["ann"])
    assert (stored.name, stored.rating) == ("Ann", 1)
    stored.rating = F("rating") + 2
    await stored.save(update_fields=["rating"])
    assert await rating("ann") == 3


async def test_create_returns_the_values_the_target_sets(library, pool):  # noqa: ARG001
    before = datetime.datetime.now(datetime.UTC)
    book = await Book.objects.create(isbn="9780000000099", title="New", pages=10)
    assert book.added >= before
    assert book.published >= before
    stored = await Book.objects.get(pk="9780000000099")
    assert (stored.added, stored.published, stored.keywords, stored.extra) == (book.added, book.published, [], {})
    assert (stored.genre, stored.price, stored.in_print) == ("fiction", Decimal("0.00"), True)
    tag = await Tag.objects.create(name="night")
    assert isinstance(tag.pk, int)


async def test_renamed_fields_are_written_by_their_django_names(library, pool):  # noqa: ARG001
    content_type = await in_thread(lambda: dj.ContentType.objects.get_for_model(dj.Book).pk)
    note = await Note.objects.create(content_type_id=content_type, object_id=DUNE, json_="[]", schema_="v9")
    assert (note.json_, note.schema_, note.model_dump_, note.score) == ("[]", "v9", False, Decimal("2.5"))
    note.schema_ = "v10"
    await note.save(update_fields=["schema_"])
    stored = await in_thread(lambda: dj.Note.objects.get(pk=note.code))
    assert (stored.json, stored.schema) == ("[]", "v10")


async def test_a_multi_table_child_is_created_saved_and_updated_across_its_tables(library, pool):  # noqa: ARG001
    released = datetime.date(2024, 1, 1)
    edition = await Edition.objects.create(isbn="9780000000077", title="Third", pages=3, released=released)
    assert (edition.book_ptr_id, edition.number) == ("9780000000077", 2)
    stored = await in_thread(lambda: dj.Edition.objects.get(pk="9780000000077"))
    assert (stored.title, stored.pages, stored.released, stored.number) == ("Third", 3, released, 2)
    partial = await Edition.objects.only("number", "title").get(pk=DUNE_2)
    partial.number, partial.title = 3, "Dune II, revised"
    await partial.save()
    stored = await in_thread(lambda: dj.Edition.objects.get(pk=DUNE_2))
    assert (stored.number, stored.title, stored.pages) == (3, "Dune II, revised", 500)
    # A query set update of parent and child fields: the keys first, then one statement per table.
    assert await Edition.objects.filter(pages__gte=3).update(title="Edited", number=7) == 2
    assert await Edition.objects.filter(pk=DUNE_2).update(title="Parent only") == 1
    assert await Edition.objects.filter(pk="none").update(title="Nothing", number=1) == 0
    rows = await in_thread(lambda: sorted(dj.Edition.objects.values_list("isbn", "title", "number")))
    assert rows == [("9780000000004", "Parent only", 7), ("9780000000077", "Edited", 7)]
    assert await in_thread(lambda: dj.Book.objects.get(pk=DUNE).title) == "Dune"


async def test_order_with_respect_to_numbers_new_rows(library, pool):  # noqa: ARG001
    shelf = await Shelf.objects.create(code="A1")
    for isbn in (DUNE, DUNE, POEMS, DUNE):
        await Placement.objects.create(shelf_id=shelf.code, book_id=isbn)
    rows = [(p.book_id, p.order_) async for p in Placement.objects.order_by("id")]
    assert rows == [(DUNE, 0), (DUNE, 1), (POEMS, 0), (DUNE, 2)]


async def test_a_managers_create_without_an_override_runs_natively(library, pool):  # noqa: ARG001
    loan = await Loan.objects.create(reader_id=library["reader"], book_id=PHYSICS, due=datetime.date(2026, 3, 1))
    assert (loan.archived, loan.note) == (False, "")
    assert await in_thread(lambda: dj.Loan.all_loans.filter(book_id=PHYSICS).count()) == 1


# Updates and deletes, compared with the Django ORM


async def test_update_with_arithmetic_and_an_instance_value(library, pool):  # noqa: ARG001
    bob = await Author.objects.get(pk=library["bob"])
    assert await Author.objects.filter(rating__lt=5).update(rating=F("rating") + 1) == 3
    assert await Author.objects.filter(slug="cid").update(mentor=bob) == 1
    assert [a.rating async for a in Author.objects.order_by("slug")] == [5, 4, 2, 5]
    assert await Author.objects.filter(pk__in=[]).update(rating=1) == 0


DELETES = {
    "author_with_pupils_and_books": (
        lambda: dj.Author.objects.filter(slug="ann").delete(),
        lambda: Author.objects.filter(slug="ann").delete(),
    ),
    "book_with_loans_taggings_notes_and_authors": (
        lambda: dj.Book.objects.filter(pk=DUNE).delete(),
        lambda: Book.objects.filter(pk=DUNE).delete(),
    ),
    "multi_table_child": (
        lambda: dj.Edition.objects.filter(pk=DUNE_2).delete(),
        lambda: Edition.objects.filter(pk=DUNE_2).delete(),
    ),
    "multi_table_parent": (
        lambda: dj.Book.objects.filter(pk=DUNE_2).delete(),
        lambda: Book.objects.filter(pk=DUNE_2).delete(),
    ),
    "every_book": (lambda: dj.Book.objects.all().delete(), lambda: Book.objects.all().delete()),
    "reader_with_loans": (lambda: dj.Reader.objects.all().delete(), lambda: Reader.objects.all().delete()),
    "tags_only": (lambda: dj.Tagging.objects.all().delete(), lambda: Tagging.objects.all().delete()),
    "nothing": (lambda: dj.Author.objects.filter(pk=0).delete(), lambda: Author.objects.filter(pk=0).delete()),
}


@pytest.mark.parametrize("name", sorted(DELETES))
async def test_a_delete_matches_djangos(library, pool, name):  # noqa: ARG001
    django_delete, adorm_delete = DELETES[name]
    expected = await django_outcome(django_delete)
    assert await adorm_outcome(adorm_delete()) == expected
    assert pool.in_use == 0


async def test_deleting_an_instance_matches_django(library, pool):  # noqa: ARG001
    expected = await django_outcome(lambda: dj.Author.objects.get(slug="ann").delete())
    ann = await Author.objects.get(slug="ann")
    assert await adorm_outcome(ann.delete()) == expected
    assert ann.pk is None
    edition = await Edition.objects.get(pk=DUNE_2)
    assert await edition.delete() == (2, {"library.Book": 1, "library.Edition": 1})
    tag = await Tag.objects.get(name="rare")
    tag_counts = {"library.Tag": 1, "library.Tagging": 2}
    assert await tag.delete() == (3, tag_counts)
    assert await Tag.from_row({"id": 999_999, "name": "gone"}).delete() == (0, {})
    assert pool.in_use == 0


async def test_a_foreign_key_to_another_field_cascades(library, pool):  # noqa: ARG001
    def place():
        shelf = dj.Shelf.objects.create(code="B2")
        for isbn in (DUNE, POEMS):
            dj.Placement.objects.create(shelf=shelf, book_id=isbn)

    await in_thread(place)
    expected = await django_outcome(lambda: dj.Shelf.objects.filter(code="B2").delete())
    assert await adorm_outcome(Shelf.objects.filter(code="B2").delete()) == expected
    assert expected[0] == ("ok", (3, {"library.Placement": 2, "library.Shelf": 1}))


ON_DELETE = {
    "CASCADE": models.CASCADE,
    "PROTECT": models.PROTECT,
    "RESTRICT": models.RESTRICT,
    "SET_DEFAULT": models.SET_DEFAULT,
    "SET_value": None,  # SET(dora's primary key)
    "SET_callable": None,  # SET(a function returning it)
    "DO_NOTHING": models.DO_NOTHING,
}


@pytest.mark.parametrize("handler", sorted(ON_DELETE))
async def test_every_on_delete_handler_matches_django(library, pool, monkeypatch, handler):  # noqa: ARG001
    on_delete = ON_DELETE[handler]
    if handler == "SET_value":
        on_delete = models.SET(library["dora"])
    elif handler == "SET_callable":
        on_delete = models.SET(lambda: library["dora"])
    monkeypatch.setattr(dj.Author._meta.get_field("mentor").remote_field, "on_delete", on_delete)
    expected = await django_outcome(lambda: dj.Author.objects.filter(slug="ann").delete())
    assert await adorm_outcome(Author.objects.filter(slug="ann").delete()) == expected


async def test_a_custom_on_delete_handler_is_refused_before_anything_changes(library, pool, monkeypatch):  # noqa: ARG001
    def archive(collector, field, sub_objs, using):  # noqa: ARG001 - Django's handler signature
        collector.add_field_update(field, None, sub_objs)

    monkeypatch.setattr(dj.Author._meta.get_field("mentor").remote_field, "on_delete", archive)
    with pytest.raises(NotSupportedError, match="on_delete handlers, not"):
        await Author.objects.filter(slug="ann").delete()
    assert await Author.objects.count() == 4
    async with adorm.atomic():
        with pytest.raises(NotSupportedError):
            await Author.objects.filter(slug="ann").delete()
        await Tag.objects.create(name="after the refusal")
    assert await tag_names() == ["after the refusal", "rare"]


async def test_protected_and_restricted_deletes_raise_with_complete_instances(library, pool):
    loan = await Loan.objects.get(due=datetime.date(2026, 1, 1))
    fines = [await Fine.objects.create(loan_id=loan.pk, amount=Decimal(n)) for n in (1, 2)]
    deletes = (
        Loan.all_loans.filter(pk=loan.pk).delete,
        Book.objects.filter(pk=DUNE).delete,  # cascades to the loan
        Reader.objects.filter(pk=library["reader"]).delete,
    )
    for delete in deletes:
        with pytest.raises(RestrictedError) as refused:
            await delete()
        objects = refused.value.restricted_objects
        assert all(type(obj) is Fine for obj in objects)
        assert sorted((obj.pk, obj.amount, obj.waived) for obj in objects) == [(f.pk, f.amount, False) for f in fines]
    assert await Loan.all_loans.count() == 2
    # Deleting the fines with the loan's reader is allowed once nothing restricts it.
    await Fine.objects.all().delete()
    assert await Reader.objects.filter(pk=library["reader"]).delete() == (3, {"library.Loan": 2, "library.Reader": 1})
    # The refusal leaves the transaction usable, as in Django.
    async with adorm.atomic():
        with pytest.raises(ProtectedError) as protected:
            await _protect_tags()
        assert [type(obj).__name__ for obj in protected.value.protected_objects] == ["Tagging", "Tagging"]
        await Tag.objects.create(name="after the refusal")
    assert await tag_names() == ["after the refusal", "rare"]
    assert pool.in_use == 0


async def _protect_tags():
    field = dj.Tagging._meta.get_field("tag").remote_field
    original = field.on_delete
    field.on_delete = models.PROTECT
    try:
        await Tag.objects.filter(name="rare").delete()
    finally:
        field.on_delete = original


async def test_get_or_create_and_update_or_create_with_relations(library, pool):  # noqa: ARG001
    dune, physics = await Book.objects.get(pk=DUNE), await Book.objects.get(pk=PHYSICS)
    rare = await Tag.objects.get(name="rare")
    found, created = await Tagging.objects.get_or_create(book=dune, tag=rare, defaults={"weight": 2})
    assert (created, found.weight) == (False, 3)
    new, created = await Tagging.objects.get_or_create(book=physics, tag=rare, defaults={"weight": 2})
    assert (created, new.weight, new.book_id) == (True, 2, PHYSICS)
    updated, created = await Tagging.objects.update_or_create(book=physics, tag=rare, defaults={"weight": 1})
    assert (created, updated.pk, updated.weight) == (False, new.pk, 1)
    author, created = await Author.objects.update_or_create(
        slug="eve", defaults={"rating": 1}, create_defaults={"name": "Eve", "rating": 4, "mentor_id": library["ann"]}
    )
    assert (created, author.rating, author.mentor_id) == (True, 4, library["ann"])
    with pytest.raises(adorm.MultipleObjectsReturned):
        await Author.objects.get_or_create(rating__gte=3, defaults={"name": "X", "slug": "x"})


# Python code of the target ORM


async def test_overrides_of_djangos_write_methods_are_refused(library, pool, monkeypatch):  # noqa: ARG001
    with pytest.raises(NotSupportedError, match=r"query set of library.Reader overrides create\(\)"):
        await Reader.objects.create(name="Eve", username="eve")
    with pytest.raises(NotSupportedError, match=r"overrides delete\(\)"):
        await LoanPolicy.objects.all().delete()
    loan = await Loan.objects.get(due=datetime.date(2026, 1, 1))
    fine = await Fine.objects.create(loan_id=loan.pk, amount=Decimal("2.50"))
    with pytest.raises(NotSupportedError, match=r"library.Fine overrides delete\(\)"):
        await fine.delete()
    assert await Fine.objects.filter(pk=fine.pk).delete() == (1, {"library.Fine": 1})

    def save(self, *args, **kwargs):
        self.name = self.name.upper()
        super(dj.Tag, self).save(*args, **kwargs)

    monkeypatch.setattr(dj.Tag, "save", save)
    rare = await Tag.objects.get(name="rare")
    writes = (
        Tag.objects.create(name="night"),
        Tag(name="dawn").save(),
        rare.save(),
        Tag.objects.get_or_create(name="noon"),
        Tag.objects.update_or_create(name="rare", defaults={"name": "rarer"}),
    )
    for write in writes:
        with pytest.raises(NotSupportedError, match=r"library.Tag overrides save\(\)"):
            await write
    # Nothing was written, the refusals took no slot for long and leave a transaction usable.
    assert await tag_names() == ["rare"]
    async with adorm.atomic():
        with pytest.raises(NotSupportedError):
            await Tag.objects.create(name="night")
        assert await Tagging.objects.filter(tag=rare).update(weight=2) == 2
    assert await Tagging.objects.filter(weight=2).count() == 2
    assert pool.in_use == 0


async def test_writes_send_no_signals_and_run_on_the_event_loop(library, pool, monkeypatch):  # noqa: ARG001
    sent = []
    threads = set()
    original = Writer.__init__

    def receiver(sender, **kwargs):
        sent.append((sender, kwargs.get("instance")))

    def init(self, connection):
        threads.add(threading.get_ident())
        original(self, connection)

    def no_thread(*_args, **_kwargs):
        raise AssertionError("a write used a thread")

    async def request(number):
        REQUEST.set(number)
        async with adorm.atomic():
            tag = await Tag.objects.create(name=f"tag{number}")
            tag.name = f"tag{number}!"
            await tag.save()
        await Tag.objects.get_or_create(name=f"tag{number}!")
        await Author.objects.update_or_create(slug=f"a{number}", defaults={"name": f"A{number}"})
        await tag.delete()
        return REQUEST.get()

    async def requests():
        assert await asyncio.gather(*(request(n) for n in range(8))) == list(range(8))
        await Author.objects.filter(slug__startswith="a").exclude(slug="ann").delete()
        await Book.objects.filter(pk=PHYSICS).delete()
        await Edition.objects.filter(pk=DUNE_2).update(title="Again", number=9)

    signals = (pre_save, post_save, pre_delete, post_delete)
    for signal in signals:
        signal.connect(receiver, weak=False)
    try:
        await requests()  # the pool opens its connections
        # Undone before the loop closes, which may start a thread to shut down its default executor.
        with monkeypatch.context() as patched:
            patched.setattr(Writer, "__init__", init)
            patched.setattr(asyncio.get_running_loop(), "run_in_executor", no_thread)
            patched.setattr(threading.Thread, "start", no_thread)
            for _ in REPEAT:
                await requests()
    finally:
        for signal in signals:
            signal.disconnect(receiver)
    assert sent == []
    assert threads == {threading.get_ident()}
    assert await tag_names() == ["rare"]
    assert pool.size <= 2


# Row locks


async def test_select_for_update_needs_a_transaction(library, pool):  # noqa: ARG001
    with pytest.raises(TransactionManagementError, match="outside of a transaction"):
        await Author.objects.select_for_update().get(slug="ann")
    assert pool.in_use == 0
    async with adorm.atomic():
        assert (await Author.objects.select_for_update(of=["self"]).get(slug="ann")).name == "Ann"


@pytest.mark.parametrize("attempt", REPEAT)
async def test_select_for_update_serialises_a_concurrent_update_or_create(library, pool, database, attempt):  # noqa: ARG001
    locked, release = asyncio.Event(), asyncio.Event()

    async def holder():
        async with adorm.atomic():
            ann = await Author.objects.select_for_update().get(slug="ann")
            locked.set()
            await release.wait()
            ann.rating = 1
            await ann.save(update_fields=["rating"])

    async def updater():
        await locked.wait()
        # Outside an adorm transaction: the driver opens one for the locked read and the save.
        return await Author.objects.update_or_create(slug="ann", defaults={"rating": 2})

    first, second = asyncio.create_task(holder()), asyncio.create_task(updater())
    await database.waiting()
    release.set()
    await first
    author, created = await second
    assert (created, author.rating) == (False, 2)
    assert await rating("ann") == 2
    assert pool.in_use == 0


async def test_nowait_and_skip_locked_on_a_locked_row(library, pool, database):  # noqa: ARG001
    await database.lock_author("ann")

    async def nowait():
        async with adorm.atomic():
            await Author.objects.select_for_update(nowait=True).get(slug="ann")

    with pytest.raises(OperationalError) as busy:
        await nowait()
    assert busy.value.sqlstate == "55P03"
    async with adorm.atomic():
        free = [a.slug async for a in Author.objects.select_for_update(skip_locked=True).order_by("slug")]
    assert free == ["bob", "cid", "dora"]
    await database.release()
    assert pool.in_use == 0


@pytest.mark.parametrize("in_transaction", [True, False])
@pytest.mark.parametrize("attempt", REPEAT)
async def test_concurrent_get_or_create_of_one_row_creates_it_once(library, pool, database, in_transaction, attempt):  # noqa: ARG001
    # Another transaction inserted the tag and has not committed: the insert of get_or_create() waits for its unique
    # key, fails once it commits, and get_or_create() reads the row.
    await database.hold("INSERT INTO library_tag (name) VALUES ('new')")

    async def get_or_create():
        if not in_transaction:
            return await Tag.objects.get_or_create(name="new")
        async with adorm.atomic():
            result = await Tag.objects.get_or_create(name="new")
            await Tag.objects.create(name="after")  # the transaction survived the failed insert
            return result

    task = asyncio.create_task(get_or_create())
    await database.waiting()
    await database.transaction.commit()
    database.transaction = None
    tag, created = await task
    assert (created, tag.name) == (False, "new")
    assert await Tag.objects.filter(name="new").count() == 1
    assert await tag_names() == (["after", "new", "rare"] if in_transaction else ["new", "rare"])
    assert pool.in_use == 0


# Savepoints and transactions on a connection


async def test_savepoints_follow_the_slots_calls(connection):
    names = Select(Query("library.Tag").order_by("name").values_list("name", flat=True))
    with pytest.raises(TransactionManagementError, match="no transaction"):
        await connection.savepoint("s1")
    await connection.begin()
    with pytest.raises(TransactionManagementError, match="already open"):
        await connection.begin()
    await connection.savepoint("s1")
    await connection.execute(Create("library.Tag", {"name": "a1"}))
    await connection.savepoint('s"2')
    await connection.execute(Create("library.Tag", {"name": "a2"}))
    with pytest.raises(TransactionManagementError, match="already open"):
        await connection.savepoint('s"2')
    # Rolling back to s1 discards s"2; s1 stays open until it is released.
    await connection.rollback_to_savepoint("s1")
    with pytest.raises(TransactionManagementError, match="no savepoint 's\"2'"):
        await connection.release_savepoint('s"2')
    await connection.execute(Create("library.Tag", {"name": "b1"}))
    await connection.release_savepoint("s1")
    await connection.savepoint("s3")
    await connection.savepoint("s4")
    await connection.release_savepoint("s3")  # releases s4 with it
    with pytest.raises(TransactionManagementError, match="no savepoint 's4'"):
        await connection.rollback_to_savepoint("s4")
    await connection.commit()
    assert [row["name"] for row in await connection.execute(names)] == ["b1", "rare"]
    assert connection.is_usable()


async def test_a_failure_the_core_did_not_see_stops_the_commit(connection):
    for _ in REPEAT:
        await connection.begin()
        await connection.execute(Create("library.Tag", {"name": "lost"}))
        with pytest.raises(IntegrityError):
            await connection.execute(Create("library.Tag", {"name": "rare"}))
        with pytest.raises(TransactionManagementError, match="failed earlier"):
            await connection.commit()
        assert not connection.conn.is_in_transaction()
        await connection.begin()
        await connection.execute(Create("library.Tag", {"name": "next"}))
        await connection.commit()
        rows = await connection.execute(Select(Query("library.Tag").order_by("name").values_list("name", flat=True)))
        assert [row["name"] for row in rows] == ["next", "rare"]
        await connection.execute(Create("library.Tag", {"name": "x"}))  # outside a transaction
        await in_thread(lambda: dj.Tag.objects.exclude(name="rare").delete())
        assert connection.is_usable()


async def test_a_commit_the_database_refuses_raises_its_error(library, pool, database):  # noqa: ARG001
    async def orphan():
        async with adorm.atomic():
            await Tag.objects.create(name="orphan")
            # Django's foreign keys are deferred: the missing tag fails at COMMIT, not at the insert.
            await Tagging.objects.create(book_id=DUNE, tag_id=999_999, weight=1)

    for _ in REPEAT:
        with pytest.raises(IntegrityError) as refused:
            await orphan()
        assert refused.value.sqlstate == "23503"
        await database.transactions_ended()
        assert pool.in_use == 0
        assert await tag_names() == ["rare"]


@pytest.mark.parametrize("attempt", REPEAT)
async def test_an_integrity_error_inside_a_savepoint_leaves_the_transaction_usable(library, pool, attempt):  # noqa: ARG001
    async with adorm.atomic():
        await Tag.objects.create(name="outer")
        with pytest.raises(IntegrityError):
            async with adorm.atomic():
                await Tag.objects.create(name="rare")
        await Tag.objects.create(name="after")
    assert await tag_names() == ["after", "outer", "rare"]
    assert pool.in_use == 0


async def test_an_integrity_error_inside_a_transaction_rolls_it_back(library, executor):  # noqa: ARG001
    pool = executor(max_connections=1, acquire_timeout=5)

    async def duplicate():
        async with adorm.atomic():
            await Tag.objects.create(name="first")
            with pytest.raises(IntegrityError):
                await Tag.objects.create(name="rare")

    for _ in REPEAT:
        with pytest.raises(TransactionManagementError, match="rolled back because an operation failed"):
            await duplicate()
        assert await tag_names() == ["rare"]
        async with adorm.atomic():
            await Tag.objects.create(name="ok")
        await Tag.objects.filter(name="ok").delete()
    assert (pool.size, pool.in_use) == (1, 0)


# Cancellation, lost connections and abandoned transactions


@pytest.mark.parametrize("attempt", REPEAT)
async def test_cancelling_a_write_waiting_for_a_row_lock_rolls_its_transaction_back(library, pool, database, attempt):  # noqa: ARG001
    async def write():
        async with adorm.atomic():
            await Tag.objects.create(name="half")
            await Author.objects.filter(slug="ann").update(rating=0)

    await database.lock_author("ann")
    task = asyncio.create_task(write())
    await database.waiting()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await database.transactions_ended()
    await database.release()
    assert pool.in_use == 0
    assert await tag_names() == ["rare"]
    assert await rating("ann") == 5


async def test_cancellation_racing_a_write_keeps_the_transaction_whole(library, executor, database):  # noqa: ARG001
    pool = executor(max_connections=1, acquire_timeout=5)
    outcomes = set()
    for attempt in range(90):
        await Tag.objects.exclude(name="rare").delete()
        await Author.objects.filter(slug="ann").update(rating=5)

        async def write(number=attempt):
            async with adorm.atomic():
                await Tag.objects.create(name=f"t{number}")
                await Author.objects.filter(slug="ann").update(rating=0)
            return "done"

        await database.lock_author("ann")
        task = asyncio.create_task(write())
        await database.waiting()
        await database.release()
        # Cancel after 0 to 14 loop iterations, then after up to 2744, or once the transaction has ended.
        for _ in range(attempt % 15 if attempt < 60 else (attempt % 15) ** 3):
            if task.done():
                break
            await asyncio.sleep(0)
        task.cancel()
        try:
            outcomes.add(await task)
        except asyncio.CancelledError:
            outcomes.add("cancelled")
        await database.transactions_ended()
        assert pool.in_use == 0
        # Both writes of the transaction landed, or neither did.
        assert (await tag_names() == ["rare", f"t{attempt}"]) is (await rating("ann") == 0)
    assert outcomes == {"done", "cancelled"}


@pytest.mark.parametrize("attempt", REPEAT)
async def test_cancelling_a_write_inside_a_savepoint_leaves_the_transaction_usable(library, pool, database, attempt):  # noqa: ARG001
    async def nested():
        async with adorm.atomic():
            await Tag.objects.create(name="dropped")
            await Author.objects.filter(slug="ann").update(rating=0)

    await database.lock_author("ann")
    async with adorm.atomic():
        await Tag.objects.create(name="outer")
        inner = asyncio.create_task(nested())
        await database.waiting()
        inner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await inner
        await Tag.objects.create(name="after")
    await database.release()
    assert await tag_names() == ["after", "outer", "rare"]
    assert await rating("ann") == 5
    assert pool.in_use == 0


async def create_edition(isbn):
    return await Edition.objects.create(isbn=isbn, title="Held", pages=1, released=datetime.date(2024, 1, 1))


async def edition_rows(isbn):
    def count():
        return dj.Book.objects.filter(pk=isbn).count(), dj.Edition.objects.filter(pk=isbn).count()

    return await in_thread(count)


@pytest.mark.parametrize("attempt", REPEAT)
async def test_cancelling_a_write_of_several_statements_leaves_none_of_them(library, executor, database, attempt):  # noqa: ARG001
    # The parent row is inserted; the child row waits for the lock on its table.
    pool = executor(max_connections=1, acquire_timeout=5)
    await database.hold("LOCK TABLE library_edition IN ACCESS EXCLUSIVE MODE")
    task = asyncio.create_task(create_edition("9780000000055"))
    await database.waiting()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await database.release()
    # The driver's own transaction is not reused: the slot closes its connection, which rolls it back.
    await database.transactions_ended()
    assert await edition_rows("9780000000055") == (0, 0)
    assert pool.in_use == 0
    edition = await create_edition("9780000000055")
    assert await edition_rows(edition.isbn) == (1, 1)
    assert pool.size == 1


async def test_cancellation_racing_a_write_of_several_statements_keeps_it_whole(library, executor, database):  # noqa: ARG001
    pool = executor(max_connections=1, acquire_timeout=5)
    outcomes = set()
    for attempt in range(60):
        isbn = f"97811111{attempt:05d}"
        await database.hold("LOCK TABLE library_edition IN ACCESS EXCLUSIVE MODE")
        task = asyncio.create_task(create_edition(isbn))
        await database.waiting()
        await database.release()
        for _ in range(attempt % 12 if attempt < 36 else (attempt % 12) ** 3):
            if task.done():
                break
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
            outcomes.add("done")
        except asyncio.CancelledError:
            outcomes.add("cancelled")
        await database.transactions_ended()
        assert pool.in_use == 0
        assert await edition_rows(isbn) in {(0, 0), (1, 1)}
    assert outcomes == {"done", "cancelled"}


@pytest.mark.parametrize("attempt", REPEAT)
async def test_cancelling_a_cascading_delete_deletes_nothing(library, executor, database, attempt):  # noqa: ARG001
    # The related rows are deleted first; the book's own row waits for its row lock.
    pool = executor(max_connections=1, acquire_timeout=5)
    before = await in_thread(tables_now)
    await database.hold("SELECT isbn FROM library_book WHERE isbn = $1 FOR UPDATE", DUNE)
    task = asyncio.create_task(Book.objects.filter(pk=DUNE).delete())
    await database.waiting()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await database.release()
    await database.transactions_ended()
    assert await in_thread(tables_now) == before
    assert pool.in_use == 0
    total, counts = await Book.objects.filter(pk=DUNE).delete()
    assert counts["library.Book"] == 1
    assert total == sum(counts.values())


async def test_a_connection_lost_mid_write_is_dropped(library, executor, database):  # noqa: ARG001
    pool = executor(max_connections=1, acquire_timeout=5)
    for _ in REPEAT:

        async def write():
            async with adorm.atomic():
                await Tag.objects.create(name="lost")
                await Author.objects.filter(slug="ann").update(rating=0)

        await database.lock_author("ann")
        task = asyncio.create_task(write())
        await database.monitor.execute("SELECT pg_terminate_backend($1)", await database.waiting())
        with pytest.raises(DatabaseError) as lost:
            await task
        assert is_connection_error(lost.value)
        await database.release()
        assert (pool.size, pool.in_use) == (0, 0)
        assert await tag_names() == ["rare"]
        assert await rating("ann") == 5


async def test_a_connection_lost_inside_the_drivers_own_transaction_is_dropped(library, executor, database):  # noqa: ARG001
    pool = executor(max_connections=1, acquire_timeout=5)
    for attempt in REPEAT:
        isbn = f"978222222222{attempt}"
        await database.hold("LOCK TABLE library_edition IN ACCESS EXCLUSIVE MODE")
        task = asyncio.create_task(create_edition(isbn))
        await database.monitor.execute("SELECT pg_terminate_backend($1)", await database.waiting())
        with pytest.raises(DatabaseError) as lost:
            await task
        assert is_connection_error(lost.value)
        await database.release()
        assert (pool.size, pool.in_use) == (0, 0)
        assert await edition_rows(isbn) == (0, 0)


async def test_an_abandoned_transaction_is_rolled_back_by_the_slot(library, executor, database):  # noqa: ARG001
    pool = executor(max_connections=1, acquire_timeout=5, commit_timeout=0.2)

    async def abandoned():
        async with adorm.atomic():
            await Tag.objects.create(name="abandoned")
            await database.transactions_ended()  # the slot gives up waiting for the next request

    with pytest.raises(TransactionClosed, match="rolled back the transaction"):
        await abandoned()
    assert await tag_names() == ["rare"]
    assert (pool.size, pool.in_use) == (1, 0)


async def test_many_flows_write_concurrently_on_a_small_pool(library, executor):  # noqa: ARG001
    pool = executor(max_connections=3, acquire_timeout=10)

    async def flow(number):
        async with adorm.atomic():
            tag = await Tag.objects.create(name=f"c{number}")
            async with adorm.atomic():
                await Author.objects.filter(slug="ann").update(rating=F("rating") + 0)
                tag.name = f"c{number}!"
                await tag.save(update_fields=["name"])
            await Tagging.objects.get_or_create(book_id=PHYSICS, tag_id=tag.pk, defaults={"weight": 2})
        return tag.pk

    for _ in REPEAT:
        await Tag.objects.exclude(name="rare").delete()
        pks = await asyncio.gather(*(flow(n) for n in range(30)))
        assert len(set(pks)) == 30
        assert await tag_names() == sorted(["rare", *(f"c{n}!" for n in range(30))])
        assert await Tagging.objects.filter(book_id=PHYSICS).count() == 30
    assert pool.size <= 3
    assert pool.in_use == 0


def test_the_connection_runs_every_write_kind():
    from adorm.drivers.asyncpg.writes import WRITES

    assert set(WRITES) == AsyncpgDriver.capabilities.operations - {
        "select",
        "get",
        "first",
        "last",
        "count",
        "exists",
        "aggregate",
        "in_bulk",
        "check_table",
    }
    assert all(callable(write) for write in WRITES.values())
    assert AsyncpgConnection.__dict__.get("_savepoint") is not None


async def test_a_delete_in_several_batches_is_one_transaction(library, executor, database):
    # 300 loans are deleted 100 keys per statement, highest key first; the lowest waits for its row lock.
    pool = executor(max_connections=1, acquire_timeout=5)

    def lend():
        reader = dj.Reader.objects.get(pk=library["reader"])
        dj.Loan.objects.bulk_create(
            dj.Loan(reader=reader, book_id=POEMS, due=datetime.date(2027, 1, 1)) for _ in range(298)
        )
        return min(dj.Loan.all_loans.values_list("pk", flat=True))

    lowest = await in_thread(lend)
    for _ in REPEAT:
        await database.hold("SELECT id FROM library_loan WHERE id = $1 FOR UPDATE", lowest)
        task = asyncio.create_task(Loan.all_loans.all().delete())
        await database.waiting()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await database.release()
        await database.transactions_ended()
        assert await Loan.all_loans.count() == 300
        assert pool.in_use == 0
    assert await Loan.all_loans.all().delete() == (300, {"library.Loan": 300})


@pytest.mark.parametrize("statement", ["BEGIN", "COMMIT"])
async def test_a_cancellation_right_after_the_drivers_own_begin_or_commit_gives_the_connection_up(
    connection, monkeypatch, statement
):
    # The statement completed on the server, and the caller is cancelled before it resumes.
    original = AsyncpgConnection._command

    async def command(self, sql):
        status = await original(self, sql)
        if sql.startswith(statement):
            asyncio.current_task().cancel()
            await asyncio.sleep(0)
        return status

    monkeypatch.setattr(AsyncpgConnection, "_command", command)
    values = {"isbn": "9780000000066", "title": "Cut", "pages": 1, "released": datetime.date(2024, 1, 1)}
    task = asyncio.create_task(connection.execute(Create("library.Edition", values)))
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not connection.is_usable()


# Writes that fail partway through inside a transaction

GARBAGE_EDITION = {"isbn": "9780000000073", "title": "Half", "pages": 1, "released": "garbage"}


async def test_a_write_failing_after_its_first_statement_leaves_the_transaction_rollback_only(connection):
    # The book row is inserted, then the edition's date fails to convert on the client.
    books = Select(Query("library.Book").values_list("isbn", flat=True))
    for _ in REPEAT:
        await connection.begin()
        with pytest.raises(adorm.ValidationError):
            await connection.execute(Create("library.Edition", GARBAGE_EDITION))
        with pytest.raises(TransactionManagementError, match="partway through"):
            await connection.execute(books)
        with pytest.raises(TransactionManagementError, match="partway through"):
            await connection.commit()
        assert not connection.conn.is_in_transaction()
        assert len(await connection.execute(books)) == 4
        assert connection.is_usable()


async def test_rolling_back_to_a_savepoint_undoes_a_write_that_failed_partway_through(connection):
    tags = Select(Query("library.Tag").order_by("name").values_list("name", flat=True))
    await connection.begin()
    await connection.execute(Create("library.Tag", {"name": "kept"}))
    await connection.savepoint("s1")
    await connection.savepoint("s2")
    with pytest.raises(adorm.ValidationError):
        await connection.execute(Create("library.Edition", GARBAGE_EDITION))
    await connection.release_savepoint("s2")  # the failed write is now part of s1
    await connection.rollback_to_savepoint("s1")
    await connection.execute(Create("library.Tag", {"name": "after"}))
    await connection.commit()
    assert [row["name"] for row in await connection.execute(tags)] == ["after", "kept", "rare"]


async def test_a_caught_failure_partway_through_a_write_never_commits_its_first_rows(library, executor, monkeypatch):  # noqa: ARG001
    # A child field whose pre_save() queries the database, as a slug field may: refused after the book row is in.
    from django.core.exceptions import SynchronousOnlyOperation

    def pre_save(instance, add):  # noqa: ARG001 - Django's signature
        raise SynchronousOnlyOperation("You cannot call this from an async context")

    monkeypatch.setattr(dj.Edition._meta.get_field("released"), "pre_save", pre_save)
    values = {**GARBAGE_EDITION, "released": datetime.date(2024, 1, 1)}
    pool = executor(max_connections=1, acquire_timeout=5)

    async def caught():
        async with adorm.atomic():
            await Tag.objects.create(name="before")
            with pytest.raises(NotSupportedError):
                await Edition.objects.create(**values)

    for _ in REPEAT:
        with pytest.raises(TransactionManagementError):
            await caught()
        assert await Book.objects.count() == 4
        assert await tag_names() == ["rare"]
        # Inside a savepoint the failure is rolled back with it and the transaction goes on.
        async with adorm.atomic():
            await Tag.objects.create(name="outer")
            with pytest.raises(NotSupportedError):
                async with adorm.atomic():
                    await Edition.objects.create(**values)
            await Tag.objects.create(name="after")
        assert await Book.objects.count() == 4
        assert await tag_names() == ["after", "outer", "rare"]
        await Tag.objects.exclude(name="rare").delete()
        assert (pool.size, pool.in_use) == (1, 0)


# Deletes larger than one statement's parameters


async def test_a_delete_beyond_the_parameter_limit_matches_django(library, pool, monkeypatch):  # noqa: ARG001
    # 600 pupils of ann: the SET_NULL updates and the many-to-many deletes need several statements each.
    from adorm.drivers.asyncpg import compiler, writes

    def pupils():
        ann = dj.Author.objects.get(pk=library["ann"])
        created = dj.Author.objects.bulk_create(dj.Author(name=f"P{n}", slug=f"p{n}", mentor=ann) for n in range(600))
        dj.Book.objects.get(pk=DUNE).authors.add(*created)

    await in_thread(pupils)
    monkeypatch.setattr(compiler, "MAX_QUERY_PARAMS", 250)
    monkeypatch.setattr(writes, "BATCH_SIZE", 100)
    expected = await django_outcome(lambda: dj.Author.objects.all().delete())
    assert expected[0][0] == "ok"
    assert await adorm_outcome(Author.objects.all().delete()) == expected
    assert pool.in_use == 0


def test_delete_batches_stay_within_the_parameter_limit(monkeypatch):
    from adorm.drivers.asyncpg import compiler, writes

    assert writes._batch_size(1) == writes.BATCH_SIZE
    assert writes._batch_size(4) == compiler.MAX_QUERY_PARAMS // 4
    monkeypatch.setattr(compiler, "MAX_QUERY_PARAMS", 5)
    assert [writes._batch_size(n) for n in (1, 2, 6)] == [5, 2, 1]


# Bulk creates, compared with the Django ORM


def _books(model, prefix):
    return [
        model(isbn=f"{prefix}0000000001", title="No extras", pages=1, weight=None, cover=None),
        model(
            isbn=f"{prefix}0000000002",
            title="Every type",
            genre="science",
            pages=2,
            price=Decimal("3.50"),
            weight=1.25,
            reading_time=datetime.timedelta(hours=2),
            cover=b"\x89PNG",
            keywords=["a", "b"],
            extra={"k": [1, None]},
            in_print=False,
        ),
        model(isbn=f"{prefix}0000000003", title="Nulls last", pages=3, weight=2.5, cover=b"", keywords=[]),
    ]


BULK_CASES = {
    "fields of every type with NULLs first": (
        lambda: dj.Book.objects.bulk_create(_books(dj.Book, "979")),
        lambda: Book.objects.bulk_create(_books(Book, "979")),
    ),
    "renamed fields and a proxy model": (
        lambda: dj.BookProxy.objects.bulk_create([dj.BookProxy(isbn="9790000000009", title="P", pages=1, schema="v7")]),
        lambda: Book.objects.bulk_create([Book(isbn="9790000000009", title="P", pages=1, schema_="v7")]),
    ),
    "given primary keys and foreign keys": (
        lambda: dj.Author.objects.bulk_create(
            [dj.Author(id=800 + n, name=f"N{n}", slug=f"n{n}", mentor_id=800 if n else None) for n in range(4)]
        ),
        lambda: Author.objects.bulk_create(
            [Author(id=800 + n, name=f"N{n}", slug=f"n{n}", mentor_id=800 if n else None) for n in range(4)]
        ),
    ),
    "a row that conflicts is ignored": (
        lambda: dj.Tag.objects.bulk_create(
            [dj.Tag(id=700, name="rare"), dj.Tag(id=701, name="fresh")], ignore_conflicts=True
        ),
        lambda: Tag.objects.bulk_create([Tag(id=700, name="rare"), Tag(id=701, name="fresh")], ignore_conflicts=True),
    ),
    "a row that conflicts updates the stored row": (
        lambda: dj.Author.objects.bulk_create(
            [dj.Author(id=810, name="X", slug="ann", rating=0), dj.Author(id=811, name="Y", slug="yan", rating=1)],
            update_conflicts=True,
            update_fields=["rating", "name"],
            unique_fields=["slug"],
        ),
        lambda: Author.objects.bulk_create(
            [Author(id=810, name="X", slug="ann", rating=0), Author(id=811, name="Y", slug="yan", rating=1)],
            update_conflicts=True,
            update_fields=["rating", "name"],
            unique_fields=["slug"],
        ),
    ),
    "a duplicate key inserts nothing": (
        lambda: dj.Author.objects.bulk_create(
            [dj.Author(id=820, name="Z", slug="zed"), dj.Author(id=821, name="Ann", slug="ann")], batch_size=1
        ),
        lambda: Author.objects.bulk_create(
            [Author(id=820, name="Z", slug="zed"), Author(id=821, name="Ann", slug="ann")], batch_size=1
        ),
    ),
}


@pytest.mark.parametrize("name", BULK_CASES)
async def test_a_bulk_create_matches_django(library, pool, name):  # noqa: ARG001
    django_write, adorm_write = BULK_CASES[name]

    async def created():
        return [instance.pk for instance in await adorm_write()]

    expected = await django_outcome(lambda: [instance.pk for instance in django_write()])
    actual = await adorm_outcome(created())
    if actual != expected:
        import pprint

        print("RESULT", expected[0], actual[0])
        for k in expected[1]:
            if expected[1][k] != actual[1][k]:
                pprint.pprint((k, expected[1][k], actual[1][k]))
    assert actual == expected
    assert pool.in_use == 0


async def test_bulk_create_numbers_rows_with_order_with_respect_to_as_django_does(library, pool):  # noqa: ARG001
    def django_write():
        shelf = dj.Shelf.objects.create(id=60, code="B1")
        dj.Placement.objects.create(id=60, shelf=shelf, book_id=DUNE)
        dj.Placement.objects.bulk_create(
            [dj.Placement(id=61 + n, shelf=shelf, book_id=isbn) for n, isbn in enumerate((DUNE, POEMS, DUNE))]
        )

    async def adorm_write():
        # Django 5.2 leaves _order unset, which the database refuses: the block leaves no row then, as Django's.
        async with adorm.atomic():
            await Shelf.objects.create(id=60, code="B1")
            await Placement.objects.create(id=60, shelf_id="B1", book_id=DUNE)
            placements = [
                Placement(id=61 + n, shelf_id="B1", book_id=isbn) for n, isbn in enumerate((DUNE, POEMS, DUNE))
            ]
            await Placement.objects.bulk_create(placements)

    expected = await django_outcome(django_write)
    assert await adorm_outcome(adorm_write()) == expected


async def test_bulk_create_refuses_what_django_refuses_before_anything_runs(library, pool):  # noqa: ARG001
    with pytest.raises(ProgrammingError, match="multi-table inheritance child"):
        await Edition.objects.bulk_create(
            [Edition(isbn="9790000000010", title="E", pages=1, released=datetime.date(2024, 1, 1))]
        )
    with pytest.raises(ProgrammingError, match="update_conflicts needs unique_fields"):
        await Author.objects.bulk_create([Author(name="A", slug="a")], update_conflicts=True, update_fields=["name"])
    with pytest.raises(ProgrammingError, match="cannot update a primary key"):
        await Author.objects.bulk_create(
            [Author(name="A", slug="a")], update_conflicts=True, update_fields=["id"], unique_fields=["slug"]
        )
    assert (pool.size, pool.in_use) == (1, 0)


async def test_a_bulk_create_beyond_the_parameter_limit_is_one_transaction(library, executor, database, monkeypatch):  # noqa: ARG001
    # Seven columns per author and at most 20 parameters per statement: two rows per INSERT.
    from adorm.drivers.asyncpg import compiler

    monkeypatch.setattr(compiler, "MAX_QUERY_PARAMS", 20)
    pool = executor(max_connections=1, acquire_timeout=5)
    inserts = []
    compile_insert = compiler.DjangoCompiler.compile_insert

    def counting(self, query, returning=()):
        inserts.append(len(query.objs))
        return compile_insert(self, query, returning)

    monkeypatch.setattr(compiler.DjangoCompiler, "compile_insert", counting)
    created = await Author.objects.bulk_create([Author(name=f"B{n}", slug=f"bulk{n}") for n in range(7)])
    assert inserts == [2, 2, 2, 1]
    assert len({author.pk for author in created}) == 7
    assert await Author.objects.filter(slug__startswith="bulk").count() == 7
    # The last statement fails on a duplicate slug; the three before it are rolled back with it.
    inserts.clear()
    with pytest.raises(IntegrityError):
        await Author.objects.bulk_create(
            [Author(name=f"C{n}", slug=f"cut{n}") for n in range(6)] + [Author(name="D", slug="ann")]
        )
    assert inserts == [2, 2, 2, 1]
    assert await Author.objects.filter(slug__startswith="cut").count() == 0
    await database.transactions_ended()
    assert pool.in_use == 0
