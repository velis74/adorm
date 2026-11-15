"""Translation of adorm queries into Django query sets, and of the rows they return into adorm rows.

`translate()` turns a `Query` into a `TranslatedQuery`: a Django query set that selects the query's columns with
``values_list()`` and the plan that turns each tuple into a row keyed by adorm names. `model_queryset()`,
`field_kwargs()` and `lookup_kwargs()` give the model query set and the keyword arguments that writes pass to Django,
and `instance_row()` turns a Django model instance into an adorm row. Building them needs no database
connection, only Django's app registry. Field paths are translated segment by segment with the `ModelMeta.orm_names`
of the registered adorm model each segment reaches; a model without a registered adorm model uses Django's names.
"""

from __future__ import annotations

import datetime
import decimal
import itertools
import uuid

from collections.abc import Iterable, Mapping, Sequence
from types import MappingProxyType
from typing import Any

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist
from django.db import models as dj
from django.db.models import functions as djf
from django.db.models.expressions import Combinable, CombinedExpression
from django.db.models.fields.reverse_related import ForeignObjectRel

from adorm.exceptions import NotSupportedError, ProgrammingError
from adorm.models.registry import registry
from adorm.models.relations import ForeignKey
from adorm.query import expressions as ax
from adorm.query.expressions import split_field_path
from adorm.query.lookups import build_lookup, Lookup, split_lookup
from adorm.query.query import Query

__all__ = [
    "FUNCTIONS",
    "SELECT_RELATED_DEPTH",
    "TranslatedQuery",
    "django_model",
    "field_kwargs",
    "instance_row",
    "lookup_kwargs",
    "model_queryset",
    "orm_path",
    "output_field",
    "subquery",
    "translate",
]

FUNCTIONS: Mapping[str, type[dj.Func]] = MappingProxyType({"Lower": djf.Lower, "Upper": djf.Upper})
"""Django's function class for each adorm function class name the translation knows."""

_OUTPUT_FIELDS: Mapping[type, type[dj.Field]] = MappingProxyType(
    {
        bool: dj.BooleanField,
        int: dj.IntegerField,
        float: dj.FloatField,
        str: dj.TextField,
        decimal.Decimal: dj.DecimalField,
        datetime.datetime: dj.DateTimeField,
        datetime.date: dj.DateField,
        datetime.time: dj.TimeField,
        datetime.timedelta: dj.DurationField,
        uuid.UUID: dj.UUIDField,
        bytes: dj.BinaryField,
        dict: dj.JSONField,
        list: dj.JSONField,
    }
)

SELECT_RELATED_DEPTH = 5
"""How many relations deep ``select_related()`` without paths follows non-null foreign keys, as Django does."""


def django_model(label: str) -> type[dj.Model]:
    """The installed Django model with the label ``label`` (``"app_label.ModelName"``, auto-created many-to-many
    through models included). Raises `ProgrammingError` when there is none."""
    app_label, _, name = label.partition(".")
    try:
        return apps.get_model(app_label, name)
    except LookupError:
        raise ProgrammingError(f"no Django model is installed as {label!r}") from None


def _adorm_model(model: type[dj.Model] | None) -> Any:
    if model is None:
        return None
    try:
        return registry.get(model._meta.label)
    except LookupError:
        return None


def _orm_name(adorm: Any, name: str) -> str:
    return name if adorm is None else adorm._meta.orm_name(name)


def _field(model: type[dj.Model], name: str) -> Any:
    """The Django field, attname or relation ``name`` names on ``model``; a reverse relation is also found by its
    accessor name (``tagging_set``). ``None`` when there is none."""
    opts = model._meta
    if name == "pk":
        return opts.pk
    try:
        return opts.get_field(name)
    except FieldDoesNotExist:
        pass
    for relation in opts.related_objects:
        if relation.get_accessor_name() == name:
            return relation
    return None


def _query_name(field: Any, name: str) -> str:
    """The name Django's query paths use for ``field``, found under ``name``."""
    if name == "pk":
        return "pk"
    return field.name if isinstance(field, ForeignObjectRel) else name


