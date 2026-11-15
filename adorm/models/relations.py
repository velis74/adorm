"""Relation descriptors of models.

A relation is a class attribute, not a pydantic field: it is absent from validation, serialisation and the JSON
schema. On an instance, a `ForeignKey` or `ReverseOneToOne` returns an awaitable that loads the related object; a
`ReverseForeignKey` or `ManyToMany` returns a `QuerySet` of the related rows on the owner's executor.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any, TYPE_CHECKING

from adorm.exceptions import ObjectDoesNotExist
from adorm.query.query import check_model_label, Query

if TYPE_CHECKING:
    from adorm.models.base import Model

__all__ = ["ForeignKey", "ManyToMany", "RelatedObject", "Relation", "ReverseForeignKey", "ReverseOneToOne"]

_MISSING = object()


def _check_field_name(value: Any) -> str:
    if not isinstance(value, str) or not value.isidentifier():
        raise ValueError(f"{value!r} is not a field name")
    return value


class Relation:
    """Base class of relation descriptors. ``target`` is the label of the related model, ``name`` the attribute
    name the descriptor is assigned to. A bare ``Relation`` names the target only: query paths traverse it, but it
    carries no join columns."""

    name: str

    def __init__(self, target: str) -> None:
        self.target = check_model_label(target)

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    def target_model(self) -> type[Model]:
        """The registered model class of ``target``; ``LookupError`` when none is registered."""
        from adorm.models.registry import registry

        return registry.get(self.target)

    def _check_target(self, value: Any) -> None:
        from adorm.models.base import Model

        meta = getattr(type(value), "_meta", None) if isinstance(value, Model) else None
        if meta is None or meta.label != self.target:
            raise TypeError(f"{self.name} takes a {self.target} instance, not {type(value).__name__}")

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.target!r})"


class RelatedObject:
    """The awaitable a `ForeignKey` or `ReverseOneToOne` returns: ``await member.club`` gives the related instance.

    Nothing runs until it is awaited; each ``await`` checks the cache again.
    """

    __slots__ = ("instance", "relation")

    def __init__(self, relation: ForeignKey | ReverseOneToOne, instance: Model) -> None:
        self.relation = relation
        self.instance = instance

    def __await__(self) -> Generator[Any, None, Model | None]:
        return self.relation.load(self.instance).__await__()

    def __repr__(self) -> str:
        return f"<RelatedObject {type(self.instance).__name__}.{self.relation.name}>"


class ForeignKey(Relation):
    """A many-to-one (or one-to-one) relation stored in the field ``column`` (default ``<name>_id``).

    ``await instance.rel`` returns the related instance, ``None`` when ``column`` is ``None``. The instance is
    cached on the owner while its primary key equals ``column``; a row loaded with ``select_related()`` fills the
    cache. Assigning an instance sets ``column`` to its primary key and caches it; assigning ``None`` clears both.
    Reading the relation while ``column`` is deferred raises ``AttributeError``.
    """

    def __init__(self, target: str, *, column: str | None = None) -> None:
        super().__init__(target)
        self._column = None if column is None else _check_field_name(column)

    @property
    def column(self) -> str:
        """The name of the field holding the related primary key."""
        return self._column or f"{self.name}_id"

    def __get__(self, instance: Model | None, owner: type | None = None) -> Any:
        if instance is None:
            return self
        if self.column not in instance.__dict__:
            raise AttributeError(f"{type(instance).__name__}.{self.name} needs {self.column!r}, which is deferred")
        return RelatedObject(self, instance)

    def __set__(self, instance: Model, value: Any) -> None:
        if value is None:
            setattr(instance, self.column, None)
        else:
            self._check_target(value)
            pk = value.pk
            if pk is None:
                raise ValueError(f"cannot assign an unsaved {self.target} instance to {self.name}")
            setattr(instance, self.column, pk)
        instance._state.related[self.name] = value

    def cached(self, instance: Model) -> Any:
        """The cached related instance (or ``None``) while it matches ``column``; a sentinel otherwise."""
        value = instance.__dict__.get(self.column)
        cached = instance._state.related.get(self.name, _MISSING)
        if value is None:
            return None
        if cached is not _MISSING and cached is not None and cached.__dict__.get(cached._meta.pk) == value:
            return cached
        return _MISSING

    async def load(self, instance: Model) -> Model | None:
        """The related instance: from the cache, or loaded with a `Get` operation on the owner's executor and
        cached. A missing row raises the target's ``DoesNotExist``."""
        if self.column not in instance.__dict__:
            raise AttributeError(f"{type(instance).__name__}.{self.name} needs {self.column!r}, which is deferred")
        cached = self.cached(instance)
        if cached is not _MISSING:
            return cached
        return await _load_one(self, instance, Query(self.target).filter(pk=instance.__dict__[self.column]))


