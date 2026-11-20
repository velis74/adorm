"""Translation of adorm queries into Django query sets, and of the rows they return into adorm rows.

`translate()` turns a `Query` into a `TranslatedQuery`: a Django query set that selects the query's columns with
``values_list()`` and the plan that turns each tuple into a row keyed by adorm names. `model_queryset()`,
`field_kwargs()` and `lookup_kwargs()` give the model query set and the keyword arguments that writes pass to Django,
and `instance_row()` turns a Django model instance into an adorm row. Building them needs no database
connection, only Django's app registry. Field paths are translated segment by segment with the `ModelMeta.orm_names`
of the registered adorm model each segment reaches; a model without a registered adorm model uses Django's names.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime
import decimal
import itertools
import re
import threading
import uuid
import weakref

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist
from django.db import models as dj, NotSupportedError as DjangoNotSupportedError
from django.db.models import functions as djf
from django.db.models.expressions import Combinable, CombinedExpression
from django.db.models.fields.reverse_related import ForeignObjectRel

from adorm.exceptions import DatabaseError, FieldError, NotSupportedError, ProgrammingError
from adorm.models.registry import registry
from adorm.models.relations import ForeignKey
from adorm.query import expressions as ax
from adorm.query.expressions import _has_sql, split_field_path, sql_constructs
from adorm.query.lookups import _apply_transforms, build_lookup, Exact, Lookup, split_lookup, Transform, TRANSFORMS
from adorm.query.query import Query
from adorm.uuids import uuid7

__all__ = [
    "AGGREGATES",
    "FUNCTIONS",
    "SELECT_RELATED_DEPTH",
    "TranslatedQuery",
    "aggregation",
    "bulk_create_options",
    "check_bulk_create",
    "check_in_bulk_field",
    "child_instance",
    "clean_exclude",
    "column_fields",
    "clean_instance",
    "clean_needs_row",
    "django_model",
    "field_kwargs",
    "instance_row",
    "lookup_kwargs",
    "model_queryset",
    "operation_model",
    "orm_path",
    "output_field",
    "own_constructs",
    "parent_link_field",
    "pinned_alias",
    "register_sqlite_function",
    "register_translation",
    "sqlite_functions",
    "validation_error",
    "subquery",
    "table_model",
    "translate",
]

FUNCTIONS: Mapping[str, type[dj.Func]] = MappingProxyType(
    {
        "Lower": djf.Lower,
        "Upper": djf.Upper,
        "Length": djf.Length,
        "Coalesce": djf.Coalesce,
        "Concat": djf.Concat,
        "Now": djf.Now,
        "TruncYear": djf.TruncYear,
        "TruncQuarter": djf.TruncQuarter,
        "TruncMonth": djf.TruncMonth,
        "TruncWeek": djf.TruncWeek,
        "TruncDay": djf.TruncDay,
        "TruncHour": djf.TruncHour,
        "TruncMinute": djf.TruncMinute,
        "TruncSecond": djf.TruncSecond,
        "TruncDate": djf.TruncDate,
        "TruncTime": djf.TruncTime,
        **{
            name: getattr(djf, name)
            for name in (
                *("Cast", "Collate", "Greatest", "Least", "NullIf", "JSONArray", "JSONObject", "Extract"),
                *("ExtractYear", "ExtractIsoYear", "ExtractQuarter", "ExtractMonth", "ExtractWeek", "ExtractDay"),
                *("ExtractWeekDay", "ExtractIsoWeekDay", "ExtractHour", "ExtractMinute", "ExtractSecond"),
                *("Abs", "ACos", "ASin", "ATan", "ATan2", "Ceil", "Cos", "Cot", "Degrees", "Exp", "Floor", "Ln"),
                *("Log", "Mod", "Pi", "Power", "Radians", "Random", "Round", "Sign", "Sin", "Sqrt", "Tan"),
                *("Chr", "Left", "LPad", "LTrim", "MD5", "Ord", "Repeat", "Replace", "Reverse", "Right", "RPad"),
                *("RTrim", "SHA1", "SHA224", "SHA256", "SHA384", "SHA512", "StrIndex", "Substr", "Trim"),
            )
            if hasattr(djf, name)
        },
    }
)
"""Django's function class for each adorm function class name the translation knows; `Trunc` translates to Django's
``Trunc``, or to ``TruncDate`` / ``TruncTime`` for those kinds. ``UUID4`` and ``UUID7`` have registered translations
(`register_translation()`)."""

AGGREGATES: Mapping[str, type[dj.Aggregate]] = MappingProxyType(
    {"Count": dj.Count, "Sum": dj.Sum, "Avg": dj.Avg, "Min": dj.Min, "Max": dj.Max}
)
"""Django's aggregate class for each adorm aggregate class name the translation knows."""

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


def table_model(table: str) -> type[dj.Model] | None:
    """The installed concrete Django model whose table is ``table``, one registered with adorm first; ``None`` when
    there is none."""
    models = [
        model
        for model in apps.get_models(include_auto_created=True)
        if not model._meta.proxy and model._meta.db_table == table
    ]
    return next((model for model in models if _adorm_model(model) is not None), models[0] if models else None)


def column_fields(model: type[dj.Model], columns: Iterable[str]) -> tuple[str, ...]:
    """The adorm field names of the columns of ``model``'s own table, in the order of ``columns``: the fields of the
    registered adorm model (the attnames without one). A column that is no field of the table is left out."""
    adorm = _adorm_model(model)
    local = model._meta.local_concrete_fields
    names: dict[str, str] = {}
    if adorm is None:
        names = {field.column: field.attname for field in local}
    else:
        for name in adorm.__pydantic_fields__:
            field = _field(model, _orm_name(adorm, name))
            if field in local:
                names.setdefault(field.column, name)
    return tuple(names[column] for column in columns if column in names)


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


def _check_key_lookup(model: type[dj.Model], path: str, lookup_name: str, key: str) -> None:
    """Raise `FieldError` when the Django query path ``path`` on ``model`` ends at or inside a JSON or hstore field
    that has no lookup ``lookup_name``: Django would read the name as one more key."""
    current: type[dj.Model] | None = model
    for name in path.split("__"):
        if current is None:
            return
        field = _field(current, name)
        if _internal_type(field) in ("JSONField", "HStoreField"):
            if field.get_lookup(lookup_name) is None:
                adorm = _adorm_model(current)
                label = current._meta.label if adorm is None else adorm._meta.label
                field_name = field.name if adorm is None else _adorm_name(adorm, field.name)
                message = f"unsupported lookup {lookup_name!r} for {label}.{field_name}"
                raise FieldError(message, model=label, key=key)
            return
        current = field.related_model if _follows(field, name) else None


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


