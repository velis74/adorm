"""Probes for building SQL with Django's compiler without a connection and running it on asyncpg.

Tests without a ``django_db`` mark run with pytest-django's database blocker: any attempt to connect raises
``RuntimeError``, so a test that compiles there proves that compiling opened no connection.
"""

import asyncio
import datetime
import decimal
import ipaddress
import threading
import uuid

import asyncpg
import pytest

from django.contrib.contenttypes.models import ContentType
from django.contrib.postgres.search import SearchHeadline
from django.db import connection, connections, NotSupportedError as DjangoNotSupportedError, transaction
from django.db.backends.signals import connection_created
from django.db.models import (
    AnyValue,
    Case,
    Count,
    Exists,
    F,
    GenericIPAddressField,
    OuterRef,
    Q,
    Sum,
    UUIDField,
    Value,
    When,
)
from django.db.models.expressions import RawSQL
from django.db.models.functions import Coalesce, Concat, JSONObject, Lower, Now, TruncDay
from django.db.models.sql import DeleteQuery, InsertQuery, UpdateQuery
from psycopg import Binary, sql as psycopg_sql
from psycopg.types.json import Json, Jsonb
from psycopg.types.numeric import Float8, Int2, Int4, Int8
from psycopg.types.range import Range

from adorm.drivers.asyncpg import compiler as compiler_module
from adorm.drivers.asyncpg.compiler import bind_param, DjangoCompiler, DjangoStatement, MAX_QUERY_PARAMS
from adorm.drivers.sql import Statement
from adorm.exceptions import NotSupportedError, TransactionManagementError
from tests.djangoproject.library.models import Author, Book, Edition, Note

SERVER_VERSION = 170000
PUBLISHED = datetime.datetime(2024, 3, 1, 12, 30, tzinfo=datetime.UTC)


