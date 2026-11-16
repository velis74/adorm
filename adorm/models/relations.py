"""Relation descriptors of models.

A relation is a class attribute, not a pydantic field: it is absent from validation, serialisation and the JSON
schema. On an instance, a `ForeignKey`, `ReverseOneToOne` or `GenericForeignKey` returns an awaitable that loads the
related object; a `ReverseForeignKey` or `ManyToMany` returns a `QuerySet` of the related rows on the owner's executor.
"""

from __future__ import annotations

import functools

from collections.abc import Generator
from typing import Annotated, Any, TYPE_CHECKING

from adorm.exceptions import InterfaceError, ObjectDoesNotExist
from adorm.models.contenttypes import CONTENT_TYPE_MODEL
from adorm.query.query import check_model_label, Query

if TYPE_CHECKING:
    from adorm.models.base import Model

__all__ = [
    "ForeignKey",
    "GenericForeignKey",
    "ManyToMany",
    "RelatedObject",
    "Relation",
    "ReverseForeignKey",
    "ReverseOneToOne",
]

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


@functools.lru_cache(maxsize=512)
def _field_adapter(model: type[Model], name: str) -> Any:
    from pydantic import TypeAdapter

    info = model.__pydantic_fields__[name]
    return TypeAdapter(Annotated[(info.annotation, *info.metadata)] if info.metadata else info.annotation)