def _follows(field: Any, name: str) -> bool:
    """Whether a path continues into the related model of ``field`` found under ``name`` (not into a column)."""
    if field is None or not field.is_relation or field.related_model is None:
        return False
    return name in ("pk", field.name) or isinstance(field, ForeignObjectRel)


def _path_field(model: type[dj.Model], path: str) -> Any:
    """The Django field the Django query path ``path`` on ``model`` ends on; ``None`` when it ends elsewhere."""
    field = None
    current: type[dj.Model] | None = model
    for name in path.split("__"):
        if current is None:
            return None
        field = _field(current, name)
        current = field.related_model if _follows(field, name) else None
    return field


def _adorm_name(adorm: Any, orm_name: str) -> str:
    """The adorm field name of the target ORM's field ``orm_name`` on the adorm model ``adorm``."""
    for name, orm in adorm._meta.orm_names.items():
        if orm == orm_name:
            return name
    return orm_name


def output_field(output_type: type) -> dj.Field:
    """The Django field for an adorm ``output_type``; raises `NotSupportedError` for a type without one."""
    field = _OUTPUT_FIELDS.get(output_type)
    if field is None:
        raise NotSupportedError(f"the Django driver has no field for the output type {output_type.__name__}")
    return field()


def orm_path(model: type[dj.Model], path: str, annotations: Iterable[str] = ()) -> str:
    """Django's query path for the adorm field path ``path`` on ``model``.

    Each segment is renamed with the `ModelMeta.orm_names` of the adorm model registered for the Django model it is
    on, and a reverse relation's accessor name becomes its query name (``tagging_set`` → ``tagging``). A path whose
    first segment is one of ``annotations`` is returned unchanged; a segment Django does not know is kept, so Django
    reports it.
    """
    segments = split_field_path(path)
    if segments[0] in annotations:
        return path
    names: list[str] = []
    current: type[dj.Model] | None = model
    for segment in segments:
        if current is None:
            names.append(segment)
            continue
        name = "pk" if segment == "pk" else _orm_name(_adorm_model(current), segment)
        field = _field(current, name)
        names.append(name if field is None else _query_name(field, name))
        current = field.related_model if _follows(field, name) else None
    return "__".join(names)


def _level(paths: Iterable[str]) -> tuple[set[str], dict[str, list[str]]]:
    """The first segments of ``paths`` without further segments, and the rest of the others by first segment."""
    direct: set[str] = set()
    nested: dict[str, list[str]] = {}
    for path in paths:
        head, *rest = split_field_path(path)
        if rest:
            nested.setdefault(head, []).append("__".join(rest))
        else:
            direct.add(head)
    return direct, nested


class _Columns:
    """The ``values_list()`` paths of a translation, each once."""

    def __init__(self) -> None:
        self.paths: list[str] = []
        self._index: dict[str, int] = {}

    def add(self, path: str) -> int:
        index = self._index.get(path)
        if index is None:
            index = self._index[path] = len(self.paths)
            self.paths.append(path)
        return index


class _RowPlan:
    """Builds a row from a tuple: adorm keys by column index, and the rows of related models by relation name."""

    __slots__ = ("fields", "pk", "relations")

    def __init__(
        self, fields: Sequence[tuple[str, int]], pk: int | None, relations: Sequence[tuple[str, _RowPlan]] = ()
    ) -> None:
        self.fields = tuple(fields)
        self.pk = pk
        self.relations = tuple(relations)

    def build(self, values: Sequence[Any]) -> dict[str, Any]:
        row = {name: values[index] for name, index in self.fields}
        for name, plan in self.relations:
            row[name] = None if values[plan.pk] is None else plan.build(values)  # type: ignore[index]
        return row


