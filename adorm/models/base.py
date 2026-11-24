"""`Model`: the pydantic base class of every adorm model."""

from __future__ import annotations

import copy
import dataclasses

from collections.abc import Callable, Iterable, Mapping
from typing import Any, ClassVar, Protocol, runtime_checkable, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, model_validator, PrivateAttr, ValidationInfo
from pydantic_core.core_schema import ValidatorFunctionWrapHandler

from adorm.exceptions import InterfaceError, ModelExceptionsMixin, ProtectedError, RestrictedError
from adorm.models.managers import BoundModel, Manager
from adorm.models.meta import ModelMeta
from adorm.models.registry import registry
from adorm.models.relations import ForeignKey, GenericForeignKey, in_scope_of, Relation, TenantRelation

if TYPE_CHECKING:
    from adorm.operations import Operation
    from adorm.query.query import TenantSchema

__all__ = [
    "Executor",
    "Model",
    "ModelState",
    "get_generic_foreign_keys",
    "get_managers",
    "get_relations",
    "get_tenant_relations",
    "set_executor_resolver",
]


@runtime_checkable
class Executor(Protocol):
    """Runs operations for model instances: ``await executor.execute(operation)`` returns the operation's result."""

    async def execute(self, operation: Operation) -> Any: ...


ExecutorResolver = Callable[[type["Model"]], Executor | None]
_resolver: ExecutorResolver | None = None


def set_executor_resolver(resolver: ExecutorResolver | None) -> ExecutorResolver | None:
    """Set the function that gives an instance without its own executor one: ``resolver(model_class)`` returns an
    `Executor` or ``None``. Returns the previous resolver; ``None`` removes it."""
    global _resolver
    previous, _resolver = _resolver, resolver
    return previous


class ModelState:
    """Per-instance state outside the fields.

    ``adding`` is ``True`` until the instance is loaded or saved; ``executor`` runs the instance's operations
    (the one that loaded or last saved it); ``related`` caches related instances by relation name, the prefetched rows
    of a reverse or many-to-many relation as a tuple; for a generic foreign key, the content type, the object id and
    the instance loaded for them. ``generic`` holds, by relation name, the instance assigned to a generic foreign key
    whose content type the next write resolves, with the object id and the content type id set at assignment.
    ``tenant_schema`` is the `adorm.TenantSchema` of an instance read through a `adorm.TenantRelation`: the instance's
    own operations and relation loads qualify the tables of its models with its schema. ``None`` otherwise.
    """

    __slots__ = ("adding", "executor", "generic", "related", "tenant_schema")

    def __init__(self) -> None:
        self.adding = True
        self.executor: Executor | None = None
        self.related: dict[str, Any] = {}
        self.generic: dict[str, tuple[Any, Any, Any]] = {}
        self.tenant_schema: TenantSchema | None = None

    def __copy__(self) -> ModelState:
        state = ModelState()
        state.adding, state.executor, state.related = self.adding, self.executor, dict(self.related)
        state.generic = dict(self.generic)
        state.tenant_schema = self.tenant_schema
        return state

    def __deepcopy__(self, memo: dict[int, Any]) -> ModelState:
        state = ModelState()
        state.adding, state.executor = self.adding, self.executor
        state.related = copy.deepcopy(self.related, memo)
        state.generic = copy.deepcopy(self.generic, memo)
        state.tenant_schema = self.tenant_schema
        return state

    def __getstate__(self) -> dict[str, Any]:
        tenant_schema = self.tenant_schema
        scope = None if tenant_schema is None else (tenant_schema.schema, tenant_schema.models)
        return {"adding": self.adding, "related": self.related, "generic": self.generic, "tenant_schema": scope}

    def __setstate__(self, data: dict[str, Any]) -> None:
        from adorm.query.query import TenantSchema

        self.adding, self.executor, self.related = data["adding"], None, data["related"]
        self.generic = data.get("generic", {})
        scope = data.get("tenant_schema")
        self.tenant_schema = None if scope is None else TenantSchema(*scope)

    def __repr__(self) -> str:
        return f"ModelState(adding={self.adding!r}, related={sorted(self.related)!r})"


