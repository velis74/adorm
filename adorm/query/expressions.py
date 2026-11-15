"""Expressions: field references, values, functions, aggregates, conditions and `Q`.

Every node is immutable and compares by value: two nodes are equal when they have the same class and equal
fields. Building a node does no I/O and needs no model; a driver resolves field names against the model and
translates the node into its target's construct.
"""

from __future__ import annotations

import copy
import datetime
import re

from collections.abc import Iterable, Iterator, Mapping, Set
from typing import Any, ClassVar, Self, TYPE_CHECKING

if TYPE_CHECKING:
    from adorm.query.query import Query
    from adorm.schema import SchemaSource

__all__ = [
    "Aggregate",
    "Avg",
    "Case",
    "Coalesce",
    "CombinedExpression",
    "Concat",
    "Count",
    "Exists",
    "Expression",
    "F",
    "Func",
    "Length",
    "Lower",
    "Max",
    "Min",
    "Node",
    "Now",
    "OrderBy",
    "OuterRef",
    "Q",
    "Star",
    "Subquery",
    "Sum",
    "Trunc",
    "TruncDate",
    "TruncDay",
    "TruncHour",
    "TruncMinute",
    "TruncMonth",
    "TruncQuarter",
    "TruncSecond",
    "TruncTime",
    "TruncWeek",
    "TruncYear",
    "Upper",
    "Value",
    "When",
]


def make_hashable(value: Any) -> Any:
    """A hashable form of ``value`` for comparing nodes: containers become tuples or frozensets, scalars keep their
    type, so ``1``, ``1.0`` and ``True`` differ. Raises ``TypeError`` for an unhashable value of another kind."""
    if isinstance(value, Node):
        return value
    if isinstance(value, Mapping):
        return (dict, frozenset((make_hashable(k), make_hashable(v)) for k, v in value.items()))
    if isinstance(value, Set):
        return (frozenset, frozenset(make_hashable(v) for v in value))
    if isinstance(value, list | tuple):
        return (tuple, tuple(make_hashable(v) for v in value))
    hash(value)
    return (type(value), value)


def freeze_value(value: Any) -> Any:
    """A private deep copy of the plain value ``value``, so changing the caller's object does not change the node.

    Raises ``TypeError`` for a value that cannot be compared (see `make_hashable`) or copied.
    """
    try:
        make_hashable(value)
    except TypeError:
        raise TypeError(f"{type(value).__name__} is not a supported query value: it cannot be compared") from None
    return copy.deepcopy(value)


_PATH_SEPARATOR = re.compile(r"__(?!_)")


def split_field_path(name: str) -> list[str]:
    """The segments of a field path. A separator is the last ``__`` of a run of underscores, so a segment may end in
    ``_`` (a renamed field such as ``json_``): ``"copy___title"`` is ``["copy_", "title"]``."""
    return _PATH_SEPARATOR.split(name)


def check_field_path(name: Any) -> str:
    """Return ``name`` when it is a field path (``"name"``, ``"club__owner__name"``); raise otherwise."""
    if not isinstance(name, str):
        raise TypeError(f"a field path is a string, not {type(name).__name__}")
    if not all(segment.isidentifier() and "__" not in segment for segment in split_field_path(name)):
        raise ValueError(f"{name!r} is not a field path: segments are identifiers separated by '__'")
    return name


def _require(condition: bool, message: str) -> None:
    """Raise ``ValueError`` with ``message`` unless ``condition``; checks a node rebuilt by ``from_dict``."""
    if not condition:
        raise ValueError(message)


def _is_expressions(values: Any, *, conditional: bool = False) -> bool:
    return isinstance(values, tuple) and all(
        isinstance(v, Expression) and (v.conditional or not conditional) for v in values
    )


def check_output_type(output_type: Any) -> Any:
    """Return ``output_type`` when it is ``None`` or a type `adorm.codec.type_name` names; raise ``TypeError``
    otherwise."""
    if output_type is not None:
        from adorm.codec import type_name

        type_name(output_type)
    return output_type


