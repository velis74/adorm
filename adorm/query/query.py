"""The query: an immutable description of the rows to read from one model.

Every method returns a new `Query` and leaves the original unchanged; building does no I/O. The model is named by
its label (``"app_label.ModelName"``); the driver resolves the label and the field paths.
"""

from __future__ import annotations

import re

from collections.abc import Iterable
from typing import Any, ClassVar

from adorm.query.expressions import check_field_path, Expression, F, Node, OrderBy, Q

__all__ = ["ForUpdate", "Query", "as_query", "check_model_label"]

_LABEL = re.compile(r"[A-Za-z_]\w*\.[A-Za-z_]\w*")

ROW_FORMATS = ("model", "dict", "tuple", "flat", "named")
_SAME_WHEN_COMBINED = (
    "is_distinct",
    "distinct_fields",
    "select",
    "row_format",
    "annotations",
    "only_fields",
    "deferred_fields",
    "for_update",
    "manager",
)


def check_model_label(label: Any) -> str:
    """Return ``label`` when it is a model label (``"app_label.ModelName"``); raise ``ValueError`` otherwise."""
    if not isinstance(label, str) or not _LABEL.fullmatch(label):
        raise ValueError(f"{label!r} is not a model label ('app_label.ModelName')")
    return label


def _check_manager(name: Any) -> str | None:
    if name is not None and (not isinstance(name, str) or not name.isidentifier() or name.startswith("_")):
        raise ValueError(f"{name!r} is not a manager name")
    return name


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _unique(names: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(names))


class ForUpdate(Node):
    """The row locks of ``select_for_update()``.

    ``nowait`` fails instead of waiting for a locked row, ``skip_locked`` skips locked rows, ``of`` names the
    relations whose rows are locked (``"self"`` for the queried model) and ``no_key`` takes the weaker
    ``FOR NO KEY UPDATE`` lock.
    """

    _fields = ("nowait", "skip_locked", "of", "no_key")
    nowait: bool
    skip_locked: bool
    of: tuple[str, ...]
    no_key: bool

    def __init__(
        self, *, nowait: bool = False, skip_locked: bool = False, of: Iterable[str] = (), no_key: bool = False
    ) -> None:
        if nowait and skip_locked:
            raise ValueError("nowait and skip_locked are mutually exclusive")
        if isinstance(of, str):
            raise TypeError("of is an iterable of field paths, not a string")
        self._init(
            nowait=bool(nowait),
            skip_locked=bool(skip_locked),
            of=_unique(check_field_path(name) for name in of),
            no_key=bool(no_key),
        )

    def _own_constructs(self) -> Iterable[str]:
        yield "clause.select_for_update"
        for option in ("nowait", "skip_locked", "of", "no_key"):
            if getattr(self, option):
                yield f"clause.select_for_update.{option}"

    def _check_decoded(self) -> None:
        if not all(isinstance(getattr(self, name), bool) for name in ("nowait", "skip_locked", "no_key")):
            raise ValueError("nowait, skip_locked and no_key are bools")
        if self.nowait and self.skip_locked:
            raise ValueError("nowait and skip_locked are mutually exclusive")
        if not isinstance(self.of, tuple):
            raise ValueError("of is a list of field paths")
        for name in self.of:
            check_field_path(name)


def as_query(value: Any) -> Query | None:
    """``value`` when it is a `Query`, its ``query`` attribute when that is one, else ``None``."""
    if isinstance(value, Query):
        return value
    query = getattr(value, "query", None) if not isinstance(value, Node) else None
    return query if isinstance(query, Query) else None


