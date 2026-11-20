"""JSON-safe encoding of query nodes and operations, with every value encoded by its type.

The encoded forms:

- a node: ``{"node": <node name>, <field>: <encoded field>, ...}``;
- a value whose position the schema types: ``{"value": <JSON>}``, encoded and decoded with a pydantic
  ``TypeAdapter`` of that type;
- a value whose position the schema does not type: ``{"type": <type name>, "value": <JSON>}``, the type inferred
  from the value (`TYPES`, or a container of encoded values);
- a value of a union whose members can have the same JSON form: ``{"branch": <member index>, "value": <JSON>}`` in
  place of the member's JSON, at any depth of the type;
- the values of ``in``, ``range`` and ``overlap``: ``{"items": [<node or value>, ...]}``.

A position is typed by the field a value is compared with or written to (see `adorm.schema`), or by a `Value`'s
``output_type``. Decoding resolves the same positions, so it needs the schema the data was encoded with.
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import decimal
import functools
import json
import operator
import types
import uuid

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, get_args, get_origin, Literal, Union

import pydantic

from pydantic import PlainSerializer, PlainValidator, TypeAdapter

from adorm.query.expressions import (
    Case,
    Expression,
    F,
    Func,
    Node,
    OuterRef,
    split_field_path,
    Subquery,
    Value,
    When,
)
from adorm.query.lookups import Lookup, LOOKUPS
from adorm.query.query import check_model_label, Query
from adorm.schema import resolve_field_type, SchemaSource

__all__ = [
    "TYPES",
    "Decoder",
    "Encoder",
    "adapter_for",
    "decode_node",
    "decode_value",
    "encode_node",
    "encode_value",
    "node_name",
    "type_from_name",
    "type_name",
]

TYPES: Mapping[str, type] = types.MappingProxyType(
    {
        "bool": bool,
        "int": int,
        "float": float,
        "str": str,
        "decimal": decimal.Decimal,
        "datetime": datetime.datetime,
        "date": datetime.date,
        "time": datetime.time,
        "timedelta": datetime.timedelta,
        "uuid": uuid.UUID,
        "bytes": bytes,
    }
)
"""The scalar types a value without a schema type may have, by type name; checked in this order."""

_CONTAINERS: dict[str, type] = {"list": list, "tuple": tuple, "set": set, "frozenset": frozenset, "dict": dict}


def type_name(tp: type) -> str:
    """The name of ``tp`` in `TYPES` or of a container type (``list``, ``tuple``, ``set``, ``frozenset``, ``dict``)."""
    for name, known in (*TYPES.items(), *_CONTAINERS.items()):
        if tp is known:
            return name
    raise TypeError(f"{tp!r} has no type name; output types are {', '.join([*TYPES, *_CONTAINERS])}")


def type_from_name(name: Any) -> type:
    """The type `type_name` names."""
    tp = (TYPES.get(name) or _CONTAINERS.get(name)) if isinstance(name, str) else None
    if tp is None:
        raise ValueError(f"unknown type name {name!r}")
    return tp


def _b64decode(value: Any) -> bytes:
    if isinstance(value, bytes | bytearray):
        return bytes(value)
    if not isinstance(value, str):
        raise ValueError("bytes are encoded as a base64 string")
    return base64.b64decode(value, validate=True)


def _b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


_BYTES = Annotated[bytes, PlainValidator(_b64decode), PlainSerializer(_b64encode, when_used="json")]


_ENCODING = "adorm.codec.encoding"
"""The validation context key under which `encode_value` validates a value before it encodes it."""


class _NotTextError(Exception):
    """Bytes that a model without base64 config cannot encode as UTF-8 text, raised past pydantic's validators."""


@dataclass(frozen=True)
class _Branch:
    index: int
    data: Any


_JSON_KINDS = frozenset({"string", "number", "boolean", "array", "object", "null"})


def _json_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    return "array" if isinstance(value, list) else "object"


