"""`ModelMeta`: what adorm knows about a model beyond its fields."""

from __future__ import annotations

from dataclasses import dataclass

from adorm.query.query import check_model_label

__all__ = ["ModelMeta"]


@dataclass(frozen=True)
class ModelMeta:
    """The identity of a model: its ``label`` (``"app_label.ModelName"``) and the name of its primary key field.

    A model class declares it as ``_meta``; a class without one is abstract (it cannot load or save rows).
    """

    label: str
    pk: str = "id"

    def __post_init__(self) -> None:
        check_model_label(self.label)
        if not isinstance(self.pk, str) or not self.pk.isidentifier():
            raise ValueError(f"{self.pk!r} is not a field name")