async def _load_one(relation: Relation, instance: Model, query: Query) -> Model:
    """The row of ``query`` as an instance of the relation's target, loaded on the owner's executor and cached."""
    from adorm.operations import Get

    target = relation.target_model()
    executor = instance._executor()
    try:
        row = await executor.execute(Get(query))
    except ObjectDoesNotExist as exc:
        if type(exc) is not ObjectDoesNotExist:
            raise
        raise target.DoesNotExist(*exc.args) from exc
    related = target.from_row(row, executor=executor)
    instance._state.related[relation.name] = related
    return related


def _related(relation: Relation, instance: Model, query: Query) -> Any:
    from adorm.query.queryset import QuerySet

    return QuerySet(relation.target_model(), instance._executor(), query)


def _owner_pk(relation: Relation, instance: Model) -> Any:
    pk = instance.pk
    if pk is None:
        raise ValueError(f"{type(instance).__name__}.{relation.name} needs a primary key, the instance has none")
    return pk


class ReverseForeignKey(Relation):
    """The reverse side of a `ForeignKey`: the rows of ``target`` whose field ``column`` holds the owner's primary
    key.

    On an instance it returns a `QuerySet` of ``Query(target).filter(<column>=instance.pk)`` on the owner's
    executor. An instance without a primary key value raises ``ValueError``.
    """

    def __init__(self, target: str, *, column: str) -> None:
        super().__init__(target)
        self.column = _check_field_name(column)

    def __get__(self, instance: Model | None, owner: type | None = None) -> Any:
        if instance is None:
            return self
        return _related(self, instance, Query(self.target).filter(**{self.column: _owner_pk(self, instance)}))

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.target!r}, column={self.column!r})"


class ReverseOneToOne(Relation):
    """The reverse side of a one-to-one `ForeignKey`: the row of ``target`` whose field ``column`` holds the owner's
    primary key.

    ``await instance.rel`` returns that row as an instance of ``target``, loaded with a `Get` operation on the owner's
    executor and cached on the owner; a row loaded with ``select_related()`` fills the cache. No such row raises the
    target's ``DoesNotExist`` (also when ``select_related()`` cached ``None``); an instance without a primary key value
    raises ``ValueError``.
    """

    def __init__(self, target: str, *, column: str) -> None:
        super().__init__(target)
        self.column = _check_field_name(column)

    def __get__(self, instance: Model | None, owner: type | None = None) -> Any:
        if instance is None:
            return self
        return RelatedObject(self, instance)

    async def load(self, instance: Model) -> Model:
        """The related instance: from the cache, or loaded on the owner's executor and cached."""
        pk = _owner_pk(self, instance)
        cached = instance._state.related.get(self.name, _MISSING)
        if cached is None:
            target = self.target_model()
            raise target.DoesNotExist(f"{type(instance).__name__} has no {self.name}")
        if cached is not _MISSING:
            return cached
        return await _load_one(self, instance, Query(self.target).filter(**{self.column: pk}))

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.target!r}, column={self.column!r})"


class ManyToMany(Relation):
    """A many-to-many relation through the model ``through`` (a label; for a table without its own model, the
    label of the ORM's generated through model, e.g. ``"app.Activity_tags"``). Each ``through`` row links the owner
    in ``source_column`` with ``target`` in ``target_column``.

    On an instance it returns a `QuerySet`, on the owner's executor, of the related ``target`` rows:
    ``Query(target).filter(pk__in=Query(through).filter(<source_column>=instance.pk).values(<target_column>))``.
    An instance without a primary key value raises ``ValueError``.
    """

    def __init__(self, target: str, *, through: str, source_column: str, target_column: str) -> None:
        super().__init__(target)
        self.through = check_model_label(through)
        self.source_column = _check_field_name(source_column)
        self.target_column = _check_field_name(target_column)

    def __get__(self, instance: Model | None, owner: type | None = None) -> Any:
        if instance is None:
            return self
        links = Query(self.through).filter(**{self.source_column: _owner_pk(self, instance)})
        return _related(self, instance, Query(self.target).filter(pk__in=links.values(self.target_column)))

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}({self.target!r}, through={self.through!r}, "
            f"source_column={self.source_column!r}, target_column={self.target_column!r})"
        )