def _schema_kinds(schema: Any, defs: Mapping[str, Any]) -> tuple[frozenset[str], tuple[str, ...] | None]:
    """The JSON kinds a JSON schema admits, and its JSON values when it is an enumeration."""
    if not isinstance(schema, Mapping):
        return _JSON_KINDS, None
    if "$ref" in schema:
        return _schema_kinds(defs.get(schema["$ref"].rsplit("/", 1)[-1]), defs)
    if "const" in schema or "enum" in schema:
        values = [schema["const"]] if "const" in schema else list(schema["enum"])
        return frozenset(map(_json_kind, values)), tuple(json.dumps(value, sort_keys=True) for value in values)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            parts = [_schema_kinds(part, defs) for part in schema[key]]
            values = None if any(v is None for _, v in parts) else tuple(x for _, v in parts for x in v or ())
            return frozenset().union(*(k for k, _ in parts)), values
    if "allOf" in schema and len(schema["allOf"]) == 1:
        return _schema_kinds(schema["allOf"][0], defs)
    kind = schema.get("type")
    if kind is None:
        return _JSON_KINDS, None
    kinds = {kind} if isinstance(kind, str) else set(kind)
    return frozenset("number" if k == "integer" else k for k in kinds), None


def _member_kinds(hint: Any) -> tuple[frozenset[str], tuple[str, ...] | None]:
    try:
        schema = TypeAdapter(hint).json_schema(mode="serialization")
    except Exception:  # noqa: BLE001 - a type without a JSON schema may encode as anything
        return _JSON_KINDS, None
    return _schema_kinds(schema, schema.get("$defs", {}))


def _ambiguous(members: tuple[Any, ...]) -> bool:
    """Whether the JSON forms of two of ``members`` can coincide: they share a JSON kind and are not enumerations
    of different values."""
    kinds = [_member_kinds(member) for member in members]
    for i, (kinds_a, values_a) in enumerate(kinds):
        for kinds_b, values_b in kinds[i + 1 :]:
            if kinds_a & kinds_b and (values_a is None or values_b is None or set(values_a) & set(values_b)):
                return True
    return False


_value_adapters: dict[Any, TypeAdapter] = {}


def _value_adapter(hint: Any) -> TypeAdapter:
    """The cached ``TypeAdapter`` of ``hint`` itself, which validates Python values without the transport forms."""
    try:
        adapter = _value_adapters.get(hint)
    except TypeError:
        return TypeAdapter(hint)
    if adapter is None:
        adapter = _value_adapters[hint] = TypeAdapter(hint)
    return adapter


def _select_branch(value: Any, members: tuple[Any, ...]) -> tuple[int, Any]:
    """The index of the member ``value`` belongs to, and ``value`` validated by it: the first member that takes it
    strictly with its own type, else the first that takes it strictly, else the first that takes it."""
    strict = None
    for index, member in enumerate(members):
        try:
            result = _value_adapter(member).validate_python(value, strict=True)
        except (pydantic.ValidationError, ValueError, TypeError):
            continue
        if type(result) is type(value):
            return index, result
        if strict is None:
            strict = index, result
    if strict is not None:
        return strict
    first_error = None
    for index, member in enumerate(members):
        try:
            return index, _value_adapter(member).validate_python(value)
        except pydantic.ValidationError as exc:
            first_error = first_error or exc
    raise first_error  # type: ignore[misc]


def _encode_branch(value: Any, members: tuple[Any, ...]) -> _Branch:
    index, validated = _select_branch(value, members)
    try:
        return _Branch(index, adapter_for(members[index]).dump_python(validated, mode="json"))
    except UnicodeDecodeError as exc:
        raise _NotTextError from exc


def _tagged_union(members: tuple[Any, ...], optional: bool) -> Any:
    """A union whose JSON form records its member: ``{"branch": <index in members>, "value": <member's JSON>}``."""

    def validate(value: Any, info: pydantic.ValidationInfo) -> Any:
        if value is None and optional:
            return None
        if info.context and info.context.get(_ENCODING):
            return _encode_branch(value, members)
        if not isinstance(value, Mapping) or set(value) != {"branch", "value"}:
            raise ValueError('a union value is encoded as {"branch": <index>, "value": <value>}')
        index = value["branch"]
        if type(index) is not int or not 0 <= index < len(members):
            raise ValueError(f"{index!r} is not a branch of the union")
        return adapter_for(members[index]).validate_python(value["value"])

    def serialize(value: Any) -> Any:
        if value is None:
            return None
        branch = value if isinstance(value, _Branch) else _encode_branch(value, members)
        return {"branch": branch.index, "value": branch.data}

    return Annotated[Any, PlainValidator(validate), PlainSerializer(serialize, when_used="json")]