class Node:
    """Base class of every query node.

    ``_fields`` names the attributes that make up the node's value. Attributes cannot be set or deleted after
    construction.
    """

    _fields: ClassVar[tuple[str, ...]] = ()
    construct: ClassVar[str | None] = None

    def _init(self, **values: Any) -> None:
        for name, value in values.items():
            object.__setattr__(self, name, value)

    def _replace(self, **changes: Any) -> Self:
        clone = copy.copy(self)
        clone.__dict__.update(changes)
        return clone

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def _identity(self) -> tuple[Any, ...]:
        return (type(self), tuple(make_hashable(getattr(self, name)) for name in self._fields))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Node):
            return NotImplemented
        return self._identity() == other._identity()

    def __hash__(self) -> int:
        return hash(self._identity())

    def __repr__(self) -> str:
        args = ", ".join(f"{name}={getattr(self, name)!r}" for name in self._fields)
        return f"{type(self).__name__}({args})"

    def get_source_expressions(self) -> tuple[Node, ...]:
        """The child nodes."""
        return ()

    def walk(self) -> Iterator[Node]:
        """This node and all its descendants, depth first."""
        yield self
        for child in self.get_source_expressions():
            yield from child.walk()

    def _own_constructs(self) -> Iterable[str]:
        return (self.construct,) if self.construct else ()

    def constructs(self) -> Iterator[str]:
        """Construct names (see `Capabilities`) used by this node and its descendants, possibly repeated."""
        yield from self._own_constructs()
        for child in self.get_source_expressions():
            yield from child.constructs()

    def _check_decoded(self) -> None:
        """Validate a node rebuilt by `from_dict` without its constructor; raise ``ValueError``."""

    def to_dict(self, schema: SchemaSource | None = None) -> dict[str, Any]:
        """This node as JSON-safe data; values are encoded by the types ``schema`` gives them (see `adorm.codec`)."""
        from adorm.codec import encode_node

        return encode_node(self, schema)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], schema: SchemaSource | None = None) -> Self:
        """The node ``to_dict()`` produced; ``schema`` must give the values the types they were encoded with."""
        from adorm.codec import decode_node

        node = decode_node(data, schema)
        if not isinstance(node, cls):
            raise ValueError(f"expected a {cls.__name__} node, got {type(node).__name__}")
        return node


def _expression(value: Any, *, fields: bool) -> Expression:
    """``value`` as an expression: expressions as they are, strings as `F` when ``fields``, else `Value`."""
    if isinstance(value, Expression):
        return value
    if isinstance(value, Node):
        raise TypeError(f"{type(value).__name__} is not an expression")
    if fields and isinstance(value, str):
        return F(value)
    return Value(value)


def _condition(value: Any) -> Expression:
    if isinstance(value, Expression) and value.conditional:
        return value
    raise TypeError(f"a condition is a Q, a lookup or another boolean expression, not {type(value).__name__}")


