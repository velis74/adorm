"""Lookups: the conditions of ``filter(field__lookup=value)`` and their parsing.

A lookup key is a field path followed by an optional lookup name, joined with ``__``: ``"name"``,
``"name__icontains"``, ``"club__owner__name__startswith"``. The last segment is the lookup name when it is a
registered lookup and the key has more than one segment; otherwise the whole key is the field path and the lookup
is ``exact``. A field whose name equals a lookup name is filtered with an explicit lookup (``"range__exact"``).
`resolve_lookup` and `check_lookups` resolve keys and conditions against model schemas.
"""

from __future__ import annotations

import difflib

from collections.abc import Iterable, Mapping, Set
from dataclasses import dataclass
from types import MappingProxyType, UnionType
from typing import Annotated, Any, ClassVar, get_args, get_origin, is_typeddict, TYPE_CHECKING, Union

from adorm.exceptions import FieldError
from adorm.query.expressions import _expression, Expression, F, freeze_value, Node, OuterRef, split_field_path, Subquery
from adorm.query.query import as_query, check_model_label, Query

if TYPE_CHECKING:
    from adorm.schema import SchemaSource

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
    "ResolvedLookup",
    "StartsWith",
    "build_lookup",
    "check_lookups",
    "register_lookup",
    "resolve_lookup",
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
    on_relations: ClassVar[bool] = False
    """Whether the lookup applies to a relation (compared by the target's primary key)."""
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
    on_relations = True


@register_lookup
class IExact(_PatternLookup):
    """Equal to ``rhs``, ignoring case."""

    lookup_name = "iexact"


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


@register_lookup
class In(Lookup):
    """One of the values in ``rhs``: an iterable of values or a query selecting one column.

    The values are stored as a tuple without ``None`` (which never matches); a set is sorted when its values are
    orderable. Each value is prepared as a single right-hand side: a query becomes a `Subquery`, and a condition or
    another non-expression node raises ``TypeError``.
    """

    lookup_name = "in"
    on_relations = True
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


@dataclass(frozen=True)
class ResolvedLookup:
    """A lookup key resolved against a model's schema (`resolve_lookup`).

    ``path`` holds the segments of the key's field path; ``lookup`` is the lookup class. ``model`` and ``field`` name
    the model and the field or relation the path ends at (``pk`` resolved to the primary key's name); both are ``None``
    when the path ends at an annotation or passes a model the schema source does not know. ``keys`` holds the segments
    after a JSON or record field: keys inside its value. ``field_type`` is the type of the compared values: the
    field's type, the target's primary key type for a relation, an annotation's given type, or ``None`` when unknown
    (always after ``keys``).
    """

    key: str
    path: tuple[str, ...]
    lookup: type[Lookup]
    model: str | None
    field: str | None
    field_type: Any = None
    keys: tuple[str, ...] = ()

    @property
    def value_type(self) -> Any:
        """The type of a right-hand side value (of each value for ``in`` and ``range``):
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


def _walk(source: SchemaSource, label: str, segments: tuple[str, ...], key: str, *, lookup_hint: bool) -> _PathEnd:
    """Where the field path ``segments`` from the model ``label`` ends. ``lookup_hint``: a single segment after a
    scalar field is reported as an unknown lookup name."""
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
            if len(rest) == 1 and lookup_hint:
                message = f"unsupported lookup {rest[0]!r} for {schema.label}.{name}" + _suggestion(rest[0], _LOOKUPS)
            else:
                following = "__".join(rest)
                message = f"{schema.label}.{name} is neither a relation nor a JSON field; {following!r} cannot follow"
            raise FieldError(message, model=schema.label, key=key)
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
        field_type = annotations[segments[0]] if len(segments) == 1 else None
        return ResolvedLookup(key, segments, lookup, None, None, field_type, segments[1:])
    end = _walk(source, label, segments, key, lookup_hint=not explicit)
    if end.relation and not lookup.on_relations:
        allowed = ", ".join(name for name, cls in _LOOKUPS.items() if cls.on_relations)
        message = f"unsupported lookup {lookup.lookup_name!r} for the relation {end.model}.{end.field}"
        raise FieldError(f"{message}; a relation takes {allowed}", model=end.model, key=key)
    return ResolvedLookup(key, segments, lookup, end.model, end.field, end.field_type, end.keys)


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
    models are resolved in, the model registry by default. Every segment but the last of the field path is a relation,
    until a JSON or record field (a dict, list, ``Any``, a pydantic model or a ``TypedDict``), whose following segments
    are keys inside its value. ``pk`` stands for the primary key.

    Raises `adorm.FieldError` for a segment that is neither a field nor a relation, and for segments after a field
    that is neither a relation nor a JSON field (``first_name__icontain``, an unknown lookup name). A path that ends at
    a relation takes only the lookups whose ``on_relations`` is true (``exact``, ``in``, ``gt``, ``gte``, ``lt``,
    ``lte``, ``isnull``); another raises `adorm.FieldError` too. A model ``source``
    does not know ends the check: the rest of the path is not resolved. A key that is not a lookup key raises
    ``ValueError``.
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
            explicit = type(node) not in (Exact, IsNull)
            _resolve(source, label, node.lhs.name, type(node), key, types, explicit=explicit)
            sources = sources[1:]
        pending.extend(reversed(sources))
