"""Writes and transactions of the Django driver on PostgreSQL: what Django does on the driver side (signals, its
``on_commit()`` callbacks, its own atomic blocks), row locks, savepoints, and the failures that matter in production:
lock waits, races between flows, commits that fail, cancellation and lost connections. The results the driver shares
with every driver are in ``tests/conformance``."""

import asyncio
import contextvars
import datetime
import logging
import threading

from decimal import Decimal

import pytest

from django.db import connections, transaction
from django.db.models.signals import post_save

import adorm

from adorm import (
    ConnectionProfile,
    DatabaseError,
    F,
    IntegrityError,
    NotSupportedError,
    OperationalError,
    RestrictedError,
    TransactionClosed,
    TransactionManagementError,
)
from adorm.drivers import is_connection_error
from adorm.drivers.django import DjangoConnection, DjangoDriver
from adorm.executor import Pool, Router, set_runner
from adorm.operations import Create, Save, Select
from adorm.query import Query
from tests.djangoproject.library import models as dj
from tests.generated.library import Author, Book, Edition, Fine, Loan, LoanPolicy, Note, Reader, Tag, Tagging
from tests.library_data import DUNE, DUNE_2, in_thread, PHYSICS

pytestmark = pytest.mark.django_db(transaction=True)

REPEAT = range(3)
REQUEST = contextvars.ContextVar("request", default=None)


@pytest.fixture
async def executor():
    """``make(**pool)`` sets up a router with one pool of `DjangoDriver` slots for the profile ``default``."""
    routers = []

    def make(driver=None, **kwargs):
        driver = driver or DjangoDriver()
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
def receiver():
    """``receiver(fn, sender)`` connects ``fn`` to Django's ``post_save`` for the test."""
    connected = []

    def connect(fn, sender):
        post_save.connect(fn, sender=sender, weak=False)
        connected.append((fn, sender))

    yield connect
    for fn, sender in connected:
        post_save.disconnect(fn, sender=sender)


async def tag_names():
    return sorted([tag.name async for tag in Tag.objects.all()])


async def rating(slug):
    return (await Author.objects.get(slug=slug)).rating


# Saving


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
    assert zed.rating == 3  # the value the target set is returned


async def test_forced_and_limited_saves_never_insert(library, pool):  # noqa: ARG001
    with pytest.raises(DatabaseError, match="Forced update did not affect any rows"):
        await Author(id=901, name="Ghost", slug="ghost").save(force_update=True)
    ghost = Author.from_row({"id": 902, "name": "Ghost", "slug": "ghost"})
    with pytest.raises(DatabaseError, match="did not affect any rows"):
        await ghost.save(update_fields=["name"])
    assert not await Author.objects.filter(slug="ghost").exists()
    with pytest.raises(IntegrityError) as duplicate:
        await Author(id=library["ann"], name="Twin", slug="twin").save(force_insert=True)
    assert duplicate.value.sqlstate == "23505"


async def test_a_complete_instance_with_a_new_key_is_inserted(library, pool):  # noqa: ARG001
    tag = Tag(id=950, name="fresh")
    await tag.save()
    tag.name = "fresher"
    await tag.save()
    assert await tag_names() == ["fresher", "rare"]


async def test_save_with_update_fields_writes_only_those(library, pool):  # noqa: ARG001
    ann = await Author.objects.get(pk=library["ann"])
    ann.name, ann.rating = "Changed", 1
    await ann.save(update_fields=["rating"])
    stored = await Author.objects.get(pk=library["ann"])
    assert (stored.name, stored.rating) == ("Ann", 1)


async def test_create_returns_the_values_the_target_sets(library, pool):  # noqa: ARG001
    book = await Book.objects.create(isbn="9780000000099", title="New", pages=10)
    assert book.added is not None
    assert book.published is not None
    stored = await Book.objects.get(pk="9780000000099")
    assert (stored.added, stored.published, stored.keywords, stored.extra) == (book.added, book.published, [], {})