class Expression(Node):
    """Base class of expressions.

    Arithmetic operators build a `CombinedExpression`; a plain operand becomes a `Value`. A conditional expression
    (``conditional`` true: `Q`, lookups, `Exists`) combines with ``&``, ``|`` and ``~`` into a `Q`.
    """

    conditional: ClassVar[bool] = False
    output_type: type | None = None

    ADD: ClassVar[str] = "+"
    SUB: ClassVar[str] = "-"
    MUL: ClassVar[str] = "*"
    DIV: ClassVar[str] = "/"
    MOD: ClassVar[str] = "%"
    POW: ClassVar[str] = "^"

    @property
    def contains_aggregate(self) -> bool:
        """Whether an aggregate occurs in this expression outside a subquery."""
        return any(isinstance(c, Expression) and c.contains_aggregate for c in self.get_source_expressions())

    def _combine(self, other: Any, connector: str, reverse: bool) -> CombinedExpression:
        other = _expression(other, fields=False)
        return CombinedExpression(other, connector, self) if reverse else CombinedExpression(self, connector, other)

    def __add__(self, other: Any) -> CombinedExpression:
        return self._combine(other, self.ADD, False)

    def __radd__(self, other: Any) -> CombinedExpression:
        return self._combine(other, self.ADD, True)

    def __sub__(self, other: Any) -> CombinedExpression:
        return self._combine(other, self.SUB, False)

    def __rsub__(self, other: Any) -> CombinedExpression:
        return self._combine(other, self.SUB, True)

    def __mul__(self, other: Any) -> CombinedExpression:
        return self._combine(other, self.MUL, False)

    def __rmul__(self, other: Any) -> CombinedExpression:
        return self._combine(other, self.MUL, True)

    def __truediv__(self, other: Any) -> CombinedExpression:
        return self._combine(other, self.DIV, False)

    def __rtruediv__(self, other: Any) -> CombinedExpression:
        return self._combine(other, self.DIV, True)

    def __mod__(self, other: Any) -> CombinedExpression:
        return self._combine(other, self.MOD, False)

    def __rmod__(self, other: Any) -> CombinedExpression:
        return self._combine(other, self.MOD, True)

    def __pow__(self, other: Any) -> CombinedExpression:
        return self._combine(other, self.POW, False)

    def __rpow__(self, other: Any) -> CombinedExpression:
        return self._combine(other, self.POW, True)

    def __neg__(self) -> CombinedExpression:
        return self._combine(-1, self.MUL, False)

    def __and__(self, other: Any) -> Q:
        return Q(_condition(self)) & other

    def __or__(self, other: Any) -> Q:
        return Q(_condition(self)) | other

    def __rand__(self, other: Any) -> Q:
        return Q(_condition(other)) & self

    def __ror__(self, other: Any) -> Q:
        return Q(_condition(other)) | self

    def __invert__(self) -> Expression:
        return ~Q(_condition(self))

    def asc(self, *, nulls_first: bool | None = None, nulls_last: bool | None = None) -> OrderBy:
        """Ascending ordering by this expression."""
        return OrderBy(self, descending=False, nulls_first=nulls_first, nulls_last=nulls_last)

    def desc(self, *, nulls_first: bool | None = None, nulls_last: bool | None = None) -> OrderBy:
        """Descending ordering by this expression."""
        return OrderBy(self, descending=True, nulls_first=nulls_first, nulls_last=nulls_last)


class F(Expression):
    """A reference to a field of the queried model, or across relations: ``F("club__name")``. A path of several
    segments uses the construct ``path.nested``."""

    _fields = ("name",)
    name: str

    def __init__(self, name: str) -> None:
        self._init(name=check_field_path(name))

    @property
    def path(self) -> tuple[str, ...]:
        """The field path split at ``__``: relations first, the field last."""
        return tuple(split_field_path(self.name))

    def _own_constructs(self) -> Iterable[str]:
        yield from super()._own_constructs()
        if "__" in self.name:
            yield "path.nested"

    def _check_decoded(self) -> None:
        check_field_path(self.name)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.name!r})"


class OuterRef(F):
    """A reference to a field of the outer query, used inside a `Subquery` or `Exists`.

    ``OuterRef(OuterRef("pk"))`` refers to the query one level further out; ``levels`` counts the levels (1 for the
    enclosing query).
    """

    construct = "expression.OuterRef"
    _fields = ("name", "levels")
    levels: int

    def __init__(self, name: str | OuterRef) -> None:
        if isinstance(name, OuterRef):
            self._init(name=name.name, levels=name.levels + 1)
        else:
            self._init(name=check_field_path(name), levels=1)

    def _check_decoded(self) -> None:
        super()._check_decoded()
        _require(
            isinstance(self.levels, int) and not isinstance(self.levels, bool) and self.levels >= 1,
            "levels is a positive integer",
        )

    def __repr__(self) -> str:
        return "OuterRef(" * self.levels + repr(self.name) + ")" * self.levels