_CONTAINER_FIELDS = frozenset({"JSONField", "HStoreField", "ArrayField"})
"""Internal types of the Django fields whose values a path continues into."""


def _internal_type(field: Any) -> str | None:
    get = getattr(field, "get_internal_type", None)
    return None if get is None else get()


def orm_path(
    model: type[dj.Model],
    path: str,
    annotations: Iterable[str] = (),
    lookup: type[Lookup] | None = None,
    *,
    implicit: bool = False,
) -> str:
    """Django's query path for the adorm field path ``path`` on ``model``.

    Each segment is renamed with the `ModelMeta.orm_names` of the adorm model registered for the Django model it is
    on, and a reverse relation's accessor name becomes its query name (``tagging_set`` → ``tagging``). A path whose
    first segment is one of ``annotations`` is returned unchanged; a segment Django does not know is kept, so Django
    reports it. Segments after a field that is neither a relation nor a JSON field are transforms, kept by name
    (``joined__year``); one that is no registered transform applying to the field's adorm type raises `FieldError`.
    ``lookup`` is the lookup class the path is compared with: one whose ``applies_to()`` refuses the adorm type of a
    scalar field (after its transforms) raises `FieldError` too. ``implicit`` tells that the key named no lookup
    (``joined__yeer``): the message then calls an unknown last transform name a lookup.
    """
    return "__".join(_orm_names(model, path, annotations, lookup, implicit=implicit)[0])


def _orm_names(
    model: type[dj.Model],
    path: str,
    annotations: Iterable[str] = (),
    lookup: type[Lookup] | None = None,
    *,
    implicit: bool = False,
) -> tuple[list[str], int | None]:
    """The segments of `orm_path()` and the index of the first transform among them (``None`` without one)."""
    segments = split_field_path(path)
    if segments[0] in annotations:
        return segments, 1 if len(segments) > 1 else None
    names: list[str] = []
    current: type[dj.Model] | None = model
    for index, segment in enumerate(segments):
        if current is None:
            names.append(segment)
            continue
        adorm = _adorm_model(current)
        name = "pk" if segment == "pk" else _orm_name(adorm, segment)
        field = _field(current, name)
        names.append(name if field is None else _query_name(field, name))
        follows = _follows(field, name)
        rest = segments[index + 1 :]
        scalar = field is not None and not field.is_relation and _internal_type(field) not in _CONTAINER_FIELDS
        if (rest or lookup is not None) and scalar:
            label = current._meta.label if adorm is None else adorm._meta.label
            schema = registry.model_schema(label) if adorm is not None else None
            field_type = None if schema is None else schema.fields.get(segment)
            _, field_type = _apply_transforms(
                f"{label}.{segment}", field_type, tuple(rest), path, label, lookup_hint=implicit
            )
            if lookup is not None and schema is not None and not lookup.applies_to(field_type, ()):
                target = "__".join((f"{label}.{segment}", *rest))
                raise FieldError(f"unsupported lookup {lookup.lookup_name!r} for {target}", model=label, key=path)
            return [*names, *rest], len(names) if rest else None
        if rest and field is not None and not follows and _internal_type(field) not in _CONTAINER_FIELDS:
            label = current._meta.label if adorm is None else adorm._meta.label
            _apply_transforms(f"{label}.{segment}", None, tuple(rest), path, label, lookup_hint=False)
            return [*names, *rest], len(names)
        current = field.related_model if follows else None
    return names, None


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
    """The ``values_list()`` arguments of a translation, each once: Django paths, or expressions keyed by the adorm
    path they select."""

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.values: list[Any] = []
        self._index: dict[str, int] = {}

    def add(self, value: Any, key: str | None = None) -> int:
        path = value if isinstance(value, str) else key
        index = self._index.get(path)
        if index is None:
            index = self._index[path] = len(self.paths)
            self.paths.append(path)
            self.values.append(value)
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


def _cast_field(node: ax.Cast) -> dj.Field:
    """The Django field ``node`` casts to: a ``CharField`` with ``max_length``, a ``DecimalField`` with
    ``max_digits`` and ``decimal_places``, else `output_field`."""
    if node.max_length is not None:
        return dj.CharField(max_length=node.max_length)
    if node.max_digits is not None:
        return dj.DecimalField(max_digits=node.max_digits, decimal_places=node.decimal_places)
    return output_field(node.output_type)


def _output_options(node: Any, implied: Any = None) -> dict[str, Any]:
    """``output_field`` for a node whose ``output_type`` differs from the type its class or arguments imply."""
    output_type = node.output_type
    if output_type is None or output_type is implied or output_type is getattr(type(node), "default_output_type", None):
        return {}
    return {"output_field": output_field(output_type)}


# Own expressions: SQL templates, registered translations and SQLite functions


@dataclass(frozen=True)
class _Translation:
    function: Callable[..., Any]
    vendors: frozenset[str] | None


_TRANSLATIONS: weakref.WeakKeyDictionary[type, _Translation] = weakref.WeakKeyDictionary()


def register_translation(cls: type, *, vendors: Iterable[str] | None = None) -> Callable[[Any], Any]:
    """Register a translation of the adorm expression or transform class ``cls`` for the Django-based drivers; returns
    a decorator of the translation, ``translation(node, translate)`` for an expression class, ``translation(lhs)`` for
    a `Transform` subclass.

    An expression's translation gets a node of exactly ``cls`` and ``translate``, which translates a child node (one of
    ``node.get_source_expressions()``) into a Django expression, and returns the Django expression of the node. A
    transform's translation gets the Django expression of the transformed value and returns the Django expression of
    the transformed value. A translation runs instead of the class's SQL template and the bundled translation. The
    Django driver and the native PostgreSQL driver declare ``cls.construct`` on databases of ``vendors`` (Django's
    vendor names; every vendor by default). A later registration for ``cls`` replaces the earlier one. Raises
    ``TypeError`` when ``cls`` is not an adorm expression or transform class with a construct name, or ``vendors`` is
    a string.
    """
    if not (isinstance(cls, type) and issubclass(cls, (ax.Node, Transform)) and cls.construct):
        raise TypeError(f"{cls!r} is not an adorm expression or transform class with a construct name")
    if isinstance(vendors, str):
        raise TypeError("vendors is an iterable of vendor names, not a string")
    names = None if vendors is None else frozenset(vendors)

    def register(translation: Any) -> Any:
        if not callable(translation):
            raise TypeError(f"a translation is a callable, not {type(translation).__name__}")
        _TRANSLATIONS[cls] = _Translation(translation, names)
        return translation

    return register


