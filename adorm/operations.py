"""Operations: the description of every read and write a driver executes.

An operation is an immutable dataclass compared by value. ``kind`` names it for `Capabilities` and
``constructs()`` names the constructs it uses. ``to_dict()`` and ``from_dict()`` convert it to and from JSON-safe
data, with values encoded by the types a schema gives them (`adorm.codec`). Building an operation does no I/O.
"""

from __future__ import annotations

import re
import types

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, fields, KW_ONLY
from functools import cached_property
from types import MappingProxyType
from typing import Annotated, Any, ClassVar, get_args, get_origin, Self, Union

from adorm.codec import Decoder, Encoder
from adorm.exceptions import NotSupportedError
from adorm.query.expressions import Expression, F, freeze_value, make_hashable, Node, OuterRef
from adorm.query.lookups import build_lookup, LOOKUPS, split_lookup
from adorm.query.query import check_model_label, Query, TenantSchema
from adorm.schema import SchemaSource

__all__ = [
    "OPERATIONS",
    "Aggregate",
    "BulkCreate",
    "Call",
    "CheckTable",
    "Count",
    "Create",
    "CreateFromParent",
    "Delete",
    "DeleteInstance",
    "Exists",
    "First",
    "FullClean",
    "Get",
    "GetOrCreate",
    "InBulk",
    "Last",
    "ModelRow",
    "Operation",
    "Save",
    "Select",
    "Update",
    "UpdateOrCreate",
]

_OPERATIONS: dict[str, type[Operation]] = {}
OPERATIONS: Mapping[str, type[Operation]] = MappingProxyType(_OPERATIONS)
"""Operation classes by kind (read-only view)."""

FieldValues = Mapping[str, Any] | Iterable[tuple[str, Any]]


def _operand(value: Any, name: str) -> Any:
    if isinstance(value, Expression):
        if value.contains_aggregate:
            raise TypeError(f"the value of {name!r} cannot contain an aggregate")
        return value
    if isinstance(value, Node):
        raise TypeError(f"the value of {name!r} is not an expression: {type(value).__name__}")
    return freeze_value(value)


def _refers_to_fields(node: Node, depth: int = 0) -> bool:
    """Whether ``node`` refers to a field of the written row: an `F` outside any query, or an `OuterRef` in a
    subquery of the value itself."""
    if isinstance(node, OuterRef):
        return depth <= node.levels
    if isinstance(node, F):
        return depth == 0
    if isinstance(node, Query) and node.is_combined:
        # The combined queries are at the query's own level; its ordering names the combined rows' columns.
        return any(_refers_to_fields(part, depth) for part in node.combined_queries)
    inner = depth + 1 if isinstance(node, Query) else depth
    return any(_refers_to_fields(child, inner) for child in node.get_source_expressions())


def _field_values(values: FieldValues, *, refs: bool = True) -> tuple[tuple[str, Any], ...]:
    """``values`` as ``(field name, value)`` pairs; ``refs`` allows expressions that refer to fields."""
    items = values.items() if isinstance(values, Mapping) else values
    pairs: list[tuple[str, Any]] = []
    for name, value in items:
        if not isinstance(name, str) or not name.isidentifier() or "__" in name:
            raise ValueError(f"{name!r} is not a field name")
        if any(n == name for n, _ in pairs):
            raise ValueError(f"the field {name!r} is given twice")
        value = _operand(value, name)
        if not refs and isinstance(value, Expression) and _refers_to_fields(value):
            raise TypeError(f"the value of {name!r} cannot refer to a field")
        pairs.append((name, value))
    return tuple(pairs)


def _check_query(query: Any) -> None:
    if not isinstance(query, Query):
        raise TypeError(f"expected a Query, not {type(query).__name__}")


def _refuse_tenant_schemas(operation: Any, what: str, nodes: Iterable[Node] | None = None) -> None:
    """Raise `adorm.NotSupportedError` when a query of ``operation`` (its parts and subqueries included), or of
    ``nodes`` when given, has a `TenantSchema`: ``what`` of such a query is not supported."""
    for node in operation.nodes() if nodes is None else nodes:
        for found in node.walk():
            if isinstance(found, Query) and found.tenant_schema is not None:
                raise NotSupportedError(f"{what} of a query in a tenant schema is not supported")


def _check_tenant_schema(value: Any) -> None:
    if value is not None and not isinstance(value, TenantSchema):
        raise TypeError(f"tenant_schema is a TenantSchema or None, not {type(value).__name__}")


def _encode_tenant_schema(encoder: Encoder, data: dict[str, Any], value: TenantSchema | None) -> dict[str, Any]:
    if value is not None:
        data["tenant_schema"] = encoder.node(value)
    return data


def _decode_tenant_schema(decoder: Decoder, data: Mapping[str, Any]) -> TenantSchema | None:
    raw = data.get("tenant_schema")
    if raw is None:
        return None
    node = decoder.node(raw)
    if not isinstance(node, TenantSchema):
        raise ValueError(f"tenant_schema is a TenantSchema, not {type(node).__name__}")
    return node


def _value_type(codec: Encoder | Decoder, name: str, extra: bool) -> Any:
    """The type of the field ``name``; with ``extra``, ``None`` for a name that is not a field."""
    if extra:
        model = codec.schema.model_schema(codec._scopes[-1].model) if codec.schema is not None else None
        if model is not None and name not in model.fields and name != "pk":
            return None
    return codec.field_type(name)


def _encode_values(encoder: Encoder, pairs: tuple[tuple[str, Any], ...], *, extra: bool = False) -> dict[str, Any]:
    return {name: encoder.operand(value, _value_type(encoder, name, extra)) for name, value in pairs}


def _decode_values(decoder: Decoder, data: Any, what: str, *, extra: bool = False) -> tuple[tuple[str, Any], ...]:
    if not isinstance(data, Mapping):
        raise ValueError(f"the {what} are a mapping of field names to values")
    return tuple((name, decoder.operand(value, _value_type(decoder, name, extra))) for name, value in data.items())


def _get(data: Mapping[str, Any], name: str) -> Any:
    if name not in data:
        raise ValueError(f"the encoded {data.get('operation')} operation has no {name!r}")
    return data[name]


def _flag(data: Mapping[str, Any], name: str) -> bool:
    value = _get(data, name)
    if not isinstance(value, bool):
        raise ValueError(f"{name} is a bool, not {value!r}")
    return value


def _decode_query(decoder: Decoder, data: Mapping[str, Any]) -> Query:
    query = decoder.node(_get(data, "query"))
    if not isinstance(query, Query):
        raise ValueError(f"the query of an operation is a Query, not {type(query).__name__}")
    return query


_SELECT_RELATED_DEPTH = 5
"""How many relations deep ``select_related()`` without arguments follows non-null foreign keys (Django's
``max_depth``)."""


def _model_class(label: str) -> Any:
    from adorm.models.registry import registry

    try:
        return registry.get(label)
    except LookupError:
        return None


def _admits_none(hint: Any) -> bool:
    if hint is None or hint is type(None) or hint is Any:
        return True
    origin = get_origin(hint)
    if origin is Annotated:
        return _admits_none(get_args(hint)[0])
    if origin is Union or origin is types.UnionType:
        return any(_admits_none(arg) for arg in get_args(hint))
    return False


def _with_concrete_tables(labels: set[str]) -> set[str]:
    """``labels`` with the concrete model of each proxy model and the parents of each multi-table inheritance child,
    as far as the model registry knows them."""
    from adorm.models.inheritance import parent_link

    pending = list(labels)
    while pending:
        model = _model_class(pending.pop())
        meta = None if model is None else model._meta
        if meta is None:
            continue
        if meta.concrete_model is not None:
            found: str | None = meta.concrete_model
        else:
            link = parent_link(model)
            found = None if link is None else link.target
        if found is not None and found not in labels:
            labels.add(found)
            pending.append(found)
    return labels


class _UnknownPathError(Exception):
    """A path segment that is neither a relation, a field nor an annotation of a registered model."""


_END = object()
"""`_step()` for a path segment that ends the path: a field, ``pk`` or an annotation."""


def _step(model: Any, name: str) -> Any:
    """The relation the path segment ``name`` names on the registered model class ``model``, `_END` for a field or
    ``pk``; raises `_UnknownPathError` for another name (a relation the target ORM knows but adorm does not, such as a
    ``GenericRelation`` or one to a model that is not generated)."""
    relations = model.__adorm_relations__
    if name == "pk" or name in model.__pydantic_fields__:
        return _END
    if name in relations:
        return relations[name]
    for adorm_name, orm_name in model._meta.orm_names.items():
        if orm_name == name:
            return relations.get(adorm_name, _END)
    raise _UnknownPathError(name)