async def test_renamed_fields_are_written_by_their_django_names(library, pool):  # noqa: ARG001
    content_type = await in_thread(lambda: dj.ContentType.objects.get_for_model(dj.Book).pk)
    note = await Note.objects.create(content_type_id=content_type, object_id=DUNE, json_="[]", schema_="v9")
    assert (note.json_, note.schema_, note.model_dump_) == ("[]", "v9", False)
    note.schema_ = "v10"
    await note.save(update_fields=["schema_"])
    stored = await in_thread(lambda: dj.Note.objects.get(pk=note.code))
    assert (stored.json, stored.schema) == ("[]", "v10")


async def test_saving_a_multi_table_child_writes_parent_and_child_fields(library, pool):  # noqa: ARG001
    edition = await Edition.objects.only("number", "title").get(pk=DUNE_2)
    edition.number, edition.title = 3, "Dune II, revised"
    await edition.save()
    stored = await in_thread(lambda: dj.Edition.objects.get(pk=DUNE_2))
    assert (stored.number, stored.title, stored.pages) == (3, "Dune II, revised", 500)


async def test_a_managers_create_override_runs(library, pool):  # noqa: ARG001
    reader = await Reader.objects.create(name="Eve", username="eve")
    assert reader.name == "Eve"
    assert await in_thread(lambda: dj.Reader.objects.filter(name="Eve").exists())


# Updates and deletes


async def test_update_with_arithmetic_and_an_instance_value(library, pool):  # noqa: ARG001
    bob = await Author.objects.get(pk=library["bob"])
    assert await Author.objects.filter(rating__lt=5).update(rating=F("rating") + 1) == 3
    assert await Author.objects.filter(slug="cid").update(mentor_id=bob.pk) == 1
    assert [a.rating async for a in Author.objects.order_by("slug")] == [5, 4, 2, 5]


async def test_delete_cascades_and_sets_null(library, pool):  # noqa: ARG001
    total, counts = await Author.objects.filter(slug="ann").delete()
    assert counts["library.Author"] == 1
    assert total == sum(counts.values())
    assert (await Author.objects.get(slug="bob")).mentor_id is None
    ann = await Author.objects.get(slug="dora")
    assert await ann.delete() == (1, {"library.Author": 1})
    assert ann.pk is None
    assert await Author.objects.count() == 2
    assert pool.in_use == 0


async def test_a_query_set_delete_override_refuses(library, pool):  # noqa: ARG001
    await LoanPolicy.objects.create(name="short", max_days=3)
    with pytest.raises(NotSupportedError, match="deleted one at a time"):
        await LoanPolicy.objects.all().delete()
    assert await LoanPolicy.objects.count() == 1


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


async def test_a_models_delete_override_runs_for_an_instance(library, pool):  # noqa: ARG001
    loan = await Loan.objects.get(due=datetime.date(2026, 1, 1))
    fine = await Fine.objects.create(loan_id=loan.pk, amount=Decimal("2.50"))
    # Fine.delete() waives the fine: no row is deleted and the instance keeps its key.
    assert await fine.delete() == (0, {})
    assert fine.pk is not None
    stored = await Fine.objects.get(pk=fine.pk)
    assert (stored.waived, stored.amount) == (True, Decimal("2.50"))
    # A query set delete runs no model's delete().
    assert await Fine.objects.filter(pk=fine.pk).delete() == (1, {"library.Fine": 1})
    gone = Fine.from_row({"id": fine.pk, "loan_id": loan.pk, "amount": Decimal(1)})
    assert await gone.delete() == (0, {})
    assert pool.in_use == 0