def own_constructs(vendor: str) -> frozenset[str]:
    """The constructs the Django-based drivers declare on databases of ``vendor`` beyond their bundled ones: those
    with SQL for the vendor (`adorm.query.sql_constructs()`) and those with a translation registered for it."""
    translated = {
        cls.construct
        for cls, translation in list(_TRANSLATIONS.items())
        if translation.vendors is None or vendor in translation.vendors
    }
    return sql_constructs(vendor) | translated


@dataclass(frozen=True)
class _SQLiteFunction:
    name: str
    num_params: int
    function: Callable[..., Any]
    deterministic: bool


_SQLITE_FUNCTIONS: dict[tuple[str, int], _SQLiteFunction] = {}


def register_sqlite_function(
    name: str, num_params: int, function: Callable[..., Any], *, deterministic: bool = False
) -> Callable[..., Any]:
    """Register the Python callable ``function`` as the SQL function ``name`` of ``num_params`` arguments (``-1`` for
    any number) on the Django driver's SQLite connections, as ``sqlite3.Connection.create_function()`` does; an open
    connection gets it before its next call. ``deterministic`` tells SQLite that the result depends on the arguments
    only. A later registration of the same name (in any case) and number of arguments replaces the earlier one.
    Returns ``function``."""
    if not isinstance(name, str) or not name:
        raise TypeError("name is the SQL function's name")
    if not callable(function):
        raise TypeError(f"function is a callable, not {type(function).__name__}")
    entry = _SQLiteFunction(name, int(num_params), function, bool(deterministic))
    _SQLITE_FUNCTIONS[(name.lower(), entry.num_params)] = entry
    return function


def sqlite_functions() -> list[tuple[tuple[str, int], Any]]:
    """The registered SQLite functions (`register_sqlite_function()`) keyed by name in lower case and number of
    arguments; each has ``name``, ``num_params``, ``function`` and ``deterministic``."""
    return list(_SQLITE_FUNCTIONS.items())


_DJANGO_CLASSES: weakref.WeakKeyDictionary[type, type] = weakref.WeakKeyDictionary()


def _django_class(cls: type, base: type) -> Any:
    """The Django class derived from ``base`` that renders the SQL of the adorm class ``cls``."""
    found = _DJANGO_CLASSES.get(cls)
    if found is None:
        attrs: dict[str, Any] = {"_adorm": weakref.ref(cls), "__module__": __name__}
        if getattr(cls, "lookup_name", None):
            attrs["lookup_name"] = cls.lookup_name
        found = _DJANGO_CLASSES[cls] = type(cls.__name__, (base,), attrs)
    return found


def _template(node: Any, connection: Any) -> tuple[type, str]:
    """The adorm class of the Django node ``node`` and its SQL template for ``connection``'s vendor."""
    cls = node._adorm()
    template = None if cls is None else cls.sql_template(connection.vendor)
    if template is None:
        name = "the expression" if cls is None else cls.construct
        raise DjangoNotSupportedError(f"{name} has no SQL template for {connection.vendor}")
    return cls, template


class _TemplateSQL:
    """Renders the SQL template of an adorm function or transform; the arguments' parameters repeat for each
    ``%(expressions)s`` in it."""

    _adorm: Callable[[], type | None]

    def as_sql(self, compiler: Any, connection: Any, **extra_context: Any) -> tuple[str, Any]:
        for name in ("function", "template", "arg_joiner"):
            extra_context.pop(name, None)
        cls, template = _template(self, connection)
        sql, params = super().as_sql(  # type: ignore[misc]
            compiler,
            connection,
            function=getattr(cls, "function", None),
            template=template,
            arg_joiner=getattr(cls, "arg_joiner", ", "),
            **extra_context,
        )
        count = template.count("%(expressions)s") + template.count("%(field)s")
        return sql, params if count == 1 else params * count


class _TemplateFunc(_TemplateSQL, dj.Func):
    pass


class _TemplateTransform(_TemplateSQL, dj.Transform):
    pass


_LOOKUP_PARTS = re.compile(r"%\((lhs|rhs)\)s")


def _template_lookup_class() -> type:
    from django.db.models.lookups import FieldGetDbPrepValueMixin

    class _TemplateLookup(FieldGetDbPrepValueMixin, dj.Lookup):
        """Renders the SQL template of an adorm lookup with ``%(lhs)s`` and ``%(rhs)s``; their parameters follow the
        order in which they appear in it."""

        _adorm: Callable[[], type | None]

        def resolve_expression(self, *args: Any, **kwargs: Any) -> Any:
            resolved = super().resolve_expression(*args, **kwargs)
            resolved.rhs = resolved.get_prep_lookup()
            return resolved

        def as_sql(self, compiler: Any, connection: Any) -> tuple[str, Any]:
            _, template = _template(self, connection)
            lhs, lhs_params = self.process_lhs(compiler, connection)
            rhs, rhs_params = self.process_rhs(compiler, connection)
            parts = {"lhs": lhs_params, "rhs": rhs_params}
            params = [param for name in _LOOKUP_PARTS.findall(template) for param in parts[name]]
            return template % {"lhs": lhs, "rhs": rhs}, tuple(params) if isinstance(lhs_params, tuple) else params

    return _TemplateLookup


_TemplateLookup = _template_lookup_class()


def _own_transform(name: str) -> type | None:
    """The registered transform ``name`` when it has SQL of its own or a registered translation."""
    cls = TRANSFORMS.get(name)
    return cls if cls is not None and (cls in _TRANSLATIONS or _has_sql(cls)) else None


def _first_own_transform(names: Sequence[str], start: int | None) -> int | None:
    """The index of the first transform with SQL of its own or a translation in the Django path segments ``names``,
    whose transforms begin at ``start``."""
    if start is None:
        return None
    return next((index for index in range(start, len(names)) if _own_transform(names[index])), None)


def _apply_own_transforms(expression: Any, names: Sequence[str]) -> Any:
    """``expression`` with the transforms ``names`` applied, the first of which has SQL of its own or a translation."""
    for name in names:
        cls = _own_transform(name)
        if cls is None:
            raise NotSupportedError(
                f"the Django driver applies the transform {name!r} only before a transform with SQL of its own or a "
                "translation"
            )
        translation = _TRANSLATIONS.get(cls)
        if translation is not None:
            expression = translation.function(expression)
            continue
        output_type = cls.output_type(None)
        options = {} if output_type is None else {"output_field": output_field(output_type)}
        expression = _django_class(cls, _TemplateTransform)(expression, **options)
    return expression


# UUID4 and UUID7: Django's functions where Django and the database have them, else the database's own function or a
# SQLite function of adorm's that generates the value in Python

_DJANGO_UUID4: Any = getattr(djf, "UUID4", None)
_DJANGO_UUID7: Any = getattr(djf, "UUID7", None)


def _sqlite_uuid4() -> str:
    return uuid.uuid4().hex


def _sqlite_uuid7() -> str:
    return uuid7().hex


