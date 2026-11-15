"""SQL for asyncpg from Django's query compiler, built without a database connection.

`DjangoCompiler` compiles a Django query on a database wrapper of its own that never connects; `bind_param()`
turns the parameters Django prepares for psycopg into values asyncpg encodes, with the PostgreSQL type psycopg
gives them as an explicit cast. Django's PostgreSQL backend must run on psycopg 3.
"""

from __future__ import annotations

import contextvars
import copy
import datetime
import decimal
import ipaddress
import itertools
import json
import math
import uuid
import zoneinfo

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, NoReturn

import asyncpg

from django.conf import settings
from django.core.exceptions import EmptyResultSet
from django.db import connections
from django.db.backends.postgresql.psycopg_any import is_psycopg3
from django.db.models import Model, sql
from django.db.utils import load_backend
from psycopg import Binary
from psycopg.types.json import Json, Jsonb
from psycopg.types.numeric import Float4, Float8, Int2, Int4, Int8, IntNumeric, Oid
from psycopg.types.range import Range

from adorm.drivers.sql import numbered_placeholders, Statement
from adorm.exceptions import NotSupportedError, TransactionManagementError

__all__ = ["bind_param", "DjangoAggregation", "DjangoCompiler", "DjangoStatement", "MAX_QUERY_PARAMS"]

MAX_QUERY_PARAMS = 32767
"""The most parameters asyncpg sends with one statement."""

# Django 6 compiles RETURNING for an update; Django 5.2 drops it.
_UPDATE_RETURNING = hasattr(Model, "_assign_returned_values")

# psycopg's typed wrappers; Django wraps integer field values in the Int* ones.
_WRAPPER_TYPES = {
    Int2: "int2",
    Int4: "int4",
    Int8: "int8",
    IntNumeric: "numeric",
    Oid: "oid",
    Float4: "float4",
    Float8: "float8",
}
_INT_WIDTHS = ("int2", "int4", "int8", "numeric")
_IP_TYPES = (
    ipaddress.IPv4Address,
    ipaddress.IPv6Address,
    ipaddress.IPv4Interface,
    ipaddress.IPv6Interface,
    ipaddress.IPv4Network,
    ipaddress.IPv6Network,
)


def _int_type(value: int) -> str:
    """The smallest integer type psycopg sends ``value`` as with server-side binding."""
    if -(2**15) <= value < 2**15:
        return "int2"
    return _literal_int_type(value)


def _literal_int_type(value: int) -> str:
    """The type PostgreSQL gives the integer literal psycopg renders for ``value`` with client-side binding."""
    if -(2**31) <= value < 2**31:
        return "int4"
    if -(2**63) <= value < 2**63:
        return "int8"
    return "numeric"


def _flatten(items: Iterable[Any]) -> Iterable[Any]:
    for item in items:
        if isinstance(item, list | tuple):
            yield from _flatten(item)
        elif item is not None:
            yield item


def _array_type(items: Sequence[Any]) -> str | None:
    """The array type psycopg gives a list: the widest integer type of its items, else the first item's type."""
    casts = [bind_param(item, server_side_binding=True)[1] for item in _flatten(items)]
    if not casts or casts[0] is None:
        return None
    if all(cast in _INT_WIDTHS for cast in casts):
        return max(casts, key=_INT_WIDTHS.index) + "[]"
    return casts[0] + "[]"


def _items(items: Sequence[Any]) -> list[Any]:
    return [
        _items(item) if isinstance(item, list | tuple) else bind_param(item, server_side_binding=True)[0]
        for item in items
    ]


