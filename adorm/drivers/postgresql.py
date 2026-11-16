"""What PostgreSQL reports about a violated constraint, read from the errors of psycopg (2 and 3) and asyncpg, about
the sessions of a database and in its notices, the statements that listen for notifications, and the comparison of a
table's columns in the catalogue with what a model expects of them."""

from __future__ import annotations

import re

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from adorm.drivers.base import BackendActivity
from adorm.exceptions import _sqlstate

__all__ = [
    "MAX_IDENTIFIER_LENGTH",
    "ExpectedColumn",
    "ExpectedTable",
    "activity_sql",
    "backend_activity",
    "columns_sql",
    "normalize_type",
    "table_problems",
    "integrity_details",
    "listen_sql",
    "notice_payload",
    "parse_key_detail",
    "references_key",
    "unlisten_sql",
]

MAX_IDENTIFIER_LENGTH = 63
"""The longest PostgreSQL identifier in bytes (``NAMEDATALEN - 1``), and so the longest notification channel name: the
``max_channel_length`` of the PostgreSQL drivers' `Capabilities`."""

# The key part of PostgreSQL's detail message of a unique, foreign key or exclusion violation.
_KEY_DETAIL = re.compile(
    r"Key \((?P<columns>.+?)\)=\((?P<values>.*)\)"
    r" (?P<reason>already exists|is not present in table \".*\"|is still referenced from table \".*\""
    r"|conflicts with (?:existing )?key \(.*\)=\(.*\))\.",
    re.DOTALL,
)


def _split_columns(text: str) -> list[str]:
    """``text`` split at top-level ``", "``: commas inside parentheses or quoted identifiers do not split."""
    parts: list[str] = []
    depth, quoted, start, i = 0, False, 0, 0
    while i < len(text):
        char = text[i]
        if char == '"':
            quoted = not quoted
        elif not quoted and char == "(":
            depth += 1
        elif not quoted and char == ")":
            depth -= 1
        elif not quoted and depth == 0 and text.startswith(", ", i):
            parts.append(text[start:i])
            start = i = i + 2
            continue
        i += 1
    parts.append(text[start:])
    return [_unquote(part) for part in parts]


def _unquote(name: str) -> str:
    if len(name) >= 2 and name[0] == name[-1] == '"' and '"' not in name[1:-1].replace('""', ""):
        return name[1:-1].replace('""', '"')
    return name