def queries():
    """Querysets covering the constructs whose SQL or parameters differ between psycopg and asyncpg."""
    return {
        "lookups": Book.objects.filter(title__icontains="war", pages__gte=100).order_by("-published", "isbn")[:5],
        "pattern_with_percent": Book.objects.filter(title__contains="100%_").order_by("isbn"),
        "contains_expression": Book.objects.filter(title__contains=F("genre")).order_by("isbn"),
        "json_key": Book.objects.filter(extra__author__name="Ana").order_by("isbn"),
        "json_contains": Book.objects.filter(extra__contains={"tags": ["x"]}).order_by("isbn"),
        "array_contains": Book.objects.filter(keywords__contains=["war"]).order_by("isbn"),
        "aggregate": Book.objects.values("genre").annotate(n=Count("authors"), p=Sum("pages")).order_by("genre"),
        "value_in_select": Book.objects.annotate(one=Value(1), yes=Value(True)).values("isbn", "one", "yes"),
        "datetime_in_select": Book.objects.annotate(at=Value(PUBLISHED)).values("isbn", "at").order_by("isbn"),
        "exists": Book.objects.filter(Exists(Author.objects.filter(books=OuterRef("pk")))).order_by("isbn"),
        "case": Book.objects.annotate(
            length=Case(When(pages__gt=300, then=Value("long")), default=Value("short"))
        ).values_list("isbn", "length"),
        "concat_coalesce": Book.objects.annotate(
            label=Lower(Concat("title", Value(" / "), "genre")), w=Coalesce("weight", Value(0.0))
        ).values("label", "w"),
        "aware_datetime": Book.objects.filter(published__gte=PUBLISHED).order_by("isbn"),
        "decimal": Book.objects.filter(price__gt=decimal.Decimal("9.99")).order_by("isbn"),
        "interval": Book.objects.filter(reading_time__gt=datetime.timedelta(hours=1)).order_by("isbn"),
        "bytes": Book.objects.filter(cover=b"\x00\x01").order_by("isbn"),
        "uuid": Note.objects.filter(code=uuid.UUID(int=1)),
        "trunc_in_time_zone": Book.objects.annotate(day=TruncDay("published")).values("isbn", "day").order_by("isbn"),
        "now": Book.objects.filter(published__lte=Now()).order_by("isbn"),
        "in_list": Book.objects.filter(pk__in=["9780000000001", "9780000000002"]).order_by("isbn"),
        "or_not": Book.objects.filter(Q(pages__lt=200) | ~Q(genre="fiction")).order_by("isbn"),
        "json_object": Book.objects.annotate(j=JSONObject(t="title", p="pages")).values("isbn", "j").order_by("isbn"),
        "any_value": Book.objects.values("genre").annotate(t=AnyValue("title")).filter(t__isnull=False),
        "int_arithmetic": Book.objects.annotate(x=Value(30000) + Value(30000)).values("isbn", "x").order_by("isbn"),
        "smallint_column_arithmetic": Author.objects.annotate(x=F("rating") + 30000)
        .values("slug", "x")
        .order_by("slug"),
        "float_arithmetic": Book.objects.annotate(x=Value(0.1) + Value(0.2)).values("isbn", "x").order_by("isbn"),
        "raw_int_product": Book.objects.annotate(x=RawSQL("%s * %s", (200, 200))).values("isbn", "x").order_by("isbn"),
        "raw_list": Book.objects.annotate(x=RawSQL("cardinality(%s)", ([30000, 70000],))).values("isbn", "x"),
        "distinct_order_by_expression": Book.objects.values("isbn").order_by(Lower("title")).distinct(),
        "extra_select": Book.objects.extra(select={"double": "pages * 2"}).values_list("isbn").order_by("double"),
        "inet_and_cidr": Book.objects.annotate(
            ip=RawSQL("'10.0.0.1'::inet", (), output_field=GenericIPAddressField()),
            net=RawSQL("'10.0.0.0/8'::cidr", (), output_field=GenericIPAddressField()),
            ips=RawSQL("ARRAY['10.0.0.2'::inet]", ()),
        )
        .values("isbn", "ip", "net", "ips")
        .order_by("isbn"),
        "uuid_select": Note.objects.annotate(
            other=RawSQL("%s::uuid", (str(uuid.UUID(int=2)),), output_field=UUIDField())
        ).values_list("code", "other"),
    }


def in_fresh_thread(fn):
    """Run ``fn`` on a new thread, which has no Django connection yet, and return its result or raise its error."""
    outcome = {}

    def run():
        try:
            outcome["result"] = fn()
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


@pytest.fixture
def compiler():
    return DjangoCompiler(server_version=SERVER_VERSION)


# Compilation without a connection


@pytest.mark.parametrize("name", list(queries()))
def test_django_compiler_builds_sql_without_a_connection(name):
    """Django's own ``get_compiler(using).as_sql()`` connects for nothing but version-dependent constructs."""

    def compile_on_default():
        return queries()[name].query.get_compiler("default").as_sql()

    if name in ("json_object", "any_value"):
        with pytest.raises(RuntimeError, match="Database access not allowed"):
            in_fresh_thread(compile_on_default)
    else:
        sql, params = in_fresh_thread(compile_on_default)
        assert sql.startswith("SELECT")
        assert "$" not in sql


@pytest.mark.parametrize("name", list(queries()))
def test_compiler_builds_numbered_sql_without_a_connection(compiler, name):
    statement = compiler.compile(queries()[name])
    assert isinstance(statement, DjangoStatement)
    assert isinstance(statement, Statement)
    assert "%s" not in statement.sql
    assert statement.sql.count("$") >= len(statement.params)


def test_compiler_leaves_the_thread_connection_alone(compiler):
    created = []

    def receiver(**kwargs):
        created.append(kwargs)

    def run():
        for queryset in queries().values():
            compiler.compile(queryset)
        return connections["default"].connection

    connection_created.connect(receiver)
    try:
        assert in_fresh_thread(run) is None
    finally:
        connection_created.disconnect(receiver)
    assert created == []
    assert compiler.connection.connection is None


