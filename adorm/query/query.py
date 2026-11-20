"""The query: an immutable description of the rows to read from one model.

Every method returns a new `Query` and leaves the original unchanged; building does no I/O. The model is named by
its label (``"app_label.ModelName"``); the driver resolves the label and the field paths.
"""

from __future__ import annotations

import functools
import operator
import re

from collections.abc import Iterable, Iterator
from typing import Any, ClassVar

from adorm.exceptions import NotSupportedError
from adorm.query.expressions import check_field_path, Expression, F, Node, OrderBy, Q

__all__ = ["CLAUSES", "COMBINATORS", "NESTED_PATH", "ForUpdate", "Query", "as_query", "construct_names"]

_LABEL = re.compile(r"[A-Za-z_]\w*\.[A-Za-z_]\w*")

ROW_FORMATS = ("model", "dict", "tuple", "flat", "named")
COMBINATORS: tuple[str, ...] = ("union", "intersection", "difference")
"""The set operations of `Query.union()`, `Query.intersection()` and `Query.difference()`."""
_SAME_WHEN_COMBINED = (
    "is_distinct",
    "distinct_fields",
    "select",
    "row_format",
    "annotations",
    "aliases",
    "group_by",
    "group_mark",
    "only_fields",
    "deferred_fields",
    "for_update",
    "manager",
)

_OWN_WHEN_COMBINED = frozenset(
    {
        "model",
        "ordering",
        "select",
        "row_format",
        "low_mark",
        "high_mark",
        "is_none",
        "combinator",
        "combinator_all",
        "combined_queries",
    }
)


CLAUSES: tuple[str, ...] = (
    "clause.annotate",
    "clause.alias",
    "clause.group_by",
    "clause.distinct",
    "clause.distinct_on",
    "clause.only",
    "clause.defer",
    "clause.select_related",
    "clause.manager",
    "clause.select_for_update",
    "clause.select_for_update.nowait",
    "clause.select_for_update.skip_locked",
    "clause.select_for_update.of",
    "clause.select_for_update.no_key",
    "clause.on_conflict.ignore",
    "clause.on_conflict.update",
    "clause.union",
    "clause.intersection",
    "clause.difference",
    "clause.combinator.slice",
)
"""The construct names of `Query` clauses and set operations, `ForUpdate` options and the conflict handling of
`adorm.operations.BulkCreate`."""

NESTED_PATH = "path.nested"
"""The construct name of a field path with several segments (``club__name``, ``joined__year``, ``address__street``): a
relation, a transform or a key inside a JSON field. `F` reports it, and `Query` for its ``values()``, ``only()``,
``defer()`` and ``distinct()`` fields. A transform the path applies is reported as ``transform.<name>`` too."""


def construct_names() -> frozenset[str]:
    """Every construct name an operation can report: `CLAUSES`, ``aggregate.distinct`` and ``aggregate.filter``, and
    `NESTED_PATH`, the ``construct`` of every `Node` subclass defined so far (custom lookups and functions included)
    and of every registered transform (``transform.<name>``)."""
    from adorm.query.lookups import TRANSFORMS

    names = {*CLAUSES, NESTED_PATH, "aggregate.distinct", "aggregate.filter"}
    names.update(cls.construct for cls in TRANSFORMS.values() if cls.construct)
    pending: list[type[Node]] = [Node]
    while pending:
        cls = pending.pop()
        if cls.construct:
            names.add(cls.construct)
        pending.extend(cls.__subclasses__())
    return frozenset(names)


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


def _outer_nodes(node: Node) -> Iterator[Node]:
    """``node`` and its descendants outside subqueries."""
    yield node
    if isinstance(node, Query):
        return
    for child in node.get_source_expressions():
        if not isinstance(child, Query):
            yield from _outer_nodes(child)


def _queries(others: Iterable[Any]) -> list[Query]:
    queries = list(others)
    for query in queries:
        if not isinstance(query, Query):
            raise TypeError(f"a set operation combines queries, not {type(query).__name__}")
    return queries


def as_query(value: Any) -> Query | None:
    """``value`` when it is a `Query`, its ``query`` attribute when that is one, else ``None``."""
    if isinstance(value, Query):
        return value
    query = getattr(value, "query", None) if not isinstance(value, Node) else None
    return query if isinstance(query, Query) else None


