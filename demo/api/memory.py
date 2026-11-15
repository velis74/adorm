"""The demo's in-memory driver: adorm operations on Python dicts, with transactions, savepoints and row locks.

`MemoryDriver` stands in for an ORM driver so that the demo runs sessions and transactions on the executor. Its
`MemoryDatabase` holds one table per model label (rows keyed by primary key). The demo's setup fills it once from
the demo's PostgreSQL database through Django; writes stay in memory.

Supported: the operations in `CAPABILITIES` on a query of one model: conditions (`Q`, negation, the lookups listed)
on the model's own fields and foreign key columns, with a model instance standing for its primary key, arithmetic
with `F`, ordering by fields, ``distinct()``, slicing and ``values()`` / ``values_list()`` of fields. Constructs
outside `CAPABILITIES` (``path.nested`` for field paths across relations, ``clause.annotate`` …) are refused before
execution; a reverse relation named in a condition raises `NotSupportedError`, and so does ``in_bulk()`` on a field
other than the primary key. A negated condition keeps a row whose field in a lookup is NULL, as Django's ``exclude()``
does.

Each model is its own table. An update of a row copies the values to the row with the same primary key of the models
it shares rows with through multi-table inheritance; inserting or deleting a row of a multi-table inheritance child
raises `NotSupportedError`, and so does deleting a row that a foreign key references (deletes do not cascade). Unique
constraints are not enforced, except the primary key.

Transactions read committed rows and their own writes. A write locks its row until the transaction ends, and so
does a read with ``select_for_update()`` (also ``nowait`` and ``skip_locked``, not ``of`` and ``no_key``); a
conflicting write or locking read of another connection waits for the lock and re-checks the row, and a wait that
would close a cycle raises `OperationalError` with SQLSTATE ``40P01``. A locking read with ``nowait`` raises
`OperationalError` with SQLSTATE ``55P03`` for a locked row; one with ``skip_locked`` leaves locked rows out before
slicing. Rolling back to a savepoint releases the locks taken after it. A lookup, arithmetic or ordering on values
that do not compare raises `ProgrammingError` with SQLSTATE ``42883``. Every call is recorded in the trace of the
calling context (`tracing()`).
"""

from __future__ import annotations

import asyncio
import copy
import itertools
import operator
import re
import types
import typing

from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from adorm.drivers import Capabilities, Driver, DriverConnection
from adorm.events import Emit
from adorm.exceptions import (
    DatabaseError,
    FieldError,
    IntegrityError,
    InterfaceError,
    MultipleObjectsReturned,
    NotSupportedError,
    ObjectDoesNotExist,
    OperationalError,
    ProgrammingError,
    TransactionManagementError,
)
from adorm.models import ForeignKey, Model, registry
from adorm.operations import (
    Count,
    Create,
    Delete,
    DeleteInstance,
    Exists,
    First,
    Get,
    GetOrCreate,
    InBulk,
    Last,
    Save,
    Select,
    Update,
    UpdateOrCreate,
)
from adorm.query import CombinedExpression, F, OrderBy, Q, Query, Value
from adorm.query.expressions import Node, OuterRef
from adorm.query.lookups import Lookup, resolve_lookup

__all__ = [
    "CAPABILITIES",
    "DATABASE",
    "LOOKUPS",
    "MemoryConnection",
    "MemoryDatabase",
    "MemoryDriver",
    "TraceEntry",
    "django_tables",
    "on_acquire",
    "on_release",
    "tracing",
]


def _text(value: Any) -> str:
    return value if isinstance(value, str) else str(value)


def _contains(value: Any, rhs: Any) -> bool:
    if isinstance(value, list | tuple):
        items = rhs if isinstance(rhs, list | tuple) else (rhs,)
        return all(item in value for item in items)
    if isinstance(value, Mapping) and isinstance(rhs, Mapping):
        return all(key in value and value[key] == item for key, item in rhs.items())
    return _text(rhs) in _text(value)