class Query(Node):
    """The rows of ``model`` (a label, ``"app_label.ModelName"``) that a read returns.

    Attributes: ``where`` (a `Q`, empty for all rows), ``annotations`` (``(name, expression)`` pairs in the order
    added), ``ordering`` (`OrderBy` tuple), ``select`` (selected field paths or annotation names, empty for all fields),
    ``row_format`` (``"model"``, ``"dict"``, ``"tuple"``, ``"flat"`` or ``"named"``), ``is_distinct`` and
    ``distinct_fields``, ``low_mark`` and ``high_mark`` (the slice; ``high_mark`` ``None`` for no limit),
    ``only_fields`` (``None`` unless ``only()`` limits the loaded fields) and ``deferred_fields``,
    ``select_related_fields`` (``False``, ``True`` for every non-null relation, or a tuple of relation paths),
    ``is_none`` (``none()``), ``for_update`` (a `ForUpdate` or ``None``) and ``manager``.

    ``manager`` names the target ORM's manager whose rows and method overrides the driver applies (Django: the
    manager attribute of the model, so its ``get_queryset()`` and its query set's ``create()``, ``delete()`` …);
    ``None`` reads the model's rows without manager logic. A query with a manager uses the construct
    ``clause.manager``.
    """

    _fields = (
        "model",
        "where",
        "annotations",
        "ordering",
        "select",
        "row_format",
        "is_distinct",
        "distinct_fields",
        "low_mark",
        "high_mark",
        "only_fields",
        "deferred_fields",
        "select_related_fields",
        "is_none",
        "for_update",
        "manager",
    )
    ROW_FORMATS: ClassVar[tuple[str, ...]] = ROW_FORMATS
    model: str
    where: Q
    annotations: tuple[tuple[str, Expression], ...]
    ordering: tuple[OrderBy, ...]
    select: tuple[str, ...]
    row_format: str
    is_distinct: bool
    distinct_fields: tuple[str, ...]
    low_mark: int
    high_mark: int | None
    only_fields: tuple[str, ...] | None
    deferred_fields: tuple[str, ...]
    select_related_fields: bool | tuple[str, ...]
    is_none: bool
    for_update: ForUpdate | None
    manager: str | None

    def __init__(self, model: str, *, manager: str | None = None) -> None:
        self._init(
            model=check_model_label(model),
            where=Q(),
            annotations=(),
            ordering=(),
            select=(),
            row_format="model",
            is_distinct=False,
            distinct_fields=(),
            low_mark=0,
            high_mark=None,
            only_fields=None,
            deferred_fields=(),
            select_related_fields=False,
            is_none=False,
            for_update=None,
            manager=_check_manager(manager),
        )

    @property
    def is_sliced(self) -> bool:
        """Whether a slice limits the rows."""
        return self.low_mark != 0 or self.high_mark is not None

    @property
    def is_empty(self) -> bool:
        """Whether the query returns no rows: ``none()`` was called or the slice leaves none."""
        return self.is_none or (self.high_mark is not None and self.high_mark <= self.low_mark)

    def _check_not_sliced(self, action: str) -> None:
        if self.is_sliced:
            raise TypeError(f"cannot {action} a query once a slice has been taken")

    def filter(self, *args: Any, **kwargs: Any) -> Query:
        """Rows that also match the conditions; arguments as for `Q`."""
        self._check_not_sliced("filter")
        return self._replace(where=self.where & Q(*args, **kwargs))

    def exclude(self, *args: Any, **kwargs: Any) -> Query:
        """Rows that also do not match the conditions; arguments as for `Q`."""
        self._check_not_sliced("filter")
        condition = Q(*args, **kwargs)
        return self._replace(where=self.where & ~condition) if condition else self

    def annotate(self, **annotations: Any) -> Query:
        """Add computed columns: name → expression."""
        existing = {name for name, _ in self.annotations}
        for name, expression in annotations.items():
            if name in existing:
                raise ValueError(f"the annotation {name!r} already exists")
            if "__" in name or not name.isidentifier():
                raise ValueError(f"{name!r} is not an annotation name")
            if not isinstance(expression, Expression):
                raise TypeError(f"the annotation {name!r} is not an expression")
        return self._replace(annotations=self.annotations + tuple(annotations.items()))

    def order_by(self, *keys: Any) -> Query:
        """Replace the ordering: field paths (``"-name"`` descending), expressions or `OrderBy`. No keys clears it."""
        self._check_not_sliced("reorder")
        return self._replace(ordering=tuple(self._order_key(k) for k in keys))

    @staticmethod
    def _order_key(key: Any) -> OrderBy:
        if isinstance(key, OrderBy):
            return key
        if isinstance(key, str):
            descending = key.startswith("-")
            return OrderBy(F(key.removeprefix("-")), descending=descending)
        if isinstance(key, Expression):
            return OrderBy(key)
        raise TypeError(f"an ordering key is a field path or an expression, not {type(key).__name__}")

    def values(self, *fields: str) -> Query:
        """Rows as dicts of ``fields`` (field paths or annotation names); no fields selects all."""
        return self._replace(select=tuple(check_field_path(f) for f in fields), row_format="dict")

    def values_list(self, *fields: str, flat: bool = False, named: bool = False) -> Query:
        """Rows as tuples of ``fields``; ``flat`` returns the single field's values, ``named`` named tuples."""
        if flat and named:
            raise TypeError("flat and named are mutually exclusive")
        if flat and len(fields) > 1:
            raise TypeError("flat needs a single field")
        row_format = "flat" if flat else "named" if named else "tuple"
        return self._replace(select=tuple(check_field_path(f) for f in fields), row_format=row_format)

    def _check_no_values(self, action: str) -> None:
        if self.row_format != "model":
            raise TypeError(f"cannot call {action}() after values() or values_list()")

    def only(self, *fields: str) -> Query:
        """Load only ``fields`` (and the primary key); replaces an earlier ``only()``, minus deferred fields."""
        self._check_no_values("only")
        names = _unique(check_field_path(f) for f in fields)
        if self.deferred_fields:
            names = tuple(n for n in names if n not in self.deferred_fields)
        return self._replace(only_fields=names, deferred_fields=())

    def defer(self, *fields: str | None) -> Query:
        """Do not load ``fields`` until accessed; ``defer(None)`` loads every field again."""
        self._check_no_values("defer")
        if fields == (None,):
            return self._replace(only_fields=None, deferred_fields=())
        names = tuple(check_field_path(f) for f in fields)
        if self.only_fields is not None:
            return self._replace(only_fields=tuple(n for n in self.only_fields if n not in names))
        return self._replace(deferred_fields=_unique(self.deferred_fields + names))

    def select_related(self, *fields: str | None) -> Query:
        """Load the related rows of the relation paths ``fields`` in the same query.

        No fields follows every non-null relation; ``select_related(None)`` clears the list. Paths add up over
        calls.
        """
        self._check_no_values("select_related")
        if fields == (None,):
            return self._replace(select_related_fields=False)
        if not fields:
            return self._replace(select_related_fields=True)
        names = tuple(check_field_path(f) for f in fields)
        current = self.select_related_fields if isinstance(self.select_related_fields, tuple) else ()
        return self._replace(select_related_fields=_unique(current + names))

    def none(self) -> Query:
        """A query that returns no rows."""
        return self._replace(is_none=True)

    def select_for_update(
        self, *, nowait: bool = False, skip_locked: bool = False, of: Iterable[str] = (), no_key: bool = False
    ) -> Query:
        """Lock the selected rows until the end of the transaction; options as for `ForUpdate`."""
        return self._replace(for_update=ForUpdate(nowait=nowait, skip_locked=skip_locked, of=of, no_key=no_key))

    def _combine(self, other: Any, connector: str) -> Query:
        if not isinstance(other, Query):
            return NotImplemented
        if other.model != self.model:
            raise TypeError(f"cannot combine a query on {self.model} with one on {other.model}")
        if self.is_sliced or other.is_sliced:
            raise TypeError("cannot combine queries once a slice has been taken")
        for name in _SAME_WHEN_COMBINED:
            if getattr(self, name) != getattr(other, name):
                raise TypeError(f"combined queries need the same {name}")
        if connector == Q.OR:
            if self.is_none or other.is_none:
                return other if self.is_none else self
            where = self.where | other.where if self.where and other.where else Q()
        else:
            if self.is_none or other.is_none:
                return other if other.is_none else self
            where = self.where & other.where
        if self.select_related_fields is True or other.select_related_fields is True:
            related: bool | tuple[str, ...] = True
        else:
            related = _unique((self.select_related_fields or ()) + (other.select_related_fields or ())) or False
        return self._replace(where=where, ordering=other.ordering or self.ordering, select_related_fields=related)

    def __or__(self, other: Any) -> Query:
        """Rows matching either query; both are on the same model and select the same way."""
        return self._combine(other, Q.OR)

    def __and__(self, other: Any) -> Query:
        """Rows matching both queries; both are on the same model and select the same way."""
        return self._combine(other, Q.AND)

    def distinct(self, *fields: str) -> Query:
        """Remove duplicate rows; with ``fields``, keep one row per distinct value of those fields."""
        self._check_not_sliced("make distinct")
        return self._replace(is_distinct=True, distinct_fields=tuple(check_field_path(f) for f in fields))

    def __getitem__(self, key: slice) -> Query:
        if not isinstance(key, slice):
            raise TypeError(f"a query takes a slice, not {type(key).__name__}")
        if key.step is not None:
            raise ValueError("a slice of a query has no step")
        start, stop = key.start, key.stop
        for bound in (start, stop):
            if bound is not None and (not isinstance(bound, int) or isinstance(bound, bool)):
                raise TypeError("slice bounds are integers")
            if bound is not None and bound < 0:
                raise ValueError("negative indexing is not supported")
        low, high = self.low_mark, self.high_mark
        if stop is not None:
            high = min(high, low + stop) if high is not None else low + stop
        if start is not None:
            low = min(high, low + start) if high is not None else low + start
        return self._replace(low_mark=low, high_mark=high)

    def get_source_expressions(self) -> tuple[Node, ...]:
        children: tuple[Node, ...] = (self.where, *(expression for _, expression in self.annotations), *self.ordering)
        return children if self.for_update is None else (*children, self.for_update)

    def _own_constructs(self) -> Iterable[str]:
        if self.distinct_fields:
            yield "clause.distinct_on"
        elif self.is_distinct:
            yield "clause.distinct"
        if self.only_fields is not None:
            yield "clause.only"
        if self.deferred_fields:
            yield "clause.defer"
        if self.select_related_fields:
            yield "clause.select_related"
        if self.manager is not None:
            yield "clause.manager"

    def _check_decoded(self) -> None:
        check_model_label(self.model)
        _check_manager(self.manager)
        if not isinstance(self.where, Q) or not all(isinstance(o, OrderBy) for o in self.ordering):
            raise ValueError("a query's where is a Q and its ordering OrderBy nodes")
        if self.for_update is not None and not isinstance(self.for_update, ForUpdate):
            raise ValueError("a query's for_update is a ForUpdate")
        if self.row_format not in ROW_FORMATS:
            raise ValueError(f"unknown row format {self.row_format!r}")
        if not isinstance(self.is_distinct, bool) or not isinstance(self.is_none, bool):
            raise ValueError("is_distinct and is_none are bools")
        if not all(_is_int(mark) for mark in (self.low_mark, self.high_mark) if mark is not None):
            raise ValueError("the slice marks are integers")
        if (
            self.low_mark is None
            or self.low_mark < 0
            or (self.high_mark is not None and self.high_mark < self.low_mark)
        ):
            raise ValueError("invalid slice")
        for name in ("select", "distinct_fields", "deferred_fields"):
            if not isinstance(getattr(self, name), tuple):
                raise ValueError(f"{name} is a list of field paths")
        if self.only_fields is not None and not isinstance(self.only_fields, tuple):
            raise ValueError("only_fields is a list of field paths or null")
        if not isinstance(self.select_related_fields, bool | tuple):
            raise ValueError("select_related_fields is a bool or a list of relation paths")
        if self.row_format == "flat" and len(self.select) > 1:
            raise ValueError("flat needs a single field")
        names = (*self.select, *self.distinct_fields, *self.deferred_fields, *(self.only_fields or ()))
        if isinstance(self.select_related_fields, tuple):
            names += self.select_related_fields
        for name in names:
            check_field_path(name)
        for name, _ in self.annotations:
            if "__" in name or not name.isidentifier():
                raise ValueError(f"{name!r} is not an annotation name")

    def __repr__(self) -> str:
        parts = [repr(self.model)]
        if self.manager is not None:
            parts.append(f"manager={self.manager!r}")
        if self.where:
            parts.append(f"where={self.where!r}")
        for name in ("ordering", "select", "distinct_fields", "deferred_fields", "select_related_fields"):
            if getattr(self, name):
                parts.append(f"{name}={getattr(self, name)!r}")
        if self.row_format != "model":
            parts.append(f"row_format={self.row_format!r}")
        if self.only_fields is not None:
            parts.append(f"only_fields={self.only_fields!r}")
        if self.for_update is not None:
            parts.append(f"for_update={self.for_update!r}")
        if self.is_none:
            parts.append("is_none=True")
        if self.annotations:
            parts.append(f"annotations={dict(self.annotations)!r}")
        if self.is_distinct:
            parts.append("is_distinct=True")
        if self.is_sliced:
            parts.append(f"slice=[{self.low_mark}:{'' if self.high_mark is None else self.high_mark}]")
        return f"Query({', '.join(parts)})"