class _Reached:
    """What `_reach()` found: ``models`` (every label reached, concrete tables included), ``unqualified`` (the labels
    whose tables are read or written without an explicit schema) and ``schemas`` (the schema names of the
    `TenantSchema` scopes on the way)."""

    __slots__ = ("models", "schemas", "unqualified")

    def __init__(self, models: frozenset[str], unqualified: frozenset[str], schemas: frozenset[str]) -> None:
        self.models = models
        self.unqualified = unqualified
        self.schemas = schemas


def _reach(model: str, nodes: Iterable[Node], annotations: Iterable[str] = ()) -> _Reached | None:
    """``model`` and the labels of the models ``nodes`` (expressions of ``model``'s rows, which may name the
    ``annotations``) reach: every query's model, the targets of the relation paths of lookups, ``F()`` and
    ``OuterRef()`` references, ``values()``, ``only()``, ``defer()``, ``distinct()``, ``group_by``,
    ``select_for_update(of=...)`` and ``select_related()`` (without arguments: the non-null foreign keys, five
    relations deep), the ``through`` model of every many-to-many relation on a path, and the concrete tables of all of
    them (`_with_concrete_tables()`). A field, ``pk`` or an annotation ends a path, and so does a model that is not
    registered; ``None`` when a path names anything else on a registered model.

    A query with a `TenantSchema` qualifies the tables of its scope's models in its subtree: they are left out of
    ``unqualified``, and the scope's schema is in ``schemas``."""
    from adorm.models.relations import ForeignKey, ManyToMany

    schemas: set[str] = set()

    def walk(
        items: list[tuple[Node, tuple[tuple[str, frozenset[str]], ...]]], start: set[str], scoped_root: bool
    ) -> tuple[set[str], set[str]]:
        """The labels the subtrees of ``items`` reach and the unqualified ones among them."""
        labels = set(start)
        unqualified: set[str] = set()

        def add(label: str) -> None:
            labels.add(label)
            unqualified.add(label)

        def follow(begin: str, path: str, names: frozenset[str]) -> None:
            current = begin
            for index, segment in enumerate(path.split("__")):
                if index == 0 and segment in names:
                    return
                found = _model_class(current)
                if found is None or found._meta is None:
                    return
                relation = _step(found, segment)
                if relation is _END:
                    return
                if isinstance(relation, ManyToMany):
                    add(relation.through)
                add(relation.target)
                current = relation.target

        def follow_non_null(label: str, depth: int) -> None:
            found = _model_class(label)
            if found is None or found._meta is None or depth > _SELECT_RELATED_DEPTH:
                return
            for relation in found.__adorm_relations__.values():
                if not isinstance(relation, ForeignKey):
                    continue
                info = found.__pydantic_fields__.get(relation.column)
                if relation.column != found._meta.pk and (info is None or _admits_none(info.annotation)):
                    continue
                add(relation.target)
                follow_non_null(relation.target, depth + 1)

        pending = list(items)
        first = scoped_root
        while pending:
            node, owners = pending.pop()
            if isinstance(node, Query) and node.tenant_schema is not None and not first:
                scope = node.tenant_schema
                schemas.add(scope.schema)
                inner, inner_unqualified = walk([(node, owners)], set(), True)
                labels.update(_with_concrete_tables(inner))
                unqualified.update(_with_concrete_tables(inner_unqualified) - set(scope.models))
                continue
            first = False
            if isinstance(node, Query) and node.is_combined:
                # A combined query reads the tables of its parts only; its selected names and ordering name columns of
                # the combined rows, which join nothing.
                labels.add(node.model)
                pending.extend((part, owners) for part in node.combined_queries)
            elif isinstance(node, Query):
                unqualified.add(node.model)
                labels.add(node.model)
                names = frozenset(_annotation_names(node))
                paths = (
                    *node.select,
                    *node.distinct_fields,
                    *(node.only_fields or ()),
                    *node.deferred_fields,
                    *(node.group_by or ()),
                )
                if isinstance(node.select_related_fields, tuple):
                    paths += node.select_related_fields
                elif node.select_related_fields:
                    follow_non_null(node.model, 1)
                if node.for_update is not None:
                    paths += tuple(path for path in node.for_update.of if path != "self")
                for path in paths:
                    follow(node.model, path, names)
                inner_owners = (*owners, (node.model, names))
                children = (*node.filters, *(expression for _, expression in node.annotations), *node.ordering)
                pending.extend((child, inner_owners) for child in children)
            elif isinstance(node, OuterRef):
                if len(owners) > node.levels:
                    outer, outer_names = owners[-1 - node.levels]
                    follow(outer, node.name, outer_names)
            elif isinstance(node, F):
                owner, owner_names = owners[-1]
                follow(owner, node.name, owner_names)
            else:
                pending.extend((child, owners) for child in node.get_source_expressions())
        return labels, unqualified

    # (node, the models of the enclosing queries with their annotation names, innermost last)
    root = ((model, frozenset(annotations)),)
    items = [(node, root) for node in nodes]
    try:
        labels, unqualified = walk(items, {model}, False)
    except _UnknownPathError:
        return None
    if not any(isinstance(node, Query) and node.model == model for node, _ in items):
        unqualified.add(model)
    return _Reached(
        frozenset(_with_concrete_tables(labels)),
        frozenset(_with_concrete_tables(unqualified)),
        frozenset(schemas),
    )


def _delete_models(label: str) -> frozenset[str] | None:
    """`ModelMeta.delete_models` of the model registered under ``label``; ``None`` when it is unknown."""
    model = _model_class(label)
    meta = None if model is None else model._meta
    return None if meta is None or meta.delete_models is None else frozenset(meta.delete_models)


def _annotation_names(query: Query) -> tuple[str, ...]:
    """The annotation names a path on ``query``'s rows may start with; for a combined query, those of its first
    query, whose names the combined rows carry."""
    while query.is_combined and not query.annotations:
        query = query.combined_queries[0]
    return tuple(name for name, _ in query.annotations)


def _query_annotations(operation: Any) -> tuple[str, ...]:
    query = getattr(operation, "query", None)
    return _annotation_names(query) if isinstance(query, Query) else ()


def _reach_of(operation: Any) -> _Reached | None:
    """`_reach()` over the nodes of an operation on the rows of its ``model``, computed once per operation."""
    cached = operation.__dict__.get("_adorm_reached", _MISSING)
    if cached is _MISSING:
        cached = _reach(operation.model, operation.nodes(), _query_annotations(operation))
        operation.__dict__["_adorm_reached"] = cached
    return cached


_MISSING = object()


def _reached(operation: Any) -> frozenset[str] | None:
    """The `Operation.models` of an operation on the rows of its ``model``: `_reach()` over its nodes."""
    reached = _reach_of(operation)
    return None if reached is None else reached.models


def _reached_schemas(operation: Any) -> frozenset[str]:
    """The `Operation.schemas` of an operation on the rows of its ``model``."""
    reached = _reach_of(operation)
    tenant_schema = getattr(operation, "tenant_schema", None)
    found = frozenset() if reached is None else reached.schemas
    return found | {tenant_schema.schema} if tenant_schema is not None else found


def _reached_unqualified(operation: Any) -> frozenset[str] | None:
    """The `Operation.unqualified_models` of an operation on the rows of its ``model``."""
    models = operation.models
    reached = _reach_of(operation)
    if models is None or reached is None:
        return None if models is None else models
    unqualified = models - (reached.models - reached.unqualified)
    tenant_schema = getattr(operation, "tenant_schema", None)
    return unqualified - set(tenant_schema.models) if tenant_schema is not None else unqualified