class TranslatedQuery:
    """A `Query` translated into Django's terms.

    ``queryset`` is the Django query set of the query's rows, selecting ``columns`` (Django query paths) with
    ``values_list()``; ``row()`` turns one of its tuples into a row keyed by adorm names: a model row with the loaded
    fields, the annotations and, under each relation name ``select_related()`` follows, the related row (``None`` when
    there is none), or a ``values()`` row keyed by the selected names.
    """

    __slots__ = ("_plan", "columns", "queryset")

    def __init__(self, queryset: Any, columns: Sequence[str], plan: _RowPlan) -> None:
        self.queryset = queryset
        self.columns = tuple(columns)
        self._plan = plan

    def row(self, values: Sequence[Any]) -> dict[str, Any]:
        """The adorm row of one tuple of ``queryset``."""
        return self._plan.build(values)

    def rows(self, records: Iterable[Sequence[Any]]) -> list[dict[str, Any]]:
        """The adorm rows of ``records``, tuples of ``queryset``."""
        return [self._plan.build(values) for values in records]


class _Translator:
    """Translates one `Query`; a subquery gets a translator of its own."""

    def __init__(self, query: Query) -> None:
        if not isinstance(query, Query):
            raise TypeError(f"expected a Query, not {type(query).__name__}")
        self.query = query
        self.model = django_model(query.model)
        self.annotation_names = tuple(name for name, _ in query.annotations)
        self.aliases: dict[str, Any] = {}
        self._alias_numbers = itertools.count()

    def path(self, name: str) -> str:
        return orm_path(self.model, name, self.annotation_names)

    # Expressions and conditions

    def expression(self, node: Any) -> Any:
        if isinstance(node, ax.OuterRef):
            raise NotSupportedError("the Django driver does not translate OuterRef")
        if isinstance(node, ax.F):
            return dj.F(self.path(node.name))
        if isinstance(node, ax.Value):
            if node.output_type is None:
                return dj.Value(node.value)
            return dj.Value(node.value, output_field=output_field(node.output_type))
        if isinstance(node, ax.Q):
            return self.condition(node)
        if isinstance(node, Lookup):
            return self.lookup(node)
        if isinstance(node, ax.Exists):
            raise NotSupportedError("the Django driver does not translate Exists")
        if isinstance(node, ax.Subquery):
            return dj.Subquery(subquery(node.query))
        if isinstance(node, ax.CombinedExpression):
            connector = Combinable.MOD if node.connector == "%" else node.connector
            output = None if node.output_type is None else output_field(node.output_type)
            return CombinedExpression(
                self.expression(node.lhs), connector, self.expression(node.rhs), output_field=output
            )
        if isinstance(node, ax.Func) and not isinstance(node, ax.Aggregate) and type(node).__name__ in FUNCTIONS:
            return FUNCTIONS[type(node).__name__](*(self.expression(e) for e in node.expressions))
        raise NotSupportedError(f"the Django driver does not translate {type(node).__name__}")

    def condition(self, node: ax.Q) -> dj.Q:
        children = [self.expression(child) for child in node.children]
        connector = dj.Q.OR if node.connector == ax.Q.OR else dj.Q.AND
        return dj.Q(*children, _connector=connector, _negated=node.negated)

    def operand(self, value: Any, field: Any = None) -> Any:
        """``value`` in Django's terms; a model instance stands for the value of the field ``field`` (the Django field
        the lookup path ends on) refers to: its ``to_field`` for a foreign key, else the primary key."""
        from adorm.models.base import Model

        if isinstance(value, ax.Node):
            return self.expression(value)
        if isinstance(value, Model):
            target = getattr(field, "target_field", None)
            if isinstance(field, ForeignObjectRel) or target is None or target.primary_key:
                return value.pk
            return getattr(value, _adorm_name(type(value), target.attname))
        return value

    def lookup(self, node: Lookup) -> dj.Q:
        lhs = node.lhs
        field = None
        if isinstance(lhs, ax.F) and not isinstance(lhs, ax.OuterRef):
            key = self.path(lhs.name)
            field = _path_field(self.model, key)
        else:
            key = f"_adorm{next(self._alias_numbers)}"
            self.aliases[key] = self.expression(lhs)
        rhs = node.rhs
        if isinstance(rhs, ax.Subquery) and not isinstance(rhs, ax.Exists) and node.lookup_name == "in":
            value: Any = subquery(rhs.query)
        elif isinstance(rhs, tuple):
            value = tuple(self.operand(item, field) for item in rhs)
        else:
            value = self.operand(rhs, field)
        return dj.Q(**{f"{key}__{node.lookup_name}": value})

    def write_value(self, name: str, value: Any) -> tuple[str, Any]:
        """The Django keyword and value of the adorm field ``name`` set to ``value``."""
        key = _orm_name(_adorm_model(self.model), name)
        return key, self.operand(value, _field(self.model, key))

    def lookup_item(self, key: str, value: Any) -> tuple[str, Any]:
        """The Django keyword and value of the lookup ``key=value``. A key without a lookup name and without ``__``
        names a field Django can also create the row with: a foreign key by its attname (``club_id``)."""
        path, name = split_lookup(key)
        if key == path and len(split_field_path(path)) == 1 and path not in self.annotation_names:
            orm = "pk" if path == "pk" else _orm_name(_adorm_model(self.model), path)
            field = _field(self.model, orm)
            if field is not None and field.concrete and field.is_relation and orm == field.name:
                orm = field.attname
            return orm, self.operand(value, field)
        if key == path and value is None:
            return self.path(path), None
        q = self.lookup(build_lookup(key, value))
        ((django_key, django_value),) = q.children
        if key == path:
            # Written without a lookup name, as the key was; its "__" keeps it out of the created row's values.
            django_key = django_key.rpartition("__")[0]
        return django_key, django_value

    def order(self, order: ax.OrderBy) -> Any:
        expression = self.expression(order.expression)
        options = {"nulls_first": order.nulls_first, "nulls_last": order.nulls_last}
        return expression.desc(**options) if order.descending else expression.asc(**options)

    # The query set

    def base(self) -> Any:
        """The Django query set of the model rows: manager, annotations, filters, ordering, distinct, slice, none and
        row locks."""
        query = self.query
        if query.manager is None:
            queryset = self.model._base_manager.all()
        else:
            manager = getattr(self.model, query.manager, None)
            if not isinstance(manager, dj.Manager):
                raise ProgrammingError(f"{query.model} has no Django manager {query.manager!r}")
            queryset = manager.all()
        if query.annotations:
            queryset = queryset.annotate(**{name: self.expression(value) for name, value in query.annotations})
        filters = [self.condition(condition) for condition in query.filters]
        ordering = [self.order(order) for order in query.ordering]
        if self.aliases:
            queryset = queryset.alias(**self.aliases)
        for condition in filters:
            # One filter() call per adorm filter: Django joins a multi-valued relation once per call.
            queryset = queryset.filter(condition)
        if ordering:
            queryset = queryset.order_by(*ordering)
        if query.is_distinct:
            queryset = queryset.distinct(*(self.path(name) for name in query.distinct_fields))
        if query.is_sliced:
            queryset = queryset[query.low_mark : query.high_mark]
        if query.is_none:
            queryset = queryset.none()
        lock = query.for_update
        if lock is not None:
            queryset = queryset.select_for_update(
                nowait=lock.nowait,
                skip_locked=lock.skip_locked,
                of=tuple(name if name == "self" else self.path(name) for name in lock.of),
                no_key=lock.no_key,
            )
        return queryset

    # Columns and rows

    def model_plan(
        self,
        columns: _Columns,
        model: type[dj.Model],
        path: str,
        prefix: str,
        only: set[str] | None,
        deferred: set[str],
        related: Mapping[str, Any],
    ) -> _RowPlan:
        """The plan of the rows of ``model`` reached by the adorm relation path ``path`` (Django prefix ``prefix``)."""
        adorm = _adorm_model(model)
        if adorm is not None:
            names = list(adorm.__pydantic_fields__)
            pk = adorm._meta.pk
            relations = adorm.__adorm_relations__
        else:
            names = [field.attname for field in model._meta.concrete_fields]
            pk = model._meta.pk.attname
            relations = {}

        def column(name: str) -> str:
            if name == "pk":
                return pk
            relation = relations.get(name)
            return relation.column if isinstance(relation, ForeignKey) else name

        only_columns = None if only is None else {column(name) for name in only}
        deferred_columns = {column(name) for name in deferred}
        fields = []
        for name in names:
            if name != pk and ((only_columns is not None and name not in only_columns) or name in deferred_columns):
                continue
            fields.append((name, columns.add(prefix + _orm_name(adorm, name))))
        pk_index = columns.add(prefix + _orm_name(adorm, pk))
        plans = []
        for name, subtree in related.items():
            field, orm = self.relation(model, adorm, name)
            child = f"{path}__{name}" if path else name
            nested_only, nested_deferred = self.restrictions(child)
            plan = self.model_plan(
                columns, field.related_model, child, f"{prefix}{orm}__", nested_only, nested_deferred, subtree
            )
            plans.append((name, plan))
        return _RowPlan(fields, pk_index, plans)

    def relation(self, model: type[dj.Model], adorm: Any, name: str) -> tuple[Any, str]:
        """The Django field and query name of the relation ``name`` that ``select_related()`` follows."""
        if adorm is not None and name not in adorm.__adorm_relations__:
            raise ProgrammingError(f"{adorm._meta.label} has no relation {name!r} for select_related()")
        orm = _orm_name(adorm, name)
        field = _field(model, orm)
        single = field is not None and (field.many_to_one or field.one_to_one) and field.related_model is not None
        if not single:
            raise ProgrammingError(
                f"{model._meta.label}: select_related() follows foreign keys and one-to-one relations, not {name!r}"
            )
        return field, _query_name(field, orm)

    def restrictions(self, path: str) -> tuple[set[str] | None, set[str]]:
        """The ``only()`` and ``defer()`` field names of the related rows at ``path``."""
        query = self.query
        prefix = split_field_path(path)
        only: set[str] | None = None
        if query.only_fields is not None:
            nested = [p for p in query.only_fields if split_field_path(p)[: len(prefix)] == prefix]
            rest = ["__".join(split_field_path(p)[len(prefix) :]) for p in nested if p != path]
            names, _ = _level(rest)
            only = names or None
        rest = [
            "__".join(split_field_path(p)[len(prefix) :])
            for p in query.deferred_fields
            if split_field_path(p)[: len(prefix)] == prefix and p != path
        ]
        deferred, _ = _level(rest)
        return only, deferred

    def related_tree(self) -> dict[str, Any]:
        """The relations ``select_related()`` follows, as a tree of adorm relation names."""
        selected = self.query.select_related_fields
        if selected is True:
            return _non_null_relations(self.model, 1)
        tree: dict[str, Any] = {}
        for path in selected or ():
            node = tree
            for segment in split_field_path(path):
                node = node.setdefault(segment, {})
        return tree

    def translate(self) -> TranslatedQuery:
        query = self.query
        queryset = self.base()
        columns = _Columns()
        if query.row_format == "model":
            only = None
            if query.only_fields is not None:
                only, _ = _level(query.only_fields)
            deferred, _ = _level(query.deferred_fields)
            plan = self.model_plan(columns, self.model, "", "", only, deferred, self.related_tree())
            plan = _RowPlan(
                (*plan.fields, *((name, columns.add(name)) for name in self.annotation_names)), plan.pk, plan.relations
            )
        else:
            names = query.select or (*self.field_names(), *self.annotation_names)
            fields = [(name, columns.add(name if name in self.annotation_names else self.path(name))) for name in names]
            plan = _RowPlan(fields, None)
        return TranslatedQuery(queryset.values_list(*columns.paths), columns.paths, plan)

    def field_names(self) -> list[str]:
        adorm = _adorm_model(self.model)
        if adorm is not None:
            return list(adorm.__pydantic_fields__)
        return [field.attname for field in self.model._meta.concrete_fields]


