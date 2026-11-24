"""The read-only cross-tenant reverse relation on the SQL drivers: a shared person reads their enrolments from the
tenant schemas their organisations name, in one statement, with filters, ordering, slicing, values and aggregates done
by the database; a returned enrolment writes to its own schema; writes through the relation are refused; and a
``check_operation`` hook sees the tenant model together with the explicit schemas.

Tenant schemas are PostgreSQL schemas, or SQLite databases attached under the schema names
(``--ds=tests.djangoproject.settings_sqlite``). The test app ``tests.djangoproject.tenancy`` declares the relation
``Person.enrolments``; its generated adorm models are ``tests.tenancymodels.tenancy``.
"""

from __future__ import annotations

import datetime
import inspect
import re
import sqlite3

from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from django.core import checks
from django.core.management import call_command
from django.db import models as django_models
from django.db.backends.signals import connection_created

import adorm

from adorm import (
    Count,
    DatabaseError,
    F,
    Max,
    NotSupportedError,
    OperationalError,
    ProgrammingError,
    Q,
    Sum,
    TenantQuerySet,
    TenantRelation,
    TenantSchema,
    ValidationError,
)
from adorm.models import get_relations, get_tenant_relations
from adorm.operations import Count as CountOperation, DeleteInstance, Operation, Save, Select, Update
from adorm.query import Query
from tests.clubmodels.clubs import Membership
from tests.conformance.drivers import on_sqlite
from tests.djangoproject.tenancy import models as dj
from tests.library_data import in_thread
from tests.tenancymodels.tenancy import Enrolment, Organisation, Person, PersonOrganisation, Team

pytestmark = pytest.mark.django_db(transaction=True)

TENANT_MODELS = ("tenancy.Enrolment", "tenancy.Team")
"""The labels of the tenant models, as the relation declares them (sorted)."""

SHARED_MODELS = frozenset({"tenancy.Organisation", "tenancy.Person", "tenancy.PersonOrganisation"})

SCHEMAS = ("org_a", "org_b", "org_c")
"""The tenant schemas the fixture creates; ``org_missing`` and ``org"x`` are named by organisations only."""

ORGANISATIONS = {"A": "org_a", "B": "org_b", "C": "org_c", "Missing": "org_missing", "Evil": 'org"x'}
PEOPLE = {
    "Ana": ["A", "B"],
    "Bor": ["A", "C"],
    "Cene": ["C"],
    "Nobody": [],
    "Lost": ["A", "Missing"],
    "Evil": ["A", "Evil"],
}
"""Each person's organisations: Ana's enrolments are in org_a and org_b; Cene's organisation has none of hers."""

TEAMS = {
    "org_a": [(1, "Sailors A"), (2, "Rowers A")],
    "org_b": [(1, "Sailors B"), (3, "Climbers B")],
    "org_c": [(1, "C team")],
}
"""``(id, name)`` per schema: equal ids name different teams in different schemas."""

ENROLMENTS = {
    "org_a": [
        (1, "Ana", 1, "coach", datetime.date(2024, 1, 10), Decimal("10.00")),
        (2, "Ana", 2, "member", datetime.date(2024, 3, 1), Decimal("20.00")),
        (3, "Bor", 1, "member", datetime.date(2024, 2, 1), Decimal("99.00")),
    ],
    "org_b": [
        (1, "Ana", 3, "member", datetime.date(2024, 2, 15), Decimal("5.50")),
        (4, "Ana", 1, "coach", datetime.date(2023, 12, 1), Decimal("7.25")),
        (5, "Ana", None, "board", datetime.date(2024, 4, 1), Decimal("1.00")),
    ],
    "org_c": [(6, "Bor", 1, "member", datetime.date(2024, 5, 1), Decimal("3.00"))],
}
"""``(id, person, team id, role, joined, fee)`` per schema; enrolment 1 is in org_a and in org_b."""

ANA = {
    (schema, pk): (team, role, joined, fee)
    for schema, rows in ENROLMENTS.items()
    for pk, person, team, role, joined, fee in rows
    if person == "Ana"
}
"""Ana's enrolments by ``(schema, id)``."""

BY_JOINED = [("org_b", 4), ("org_a", 1), ("org_b", 1), ("org_a", 2), ("org_b", 5)]
"""Ana's enrolments, oldest first."""


def origin(enrolment: Any) -> tuple[str, int]:
    return enrolment._state.tenant_schema.schema, enrolment.pk


async def origins(queryset: Any) -> list[tuple[str, int]]:
    return [origin(enrolment) async for enrolment in queryset]


# The data set