class Operation:
    """Base class of operations. A subclass sets ``kind`` and is registered in `OPERATIONS` under it; every
    operation has ``model``, the label of the model it reads or writes."""

    kind: ClassVar[str]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        kind = cls.__dict__.get("kind")
        if kind is not None:
            if _OPERATIONS.get(kind, cls) is not cls:
                raise TypeError(f"the operation kind {kind!r} is already registered as {_OPERATIONS[kind].__name__}")
            _OPERATIONS[kind] = cls

    def nodes(self) -> Iterator[Node]:
        """The query and the expressions of the operation, without their descendants."""
        return iter(())

    @cached_property
    def models(self) -> frozenset[str] | None:
        """The labels (``app_label.ModelName``) of the models whose tables the operation reads or writes, by the model
        graph (reachability, not rows); ``None`` when adorm cannot know them. Computed from the model registry on first
        access, without I/O. ``None`` here: a subclass of a bundled operation inherits that class's ``models``, and
        one that touches other tables overrides it."""
        return None

    @cached_property
    def schemas(self) -> frozenset[str]:
        """The schema names the operation qualifies tables with: those of the `TenantSchema` of its queries (combined
        queries and subqueries included) and of its ``tenant_schema``; empty without one. Computed on first access,
        without I/O."""
        tenant_schema = getattr(self, "tenant_schema", None)
        return frozenset() if tenant_schema is None else frozenset({tenant_schema.schema})

    @cached_property
    def unqualified_models(self) -> frozenset[str] | None:
        """The labels of the models whose tables the operation reads or writes without an explicit schema, so that the
        connection's search path or default database resolves them: `models` without the models a `TenantSchema`
        qualifies where it applies. ``None`` when `models` is ``None``; `models` itself for an operation without a
        schema."""
        models = self.models
        tenant_schema = getattr(self, "tenant_schema", None)
        return models if models is None or tenant_schema is None else models - set(tenant_schema.models)

    def constructs(self) -> Iterator[str]:
        """Construct names used by the operation's nodes, possibly repeated."""
        for node in self.nodes():
            yield from node.constructs()
        if getattr(self, "tenant_schema", None) is not None:
            yield "clause.tenant_schema"

    def _identity(self) -> tuple[Any, ...]:
        return (type(self), tuple(make_hashable(getattr(self, f.name)) for f in fields(self)))  # type: ignore[arg-type]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Operation):
            return NotImplemented
        return self._identity() == other._identity()

    def __hash__(self) -> int:
        return hash(self._identity())

    def to_dict(self, schema: SchemaSource | None = None) -> dict[str, Any]:
        """The operation as JSON-safe data: ``{"operation": kind, ...}``; values are encoded by the types ``schema``
        gives them. Raises ``ValueError`` for a value that is not valid for its type, ``TypeError`` for a value
        without a type that `adorm.codec` cannot infer and `adorm.FieldError` for a field path ``schema`` does not
        resolve."""
        return {"operation": self.kind, **self._encode(Encoder(schema))}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], schema: SchemaSource | None = None) -> Self:
        """The operation ``to_dict()`` produced with the same ``schema``. Raises ``ValueError`` for malformed data, an
        unknown operation kind, one that is not a ``cls`` or an operation its constructor refuses, and
        `adorm.FieldError` for a field path ``schema`` does not resolve."""
        if not isinstance(data, Mapping):
            raise ValueError(f"an encoded operation is a mapping, not {type(data).__name__}")
        kind = data.get("operation")
        operation = _OPERATIONS.get(kind) if isinstance(kind, str) else None
        if operation is None:
            raise ValueError(f"unknown operation {kind!r}")
        if not issubclass(operation, cls):
            raise ValueError(f"expected a {cls.__name__} operation, got {kind!r}")
        try:
            return operation._decode(Decoder(schema), data)  # type: ignore[return-value]
        except TypeError as exc:
            raise ValueError(f"invalid {kind} operation: {exc}") from None

    def _encode(self, encoder: Encoder) -> dict[str, Any]:
        raise NotImplementedError

    @classmethod
    def _decode(cls, decoder: Decoder, data: Mapping[str, Any]) -> Operation:
        raise NotImplementedError


@dataclass(frozen=True, eq=False)
class _QueryOperation(Operation):
    query: Query

    def __post_init__(self) -> None:
        _check_query(self.query)

    @property
    def model(self) -> str:
        """The label of the queried model."""
        return self.query.model

    def nodes(self) -> Iterator[Node]:
        yield self.query

    models = cached_property(_reached)
    schemas = cached_property(_reached_schemas)
    unqualified_models = cached_property(_reached_unqualified)

    def _encode(self, encoder: Encoder) -> dict[str, Any]:
        return {"query": encoder.node(self.query)}

    @classmethod
    def _decode(cls, decoder: Decoder, data: Mapping[str, Any]) -> Operation:
        return cls(_decode_query(decoder, data))


@dataclass(frozen=True, eq=False)
class Select(_QueryOperation):
    """The rows of ``query``: iterating or listing a query. Result: a list of rows (dicts)."""

    kind: ClassVar[str] = "select"


@dataclass(frozen=True, eq=False)
class Get(_QueryOperation):
    """The single row of ``query``. Result: the row; no row raises `ObjectDoesNotExist`, more than one
    `MultipleObjectsReturned`."""

    kind: ClassVar[str] = "get"


@dataclass(frozen=True, eq=False)
class First(_QueryOperation):
    """The first row of ``query``, ordered by the primary key when the query and the model have no ordering.
    Result: the row or ``None``. A sliced or grouped query has an ordering of its own (``TypeError``)."""

    kind: ClassVar[str] = "first"

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.query.is_sliced and not self.query.ordering:
            raise TypeError("cannot take the first row of a sliced query without an ordering")
        _check_grouped_order(self.query, "first")


def _check_grouped_order(query: Query, which: str) -> None:
    if query.is_grouped and not query.ordering:
        raise TypeError(f"cannot take the {which} row of a grouped query without an ordering")


@dataclass(frozen=True, eq=False)
class Last(_QueryOperation):
    """The last row of ``query`` (its ordering reversed, or descending primary key without one). Result: the row or
    ``None``. The query is not sliced, and a grouped query has an ordering (``TypeError``)."""

    kind: ClassVar[str] = "last"

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.query.is_sliced:
            raise TypeError("cannot reverse a query once a slice has been taken")
        _check_grouped_order(self.query, "last")


@dataclass(frozen=True, eq=False)
class Count(_QueryOperation):
    """The number of rows of ``query``. Result: an ``int``."""

    kind: ClassVar[str] = "count"


@dataclass(frozen=True, eq=False)
class Exists(_QueryOperation):
    """Whether ``query`` has a row. Result: a ``bool``."""

    kind: ClassVar[str] = "exists"


@dataclass(frozen=True, eq=False)
class InBulk(_QueryOperation):
    """The rows of ``query`` whose field ``field_name`` holds one of ``ids``; every row when ``ids`` is ``None``.
    Result: a list of rows (dicts), which the caller keys by the field's value.

    ``field_name`` is ``"pk"`` or a field name without ``__``; the driver refuses a field that is neither the primary
    key nor unique on its own (nor the only field the query is distinct on) with `ProgrammingError`. ``ids`` is an
    iterable of values (not a string), kept in order without repeats. The query selects model rows (no ``values()``),
    is not sliced and loads the field (not deferred); otherwise ``TypeError``. A combined query (`Query.is_combined`)
    raises `adorm.NotSupportedError`.
    """

    kind: ClassVar[str] = "in_bulk"
    field_name: str = "pk"
    ids: tuple[Any, ...] | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        query = self.query
        query.check_not_combined("in_bulk")
        _refuse_tenant_schemas(self, "in_bulk()")
        if query.is_sliced:
            raise TypeError("cannot use in_bulk() on a query once a slice has been taken")
        if query.row_format != "model":
            raise TypeError("in_bulk() cannot be used with values() or values_list()")
        name = self.field_name
        if not isinstance(name, str) or not name.isidentifier() or "__" in name:
            raise ValueError(f"{name!r} is not a field name")
        if name != "pk" and (
            name in query.deferred_fields or (query.only_fields is not None and name not in query.only_fields)
        ):
            raise TypeError(f"in_bulk() needs the field {name!r}, which the query defers")
        if self.ids is not None:
            if isinstance(self.ids, str | bytes) or not isinstance(self.ids, Iterable):
                raise TypeError("ids is an iterable of values, not a string")
            values = []
            for value in self.ids:
                if isinstance(value, Node):
                    raise TypeError("an id is a value, not an expression")
                value = freeze_value(value)
                if value not in values:
                    values.append(value)
            object.__setattr__(self, "ids", tuple(values))

    def select_query(self) -> Query:
        """``query`` filtered by ``<field_name>__in=ids`` (unchanged for ``ids=None``)."""
        if self.ids is None:
            return self.query
        return self.query.filter(**{f"{self.field_name}__in": list(self.ids)})

    def constructs(self) -> Iterator[str]:
        yield from super().constructs()
        if self.ids is not None:
            yield "lookup.in"

    def _encode(self, encoder: Encoder) -> dict[str, Any]:
        data = super()._encode(encoder)
        data["field_name"] = self.field_name
        with encoder.scope(self.query.model):
            hint = encoder.field_type(self.field_name)
            data["ids"] = None if self.ids is None else [encoder.operand(value, hint) for value in self.ids]
        return data

    @classmethod
    def _decode(cls, decoder: Decoder, data: Mapping[str, Any]) -> Operation:
        query = _decode_query(decoder, data)
        name, raw = _get(data, "field_name"), _get(data, "ids")
        if not isinstance(name, str):
            raise ValueError("the field name is a string")
        if raw is not None and not isinstance(raw, list):
            raise ValueError("the ids are a list or null")
        with decoder.scope(query.model):
            hint = decoder.field_type(name)
            ids = None if raw is None else tuple(decoder.operand(value, hint) for value in raw)
        return cls(query, name, ids)