class Query(Node):
    """The rows of ``model`` (a label, ``"app_label.ModelName"``) that a read returns.

    Attributes: ``filters`` (one non-empty `Q` per ``filter()`` or ``exclude()`` call, empty for all rows),
    ``filter_marks`` (for each filter, the number of annotations added before it), ``annotations`` (``(name,
    expression)`` pairs in the order added), ``aliases`` (the names of the annotations ``alias()`` added, which rows
    do not include), ``group_by`` (``None``, or the selected names the rows are grouped by after ``values()``
    followed by an aggregate annotation), ``group_mark`` (``None``, or the number of annotations added before the
    ``annotate()`` or ``alias()`` call that grouped the rows), ``ordering`` (`OrderBy` tuple), ``select``
    (selected field paths or annotation names, empty for all fields), ``row_format`` (``"model"``, ``"dict"``,
    ``"tuple"``, ``"flat"`` or ``"named"``), ``is_distinct`` and ``distinct_fields``, ``low_mark`` and ``high_mark``
    (the slice; ``high_mark`` ``None`` for no limit), ``only_fields`` (``None`` unless ``only()`` limits the loaded
    fields) and ``deferred_fields``, ``select_related_fields`` (``False``, ``True`` for every non-null relation, or a
    tuple of relation paths), ``is_none`` (``none()``), ``for_update`` (a `ForUpdate` or ``None``) and ``manager``.

    The conditions of separate ``filter()`` calls are kept apart because, as in Django, a condition on a multi-valued
    relation (reverse foreign key, many-to-many) joins that relation once per call: with two calls, each condition
    may match a different related row. ``where`` is all of them joined with AND. The order of filters and
    annotations is kept as well: as in Django, an aggregate annotation added after a filter on a multi-valued
    relation aggregates only the related rows the filter matched, and a filter added after it joins the relation
    again.

    ``manager`` names the target ORM's manager whose rows and method overrides the driver applies (Django: the
    manager attribute of the model, so its ``get_queryset()`` and its query set's ``create()``, ``delete()`` …);
    ``None`` reads the model's rows without manager logic. A query with a manager uses the construct
    ``clause.manager``.

    ``combinator`` is ``None``, or ``"union"``, ``"intersection"`` or ``"difference"`` for the set operation that
    combines the rows of ``combined_queries``; ``combinator_all`` keeps duplicate rows of a union. A combined query
    has no filters, annotations or other clauses of its own: its ``select`` and ``row_format`` describe the combined
    rows, and its ordering and slice apply to them.
    """

    _fields = (
        "model",
        "filters",
        "filter_marks",
        "annotations",
        "aliases",
        "group_by",
        "group_mark",
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
        "combinator",
        "combinator_all",
        "combined_queries",
    )
    ROW_FORMATS: ClassVar[tuple[str, ...]] = ROW_FORMATS
    model: str
    filters: tuple[Q, ...]
    filter_marks: tuple[int, ...]
    annotations: tuple[tuple[str, Expression], ...]
    aliases: tuple[str, ...]
    group_by: tuple[str, ...] | None
    group_mark: int | None
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
    combinator: str | None
    combinator_all: bool
    combined_queries: tuple[Query, ...]

    def __init__(self, model: str, *, manager: str | None = None) -> None:
        self._init(
            model=check_model_label(model),
            filters=(),
            filter_marks=(),
            annotations=(),
            aliases=(),
            group_by=None,
            group_mark=None,
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
            combinator=None,
            combinator_all=False,
            combined_queries=(),
        )

    @property
    def where(self) -> Q:
        """The conditions of every ``filter()`` and ``exclude()`` call joined with AND; empty for all rows."""
        return functools.reduce(operator.and_, self.filters, Q())

    @property
    def is_sliced(self) -> bool:
        """Whether a slice limits the rows."""
        return self.low_mark != 0 or self.high_mark is not None

    @property
    def is_empty(self) -> bool:
        """Whether the query returns no rows: ``none()`` was called or the slice leaves none."""
        return self.is_none or (self.high_mark is not None and self.high_mark <= self.low_mark)

    @property
    def is_combined(self) -> bool:
        """Whether the query combines other queries with ``union()``, ``intersection()`` or ``difference()``."""
        return self.combinator is not None

    def check_not_combined(self, action: str) -> None:
        """Raise `adorm.NotSupportedError` when the query is combined: ``action`` is not allowed after a set
        operation."""
        if self.combinator is not None:
            raise NotSupportedError(f"cannot call {action}() after {self.combinator}()")

    def _check_not_sliced(self, action: str) -> None:
        if self.is_sliced:
            raise TypeError(f"cannot {action} a query once a slice has been taken")

    def filter(self, *args: Any, **kwargs: Any) -> Query:
        """Rows that also match the conditions; arguments as for `Q`."""
        self.check_not_combined("filter")
        self._check_not_sliced("filter")
        condition = Q(*args, **kwargs)
        return self._add_filter(condition) if condition else self

    def exclude(self, *args: Any, **kwargs: Any) -> Query:
        """Rows that also do not match the conditions; arguments as for `Q`."""
        self.check_not_combined("exclude")
        self._check_not_sliced("filter")
        condition = Q(*args, **kwargs)
        return self._add_filter(~condition) if condition else self

    def _add_filter(self, condition: Q) -> Query:
        return self._replace(
            filters=self.filters + (condition,), filter_marks=self.filter_marks + (len(self.annotations),)
        )

    def annotate(self, **annotations: Any) -> Query:
        """Add computed columns: name → expression.

        After ``values()`` or ``values_list()`` with fields, the names are added to the selected names; the first
        annotation that contains an aggregate there groups the rows by the names selected so far and the new
        annotations without an aggregate (``group_by``). Annotations without an aggregate added to a grouped query
        are added to ``group_by``.
        """
        return self._annotate(annotations, selected=True)

    def alias(self, **annotations: Any) -> Query:
        """Add named expressions that conditions, orderings and other expressions refer to, without returning them in
        the rows: as `annotate`, but the names go to ``aliases`` and are not selected."""
        return self._annotate(annotations, selected=False)

    def _annotate(self, annotations: dict[str, Any], *, selected: bool) -> Query:
        self.check_not_combined("annotate" if selected else "alias")
        existing = {name for name, _ in self.annotations}
        for name, expression in annotations.items():
            if name in existing:
                raise ValueError(f"the annotation {name!r} already exists")
            if "__" in name or not name.isidentifier():
                raise ValueError(f"{name!r} is not an annotation name")
            if not isinstance(expression, Expression):
                raise TypeError(f"the annotation {name!r} is not an expression")
        if not annotations:
            return self
        query = self._replace(
            annotations=self.annotations + tuple(annotations.items()),
            aliases=self.aliases if selected else self.aliases + tuple(annotations),
        )
        if self.row_format == "model" or not self.select:
            return query
        aggregates = query.aggregate_names()
        new = tuple(annotations)
        changes: dict[str, Any] = {}
        if selected:
            changes["select"] = self.select + new
        if self.group_by is None and any(name in aggregates for name in new):
            plain = (name for name in (*self.select, *(new if selected else ())) if name not in aggregates)
            changes["group_by"] = _unique(plain)
            changes["group_mark"] = len(self.annotations)
        elif self.group_by is not None and selected:
            changes["group_by"] = _unique((*self.group_by, *(name for name in new if name not in aggregates)))
        return query._replace(**changes) if changes else query

    def aggregate_names(self) -> frozenset[str]:
        """The names of the annotations that contain an aggregate, directly or through another such annotation."""
        names: set[str] = set()
        for name, expression in self.annotations:
            if expression.contains_aggregate or any(
                type(node) is F and node.path[0] in names for node in _outer_nodes(expression)
            ):
                names.add(name)
        return frozenset(names)

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
        return self._replace(select=self._selected(fields), row_format="dict")

    def values_list(self, *fields: str, flat: bool = False, named: bool = False) -> Query:
        """Rows as tuples of ``fields``; ``flat`` returns the single field's values, ``named`` named tuples."""
        if flat and named:
            raise TypeError("flat and named are mutually exclusive")
        if flat and len(fields) > 1:
            raise TypeError("flat needs a single field")
        row_format = "flat" if flat else "named" if named else "tuple"
        return self._replace(select=self._selected(fields), row_format=row_format)

    def _selected(self, fields: tuple[str, ...]) -> tuple[str, ...]:
        names = tuple(check_field_path(f) for f in fields)
        for name in names:
            if name in self.aliases:
                raise ValueError(f"cannot select the alias {name!r}: use annotate() to select it")
        return names

    def _check_no_values(self, action: str) -> None:
        if self.row_format != "model":
            raise TypeError(f"cannot call {action}() after values() or values_list()")

    def only(self, *fields: str) -> Query:
        """Load only ``fields`` (and the primary key); replaces an earlier ``only()``, minus deferred fields."""
        self.check_not_combined("only")
        self._check_no_values("only")
        names = _unique(check_field_path(f) for f in fields)
        if self.deferred_fields:
            names = tuple(n for n in names if n not in self.deferred_fields)
        return self._replace(only_fields=names, deferred_fields=())

    def defer(self, *fields: str | None) -> Query:
        """Do not load ``fields`` until accessed; ``defer(None)`` loads every field again."""
        self.check_not_combined("defer")
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
        self.check_not_combined("select_related")
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
        self.check_not_combined("select_for_update")
        return self._replace(for_update=ForUpdate(nowait=nowait, skip_locked=skip_locked, of=of, no_key=no_key))

    def _combine(self, other: Any, connector: str) -> Query:
        if not isinstance(other, Query):
            return NotImplemented
        if self.is_combined or other.is_combined:
            raise TypeError(f"cannot use the {'|' if connector == Q.OR else '&'} operator with a combined query")
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
            both = self.filters and other.filters
            filters = (self.where | other.where,) if both else ()
            marks = (max(self.filter_marks + other.filter_marks),) if both else ()
        else:
            if self.is_none or other.is_none:
                return other if other.is_none else self
            filters = self.filters + other.filters
            marks = self.filter_marks + other.filter_marks
        if self.select_related_fields is True or other.select_related_fields is True:
            related: bool | tuple[str, ...] = True
        else:
            related = _unique((self.select_related_fields or ()) + (other.select_related_fields or ())) or False
        return self._replace(
            filters=filters,
            filter_marks=marks,
            ordering=other.ordering or self.ordering,
            select_related_fields=related,
        )

    def __or__(self, other: Any) -> Query:
        """Rows matching either query; both are on the same model and select the same way. The result has one
        filter: the two queries' conditions joined with OR, placed after the annotations either query's filters
        followed."""
        return self._combine(other, Q.OR)

    def __and__(self, other: Any) -> Query:
        """Rows matching both queries; both are on the same model and select the same way. The result keeps the
        filters of both."""
        return self._combine(other, Q.AND)

    def union(self, *others: Query, all: bool = False) -> Query:
        """The rows of this query and of ``others`` (SQL ``UNION``); ``all`` keeps duplicate rows.

        Without ``others`` the query itself. When this query is empty (`is_empty`), the other queries that are not
        empty are combined instead, and a single one is returned as it is.
        """
        queries = _queries(others)
        if self.is_empty:
            rest = [query for query in queries if not query.is_empty]
            if not rest:
                return self
            return rest[0] if len(rest) == 1 else rest[0]._combinator("union", rest[1:], all=all)
        if not queries:
            return self
        return self._combinator("union", queries, all=all)

    def intersection(self, *others: Query) -> Query:
        """The rows this query and every one of ``others`` return (SQL ``INTERSECT``), without duplicates. When this
        query or one of ``others`` is empty (`is_empty`), that query. Without ``others`` the query alone is
        combined."""
        queries = _queries(others)
        if self.is_empty:
            return self
        empty = next((query for query in queries if query.is_empty), None)
        return empty if empty is not None else self._combinator("intersection", queries)

    def difference(self, *others: Query) -> Query:
        """The rows of this query that none of ``others`` returns (SQL ``EXCEPT``), without duplicates. On an empty
        query (`is_empty`) the query itself. Without ``others`` the query alone is combined."""
        queries = _queries(others)
        if self.is_empty:
            return self
        return self._combinator("difference", queries)

    def _combinator(self, combinator: str, others: Iterable[Query], *, all: bool = False) -> Query:
        # An unordered set operation ignores the ordering of an unsliced query.
        parts = tuple(q if q.is_sliced or q.distinct_fields else q._replace(ordering=()) for q in others)
        return Query(self.model)._replace(
            select=self.select,
            row_format=self.row_format,
            combinator=combinator,
            combinator_all=bool(all),
            combined_queries=(self, *parts),
        )

    def distinct(self, *fields: str) -> Query:
        """Remove duplicate rows; with ``fields``, keep one row per distinct value of those fields."""
        self.check_not_combined("distinct")
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

    @property
    def is_grouped(self) -> bool:
        """Whether the rows are grouped by ``group_by``."""
        return self.group_by is not None

    def get_source_expressions(self) -> tuple[Node, ...]:
        annotations = (expression for _, expression in self.annotations)
        children: tuple[Node, ...] = (*self.filters, *annotations, *self.ordering)
        if self.for_update is not None:
            children = (*children, self.for_update)
        return (*children, *self.combined_queries)

    def _own_constructs(self) -> Iterable[str]:
        if self.combinator is not None:
            yield f"clause.{self.combinator}"
            if any(part.is_sliced for part in self.combined_queries):
                yield "clause.combinator.slice"
        if len(self.annotations) > len(self.aliases):
            yield "clause.annotate"
        if self.aliases:
            yield "clause.alias"
        if self.group_by is not None:
            yield "clause.group_by"
        paths = (
            *self.select,
            *(self.group_by or ()),
            *self.distinct_fields,
            *(self.only_fields or ()),
            *self.deferred_fields,
        )
        if any("__" in path for path in paths):
            yield NESTED_PATH
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
        from adorm.query.lookups import transform_constructs

        yield from sorted(transform_constructs(self))

    def _check_decoded(self) -> None:
        check_model_label(self.model)
        _check_manager(self.manager)
        if not isinstance(self.filters, tuple) or not all(isinstance(f, Q) and f for f in self.filters):
            raise ValueError("a query's filters are non-empty Q nodes")
        if not all(isinstance(o, OrderBy) for o in self.ordering):
            raise ValueError("a query's ordering is OrderBy nodes")
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
        annotation_names = {name for name, _ in self.annotations}
        if not isinstance(self.aliases, tuple) or not all(name in annotation_names for name in self.aliases):
            raise ValueError("aliases are names of annotations")
        if len(set(self.aliases)) != len(self.aliases) or any(name in self.aliases for name in self.select):
            raise ValueError("an alias is named once and is not selected")
        if self.group_by is not None:
            if not isinstance(self.group_by, tuple):
                raise ValueError("group_by is a list of selected names or null")
            for name in self.group_by:
                check_field_path(name)
        count = len(self.annotations)
        marks = self.filter_marks
        if (
            not isinstance(marks, tuple)
            or len(marks) != len(self.filters)
            or not all(_is_int(mark) and 0 <= mark <= count for mark in marks)
        ):
            raise ValueError("filter_marks are annotation counts, one per filter")
        if (self.group_mark is None) != (self.group_by is None):
            raise ValueError("group_mark is set exactly when group_by is")
        if self.group_mark is not None and not (_is_int(self.group_mark) and 0 <= self.group_mark < count):
            raise ValueError("group_mark is the index of an annotation")
        self._check_decoded_combination()

    def _check_decoded_combination(self) -> None:
        if not isinstance(self.combinator_all, bool):
            raise ValueError("combinator_all is a bool")
        parts = self.combined_queries
        if not isinstance(parts, tuple) or not all(isinstance(part, Query) for part in parts):
            raise ValueError("combined_queries is a list of queries")
        if self.combinator is None:
            if parts or self.combinator_all:
                raise ValueError("combined_queries and combinator_all need a combinator")
            return
        if self.combinator not in COMBINATORS:
            raise ValueError(f"unknown combinator {self.combinator!r}")
        if not parts:
            raise ValueError("a combined query has combined_queries")
        if self.combinator_all and self.combinator != "union":
            raise ValueError("combinator_all applies to a union")
        if parts[0].model != self.model:
            raise ValueError("a combined query is on the model of its first query")
        clean = Query(self.model)
        for name in self._fields:
            if name not in _OWN_WHEN_COMBINED and getattr(self, name) != getattr(clean, name):
                raise ValueError(f"a combined query has no {name} of its own")

    def __repr__(self) -> str:
        parts = [repr(self.model)]
        if self.combinator is not None:
            all_rows = ", all=True" if self.combinator_all else ""
            parts.append(f"{self.combinator}={self.combined_queries!r}{all_rows}")
        if self.manager is not None:
            parts.append(f"manager={self.manager!r}")
        if self.filters:
            parts.append(f"filters={self.filters!r}")
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
        if self.aliases:
            parts.append(f"aliases={self.aliases!r}")
        if any(self.filter_marks):
            parts.append(f"filter_marks={self.filter_marks!r}")
        if self.group_by is not None:
            parts.append(f"group_by={self.group_by!r}, group_mark={self.group_mark!r}")
        if self.is_distinct:
            parts.append("is_distinct=True")
        if self.is_sliced:
            parts.append(f"slice=[{self.low_mark}:{'' if self.high_mark is None else self.high_mark}]")
        return f"Query({', '.join(parts)})"