def bind_param(value: Any, *, server_side_binding: bool = False) -> tuple[Any, str | None]:
    """Return the value asyncpg encodes for ``value``, a parameter Django prepared for psycopg, and the PostgreSQL
    type to cast its placeholder to, or ``None`` for no cast.

    The cast is the type the value has under psycopg, so PostgreSQL resolves the statement as it does there.
    ``server_side_binding`` selects psycopg's binding mode (Django's ``OPTIONS["server_side_binding"]``). With
    client-side binding, Django's default, psycopg renders numbers as untyped literals: integers (psycopg's integer
    wrappers too) take ``int4``, ``int8`` or ``numeric`` by size, finite floats ``numeric``. With server-side
    binding an integer takes the smallest of ``int2``, ``int4``, ``int8``, ``numeric``, a wrapper its own type and
    a float ``float8``. ``str`` and ``None`` stay untyped and take their type from the context. A list or tuple
    takes the array of its items' widest integer type or of its first item's type; without a typed item it stays
    untyped. A psycopg object this function does not know raises ``NotSupportedError``.
    """
    kind = type(value)
    if kind in _WRAPPER_TYPES:
        number = float(value) if kind in (Float4, Float8) else int(value)
        if server_side_binding:
            return number, _WRAPPER_TYPES[kind]
        return bind_param(number)
    if value is None or isinstance(value, str):
        return value, None
    if isinstance(value, bool):
        return value, "bool"
    if isinstance(value, int):
        return value, _int_type(value) if server_side_binding else _literal_int_type(value)
    if isinstance(value, float):
        if server_side_binding or not math.isfinite(value):
            return value, "float8"
        return decimal.Decimal(repr(value)), "numeric"
    if isinstance(value, decimal.Decimal):
        return value, "numeric"
    if isinstance(value, datetime.datetime):
        return value, "timestamp" if value.tzinfo is None else "timestamptz"
    if isinstance(value, datetime.date):
        return value, "date"
    if isinstance(value, datetime.time):
        return value, "time" if value.tzinfo is None else "timetz"
    if isinstance(value, datetime.timedelta):
        return value, "interval"
    if isinstance(value, uuid.UUID):
        return value, "uuid"
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value), "bytea"
    if isinstance(value, Binary):
        return bytes(value.obj), "bytea"
    if isinstance(value, Json | Jsonb):
        dumps = value.dumps or json.dumps
        return dumps(value.obj), "jsonb" if isinstance(value, Jsonb) else "json"
    if isinstance(value, ipaddress.IPv4Address | ipaddress.IPv6Address):
        return value, "inet"
    if isinstance(value, ipaddress.IPv4Network | ipaddress.IPv6Network):
        return value, "cidr"
    if isinstance(value, Range):
        if value.isempty:
            return asyncpg.Range(empty=True), None
        bounds = value.bounds
        lower_inc, upper_inc = bounds[0] == "[", bounds[1] == "]"
        return asyncpg.Range(value.lower, value.upper, lower_inc=lower_inc, upper_inc=upper_inc), None
    if isinstance(value, list | tuple):
        return _items(value), _array_type(value)
    if kind.__module__.split(".")[0] == "psycopg":
        raise NotSupportedError(f"no asyncpg value for the psycopg parameter {value!r}")
    return value, None


def _psycopg_value(value: Any, timezone: datetime.tzinfo, naive: bool) -> Any:
    """``value`` from an asyncpg row as Django's psycopg setup loads it: ``inet`` / ``cidr`` as text, ``uuid`` as
    ``uuid.UUID``, an aware ``datetime`` in ``timezone`` (naive when ``naive``), lists item by item."""
    if isinstance(value, _IP_TYPES):
        return str(value)
    if isinstance(value, uuid.UUID) and type(value) is not uuid.UUID:
        return uuid.UUID(bytes=value.bytes)
    if isinstance(value, datetime.datetime) and value.tzinfo is not None:
        value = value.astimezone(timezone)
        return value.replace(tzinfo=None) if naive else value
    if isinstance(value, list):
        return [_psycopg_value(item, timezone, naive) for item in value]
    return value