_FORBIDDEN_NAME = re.compile(r"['`\"\]\[;\s\x00-\x1F\x7F-\x9F]|#|--|/\*|\*/")


def _combined_columns(query: Query) -> frozenset[str] | None:
    """The column names of the rows of the combined ``query``: its selected names, or ``None`` for model rows, whose
    columns are the first query's fields."""
    return frozenset(query.select) if query.row_format != "model" and query.select else None


def _check_combined_columns(query: Query, name: str, expression: Node) -> None:
    """Raise ``TypeError`` when ``expression`` refers to anything but a column of the combined rows of ``query``."""
    columns = _combined_columns(query)
    pending: list[Node] = [expression]
    while pending:
        node = pending.pop()
        if isinstance(node, Query):
            continue
        if isinstance(node, OuterRef):
            raise TypeError(f"{name!r} aggregates combined rows, which have no outer query to refer to")
        if isinstance(node, F):
            if "__" in node.name:
                raise TypeError(f"{name!r} aggregates combined rows: {node.name!r} is not a column of them")
            if columns is not None and node.name not in columns:
                raise TypeError(f"{name!r} aggregates combined rows: {node.name!r} is not a selected name")
            continue
        pending.extend(node.get_source_expressions())


@dataclass(frozen=True, eq=False)
class Aggregate(_QueryOperation):
    """The values of ``aggregations`` (name → expression that contains an aggregate) over the rows of ``query``.
    Result: a dict of the values by name.

    ``aggregations`` is a mapping or ``(name, expression)`` pairs, at least one. A name is a non-empty string without
    whitespace, control characters, quotes, backticks, brackets, ``;``, ``#``, ``--``, ``/*`` or ``*/``
    (``ValueError``); it may contain ``__`` (``"fee__sum"``) and may not repeat. An expression without an aggregate
    outside a subquery raises ``TypeError``, and so does a query distinct on fields. The expressions refer to the
    query's fields and annotations; over a sliced, distinct or grouped query, or one with aggregate annotations, they
    aggregate the query's rows as a subquery.

    Over a combined query (`Query.is_combined`) the expressions aggregate the combined rows: each ``F`` outside a
    subquery names a column of them, a field of the first query's model for model rows or a name the combined query
    selects; a path with ``__`` and an ``OuterRef`` raise ``TypeError``. Such an aggregate uses the construct
    ``aggregate.combined``.
    """

    kind: ClassVar[str] = "aggregate"
    aggregations: tuple[tuple[str, Expression], ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.query.distinct_fields:
            raise TypeError("cannot aggregate a query distinct on fields")
        items = self.aggregations.items() if isinstance(self.aggregations, Mapping) else self.aggregations
        pairs: list[tuple[str, Expression]] = []
        for name, expression in items:
            if not isinstance(name, str) or not name or _FORBIDDEN_NAME.search(name):
                raise ValueError(f"{name!r} is not an aggregation name")
            if any(n == name for n, _ in pairs):
                raise ValueError(f"the aggregation {name!r} is given twice")
            if not isinstance(expression, Expression) or not expression.contains_aggregate:
                raise TypeError(f"{name!r} is not an aggregate expression")
            pairs.append((name, expression))
        if not pairs:
            raise TypeError("aggregate() needs at least one aggregation")
        if self.query.is_combined:
            for name, expression in pairs:
                _check_combined_columns(self.query, name, expression)
        object.__setattr__(self, "aggregations", tuple(pairs))

    def constructs(self) -> Iterator[str]:
        yield from super().constructs()
        if self.query.is_combined:
            yield "aggregate.combined"

    def nodes(self) -> Iterator[Node]:
        yield self.query
        yield from (expression for _, expression in self.aggregations)

    def _encode(self, encoder: Encoder) -> dict[str, Any]:
        data = super()._encode(encoder)
        with encoder.scope(self.query.model, self.query.annotations):
            data["aggregations"] = [[name, encoder.node(expression)] for name, expression in self.aggregations]
        return data

    @classmethod
    def _decode(cls, decoder: Decoder, data: Mapping[str, Any]) -> Operation:
        query = _decode_query(decoder, data)
        raw = _get(data, "aggregations")
        if not isinstance(raw, list):
            raise ValueError("the aggregations are a list of [name, expression] pairs")
        pairs = []
        with decoder.scope(query.model, query.annotations):
            for item in raw:
                if not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str):
                    raise ValueError("an aggregation is a [name, expression] pair")
                pairs.append((item[0], decoder.node(item[1])))
        return cls(query, tuple(pairs))


@dataclass(frozen=True, eq=False)
class Delete(_QueryOperation):
    """Delete the rows of ``query`` and the rows that cascade from them. Result: ``(total, {label: count})``.

    The query is not sliced, not distinct on fields and selects model rows (no ``values()``); otherwise
    ``TypeError``. A combined query (`Query.is_combined`) raises `adorm.NotSupportedError`. `DeleteInstance` deletes a
    model instance.
    """

    kind: ClassVar[str] = "delete"

    def __post_init__(self) -> None:
        super().__post_init__()
        self.query.check_not_combined("delete")
        _refuse_tenant_schemas(self, "delete")
        if self.query.is_sliced:
            raise TypeError("cannot delete from a query once a slice has been taken")
        if self.query.distinct_fields:
            raise TypeError("cannot delete from a query distinct on fields")
        if self.query.row_format != "model":
            raise TypeError("cannot delete from a query after values() or values_list()")

    @cached_property
    def models(self) -> frozenset[str] | None:
        """The models the query reaches and those deleting its model's rows reaches (`ModelMeta.delete_models`);
        ``None`` when the model's ``delete_models`` is unknown."""
        cascade = _delete_models(self.query.model)
        reached = _reached(self)
        return None if cascade is None or reached is None else reached | cascade


@dataclass(frozen=True, eq=False)
class DeleteInstance(Operation):
    """Delete the instance of ``model`` with primary key ``pk`` as the driver deletes one instance, with the rows that
    cascade from it (the Django driver runs the model's ``delete()``; the native PostgreSQL driver refuses a model
    that overrides it). Result: ``(total, {label: count})``; a missing row deletes nothing.

    ``pk`` is a value, not ``None`` (``ValueError``) and not an expression (``TypeError``). ``tenant_schema``, a
    `TenantSchema` or ``None``, qualifies the tables of its models that the delete touches, cascades included; the
    encoded form has ``"tenant_schema"`` only when there is one.
    """

    kind: ClassVar[str] = "delete_instance"
    model: str
    pk: Any
    _: KW_ONLY
    tenant_schema: TenantSchema | None = None

    def __post_init__(self) -> None:
        check_model_label(self.model)
        if self.pk is None:
            raise ValueError("deleting an instance needs its primary key")
        if isinstance(self.pk, Node):
            raise TypeError("the primary key is a value, not an expression")
        object.__setattr__(self, "pk", freeze_value(self.pk))
        _check_tenant_schema(self.tenant_schema)

    @cached_property
    def models(self) -> frozenset[str] | None:
        """The model, its concrete tables and the models deleting its rows reaches (`ModelMeta.delete_models`);
        ``None`` when the model's ``delete_models`` is unknown."""
        cascade = _delete_models(self.model)
        return None if cascade is None else frozenset(_with_concrete_tables({self.model}) | cascade)

    def _encode(self, encoder: Encoder) -> dict[str, Any]:
        with encoder.scope(self.model):
            data = {"model": self.model, "pk": encoder.operand(self.pk, encoder.field_type("pk"))}
        return _encode_tenant_schema(encoder, data, self.tenant_schema)

    @classmethod
    def _decode(cls, decoder: Decoder, data: Mapping[str, Any]) -> Operation:
        model = check_model_label(_get(data, "model"))
        tenant_schema = _decode_tenant_schema(decoder, data)
        with decoder.scope(model):
            return cls(model, decoder.operand(_get(data, "pk"), decoder.field_type("pk")), tenant_schema=tenant_schema)


