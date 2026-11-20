"""Lookups and transforms: the conditions of ``filter(field__lookup=value)``, the transforms a field path applies
to a field (``joined__year``), and their parsing.

A lookup key is a field path followed by an optional lookup name, joined with ``__``: ``"name"``,
``"name__icontains"``, ``"club__owner__name__startswith"``, ``"joined__year__gte"``. The last segment is the lookup
name when it is a registered lookup and the key has more than one segment; otherwise the whole key is the field path
and the lookup is ``exact``. A field whose name equals a lookup name is filtered with an explicit lookup
(``"range__exact"``). A field path is relations, a field, then transforms (`TRANSFORMS`) or, after a JSON field,
keys inside its value. `resolve_lookup` and `check_lookups` resolve keys and conditions against model schemas.
"""

from __future__ import annotations

import datetime
import difflib

from collections.abc import Iterable, Mapping, Set
from dataclasses import dataclass
from types import MappingProxyType, UnionType
from typing import Annotated, Any, ClassVar, get_args, get_origin, is_typeddict, TYPE_CHECKING, Union

from adorm.exceptions import FieldError
from adorm.query.expressions import (
    _check_sql,
    _DEFAULT_TEMPLATE,
    _expression,
    _sql_template,
    Expression,
    F,
    freeze_value,
    Node,
    OuterRef,
    split_field_path,
    Subquery,
)
from adorm.query.query import as_query, check_model_label, Query

if TYPE_CHECKING:
    from adorm.schema import SchemaSource

__all__ = [
    "LOOKUPS",
    "TRANSFORMS",
    "ContainedBy",
    "Contains",
    "Date",
    "Day",
    "EndsWith",
    "Exact",
    "GreaterThan",
    "GreaterThanOrEqual",
    "HasAnyKeys",
    "HasKey",
    "HasKeys",
    "Hour",
    "IContains",
    "IEndsWith",
    "IExact",
    "In",
    "IRegex",
    "IsNull",
    "IStartsWith",
    "IsoWeekDay",
    "IsoYear",
    "LessThan",
    "LessThanOrEqual",
    "Lookup",
    "Minute",
    "Month",
    "Overlap",
    "Quarter",
    "Range",
    "Regex",
    "ResolvedLookup",
    "Second",
    "StartsWith",
    "Time",
    "Transform",
    "Week",
    "WeekDay",
    "Year",
    "build_lookup",
    "check_lookups",
    "register_lookup",
    "register_transform",
    "resolve_lookup",
    "split_lookup",
]

_LOOKUPS: dict[str, type[Lookup]] = {}
LOOKUPS: Mapping[str, type[Lookup]] = MappingProxyType(_LOOKUPS)
"""Registered lookups by name (read-only view)."""

_TRANSFORMS: dict[str, type[Transform]] = {}
TRANSFORMS: Mapping[str, type[Transform]] = MappingProxyType(_TRANSFORMS)
"""Registered transforms by name (read-only view)."""


