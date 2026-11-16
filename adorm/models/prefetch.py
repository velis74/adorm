"""Prefetching related objects: `Prefetch`, `GenericPrefetch` and `prefetch_related_objects()`.

A prefetch runs after the rows of a query are read, with one `adorm.operations.Select` per relation and level (two for
a many-to-many relation: the links, then the targets; one per content type for a generic foreign key, after one for the
content types `adorm.content_types` has not cached), and one more for each further `BATCH_SIZE` keys. The operations
run one after another on one executor, so inside a transaction they share its slot; outside one each takes a slot of
its own.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, TYPE_CHECKING

from adorm.models.relations import (
    _MISSING,
    coerce_pk,
    ForeignKey,
    GenericForeignKey,
    ManyToMany,
    Relation,
    ReverseForeignKey,
    ReverseOneToOne,
    target_queryset,
)
from adorm.query.expressions import check_field_path
from adorm.query.query import as_query, Query

if TYPE_CHECKING:
    from adorm.models.base import Executor, Model

__all__ = ["BATCH_SIZE", "GenericPrefetch", "Prefetch", "normalize_lookups", "prefetch_related_objects"]

BATCH_SIZE = 10_000
"""The most keys one `adorm.operations.Select` of a prefetch filters on; more keys run one operation per batch."""


def _batches(values: list[Any]) -> list[list[Any]]:
    return [values[start : start + BATCH_SIZE] for start in range(0, len(values), BATCH_SIZE)]


def _prefetch_source(queryset: Any) -> tuple[Query, type[Model], tuple[Prefetch, ...]]:
    """The query, model class and prefetch lookups of a prefetch query set (a `QuerySet`, or a `Query` on a registered
    model)."""
    from adorm.models.registry import registry

    query = as_query(queryset)
    if query is None:
        raise TypeError(f"a prefetch query set is a QuerySet or a Query, not {type(queryset).__name__}")
    model = getattr(queryset, "model", None)
    if model is None or getattr(model, "_meta", None) is None or model._meta.label != query.model:
        model = registry.get(query.model)
    if query.row_format != "model":
        raise ValueError("a prefetch query set cannot use values() or values_list()")
    if query.is_sliced:
        raise ValueError("a prefetch query set cannot be sliced")
    return query, model, tuple(getattr(queryset, "prefetch_lookups", ()))


class Prefetch:
    """A prefetch lookup: the relation path ``lookup`` (``"attendances__member"``), with the rows of the last relation
    read from ``queryset`` and stored in ``to_attr``.

    ``queryset`` (a `adorm.QuerySet` or a `adorm.query.Query` on the relation's target model) selects, filters and
    orders the related rows of the path's last relation; its own ``prefetch_related()`` lookups continue from there.
    It selects model rows and is not sliced (``ValueError``); the operations run on the executor of the instances
    prefetched for, not the query set's. A ``to_attr`` stores the related rows of the last relation as an extra value
    of each instance under that name (a list for a multi-valued relation, an instance or ``None`` otherwise) instead
    of the relation's cache; it names no attribute of the model (``ValueError`` when the prefetch runs).

    ``prefetch_through`` is ``lookup``; ``prefetch_to`` is ``lookup`` with ``to_attr`` in place of its last segment.
    Two prefetches are equal when their ``prefetch_to`` is.
    """

    def __init__(self, lookup: str, queryset: Any = None, to_attr: str | None = None) -> None:
        if not isinstance(lookup, str):
            raise TypeError(f"a prefetch lookup is a relation path, not {type(lookup).__name__}")
        self.prefetch_through = check_field_path(lookup)
        self.queryset = queryset
        if queryset is not None:
            self._source = _prefetch_source(queryset)
        if to_attr is not None and (not isinstance(to_attr, str) or not to_attr.isidentifier() or "__" in to_attr):
            raise ValueError(f"{to_attr!r} is not an attribute name")
        self.to_attr = to_attr
        segments = lookup.split("__")
        if to_attr is not None:
            segments[-1] = to_attr
        self.prefetch_to = "__".join(segments)

    def levels(self) -> int:
        """The number of relations on the path."""
        return self.prefetch_through.count("__") + 1

    def current_prefetch_to(self, level: int) -> str:
        """The part of ``prefetch_to`` up to and including the relation at ``level`` (0 is the first)."""
        return "__".join(self.prefetch_to.split("__")[: level + 1])

    def sources(self) -> tuple[tuple[Query, type[Model], tuple[Prefetch, ...]], ...]:
        """The query, model class and prefetch lookups of each query set, empty without a query set."""
        return () if self.queryset is None else (self._source,)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Prefetch):
            return NotImplemented
        return self.prefetch_to == other.prefetch_to

    def __hash__(self) -> int:
        return hash((Prefetch, self.prefetch_to))

    def __repr__(self) -> str:
        parts = [repr(self.prefetch_through)]
        if self.queryset is not None:
            parts.append(f"queryset={self.queryset!r}")
        if self.to_attr is not None:
            parts.append(f"to_attr={self.to_attr!r}")
        return f"{type(self).__name__}({', '.join(parts)})"

    def __reduce__(self) -> tuple[Any, ...]:
        return type(self), (self.prefetch_through, self.queryset, self.to_attr)


class GenericPrefetch(Prefetch):
    """A prefetch lookup whose last relation is a `adorm.GenericForeignKey`, with a query set per related model:
    ``querysets`` holds query sets (or queries) on different models, and the related rows of a content type are read
    from the one on its model (the plain rows of models without one). ``to_attr`` is as for `Prefetch`.
    """

    def __init__(self, lookup: str, querysets: Iterable[Any], to_attr: str | None = None) -> None:
        if isinstance(querysets, str) or not isinstance(querysets, Iterable):
            raise TypeError("querysets is an iterable of query sets")
        querysets = tuple(querysets)
        super().__init__(lookup, None, to_attr)
        sources = tuple(_prefetch_source(queryset) for queryset in querysets)
        labels = [query.model for query, _, _ in sources]
        if len(set(labels)) != len(labels):
            raise ValueError("each query set of a GenericPrefetch is on a different model")
        self.querysets = querysets
        self._sources = sources

    def sources(self) -> tuple[tuple[Query, type[Model], tuple[Prefetch, ...]], ...]:
        return self._sources

    def __repr__(self) -> str:
        to_attr = "" if self.to_attr is None else f", to_attr={self.to_attr!r}"
        return f"GenericPrefetch({self.prefetch_through!r}, {list(self.querysets)!r}{to_attr})"

    def __reduce__(self) -> tuple[Any, ...]:
        return type(self), (self.prefetch_through, self.querysets, self.to_attr)


def _lookup(value: Any) -> Prefetch:
    if isinstance(value, Prefetch):
        return value
    if isinstance(value, str):
        return Prefetch(value)
    raise TypeError(f"a prefetch lookup is a relation path or a Prefetch, not {type(value).__name__}")


def normalize_lookups(current: Sequence[Prefetch], lookups: Iterable[Any], prefix: str = "") -> tuple[Prefetch, ...]:
    """``current`` followed by ``lookups`` as `Prefetch` objects, with ``prefix`` (a relation path) put before each
    new path. A path that is already there is kept once; a new one with a query set or ``to_attr`` whose
    ``prefetch_to`` is already there raises ``ValueError``."""
    result = list(current)
    for value in lookups:
        lookup = _lookup(value)
        if prefix:
            lookup = _prefixed(lookup, prefix)
        existing = next((item for item in result if item.prefetch_to == lookup.prefetch_to), None)
        if existing is None:
            result.append(lookup)
        elif lookup.sources() or existing.sources() or lookup.to_attr != existing.to_attr:
            raise ValueError(f"the prefetch lookup {lookup.prefetch_to!r} is given twice with different query sets")
    return tuple(result)


def _prefixed(lookup: Prefetch, prefix: str) -> Prefetch:
    path = f"{prefix}__{lookup.prefetch_through}"
    if isinstance(lookup, GenericPrefetch):
        return GenericPrefetch(path, lookup.querysets, lookup.to_attr)
    return Prefetch(path, lookup.queryset, lookup.to_attr)


async def prefetch_related_objects(
    instances: Iterable[Model], *lookups: str | Prefetch, executor: Executor | None = None
) -> None:
    """Load the related objects of ``lookups`` (relation paths or `Prefetch` objects) for ``instances`` and cache
    them on the instances, with operations on ``executor`` (by default the first instance's). A segment that names
    nothing raises ``AttributeError``, one that is no relation or a query set that does not fit ``ValueError``."""
    instances = list(instances)
    if not instances or not lookups:
        return
    if executor is None:
        executor = instances[0]._executor()
    pending = list(normalize_lookups((), lookups))
    done: dict[str, list[Model]] = {}
    while pending:
        lookup = pending.pop(0)
        if lookup.prefetch_to in done:
            continue
        objects: list[Model] = instances
        for level, segment in enumerate(lookup.prefetch_through.split("__")):
            if not objects:
                break
            prefetch_to = lookup.current_prefetch_to(level)
            if prefetch_to in done:
                objects = done[prefetch_to]
                continue
            last = level == lookup.levels() - 1
            objects = await _prefetch_level(objects, segment, lookup if last else None, executor, lookup)
            done[prefetch_to] = objects


def _by_class(objects: Iterable[Model]) -> dict[type[Model], list[Model]]:
    groups: dict[type[Model], list[Model]] = {}
    for instance in objects:
        groups.setdefault(type(instance), []).append(instance)
    return groups


def _descriptor(cls: type[Model], segment: str, lookup: Prefetch) -> Any:
    relation = cls.__adorm_relations__.get(segment) or cls.__adorm_generic_foreign_keys__.get(segment)
    if relation is not None and (type(relation) is not Relation):
        return relation
    if relation is not None or segment in cls.__pydantic_fields__ or hasattr(cls, segment):
        raise ValueError(
            f"{segment!r} does not resolve to a relation that supports prefetching: {lookup.prefetch_through!r} is "
            "an invalid parameter to prefetch_related()"
        )
    return None


def _extra(instance: Model, name: str) -> Any:
    extra = instance.__pydantic_extra__ or {}
    return extra.get(name, _MISSING)


def _flatten(values: Iterable[Any]) -> list[Model]:
    objects: list[Model] = []
    for value in values:
        if isinstance(value, list | tuple):
            objects.extend(value)
        elif value is not None:
            objects.append(value)
    return objects


async def _prefetch_level(
    objects: list[Model], segment: str, final: Prefetch | None, executor: Executor, lookup: Prefetch
) -> list[Model]:
    """Prefetch the relation ``segment`` of ``objects`` and return the related objects (the next level's instances).
    ``final`` is the lookup whose last relation this is; the prefetch lookups of its query sets run on the rows read
    from them."""
    to_attr = final.to_attr if final is not None else None
    related: list[Any] = []
    for cls, group in _by_class(objects).items():
        descriptor = _descriptor(cls, segment, lookup)
        if descriptor is None:
            values = [_extra(instance, segment) for instance in group]
            if any(value is _MISSING for value in values):
                raise AttributeError(
                    f"cannot find {segment!r} on {cls.__name__} object: {lookup.prefetch_through!r} is an invalid "
                    "parameter to prefetch_related()"
                )
            related.extend(values)
            continue
        if to_attr is not None and (to_attr in cls.__pydantic_fields__ or hasattr(cls, to_attr)):
            raise ValueError(f"to_attr={to_attr!r} conflicts with an attribute of the {cls.__name__} model")
        if isinstance(descriptor, GenericForeignKey):
            if final is not None and final.sources() and not isinstance(final, GenericPrefetch):
                raise ValueError(f"use GenericPrefetch for the generic foreign key {cls.__name__}.{segment}")
        elif isinstance(final, GenericPrefetch):
            raise ValueError(f"GenericPrefetch needs a generic foreign key, {cls.__name__}.{segment} is not one")
        sources = final.sources() if final is not None else ()
        pending = [instance for instance in group if not _fetched(descriptor, instance, to_attr)]
        if pending:
            # A generic foreign key's value is cached for the fields as read before the operations.
            keys = [descriptor.values(i) if isinstance(descriptor, GenericForeignKey) else None for i in pending]
            values = await _fetcher(descriptor)(descriptor, cls, pending, sources, executor)
            for instance, value, key in zip(pending, values, keys, strict=True):
                _store(descriptor, instance, value, to_attr, key)
            fresh = _flatten(values)
            for query, _, lookups in sources:
                if lookups:
                    items = [item for item in fresh if item._require_meta().label == query.model]
                    await prefetch_related_objects(items, *lookups, executor=executor)
        related.extend(_value(descriptor, instance, to_attr) for instance in group)
    return _flatten(related)


def _fetched(descriptor: Any, instance: Model, to_attr: str | None) -> bool:
    if to_attr is not None:
        return _extra(instance, to_attr) is not _MISSING
    if isinstance(descriptor, ForeignKey | GenericForeignKey):
        return descriptor.cached(instance) is not _MISSING
    return descriptor.name in instance._state.related


def _value(descriptor: Any, instance: Model, to_attr: str | None) -> Any:
    if to_attr is not None:
        return _extra(instance, to_attr)
    if isinstance(descriptor, ForeignKey | GenericForeignKey):
        value = descriptor.cached(instance)
        return None if value is _MISSING else value
    return instance._state.related.get(descriptor.name)


def _store(descriptor: Any, instance: Model, value: Any, to_attr: str | None, key: Any) -> None:
    if to_attr is not None:
        setattr(instance, to_attr, list(value) if isinstance(value, tuple) else value)
    elif isinstance(descriptor, GenericForeignKey):
        descriptor.cache(instance, value, key)
    else:
        instance._state.related[descriptor.name] = value


def _source_for(
    sources: Sequence[tuple[Query, type[Model], tuple[Prefetch, ...]]], label: str, where: str
) -> tuple[Query, type[Model]] | None:
    """The query and model class of the prefetch query set of a relation to ``label``; ``ValueError`` for a query set
    on another model."""
    if not sources:
        return None
    query, model, _ = sources[0]
    if query.model != label:
        raise ValueError(f"the prefetch query set of {where} is on {query.model}, not on {label}")
    return query, model


def _loading(query: Query, *names: str) -> Query:
    """``query`` with ``names`` loaded although ``only()`` or ``defer()`` left them out."""
    changes: dict[str, Any] = {}
    if query.only_fields is not None and any(name not in query.only_fields for name in names):
        changes["only_fields"] = (*query.only_fields, *(name for name in names if name not in query.only_fields))
    if any(name in query.deferred_fields for name in names):
        changes["deferred_fields"] = tuple(name for name in query.deferred_fields if name not in names)
    return query._replace(**changes) if changes else query


async def _instances(executor: Executor, model: type[Model], query: Query) -> list[Model]:
    from adorm.operations import Select

    if query.is_empty:
        return []
    return [model.from_row(row, executor=executor) for row in await executor.execute(Select(query))]


async def _batched_instances(
    executor: Executor, model: type[Model], query: Query, field: str, keys: list[Any]
) -> list[Model]:
    """The instances of ``query`` filtered by ``<field>__in`` each batch of ``keys``."""
    items: list[Model] = []
    for batch in _batches(keys):
        items.extend(await _instances(executor, model, query.filter(**{f"{field}__in": batch})))
    return items


def _unique(values: Iterable[Any]) -> list[Any]:
    return list(dict.fromkeys(value for value in values if value is not None))


def _owner_pks(cls: type[Model], instances: list[Model], name: str) -> list[Any]:
    pks = [instance.pk for instance in instances]
    if any(pk is None for pk in pks):
        raise ValueError(f"{cls.__name__}.{name} needs a primary key, an instance has none")
    return _unique(pks)


async def _fetch_foreign_key(
    relation: ForeignKey, cls: type[Model], instances: list[Model], sources: Any, executor: Executor
) -> list[Any]:
    for instance in instances:
        if relation.column not in instance.__dict__:
            raise AttributeError(f"{cls.__name__}.{relation.name} needs {relation.column!r}, which is deferred")
    keys = _unique(instance.__dict__[relation.column] for instance in instances)
    source = _source_for(sources, relation.target, f"{cls.__name__}.{relation.name}")
    query, model = source if source is not None else (Query(relation.target), relation.target_model())
    by_pk = {item.pk: item for item in await _batched_instances(executor, model, query, "pk", keys)}
    return [by_pk.get(instance.__dict__[relation.column]) for instance in instances]


def _back_reference(model: type[Model], column: str, owner: type[Model]) -> ForeignKey | None:
    """The foreign key of ``model`` stored in ``column`` that points back to ``owner``."""
    label = owner._require_meta().label
    for relation in model.__adorm_relations__.values():
        if isinstance(relation, ForeignKey) and relation.column == column and relation.target == label:
            return relation
    return None


def _set_back_reference(items: Iterable[Model], column: str, owner: Model) -> None:
    for item in items:
        back = _back_reference(type(item), column, type(owner))
        if back is not None:
            item._state.related[back.name] = owner


async def _fetch_reverse(
    relation: ReverseForeignKey | ReverseOneToOne,
    cls: type[Model],
    instances: list[Model],
    sources: Any,
    executor: Executor,
) -> list[Any]:
    pks = _owner_pks(cls, instances, relation.name)
    source = _source_for(sources, relation.target, f"{cls.__name__}.{relation.name}")
    if source is not None:
        query, model = source
    elif isinstance(relation, ReverseOneToOne):
        query, model = Query(relation.target), relation.target_model()
    else:
        queryset = target_queryset(relation, executor)
        query, model = queryset.query, queryset.model
    query = _loading(query, relation.column)
    groups: dict[Any, list[Model]] = {}
    for item in await _batched_instances(executor, model, query, relation.column, pks):
        groups.setdefault(item.__dict__[relation.column], []).append(item)
    values: list[Any] = []
    for instance in instances:
        items = groups.get(instance.pk, [])
        _set_back_reference(items, relation.column, instance)
        if isinstance(relation, ReverseOneToOne):
            values.append(items[0] if items else None)
        else:
            values.append(tuple(items))
    return values


async def _fetch_many_to_many(
    relation: ManyToMany, cls: type[Model], instances: list[Model], sources: Any, executor: Executor
) -> list[Any]:
    from adorm.operations import Select

    pks = _owner_pks(cls, instances, relation.name)
    source = _source_for(sources, relation.target, f"{cls.__name__}.{relation.name}")
    if source is not None:
        query, model = source
    else:
        queryset = target_queryset(relation, executor)
        query, model = queryset.query, queryset.model
    targets: dict[Any, set[Any]] = {}
    for batch in _batches(pks):
        links = Query(relation.through).filter(**{f"{relation.source_column}__in": batch})
        for row in await executor.execute(Select(links.values(relation.source_column, relation.target_column))):
            targets.setdefault(row[relation.source_column], set()).add(row[relation.target_column])
    related: dict[Any, tuple[Model, ...]] = {}
    for owners in _owner_groups(pks, targets):
        # The targets of a group of owners come from one Select (several for an owner with more than BATCH_SIZE
        # targets), in the order of the query set.
        keys = _unique(key for pk in owners for key in targets[pk])
        items = await _batched_instances(executor, model, query, "pk", keys)
        for pk in owners:
            related[pk] = tuple(item for item in items if item.pk in targets[pk])
    return [related.get(instance.pk, ()) for instance in instances]


def _owner_groups(pks: list[Any], targets: dict[Any, set[Any]]) -> list[list[Any]]:
    """The owners with targets, grouped so that the targets of a group number at most `BATCH_SIZE` (an owner with more
    is a group of its own)."""
    groups: list[list[Any]] = []
    keys: set[Any] = set()
    for pk in pks:
        linked = targets.get(pk)
        if not linked:
            continue
        if groups and len(keys | linked) <= BATCH_SIZE:
            groups[-1].append(pk)
            keys |= linked
        else:
            groups.append([pk])
            keys = set(linked)
    return groups


async def _fetch_generic(
    relation: GenericForeignKey, cls: type[Model], instances: list[Model], sources: Any, executor: Executor
) -> list[Any]:
    from adorm.models.contenttypes import content_types
    from adorm.models.registry import registry

    pairs = [relation.values(instance) for instance in instances]
    wanted_types = _unique(content_type for content_type, object_id in pairs if object_id is not None)
    natural = await content_types.natural_keys(
        wanted_types, session=executor, content_type_model=relation.content_type_model(cls)
    )
    models = {content_type: registry.get_for_content_type(*key) for content_type, key in natural.items()}
    wanted: dict[type[Model], list[Any]] = {}
    keys: list[Any] = []
    for content_type, object_id in pairs:
        model = models.get(content_type) if object_id is not None else None
        key = None if model is None else coerce_pk(model, object_id)
        keys.append(key)
        if model is not None:
            wanted.setdefault(model, []).append(key)
    found: dict[tuple[type[Model], Any], Model] = {}
    for model, ids in wanted.items():
        label = model._require_meta().label
        query, target = next(((q, m) for q, m, _ in sources if q.model == label), (Query(label), model))
        for item in await _batched_instances(executor, target, query, "pk", _unique(ids)):
            found[(model, item.pk)] = item
    values: list[Any] = []
    for (content_type, object_id), key in zip(pairs, keys, strict=True):
        model = models.get(content_type) if object_id is not None else None
        values.append(None if model is None else found.get((model, key)))
    return values


def _fetcher(descriptor: Any) -> Any:
    if isinstance(descriptor, ForeignKey):
        return _fetch_foreign_key
    if isinstance(descriptor, ReverseForeignKey | ReverseOneToOne):
        return _fetch_reverse
    if isinstance(descriptor, ManyToMany):
        return _fetch_many_to_many
    return _fetch_generic