@dataclass(frozen=True, eq=False)
class Update(_QueryOperation):
    """Set ``values`` (field name → value or expression) on the rows of ``query``. Result: the number of rows.

    ``values`` is a mapping or ``(name, value)`` pairs; field names have no ``__``. The query is not sliced, not
    distinct on fields and not grouped, and an expression value contains no aggregate; otherwise ``TypeError``. A
    combined query (`Query.is_combined`) raises `adorm.NotSupportedError`.
    """

    kind: ClassVar[str] = "update"
    values: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        self.query.check_not_combined("update")
        if self.query.is_sliced:
            raise TypeError("cannot update a query once a slice has been taken")
        if self.query.distinct_fields:
            raise TypeError("cannot update a query distinct on fields")
        if self.query.is_grouped:
            raise TypeError("cannot update a grouped query")
        object.__setattr__(self, "values", _field_values(self.values))
        _refuse_tenant_schemas(self, "update")

    def nodes(self) -> Iterator[Node]:
        yield self.query
        yield from (value for _, value in self.values if isinstance(value, Node))

    def _encode(self, encoder: Encoder) -> dict[str, Any]:
        data = super()._encode(encoder)
        with encoder.scope(self.query.model):
            data["values"] = _encode_values(encoder, self.values)
        return data

    @classmethod
    def _decode(cls, decoder: Decoder, data: Mapping[str, Any]) -> Operation:
        query = _decode_query(decoder, data)
        with decoder.scope(query.model):
            values = _decode_values(decoder, _get(data, "values"), "values")
        return cls(query, values)


@dataclass(frozen=True, eq=False)
class Create(Operation):
    """Insert a row of ``model`` with ``values``. Result: the row as stored, with the values the database set (the
    primary key, defaults).

    ``values`` is a mapping or ``(name, value)`` pairs. An expression value refers to no field and contains no
    aggregate (``TypeError``). ``manager`` names the target ORM's manager whose ``create()`` inserts the row
    (`Query.manager`); with one, ``values`` may name arguments that are not fields, encoded with their inferred types,
    and a value that is a model instance raises ``TypeError`` (pass its primary key). The encoded form has
    ``"manager"`` only when there is one.
    """

    kind: ClassVar[str] = "create"
    model: str
    values: tuple[tuple[str, Any], ...] = ()
    manager: str | None = None

    def __post_init__(self) -> None:
        check_model_label(self.model)
        object.__setattr__(self, "values", _field_values(self.values, refs=False))
        Query(self.model, manager=self.manager)
        _refuse_tenant_schemas(self, "create()")
        if self.manager is not None:
            for name, value in self.values:
                _check_not_instance(value, f"the value of {name!r}")

    def constructs(self) -> Iterator[str]:
        yield from super().constructs()
        if self.manager is not None:
            yield "clause.manager"

    def nodes(self) -> Iterator[Node]:
        return (value for _, value in self.values if isinstance(value, Node))

    models = cached_property(_reached)

    def _encode(self, encoder: Encoder) -> dict[str, Any]:
        with encoder.scope(self.model):
            data = {"model": self.model, "values": _encode_values(encoder, self.values, extra=self.manager is not None)}
        if self.manager is not None:
            data["manager"] = self.manager
        return data

    @classmethod
    def _decode(cls, decoder: Decoder, data: Mapping[str, Any]) -> Operation:
        model = check_model_label(_get(data, "model"))
        manager = data.get("manager")
        if manager is not None and not isinstance(manager, str):
            raise ValueError("the manager is a name or null")
        with decoder.scope(model):
            values = _decode_values(decoder, _get(data, "values"), "values", extra=manager is not None)
            return cls(model, values, manager)


def _names(names: Any, what: str, *, pk: bool = False) -> tuple[str, ...] | None:
    """``names`` (``None`` or an iterable of field names, not a string) as a tuple without repeats; ``pk`` allows
    ``"pk"``."""
    if names is None:
        return None
    if isinstance(names, str) or not isinstance(names, Iterable):
        raise TypeError(f"{what} is an iterable of field names, not a string")
    result = tuple(dict.fromkeys(names))
    for name in result:
        if not isinstance(name, str) or not name.isidentifier() or "__" in name or (name == "pk" and not pk):
            raise ValueError(f"{name!r} is not a field name")
    return result


@dataclass(frozen=True, eq=False)
class BulkCreate(Operation):
    """Insert a row of ``model`` for each of ``rows``, as the target inserts several instances at once (Django's
    ``bulk_create()``). Result: a list with one row per item of ``rows``, in order: its values with those the
    database set (a primary key it generated, except under ``ignore_conflicts``).

    ``rows`` is an iterable of mappings or ``(name, value)`` pairs; the rows may name different fields, and an
    expression value refers to no field and contains no aggregate (``TypeError``). ``batch_size`` is ``None`` or a
    positive ``int`` (``ValueError``), the most rows one statement inserts; the driver may insert fewer per statement,
    and several statements run in one transaction (savepoint-free inside an open one). ``ignore_conflicts`` skips a
    row that violates a unique constraint; ``update_conflicts`` updates ``update_fields`` of the row it conflicts with
    on ``unique_fields`` (field names; ``unique_fields`` may name ``"pk"``). Both at once and ``update_conflicts``
    without ``update_fields`` raise ``ValueError``; without ``update_conflicts``, ``update_fields`` and
    ``unique_fields`` are checked as names and dropped (``None``), as Django ignores them.
    ``manager`` names the target ORM's manager whose ``bulk_create()`` inserts the rows (`Query.manager`). The encoded
    form has ``"manager"`` only when there is one.
    """

    kind: ClassVar[str] = "bulk_create"
    model: str
    rows: tuple[tuple[tuple[str, Any], ...], ...] = ()
    batch_size: int | None = None
    ignore_conflicts: bool = False
    update_conflicts: bool = False
    update_fields: tuple[str, ...] | None = None
    unique_fields: tuple[str, ...] | None = None
    manager: str | None = None

    def __post_init__(self) -> None:
        check_model_label(self.model)
        Query(self.model, manager=self.manager)
        if isinstance(self.rows, str | bytes | Mapping) or not isinstance(self.rows, Iterable):
            raise TypeError("rows is an iterable of field values")
        object.__setattr__(self, "rows", tuple(_field_values(row, refs=False) for row in self.rows))
        _refuse_tenant_schemas(self, "bulk_create()")
        size = self.batch_size
        if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size <= 0):
            raise ValueError(f"batch_size is a positive integer or None, not {size!r}")
        object.__setattr__(self, "ignore_conflicts", bool(self.ignore_conflicts))
        object.__setattr__(self, "update_conflicts", bool(self.update_conflicts))
        object.__setattr__(self, "update_fields", _names(self.update_fields, "update_fields"))
        object.__setattr__(self, "unique_fields", _names(self.unique_fields, "unique_fields", pk=True))
        if self.ignore_conflicts and self.update_conflicts:
            raise ValueError("ignore_conflicts and update_conflicts are mutually exclusive")
        if self.update_conflicts and not self.update_fields:
            raise ValueError("update_conflicts needs update_fields, the fields a conflicting row gets")
        if not self.update_conflicts:
            object.__setattr__(self, "update_fields", None)
            object.__setattr__(self, "unique_fields", None)

    def constructs(self) -> Iterator[str]:
        yield from super().constructs()
        if self.ignore_conflicts:
            yield "clause.on_conflict.ignore"
        if self.update_conflicts:
            yield "clause.on_conflict.update"
        if self.manager is not None:
            yield "clause.manager"

    def nodes(self) -> Iterator[Node]:
        return (value for row in self.rows for _, value in row if isinstance(value, Node))

    models = cached_property(_reached)

    def _encode(self, encoder: Encoder) -> dict[str, Any]:
        with encoder.scope(self.model):
            data: dict[str, Any] = {
                "model": self.model,
                "rows": [_encode_values(encoder, row) for row in self.rows],
                "batch_size": self.batch_size,
                "ignore_conflicts": self.ignore_conflicts,
                "update_conflicts": self.update_conflicts,
                "update_fields": None if self.update_fields is None else list(self.update_fields),
                "unique_fields": None if self.unique_fields is None else list(self.unique_fields),
            }
        if self.manager is not None:
            data["manager"] = self.manager
        return data

    @classmethod
    def _decode(cls, decoder: Decoder, data: Mapping[str, Any]) -> Operation:
        model = check_model_label(_get(data, "model"))
        manager = data.get("manager")
        if manager is not None and not isinstance(manager, str):
            raise ValueError("the manager is a name or null")
        raw = _get(data, "rows")
        if not isinstance(raw, list):
            raise ValueError("the rows are a list of mappings of field names to values")
        batch_size = _get(data, "batch_size")
        if batch_size is not None and (isinstance(batch_size, bool) or not isinstance(batch_size, int)):
            raise ValueError("batch_size is an integer or null")
        names = {}
        for key in ("update_fields", "unique_fields"):
            value = _get(data, key)
            if value is not None and not (isinstance(value, list) and all(isinstance(n, str) for n in value)):
                raise ValueError(f"{key} is a list of field names or null")
            names[key] = value
        with decoder.scope(model):
            rows = tuple(_decode_values(decoder, row, "row values") for row in raw)
        return cls(
            model,
            rows,
            batch_size,
            ignore_conflicts=_flag(data, "ignore_conflicts"),
            update_conflicts=_flag(data, "update_conflicts"),
            manager=manager,
            **names,
        )


