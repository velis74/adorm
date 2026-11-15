"""`IntegrityError`'s description of a violation, read from the errors of psycopg (through Django) and asyncpg for
every kind of violation PostgreSQL reports, also when the error arrives at ``COMMIT``. Both libraries must give the
same values. Through the drivers, the model and its adorm field names of the key's columns follow renamed fields and
``db_column``."""

import datetime

from dataclasses import dataclass

import asyncpg
import pytest

from django.db import connection, IntegrityError as DjangoIntegrityError, transaction

from adorm import ConnectionProfile
from adorm.drivers.django.driver import map_django_error
from adorm.exceptions import IntegrityError
from adorm.executor import Pool, Router, set_runner
from tests.djangoproject.library import models as dj
from tests.generated.library import Reader, Reservation, Tag, Tagging
from tests.library_data import DUNE, in_thread

SETUP = [
    'CREATE TEMP TABLE parent (id int PRIMARY KEY, "Code" text, name text NOT NULL, qty int CHECK (qty > 0),'
    ' UNIQUE ("Code", name))',
    "CREATE TEMP TABLE child (id int PRIMARY KEY, parent_id int REFERENCES parent (id))",
    "CREATE TEMP TABLE late (id int PRIMARY KEY, parent_id int REFERENCES parent (id) DEFERRABLE INITIALLY DEFERRED)",
    "CREATE TEMP TABLE alias (id int PRIMARY KEY, label text)",
    "CREATE TEMP TABLE place (id int PRIMARY KEY, name text UNIQUE)",
    "CREATE UNIQUE INDEX alias_label ON alias (lower(label), length(label))",
    "CREATE TEMP TABLE booking (id int PRIMARY KEY, during int4range, EXCLUDE USING gist (during WITH &&))",
    "INSERT INTO parent VALUES (1, 'A', 'ana', 1), (2, 'A, B', 'x', 1)",
    "INSERT INTO child VALUES (1, 1)",
    "INSERT INTO alias VALUES (1, 'Ana')",
    "INSERT INTO place VALUES (1, 'Koper, Slovenia')",
    "INSERT INTO booking VALUES (1, '[1,5)')",
]


@dataclass(frozen=True)
class Case:
    statement: str
    sqlstate: str
    constraint_name: str | None
    table_name: str
    columns: tuple[str, ...]
    values: tuple[str, ...]
    detail: str
    at_commit: bool = False


CASES = {
    "primary key": Case(
        "INSERT INTO parent VALUES (1, 'B', 'bob', 1)",
        "23505",
        "parent_pkey",
        "parent",
        ("id",),
        ("1",),
        "Key (id)=(1) already exists.",
    ),
    "unique, quoted column": Case(
        "INSERT INTO parent VALUES (3, 'A', 'ana', 1)",
        "23505",
        "parent_Code_name_key",
        "parent",
        ("Code", "name"),
        ("A", "ana"),
        'Key ("Code", name)=(A, ana) already exists.',
    ),
    "unique, a value with a comma": Case(
        "INSERT INTO parent VALUES (3, 'A, B', 'x', 1)",
        "23505",
        "parent_Code_name_key",
        "parent",
        ("Code", "name"),
        (),
        'Key ("Code", name)=(A, B, x) already exists.',
    ),
    "unique, one column, a value with a comma": Case(
        "INSERT INTO place VALUES (2, 'Koper, Slovenia')",
        "23505",
        "place_name_key",
        "place",
        ("name",),
        ("Koper, Slovenia",),
        "Key (name)=(Koper, Slovenia) already exists.",
    ),
    "unique expression index": Case(
        "INSERT INTO alias VALUES (2, 'ANA')",
        "23505",
        "alias_label",
        "alias",
        ("lower(label)", "length(label)"),
        ("ana", "3"),
        "Key (lower(label), length(label))=(ana, 3) already exists.",
    ),
    "not null": Case(
        "INSERT INTO parent VALUES (4, 'C', NULL, 1)",
        "23502",
        None,
        "parent",
        ("name",),
        (),
        "Failing row contains (4, C, null, 1).",
    ),
    "check": Case(
        "INSERT INTO parent VALUES (5, 'D', 'dan', 0)",
        "23514",
        "parent_qty_check",
        "parent",
        (),
        (),
        "Failing row contains (5, D, dan, 0).",
    ),
    "foreign key, missing parent": Case(
        "INSERT INTO child VALUES (2, 9)",
        "23503",
        "child_parent_id_fkey",
        "child",
        ("parent_id",),
        ("9",),
        'Key (parent_id)=(9) is not present in table "parent".',
    ),
    "foreign key, referenced parent": Case(
        "DELETE FROM parent WHERE id = 1",
        "23503",
        "child_parent_id_fkey",
        "child",
        ("id",),
        ("1",),
        'Key (id)=(1) is still referenced from table "child".',
    ),
    "exclusion": Case(
        "INSERT INTO booking VALUES (2, '[3,8)')",
        "23P01",
        "booking_during_excl",
        "booking",
        ("during",),
        ("[3,8)",),
        "Key (during)=([3,8)) conflicts with existing key (during)=([1,5)).",
    ),
    "deferred foreign key at commit": Case(
        "INSERT INTO late VALUES (1, 9)",
        "23503",
        "late_parent_id_fkey",
        "late",
        ("parent_id",),
        ("9",),
        'Key (parent_id)=(9) is not present in table "parent".',
        at_commit=True,
    ),
}