class Lookup(Expression):
    """Base class of lookups: a condition on ``lhs`` with the right-hand side ``rhs``.

    ``lhs`` is an expression; a string is a field path. ``rhs`` is a plain value or an expression; a query (a
    `Query` or an object with a ``query`` attribute that is one) becomes a `Subquery`. A plain value is copied
    (`freeze_value`). ``None`` is rejected unless ``rhs_accepts_none``: a NULL test is `IsNull`. The construct name is
    ``lookup.<lookup_name>``.

    A lookup of the application's own declares its SQL in ``template`` (or ``templates``, one per database vendor)
    with ``%(lhs)s`` and ``%(rhs)s``; drivers that render SQL run it.
    """

    lookup_name: ClassVar[str | None] = None
    rhs_is_sequence: ClassVar[bool] = False
    rhs_accepts_none: ClassVar[bool] = False
    """Whether ``None`` is a right-hand side value."""
    on_relations: ClassVar[bool] = False
    """Whether the lookup applies to a relation (compared by the target's primary key)."""
    conditional = True
    output_type = bool
    _fields = ("lhs", "rhs")
    lhs: Expression
    rhs: Any

    template: ClassVar[str | None] = None
    """The SQL template for every vendor without one in ``templates``."""
    templates: ClassVar[Mapping[str, str] | None] = None
    """SQL templates by database vendor."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.lookup_name is not None and "construct" not in cls.__dict__:
            cls.construct = f"lookup.{cls.lookup_name}"
        _check_sql(cls)

    @classmethod
    def sql_template(cls, vendor: str) -> str | None:
        """The SQL template on databases of ``vendor``: ``templates[vendor]``, else ``template``; ``None`` without
        one."""
        return _sql_template(cls, vendor)

    def __init__(self, lhs: Any, rhs: Any) -> None:
        if self.lookup_name is None:
            raise TypeError(f"{type(self).__name__} is a base class; use one of its subclasses")
        self._init(lhs=_expression(lhs, fields=True), rhs=self.prepare_rhs(rhs))

    def prepare_rhs(self, value: Any) -> Any:
        """Validate and normalise the right-hand side."""
        if isinstance(value, Expression):
            return value
        if as_query(value) is not None:
            return Subquery(value)
        if isinstance(value, Node):
            raise TypeError(f"{type(value).__name__} is not a value for the {self.lookup_name} lookup")
        if value is None and not self.rhs_accepts_none:
            raise ValueError(f"None is not a value for the {self.lookup_name} lookup; use isnull")
        return freeze_value(value)

    @classmethod
    def rhs_type(cls, field_type: Any) -> Any:
        """The type of a right-hand side value (of each value when ``rhs_is_sequence``) for a field of
        ``field_type``; ``None`` when unknown. Defaults to ``field_type``."""
        return field_type

    @classmethod
    def applies_to(cls, field_type: Any, keys: tuple[str, ...]) -> bool:  # noqa: ARG003 - any field by default
        """Whether the lookup applies to a field of ``field_type`` (``None`` when unknown), or to the key path
        ``keys`` inside a JSON or record field; ``True`` by default."""
        return True

    def _check_decoded(self) -> None:
        if self.lookup_name is None:
            raise ValueError(f"{type(self).__name__} is a base class")
        if not isinstance(self.lhs, Expression):
            raise ValueError(f"the lhs of the {self.lookup_name} lookup is an expression")
        if self.rhs_is_sequence and not isinstance(self.rhs, tuple | Expression):
            raise ValueError(f"the {self.lookup_name} lookup takes a sequence of values")
        values = self.rhs if self.rhs_is_sequence and isinstance(self.rhs, tuple) else (self.rhs,)
        for value in values:
            if isinstance(value, Node) and not isinstance(value, Expression):
                raise ValueError(f"{type(value).__name__} is not a value for the {self.lookup_name} lookup")
            if value is None and not self.rhs_accepts_none:
                raise ValueError(f"None is not a value for the {self.lookup_name} lookup")

    def get_source_expressions(self) -> tuple[Node, ...]:
        rhs = self.rhs if isinstance(self.rhs, tuple) else (self.rhs,)
        return (self.lhs, *(v for v in rhs if isinstance(v, Expression)))

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.lhs!r}, {self.rhs!r})"


def register_lookup(cls: type[Lookup]) -> type[Lookup]:
    """Register a `Lookup` subclass under its ``lookup_name``; usable as a class decorator.

    Raises ``ValueError`` when another class is registered under that name, or a transform is.
    """
    name = cls.lookup_name
    if not isinstance(name, str) or not name.isidentifier() or "__" in name:
        raise ValueError(f"{cls.__name__} needs a lookup_name that is an identifier without '__'")
    if _LOOKUPS.get(name, cls) is not cls:
        raise ValueError(f"lookup {name!r} is already registered as {_LOOKUPS[name].__name__}")
    if name in _TRANSFORMS:
        raise ValueError(f"{name!r} is registered as the transform {_TRANSFORMS[name].__name__}")
    _LOOKUPS[name] = cls
    return cls


def _base_type(tp: Any) -> Any:
    """``tp`` without ``Optional`` and ``Annotated``; ``None`` for a union of several types."""
    origin = get_origin(tp)
    if origin is Annotated:
        return _base_type(get_args(tp)[0])
    if origin in (Union, UnionType):
        found = [arg for arg in get_args(tp) if arg is not type(None)]
        return _base_type(found[0]) if len(found) == 1 else None
    return tp


def _is_type(tp: Any, *types: type) -> bool:
    """Whether ``tp`` (`_base_type`) is one of ``types`` or a subclass of one; a ``datetime`` is not a ``date``."""
    tp = _base_type(tp)
    if not isinstance(tp, type):
        return False
    if issubclass(tp, datetime.datetime):
        return datetime.datetime in types
    return issubclass(tp, types)


class Transform:
    """Base class of transforms: a named function of a field's value that a field path applies after the field, before
    the lookup (``joined__year__gte``) or at the end of a path in ``F()``, ``values()`` and ``order_by()``.

    A transform is named in field paths, not instantiated. Its construct name is ``transform.<lookup_name>``. A
    transform of the application's own declares its SQL as a `Func` does: ``function``, ``template`` with
    ``%(expressions)s`` (the transformed value) or ``templates``, one per database vendor; drivers that render SQL run
    it. The bundled transforms have no SQL here: drivers apply them by name.
    """

    lookup_name: ClassVar[str | None] = None
    construct: ClassVar[str | None] = None
    function: ClassVar[str | None] = None
    """The SQL function's name."""
    template: ClassVar[str | None] = None
    """The SQL template for every vendor without one in ``templates``."""
    templates: ClassVar[Mapping[str, str] | None] = None
    """SQL templates by database vendor."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if isinstance(cls.lookup_name, str) and cls.lookup_name and "construct" not in cls.__dict__:
            cls.construct = f"transform.{cls.lookup_name}"
        _check_sql(cls)

    @classmethod
    def sql_template(cls, vendor: str) -> str | None:
        """The SQL template on databases of ``vendor``: ``templates[vendor]``, else ``template``, else
        ``%(function)s(%(expressions)s)`` when the class names a ``function``; ``None`` without one."""
        return _sql_template(cls, vendor, _DEFAULT_TEMPLATE)

    @classmethod
    def applies_to(cls, field_type: Any) -> bool:  # noqa: ARG003 - any field by default
        """Whether the transform applies to a value of ``field_type`` (without ``Annotated`` and ``Optional``; ``None``
        when unknown); ``True`` by default."""
        return True

    @classmethod
    def output_type(cls, field_type: Any) -> Any:  # noqa: ARG003 - unknown by default
        """The type of the transformed value of a ``field_type`` value; ``None`` (unknown) by default."""
        return None


def register_transform(cls: type[Transform]) -> type[Transform]:
    """Register a `Transform` subclass under its ``lookup_name``; usable as a class decorator.

    Raises ``ValueError`` when the name is not an identifier without ``__``, or another class is registered under it
    as a transform or as a lookup.
    """
    name = cls.lookup_name
    if not isinstance(name, str) or not name.isidentifier() or "__" in name:
        raise ValueError(f"{cls.__name__}.lookup_name {name!r} is not an identifier without '__'")
    if _TRANSFORMS.get(name, cls) is not cls:
        raise ValueError(f"transform {name!r} is already registered as {_TRANSFORMS[name].__name__}")
    if name in _LOOKUPS:
        raise ValueError(f"{name!r} is registered as the lookup {_LOOKUPS[name].__name__}")
    _TRANSFORMS[name] = cls
    return cls


class _DatePart(Transform):
    """A part of a date or datetime, as an integer."""

    @classmethod
    def applies_to(cls, field_type: Any) -> bool:
        return field_type is None or _is_type(field_type, datetime.date, datetime.datetime)

    @classmethod
    def output_type(cls, field_type: Any) -> Any:  # noqa: ARG003 - an integer for any field
        return int


class _TimePart(Transform):
    """A part of a time or datetime, as an integer."""

    @classmethod
    def applies_to(cls, field_type: Any) -> bool:
        return field_type is None or _is_type(field_type, datetime.time, datetime.datetime)

    @classmethod
    def output_type(cls, field_type: Any) -> Any:  # noqa: ARG003 - an integer for any field
        return int


@register_transform
class Year(_DatePart):
    """The year."""

    lookup_name = "year"


@register_transform
class IsoYear(_DatePart):
    """The ISO 8601 week-numbering year."""

    lookup_name = "iso_year"


@register_transform
class Quarter(_DatePart):
    """The quarter of the year, 1 to 4."""

    lookup_name = "quarter"


@register_transform
class Month(_DatePart):
    """The month, 1 to 12."""

    lookup_name = "month"


@register_transform
class Week(_DatePart):
    """The ISO 8601 week number, 1 to 53."""

    lookup_name = "week"


@register_transform
class WeekDay(_DatePart):
    """The day of the week, 1 (Sunday) to 7 (Saturday)."""

    lookup_name = "week_day"


@register_transform
class IsoWeekDay(_DatePart):
    """The ISO 8601 day of the week, 1 (Monday) to 7 (Sunday)."""

    lookup_name = "iso_week_day"


@register_transform
class Day(_DatePart):
    """The day of the month."""

    lookup_name = "day"


@register_transform
class Hour(_TimePart):
    """The hour, 0 to 23."""

    lookup_name = "hour"


@register_transform
class Minute(_TimePart):
    """The minute, 0 to 59."""

    lookup_name = "minute"


@register_transform
class Second(_TimePart):
    """The second, 0 to 59."""

    lookup_name = "second"


@register_transform
class Date(Transform):
    """The date of a datetime in the current time zone."""

    lookup_name = "date"

    @classmethod
    def applies_to(cls, field_type: Any) -> bool:
        return field_type is None or _is_type(field_type, datetime.datetime)

    @classmethod
    def output_type(cls, field_type: Any) -> Any:  # noqa: ARG003 - a date for any field
        return datetime.date


@register_transform
class Time(Transform):
    """The time of a datetime in the current time zone."""

    lookup_name = "time"

    @classmethod
    def applies_to(cls, field_type: Any) -> bool:
        return field_type is None or _is_type(field_type, datetime.datetime)

    @classmethod
    def output_type(cls, field_type: Any) -> Any:  # noqa: ARG003 - a time for any field
        return datetime.time


def _container_kind(tp: Any) -> str | None:
    """``"record"`` for a pydantic model or ``TypedDict``, ``"container"`` for a list, tuple, set, dict or ``Any``,
    else ``None``; ``Optional`` and ``Annotated`` are unwrapped."""
    origin = get_origin(tp)
    if origin is Annotated:
        return _container_kind(get_args(tp)[0])
    if origin in (Union, UnionType):
        kinds = {_container_kind(arg) for arg in get_args(tp) if arg is not type(None)}
        return "record" if "record" in kinds else "container" if "container" in kinds else None
    tp = origin or tp
    if is_typeddict(tp) or (isinstance(tp, type) and hasattr(tp, "model_fields")):
        return "record"
    return "container" if tp is Any or tp in (list, tuple, set, frozenset, dict) else None


def _item_type(tp: Any) -> Any:
    """The item type of a list, tuple (``tuple[X, ...]``), set or frozenset type, else ``None``; ``Optional`` and
    ``Annotated`` are unwrapped."""
    origin = get_origin(tp)
    if origin is Annotated:
        return _item_type(get_args(tp)[0])
    if origin in (Union, UnionType):
        found = [_item_type(arg) for arg in get_args(tp) if arg is not type(None)]
        return found[0] if len(found) == 1 else None
    args = get_args(tp)
    if origin in (list, set, frozenset) and len(args) == 1:
        return args[0]
    if origin is tuple and len(args) == 2 and args[1] is Ellipsis:
        return args[0]
    return None


class _PatternLookup(Lookup):
    """A text pattern match: the database compares the field as text, so the right-hand side is a string."""

    @classmethod
    def rhs_type(cls, field_type: Any) -> Any:
        return None if field_type is None else str


@register_lookup
class Exact(Lookup):
    """Equal to ``rhs``. ``None`` matches SQL NULL at a field or an expression and JSON ``null`` at a key inside a JSON
    field.

    ``implicit`` is true when `build_lookup` built the lookup from a key without a lookup name; an error message then
    calls an unknown last segment of the path a lookup. It is not part of the node's value and is not encoded."""

    lookup_name = "exact"
    rhs_accepts_none = True
    on_relations = True
    implicit = False


