"""`QuerySet`: a `Query` on a model class with the executor that runs it.

Building methods return a new `QuerySet` and do no I/O. Terminal methods are coroutines that hand one operation to
the executor and turn its rows into model instances, dicts or tuples.
"""

from __future__ import annotations

from collections import namedtuple
from collections.abc import AsyncIterator, Iterable, Mapping
from functools import lru_cache
from typing import Any, TYPE_CHECKING

from adorm.exceptions import MultipleObjectsReturned, ObjectDoesNotExist
from adorm.query.query import Query

if TYPE_CHECKING:
    from adorm.models.base import Executor, Model
    from adorm.operations import Operation

__all__ = ["QuerySet"]


@lru_cache(maxsize=256)
def _row_class(names: tuple[str, ...]) -> type:
    return namedtuple("Row", names, rename=True)  # noqa: PYI024 - field names are only known at run time


class QuerySet:
    """The rows of ``query`` on ``model`` (a model class), run by ``executor`` (a session).

    ``Model.objects`` and ``session.Model`` create query sets; the building methods are those of `Query`, each
    returning a new query set. ``query`` is the underlying `Query`, so a query set can stand wherever a query is
    accepted, e.g. ``filter(pk__in=qs)``.
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
        return QuerySet, (self.model, self.executor, self.query)

    def __copy__(self) -> QuerySet:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> QuerySet:
        return self

    def _clone(self, query: Query) -> QuerySet:
        return QuerySet(self.model, self.executor, query)

    def __repr__(self) -> str:
        return f"<QuerySet {self.query!r} on {self.executor!r}>"

    # Building: no I/O

    def all(self) -> QuerySet:
        """A copy of the query set."""
        return self._clone(self.query)

    def filter(self, *args: Any, **kwargs: Any) -> QuerySet:
        """`Query.filter()`."""
        return self._clone(self.query.filter(*args, **kwargs))

    def exclude(self, *args: Any, **kwargs: Any) -> QuerySet:
        """`Query.exclude()`."""
        return self._clone(self.query.exclude(*args, **kwargs))

    def annotate(self, **annotations: Any) -> QuerySet:
        """`Query.annotate()`."""
        return self._clone(self.query.annotate(**annotations))

    def order_by(self, *keys: Any) -> QuerySet:
        """`Query.order_by()`."""
        return self._clone(self.query.order_by(*keys))

    def values(self, *fields: str) -> QuerySet:
        """`Query.values()`: rows as dicts."""
        return self._clone(self.query.values(*fields))

    def values_list(self, *fields: str, flat: bool = False, named: bool = False) -> QuerySet:
        """`Query.values_list()`: rows as tuples, single values (``flat``) or named tuples (``named``)."""
        return self._clone(self.query.values_list(*fields, flat=flat, named=named))

    def only(self, *fields: str) -> QuerySet:
        """`Query.only()`."""
        return self._clone(self.query.only(*fields))

    def defer(self, *fields: str | None) -> QuerySet:
        """`Query.defer()`."""
        return self._clone(self.query.defer(*fields))

    def select_related(self, *fields: str | None) -> QuerySet:
        """`Query.select_related()`."""
        return self._clone(self.query.select_related(*fields))

    def distinct(self, *fields: str) -> QuerySet:
        """`Query.distinct()`."""
        return self._clone(self.query.distinct(*fields))

    def none(self) -> QuerySet:
        """`Query.none()`: terminal methods return an empty result without running an operation."""
        return self._clone(self.query.none())

    def select_for_update(
        self, *, nowait: bool = False, skip_locked: bool = False, of: Iterable[str] = (), no_key: bool = False
    ) -> QuerySet:
        """`Query.select_for_update()`."""
        return self._clone(self.query.select_for_update(nowait=nowait, skip_locked=skip_locked, of=of, no_key=no_key))

    def __getitem__(self, key: slice) -> QuerySet:
        return self._clone(self.query[key])

    def _combine(self, other: Any, combine: Any) -> Any:
        if not isinstance(other, QuerySet):
            return NotImplemented
        if other.executor is not self.executor:
            raise TypeError("cannot combine query sets of different sessions")
        return self._clone(combine(self.query, other.query))

    def __or__(self, other: Any) -> QuerySet:
        return self._combine(other, Query.__or__)

    def __and__(self, other: Any) -> QuerySet:
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

    async def update(self, **values: Any) -> int:
        """Set ``values`` on the rows (an `Update` operation); returns the number of rows."""
        from adorm.operations import Update

        if not values:
            raise TypeError("update() needs at least one field value")
        operation = Update(self.query, values)
        return 0 if self.query.is_empty else await self._run(operation)

    async def delete(self) -> tuple[int, dict[str, int]]:
        """Delete the rows and those that cascade from them (a `Delete` operation); returns
        ``(total, {label: count})``."""
        from adorm.operations import Delete

        operation = Delete(self.query)
        if self.query.is_empty:
            return 0, {}
        total, counts = await self._run(operation)
        return total, dict(counts)

    async def create(self, **values: Any) -> Model:
        """A new instance validated from ``values`` (as the model's constructor takes them) and inserted with a
        `Create` operation. The instance gets the values the target set (primary key, defaults)."""
        from adorm.operations import Create

        instance = self.model(**values)
        pk = self.model._require_meta().pk
        stored = {
            name: instance.__dict__[name]
            for name in self.model.__pydantic_fields__
            if name in instance.model_fields_set and not (name == pk and instance.__dict__[name] is None)
        }
        instance._stored(await self._run(Create(self.query.model, stored)), self.executor)
        return instance

    async def get_or_create(self, defaults: Mapping[str, Any] | None = None, **lookups: Any) -> tuple[Model, bool]:
        """The instance matching ``lookups``, or a new one from the plain lookups and ``defaults`` (a
        `GetOrCreate` operation); returns ``(instance, created)``."""
        from adorm.operations import GetOrCreate

        operation = GetOrCreate(self.query, lookups, defaults or {})
        row, created = await self._run(operation)
        return self._instance(row), bool(created)
