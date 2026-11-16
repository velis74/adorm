"""The schema check: whether the database tables and the driver's models match the adorm models."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal, TYPE_CHECKING

from adorm.exceptions import SchemaCheckError, SchemaMismatchError
from adorm.models.base import get_relations, Model
from adorm.models.registry import registry
from adorm.models.relations import ManyToMany
from adorm.operations import CheckTable

if TYPE_CHECKING:
    from adorm.executor.sessions import Session

__all__ = ["SchemaProblem", "SchemaProblemKind", "check_models", "check_schema", "table_check"]

SchemaProblemKind = Literal[
    "schema_hash", "missing_table", "missing_column", "column_type", "nullability", "unknown_field", "required_column"
]
"""What a `SchemaProblem` reports:

- ``schema_hash``: the model's schema hash ``expected`` differs from the driver's ``actual``, or the driver has no hash
  to compare it with (``actual`` ``None``, ``detail`` says why) (`adorm.SchemaMismatchError`);
- ``missing_table``: the database has no table ``table``;
- ``missing_column``: the table has no column ``column`` for ``field``;
- ``column_type``: the column's type ``actual`` differs from the ``expected`` one;
- ``nullability``: the column is ``actual`` (``"NULL"`` or ``"NOT NULL"``), the model expects ``expected``;
- ``unknown_field``: the driver's model has no field (or many-to-many relation) ``field``;
- ``required_column``: the table has a ``NOT NULL`` column without a default that the model does not write, so an
  insert fails.
"""

_KINDS = frozenset(SchemaProblemKind.__args__)  # type: ignore[attr-defined]


@dataclass(frozen=True)
class SchemaProblem:
    """A difference `check_schema()` found for the model ``model`` (a label).

    ``field`` is the adorm name of the model's field or relation, ``table`` and ``column`` name the database table and
    column, ``expected`` and ``actual`` the values compared, ``detail`` the reason of a ``schema_hash`` problem without
    ``actual``; each is ``None`` where it does not apply. ``str()`` gives a one-line description.
    """

    model: str
    kind: SchemaProblemKind
    table: str | None = None
    column: str | None = None
    field: str | None = None
    expected: str | None = None
    actual: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError(f"unknown schema problem kind {self.kind!r}")

    def __str__(self) -> str:
        where = self.model if self.field is None else f"{self.model}.{self.field}"
        column = f"{self.table}.{self.column}"
        if self.kind == "schema_hash":
            if self.actual is None:
                return f"{where}: cannot be compared with the driver's model: {self.detail or 'it has no schema hash'}"
            return f"{where}: generated with schema hash {self.expected}, the driver's model has {self.actual}"
        if self.kind == "missing_table":
            return f"{where}: the table {self.table} does not exist"
        if self.kind == "missing_column":
            return f"{where}: the column {column} does not exist"
        if self.kind in ("column_type", "nullability"):
            return f"{where}: the column {column} is {self.actual}, the model expects {self.expected}"
        if self.kind == "unknown_field":
            return f"{where}: the driver's model has no such field"
        return f"{where}: the column {column} ({self.actual}) is NOT NULL without a default; the model does not set it"


def _model(model: type[Model] | str) -> type[Model]:
    if isinstance(model, str):
        return registry.get(model)
    if not isinstance(model, type) or not issubclass(model, Model) or model._meta is None:
        raise TypeError(f"check_schema() takes model classes with _meta or their labels, not {model!r}")
    return model


def table_check(model: type[Model]) -> CheckTable:
    """The `CheckTable` operation of the model class ``model``: the ORM names of its fields and of its many-to-many
    relations."""
    meta = model._require_meta()
    many_to_many = [name for name, relation in get_relations(model).items() if isinstance(relation, ManyToMany)]
    return CheckTable(
        meta.label,
        tuple(meta.orm_name(name) for name in model.__pydantic_fields__),
        tuple(meta.orm_name(name) for name in many_to_many),
    )


def _problems(model: type[Model], operation: CheckTable, result: Any) -> list[SchemaProblem]:
    names = {orm_name: name for name, orm_name in _orm_names(model).items()}
    problems = []
    for item in result:
        values = {key: item.get(key) for key in ("table", "column", "field", "expected", "actual")}
        if values["field"] is not None:
            values["field"] = names.get(values["field"], values["field"])
        problems.append(SchemaProblem(operation.model, item["kind"], **values))
    return problems


def _orm_names(model: type[Model]) -> dict[str, str]:
    """ORM name by adorm name, for the fields and relations of ``model``."""
    meta = model._require_meta()
    return {name: meta.orm_name(name) for name in (*model.__pydantic_fields__, *get_relations(model))}


async def check_models(
    models: Iterable[type[Model]], execute: Callable[[CheckTable], Awaitable[Any]]
) -> tuple[SchemaProblem, ...]:
    """The problems of ``models`` (model classes with ``_meta``), each checked with `table_check()` passed to
    ``execute``, in the order of ``models``; see `check_schema()`."""
    found: list[SchemaProblem] = []
    seen: set[tuple[Any, ...]] = set()
    for model in dict.fromkeys(models):
        operation = table_check(model)
        try:
            result = await execute(operation)
        except SchemaMismatchError as exc:
            if exc.model != operation.model:
                raise
            problem = SchemaProblem(
                operation.model, "schema_hash", expected=exc.expected, actual=exc.actual, detail=exc.reason
            )
            problems = [problem]
        else:
            problems = _problems(model, operation, result)
        for problem in problems:
            key = (problem.kind, problem.table, problem.column, problem.field) if problem.table else (problem,)
            if key not in seen:
                seen.add(key)
                found.append(problem)
    return tuple(found)


async def check_schema(
    models: Iterable[type[Model] | str] | None = None,
    *,
    session: Session | None = None,
    raise_on_problems: bool = False,
) -> tuple[SchemaProblem, ...]:
    """Compare ``models`` (model classes or labels; default: every registered model) with the database and the
    driver of ``session`` (default: the current session) and return the problems found, in the order of ``models``;
    an empty tuple means they match.

    Each model is checked with one `CheckTable` operation on the session, which also checks the model's schema hash
    against the driver's (`adorm.drivers.Driver.check_schema_hash()`): a mismatch, and a model the driver's target
    does not have, is a ``schema_hash`` problem and the model's tables are not compared. A problem of a table that
    several models share (a proxy model and its concrete model, a parent table) is reported once, for the first of
    them. With ``raise_on_problems`` it raises `SchemaCheckError` holding the problems instead of returning them.

    A label without a registered model raises ``LookupError``, anything else that is not a model class with ``_meta``
    ``TypeError``; a driver that does not support ``check_table`` raises `NotSupportedError`, and errors of the
    database pass through.
    """
    from adorm.executor.sessions import current_session

    session = current_session() if session is None else session
    selected = list(registry) if models is None else [_model(model) for model in models]
    found = await check_models(selected, session.execute)
    if raise_on_problems and found:
        raise SchemaCheckError(problems=found)
    return found