class _UUID4(dj.Func):
    """``UUID4`` where Django has none: ``GEN_RANDOM_UUID()`` on PostgreSQL, ``ADORM_UUID4()`` on SQLite."""

    arity = 0
    output_field = dj.UUIDField()

    def as_sql(self, compiler: Any, connection: Any, **extra_context: Any) -> tuple[str, Any]:  # noqa: ARG002
        raise DjangoNotSupportedError("UUID4 is not supported on this database backend.")

    def as_postgresql(self, compiler: Any, connection: Any, **extra_context: Any) -> tuple[str, Any]:
        return dj.Func.as_sql(self, compiler, connection, template="GEN_RANDOM_UUID()", **extra_context)

    def as_sqlite(self, compiler: Any, connection: Any, **extra_context: Any) -> tuple[str, Any]:
        return dj.Func.as_sql(self, compiler, connection, template="ADORM_UUID4()", **extra_context)


class _UUID7(dj.Func):
    """``UUID7``: Django's where Django and the database have it; else PostgreSQL 18's ``UUIDV7()`` and
    ``ADORM_UUID7()`` on SQLite (without a shift). PostgreSQL before 18 refuses it."""

    output_field = dj.UUIDField()

    def native(self, connection: Any) -> Any:
        """Django's ``UUID7`` of the same arguments when the database has the function, else ``None``."""
        if _DJANGO_UUID7 is None or not getattr(connection.features, "supports_uuid7_function", False):
            return None
        return _DJANGO_UUID7(*self.get_source_expressions(), output_field=self.output_field)

    def as_sql(self, compiler: Any, connection: Any, **extra_context: Any) -> tuple[str, Any]:  # noqa: ARG002
        if _DJANGO_UUID7 is None:
            raise DjangoNotSupportedError("UUID7 is not supported on this database backend.")
        return compiler.compile(_DJANGO_UUID7(*self.get_source_expressions(), output_field=self.output_field))

    def as_postgresql(self, compiler: Any, connection: Any, **extra_context: Any) -> tuple[str, Any]:
        native = self.native(connection)
        if native is not None:
            return compiler.compile(native)
        if connection.pg_version < 180000:
            raise DjangoNotSupportedError("UUID7 requires PostgreSQL version 18 or later.")
        template = "UUIDV7(CAST(%(expressions)s AS INTERVAL))" if self.source_expressions else "UUIDV7()"
        return dj.Func.as_sql(self, compiler, connection, template=template, **extra_context)

    def as_sqlite(self, compiler: Any, connection: Any, **extra_context: Any) -> tuple[str, Any]:
        native = self.native(connection)
        if native is not None:
            return compiler.compile(native)
        if self.source_expressions:
            raise DjangoNotSupportedError("The shift argument to UUID7 is not supported on SQLite.")
        return dj.Func.as_sql(self, compiler, connection, template="ADORM_UUID7()", **extra_context)


_FALLBACK_VENDORS = ("postgresql", "sqlite")
"""The vendors adorm runs ``UUID4`` and ``UUID7`` on where the installed Django lacks the function."""


@register_translation(ax.UUID4, vendors=None if _DJANGO_UUID4 is not None else _FALLBACK_VENDORS)
def _translate_uuid4(node: ax.UUID4, translate: Callable[[Any], Any]) -> Any:  # noqa: ARG001 - no arguments
    function = _DJANGO_UUID4 if _DJANGO_UUID4 is not None else _UUID4
    return function(**_output_options(node))


@register_translation(ax.UUID7, vendors=None if _DJANGO_UUID7 is not None else _FALLBACK_VENDORS)
def _translate_uuid7(node: ax.UUID7, translate: Callable[[Any], Any]) -> Any:
    return _UUID7(*(translate(e) for e in node.expressions), **_output_options(node))


if _DJANGO_UUID4 is None:
    register_sqlite_function("ADORM_UUID4", 0, _sqlite_uuid4)
if _DJANGO_UUID7 is None or not hasattr(uuid, "uuid7"):
    register_sqlite_function("ADORM_UUID7", 0, _sqlite_uuid7)


