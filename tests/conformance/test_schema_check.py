"""The schema check on every driver: schema hashes compared at use, and the tables and columns compared with the
models by `adorm.check_schema()`."""

import asyncio
import itertools

from dataclasses import replace

import asyncpg
import pytest

import adorm

from adorm import check_schema, Model, ModelMeta, NotSupportedError, SchemaMismatchError, SchemaProblem
from adorm.models.registry import registry
from adorm.operations import CheckTable
from demo.models import MODELS
from demo.models.clubs import Club, Member, Membership
from tests.conformance.drivers import on_sqlite
from tests.generated import MODELS as GENERATED
from tests.generated.library import Author, Book, Edition

pytestmark = pytest.mark.django_db(transaction=True)

SCRATCH = "adorm_schema_check"
_names = itertools.count()


def session():
    return adorm.create_session("default", f"schema-{next(_names)}")


def checks_tables(driver) -> bool:
    return "check_table" in driver.capabilities.operations


async def test_the_models_match_the_test_database(driver, router):
    if not checks_tables(driver):
        with pytest.raises(NotSupportedError, match="'check_table'"):
            await check_schema(MODELS, session=session())
        return
    assert await check_schema(MODELS, session=session()) == ()
    if not on_sqlite():  # the SQLite settings install no library app
        assert await check_schema(GENERATED, session=session()) == ()
    assert router.pools[0].in_use == 0


async def test_reading_the_catalogue_keeps_the_slot(driver, make_router):
    if not checks_tables(driver):
        pytest.skip(f"the {driver.name} driver does not check tables")
    pool = make_router(max_connections=1, acquire_timeout=5).pools[0]
    s = session()
    for _ in range(3):
        assert await check_schema([Club, Member], session=s) == ()
        assert (pool.size, pool.in_use) == (1, 0)


async def test_names_the_orm_model_lacks_are_unknown_fields(driver, router):  # noqa: ARG001
    if not checks_tables(driver):
        pytest.skip(f"the {driver.name} driver does not check tables")
    if on_sqlite():
        pytest.skip("the SQLite settings install no library app")
    result = await session().execute(CheckTable("library.Author", ("id", "nickname", "pupils"), ("mentor",)))
    assert [(item["kind"], item["field"] or item["column"]) for item in result] == [
        ("unknown_field", "nickname"),
        ("unknown_field", "pupils"),
        ("unknown_field", "mentor"),
        ("required_column", "name"),
        ("required_column", "slug"),
        ("required_column", "rating"),
        ("required_column", "website"),
    ]
    with pytest.raises(SchemaMismatchError, match="no Django model is installed as 'library.Nothing'") as raised:
        await session().execute(CheckTable("library.Nothing", ("id",)))
    assert (raised.value.model, raised.value.actual, raised.value.sqlstate) == ("library.Nothing", None, None)


async def test_a_model_the_target_lacks_leaves_the_transaction_usable(driver, router):
    if not checks_tables(driver):
        pytest.skip(f"the {driver.name} driver does not check tables")
    s = session()
    async with s.atomic():
        with pytest.raises(SchemaMismatchError):
            await s.execute(CheckTable("library.Nothing", ("id",)))
        assert await s.bind(Member).filter(first_name="Ana").update(last_name="Still-usable") == 1
    assert await s.bind(Member).filter(last_name="Still-usable").count() == 1
    assert router.pools[0].in_use == 0


@pytest.fixture
def ghost():
    """A registered model without a schema hash that no driver's target has."""

    class Ghost(Model):
        _meta = ModelMeta("library.Ghost")
        id: int | None = None

    try:
        yield Ghost
    finally:
        registry.unregister("library.Ghost")


async def test_check_schema_reports_a_model_the_target_lacks(driver, router, ghost):  # noqa: ARG001
    if not checks_tables(driver):
        pytest.skip(f"the {driver.name} driver does not check tables")
    s = session()
    async with s.atomic():
        problems = await check_schema([Club, ghost, Member], session=s)
        assert await s.bind(Club).count() == 3
    assert problems == (
        SchemaProblem("library.Ghost", "schema_hash", detail="no Django model is installed as 'library.Ghost'"),
    )


