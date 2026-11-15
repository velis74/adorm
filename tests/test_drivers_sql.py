import pytest

from adorm.drivers.sql import numbered_placeholders, Statement


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