class GenericForeignKey:
    """A relation to a row of any model: the content type in the foreign key ``ct_field`` and the primary key in the
    field ``fk_field`` (Django's ``GenericForeignKey``).

    ``await instance.rel`` returns the related instance: the content type row (``app_label`` and ``model``) names the
    registered model, whose row with the primary key in ``fk_field`` is loaded with a `Get` operation on the owner's
    executor and cached on the owner while both fields keep their values. The content type is resolved through
    `adorm.content_types`, which caches it. Either field ``None``, or no row with that primary key, gives ``None``. A
    content type of a model that is not registered raises ``LookupError``. Reading the relation while either field is
    deferred raises ``AttributeError``.

    Assigning an instance (``note.item = book``, also as a constructor keyword) sets ``fk_field`` to its primary key,
    converted to the field's type (``str(pk)`` when the key itself does not validate), and caches it as the related
    instance. The owner's next write (`Model.save()`, `Model.full_clean()`, ``create()``, ``bulk_create()``) sets the
    content type field to the instance model's content type id (its concrete model's with ``for_concrete_model``),
    taken through `adorm.content_types` on the executor that writes. Until then the field holds the id cached for the
    owner's executor, or, when none is cached, is unset: reading it raises ``AttributeError``. Assigning either field
    afterwards drops the pending content type. As an attribute, an instance without a primary key raises
    ``ValueError`` and a value that is not a model instance ``TypeError``; assigning ``None`` sets both fields to
    ``None``. As a constructor keyword, every refusal raises pydantic's ``ValidationError``.

    The content type field is a `ForeignKey` of the model, whose column and target are used, or a field
    ``<ct_field>_id`` holding a primary key of `CONTENT_TYPE_MODEL`. The descriptor is not a `Relation`: query paths
    do not traverse it, and it is absent from `adorm.models.get_relations()`.
    """

    name: str

    def __init__(
        self, ct_field: str = "content_type", fk_field: str = "object_id", *, for_concrete_model: bool = True
    ) -> None:
        self.ct_field = _check_field_name(ct_field)
        self.fk_field = _check_field_name(fk_field)
        if not isinstance(for_concrete_model, bool):
            raise TypeError(f"for_concrete_model is a bool, not {for_concrete_model!r}")
        self.for_concrete_model = for_concrete_model

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        concrete = "" if self.for_concrete_model else ", for_concrete_model=False"
        return f"{type(self).__name__}({self.ct_field!r}, {self.fk_field!r}{concrete})"

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

    def object_id(self, model: type[Model], value: Any) -> Any:
        """The value ``fk_field`` of ``model`` holds for the instance ``value``: its primary key converted to the
        field's type, or ``str(pk)`` when the key itself does not validate. Raises ``TypeError`` for a value that is
        not an instance of a model with ``_meta``, and ``ValueError`` for one without a primary key value or whose key
        fits neither way."""
        from pydantic import ValidationError

        from adorm.models.base import Model

        if not isinstance(value, Model) or type(value)._meta is None:
            raise TypeError(f"{self.name} takes a model instance, not {type(value).__name__}")
        pk = value.pk
        if pk is None:
            raise ValueError(f"cannot assign an unsaved {type(value)._meta.label} instance to {self.name}")
        adapter = _field_adapter(model, self.fk_field)
        for candidate in (pk, str(pk)):
            try:
                return adapter.validate_python(candidate)
            except ValidationError:
                continue
        raise ValueError(f"the primary key {pk!r} does not fit {model.__name__}.{self.fk_field}")

    def cached_content_type(self, model: type[Model], executor: Any, value: Model) -> Any:
        """The content type id of ``value``'s model as `adorm.content_types` has it cached for ``executor`` (the
        executor of an instance of ``model``), ``None`` when it has none."""
        from adorm.models.contenttypes import content_types

        if executor is None:
            return None
        return content_types.cached_id(
            value,
            for_concrete_model=self.for_concrete_model,
            session=executor,
            content_type_model=self.content_type_model(model),
        )

    def values(self, instance: Model) -> tuple[Any, Any]:
        """The content type's primary key and the object id of ``instance``; ``AttributeError`` when either field is
        deferred."""
        values = instance.__dict__
        for name in (self.ct_column(type(instance)), self.fk_field):
            if name not in values:
                raise AttributeError(f"{type(instance).__name__}.{self.name} needs {name!r}, which is deferred")
        return values[self.ct_column(type(instance))], values[self.fk_field]

    def assigned(self, instance: Model) -> Model | None:
        """The instance assigned to the relation whose content type the next write resolves, ``None`` when there is
        none or either field changed since."""
        entry = instance._state.generic.get(self.name)
        if entry is None:
            return None
        value, object_id, content_type = entry
        values = instance.__dict__
        column = self.ct_column(type(instance))
        unchanged = values.get(self.fk_field, _MISSING) == object_id and (
            column not in values if content_type is None else values.get(column, _MISSING) == content_type
        )
        if not unchanged:
            del instance._state.generic[self.name]
            return None
        return value

    def __get__(self, instance: Model | None, owner: type | None = None) -> Any:
        if instance is None:
            return self
        if self.assigned(instance) is None:
            self.values(instance)
        return RelatedObject(self, instance)

    def __set__(self, instance: Model, value: Any) -> None:
        model = type(instance)
        column = self.ct_column(model)
        state = instance._state
        if value is None:
            state.generic.pop(self.name, None)
            setattr(instance, column, None)
            setattr(instance, self.fk_field, None)
            state.related[self.name] = (None, None, None)
            return
        object_id = self.object_id(model, value)
        try:
            executor = instance._executor()
        except InterfaceError:
            executor = None
        content_type = self.cached_content_type(model, executor, value)
        setattr(instance, self.fk_field, object_id)
        if content_type is None:
            instance.__dict__.pop(column, None)
            instance.__pydantic_fields_set__.discard(column)
        else:
            setattr(instance, column, content_type)
        state.generic[self.name] = (value, object_id, content_type)
        state.related[self.name] = (content_type, object_id, value)

    async def resolve(self, instance: Model, executor: Any) -> None:
        """Set the content type field of ``instance`` for the instance assigned to the relation, read through
        `adorm.content_types` on ``executor``; nothing when no assignment is pending. An assignment that changes
        during the lookup is resolved anew; one that is resolved or dropped meanwhile is left as it is."""
        from adorm.models.contenttypes import content_types

        state = instance._state
        while True:
            value = self.assigned(instance)
            if value is None:
                return
            entry = state.generic[self.name]
            content_type = await content_types.get_id(
                value,
                for_concrete_model=self.for_concrete_model,
                session=executor,
                content_type_model=self.content_type_model(type(instance)),
            )
            # Another task may have resolved, replaced or dropped the assignment during the lookup.
            if state.generic.get(self.name) is entry and self.assigned(instance) is value:
                break
        object_id = instance.__dict__[self.fk_field]
        setattr(instance, self.ct_column(type(instance)), content_type)
        state.generic.pop(self.name, None)
        state.related[self.name] = (content_type, object_id, value)

    def cached(self, instance: Model) -> Any:
        """The cached related instance (or ``None``) while it matches both fields; a sentinel otherwise."""
        assigned = self.assigned(instance)
        if assigned is not None:
            return assigned
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

        cached = self.cached(instance)
        if cached is not _MISSING:
            return cached
        content_type, object_id = self.values(instance)
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
        through `adorm.content_types`."""
        from adorm.models.contenttypes import content_types
        from adorm.models.registry import registry

        relation = self._content_type_relation(type(instance))
        cached = relation.cached(instance) if relation is not None else _MISSING
        if cached is not _MISSING and cached is not None and {"app_label", "model"} <= cached.__dict__.keys():
            return registry.get_for_content_type(cached.__dict__["app_label"], cached.__dict__["model"])
        return await content_types.get_model(
            content_type, session=executor, content_type_model=self.content_type_model(type(instance))
        )