def _is_discriminator(item: Any) -> bool:
    return isinstance(item, pydantic.Discriminator) or getattr(item, "discriminator", None) is not None


def _tag_union(hint: Any, metadata: tuple[Any, ...]) -> Any:
    """The tagged form of the union ``hint`` with ``metadata`` on each member, or ``None`` when its members' JSON
    forms cannot coincide."""
    args = get_args(hint)
    members = tuple(arg for arg in args if arg is not type(None))
    if len(members) < 2 or not _ambiguous(members):
        return None
    if metadata:
        members = tuple(Annotated[(member, *metadata)] for member in members)
    return _tagged_union(members, len(members) < len(args))


def _transport_type(hint: Any, *, tag: bool = True) -> Any:
    """``hint`` with ``bytes`` encoded as base64 at any depth of ``Annotated``, unions and generic containers, and
    with each union whose members' JSON forms can coincide (`_ambiguous`) encoded with the member a value belongs to.

    The metadata of an ``Annotated`` tagged union applies to each member; a discriminated union is not tagged.
    Bytes inside a pydantic model, dataclass or ``TypedDict`` follow that type's own config (``ser_json_bytes``).
    """
    if hint is bytes:
        return _BYTES
    origin = get_origin(hint)
    if origin is Annotated:
        inner, metadata = hint.__origin__, hint.__metadata__
        if get_origin(inner) in (Union, types.UnionType):
            if any(_is_discriminator(item) for item in metadata):
                return Annotated[(_transport_type(inner, tag=False), *metadata)]
            tagged = _tag_union(inner, metadata)
            if tagged is not None:
                return tagged
        transported = _transport_type(inner)
        return hint if transported is inner else Annotated[(transported, *metadata)]
    if origin is None or origin is Literal:
        return hint
    args = get_args(hint)
    if tag and origin in (Union, types.UnionType):
        tagged = _tag_union(hint, ())
        if tagged is not None:
            return tagged
    replaced = tuple(arg if arg is Ellipsis else _transport_type(arg) for arg in args)
    if replaced == args:
        return hint
    if origin in (Union, types.UnionType):
        return functools.reduce(operator.or_, replaced)
    try:
        return origin[replaced if len(replaced) > 1 else replaced[0]]
    except TypeError:
        return hint


_adapters: dict[Any, TypeAdapter] = {}


def adapter_for(hint: Any) -> TypeAdapter:
    """The cached ``TypeAdapter`` values of type ``hint`` are encoded and decoded with."""
    try:
        adapter = _adapters.get(hint)
    except TypeError:
        return TypeAdapter(_transport_type(hint))
    if adapter is None:
        adapter = _adapters[hint] = TypeAdapter(_transport_type(hint))
    return adapter


def _hint_repr(hint: Any) -> str:
    return getattr(hint, "__name__", None) or repr(hint)


def _infer_type_name(value: Any) -> str | None:
    for name, tp in TYPES.items():
        if isinstance(value, tp):
            return name
    return None


def _check_json(data: Any, value: Any) -> Any:
    try:
        json.dumps(data, allow_nan=False)
    except ValueError:
        raise ValueError(f"{value!r} is not JSON compliant: NaN and infinity have no JSON form") from None
    return data