@dataclass(frozen=True, eq=False)
class CreateFromParent(Operation):
    """Insert the row of the multi-table inheritance child ``model`` for the existing row of its parent whose primary
    key is ``parent_pk``: only the child's own table gets a row, with ``values`` (field name → value of the child's
    own fields); the parent's row is not written. Result: the child's row as stored, the parent's fields included.

    ``parent_pk`` is a value, not ``None`` (``ValueError``) and not an expression; a value of ``values`` refers to no
    field and contains no aggregate (``TypeError``). The driver raises `ProgrammingError` for a model that is not a
    multi-table inheritance child and for a value of an inherited field, `ObjectDoesNotExist` when the parent has no
    row with ``parent_pk`` and `IntegrityError` when the child already has one.
    """

    kind: ClassVar[str] = "create_from_parent"
    model: str
    parent_pk: Any
    values: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        check_model_label(self.model)
        if self.parent_pk is None:
            raise ValueError("creating a child of an existing parent needs the parent's primary key")
        if isinstance(self.parent_pk, Node):
            raise TypeError("the primary key is a value, not an expression")
        object.__setattr__(self, "parent_pk", freeze_value(self.parent_pk))
        object.__setattr__(self, "values", _field_values(self.values, refs=False))
        _refuse_tenant_schemas(self, "create_from_parent()")

    def nodes(self) -> Iterator[Node]:
        return (value for _, value in self.values if isinstance(value, Node))

    models = cached_property(_reached)

    def _encode(self, encoder: Encoder) -> dict[str, Any]:
        with encoder.scope(self.model):
            return {
                "model": self.model,
                "parent_pk": encoder.operand(self.parent_pk, encoder.field_type("pk")),
                "values": _encode_values(encoder, self.values),
            }

    @classmethod
    def _decode(cls, decoder: Decoder, data: Mapping[str, Any]) -> Operation:
        model = check_model_label(_get(data, "model"))
        with decoder.scope(model):
            return cls(
                model,
                decoder.operand(_get(data, "parent_pk"), decoder.field_type("pk")),
                _decode_values(decoder, _get(data, "values"), "values"),
            )


@dataclass(frozen=True, eq=False)
class FullClean(Operation):
    """Validate an instance of ``model`` as the target validates one before a save (Django: ``Model.full_clean()``).

    The instance has the primary key ``pk`` and ``values`` (field name → value: its loaded and assigned fields); the
    driver puts them over the stored row when ``adding`` is false and ``pk`` is not ``None`` (the row read first when
    ``values`` lacks fields), otherwise over the target's defaults. ``exclude`` names fields or relations that are not
    validated; ``validate_unique`` and ``validate_constraints`` switch the unique checks and the model's constraints.
    ``adding`` tells whether the instance is not stored yet: a unique check of a stored instance leaves its own row
    out. Result: the instance's fields after cleaning, as a row; errors raise `adorm.ValidationError` with the
    messages by adorm field name (a foreign key's under its relation's name, the model's under ``"__all__"``).

    ``pk`` and the values are plain values, not expressions (``TypeError``); ``exclude`` is an iterable of names, not
    a string (``TypeError``). ``tenant_schema``, a `TenantSchema` or ``None``, qualifies the tables of its models in
    the validation's queries; the encoded form has ``"tenant_schema"`` only when there is one.
    """

    kind: ClassVar[str] = "full_clean"
    model: str
    pk: Any
    values: tuple[tuple[str, Any], ...] = ()
    exclude: tuple[str, ...] = ()
    validate_unique: bool = True
    validate_constraints: bool = True
    adding: bool = True
    _: KW_ONLY
    tenant_schema: TenantSchema | None = None

    def __post_init__(self) -> None:
        check_model_label(self.model)
        _check_tenant_schema(self.tenant_schema)
        if isinstance(self.pk, Node):
            raise TypeError("the primary key is a value, not an expression")
        object.__setattr__(self, "pk", freeze_value(self.pk))
        pairs = _field_values(self.values, refs=False)
        for name, value in pairs:
            if isinstance(value, Node):
                raise TypeError(f"the value of {name!r} is a plain value, not an expression")
        object.__setattr__(self, "values", pairs)
        if isinstance(self.exclude, str):
            raise TypeError("exclude is an iterable of field names, not a string")
        names = tuple(dict.fromkeys(self.exclude))
        for name in names:
            if not isinstance(name, str) or not name.isidentifier():
                raise ValueError(f"{name!r} is not a field name")
        object.__setattr__(self, "exclude", names)
        for flag in ("validate_unique", "validate_constraints", "adding"):
            object.__setattr__(self, flag, bool(getattr(self, flag)))

    @cached_property
    def models(self) -> frozenset[str] | None:
        """The model, its concrete tables (unique checks and constraints) and the targets of its foreign keys (each
        assigned key is validated against its target), with theirs."""
        from adorm.models.relations import ForeignKey

        model = _model_class(self.model)
        targets = () if model is None else model.__adorm_relations__.values()
        labels = {self.model, *(r.target for r in targets if isinstance(r, ForeignKey))}
        return frozenset(_with_concrete_tables(labels))

    def _encode(self, encoder: Encoder) -> dict[str, Any]:
        with encoder.scope(self.model):
            return {
                "model": self.model,
                "pk": encoder.operand(self.pk, encoder.field_type("pk")),
                "values": _encode_values(encoder, self.values),
                "exclude": list(self.exclude),
                "validate_unique": self.validate_unique,
                "validate_constraints": self.validate_constraints,
                "adding": self.adding,
            } | _encode_tenant_schema(encoder, {}, self.tenant_schema)

    @classmethod
    def _decode(cls, decoder: Decoder, data: Mapping[str, Any]) -> Operation:
        model = check_model_label(_get(data, "model"))
        exclude = _get(data, "exclude")
        if not (isinstance(exclude, list) and all(isinstance(name, str) for name in exclude)):
            raise ValueError("exclude is a list of field names")
        with decoder.scope(model):
            return cls(
                model,
                decoder.operand(_get(data, "pk"), decoder.field_type("pk")),
                _decode_values(decoder, _get(data, "values"), "values"),
                exclude=tuple(exclude),
                validate_unique=_flag(data, "validate_unique"),
                validate_constraints=_flag(data, "validate_constraints"),
                adding=_flag(data, "adding"),
                tenant_schema=_decode_tenant_schema(decoder, data),
            )