class Value(Expression):
    """A literal value. ``output_type`` optionally names its Python type."""

    _fields = ("value", "output_type")
    value: Any

    def __init__(self, value: Any, output_type: type | None = None) -> None:
        if isinstance(value, Node):
            raise TypeError(f"Value wraps a literal, not {type(value).__name__}")
        self._init(value=freeze_value(value), output_type=check_output_type(output_type))

    def __repr__(self) -> str:
        if self.output_type is None:
            return f"Value({self.value!r})"
        return f"Value({self.value!r}, output_type={self.output_type.__name__})"


class Star(Expression):
    """All rows, as in ``Count("*")``."""

    def __repr__(self) -> str:
        return "Star()"


class CombinedExpression(Expression):
    """``lhs <connector> rhs`` with an arithmetic connector: ``+``, ``-``, ``*``, ``/``, ``%`` or ``^`` (power)."""

    construct = "expression.CombinedExpression"
    CONNECTORS: ClassVar[frozenset[str]] = frozenset({"+", "-", "*", "/", "%", "^"})
    _fields = ("lhs", "connector", "rhs", "output_type")
    lhs: Expression
    connector: str
    rhs: Expression

    def __init__(self, lhs: Any, connector: str, rhs: Any, output_type: type | None = None) -> None:
        if connector not in self.CONNECTORS:
            raise ValueError(f"unknown connector {connector!r}")
        self._init(
            lhs=_expression(lhs, fields=False),
            connector=connector,
            rhs=_expression(rhs, fields=False),
            output_type=check_output_type(output_type),
        )

    def get_source_expressions(self) -> tuple[Node, ...]:
        return (self.lhs, self.rhs)

    def __repr__(self) -> str:
        return f"({self.lhs!r} {self.connector} {self.rhs!r})"

    def _check_decoded(self) -> None:
        if self.connector not in self.CONNECTORS:
            raise ValueError(f"unknown connector {self.connector!r}")
        _require(_is_expressions((self.lhs, self.rhs)), "the operands of a CombinedExpression are expressions")


class OrderBy(Node):
    """Ordering by ``expression``; ``nulls_first`` or ``nulls_last`` (not both) places NULLs."""

    _fields = ("expression", "descending", "nulls_first", "nulls_last")
    expression: Expression
    descending: bool
    nulls_first: bool | None
    nulls_last: bool | None

    def __init__(
        self,
        expression: Any,
        *,
        descending: bool = False,
        nulls_first: bool | None = None,
        nulls_last: bool | None = None,
    ) -> None:
        if isinstance(expression, OrderBy):
            raise TypeError("OrderBy cannot wrap another OrderBy")
        if nulls_first not in (None, True) or nulls_last not in (None, True):
            raise ValueError("nulls_first and nulls_last are True or None")
        if nulls_first and nulls_last:
            raise ValueError("nulls_first and nulls_last are mutually exclusive")
        self._init(
            expression=_expression(expression, fields=True),
            descending=bool(descending),
            nulls_first=nulls_first,
            nulls_last=nulls_last,
        )

    def get_source_expressions(self) -> tuple[Node, ...]:
        return (self.expression,)

    def _check_decoded(self) -> None:
        _require(isinstance(self.expression, Expression), "an OrderBy orders by an expression")
        _require(isinstance(self.descending, bool), "descending is a bool")
        _require(
            self.nulls_first in (None, True) and self.nulls_last in (None, True),
            "nulls_first and nulls_last are True or None",
        )
        _require(not (self.nulls_first and self.nulls_last), "nulls_first and nulls_last are mutually exclusive")


