"""PostgreSQL's description of a violated constraint, read from psycopg's and asyncpg's errors, and the comparison of
a table's columns in the catalogue with what a model expects."""

import pytest

from django.db import IntegrityError as DjangoIntegrityError

from adorm.drivers.postgresql import (
    columns_sql,
    ExpectedColumn,
    ExpectedTable,
    integrity_details,
    normalize_type,
    parse_key_detail,
    references_key,
    table_problems,
)
from adorm.exceptions import DataError, IntegrityError, wrap_driver_error


@pytest.mark.parametrize(
    ("detail", "columns", "values"),
    [
        ("Key (id)=(1) already exists.", ("id",), ("1",)),
        ("Key (club_id, member_id)=(3, 17) already exists.", ("club_id", "member_id"), ("3", "17")),
        ('Key ("Code", name)=(A, ana) already exists.', ("Code", "name"), ("A", "ana")),
        ('Key ("a, b", "say ""hi""")=(1, 2) already exists.', ("a, b", 'say "hi"'), ("1", "2")),
        (
            "Key (lower(name::text), coalesce(code, 'x, y'::text))=(ana, A) already exists.",
            ("lower(name::text)", "coalesce(code, 'x, y'::text)"),
            ("ana", "A"),
        ),
        ("Key (name, city)=(Koper, Sailing, Koper) already exists.", ("name", "city"), ()),
        ("Key (name)=(Koper, Sailing) already exists.", ("name",), ("Koper, Sailing",)),
        ('Key (parent_id)=(9, 3) is not present in table "parent".', ("parent_id",), ("9, 3",)),
        ("Key (name)=(a)=(b) already exists.", ("name",), ("a)=(b",)),
        ("Key (name)=() already exists.", ("name",), ("",)),
        ("Key (name)=(line\nbreak) already exists.", ("name",), ("line\nbreak",)),
        ('Key (club_id)=(9) is not present in table "clubs_club".', ("club_id",), ("9",)),
        ('Key (id)=(1) is still referenced from table "clubs_membership".', ("id",), ("1",)),
        ("Key (during)=([3,8)) conflicts with existing key (during)=([1,5)).", ("during",), ("[3,8)",)),
        ("Key (during)=([3,8)) conflicts with key (during)=([1,5)).", ("during",), ("[3,8)",)),
        ('Key is not present in table "clubs_club".', (), ()),
        ("Failing row contains (3, null).", (), ()),
        ("Schlüssel (id)=(1) existiert bereits.", (), ()),
        ("Key (id)=(1) is something new.", (), ()),
        ("", (), ()),
        (None, (), ()),
    ],
)
def test_parse_key_detail(detail, columns, values):
    assert parse_key_detail(detail) == (columns, values)


class FakeDiag:
    def __init__(self, **fields):
        self.__dict__.update(fields)

    def __getattr__(self, name):
        return None


class FakePsycopgError(Exception):
    def __init__(self, sqlstate, **fields):
        super().__init__("psycopg")
        self.sqlstate = sqlstate
        self.diag = FakeDiag(**fields)


class FakeAsyncpgError(Exception):
    constraint_name = table_name = schema_name = column_name = detail = None

    def __init__(self, sqlstate, **fields):
        super().__init__("asyncpg")
        self.sqlstate = sqlstate
        self.__dict__.update(fields)


UNIQUE = {
    "constraint_name": "clubs_membership_unique",
    "table_name": "clubs_membership",
    "schema_name": "public",
    "columns": ("club_id", "member_id"),
    "values": ("3", "17"),
    "detail": "Key (club_id, member_id)=(3, 17) already exists.",
}


def test_integrity_details_of_psycopg_diag():
    exc = FakePsycopgError(
        "23505",
        constraint_name="clubs_membership_unique",
        table_name="clubs_membership",
        schema_name="public",
        message_detail="Key (club_id, member_id)=(3, 17) already exists.",
    )
    assert integrity_details(exc) == UNIQUE


def test_integrity_details_of_asyncpg_attributes():
    exc = FakeAsyncpgError(
        "23505",
        constraint_name="clubs_membership_unique",
        table_name="clubs_membership",
        schema_name="public",
        detail="Key (club_id, member_id)=(3, 17) already exists.",
    )
    assert integrity_details(exc) == UNIQUE


def test_integrity_details_follow_the_cause_chain():
    cause = FakePsycopgError("23502", table_name="clubs_member", column_name="email")
    exc = DjangoIntegrityError("null value")
    exc.__cause__ = cause
    details = integrity_details(exc)
    assert details["table_name"] == "clubs_member"
    assert (details["columns"], details["values"], details["constraint_name"]) == (("email",), (), None)
    wrapped = wrap_driver_error(exc, details=integrity_details)
    assert type(wrapped) is IntegrityError
    assert (wrapped.original, wrapped.sqlstate, wrapped.columns) == (exc, "23502", ("email",))


def test_integrity_details_prefer_the_key_to_the_column():
    exc = FakeAsyncpgError("23505", column_name="ignored", detail="Key (id)=(1) already exists.")
    assert integrity_details(exc)["columns"] == ("id",)


