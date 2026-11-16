"""`QuerySet`: a `Query` on a model class with the executor that runs it.

Building methods return a new `QuerySet` and do no I/O. Terminal methods are coroutines that hand one operation to
the executor and turn its rows into model instances, dicts or tuples; those that return instances then run the
prefetches of `prefetch_related()`.
"""

from __future__ import annotations

from collections import namedtuple
from collections.abc import AsyncIterator, Iterable, Mapping
from functools import lru_cache
from typing import Any, Self, TYPE_CHECKING

from adorm.exceptions import (
    InterfaceError,
    MultipleObjectsReturned,
    ObjectDoesNotExist,
    ProtectedError,
    RestrictedError,
)
from adorm.query.query import Query

if TYPE_CHECKING:
    from adorm.models.base import Executor, Model
    from adorm.models.managers import Manager
    from adorm.operations import Operation

__all__ = ["QuerySet"]


@lru_cache(maxsize=256)
def _row_class(names: tuple[str, ...]) -> type:
    return namedtuple("Row", names, rename=True)  # noqa: PYI024 - field names are only known at run time


def _bind_error_objects(exc: BaseException, executor: Executor) -> None:
    """Turn the `adorm.operations.ModelRow` values in the objects of a `ProtectedError` or `RestrictedError` into
    instances of the registered models, bound to ``executor``; a row of a model that is not registered stays a
    `ModelRow`."""
    from adorm.models.registry import registry
    from adorm.operations import ModelRow

    if isinstance(exc, ProtectedError):
        attribute = "protected_objects"
    elif isinstance(exc, RestrictedError):
        attribute = "restricted_objects"
    else:
        return
    objects = []
    for item in getattr(exc, attribute):
        if isinstance(item, ModelRow):
            try:
                item = registry.get(item.model).from_row(item.row, executor=executor)
            except LookupError:
                pass
        objects.append(item)
    setattr(exc, attribute, tuple(objects))


def _stored_values(instance: Model, pk: str) -> dict[str, Any]:
    """The field values an insert of ``instance`` writes: its loaded and assigned fields, without a primary key of
    ``None``."""
    return {
        name: instance.__dict__[name]
        for name in type(instance).__pydantic_fields__
        if name in instance.model_fields_set and not (name == pk and instance.__dict__[name] is None)
    }


def _empty_aggregation(aggregations: Iterable[tuple[str, Any]]) -> dict[str, Any] | None:
    """The result of ``aggregations`` over no rows, or ``None`` when one of them needs the database for it."""
    from adorm.query.expressions import Aggregate, Count, Value

    result: dict[str, Any] = {}
    for name, expression in aggregations:
        if type(expression) is Count:
            result[name] = 0
        elif not isinstance(expression, Aggregate):
            return None
        elif expression.default is None:
            result[name] = None
        elif isinstance(expression.default, Value):
            result[name] = expression.default.value
        else:
            return None
    return result


def _rebuild(cls: type[QuerySet], model: type[Model], executor: Executor, query: Query, prefetch: tuple) -> QuerySet:
    queryset = cls(model, executor, query)
    object.__setattr__(queryset, "prefetch_lookups", prefetch)
    return queryset


