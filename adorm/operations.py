"""Operations: the description of every read and write a driver executes.

An operation is an immutable dataclass compared by value. ``kind`` names it for `Capabilities` and
``constructs()`` names the constructs it uses. ``to_dict()`` and ``from_dict()`` convert it to and from JSON-safe
data, with values encoded by the types a schema gives them (`adorm.codec`). Building an operation does no I/O.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, fields
from types import MappingProxyType
from typing import Any, ClassVar, Self

from adorm.codec import Decoder, Encoder
from adorm.query.expressions import Expression, F, freeze_value, make_hashable, Node, OuterRef
from adorm.query.lookups import build_lookup, LOOKUPS, split_lookup
from adorm.query.query import check_model_label, Query
from adorm.schema import SchemaSource

__all__ = [
    "OPERATIONS",
    "Call",
    "Count",
    "Create",
    "Delete",
    "Exists",
    "First",
    "Get",
    "GetOrCreate",
    "Last",
    "ModelRow",
    "Operation",
    "Save",
    "Select",
    "Update",
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
        return depth <= 1
    if isinstance(node, F):
        return depth == 0
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

    def constructs(self) -> Iterator[str]:
        """Construct names used by the operation's nodes, possibly repeated."""
        for node in self.nodes():
            yield from node.constructs()

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
        gives them. Raises ``ValueError`` for a value that is not valid for its type and ``TypeError`` for a value
        without a type that `adorm.codec` cannot infer."""
        return {"operation": self.kind, **self._encode(Encoder(schema))}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], schema: SchemaSource | None = None) -> Self:
        """The operation ``to_dict()`` produced with the same ``schema``. Raises ``ValueError`` for malformed data, an
        unknown operation kind, one that is not a ``cls`` or an operation its constructor refuses."""
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
    Result: the row or ``None``. A sliced query has an ordering of its own (``TypeError``)."""

    kind: ClassVar[str] = "first"

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.query.is_sliced and not self.query.ordering:
            raise TypeError("cannot take the first row of a sliced query without an ordering")


@dataclass(frozen=True, eq=False)
class Last(_QueryOperation):
    """The last row of ``query`` (its ordering reversed, or descending primary key without one). Result: the row or
    ``None``. The query is not sliced (``TypeError``)."""

    kind: ClassVar[str] = "last"

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.query.is_sliced:
            raise TypeError("cannot reverse a query once a slice has been taken")


@dataclass(frozen=True, eq=False)
class Count(_QueryOperation):
    """The number of rows of ``query``. Result: an ``int``."""

    kind: ClassVar[str] = "count"


@dataclass(frozen=True, eq=False)
class Exists(_QueryOperation):
    """Whether ``query`` has a row. Result: a ``bool``."""

    kind: ClassVar[str] = "exists"


@dataclass(frozen=True, eq=False)
class Delete(_QueryOperation):
    """Delete the rows of ``query`` and the rows that cascade from them. Result: ``(total, {label: count})``.

    A model instance is deleted with ``Delete(Query(label).filter(pk=pk))``. The query is not sliced, not distinct on
    fields and selects model rows (no ``values()``); otherwise ``TypeError``.
    """

    kind: ClassVar[str] = "delete"

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.query.is_sliced:
            raise TypeError("cannot delete from a query once a slice has been taken")
        if self.query.distinct_fields:
            raise TypeError("cannot delete from a query distinct on fields")
        if self.query.row_format != "model":
            raise TypeError("cannot delete from a query after values() or values_list()")


@dataclass(frozen=True, eq=False)
class Update(_QueryOperation):
    """Set ``values`` (field name → value or expression) on the rows of ``query``. Result: the number of rows.

    ``values`` is a mapping or ``(name, value)`` pairs; field names have no ``__``. The query is not sliced and not
    distinct on fields, and an expression value contains no aggregate; otherwise ``TypeError``.
    """

    kind: ClassVar[str] = "update"
    values: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.query.is_sliced:
            raise TypeError("cannot update a query once a slice has been taken")
        if self.query.distinct_fields:
            raise TypeError("cannot update a query distinct on fields")
        object.__setattr__(self, "values", _field_values(self.values))

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
        if self.manager is not None:
            for name, value in self.values:
                _check_not_instance(value, f"the value of {name!r}")

    def constructs(self) -> Iterator[str]:
        yield from super().constructs()
        if self.manager is not None:
            yield "clause.manager"

    def nodes(self) -> Iterator[Node]:
        return (value for _, value in self.values if isinstance(value, Node))

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


@dataclass(frozen=True, eq=False)
class Save(Operation):
    """Save an instance of ``model``: update the row with primary key ``pk``, or insert one when there is none.

    ``values`` holds the instance's loaded and assigned fields; ``update_fields``, a subset of them, limits the
    update to those fields and never inserts. ``force_insert`` always inserts, ``force_update`` never inserts.
    Result: the values the database set (at least the primary key).

    ``force_insert`` with ``force_update`` or ``update_fields``, ``force_update`` without ``pk`` and an
    ``update_fields`` name missing from ``values`` raise ``ValueError``. A value that refers to a field of the row
    raises ``TypeError`` when the save inserts (no ``pk``, or ``force_insert``).
    """

    kind: ClassVar[str] = "save"
    model: str
    pk: Any
    values: tuple[tuple[str, Any], ...] = ()
    update_fields: tuple[str, ...] | None = None
    force_insert: bool = False
    force_update: bool = False

    def __post_init__(self) -> None:
        check_model_label(self.model)
        if isinstance(self.pk, Node):
            raise TypeError("the primary key is a value, not an expression")
        object.__setattr__(self, "pk", freeze_value(self.pk))
        object.__setattr__(
            self, "values", _field_values(self.values, refs=self.pk is not None and not self.force_insert)
        )
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

    def _encode(self, encoder: Encoder) -> dict[str, Any]:
        with encoder.scope(self.model):
            return {
                "model": self.model,
                "pk": encoder.operand(self.pk, encoder.field_type("pk")),
                "values": _encode_values(encoder, self.values),
                "update_fields": None if self.update_fields is None else list(self.update_fields),
                "force_insert": self.force_insert,
                "force_update": self.force_update,
            }

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
class GetOrCreate(Operation):
    """The row of ``query`` matching ``lookups``, or a new row when there is none.

    The new row has the values of the ``lookups`` whose keys have no ``__``, updated with ``defaults``. Result:
    ``(row, created)``.

    ``lookups`` and ``defaults`` are mappings or pairs; lookup values are prepared as for ``filter()``. The query is
    not sliced and a default refers to no field and contains no aggregate; otherwise ``TypeError``.
    """

    kind: ClassVar[str] = "get_or_create"
    query: Query
    lookups: tuple[tuple[str, Any], ...] = ()
    defaults: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        _check_query(self.query)
        if self.query.is_sliced:
            raise TypeError("cannot filter a query once a slice has been taken")
        object.__setattr__(self, "lookups", _lookups(self.lookups))
        object.__setattr__(self, "defaults", _field_values(self.defaults, refs=False))

    @property
    def model(self) -> str:
        """The label of the queried model."""
        return self.query.model

    def nodes(self) -> Iterator[Node]:
        yield self.query
        yield from (build_lookup(key, value) for key, value in self.lookups)
        yield from (value for _, value in self.defaults if isinstance(value, Node))

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
    def _decode(cls, decoder: Decoder, data: Mapping[str, Any]) -> Operation:
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
        return cls(query, tuple(lookups), defaults)


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
