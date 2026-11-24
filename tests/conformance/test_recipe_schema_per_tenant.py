"""The how-to recipe docs/howto/schema-per-tenant.md on the PostgreSQL drivers: its definitions run once, its queries
run, each tenant reads its own schema, and without a tenant its ``check_operation`` refuses the tenant tables while the
shared ones run. The blocks of its section on reading across tenants run on every SQL driver against the tenant
schemas of `tests.conformance.test_tenant_relation`: its model declaration is the test app's ``Person``, and its
queries run without a tenant under the recipe's hook."""

from __future__ import annotations

import ast
import inspect
import re
import textwrap

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import asyncpg
import pytest

from django.apps import apps

import adorm

from adorm import Count, Q, Sum
from tests.clubmodels.activities import Tag
from tests.clubmodels.clubs import Club, Member
from tests.conformance.drivers import on_sqlite
from tests.conformance.test_tenant_relation import people, sql_driver, tenants  # noqa: F401 - fixtures
from tests.djangoproject.tenancy import models as tenancy_models
from tests.tenancymodels.tenancy import Enrolment, Person

pytestmark = pytest.mark.django_db(transaction=True)

RECIPE = Path(__file__).parent.parent.parent / "docs" / "howto" / "schema-per-tenant.md"
PYTHON_FENCE = re.compile(r"^( *)```(?:python|py)\b[^\n]*\n(.*?)^\1```", re.MULTILINE | re.DOTALL)

TENANTS = {"tenant_a": ["Ana"], "tenant_b": ["Bor", "Cene"]}
"""The tenant schemas and the first names of their members."""


ACROSS_TENANTS = "## Reading across tenants"
"""The heading of the recipe's section whose blocks read the tenant relation's models."""


def defines_models(source: str) -> bool:
    """Whether ``source`` defines a model class, which the recipe shows and does not run."""
    return any(
        isinstance(node, ast.ClassDef) and any("Model" in ast.unparse(base) for base in node.bases)
        for node in ast.walk(ast.parse(source))
    )


def sources(*, across: bool = False) -> list[str]:
    """The python blocks of the recipe before the section on reading across tenants, or with ``across`` those of that
    section, in order."""
    before, _, after = RECIPE.read_text(encoding="utf-8").partition(ACROSS_TENANTS)
    text = after.partition("\n## ")[0] if across else before
    return [textwrap.dedent(match.group(2)) for match in PYTHON_FENCE.finditer(text)]


