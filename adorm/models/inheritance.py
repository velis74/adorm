"""Multi-table inheritance: the parent link of a child model and the values of the child's own fields."""

from __future__ import annotations

import functools

from collections.abc import Mapping
from typing import Annotated, Any, TYPE_CHECKING

from adorm.models.relations import ForeignKey

if TYPE_CHECKING:
    from adorm.models.base import Model

__all__ = ["child_values", "own_fields", "parent_link"]


def parent_link(model: type[Model]) -> ForeignKey | None:
    """The `ForeignKey` stored in the primary key field of ``model`` (the link of a multi-table inheritance child to
    its parent), ``None`` when the primary key is not a relation's column."""
    pk = model._require_meta().pk
    return next(
        (r for r in model.__adorm_relations__.values() if isinstance(r, ForeignKey) and r.column == pk),
        None,
    )


def own_fields(model: type[Model], parent: type[Model]) -> list[str]:
    """The fields of ``model`` that ``parent`` does not have, the primary key excepted."""
    pk = model._require_meta().pk
    return [name for name in model.__pydantic_fields__ if name != pk and name not in parent.__pydantic_fields__]


@functools.lru_cache(maxsize=1024)
def _adapter(model: type[Model], name: str) -> Any:
    from pydantic import TypeAdapter

    info = model.__pydantic_fields__[name]
    return TypeAdapter(Annotated[(info.annotation, *info.metadata)] if info.metadata else info.annotation)


def child_values(model: type[Model], values: Mapping[str, Any]) -> tuple[ForeignKey, type[Model], dict[str, Any]]:
    """The parent link of ``model``, the parent model class and ``values`` validated as the child's own fields (a
    `ForeignKey` of the child given by name stands for its column, set to the instance's primary key).

    Raises ``TypeError`` when ``model`` is not a multi-table inheritance child and ``ValueError`` for a name that is not
    one of the child's own fields or relations and for a value its field type rejects.
    """
    from pydantic import ValidationError

    from adorm.models.base import Model

    label = model._require_meta().label
    link = parent_link(model)
    if link is None:
        raise TypeError(f"{label} is not a multi-table inheritance child: its primary key is not a parent link")
    parent = link.target_model()
    own = own_fields(model, parent)
    relations = {
        name: relation
        for name, relation in model.__adorm_relations__.items()
        if isinstance(relation, ForeignKey) and relation.column in own
    }
    stored: dict[str, Any] = {}
    for name, value in values.items():
        relation = relations.get(name)
        if relation is not None:
            if isinstance(value, Model):
                relation._check_target(value)
                value = value.pk
            name = relation.column
        if name not in own:
            where = "an inherited field" if name in model.__pydantic_fields__ or name == "pk" else "not a field"
            raise ValueError(f"{name!r} is {where} of {label}: give the child's own fields")
        if name in stored:
            raise ValueError(f"the field {name!r} is given twice")
        try:
            stored[name] = _adapter(model, name).validate_python(value)
        except ValidationError as exc:
            messages = "; ".join(error["msg"] for error in exc.errors())
            raise ValueError(f"{name!r}: {messages}") from None
    return link, parent, stored