@dataclass(frozen=True, eq=False)
class Save(Operation):
    """Save an instance of ``model``: update the row with primary key ``pk``, or insert one when there is none.

    ``values`` holds the instance's loaded and assigned fields; ``update_fields``, a subset of them, limits the
    update to those fields and never inserts. ``force_insert`` always inserts, ``force_update`` never inserts.
    Result: the values the database set (at least the primary key).

    ``force_insert`` with ``force_update`` or ``update_fields``, ``force_update`` without ``pk`` and an
    ``update_fields`` name missing from ``values`` raise ``ValueError``. A value that refers to a field of the row
    raises ``TypeError`` when the save inserts (no ``pk``, or ``force_insert``). ``tenant_schema``, a `TenantSchema`
    or ``None``, qualifies the tables of its models that the save reads or writes; a query in a value with one raises
    `adorm.NotSupportedError`. The encoded form has ``"tenant_schema"`` only when there is one.
    """

    kind: ClassVar[str] = "save"
    model: str
    pk: Any
    values: tuple[tuple[str, Any], ...] = ()
    update_fields: tuple[str, ...] | None = None
    force_insert: bool = False
    force_update: bool = False
    _: KW_ONLY
    tenant_schema: TenantSchema | None = None

    def __post_init__(self) -> None:
        check_model_label(self.model)
        _check_tenant_schema(self.tenant_schema)
        if isinstance(self.pk, Node):
            raise TypeError("the primary key is a value, not an expression")
        object.__setattr__(self, "pk", freeze_value(self.pk))
        object.__setattr__(
            self, "values", _field_values(self.values, refs=self.pk is not None and not self.force_insert)
        )
        _refuse_tenant_schemas(self, "a save with a value")
        if self.update_fields is not None:
            if isinstance(self.update_fields, str):
                raise TypeError("update_fields is an iterable of field names, not a string")
            names = tuple(dict.fromkeys(self.update_fields))
            missing = [n for n in names if n not in {name for name, _ in self.values}]
            if missing:
                raise ValueError(f"update_fields names fields without a value: {', '.join(map(repr, missing))}")
            object.__setattr__(self, "update_fields", names)
        object.__setattr__(self, "force_insert", bool(self.force_insert))
        object.__setattr__(self, "force_update", bool(self.force_update))
        if self.force_insert and (self.force_update or self.update_fields is not None):
            raise ValueError("force_insert excludes force_update and update_fields")
        if self.force_update and self.pk is None:
            raise ValueError("force_update needs a primary key")

    def nodes(self) -> Iterator[Node]:
        return (value for _, value in self.values if isinstance(value, Node))

    models = cached_property(_reached)
    schemas = cached_property(_reached_schemas)
    unqualified_models = cached_property(_reached_unqualified)

    def _encode(self, encoder: Encoder) -> dict[str, Any]:
        with encoder.scope(self.model):
            data = {
                "model": self.model,
                "pk": encoder.operand(self.pk, encoder.field_type("pk")),
                "values": _encode_values(encoder, self.values),
                "update_fields": None if self.update_fields is None else list(self.update_fields),
                "force_insert": self.force_insert,
                "force_update": self.force_update,
            }
        return _encode_tenant_schema(encoder, data, self.tenant_schema)

    @classmethod
    def _decode(cls, decoder: Decoder, data: Mapping[str, Any]) -> Operation:
        model = check_model_label(_get(data, "model"))
        update_fields = _get(data, "update_fields")
        if update_fields is not None and not (
            isinstance(update_fields, list) and all(isinstance(n, str) for n in update_fields)
        ):
            raise ValueError("update_fields is a list of field names or null")
        with decoder.scope(model):
            return cls(
                model,
                decoder.operand(_get(data, "pk"), decoder.field_type("pk")),
                _decode_values(decoder, _get(data, "values"), "values"),
                update_fields=update_fields,
                force_insert=_flag(data, "force_insert"),
                force_update=_flag(data, "force_update"),
                tenant_schema=_decode_tenant_schema(decoder, data),
            )


def _lookups(lookups: FieldValues) -> tuple[tuple[str, Any], ...]:
    items = lookups.items() if isinstance(lookups, Mapping) else lookups
    pairs: list[tuple[str, Any]] = []
    for key, value in items:
        if not isinstance(key, str):
            raise TypeError(f"a lookup key is a string, not {type(key).__name__}")
        if any(k == key for k, _ in pairs):
            raise ValueError(f"the lookup {key!r} is given twice")
        pairs.append((key, None if value is None else build_lookup(key, value).rhs))
    return tuple(pairs)


@dataclass(frozen=True, eq=False)
class _LookupWrite(Operation):
    """The common part of `GetOrCreate` and `UpdateOrCreate`: a query, lookups and defaults."""

    query: Query
    lookups: tuple[tuple[str, Any], ...] = ()
    defaults: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        _check_query(self.query)
        self.query.check_not_combined(self.kind)
        if self.query.is_sliced:
            raise TypeError("cannot filter a query once a slice has been taken")
        object.__setattr__(self, "lookups", _lookups(self.lookups))
        object.__setattr__(self, "defaults", _field_values(self.defaults, refs=False))
        values = (value for _, value in self.defaults if isinstance(value, Node))
        _refuse_tenant_schemas(self, f"{self.kind}()", (self.query, *values))

    @property
    def model(self) -> str:
        """The label of the queried model."""
        return self.query.model

    def nodes(self) -> Iterator[Node]:
        yield self.query
        yield from (build_lookup(key, value) for key, value in self.lookups)
        yield from (value for _, value in self.defaults if isinstance(value, Node))

    models = cached_property(_reached)

    def _encode(self, encoder: Encoder) -> dict[str, Any]:
        data: dict[str, Any] = {"query": encoder.node(self.query)}
        with encoder.scope(self.query.model, self.query.annotations):
            lookups = []
            for key, value in self.lookups:
                path, name = split_lookup(key)
                lookup = LOOKUPS[name]
                hint = lookup.rhs_type(encoder.field_type(path))
                lookups.append([key, encoder.operand(value, hint, sequence=lookup.rhs_is_sequence)])
            data["lookups"] = lookups
        with encoder.scope(self.query.model):
            data["defaults"] = _encode_values(encoder, self.defaults)
        return data

    @classmethod
    def _decode_parts(
        cls, decoder: Decoder, data: Mapping[str, Any]
    ) -> tuple[Query, tuple[tuple[str, Any], ...], tuple[tuple[str, Any], ...]]:
        query = _decode_query(decoder, data)
        raw = _get(data, "lookups")
        if not isinstance(raw, list):
            raise ValueError("the lookups are a list of [key, value] pairs")
        with decoder.scope(query.model, query.annotations):
            lookups = []
            for item in raw:
                if not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str):
                    raise ValueError("a lookup is a [key, value] pair")
                path, name = split_lookup(item[0])
                lookup = LOOKUPS[name]
                hint = lookup.rhs_type(decoder.field_type(path))
                lookups.append((item[0], decoder.operand(item[1], hint, sequence=lookup.rhs_is_sequence)))
        with decoder.scope(query.model):
            defaults = _decode_values(decoder, _get(data, "defaults"), "defaults")
        return query, tuple(lookups), defaults


@dataclass(frozen=True, eq=False)
class GetOrCreate(_LookupWrite):
    """The row of ``query`` matching ``lookups``, or a new row when there is none.

    The new row has the values of the ``lookups`` whose keys have no ``__``, updated with ``defaults``. Result:
    ``(row, created)``.

    ``lookups`` and ``defaults`` are mappings or pairs; lookup values are prepared as for ``filter()``. The query is
    not sliced and a default refers to no field and contains no aggregate; otherwise ``TypeError``. A combined query
    (`Query.is_combined`) raises `adorm.NotSupportedError`.
    """

    kind: ClassVar[str] = "get_or_create"

    @classmethod
    def _decode(cls, decoder: Decoder, data: Mapping[str, Any]) -> Operation:
        return cls(*cls._decode_parts(decoder, data))


@dataclass(frozen=True, eq=False)
class UpdateOrCreate(_LookupWrite):
    """Set ``defaults`` on the row of ``query`` matching ``lookups``, or insert a new row when there is none.

    The new row has the values of the ``lookups`` whose keys have no ``__``, updated with ``create_defaults``, or with
    ``defaults`` when ``create_defaults`` is ``None``. Result: ``(row, created)``, the row as stored.

    ``lookups``, ``defaults`` and ``create_defaults`` are as for `GetOrCreate`: a default refers to no field and
    contains no aggregate (``TypeError``). The encoded form has ``"create_defaults"`` (``null`` when there are none).
    """

    kind: ClassVar[str] = "update_or_create"
    create_defaults: tuple[tuple[str, Any], ...] | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.create_defaults is not None:
            object.__setattr__(self, "create_defaults", _field_values(self.create_defaults, refs=False))
            values = (value for _, value in self.create_defaults if isinstance(value, Node))
            _refuse_tenant_schemas(self, f"{self.kind}()", values)

    def nodes(self) -> Iterator[Node]:
        yield from super().nodes()
        yield from (value for _, value in self.create_defaults or () if isinstance(value, Node))

    def _encode(self, encoder: Encoder) -> dict[str, Any]:
        data = super()._encode(encoder)
        with encoder.scope(self.query.model):
            data["create_defaults"] = (
                None if self.create_defaults is None else _encode_values(encoder, self.create_defaults)
            )
        return data

    @classmethod
    def _decode(cls, decoder: Decoder, data: Mapping[str, Any]) -> Operation:
        query, lookups, defaults = cls._decode_parts(decoder, data)
        raw = _get(data, "create_defaults")
        create_defaults = None
        if raw is not None:
            with decoder.scope(query.model):
                create_defaults = _decode_values(decoder, raw, "create defaults")
        return cls(query, lookups, defaults, create_defaults)