@register_lookup
class IExact(_PatternLookup):
    """Equal to ``rhs``, ignoring case. ``None`` matches as for `Exact`."""

    lookup_name = "iexact"
    rhs_accepts_none = True


@register_lookup
class GreaterThan(Lookup):
    """Greater than ``rhs``."""

    lookup_name = "gt"
    on_relations = True


@register_lookup
class GreaterThanOrEqual(Lookup):
    """Greater than or equal to ``rhs``."""

    lookup_name = "gte"
    on_relations = True


@register_lookup
class LessThan(Lookup):
    """Less than ``rhs``."""

    lookup_name = "lt"
    on_relations = True


@register_lookup
class LessThanOrEqual(Lookup):
    """Less than or equal to ``rhs``."""

    lookup_name = "lte"
    on_relations = True


class _ValuesLookup(Lookup):
    """A lookup whose right-hand side is a tuple of values, built from an iterable."""

    rhs_is_sequence = True

    def _values(self, value: Any, accepted: str) -> tuple[Any, ...]:
        """The values of the iterable ``value`` as a tuple without ``None``, a set sorted when its values are
        orderable, each prepared as a single right-hand side. ``accepted`` names what else the lookup takes, for the
        ``TypeError`` raised for a string, bytes, a mapping, a node or a non-iterable."""
        if isinstance(value, str | bytes | Mapping | Node) or not isinstance(value, Iterable):
            raise TypeError(f"the {self.lookup_name} lookup takes {accepted}, not {type(value).__name__}")
        values = [v for v in value if v is not None]
        if isinstance(value, Set):
            try:
                values.sort()
            except TypeError:
                pass
        return tuple(self._prepare_item(v) for v in values)

    def _prepare_item(self, value: Any) -> Any:
        if isinstance(value, Expression) and value.conditional:
            raise TypeError(f"{type(value).__name__} is not a value for the {self.lookup_name} lookup")
        return Lookup.prepare_rhs(self, value)