class Func(Expression):
    """Base class of functions. Arguments that are strings are field references; use `Value` for a literal string.

    A subclass sets ``arity`` (exact number of arguments) or ``min_args``, and ``default_output_type``. Its construct
    name is ``function.<class name>``.
    """

    category: ClassVar[str] = "function"
    arity: ClassVar[int | None] = None
    min_args: ClassVar[int] = 0
    default_output_type: ClassVar[type | None] = None
    _fields = ("expressions", "output_type")
    expressions: tuple[Expression, ...]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "construct" not in cls.__dict__:
            cls.construct = f"{cls.category}.{cls.__name__}"

    def __init__(self, *expressions: Any, output_type: type | None = None) -> None:
        if type(self).construct is None:
            raise TypeError(f"{type(self).__name__} is a base class; use one of its subclasses")
        name = type(self).__name__
        if self.arity is not None and len(expressions) != self.arity:
            raise TypeError(f"{name} takes {self.arity} argument(s), got {len(expressions)}")
        if len(expressions) < self.min_args:
            raise TypeError(f"{name} takes at least {self.min_args} arguments, got {len(expressions)}")
        self._init(
            expressions=tuple(_expression(e, fields=True) for e in expressions),
            output_type=check_output_type(output_type) or self.default_output_type,
        )

    def _check_decoded(self) -> None:
        name = type(self).__name__
        _require(type(self).construct is not None, f"{name} is a base class")
        _require(_is_expressions(self.expressions), f"the arguments of {name} are expressions")
        count = len(self.expressions)
        _require(self.arity is None or count == self.arity, f"{name} takes {self.arity} argument(s), got {count}")
        _require(count >= self.min_args, f"{name} takes at least {self.min_args} arguments, got {count}")

    def get_source_expressions(self) -> tuple[Node, ...]:
        return self.expressions

    def __repr__(self) -> str:
        args = [repr(e) for e in self.expressions]
        extras = (n for n in self._fields if n not in ("expressions", "output_type"))
        args += [f"{n}={getattr(self, n)!r}" for n in extras if getattr(self, n) not in (None, False)]
        if self.output_type is not self.default_output_type:
            args.append(f"output_type={self.output_type!r}")
        return f"{type(self).__name__}({', '.join(args)})"


class Lower(Func):
    """The string in lower case."""

    arity = 1


class Upper(Func):
    """The string in upper case."""

    arity = 1


class Length(Func):
    """The number of characters in the string."""

    arity = 1
    default_output_type = int


class Coalesce(Func):
    """The first argument that is not NULL."""

    min_args = 2


class Concat(Func):
    """The arguments concatenated as strings; a NULL argument counts as an empty string."""

    min_args = 2


class Now(Func):
    """The current date and time of the database; the Django and native PostgreSQL drivers give the start time of the
    statement (``STATEMENT_TIMESTAMP()``)."""

    arity = 0
    default_output_type = datetime.datetime


class Trunc(Func):
    """A date or time truncated to ``kind``: ``year``, ``quarter``, ``month``, ``week``, ``day``, ``hour``,
    ``minute``, ``second``, ``date`` or ``time``."""

    KINDS: ClassVar[frozenset[str]] = frozenset(
        {"year", "quarter", "month", "week", "day", "hour", "minute", "second", "date", "time"}
    )
    arity = 1
    _fields = ("expressions", "kind", "output_type")
    kind: str

    def __init__(self, expression: Any, kind: str, output_type: type | None = None) -> None:
        if kind not in self.KINDS:
            raise ValueError(f"unknown truncation kind {kind!r}")
        if output_type is None and kind in ("date", "time"):
            output_type = datetime.date if kind == "date" else datetime.time
        super().__init__(expression, output_type=output_type)
        self._init(kind=kind)

    def _check_decoded(self) -> None:
        super()._check_decoded()
        if self.kind not in self.KINDS:
            raise ValueError(f"unknown truncation kind {self.kind!r}")


class _FixedTrunc(Trunc):
    construct = None
    fixed_kind: ClassVar[str]

    def __init__(self, expression: Any, output_type: type | None = None) -> None:
        super().__init__(expression, self.fixed_kind, output_type)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.expressions[0]!r})"


class TruncYear(_FixedTrunc):
    """``Trunc(expression, "year")``."""

    fixed_kind = "year"


class TruncQuarter(_FixedTrunc):
    """``Trunc(expression, "quarter")``."""

    fixed_kind = "quarter"


class TruncMonth(_FixedTrunc):
    """``Trunc(expression, "month")``."""

    fixed_kind = "month"


class TruncWeek(_FixedTrunc):
    """``Trunc(expression, "week")``: the Monday of the week."""

    fixed_kind = "week"


class TruncDay(_FixedTrunc):
    """``Trunc(expression, "day")``."""

    fixed_kind = "day"