async def test_a_restricted_delete_raises_with_complete_instances(library, pool):  # noqa: ARG001
    loan = await Loan.objects.get(due=datetime.date(2026, 1, 1))
    fines = [await Fine.objects.create(loan_id=loan.pk, amount=Decimal(n)) for n in (1, 2)]
    for delete in (loan.delete, Loan.objects.filter(pk=loan.pk).delete):
        with pytest.raises(RestrictedError) as refused:
            await delete()
        objects = refused.value.restricted_objects
        assert all(type(obj) is Fine for obj in objects)
        assert sorted((obj.pk, obj.amount, obj.waived) for obj in objects) == [(f.pk, f.amount, False) for f in fines]
        assert loan.pk is not None
    # The refusal leaves the transaction usable, as in Django.
    async with adorm.atomic():
        with pytest.raises(RestrictedError):
            await loan.delete()
        await Tag.objects.create(name="after the refusal")
    assert await tag_names() == ["after the refusal", "rare"]
    assert await Loan.objects.filter(pk=loan.pk).exists()
    assert pool.in_use == 0


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


@pytest.mark.parametrize("attempt", REPEAT)
async def test_concurrent_get_or_create_of_one_row_creates_it_once(library, pool, database, attempt):  # noqa: ARG001
    created_first, release = asyncio.Event(), asyncio.Event()

    async def first():
        async with adorm.atomic():
            result = await Tag.objects.get_or_create(name="new")
            created_first.set()
            await release.wait()
        return result

    async def second():
        await created_first.wait()
        async with adorm.atomic():
            return await Tag.objects.get_or_create(name="new")

    tasks = asyncio.create_task(first()), asyncio.create_task(second())
    await database.waiting()  # the second insert waits for the first transaction's unique key
    release.set()
    (tag, created), (again, created_again) = await asyncio.gather(*tasks)
    assert (created, created_again, again.pk) == (True, False, tag.pk)
    assert await Tag.objects.filter(name="new").count() == 1
    assert pool.in_use == 0


# Savepoints and on_commit


async def test_savepoints_and_djangos_on_commit_callbacks(library, executor, receiver):  # noqa: ARG001
    pool = executor(max_connections=1, acquire_timeout=5)  # the next operation runs after Django's callbacks
    calls = []

    def saved(sender, instance, created, **kwargs):  # noqa: ARG001
        name = instance.name
        transaction.on_commit(lambda: calls.append(("django", name, threading.current_thread().name)))

    receiver(saved, dj.Tag)
    async with adorm.atomic() as tx:
        await Tag.objects.create(name="kept")

        async def dropped():
            async with adorm.atomic():
                await Tag.objects.create(name="dropped")
                async with adorm.atomic():
                    await Tag.objects.create(name="dropped too")
                raise ZeroDivisionError

        with pytest.raises(ZeroDivisionError):
            await dropped()
        async with adorm.atomic():
            await Tag.objects.create(name="inner")
        tx.on_commit(lambda: calls.append(("adorm", None, threading.current_thread().name)))
        assert calls == []
        assert await tag_names() == ["inner", "kept", "rare"]
    assert await tag_names() == ["inner", "kept", "rare"]
    django_calls = [call for call in calls if call[0] == "django"]
    assert [call[1] for call in django_calls] == ["kept", "inner"]
    assert all(call[2].startswith("adorm-django-") for call in django_calls)
    assert [call for call in calls if call[0] == "adorm"] == [("adorm", None, threading.current_thread().name)]
    assert pool.in_use == 0


async def test_a_rolled_back_transaction_runs_no_on_commit_callback(library, pool, receiver):  # noqa: ARG001
    calls = []
    receiver(lambda instance, **kwargs: transaction.on_commit(lambda: calls.append(instance.name)), dj.Tag)  # noqa: ARG005

    async def gone():
        async with adorm.atomic() as tx:
            await Tag.objects.create(name="gone")
            tx.on_commit(lambda: calls.append("adorm"))
            raise KeyError

    with pytest.raises(KeyError):
        await gone()
    await Tag.objects.create(name="autocommit")  # outside a transaction Django runs the callback at once
    assert calls == ["autocommit"]
    assert await tag_names() == ["autocommit", "rare"]