async def test_a_mismatched_model_fails_at_use_and_only_there(driver, router, monkeypatch):
    monkeypatch.setattr(Club, "_meta", replace(Club._meta, schema_hash="0000000000000000"))
    pool = router.pools[0]
    s = session()
    if driver.schema_hash(Club._meta.label) is None:
        assert await s.bind(Club).count() == 3
        return
    with pytest.raises(SchemaMismatchError) as raised:
        await s.bind(Club).count()
    assert (raised.value.model, raised.value.expected, raised.value.driver) == (
        "clubs.Club",
        "0000000000000000",
        driver.name,
    )
    assert raised.value.actual == driver.schema_hash("clubs.Club") != "0000000000000000"
    assert await s.bind(Member).count() > 0
    with pytest.raises(SchemaMismatchError):
        await s.bind(Membership).select_related("club").count()
    assert await s.bind(Membership).count() > 0
    async with s.atomic():
        with pytest.raises(SchemaMismatchError):
            await s.bind(Club).filter(slug="koper-sailing").update(city="Izola")
        assert await s.bind(Member).filter(first_name="Ana").update(last_name="Schema-check") == 1
    assert await s.bind(Member).filter(last_name="Schema-check").count() == 1
    assert pool.in_use == 0
    assert problems_of(await check_schema([Membership, Club], session=s)) == [("clubs.Club", "schema_hash")]


def problems_of(problems):
    return [(problem.model, problem.kind) for problem in problems]


@pytest.fixture
async def scratch(test_database_url):
    """An asyncpg connection with the schema ``SCRATCH`` holding copies of the library's tables, dropped afterwards;
    the test is skipped on SQLite."""
    if on_sqlite():
        pytest.skip("the test drifts copies of the library's PostgreSQL tables")
    conn = await asyncpg.connect(test_database_url)
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {SCRATCH} CASCADE; CREATE SCHEMA {SCRATCH}")
        for table in ("library_author", "library_book", "library_edition", "library_book_authors"):
            await conn.execute(f"CREATE TABLE {SCRATCH}.{table} (LIKE public.{table} INCLUDING ALL)")
        yield conn
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {SCRATCH} CASCADE")
        await conn.close()


async def test_check_schema_reports_what_differs_in_the_database(driver, make_router, scratch):
    if not checks_tables(driver):
        pytest.skip(f"the {driver.name} driver does not check tables")
    make_router(profile=scratch_profile(), max_connections=2, acquire_timeout=5)
    s = session()
    models = [Author, Book, Edition]
    assert await check_schema(models, session=s) == ()
    await scratch.execute(
        f"SET search_path = {SCRATCH};"
        " ALTER TABLE library_author DROP COLUMN slug;"
        " ALTER TABLE library_author ALTER COLUMN name TYPE varchar(50);"
        " ALTER TABLE library_author ALTER COLUMN rating DROP NOT NULL;"
        " ALTER TABLE library_author ALTER COLUMN born SET NOT NULL;"
        " ALTER TABLE library_author ADD COLUMN nickname text NOT NULL;"
        " ALTER TABLE library_author ADD COLUMN note text NOT NULL DEFAULT '';"
        " ALTER TABLE library_author ADD COLUMN serial_no integer GENERATED ALWAYS AS IDENTITY;"
        " ALTER TABLE library_book ALTER COLUMN keywords TYPE varchar(20)[];"
        " DROP TABLE library_book_authors;"
        " DROP TABLE library_edition"
    )
    problems = await check_schema(models, session=s)
    assert problems == (
        SchemaProblem(
            "library.Author", "column_type", "library_author", "name", "name", "varchar(100)", "character varying(50)"
        ),
        SchemaProblem("library.Author", "missing_column", "library_author", "slug", "slug"),
        SchemaProblem("library.Author", "nullability", "library_author", "born", "born", "NULL", "NOT NULL"),
        SchemaProblem("library.Author", "nullability", "library_author", "rating", "rating", "NOT NULL", "NULL"),
        SchemaProblem("library.Author", "required_column", "library_author", "nickname", actual="text"),
        SchemaProblem("library.Author", "missing_table", "library_book_authors"),
        SchemaProblem(
            "library.Book",
            "column_type",
            "library_book",
            "keywords",
            "keywords",
            "varchar(30)[10]",
            "character varying(20)[]",
        ),
        SchemaProblem("library.Edition", "missing_table", "library_edition"),
    )
    with pytest.raises(adorm.SchemaCheckError) as raised:
        await check_schema(models, session=s, raise_on_problems=True)
    assert raised.value.problems == problems