async def test_compiler_runs_inside_an_event_loop(compiler):
    """No ``SynchronousOnlyOperation``: compiling is not one of Django's ``async_unsafe`` calls."""
    statements = [compiler.compile(q) for q in queries().values()]
    assert len(statements) == len(queries())


def test_server_version_selects_the_sql():
    queryset = Book.objects.annotate(j=JSONObject(t="title")).values("j")
    assert "JSON_OBJECT(" in DjangoCompiler(server_version=170000).compile(queryset).sql
    assert "JSONB_BUILD_OBJECT(" in DjangoCompiler(server_version=150000).compile(queryset).sql
    any_value = Book.objects.values("genre").annotate(t=AnyValue("title"))
    assert "ANY_VALUE(" in DjangoCompiler(server_version=160000).compile(any_value).sql
    with pytest.raises(DjangoNotSupportedError, match="ANY_VALUE"):
        DjangoCompiler(server_version=150000).compile(any_value)


def test_construct_that_needs_a_connection_raises(compiler):
    with_options = Book.objects.annotate(h=SearchHeadline("title", "war", max_words=5))
    with pytest.raises(NotSupportedError, match="needs a database connection"):
        compiler.compile(with_options)
    assert "ts_headline(" in compiler.compile(Book.objects.annotate(h=SearchHeadline("title", "war"))).sql


def test_parent_field_update_is_not_supported(compiler):
    """Django pre-selects the keys on the thread's own connection and runs one UPDATE per table."""
    update = Edition.objects.filter(number=2).query.chain(UpdateQuery)
    update.add_update_values({"title": "Second", "number": 3})
    assert update.related_updates
    with pytest.raises(NotSupportedError, match="several statements"):
        in_fresh_thread(lambda: compiler.compile(update))
    child_only = Edition.objects.filter(number=2).query.chain(UpdateQuery)
    child_only.add_update_values({"number": 3})
    assert compiler.compile(child_only).sql.startswith('UPDATE "library_edition" SET')


@pytest.mark.parametrize(
    "queryset",
    [
        Book.objects.select_for_update().filter(pages=1),
        Book.objects.select_for_update(nowait=True).filter(pages=1),
        Book.objects.select_for_update(skip_locked=True, of=("self",)).filter(pages=1),
        Book.objects.select_for_update(no_key=True).filter(pages=1),
    ],
)
def test_select_for_update_compiles_inside_a_transaction(compiler, queryset):
    statement = in_fresh_thread(lambda: compiler.compile(queryset, in_transaction=True))
    assert " FOR " in statement.sql
    with pytest.raises(TransactionManagementError, match="outside of a transaction"):
        compiler.compile(queryset)


def test_select_for_update_options_render(compiler):
    def sql(queryset):
        return compiler.compile(queryset, in_transaction=True).sql

    assert sql(Book.objects.select_for_update(nowait=True)).endswith(" FOR UPDATE NOWAIT")
    assert sql(Book.objects.select_for_update(skip_locked=True, of=("self",))).endswith(
        ' FOR UPDATE OF "library_book" SKIP LOCKED'
    )
    assert sql(Book.objects.select_for_update(no_key=True)).endswith(" FOR NO KEY UPDATE")
    subquery = Book.objects.filter(pk__in=Book.objects.select_for_update().values("pk"))
    assert "FOR UPDATE" in compiler.compile(subquery, in_transaction=True).sql


def test_empty_select_compiles_to_no_rows(compiler):
    statement = compiler.compile(Book.objects.filter(pk__in=[]))
    assert not statement.empty
    assert statement.sql.endswith("WHERE 0 = 1")
    assert statement.params == ()


def test_empty_update_and_delete_compile_to_empty_statements(compiler):
    update = Book.objects.filter(pk__in=[]).query.chain(UpdateQuery)
    update.add_update_values({"pages": 1})
    no_values = Book.objects.filter(pages=1).query.chain(UpdateQuery)
    delete = Book.objects.filter(pk__in=[]).query.chain(DeleteQuery)
    for query in (update, no_values, delete):
        statement = compiler.compile(query)
        assert statement.empty
        assert (statement.sql, statement.params) == ("", ())


