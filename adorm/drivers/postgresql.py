"""What PostgreSQL reports about a violated constraint, read from the errors of psycopg (2 and 3) and asyncpg."""

from __future__ import annotations

import re

from typing import Any

from adorm.exceptions import _sqlstate

__all__ = ["integrity_details", "parse_key_detail", "references_key"]

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