def blocks(*, across: bool = False) -> list[Any]:
    """The compiled python blocks of `sources()` without those that define models; a query block has top-level
    ``await``."""
    return [
        compile(source, str(RECIPE), "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        for source in sources(across=across)
        if not defines_models(source)
    ]


def is_query(code: Any) -> bool:
    return bool(code.co_flags & inspect.CO_COROUTINE)


_definitions: dict[str, Any] = {}


def definitions() -> dict[str, Any]:
    """The namespace of the recipe's definition blocks, run once."""
    if not _definitions:
        namespace: dict[str, Any] = {"__name__": "schema_per_tenant_recipe"}
        for code in blocks():
            if not is_query(code):
                exec(code, namespace)  # noqa: S102 - the recipe's own code
        _definitions.update(namespace)
    return _definitions


@pytest.fixture
def recipe(settings):
    """The recipe's namespace, with the clubs app a tenant app and the activities app a shared one."""
    namespace = definitions()
    settings.TENANT_APPS = [apps.get_app_config("clubs").name, "django.contrib.contenttypes"]
    settings.SHARED_APPS = [apps.get_app_config("activities").name, "django.contrib.contenttypes"]
    namespace["tenant_tables"].cache_clear()
    yield namespace
    namespace["tenant_tables"].cache_clear()


@pytest.fixture
async def tenant_schemas(conformance_driver, club_data):
    """A schema per tenant with copies of the clubs app's tables, each with members of its own; dropped afterwards."""
    if conformance_driver.name == "memory" or on_sqlite():
        pytest.skip("the schemas are PostgreSQL's")
    tables = [model._meta.db_table for model in apps.get_app_config("clubs").get_models(include_auto_created=True)]
    conn = await asyncpg.connect(club_data)
    try:
        for schema, names in TENANTS.items():
            await conn.execute(f"CREATE SCHEMA {schema}")
            for table in tables:
                await conn.execute(f"CREATE TABLE {schema}.{table} (LIKE public.{table} INCLUDING ALL)")
            for name in names:
                await conn.execute(
                    f"INSERT INTO {schema}.clubs_member (first_name, last_name, email, level, joined, tags)"
                    " VALUES ($1, 'Tenant', $2, 'beginner', now(), '[]')",
                    name,
                    f"{name.lower()}@{schema}.example.com",
                )
        yield TENANTS
    finally:
        for schema in TENANTS:
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


async def first_names(bound) -> list[str]:
    return [member.first_name async for member in bound.order_by("first_name")]


async def assert_tenant_tables_are_refused(recipe, pool):
    assert recipe["tenant_tables"]() == {"clubs.Club", "clubs.Member", "clubs.Membership"}
    await Tag.objects.count()
    for make in (
        lambda: Member.objects.count(),
        lambda: Club.objects.filter(slug="koper-sailing").exists(),
        lambda: Tag.objects.filter(activities__club__slug="koper-sailing").count(),
    ):
        in_use = pool.in_use
        with pytest.raises(recipe["NoTenant"]):
            await make()
        assert pool.in_use == in_use


async def test_the_tenant_in_a_context_variable(recipe, tenant_schemas, make_router):
    profile = {"on_acquire": recipe["on_acquire"], "check_operation": recipe["check_operation"]}
    pool = make_router(profile=profile, max_connections=2, acquire_timeout=5).pools[0]
    current_schema = recipe["current_schema"]
    for schema, names in tenant_schemas.items():
        token = current_schema.set(schema)
        try:
            assert await first_names(Member.objects) == names
            async with adorm.atomic():
                assert await first_names(Member.objects) == names
            namespace = {**recipe, "adorm": adorm, "Member": Member, "tenant": SimpleNamespace(schema_name=schema)}
            for code in blocks():
                if is_query(code):
                    await eval(code, namespace)  # noqa: S307 - the recipe's own code
        finally:
            current_schema.reset(token)
    await assert_tenant_tables_are_refused(recipe, pool)


async def test_the_tenant_in_the_session(recipe, tenant_schemas, make_router):
    profile = {
        "on_acquire": recipe["on_acquire_from_session"],
        "content_types_key": recipe["content_types_key_from_session"],
        "check_operation": recipe["check_operation"],
    }
    pool = make_router(profile=profile, max_connections=2, acquire_timeout=5).pools[0]
    for schema, names in tenant_schemas.items():
        tenant = adorm.create_session("default", schema)
        assert await first_names(tenant.bind(Member)) == names
        with tenant.activate():
            assert await first_names(Member.objects) == names
            async with adorm.atomic():
                assert await first_names(Member.objects) == names
    await assert_tenant_tables_are_refused(recipe, pool)


def test_the_declaration_across_tenants_is_the_test_apps():
    (source,) = [source for source in sources(across=True) if defines_models(source)]
    (declared,) = [node for node in ast.parse(source).body if isinstance(node, ast.ClassDef)]
    assert ast.get_source_segment(source, declared) == inspect.getsource(tenancy_models.Person).rstrip()


async def test_reading_across_tenants(recipe, tenants, make_router):  # noqa: F811 - the imported fixture
    profile = {"check_operation": recipe["check_operation"]}
    make_router(profile=profile, max_connections=1, acquire_timeout=5)
    # The tenancy app holds the shared models and the tenant models: its tenant tables are named.
    hook_globals = recipe["check_operation"].__globals__
    tenant_tables = hook_globals["tenant_tables"]
    hook_globals["tenant_tables"] = lambda: frozenset({"tenancy.Enrolment", "tenancy.Team"})
    try:
        namespace = {**recipe, "adorm": adorm, "Person": Person, "Count": Count, "Q": Q, "Sum": Sum}
        for code in blocks(across=True):
            if is_query(code):
                await eval(code, namespace)  # noqa: S307 - the recipe's own code
            else:
                exec(code, namespace)  # noqa: S102 - the recipe's own code
        with pytest.raises(recipe["NoTenant"]):
            await Enrolment.objects.count()
    finally:
        hook_globals["tenant_tables"] = tenant_tables
    origins = [(enrolment._state.tenant_schema.schema, enrolment.pk) for enrolment in namespace["recent"]]
    assert origins == [("org_a", 1), ("org_b", 4)]
    assert namespace["enrolments"] == 5
    assert namespace["fees"] == {"fee__sum": Decimal("43.75"), "coaches": 2}
    assert [name for name in namespace["teams"] if name is not None] == [
        "Climbers B",
        "Rowers A",
        "Sailors A",
        "Sailors B",
    ]
    assert {person.name for person in namespace["people"]} == {"Ana"}
    assert (namespace["enrolment"]._state.tenant_schema.schema, namespace["enrolment"].pk) == ("org_b", 4)
    assert namespace["team"].name == "Sailors B"
    assert await tenants.fetch("org_b", "SELECT fee FROM {enrolment} WHERE id = 4") in ([(0,)], [(Decimal(0),)])
    assert (await tenants.enrolment("org_a", 1))[2] == Decimal("10.00")
    assert namespace["session_for_schema"]("org_a", None).session_name == "org_a"