def test_server_side_binding_option_selects_the_casts(monkeypatch):
    settings_dict = connections.settings["default"]
    server_side = {**settings_dict, "OPTIONS": {**settings_dict.get("OPTIONS", {}), "server_side_binding": True}}
    monkeypatch.setitem(connections.settings, "server_side", server_side)
    queryset = Book.objects.filter(pages__gte=100, weight__lt=1.5)
    assert "$1::int4" in DjangoCompiler(server_version=SERVER_VERSION).compile(queryset).sql
    statement = DjangoCompiler("server_side", server_version=SERVER_VERSION).compile(queryset)
    assert "$1::int4" in statement.sql
    assert "$2::float8" in statement.sql
    raw = Book.objects.annotate(x=RawSQL("%s", (7,))).values("x")
    assert "$1::int2" in DjangoCompiler("server_side", server_version=SERVER_VERSION).compile(raw).sql


def test_psycopg2_backend_is_refused(monkeypatch):
    monkeypatch.setattr(compiler_module, "is_psycopg3", False)
    with pytest.raises(NotSupportedError, match="psycopg2"):
        DjangoCompiler(server_version=SERVER_VERSION)


def test_update_compiles(compiler):
    update = Book.objects.filter(genre="poetry").query.chain(UpdateQuery)
    update.add_update_values({"pages": F("pages") + 1, "in_print": False})
    statement = compiler.compile(update)
    assert statement.sql.startswith('UPDATE "library_book" SET')
    assert statement.params == (1, False, "poetry")
    assert '"pages" + $1::int4' in statement.sql
    assert "$2::bool" in statement.sql


def test_delete_compiles(compiler):
    query = Book.objects.filter(pages__lt=10).query.chain(DeleteQuery)
    statement = compiler.compile(query)
    assert statement.sql == 'DELETE FROM "library_book" WHERE "library_book"."pages" < $1::int4'
    assert statement.params == (10,)


def test_insert_is_not_supported(compiler):
    query = InsertQuery(Author)
    query.insert_values([Author._meta.get_field("name")], [Author(name="Ana")])
    with pytest.raises(NotSupportedError, match="several statements"):
        compiler.compile(query)


def test_parameter_limit(compiler):
    keys = [f"{n:013d}" for n in range(MAX_QUERY_PARAMS + 1)]
    assert len(compiler.compile(Book.objects.filter(pk__in=keys[:-1])).params) == MAX_QUERY_PARAMS
    with pytest.raises(NotSupportedError, match="at most 32767"):
        compiler.compile(Book.objects.filter(pk__in=keys))
    fields = [Author._meta.get_field(name) for name in ("name", "slug", "rating")]
    assert compiler.connection.ops.bulk_batch_size(fields, [None] * 50_000) == MAX_QUERY_PARAMS // 3