def _seed_shared() -> dict[str, int]:
    organisations = {
        key: dj.Organisation.objects.create(name=key, schema_name=schema) for key, schema in ORGANISATIONS.items()
    }
    people = {}
    for name, keys in PEOPLE.items():
        person = dj.Person.objects.create(name=name)
        for key in keys:
            dj.PersonOrganisation.objects.create(person=person, organisation=organisations[key])
        people[name] = person.pk
    return people


class TenantStore:
    """Direct access to the tenant tables, outside adorm: ``{team}`` and ``{enrolment}`` in a statement name the
    schema's tables."""

    async def fetch(self, schema: str, sql: str) -> list[tuple]:
        raise NotImplementedError

    async def execute(self, schema: str, sql: str) -> None:
        raise NotImplementedError

    async def enrolment(self, schema: str, pk: int) -> tuple | None:
        """``(team id, role, fee)`` of the schema's enrolment ``pk``, ``None`` when there is none."""
        rows = await self.fetch(schema, f"SELECT team_id, role, fee FROM {{enrolment}} WHERE id = {pk}")
        return (rows[0][0], rows[0][1], Decimal(str(rows[0][2])).quantize(Decimal("0.01"))) if rows else None


class PostgresTenants(TenantStore):
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    def _sql(self, schema: str, sql: str) -> str:
        return sql.format(team=f"{schema}.tenancy_team", enrolment=f"{schema}.tenancy_enrolment")

    async def fetch(self, schema: str, sql: str) -> list[tuple]:
        return [tuple(row) for row in await self.conn.fetch(self._sql(schema, sql))]

    async def execute(self, schema: str, sql: str) -> None:
        await self.conn.execute(self._sql(schema, sql))


class SQLiteTenants(TenantStore):
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files

    def _run(self, schema: str, sql: str) -> list[tuple]:
        conn = sqlite3.connect(self.files[schema], isolation_level=None)
        try:
            return conn.execute(sql.format(team="tenancy_team", enrolment="tenancy_enrolment")).fetchall()
        finally:
            conn.close()

    async def fetch(self, schema: str, sql: str) -> list[tuple]:
        return self._run(schema, sql)

    async def execute(self, schema: str, sql: str) -> None:
        self._run(schema, sql)


def _rows(people: dict[str, int]) -> dict[str, list[tuple[str, tuple]]]:
    """The insert statements of each schema with their values."""
    statements: dict[str, list[tuple[str, tuple]]] = {}
    for schema in SCHEMAS:
        statements[schema] = [("INSERT INTO {team} (id, name) VALUES (?, ?)", team) for team in TEAMS[schema]]
        statements[schema] += [
            (
                "INSERT INTO {enrolment} (id, person_id, team_id, role, joined, fee) VALUES (?, ?, ?, ?, ?, ?)",
                (pk, people[person], team, role, joined, fee),
            )
            for pk, person, team, role, joined, fee in ENROLMENTS[schema]
        ]
    return statements


REFERENCES = re.compile(r'\s+REFERENCES\s+"[^"]+"\s*\("[^"]+"\)(\s+DEFERRABLE INITIALLY DEFERRED)?', re.IGNORECASE)


@pytest.fixture
def sql_driver(conformance_driver):
    if conformance_driver.name == "memory":
        pytest.skip("the memory driver has no tenant schemas")
    return conformance_driver


@pytest.fixture
async def people(sql_driver, club_data):  # noqa: ARG001 - the tables exist once the test database does
    """The shared rows; the primary keys of the people by name."""
    return await in_thread(_seed_shared)


@pytest.fixture
async def tenants(people, club_data, tmp_path):
    """The tenant schemas with the tenant tables and their rows, as a `TenantStore`; the profile keywords the
    tenant schemas need are its ``profile``. Dropped afterwards."""
    statements = _rows(people)
    if on_sqlite():
        path = club_data.removeprefix("sqlite:///")
        main = sqlite3.connect(path)
        try:
            ddl = [
                REFERENCES.sub("", sql)
                for (sql,) in main.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table'"
                    " AND name IN ('tenancy_team', 'tenancy_enrolment')"
                )
            ]
        finally:
            main.close()
        files = {schema: str(tmp_path / f"{schema}.sqlite3") for schema in SCHEMAS}
        for schema, file in files.items():
            conn = sqlite3.connect(file, isolation_level=None)
            try:
                for sql in ddl:
                    conn.execute(sql)
                for sql, values in statements[schema]:
                    conn.execute(
                        sql.format(team="tenancy_team", enrolment="tenancy_enrolment"),
                        tuple(str(v) if isinstance(v, (Decimal, datetime.date)) else v for v in values),
                    )
            finally:
                conn.close()

        def attach(sender, connection, **kwargs):  # noqa: ARG001 - a signal receiver
            if connection.alias == "default":
                for schema, file in files.items():
                    connection.connection.execute(f"ATTACH DATABASE ? AS {schema}", (file,))

        # Django's SQLite backend opens connections without the profile's ``connect`` hook.
        connection_created.connect(attach)
        try:
            store = SQLiteTenants(files)
            store.profile = {}
            yield store
        finally:
            connection_created.disconnect(attach)
        return
    conn = await asyncpg.connect(club_data)
    try:
        for schema in SCHEMAS:
            await conn.execute(f"CREATE SCHEMA {schema}")
            for table in ("tenancy_team", "tenancy_enrolment"):
                await conn.execute(f"CREATE TABLE {schema}.{table} (LIKE public.{table} INCLUDING ALL)")
            for sql, values in statements[schema]:
                numbered = sql
                for n in range(1, len(values) + 1):
                    numbered = numbered.replace("?", f"${n}", 1)
                await conn.execute(
                    numbered.format(team=f"{schema}.tenancy_team", enrolment=f"{schema}.tenancy_enrolment"), *values
                )
        store = PostgresTenants(conn)
        store.profile = {}
        yield store
    finally:
        for schema in SCHEMAS:
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