class TruncHour(_FixedTrunc):
    """``Trunc(expression, "hour")``."""

    fixed_kind = "hour"


class TruncMinute(_FixedTrunc):
    """``Trunc(expression, "minute")``."""

    fixed_kind = "minute"


class TruncSecond(_FixedTrunc):
    """``Trunc(expression, "second")``."""

    fixed_kind = "second"


class TruncDate(_FixedTrunc):
    """The date part of a date and time."""

    fixed_kind = "date"


class TruncTime(_FixedTrunc):
    """The time part of a date and time."""

    fixed_kind = "time"


class Aggregate(Func):
    """Base class of aggregates.

    ``distinct`` aggregates distinct values (where the aggregate allows it), ``filter`` (a condition) limits the
    rows aggregated and ``default`` replaces a NULL result; a string ``default`` is a field reference. The construct
    names are ``aggregate.<class name>``, plus ``aggregate.distinct`` and ``aggregate.filter`` when used.
    """

    category = "aggregate"
    construct = None
    arity = 1
    allow_distinct: ClassVar[bool] = False
    allow_default: ClassVar[bool] = True
    _fields = ("expressions", "distinct", "filter", "default", "output_type")
    distinct: bool
    filter: Expression | None
    default: Expression | None

    def __init__(
        self,
        *expressions: Any,
        distinct: bool = False,
        filter: Any = None,
        default: Any = None,
        output_type: type | None = None,
    ) -> None:
        name = type(self).__name__
        if distinct and not self.allow_distinct:
            raise TypeError(f"{name} does not allow distinct")
        if default is not None and not self.allow_default:
            raise TypeError(f"{name} does not allow default")
        super().__init__(*expressions, output_type=output_type)
        if any(e.contains_aggregate for e in self.expressions):
            raise TypeError(f"{name} cannot aggregate an aggregate")
        if isinstance(filter, Q) and not filter:
            filter = None
        self._init(
            distinct=bool(distinct),
            filter=None if filter is None else _condition(filter),
            default=None if default is None else _expression(default, fields=True),
        )
        if any(e is not None and e.contains_aggregate for e in (self.filter, self.default)):
            raise TypeError(f"the filter and default of {name} cannot contain an aggregate")

    @property
    def contains_aggregate(self) -> bool:
        return True

    @property
    def default_alias(self) -> str:
        """The result name of an aggregate passed to ``aggregate()`` without a name: ``<field path>__<class name in
        lower case>`` (``Sum("fee")`` → ``"fee__sum"``). An aggregate of anything but a field raises ``TypeError``."""
        (expression,) = self.expressions
        if type(expression) is not F:
            raise TypeError(f"{self!r} has no default name: pass it as a keyword argument")
        return f"{expression.name}__{type(self).__name__.lower()}"

    def get_source_expressions(self) -> tuple[Node, ...]:
        return self.expressions + tuple(e for e in (self.filter, self.default) if e is not None)

    def _check_decoded(self) -> None:
        super()._check_decoded()
        name = type(self).__name__
        _require(isinstance(self.distinct, bool), "distinct is a bool")
        _require(not self.distinct or self.allow_distinct, f"{name} does not allow distinct")
        _require(not self.distinct or not isinstance(self.expressions[0], Star), f"{name}('*') does not allow distinct")
        _require(self.default is None or self.allow_default, f"{name} does not allow default")
        _require(
            self.filter is None or (isinstance(self.filter, Expression) and self.filter.conditional),
            f"the filter of {name} is a condition",
        )
        _require(
            self.default is None or isinstance(self.default, Expression), f"the default of {name} is an expression"
        )
        inner = (*self.expressions, *(e for e in (self.filter, self.default) if e is not None))
        _require(not any(e.contains_aggregate for e in inner), f"{name} cannot contain an aggregate")

    def _own_constructs(self) -> Iterable[str]:
        yield from super()._own_constructs()
        if self.distinct:
            yield "aggregate.distinct"
        if self.filter is not None:
            yield "aggregate.filter"