def test_non_postgresql_alias_is_refused(monkeypatch):
    lite = {**connections.settings["default"], "ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
    monkeypatch.setitem(connections.settings, "lite", lite)
    with pytest.raises(NotSupportedError, match="sqlite, not PostgreSQL"):
        DjangoCompiler("lite", server_version=SERVER_VERSION)


def test_percent_in_values_stays_in_parameters(compiler):
    statement = compiler.compile(Book.objects.annotate(x=Value("a%s%%")).values("x"))
    assert statement.sql == 'SELECT $1 AS "x" FROM "library_book"'
    assert statement.params == ("a%s%%",)


@pytest.mark.parametrize("repeat", range(5))
def test_one_compiler_shared_by_threads(compiler, repeat):  # noqa: ARG001 - repeated to expose races
    expected = {name: (s.sql, s.params) for name, s in ((n, compiler.compile(q)) for n, q in queries().items())}
    workers = 8
    barrier = threading.Barrier(workers)
    failures = []

    def work():
        barrier.wait()
        for _ in range(20):
            for name, queryset in queries().items():
                statement = compiler.compile(queryset)
                if (statement.sql, statement.params) != expected[name]:
                    failures.append((name, statement))

    threads = [threading.Thread(target=work) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    assert compiler.connection.connection is None


# Parameters


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, (None, None)),
        ("text", ("text", None)),
        (Int2(1), (1, "int4")),
        (Int4(1), (1, "int4")),
        (Int8(2**40), (2**40, "int8")),
        (Float8(1.5), (decimal.Decimal("1.5"), "numeric")),
        (True, (True, "bool")),
        (7, (7, "int4")),
        (-(2**31), (-(2**31), "int4")),
        (2**31, (2**31, "int8")),
        (2**40, (2**40, "int8")),
        (2**70, (2**70, "numeric")),
        (1.5, (decimal.Decimal("1.5"), "numeric")),
        (0.1, (decimal.Decimal("0.1"), "numeric")),
        (float("inf"), (float("inf"), "float8")),
        (decimal.Decimal("1.50"), (decimal.Decimal("1.50"), "numeric")),
        (PUBLISHED, (PUBLISHED, "timestamptz")),
        (datetime.datetime(2024, 1, 1), (datetime.datetime(2024, 1, 1), "timestamp")),
        (datetime.date(2024, 1, 1), (datetime.date(2024, 1, 1), "date")),
        (datetime.time(10, 0), (datetime.time(10, 0), "time")),
        (datetime.time(10, 0, tzinfo=datetime.UTC), (datetime.time(10, 0, tzinfo=datetime.UTC), "timetz")),
        (datetime.timedelta(hours=1), (datetime.timedelta(hours=1), "interval")),
        (uuid.UUID(int=1), (uuid.UUID(int=1), "uuid")),
        (b"ab", (b"ab", "bytea")),
        (memoryview(b"ab"), (b"ab", "bytea")),
        (Binary(b"ab"), (b"ab", "bytea")),
        (Jsonb({"a": [1, None]}), ('{"a": [1, null]}', "jsonb")),
        (Jsonb({"a": 1}, dumps=lambda obj: f"custom {obj}"), ("custom {'a': 1}", "jsonb")),
        (Json([1]), ("[1]", "json")),
        (ipaddress.ip_address("10.0.0.1"), (ipaddress.ip_address("10.0.0.1"), "inet")),
        (ipaddress.ip_network("10.0.0.0/8"), (ipaddress.ip_network("10.0.0.0/8"), "cidr")),
        ([Int4(1), Jsonb(2)], ([1, "2"], "int4[]")),
        ([1, 2], ([1, 2], "int2[]")),
        ([30000, 70000, None], ([30000, 70000, None], "int4[]")),
        ([[1, 2], [3, 2**40]], ([[1, 2], [3, 2**40]], "int8[]")),
        ([1.5], ([1.5], "float8[]")),
        ([Jsonb(1)], (["1"], "jsonb[]")),
        ([None], ([None], None)),
        ([], ([], None)),
        (("a", "b"), (["a", "b"], None)),
        ({"k": "v"}, ({"k": "v"}, None)),
    ],
)
def test_bind_param(value, expected):
    result = bind_param(value)
    assert result == expected
    assert type(result[0]) is type(expected[0])


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Int2(1), (1, "int2")),
        (Int4(1), (1, "int4")),
        (Int8(1), (1, "int8")),
        (Float8(1.5), (1.5, "float8")),
        (7, (7, "int2")),
        (2**20, (2**20, "int4")),
        (2**40, (2**40, "int8")),
        (2**70, (2**70, "numeric")),
        (1.5, (1.5, "float8")),
        ([1, 2], ([1, 2], "int2[]")),
    ],
)
def test_bind_param_server_side_binding(value, expected):
    result = bind_param(value, server_side_binding=True)
    assert result == expected
    assert type(result[0]) is type(expected[0])