@register_lookup
class In(_ValuesLookup):
    """One of the values in ``rhs``: an iterable of values or a query selecting one column.

    The values are stored as a tuple without ``None`` (which never matches); a set is sorted when its values are
    orderable. Each value is prepared as a single right-hand side: a query becomes a `Subquery`, and a condition or
    another non-expression node raises ``TypeError``.
    """

    lookup_name = "in"
    on_relations = True

    def prepare_rhs(self, value: Any) -> Any:
        if isinstance(value, Subquery) and not getattr(value, "conditional", False):
            return value
        if as_query(value) is not None:
            return Subquery(value)
        return self._values(value, "an iterable of values or a query")


@register_lookup
class Range(Lookup):
    """Between the two values of ``rhs``, inclusive."""

    lookup_name = "range"
    rhs_is_sequence = True

    def prepare_rhs(self, value: Any) -> Any:
        if isinstance(value, str | bytes | Mapping | Node) or not isinstance(value, Iterable):
            raise TypeError(f"the range lookup takes two values, not {type(value).__name__}")
        bounds = tuple(value)
        if len(bounds) != 2:
            raise ValueError(f"the range lookup takes two values, got {len(bounds)}")
        return tuple(Lookup.prepare_rhs(self, bound) for bound in bounds)

    def _check_decoded(self) -> None:
        super()._check_decoded()
        if not isinstance(self.rhs, tuple) or len(self.rhs) != 2:
            raise ValueError("the range lookup takes two values")