class _Translator:
    """Translates one `Query`; a subquery gets a translator of its own, with the outer query's as ``parent``."""

    def __init__(self, query: Query, parent: _Translator | None = None) -> None:
        if not isinstance(query, Query):
            raise TypeError(f"expected a Query, not {type(query).__name__}")
        self.query = query
        self.parent = parent
        self.model = django_model(query.model)
        self.annotation_names = tuple(name for name, _ in query.annotations)
        self.selected_annotations = tuple(name for name in self.annotation_names if name not in query.aliases)
        self.aliases: dict[str, Any] = {}
        self._applied: set[str] = set()
        self._alias_numbers = itertools.count() if parent is None else parent._alias_numbers

    def path(self, name: str, lookup: type[Lookup] | None = None, *, implicit: bool = False) -> str:
        return orm_path(self.model, name, self.annotation_names, lookup, implicit=implicit)

    def own_transform(self, name: str) -> tuple[list[str], int] | None:
        """The Django path segments of the adorm path ``name`` and the index of its first transform with SQL of its
        own or a translation; ``None`` without one."""
        names, start = _orm_names(self.model, name, self.annotation_names)
        index = _first_own_transform(names, start)
        return None if index is None else (names, index)

    def reference(self, name: str) -> Any:
        """Django's expression for the adorm field path ``name``: an ``F`` of its Django path, with the transforms
        from the first one with SQL of its own or a translation on applied as expressions."""
        own = self.own_transform(name)
        if own is None:
            return dj.F(self.path(name))
        names, index = own
        return _apply_own_transforms(dj.F("__".join(names[:index])), names[index:])

    def column(self, name: str) -> Any:
        """The ``values_list()`` argument that selects the adorm name ``name``: an annotation's name, a Django path,
        or the expression of a path with a transform that has SQL of its own or a translation."""
        if name in self.annotation_names:
            return name
        return self.reference(name) if self.own_transform(name) is not None else self.path(name)

    def field_path(self, name: str) -> str | None:
        """The Django field path of the adorm path ``name``, following annotations that are plain field references;
        ``None`` when it starts at another annotation."""
        annotations = dict(self.query.annotations)
        segments = split_field_path(name)
        seen: set[str] = set()
        while segments[0] in annotations:
            value = annotations[segments[0]]
            if type(value) is not ax.F or segments[0] in seen:
                return None
            seen.add(segments[0])
            segments = [*split_field_path(value.name), *segments[1:]]
        return self.path("__".join(segments))

    def subquery(self, query: Query) -> Any:
        """The Django query set of ``query`` inside this one; its `OuterRef` refers to this query."""
        return _Translator(query, self).selected()

    def selected(self) -> Any:
        """The Django query set of the query used inside another query: its model rows, or the selected columns after
        ``values()`` / ``values_list()`` with fields. The combined queries of a set operation select the primary key
        for model rows."""
        query = self.query
        if query.is_combined:
            parts = []
            for part in self.parts():
                queryset = part.selected()
                parts.append(queryset.values_list("pk") if part.query.row_format == "model" else queryset)
            return self.combined(parts)
        queryset = self.base()
        if query.row_format != "model" and query.select:
            queryset = queryset.values_list(*(self.column(n) for n in query.select))
        return queryset

    # Set operations

    def parts(self) -> list[_Translator]:
        """The translators of the combined queries, at this query's level. A combined query that selects model rows
        selects the names of this query's ``values()`` or ``values_list()``."""
        query = self.query
        parts = query.combined_queries
        if query.row_format != "model":
            parts = tuple(part.values(*query.select) if part.row_format == "model" else part for part in parts)
        return [_Translator(part, self.parent) for part in parts]

    def combined(self, parts: Sequence[Any]) -> Any:
        """The Django query set of the set operation over the query sets ``parts``, ordered and sliced."""
        query = self.query
        # Ordering of an unsliced part has no effect; Django 5.2 keeps it when only one part remains, which PostgreSQL
        # refuses under the combined ordering.
        first, *rest = (
            part if part.query.is_sliced or part.query.distinct_fields else part.order_by() for part in parts
        )
        if query.is_none:
            return first.none()
        if query.combinator == "union":
            queryset = first.union(*rest, all=query.combinator_all)
        else:
            queryset = getattr(first, query.combinator)(*rest)
        if query.ordering:
            queryset = queryset.order_by(*(self.combined_order(order) for order in query.ordering))
        if query.is_sliced:
            queryset = queryset[query.low_mark : query.high_mark]
        return queryset

    def combined_order(self, order: ax.OrderBy) -> Any:
        """The Django ordering of the combined rows by ``order``: a column of the combined rows is named as the first
        query that is not combined selects it."""
        query = self.query
        names = query.select if query.row_format != "model" else ()
        expression = order.expression
        if names and isinstance(expression, ax.F) and expression.name not in names:
            raise DatabaseError("ORDER BY term does not match any column in the result set.")
        first = query
        while first.is_combined:
            inner = first.combined_queries[0]
            outer_names = first.select if first.row_format != "model" else ()
            inner_names = inner.select if inner.row_format != "model" else ()
            if isinstance(expression, ax.F) and expression.name in outer_names and len(inner_names) == len(outer_names):
                expression = ax.F(inner_names[outer_names.index(expression.name)])
            first = inner
        if expression is not order.expression:
            order = order._replace(expression=expression)
        return _Translator(first, self.parent).order(order)

    def translate_combined(self) -> TranslatedQuery:
        """The `TranslatedQuery` of a set operation: the combined queries' rows are built as the first one's, keyed
        by this query's selected names."""
        query = self.query
        parts = [part.translate() for part in self.parts()]
        first = parts[0]
        queryset = self.combined([part.queryset for part in parts])
        plan = first._plan
        if query.row_format != "model" and query.select:
            if len(query.select) != len(plan.fields):
                raise ProgrammingError(
                    f"values() names {len(query.select)} columns, the combined queries select {len(plan.fields)}"
                )
            plan = _RowPlan(zip(query.select, (index for _, index in plan.fields), strict=True), None)
        return TranslatedQuery(queryset, first.columns, plan)

    # Expressions and conditions

    def expression(self, node: Any) -> Any:
        translation = _TRANSLATIONS.get(type(node))
        if translation is not None:
            return translation.function(node, self.expression)
        if isinstance(node, ax.Func) and not isinstance(node, ax.Aggregate) and _has_sql(type(node)):
            arguments = [self.expression(e) for e in node.expressions]
            options = {} if node.output_type is None else {"output_field": output_field(node.output_type)}
            return _django_class(type(node), _TemplateFunc)(*arguments, **options)
        if isinstance(node, ax.OuterRef):
            target: _Translator | None = self
            for _ in range(node.levels):
                target = target.parent if target is not None else None
            if target is None:
                raise ProgrammingError(f"{node!r} is outside a subquery: it has no outer query to refer to")
            reference: Any = target.path(node.name)
            for _ in range(node.levels):
                reference = dj.OuterRef(reference)
            return reference
        if isinstance(node, ax.F):
            return self.reference(node.name)
        if isinstance(node, ax.Value):
            if node.output_type is None:
                return dj.Value(node.value)
            return dj.Value(node.value, output_field=output_field(node.output_type))
        if isinstance(node, ax.Q):
            return self.condition(node)
        if isinstance(node, Lookup):
            return self.lookup(node)
        if isinstance(node, ax.Exists):
            exists = dj.Exists(self.subquery(node.query))
            return ~exists if node.negated else exists
        if isinstance(node, ax.Subquery):
            return dj.Subquery(self.subquery(node.query), **_output_options(node))
        if isinstance(node, ax.CombinedExpression):
            connector = Combinable.MOD if node.connector == "%" else node.connector
            output = None if node.output_type is None else output_field(node.output_type)
            return CombinedExpression(
                self.expression(node.lhs), connector, self.expression(node.rhs), output_field=output
            )
        if isinstance(node, ax.Star):
            return dj.expressions.Star()
        if isinstance(node, ax.Case):
            cases = [dj.When(self.expression(c.condition), then=self.expression(c.result)) for c in node.cases]
            return dj.Case(*cases, default=self.expression(node.default), **_output_options(node))
        name = type(node).__name__
        if isinstance(node, ax.Aggregate) and name in AGGREGATES:
            options = _output_options(node)
            if node.distinct:
                options["distinct"] = True
            if node.filter is not None:
                options["filter"] = self.expression(node.filter)
            if node.default is not None:
                options["default"] = self.expression(node.default)
            return AGGREGATES[name](*(self.expression(e) for e in node.expressions), **options)
        if type(node) is ax.Trunc:
            (source,) = (self.expression(e) for e in node.expressions)
            if node.kind in ("date", "time"):
                function = djf.TruncDate if node.kind == "date" else djf.TruncTime
                implied = datetime.date if node.kind == "date" else datetime.time
                return function(source, **_output_options(node, implied))
            return djf.Trunc(source, node.kind, **_output_options(node))
        if isinstance(node, ax.Func) and not isinstance(node, ax.Aggregate) and name in FUNCTIONS:
            arguments = [self.expression(e) for e in node.expressions]
            if isinstance(node, ax.Cast):
                return djf.Cast(arguments[0], _cast_field(node))
            if isinstance(node, ax.Collate):
                return djf.Collate(arguments[0], node.collation)
            if isinstance(node, ax.JSONObject):
                return djf.JSONObject(**dict(zip(node.keys, arguments, strict=True)))
            if type(node) is ax.Extract:
                return djf.Extract(arguments[0], node.lookup_name, **_output_options(node))
            if isinstance(node, ax.Concat):
                # A text result also for arguments of different field types (CharField and TextField).
                return djf.Concat(*arguments, **(_output_options(node) or {"output_field": dj.TextField()}))
            implied = {"TruncDate": datetime.date, "TruncTime": datetime.time}.get(name)
            return FUNCTIONS[name](*arguments, **_output_options(node, implied))
        raise NotSupportedError(f"the Django driver does not translate {name}")

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

    def own_lookup(self, node: Lookup) -> dj.Q:
        """The condition of a lookup with SQL of its own."""
        lhs = node.lhs
        field = None
        if isinstance(lhs, ax.F) and not isinstance(lhs, ax.OuterRef):
            field = _path_field(self.model, self.path(lhs.name, type(node)))
        rhs = node.rhs
        value = tuple(self.operand(item, field) for item in rhs) if isinstance(rhs, tuple) else self.operand(rhs, field)
        return dj.Q(_django_class(type(node), _TemplateLookup)(self.expression(lhs), value))

    def lookup(self, node: Lookup) -> dj.Q:
        if _has_sql(type(node)):
            return self.own_lookup(node)
        lhs = node.lhs
        field = None
        if isinstance(lhs, ax.F) and not isinstance(lhs, ax.OuterRef):
            key = self.path(lhs.name, type(node), implicit=isinstance(node, Exact) and node.implicit)
            if self.own_transform(lhs.name) is not None:
                key = f"_adorm{next(self._alias_numbers)}"
                self.aliases[key] = self.reference(lhs.name)
            else:
                field = _path_field(self.model, key)
                field_path = self.field_path(lhs.name)
                if field_path is not None:
                    _check_key_lookup(self.model, field_path, node.lookup_name, f"{lhs.name}__{node.lookup_name}")
        else:
            key = f"_adorm{next(self._alias_numbers)}"
            self.aliases[key] = self.expression(lhs)
        rhs = node.rhs
        if rhs is None and (field is not None or not isinstance(lhs, ax.F) or isinstance(lhs, ax.OuterRef)):
            # At a key inside a JSON field Django matches JSON null; here None is SQL NULL.
            return dj.Q(**{f"{key}__isnull": True})
        if isinstance(rhs, ax.Subquery) and not isinstance(rhs, ax.Exists) and node.lookup_name in ("in", "overlap"):
            value: Any = self.subquery(rhs.query)
            if node.lookup_name == "overlap" and (rhs.query.row_format == "model" or not rhs.query.select):
                value = value.values_list("pk")  # ARRAY(subquery) takes one column
        elif isinstance(rhs, tuple) and node.lookup_name == "overlap":
            value = [self.operand(item, field) for item in rhs]  # an empty tuple is no array literal
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
        (child,) = q.children
        if not isinstance(child, tuple) or child[0] in self.aliases:
            raise NotSupportedError(
                f"{key!r}: get_or_create() and update_or_create() take no lookup or transform with SQL of its own or "
                "a translation"
            )
        django_key, django_value = child
        if key == path:
            # Written without a lookup name, as the key was; its "__" keeps it out of the created row's values.
            django_key = django_key.rpartition("__")[0]
        return django_key, django_value

    def order(self, order: ax.OrderBy) -> Any:
        expression = self.expression(order.expression)
        options = {"nulls_first": order.nulls_first, "nulls_last": order.nulls_last}
        return expression.desc(**options) if order.descending else expression.asc(**options)

    # The query set

    def annotated(self, queryset: Any) -> Any:
        """``queryset`` with the filters, annotations and aliases of the query in the order they were added, grouped
        by ``group_by`` as ``values()`` followed by ``annotate()`` groups in Django.

        A grouped query calls ``values()`` before the first annotation that is added at or after ``group_mark`` or
        contains an aggregate; the filters added before that annotation, and the annotations without an aggregate
        added before ``group_mark``, come before ``values()``, the rest after it in their order. ``values()`` takes
        the names of ``group_by`` that are not annotations added at or after ``group_mark``; Django groups by the
        selected annotations added after it as well.
        """
        query = self.query
        count = len(query.annotations)
        steps: list[tuple[str, int]] = []
        for index in range(count + 1):
            steps.extend(("filter", i) for i, mark in enumerate(query.filter_marks) if mark == index)
            if index < count:
                steps.append(("annotate", index))
        if query.group_mark is None:
            return self.replay(queryset, steps)
        aggregates = query.aggregate_names()

        def early(index: int) -> bool:
            return index < query.group_mark and query.annotations[index][0] not in aggregates

        first = next(n for n, (kind, i) in enumerate(steps) if kind == "annotate" and not early(i))
        before = steps[:first] + [step for step in steps[first:] if step[0] == "annotate" and early(step[1])]
        after = [step for step in steps[first:] if not (step[0] == "annotate" and early(step[1]))]
        late = {name for name, _ in query.annotations[query.group_mark :]}
        queryset = self.replay(queryset, before)
        names = (n if n in self.annotation_names else self.path(n) for n in query.group_by if n not in late)
        return self.replay(queryset.values(*names), after)

    def replay(self, queryset: Any, steps: Iterable[tuple[str, int]]) -> Any:
        """``queryset`` with the filters and annotations ``steps`` names by index, in that order. Each filter is a
        ``filter()`` call of its own: Django joins a multi-valued relation once per call."""
        query = self.query
        for kind, index in steps:
            if kind == "filter":
                condition = self.condition(query.filters[index])
                queryset = self.with_aliases(queryset).filter(condition)
            else:
                name, value = query.annotations[index]
                queryset = self.annotate(queryset, name, value)
        return queryset

    def with_aliases(self, queryset: Any) -> Any:
        """``queryset`` with the aliases of lookups on expressions translated since the last call."""
        pending = {key: value for key, value in self.aliases.items() if key not in self._applied}
        self._applied.update(pending)
        return queryset.alias(**pending) if pending else queryset

    def annotate(self, queryset: Any, name: str, value: Any) -> Any:
        expression = self.expression(value)
        queryset = self.with_aliases(queryset)
        if name in self.query.aliases:
            return queryset.alias(**{name: expression})
        return queryset.annotate(**{name: expression})

    def base(self) -> Any:
        """The Django query set of the model rows: manager, filters, annotations and grouping, ordering, distinct,
        slice, none and row locks."""
        query = self.query
        if query.manager is None:
            queryset = self.model._base_manager.all()
        else:
            manager = getattr(self.model, query.manager, None)
            if not isinstance(manager, dj.Manager):
                raise ProgrammingError(f"{query.model} has no Django manager {query.manager!r}")
            queryset = manager.all()
        queryset = self.annotated(queryset)
        ordering = [self.order(order) for order in query.ordering]
        queryset = self.with_aliases(queryset)
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
        if query.is_combined:
            return self.translate_combined()
        queryset = self.base()
        columns = _Columns()
        if query.row_format == "model":
            only = None
            if query.only_fields is not None:
                only, _ = _level(query.only_fields)
            deferred, _ = _level(query.deferred_fields)
            plan = self.model_plan(columns, self.model, "", "", only, deferred, self.related_tree())
            plan = _RowPlan(
                (*plan.fields, *((name, columns.add(name)) for name in self.selected_annotations)),
                plan.pk,
                plan.relations,
            )
        else:
            names = query.select or (*self.field_names(), *self.selected_annotations)
            fields = [(name, columns.add(self.column(name), name)) for name in names]
            plan = _RowPlan(fields, None)
        return TranslatedQuery(queryset.values_list(*columns.values), columns.paths, plan)

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
    key), or the selected column after ``values()`` / ``values_list()`` with fields. An `OuterRef` in ``query`` raises
    `ProgrammingError`: the query has no outer query here; inside a translated query it refers to that query."""
    return _Translator(query).selected()


def aggregation(query: Query, aggregations: Iterable[tuple[str, Any]]) -> tuple[Any, dict[str, Any]]:
    """The Django query set of ``query``'s rows, as `model_queryset()` builds it, and the Django expressions of
    ``aggregations`` (name and adorm expression pairs) for its ``aggregate()``."""
    translator = _Translator(query)
    queryset = translator.base()
    expressions = {name: translator.expression(value) for name, value in aggregations}
    return translator.with_aliases(queryset), expressions


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


def bulk_create_options(operation: Any) -> dict[str, Any]:
    """The keyword arguments of Django's ``bulk_create()`` for the `adorm.operations.BulkCreate` ``operation``, with
    adorm field names translated to Django's (``"pk"`` kept)."""
    adorm = _adorm_model(django_model(operation.model))

    def names(values: tuple[str, ...] | None) -> list[str] | None:
        return None if values is None else [n if n == "pk" else _orm_name(adorm, n) for n in values]

    return {
        "batch_size": operation.batch_size,
        "ignore_conflicts": operation.ignore_conflicts,
        "update_conflicts": operation.update_conflicts,
        "update_fields": names(operation.update_fields),
        "unique_fields": names(operation.unique_fields),
    }