def check(wrapped, case, original):
    assert type(wrapped) is IntegrityError
    assert wrapped.original is original
    assert wrapped.sqlstate == case.sqlstate
    assert wrapped.constraint_name == case.constraint_name
    assert wrapped.table_name == case.table_name
    assert wrapped.schema_name.startswith("pg_temp")
    assert wrapped.columns == case.columns
    assert wrapped.values == case.values
    assert wrapped.detail == case.detail
    assert wrapped.key == (dict(zip(case.columns, case.values, strict=True)) if case.values else {})
    assert (wrapped.model, wrapped.fields) == (None, ()), "no model has a temporary table"


async def run_asyncpg(conn, statement):
    async with conn.transaction():
        await conn.execute(statement)


def run_django(case):
    # The test runs in Django's transaction, so the end of this block is a savepoint's, not a commit:
    # SET CONSTRAINTS runs the deferred check instead.
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(case.statement)
        if case.at_commit:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


@pytest.mark.parametrize("name", CASES)
async def test_asyncpg(test_database_url, name):
    case = CASES[name]
    conn = await asyncpg.connect(test_database_url)
    try:
        for statement in SETUP:
            await conn.execute(statement)
        with pytest.raises(asyncpg.IntegrityConstraintViolationError) as info:
            await run_asyncpg(conn, case.statement)
        check(map_django_error(info.value), case, info.value)
    finally:
        await conn.close()


@pytest.mark.django_db
@pytest.mark.parametrize("name", CASES)
def test_django(name):
    case = CASES[name]
    with connection.cursor() as cursor:
        for statement in SETUP:
            cursor.execute(statement)
    with pytest.raises(DjangoIntegrityError) as info:
        run_django(case)
    check(map_django_error(info.value), case, info.value)


@pytest.mark.django_db(transaction=True)
def test_django_deferred_foreign_key_at_commit():
    case = CASES["deferred foreign key at commit"]
    with connection.cursor() as cursor:
        for statement in SETUP:
            cursor.execute(statement)
    try:
        with pytest.raises(DjangoIntegrityError) as info, transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(case.statement)
        check(map_django_error(info.value), case, info.value)
    finally:
        connection.close()  # drops the temporary tables


# Through the drivers, on models whose field names differ from the column names


@pytest.fixture(params=["django", "asyncpg"])
async def pool(request):
    if request.param == "django":
        from adorm.drivers.django import DjangoDriver

        driver = DjangoDriver()
    else:
        from adorm.drivers.asyncpg import AsyncpgDriver

        driver = AsyncpgDriver()
    pool = Pool(driver, ConnectionProfile("default", driver), max_connections=2)
    router = Router([pool])
    previous = set_runner(router)
    try:
        yield pool
    finally:
        set_runner(previous)
        await router.close()


@pytest.mark.django_db(transaction=True)
async def test_a_foreign_key_with_db_column_names_its_field(library, pool):  # noqa: ARG001
    rare = await Tag.objects.get(name="rare")
    with pytest.raises(IntegrityError) as raised:
        await Tagging.objects.create(book_id="9780000000999", tag_id=rare.pk)
    exc = raised.value
    assert (exc.sqlstate, exc.table_name, exc.model) == ("23503", "library_tagging", "library.Tagging")
    assert (exc.columns, exc.fields) == (("book_isbn",), ("book_id",))
    assert exc.key == {"book_isbn": "9780000000999"}
    assert pool.in_use == 0


@pytest.mark.django_db(transaction=True)
async def test_a_renamed_field_is_named_by_its_adorm_name(library, pool):
    def reserve(copy):
        return dj.Reservation.objects.create(
            reader_id=library["reader"],
            book_id=DUNE,
            copy=copy,
            starts=datetime.date(2026, 3, 1),
            ends=datetime.date(2026, 3, 2),
        ).pk

    await in_thread(lambda: reserve(1))
    second = await in_thread(lambda: reserve(2))
    with pytest.raises(IntegrityError) as raised:
        await Reservation.objects.filter(pk=second).update(copy_=1)
    exc = raised.value
    assert (exc.constraint_name, exc.model) == ("library_reservation_one_per_copy", "library.Reservation")
    assert (exc.columns, exc.fields) == (("book_id", "copy"), ("book_id", "copy_"))
    assert exc.key == {"book_id": DUNE, "copy": "1"}
    assert pool.in_use == 0


@pytest.mark.django_db(transaction=True)
async def test_the_key_of_a_referenced_row_names_no_field(library, pool):
    reader = library["reader"]
    with pytest.raises(IntegrityError) as raised:
        await Reader.objects.filter(pk=reader).update(id=reader + 1000)
    exc = raised.value
    assert exc.detail == f'Key (id)=({reader}) is still referenced from table "library_loan".'
    # The key is the reader's, the table the loan's: its own ``id`` column is not the violated key.
    assert (exc.table_name, exc.model, exc.columns, exc.fields) == ("library_loan", "library.Loan", ("id",), ())
    assert pool.in_use == 0
