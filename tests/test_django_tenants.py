"""Schema qualification of tenant tables in the SQL Django's compiler builds (`adorm.drivers.django.tenants`), compiled
without a connection."""

import pytest

from adorm import NotSupportedError, OuterRef, Subquery
from adorm.drivers.asyncpg.compiler import DjangoCompiler
from adorm.drivers.django.tenants import _replace_head
from adorm.drivers.django.translation import translate
from adorm.query import Query
from tests.tenancymodels import tenancy  # noqa: F401 - registers the models

TENANT = ("tenancy.Enrolment", "tenancy.Team")


@pytest.fixture
def compile_sql():
    compiler = DjangoCompiler(server_version=170000)

    def compile_sql(query: Query) -> str:
        return compiler.compile(translate(query).queryset).sql

    return compile_sql


def enrolments(schema: str = "org_a") -> Query:
    return Query("tenancy.Enrolment").filter(role="tenancy_enrolment").in_tenant_schema(schema, TENANT)


def test_the_tables_of_the_scope_are_qualified_and_keep_their_name_as_alias(compile_sql):
    sql = compile_sql(enrolments().filter(team__name="Alpha", person__name="Ana"))
    assert 'FROM "org_a"."tenancy_enrolment" "tenancy_enrolment"' in sql
    assert 'JOIN "org_a"."tenancy_team" "tenancy_team" ON' in sql
    assert 'JOIN "tenancy_person" ON' in sql
    assert '"tenancy_enrolment"."role" = $1' in sql
    assert sql.count('"org_a".') == 2


def test_a_self_join_alias_is_qualified(compile_sql):
    sql = compile_sql(enrolments().filter(team__enrolment__role="coach"))
    # Django 6 quotes the alias, Django 5.2 does not.
    alias = '"T3"' if '"T3"' in sql else "T3"
    assert f'INNER JOIN "org_a"."tenancy_enrolment" {alias} ON ("tenancy_team"."id" = {alias}."team_id")' in sql
    assert sql.count('"org_a".') == 3


def test_a_subquery_takes_the_scope_of_its_query(compile_sql):
    inner = Query("tenancy.Team").filter(pk=OuterRef("team_id")).values("name")
    sql = compile_sql(
        enrolments().annotate(team_name=Subquery(inner)).filter(pk__in=Query("tenancy.Enrolment").values("id"))
    )
    assert sql.count('"org_a"."tenancy_team"') == 1
    assert sql.count('"org_a"."tenancy_enrolment"') == 2


def test_each_query_of_a_union_has_its_own_schema(compile_sql):
    sql = compile_sql(enrolments("org_a").union(enrolments("org_b"), all=True))
    assert '"org_a"."tenancy_enrolment"' in sql
    assert '"org_b"."tenancy_enrolment"' in sql
    assert "UNION ALL" in sql


def test_a_query_without_a_scope_is_not_qualified(compile_sql):
    assert '"org_a"' not in compile_sql(Query("tenancy.Enrolment").filter(team__name="Alpha"))


def test_another_head_is_refused():
    with pytest.raises(NotSupportedError, match="cannot qualify the table 'tenancy_team'"):
        _replace_head('DELETE t FROM "tenancy_team" t', 'DELETE FROM "tenancy_team"', "x", "tenancy_team")
