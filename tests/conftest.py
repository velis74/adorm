import asyncio
import datetime
import os

from decimal import Decimal
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest


@pytest.fixture(autouse=True)
def _forget_content_types():
    """Start every test with an empty content type cache, so the operations a test records do not depend on the
    tests before it."""
    from adorm.models.contenttypes import content_types

    content_types.clear()
    yield
    content_types.clear()


@pytest.fixture(scope="session")
def database_url() -> str:
    """URL of the PostgreSQL server the suite runs against."""
    from tests.djangoproject.settings import DEFAULT_DATABASE_URL

    return os.environ.get("ADORM_TEST_DATABASE_URL", DEFAULT_DATABASE_URL)


def pytest_collection_modifyitems(items):
    # pytest-django creates the test database only for tests marked django_db.
    for item in items:
        if "test_database_url" in getattr(item, "fixturenames", ()) and not item.get_closest_marker("django_db"):
            item.add_marker(pytest.mark.django_db(transaction=True))


@pytest.fixture(scope="session")
def test_database_url(database_url, django_db_setup) -> str:  # noqa: ARG001 - needs the test database created
    """``database_url`` pointing at the test database pytest-django created."""
    from django.db import connection

    from tests.djangoproject.settings import TEST_DATABASE_NAME

    name = connection.settings_dict["NAME"]
    if not name.startswith(TEST_DATABASE_NAME):
        pytest.fail(f"the test database was not created; Django points at {name!r}")
    parts = urlsplit(database_url)
    return urlunsplit(parts._replace(path="/" + quote(name)))


@pytest.fixture
def library():
    """Authors with mentors, books with an edition (multi-table inheritance), tags, a note and loans."""
    from django.contrib.contenttypes.models import ContentType as DjangoContentType

    from tests.djangoproject.library import models as dj
    from tests.library_data import DUNE, DUNE_2, NOTE, PHYSICS, POEMS

    ann = dj.Author.objects.create(name="Ann", slug="ann", rating=5, born=datetime.date(1970, 1, 1))
    bob = dj.Author.objects.create(name="Bob", slug="bob", rating=3, born=datetime.date(1980, 1, 1), mentor=ann)
    cid = dj.Author.objects.create(name="Cid", slug="cid", rating=1, mentor=bob)
    dora = dj.Author.objects.create(name="dora", slug="dora", rating=4, born=datetime.date(1990, 1, 1))
    dune = dj.Book.objects.create(
        isbn=DUNE, title="Dune", pages=412, price=Decimal("9.99"), keywords=["sand", "spice"], extra={"series": "dune"}
    )
    poems = dj.Book.objects.create(isbn=POEMS, title="Poems", genre="poetry", pages=80, schema="v2")
    physics = dj.Book.objects.create(isbn=PHYSICS, title="Physics", genre="science", pages=900, in_print=False)
    dj.Edition.objects.create(isbn=DUNE_2, title="Dune II", pages=500, released=datetime.date(2020, 5, 1), number=2)
    dune.authors.add(ann)
    poems.authors.add(bob)
    physics.authors.add(ann, cid)
    rare = dj.Tag.objects.create(name="rare")
    dj.Tagging.objects.create(book=dune, tag=rare, weight=3)
    dj.Tagging.objects.create(book=poems, tag=rare, weight=1)
    dj.Note.objects.create(
        code=NOTE,
        content_type=DjangoContentType.objects.get_for_model(dj.Book),
        object_id=DUNE,
        json="{}",
        copy=dune,
        schema="v3",
    )
    reader = dj.Reader.objects.create(name="Rea")
    dj.Loan.objects.create(reader=reader, book=dune, due=datetime.date(2026, 1, 1))
    dj.Loan.objects.create(reader=reader, book=poems, due=datetime.date(2026, 2, 1), archived=True)
    return {"ann": ann.pk, "bob": bob.pk, "cid": cid.pk, "dora": dora.pk, "reader": reader.pk}


@pytest.fixture
async def locker(test_database_url):
    """``await lock()`` holds an exclusive lock on the authors' table, so reads of it wait until ``await unlock()``;
    ``await waiting()`` returns the pid of the backend waiting for it, once there is one. The lock and the watching
    use two connections of their own."""
    from tests.library_data import WAITING

    holder = await asyncpg.connect(test_database_url)
    monitor = await asyncpg.connect(test_database_url)
    state = {}

    async def lock():
        state["tx"] = holder.transaction()
        await state["tx"].start()
        await holder.execute("LOCK TABLE library_author IN ACCESS EXCLUSIVE MODE")

    async def unlock():
        await state.pop("tx").rollback()

    async def waiting(present=True):
        async with asyncio.timeout(10):
            while True:
                pid = await monitor.fetchval(WAITING)
                if (pid is not None) is present:
                    return pid
                await asyncio.sleep(0.01)

    try:
        yield lock, unlock, waiting, monitor
    finally:
        if "tx" in state:
            await unlock()
        await holder.close()
        await monitor.close()


def _seed_demo() -> None:
    from django.db import transaction

    from demo.backend.clubs.management.commands.seed_demo import Command

    with transaction.atomic():
        Command().seed()


@pytest.fixture
async def demo_data(test_database_url):
    """The demo's data set (``manage.py seed_demo``) in the test database; returns the database URL."""
    from tests.library_data import in_thread

    await in_thread(_seed_demo)
    return test_database_url


class Database:
    """Two asyncpg connections of the test's own: ``holder`` takes locks, ``monitor`` watches the server."""

    def __init__(self, holder, monitor):
        self.holder = holder
        self.monitor = monitor
        self.transaction = None

    async def hold(self, statement, *args):
        """Run ``statement`` in a transaction of the holder that lasts until `release()`."""
        self.transaction = self.holder.transaction()
        await self.transaction.start()
        await self.holder.execute(statement, *args)

    async def lock_author(self, slug):
        await self.hold("SELECT id FROM library_author WHERE slug = $1 FOR UPDATE", slug)

    async def release(self):
        transaction, self.transaction = self.transaction, None
        if transaction is not None:
            await transaction.rollback()

    async def waiting(self):
        """The pid of a backend that waits for a lock, once there is one."""
        async with asyncio.timeout(10):
            while True:
                pid = await self.monitor.fetchval(
                    "SELECT pid FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND datname = current_database()"
                    " AND pid <> pg_backend_pid()"
                )
                if pid is not None:
                    return pid
                await asyncio.sleep(0.01)

    async def open_transactions(self):
        """How many connections other than the test's own are inside a transaction."""
        own = [self.holder.get_server_pid()]
        return await self.monitor.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND xact_start IS NOT NULL"
            " AND pid <> pg_backend_pid() AND pid <> ALL($1::int[])",
            own,
        )

    async def transactions_ended(self):
        async with asyncio.timeout(5):
            while await self.open_transactions():
                await asyncio.sleep(0.01)


@pytest.fixture
async def database(test_database_url):
    """A `Database` on the test database."""
    holder = await asyncpg.connect(test_database_url)
    monitor = await asyncpg.connect(test_database_url)
    try:
        yield Database(holder, monitor)
    finally:
        await Database(holder, monitor).release()
        await holder.close()
        await monitor.close()
