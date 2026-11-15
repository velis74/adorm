"""Relation descriptors of models.

A relation is a class attribute, not a pydantic field: it is absent from validation, serialisation and the JSON
schema. On an instance, a `ForeignKey`, `ReverseOneToOne` or `GenericForeignKey` returns an awaitable that loads the
related object; a `ReverseForeignKey` or `ManyToMany` returns a `QuerySet` of the related rows on the owner's executor.
"""

from __future__ import annotations

import functools

from collections.abc import Generator
from typing import Any, TYPE_CHECKING

from adorm.exceptions import ObjectDoesNotExist
from adorm.query.query import check_model_label, Query

if TYPE_CHECKING:
    from adorm.models.base import Model

__all__ = [
    "CONTENT_TYPE_MODEL",
    "ForeignKey",
    "GenericForeignKey",
    "ManyToMany",
    "RelatedObject",
    "Relation",
    "ReverseForeignKey",
    "ReverseOneToOne",
]

CONTENT_TYPE_MODEL = "contenttypes.ContentType"
"""The label of the content type model a `GenericForeignKey` reads when its content type field is not a `ForeignKey`
of the model."""

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

    def __init__(self, relation: ForeignKey | ReverseOneToOne | GenericForeignKey, instance: Model) -> None:
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


def target_queryset(relation: Relation, executor: Any) -> Any:
    """Every row of the relation's target from the target's ``objects`` manager on ``executor``: a query set of the
    manager's ``queryset_class`` that starts from its ``get_queryset()``."""
    from adorm.executor.sessions import Session
    from adorm.query.queryset import QuerySet

    target = relation.target_model()
    manager = target.__adorm_managers__.get("objects")
    if manager is None:
        return QuerySet(target, executor, Query(relation.target))
    if isinstance(executor, Session):
        return manager._bind(target, executor).get_queryset()
    return manager.queryset_class(target, executor, Query(relation.target, manager=manager.orm_name))


def _related(relation: Relation, instance: Model, **lookups: Any) -> Any:
    """The target's rows matching ``lookups`` (`target_queryset()` on the owner's executor, filtered); the rows a
    prefetch cached for the relation serve the query set's ``async for``, ``count()`` and ``exists()``."""
    queryset = target_queryset(relation, instance._executor()).filter(**lookups)
    cached = instance._state.related.get(relation.name, _MISSING)
    return queryset if cached is _MISSING else queryset._with_cache(cached)


def _owner_pk(relation: Relation, instance: Model) -> Any:
    pk = instance.pk
    if pk is None:
        raise ValueError(f"{type(instance).__name__}.{relation.name} needs a primary key, the instance has none")
    return pk


class ReverseForeignKey(Relation):
    """The reverse side of a `ForeignKey`: the rows of ``target`` whose field ``column`` holds the owner's primary
    key.

    On an instance it returns the target's ``objects.filter(<column>=instance.pk)`` on the owner's executor: a query
    set of the manager's ``queryset_class`` that applies its ``get_queryset()`` and ORM manager. After a
    ``prefetch_related()`` of the relation, the query set's ``async for``, ``count()`` and ``exists()`` read the
    prefetched rows; its building methods return query sets that run again. An instance without a primary key value
    raises ``ValueError``.
    """

    def __init__(self, target: str, *, column: str) -> None:
        super().__init__(target)
        self.column = _check_field_name(column)

    def __get__(self, instance: Model | None, owner: type | None = None) -> Any:
        if instance is None:
            return self
        return _related(self, instance, **{self.column: _owner_pk(self, instance)})

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

    On an instance it returns the related ``target`` rows on the owner's executor, from the target's ``objects`` manager
    and with the prefetched rows as for `ReverseForeignKey`:
    ``objects.filter(pk__in=Query(through).filter(<source_column>=instance.pk).values(<target_column>))``.
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
        return _related(self, instance, pk__in=links.values(self.target_column))

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}({self.target!r}, through={self.through!r}, "
            f"source_column={self.source_column!r}, target_column={self.target_column!r})"
        )


@functools.lru_cache(maxsize=512)
def _pk_adapter(model: type[Model]) -> Any:
    from pydantic import TypeAdapter

    return TypeAdapter(model.__pydantic_fields__[model._require_meta().pk].annotation)


def coerce_pk(model: type[Model], value: Any) -> Any:
    """``value`` (an object id of a `GenericForeignKey`) as a primary key of ``model``: converted to the type of the
    primary key field (its constraints are not checked), so ``"7"`` becomes ``7`` for an integer key. Raises
    ``ValueError`` for a value that does not convert."""
    return _pk_adapter(model).validate_python(value)