def _placeholder(model: type[Model], name: str) -> Any:
    """A value the field ``name`` of ``model`` validates: ``None``, ``0`` or ``""``."""
    from adorm.models.inheritance import _adapter

    adapter = _adapter(model, name)
    for candidate in (None, 0, ""):
        try:
            return adapter.validate_python(candidate)
        except ValueError:
            continue
    raise ValueError(f"{model.__name__}.{name} takes no placeholder while the content type id is not cached")


def get_relations(model: type[Model]) -> Mapping[str, Relation]:
    """The relation descriptors of ``model`` (inherited ones included) by attribute name."""
    return model.__adorm_relations__


def get_generic_foreign_keys(model: type[Model]) -> Mapping[str, GenericForeignKey]:
    """The `GenericForeignKey` descriptors of ``model`` (inherited ones included) by attribute name."""
    return model.__adorm_generic_foreign_keys__


def get_tenant_relations(model: type[Model]) -> Mapping[str, TenantRelation]:
    """The `TenantRelation` descriptors of ``model`` (inherited ones included) by attribute name."""
    return model.__adorm_tenant_relations__


def get_managers(model: type[Model]) -> Mapping[str, Manager]:
    """The manager declarations of ``model`` (inherited ones included) by attribute name."""
    return model.__adorm_managers__


def _collect_managers(cls: type) -> dict[str, Manager]:
    managers: dict[str, Manager] = {}
    for klass in reversed(cls.__mro__):
        for name, value in vars(klass).items():
            if isinstance(value, Manager) and not value._is_bound():
                managers[name] = value
            elif name in managers:
                del managers[name]
    return managers


def _collect(cls: type, kind: type) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for klass in reversed(cls.__mro__):
        for name, value in vars(klass).items():
            if isinstance(value, kind):
                found[name] = value
            elif name in found:
                del found[name]
    return found


def _inherited_orm_names(cls: type, fields: Mapping[str, Any], relations: Mapping[str, Relation]) -> dict[str, str]:
    """The ``orm_names`` entries of ``cls``'s model bases for names that are still fields or relations of ``cls``."""
    names: dict[str, str] = {}
    for klass in reversed(cls.__mro__[1:]):
        meta = klass.__dict__.get("_meta")
        if isinstance(meta, ModelMeta):
            names.update(meta.orm_names)
    return {name: orm_name for name, orm_name in names.items() if name in fields or name in relations}


