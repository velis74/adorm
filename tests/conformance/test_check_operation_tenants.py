"""Schema per tenant on the PostgreSQL drivers, guarded by ``check_operation``: each tenant reads the tables of its own
schema, chosen from a context variable or from the session name, and an operation on a tenant's table without a tenant
is refused before it reaches the database, while one on the shared tables runs."""

from __future__ import annotations

from contextvars import ContextVar

import asyncpg
import pytest

import adorm

from tests.clubmodels.clubs import Club, Member, Membership
from tests.conformance.drivers import on_sqlite

pytestmark = pytest.mark.django_db(transaction=True)

TENANTS = {"tenant_a": ["Ana"], "tenant_b": ["Bor", "Cene"]}
"""The tenant schemas and the first names of their members."""

TENANT_TABLES = frozenset({"clubs.Member", "clubs.Membership"})
TENANT_DB_TABLES = ("clubs_member", "clubs_membership")


class NoTenantError(Exception):
    """A tenant's table without a tenant."""


def refuse_without_tenant(operation, tenant):
    models = operation.models
    if tenant is None and (models is None or models & TENANT_TABLES):
        raise NoTenantError(f"{operation.kind} reads or writes a tenant's tables without a tenant")


@pytest.fixture
def postgresql_driver(conformance_driver):
    if conformance_driver.name == "memory" or on_sqlite():
        pytest.skip("the schemas are PostgreSQL's")
    return conformance_driver


@pytest.fixture
async def tenant_schemas(postgresql_driver, club_data):  # noqa: ARG001
    """A schema per tenant with copies of the tenant tables, each with members of its own; dropped afterwards."""
    conn = await asyncpg.connect(club_data)
    try:
        for schema, names in TENANTS.items():
            await conn.execute(f"CREATE SCHEMA {schema}")
            for table in TENANT_DB_TABLES:
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
    return sorted([member.first_name async for member in bound.all()])


async def assert_shared_tables_run_and_tenant_tables_are_refused(pool):
    assert await Club.objects.filter(slug="koper-sailing").count() == 1
    for make in (
        lambda: Member.objects.count(),
        lambda: Membership.objects.filter(role="coach").exists(),
        lambda: Club.objects.filter(members__first_name="Ana").count(),
        lambda: Club.objects.filter(slug="nobody").delete(),
    ):
        in_use = pool.in_use
        with pytest.raises(NoTenantError):
            await make()
        assert pool.in_use == in_use


current_schema: ContextVar[str | None] = ContextVar("current_schema", default=None)


async def test_the_tenant_from_a_context_variable(tenant_schemas, make_router):
    def on_acquire(connection, profile, session):  # noqa: ARG001 - the hook's signature
        schema = current_schema.get()
        return None if schema is None else {"search_path": (schema, "public")}

    def check_operation(operation, session):  # noqa: ARG001 - the hook's signature
        refuse_without_tenant(operation, current_schema.get())

    profile = {"on_acquire": on_acquire, "check_operation": check_operation}
    pool = make_router(profile=profile, max_connections=2, acquire_timeout=5).pools[0]
    for schema, names in tenant_schemas.items():
        token = current_schema.set(schema)
        try:
            assert await first_names(Member.objects) == names
            assert await Club.objects.filter(slug="koper-sailing").count() == 1, "public stays on the path"
            async with adorm.atomic():
                assert await first_names(Member.objects) == names
        finally:
            current_schema.reset(token)
    await assert_shared_tables_run_and_tenant_tables_are_refused(pool)


async def test_the_tenant_from_the_session_name(tenant_schemas, make_router):
    def on_acquire(connection, profile, session):  # noqa: ARG001 - the hook's signature
        return None if session.session_name == "default" else {"search_path": (session.session_name, "public")}

    def check_operation(operation, session):
        refuse_without_tenant(operation, None if session.session_name == "default" else session.session_name)

    profile = {"on_acquire": on_acquire, "check_operation": check_operation}
    pool = make_router(profile=profile, max_connections=2, acquire_timeout=5).pools[0]
    for schema, names in tenant_schemas.items():
        tenant = adorm.Session("default", schema)
        assert await first_names(tenant.bind(Member)) == names
        with tenant.activate():
            assert await first_names(Member.objects) == names
            async with adorm.atomic():
                assert await first_names(Member.objects) == names
    await assert_shared_tables_run_and_tenant_tables_are_refused(pool)