def check_bulk_create(operation: Any) -> None:
    """Raise `ProgrammingError` for an `adorm.operations.BulkCreate` ``operation`` Django's ``bulk_create()`` refuses
    on PostgreSQL: a model with a multi-table inheritance parent, and under ``update_conflicts`` no ``unique_fields``,
    a primary key or a field without a column of its own in ``update_fields``, or such a field in ``unique_fields``.
    A name that is no field raises `FieldError`."""
    model = django_model(operation.model)
    opts = model._meta
    parents = getattr(opts, "all_parents", None) or opts.get_parent_list()
    if any(parent._meta.concrete_model is not opts.concrete_model for parent in parents):
        raise ProgrammingError(
            f"bulk_create() cannot insert rows of {operation.model}, a multi-table inheritance child"
        )
    if not operation.update_conflicts:
        return
    options = bulk_create_options(operation)
    if not operation.unique_fields:
        raise ProgrammingError("update_conflicts needs unique_fields, the fields whose conflict updates the row")
    fields = {}
    for key in ("update_fields", "unique_fields"):
        names = [opts.pk.name if name == "pk" else name for name in options[key]]
        missing = [name for name in names if _field(model, name) is None]
        if missing:
            raise FieldError(f"{operation.model} has no field {missing[0]!r}", model=operation.model, key=missing[0])
        fields[key] = [opts.get_field(name) for name in names]
    if any(not field.concrete for field in (*fields["update_fields"], *fields["unique_fields"])):
        raise ProgrammingError(
            "bulk_create() takes only fields with a column of their own in update_fields and unique_fields"
        )
    if any(field in opts.pk_fields for field in fields["update_fields"]):
        raise ProgrammingError("bulk_create() cannot update a primary key on a conflict")


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


