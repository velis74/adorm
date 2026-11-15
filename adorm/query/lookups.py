"""Lookups: the conditions of ``filter(field__lookup=value)`` and their parsing.

A lookup key is a field path followed by an optional lookup name, joined with ``__``: ``"name"``,
``"name__icontains"``, ``"club__owner__name__startswith"``. The last segment is the lookup name when it is a
registered lookup and the key has more than one segment; otherwise the whole key is the field path and the lookup
is ``exact``. A field whose name equals a lookup name is filtered with an explicit lookup (``"range__exact"``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Set
from types import MappingProxyType, UnionType
from typing import Annotated, Any, ClassVar, get_args, get_origin, is_typeddict, Union

from adorm.query.expressions import _expression, Expression, F, freeze_value, Node, Subquery
from adorm.query.query import as_query

__all__ = [
    "LOOKUPS",
    "Contains",
    "EndsWith",
    "Exact",
    "GreaterThan",
    "GreaterThanOrEqual",
    "IContains",
    "IEndsWith",
    "IExact",
    "In",
    "IRegex",
    "IsNull",
    "IStartsWith",
    "LessThan",
    "LessThanOrEqual",
    "Lookup",
    "Range",
    "Regex",
    "StartsWith",
    "build_lookup",
    "register_lookup",
    "split_lookup",
]

_LOOKUPS: dict[str, type[Lookup]] = {}
LOOKUPS: Mapping[str, type[Lookup]] = MappingProxyType(_LOOKUPS)
"""Registered lookups by name (read-only view)."""


class Lookup(Expression):
    """Base class of lookups: a condition on ``lhs`` with the right-hand side ``rhs``.

    ``lhs`` is an expression; a string is a field path. ``rhs`` is a plain value or an expression; a query (a
    `Query` or an object with a ``query`` attribute that is one) becomes a `Subquery`. A plain value is copied
    (`freeze_value`). ``None`` is rejected: a NULL test is `IsNull`. The construct name is ``lookup.<lookup_name>``.
    """

    lookup_name: ClassVar[str | None] = None
    rhs_is_sequence: ClassVar[bool] = False
    conditional = True
    output_type = bool
    _fields = ("lhs", "rhs")
    lhs: Expression
    rhs: Any

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.lookup_name is not None and "construct" not in cls.__dict__:
            cls.construct = f"lookup.{cls.lookup_name}"

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
        if value is None:
            raise ValueError(f"None is not a value for the {self.lookup_name} lookup; use isnull")
        return freeze_value(value)

    @classmethod
    def rhs_type(cls, field_type: Any) -> Any:
        """The type of a right-hand side value (of each value when ``rhs_is_sequence``) for a field of
        ``field_type``; ``None`` when unknown. Defaults to ``field_type``."""
        return field_type

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
            if value is None:
                raise ValueError(f"None is not a value for the {self.lookup_name} lookup")

    def get_source_expressions(self) -> tuple[Node, ...]:
        rhs = self.rhs if isinstance(self.rhs, tuple) else (self.rhs,)
        return (self.lhs, *(v for v in rhs if isinstance(v, Expression)))

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.lhs!r}, {self.rhs!r})"


def register_lookup(cls: type[Lookup]) -> type[Lookup]:
    """Register a `Lookup` subclass under its ``lookup_name``; usable as a class decorator.

    Raises ``ValueError`` when another class is registered under that name.
    """
    name = cls.lookup_name
    if not isinstance(name, str) or not name.isidentifier() or "__" in name:
        raise ValueError(f"{cls.__name__} needs a lookup_name that is an identifier without '__'")
    if _LOOKUPS.get(name, cls) is not cls:
        raise ValueError(f"lookup {name!r} is already registered as {_LOOKUPS[name].__name__}")
    _LOOKUPS[name] = cls
    return cls


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


class _PatternLookup(Lookup):
    """A text pattern match: the database compares the field as text, so the right-hand side is a string."""

    @classmethod
    def rhs_type(cls, field_type: Any) -> Any:
        return None if field_type is None else str


@register_lookup
class Exact(Lookup):
    """Equal to ``rhs``."""

    lookup_name = "exact"


@register_lookup
class IExact(_PatternLookup):
    """Equal to ``rhs``, ignoring case."""

    lookup_name = "iexact"


@register_lookup
class GreaterThan(Lookup):
    """Greater than ``rhs``."""

    lookup_name = "gt"


@register_lookup
class GreaterThanOrEqual(Lookup):
    """Greater than or equal to ``rhs``."""

    lookup_name = "gte"


@register_lookup
class LessThan(Lookup):
    """Less than ``rhs``."""

    lookup_name = "lt"


@register_lookup
class LessThanOrEqual(Lookup):
    """Less than or equal to ``rhs``."""

    lookup_name = "lte"


@register_lookup
class In(Lookup):
    """One of the values in ``rhs``: an iterable of values or a query selecting one column.

    The values are stored as a tuple without ``None`` (which never matches); a set is sorted when its values are
    orderable. Each value is prepared as a single right-hand side: a query becomes a `Subquery`, and a condition or
    another non-expression node raises ``TypeError``.
    """

    lookup_name = "in"
    rhs_is_sequence = True

    def prepare_rhs(self, value: Any) -> Any:
        if isinstance(value, Subquery) and not getattr(value, "conditional", False):
            return value
        if as_query(value) is not None:
            return Subquery(value)
        if isinstance(value, str | bytes | Mapping | Node) or not isinstance(value, Iterable):
            raise TypeError(f"the in lookup takes an iterable of values or a query, not {type(value).__name__}")
        values = [v for v in value if v is not None]
        if isinstance(value, Set):
            try:
                values.sort()
            except TypeError:
                pass
        return tuple(self._prepare_item(v) for v in values)

    def _prepare_item(self, value: Any) -> Any:
        if isinstance(value, Expression) and value.conditional:
            raise TypeError(f"{type(value).__name__} is not a value for the in lookup")
        return Lookup.prepare_rhs(self, value)


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
    """The lookup for ``filter(key=value)``. ``exact`` and ``iexact`` with ``None`` become ``isnull=True``."""
    path, name = split_lookup(key)
    if value is None and name in ("exact", "iexact"):
        return IsNull(path, True)
    return _LOOKUPS[name](path, value)