def test_bind_param_nan_decimal():
    value, cast = bind_param(decimal.Decimal("NaN"))
    assert (value.is_nan(), cast) == (True, "numeric")


def test_bind_param_ranges():
    value, cast = bind_param(Range(1, 5, "[)"))
    assert (value, cast) == (asyncpg.Range(1, 5, lower_inc=True, upper_inc=False), None)
    assert bind_param(Range(None, 5, "(]"))[0] == asyncpg.Range(None, 5, lower_inc=False, upper_inc=True)
    assert bind_param(Range(empty=True))[0].isempty


def test_bind_param_refuses_unknown_psycopg_objects():
    with pytest.raises(NotSupportedError, match="no asyncpg value"):
        bind_param(psycopg_sql.Literal("x"))


# Execution on asyncpg, compared with Django's own evaluation on psycopg


def seed():
    ana = Author.objects.create(name="Ana", slug="ana")
    bor = Author.objects.create(name="Bor", slug="bor")
    war = Book.objects.create(
        isbn="9780000000001",
        title="War and Peace",
        genre="fiction",
        pages=1225,
        price=decimal.Decimal("19.90"),
        weight=1.2,
        published=PUBLISHED,
        reading_time=datetime.timedelta(hours=30),
        cover=b"\x00\x01",
        keywords=["war", "peace"],
        extra={"author": {"name": "Ana"}, "tags": ["x", "y"]},
    )
    poems = Book.objects.create(
        isbn="9780000000002",
        title="100%_ Poems",
        genre="poetry",
        pages=120,
        price=decimal.Decimal("9.99"),
        published=PUBLISHED - datetime.timedelta(days=400),
        keywords=[],
        extra={},
    )
    Book.objects.create(isbn="9780000000003", title="Fiction fiction", genre="fiction", pages=300, keywords=["x"])
    war.authors.add(ana, bor)
    poems.authors.add(bor)
    Note.objects.create(
        code=uuid.UUID(int=1), content_type=ContentType.objects.get_for_model(Book), object_id=war.pk, copy=war
    )


async def fetch(url, statement):
    conn = await asyncpg.connect(url)
    try:
        return await conn.fetch(statement.sql, *statement.params)
    finally:
        await conn.close()


async def execute(url, statement):
    conn = await asyncpg.connect(url)
    try:
        return await conn.execute(statement.sql, *statement.params)
    finally:
        await conn.close()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("name", list(queries()))
def test_asyncpg_rows_match_django(test_database_url, name):
    seed()
    queryset = queries()[name]
    expected = list(queryset.query.get_compiler("default").results_iter(tuple_expected=True))
    statement = DjangoCompiler(server_version=connection.pg_version).compile(queryset)
    records = asyncio.run(fetch(test_database_url, statement))
    rows = statement.rows(records)
    assert rows == expected
    assert [[type(v) for v in row] for row in rows] == [[type(v) for v in row] for row in expected]
    if name in ("lookups", "json_key", "exists", "value_in_select", "inet_and_cidr", "uuid_select", "extra_select"):
        assert expected, f"{name} matches no rows, so it compares nothing"


@pytest.mark.django_db(transaction=True)
def test_empty_select_runs_on_asyncpg(test_database_url):
    seed()
    queryset = Book.objects.filter(pk__in=[])
    statement = DjangoCompiler(server_version=connection.pg_version).compile(queryset)
    assert statement.rows(asyncio.run(fetch(test_database_url, statement))) == list(queryset.values_list()) == []


@pytest.mark.django_db(transaction=True)
def test_asyncpg_update_matches_django(test_database_url):
    seed()
    update = Book.objects.filter(genre="fiction").query.chain(UpdateQuery)
    update.add_update_values({"pages": F("pages") + 1, "extra": {"edited": True}})
    statement = DjangoCompiler(server_version=connection.pg_version).compile(update)
    assert asyncio.run(execute(test_database_url, statement)) == "UPDATE 2"
    assert sorted(Book.objects.filter(genre="fiction").values_list("pages", "extra")) == [
        (301, {"edited": True}),
        (1226, {"edited": True}),
    ]