@dataclass(frozen=True)
class DjangoStatement(Statement):
    """A `Statement` compiled from a Django query; `rows()` applies Django's converters to its result rows, `returned()`
    to the rows of its ``RETURNING`` clause.

    An empty statement (``sql == ""``, `empty` true) is one Django does not execute: a query that matches no rows
    (an update or delete filtered by ``pk__in=[]``) or an update without values. It affects no rows. ``returning``
    holds the Django fields an insert or update returns, in order."""

    compiler: Any = field(default=None, repr=False, compare=False)
    returning: tuple[Any, ...] = field(default=(), repr=False, compare=False)

    @property
    def empty(self) -> bool:
        """True when there is no statement to execute."""
        return not self.sql

    def rows(self, records: Iterable[Sequence[Any]]) -> list[tuple[Any, ...]]:
        """Return ``records`` (asyncpg ``Record`` objects or tuples, in select order) as tuples of the values Django's
        own evaluation of the query produces: columns Django adds only for ordering or ``extra(select=)`` dropped,
        ``inet`` / ``cidr`` values as text, ``uuid`` values as ``uuid.UUID`` and ``timestamptz`` values in the
        connection's time zone (naive without ``USE_TZ``), as psycopg loads them, and the fields' ``from_db_value()``
        converters applied."""
        width = self.compiler.col_count if self.compiler.has_extra_select else None
        timezone, naive = _result_timezone(self.compiler.connection)
        rows = [tuple(_psycopg_value(v, timezone, naive) for v in tuple(r)[:width]) for r in records]
        return list(self.compiler.results_iter(results=[rows], tuple_expected=True))

    def returned(self, records: Iterable[Sequence[Any]]) -> list[tuple[Any, ...]]:
        """Return ``records``, the rows of the statement's ``RETURNING`` clause, as tuples of the values Django's own
        insert or update returns: values loaded as in `rows()` and the ``returning`` fields' converters applied."""
        timezone, naive = _result_timezone(self.compiler.connection)
        rows: Iterable[Sequence[Any]] = [tuple(_psycopg_value(v, timezone, naive) for v in tuple(r)) for r in records]
        if self.returning:
            table = self.compiler.query.get_meta().db_table
            converters = self.compiler.get_converters([f.get_col(table) for f in self.returning])
            if converters:
                rows = self.compiler.apply_converters(rows, converters)
        return [tuple(row) for row in rows]


class _Captured(Exception):  # noqa: N818 - control flow inside this module only
    def __init__(self, sql: str, params: Sequence[Any]) -> None:
        super().__init__(sql)
        self.sql = sql
        self.params = params


class _Cursor:
    """The cursor Django's ``get_aggregation()`` runs its statement on: it captures the statement, or replays a row
    fetched elsewhere."""

    def __init__(self, row: Sequence[Any] | None = None, *, capture: bool) -> None:
        self.row = row
        self.capture = capture

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        if self.capture:
            raise _Captured(sql, params)

    def fetchone(self) -> Sequence[Any] | None:
        return self.row

    def close(self) -> None:
        pass


_CURSOR: contextvars.ContextVar[_Cursor | None] = contextvars.ContextVar("adorm_aggregate_cursor", default=None)
_ALIAS_NUMBERS = itertools.count()


@dataclass(frozen=True)
class DjangoAggregation:
    """An aggregation compiled from a Django query: ``statement`` returns its one row (it is empty when the query
    matches no rows, and `result()` then takes no row); `result()` turns that row into Django's ``aggregate()`` result:
    a dict by name, values converted as Django's own evaluation converts them."""

    statement: DjangoStatement
    replay: Any = field(repr=False, compare=False)

    def result(self, record: Sequence[Any] | None) -> dict[str, Any]:
        """The result of the aggregation over ``record``, the statement's row (``None`` for no row)."""
        return self.replay(record)


def _result_timezone(wrapper: Any) -> tuple[datetime.tzinfo, bool]:
    """The time zone Django's psycopg setup gives ``timestamptz`` values of ``wrapper``'s connections, and whether it
    drops it: the connection's time zone under ``USE_TZ``, else ``TIME_ZONE`` with naive values."""
    if settings.USE_TZ:
        return wrapper.timezone, False
    return zoneinfo.ZoneInfo(wrapper.timezone_name), True


