"""`QuerySet`: a `Query` on a model class with the executor that runs it.

Building methods return a new `QuerySet` and do no I/O. Terminal methods are coroutines that hand one operation to
the executor and turn its rows into model instances, dicts or tuples.
"""

from __future__ import annotations

from collections import namedtuple
from collections.abc import AsyncIterator, Iterable, Mapping
from functools import lru_cache
from typing import Any, Self, TYPE_CHECKING

from adorm.exceptions import MultipleObjectsReturned, ObjectDoesNotExist, ProtectedError, RestrictedError
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


class QuerySet:
    """The rows of ``query`` on ``model`` (a model class), run by ``executor`` (a session).

    ``Model.objects`` and ``session.Model`` create query sets; the building methods are those of `Query`, each
    returning a new query set of the same class. ``query`` is the underlying `Query`, so a query set can stand wherever
    a query is accepted, e.g. ``filter(pk__in=qs)``. A subclass adds client-side methods that build on these and
    driver methods (`adorm.driver_method`); a manager's ``queryset_class`` names it.
    """

    __slots__ = ("executor", "model", "query")

    model: type[Model]
    executor: Executor
    query: Query

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

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"a query set is immutable: cannot set {name!r}")

    def __reduce__(self) -> tuple[Any, ...]:
        return type(self), (self.model, self.executor, self.query)

    def __copy__(self) -> QuerySet:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> QuerySet:
        return self

    def _clone(self, query: Query) -> Self:
        return type(self)(self.model, self.executor, query)

    @classmethod
    def as_manager(cls, orm_name: str | None = None, *, method_manager: str | None = None) -> Manager:
        """A `Manager` whose ``queryset_class`` is this class:
        ``Manager.from_queryset(cls)(orm_name, method_manager=method_manager)``."""
        from adorm.models.managers import _queryset_as_manager

        return _queryset_as_manager(cls, orm_name, method_manager)

    def __repr__(self) -> str:
        return f"<QuerySet {self.query!r} on {self.executor!r}>"

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
        return self._clone(self.query.values(*fields))

    def values_list(self, *fields: str, flat: bool = False, named: bool = False) -> Self:
        """`Query.values_list()`: rows as tuples, single values (``flat``) or named tuples (``named``)."""
        return self._clone(self.query.values_list(*fields, flat=flat, named=named))

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

    async def __aiter__(self) -> AsyncIterator[Any]:
        """The rows of a `Select` operation, formatted by the query's row format."""
        from adorm.operations import Select

        if self.query.is_empty:
            return
        for row in await self._run(Select(self.query)):
            yield self._format(row)

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        """The single row matching the query and the conditions (arguments as for `filter()`), with a `Get`
        operation. No row raises the model's ``DoesNotExist``, several rows its ``MultipleObjectsReturned``."""
        from adorm.operations import Get

        query = self.query.filter(*args, **kwargs) if args or kwargs else self.query
        if query.is_empty:
            raise self.model.DoesNotExist(f"{self.model.__name__} matching query does not exist.")
        return self._format(await self._run(Get(query)))

    async def first(self) -> Any:
        """The first row (a `First` operation), ``None`` when there is none."""
        from adorm.operations import First

        if self.query.is_empty:
            return None
        row = await self._run(First(self.query))
        return None if row is None else self._format(row)

    async def last(self) -> Any:
        """The last row (a `Last` operation), ``None`` when there is none."""
        from adorm.operations import Last

        if self.query.is_empty:
            return None
        row = await self._run(Last(self.query))
        return None if row is None else self._format(row)

    async def count(self) -> int:
        """The number of rows (a `Count` operation)."""
        from adorm.operations import Count

        return 0 if self.query.is_empty else await self._run(Count(self.query))

    async def exists(self) -> bool:
        """Whether the query has a row (an `Exists` operation)."""
        from adorm.operations import Exists

        return False if self.query.is_empty else await self._run(Exists(self.query))

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
        extra values of the instance.
        """
        from adorm.operations import Create

        instance = self.model(**values)
        pk = self.model._require_meta().pk
        stored = {
            name: instance.__dict__[name]
            for name in self.model.__pydantic_fields__
            if name in instance.model_fields_set and not (name == pk and instance.__dict__[name] is None)
        }
        manager = self.query.manager
        if manager is not None and instance.__pydantic_extra__:
            stored.update(instance.__pydantic_extra__)
        instance._stored(await self._run(Create(self.query.model, stored, manager=manager)), self.executor)
        return instance

    async def get_or_create(self, defaults: Mapping[str, Any] | None = None, **lookups: Any) -> tuple[Model, bool]:
        """The instance matching ``lookups``, or a new one from the plain lookups and ``defaults`` (a
        `GetOrCreate` operation); returns ``(instance, created)``."""
        from adorm.operations import GetOrCreate

        operation = GetOrCreate(self.query, lookups, defaults or {})
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
        ``(instance, created)``."""
        from adorm.operations import UpdateOrCreate

        operation = UpdateOrCreate(
            self.query, lookups, defaults or {}, None if create_defaults is None else create_defaults
        )
        row, created = await self._run(operation)
        return self._instance(row), bool(created)