def _non_null_relations(model: type[dj.Model], depth: int) -> dict[str, Any]:
    """The tree of non-null foreign keys from ``model`` that Django's ``select_related()`` without paths follows,
    keyed by adorm relation names; relations the adorm model does not declare are left out."""
    if depth > SELECT_RELATED_DEPTH:
        return {}
    adorm = _adorm_model(model)
    names = {} if adorm is None else {_orm_name(adorm, name): name for name in adorm.__adorm_relations__}
    tree: dict[str, Any] = {}
    for field in model._meta.concrete_fields:
        if not field.is_relation or field.null or field.remote_field.parent_link:
            continue
        name = field.name if adorm is None else names.get(field.name)
        if name is not None:
            tree[name] = _non_null_relations(field.related_model, depth + 1)
    return tree


def translate(query: Query) -> TranslatedQuery:
    """``query`` as a `TranslatedQuery`.

    The query set starts from the Django manager `Query.manager` names, or from the model's ``_base_manager`` without
    one, so a manager's ``get_queryset()`` applies only through it; the model's ``Meta.ordering`` applies when the
    query has no ordering. Raises `ProgrammingError` for an unknown model, manager or ``select_related()`` path and
    `NotSupportedError` for a node the translation does not know; Django raises its own errors for unknown fields.
    """
    return _Translator(query).translate()