class DjangoCompiler:
    """Compiles Django queries for a ``DATABASES`` entry into `DjangoStatement` objects, without a connection.

    The compiler runs on a database wrapper of its own, built from ``DATABASES[alias]`` and never connected:
    Django's thread-local connection for ``alias`` is not touched, so compiling works in any thread and inside a
    running event loop. ``server_version`` is PostgreSQL's ``server_version_num`` (``170006`` for 17.6); Django
    reads it to choose SQL for some constructs (``AnyValue``, ``JSONObject``, ``JSONArray``, ``UUID7``) and would
    otherwise connect to ask. ``params`` replace keys of the entry for this compiler, as a connection profile's
    ``params`` do. Parameters are bound for the entry's ``OPTIONS["server_side_binding"]``. A construct that needs a
    connection to compile (``SearchHeadline`` with options, which quotes them through psycopg) raises
    ``NotSupportedError``. The entry must use Django's PostgreSQL backend on psycopg 3.
    """

    def __init__(self, alias: str = "default", *, server_version: int, params: Mapping[str, Any] | None = None):
        settings_dict = copy.deepcopy({**connections.settings[alias], **(params or {})})
        wrapper = load_backend(settings_dict["ENGINE"]).DatabaseWrapper(settings_dict, alias)
        if wrapper.vendor != "postgresql":
            raise NotSupportedError(f"DATABASES[{alias!r}] is {wrapper.vendor}, not PostgreSQL")
        if not is_psycopg3:
            raise NotSupportedError("Django's PostgreSQL backend runs on psycopg2; DjangoCompiler needs psycopg 3")
        wrapper.__dict__["pg_version"] = server_version
        wrapper.features.max_query_params = MAX_QUERY_PARAMS
        wrapper.ensure_connection = wrapper.connect = self._refuse_connection
        wrapper.cursor = wrapper.chunked_cursor = self._cursor
        # select_for_update compiles as inside a transaction; compile() checks the caller's transaction state.
        wrapper.get_autocommit = lambda: False
        self.alias = alias
        self.server_version = server_version
        self.server_side_binding = bool(settings_dict.get("OPTIONS", {}).get("server_side_binding"))
        self.settings_dict = settings_dict
        self.connection = wrapper

    def _refuse_connection(self) -> NoReturn:
        raise NotSupportedError(f"compiling this query for {self.alias!r} needs a database connection")

    def _cursor(self) -> _Cursor:
        cursor = _CURSOR.get()
        if cursor is None:
            self._refuse_connection()
        return cursor

    def compile(self, query: Any, *, in_transaction: bool = False, returning: Sequence[Any] = ()) -> DjangoStatement:
        """Compile ``query``, a Django ``QuerySet`` or ``Query`` (select, update, delete or aggregate), into a
        statement with numbered placeholders and asyncpg parameters.

        A select that matches no rows compiles with ``WHERE 0 = 1``; an update or delete that matches no rows and an
        update without values compile into an empty statement. ``in_transaction`` tells whether the statement runs
        inside a transaction: ``select_for_update()`` outside one raises ``TransactionManagementError``, as Django
        does. ``returning`` names the Django fields an update returns (``RETURNING``). Raises ``NotSupportedError`` for
        an insert (`compile_insert()` compiles one), for an update of fields of a multi-table inheritance parent,
        which Django runs as several statements after selecting the keys, and for a statement with more than
        `MAX_QUERY_PARAMS` parameters, and for ``returning`` on Django before 6, which does not compile it."""
        query = getattr(query, "query", query)
        if returning and not _UPDATE_RETURNING:
            raise NotSupportedError("this Django version does not compile RETURNING for an update")
        if isinstance(query, sql.UpdateQuery) and query.related_updates:
            raise NotSupportedError("an update of parent model fields runs as several statements")
        if isinstance(query, sql.InsertQuery):
            raise NotSupportedError("an insert compiles with compile_insert()")
        compiler = self._compiler(query, in_transaction)
        if returning:
            compiler.returning_fields = list(returning)
        try:
            sql_text, params = compiler.as_sql()
        except EmptyResultSet:
            return DjangoStatement("", (), compiler=compiler)
        if not sql_text:
            return DjangoStatement("", (), compiler=compiler)
        return self._statement(sql_text, params, compiler, returning)

    def compile_insert(self, query: Any, returning: Sequence[Any] = ()) -> DjangoStatement:
        """Compile ``query``, a Django ``InsertQuery`` of one row, into a statement that returns the Django fields
        ``returning`` (``RETURNING``), in order. Raises ``NotSupportedError`` for a query that compiles into several
        statements and for one with more than `MAX_QUERY_PARAMS` parameters."""
        compiler = query.get_compiler(connection=self.connection)
        compiler.returning_fields = list(returning) or None
        statements = compiler.as_sql()
        if len(statements) != 1:
            raise NotSupportedError(f"the insert compiles into {len(statements)} statements")
        sql_text, params = statements[0]
        return self._statement(sql_text, params, compiler, returning)

    def compile_count(self, query: Any, *, in_transaction: bool = False) -> DjangoStatement:
        """Compile the number of rows of ``query``, a Django select ``QuerySet`` or ``Query``, into a statement that
        returns one row with one ``bigint`` column: ``SELECT COUNT(*) FROM (<query>) subquery``.

        The rows counted are those the query returns, after its distinct, slice and annotations; its ordering is
        dropped unless the slice or ``distinct()`` on fields needs it, as Django's ``count()`` does. A query that
        matches no rows counts 0. ``in_transaction`` and the errors are those of `compile()`."""
        inner = getattr(query, "query", query).clone()
        inner.subquery = True
        inner.clear_ordering(force=False)
        compiler = self._compiler(inner, in_transaction)
        inner_sql, params = compiler.as_sql(with_col_aliases=True)
        return self._statement(f"SELECT COUNT(*) FROM ({inner_sql}) subquery", params, compiler)  # noqa: S608 - compiled

    def compile_aggregate(self, query: Any, aggregates: Mapping[str, Any]) -> DjangoAggregation:
        """Compile ``aggregate(**aggregates)`` of ``query``, a Django select ``QuerySet`` or ``Query``, into a
        `DjangoAggregation`: the statement Django's ``get_aggregation()`` runs (one row) and the conversion of that
        row into Django's result. Row locks of ``query`` are dropped, as Django drops them. The errors are those of
        `compile()`."""
        query = getattr(query, "query", query)

        def run(cursor: _Cursor) -> dict[str, Any]:
            # get_aggregation() runs on the wrapper through a thread-local alias of this call's own.
            using = f"adorm-compile-{next(_ALIAS_NUMBERS)}"
            connections[using] = self.connection
            token = _CURSOR.set(cursor)
            try:
                return query.chain().get_aggregation(using, dict(aggregates))
            finally:
                _CURSOR.reset(token)
                del connections[using]

        timezone, naive = _result_timezone(self.connection)

        def replay(record: Sequence[Any] | None) -> dict[str, Any]:
            row = None if record is None else tuple(_psycopg_value(v, timezone, naive) for v in tuple(record))
            return run(_Cursor(row, capture=False))

        try:
            empty = run(_Cursor(capture=True))
        except _Captured as captured:
            statement = self._statement(captured.sql, captured.params, None)
        else:
            return DjangoAggregation(DjangoStatement("", ()), lambda _record: dict(empty))
        return DjangoAggregation(statement, replay)

    def _compiler(self, query: Any, in_transaction: bool) -> Any:
        if getattr(query, "select_for_update", False) and not in_transaction:
            raise TransactionManagementError("select_for_update cannot be used outside of a transaction.")
        return query.get_compiler(connection=self.connection, elide_empty=False)

    def _statement(
        self, sql_text: str, params: Sequence[Any], compiler: Any, returning: Sequence[Any] = ()
    ) -> DjangoStatement:
        if len(params) > MAX_QUERY_PARAMS:
            raise NotSupportedError(f"{len(params)} parameters; asyncpg sends at most {MAX_QUERY_PARAMS}")
        bound = [bind_param(p, server_side_binding=self.server_side_binding) for p in params]
        return DjangoStatement(
            numbered_placeholders(sql_text, [cast for _, cast in bound]),
            tuple(value for value, _ in bound),
            compiler=compiler,
            returning=tuple(returning),
        )