def parse_key_detail(detail: str | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The columns and values of the key in PostgreSQL's detail message ``Key (columns)=(values) ...``.

    Quoted column names are unquoted; an index expression stays as PostgreSQL prints it. The value of a key of one
    column is the whole text. The values of a key of several columns are empty when their text splits at ``", "``
    into a different number of parts than there are columns (a value contains ``", "``). Both are empty for a detail
    without a key, as of a not-null or check violation.
    """
    match = _KEY_DETAIL.match(detail or "")
    if match is None:
        return (), ()
    columns = tuple(_split_columns(match["columns"]))
    if len(columns) == 1:
        return columns, (match["values"],)
    values = tuple(match["values"].split(", "))
    return columns, values if len(values) == len(columns) else ()


def references_key(detail: str | None) -> bool:
    """Whether the key in PostgreSQL's detail message is a referenced row's (``Key (...)=(...) is still referenced
    from table ...``, a delete or update of the row): its columns are the referenced table's, not those of the
    violated constraint's table."""
    match = _KEY_DETAIL.match(detail or "")
    return match is not None and match["reason"].startswith("is still referenced")


def integrity_details(exc: BaseException) -> dict[str, Any]:
    """The keyword arguments of `adorm.IntegrityError` that describe the violation, read from the first exception with
    a SQLSTATE on ``exc``'s ``__cause__`` chain: psycopg's and psycopg2's ``diag``, asyncpg's attributes. ``{}`` when
    the chain has no SQLSTATE."""
    found = _sqlstate(exc)
    if found is None:
        return {}
    source = found[0]
    diag = getattr(source, "diag", None)
    if diag is not None:
        detail = getattr(diag, "message_detail", None)
    else:
        diag, detail = source, getattr(source, "detail", None)

    def text(name: str) -> str | None:
        value = getattr(diag, name, None)
        return value if isinstance(value, str) else None

    detail = detail if isinstance(detail, str) else None
    columns, values = parse_key_detail(detail)
    column = text("column_name")
    if not columns and column is not None:
        columns = (column,)
    return {
        "constraint_name": text("constraint_name"),
        "table_name": text("table_name"),
        "schema_name": text("schema_name"),
        "columns": columns,
        "values": values,
        "detail": detail,
    }


def activity_sql(parameter: str) -> str:
    """The query of the sessions whose pids are in the array bound to ``parameter`` (``"%s"`` for psycopg,
    ``"$1"`` for asyncpg): one row of `backend_activity()`'s arguments per session."""
    columns = "pid, state, wait_event_type, wait_event, pg_blocking_pids(pid)"
    return f"SELECT {columns} FROM pg_stat_activity WHERE pid = ANY({parameter}::integer[])"  # noqa: S608


def backend_activity(
    pid: int, state: str | None, wait_event_type: str | None, wait_event: str | None, blocking_pids: Any
) -> BackendActivity:
    """The `BackendActivity` of a row of `activity_sql()`."""
    return BackendActivity(int(pid), state, wait_event_type, wait_event, tuple(int(p) for p in blocking_pids or ()))


def notice_payload(
    severity: str | None, sqlstate: str | None, message: str | None, detail: str | None, hint: str | None
) -> dict[str, str | None]:
    """The payload of a `NOTICE` backend event: a PostgreSQL notice's untranslated severity, SQLSTATE, primary
    message, detail and hint, ``None`` for a field the server did not send."""
    return {"severity": severity, "sqlstate": sqlstate, "message": message, "detail": detail, "hint": hint}


def _quote_channel(channel: str) -> str:
    return '"' + channel.replace('"', '""') + '"'


def listen_sql(channel: str) -> str:
    """``LISTEN`` for ``channel``, quoted, so that the channel name is used exactly as given."""
    return f"LISTEN {_quote_channel(channel)}"


def unlisten_sql(channel: str) -> str:
    """``UNLISTEN`` for ``channel``, quoted as in `listen_sql()`."""
    return f"UNLISTEN {_quote_channel(channel)}"


@dataclass(frozen=True)
class ExpectedColumn:
    """A column a model expects: its ``name``, its ``type`` as the ORM declares it (``"varchar(100)"``), ``None`` to
    leave the type unchecked, whether it takes ``NULL`` (``nullable``) and the ORM name of its ``field``, ``None`` for a
    column of an automatic link table."""

    name: str
    type: str | None
    nullable: bool
    field: str | None = None


@dataclass(frozen=True)
class ExpectedTable:
    """A table a model expects: its ``name`` as the ORM names it, the same name quoted for ``to_regclass()``
    (``regclass``) and its ``columns``."""

    name: str
    regclass: str
    columns: tuple[ExpectedColumn, ...]


def columns_sql(parameter: str) -> str:
    """The query of the columns of the table whose quoted name is bound to ``parameter`` (``"%s"`` for psycopg,
    ``"$1"`` for asyncpg), resolved on the search path. Each row is ``(table exists, column, type, not null, has a
    default)``, the type as ``format_type()`` prints it, an identity or generated column counting as one with a
    default; a missing table gives one row ``(False, None, None, None, None)``."""
    return (
        "SELECT t.oid IS NOT NULL, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,"  # noqa: S608
        " a.atthasdef OR a.attidentity <> '' OR a.attgenerated <> ''"
        f" FROM (SELECT to_regclass({parameter}::text) AS oid) AS t"
        " LEFT JOIN pg_attribute AS a ON a.attrelid = t.oid AND a.attnum > 0 AND NOT a.attisdropped"
        " ORDER BY a.attnum"
    )


_TYPE_SPELLINGS = (
    (re.compile(r"\bvarchar\b"), "character varying"),
    (re.compile(r"\bchar\b"), "character"),
    (re.compile(r"\bbpchar\b"), "character"),
    (re.compile(r"\btimestamptz\b"), "timestamp with time zone"),
    (re.compile(r"\btimetz\b"), "time with time zone"),
    (re.compile(r"^(timestamp|time)(\(\d+\))?(?=\[|$)"), r"\1\2 without time zone"),
    (re.compile(r"\b(serial4|serial|int4|int)\b"), "integer"),
    (re.compile(r"\b(bigserial|serial8|int8)\b"), "bigint"),
    (re.compile(r"\b(smallserial|serial2|int2)\b"), "smallint"),
    (re.compile(r"\bbool\b"), "boolean"),
    (re.compile(r"\bfloat8\b"), "double precision"),
    (re.compile(r"\bfloat4\b"), "real"),
    (re.compile(r"\bdecimal\b"), "numeric"),
    (re.compile(r"\[\d*\]"), "[]"),
)


def normalize_type(type_name: str) -> str:
    """``type_name``, a PostgreSQL type as an ORM declares it or ``format_type()`` prints it, in ``format_type()``'s
    spelling: lower case, aliases spelt out (``varchar(100)`` is ``character varying(100)``, ``int`` is ``integer``,
    ``serial`` is ``integer``, ``time`` is ``time without time zone``), no spaces inside parentheses and array
    dimensions without a size (``integer[3]`` is ``integer[]``)."""
    text = " ".join(type_name.lower().split())
    text = re.sub(r"\s*\(\s*", "(", text)
    text = re.sub(r"\s*,\s*", ",", text)
    text = re.sub(r"\s*\)", ")", text)
    for pattern, replacement in _TYPE_SPELLINGS:
        text = pattern.sub(replacement, text)
    return re.sub(r"(\w|\))\s*\[\]", r"\1[]", text)


def _problem(kind: str, table: str, column: str | None = None, field: str | None = None, **values: Any) -> dict:
    return {
        "kind": kind,
        "table": table,
        "column": column,
        "field": field,
        "expected": values.get("expected"),
        "actual": values.get("actual"),
    }


def table_problems(table: ExpectedTable, rows: Iterable[tuple[Any, ...]]) -> list[dict[str, Any]]:
    """The differences between ``table`` and its columns in the catalogue, the ``rows`` of `columns_sql()`, as the
    problem dicts of `adorm.operations.CheckTable`: ``missing_table``; per expected column ``missing_column``,
    ``column_type`` (types compared with `normalize_type()`) and ``nullability`` (``"NULL"`` or ``"NOT NULL"``); and
    ``required_column`` for a column the table has and the model does not write that is ``NOT NULL`` without a
    default."""
    rows = [tuple(row) for row in rows]
    if not rows or not rows[0][0]:
        return [_problem("missing_table", table.name)]
    found = {row[1]: row for row in rows if row[1] is not None}
    problems = []
    for column in table.columns:
        row = found.get(column.name)
        if row is None:
            problems.append(_problem("missing_column", table.name, column.name, column.field))
            continue
        _, _, actual_type, not_null, _ = row
        if column.type is not None and normalize_type(column.type) != normalize_type(actual_type):
            problems.append(
                _problem("column_type", table.name, column.name, column.field, expected=column.type, actual=actual_type)
            )
        if column.nullable == bool(not_null):
            problems.append(
                _problem(
                    "nullability",
                    table.name,
                    column.name,
                    column.field,
                    expected="NULL" if column.nullable else "NOT NULL",
                    actual="NOT NULL" if not_null else "NULL",
                )
            )
    expected = {column.name for column in table.columns}
    for name, (_, _, actual_type, not_null, has_default) in found.items():
        if name not in expected and not_null and not has_default:
            problems.append(_problem("required_column", table.name, name, actual=actual_type))
    return problems