@pytest.fixture
def seen():
    """The operations the profile's ``check_operation`` hook saw, in order."""
    return []


@pytest.fixture
def connect(tenants, make_router, seen):
    """``connect(check=None, **pool)`` routes ``default`` to a pool over the tenant schemas whose ``check_operation``
    records each operation in ``seen`` and then calls ``check``; returns the pool."""

    def make(check=None, **pool):
        def check_operation(operation, session):
            seen.append(operation)
            if check is not None:
                check(operation, session)

        profile = {**tenants.profile, "check_operation": check_operation}
        pool = {"max_connections": 2, "acquire_timeout": 5, **pool}
        return make_router(profile=profile, **pool).pools[0]

    return make


@pytest.fixture
async def ana(connect, people):
    connect()
    return await Person.objects.get(pk=people["Ana"])


async def person(name: str, people: dict[str, int]) -> Any:
    return await Person.objects.get(pk=people[name])


# The declaration


def test_the_django_declaration_creates_no_column_migration_or_check_message():
    call_command("makemigrations", "tenancy", "--check", "--dry-run", verbosity=0)
    assert [message for message in checks.run_checks() if "tenancy" in str(message.obj)] == []
    assert "enrolments" not in {field.name for field in dj.Person._meta.get_fields()}
    declaration = dj.Person.enrolments
    assert dj.Person(name="x").enrolments is declaration
    inspect.getmembers(dj.Person)


def test_the_generated_model_carries_the_relation():
    relation = Person.enrolments
    assert isinstance(relation, TenantRelation)
    assert relation.target == "tenancy.Enrolment"
    assert relation.column == "person_id"
    assert relation.models == frozenset(TENANT_MODELS)
    assert get_tenant_relations(Person) == {"enrolments": relation}
    assert "enrolments" not in get_relations(Person)


def test_an_owner_without_a_primary_key_has_no_tenant_relation():
    with pytest.raises(ValueError):  # noqa: PT011 - the message is not part of the contract
        Person(name="new").enrolments  # noqa: B018 - the access raises


# Reading across tenants


async def test_rows_of_every_tenant_come_back_with_their_schema(ana):
    enrolments = ana.enrolments
    assert isinstance(enrolments, TenantQuerySet)
    assert enrolments.model is Enrolment
    rows = [enrolment async for enrolment in enrolments]
    assert sorted(origin(row) for row in rows) == sorted(ANA)
    for row in rows:
        assert row._state.tenant_schema.models == TENANT_MODELS
        team, role, joined, fee = ANA[origin(row)]
        assert (row.team_id, row.role, row.joined, row.fee) == (team, role, joined, fee)
        assert row.person_id == ana.pk
    assert await enrolments.schemas() == ["org_a", "org_b"]


async def test_filter_exclude_order_and_slice_across_tenants(ana):
    assert sorted(await origins(ana.enrolments.filter(role="member"))) == [("org_a", 2), ("org_b", 1)]
    assert sorted(await origins(ana.enrolments.exclude(role="member"))) == [("org_a", 1), ("org_b", 4), ("org_b", 5)]
    assert await origins(ana.enrolments.order_by("joined")) == BY_JOINED
    assert await origins(ana.enrolments.order_by("-joined")) == BY_JOINED[::-1]
    assert await origins(ana.enrolments.order_by("-joined")[1:3]) == [("org_a", 2), ("org_b", 1)]
    assert await origins(ana.enrolments.order_by("joined")[:2]) == BY_JOINED[:2]
    by_team = ana.enrolments.filter(team__isnull=False).order_by("team__name")
    assert await origins(by_team) == [("org_b", 1), ("org_a", 2), ("org_a", 1), ("org_b", 4)]
    assert await origins(ana.enrolments.filter(fee__gte=7).order_by("-fee")[:2]) == [("org_a", 2), ("org_a", 1)]


