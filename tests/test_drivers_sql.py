import pytest

from adorm.drivers.sql import numbered_placeholders, POSTGRESQL_OPTIONS, set_options_sql, Statement


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 1", "SELECT 1"),
        ("SELECT %s", "SELECT $1"),
        ('SELECT "a" FROM "t" WHERE "a" = %s AND "b" > %s', 'SELECT "a" FROM "t" WHERE "a" = $1 AND "b" > $2'),
        # Django's pattern escaping for `contains` with an expression
        ("REPLACE(%s, E'%%', E'\\\\%%')", "REPLACE($1, E'%', E'\\\\%')"),
        ("%%s", "%s"),
        ("%%%s", "%$1"),
        ("%s%%", "$1%"),
        # placeholders are numbered inside quotes too, as psycopg formats them
        ("SELECT '%s', \"%s\"", "SELECT '$1', \"$2\""),
    ],
)
def test_numbered_placeholders(sql, expected):
    assert numbered_placeholders(sql) == expected


def test_numbered_placeholders_beyond_nine():
    sql = ", ".join(["%s"] * 12)
    assert numbered_placeholders(sql) == ", ".join(f"${n}" for n in range(1, 13))


def test_numbered_placeholders_with_casts():
    sql = "SELECT %s, %s, %s"
    assert numbered_placeholders(sql, ["int4", None, "jsonb"]) == "SELECT $1::int4, $2, $3::jsonb"


@pytest.mark.parametrize("sql", ["SELECT %d", "SELECT %(name)s", "SELECT 5 %", "LIKE 'a%b'"])
def test_numbered_placeholders_rejects_other_percent_sequences(sql):
    with pytest.raises(ValueError, match="unsupported placeholder"):
        numbered_placeholders(sql)


@pytest.mark.parametrize("casts", [[], ["int4"], ["int4", None, None]])
def test_numbered_placeholders_rejects_cast_count_mismatch(casts):
    with pytest.raises(ValueError, match="casts for"):
        numbered_placeholders("SELECT %s, %s", casts)


def test_statement_is_frozen_and_compares_by_value():
    statement = Statement("SELECT $1", (1,))
    assert statement == Statement("SELECT $1", (1,))
    assert Statement("SELECT 1").params == ()
    with pytest.raises(AttributeError):
        statement.sql = "SELECT 2"


def test_set_options_sql():
    assert set_options_sql({"statement_timeout": 5, "lock_timeout": 0.25}, local=False) == (
        "SET statement_timeout = 5000; SET lock_timeout = 250"
    )
    assert set_options_sql({"idle_in_transaction_session_timeout": 0}, local=True) == (
        "SET LOCAL idle_in_transaction_session_timeout = 0"
    )
    assert set_options_sql({"statement_timeout": None}, local=False) == "RESET statement_timeout"
    assert set_options_sql({"statement_timeout": None}, local=True) == "SET LOCAL statement_timeout TO DEFAULT"
    assert set_options_sql({"lock_timeout": 0.0001, "statement_timeout": 0.3}, local=False) == (
        "SET lock_timeout = 1; SET statement_timeout = 300"
    )
    assert set_options_sql({}, local=False) == ""
    assert set_options_sql({"lock_timeout": 2147483.647}, local=False) == "SET lock_timeout = 2147483647"
    assert {"statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout", "role"} == POSTGRESQL_OPTIONS


def test_set_options_sql_for_role_and_custom_options():
    assert set_options_sql({"role": 'Al"ice', "app.user_id": "7"}, local=False) == (
        """SET role = "Al""ice"; SET app.user_id = '7'"""
    )
    assert set_options_sql({"role": None, "app.user_id": None}, local=False) == "RESET role; RESET app.user_id"
    assert set_options_sql({"role": None, "app.user_id": None}, local=True) == (
        "SET LOCAL role TO DEFAULT; SET LOCAL app.user_id TO DEFAULT"
    )
    assert set_options_sql({"a.b.c": "o'neil"}, local=True) == "SET LOCAL a.b.c = 'o''neil'"
    assert set_options_sql({"a.b": "back\\slash'"}, local=False) == "SET a.b = E'back\\\\slash''" + "'"
    assert set_options_sql({"a.b": ""}, local=False) == "SET a.b = ''"


@pytest.mark.parametrize(
    "options",
    [
        {"work_mem": "1MB"},
        {"statement_timeout": -1},
        {"lock_timeout": True},
        {"lock_timeout": float("inf")},
        {"statement_timeout": 2147483.648},
        {"role": ""},
        {"role": 5},
        {"role": "a\x00"},
        {"app.user_id": 7},
        {"app.user_id": "a\x00"},
        {"App.user": "x"},
        {"app.user-id": "x"},
    ],
)
def test_set_options_sql_refuses_other_names_and_values(options):
    with pytest.raises(ValueError, match="work_mem|seconds|role|string|PostgreSQL connection option"):
        set_options_sql(options, local=False)