async def test_a_failing_django_callback_does_not_undo_the_commit(library, executor, receiver, caplog):  # noqa: ARG001
    executor(max_connections=1, acquire_timeout=5)  # the next operation runs after Django's callbacks
    calls = []

    def saved(instance, **kwargs):  # noqa: ARG001
        if instance.name == "robust":
            transaction.on_commit(lambda: 1 / 0, robust=True)
        elif instance.name == "fragile":
            transaction.on_commit(lambda: [][0])
        transaction.on_commit(lambda: calls.append(instance.name))

    receiver(saved, dj.Tag)
    with caplog.at_level(logging.ERROR, logger="adorm.drivers.django"):
        async with adorm.atomic() as tx:
            await Tag.objects.create(name="robust")
            await Tag.objects.create(name="fragile")
            tx.on_commit(lambda: calls.append("adorm"))
        assert await tag_names() == ["fragile", "rare", "robust"]
    # The robust failure is logged and the next callback runs; the other failure stops the remaining ones.
    assert sorted(calls) == ["adorm", "robust"]
    failures = [record for record in caplog.records if "on_commit callback" in record.getMessage()]
    assert [record.exc_info[0] for record in failures] == [ZeroDivisionError, IndexError]


@pytest.mark.parametrize("attempt", REPEAT)
async def test_cancelling_the_flow_while_a_django_callback_runs_keeps_the_commit(
    library,  # noqa: ARG001
    executor,
    receiver,
    attempt,  # noqa: ARG001
):
    pool = executor(max_connections=1, acquire_timeout=5)
    loop = asyncio.get_running_loop()
    running, asked, release = asyncio.Event(), asyncio.Event(), threading.Event()
    calls = []

    def callback():
        loop.call_soon_threadsafe(running.set)
        assert release.wait(10)
        # The callback's own statement runs to the end.
        dj.Tag.objects.create(name="from the callback")
        calls.append("django")

    receiver(lambda instance, **kwargs: instance.name == "kept" and transaction.on_commit(callback), dj.Tag)  # noqa: ARG005

    async def flow():
        async with adorm.atomic() as tx:
            await Tag.objects.create(name="kept")
            tx.on_commit(lambda: calls.append("adorm"))
        asked.set()
        # Runs on the slot's thread after the callback, so it is still waiting when it is cancelled.
        return await tag_names()

    task = asyncio.create_task(flow())
    await asyncio.wait_for(running.wait(), 10)
    await asyncio.wait_for(asked.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ["adorm"]
    release.set()
    assert await tag_names() == ["from the callback", "kept", "rare"]
    assert calls == ["adorm", "django"]
    assert pool.in_use == 0


async def test_a_commit_the_database_refuses_runs_no_callbacks(library, pool, receiver, database):  # noqa: ARG001
    calls = []
    receiver(lambda instance, **kwargs: transaction.on_commit(lambda: calls.append("django")), dj.Tag)  # noqa: ARG005

    async def orphan():
        async with adorm.atomic() as tx:
            await Tag.objects.create(name="orphan")
            # Django's foreign keys are deferred: the missing tag fails at COMMIT, not at the insert.
            await Tagging.objects.create(book_id=DUNE, tag_id=999_999, weight=1)
            tx.on_commit(lambda: calls.append("adorm"))

    for _ in REPEAT:
        with pytest.raises(IntegrityError) as refused:
            await orphan()
        assert refused.value.sqlstate == "23503"
        assert calls == []
        await database.transactions_ended()
        assert pool.in_use == 0
        assert await tag_names() == ["rare"]


async def test_an_error_the_driver_hid_from_the_core_still_stops_the_commit(library):  # noqa: ARG001
    driver = DjangoDriver()
    connection = await driver.open_connection(ConnectionProfile("default", driver), lambda _event: None)
    try:
        await connection.begin()
        await connection.execute(Create("library.Tag", {"name": "lost"}))
        # A multi-table insert runs in one of Django's own atomic blocks; its failure marks Django's transaction.
        duplicate = Save("library.Edition", None, {"isbn": DUNE, "title": "Dup", "pages": 1, "released": "2020-01-01"})
        with pytest.raises(IntegrityError):
            await connection.execute(duplicate)
        with pytest.raises(TransactionManagementError, match="failed earlier"):
            await connection.commit()
        await connection.begin()
        await connection.execute(Create("library.Tag", {"name": "next"}))
        await connection.commit()
        rows = await connection.execute(Select(Query("library.Tag").order_by("name").values_list("name", flat=True)))
        assert [row["name"] for row in rows] == ["next", "rare"]
        assert connection.is_usable()
    finally:
        await connection.close()


async def test_savepoints_follow_the_slots_calls(library):  # noqa: ARG001
    driver = DjangoDriver()
    connection = await driver.open_connection(ConnectionProfile("default", driver), lambda _event: None)
    names = Select(Query("library.Tag").order_by("name").values_list("name", flat=True))
    try:
        with pytest.raises(TransactionManagementError, match="no transaction"):
            await connection.savepoint("s1")
        await connection.begin()
        with pytest.raises(TransactionManagementError, match="already open"):
            await connection.begin()
        await connection.savepoint("s1")
        await connection.execute(Create("library.Tag", {"name": "a1"}))
        await connection.savepoint("s2")
        await connection.execute(Create("library.Tag", {"name": "a2"}))
        with pytest.raises(TransactionManagementError, match="already open"):
            await connection.savepoint("s2")
        # Rolling back to s1 discards s2; s1 stays open until it is released.
        await connection.rollback_to_savepoint("s1")
        with pytest.raises(TransactionManagementError, match="no savepoint 's2'"):
            await connection.release_savepoint("s2")
        await connection.execute(Create("library.Tag", {"name": "b1"}))
        await connection.release_savepoint("s1")
        await connection.savepoint("s3")
        await connection.savepoint("s4")
        await connection.release_savepoint("s3")  # releases s4 with it
        with pytest.raises(TransactionManagementError, match="no savepoint 's4'"):
            await connection.rollback_to_savepoint("s4")
        await connection.commit()
        assert [row["name"] for row in await connection.execute(names)] == ["b1", "rare"]
        assert connection.db.savepoint_ids == []
        assert not connection.db.in_atomic_block
    finally:
        await connection.close()


# Signals and the thread


async def test_signals_fire_on_the_slots_thread_in_the_callers_context(library, pool, receiver, monkeypatch):  # noqa: ARG001
    seen = []
    emitted = []
    monkeypatch.setattr(DjangoConnection, "emit", lambda _self, event: emitted.append(event))
    receiver(lambda instance, created, **kwargs: seen.append((instance.name, created, REQUEST.get())), dj.Tag)  # noqa: ARG005
    threads = set()
    receiver(lambda **kwargs: threads.add(threading.current_thread()), dj.Tag)  # noqa: ARG005

    async def request(number):
        REQUEST.set(f"request-{number}")
        tag = await Tag.objects.create(name=f"tag{number}")
        tag.name = f"tag{number}!"
        await tag.save()

    for _ in REPEAT:
        seen.clear()
        await Tag.objects.exclude(name="rare").delete()
        await asyncio.gather(*(request(n) for n in range(12)))
        assert sorted(seen) == sorted(
            [(f"tag{n}", True, f"request-{n}") for n in range(12)]
            + [(f"tag{n}!", False, f"request-{n}") for n in range(12)]
        )
    assert len(threads) <= 2
    assert all(thread.name.startswith("adorm-django-") for thread in threads)
    assert emitted == []  # the client sees no event of a signal
    assert pool.size <= 2


# Failures


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
    assert pool.size == 1


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


async def test_an_abandoned_transaction_is_rolled_back_by_the_slot(library, executor, database):  # noqa: ARG001
    pool = executor(max_connections=1, acquire_timeout=5, commit_timeout=0.2)

    async def abandoned():
        async with adorm.atomic():
            await Tag.objects.create(name="abandoned")
            await database.transactions_ended()  # the slot gives up waiting for the next request

    with pytest.raises(TransactionClosed, match="rolled back the transaction"):
        await abandoned()
    # The slot leaves the pool in the background; the next operation waits for it and gets a new one.
    assert await tag_names() == ["rare"]
    assert (pool.size, pool.in_use) == (1, 0)


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


async def test_many_flows_write_concurrently_on_a_small_pool(library, executor):  # noqa: ARG001
    pool = executor(max_connections=3, acquire_timeout=10)

    async def flow(number):
        async with adorm.atomic():
            tag = await Tag.objects.create(name=f"c{number}")
            async with adorm.atomic():
                await Author.objects.filter(slug="ann").update(rating=F("rating") + 0)
                tag.name = f"c{number}!"
                await tag.save(update_fields=["name"])
        return tag.pk

    for _ in REPEAT:
        await Tag.objects.exclude(name="rare").delete()
        pks = await asyncio.gather(*(flow(n) for n in range(30)))
        assert len(set(pks)) == 30
        assert await tag_names() == sorted(["rare", *(f"c{n}!" for n in range(30))])
    assert pool.size <= 3
    assert pool.in_use == 0


async def test_closing_a_direct_connection_inside_a_transaction_resets_djangos_state(library):  # noqa: ARG001
    class DirectDjangoDriver(DjangoDriver):
        direct = True

    driver = DirectDjangoDriver()
    profile = ConnectionProfile("default", driver)
    connection = await driver.open_connection(profile, lambda _event: None)
    await connection.begin()
    await connection.execute(Create("library.Tag", {"name": "unfinished"}))
    await connection.close()
    wrapper = connections["default"]
    assert (wrapper.in_atomic_block, wrapper.savepoint_ids, wrapper.atomic_blocks) == (False, [], [])
    second = await driver.open_connection(profile, lambda _event: None)
    try:
        await second.begin()
        await second.execute(Create("library.Tag", {"name": "finished"}))
        await second.commit()
    finally:
        await second.close()
    assert sorted(await in_thread(lambda: list(dj.Tag.objects.values_list("name", flat=True)))) == ["finished", "rare"]


async def test_a_direct_transaction_refuses_to_start_inside_djangos_atomic_block(library):  # noqa: ARG001
    class DirectDjangoDriver(DjangoDriver):
        direct = True

    driver = DirectDjangoDriver()
    connection = await driver.open_connection(ConnectionProfile("default", driver), lambda _event: None)
    wrapper = connection.target
    try:
        wrapper.in_atomic_block = True  # the thread's own code is inside transaction.atomic()
        with pytest.raises(TransactionManagementError, match="inside an atomic block"):
            await connection.begin()
        wrapper.in_atomic_block = False
        await connection.begin()
        await connection.execute(Create("library.Tag", {"name": "direct"}))
        await connection.commit()
    finally:
        wrapper.in_atomic_block = False
        await connection.close()
    assert sorted(await in_thread(lambda: list(dj.Tag.objects.values_list("name", flat=True)))) == ["direct", "rare"]


def test_capabilities_of_writes():
    from adorm.drivers.django import CAPABILITIES, READ_CAPABILITIES

    assert {"create", "save", "update", "delete", "delete_instance", "get_or_create", "update_or_create"} <= (
        CAPABILITIES.operations
    )
    assert {"clause.select_for_update.of", "expression.CombinedExpression"} <= CAPABILITIES.constructs
    assert "expression.CombinedExpression" in READ_CAPABILITIES.constructs
    assert "clause.select_for_update" not in READ_CAPABILITIES.constructs
    assert CAPABILITIES.savepoints
