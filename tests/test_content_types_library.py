"""Content type lookups and generic foreign key assignment on the drivers that translate with Django (Django and
asyncpg), over the library schema: the ids Django assigns, the writes Django reads back, a transaction on one slot,
and the cache cleared by Django's ``post_migrate``."""

import asyncio

import pytest

from django.contrib.contenttypes.models import ContentType as DjangoContentType

import adorm

from adorm import ConnectionProfile, content_types
from adorm.executor import Pool, Router, set_runner
from tests.djangoproject.library import models as dj
from tests.generated.library import Book, BookProxy, Edition, Note, Reader
from tests.library_data import DUNE, in_thread

pytestmark = pytest.mark.django_db(transaction=True)

REPEAT = range(3)


@pytest.fixture(params=["django", "asyncpg"])
def driver_class(request):
    if request.param == "django":
        from adorm.drivers.django import DjangoDriver

        return DjangoDriver
    from adorm.drivers.asyncpg import AsyncpgDriver

    return AsyncpgDriver


@pytest.fixture
async def pool(driver_class):
    driver = driver_class()
    pool = Pool(driver, ConnectionProfile("default", driver), max_connections=1, acquire_timeout=2.0)
    router = Router([pool])
    previous = set_runner(router)
    try:
        yield pool
    finally:
        set_runner(previous)
        await router.close()


def django_ids():
    get = DjangoContentType.objects.get_for_model
    return {
        "book": get(dj.Book).id,
        "proxy": get(dj.BookProxy, for_concrete_model=False).id,
        "edition": get(dj.Edition).id,
        "reader": get(dj.Reader).id,
    }


async def test_ids_are_django_s(library, pool):  # noqa: ARG001
    ids = await in_thread(django_ids)
    assert await content_types.get_id(Book) == ids["book"]
    assert await content_types.get_id(BookProxy) == ids["book"], "a proxy uses its concrete model's"
    assert await content_types.get_id(BookProxy, for_concrete_model=False) == ids["proxy"]
    assert await content_types.get_id(Edition) == ids["edition"]
    content_types.clear()
    assert await content_types.get_model(ids["reader"]) is Reader
    assert content_types.cached_id(Reader) == ids["reader"]
    assert set(await content_types.natural_keys([ids["book"], ids["edition"], ids["proxy"]])) == {
        ids["book"],
        ids["edition"],
        ids["proxy"],
    }


async def test_assigned_instances_are_stored_as_django_reads_them(library, pool):  # noqa: ARG001
    reader = await Reader.objects.get(name="Rea")
    note = await Note.objects.create(item=reader, json="{}")
    assert note.object_id == str(reader.pk)
    book = await Book.objects.get(isbn=DUNE)
    note.item = book
    await note.save()

    def stored():
        return dj.Note.objects.get(code=note.code).item

    assert await in_thread(stored) == await in_thread(lambda: dj.Book.objects.get(isbn=DUNE))
    again = await Note.objects.get(code=note.code)
    assert await again.item == book


@pytest.mark.parametrize("attempt", REPEAT)
async def test_a_transaction_on_the_only_slot_resolves_on_that_slot(library, pool, attempt):  # noqa: ARG001
    async with adorm.atomic():
        reader = await Reader.objects.get(name="Rea")
        async with asyncio.TaskGroup() as group:
            book_type = group.create_task(content_types.get_id(Book))
            note = group.create_task(Note.objects.create(item=reader, json="{}"))
        assert pool.in_use == 1
    stored = await Note.objects.get(code=note.result().code)
    assert await stored.item == reader
    assert book_type.result() == await in_thread(lambda: DjangoContentType.objects.get_for_model(dj.Book).id)
    assert pool.in_use == 0


async def test_django_post_migrate_clears_the_cache(library, pool):  # noqa: ARG001
    from django.core.management.sql import emit_post_migrate_signal

    await content_types.get_id(Book)
    assert content_types.cached_id(Book) is not None
    await in_thread(lambda: emit_post_migrate_signal(0, False, "default"))
    assert content_types.cached_id(Book) is None


def test_database_key_names_the_profile_s_postgresql_database(driver_class):
    from django.db import connections

    driver = driver_class()
    entry = connections.settings["default"]
    plain = driver.database_key(ConnectionProfile("default", driver))
    assert plain == ("postgresql", str(entry.get("HOST") or ""), str(entry.get("PORT") or ""), entry["NAME"])
    hooked = ConnectionProfile("user_7", driver, on_acquire=lambda *args: None)  # noqa: ARG005 - hook signature
    assert driver.database_key(hooked) == plain, "hooks do not change the database"
    assert driver.database_key(ConnectionProfile("other", driver, params={"NAME": "elsewhere"})) != plain
    assert driver.database_key(ConnectionProfile("own", driver, connect=lambda profile: None)) is None  # noqa: ARG005
    assert driver.database_key(ConnectionProfile("missing", driver, alias="nowhere")) is None


async def test_profiles_of_one_database_share_cached_ids(library, driver_class):  # noqa: ARG001
    driver = driver_class()
    profiles = {name: ConnectionProfile(name, driver) for name in ("default", "user_7")}
    adorm.configure(profiles=profiles, pool={"max_connections": 1, "acquire_timeout": 2.0})
    try:
        book_type = await content_types.get_id(Book)
        assert content_types.cached_id(Book, session=adorm.create_session("user_7")) == book_type
    finally:
        from adorm.settings import close_pools

        await close_pools()
        adorm.configure()
