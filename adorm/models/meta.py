"""`ModelMeta`: what adorm knows about a model beyond its fields."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from adorm.query.query import check_model_label

__all__ = ["ModelMeta"]


@dataclass(frozen=True)
class ModelMeta:
    """The identity of a model: its ``label`` (``"app_label.ModelName"``) and the name of its primary key field.

    ``schema_hash`` is the hash `adorm generate` computed from the ORM model, ``None`` for a model written by hand.
    ``orm_names`` maps a field or relation name to the target ORM's name where the two differ (a generated model
    renames names that clash with pydantic); a model class's ``_meta`` also holds the entries of its model bases for
    the fields and relations it inherits. ``concrete_model`` is the label of the concrete model a proxy model stands
    for (Django's ``concrete_model``), ``None`` for a concrete model. ``delete_models`` holds the labels of the models
    whose tables deleting rows of this model reads or writes (the target ORM's deletion collector, followed by model,
    the model itself included), any iterable of labels stored as a ``frozenset``; ``None`` when they are unknown (a
    model written by hand). A model class declares it as ``_meta``; a class without one is abstract (it cannot load or
    save rows).
    """

    label: str
    pk: str = "id"
    schema_hash: str | None = field(default=None, kw_only=True)
    orm_names: Mapping[str, str] | None = field(default=None, kw_only=True, hash=False)
    concrete_model: str | None = field(default=None, kw_only=True)
    delete_models: Iterable[str] | None = field(default=None, kw_only=True, hash=False)

    def __post_init__(self) -> None:
        check_model_label(self.label)
        if not isinstance(self.pk, str) or not self.pk.isidentifier():
            raise ValueError(f"{self.pk!r} is not a field name")
        if self.schema_hash is not None and (not isinstance(self.schema_hash, str) or not self.schema_hash):
            raise ValueError(f"{self.schema_hash!r} is not a schema hash")
        if self.concrete_model is not None:
            check_model_label(self.concrete_model)
            if self.concrete_model == self.label:
                raise ValueError(f"{self.label} cannot be a proxy of itself")
        if self.delete_models is not None:
            if isinstance(self.delete_models, str) or not isinstance(self.delete_models, Iterable):
                raise TypeError("delete_models is an iterable of model labels, not a string")
            object.__setattr__(self, "delete_models", frozenset(check_model_label(m) for m in self.delete_models))
        if self.orm_names is None:
            object.__setattr__(self, "orm_names", MappingProxyType({}))
        if not isinstance(self.orm_names, Mapping):
            raise TypeError(f"orm_names is a mapping, not {type(self.orm_names).__name__}")
        for name, orm_name in self.orm_names.items():
            if not isinstance(name, str) or not name.isidentifier():
                raise ValueError(f"{name!r} is not a field name")
            if not isinstance(orm_name, str) or not orm_name:
                raise ValueError(f"{orm_name!r} is not an ORM name")
        object.__setattr__(self, "orm_names", MappingProxyType(dict(self.orm_names)))

    def orm_name(self, name: str) -> str:
        """The target ORM's name of the field or relation ``name``."""
        return self.orm_names.get(name, name)