class QuerySet:
    """The rows of ``query`` on ``model`` (a model class), run by ``executor`` (a session).

    ``Model.objects`` and ``session.Model`` create query sets; the building methods are those of `Query`, each
    returning a new query set of the same class. ``query`` is the underlying `Query`, so a query set can stand wherever
    a query is accepted, e.g. ``filter(pk__in=qs)``. ``prefetch_lookups`` holds the `adorm.Prefetch` lookups of
    `prefetch_related()`, which the terminal methods that return instances run after their operation. A subclass adds
    client-side methods that build on these and driver methods (`adorm.driver_method`); a manager's
    ``queryset_class`` names it.
    """

    __slots__ = ("_cache", "executor", "model", "prefetch_lookups", "query")

    model: type[Model]
    executor: Executor
    query: Query
    prefetch_lookups: tuple[Any, ...]

    def __init__(self, model: type[Model], executor: Executor, query: Query | None = None) -> None:
        meta = model._meta
        if meta is None:
            raise TypeError(f"{model.__name__} is abstract: it has no _meta")
        if query is None:
            query = Query(meta.label)
        elif query.model != meta.label:
            raise ValueError(f"a query on {query.model} is not a query on {meta.label}")
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "executor", executor)
        object.__setattr__(self, "query", query)
        object.__setattr__(self, "prefetch_lookups", ())
        object.__setattr__(self, "_cache", None)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"a query set is immutable: cannot set {name!r}")

    def __reduce__(self) -> tuple[Any, ...]:
        return _rebuild, (type(self), self.model, self.executor, self.query, self.prefetch_lookups)

    def __copy__(self) -> QuerySet:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> QuerySet:
        return self

    def _clone(self, query: Query, prefetch: tuple[Any, ...] | None = None) -> Self:
        clone = type(self)(self.model, self.executor, query)
        object.__setattr__(clone, "prefetch_lookups", self.prefetch_lookups if prefetch is None else prefetch)
        return clone

    def _with_cache(self, instances: Iterable[Model]) -> Self:
        """A copy whose ``async for``, `count()` and `exists()` use ``instances`` (prefetched rows) without an
        operation."""
        clone = self._clone(self.query)
        object.__setattr__(clone, "_cache", tuple(instances))
        return clone

    @classmethod
    def as_manager(cls, orm_name: str | None = None, *, method_manager: str | None = None) -> Manager:
        """A `Manager` whose ``queryset_class`` is this class:
        ``Manager.from_queryset(cls)(orm_name, method_manager=method_manager)``."""
        from adorm.models.managers import _queryset_as_manager

        return _queryset_as_manager(cls, orm_name, method_manager)

    def __repr__(self) -> str:
        prefetch = f" prefetch_related={list(self.prefetch_lookups)!r}" if self.prefetch_lookups else ""
        return f"<QuerySet {self.query!r}{prefetch} on {self.executor!r}>"

    # Building: no I/O

    def all(self) -> Self:
        """A copy of the query set."""
        return self._clone(self.query)

    def filter(self, *args: Any, **kwargs: Any) -> Self:
        """`Query.filter()`."""
        return self._clone(self.query.filter(*args, **kwargs))

    def exclude(self, *args: Any, **kwargs: Any) -> Self:
        """`Query.exclude()`."""
        return self._clone(self.query.exclude(*args, **kwargs))

    def annotate(self, **annotations: Any) -> Self:
        """`Query.annotate()`."""
        return self._clone(self.query.annotate(**annotations))

    def alias(self, **annotations: Any) -> Self:
        """`Query.alias()`."""
        return self._clone(self.query.alias(**annotations))

    def order_by(self, *keys: Any) -> Self:
        """`Query.order_by()`."""
        return self._clone(self.query.order_by(*keys))

    def values(self, *fields: str) -> Self:
        """`Query.values()`: rows as dicts."""
        self._check_no_prefetch("values")
        return self._clone(self.query.values(*fields))

    def values_list(self, *fields: str, flat: bool = False, named: bool = False) -> Self:
        """`Query.values_list()`: rows as tuples, single values (``flat``) or named tuples (``named``)."""
        self._check_no_prefetch("values_list")
        return self._clone(self.query.values_list(*fields, flat=flat, named=named))

    def _check_no_prefetch(self, action: str) -> None:
        if self.prefetch_lookups:
            raise TypeError(f"cannot call {action}() after prefetch_related()")

    def prefetch_related(self, *lookups: Any) -> Self:
        """A copy that loads the related objects of ``lookups`` (relation paths or `adorm.Prefetch` objects, added to
        the earlier ones; ``None`` clears them) for every instance it returns. Raises ``ValueError`` for a path given
        again with another query set, ``TypeError`` after ``values()`` or ``values_list()`` and
        `adorm.NotSupportedError` after a set operation."""
        from adorm.models.prefetch import normalize_lookups

        if lookups == (None,):
            return self._clone(self.query, ())
        self.query.check_not_combined("prefetch_related")
        if self.query.row_format != "model":
            raise TypeError("cannot call prefetch_related() after values() or values_list()")
        return self._clone(self.query, normalize_lookups(self.prefetch_lookups, lookups))

    def only(self, *fields: str) -> Self:
        """`Query.only()`."""
        return self._clone(self.query.only(*fields))

    def defer(self, *fields: str | None) -> Self:
        """`Query.defer()`."""
        return self._clone(self.query.defer(*fields))

    def select_related(self, *fields: str | None) -> Self:
        """`Query.select_related()`."""
        return self._clone(self.query.select_related(*fields))

    def distinct(self, *fields: str) -> Self:
        """`Query.distinct()`."""
        return self._clone(self.query.distinct(*fields))

    def none(self) -> Self:
        """`Query.none()`: terminal methods return an empty result without running an operation."""
        return self._clone(self.query.none())

    def select_for_update(
        self, *, nowait: bool = False, skip_locked: bool = False, of: Iterable[str] = (), no_key: bool = False
    ) -> Self:
        """`Query.select_for_update()`."""
        return self._clone(self.query.select_for_update(nowait=nowait, skip_locked=skip_locked, of=of, no_key=no_key))

    def __getitem__(self, key: slice) -> Self:
        return self._clone(self.query[key])

    def _combine(self, other: Any, combine: Any) -> Any:
        if not isinstance(other, QuerySet):
            return NotImplemented
        if other.executor is not self.executor:
            raise TypeError("cannot combine query sets of different sessions")
        return self._clone(combine(self.query, other.query))

    def _others(self, others: tuple[Any, ...]) -> list[QuerySet]:
        for other in others:
            if not isinstance(other, QuerySet):
                raise TypeError(f"a set operation combines query sets, not {type(other).__name__}")
            if other.executor is not self.executor:
                raise TypeError("cannot combine query sets of different sessions")
        return list(others)

    def union(self, *others: QuerySet, all: bool = False) -> QuerySet:
        """`Query.union()` of the query sets' queries; ``others`` are query sets of the same session. When this
        query set is empty, the other query sets that are not empty are combined instead, and a single one is
        returned as it is."""
        querysets = self._others(others)
        if self.query.is_empty:
            rest = [queryset for queryset in querysets if not queryset.query.is_empty]
            if rest:
                return rest[0] if len(rest) == 1 else rest[0].union(*rest[1:], all=all)
        return self._clone(self.query.union(*(queryset.query for queryset in querysets), all=all))

    def intersection(self, *others: QuerySet) -> QuerySet:
        """`Query.intersection()` of the query sets' queries; ``others`` are query sets of the same session. When
        one of the others is empty and this query set is not, that query set."""
        querysets = self._others(others)
        empty = next((queryset for queryset in querysets if queryset.query.is_empty), None)
        if empty is not None and not self.query.is_empty:
            return empty
        return self._clone(self.query.intersection(*(queryset.query for queryset in querysets)))

    def difference(self, *others: QuerySet) -> QuerySet:
        """`Query.difference()` of the query sets' queries; ``others`` are query sets of the same session."""
        querysets = self._others(others)
        return self._clone(self.query.difference(*(queryset.query for queryset in querysets)))

    def __or__(self, other: Any) -> Self:
        return self._combine(other, Query.__or__)

    def __and__(self, other: Any) -> Self:
        return self._combine(other, Query.__and__)

    # Rows

    def _instance(self, row: Any) -> Model:
        return self.model.from_row(row, executor=self.executor)

    def _format(self, row: Mapping[str, Any]) -> Any:
        row_format = self.query.row_format
        if row_format == "model":
            return self._instance(row)
        if row_format == "dict":
            return dict(row)
        names = self.query.select or tuple(row)
        if row_format == "flat":
            return row[names[0]]
        values = tuple(row[name] for name in names)
        return values if row_format == "tuple" else _row_class(names)(*values)

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
        except (ProtectedError, RestrictedError) as exc:
            _bind_error_objects(exc, self.executor)
            raise

    # Terminal: one operation each

    async def _prefetch(self, instances: list[Any]) -> None:
        if self.prefetch_lookups and instances:
            from adorm.models.prefetch import prefetch_related_objects

            await prefetch_related_objects(instances, *self.prefetch_lookups, executor=self.executor)

    async def __aiter__(self) -> AsyncIterator[Any]:
        """The rows of a `Select` operation, formatted by the query's row format, after the prefetches of
        `prefetch_related()`; the prefetched rows of a relation (no operation) for the query set a relation returns."""
        from adorm.operations import Select

        if self._cache is not None:
            for instance in self._cache:
                yield instance
            return
        if self.query.is_empty:
            return
        rows = [self._format(row) for row in await self._run(Select(self.query))]
        await self._prefetch(rows)
        for row in rows:
            yield row

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        """The single row matching the query and the conditions (arguments as for `filter()`), with a `Get`
        operation. No row raises the model's ``DoesNotExist``, several rows its ``MultipleObjectsReturned``."""
        from adorm.operations import Get

        query = self.query.filter(*args, **kwargs) if args or kwargs else self.query
        if query.is_empty:
            raise self.model.DoesNotExist(f"{self.model.__name__} matching query does not exist.")
        instance = self._format(await self._run(Get(query)))
        await self._prefetch([instance])
        return instance

    async def first(self) -> Any:
        """The first row (a `First` operation), ``None`` when there is none."""
        from adorm.operations import First

        if self.query.is_empty:
            return None
        row = await self._run(First(self.query))
        return None if row is None else await self._formatted(row)

    async def _formatted(self, row: Mapping[str, Any]) -> Any:
        instance = self._format(row)
        await self._prefetch([instance])
        return instance

    async def last(self) -> Any:
        """The last row (a `Last` operation), ``None`` when there is none."""
        from adorm.operations import Last

        if self.query.is_empty:
            return None
        row = await self._run(Last(self.query))
        return None if row is None else await self._formatted(row)

    async def count(self) -> int:
        """The number of rows (a `Count` operation)."""
        from adorm.operations import Count

        if self._cache is not None:
            return len(self._cache)
        return 0 if self.query.is_empty else await self._run(Count(self.query))

    async def exists(self) -> bool:
        """Whether the query has a row (an `Exists` operation)."""
        from adorm.operations import Exists

        if self._cache is not None:
            return bool(self._cache)
        return False if self.query.is_empty else await self._run(Exists(self.query))

    async def in_bulk(self, id_list: Iterable[Any] | None = None, *, field_name: str = "pk") -> dict[Any, Model]:
        """The instances whose field ``field_name`` (``"pk"``, a unique field or a unique foreign key, which is keyed
        by its column) holds one of ``id_list``, every instance for ``None``, as a dict keyed by that value; with an
        `InBulk` operation and the prefetches of `prefetch_related()`."""
        from adorm.models.relations import ForeignKey
        from adorm.operations import InBulk

        relation = self.model.__adorm_relations__.get(field_name)
        if isinstance(relation, ForeignKey):
            field_name = relation.column
        ids = None if id_list is None else list(id_list)
        operation = InBulk(self.query, field_name, ids)
        if self.query.is_empty or ids == []:
            return {}
        key = self.model._require_meta().pk if field_name == "pk" else field_name
        instances = [self._instance(row) for row in await self._run(operation)]
        await self._prefetch(instances)
        return {instance.__dict__[key]: instance for instance in instances}

    async def aggregate(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """The values of aggregate expressions over the rows (an `Aggregate` operation), as a dict by name.

        Keyword arguments name their expression; a positional aggregate of one field takes its `default_alias`
        (``Sum("fee")`` → ``"fee__sum"``); a name given twice raises ``ValueError``. Without arguments the result is
        ``{}``. On an empty query (`Query.is_empty`) of plain aggregates the result is computed without an operation:
        ``0`` for `Count`, the ``default`` value or ``None`` for the others.
        """
        from adorm.operations import Aggregate
        from adorm.query.expressions import Aggregate as AggregateExpression

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
        operation = Aggregate(self.query, aggregations)
        if self.query.is_empty:
            empty = _empty_aggregation(operation.aggregations)
            if empty is not None:
                return empty
        return dict(await self._run(operation))

    async def update(self, **values: Any) -> int:
        """Set ``values`` on the rows (an `Update` operation); returns the number of rows."""
        from adorm.operations import Update

        if not values:
            raise TypeError("update() needs at least one field value")
        operation = Update(self.query, values)
        return 0 if self.query.is_empty else await self._run(operation)

    async def delete(self) -> tuple[int, dict[str, int]]:
        """Delete the rows and those that cascade from them (a `Delete` operation); returns
        ``(total, {label: count})``. A reference that forbids the delete raises `ProtectedError` or `RestrictedError`,
        whose objects are instances of the registered models."""
        from adorm.operations import Delete

        operation = Delete(self.query)
        if self.query.is_empty:
            return 0, {}
        total, counts = await self._run(operation)
        return total, dict(counts)

    async def create(self, **values: Any) -> Model:
        """A new instance validated from ``values`` (as the model's constructor takes them) and inserted with a
        `Create` operation. The instance gets the values the target set (primary key, defaults).

        When the query carries an ORM manager (`Query.manager`), the operation carries it too and the values that are
        not fields go along to the target manager's ``create()``: an override of it may take arguments that are not
        fields (on the Django driver; the native PostgreSQL driver refuses an override). Without one, such values stay
        extra values of the instance. A content type a generic foreign key keyword left unset is resolved on the query
        set's executor first (`adorm.GenericForeignKey`).
        """
        from adorm.operations import Create

        instance = self.model(**values)
        if instance._state.generic:
            await instance._resolve_generic_foreign_keys(self.executor)
        stored = _stored_values(instance, self.model._require_meta().pk)
        manager = self.query.manager
        if manager is not None and instance.__pydantic_extra__:
            stored.update(instance.__pydantic_extra__)
        instance._stored(await self._run(Create(self.query.model, stored, manager=manager)), self.executor)
        return instance

    async def bulk_create(
        self,
        objs: Iterable[Model],
        batch_size: int | None = None,
        ignore_conflicts: bool = False,
        update_conflicts: bool = False,
        update_fields: Iterable[str] | None = None,
        unique_fields: Iterable[str] | None = None,
    ) -> list[Model]:
        """Insert the instances ``objs`` with one `BulkCreate` operation and return them as a list, in order, each with
        the values the target set (a generated primary key, except under ``ignore_conflicts``).

        Each instance writes its loaded and assigned fields (``model_fields_set``; a primary key of ``None`` is left
        to the target), so the instances may set different fields. The arguments are those of `BulkCreate`. An
        object that is not an instance of the model raises ``TypeError`` and an invalid ``batch_size`` ``ValueError``,
        also for no instances; no instances return ``[]`` without an operation, so the other arguments and the driver's
        checks (a multi-table inheritance child) are not applied to them. The query's ORM manager (`Query.manager`)
        inserts the rows when it has one. A content type a generic foreign key assignment left unset is resolved on the
        query set's executor first (`adorm.GenericForeignKey`).
        """
        from adorm.models.base import Model
        from adorm.operations import BulkCreate

        if batch_size is not None and (
            isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0
        ):
            raise ValueError(f"batch_size is a positive integer or None, not {batch_size!r}")
        if isinstance(objs, str | bytes) or not isinstance(objs, Iterable):
            raise TypeError("bulk_create() takes an iterable of model instances")
        instances = list(objs)
        meta = self.model._require_meta()
        for instance in instances:
            if (
                not isinstance(instance, Model)
                or type(instance)._meta is None
                or type(instance)._meta.label != meta.label
            ):
                raise TypeError(f"bulk_create() takes {meta.label} instances, not {instance!r}")
        if not instances:
            return instances
        for instance in instances:
            if instance._state.generic:
                await instance._resolve_generic_foreign_keys(self.executor)
        operation = BulkCreate(
            meta.label,
            [_stored_values(instance, meta.pk) for instance in instances],
            batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
            manager=self.query.manager,
        )
        rows = await self._run(operation)
        if len(rows) != len(instances):
            raise InterfaceError(f"bulk_create() of {len(instances)} instances returned {len(rows)} rows")
        for instance, row in zip(instances, rows, strict=True):
            instance._stored(row, self.executor)
        return instances

    async def create_from_parent(self, parent: Any, **values: Any) -> Model:
        """A new multi-table inheritance child for the existing parent row ``parent`` (an instance or its primary
        key), with the child's own field ``values``, inserted with a `CreateFromParent` operation. Raises ``TypeError``
        or ``ValueError`` for arguments that do not fit, the parent's ``DoesNotExist``, `adorm.IntegrityError`."""
        from adorm.models.base import Model
        from adorm.models.inheritance import child_values
        from adorm.operations import CreateFromParent

        link, parent_model, stored = child_values(self.model, values)
        if isinstance(parent, Model):
            link._check_target(parent)
            parent = parent.pk
            if parent is None:
                raise ValueError(f"cannot create a {self.model._require_meta().label} row for an unsaved parent")
        operation = CreateFromParent(self.model._require_meta().label, parent, stored)
        try:
            row = await self.executor.execute(operation)
        except ObjectDoesNotExist as exc:
            if type(exc) is not ObjectDoesNotExist:
                raise
            raise parent_model.DoesNotExist(*exc.args) from exc
        return self._instance(row)

    async def _generic_values(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """``values`` with each generic foreign key of the model (`adorm.GenericForeignKey`) replaced by its two
        fields: for an instance, its object id and its content type id, read through `adorm.content_types` on the
        query set's executor; for ``None``, ``None`` in both. A relation given together with either of its fields
        raises ``ValueError``, a value that is not a stored model instance ``TypeError`` or ``ValueError`` as for
        assignment; a missing content type row raises as `adorm.content_types.get_id()`."""
        from adorm.models.contenttypes import content_types

        model = self.model
        values = dict(values)
        for name, generic in model.__adorm_generic_foreign_keys__.items():
            if name not in values:
                continue
            column = generic.ct_column(model)
            for field in dict.fromkeys((column, generic.ct_field, generic.fk_field)):
                if field in values:
                    raise ValueError(f"give either {name} or {field}, not both")
            value = values.pop(name)
            if value is None:
                values[column] = values[generic.fk_field] = None
                continue
            values[generic.fk_field] = generic.object_id(model, value)
            values[column] = await content_types.get_id(
                value,
                for_concrete_model=generic.for_concrete_model,
                session=self.executor,
                content_type_model=generic.content_type_model(model),
            )
        return values

    async def get_or_create(self, defaults: Mapping[str, Any] | None = None, **lookups: Any) -> tuple[Model, bool]:
        """The instance matching ``lookups``, or a new one from the plain lookups and ``defaults`` (a
        `GetOrCreate` operation); returns ``(instance, created)``. A generic foreign key in ``defaults`` takes an
        instance, as for `create()`."""
        from adorm.operations import GetOrCreate

        operation = GetOrCreate(self.query, lookups, await self._generic_values(defaults or {}))
        row, created = await self._run(operation)
        return self._instance(row), bool(created)

    async def update_or_create(
        self,
        defaults: Mapping[str, Any] | None = None,
        create_defaults: Mapping[str, Any] | None = None,
        **lookups: Any,
    ) -> tuple[Model, bool]:
        """Set ``defaults`` on the instance matching ``lookups``, or create one from the plain lookups and
        ``create_defaults`` (``defaults`` when it is ``None``), with an `UpdateOrCreate` operation; returns
        ``(instance, created)``. A generic foreign key in ``defaults`` or ``create_defaults`` takes an instance, as
        for `create()`."""
        from adorm.operations import UpdateOrCreate

        operation = UpdateOrCreate(
            self.query,
            lookups,
            await self._generic_values(defaults or {}),
            None if create_defaults is None else await self._generic_values(create_defaults),
        )
        row, created = await self._run(operation)
        return self._instance(row), bool(created)