def test_integrity_details_ignore_values_that_are_not_text():
    exc = FakeAsyncpgError("23505", constraint_name=42, table_name=b"t", detail=object())
    assert integrity_details(exc) == {
        "constraint_name": None,
        "table_name": None,
        "schema_name": None,
        "columns": (),
        "values": (),
        "detail": None,
    }


def test_integrity_details_without_sqlstate():
    assert integrity_details(DjangoIntegrityError("no cause")) == {}
    assert integrity_details(KeyError("x")) == {}


def test_integrity_details_survive_a_cause_cycle():
    first, second = KeyError("a"), KeyError("b")
    first.__cause__, second.__cause__ = second, first
    assert integrity_details(first) == {}


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ('Key (id)=(1) is still referenced from table "clubs_membership".', True),
        ('Key (club_id)=(9) is not present in table "clubs_club".', False),
        ("Key (id)=(1) already exists.", False),
        ("Failing row contains (3, null).", False),
        (None, False),
    ],
)
def test_references_key(detail, expected):
    assert references_key(detail) is expected


def test_wrap_with_integrity_details_describes_only_integrity_errors():
    wrapped = wrap_driver_error(FakeAsyncpgError("22001", detail="Key (id)=(1) already exists."), integrity_details)
    assert type(wrapped) is DataError
    assert not hasattr(wrapped, "detail")
    wrapped = wrap_driver_error(
        FakeAsyncpgError("23505", constraint_name="c", detail="Key (id)=(1) already exists."), integrity_details
    )
    assert (wrapped.constraint_name, wrapped.key) == ("c", {"id": "1"})


@pytest.mark.parametrize(
    ("declared", "printed"),
    [
        ("integer", "integer"),
        ("serial", "integer"),
        ("bigserial", "bigint"),
        ("int8", "bigint"),
        ("smallint", "smallint"),
        ("bool", "boolean"),
        ("varchar(100)", "character varying(100)"),
        ("VARCHAR", "character varying"),
        ("char(3)", "character(3)"),
        ("numeric(8, 2)", "numeric(8,2)"),
        ("decimal(8,2)", "numeric(8,2)"),
        ("float8", "double precision"),
        ("double precision", "double precision"),
        ("timestamp with time zone", "timestamp with time zone"),
        ("timestamptz", "timestamp with time zone"),
        ("timestamp", "timestamp without time zone"),
        ("timestamp(3)", "timestamp(3) without time zone"),
        ("time", "time without time zone"),
        ("time without time zone", "time without time zone"),
        ("varchar(30)[10]", "character varying(30)[]"),
        ("integer[]", "integer[]"),
        ("time[]", "time without time zone[]"),
        ("jsonb", "jsonb"),
        ("interval", "interval"),
        ("uuid", "uuid"),
    ],
)
def test_normalize_type_spells_types_as_format_type(declared, printed):
    assert normalize_type(declared) == printed
    assert normalize_type(printed) == printed


@pytest.mark.parametrize(("one", "other"), [("integer", "bigint"), ("varchar(10)", "varchar(11)"), ("text", "jsonb")])
def test_normalize_type_keeps_different_types_apart(one, other):
    assert normalize_type(one) != normalize_type(other)


def test_columns_sql_binds_the_table_name():
    assert "to_regclass($1::text)" in columns_sql("$1")
    assert "to_regclass(%s::text)" in columns_sql("%s")


TABLE = ExpectedTable(
    "club",
    '"club"',
    (
        ExpectedColumn("id", "bigint", False, "id"),
        ExpectedColumn("name", "varchar(100)", False, "name"),
        ExpectedColumn("note", "text", True, "note"),
        ExpectedColumn("owner_id", None, True, "owner_id"),
        ExpectedColumn("gone", "text", False, "gone"),
    ),
)


def test_table_problems():
    rows = [
        (True, "id", "bigint", True, True),
        (True, "name", "character varying(80)", True, False),
        (True, "note", "text", True, False),
        (True, "owner_id", "uuid", False, False),
        (True, "required", "integer", True, False),
        (True, "defaulted", "integer", True, True),
        (True, "optional", "integer", False, False),
    ]
    assert table_problems(TABLE, rows) == [
        {
            "kind": "column_type",
            "table": "club",
            "column": "name",
            "field": "name",
            "expected": "varchar(100)",
            "actual": "character varying(80)",
        },
        {
            "kind": "nullability",
            "table": "club",
            "column": "note",
            "field": "note",
            "expected": "NULL",
            "actual": "NOT NULL",
        },
        {
            "kind": "missing_column",
            "table": "club",
            "column": "gone",
            "field": "gone",
            "expected": None,
            "actual": None,
        },
        {
            "kind": "required_column",
            "table": "club",
            "column": "required",
            "field": None,
            "expected": None,
            "actual": "integer",
        },
    ]


def test_a_missing_table_is_one_problem():
    missing = [
        {"kind": "missing_table", "table": "club", "column": None, "field": None, "expected": None, "actual": None}
    ]
    assert table_problems(TABLE, [(False, None, None, None, None)]) == missing
    assert table_problems(TABLE, []) == missing
