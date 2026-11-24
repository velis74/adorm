"""Schema qualification of tenant tables in the SQL Django's compiler builds, for the Django-based drivers.

A Django query that `adorm.drivers.django.translation` built from a query with a `adorm.TenantSchema` carries its
scope: the schema and the tables of the scope's models. The compiler classes of `qualifying_operations()` write each of
those tables as ``schema.table`` where Django writes the table's name at the head of a ``FROM`` item or a join, of an
insert, an update and a delete, keeping the table's own name as the alias that column references use. Subqueries
compiled inside a scoped query take its scope; `tenant_scope()` scopes every query compiled in a block (a write and
the queries it runs). No SQL text is scanned: only the head Django renders for the table is replaced, and a head of
another shape raises `adorm.NotSupportedError`.
"""

from __future__ import annotations

import contextlib
import contextvars

from collections.abc import Iterator
from typing import Any

from django.db.models.sql import compiler as django_compiler
from django.db.models.sql.datastructures import BaseTable, Join

from adorm.exceptions import NotSupportedError
from adorm.query.query import TenantSchema

__all__ = ["SCOPE_ATTRIBUTE", "qualifying_operations", "scope_of", "tag", "tenant_scope"]

SCOPE_ATTRIBUTE = "_adorm_tenant_scope"
"""The attribute of a Django ``Query`` that holds its scope: ``(schema, db_tables)``."""

Scope = tuple[str, frozenset[str]]

_SCOPE: contextvars.ContextVar[Scope | None] = contextvars.ContextVar("adorm_tenant_scope", default=None)


def scope_of(tenant_schema: TenantSchema) -> Scope:
    """The schema of ``tenant_schema`` and the database tables of its models. A model that is not installed raises
    `adorm.ProgrammingError`."""
    from adorm.drivers.django.translation import django_model

    tables = frozenset(django_model(label)._meta.db_table for label in tenant_schema.models)
    return tenant_schema.schema, tables


def tag(django_query: Any, tenant_schema: TenantSchema | None) -> None:
    """Set the scope of ``tenant_schema`` on the Django query ``django_query`` (nothing for ``None``); Django's clones
    of the query keep it."""
    if tenant_schema is not None:
        setattr(django_query, SCOPE_ATTRIBUTE, scope_of(tenant_schema))


@contextlib.contextmanager
def tenant_scope(tenant_schema: TenantSchema | None) -> Iterator[None]:
    """Qualify the tables of ``tenant_schema``'s models in every query compiled in the block, in the current context;
    nothing for ``None``."""
    if tenant_schema is None:
        yield
        return
    token = _SCOPE.set(scope_of(tenant_schema))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def _scoped(compiler: Any) -> Scope | None:
    return getattr(compiler.query, SCOPE_ATTRIBUTE, None) or _SCOPE.get()


def _refuse(table: str) -> NotSupportedError:
    return NotSupportedError(f"cannot qualify the table {table!r} with a schema on this database backend")


def _qualified(compiler: Any, schema: str, table: str) -> str:
    quote = compiler.connection.ops.quote_name
    return f"{quote(schema)}.{quote(table)}"


def _replace_head(sql: str, head: str, replacement: str, table: str) -> str:
    if not sql.startswith(head):
        raise _refuse(table)
    return replacement + sql[len(head) :]


class _QualifyingSelect:
    """A compiler that qualifies the scope's tables in ``FROM`` items and joins."""

    def as_sql(self, *args: Any, **kwargs: Any) -> Any:
        scope = _scoped(self)
        if scope is None or scope is _SCOPE.get():
            return super().as_sql(*args, **kwargs)  # type: ignore[misc]
        token = _SCOPE.set(scope)
        try:
            return super().as_sql(*args, **kwargs)  # type: ignore[misc]
        finally:
            _SCOPE.reset(token)

    def compile(self, node: Any) -> Any:
        sql, params = super().compile(node)  # type: ignore[misc]
        scope = _SCOPE.get()
        if scope is None or not isinstance(node, BaseTable | Join):
            return sql, params
        schema, tables = scope
        table = node.table_name
        if table not in tables:
            return sql, params
        head = self.quote_name_unless_alias(table)  # type: ignore[attr-defined]
        lead = f"{node.join_type} " if isinstance(node, Join) else ""
        alias = f" {head}" if node.table_alias == table else ""
        replacement = f"{lead}{_qualified(self, schema, table)}{alias}"
        return _replace_head(sql, f"{lead}{head}", replacement, table), params


