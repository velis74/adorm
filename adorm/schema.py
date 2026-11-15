"""Model schemas: the field types and relations an operation's values are typed by.

A `ModelSchema` describes one model by its label: its primary key, the Python type of every field, the target
of every relation and the parameter types of its driver methods (`MethodSchema`). A `SchemaSource` returns the
schema of a label; `Schema` is a plain registry of them. The operation codec (`adorm.codec`) resolves field paths
against a source to encode each value by its field's type.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from adorm.query.lookups import _walk
from adorm.query.query import check_model_label

__all__ = ["MethodKey", "MethodSchema", "ModelSchema", "Schema", "SchemaSource", "resolve_field_type"]

MethodKey = tuple[str, str, str]
"""The key of a driver method in `ModelSchema.methods`: ``(manager, target, method)``, where ``manager`` is the ORM
manager name, ``target`` ``"manager"`` or ``"queryset"`` and ``method`` the method name."""


@dataclass(frozen=True, eq=False)
class MethodSchema:
    """The parameter types of a driver method, for the arguments of a `adorm.operations.Call`.

    ``positional`` holds the types of the parameters that take positional arguments, in order; ``keyword`` the types
    of the parameters that take keyword arguments, by name; ``var_positional`` and ``var_keyword`` the types of
    ``*args`` and ``**kwargs``. ``None`` is a parameter without a type: its values are encoded with their inferred
    type.
    """

    positional: tuple[Any, ...] = ()
    keyword: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    var_positional: Any = None
    var_keyword: Any = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "positional", tuple(self.positional))
        object.__setattr__(self, "keyword", MappingProxyType(dict(self.keyword)))

    def arg_type(self, index: int) -> Any:
        """The type of the positional argument at ``index``."""
        return self.positional[index] if index < len(self.positional) else self.var_positional

    def kwarg_type(self, name: str) -> Any:
        """The type of the keyword argument ``name``."""
        return self.keyword[name] if name in self.keyword else self.var_keyword


@dataclass(frozen=True, eq=False)
class ModelSchema:
    """The schema of the model ``label``.

    ``fields`` maps every concrete field name (a foreign key's column field too, e.g. ``club_id``) to its type hint;
    ``relations`` maps every relation name (forward, reverse and many-to-many) to the target model's label; ``pk``
    names the primary key field, which is one of ``fields``. ``orm_names`` maps a field or relation name to the target
    ORM's name where the two differ (`adorm.ModelMeta.orm_names`). ``methods`` maps the `MethodKey` of every driver
    method of the model's managers and query sets to its `MethodSchema`.
    """

    label: str
    pk: str
    fields: Mapping[str, Any]
    relations: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    orm_names: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}), kw_only=True)
    methods: Mapping[MethodKey, MethodSchema] = field(default_factory=lambda: MappingProxyType({}), kw_only=True)

    def __post_init__(self) -> None:
        check_model_label(self.label)
        object.__setattr__(self, "methods", MappingProxyType(dict(self.methods)))
        for key, method in self.methods.items():
            if not (isinstance(key, tuple) and len(key) == 3 and all(isinstance(part, str) for part in key)):
                raise ValueError(f"{key!r} is not a (manager, target, method) key")
            if key[1] not in ("manager", "queryset"):
                raise ValueError(f"the target of {key!r} is 'manager' or 'queryset'")
            if not isinstance(method, MethodSchema):
                raise TypeError(f"the schema of {key!r} is a MethodSchema, not {type(method).__name__}")
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))
        object.__setattr__(self, "relations", MappingProxyType(dict(self.relations)))
        object.__setattr__(self, "orm_names", MappingProxyType(dict(self.orm_names)))
        unknown = sorted(name for name in self.orm_names if name not in self.fields and name not in self.relations)
        if unknown:
            raise ValueError(f"orm_names of {self.label} names no field or relation: {', '.join(unknown)}")
        if self.pk not in self.fields:
            raise ValueError(f"the primary key {self.pk!r} of {self.label} is not one of its fields")
        for target in self.relations.values():
            check_model_label(target)
        clashes = sorted(set(self.fields) & set(self.relations))
        if clashes:
            raise ValueError(f"{self.label} has fields and relations with the same names: {', '.join(clashes)}")


@runtime_checkable
class SchemaSource(Protocol):
    """Returns the schema of a model label, ``None`` for a model it does not know."""

    def model_schema(self, label: str) -> ModelSchema | None: ...


class Schema:
    """A registry of `ModelSchema` values by label; a `SchemaSource`."""

    def __init__(self, models: Iterable[ModelSchema] = ()) -> None:
        self._models: dict[str, ModelSchema] = {}
        for model in models:
            self.add(model)

    def add(self, model: ModelSchema) -> None:
        """Register ``model``; replaces an earlier schema of the same label."""
        if not isinstance(model, ModelSchema):
            raise TypeError(f"expected a ModelSchema, not {type(model).__name__}")
        self._models[model.label] = model

    def model_schema(self, label: str) -> ModelSchema | None:
        return self._models.get(label)

    def __contains__(self, label: object) -> bool:
        return label in self._models

    def __iter__(self):
        return iter(self._models.values())

    def __len__(self) -> int:
        return len(self._models)


def resolve_field_type(source: SchemaSource | None, label: str, path: Sequence[str]) -> Any | None:
    """The type hint of the field at ``path`` (segments of a field path) from the model ``label``.

    Every segment but the last is a relation, until a JSON or record field, whose following segments are keys inside
    its value; ``pk`` stands for the primary key at any segment. A path that ends at a relation resolves to the
    target's primary key type. ``None`` when ``source`` is ``None``, does not know a model on the way, or the path
    continues into a JSON field's value. Raises `adorm.FieldError` for a segment that is neither a field nor a relation
    of a model ``source`` knows, and for segments after a field that is neither a relation nor a JSON field.
    """
    if source is None or not path:
        return None
    return _walk(source, label, tuple(path), "__".join(path), lookup_hint=False).field_type
