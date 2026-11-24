"""`TenantQuerySet`: the rows a `adorm.TenantRelation` reads across the tenant schemas of its owner.

A terminal call runs two operations on the owner's executor: a `Select` of the owner's schema names, then one
statement over the rows of every schema (``UNION ALL``), whose ordering, slice, count and aggregates the database
applies. Building does no I/O.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, NoReturn, Self, TYPE_CHECKING

from adorm.exceptions import MultipleObjectsReturned, NotSupportedError, ObjectDoesNotExist
from adorm.query.expressions import (
    Aggregate as AggregateExpression,
    Case,
    Cast,
    Expression,
    F,
    Node,
    OrderBy,
    Star,
    Value,
    When,
)
from adorm.query.query import Query, TenantSchema
from adorm.query.queryset import _empty_aggregation, _row_class

if TYPE_CHECKING:
    from adorm.models.base import Executor, Model
    from adorm.models.relations import TenantRelation
    from adorm.operations import Operation

__all__ = ["HIDDEN_PREFIX", "TenantQuerySet"]

HIDDEN_PREFIX = "_adorm_"
"""The prefix of the names a `TenantQuerySet` adds to the rows it reads and removes from the rows it returns; an
annotation name with it raises ``ValueError``."""

_SCHEMA = "_adorm_schema"
_ORDER = "_adorm_order_"
_AGGREGATE = "_adorm_agg_"


def _refused(name: str) -> Any:
    """A method that raises `adorm.NotSupportedError` for ``name`` (a method name, or an operator)."""
    what = name if not name.isidentifier() else f"{name}()"

    def refuse(self: TenantQuerySet, *args: Any, **kwargs: Any) -> NoReturn:  # noqa: ARG001
        raise NotSupportedError(f"{what} is not supported on a tenant relation")

    refuse.__doc__ = f"Raises `adorm.NotSupportedError`: {what} is not supported on a tenant relation."
    return refuse


def _reversed(order: OrderBy) -> OrderBy:
    return order._replace(descending=not order.descending, nulls_first=order.nulls_last, nulls_last=order.nulls_first)


def _rewrite_aggregates(node: Node, replace: Any) -> Node:
    """``node`` with each aggregate outside subqueries replaced by ``replace(aggregate)``."""
    if isinstance(node, AggregateExpression):
        return replace(node)
    if isinstance(node, Query):
        return node
    changes: dict[str, Any] = {}
    for name in node._fields:
        value = getattr(node, name)
        if isinstance(value, Node):
            new: Any = _rewrite_aggregates(value, replace)
        elif isinstance(value, tuple) and any(isinstance(item, Node) for item in value):
            new = tuple(_rewrite_aggregates(item, replace) if isinstance(item, Node) else item for item in value)
        else:
            continue
        if new is not value:
            changes[name] = new
    return node._replace(**changes) if changes else node


class TenantQuerySet:
    """The rows of ``model`` (the target model class of ``relation``, a `adorm.TenantRelation`) that belong to
    ``owner`` in every tenant schema the owner belongs to, read on ``executor`` (the owner's).

    ``query`` is the query of one schema's rows: the target's ``objects`` manager filtered by the relation's column,
    with the building methods applied. The building methods are those of `adorm.QuerySet` that a read across schemas
    supports; each returns a new tenant query set. Annotations that contain an aggregate, ``distinct()``,
    ``prefetch_related()``, ``select_for_update()``, set operations and every write raise `adorm.NotSupportedError`
    when called, before any I/O; the instances it returns are written with their own ``save()`` and ``delete()``.

    A terminal call lists the owner's schemas (`schemas()`) with one `adorm.operations.Select` and, when there are
    any, runs one more operation over the union of the rows of every schema. The ordering and the slice apply to the
    rows of all schemas together; without schemas the result is empty and the second operation does not run.
    """

    __slots__ = ("executor", "model", "owner", "query", "relation")

    relation: TenantRelation
    owner: Model
    model: type[Model]
    executor: Executor
    query: Query

    def __init__(self, relation: TenantRelation, owner: Model, query: Query | None = None) -> None:
        from adorm.models.relations import target_queryset

        pk = owner.pk
        if pk is None:
            raise ValueError(f"{type(owner).__name__}.{relation.name} needs a primary key, the instance has none")
        executor = owner._executor()
        if query is None:
            query = target_queryset(relation, executor).filter(**{relation.column: pk}).query
        object.__setattr__(self, "relation", relation)
        object.__setattr__(self, "owner", owner)
        object.__setattr__(self, "model", relation.target_model())
        object.__setattr__(self, "executor", executor)
        object.__setattr__(self, "query", query)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"a tenant query set is immutable: cannot set {name!r}")

    def __repr__(self) -> str:
        return f"<TenantQuerySet {type(self.owner).__name__}.{self.relation.name} {self.query!r}>"

    def _clone(self, query: Query) -> Self:
        return type(self)(self.relation, self.owner, query)

    # Building: no I/O

    def all(self) -> Self:
        """A copy of the tenant query set."""
        return self._clone(self.query)

    def filter(self, *args: Any, **kwargs: Any) -> Self:
        """`adorm.Query.filter()`."""
        return self._clone(self.query.filter(*args, **kwargs))

    def exclude(self, *args: Any, **kwargs: Any) -> Self:
        """`adorm.Query.exclude()`."""
        return self._clone(self.query.exclude(*args, **kwargs))

    def _check_annotations(self, annotations: dict[str, Any]) -> None:
        for name, expression in annotations.items():
            if isinstance(name, str) and name.startswith(HIDDEN_PREFIX):
                raise ValueError(f"{name!r}: names starting with {HIDDEN_PREFIX!r} are reserved on a tenant relation")
            if isinstance(expression, Expression) and expression.contains_aggregate:
                raise NotSupportedError(f"the aggregate annotation {name!r} is not supported on a tenant relation")

    def annotate(self, **annotations: Any) -> Self:
        """`adorm.Query.annotate()`, without aggregates."""
        self._check_annotations(annotations)
        return self._clone(self.query.annotate(**annotations))

    def alias(self, **annotations: Any) -> Self:
        """`adorm.Query.alias()`, without aggregates."""
        self._check_annotations(annotations)
        return self._clone(self.query.alias(**annotations))

    def order_by(self, *keys: Any) -> Self:
        """`adorm.Query.order_by()`: the rows of every schema are ordered together."""
        return self._clone(self.query.order_by(*keys))

    def values(self, *fields: str) -> Self:
        """`adorm.Query.values()`: rows as dicts."""
        return self._clone(self.query.values(*fields))

    def values_list(self, *fields: str, flat: bool = False, named: bool = False) -> Self:
        """`adorm.Query.values_list()`: rows as tuples, single values (``flat``) or named tuples (``named``)."""
        return self._clone(self.query.values_list(*fields, flat=flat, named=named))

    def only(self, *fields: str) -> Self:
        """`adorm.Query.only()`."""
        return self._clone(self.query.only(*fields))

    def defer(self, *fields: str | None) -> Self:
        """`adorm.Query.defer()`."""
        return self._clone(self.query.defer(*fields))

    def select_related(self, *fields: str | None) -> Self:
        """`adorm.Query.select_related()`: a related row of a model the relation's tenant models include is read in
        the same schema."""
        return self._clone(self.query.select_related(*fields))

    def none(self) -> Self:
        """`adorm.Query.none()`: terminal calls return an empty result without an operation."""
        return self._clone(self.query.none())

    def __getitem__(self, key: slice) -> Self:
        return self._clone(self.query[key])

    create = _refused("create")
    bulk_create = _refused("bulk_create")
    get_or_create = _refused("get_or_create")
    update_or_create = _refused("update_or_create")
    create_from_parent = _refused("create_from_parent")
    update = _refused("update")
    delete = _refused("delete")
    in_bulk = _refused("in_bulk")
    add = _refused("add")
    remove = _refused("remove")
    clear = _refused("clear")
    set = _refused("set")
    select_for_update = _refused("select_for_update")
    distinct = _refused("distinct")
    prefetch_related = _refused("prefetch_related")
    union = _refused("union")
    intersection = _refused("intersection")
    difference = _refused("difference")
    __or__ = _refused("|")
    __and__ = _refused("&")

    # Schemas

    def _listing(self) -> Query:
        """The query of the owner's schema names: distinct, without ``NULL``, sorted."""
        owner = self.owner
        schema = self.relation.schema
        expression = F(schema) if isinstance(schema, str) else schema
        return (
            Query(type(owner)._require_meta().label)
            .filter(pk=owner.pk)
            .annotate(**{_SCHEMA: expression})
            .exclude(**{_SCHEMA: None})
            .values_list(_SCHEMA, flat=True)
            .distinct()
            .order_by(_SCHEMA)
        )

    async def _scopes(self) -> list[TenantSchema]:
        from adorm.operations import Select

        models = self.relation.models
        rows = await self.executor.execute(Select(self._listing()))
        return [TenantSchema(row[_SCHEMA], models) for row in rows]

    async def schemas(self) -> list[str]:
        """The owner's schema names, sorted and distinct, read with a `adorm.operations.Select` operation. A value
        that is not a schema name (`adorm.TenantSchema`) raises ``ValueError``."""
        return [scope.schema for scope in await self._scopes()]

    # Branches and their union

    def _base(self) -> Query:
        return self.query._replace(ordering=(), low_mark=0, high_mark=None)

    def _ordering(self, default: tuple[OrderBy, ...] = ()) -> tuple[OrderBy, ...]:
        return self.query.ordering or default

    def _combined(self, scopes: list[TenantSchema], branch: Any, ordering: tuple[OrderBy, ...]) -> Query:
        """The union of ``branch(query, scope)`` over ``scopes`` (the query itself for one), each in its schema with
        the ordering keys as hidden annotations, ordered by them and sliced as the query is."""
        keys = {f"{_ORDER}{index}": order.expression for index, order in enumerate(ordering)}
        parts = []
        for scope in scopes:
            part = branch(self._base().in_tenant_schema(scope.schema, scope.models), scope)
            parts.append(part.annotate(**keys) if keys else part)
        combined = parts[0].union(*parts[1:], all=True)
        if ordering:
            combined = combined.order_by(
                *(order._replace(expression=F(name)) for name, order in zip(keys, ordering, strict=True))
            )
        if self.query.is_sliced:
            combined = combined[self.query.low_mark : self.query.high_mark]
        return combined

    def _rows_query(self, scopes: list[TenantSchema], ordering: tuple[OrderBy, ...]) -> Query:
        def branch(query: Query, scope: TenantSchema) -> Query:
            if query.row_format == "model":
                return query.annotate(**{_SCHEMA: Cast(Value(scope.schema), str)})
            return query

        return self._combined(scopes, branch, ordering)

    async def _run(self, operation: Operation) -> Any:
        try:
            return await self.executor.execute(operation)
        except ObjectDoesNotExist as exc:
            if type(exc) is not ObjectDoesNotExist:
                raise
            raise self.model.DoesNotExist(*exc.args) from exc
        except MultipleObjectsReturned as exc:
            if type(exc) is not MultipleObjectsReturned:
                raise
            raise self.model.MultipleObjectsReturned(*exc.args) from exc

    def _format(self, row: dict[str, Any], scopes: dict[str, TenantSchema]) -> Any:
        row = dict(row)
        schema = row.pop(_SCHEMA, None)
        for name in [name for name in row if name.startswith(HIDDEN_PREFIX)]:
            del row[name]
        row_format = self.query.row_format
        if row_format == "model":
            scope = scopes[schema]
            executor = self.relation.session_for(scope.schema, self.executor)
            return self.model.from_row(row, executor=executor, tenant_schema=scope)
        if row_format == "dict":
            return row
        names = self.query.select or tuple(row)
        if row_format == "flat":
            return row[names[0]]
        values = tuple(row[name] for name in names)
        return values if row_format == "tuple" else _row_class(names)(*values)

    async def _select(self, ordering: tuple[OrderBy, ...], *, first: bool = False) -> list[Any]:
        from adorm.operations import Select

        scopes = await self._scopes()
        if not scopes:
            return []
        query = self._rows_query(scopes, ordering)
        if first:
            query = query[:1]
        by_schema = {scope.schema: scope for scope in scopes}
        return [self._format(row, by_schema) for row in await self._run(Select(query))]

    # Terminal

    async def __aiter__(self) -> AsyncIterator[Any]:
        """The rows of every schema, formatted by the query's row format: instances whose
        `adorm.ModelState.tenant_schema` is the schema they were read in, dicts or tuples."""
        if self.query.is_empty:
            return
        for row in await self._select(self._ordering()):
            yield row

    def _default_ordering(self) -> tuple[OrderBy, ...]:
        return self._ordering((OrderBy(F("pk")),))

    async def first(self) -> Any:
        """The first row of every schema's rows together, by the ordering (the primary key without one); ``None``
        when there is none."""
        if self.query.is_empty:
            return None
        rows = await self._select(self._default_ordering(), first=True)
        return rows[0] if rows else None

    async def last(self) -> Any:
        """The last row of every schema's rows together, by the ordering (the primary key without one); ``None`` when
        there is none. A sliced tenant query set raises ``TypeError``."""
        if self.query.is_sliced:
            raise TypeError("cannot reverse a query once a slice has been taken")
        if self.query.is_empty:
            return None
        rows = await self._select(tuple(_reversed(order) for order in self._default_ordering()), first=True)
        return rows[0] if rows else None

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        """The single row of every schema's rows matching the conditions (arguments as for `filter()`), with a
        `adorm.operations.Get` operation. No row raises the target's ``DoesNotExist``, several rows (the same primary
        key in two schemas included) its ``MultipleObjectsReturned``."""
        from adorm.operations import Get

        queryset = self.filter(*args, **kwargs) if args or kwargs else self
        name = self.model.__name__
        if queryset.query.is_empty:
            raise self.model.DoesNotExist(f"{name} matching query does not exist.")
        scopes = await queryset._scopes()
        if not scopes:
            raise self.model.DoesNotExist(f"{name} matching query does not exist.")
        query = queryset._rows_query(scopes, queryset._ordering())
        row = await queryset._run(Get(query))
        return queryset._format(row, {scope.schema: scope for scope in scopes})

    def _plain(self, scopes: list[TenantSchema]) -> Query:
        ordering = self._ordering() if self.query.is_sliced else ()
        return self._combined(scopes, lambda query, scope: query, ordering)  # noqa: ARG005

    async def count(self) -> int:
        """The number of rows of every schema together (a `adorm.operations.Count` operation)."""
        from adorm.operations import Count

        if self.query.is_empty:
            return 0
        scopes = await self._scopes()
        return 0 if not scopes else await self._run(Count(self._plain(scopes)))

    async def exists(self) -> bool:
        """Whether a schema has a row (an `adorm.operations.Exists` operation)."""
        from adorm.operations import Exists

        if self.query.is_empty:
            return False
        scopes = await self._scopes()
        return False if not scopes else await self._run(Exists(self._plain(scopes)))

    async def aggregate(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """The values of aggregate expressions over the rows of every schema together, as a dict by name (an
        `adorm.operations.Aggregate` over the union).

        The arguments are those of `adorm.QuerySet.aggregate()`. Each aggregate's argument is computed in every
        schema's query (its ``filter`` folded in as a ``Case``) and the aggregate is taken over the union. Without
        schemas the result is that of no rows: ``0`` for `adorm.Count`, the ``default`` or ``None`` for the others.
        """
        from adorm.operations import Aggregate

        aggregations: dict[str, Any] = {}
        for expression in args:
            if not isinstance(expression, AggregateExpression):
                raise TypeError(f"a positional argument of aggregate() is an aggregate, not {expression!r}")
            name = expression.default_alias
            if name in aggregations:
                raise ValueError(f"the aggregation {name!r} is given twice")
            aggregations[name] = expression
        for name, expression in kwargs.items():
            if name in aggregations:
                raise ValueError(f"the aggregation {name!r} is given twice")
            aggregations[name] = expression
        if not aggregations:
            return {}
        Aggregate(self.query, aggregations)
        inputs: dict[str, Expression] = {}

        def replace(aggregate: AggregateExpression) -> AggregateExpression:
            (source,) = aggregate.expressions
            if aggregate.filter is not None:
                value = Value(1) if isinstance(source, Star) else source
                source = Case(When(aggregate.filter, then=value))
            elif isinstance(source, Star):
                return aggregate
            name = f"{_AGGREGATE}{len(inputs)}"
            inputs[name] = source
            return aggregate._replace(expressions=(F(name),), filter=None)

        rewritten = {name: _rewrite_aggregates(expression, replace) for name, expression in aggregations.items()}
        empty = self.query.is_empty
        scopes = [] if empty else await self._scopes()
        if not scopes:
            result = _empty_aggregation(aggregations.items())
            return result if result is not None else dict.fromkeys(aggregations)
        if not inputs:
            inputs[f"{_AGGREGATE}0"] = Value(1)
        ordering = self._ordering() if self.query.is_sliced else ()

        def branch(query: Query, scope: TenantSchema) -> Query:  # noqa: ARG001
            return query.annotate(**inputs).values(*inputs)

        combined = self._combined(scopes, branch, ordering)
        return dict(await self._run(Aggregate(combined, rewritten)))