# in_bulk(), children of existing parents, full_clean()


def check_in_bulk_field(operation: Any) -> None:
    """Raise `ProgrammingError` unless the field of the `adorm.operations.InBulk` ``operation`` is the primary key, a
    field unique on its own (``unique=True`` or a single-field ``UniqueConstraint`` without a condition) or the only
    field the query is distinct on, as Django's ``in_bulk()`` requires; `FieldError` for a name that is no field."""
    name = operation.field_name
    if name == "pk":
        return
    query = operation.query
    model = django_model(query.model)
    field = _field(model, _orm_name(_adorm_model(model), name))
    if field is None or not getattr(field, "concrete", False):
        raise FieldError(f"{query.model} has no field {name!r}")
    opts = model._meta
    unique = {constraint.fields[0] for constraint in opts.total_unique_constraints if len(constraint.fields) == 1}
    if not (field.unique or field.name in unique or query.distinct_fields == (name,)):
        raise ProgrammingError(f"in_bulk()'s field_name must be a unique field but {name!r} isn't")


def parent_link_field(model: type[dj.Model]) -> Any:
    """The primary key of ``model`` when it is the link of a multi-table inheritance child to its parent;
    `ProgrammingError` otherwise."""
    pk = model._meta.pk
    if pk.remote_field is None or not pk.remote_field.parent_link:
        raise ProgrammingError(f"{model._meta.label} is not a multi-table inheritance child")
    return pk


def child_instance(operation: Any, parent: dj.Model) -> dj.Model:
    """The Django instance of the `adorm.operations.CreateFromParent` ``operation``: the stored ``parent``'s field
    values, the child's own field values and the parent link. A value of a field the child does not declare itself
    raises `ProgrammingError`."""
    model = django_model(operation.model)
    opts = model._meta
    link = parent_link_field(model)
    kwargs = field_kwargs(operation.model, operation.values)
    own = {name for f in opts.local_concrete_fields if f is not link for name in (f.name, f.attname)}
    inherited = sorted(name for name in kwargs if name not in own)
    if inherited:
        raise ProgrammingError(
            f"{', '.join(map(repr, inherited))} are not fields of {opts.label} itself: give the child's own fields"
        )
    values = {field.attname: getattr(parent, field.attname) for field in parent._meta.concrete_fields}
    instance = model(**values, **kwargs)
    setattr(instance, link.attname, parent.pk)
    return instance