@register_lookup
class IsNull(Lookup):
    """NULL when ``rhs`` is ``True``, not NULL when it is ``False``."""

    lookup_name = "isnull"
    on_relations = True

    @classmethod
    def rhs_type(cls, field_type: Any) -> Any:  # noqa: ARG003 - the right-hand side is a bool for any field
        return bool

    def prepare_rhs(self, value: Any) -> Any:
        if not isinstance(value, bool):
            raise ValueError(f"the isnull lookup takes True or False, not {value!r}")
        return value

    def _check_decoded(self) -> None:
        super()._check_decoded()
        self.prepare_rhs(self.rhs)


@register_lookup
class StartsWith(_PatternLookup):
    """Starts with ``rhs``."""

    lookup_name = "startswith"


@register_lookup
class IStartsWith(_PatternLookup):
    """Starts with ``rhs``, ignoring case."""

    lookup_name = "istartswith"


@register_lookup
class EndsWith(_PatternLookup):
    """Ends with ``rhs``."""

    lookup_name = "endswith"


@register_lookup
class IEndsWith(_PatternLookup):
    """Ends with ``rhs``, ignoring case."""

    lookup_name = "iendswith"


@register_lookup
class Contains(_PatternLookup):
    """Contains ``rhs``: a substring; for a container field a value of the field's type, for a record field (a
    pydantic model or ``TypedDict``) a partial record, typed by its own values."""

    lookup_name = "contains"

    @classmethod
    def rhs_type(cls, field_type: Any) -> Any:
        kind = _container_kind(field_type)
        if kind == "record":
            return None
        return field_type if kind == "container" else super().rhs_type(field_type)


@register_lookup
class IContains(_PatternLookup):
    """Contains ``rhs``, ignoring case."""

    lookup_name = "icontains"


@register_lookup
class Overlap(_ValuesLookup):
    """Has at least one item in common with ``rhs``: an iterable of values, a query selecting one column, or an
    expression whose value is an array.

    The values are stored as a tuple without ``None`` (which never matches); a set is sorted when its values are
    orderable. Each value is prepared as a single right-hand side, as for `In`.
    """

    lookup_name = "overlap"

    def prepare_rhs(self, value: Any) -> Any:
        if isinstance(value, Expression) and not value.conditional:
            return value
        if as_query(value) is not None:
            return Subquery(value)
        return self._values(value, "an iterable of values, a query or an expression")

    @classmethod
    def rhs_type(cls, field_type: Any) -> Any:
        return _item_type(field_type)

    @classmethod
    def applies_to(cls, field_type: Any, keys: tuple[str, ...]) -> bool:
        """An array field or a field of unknown type; never a key inside a JSON field."""
        return not keys and (field_type is None or _item_type(field_type) is not None)


@register_lookup
class ContainedBy(Lookup):
    """Contained in ``rhs``: for a JSON field a value whose top-level keys and items all occur in ``rhs``, for an
    array field an array whose items all occur in ``rhs``."""

    lookup_name = "contained_by"

    @classmethod
    def rhs_type(cls, field_type: Any) -> Any:
        return field_type if _container_kind(field_type) == "container" else None

    @classmethod
    def applies_to(cls, field_type: Any, keys: tuple[str, ...]) -> bool:
        """A JSON, record or array field, a key inside a JSON field, or a field of unknown type."""
        return bool(keys) or field_type is None or _container_kind(field_type) is not None


def _json_object(field_type: Any, keys: tuple[str, ...]) -> bool:
    """Whether a value of ``field_type`` at ``keys`` may be a JSON object: a key inside a JSON field, a field of
    unknown type, ``Any``, a dict or a record."""
    if keys or field_type is None:
        return True
    base = _base_type(field_type)
    return base is Any or base is dict or get_origin(base) is dict or _container_kind(field_type) == "record"


@register_lookup
class HasKey(Lookup):
    """A JSON object with the key ``rhs``."""

    lookup_name = "has_key"

    @classmethod
    def rhs_type(cls, field_type: Any) -> Any:  # noqa: ARG003 - a key name for any field
        return str

    @classmethod
    def applies_to(cls, field_type: Any, keys: tuple[str, ...]) -> bool:
        """A JSON object (`_json_object`)."""
        return _json_object(field_type, keys)

    def prepare_rhs(self, value: Any) -> Any:
        if not isinstance(value, str):
            raise TypeError(f"the has_key lookup takes a key name, not {type(value).__name__}")
        return value

    def _check_decoded(self) -> None:
        super()._check_decoded()
        self.prepare_rhs(self.rhs)