LOOKUPS: Mapping[str, Callable[[Any, Any], bool]] = types.MappingProxyType(
    {
        "exact": operator.eq,
        "iexact": lambda value, rhs: _text(value).casefold() == _text(rhs).casefold(),
        "gt": operator.gt,
        "gte": operator.ge,
        "lt": operator.lt,
        "lte": operator.le,
        "in": lambda value, rhs: value in rhs,
        "range": lambda value, rhs: rhs[0] <= value <= rhs[1],
        "contains": _contains,
        "icontains": lambda value, rhs: _text(rhs).casefold() in _text(value).casefold(),
        "startswith": lambda value, rhs: _text(value).startswith(_text(rhs)),
        "istartswith": lambda value, rhs: _text(value).casefold().startswith(_text(rhs).casefold()),
        "endswith": lambda value, rhs: _text(value).endswith(_text(rhs)),
        "iendswith": lambda value, rhs: _text(value).casefold().endswith(_text(rhs).casefold()),
        "regex": lambda value, rhs: re.search(rhs, _text(value)) is not None,
        "iregex": lambda value, rhs: re.search(rhs, _text(value), re.IGNORECASE) is not None,
    }
)
"""The lookups the driver evaluates, by name; ``isnull`` is evaluated separately. A NULL field value makes every
one of them unknown (SQL's three-valued logic)."""

CAPABILITIES = Capabilities(
    operations={
        "select",
        "get",
        "first",
        "last",
        "count",
        "exists",
        "in_bulk",
        "create",
        "save",
        "update",
        "delete",
        "delete_instance",
        "get_or_create",
        "update_or_create",
    },
    constructs={f"lookup.{name}" for name in (*LOOKUPS, "isnull")}
    | {
        "clause.distinct",
        "clause.select_for_update",
        "clause.select_for_update.nowait",
        "clause.select_for_update.skip_locked",
        "expression.CombinedExpression",
    },
    savepoints=True,
    cancellation=True,
)

_ARITHMETIC: Mapping[str, Callable[[Any, Any], Any]] = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "/": operator.truediv,
    "%": operator.mod,
    "^": operator.pow,
}

Table = dict[Any, dict[str, Any]]
Overlay = dict[str, dict[Any, dict[str, Any] | None]]


@dataclass(frozen=True)
class TraceEntry:
    """One call a memory connection received: ``slot`` is the connection's number, ``session`` the session the slot
    is assigned to (``"using/session_name"``, ``None`` outside an assignment)."""

    slot: int
    session: str | None
    call: str


_trace: ContextVar[list[TraceEntry] | None] = ContextVar("demo_memory_trace", default=None)


@contextmanager
def tracing() -> Iterator[list[TraceEntry]]:
    """Collect the calls memory connections receive from this context, and from tasks created in it, in the list the
    ``with`` statement binds."""
    entries: list[TraceEntry] = []
    token = _trace.set(entries)
    try:
        yield entries
    finally:
        _trace.reset(token)


class MemoryDatabase:
    """Committed tables by model label, the row locks of open transactions (``locks``: (label, pk) → owner), the
    connections waiting for a lock (``waiters``: waiter → (owner, (label, pk))) and the primary key sequences.

    ``tables`` maps a label to its rows (mappings of field names to values). Only the labels given have tables; an
    operation on another label raises `NotSupportedError`.
    """

    def __init__(self, tables: Mapping[str, Iterable[Mapping[str, Any]]] | None = None) -> None:
        self.tables: dict[str, Table] = {}
        self.locks: dict[tuple[str, Any], MemoryConnection] = {}
        self.waiters: dict[MemoryConnection, tuple[MemoryConnection, tuple[str, Any]]] = {}
        self._sequences: dict[str, Iterator[int]] = {}
        self.load(tables or {})

    def load(self, tables: Mapping[str, Iterable[Mapping[str, Any]]]) -> None:
        """Replace every table with ``tables``."""
        loaded: dict[str, Table] = {}
        for label, rows in tables.items():
            pk = _model(label)._meta.pk
            loaded[label] = {row[pk]: copy.deepcopy(dict(row)) for row in rows}
        self.tables = loaded
        self.locks.clear()
        self.waiters.clear()
        self._sequences = {
            label: itertools.count(max((pk for pk in table if isinstance(pk, int)), default=0) + 1)
            for label, table in loaded.items()
        }

    def rows(self, label: str) -> list[dict[str, Any]]:
        """Copies of the committed rows of ``label``, by primary key."""
        table = self.tables[label]
        return [copy.deepcopy(table[pk]) for pk in sorted(table, key=_sort_key)]

    def next_pk(self, label: str) -> int:
        """The next value of the integer primary key sequence of ``label``; a rollback does not return it."""
        return next(self._sequences[label])


