"""SQL statements for drivers that execute SQL themselves: a statement with its parameters, conversion of
format-style placeholders (``%s``, the PEP 249 ``format`` paramstyle Django's compiler emits) to PostgreSQL's
numbered placeholders (``$1``), the only form asyncpg accepts, and the statements that set PostgreSQL connection
options."""

from __future__ import annotations

import math

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from adorm.drivers.base import is_custom_option

__all__ = [
    "POSTGRESQL_OPTIONS",
    "POSTGRESQL_TIMEOUTS",
    "numbered_placeholders",
    "quote_identifier",
    "quote_literal",
    "set_options_sql",
    "Statement",
]

POSTGRESQL_TIMEOUTS = frozenset({"statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout"})
"""PostgreSQL's time limits among `POSTGRESQL_OPTIONS`, in seconds."""

POSTGRESQL_OPTIONS = POSTGRESQL_TIMEOUTS | {"role", "search_path"}
"""The named connection options `set_options_sql()` sets: PostgreSQL's time limits, ``role`` and ``search_path``."""


@dataclass(frozen=True)
class Statement:
    """An SQL statement with numbered placeholders and its positional parameters, ready for asyncpg."""

    sql: str
    params: tuple[Any, ...] = ()


def numbered_placeholders(sql: str, casts: Sequence[str | None] | None = None) -> str:
    """Return ``sql`` with every ``%s`` replaced by ``$1``, ``$2`` … in order and every ``%%`` by ``%``.

    ``casts`` holds one entry per placeholder: a PostgreSQL type name appends ``::<type>`` to that placeholder,
    ``None`` leaves it bare. ``%`` is interpreted everywhere, inside quoted literals and identifiers too, as psycopg
    interprets it. Raises ``ValueError`` for any other ``%`` sequence or when ``casts`` has a different length than
    the number of placeholders.
    """
    out: list[str] = []
    count = 0
    start = 0
    position = sql.find("%")
    while position != -1:
        out.append(sql[start:position])
        marker = sql[position + 1 : position + 2]
        if marker == "%":
            out.append("%")
        elif marker == "s":
            if casts is not None and count >= len(casts):
                raise ValueError(f"{len(casts)} casts for more placeholders in: {sql}")
            count += 1
            cast = casts[count - 1] if casts is not None else None
            out.append(f"${count}" if cast is None else f"${count}::{cast}")
        else:
            raise ValueError(f"unsupported placeholder %{marker} at position {position} in: {sql}")
        start = position + 2
        position = sql.find("%", start)
    out.append(sql[start:])
    if casts is not None and count != len(casts):
        raise ValueError(f"{len(casts)} casts for {count} placeholders in: {sql}")
    return "".join(out)


_MAX_MILLISECONDS = 2147483647  # PostgreSQL's largest time limit


def _milliseconds(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value >= 0 or math.isinf(value):
        raise ValueError(f"{name} is a non-negative, finite number of seconds, not {value!r}")
    milliseconds = round(value * 1000)
    if milliseconds > _MAX_MILLISECONDS:
        raise ValueError(f"{name} is at most {_MAX_MILLISECONDS / 1000} seconds, not {value!r}")
    return 1 if milliseconds == 0 and value > 0 else milliseconds


def quote_identifier(name: str) -> str:
    """``name`` as a quoted PostgreSQL identifier."""
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    """``value`` as a PostgreSQL string literal, valid whatever ``standard_conforming_strings`` is."""
    quoted = value.replace("'", "''")
    if "\\" in value:
        return "E'" + quoted.replace("\\", "\\\\") + "'"
    return f"'{quoted}'"


def _text(name: str, value: Any) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError(f"{name} is a string without NUL, not {value!r}")
    return value


def _value_sql(name: str, value: Any) -> str:
    if name == "role":
        if value == "":
            raise ValueError("role is a non-empty string, not ''")
        return quote_identifier(_text(name, value))
    if name == "search_path":
        if not isinstance(value, (list, tuple)) or not value or "" in value:
            raise ValueError(f"search_path is a non-empty sequence of non-empty schema names, not {value!r}")
        return ", ".join(quote_identifier(_text(name, schema)) for schema in value)
    if name in POSTGRESQL_TIMEOUTS:
        return str(_milliseconds(name, value))
    return quote_literal(_text(name, value))


def _name_sql(name: str) -> str:
    if name in POSTGRESQL_OPTIONS:
        return name
    return ".".join(quote_identifier(part) for part in name.split("."))


def set_options_sql(options: Mapping[str, Any], local: bool) -> str:
    """The statements, separated by ``;``, that set ``options`` on a PostgreSQL connection: ``SET`` or, with
    ``local``, ``SET LOCAL``. The names are those of `POSTGRESQL_OPTIONS` and custom options
    (`adorm.drivers.base.is_custom_option()`). A time limit's value is in seconds, ``0`` for no limit; a positive value
    below one millisecond becomes one millisecond. ``role``'s value is a role name, ``search_path``'s a list or tuple
    of schema names (each quoted as an identifier), a custom option's a string. A value of ``None`` resets the option
    to its default: ``RESET``, or ``SET LOCAL … TO DEFAULT``. A custom option's name is quoted part by part, so parts
    that are SQL keywords (``app.user``) are valid. Raises `ValueError` for another name or a value of another form;
    an empty string for no options."""
    statements = []
    for name, value in options.items():
        if name not in POSTGRESQL_OPTIONS and not (isinstance(name, str) and is_custom_option(name)):
            raise ValueError(f"{name!r} is not a PostgreSQL connection option adorm sets")
        sql_name = _name_sql(name)
        if value is None:
            statements.append(f"SET LOCAL {sql_name} TO DEFAULT" if local else f"RESET {sql_name}")
        else:
            statements.append(f"SET{' LOCAL' if local else ''} {sql_name} = {_value_sql(name, value)}")
    return "; ".join(statements)