class _KeysLookup(Lookup):
    rhs_is_sequence = True

    @classmethod
    def rhs_type(cls, field_type: Any) -> Any:  # noqa: ARG003 - key names for any field
        return str

    @classmethod
    def applies_to(cls, field_type: Any, keys: tuple[str, ...]) -> bool:
        """A JSON object (`_json_object`)."""
        return _json_object(field_type, keys)

    def prepare_rhs(self, value: Any) -> Any:
        if isinstance(value, str | bytes | Mapping | Node) or not isinstance(value, Iterable):
            raise TypeError(f"the {self.lookup_name} lookup takes an iterable of key names, not {type(value).__name__}")
        names = tuple(value)
        if not all(isinstance(name, str) for name in names):
            raise TypeError(f"the {self.lookup_name} lookup takes key names, which are strings")
        return names

    def _check_decoded(self) -> None:
        super()._check_decoded()
        self.prepare_rhs(self.rhs)


@register_lookup
class HasKeys(_KeysLookup):
    """A JSON object with every key of ``rhs``, an iterable of key names."""

    lookup_name = "has_keys"


@register_lookup
class HasAnyKeys(_KeysLookup):
    """A JSON object with at least one key of ``rhs``, an iterable of key names."""

    lookup_name = "has_any_keys"


class _RegexLookup(Lookup):
    @classmethod
    def rhs_type(cls, field_type: Any) -> Any:  # noqa: ARG003 - the pattern is a string for any field
        return str

    def prepare_rhs(self, value: Any) -> Any:
        if not isinstance(value, str | Expression):
            raise TypeError(f"the {self.lookup_name} lookup takes a pattern string, not {type(value).__name__}")
        return super().prepare_rhs(value)

    def _check_decoded(self) -> None:
        super()._check_decoded()
        self.prepare_rhs(self.rhs)


@register_lookup
class Regex(_RegexLookup):
    """Matches the regular expression ``rhs`` (the database's syntax)."""

    lookup_name = "regex"


@register_lookup
class IRegex(_RegexLookup):
    """Matches the regular expression ``rhs``, ignoring case."""

    lookup_name = "iregex"


def split_lookup(key: str) -> tuple[str, str]:
    """Split a lookup key into its field path and lookup name: ``"club__name__icontains"`` →
    ``("club__name", "icontains")``, ``"club__name"`` → ``("club__name", "exact")``."""
    path, _, last = F(key).name.rpartition("__")
    if path and last in _LOOKUPS:
        return path, last
    return key, "exact"


def build_lookup(key: str, value: Any) -> Lookup:
    """The lookup for ``filter(key=value)``; an `Exact` from a key without a lookup name is ``implicit``."""
    path, name = split_lookup(key)
    lookup = _LOOKUPS[name](path, value)
    if path == key and isinstance(lookup, Exact):
        lookup._init(implicit=True)
    return lookup


@dataclass(frozen=True)
class ResolvedLookup:
    """A lookup key resolved against a model's schema (`resolve_lookup`).

    ``path`` holds the segments of the key's field path; ``lookup`` is the lookup class. ``model`` and ``field`` name
    the model and the field or relation the path ends at (``pk`` resolved to the primary key's name); both are ``None``
    when the path ends at an annotation or passes a model the schema source does not know. ``keys`` holds the segments
    after a JSON or record field: keys inside its value. ``transforms`` holds the names of the transforms applied after
    the field or annotation, in order. ``field_type`` is the type of the compared values: the field's type, the
    target's primary key type for a relation, an annotation's given type, the output type of the last transform, or
    ``None`` when unknown (always after ``keys``).
    """

    key: str
    path: tuple[str, ...]
    lookup: type[Lookup]
    model: str | None
    field: str | None
    field_type: Any = None
    keys: tuple[str, ...] = ()
    transforms: tuple[str, ...] = ()

    @property
    def value_type(self) -> Any:
        """The type of a right-hand side value (of each value for ``in``, ``range`` and ``overlap``):
        ``lookup.rhs_type(field_type)``."""
        return self.lookup.rhs_type(self.field_type)


def _suggestion(name: str, choices: Iterable[str]) -> str:
    close = difflib.get_close_matches(name, sorted(choices), n=1)
    return f"; perhaps you meant {close[0]!r}" if close else ""


@dataclass(frozen=True)
class _PathEnd:
    model: str | None = None
    field: str | None = None
    field_type: Any = None
    keys: tuple[str, ...] = ()
    relation: bool = False
    transforms: tuple[str, ...] = ()