def subquery(query: Query) -> Any:
    """The Django query set of ``query`` used inside another query: its model rows (Django compares their primary
    key), or the selected column after ``values()`` / ``values_list()`` with fields."""
    translator = _Translator(query)
    queryset = translator.base()
    if query.row_format != "model" and query.select:
        names = translator.annotation_names
        queryset = queryset.values_list(*(n if n in names else translator.path(n) for n in query.select))
    return queryset


def model_queryset(query: Query) -> Any:
    """The Django query set of the model rows of ``query``, as `translate()` starts it: manager, annotations, filters,
    ordering, distinct, slice, ``none()`` and ``select_for_update()``, without selecting columns. Writes (``update()``,
    ``delete()``, ``get_or_create()``, ``update_or_create()``) run on it."""
    return _Translator(query).base()


def field_kwargs(query: Query | str, values: Mapping[str, Any] | Iterable[tuple[str, Any]]) -> dict[str, Any]:
    """The keyword arguments Django takes for the adorm field values ``values`` (a mapping or pairs) of the model of
    ``query`` (a `Query` or a model label): each name translated with the model's `ModelMeta.orm_names`, expressions
    translated as in ``query``, a model instance replaced by the value it stands for."""
    translator = _Translator(query if isinstance(query, Query) else Query(query))
    items = values.items() if isinstance(values, Mapping) else values
    return dict(translator.write_value(name, value) for name, value in items)