@pytest.mark.django_db(transaction=True)
def test_typed_wrappers_need_casts_on_asyncpg(test_database_url):
    """asyncpg prepares the statement and lets the server infer each parameter's type; psycopg sends the type."""

    async def probe():
        conn = await asyncpg.connect(test_database_url)
        try:
            outcomes = {}
            for name, param in {"int4": Int4(1), "jsonb": Jsonb({"a": 1}), "bool": True, "date": PUBLISHED}.items():
                with pytest.raises(asyncpg.DataError):
                    await conn.fetchval("SELECT $1", param)
                value, cast = bind_param(param)
                outcomes[name] = await conn.fetchval(f"SELECT $1::{cast}", value)
            return outcomes
        finally:
            await conn.close()

    outcomes = asyncio.run(probe())
    with connection.cursor() as cursor:
        cursor.execute("SELECT %s, %s, %s, %s", [Int4(1), Jsonb({"a": 1}), True, PUBLISHED])
        int4, jsonb, boolean, timestamp = cursor.fetchone()
    assert outcomes == {"int4": int4, "jsonb": jsonb, "bool": boolean, "date": timestamp}
    assert (outcomes["jsonb"], jsonb) == ('{"a": 1}', '{"a": 1}')


@pytest.mark.django_db(transaction=True)
def test_naive_datetime_follows_the_session_time_zone(test_database_url):
    naive = datetime.datetime(2024, 1, 1)
    value, cast = bind_param(naive)

    async def probe():
        conn = await asyncpg.connect(test_database_url)
        try:
            await conn.execute("SET TIME ZONE 'Europe/Ljubljana'")
            typed = await conn.fetchval(f"SELECT extract(epoch FROM ($1::{cast})::timestamptz)", value)
            as_utc = await conn.fetchval("SELECT extract(epoch FROM $1::timestamptz)", value)
            return typed, as_utc
        finally:
            await conn.close()

    typed, as_utc = asyncio.run(probe())
    # epoch seconds, because Django's psycopg loader relabels every timestamptz result as UTC
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL TIME ZONE 'Europe/Ljubljana'")
        cursor.execute("SELECT extract(epoch FROM %s::timestamptz)", [naive])
        (psycopg_result,) = cursor.fetchone()
    midnight_in_ljubljana = datetime.datetime(2023, 12, 31, 23, 0, tzinfo=datetime.UTC).timestamp()
    assert typed == psycopg_result == midnight_in_ljubljana
    # asyncpg's own timestamptz encoder reads a naive datetime as UTC
    assert as_utc == datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC).timestamp()


@pytest.mark.django_db(transaction=True)
def test_range_parameters_run_on_asyncpg(test_database_url):
    async def probe():
        conn = await asyncpg.connect(test_database_url)
        try:
            same = await conn.fetchval("SELECT $1::int4range = '[1,5)'::int4range", bind_param(Range(1, 5, "[)"))[0])
            empty = await conn.fetchval("SELECT isempty($1::int4range)", bind_param(Range(empty=True))[0])
            return same, empty
        finally:
            await conn.close()

    assert asyncio.run(probe()) == (True, True)


@pytest.mark.django_db(transaction=True)
def test_asyncpg_parameter_limit(test_database_url):
    async def probe(count):
        conn = await asyncpg.connect(test_database_url)
        try:
            placeholders = ", ".join(f"${n}::int4" for n in range(1, count + 1))
            return await conn.fetchval(f"SELECT cardinality(ARRAY[{placeholders}])", *range(count))
        finally:
            await conn.close()

    assert asyncio.run(probe(MAX_QUERY_PARAMS)) == MAX_QUERY_PARAMS
    with pytest.raises(asyncpg.InterfaceError, match="cannot exceed 32767"):
        asyncio.run(probe(MAX_QUERY_PARAMS + 1))