def _apply_transforms(
    target: str, field_type: Any, names: tuple[str, ...], key: str, model: str | None, *, lookup_hint: bool
) -> tuple[tuple[str, ...], Any]:
    """The transforms ``names`` applied to a value of ``field_type`` at ``target`` (as error messages name it), and
    the type of the result; each transform gets the type without ``Annotated`` and ``Optional``. Raises
    `adorm.FieldError` for a name that is no transform applying to the value there; ``lookup_hint``: the last name
    may also have been meant as a lookup, as the message then calls it."""
    for index, name in enumerate(names):
        transform = _TRANSFORMS.get(name)
        field_type = _base_type(field_type)
        if transform is not None and transform.applies_to(field_type):
            field_type = transform.output_type(field_type)
            target = f"{target}__{name}"
            continue
        choices = [n for n, cls in _TRANSFORMS.items() if cls.applies_to(field_type)]
        kind = "transform"
        if lookup_hint and index == len(names) - 1:
            choices += [n for n, cls in _LOOKUPS.items() if cls.applies_to(field_type, ())]
            kind = "transform" if transform is not None else "lookup"
        message = f"unsupported {kind} {name!r} for {target}" + _suggestion(name, choices)
        raise FieldError(message, model=model, key=key)
    return names, field_type


def _walk(source: SchemaSource, label: str, segments: tuple[str, ...], key: str, *, lookup_hint: bool) -> _PathEnd:
    """Where the field path ``segments`` from the model ``label`` ends. ``lookup_hint``: the last segment after a
    scalar field may also have been meant as a lookup name, which an error message reports."""
    schema = source.model_schema(label)
    for index, segment in enumerate(segments):
        if schema is None:
            return _PathEnd()
        name = schema.pk if segment == "pk" else segment
        rest = segments[index + 1 :]
        if name in schema.fields:
            field_type = schema.fields[name]
            if not rest:
                return _PathEnd(schema.label, name, field_type)
            if _container_kind(field_type) is not None:
                return _PathEnd(schema.label, name, None, rest)
            target = f"{schema.label}.{name}"
            transforms, field_type = _apply_transforms(
                target, field_type, rest, key, schema.label, lookup_hint=lookup_hint
            )
            return _PathEnd(schema.label, name, field_type, transforms=transforms)
        target = schema.relations.get(name)
        if target is None:
            choices = {"pk", *schema.fields, *schema.relations}
            message = f"{schema.label} has no field or relation {segment!r}" + _suggestion(segment, choices)
            raise FieldError(message, model=schema.label, key=key)
        target_schema = source.model_schema(target)
        if not rest:
            field_type = None if target_schema is None else target_schema.fields[target_schema.pk]
            return _PathEnd(schema.label, name, field_type, relation=True)
        schema = target_schema
    return _PathEnd()


def _resolve(
    source: SchemaSource,
    label: str,
    path: str,
    lookup: type[Lookup],
    key: str,
    annotations: Mapping[str, Any],
    *,
    explicit: bool,
) -> ResolvedLookup:
    segments = tuple(split_field_path(path))
    if segments[0] in annotations:
        annotation_type, rest = annotations[segments[0]], segments[1:]
        field_type, keys, transforms = (annotation_type if not rest else None), rest, ()
        if (
            rest
            and _container_kind(annotation_type) is None
            and (annotation_type is not None or all(name in _TRANSFORMS for name in rest))
        ):
            target = f"the annotation {segments[0]}"
            transforms, field_type = _apply_transforms(
                target, annotation_type, rest, key, None, lookup_hint=not explicit
            )
            keys = ()
        if not lookup.applies_to(field_type, keys):
            message = f"unsupported lookup {lookup.lookup_name!r} for the annotation {path!r}"
            raise FieldError(message, key=key)
        return ResolvedLookup(key, segments, lookup, None, None, field_type, keys, transforms)
    end = _walk(source, label, segments, key, lookup_hint=not explicit)
    if end.relation and not lookup.on_relations:
        allowed = ", ".join(name for name, cls in _LOOKUPS.items() if cls.on_relations)
        message = f"unsupported lookup {lookup.lookup_name!r} for the relation {end.model}.{end.field}"
        raise FieldError(f"{message}; a relation takes {allowed}", model=end.model, key=key)
    if not end.relation and not lookup.applies_to(end.field_type, end.keys):
        target = f"{end.model}.{'__'.join((end.field, *end.transforms, *end.keys))}"
        raise FieldError(f"unsupported lookup {lookup.lookup_name!r} for {target}", model=end.model, key=key)
    return ResolvedLookup(key, segments, lookup, end.model, end.field, end.field_type, end.keys, end.transforms)