def encode_value(value: Any, hint: Any = None) -> dict[str, Any]:
    """``value`` as ``{"value": ...}`` encoded with the type ``hint``, or with its inferred type when ``hint`` is
    ``None``. ``None`` encodes as ``{"value": None}`` for any type.

    Raises ``ValueError`` when ``value`` is not valid for ``hint``, contains NaN or infinity, or contains bytes that a
    model without ``ser_json_bytes="base64"`` cannot encode as UTF-8 text; ``TypeError`` when ``hint`` is ``None`` and
    the value's type is not in `TYPES` or a container of such values.
    """
    if value is None:
        return {"value": None}
    if hint is None or hint is Any:
        return _check_json(_encode_inferred(value), value)
    adapter = adapter_for(hint)
    try:
        data = adapter.dump_python(adapter.validate_python(value, context={_ENCODING: True}), mode="json")
    except pydantic.ValidationError as exc:
        raise ValueError(f"{value!r} is not a valid {_hint_repr(hint)}: {exc.errors()[0]['msg']}") from None
    except (UnicodeDecodeError, _NotTextError):
        raise ValueError(
            f"{value!r} holds bytes that are not UTF-8 text: give the model, dataclass or TypedDict "
            "ser_json_bytes='base64' and val_json_bytes='base64'"
        ) from None
    return _check_json({"value": data}, value)