@pytest.fixture
def library_models(driver, monkeypatch):  # noqa: ARG001 - the driver is built with the whole registry
    """The model registry holds the library's models only, once the driver is built; the test is skipped on SQLite,
    whose settings install no library app."""
    if on_sqlite():
        pytest.skip("the SQLite settings install no library app")
    monkeypatch.setattr(registry, "_models", {model._meta.label: model for model in (Author, Book, Edition)})


def scratch_profile():
    from django.conf import settings

    options = {**settings.DATABASES["default"].get("OPTIONS", {}), "options": f"-c search_path={SCRATCH}"}
    return {"params": {"OPTIONS": options}}


@pytest.mark.parametrize("mode", ["warn", "raise"])
async def test_the_start_up_check_passes_on_a_matching_schema(driver, make_router, library_models, mode, caplog):  # noqa: ARG001
    if not checks_tables(driver):
        pytest.skip(f"the {driver.name} driver does not check tables")
    pool = make_router(max_connections=2, acquire_timeout=5, check_schema=mode).pools[0]
    with caplog.at_level("WARNING", logger="adorm.executor"):
        assert await session().bind(Author).count() >= 0
    assert [record for record in caplog.records if record.name == "adorm.executor"] == []
    assert pool._schema_problems == ()
    assert pool.in_use == 0


async def test_the_start_up_check_finds_a_drifted_schema(driver, make_router, library_models, scratch, caplog):  # noqa: ARG001
    if not checks_tables(driver):
        pytest.skip(f"the {driver.name} driver does not check tables")
    await scratch.execute(f"SET search_path = {SCRATCH}; ALTER TABLE library_author DROP COLUMN slug")
    expected = (SchemaProblem("library.Author", "missing_column", "library_author", "slug", "slug"),)
    pool = make_router(profile=scratch_profile(), max_connections=2, acquire_timeout=5, check_schema="raise").pools[0]
    for _ in range(2):
        with pytest.raises(adorm.SchemaCheckError) as raised:
            await session().bind(Book).count()
        assert raised.value.problems == expected
    assert pool.in_use == 0
    caplog.clear()
    pool = make_router(profile=scratch_profile(), max_connections=2, acquire_timeout=5, check_schema="warn").pools[0]
    with caplog.at_level("WARNING", logger="adorm.executor"):
        assert await session().bind(Book).count() == 0
    (record,) = [record for record in caplog.records if record.name == "adorm.executor"]
    assert "library.Author.slug: the column library_author.slug does not exist" in record.getMessage()
    assert pool.in_use == 0


@pytest.mark.parametrize("attempt", range(5))
@pytest.mark.usefixtures("library_models")
async def test_concurrent_first_operations_run_the_start_up_check_once(driver, make_router, monkeypatch, attempt):  # noqa: ARG001
    if not checks_tables(driver):
        pytest.skip(f"the {driver.name} driver does not check tables")
    import adorm.checks

    runs = []
    check_models = adorm.checks.check_models

    async def counting(models, execute):
        runs.append(len(models))
        return await check_models(models, execute)

    monkeypatch.setattr(adorm.checks, "check_models", counting)
    pool = make_router(max_connections=2, acquire_timeout=5, check_schema="raise").pools[0]

    async def in_transaction():
        s = session()
        async with s.atomic():
            return await s.bind(Book).count()

    counts = await asyncio.gather(
        *(session().bind(Author).count() for _ in range(8)), *(in_transaction() for _ in range(4))
    )
    assert len(set(counts[:8])) == len(set(counts[8:])) == 1
    assert runs == [3]
    assert pool.in_use == 0