def transform_constructs(query: Query, source: SchemaSource | None = None) -> set[str]:
    """The construct names of the transforms the field paths of ``query`` apply: its ``F()`` references outside
    subqueries and its ``values()``, ``group_by``, ``distinct()``, ``only()`` and ``defer()`` paths, resolved against
    ``source`` (the model registry by default). A path that does not resolve adds none."""
    source = source or _registry()
    annotations = dict.fromkeys(name for name, _ in query.annotations)
    paths = [
        *query.select,
        *(query.group_by or ()),
        *query.distinct_fields,
        *(query.only_fields or ()),
        *query.deferred_fields,
    ]
    pending: list[Node] = [*query.filters, *(value for _, value in query.annotations), *query.ordering]
    while pending:
        node = pending.pop()
        if isinstance(node, Query | OuterRef):
            continue
        if isinstance(node, F):
            paths.append(node.name)
        pending.extend(node.get_source_expressions())
    names: set[str] = set()
    for path in paths:
        if "__" not in path:
            continue
        try:
            resolved = _resolve(source, query.model, path, Exact, path, annotations, explicit=True)
        except FieldError:
            continue
        names.update(_TRANSFORMS[name].construct for name in resolved.transforms if _TRANSFORMS[name].construct)
    return names


def _target(target: Any, annotations: Iterable[str] | Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    """The model label of ``target`` and the annotation types: those of ``annotations``, else the annotation names of
    a query's ``target`` (of unknown type)."""
    types = annotations if isinstance(annotations, Mapping) else dict.fromkeys(annotations)
    query = as_query(target)
    if query is not None:
        return query.model, {**dict.fromkeys(name for name, _ in query.annotations), **types}
    meta = getattr(target, "_meta", None)
    if isinstance(target, type) and meta is not None:
        return meta.label, types
    if isinstance(target, str):
        return check_model_label(target), types
    raise TypeError(f"expected a model class, a model label or a query, not {target!r}")


def _registry() -> SchemaSource:
    from adorm.models.registry import registry

    return registry


def resolve_lookup(
    target: Any,
    key: str,
    *,
    annotations: Iterable[str] | Mapping[str, Any] = (),
    source: SchemaSource | None = None,
) -> ResolvedLookup:
    """Resolve the lookup key ``key`` (as ``filter()`` takes it) against ``target``: a model class, a model label, or a
    `Query` or `QuerySet`, whose annotation names a path may also start with.

    ``annotations`` holds further annotation names, or maps them to their types. ``source`` is the `SchemaSource` the
    models are resolved in, the model registry by default. The field path is relations, then a field. After a JSON or
    record field (a dict, list, ``Any``, a pydantic model or a ``TypedDict``) the following segments are keys inside
    its value; after another field they are transforms (`TRANSFORMS`), each applying to the value the previous one
    gives (``joined__date__year``). An annotation is followed by transforms, unless its type is a JSON or record type,
    or unknown and a following segment is no transform name: then by keys. ``pk`` stands for the primary key.

    Raises `adorm.FieldError` for a segment that is neither a field nor a relation, and for a segment after a field
    that is no transform applying there (``first_name__icontain``, an unknown lookup name, ``birth_date__hour`` on a
    date). A path that ends at a relation takes only the lookups whose ``on_relations`` is true (``exact``, ``in``,
    ``gt``, ``gte``, ``lt``, ``lte``, ``isnull``); another raises `adorm.FieldError` too, as does a lookup whose
    ``applies_to()`` refuses the field's type or the keys (``overlap`` on a field that is not an array). A model
    ``source`` does not know ends the check: the rest of the path is not resolved. A key that is not a lookup key
    raises ``ValueError``.
    """
    label, types = _target(target, annotations)
    path, name = split_lookup(key)
    explicit = name != "exact" or key.endswith("__exact")
    return _resolve(source or _registry(), label, path, _LOOKUPS[name], key, types, explicit=explicit)


def check_lookups(
    target: Any,
    condition: Any,
    *,
    annotations: Iterable[str] | Mapping[str, Any] = (),
    source: SchemaSource | None = None,
) -> None:
    """Resolve every lookup and field reference of ``condition`` (a `Q`, a lookup or another expression) against
    ``target`` as `resolve_lookup` does; raises `adorm.FieldError` for the first that does not resolve.

    An `OuterRef` and the nodes of a query inside the condition (a `Subquery`, `Exists`) refer to other models and are
    not checked.
    """
    label, types = _target(target, annotations)
    source = source or _registry()
    if not isinstance(condition, Node):
        raise TypeError(f"expected a condition, not {condition!r}")
    pending: list[Node] = [condition]
    while pending:
        node = pending.pop()
        if isinstance(node, Query | OuterRef):
            continue
        if isinstance(node, F):
            _resolve(source, label, node.name, Exact, node.name, types, explicit=True)
            continue
        sources = list(node.get_source_expressions())
        if isinstance(node, Lookup) and type(node.lhs) is F:
            key = f"{node.lhs.name}__{node.lookup_name}"
            explicit = not (isinstance(node, Exact) and node.implicit)
            _resolve(source, label, node.lhs.name, type(node), key, types, explicit=explicit)
            sources = sources[1:]
        pending.extend(reversed(sources))
