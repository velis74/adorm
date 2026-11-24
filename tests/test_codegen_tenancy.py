"""Tenant relations in code generation: the Django declaration, its checks, the emitted `adorm.TenantRelation` and the
checked-in models of ``tests/tenancymodels``."""

import warnings

from pathlib import Path

import pytest

from django.core import checks
from django.core.management import call_command
from django.db.models import CharField, F, Max, Value
from django.db.models.functions import Cast, Concat, Lower

from adorm import Concat as AdormConcat, TenantRelation
from adorm.codegen import AppSpec, check, CodegenError, CodegenWarning, plan
from adorm.codegen.django import introspect, model_spec
from adorm.codegen.render import render_module
from adorm.contrib.django import TenantRelation as DjangoTenantRelation
from adorm.models import get_relations, get_tenant_relations
from tests.djangoproject.tenancy import models as django_tenancy
from tests.tenancymodels import tenancy

GENERATED = Path(__file__).parent / "tenancymodels"
COMMAND = "adorm generate --settings tests.djangoproject.settings -o tests/tenancymodels --line-length 120 tenancy"


def test_checked_in_models_are_up_to_date():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", CodegenWarning)
        files = plan(introspect(["tenancy"]), line_length=120)
    assert check(GENERATED, files) == [], f"run {COMMAND}"


def test_the_generated_relation_is_a_tenant_relation_and_no_relation():
    relation = get_tenant_relations(tenancy.Person)["enrolments"]
    assert isinstance(tenancy.Person.enrolments, TenantRelation)
    assert (relation.target, relation.column, relation.schema) == (
        "tenancy.Enrolment",
        "person_id",
        "organisations__organisation__schema_name",
    )
    assert relation.models == {"tenancy.Enrolment", "tenancy.Team"}
    assert "enrolments" not in get_relations(tenancy.Person)
    assert "enrolments" not in tenancy.Person.model_json_schema()["properties"]


@pytest.mark.django_db
def test_the_declaration_adds_no_column_migration_or_check():
    assert "enrolments" not in {field.name for field in django_tenancy.Person._meta.get_fields()}
    call_command("makemigrations", "tenancy", check=True, dry_run=True, verbosity=0)
    messages = checks.run_checks()
    assert [message for message in messages if "tenancy" in str(message.obj)] == []
    assert django_tenancy.Person.enrolments is django_tenancy.Person().enrolments


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        ({"to": "Enrolment"}, ValueError),
        ({"field": "person id"}, ValueError),
        ({"schema": "organisations__"}, ValueError),
        ({"schema": 7}, TypeError),
        ({"tenant_models": "tenancy"}, TypeError),
        ({"tenant_models": ["tenancy.Team.x"]}, ValueError),
        ({"session": "session"}, ValueError),
    ],
)
def test_the_declaration_checks_its_arguments(arguments, error):
    values = {"to": "tenancy.Enrolment", "field": "person", "schema": "organisations__organisation__schema_name"}
    to = {**values, **arguments}.pop("to")
    with pytest.raises(error):
        DjangoTenantRelation(to, **{key: value for key, value in {**values, **arguments}.items() if key != "to"})


def declare(monkeypatch, **arguments):
    values = {"field": "person", "schema": "organisations__organisation__schema_name"} | arguments
    to = values.pop("to", "tenancy.Enrolment")
    monkeypatch.setattr(django_tenancy.Person, "enrolments", DjangoTenantRelation(to, **values))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", CodegenWarning)
        spec = model_spec(django_tenancy.Person)
    return next(relation for relation in spec.relations if relation.kind == "tenant")


def test_an_expression_schema_is_emitted_with_adorm_classes(monkeypatch):
    schema = Concat(Value("org_"), Lower(Cast("organisations__organisation_id", CharField(max_length=10))))
    relation = declare(monkeypatch, schema=schema, tenant_models=["tenancy"], session="myapp.tenants.session")
    assert relation.schema is None
    assert relation.schema_source == (
        "Concat",
        (("Value", "org_"), ("Lower", (("Cast", ("F", "organisations__organisation_id"), 10),))),
    )
    assert relation.models == (
        "tenancy.Enrolment",
        "tenancy.Organisation",
        "tenancy.Person",
        "tenancy.PersonOrganisation",
        "tenancy.Team",
    )
    source = render_module(AppSpec("tenancy", (model_spec_with(relation),)), line_length=200)
    assert (
        'schema=Concat(Value("org_"), Lower(Cast(F("organisations__organisation_id"), str, max_length=10)))' in source
    )
    assert 'session="myapp.tenants.session"' in source
    namespace: dict = {}
    exec(compile(source.replace("tenancy.Person", "tenancy_copy.Person"), "<generated>", "exec"), namespace)  # noqa: S102
    assert isinstance(namespace["Person"].enrolments.schema, AdormConcat)


def model_spec_with(relation):
    from dataclasses import replace

    spec = model_spec(django_tenancy.Person)
    relations = tuple(r for r in spec.relations if r.kind != "tenant") + (relation,)
    return replace(spec, label="tenancy_copy.Person", relations=relations)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"to": "tenancy.Nothing"}, "tenancy.Nothing is not an installed model"),
        ({"field": "team"}, "tenancy.Enrolment.team is not a foreign key to the primary key of tenancy.Person"),
        ({"schema": "organisations__club__name"}, "has no field 'club'"),
        ({"schema": "organisations__organisation"}, "does not end on a field that is not a relation"),
        ({"tenant_models": ["shop"]}, "tenant_models names 'shop'"),
        ({"schema": Max(F("name"))}, "the schema expression uses Max"),
        ({"schema": Value(1.5)}, "Value() of a str or an int"),
    ],
)
def test_a_declaration_that_names_nothing_is_a_codegen_error(monkeypatch, arguments, message):
    with pytest.raises(CodegenError, match=message.replace("(", r"\(").replace(")", r"\)")):
        declare(monkeypatch, **arguments)