DATABASE = MemoryDatabase()
"""The database of a `MemoryDriver` created without one; the demo's setup fills it."""


def _sort_key(value: Any) -> tuple[int, Any]:
    return (0, value) if isinstance(value, int | float) else (1, str(value))


def _incomparable(exc: TypeError) -> ProgrammingError:
    return ProgrammingError(f"operator does not exist: {exc}", sqlstate="42883")


def _model(label: str) -> type[Model]:
    try:
        return registry.get(label)
    except LookupError:
        raise ProgrammingError(f"no adorm model is registered as {label!r}") from None


def _inheritance(label: str) -> tuple[list[str], list[str]]:
    """The labels of the registered parents and children of ``label`` in multi-table inheritance."""
    model = _model(label)
    parents = [other._meta.label for other in registry if other is not model and issubclass(model, other)]
    children = [other._meta.label for other in registry if other is not model and issubclass(other, model)]
    return parents, children


def _is_int_field(model: type[Model], name: str) -> bool:
    annotation = model.__pydantic_fields__[name].annotation
    args = typing.get_args(annotation) if typing.get_origin(annotation) in (typing.Union, types.UnionType) else ()
    return annotation is int or int in args


class _Rows:
    """Evaluation of the nodes of one model's query against rows."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.model = _model(label)
        self.pk = self.model._meta.pk
        self.fields = self.model.__pydantic_fields__

    def field(self, path: str) -> str:
        """The field a path names: ``pk``, a field, or a foreign key's name for its column."""
        if path == "pk":
            return self.pk
        if path in self.fields:
            return path
        relation = self.model.__adorm_relations__.get(path)
        if isinstance(relation, ForeignKey):
            return relation.column
        if relation is None:
            resolve_lookup(self.label, f"{path}__exact")
        if "__" in path or relation is not None:
            raise NotSupportedError(f"the memory driver does not follow relations ({self.label}: {path!r})")
        raise FieldError(f"{self.label} has no field {path!r}", model=self.label, key=path)

    def value(self, node: Any, row: Mapping[str, Any]) -> Any:
        """The value of an expression for ``row``."""
        if isinstance(node, Value):
            return node.value
        if isinstance(node, F) and not isinstance(node, OuterRef):
            return row.get(self.field(node.name))
        if isinstance(node, CombinedExpression):
            lhs, rhs = self.value(node.lhs, row), self.value(node.rhs, row)
            if lhs is None or rhs is None:
                return None
            try:
                return _ARITHMETIC[node.connector](lhs, rhs)
            except TypeError as exc:
                raise _incomparable(exc) from exc
        if isinstance(node, Node):
            raise NotSupportedError(f"the memory driver does not evaluate {type(node).__name__}")
        if isinstance(node, Model):
            return node.pk
        return node

    def condition(self, node: Any, row: Mapping[str, Any], negated: bool = False) -> bool | None:
        """``True``, ``False`` or ``None`` (unknown) for ``row``; ``negated`` tells whether an odd number of negations
        encloses ``node``. There, as in Django, a lookup on a field whose value is NULL is false, not unknown, so the
        negation keeps the row."""
        if isinstance(node, Q):
            inner = negated != node.negated
            results = [self.condition(child, row, inner) for child in node.children]
            if node.connector == Q.AND:
                result = False if False in results else None if None in results else True
            else:
                result = True if True in results else None if None in results else False
            return (None if result is None else not result) if node.negated else result
        if isinstance(node, Lookup):
            self.check_relation_lookup(node)
            value = self.value(node.lhs, row)
            if node.lookup_name == "isnull":
                return (value is None) is node.rhs
            if node.rhs_is_sequence and isinstance(node.rhs, tuple):
                rhs = tuple(self.value(item, row) for item in node.rhs)
            else:
                rhs = self.value(node.rhs, row)
            if negated and ((value is None and isinstance(node.lhs, F)) or (rhs is None and isinstance(node.rhs, F))):
                return False
            if value is None or rhs is None:
                return None
            try:
                return bool(LOOKUPS[node.lookup_name](value, rhs))
            except TypeError as exc:
                raise _incomparable(exc) from exc
        raise NotSupportedError(f"the memory driver does not evaluate {type(node).__name__} conditions")

    def check_relation_lookup(self, node: Lookup) -> None:
        """Raise `FieldError` for a lookup on a relation that does not apply to relations."""
        lhs = node.lhs
        if not node.on_relations and type(lhs) is F and lhs.name in self.model.__adorm_relations__:
            resolve_lookup(self.label, f"{lhs.name}__{node.lookup_name}")

    def matches(self, query: Query, row: Mapping[str, Any]) -> bool:
        return not query.where or self.condition(query.where, row) is True

    def order(self, rows: list[dict[str, Any]], ordering: tuple[OrderBy, ...]) -> list[dict[str, Any]]:
        """``rows`` sorted by ``ordering`` (by primary key without one); NULLs sort as in PostgreSQL."""
        if not ordering:
            ordering = (OrderBy(F("pk")),)
        for order in reversed(ordering):
            if not isinstance(order.expression, F):
                raise NotSupportedError("the memory driver orders by fields only")
            name = self.field(order.expression.name)
            if order.nulls_first is None and order.nulls_last is None:
                nulls_first = order.descending
            else:
                nulls_first = bool(order.nulls_first)
            # sorted(reverse=True) also reverses the rank of NULLs.
            null_rank = 0 if nulls_first != order.descending else 1

            def key(row: Mapping[str, Any], name: str = name, null_rank: int = null_rank) -> tuple[int, Any]:
                value = row.get(name)
                return (null_rank, 0) if value is None else (1 - null_rank, value)

            try:
                rows = sorted(rows, key=key, reverse=order.descending)
            except TypeError as exc:
                raise _incomparable(exc) from exc
        return rows

    def project(self, query: Query, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The rows as the query's row format selects them, distinct when the query is."""
        if query.annotations:
            raise NotSupportedError("the memory driver does not compute annotations")
        if query.row_format != "model":
            names = query.select or tuple(self.fields)
            rows = [{name: row.get(self.field(name)) for name in names} for row in rows]
        if query.is_distinct:
            seen: set[str] = set()
            unique = []
            for row in rows:
                key = repr(sorted(row.items()))
                if key not in seen:
                    seen.add(key)
                    unique.append(row)
            rows = unique
        return rows


class MemoryConnection(DriverConnection):
    """A connection to a `MemoryDatabase`. ``number`` identifies it in the trace; ``session`` is set by the profile
    hooks `on_acquire` and `on_release`."""

    driver: MemoryDriver

    def __init__(self, driver: MemoryDriver, emit: Emit, number: int) -> None:
        super().__init__(driver)
        self.database = driver.database
        self.emit = emit
        self.number = number
        self.session: str | None = None
        self.closed = False
        self._overlay: Overlay | None = None
        self._savepoints: list[tuple[str, Overlay, frozenset[tuple[str, Any]]]] = []
        self._released = asyncio.Event()

    def __repr__(self) -> str:
        return f"<MemoryConnection {self.number}>"

    @property
    def in_transaction(self) -> bool:
        return self._overlay is not None

    def record(self, call: str) -> None:
        """Add ``call`` to the trace of the calling context, if one is collected."""
        entries = _trace.get()
        if entries is not None:
            entries.append(TraceEntry(self.number, self.session, call))

    def _check_open(self) -> None:
        if self.closed:
            raise InterfaceError(f"memory connection {self.number} is closed")

    # Transactions

    async def begin(self) -> None:
        self._check_open()
        self.record("begin")
        if self._overlay is not None:
            raise ProgrammingError("a transaction is already open")
        self._overlay = {}

    async def commit(self) -> None:
        self._check_open()
        self.record("commit")
        overlay = self._require_transaction()
        for label, rows in overlay.items():
            table = self.database.tables[label]
            for pk, row in rows.items():
                if row is None:
                    table.pop(pk, None)
                else:
                    table[pk] = row
        self._end()

    async def rollback(self) -> None:
        self._check_open()
        self.record("rollback")
        self._require_transaction()
        self._end()

    async def _savepoint(self, name: str) -> None:
        self._check_open()
        self.record(f"savepoint {name}")
        overlay = self._require_transaction()
        self._savepoints.append((name, {label: dict(rows) for label, rows in overlay.items()}, frozenset(self._held())))

    async def _release_savepoint(self, name: str) -> None:
        self._check_open()
        self.record(f"release savepoint {name}")
        del self._savepoints[self._find_savepoint(name) :]

    async def _rollback_to_savepoint(self, name: str) -> None:
        self._check_open()
        self.record(f"rollback to savepoint {name}")
        index = self._find_savepoint(name)
        _, saved, held = self._savepoints[index]
        self._overlay = {label: dict(rows) for label, rows in saved.items()}
        del self._savepoints[index + 1 :]
        self._release_locks([key for key in self._held() if key not in held])

    def _find_savepoint(self, name: str) -> int:
        self._require_transaction()
        for index in range(len(self._savepoints) - 1, -1, -1):
            if self._savepoints[index][0] == name:
                return index
        raise ProgrammingError(f"savepoint {name!r} does not exist")

    def _require_transaction(self) -> Overlay:
        if self._overlay is None:
            raise ProgrammingError("no transaction is open")
        return self._overlay

    def _held(self) -> list[tuple[str, Any]]:
        return [key for key, owner in self.database.locks.items() if owner is self]

    def _release_locks(self, keys: Iterable[tuple[str, Any]]) -> None:
        """Release the row locks ``keys`` and wake the connections waiting for this one."""
        for key in keys:
            del self.database.locks[key]
        self._released.set()
        self._released = asyncio.Event()

    def _end(self) -> None:
        self._overlay = None
        self._savepoints.clear()
        self._release_locks(self._held())

    # Rows

    def _table(self, label: str) -> Table:
        """The rows this connection sees: the committed ones with the transaction's own writes."""
        committed = self.database.tables.get(label)
        if committed is None:
            raise NotSupportedError(f"the memory database has no table for {label}")
        rows = dict(committed)
        if self._overlay is not None:
            for pk, row in self._overlay.get(label, {}).items():
                if row is None:
                    rows.pop(pk, None)
                else:
                    rows[pk] = row
        return rows

    def _locked_by_other(self, key: tuple[str, Any]) -> MemoryConnection | None:
        """The open transaction of another connection that holds the lock ``key``; ``None`` when there is none."""
        owner = self.database.locks.get(key)
        if owner is None or owner is self or owner.closed or owner._overlay is None:
            return None
        return owner

    async def _lock(self, label: str, pk: Any) -> None:
        """Wait until no other transaction holds the lock of the row; inside a transaction, take it."""
        key = (label, pk)
        while True:
            owner = self._locked_by_other(key)
            if owner is None:
                if self._overlay is not None:
                    self.database.locks[key] = self
                return
            waiting: MemoryConnection | None = owner
            while waiting is not None:
                if waiting is self:
                    raise OperationalError("deadlock detected", sqlstate="40P01")
                waiting = self._waits_for(waiting)
            self.database.waiters[self] = (owner, key)
            released = owner._released
            try:
                await released.wait()
            finally:
                self.database.waiters.pop(self, None)

    def _waits_for(self, connection: MemoryConnection) -> MemoryConnection | None:
        """The connection whose lock ``connection`` waits for; ``None`` when it waits for none still held."""
        entry = self.database.waiters.get(connection)
        if entry is None:
            return None
        owner, key = entry
        return owner if self.database.locks.get(key) is owner else None

    def _write(self, label: str, pk: Any, row: dict[str, Any] | None) -> None:
        if self._overlay is not None:
            self._overlay.setdefault(label, {})[pk] = row
        elif row is None:
            self.database.tables[label].pop(pk, None)
        else:
            self.database.tables[label][pk] = row

    def _select(self, query: Query) -> list[dict[str, Any]]:
        """The model rows of ``query``, filtered and ordered, before projection and slicing."""
        rows = _Rows(query.model)
        if query.is_none:
            return []
        selected = [row for row in self._table(query.model).values() if rows.matches(query, row)]
        return rows.order(selected, query.ordering)

    def _read(self, query: Query) -> list[dict[str, Any]]:
        rows = _Rows(query.model).project(query, self._select(query))
        rows = rows[query.low_mark : query.high_mark]
        return copy.deepcopy(rows)

    async def _fetch(self, query: Query) -> list[dict[str, Any]]:
        """The rows of ``query``; with ``select_for_update()`` the rows are locked as PostgreSQL locks them."""
        lock = query.for_update
        if lock is None:
            return self._read(query)
        if self._overlay is None:
            raise TransactionManagementError("select_for_update cannot be used outside of a transaction.")
        rows = _Rows(query.model)
        wanted = query.high_mark
        locked: list[dict[str, Any]] = []
        for pk in [row[rows.pk] for row in self._select(query)]:
            if wanted is not None and len(locked) >= wanted:
                break
            if self._locked_by_other((query.model, pk)) is not None:
                if lock.nowait:
                    raise OperationalError(
                        f'could not obtain lock on row in relation "{query.model}"', sqlstate="55P03"
                    )
                if lock.skip_locked:
                    continue
            await self._lock(query.model, pk)
            current = self._table(query.model).get(pk)
            if current is not None and rows.matches(query, current):
                locked.append(current)
        # Rows skipped by the offset are locked too, as in PostgreSQL.
        return copy.deepcopy(rows.project(query, locked)[query.low_mark :])

    async def _insert(self, label: str, values: Mapping[str, Any]) -> dict[str, Any]:
        parents, _ = _inheritance(label)
        if parents:
            raise NotSupportedError(
                f"the memory driver does not insert rows of {label}, a multi-table inheritance child of "
                f"{', '.join(parents)}"
            )
        model = _model(label)
        pk = model._meta.pk
        row: dict[str, Any] = {}
        rows = _Rows(label)
        for name, value in values.items():
            row[rows.field(name)] = rows.value(value, row)
        for name, field in model.__pydantic_fields__.items():
            if row.get(name) is not None or (name in row and name != pk):
                continue
            if name == pk:
                if _is_int_field(model, name):
                    row[name] = self.database.next_pk(label)
                elif field.default_factory is not None:
                    row[name] = field.get_default(call_default_factory=True)
                else:
                    raise IntegrityError(f"{label}: the primary key {name!r} has no value", sqlstate="23502")
            elif field.is_required():
                raise IntegrityError(f"{label}: the field {name!r} has no value", sqlstate="23502")
            else:
                row[name] = field.get_default(call_default_factory=True)
        await self._lock(label, row[pk])
        if row[pk] in self._table(label):
            raise IntegrityError(f"{label}: a row with the primary key {row[pk]!r} exists", sqlstate="23505")
        row = copy.deepcopy(row)
        self._write(label, row[pk], row)
        return copy.deepcopy(row)

    async def _update(self, query: Query, values: Iterable[tuple[str, Any]]) -> int:
        rows = _Rows(query.model)
        values = list(values)
        changed = 0
        for pk in [row[rows.pk] for row in self._select(query)]:
            await self._lock(query.model, pk)
            current = self._table(query.model).get(pk)
            if current is None or not rows.matches(query, current):
                continue
            updated = dict(current)
            for name, value in values:
                updated[rows.field(name)] = copy.deepcopy(rows.value(value, current))
            self._write(query.model, pk, updated)
            await self._share(query.model, pk, {rows.field(name): updated[rows.field(name)] for name, _ in values})
            changed += 1
        return changed

    async def _share(self, label: str, pk: Any, values: Mapping[str, Any]) -> None:
        """Copy ``values`` of the row ``pk`` of ``label`` to the row with that primary key of each model ``label``
        shares rows with through multi-table inheritance, for the fields that model has."""
        parents, children = _inheritance(label)
        for other in (*parents, *children):
            if other not in self.database.tables:
                continue
            row = self._table(other).get(pk)
            if row is None:
                continue
            await self._lock(other, pk)
            model = _model(other)
            shared = {name: value for name, value in values.items() if name in model.__pydantic_fields__}
            shared.pop(model._meta.pk, None)
            if shared:
                self._write(other, pk, {**row, **copy.deepcopy(shared)})

    def _reference(self, label: str, pk: Any) -> str | None:
        """A foreign key (``label.column``) of a row this connection sees that references the row ``pk`` of
        ``label``; ``None`` when no row does."""
        for model in registry:
            other = model._meta.label
            if other not in self.database.tables:
                continue
            for relation in model.__adorm_relations__.values():
                if not isinstance(relation, ForeignKey) or relation.target != label:
                    continue
                if any(row.get(relation.column) == pk for row in self._table(other).values()):
                    return f"{other}.{relation.column}"
        return None

    async def _delete(self, query: Query) -> tuple[int, dict[str, int]]:
        rows = _Rows(query.model)
        parents, _ = _inheritance(query.model)
        matched = []
        for pk in [row[rows.pk] for row in self._select(query)]:
            await self._lock(query.model, pk)
            current = self._table(query.model).get(pk)
            if current is not None and rows.matches(query, current):
                matched.append(pk)
        # Every row is checked before the first one is deleted, so a refusal deletes nothing.
        if matched and parents:
            raise NotSupportedError(
                f"the memory driver does not delete rows of {query.model}, a multi-table inheritance child of "
                f"{', '.join(parents)}"
            )
        for pk in matched:
            reference = self._reference(query.model, pk)
            if reference is not None:
                raise NotSupportedError(
                    f"the memory driver does not cascade deletes: {reference} references {query.model} {pk!r}"
                )
        for pk in matched:
            self._write(query.model, pk, None)
        return len(matched), ({query.model: len(matched)} if matched else {})

    async def _one(self, query: Query) -> dict[str, Any]:
        rows = await self._fetch(query)
        name = query.model.partition(".")[2]
        if not rows:
            raise ObjectDoesNotExist(f"{name} matching query does not exist.")
        if len(rows) > 1:
            raise MultipleObjectsReturned(f"get() returned more than one {name} -- it returned {len(rows)}!")
        return rows[0]

    async def _save(self, operation: Save) -> dict[str, Any] | None:
        model = _model(operation.model)
        pk_name = model._meta.pk
        values = dict(operation.values)
        if operation.pk is None or operation.force_insert:
            if operation.pk is not None:
                values[pk_name] = operation.pk
            return await self._insert(operation.model, values)
        if operation.update_fields is not None:
            values = {name: values[name] for name in operation.update_fields if name in values}
        query = Query(operation.model).filter(pk=operation.pk)
        if await self._update(query, values.items()):
            return None
        if operation.update_fields is not None:
            raise DatabaseError("Save with update_fields did not affect any rows.")
        if operation.force_update:
            raise DatabaseError("Forced update did not affect any rows.")
        return await self._insert(operation.model, {**values, pk_name: operation.pk})

    async def _get_or_create(self, operation: GetOrCreate) -> tuple[dict[str, Any], bool]:
        lookups = dict(operation.lookups)
        try:
            return await self._one(operation.query.filter(**lookups)), False
        except ObjectDoesNotExist:
            pass
        values = {name: value for name, value in lookups.items() if "__" not in name}
        values.update(operation.defaults)
        return await self._insert(operation.query.model, values), True

    async def _update_or_create(self, operation: UpdateOrCreate) -> tuple[dict[str, Any], bool]:
        lookups = dict(operation.lookups)
        query = operation.query.filter(**lookups)
        try:
            row = await self._one(query)
        except ObjectDoesNotExist:
            values = {name: value for name, value in lookups.items() if "__" not in name}
            values.update(operation.defaults if operation.create_defaults is None else operation.create_defaults)
            return await self._insert(operation.query.model, values), True
        pk = row[_model(operation.query.model)._meta.pk]
        await self._update(Query(operation.query.model).filter(pk=pk), operation.defaults)
        return await self._one(Query(operation.query.model).filter(pk=pk)), False

    async def _execute(self, operation: Any) -> Any:
        self._check_open()
        self.record(f"{operation.kind} {operation.model}")
        match operation:
            case Select():
                return await self._fetch(operation.query)
            case Get():
                return await self._one(operation.query)
            case First():
                rows = await self._fetch(operation.query[:1])
                return rows[0] if rows else None
            case Last():
                ordering = operation.query.ordering or (OrderBy(F("pk")),)
                reversed_ordering = tuple(
                    OrderBy(
                        order.expression,
                        descending=not order.descending,
                        nulls_first=order.nulls_last,
                        nulls_last=order.nulls_first,
                    )
                    for order in ordering
                )
                rows = await self._fetch(operation.query.order_by(*reversed_ordering)[:1])
                return rows[0] if rows else None
            case Count():
                return len(await self._fetch(operation.query))
            case Exists():
                return bool(await self._fetch(operation.query))
            case InBulk():
                if operation.field_name not in ("pk", _model(operation.model)._meta.pk):
                    raise NotSupportedError(
                        "the memory driver does not know unique constraints: in_bulk() takes the primary key"
                    )
                return await self._fetch(operation.select_query())
            case Create():
                return await self._insert(operation.model, dict(operation.values))
            case Save():
                return await self._save(operation)
            case Update():
                return await self._update(operation.query, operation.values)
            case Delete():
                return await self._delete(operation.query)
            case DeleteInstance():
                return await self._delete(Query(operation.model).filter(pk=operation.pk))
            case GetOrCreate():
                return await self._get_or_create(operation)
            case UpdateOrCreate():
                return await self._update_or_create(operation)
        raise NotSupportedError(f"the memory driver does not execute {operation.kind!r} operations")

    # Connection

    def cancel(self) -> None:
        """Nothing to do: a statement only waits on row locks, and cancelling the awaiting task ends the wait."""

    def is_usable(self) -> bool:
        return not self.closed

    async def close(self) -> None:
        if self.closed:
            return
        if self._overlay is not None:
            self._end()
        self.closed = True


class MemoryDriver(Driver):
    """A driver executing operations on a `MemoryDatabase` (default: `DATABASE`); its connections are numbered from
    1 in the order they open. A profile's ``connect`` hook is refused: there is no target connection to open."""

    name = "memory"
    mode = "async"
    capabilities = CAPABILITIES

    def __init__(self, database: MemoryDatabase | None = None) -> None:
        self.database = DATABASE if database is None else database
        self._numbers = itertools.count(1)

    async def open_connection(self, profile: Any, emit: Emit) -> MemoryConnection:
        if getattr(profile, "connect", None) is not None:
            raise InterfaceError("the memory driver opens no target connection; the profile's connect is refused")
        return MemoryConnection(self, emit, next(self._numbers))


def on_acquire(connection: MemoryConnection, profile: Any, session: Any) -> None:  # noqa: ARG001 - hook signature
    """Profile hook: assign the connection to ``session`` and record it."""
    connection.session = f"{session.using}/{session.session_name}"
    connection.record("acquire")


def on_release(connection: MemoryConnection, profile: Any, session: Any) -> None:  # noqa: ARG001 - hook signature
    """Profile hook: record the release and clear the assignment."""
    connection.record("release")
    connection.session = None


def django_tables(models: Iterable[type[Model]]) -> dict[str, list[dict[str, Any]]]:
    """The rows of every model's Django model, read through Django's base manager, keyed by adorm field names.

    Needs Django set up; runs synchronously and closes the thread's database connections afterwards.
    """
    from django.apps import apps
    from django.db import connections

    tables: dict[str, list[dict[str, Any]]] = {}
    try:
        for model in models:
            meta = model._meta
            django_model = apps.get_model(meta.label)
            names = {meta.orm_name(name): name for name in model.__pydantic_fields__}
            tables[meta.label] = [
                {names[key]: value for key, value in row.items()} for row in django_model._base_manager.values(*names)
            ]
    finally:
        connections.close_all()
    return tables