@dataclass(frozen=True, eq=False)
class CheckTable(Operation):
    """Compare the database tables of ``model`` with what the model expects of them; it reads the database's catalogue
    and changes nothing. ``fields`` are the target ORM's names of the model's fields, ``many_to_many`` those of its
    many-to-many relations, whose automatic link tables are checked too.

    Result: a list of problems, each a dict with the keys ``kind`` (an `adorm.checks.SchemaProblemKind`), ``table``,
    ``column``, ``field`` (an ORM name of ``fields`` or ``many_to_many``), ``expected`` and ``actual``, ``None`` where
    they do not apply. The list is empty when the tables match.
    """

    kind: ClassVar[str] = "check_table"
    model: str
    fields: tuple[str, ...] = ()
    many_to_many: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        check_model_label(self.model)
        object.__setattr__(self, "fields", _names(self.fields, "fields") or ())
        object.__setattr__(self, "many_to_many", _names(self.many_to_many, "many_to_many") or ())

    @cached_property
    def models(self) -> frozenset[str] | None:
        """The model, its concrete tables and the ``through`` model of each relation named in ``many_to_many``."""
        from adorm.models.relations import ManyToMany

        labels = {self.model}
        model = _model_class(self.model)
        if model is not None and model._meta is not None:
            for name, relation in model.__adorm_relations__.items():
                if isinstance(relation, ManyToMany) and model._meta.orm_name(name) in self.many_to_many:
                    labels.add(relation.through)
        return frozenset(_with_concrete_tables(labels))

    def _encode(self, encoder: Encoder) -> dict[str, Any]:  # noqa: ARG002 - names only
        return {"model": self.model, "fields": list(self.fields), "many_to_many": list(self.many_to_many)}

    @classmethod
    def _decode(cls, decoder: Decoder, data: Mapping[str, Any]) -> Operation:  # noqa: ARG003 - names only
        fields, many_to_many = _get(data, "fields"), _get(data, "many_to_many")
        if not isinstance(fields, list) or not isinstance(many_to_many, list):
            raise ValueError("fields and many_to_many are lists of names")
        return cls(check_model_label(_get(data, "model")), tuple(fields), tuple(many_to_many))


@dataclass(frozen=True)
class ModelRow:
    """A model instance in the result of a `Call`: the row of the model ``model`` (a label), keyed by adorm field
    names. A driver returns one for every instance of the target ORM's model in a driver method's result; the caller
    builds an instance of the registered model from it."""

    model: str
    row: Mapping[str, Any]

    def __post_init__(self) -> None:
        check_model_label(self.model)
        if not isinstance(self.row, Mapping):
            raise TypeError(f"a row is a mapping, not {type(self.row).__name__}")
        object.__setattr__(self, "row", MappingProxyType(dict(self.row)))

    def __hash__(self) -> int:
        return hash((self.model, make_hashable(dict(self.row))))


def _check_not_instance(value: Any, what: str) -> None:
    from adorm.models.base import Model

    if isinstance(value, Model):
        raise TypeError(f"{what} is a plain value, not a {type(value).__name__} instance: pass its primary key")


def _arguments(args: Iterable[Any], what: str) -> tuple[Any, ...]:
    values = []
    for value in args:
        if isinstance(value, Node):
            raise TypeError(f"{what} is a plain value, not an expression")
        _check_not_instance(value, what)
        values.append(freeze_value(value))
    return tuple(values)


def _method_schema(codec: Encoder | Decoder, call_key: tuple[str, str, str], model: str) -> Any:
    schema = codec.schema.model_schema(model) if codec.schema is not None else None
    return None if schema is None else schema.methods.get(call_key)


@dataclass(frozen=True, eq=False)
class Call(Operation):
    """Call the driver method ``method`` of the target ORM.

    ``manager`` names the target ORM's manager the method runs on; it defaults to `Query.manager` and, when the query
    carries one, equals it. ``target`` ``"manager"`` calls the method on that manager, and ``query`` is then the
    model's plain query (``Query(model)``, or ``Query(model, manager=manager)``); ``"queryset"`` calls it on the query
    set of ``query`` started from that manager. ``args`` are the positional arguments, ``kwargs`` the keyword arguments
    as ``(name, value)`` pairs; both are plain values, not expressions and not model instances (pass the primary key).
    Result: the method's return value as plain data, with every instance of a model as a `ModelRow`.

    No manager, a manager other than the query's, a method name that is not public or an invalid argument raises
    ``TypeError`` or ``ValueError``. A driver runs only methods the registered model declares
    (`adorm.models.find_driver_method()`). Arguments are encoded with the parameter types the model schema's
    `MethodSchema` gives, or their inferred types.
    """

    kind: ClassVar[str] = "call"
    query: Query
    method: str
    target: str = "queryset"
    args: tuple[Any, ...] = ()
    kwargs: tuple[tuple[str, Any], ...] = ()
    manager: str | None = None

    def __post_init__(self) -> None:
        _check_query(self.query)
        if self.manager is None:
            object.__setattr__(self, "manager", self.query.manager)
        if self.manager is None:
            raise TypeError("a driver method needs an ORM manager")
        if not isinstance(self.manager, str) or not self.manager.isidentifier() or self.manager.startswith("_"):
            raise ValueError(f"{self.manager!r} is not a manager name")
        if self.query.manager is not None and self.query.manager != self.manager:
            raise ValueError(f"the query runs on the manager {self.query.manager!r}, not {self.manager!r}")
        if not isinstance(self.method, str) or not self.method.isidentifier() or self.method.startswith("_"):
            raise ValueError(f"{self.method!r} is not a public method name")
        if self.target not in ("manager", "queryset"):
            raise ValueError(f"the target is 'manager' or 'queryset', not {self.target!r}")
        if self.target == "manager" and self.query != Query(self.query.model, manager=self.query.manager):
            raise ValueError("a manager method takes the manager's plain query")
        object.__setattr__(self, "args", _arguments(self.args, "an argument"))
        items = self.kwargs.items() if isinstance(self.kwargs, Mapping) else self.kwargs
        pairs: list[tuple[str, Any]] = []
        for name, value in items:
            if not isinstance(name, str) or not name.isidentifier():
                raise ValueError(f"{name!r} is not an argument name")
            if any(n == name for n, _ in pairs):
                raise ValueError(f"the argument {name!r} is given twice")
            pairs.append((name, _arguments((value,), f"the argument {name!r}")[0]))
        object.__setattr__(self, "kwargs", tuple(pairs))

    @property
    def model(self) -> str:
        """The label of the model."""
        return self.query.model

    @property
    def key(self) -> tuple[str, str, str]:
        """The `adorm.schema.MethodKey` of the method: ``(manager, target, method)``."""
        return (self.manager, self.target, self.method)  # type: ignore[return-value]

    def nodes(self) -> Iterator[Node]:
        yield self.query

    def _encode(self, encoder: Encoder) -> dict[str, Any]:
        method = _method_schema(encoder, self.key, self.query.model)
        return {
            "query": encoder.node(self.query),
            "manager": self.manager,
            "method": self.method,
            "target": self.target,
            "args": [
                encoder.operand(value, None if method is None else method.arg_type(index))
                for index, value in enumerate(self.args)
            ],
            "kwargs": {
                name: encoder.operand(value, None if method is None else method.kwarg_type(name))
                for name, value in self.kwargs
            },
        }

    @classmethod
    def _decode(cls, decoder: Decoder, data: Mapping[str, Any]) -> Operation:
        query = _decode_query(decoder, data)
        name, target, manager = _get(data, "method"), _get(data, "target"), _get(data, "manager")
        if not isinstance(manager, str):
            raise ValueError("the manager is a name")
        raw_args, raw_kwargs = _get(data, "args"), _get(data, "kwargs")
        if not isinstance(raw_args, list) or not isinstance(raw_kwargs, Mapping):
            raise ValueError("the arguments are a list and a mapping")
        method = _method_schema(decoder, (manager, target, name), query.model)
        args = tuple(
            decoder.operand(value, None if method is None else method.arg_type(index))
            for index, value in enumerate(raw_args)
        )
        kwargs = tuple(
            (key, decoder.operand(value, None if method is None else method.kwarg_type(key)))
            for key, value in raw_kwargs.items()
        )
        return cls(query, name, target, args, kwargs, manager)
