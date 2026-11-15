"""SQL statements for drivers that execute SQL themselves: a statement with its parameters, conversion of
format-style placeholders (``%s``, the PEP 249 ``format`` paramstyle Django's compiler emits) to PostgreSQL's
numbered placeholders (``$1``), the only form asyncpg accepts, and the statements that set PostgreSQL connection
options."""

from __future__ import annotations

import math

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = ["POSTGRESQL_OPTIONS", "numbered_placeholders", "set_options_sql", "Statement"]

POSTGRESQL_OPTIONS = frozenset({"statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout"})
"""The connection options `set_options_sql()` sets: PostgreSQL's time limits, in seconds."""


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


def set_options_sql(options: Mapping[str, Any], local: bool) -> str:
    """The statements, separated by ``;``, that set ``options`` (names of `POSTGRESQL_OPTIONS`, values in seconds,
    ``0`` for no limit) on a PostgreSQL connection: ``SET`` or, with ``local``, ``SET LOCAL``. A value of ``None``
    resets the option to its default: ``RESET``, or ``SET LOCAL … TO DEFAULT``. A positive value below one millisecond
    becomes one millisecond, since ``0`` means no limit. Raises `ValueError` for another name or a value that is not
    a number of seconds from 0 to 2147483.647; an empty string for no options."""
    statements = []
    for name, value in options.items():
        if name not in POSTGRESQL_OPTIONS:
            raise ValueError(f"{name!r} is not a PostgreSQL connection option adorm sets")
        if value is None:
            statements.append(f"SET LOCAL {name} TO DEFAULT" if local else f"RESET {name}")
        else:
            statements.append(f"SET{' LOCAL' if local else ''} {name} = {_milliseconds(name, value)}")
    return "; ".join(statements)
