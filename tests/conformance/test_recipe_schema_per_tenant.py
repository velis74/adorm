"""The how-to recipe docs/howto/schema-per-tenant.md on the PostgreSQL drivers: its definitions run once, its queries
run, each tenant reads its own schema, and without a tenant its ``check_operation`` refuses the tenant tables while the
shared ones run."""

from __future__ import annotations

import ast
import inspect
import re
import textwrap

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import asyncpg
import pytest

from django.apps import apps

import adorm

from tests.clubmodels.activities import Tag
from tests.clubmodels.clubs import Club, Member
from tests.conformance.drivers import on_sqlite

pytestmark = pytest.mark.django_db(transaction=True)

RECIPE = Path(__file__).parent.parent.parent / "docs" / "howto" / "schema-per-tenant.md"
PYTHON_FENCE = re.compile(r"^( *)```(?:python|py)\b[^\n]*\n(.*?)^\1```", re.MULTILINE | re.DOTALL)

TENANTS = {"tenant_a": ["Ana"], "tenant_b": ["Bor", "Cene"]}
"""The tenant schemas and the first names of their members."""


def blocks() -> list[Any]:
    """The compiled python blocks of the recipe, in order; a query block has top-level ``await``."""
    text = RECIPE.read_text(encoding="utf-8")
    return [
        compile(textwrap.dedent(match.group(2)), str(RECIPE), "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        for match in PYTHON_FENCE.finditer(text)
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
