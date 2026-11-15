"""The model registry: model classes by label, and their schemas for the operation codec."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Any, TYPE_CHECKING

from adorm.schema import ModelSchema

if TYPE_CHECKING:
    from adorm.models.base import Model

__all__ = ["ModelRegistry", "model_schema", "registry"]


def model_schema(model: type[Model]) -> ModelSchema:
    """The `ModelSchema` of the model class ``model``: its label, primary key, field types, relation targets, ORM
    names and the `MethodSchema` of every driver method (`adorm.models.driver_methods()`).

    Raises ``TypeError`` for an abstract model (no ``_meta``).
    """
    from adorm.models.base import get_relations
    from adorm.models.managers import driver_methods

    meta = model._meta
    if meta is None:
        raise TypeError(f"{model.__name__} is abstract: it has no _meta")
    fields: dict[str, Any] = {}
    for name, info in model.__pydantic_fields__.items():
        hint = info.annotation
        fields[name] = Annotated[(hint, *info.metadata)] if info.metadata else hint
    relations = {name: relation.target for name, relation in get_relations(model).items()}
    methods = {key: method.schema for key, method in driver_methods(model).items()}
    return ModelSchema(meta.label, meta.pk, fields, relations, orm_names=meta.orm_names, methods=methods)


class ModelRegistry:
    """Model classes by label; a `SchemaSource` serving each registered model's `model_schema()`.

    A model class that declares ``_meta`` registers itself when it is created.
    """

    def __init__(self) -> None:
        self._models: dict[str, type[Model]] = {}
        self._schemas: dict[str, ModelSchema] = {}

    def register(self, model: type[Model]) -> None:
        """Register ``model`` under its label.

        A class with the same module and qualified name replaces the registered one (a reloaded module); another
        class with the same label raises ``ValueError``.
        """
        label = model._meta.label  # type: ignore[union-attr]
        current = self._models.get(label)
        if current is not None and current is not model:
            if (current.__module__, current.__qualname__) != (model.__module__, model.__qualname__):
                raise ValueError(
                    f"the label {label!r} is already registered by {current.__module__}.{current.__qualname__}"
                )
        self._models[label] = model
        self._schemas.pop(label, None)

    def unregister(self, label: str) -> None:
        """Remove the model registered under ``label``; an unknown label is ignored."""
        self._models.pop(label, None)
        self._schemas.pop(label, None)

    def get(self, label: str) -> type[Model]:
        """The model class registered under ``label``; ``LookupError`` when there is none."""
        try:
            return self._models[label]
        except KeyError:
            raise LookupError(f"no model is registered under {label!r}") from None

    def get_for_content_type(self, app_label: str, model_name: str) -> type[Model]:
        """The model class registered for a content type: the label's app part equals ``app_label`` and its model part,
        compared case-insensitively, ``model_name`` (Django's ``ContentType.model`` is the lowercased class name).
        ``LookupError`` when there is none."""
        wanted = model_name.casefold()
        for label, model in self._models.items():
            app, _, name = label.partition(".")
            if app == app_label and name.casefold() == wanted:
                return model
        raise LookupError(f"no model is registered for the content type {app_label}.{model_name}")

    def model_schema(self, label: str) -> ModelSchema | None:
        """The `ModelSchema` of the model registered under ``label``, ``None`` for an unknown label. It is cached
        once the model class is complete (its forward references resolved)."""
        model = self._models.get(label)
        if model is None:
            return None
        schema = self._schemas.get(label)
        if schema is None:
            schema = model_schema(model)
            if model.__pydantic_complete__:
                self._schemas[label] = schema
        return schema

    def __contains__(self, label: object) -> bool:
        return label in self._models

    def __iter__(self) -> Iterator[type[Model]]:
        return iter(list(self._models.values()))

    def __len__(self) -> int:
        return len(self._models)


registry = ModelRegistry()
"""The registry every model class with ``_meta`` registers in."""
