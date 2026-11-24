"""Tenant schemas in queries and operations: `TenantSchema`, `Query.in_tenant_schema()`, the encoding of scoped
operations, `Operation.schemas` and `Operation.unqualified_models`, the refusal of query writes in a schema and
aggregates over a set operation."""

import pytest

from adorm import Count, F, NotSupportedError, OuterRef, Q, Subquery, Sum, TenantSchema
from adorm.operations import (
    Aggregate,
    BulkCreate,
    Create,
    Delete,
    DeleteInstance,
    FullClean,
    GetOrCreate,
    InBulk,
    Operation,
    Save,
    Select,
    Update,
    UpdateOrCreate,
)
from adorm.query import construct_names, Query
from tests.tenancymodels import tenancy  # noqa: F401 - registers the models

TENANT = ("tenancy.Enrolment", "tenancy.Team")


def enrolments(schema: str) -> Query:
    return Query("tenancy.Enrolment").filter(person_id=1).in_tenant_schema(schema, TENANT)


def test_a_tenant_schema_sorts_its_models_and_refuses_unsafe_names():
    scope = TenantSchema("org_a", ["tenancy.Team", "tenancy.Enrolment", "tenancy.Team"])
    assert scope.models == ("tenancy.Enrolment", "tenancy.Team")
    for name in ("", 'org"a', "org`a", "org[a]", "org\x00", "org\na", 7):
        with pytest.raises(ValueError, match="is not a schema name"):
            TenantSchema(name, TENANT)
    with pytest.raises(ValueError, match="at least one model"):
        TenantSchema("org_a", [])
    with pytest.raises(TypeError):
        TenantSchema("org_a", "tenancy.Team")


def test_a_query_in_a_tenant_schema_reports_the_construct_and_round_trips():
    query = enrolments("org_a")
    assert query.tenant_schema == TenantSchema("org_a", TENANT)
    assert "clause.tenant_schema" in set(query.constructs())
    assert "clause.tenant_schema" in construct_names()
    assert Query.from_dict(query.to_dict()) == query
    assert query.in_tenant_schema(None).tenant_schema is None


def test_a_combined_query_takes_its_schemas_on_its_parts():
    combined = enrolments("org_a").union(enrolments("org_b"), all=True)
    with pytest.raises(NotSupportedError):
        combined.in_tenant_schema("org_a", TENANT)
    with pytest.raises(TypeError, match="same tenant_schema"):
        enrolments("org_a") | enrolments("org_b")


def test_a_union_of_schemas_names_them_and_qualifies_their_tables():
    branches = [enrolments(schema).filter(team__name="Alpha") for schema in ("org_a", "org_b")]
    operation = Select(branches[0].union(branches[1], all=True))
    assert operation.models == {"tenancy.Enrolment", "tenancy.Team"}
    assert operation.schemas == {"org_a", "org_b"}
    assert operation.unqualified_models == frozenset()


def test_a_shared_table_joined_in_a_schema_stays_unqualified():
    operation = Select(enrolments("org_a").filter(person__name="Ana"))
    assert operation.models == {"tenancy.Enrolment", "tenancy.Person"}
    assert operation.unqualified_models == {"tenancy.Person"}


def test_an_operation_without_schemas_reads_its_models_unqualified():
    operation = Select(Query("tenancy.Enrolment").filter(team__name="Alpha"))
    assert operation.schemas == frozenset()
    assert operation.unqualified_models == operation.models


def test_a_subquery_in_another_schema_is_qualified_there():
    inner = Query("tenancy.Team").filter(pk=OuterRef("team")).in_tenant_schema("org_b", TENANT).values("name")
    operation = Select(enrolments("org_a").annotate(other=Subquery(inner)))
    assert operation.schemas == {"org_a", "org_b"}
    assert operation.unqualified_models == frozenset()


@pytest.mark.parametrize(
    "operation",
    [
        Save("tenancy.Enrolment", 1, {"role": "coach"}, tenant_schema=TenantSchema("org_a", TENANT)),
        DeleteInstance("tenancy.Team", 1, tenant_schema=TenantSchema("org_a", TENANT)),
        FullClean("tenancy.Team", 1, {"name": "Alpha"}, adding=False, tenant_schema=TenantSchema("org_a", TENANT)),
    ],
    ids=lambda operation: operation.kind,
)
def test_instance_writes_carry_their_schema(operation):
    assert "clause.tenant_schema" in set(operation.constructs())
    assert operation.schemas == {"org_a"}
    assert not operation.unqualified_models & set(TENANT)
    encoded = operation.to_dict()
    assert encoded["tenant_schema"] == {"node": "TenantSchema", "schema": "org_a", "models": list(TENANT)}
    assert Operation.from_dict(encoded) == operation


def test_instance_writes_without_a_schema_encode_none():
    operation = Save("tenancy.Enrolment", 1, {"role": "coach"})
    assert "tenant_schema" not in operation.to_dict()
    assert operation.unqualified_models == operation.models
    with pytest.raises(TypeError):
        Save("tenancy.Enrolment", 1, {}, tenant_schema="org_a")


@pytest.mark.parametrize(
    "build",
    [
        lambda q: Update(q, {"role": "x"}),
        Delete,
        InBulk,
        lambda q: GetOrCreate(q, {"role": "x"}),
        lambda q: UpdateOrCreate(q, {"role": "x"}),
        lambda q: Create("tenancy.Person", {"name": Subquery(q.values("role")[:1])}),
        lambda q: BulkCreate("tenancy.Person", [{"name": Subquery(q.values("role")[:1])}]),
    ],
)
def test_writes_of_a_query_in_a_schema_are_refused(build):
    with pytest.raises(NotSupportedError, match="in a tenant schema is not supported"):
        build(enrolments("org_a"))


def test_an_aggregate_over_a_union_reports_its_construct():
    combined = Query("tenancy.Enrolment").values("fee").union(Query("tenancy.Enrolment").values("fee"), all=True)
    operation = Aggregate(combined, {"total": Sum("fee"), "n": Count("*")})
    assert "aggregate.combined" in set(operation.constructs())
    assert Operation.from_dict(operation.to_dict()) == operation


@pytest.mark.parametrize(
    "expression",
    [Sum("team__id"), Sum("role"), Count("fee", filter=Q(role="coach")), Sum(OuterRef("fee"))],
)
def test_an_aggregate_over_a_union_names_its_columns(expression):
    combined = Query("tenancy.Enrolment").values("fee").union(Query("tenancy.Enrolment").values("fee"), all=True)
    with pytest.raises(TypeError, match="combined rows"):
        Aggregate(combined, {"value": expression})


def test_an_aggregate_over_a_union_of_model_rows_takes_fields():
    combined = Query("tenancy.Enrolment").union(Query("tenancy.Enrolment"), all=True)
    assert Aggregate(combined, {"total": Sum(F("fee"))}).aggregations[0][0] == "total"