class _QualifyingWrite(_QualifyingSelect):
    """A write compiler that also qualifies the table at the head of its statement."""

    def _head(self) -> tuple[str, str, str]:
        """The statement's table, the keywords before it and the table as the statement names it."""
        raise NotImplementedError

    def _qualify(self, sql: str) -> str:
        scope = _SCOPE.get()
        table, keyword, quoted = self._head()
        if scope is None or not sql or table not in scope[1]:
            return sql
        return _replace_head(sql, keyword + quoted, keyword + _qualified(self, scope[0], table), table)

    def as_sql(self, *args: Any, **kwargs: Any) -> Any:
        scope = _scoped(self)
        token = _SCOPE.set(scope) if scope is not None and scope is not _SCOPE.get() else None
        try:
            result = super(_QualifyingSelect, self).as_sql(*args, **kwargs)  # type: ignore[misc]
            if isinstance(result, list):
                return [(self._qualify(sql), params) for sql, params in result]
            sql, params = result
            return self._qualify(sql), params
        finally:
            if token is not None:
                _SCOPE.reset(token)


class _QualifyingInsert(_QualifyingWrite):
    def _head(self) -> tuple[str, str, str]:
        ops = self.connection.ops  # type: ignore[attr-defined]
        table = self.query.get_meta().db_table  # type: ignore[attr-defined]
        statement = ops.insert_statement(on_conflict=self.query.on_conflict)  # type: ignore[attr-defined]
        return table, f"{statement} ", ops.quote_name(table)


class _QualifyingUpdate(_QualifyingWrite):
    def _head(self) -> tuple[str, str, str]:
        table = self.query.base_table  # type: ignore[attr-defined]
        return table, "UPDATE ", self.quote_name_unless_alias(table)  # type: ignore[attr-defined]


class _QualifyingDelete(_QualifyingWrite):
    def _head(self) -> tuple[str, str, str]:
        table = self.query.base_table  # type: ignore[attr-defined]
        return table, "DELETE FROM ", self.quote_name_unless_alias(table)  # type: ignore[attr-defined]


_COMPILERS: dict[type, type] = {}
_OPERATIONS: dict[type, type] = {}


def qualifying_compiler(cls: type) -> type:
    """``cls``, a Django compiler class, with the qualification of tenant tables; one class per ``cls``."""
    found = _COMPILERS.get(cls)
    if found is None:
        if issubclass(cls, django_compiler.SQLInsertCompiler):
            mixin: type = _QualifyingInsert
        elif issubclass(cls, django_compiler.SQLUpdateCompiler):
            mixin = _QualifyingUpdate
        elif issubclass(cls, django_compiler.SQLDeleteCompiler):
            mixin = _QualifyingDelete
        else:
            mixin = _QualifyingSelect
        found = _COMPILERS[cls] = type(cls.__name__, (mixin, cls), {"__module__": cls.__module__})
    return found


class _QualifyingOperations:
    def compiler(self, compiler_name: str) -> type:
        return qualifying_compiler(super().compiler(compiler_name))  # type: ignore[misc]


def qualifying_operations(ops_class: type) -> type:
    """``ops_class``, a Django database operations class, whose ``compiler()`` returns `qualifying_compiler()` classes;
    one class per ``ops_class``, and ``ops_class`` itself when it already is one."""
    if issubclass(ops_class, _QualifyingOperations):
        return ops_class
    found = _OPERATIONS.get(ops_class)
    if found is None:
        found = _OPERATIONS[ops_class] = type(
            ops_class.__name__, (_QualifyingOperations, ops_class), {"__module__": ops_class.__module__}
        )
    return found
