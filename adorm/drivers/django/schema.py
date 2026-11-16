"""What the installed Django models expect of the database: their schema hashes and, for a
`adorm.operations.CheckTable` operation, their tables and columns."""

from __future__ import annotations

from typing import Any

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist
from django.db.models import ForeignObjectRel

from adorm.codegen.django import schema_hash
from adorm.drivers.postgresql import ExpectedColumn, ExpectedTable
from adorm.exceptions import SchemaMismatchError

__all__ = ["expected_tables", "model_schema_hash"]


def _model(label: str) -> Any:
    app_label, _, name = label.partition(".")
    try:
        return apps.get_model(app_label, name)
    except LookupError:
        raise LookupError(f"no Django model is installed as {label!r}") from None


def model_schema_hash(label: str) -> str:
    """The schema hash of the installed Django model ``label`` (`adorm.codegen.django.schema_hash()`, issuing no
    `adorm.codegen.CodegenWarning`). Raises ``LookupError`` when no such model is installed and
    `adorm.codegen.CodegenError` for a model the generator cannot describe."""
    return schema_hash(_model(label), warn=False)


class _Tables:
    """Expected tables in the order they are first named, each with its columns in the order they are added."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.columns: dict[str, dict[str, ExpectedColumn]] = {}

    def add(self, field: Any, name: str | None, *, check_type: bool = True) -> None:
        table = self.columns.setdefault(field.model._meta.db_table, {})
        if field.column not in table:
            nullable = bool(field.null) and not field.primary_key
            db_type = field.db_type(self.connection) if check_type else None
            table[field.column] = ExpectedColumn(field.column, db_type, nullable, name)

    def tables(self) -> list[ExpectedTable]:
        quote = self.connection.ops.quote_name
        return [ExpectedTable(name, quote(name), tuple(columns.values())) for name, columns in self.columns.items()]


def _relation(opts: Any, name: str) -> Any:
    """The field or relation ``name`` of the model ``opts``: a field by name or attname, a reverse relation by its
    accessor name, as adorm names reverse relations; ``None`` when there is none."""
    try:
        return opts.get_field(name)
    except FieldDoesNotExist:
        pass
    for field in opts.get_fields():
        if isinstance(field, ForeignObjectRel) and field.get_accessor_name() == name:
            return field
    return None


def _unknown(name: str, table: str | None = None) -> dict[str, Any]:
    return {"kind": "unknown_field", "table": table, "column": None, "field": name, "expected": None, "actual": None}


def expected_tables(
    operation: Any, connection: Any, driver: str | None = None
) -> tuple[list[ExpectedTable], list[dict[str, Any]]]:
    """The tables and columns the `adorm.operations.CheckTable` ``operation`` expects, with column types for the
    Django database wrapper ``connection`` (``field.db_type()``, which does not connect), and the ``unknown_field``
    problems of the names in ``operation.fields`` that are no concrete field of the model and in
    ``operation.many_to_many`` that are no many-to-many relation of it.

    A field's column belongs to the table of the model that declares it (a multi-table inheritance parent's table for
    an inherited field). A many-to-many relation, a reverse one named by its accessor name, adds its automatic link
    table with all its columns, the primary key without its type, which adorm neither reads nor writes; a link model
    of its own is a model to check by itself. Raises `adorm.SchemaMismatchError` with ``actual`` ``None`` when no such
    model is installed; ``driver`` names the driver in it."""
    try:
        opts = _model(operation.model)._meta
    except LookupError as exc:
        raise SchemaMismatchError(
            f"the adorm model {operation.model} cannot be checked against the {driver} driver: {exc}",
            model=operation.model,
            driver=driver,
            reason=str(exc),
        ) from None
    tables = _Tables(connection)
    problems: list[dict[str, Any]] = []
    for name in operation.fields:
        try:
            field = opts.get_field(name)
        except FieldDoesNotExist:
            field = None
        if field is None or not getattr(field, "concrete", False) or field.column is None:
            problems.append(_unknown(name, opts.db_table))
            continue
        tables.add(field, name)
    for name in operation.many_to_many:
        relation = _relation(opts, name)
        if relation is None or not relation.many_to_many:
            problems.append(_unknown(name, opts.db_table))
            continue
        through = relation.through if isinstance(relation, ForeignObjectRel) else relation.remote_field.through
        if through._meta.auto_created:
            for field in through._meta.concrete_fields:
                tables.add(field, None, check_type=not field.primary_key)
    return tables.tables(), problems