def _encode_inferred(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("a dict value without a schema type needs string keys")
        return {"type": "dict", "value": {key: encode_value(item) for key, item in value.items()}}
    if isinstance(value, list | tuple | set | frozenset):
        items = [encode_value(item) for item in value]
        if isinstance(value, set | frozenset):
            items.sort(key=lambda item: json.dumps(item, sort_keys=True))
        name = next(n for n, tp in reversed(_CONTAINERS.items()) if isinstance(value, tp))
        return {"type": name, "value": items}
    name = _infer_type_name(value)
    if name is None:
        raise TypeError(f"a {type(value).__name__} value needs a type: a schema field type or a Value with output_type")
    return {"type": name, "value": adapter_for(TYPES[name]).dump_python(value, mode="json")}


def decode_value(data: Any, hint: Any = None) -> Any:
    """The value `encode_value` encoded; ``hint`` is the type it was encoded with, unless the data names its type."""
    if not isinstance(data, Mapping) or "value" not in data:
        raise ValueError(f"an encoded value is a mapping with 'value', not {data!r}")
    raw = data["value"]
    name = data.get("type")
    if name is not None:
        return _decode_inferred(name, raw)
    if raw is None:
        return None
    if hint is None or hint is Any:
        raise ValueError("the value has no type: decode it with the schema it was encoded with")
    try:
        return adapter_for(hint).validate_python(raw)
    except pydantic.ValidationError as exc:
        raise ValueError(f"{raw!r} is not a valid {_hint_repr(hint)}: {exc.errors()[0]['msg']}") from None


def _decode_inferred(name: Any, raw: Any) -> Any:
    tp = type_from_name(name)
    if tp is dict:
        if not isinstance(raw, Mapping):
            raise ValueError("an encoded dict is a mapping")
        return {key: decode_value(item) for key, item in raw.items()}
    if name in _CONTAINERS:
        if not isinstance(raw, list):
            raise ValueError(f"an encoded {name} is a list")
        return tp(decode_value(item) for item in raw)
    try:
        return adapter_for(tp).validate_python(raw, strict=tp is not bytes and not isinstance(raw, str))
    except pydantic.ValidationError as exc:
        raise ValueError(f"{raw!r} is not a valid {name}: {exc.errors()[0]['msg']}") from None


def node_name(cls: type[Node]) -> str:
    """The name a node class is encoded under.

    ``lookup.<lookup name>`` for a registered lookup, the class name for adorm's own nodes and
    ``<module>.<qualified name>`` for every other class.
    """
    if issubclass(cls, Lookup) and cls.lookup_name is not None and LOOKUPS.get(cls.lookup_name) is cls:
        return f"lookup.{cls.lookup_name}"
    if cls.__module__ == "adorm" or cls.__module__.startswith("adorm."):
        return cls.__name__
    return f"{cls.__module__}.{cls.__qualname__}"


def _node_classes() -> dict[str, type[Node] | None]:
    classes: dict[str, type[Node] | None] = {}
    pending = [Node]
    seen: set[type] = set()
    while pending:
        cls = pending.pop()
        for sub in cls.__subclasses__():
            if sub in seen:
                continue
            seen.add(sub)
            pending.append(sub)
            name = node_name(sub)
            classes[name] = None if name in classes else sub
    return classes


@dataclass
class _Scope:
    model: str
    annotations: dict[str, Expression] = field(default_factory=dict)


class _Codec:
    def __init__(self, schema: SchemaSource | None) -> None:
        self.schema = schema
        self._scopes: list[_Scope] = []
        self._resolving: set[tuple[int, str]] = set()

    @contextlib.contextmanager
    def scope(self, model: str, annotations: Iterable[tuple[str, Expression]] = ()) -> Iterator[None]:
        """Resolve field paths against ``model`` and the annotation names of ``annotations`` inside the block."""
        self._scopes.append(_Scope(check_model_label(model), dict(annotations)))
        try:
            yield
        finally:
            self._scopes.pop()

    def field_type(self, path: str, *, outer: bool = False) -> Any:
        """The type of the field path in the current model (``outer``: the enclosing query's); ``None`` if unknown.

        A one-segment path naming an annotation has the annotation's type (`expression_type`). Raises
        `adorm.FieldError` for a path the schema of a known model does not resolve.
        """
        return self._field_type(len(self._scopes) - (1 if outer else 0), path)

    def _field_type(self, depth: int, path: str) -> Any:
        if depth <= 0:
            return None
        scope = self._scopes[depth - 1]
        segments = split_field_path(path)
        if segments[0] in scope.annotations:
            key = (depth, segments[0])
            if len(segments) > 1 or key in self._resolving:
                return None
            self._resolving.add(key)
            try:
                return self._expression_type(depth, scope.annotations[segments[0]])
            finally:
                self._resolving.discard(key)
        return resolve_field_type(self.schema, scope.model, segments)

    def expression_type(self, expression: Any) -> Any:
        """The type of ``expression``'s values: its field's type for `F` and `OuterRef`, else its ``output_type``,
        else the type a function's ``output_from`` gives (see `Func`), the first known type of a `Case` result, or the
        type of the field a one-column `Subquery` selects."""
        return self._expression_type(len(self._scopes), expression)

    def _expression_type(self, depth: int, expression: Any) -> Any:
        if isinstance(expression, OuterRef):
            return self._field_type(depth - expression.levels, expression.name)
        if isinstance(expression, F):
            return self._field_type(depth, expression.name)
        output_type = getattr(expression, "output_type", None)
        if output_type is not None:
            return output_type
        candidates: tuple[Any, ...] = ()
        output_from = getattr(expression, "output_from", None) if isinstance(expression, Func) else None
        if output_from == "arguments":
            candidates = expression.expressions
        elif output_from == "first":
            candidates = expression.expressions[:1]
        elif output_from == "numeric":
            argument_types = [self._expression_type(depth, argument) for argument in expression.expressions]
            return decimal.Decimal if decimal.Decimal in argument_types else float
        elif isinstance(expression, Case):
            candidates = (*(case.result for case in expression.cases), expression.default)
        elif isinstance(expression, Subquery):
            query = expression.query
            names = {name for name, _ in query.annotations}
            if len(query.select) == 1 and split_field_path(query.select[0])[0] not in names:
                return resolve_field_type(self.schema, query.model, split_field_path(query.select[0]))
        for candidate in candidates:
            found = self._expression_type(depth, candidate)
            if found is not None:
                return found
        return None

    def lookup_value_type(self, lookup: type[Lookup], lhs: Any) -> Any:
        """The type of a right-hand side value of the lookup class ``lookup`` applied to ``lhs``."""
        return lookup.rhs_type(self.expression_type(lhs))

    @staticmethod
    def child_hint(cls: type[Node], output_type: Any, name: str, hint: Any) -> Any:
        """The type of the values in the attribute ``name`` of a ``cls`` node whose position has the type ``hint``:
        the results of `Case` and `When` and the arguments of a function whose ``output_from`` is ``"arguments"``
        (`Coalesce`, `Greatest` …) have their node's type."""
        if (issubclass(cls, Case) and name in ("cases", "default")) or (
            issubclass(cls, Func) and cls.output_from == "arguments" and name == "expressions"
        ):
            return output_type or hint
        if issubclass(cls, When) and name == "result":
            return hint
        return None


class Encoder(_Codec):
    """Encodes nodes and operands for one top-level value; resolves field types against ``schema``."""

    def node(self, node: Node, hint: Any = None) -> dict[str, Any]:
        """``node`` and its descendants as JSON-safe data. ``hint`` is the type of the node's position; it types a
        `Value` without ``output_type`` there and in the children `child_hint` passes it to."""
        if not isinstance(node, Node):
            raise TypeError(f"expected a node, not {type(node).__name__}")
        if isinstance(node, Query):
            return self._query(node)
        data: dict[str, Any] = {"node": node_name(type(node))}
        output_type = getattr(node, "output_type", None) if "output_type" in node._fields else None
        if output_type is not None:
            type_name(output_type)
        for name in node._fields:
            data[name] = self._field(node, name, getattr(node, name), hint)
        return data

    def _query(self, query: Query) -> dict[str, Any]:
        data: dict[str, Any] = {"node": node_name(type(query))}
        # The combined queries are at the level of the query itself: an OuterRef in them refers to the enclosing query.
        combined = [self.node(part) for part in query.combined_queries]
        with self.scope(query.model):
            scope = self._scopes[-1]
            annotations = []
            for name, expression in query.annotations:
                annotations.append([name, self.node(expression)])
                scope.annotations[name] = expression
            for name in query._fields:
                if name == "annotations":
                    data[name] = annotations
                elif name == "combined_queries":
                    data[name] = combined
                else:
                    data[name] = self._field(query, name, getattr(query, name))
        return data

    def _field(self, node: Node, name: str, value: Any, hint: Any = None) -> Any:
        if name == "output_type":
            return None if value is None else type_name(value)
        if isinstance(node, Value) and name == "value":
            return encode_value(value, node.output_type or hint)
        if isinstance(node, Lookup) and name == "rhs":
            if node.rhs_is_sequence and isinstance(value, Node):
                return self.node(value, self.expression_type(node.lhs))  # one node in place of the values
            rhs_hint = self.lookup_value_type(type(node), node.lhs)
            return self.operand(value, rhs_hint, sequence=node.rhs_is_sequence)
        return self._plain(value, self.child_hint(type(node), getattr(node, "output_type", None), name, hint))

    def operand(self, value: Any, hint: Any = None, *, sequence: bool = False) -> Any:
        """A node, or a value encoded with ``hint``; with ``sequence``, a tuple of them as ``{"items": [...]}``."""
        if isinstance(value, Node):
            return self.node(value, hint)
        if sequence and isinstance(value, tuple | list):
            return {"items": [self.operand(item, hint) for item in value]}
        return encode_value(value, hint)

    def _plain(self, value: Any, hint: Any = None) -> Any:
        if isinstance(value, Node):
            return self.node(value, hint)
        if isinstance(value, tuple):
            return [self._plain(item, hint) for item in value]
        if value is None or isinstance(value, bool | int | str):
            return value
        raise TypeError(f"cannot encode a {type(value).__name__} node attribute")


class Decoder(_Codec):
    """Decodes what `Encoder` produced; ``schema`` must type values as it did when they were encoded."""

    def __init__(self, schema: SchemaSource | None) -> None:
        super().__init__(schema)
        self._classes = _node_classes()

    def node(self, data: Any, hint: Any = None) -> Node:
        """The node ``data`` encodes; ``hint`` as for `Encoder.node`. Raises ``ValueError`` for malformed data, an
        unknown node name or a node its constructor would refuse."""
        if not isinstance(data, Mapping) or not isinstance(data.get("node"), str):
            raise ValueError(f"an encoded node is a mapping with 'node', not {data!r}")
        name = data["node"]
        if name not in self._classes:
            raise ValueError(f"unknown node {name!r}")
        cls = self._classes[name]
        if cls is None:
            raise ValueError(f"the node name {name!r} matches more than one class")
        if issubclass(cls, Query):
            return self._query(cls, data)
        values: dict[str, Any] = {}
        names = sorted(cls._fields, key=lambda n: n != "output_type")
        for field_name in names:
            values[field_name] = self._field(cls, values, field_name, self._get(data, field_name), hint)
        return self._build(cls, {n: values[n] for n in cls._fields})

    @staticmethod
    def _get(data: Mapping[str, Any], name: str) -> Any:
        if name not in data:
            raise ValueError(f"the encoded {data.get('node')} has no {name!r}")
        return data[name]

    @staticmethod
    def _build(cls: type[Node], values: dict[str, Any]) -> Node:
        node = cls.__new__(cls)
        node._init(**values)
        try:
            node._check_decoded()
        except TypeError as exc:
            raise ValueError(f"invalid {cls.__name__}: {exc}") from None
        return node

    def _query(self, cls: type[Query], data: Mapping[str, Any]) -> Query:
        model = check_model_label(self._get(data, "model"))
        combined = self._get(data, "combined_queries")
        if not isinstance(combined, list):
            raise ValueError("the combined queries of a query are a list")
        values: dict[str, Any] = {"model": model, "combined_queries": tuple(self.node(part) for part in combined)}
        with self.scope(model):
            scope = self._scopes[-1]
            raw = self._get(data, "annotations")
            if not isinstance(raw, list):
                raise ValueError("the annotations of a query are a list")
            annotations = []
            for item in raw:
                if not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str):
                    raise ValueError("an annotation is a [name, expression] pair")
                expression = self.node(item[1])
                if not isinstance(expression, Expression):
                    raise ValueError(f"the annotation {item[0]!r} is not an expression")
                annotations.append((item[0], expression))
                scope.annotations[item[0]] = expression
            values["annotations"] = tuple(annotations)
            for name in cls._fields:
                if name not in values:
                    values[name] = self._plain(self._get(data, name))
        return self._build(cls, {n: values[n] for n in cls._fields})

    def _field(self, cls: type[Node], values: dict[str, Any], name: str, raw: Any, hint: Any = None) -> Any:
        if name == "output_type":
            return None if raw is None else type_from_name(raw)
        if issubclass(cls, Value) and name == "value":
            return decode_value(raw, values.get("output_type") or hint)
        if issubclass(cls, Lookup) and name == "rhs":
            lhs = values.get("lhs")
            if not isinstance(lhs, Expression):
                raise ValueError(f"the lhs of a lookup is an expression, not {type(lhs).__name__}")
            if cls.rhs_is_sequence and isinstance(raw, Mapping) and "node" in raw:
                return self.node(raw, self.expression_type(lhs))
            rhs_hint = self.lookup_value_type(cls, lhs)
            return self.operand(raw, rhs_hint, sequence=cls.rhs_is_sequence)
        return self._plain(raw, self.child_hint(cls, values.get("output_type"), name, hint))

    def operand(self, data: Any, hint: Any = None, *, sequence: bool = False) -> Any:
        """The node or value `Encoder.operand` encoded."""
        if isinstance(data, Mapping) and "node" in data:
            return self.node(data, hint)
        if sequence and isinstance(data, Mapping) and "items" in data:
            if not isinstance(data["items"], list):
                raise ValueError("encoded items are a list")
            return tuple(self.operand(item, hint) for item in data["items"])
        return decode_value(data, hint)

    def _plain(self, raw: Any, hint: Any = None) -> Any:
        if isinstance(raw, list):
            return tuple(self._plain(item, hint) for item in raw)
        if isinstance(raw, Mapping):
            return self.node(raw, hint)
        if raw is None or isinstance(raw, bool | int | str):
            return raw
        raise ValueError(f"cannot decode {raw!r} as a node attribute")


def encode_node(node: Node, schema: SchemaSource | None = None) -> dict[str, Any]:
    """``node`` as JSON-safe data, values encoded by the types ``schema`` gives them."""
    return Encoder(schema).node(node)


def decode_node(data: Any, schema: SchemaSource | None = None) -> Node:
    """The node `encode_node` encoded with the same ``schema``."""
    return Decoder(schema).node(data)
