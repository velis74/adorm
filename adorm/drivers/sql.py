"""SQL statements for drivers that execute SQL themselves: a statement with its parameters, and conversion of
format-style placeholders (``%s``, the PEP 249 ``format`` paramstyle Django's compiler emits) to PostgreSQL's
numbered placeholders (``$1``), the only form asyncpg accepts."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

__all__ = ["numbered_placeholders", "Statement"]


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