class Count(Aggregate):
    """The number of rows (``Count("*")``) or of non-NULL values. ``default`` is not allowed: a count is never NULL."""

    allow_distinct = True
    allow_default = False
    default_output_type = int

    def __init__(
        self,
        expression: Any,
        *,
        distinct: bool = False,
        filter: Any = None,
        output_type: type | None = None,
    ) -> None:
        if expression == "*":
            if distinct:
                raise TypeError("Count('*') does not allow distinct")
            expression = Star()
        super().__init__(expression, distinct=distinct, filter=filter, output_type=output_type)


class Sum(Aggregate):
    """The sum of the values."""

    allow_distinct = True


class Avg(Aggregate):
    """The average of the values."""

    allow_distinct = True


class Min(Aggregate):
    """The smallest value."""


class Max(Aggregate):
    """The largest value."""


class When(Expression):
    """A branch of `Case`: ``result`` when ``condition`` holds.

    The condition is a `Q`, a boolean expression, keyword lookups (``When(active=True, then=...)``) or both. A string
    ``then`` is a field reference.
    """

    _fields = ("condition", "result")
    condition: Expression
    result: Expression

    def __init__(self, condition: Any = None, then: Any = None, **lookups: Any) -> None:
        if lookups:
            condition = Q(**lookups) if condition is None else Q(_condition(condition), **lookups)
        if condition is None:
            raise TypeError("When needs a condition")
        if isinstance(condition, Q) and not condition:
            raise ValueError("an empty Q cannot be a When condition")
        self._init(condition=_condition(condition), result=_expression(then, fields=True))

    def get_source_expressions(self) -> tuple[Node, ...]:
        return (self.condition, self.result)

    def _check_decoded(self) -> None:
        _require(_is_expressions((self.condition,), conditional=True), "the condition of a When is a condition")
        _require(isinstance(self.result, Expression), "the result of a When is an expression")


class Case(Expression):
    """The result of the first `When` whose condition holds, else ``default``. A string ``default`` is a field
    reference."""

    construct = "expression.Case"
    _fields = ("cases", "default", "output_type")
    cases: tuple[When, ...]
    default: Expression

    def __init__(self, *cases: When, default: Any = None, output_type: type | None = None) -> None:
        if not all(isinstance(c, When) for c in cases):
            raise TypeError("Case takes When arguments")
        self._init(
            cases=tuple(cases), default=_expression(default, fields=True), output_type=check_output_type(output_type)
        )

    def get_source_expressions(self) -> tuple[Node, ...]:
        return (*self.cases, self.default)

    def _check_decoded(self) -> None:
        _require(
            isinstance(self.cases, tuple) and all(isinstance(c, When) for c in self.cases), "Case takes When nodes"
        )
        _require(isinstance(self.default, Expression), "the default of a Case is an expression")


class Subquery(Expression):
    """A query used as an expression. The query selects one column (``values("field")``); `OuterRef` in it refers to
    the outer query. Takes a `Query` or an object with a ``query`` attribute that is one."""

    construct = "expression.Subquery"
    _fields = ("query", "output_type")
    query: Query

    def __init__(self, query: Any, output_type: type | None = None) -> None:
        query = self._query(query)
        if len(query.select) > 1:
            raise ValueError("a subquery selects one column")
        self._init(query=query, output_type=check_output_type(output_type))

    @staticmethod
    def _query(value: Any) -> Query:
        from adorm.query.query import as_query

        query = as_query(value)
        if query is None:
            raise TypeError(f"expected a query, not {type(value).__name__}")
        return query

    @property
    def contains_aggregate(self) -> bool:
        return False

    def get_source_expressions(self) -> tuple[Node, ...]:
        return (self.query,)

    def _check_decoded(self) -> None:
        from adorm.query.query import Query

        _require(isinstance(self.query, Query), f"a {type(self).__name__} holds a Query")
        _require(len(self.query.select) <= 1 or isinstance(self, Exists), "a subquery selects one column")