async def test_count_exists_and_aggregates_across_tenants(ana, people):
    assert await ana.enrolments.count() == 5
    assert await ana.enrolments.filter(role="coach").count() == 2
    assert await ana.enrolments.exists()
    assert not await ana.enrolments.filter(role="nobody").exists()
    assert await ana.enrolments.order_by("joined")[1:].count() == 4
    result = await ana.enrolments.aggregate(Sum("fee"), Max("joined"), n=Count("id", filter=Q(role="coach")))
    assert result == {"fee__sum": Decimal("43.75"), "joined__max": datetime.date(2024, 4, 1), "n": 2}
    per_schema = {}
    for schema in ("org_a", "org_b"):
        query = Query("tenancy.Enrolment").filter(person_id=people["Ana"]).in_tenant_schema(schema, TENANT_MODELS)
        per_schema[schema] = await adorm.current_session().execute(CountOperation(query))
    assert per_schema == {"org_a": 2, "org_b": 3}
    assert sum(per_schema.values()) == await ana.enrolments.count()


async def test_values_and_values_list_carry_no_hidden_columns(ana):
    rows = [row async for row in ana.enrolments.values("role").order_by("joined")]
    assert rows == [{"role": role} for role in ("coach", "coach", "member", "member", "board")]
    fees = [fee async for fee in ana.enrolments.values_list("fee", flat=True)]
    assert sorted(fees) == sorted(fee for _, _, _, fee in ANA.values())
    named = [row async for row in ana.enrolments.values_list("role", "fee", named=True).order_by("-fee")]
    assert [tuple(row) for row in named] == [
        ("member", Decimal("20.00")),
        ("coach", Decimal("10.00")),
        ("coach", Decimal("7.25")),
        ("member", Decimal("5.50")),
        ("board", Decimal("1.00")),
    ]
    assert named[0]._fields == ("role", "fee")
    tuples = [row async for row in ana.enrolments.values_list("id", "role").order_by("joined")]
    assert tuples == [(4, "coach"), (1, "coach"), (1, "member"), (2, "member"), (5, "board")]


async def test_select_related_joins_the_team_of_the_same_schema(ana, seen):
    rows = [row async for row in ana.enrolments.select_related("team").filter(team__isnull=False)]
    sent = len(seen)
    names = {origin(row): (await row.team).name for row in rows}
    assert len(seen) == sent, "the teams come from the joined rows"
    assert names == {
        ("org_a", 1): "Sailors A",
        ("org_a", 2): "Rowers A",
        ("org_b", 1): "Climbers B",
        ("org_b", 4): "Sailors B",
    }
    for row in rows:
        assert (await row.team)._state.tenant_schema.schema == row._state.tenant_schema.schema


async def test_a_filter_through_the_shared_foreign_key(ana):
    assert await ana.enrolments.filter(person__name="Ana").count() == 5
    assert await ana.enrolments.filter(person__name="Bor").count() == 0


async def test_first_last_and_get(ana):
    assert origin(await ana.enrolments.order_by("joined").first()) == BY_JOINED[0]
    assert origin(await ana.enrolments.order_by("joined").last()) == BY_JOINED[-1]
    assert origin(await ana.enrolments.get(pk=2)) == ("org_a", 2)
    assert origin(await ana.enrolments.get(role="board")) == ("org_b", 5)
    with pytest.raises(Enrolment.MultipleObjectsReturned):
        await ana.enrolments.get(pk=1)
    with pytest.raises(Enrolment.DoesNotExist):
        await ana.enrolments.get(pk=3)
    assert await ana.enrolments.filter(role="nobody").first() is None


async def test_a_schema_without_rows_of_the_person(connect, people):
    connect()
    cene = await person("Cene", people)
    assert await cene.enrolments.schemas() == ["org_c"]
    assert await origins(cene.enrolments) == []
    assert await cene.enrolments.count() == 0
    bor = await person("Bor", people)
    assert sorted(await origins(bor.enrolments)) == [("org_a", 3), ("org_c", 6)]


async def test_the_read_is_two_operations_on_one_slot(connect, people, seen):
    pool = connect(max_connections=1)
    ana = await person("Ana", people)
    seen.clear()
    rows = [row async for row in ana.enrolments.filter(role="member").order_by("joined")]
    assert len(rows) == 2
    assert len(seen) == 2
    listing, union = seen
    assert listing.model == "tenancy.Person"
    assert listing.schemas == frozenset()
    assert union.model == "tenancy.Enrolment"
    assert union.query.combinator == "union"
    assert union.query.combinator_all
    assert len(union.query.combined_queries) == 2
    assert union.schemas == frozenset({"org_a", "org_b"})
    assert pool.in_use == 0