class GenericForeignKey:
    """A relation to a row of any model: the content type in the foreign key ``ct_field`` and the primary key in the
    field ``fk_field`` (Django's ``GenericForeignKey``).

    ``await instance.rel`` returns the related instance: the content type row (``app_label`` and ``model``) names the
    registered model, whose row with the primary key in ``fk_field`` is loaded with a `Get` operation on the owner's
    executor and cached on the owner while both fields keep their values. Either field ``None``, or no row with that
    primary key, gives ``None``. A content type of a model that is not registered raises ``LookupError``. Reading the
    relation while either field is deferred raises ``AttributeError``.

    The content type field is a `ForeignKey` of the model, whose column and target are used, or a field
    ``<ct_field>_id`` holding a primary key of `CONTENT_TYPE_MODEL`. Assigning ``None`` sets both fields to ``None``;
    assigning an instance raises ``TypeError``: set the content type and the object id fields. The descriptor is not
    a `Relation`: query paths do not traverse it, and it is absent from `adorm.models.get_relations()`.
    """

    name: str

    def __init__(self, ct_field: str = "content_type", fk_field: str = "object_id") -> None:
        self.ct_field = _check_field_name(ct_field)
        self.fk_field = _check_field_name(fk_field)

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.ct_field!r}, {self.fk_field!r})"

    def _content_type_relation(self, model: type[Model]) -> ForeignKey | None:
        relation = model.__adorm_relations__.get(self.ct_field)
        return relation if isinstance(relation, ForeignKey) else None

    def ct_column(self, model: type[Model]) -> str:
        """The field of ``model`` that holds the content type's primary key."""
        relation = self._content_type_relation(model)
        return relation.column if relation is not None else f"{self.ct_field}_id"

    def content_type_model(self, model: type[Model]) -> str:
        """The label of the content type model."""
        relation = self._content_type_relation(model)
        return relation.target if relation is not None else CONTENT_TYPE_MODEL

    def values(self, instance: Model) -> tuple[Any, Any]:
        """The content type's primary key and the object id of ``instance``; ``AttributeError`` when either field is
        deferred."""
        values = instance.__dict__
        for name in (self.ct_column(type(instance)), self.fk_field):
            if name not in values:
                raise AttributeError(f"{type(instance).__name__}.{self.name} needs {name!r}, which is deferred")
        return values[self.ct_column(type(instance))], values[self.fk_field]

    def __get__(self, instance: Model | None, owner: type | None = None) -> Any:
        if instance is None:
            return self
        self.values(instance)
        return RelatedObject(self, instance)

    def __set__(self, instance: Model, value: Any) -> None:
        if value is not None:
            raise TypeError(
                f"{self.name} is a generic foreign key: assign {self.ct_column(type(instance))!r} and "
                f"{self.fk_field!r} instead"
            )
        setattr(instance, self.ct_column(type(instance)), None)
        setattr(instance, self.fk_field, None)
        instance._state.related[self.name] = (None, None, None)

    def cached(self, instance: Model) -> Any:
        """The cached related instance (or ``None``) while it matches both fields; a sentinel otherwise."""
        content_type, object_id = self.values(instance)
        if content_type is None or object_id is None:
            return None
        entry = instance._state.related.get(self.name, _MISSING)
        if entry is _MISSING or entry[0] != content_type or entry[1] != object_id:
            return _MISSING
        return entry[2]

    def cache(self, instance: Model, value: Model | None, key: tuple[Any, Any] | None = None) -> None:
        """Cache ``value`` as the related instance for ``key`` (the content type and the object id it was loaded
        for), by default the current values of both fields."""
        content_type, object_id = self.values(instance) if key is None else key
        instance._state.related[self.name] = (content_type, object_id, value)

    async def load(self, instance: Model) -> Model | None:
        """The related instance: from the cache, or loaded on the owner's executor and cached."""
        from adorm.operations import Get

        content_type, object_id = self.values(instance)
        cached = self.cached(instance)
        if cached is not _MISSING:
            return cached
        executor = instance._executor()
        model = await self._model(instance, content_type, executor)
        try:
            row = await executor.execute(Get(Query(model._require_meta().label).filter(pk=coerce_pk(model, object_id))))
        except ObjectDoesNotExist as exc:
            if type(exc) is not ObjectDoesNotExist:
                raise
            related = None
        else:
            related = model.from_row(row, executor=executor)
        self.cache(instance, related, (content_type, object_id))
        return related

    async def _model(self, instance: Model, content_type: Any, executor: Any) -> type[Model]:
        """The registered model of the content type ``content_type``: from the content type relation's cache, or
        read with a `Get` operation."""
        from adorm.models.registry import registry
        from adorm.operations import Get

        relation = self._content_type_relation(type(instance))
        cached = relation.cached(instance) if relation is not None else _MISSING
        if cached is not _MISSING and cached is not None and {"app_label", "model"} <= cached.__dict__.keys():
            app_label, name = cached.__dict__["app_label"], cached.__dict__["model"]
        else:
            query = Query(self.content_type_model(type(instance))).filter(pk=content_type).values("app_label", "model")
            row = await executor.execute(Get(query))
            app_label, name = row["app_label"], row["model"]
        return registry.get_for_content_type(app_label, name)