def lookup_kwargs(query: Query, lookups: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    """The keyword arguments of Django's ``get_or_create()`` and ``update_or_create()`` for the adorm ``lookups``
    (lookup key and value pairs) on ``query``'s model. A key without ``__`` stays a field name (a foreign key by its
    attname), which Django also creates the row with; a lookup across relations or with a lookup name only filters."""
    translator = _Translator(query)
    return dict(translator.lookup_item(key, value) for key, value in lookups)


def instance_row(instance: dj.Model) -> dict[str, Any]:
    """The adorm row of a Django model instance: the fields of the registered adorm model (the concrete fields by
    attname without one) that the instance has loaded. A deferred field and a field that holds an expression (the
    value is known only to the database) are left out."""
    model = type(instance)
    adorm = _adorm_model(model)
    deferred = instance.get_deferred_fields()
    if adorm is None:
        names = [(field.attname, field.attname) for field in model._meta.concrete_fields]
    else:
        names = []
        for name in adorm.__pydantic_fields__:
            field = _field(model, _orm_name(adorm, name))
            if field is not None and getattr(field, "attname", None) is not None:
                names.append((name, field.attname))
    row: dict[str, Any] = {}
    for name, attname in names:
        if attname in deferred or attname not in instance.__dict__:
            continue
        value = instance.__dict__[attname]
        if hasattr(value, "resolve_expression"):
            continue
        row[name] = value
    return row