async def test_an_empty_listing_sends_no_union(connect, people, seen):
    connect()
    nobody = await person("Nobody", people)
    for read, expected in (
        (lambda: origins(nobody.enrolments), []),
        (lambda: nobody.enrolments.count(), 0),
        (lambda: nobody.enrolments.exists(), False),
        (lambda: nobody.enrolments.first(), None),
        (lambda: nobody.enrolments.order_by("joined").last(), None),
        (lambda: nobody.enrolments.schemas(), []),
        (lambda: nobody.enrolments.aggregate(n=Count("id"), total=Sum("fee")), {"n": 0, "total": None}),
        (lambda: nobody.enrolments.aggregate(total=Sum("fee", default=0)), {"total": 0}),
    ):
        seen.clear()
        assert await read() == expected
        assert [operation.model for operation in seen] == ["tenancy.Person"]
    seen.clear()
    with pytest.raises(Enrolment.DoesNotExist):
        await nobody.enrolments.get(pk=1)
    assert len(seen) == 1


async def test_a_schema_that_does_not_exist_raises_the_database_error(connect, people):
    connect()
    lost = await person("Lost", people)
    assert await lost.enrolments.schemas() == ["org_a", "org_missing"]
    with pytest.raises(OperationalError if on_sqlite() else ProgrammingError):
        [row async for row in lost.enrolments]
    with pytest.raises(DatabaseError):
        await lost.enrolments.count()


async def test_an_invalid_schema_name_is_refused_before_the_union(connect, people, seen):
    connect()
    evil = await person("Evil", people)
    seen.clear()
    with pytest.raises(ValueError):  # noqa: PT011 - the message is not part of the contract
        [row async for row in evil.enrolments]
    assert [operation.model for operation in seen] == ["tenancy.Person"]


# Returned instances


async def test_a_returned_instance_saves_to_its_own_schema(ana, tenants):
    enrolment = await ana.enrolments.filter(team_id=3).get()
    assert origin(enrolment) == ("org_b", 1)
    enrolment.role = "coach"
    await enrolment.save()
    assert await tenants.enrolment("org_b", 1) == (3, "coach", Decimal("5.50"))
    assert await tenants.enrolment("org_a", 1) == (1, "coach", Decimal("10.00"))
    enrolment.fee = Decimal("6.00")
    await enrolment.save(update_fields=["fee"])
    assert await tenants.enrolment("org_b", 1) == (3, "coach", Decimal("6.00"))
    assert await tenants.enrolment("org_a", 1) == (1, "coach", Decimal("10.00"))


async def test_a_returned_instance_saves_to_its_own_schema_on_another_session(ana, tenants):
    enrolment = await ana.enrolments.get(pk=2)
    sent = []
    other = adorm.create_session("default", "other")

    class Recorder:
        async def execute(self, operation):
            sent.append(operation)
            return await other.execute(operation)

    row = {"id": enrolment.pk, "person_id": enrolment.person_id, "team_id": enrolment.team_id, "role": "board"}
    row |= {"joined": enrolment.joined, "fee": enrolment.fee}
    copy = Enrolment.from_row(row, executor=Recorder(), tenant_schema=enrolment._state.tenant_schema)
    copy.fee = Decimal("21.00")
    await copy.save()
    assert [operation.kind for operation in sent] == ["save"]
    assert sent[0].schemas == frozenset({"org_a"})
    assert await tenants.enrolment("org_a", 2) == (2, "board", Decimal("21.00"))
    assert await tenants.fetch("org_b", "SELECT id FROM {enrolment} WHERE id = 2") == []


async def test_a_returned_instance_deletes_from_its_own_schema(ana, tenants):
    enrolment = await ana.enrolments.filter(team_id=1, role="coach", fee=Decimal("10.00")).get()
    assert origin(enrolment) == ("org_a", 1)
    await enrolment.delete()
    assert await tenants.enrolment("org_a", 1) is None
    assert await tenants.enrolment("org_b", 1) == (3, "member", Decimal("5.50"))
    assert sorted(await origins(ana.enrolments)) == [("org_a", 2), ("org_b", 1), ("org_b", 4), ("org_b", 5)]


async def test_a_delete_cascades_within_its_schema(ana, tenants):
    enrolment = await ana.enrolments.filter(team__name="Sailors A").get()
    team = await enrolment.team
    assert (team.pk, team._state.tenant_schema.schema) == (1, "org_a")
    on_delete = dj.Enrolment._meta.get_field("team").remote_field.on_delete
    if on_delete not in (django_models.CASCADE, django_models.SET_NULL):
        pytest.skip("Enrolment.team neither cascades nor sets null")
    await team.delete()
    assert await tenants.fetch("org_a", "SELECT id FROM {team} ORDER BY id") == [(2,)]
    assert await tenants.fetch("org_b", "SELECT id FROM {team} ORDER BY id") == [(1,), (3,)]
    assert await tenants.fetch("org_c", "SELECT id FROM {team} ORDER BY id") == [(1,)]
    left = await tenants.fetch("org_a", "SELECT id, team_id FROM {enrolment} ORDER BY id")
    if on_delete is django_models.CASCADE:
        assert left == [(2, 2)]
    else:
        assert left == [(1, None), (2, 2), (3, None)]
    assert await tenants.enrolment("org_b", 4) == (1, "coach", Decimal("7.25"))
    assert await tenants.enrolment("org_c", 6) == (1, "member", Decimal("3.00"))