def _loaded_values(model: type[dj.Model], operation: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    kwargs = field_kwargs(operation.model, operation.values)
    opts = model._meta
    return kwargs, {opts.get_field(key).attname: value for key, value in kwargs.items()}


class _AliasPin:
    """A Django database router that sends every read and write in a `pinned_alias()` block to its alias."""

    def db_for_read(self, model: Any, **hints: Any) -> str | None:  # noqa: ARG002
        return _PINNED_ALIAS.get()

    db_for_write = db_for_read


_PINNED_ALIAS: contextvars.ContextVar[str | None] = contextvars.ContextVar("adorm_pinned_alias", default=None)
_ALIAS_PIN = _AliasPin()
_ALIAS_PIN_LOCK = threading.Lock()


@contextlib.contextmanager
def pinned_alias(alias: str) -> Iterator[None]:
    """Route every query Django code runs in the block, in the current context, to the database ``alias``, ahead of
    the project's ``DATABASE_ROUTERS``; other threads and contexts keep their routing."""
    from django.db import router

    if router.routers[:1] != [_ALIAS_PIN]:
        with _ALIAS_PIN_LOCK:
            routers = router.routers
            if routers[:1] != [_ALIAS_PIN]:
                # A new list: threads routing now keep iterating the old one.
                router.routers = [_ALIAS_PIN, *(r for r in routers if r is not _ALIAS_PIN)]
    token = _PINNED_ALIAS.set(alias)
    try:
        yield
    finally:
        _PINNED_ALIAS.reset(token)


def clean_needs_row(operation: Any) -> bool:
    """Whether the instance of the `adorm.operations.FullClean` ``operation`` is built on its stored row: a stored
    instance whose values lack some of the model's fields."""
    if operation.adding or operation.pk is None:
        return False
    model = django_model(operation.model)
    _, values = _loaded_values(model, operation)
    return any(not f.primary_key and f.attname not in values for f in model._meta.concrete_fields)


def clean_instance(operation: Any, alias: str, stored: dj.Model | None) -> dj.Model:
    """The Django instance the `adorm.operations.FullClean` ``operation`` validates: the operation's values over
    ``stored`` (the row read for `clean_needs_row()`), over the loaded values alone when they are complete, or over a
    new instance's defaults; ``_state.adding`` as the operation says and ``_state.db`` ``alias``."""
    model = django_model(operation.model)
    opts = model._meta
    kwargs, values = _loaded_values(model, operation)
    if stored is not None:
        instance = stored
        for attname, value in values.items():
            setattr(instance, attname, value)
    elif not operation.adding and operation.pk is not None and not clean_needs_row(operation):
        loaded = [
            (field.attname, operation.pk if field.primary_key else values[field.attname])
            for field in opts.concrete_fields
        ]
        instance = model.from_db(alias, [name for name, _ in loaded], [value for _, value in loaded])
    else:
        instance = model(**kwargs)
        if operation.pk is not None:
            instance.pk = operation.pk
    instance._state.adding = operation.adding
    instance._state.db = alias
    return instance


def clean_exclude(operation: Any) -> set[str]:
    """The Django field names of the ``exclude`` names (adorm fields or relations) of a `adorm.operations.FullClean`;
    names without a Django field are left out."""
    model = django_model(operation.model)
    adorm = _adorm_model(model)
    names = set()
    for name in operation.exclude:
        field = _field(model, _orm_name(adorm, name))
        if field is not None and not isinstance(field, ForeignObjectRel):
            names.add(field.name)
    return names


def _error_key(model: type[dj.Model], adorm: Any, key: str) -> str:
    field = None if key == "__all__" else _field(model, key)
    if field is None or adorm is None:
        return key
    if field.many_to_one or field.one_to_one:
        relation = _adorm_name(adorm, field.name)
        if isinstance(adorm.__adorm_relations__.get(relation), ForeignKey):
            return relation
        return _adorm_name(adorm, field.attname)
    return _adorm_name(adorm, field.name)


def validation_error(label: str | None, exc: Any) -> Exception:
    """The `adorm.ValidationError` for Django's ``ValidationError`` ``exc``, raised for an instance of the model
    ``label`` (``None`` when unknown).

    Each error keeps its message (as text, ``%`` placeholders unfilled; a lazy plural, whose text depends on the
    params, formatted), ``code`` and ``params``. With a model,
    errors are keyed by adorm name (a foreign key's under the adorm relation's name, when the adorm model declares
    it) and the field names of the ``unique_check`` param are adorm names; the model's errors stay under
    ``"__all__"``. Params that are Django model instances or classes become the model's label, lazy translations
    become text."""
    from adorm.exceptions import ValidationError

    model = adorm = None
    if label is not None:
        try:
            model = django_model(label)
        except ProgrammingError:
            model = None
        adorm = _adorm_model(model)

    def convert(error: Any) -> ValidationError:
        params = _error_params(model, adorm, error.params)
        return ValidationError(_error_message(error.message, error.params, params), error.code, params)

    if hasattr(exc, "error_dict"):
        errors: dict[str, list[ValidationError]] = {}
        for key, items in exc.error_dict.items():
            target = errors.setdefault(key if model is None else _error_key(model, adorm, key), [])
            target.extend(convert(leaf) for item in items for leaf in item.error_list)
        return ValidationError(errors)
    if hasattr(exc, "message"):
        return convert(exc)
    return ValidationError([convert(leaf) for leaf in exc.error_list])


def _error_message(message: Any, params: Any, plain: Any) -> str:
    """``message`` as text; a message whose text depends on ``params`` (a lazy plural) is formatted by Django, with
    ``%`` doubled so that formatting it with ``plain`` again gives the same text."""
    text = str(message)
    if not params:
        return text
    formatted = str(message % params)
    try:
        if text % plain == formatted:
            return text
    except (KeyError, TypeError, ValueError):
        pass
    return formatted.replace("%", "%%")


def _error_params(model: type[dj.Model] | None, adorm: Any, params: Any) -> dict[str, Any] | None:
    if not params:
        return params
    from django.utils.functional import Promise

    def plain(value: Any) -> Any:
        if isinstance(value, Promise):
            return str(value)
        if isinstance(value, dj.Model) or (isinstance(value, type) and issubclass(value, dj.Model)):
            return value._meta.label
        return value

    result = {key: plain(value) for key, value in params.items()}
    unique_check = result.get("unique_check")
    if model is not None and isinstance(unique_check, list | tuple):
        result["unique_check"] = tuple(_error_key(model, adorm, name) for name in unique_check)
    return result


def operation_model(operation: Any) -> str | None:
    """The label of the model ``operation`` works on: its ``model``, else its query's; ``None`` when it has neither."""
    model = getattr(operation, "model", None)
    if isinstance(model, str):
        return model
    model = getattr(getattr(operation, "query", None), "model", None)
    return model if isinstance(model, str) else None
