"""Probes for SQLAlchemy Core as the native PostgreSQL driver's alternative SQL builder: its asyncpg dialect compiles
without an engine or a connection, numbers placeholders and casts every parameter itself."""

import asyncio
import datetime

import asyncpg
import pytest

from sqlalchemy import Boolean, Column, DateTime, Integer, literal, MetaData, select, String, Table, update
from sqlalchemy.dialects.postgresql import asyncpg as asyncpg_dialect, JSONB

from tests.djangoproject.library.models import Book

METADATA = MetaData()
BOOK = Table(
    "library_book",
    METADATA,
    Column("isbn", String(13), primary_key=True),
    Column("title", String(200)),
    Column("pages", Integer),
    Column("published", DateTime(timezone=True)),
    Column("extra", JSONB),
    Column("in_print", Boolean),
)
DIALECT = asyncpg_dialect.dialect()
PUBLISHED = datetime.datetime(2024, 3, 1, 12, 30, tzinfo=datetime.UTC)


def asyncpg_statement(statement):
    """SQL and positional parameters for raw asyncpg, with each parameter's bind processor applied, as SQLAlchemy
    applies them when it executes the statement itself."""
    compiled = statement.compile(dialect=DIALECT)
    params = []
    for name in compiled.positiontup:
        value = compiled.params[name]
        processor = compiled.binds[name].type.dialect_impl(DIALECT).bind_processor(DIALECT)
        params.append(processor(value) if processor else value)
    return str(compiled), params


def test_compiles_numbered_placeholders_with_casts():
    statement = (
        select(BOOK.c.isbn, literal(1).label("one"))
        .where(BOOK.c.pages >= 100, BOOK.c.extra.contains({"a": 1}), BOOK.c.published > PUBLISHED)
        .limit(5)
    )
    sql, params = asyncpg_statement(statement)
    assert DIALECT.paramstyle == "numeric_dollar"
    assert "$1::INTEGER AS one" in sql
    assert "library_book.pages >= $2::INTEGER" in sql
    assert "library_book.extra @> $3::JSONB" in sql
    assert "library_book.published > $4::TIMESTAMP WITH TIME ZONE" in sql
    assert params == [1, 100, '{"a": 1}', PUBLISHED, 5]


def test_compiling_needs_no_engine():
    sql, _ = asyncpg_statement(update(BOOK).where(BOOK.c.isbn == "x").values(pages=BOOK.c.pages + 1))
    assert sql == (
        "UPDATE library_book SET pages=(library_book.pages + $1::INTEGER) WHERE library_book.isbn = $2::VARCHAR"
    )


@pytest.mark.django_db(transaction=True)
def test_statement_runs_on_raw_asyncpg(test_database_url):
    Book.objects.create(isbn="9780000000001", title="War", pages=1225, published=PUBLISHED, extra={"a": 1})
    Book.objects.create(isbn="9780000000002", title="Poems", pages=120, published=PUBLISHED, extra={})
    statement = select(BOOK.c.isbn, literal(True).label("flag")).where(
        BOOK.c.pages >= 100, BOOK.c.extra.contains({"a": 1}), BOOK.c.published >= PUBLISHED
    )
    sql, params = asyncpg_statement(statement)

    async def run():
        conn = await asyncpg.connect(test_database_url)
        try:
            return [tuple(r) for r in await conn.fetch(sql, *params)]
        finally:
            await conn.close()

    assert asyncio.run(run()) == [("9780000000001", True)]