async def test_refresh_from_db_reads_its_own_schema(ana, tenants):
    enrolment = await ana.enrolments.filter(team_id=3).get()
    await tenants.execute("org_b", "UPDATE {enrolment} SET fee = 8.75 WHERE id = 1")
    await tenants.execute("org_a", "UPDATE {enrolment} SET fee = 99.99 WHERE id = 1")
    await enrolment.refresh_from_db()
    assert enrolment.fee == Decimal("8.75")
    assert enrolment._state.tenant_schema.schema == "org_b"


async def test_full_clean_checks_uniqueness_inside_its_schema(ana):
    if not dj.Team._meta.get_field("name").unique:
        pytest.skip("Team.name is not unique")
    team = await (await ana.enrolments.filter(team_id=1, role="coach", fee=Decimal("7.25")).get()).team
    assert (team.name, team._state.tenant_schema.schema) == ("Sailors B", "org_b")
    team.name = "Rowers A"
    await team.full_clean()
    team.name = "Climbers B"
    with pytest.raises(ValidationError):
        await team.full_clean()


async def test_relations_of_a_returned_instance(ana):
    in_b = await ana.enrolments.filter(team_id=3).get()
    in_a = await ana.enrolments.filter(team_id=1, fee=Decimal("10.00")).get()
    assert (await in_b.team).name == "Climbers B"
    assert (await in_a.team).name == "Sailors A"
    shared = await in_b.person
    assert shared.name == "Ana"
    assert shared._state.tenant_schema is None


async def test_the_same_primary_key_in_two_schemas_is_two_instances(ana):
    rows = [row async for row in ana.enrolments]
    in_a = next(row for row in rows if origin(row) == ("org_a", 1))
    in_b = next(row for row in rows if origin(row) == ("org_b", 1))
    assert in_a != in_b
    assert hash(in_a) != hash(in_b)
    assert len(set(rows)) == 5
    again = await ana.enrolments.filter(team_id=3).get()
    assert again == in_b
    assert hash(again) == hash(in_b)


# Refusals


async def _call(make):
    result = make()
    if inspect.isawaitable(result):
        return await result
    return result


async def test_writes_and_unsupported_methods_are_refused_without_an_operation(ana, seen):
    enrolments = ana.enrolments
    seen.clear()
    for make in (
        lambda: enrolments.create(role="coach", joined=datetime.date(2024, 1, 1), fee=Decimal(1)),
        lambda: enrolments.bulk_create([]),
        lambda: enrolments.get_or_create(role="coach"),
        lambda: enrolments.update_or_create(role="coach"),
        lambda: enrolments.create_from_parent(ana),
        lambda: enrolments.update(role="coach"),
        lambda: enrolments.delete(),
        lambda: enrolments.in_bulk([1]),
        lambda: enrolments.add(),
        lambda: enrolments.remove(),
        lambda: enrolments.clear(),
        lambda: enrolments.set([]),
        lambda: enrolments.select_for_update(),
        lambda: enrolments.distinct(),
        lambda: enrolments.prefetch_related("team"),
        lambda: enrolments.union(enrolments),
        lambda: enrolments.intersection(enrolments),
        lambda: enrolments.difference(enrolments),
        lambda: enrolments | enrolments,
        lambda: enrolments & enrolments,
        lambda: enrolments.annotate(n=Count("id")),
    ):
        with pytest.raises(NotSupportedError, match="not supported on a tenant relation|aggregate"):
            await _call(make)
    with pytest.raises(ValueError):  # noqa: PT011 - the message is not part of the contract
        enrolments.annotate(_adorm_x=F("role"))
    assert seen == []


async def test_the_memory_driver_refuses_tenant_schemas(conformance_driver, make_router, driver):  # noqa: ARG001
    if conformance_driver.name != "memory":
        pytest.skip("the SQL drivers support tenant schemas")
    make_router(max_connections=1)
    query = Query("tenancy.Enrolment").in_tenant_schema("org_a", TENANT_MODELS)
    with pytest.raises(NotSupportedError, match="clause.tenant_schema"):
        await adorm.current_session().execute(CountOperation(query))