class Model(ModelExceptionsMixin, BaseModel):
    """Base class of adorm models.

    A model declares ``_meta`` (a `ModelMeta`), its fields as pydantic fields and its relations as `Relation`
    descriptors. Every model class gets its own ``DoesNotExist`` and ``MultipleObjectsReturned``.

    Rows from the database become instances through `from_row()`, without validation. A field the row did not
    load (``only()``, ``defer()``) is deferred: reading it raises ``AttributeError``, assigning it sets it.
    Instances are equal when they belong to the same model (label), have the same non-``None`` primary key and the same
    tenant schema (`ModelState.tenant_schema`; instances without one compare by model and key alone); the hash
    follows. Instances of an abstract model compare and hash by identity. Names that are neither fields
    nor relations (annotations, ``to_attr`` of a prefetch) are pydantic extra values. ``Model.objects`` is the model
    bound to the current session; every `Manager` the class declares is accessed the same way.
    """

    model_config = ConfigDict(
        extra="allow",
        ignored_types=(Relation, GenericForeignKey, TenantRelation, BoundModel),
        serialize_by_alias=True,
        validate_by_name=True,
        validate_by_alias=True,
        protected_namespaces=(),
    )

    _meta: ClassVar[ModelMeta | None] = None
    objects = Manager()
    __adorm_relations__: ClassVar[Mapping[str, Relation]] = {}
    __adorm_generic_foreign_keys__: ClassVar[Mapping[str, GenericForeignKey]] = {}
    __adorm_tenant_relations__: ClassVar[Mapping[str, TenantRelation]] = {}
    __adorm_managers__: ClassVar[Mapping[str, Manager]] = {}
    _state: ModelState = PrivateAttr(default_factory=ModelState)

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        if "_meta" in cls.__private_attributes__:
            raise TypeError(f"{cls.__name__}._meta is a ClassVar[ModelMeta], not a private attribute")
        relations = _collect(cls, Relation)
        fields = cls.__pydantic_fields__
        for name, relation in relations.items():
            if isinstance(relation, ForeignKey) and relation.column not in fields:
                raise TypeError(f"{cls.__name__}.{name} is stored in {relation.column!r}, which is not a field")
        cls.__adorm_relations__ = relations
        generic = _collect(cls, GenericForeignKey)
        for name, relation in generic.items():
            for column in (relation.ct_column(cls), relation.fk_field):
                if column not in fields:
                    raise TypeError(f"{cls.__name__}.{name} is stored in {column!r}, which is not a field")
        cls.__adorm_generic_foreign_keys__ = generic
        cls.__adorm_tenant_relations__ = _collect(cls, TenantRelation)
        managers = _collect_managers(cls)
        clashes = sorted(name for name in managers if name in fields or name in relations)
        if clashes:
            raise TypeError(f"{cls.__name__} has managers named like fields or relations: {', '.join(clashes)}")
        if "objects" not in managers:
            raise TypeError(f"{cls.__name__}.objects is a Manager")
        cls.__adorm_managers__ = managers
        meta = cls.__dict__.get("_meta")
        if meta is None:
            return
        if not isinstance(meta, ModelMeta):
            raise TypeError(f"{cls.__name__}._meta is a ModelMeta, not {type(meta).__name__}")
        if meta.pk not in fields:
            raise TypeError(f"the primary key {meta.pk!r} of {meta.label} is not a field of {cls.__name__}")
        unknown = sorted(name for name in meta.orm_names if name not in fields and name not in relations)
        if unknown:
            raise TypeError(f"orm_names of {meta.label} names no field or relation: {', '.join(unknown)}")
        inherited = _inherited_orm_names(cls, fields, relations)
        if any(name not in meta.orm_names for name in inherited):
            meta = dataclasses.replace(meta, orm_names={**inherited, **meta.orm_names})
            cls._meta = meta
        registry.register(cls)

    @classmethod
    def _require_meta(cls) -> ModelMeta:
        if cls._meta is None:
            raise TypeError(f"{cls.__name__} is abstract: it has no _meta")
        return cls._meta

    @classmethod
    def _input_names(cls) -> set[str]:
        names = set(cls.__pydantic_fields__)
        for info in cls.__pydantic_fields__.values():
            for alias in (info.alias, info.validation_alias):
                if isinstance(alias, str):
                    names.add(alias)
        return names

    @classmethod
    def _field_given(cls, data: Mapping[str, Any], name: str) -> bool:
        info = cls.__pydantic_fields__[name]
        return name in data or (info.alias is not None and info.alias in data)

    @classmethod
    def _map_pk(cls, data: dict[str, Any]) -> None:
        meta = cls._meta
        if meta is None or meta.pk == "pk" or "pk" not in data:
            return
        if cls._field_given(data, meta.pk):
            raise ValueError(f"give either pk or {meta.pk}, not both")
        data[meta.pk] = data.pop("pk")

    @classmethod
    def _check_extra_names(cls, names: Iterable[str]) -> None:
        for name in names:
            if hasattr(cls, name):
                raise ValueError(f"{name!r} is an attribute of {cls.__name__}, so it cannot be an extra value")

    @model_validator(mode="wrap")
    @classmethod
    def _resolve_keywords(cls, data: Any, handler: ValidatorFunctionWrapHandler, info: ValidationInfo) -> Any:
        """Map ``pk`` to the primary key field and a relation keyword to its column, before field validation."""
        if not isinstance(data, Mapping):
            return handler(data)
        data = dict(data)
        cls._map_pk(data)
        generic_values = cls._generic_keywords(data, info.mode == "python")
        related: dict[str, Any] = {}
        for name, relation in cls.__adorm_relations__.items():
            if name not in data or not isinstance(relation, ForeignKey):
                continue
            value = data.pop(name)
            if info.mode != "python":
                raise ValueError(f"{name} is a relation: give its column {relation.column!r}")
            if cls._field_given(data, relation.column):
                raise ValueError(f"give either {name} or {relation.column}, not both")
            if value is not None:
                try:
                    relation._check_target(value)
                except TypeError as exc:
                    raise ValueError(str(exc)) from None
                if value.pk is None:
                    raise ValueError(f"cannot assign an unsaved {relation.target} instance to {name}")
            data[relation.column] = None if value is None else value.pk
            related[name] = value
        input_names = cls._input_names()
        cls._check_extra_names(name for name in data if name not in input_names)
        instance = handler(data)
        if related:
            instance._state.related.update(related)
        for name, value in generic_values.items():
            setattr(instance, name, value)
        return instance

    @classmethod
    def _generic_keywords(cls, data: dict[str, Any], python: bool) -> dict[str, Any]:
        """Take the generic foreign key keywords out of ``data`` and put the values of their fields in their place; the
        values taken, by relation name, are assigned to the validated instance. A content type field that has no
        cached value gets a placeholder its type accepts, unset again by the assignment."""
        taken: dict[str, Any] = {}
        for name, generic in cls.__adorm_generic_foreign_keys__.items():
            if name not in data:
                continue
            column = generic.ct_column(cls)
            if not python:
                raise ValueError(f"{name} is a generic foreign key: give {column!r} and {generic.fk_field!r}")
            for field in (column, generic.fk_field):
                if cls._field_given(data, field):
                    raise ValueError(f"give either {name} or {field}, not both")
            value = data.pop(name)
            taken[name] = value
            if value is None:
                data[column] = data[generic.fk_field] = None
                continue
            try:
                data[generic.fk_field] = generic.object_id(cls, value)
            except (TypeError, ValueError) as exc:
                raise ValueError(str(exc)) from None
            executor = _resolver(cls) if _resolver is not None else None
            content_type = generic.cached_content_type(cls, executor, value)
            if content_type is not None:
                data[column] = content_type
            elif cls.__pydantic_fields__[column].is_required():
                data[column] = _placeholder(cls, column)
        return taken

    @classmethod
    def from_row(
        cls, row: Mapping[str, Any], *, executor: Executor | None = None, tenant_schema: TenantSchema | None = None
    ) -> Any:
        """An instance of a stored row, built with ``model_construct()`` (no validation).

        ``row`` maps field names (or aliases) to values; ``pk`` stands for the primary key field. A field missing from
        it is deferred; a key that is a relation holds the related row (a mapping or an instance of the target,
        ``None`` for no related row) and fills the relation's cache; any other key becomes an extra value, which
        must not name an attribute of the class (``ValueError``). ``executor`` runs the instance's later operations
        (``save()``, relation loads) and those of the related instances built here. ``tenant_schema`` is the
        `adorm.TenantSchema` the row was read in: the instance and each related instance built here whose model it
        qualifies get it as `ModelState.tenant_schema`.
        """
        meta = cls._require_meta()
        relations = cls.__adorm_relations__
        data = dict(row)
        cls._map_pk(data)
        related = {name: data.pop(name) for name in relations if name in data}
        instance = cls.model_construct(None, **data)
        if instance.__pydantic_extra__:
            cls._check_extra_names(instance.__pydantic_extra__)
        present, loaded = instance.__dict__, instance.__pydantic_fields_set__
        for name in cls.__pydantic_fields__:
            if name not in loaded:
                present.pop(name, None)
        if meta.pk not in present:
            raise ValueError(f"a row of {meta.label} needs its primary key {meta.pk!r}")
        state = instance._state
        state.adding = False
        state.executor = executor
        if tenant_schema is not None and meta.label in tenant_schema.models:
            state.tenant_schema = tenant_schema
        for name, value in related.items():
            if isinstance(value, Model):
                relations[name]._check_target(value)
            elif value is not None:
                value = relations[name].target_model().from_row(value, executor=executor, tenant_schema=tenant_schema)
            state.related[name] = value
        return instance

    if not TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any:
            cls = type(self)
            if name in cls.__pydantic_fields__:
                for relation, generic in cls.__adorm_generic_foreign_keys__.items():
                    if generic.ct_column(cls) == name and generic.assigned(self) is not None:
                        raise AttributeError(
                            f"{cls.__name__}.{name} is unset until the next write: {relation} was assigned an instance "
                            "whose content type id is not cached"
                        )
                raise AttributeError(f"{cls.__name__}.{name} is deferred: the row was loaded without it")
            return super().__getattr__(name)

    @property
    def pk(self) -> Any:
        """The primary key value."""
        return getattr(self, self._require_meta().pk)

    @pk.setter
    def pk(self, value: Any) -> None:
        setattr(self, self._require_meta().pk, value)

    def __copy__(self) -> Any:
        clone = super().__copy__()
        clone._state = copy.copy(self._state)
        return clone

    def get_deferred_fields(self) -> set[str]:
        """The names of the fields the instance was loaded without and that were not assigned since."""
        return {name for name in type(self).__pydantic_fields__ if name not in self.__dict__}

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Model):
            return NotImplemented
        if self is other:
            return True
        meta, other_meta = type(self)._meta, type(other)._meta
        if meta is None or other_meta is None or meta.label != other_meta.label:
            return False
        pk = self.pk
        return pk is not None and pk == other.pk and self._schema_name() == other._schema_name()

    def __hash__(self) -> int:
        meta = type(self)._meta
        if meta is None:
            return object.__hash__(self)
        pk = self.pk
        if pk is None:
            raise TypeError("a model instance without a primary key value is unhashable")
        schema = self._schema_name()
        return hash((meta.label, pk)) if schema is None else hash((meta.label, pk, schema))

    def _schema_name(self) -> str | None:
        tenant_schema = self._state.tenant_schema
        return None if tenant_schema is None else tenant_schema.schema

    def _executor(self) -> Executor:
        executor = self._state.executor
        if executor is None and _resolver is not None:
            executor = _resolver(type(self))
        if executor is None:
            raise InterfaceError(f"no executor is available for {self._require_meta().label}")
        return executor

    async def save(
        self, *, update_fields: Iterable[str] | None = None, force_insert: bool = False, force_update: bool = False
    ) -> None:
        """Write the instance with a `Save` operation.

        The values written are the fields in ``model_fields_set`` (loaded and assigned), the primary key excepted;
        fields the instance never set are left to the target (its defaults on insert). Without a primary key value
        the row is inserted; with one it is updated, or inserted when no row has that key. ``update_fields`` limits
        the update to those fields (a relation name stands for its column, a generic foreign key's for both of its
        fields; the primary key is ignored) and never inserts; an empty one saves nothing. ``force_insert`` always
        inserts, ``force_update`` never does. The values the target returns (the primary key, defaults) are set on the
        instance. A content type a generic foreign key assignment left unset is resolved first, on the instance's
        executor (`GenericForeignKey`).

        Raises ``ValueError`` for an ``update_fields`` name that is not a field, one that is deferred, and
        ``update_fields`` or ``force_update`` without a primary key value.
        """
        from adorm.operations import Save

        meta = self._require_meta()
        cls = type(self)
        fields = cls.__pydantic_fields__
        if self._state.generic:
            await self._resolve_generic_foreign_keys(self._executor())
        names: tuple[str, ...] | None = None
        if update_fields is not None:
            if isinstance(update_fields, str):
                raise TypeError("update_fields is an iterable of field names, not a string")
            resolved = []
            for given in update_fields:
                relation = cls.__adorm_relations__.get(given)
                generic = cls.__adorm_generic_foreign_keys__.get(given)
                if isinstance(relation, ForeignKey):
                    candidates = [relation.column]
                elif generic is not None:
                    candidates = [generic.ct_column(cls), generic.fk_field]
                elif given == "pk":
                    candidates = [meta.pk]
                else:
                    candidates = [given]
                for name in candidates:
                    if name not in fields:
                        raise ValueError(f"{name!r} is not a field of {meta.label}")
                    if name not in self.__dict__:
                        raise ValueError(f"update_fields names {name!r}, which is deferred")
                    if name != meta.pk:
                        resolved.append(name)
            if not resolved:
                return
            names = tuple(resolved)
        pk = self.pk
        if names is not None and pk is None:
            raise ValueError(f"cannot update a {meta.label} row without a primary key value")
        assigned = self.model_fields_set
        if names is not None:
            assigned = assigned | set(names)
        values = {
            name: self.__dict__[name]
            for name in fields
            if name != meta.pk and name in assigned and name in self.__dict__
        }
        operation = Save(
            meta.label,
            pk,
            values,
            update_fields=names,
            force_insert=force_insert,
            force_update=force_update,
            tenant_schema=self._state.tenant_schema,
        )
        executor = self._executor()
        self._stored(await executor.execute(operation), executor)

    async def refresh_from_db(self, fields: Iterable[str] | None = None) -> None:
        """Reload the instance's field values from its row with a `Get` operation on the instance's executor (the one
        `save()` uses), from the model's base query (no manager), inside the executor's active transaction.

        Without ``fields`` the fields loaded now are reloaded and deferred fields stay deferred. ``fields`` names the
        fields to reload, loaded or deferred: ``pk`` stands for the primary key, a foreign key's name for its column,
        a generic foreign key's for both of its fields. A reverse or many-to-many relation's name loads nothing and
        clears its cached rows; an empty ``fields`` does nothing. Reloaded fields join ``model_fields_set``.

        Cached relations are cleared as Django's ``refresh_from_db()`` clears them: a foreign key's related instance
        when its column is reloaded; a reverse one-to-one instance, the prefetched rows of a reverse or many-to-many
        relation and a generic foreign key's instance (with a pending assignment) without ``fields`` or when
        ``fields`` names the relation. Annotations and ``to_attr`` values stay.

        Raises the model's ``DoesNotExist`` when the row is gone or the instance has no primary key value, leaving the
        instance unchanged; ``ValueError`` for a name that is neither a field nor a relation or that contains ``__``;
        ``TypeError`` for a string ``fields`` or a name that is not a string.
        """
        from adorm.query.query import Query
        from adorm.query.queryset import QuerySet

        meta = self._require_meta()
        cls = type(self)
        model_fields = cls.__pydantic_fields__
        relations = cls.__adorm_relations__
        generics = cls.__adorm_generic_foreign_keys__
        load: list[str] = []
        cleared: set[str] = set()
        if fields is None:
            cleared.update(name for name, relation in relations.items() if not isinstance(relation, ForeignKey))
            cleared.update(generics)
            deferred = self.get_deferred_fields() - {
                generic.ct_column(cls) for name, generic in generics.items() if name in self._state.generic
            }
            load = [name for name in model_fields if name not in deferred and name != meta.pk]
        else:
            if isinstance(fields, str):
                raise TypeError("fields is an iterable of field names, not a string")
            names = list(fields)
            for given in names:
                if not isinstance(given, str):
                    raise TypeError(f"fields holds field names, not {type(given).__name__}")
                if "__" in given:
                    raise ValueError(f"fields names {given!r}: relations and transforms (__) are not allowed")
            for given in names:
                relation = relations.get(given)
                if given == "pk":
                    load.append(meta.pk)
                elif isinstance(relation, ForeignKey):
                    load.append(relation.column)
                elif given in generics:
                    load.extend((generics[given].ct_column(cls), generics[given].fk_field))
                    cleared.add(given)
                elif relation is not None:
                    cleared.add(given)
                elif given in model_fields:
                    load.append(given)
                else:
                    raise ValueError(f"{given!r} is not a field of {meta.label}")
            if not load:
                self._clear_cached_relations(cleared)
                return
        pk = self.pk
        if pk is None:
            raise cls.DoesNotExist(f"{cls.__name__} matching query does not exist.")
        executor = self._executor()
        queryset = QuerySet(cls, executor, in_scope_of(self, Query(meta.label))).filter(pk=pk)
        if fields is not None or len(load) < len(model_fields) - 1:
            queryset = queryset.only(*dict.fromkeys(load))
        fresh = await queryset.get()
        reloaded = fresh.__dict__.keys() & {*load, meta.pk}
        for name in reloaded:
            self.__dict__[name] = fresh.__dict__[name]
        self.__pydantic_fields_set__.update(reloaded)
        for name, relation in relations.items():
            if isinstance(relation, ForeignKey) and relation.column in reloaded:
                cleared.add(name)
        self._clear_cached_relations(cleared)
        self._state.executor = executor

    def _clear_cached_relations(self, names: Iterable[str]) -> None:
        """Drop the cached values of the relations ``names`` and the pending assignments of generic foreign keys."""
        state = self._state
        for name in names:
            state.related.pop(name, None)
            state.generic.pop(name, None)

    async def full_clean(
        self, *, exclude: Iterable[str] | None = None, validate_unique: bool = True, validate_constraints: bool = True
    ) -> None:
        """Validate the instance `save()` would write as the target does (Django's ``Model.full_clean()``), with a
        `FullClean` operation, skipping the fields (or relations) in ``exclude``, and set the cleaned values on its
        loaded fields. Raises `adorm.ValidationError`; ``ValueError`` for an ``exclude`` name that names nothing."""
        from adorm.operations import FullClean

        meta = self._require_meta()
        cls = type(self)
        if self._state.generic:
            await self._resolve_generic_foreign_keys(self._executor())
        names: list[str] = []
        if exclude is not None:
            if isinstance(exclude, str):
                raise TypeError("exclude is an iterable of field names, not a string")
            for name in exclude:
                if name == "pk":
                    name = meta.pk
                if name not in cls.__pydantic_fields__ and name not in cls.__adorm_relations__:
                    raise ValueError(f"{name!r} is not a field of {meta.label}")
                names.append(name)
        values = {
            name: self.__dict__[name]
            for name in cls.__pydantic_fields__
            if name != meta.pk and name in self.model_fields_set and name in self.__dict__
        }
        operation = FullClean(
            meta.label,
            self.__dict__.get(meta.pk),
            values,
            exclude=names,
            validate_unique=validate_unique,
            validate_constraints=validate_constraints,
            adding=self._state.adding,
            tenant_schema=self._state.tenant_schema,
        )
        cleaned = await self._executor().execute(operation)
        for name, value in (cleaned or {}).items():
            if name in cls.__pydantic_fields__ and name in self.__dict__:
                self.__dict__[name] = value

    async def delete(self) -> tuple[int, dict[str, int]]:
        """Delete the instance's row and the rows that cascade from it with a `DeleteInstance` operation, which runs
        the target's delete of an instance (on the Django driver a Django model's ``delete()`` override included);
        returns ``(total, {label: count})`` and, when rows were deleted, sets the primary key to ``None``.

        Raises ``ValueError`` without a primary key value, and `ProtectedError` or `RestrictedError` (with instances of
        the registered models as objects) when a reference forbids the delete.
        """
        from adorm.operations import DeleteInstance
        from adorm.query.queryset import _bind_error_objects

        meta = self._require_meta()
        pk = self.pk
        if pk is None:
            raise ValueError(f"cannot delete a {meta.label} instance without a primary key value")
        executor = self._executor()
        try:
            total, counts = await executor.execute(
                DeleteInstance(meta.label, pk, tenant_schema=self._state.tenant_schema)
            )
        except (ProtectedError, RestrictedError) as exc:
            _bind_error_objects(exc, executor)
            raise
        if total:
            self.__dict__[meta.pk] = None
        return total, dict(counts)

    async def _resolve_generic_foreign_keys(self, executor: Executor) -> None:
        """Set the content type fields that generic foreign key assignments left unset, read on ``executor``."""
        for name in list(self._state.generic):
            await type(self).__adorm_generic_foreign_keys__[name].resolve(self, executor)

    def _stored(self, result: Any, executor: Executor) -> None:
        """Set the field values a write returned and mark the instance as stored by ``executor``."""
        if result is not None:
            if not isinstance(result, Mapping):
                raise TypeError(f"a write returns a mapping of field values, not {type(result).__name__}")
            fields = type(self).__pydantic_fields__
            for name, value in result.items():
                if name in fields:
                    self.__dict__[name] = value
                    self.__pydantic_fields_set__.add(name)
        self._state.adding = False
        self._state.executor = executor