class Exists(Subquery):
    """Whether the query returns a row. ``~Exists(query)`` negates it."""

    construct = "expression.Exists"
    conditional = True
    output_type = bool
    _fields = ("query", "negated")
    negated: bool

    def __init__(self, query: Any, *, negated: bool = False) -> None:
        self._init(query=self._query(query), negated=bool(negated))

    def __invert__(self) -> Exists:
        return self._replace(negated=not self.negated)

    def _check_decoded(self) -> None:
        super()._check_decoded()
        _require(isinstance(self.negated, bool), "negated is a bool")


class Q(Expression):
    """A condition: lookups joined with AND, combined with ``&`` (AND), ``|`` (OR) and ``~`` (NOT).

    Keyword arguments are lookups (``name__icontains="ann"``), joined with AND in sorted key order. Positional
    arguments are other conditions (`Q`, lookups, `Exists`) or ``(key, value)`` pairs; an empty `Q` argument is
    dropped, and the children of a `Q` argument that is not negated and has the same connector or one child are
    merged. ``Q()`` is empty: it is falsy and combining with it returns the other operand.
    """

    AND: ClassVar[str] = "AND"
    OR: ClassVar[str] = "OR"
    conditional = True
    output_type = bool
    _fields = ("children", "connector", "negated")
    children: tuple[Expression, ...]
    connector: str
    negated: bool

    def __init__(self, *args: Any, _connector: str = "AND", _negated: bool = False, **kwargs: Any) -> None:
        from adorm.query.lookups import build_lookup

        children: list[Expression] = []
        for arg in args:
            if isinstance(arg, tuple) and len(arg) == 2 and isinstance(arg[0], str):
                children.append(build_lookup(*arg))
            elif isinstance(arg, Q):
                if not arg.negated and (arg.connector == _connector or len(arg.children) == 1):
                    children.extend(arg.children)
                elif arg:
                    children.append(arg)
            else:
                children.append(_condition(arg))
        children.extend(build_lookup(key, value) for key, value in sorted(kwargs.items()))
        self._set(tuple(children), _connector, _negated)

    def _set(self, children: tuple[Expression, ...], connector: str, negated: bool) -> None:
        if connector not in (self.AND, self.OR):
            raise ValueError(f"unknown connector {connector!r}")
        self._init(children=children, connector=connector, negated=bool(negated))

    def _check_decoded(self) -> None:
        if self.connector not in (self.AND, self.OR):
            raise ValueError(f"unknown connector {self.connector!r}")
        _require(_is_expressions(self.children, conditional=True), "the children of a Q are conditions")
        _require(isinstance(self.negated, bool), "negated is a bool")

    @classmethod
    def _make(cls, children: tuple[Expression, ...], connector: str, negated: bool) -> Q:
        q = cls.__new__(cls)
        q._set(children, connector, negated)
        return q

    def __bool__(self) -> bool:
        return bool(self.children)

    def __len__(self) -> int:
        return len(self.children)

    def _combine_q(self, other: Any, connector: str) -> Q:
        other = other if isinstance(other, Q) else Q(_condition(other))
        if not other:
            return self
        if not self:
            return other
        children: list[Expression] = []
        for q in (self, other):
            if not q.negated and (q.connector == connector or len(q.children) == 1):
                children.extend(q.children)
            else:
                children.append(q)
        return self._make(tuple(children), connector, False)

    def __and__(self, other: Any) -> Q:
        return self._combine_q(other, self.AND)

    def __or__(self, other: Any) -> Q:
        return self._combine_q(other, self.OR)

    def __rand__(self, other: Any) -> Q:
        return Q(_condition(other))._combine_q(self, self.AND)

    def __ror__(self, other: Any) -> Q:
        return Q(_condition(other))._combine_q(self, self.OR)

    def __invert__(self) -> Q:
        return self._make(self.children, self.connector, not self.negated)

    def get_source_expressions(self) -> tuple[Node, ...]:
        return self.children

    def __repr__(self) -> str:
        inner = f"{self.connector}: {', '.join(repr(c) for c in self.children)}"
        return f"<Q: NOT ({inner})>" if self.negated else f"<Q: ({inner})>"