async def test_an_aggregate_over_a_union_of_values(conformance_driver, router, driver):  # noqa: ARG001
    from django.db.models import Count as DjangoCount, Sum as DjangoSum

    from tests.djangoproject.clubs.models import Membership as DjangoMembership

    coaches = Membership.objects.filter(role="coach").values("fee")
    board = Membership.objects.filter(role="board").values("fee")
    combined = coaches.union(board, all=True)
    if conformance_driver.name == "memory":
        with pytest.raises(NotSupportedError, match="aggregate.combined"):
            await combined.aggregate(total=Sum("fee"), n=Count("fee"))
        return
    expected = await in_thread(
        lambda: DjangoMembership.objects.filter(role__in=["coach", "board"]).aggregate(
            total=DjangoSum("fee"), n=DjangoCount("fee")
        )
    )
    assert await combined.aggregate(total=Sum("fee"), n=Count("fee")) == expected


# check_operation


class NoTenantError(Exception):
    """A tenant's table without a tenant."""


def refuse_unqualified_tenant_tables(operation, session):  # noqa: ARG001 - the hook's signature
    unqualified = operation.unqualified_models
    if unqualified is None or unqualified & set(TENANT_MODELS):
        raise NoTenantError(f"{operation.kind} of {operation.model} needs a tenant")


async def test_check_operation_sees_the_tenant_model_and_the_explicit_schemas(connect, people, seen):
    pool = connect(refuse_unqualified_tenant_tables)
    ana = await person("Ana", people)
    seen.clear()
    rows = [row async for row in ana.enrolments.select_related("team").filter(person__name="Ana")]
    assert len(rows) == 5
    listing, union = seen
    assert listing.schemas == frozenset()
    assert listing.unqualified_models == listing.models
    assert listing.models <= SHARED_MODELS
    assert union.schemas == frozenset({"org_a", "org_b"})
    assert {"tenancy.Enrolment", "tenancy.Team"} <= union.models
    assert union.unqualified_models == frozenset({"tenancy.Person"})
    assert await ana.enrolments.count() == 5
    assert (await ana.enrolments.aggregate(total=Sum("fee")))["total"] == Decimal("43.75")

    enrolment = rows[0]
    enrolment.fee = Decimal("2.00")
    await enrolment.save()
    assert seen[-1].kind == "save"
    assert seen[-1].schemas == frozenset({enrolment._state.tenant_schema.schema})
    assert not seen[-1].unqualified_models & set(TENANT_MODELS)

    unscoped = Enrolment(person_id=ana.pk, role="coach", joined=datetime.date(2024, 1, 1), fee=Decimal(1))
    for make in (lambda: Enrolment.objects.count(), lambda: unscoped.save(), lambda: Team.objects.exists()):
        in_use = pool.in_use
        with pytest.raises(NoTenantError):
            await make()
        assert pool.in_use == in_use
    assert await Organisation.objects.count() == len(ORGANISATIONS)
    assert await PersonOrganisation.objects.filter(person_id=ana.pk).count() == 2


# Operations without I/O


def test_operations_name_their_schemas_and_unqualified_models():
    branch = Query("tenancy.Enrolment").filter(person__name="Ana")
    in_a = branch.in_tenant_schema("org_a", TENANT_MODELS)
    assert in_a.tenant_schema == TenantSchema("org_a", TENANT_MODELS)
    assert in_a.in_tenant_schema(None, ()).tenant_schema is None
    select = Select(in_a.union(branch.in_tenant_schema("org_b", TENANT_MODELS), all=True))
    assert select.schemas == frozenset({"org_a", "org_b"})
    assert "tenancy.Enrolment" in select.models
    assert select.unqualified_models == frozenset({"tenancy.Person"})
    plain = Select(branch)
    assert plain.schemas == frozenset()
    assert plain.unqualified_models == plain.models
    assert "clause.tenant_schema" in set(select.constructs())
    assert Operation.from_dict(select.to_dict()) == select

    tenant_schema = TenantSchema("org_b", TENANT_MODELS)
    save = Save("tenancy.Enrolment", 1, {"role": "coach"}, tenant_schema=tenant_schema)
    assert save.schemas == frozenset({"org_b"})
    assert not save.unqualified_models & set(TENANT_MODELS)
    assert Operation.from_dict(save.to_dict()) == save
    delete = DeleteInstance("tenancy.Team", 1, tenant_schema=tenant_schema)
    assert delete.schemas == frozenset({"org_b"})
    assert not (delete.unqualified_models or frozenset()) & set(TENANT_MODELS)
    with pytest.raises(NotSupportedError):
        Update(in_a, {"role": "coach"})


@pytest.mark.parametrize("schema", ["", 'org"x', "org`x", "org[x", "org]x", "org\x00x", "org\nx"])
def test_a_schema_name_with_quoting_characters_is_refused(schema):
    with pytest.raises(ValueError):  # noqa: PT011 - the message is not part of the contract
        TenantSchema(schema, TENANT_MODELS)


