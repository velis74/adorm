"""Model schemas: the field types and relations an operation's values are typed by.

A `ModelSchema` describes one model by its label: its primary key, the Python type of every field and the target
of every relation. A `SchemaSource` returns the schema of a label; `Schema` is a plain registry of them. The
operation codec (`adorm.codec`) resolves field paths against a source to encode each value by its field's type.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from adorm.query.query import check_model_label

__all__ = ["ModelSchema", "Schema", "SchemaSource", "resolve_field_type"]


@dataclass(frozen=True, eq=False)
class ModelSchema:
    """The schema of the model ``label``.

    ``fields`` maps every concrete field name (a foreign key's column field too, e.g. ``club_id``) to its type hint;
    ``relations`` maps every relation name (forward, reverse and many-to-many) to the target model's label; ``pk``
    names the primary key field, which is one of ``fields``.
    """

    label: str
    pk: str
    fields: Mapping[str, Any]
    relations: Mapping[str, str] = MappingProxyType({})

    def __post_init__(self) -> None:
        check_model_label(self.label)
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))
        object.__setattr__(self, "relations", MappingProxyType(dict(self.relations)))
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

    Every segment but the last is a relation; ``pk`` stands for the primary key at any segment. A path that ends at a
    relation resolves to the target's primary key type. ``None`` when ``source`` is ``None``, does not know a model on
    the way, or the path continues past a field (a key of a JSON field). Raises ``ValueError`` for a segment that is
    neither a field nor a relation of a model ``source`` knows.
    """
    if source is None or not path:
        return None
    model = source.model_schema(label)
    for index, segment in enumerate(path):
        if model is None:
            return None
        if segment == "pk":
            segment = model.pk
        last = index == len(path) - 1
        if segment in model.fields:
            return model.fields[segment] if last else None
        target = model.relations.get(segment)
        if target is None:
            raise ValueError(f"{model.label} has no field or relation {segment!r}")
        model = source.model_schema(target)
        if last:
            return None if model is None else model.fields[model.pk]
    return None
