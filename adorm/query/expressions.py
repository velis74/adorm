"""Expressions: field references, values, functions, aggregates, conditions and `Q`.

Every node is immutable and compares by value: two nodes are equal when they have the same class and equal
fields. Building a node does no I/O and needs no model; a driver resolves field names against the model and
translates the node into its target's construct.
"""

from __future__ import annotations

import copy
import datetime
import decimal
import re
import uuid

from collections.abc import Iterable, Iterator, Mapping, Set
from typing import Any, ClassVar, Self, TYPE_CHECKING

if TYPE_CHECKING:
    from adorm.query.query import Query
    from adorm.schema import SchemaSource

__all__ = [
    "Cast",
    "Collate",
    "Greatest",
    "Least",
    "NullIf",
    "JSONArray",
    "JSONObject",
    "Extract",
    "ExtractYear",
    "ExtractIsoYear",
    "ExtractQuarter",
    "ExtractMonth",
    "ExtractWeek",
    "ExtractDay",
    "ExtractWeekDay",
    "ExtractIsoWeekDay",
    "ExtractHour",
    "ExtractMinute",
    "ExtractSecond",
    "Abs",
    "ACos",
    "ASin",
    "ATan",
    "ATan2",
    "Ceil",
    "Cos",
    "Cot",
    "Degrees",
    "Exp",
    "Floor",
    "Ln",
    "Log",
    "Mod",
    "Pi",
    "Power",
    "Radians",
    "Random",
    "Round",
    "Sign",
    "Sin",
    "Sqrt",
    "Tan",
    "Chr",
    "Left",
    "LPad",
    "LTrim",
    "MD5",
    "Ord",
    "Repeat",
    "Replace",
    "Reverse",
    "Right",
    "RPad",
    "RTrim",
    "SHA1",
    "SHA224",
    "SHA256",
    "SHA384",
    "SHA512",
    "StrIndex",
    "Substr",
    "Trim",
    "UUID4",
    "UUID7",
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

    A subclass sets ``arity`` (exact number of arguments) or ``min_args`` and ``max_args``, and
    ``default_output_type``. ``output_from`` names where the type of a function without an output type comes from:
    ``"arguments"`` (the first argument of a known type; every argument has the result's type), ``"first"`` (the
    first argument) or ``"numeric"`` (``Decimal`` when an argument is a ``Decimal``, else ``float``). Its construct
    name is ``function.<class name>``.
    """

    category: ClassVar[str] = "function"
    arity: ClassVar[int | None] = None
    min_args: ClassVar[int] = 0
    max_args: ClassVar[int | None] = None
    default_output_type: ClassVar[type | None] = None
    output_from: ClassVar[str | None] = None
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
        if self.max_args is not None and len(expressions) > self.max_args:
            raise TypeError(f"{name} takes at most {self.max_args} arguments, got {len(expressions)}")
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
        _require(
            self.max_args is None or count <= self.max_args,
            f"{name} takes at most {self.max_args} arguments, got {count}",
        )

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
    output_from = "arguments"


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


def _check_literal(value: Any, name: str, minimum: int, message: str) -> None:
    """Raise ``ValueError`` with ``message`` when ``value`` is a literal number below ``minimum``."""
    if isinstance(value, int | float) and not isinstance(value, bool) and value < minimum:
        raise ValueError(f"{name!r} {message}")


# Comparison and conversion functions


class Cast(Func):
    """The expression converted to ``output_type`` in the database. ``max_length`` makes a cast to ``str`` a
    ``varchar(max_length)``; ``max_digits`` and ``decimal_places`` (both or neither) give a cast to ``Decimal`` its
    precision and scale."""

    arity = 1
    _fields = ("expressions", "output_type", "max_length", "max_digits", "decimal_places")
    max_length: int | None
    max_digits: int | None
    decimal_places: int | None

    def __init__(
        self,
        expression: Any,
        output_type: type,
        *,
        max_length: int | None = None,
        max_digits: int | None = None,
        decimal_places: int | None = None,
    ) -> None:
        if output_type is None:
            raise TypeError("Cast needs an output type")
        super().__init__(expression, output_type=output_type)
        self._init(max_length=max_length, max_digits=max_digits, decimal_places=decimal_places)
        self._check_options(TypeError)

    def _check_options(self, error: type[Exception]) -> None:
        def size(value: Any) -> bool:
            return value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0)

        if self.output_type is None:
            raise error("Cast needs an output type")
        if not all(size(v) for v in (self.max_length, self.max_digits, self.decimal_places)):
            raise ValueError("max_length, max_digits and decimal_places are non-negative integers")
        if self.max_length is not None and self.output_type is not str:
            raise error("max_length is for a cast to str")
        if (self.max_digits is None) != (self.decimal_places is None):
            raise error("max_digits and decimal_places are given together")
        if self.max_digits is not None:
            if self.output_type is not decimal.Decimal:
                raise error("max_digits and decimal_places are for a cast to Decimal")
            if self.decimal_places > self.max_digits:
                raise ValueError("decimal_places cannot exceed max_digits")

    def _check_decoded(self) -> None:
        super()._check_decoded()
        self._check_options(ValueError)

    def __repr__(self) -> str:
        options = "".join(
            f", {name}={getattr(self, name)!r}"
            for name in ("max_length", "max_digits", "decimal_places")
            if getattr(self, name) is not None
        )
        return f"Cast({self.expressions[0]!r}, {self.output_type.__name__}{options})"


_COLLATION = re.compile(r"^[\w-]+\Z")


class Collate(Func):
    """The expression with the collation ``collation``, a name of letters, digits, ``_`` and ``-``."""

    arity = 1
    output_from = "first"
    _fields = ("expressions", "collation", "output_type")
    collation: str

    def __init__(self, expression: Any, collation: str) -> None:
        if not isinstance(collation, str) or not _COLLATION.match(collation):
            raise ValueError(f"invalid collation name {collation!r}")
        super().__init__(expression)
        self._init(collation=collation)

    def _check_decoded(self) -> None:
        super()._check_decoded()
        _require(isinstance(self.collation, str) and bool(_COLLATION.match(self.collation)), "invalid collation name")


class Greatest(Func):
    """The largest argument. On PostgreSQL NULL arguments are ignored; on other databases a NULL argument makes the
    result NULL."""

    min_args = 2
    output_from = "arguments"


class Least(Func):
    """The smallest argument; NULL arguments as for `Greatest`."""

    min_args = 2
    output_from = "arguments"


class NullIf(Func):
    """NULL when the two arguments are equal, else the first."""

    arity = 2
    output_from = "arguments"


# JSON functions


class JSONArray(Func):
    """A JSON array of the arguments."""

    default_output_type = list


class JSONObject(Func):
    """A JSON object with a key for each keyword argument; the values are expressions (strings are field
    references)."""

    default_output_type = dict
    _fields = ("keys", "expressions", "output_type")
    keys: tuple[str, ...]

    def __init__(self, **fields: Any) -> None:
        super().__init__(*fields.values())
        self._init(keys=tuple(fields))

    def _check_decoded(self) -> None:
        super()._check_decoded()
        _require(
            isinstance(self.keys, tuple)
            and all(isinstance(key, str) for key in self.keys)
            and len(self.keys) == len(self.expressions),
            "a JSONObject has a string key for each expression",
        )

    def __repr__(self) -> str:
        fields = ", ".join(f"{key}={value!r}" for key, value in zip(self.keys, self.expressions, strict=True))
        return f"JSONObject({fields})"


# Date and time functions


class Extract(Func):
    """A component of a date, time, date and time or duration as an ``int``. ``lookup_name`` is ``year``,
    ``iso_year``, ``quarter``, ``month``, ``week``, ``day``, ``week_day`` (1 for Sunday to 7), ``iso_week_day``
    (1 for Monday to 7), ``hour``, ``minute`` or ``second``; a date and time is taken in the current time zone."""

    KINDS: ClassVar[frozenset[str]] = frozenset(
        {
            "year",
            "iso_year",
            "quarter",
            "month",
            "week",
            "day",
            "week_day",
            "iso_week_day",
            "hour",
            "minute",
            "second",
        }
    )
    arity = 1
    default_output_type = int
    _fields = ("expressions", "lookup_name", "output_type")
    lookup_name: str

    def __init__(self, expression: Any, lookup_name: str, output_type: type | None = None) -> None:
        if lookup_name not in self.KINDS:
            raise ValueError(f"unknown extract lookup name {lookup_name!r}")
        super().__init__(expression, output_type=output_type)
        self._init(lookup_name=lookup_name)

    def _check_decoded(self) -> None:
        super()._check_decoded()
        if self.lookup_name not in self.KINDS:
            raise ValueError(f"unknown extract lookup name {self.lookup_name!r}")


class _FixedExtract(Extract):
    construct = None
    fixed_lookup_name: ClassVar[str]

    def __init__(self, expression: Any, output_type: type | None = None) -> None:
        super().__init__(expression, self.fixed_lookup_name, output_type)

    def _check_decoded(self) -> None:
        super()._check_decoded()
        _require(self.lookup_name == self.fixed_lookup_name, f"{type(self).__name__} extracts {self.fixed_lookup_name}")

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.expressions[0]!r})"


class ExtractYear(_FixedExtract):
    """``Extract(expression, "year")``."""

    fixed_lookup_name = "year"


class ExtractIsoYear(_FixedExtract):
    """``Extract(expression, "iso_year")``: the ISO 8601 week-numbering year."""

    fixed_lookup_name = "iso_year"


class ExtractQuarter(_FixedExtract):
    """``Extract(expression, "quarter")``."""

    fixed_lookup_name = "quarter"


class ExtractMonth(_FixedExtract):
    """``Extract(expression, "month")``."""

    fixed_lookup_name = "month"


class ExtractWeek(_FixedExtract):
    """``Extract(expression, "week")``: the ISO 8601 week number."""

    fixed_lookup_name = "week"


class ExtractDay(_FixedExtract):
    """``Extract(expression, "day")``."""

    fixed_lookup_name = "day"


class ExtractWeekDay(_FixedExtract):
    """``Extract(expression, "week_day")``: 1 for Sunday to 7 for Saturday."""

    fixed_lookup_name = "week_day"


class ExtractIsoWeekDay(_FixedExtract):
    """``Extract(expression, "iso_week_day")``: 1 for Monday to 7 for Sunday."""

    fixed_lookup_name = "iso_week_day"


class ExtractHour(_FixedExtract):
    """``Extract(expression, "hour")``."""

    fixed_lookup_name = "hour"


class ExtractMinute(_FixedExtract):
    """``Extract(expression, "minute")``."""

    fixed_lookup_name = "minute"


class ExtractSecond(_FixedExtract):
    """``Extract(expression, "second")``."""

    fixed_lookup_name = "second"


# Math functions


class Abs(Func):
    """The absolute value."""

    arity = 1
    output_from = "first"


class ACos(Func):
    """The arccosine, in radians."""

    arity = 1
    output_from = "numeric"


class ASin(Func):
    """The arcsine, in radians."""

    arity = 1
    output_from = "numeric"


class ATan(Func):
    """The arctangent, in radians."""

    arity = 1
    output_from = "numeric"


class ATan2(Func):
    """The arctangent of the first argument divided by the second, in radians."""

    arity = 2
    output_from = "numeric"


class Ceil(Func):
    """The smallest integer not less than the value."""

    arity = 1
    output_from = "first"


class Cos(Func):
    """The cosine of an angle in radians."""

    arity = 1
    output_from = "numeric"


class Cot(Func):
    """The cotangent of an angle in radians."""

    arity = 1
    output_from = "numeric"


class Degrees(Func):
    """Radians converted to degrees."""

    arity = 1
    output_from = "numeric"


class Exp(Func):
    """*e* raised to the value."""

    arity = 1
    output_from = "numeric"


class Floor(Func):
    """The largest integer not greater than the value."""

    arity = 1
    output_from = "first"


class Ln(Func):
    """The natural logarithm."""

    arity = 1
    output_from = "numeric"


class Log(Func):
    """The logarithm of the second argument to the base of the first."""

    arity = 2
    output_from = "numeric"


class Mod(Func):
    """The remainder of the first argument divided by the second."""

    arity = 2
    output_from = "numeric"


class Pi(Func):
    """The number π."""

    arity = 0
    default_output_type = float


class Power(Func):
    """The first argument raised to the second."""

    arity = 2
    output_from = "numeric"


class Radians(Func):
    """Degrees converted to radians."""

    arity = 1
    output_from = "numeric"


class Random(Func):
    """A random number from 0 (included) to 1 (excluded), a new one for each row."""

    arity = 0
    default_output_type = float


class Round(Func):
    """The value rounded to ``precision`` decimal places; a negative precision rounds to tens, hundreds …"""

    arity = 2
    output_from = "first"

    def __init__(self, expression: Any, precision: Any = 0, output_type: type | None = None) -> None:
        super().__init__(expression, precision, output_type=output_type)


class Sign(Func):
    """-1, 0 or 1 for a negative, zero or positive value."""

    arity = 1
    output_from = "first"


class Sin(Func):
    """The sine of an angle in radians."""

    arity = 1
    output_from = "numeric"


class Sqrt(Func):
    """The square root."""

    arity = 1
    output_from = "numeric"


class Tan(Func):
    """The tangent of an angle in radians."""

    arity = 1
    output_from = "numeric"


# Text functions


class Chr(Func):
    """The character with the Unicode code point given."""

    arity = 1
    default_output_type = str


class Left(Func):
    """The first ``length`` characters (more than 0) of the string."""

    arity = 2
    default_output_type = str

    def __init__(self, expression: Any, length: Any, output_type: type | None = None) -> None:
        _check_literal(length, "length", 1, "must be greater than 0")
        super().__init__(expression, length, output_type=output_type)


class Right(Left):
    """The last ``length`` characters (more than 0) of the string."""


class LPad(Func):
    """The string padded on the left with ``fill_text`` (a space by default) to ``length`` characters; a longer
    string is cut to ``length``."""

    arity = 3
    default_output_type = str

    def __init__(self, expression: Any, length: Any, fill_text: Any = None, output_type: type | None = None) -> None:
        _check_literal(length, "length", 0, "must be greater than or equal to 0")
        fill = Value(" ") if fill_text is None else fill_text
        super().__init__(expression, length, fill, output_type=output_type)


class RPad(LPad):
    """The string padded on the right, as `LPad`."""


class LTrim(Func):
    """The string without leading spaces."""

    arity = 1
    output_from = "first"


class RTrim(Func):
    """The string without trailing spaces."""

    arity = 1
    output_from = "first"


class Trim(Func):
    """The string without leading and trailing spaces."""

    arity = 1
    output_from = "first"


class MD5(Func):
    """The MD5 hash of the string, in hexadecimal."""

    arity = 1
    default_output_type = str


class SHA1(Func):
    """The SHA-1 hash of the string, in hexadecimal; on PostgreSQL it needs the ``pgcrypto`` extension."""

    arity = 1
    default_output_type = str


class SHA224(Func):
    """The SHA-224 hash of the string, in hexadecimal; on PostgreSQL it needs the ``pgcrypto`` extension."""

    arity = 1
    default_output_type = str


class SHA256(Func):
    """The SHA-256 hash of the string, in hexadecimal; on PostgreSQL it needs the ``pgcrypto`` extension."""

    arity = 1
    default_output_type = str


class SHA384(Func):
    """The SHA-384 hash of the string, in hexadecimal; on PostgreSQL it needs the ``pgcrypto`` extension."""

    arity = 1
    default_output_type = str


class SHA512(Func):
    """The SHA-512 hash of the string, in hexadecimal; on PostgreSQL it needs the ``pgcrypto`` extension."""

    arity = 1
    default_output_type = str


class Ord(Func):
    """The Unicode code point of the first character of the string."""

    arity = 1
    default_output_type = int


class Repeat(Func):
    """The string repeated ``number`` times (0 or more)."""

    arity = 2
    default_output_type = str

    def __init__(self, expression: Any, number: Any, output_type: type | None = None) -> None:
        _check_literal(number, "number", 0, "must be greater than or equal to 0")
        super().__init__(expression, number, output_type=output_type)


class Replace(Func):
    """The string with every occurrence of ``text`` replaced by ``replacement`` (removed by default); case
    sensitive."""

    arity = 3
    output_from = "first"

    def __init__(self, expression: Any, text: Any, replacement: Any = None, output_type: type | None = None) -> None:
        replacement = Value("") if replacement is None else replacement
        super().__init__(expression, text, replacement, output_type=output_type)


class Reverse(Func):
    """The string with its characters in reverse order."""

    arity = 1
    output_from = "first"


class StrIndex(Func):
    """The position (from 1) of the first occurrence of ``substring`` in the string, 0 when there is none; case
    sensitive."""

    arity = 2
    default_output_type = int


class Substr(Func):
    """``length`` characters of the string from position ``pos`` (from 1); to the end without ``length``."""

    min_args = 2
    max_args = 3
    default_output_type = str

    def __init__(self, expression: Any, pos: Any, length: Any = None, output_type: type | None = None) -> None:
        _check_literal(pos, "pos", 1, "must be greater than 0")
        arguments = (expression, pos) if length is None else (expression, pos, length)
        super().__init__(*arguments, output_type=output_type)


# UUID functions


class UUID4(Func):
    """A random version 4 UUID, a new one for each row."""

    arity = 0
    default_output_type = uuid.UUID


class UUID7(Func):
    """A version 7 (time-ordered) UUID, a new one for each row; ``shift`` (a ``timedelta`` or an expression of one)
    moves its timestamp."""

    max_args = 1
    default_output_type = uuid.UUID

    def __init__(self, shift: Any = None, output_type: type | None = None) -> None:
        super().__init__(*(() if shift is None else (shift,)), output_type=output_type)


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