async def test_slices_aggregates_deferred_fields_annotations_and_subqueries_across_tenants(ana):
    from adorm import Exists, OuterRef

    assert (await ana.enrolments.order_by("joined")[:2].aggregate(Sum("fee")))["fee__sum"] == Decimal("17.25")
    assert (await ana.enrolments.aggregate(n=Count("*")))["n"] == 5
    assert (await ana.enrolments.aggregate(t=Count("team", distinct=True)))["t"] == 3
    rows = [e async for e in ana.enrolments.only("role").order_by("joined")]
    assert [origin(e) for e in rows] == BY_JOINED
    rows = [e async for e in ana.enrolments.annotate(double=F("fee") * 2).order_by("-double")]
    assert rows[0].double == Decimal("40.00")
    teams = Team.objects.filter(pk=OuterRef("team_id"), name__in=["Sailors A", "Sailors B"])
    rows = [e async for e in ana.enrolments.filter(Exists(teams)).order_by("joined")]
    assert [origin(e) for e in rows] == [("org_b", 4), ("org_a", 1)]
    rows = [e async for e in ana.enrolments.select_related("person").order_by("joined")]
    assert {(await e.person).name for e in rows} == {"Ana"}
    names = [n async for n in ana.enrolments.values_list("team__name", flat=True).order_by("team__name")]
    assert [n for n in names if n is not None] == ["Climbers B", "Rowers A", "Sailors A", "Sailors B"]


async def test_one_schema_is_read_without_a_union(connect, tenants):
    def add():
        organisation = dj.Organisation.objects.get(schema_name="org_c")
        solo = dj.Person.objects.create(name="Solo")
        dj.PersonOrganisation.objects.create(person=solo, organisation=organisation)
        return solo.pk

    pk = await in_thread(add)
    await tenants.execute(
        "org_c",
        f"INSERT INTO {{enrolment}} (id, person_id, team_id, role, joined, fee) VALUES (7, {pk}, 1, 'coach', "
        "'2024-06-01', 4.00), (8, " + str(pk) + ", NULL, 'member', '2024-07-01', 2.00)",
    )
    connect()
    solo = await Person.objects.get(pk=pk)
    assert [origin(e) async for e in solo.enrolments.order_by("-joined")] == [("org_c", 8), ("org_c", 7)]
    assert await solo.enrolments.count() == 2
    result = await solo.enrolments.aggregate(Sum("fee"), n=Count("id", filter=Q(role="coach")))
    assert result == {"fee__sum": Decimal("6.00"), "n": 1}
    assert [row async for row in solo.enrolments.values_list("role", flat=True).order_by("joined")] == [
        "coach",
        "member",
    ]
    first = await solo.enrolments.select_related("team").order_by("joined").first()
    assert (await first.team).name == "C team"


async def test_writes_of_a_query_set_in_a_tenant_schema_are_refused(ana, tenants, seen):
    enrolment = await ana.enrolments.filter(team__isnull=False).order_by("joined").first()
    team = await enrolment.team
    related = team.enrolment_set
    new = Enrolment(person_id=ana.pk, team_id=team.pk, role="x", joined=datetime.date(2025, 1, 1), fee=Decimal(1))
    seen.clear()
    for write in (
        lambda: related.create(person_id=ana.pk, role="x", joined=datetime.date(2025, 1, 1), fee=Decimal(1)),
        lambda: related.bulk_create([new]),
        lambda: related.update(role="x"),
        lambda: related.delete(),
        lambda: related.get_or_create(role="x"),
    ):
        with pytest.raises(NotSupportedError, match="tenant schema"):
            await write()
    assert seen == []
    assert await tenants.fetch("org_a", "SELECT id FROM {enrolment} WHERE role = 'x'") == []


async def test_a_returned_instance_inserts_into_its_own_schema(ana, tenants):
    enrolment = await ana.enrolments.order_by("joined").first()
    schema = enrolment._state.tenant_schema.schema
    copy = Enrolment.from_row(
        {
            "id": 100,
            "person_id": ana.pk,
            "team_id": None,
            "role": "copy",
            "joined": datetime.date(2025, 1, 1),
            "fee": Decimal(3),
        },
        tenant_schema=enrolment._state.tenant_schema,
    )
    await copy.save(force_insert=True)
    assert copy._state.tenant_schema == enrolment._state.tenant_schema
    assert await tenants.fetch(schema, "SELECT role FROM {enrolment} WHERE role = 'copy'") == [("copy",)]
    other = "org_a" if schema == "org_b" else "org_b"
    assert await tenants.fetch(other, "SELECT role FROM {enrolment} WHERE role = 'copy'") == []
