"""Introspection of Django models into `adorm.codegen.spec` values.

Needs a configured Django (``django.setup()`` done). Concrete, non-swapped models are generated; auto-created
many-to-many through models are not (a `ManyToMany` names them by label). Managers become `ManagerSpec` values: a
manager whose class or query set class overrides a method the driver calls on the ORM manager (`ROUTED_METHODS`:
``get_queryset``, ``create``, ``delete`` …) gets its ORM name, unless its client base defines the same method; the
methods marked with `adorm.expose` or listed in the ``ADORM_EXPOSE`` setting (``"app_label.Model.manager.method"``)
become driver methods, which run on the manager (``method_manager``) without routing its other operations. Methods
that are neither exposed nor ported are reported as `CodegenWarning`, one per method; those of installed packages
(`is_installed`) as one `ThirdPartyMethodsWarning` per manager, and `PROTOCOL_METHODS` not at all.
"""

from __future__ import annotations

import contextvars
import enum
import importlib
import inspect
import os
import re
import sys
import types
import typing
import warnings

from collections.abc import Collection, Iterable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

import django
import pydantic

from django.apps import apps
from django.conf import settings as django_settings
from django.core import validators as django_validators
from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured
from django.db import models
from django.db.models import deletion
from django.db.models.fields import NOT_PROVIDED
from django.db.models.fields.proxy import OrderWrt

from adorm.codegen.render import can_render_value
from adorm.codegen.spec import (
    AppSpec,
    CodegenError,
    CodegenWarning,
    compute_schema_hash,
    FieldSpec,
    ManagerSpec,
    MethodSpec,
    ModelSpec,
    NO_DEFAULT,
    ParameterSpec,
    python_name,
    QuerySetSpec,
    RelationSpec,
    ThirdPartyMethodsWarning,
    TypeSpec,
)
from adorm.models.base import Model as AdormModelBase
from adorm.models.managers import Manager as AdormManager
from adorm.porting import get_client_base, is_exposed
from adorm.query.queryset import QuerySet as AdormQuerySet

__all__ = [
    "delete_models_of",
    "exposed_methods",
    "field_spec",
    "introspect",
    "is_installed",
    "manager_specs",
    "model_spec",
    "schema_hash",
    "setup",
]

_INT = TypeSpec("int")
_STR = TypeSpec("str")
_TYPES: dict[str, TypeSpec] = {
    "AutoField": _INT,
    "BigAutoField": _INT,
    "SmallAutoField": _INT,
    "IntegerField": _INT,
    "BigIntegerField": _INT,
    "SmallIntegerField": _INT,
    "PositiveIntegerField": _INT,
    "PositiveBigIntegerField": _INT,
    "PositiveSmallIntegerField": _INT,
    "CharField": _STR,
    "TextField": _STR,
    "SlugField": _STR,
    "FileField": _STR,
    "ImageField": _STR,
    "FilePathField": _STR,
    "GenericIPAddressField": _STR,
    "IPAddressField": _STR,
    "BooleanField": TypeSpec("bool"),
    "FloatField": TypeSpec("float"),
    "DecimalField": TypeSpec("decimal.Decimal"),
    "DateField": TypeSpec("datetime.date"),
    "DateTimeField": TypeSpec("datetime.datetime"),
    "TimeField": TypeSpec("datetime.time"),
    "DurationField": TypeSpec("datetime.timedelta"),
    "UUIDField": TypeSpec("uuid.UUID"),
    "BinaryField": TypeSpec("bytes"),
    "JSONField": TypeSpec("typing.Any"),
    "HStoreField": TypeSpec("dict", (_STR, _STR.with_nullable())),
}
"""Python types by Django internal type; any other type becomes ``typing.Any``."""

_AUTO_TYPES = frozenset({"AutoField", "BigAutoField", "SmallAutoField"})
_POSITIVE_TYPES = frozenset({"PositiveIntegerField", "PositiveBigIntegerField", "PositiveSmallIntegerField"})
_BUILTIN_FACTORIES = (dict, list, set, tuple, str, int, float, bool, bytes)
_FACTORY_MODULES = frozenset({"uuid", "datetime", "decimal"})
_SLUG_PATTERNS = {False: r"^[-a-zA-Z0-9_]+$", True: r"^[-\w]+$"}
_END_OF_TEXT = re.compile(r"(?<!\\)((?:\\\\)*)\\Z")


_silent: contextvars.ContextVar[bool] = contextvars.ContextVar("adorm_codegen_silent", default=False)


def _warn(message: str, category: type[CodegenWarning] = CodegenWarning) -> None:
    if not _silent.get():
        warnings.warn(message, category, stacklevel=3)


def _where(field: Any) -> str:
    return f"{field.model._meta.label}.{field.name}"


def _pattern(regex: Any) -> str | None:
    pattern = getattr(regex, "pattern", regex)
    if not isinstance(pattern, str):
        return None
    pattern = _END_OF_TEXT.sub(r"\1$", pattern)
    try:
        pydantic.TypeAdapter(Annotated[str, pydantic.Field(pattern=pattern)])
    except Exception:  # noqa: BLE001 - any compile error means pydantic cannot take the pattern
        return None
    return pattern


def _validator_constraints(field: models.Field, constraints: dict[str, Any]) -> None:
    def tighten(key: str, value: Any, pick: Any) -> None:
        constraints[key] = value if key not in constraints else pick(constraints[key], value)

    for validator in field._validators:
        limit = getattr(validator, "limit_value", None)
        if callable(limit) or not can_render_value(limit):
            continue
        if isinstance(validator, django_validators.MinValueValidator):
            tighten("ge", limit, max)
        elif isinstance(validator, django_validators.MaxValueValidator):
            tighten("le", limit, min)
        elif isinstance(validator, django_validators.MinLengthValidator):
            tighten("min_length", limit, max)
        elif isinstance(validator, django_validators.MaxLengthValidator):
            tighten("max_length", limit, min)
        elif (
            isinstance(validator, django_validators.RegexValidator)
            and not validator.inverse_match
            and not validator.flags
            and "pattern" not in constraints
        ):
            pattern = _pattern(validator.regex)
            if pattern is None:
                _warn(f"{_where(field)}: the pattern of {type(validator).__name__} is not generated")
            else:
                constraints["pattern"] = pattern


def _value_type(field: models.Field) -> TypeSpec:
    """The Python type of ``field``'s values, without constraints; a foreign key takes its target field's type."""
    if field.is_relation and (field.many_to_one or field.one_to_one):
        return _value_type(field.target_field)
    internal = field.get_internal_type()
    if internal == "ArrayField":
        base = field.base_field
        return TypeSpec("list", (_value_type(base).with_nullable(base.null),))
    spec = _TYPES.get(internal)
    if spec is None:
        _warn(f"{_where(field)}: the Django type {internal} is generated as typing.Any")
        return TypeSpec("typing.Any")
    return spec


def _choice_type(field: models.Field) -> TypeSpec | None:
    values = []
    for value, _label in field.flatchoices:
        value = value.value if isinstance(value, enum.Enum) else value
        if not isinstance(value, str | int | bool):
            return None
        if value not in values:
            values.append(value)
    return TypeSpec("typing.Literal", values=tuple(values)) if values else None


def _type_and_constraints(field: models.Field) -> tuple[TypeSpec, dict[str, Any]]:
    if field.is_relation:
        return _value_type(field), {}
    if field.choices:
        literal = _choice_type(field)
        if literal is not None:
            return literal, {}
    spec = _value_type(field)
    internal = field.get_internal_type()
    constraints: dict[str, Any] = {}
    if spec.name in ("str", "bytes") and field.max_length is not None:
        constraints["max_length"] = field.max_length
    if internal == "ArrayField" and field.size is not None:
        constraints["max_length"] = field.size
    if internal == "DecimalField":
        if field.max_digits is not None:
            constraints["max_digits"] = field.max_digits
        if field.decimal_places is not None:
            constraints["decimal_places"] = field.decimal_places
    if internal in _POSITIVE_TYPES:
        constraints["ge"] = 0
    if internal == "SlugField":
        constraints["pattern"] = _SLUG_PATTERNS[bool(field.allow_unicode)]
    _validator_constraints(field, constraints)
    return spec, constraints


def _factory_path(factory: Any) -> str | None:
    if factory in _BUILTIN_FACTORIES:
        return f"builtins.{factory.__name__}"
    module, qualname = getattr(factory, "__module__", None), getattr(factory, "__qualname__", "")
    if module in _FACTORY_MODULES and qualname.isidentifier():
        return f"{module}.{qualname}"
    return None


def field_spec(field: models.Field) -> FieldSpec:
    """The `FieldSpec` of the concrete Django field ``field`` (a foreign key gives its column field, ``<name>_id``).

    The type follows the internal type (`TypeSpec`); choices become a ``Literal``; ``max_length``,
    ``max_digits``, ``decimal_places``, positive integer types and the ``Min``/``MaxValueValidator``,
    ``Min``/``MaxLengthValidator`` and ``RegexValidator`` validators become constraints. A value Django or the database
    fills in (an automatic or parent-link primary key, ``auto_now``, ``auto_now_add``, ``db_default``, a generated
    field, the ``_order`` field of ``order_with_respect_to``, a default callable outside ``builtins``, ``uuid``,
    ``datetime`` and ``decimal``, a default that is not a plain literal) makes the field ``db_filled``. A ``blank``
    string field without a default defaults to ``""``, the value Django gives it. Raises ``ValueError`` for a name no
    Python name maps to (`python_name()`).
    """
    spec, constraints = _type_and_constraints(field)
    default, factory, db_filled = NO_DEFAULT, None, False
    internal = field.get_internal_type()
    remote = field.remote_field
    if (
        (field.primary_key and (internal in _AUTO_TYPES or (remote is not None and remote.parent_link)))
        or getattr(field, "auto_now", False)
        or getattr(field, "auto_now_add", False)
        or getattr(field, "db_default", NOT_PROVIDED) is not NOT_PROVIDED
        or getattr(field, "generated", False)
        or isinstance(field, OrderWrt)
    ):
        db_filled = True
    elif field.has_default():
        value = field.default
        if callable(value):
            factory = _factory_path(value)
            db_filled = factory is None
        else:
            value = value.value if isinstance(value, enum.Enum) else value
            if internal == "DecimalField" and isinstance(value, int | float | str) and not isinstance(value, bool):
                value = Decimal(str(value))
            if can_render_value(value):
                default = value
            else:
                db_filled = True
    elif spec == TypeSpec("str") and field.blank and not field.null and not field.primary_key:
        default = ""
    help_text = str(field.help_text) if field.help_text else None
    return FieldSpec(
        name=python_name(field.attname),
        orm_name=field.attname,
        type=spec,
        nullable=field.null,
        default=default,
        default_factory=factory,
        db_filled=db_filled,
        constraints=constraints,
        description=help_text,
        primary_key=field.primary_key,
    )


def _through_column(through: type[models.Model], name: str) -> str:
    return python_name(through._meta.get_field(name).attname)


def _off_pk(foreign_keys: Iterable[Any]) -> Any:
    """The first of ``foreign_keys`` that references a field other than its target's primary key, or ``None``."""
    return next((fk for fk in foreign_keys if not fk.target_field.primary_key), None)


def _warn_off_pk(where: str, fk: Any) -> None:
    target = f"{fk.related_model._meta.label}.{fk.target_field.name}"
    _warn(f"{where}: the relation references {target}, not a primary key, and is not generated")


def _m2m_foreign_keys(field: models.ManyToManyField) -> list[Any]:
    through = field.remote_field.through._meta
    return [through.get_field(field.m2m_field_name()), through.get_field(field.m2m_reverse_field_name())]


def _forward_relation(field: Any) -> RelationSpec | None:
    """The `RelationSpec` of a concrete or many-to-many field; ``None``, with a `CodegenWarning` for a relation, when
    it is not generated."""
    if isinstance(field, models.ForeignKey):
        if _off_pk([field]) is not None:
            _warn_off_pk(_where(field), field)
            return None
        return RelationSpec(
            "foreign_key",
            python_name(field.name),
            field.name,
            field.related_model._meta.label,
            column=python_name(field.attname),
        )
    if isinstance(field, models.ManyToManyField):
        off_pk = _off_pk(_m2m_foreign_keys(field))
        if off_pk is not None:
            _warn_off_pk(_where(field), off_pk)
            return None
        through = field.remote_field.through
        return RelationSpec(
            "many_to_many",
            python_name(field.name),
            field.name,
            field.related_model._meta.label,
            through=through._meta.label,
            source_column=_through_column(through, field.m2m_field_name()),
            target_column=_through_column(through, field.m2m_reverse_field_name()),
        )
    if field.is_relation:
        _warn(f"{_where(field)}: {type(field).__name__} is not generated")
    return None


def _reverse_relation(rel: Any, owner: str) -> RelationSpec | None:
    """The `RelationSpec` of the reverse relation ``rel`` on the model ``owner``; ``None``, with a `CodegenWarning`,
    when it is not generated."""
    forward = rel.field
    name = rel.get_accessor_name()
    target = rel.related_model._meta.label
    where = f"{owner}.{name}"
    if isinstance(forward, models.ManyToManyField):
        off_pk = _off_pk(_m2m_foreign_keys(forward))
        if off_pk is not None:
            _warn_off_pk(where, off_pk)
            return None
        through = forward.remote_field.through
        return RelationSpec(
            "many_to_many",
            python_name(name),
            name,
            target,
            through=through._meta.label,
            source_column=_through_column(through, forward.m2m_reverse_field_name()),
            target_column=_through_column(through, forward.m2m_field_name()),
            reverse=True,
        )
    if isinstance(forward, models.ForeignKey):
        if _off_pk([forward]) is not None:
            _warn_off_pk(where, forward)
            return None
        kind = "reverse_one_to_one" if rel.one_to_one else "reverse_foreign_key"
        return RelationSpec(kind, python_name(name), name, target, column=python_name(forward.attname))
    _warn(f"{where}: {type(rel).__name__} is not generated")
    return None


def _forward_fields(opts: Any, include_parents: bool) -> list[Any]:
    fields = list(opts.concrete_fields if include_parents else opts.local_concrete_fields)
    return fields + list(opts.many_to_many if include_parents else opts.local_many_to_many)


def _relations(model: type[models.Model], include_parents: bool, included: Collection[str] | None) -> list[Any]:
    opts = model._meta
    relations = []
    own = opts.proxy and not include_parents
    for field in _forward_fields(opts, include_parents):
        relation = _forward_relation(field)
        if relation is None:
            continue
        relations.append(relation)
        if included is None:
            continue
        missing = [relation.target] if relation.target not in included else []
        through = getattr(field.remote_field, "through", None)
        if through is not None and not through._meta.auto_created and relation.through not in included:
            missing.append(relation.through)
        if missing:
            _warn(f"{opts.label}.{field.name}: {' and '.join(missing)} not generated, so the relation fails when used")
    for field in opts.get_fields(include_parents=include_parents):
        if isinstance(field, models.ForeignObjectRel):
            if included is not None and field.related_model._meta.label not in included:
                continue
            if own and field.model is not model:
                continue
            relation = _reverse_relation(field, opts.label)
            if relation is not None:
                relations.append(relation)
        elif _is_generic_foreign_key(field):
            if own and field.model is not model:
                continue
            relations.append(_generic_relation(field))
        elif field.is_relation and not field.concrete and not field.many_to_many:
            _warn(f"{opts.label}.{field.name}: {type(field).__name__} is not generated")
    return relations


def _is_generic_foreign_key(field: Any) -> bool:
    # A model with a generic foreign key has imported the module; importing it otherwise needs contenttypes installed.
    module = sys.modules.get("django.contrib.contenttypes.fields")
    return module is not None and isinstance(field, module.GenericForeignKey)


def _generic_relation(field: Any) -> RelationSpec:
    """The `RelationSpec` of the generic foreign key ``field``."""
    opts = field.model._meta
    content_type = opts.get_field(field.ct_field)
    return RelationSpec(
        "generic_foreign_key",
        python_name(field.name),
        field.name,
        content_type.related_model._meta.label,
        column=python_name(opts.get_field(field.fk_field).attname),
        ct_field=python_name(content_type.name),
        for_concrete_model=field.for_concrete_model,
    )


def _declarations(model: type[models.Model], include_parents: bool) -> dict[str, Any]:
    """The `adorm.contrib.django.TenantRelation` declarations of ``model`` by attribute name: its own, those of its
    abstract bases and, with ``include_parents``, those of its concrete parents."""
    from adorm.contrib.django.tenants import TenantRelation

    found: dict[str, Any] = {}
    for klass in model.__mro__:
        meta = getattr(klass, "_meta", None)
        if meta is None or not isinstance(klass, type) or not issubclass(klass, models.Model):
            continue
        if klass is not model and not meta.abstract and not include_parents:
            continue
        for name, value in vars(klass).items():
            if isinstance(value, TenantRelation) and name not in found:
                found[name] = value
    return found


def _path_names(model: type[models.Model], path: str, where: str, *, end: bool) -> tuple[str, Any]:
    """The adorm names of the Django query path ``path`` on ``model`` and the field it ends on; with ``end`` the path
    ends on a field that is not a relation. A reverse relation may be named by its query name or its accessor. Raises
    `CodegenError` for a path that does not resolve."""
    current: Any = model
    names: list[str] = []
    field: Any = None
    for segment in path.split("__"):
        if current is None:
            raise CodegenError(f"{where}: {path!r} continues after {names[-1]!r}, which is not a relation")
        opts = current._meta
        try:
            field = opts.get_field(segment)
        except FieldDoesNotExist:
            field = next((rel for rel in opts.related_objects if rel.get_accessor_name() == segment), None)
            if field is None:
                raise CodegenError(f"{where}: {opts.label} has no field {segment!r} ({path!r})") from None
        if isinstance(field, models.ForeignObjectRel):
            names.append(python_name(field.get_accessor_name()))
        elif segment == getattr(field, "attname", None) and segment != field.name:
            names.append(python_name(segment))
            current = None
            continue
        else:
            names.append(python_name(field.name))
        current = field.related_model if field.is_relation else None
        if field.is_relation and current is None:
            raise CodegenError(f"{where}: {path!r} follows {segment!r}, which is not a relation to one model")
    if end and field.is_relation:
        raise CodegenError(f"{where}: {path!r} does not end on a field that is not a relation")
    return "__".join(names), field


_SCHEMA_FUNCTIONS = ("Concat", "Lower", "Upper", "Replace", "Left", "Right", "LPad", "Coalesce")


def _schema_source(model: type[models.Model], expression: Any, where: str) -> tuple[Any, ...]:
    """The `adorm.codegen.spec.SchemaSource` of the Django expression ``expression`` of ``model``'s rows; raises
    `CodegenError` for a class outside the supported set."""
    from django.db.models import functions
    from django.db.models.functions.text import ConcatPair

    name = type(expression).__name__
    if isinstance(expression, models.F):
        return ("F", _path_names(model, expression.name, where, end=False)[0])
    if isinstance(expression, models.Value):
        value = expression.value
        if not isinstance(value, str | int) or isinstance(value, bool):
            raise CodegenError(f"{where}: the schema takes Value() of a str or an int, not {value!r}")
        return ("Value", value)
    if isinstance(expression, functions.Cast):
        target = expression.output_field
        if type(target) not in (models.CharField, models.TextField):
            raise CodegenError(f"{where}: the schema takes Cast() to CharField or TextField only")
        (source,) = expression.get_source_expressions()
        return ("Cast", _schema_source(model, source, where), getattr(target, "max_length", None))
    if isinstance(expression, functions.Concat):
        arguments: list[Any] = []
        pending = list(expression.get_source_expressions())
        while pending:
            item = pending.pop(0)
            if isinstance(item, ConcatPair):
                pending[:0] = item.get_source_expressions()
            else:
                arguments.append(item)
        return ("Concat", tuple(_schema_source(model, argument, where) for argument in arguments))
    if name in _SCHEMA_FUNCTIONS and type(expression) is getattr(functions, name):
        return (name, tuple(_schema_source(model, argument, where) for argument in expression.get_source_expressions()))
    raise CodegenError(
        f"{where}: the schema expression uses {name}; F, Value, Concat, Lower, Upper, Replace, Left, Right, LPad, "
        "Coalesce and Cast to CharField or TextField are supported"
    )


def _tenant_models(declaration: Any, where: str) -> tuple[str, ...]:
    labels = {declaration.to}
    for entry in declaration.tenant_models:
        if "." in entry:
            try:
                labels.add(apps.get_model(entry)._meta.label)
            except LookupError:
                raise CodegenError(f"{where}: tenant_models names {entry!r}, which is not an installed model") from None
            continue
        try:
            config = apps.get_app_config(entry)
        except LookupError:
            raise CodegenError(f"{where}: tenant_models names {entry!r}, which is no installed app or model") from None
        labels.update(model._meta.label for model in config.get_models(include_auto_created=True))
    return tuple(sorted(labels))


def _tenant_relations(
    model: type[models.Model], include_parents: bool, included: Collection[str] | None
) -> list[RelationSpec]:
    """The `RelationSpec` values of ``model``'s `adorm.contrib.django.TenantRelation` declarations."""
    relations = []
    for name, declaration in _declarations(model, include_parents).items():
        where = f"{model._meta.label}.{name}"
        try:
            target = apps.get_model(declaration.to)
        except LookupError:
            raise CodegenError(f"{where}: {declaration.to} is not an installed model") from None
        try:
            field = target._meta.get_field(declaration.field)
        except FieldDoesNotExist:
            field = None
        if (
            not isinstance(field, models.ForeignKey)
            or field.related_model._meta.concrete_model is not model._meta.concrete_model
            or not field.target_field.primary_key
        ):
            raise CodegenError(
                f"{where}: {declaration.to}.{declaration.field} is not a foreign key to the primary key of "
                f"{model._meta.label}"
            )
        if isinstance(declaration.schema, str):
            schema, source = _path_names(model, declaration.schema, where, end=True)[0], None
        else:
            schema, source = None, _schema_source(model, declaration.schema, where)
        labels = _tenant_models(declaration, where)
        if included is not None:
            missing = [
                label for label in labels if label not in included and not apps.get_model(label)._meta.auto_created
            ]
            if missing:
                _warn(f"{where}: {' and '.join(missing)} not generated, so the relation fails when used")
        relations.append(
            RelationSpec(
                "tenant",
                python_name(name),
                name,
                target._meta.label,
                column=python_name(field.attname),
                schema=schema,
                schema_source=source,
                models=labels,
                session=declaration.session,
            )
        )
    return relations


_CORE_MODULES = frozenset({"django.db.models.manager", "django.db.models.query", "builtins"})
_MANAGER_NAMES = frozenset(name for name in dir(models.Manager) if not name.startswith("__"))
_QUERYSET_NAMES = frozenset(name for name in dir(models.QuerySet) if not name.startswith("__"))
ROUTED_METHODS = frozenset(
    {
        "get_queryset", "all", "filter", "exclude", "annotate", "aggregate", "order_by", "values", "values_list",
        "only", "defer", "select_related", "prefetch_related", "distinct", "none", "select_for_update", "get",
        "first", "last", "count", "exists", "iterator", "create", "get_or_create", "update_or_create", "update",
        "delete", "bulk_create", "bulk_update", "in_bulk",
    }
)  # fmt: skip
"""The Django manager and query set methods whose override makes a manager run its operations through the ORM
manager (``orm_name``)."""
PROTOCOL_METHODS = frozenset({"get_by_natural_key"})
"""Manager methods Django itself calls (deserialisation, authentication backends), never reported when not
generated."""
_SCALARS = frozenset({bool, int, float, str, bytes})
_CONTAINERS = frozenset({list, tuple, set, frozenset, dict})
_DOTTED_MODULES = frozenset({"datetime", "decimal", "uuid"})


class _Skip(Exception):  # noqa: N818 - control flow inside this module only
    pass


def _user_classes(cls: type) -> list[type]:
    """The classes of ``cls``'s MRO outside Django's own manager and query set modules."""
    return [klass for klass in cls.__mro__ if klass.__module__ not in _CORE_MODULES]


_PACKAGE_DIRECTORIES = frozenset({"site-packages", "dist-packages"})


def is_installed(func: Any) -> bool:
    """Whether the function ``func`` (unwrapped) is defined in a ``site-packages`` or ``dist-packages`` directory,
    the code of an installed distribution; an editable install and a package vendored into the project's source tree
    are not."""
    code = getattr(inspect.unwrap(func), "__code__", None)
    return code is not None and not _PACKAGE_DIRECTORIES.isdisjoint(Path(code.co_filename).parts)


def _own_functions(
    cls: type, queryset_class: type | None = None, owners: dict[str, type] | None = None
) -> dict[str, Any]:
    """Public functions and ``get_queryset`` the user classes of ``cls`` define, nearest first; a manager method
    Django copied from ``queryset_class`` is left out. ``owners`` receives the class each function is found on."""
    copied = _own_functions(queryset_class) if queryset_class is not None else {}
    found: dict[str, Any] = {}
    for klass in _user_classes(cls):
        for name, value in vars(klass).items():
            if name in found or not inspect.isfunction(value):
                continue
            if name.startswith("_") and name != "get_queryset":
                continue
            if name in copied and getattr(value, "__wrapped__", None) is copied[name]:
                continue
            found[name] = value
            if owners is not None:
                owners[name] = klass
    return found


def exposed_methods() -> dict[str, set[str]]:
    """The ``ADORM_EXPOSE`` setting as method names by ``"app_label.Model.manager"``.

    Raises `CodegenError` for an entry that is not ``"app_label.Model.manager.method"``, names a model Django does not
    know or a manager the model does not have.
    """
    entries = getattr(django_settings, "ADORM_EXPOSE", ())
    if isinstance(entries, str):
        raise CodegenError("ADORM_EXPOSE is a list of 'app_label.Model.manager.method' strings, not a string")
    exposed: dict[str, set[str]] = {}
    for entry in entries:
        parts = entry.split(".") if isinstance(entry, str) else []
        if len(parts) != 4 or not all(part.isidentifier() for part in parts):
            raise CodegenError(f"ADORM_EXPOSE: {entry!r} is not 'app_label.Model.manager.method'")
        label, manager, method = f"{parts[0]}.{parts[1]}", parts[2], parts[3]
        try:
            model = apps.get_model(label)
        except LookupError:
            raise CodegenError(f"ADORM_EXPOSE: {entry!r} names no installed model") from None
        if manager not in {m.name for m in model._meta.managers}:
            raise CodegenError(f"ADORM_EXPOSE: {label} has no manager {manager!r}")
        exposed.setdefault(f"{label}.{manager}", set()).add(method)
    return exposed


def _annotation_type(annotation: Any, where: str, *, parameter: bool, included: Collection[str] | None) -> TypeSpec:
    if annotation is None or annotation is type(None):
        if parameter:
            _warn(f"{where}: the annotation None is generated as typing.Any")
            return TypeSpec("typing.Any")
        return TypeSpec("None")
    if annotation is Any:
        return TypeSpec("typing.Any")
    if isinstance(annotation, str):
        _warn(f"{where}: the annotation {annotation!r} does not evaluate and is generated as typing.Any")
        return TypeSpec("typing.Any")
    origin, args = typing.get_origin(annotation), typing.get_args(annotation)
    if origin is typing.Union or origin is types.UnionType:
        rest = [arg for arg in args if arg is not type(None)]
        if len(rest) == 1:
            return _annotation_type(rest[0], where, parameter=parameter, included=included).with_nullable(
                len(rest) < len(args)
            )
    elif origin is typing.Literal:
        if all(isinstance(value, str | int | bool) for value in args):
            return TypeSpec("typing.Literal", values=args)
    elif origin in _CONTAINERS and Ellipsis not in args:
        return TypeSpec(
            origin.__name__,
            tuple(_annotation_type(arg, where, parameter=parameter, included=included) for arg in args),
        )
    elif isinstance(annotation, type):
        if annotation in _SCALARS or annotation in _CONTAINERS:
            return TypeSpec(annotation.__name__)
        if annotation.__module__ in _DOTTED_MODULES and annotation.__qualname__.isidentifier():
            return TypeSpec(f"{annotation.__module__}.{annotation.__qualname__}")
        if issubclass(annotation, models.QuerySet):
            raise _Skip(f"{where}: it returns a query set; port it to the client base (adorm.client_base)")
        if issubclass(annotation, models.Model):
            label = annotation._meta.label
            if parameter:
                _warn(f"{where}: a {label} parameter is generated as typing.Any: pass the primary key instead")
                return TypeSpec("typing.Any")
            if included is None or label in included:
                return TypeSpec(label, model=True)
            return TypeSpec("typing.Any")
    _warn(f"{where}: the annotation {annotation!r} is generated as typing.Any")
    return TypeSpec("typing.Any")


_KIND_NAMES = {
    inspect.Parameter.POSITIONAL_ONLY: "positional_only",
    inspect.Parameter.POSITIONAL_OR_KEYWORD: "positional_or_keyword",
    inspect.Parameter.VAR_POSITIONAL: "var_positional",
    inspect.Parameter.KEYWORD_ONLY: "keyword_only",
    inspect.Parameter.VAR_KEYWORD: "var_keyword",
}


def _evaluate(annotation: Any, func: Any) -> Any:
    """``annotation``, a string evaluated in ``func``'s module; a string that does not evaluate stays a string."""
    if not isinstance(annotation, str):
        return annotation
    try:
        return eval(annotation, getattr(func, "__globals__", {}))  # noqa: S307 - an annotation of the ORM's code
    except Exception:  # noqa: BLE001 - reported as an annotation that does not evaluate
        return annotation


def _method_spec(func: Any, name: str, target: str, where: str, included: Collection[str] | None) -> MethodSpec | None:
    """The `MethodSpec` of the exposed function ``func``; ``None``, with a `CodegenWarning`, when it is not
    generated."""
    where = f"{where}.{name}"
    try:
        if inspect.iscoroutinefunction(func):
            raise _Skip(f"{where}: it is a coroutine function; expose the synchronous method")
        signature = inspect.signature(func)
        parameters = []
        for parameter in list(signature.parameters.values())[1:]:
            spec_type = None
            if parameter.annotation is not inspect.Parameter.empty:
                spec_type = _annotation_type(
                    _evaluate(parameter.annotation, func),
                    f"{where}({parameter.name})",
                    parameter=True,
                    included=included,
                )
            default = NO_DEFAULT
            if parameter.default is not inspect.Parameter.empty:
                default = parameter.default
                if not can_render_value(default):
                    raise _Skip(f"{where}: the default of {parameter.name} cannot be written as a literal")
            parameters.append(ParameterSpec(parameter.name, _KIND_NAMES[parameter.kind], spec_type, default))
        returns = None
        if signature.return_annotation is not inspect.Signature.empty:
            returns = _annotation_type(
                _evaluate(signature.return_annotation, func), where, parameter=False, included=included
            )
    except _Skip as exc:
        _warn(f"{exc} (not generated)")
        return None
    return MethodSpec(
        name,
        target,  # type: ignore[arg-type]
        tuple(parameters),
        returns,
        doc=func.__doc__ if func.__doc__ and func.__doc__.strip() else None,
        queryset_only=target == "queryset" and getattr(func, "queryset_only", False) is True,
    )


def _import_base(path: str | None, expected: type, where: str) -> type | None:
    """The client base class at ``path``; `CodegenError` when it does not import or is not an ``expected`` subclass."""
    if path is None:
        return None
    module, _, name = path.partition(":")
    try:
        base = getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError) as exc:
        raise CodegenError(f"{where}: the client base {path} does not import: {exc}") from exc
    if not isinstance(base, type) or not issubclass(base, expected):
        raise CodegenError(f"{where}: the client base {path} is not a subclass of adorm.{expected.__name__}")
    return base


def _ported(base: type | None, name: str) -> bool:
    """Whether the client base ``base`` defines ``name`` itself, not only through adorm's classes."""
    return base is not None and any(
        name in vars(klass) for klass in base.__mro__ if not klass.__module__.startswith("adorm.")
    )


def _manager_spec(
    model: type[models.Model], manager: Any, exposed: Mapping[str, set[str]], included: Collection[str] | None
) -> ManagerSpec | None:
    label = model._meta.label
    where = f"{label}.{manager.name}"
    queryset_class = manager._queryset_class
    manager_owners: dict[str, type] = {}
    queryset_owners: dict[str, type] = {}
    manager_functions = _own_functions(type(manager), queryset_class, manager_owners)
    queryset_functions = _own_functions(queryset_class, owners=queryset_owners)
    listed = set(exposed.get(where, ()))
    unknown = sorted(n for n in listed if n not in manager_functions and not hasattr(queryset_class, n))
    unknown += sorted(
        n for n in listed if n not in unknown and n not in manager_functions and n not in queryset_functions
    )
    if unknown:
        raise CodegenError(f"ADORM_EXPOSE: {where} has no method {', '.join(map(repr, unknown))}")
    manager_methods = [
        spec
        for name, func in manager_functions.items()
        if (is_exposed(func) or name in listed)
        and (spec := _method_spec(func, name, "manager", where, included)) is not None
    ]
    queryset_methods = [
        spec
        for name, func in queryset_functions.items()
        if (is_exposed(func) or name in listed)
        and (spec := _method_spec(func, name, "queryset", f"{where}.{queryset_class.__name__}", included)) is not None
    ]
    manager_base, queryset_base = get_client_base(type(manager)), get_client_base(queryset_class)
    manager_client = _import_base(manager_base, AdormManager, where)
    queryset_client = _import_base(queryset_base, AdormQuerySet, where)
    overrides = any(name in ROUTED_METHODS and not _ported(manager_client, name) for name in manager_functions) or any(
        name in ROUTED_METHODS and not _ported(queryset_client, name) for name in queryset_functions
    )
    exposed_names = {m.name for m in manager_methods} | {m.name for m in queryset_methods}
    installed: dict[str, set[str]] = {}
    for functions, owners, client, owner in (
        (manager_functions, manager_owners, manager_client, where),
        (queryset_functions, queryset_owners, queryset_client, f"{where}.{queryset_class.__name__}"),
    ):
        for name, func in functions.items():
            if (
                name in exposed_names
                or is_exposed(func)
                or name in listed
                or name in _MANAGER_NAMES
                or name in _QUERYSET_NAMES
                or name in PROTOCOL_METHODS
                or _ported(client, name)
                or func.__module__.startswith("django.")
            ):
                continue
            if is_installed(func):
                klass = owners[name]
                installed.setdefault(f"{klass.__module__}.{klass.__qualname__}", set()).add(name)
                continue
            _warn(f"{owner}.{name}: not generated; expose it or port it to a client base")
    if installed:
        count = sum(map(len, installed.values()))
        listing = ", ".join(f"{owner} ({', '.join(sorted(names))})" for owner, names in sorted(installed.items()))
        _warn(
            f"{where}: {count} method{'s' if count > 1 else ''} of installed packages not generated: {listing}; "
            "expose or port the ones the application calls",
            ThirdPartyMethodsWarning,
        )
    orm_name = manager.name if overrides else None
    method_manager = manager.name if not overrides and (manager_methods or queryset_methods) else None
    queryset = None
    if queryset_methods or queryset_base is not None:
        queryset = QuerySetSpec(queryset_class.__name__, tuple(queryset_methods), queryset_base)
    class_name = None
    if manager_methods or manager_base is not None:
        class_name = type(manager).__name__
    spec = ManagerSpec(
        manager.name, orm_name, class_name, tuple(manager_methods), queryset, manager_base, method_manager
    )
    if spec.is_plain and manager.name == "objects":
        return None
    return spec


def manager_specs(
    model: type[models.Model],
    *,
    local: bool = False,
    included: Collection[str] | None = None,
    exposed: Mapping[str, set[str]] | None = None,
) -> tuple[ManagerSpec, ...]:
    """The `ManagerSpec` values of the Django model ``model``'s managers, in declaration order.

    ``local`` takes only the managers the model declares itself, not inherited ones. A plain ``objects`` (no ORM name,
    class or query set class) is left out, since `adorm.Model` declares it. When the model has no manager named
    ``objects``, ``objects`` also stands for the default manager unless that is plain. ``exposed`` holds the
    ``ADORM_EXPOSE`` methods (`exposed_methods()`, the default); ``included`` the labels of the generated models a
    return type may name. A manager whose name would hide an attribute of `adorm.Model` is not generated, with a
    `CodegenWarning`.
    """
    if exposed is None:
        exposed = exposed_methods()
    opts = model._meta
    specs = []
    for manager in opts.local_managers if local else opts.managers:
        if manager.name.startswith("_") or (manager.name != "objects" and hasattr(AdormModelBase, manager.name)):
            _warn(f"{opts.label}.{manager.name}: the manager would hide an attribute of adorm.Model (not generated)")
            continue
        spec = _manager_spec(model, manager, exposed, included)
        if spec is not None:
            specs.append(spec)
    names = {m.name for m in opts.managers}
    default = opts.default_manager
    if "objects" not in names and default is not None and (not local or default in opts.local_managers):
        spec = next((s for s in specs if s.name == default.name), None)
        if spec is not None and not spec.is_plain:
            specs.insert(
                0,
                ManagerSpec(
                    "objects",
                    spec.orm_name,
                    spec.class_name,
                    spec.methods,
                    spec.queryset,
                    spec.base,
                    spec.method_manager,
                ),
            )
    return tuple(specs)


def _check_model(model: type[models.Model]) -> None:
    opts = model._meta
    if not isinstance(opts.pk, models.Field) or opts.pk.get_internal_type() == "CompositePrimaryKey":
        raise CodegenError(f"{opts.label}: composite primary keys are not supported")


def _base_label(model: type[models.Model]) -> str | None:
    opts = model._meta
    if opts.proxy:
        return opts.proxy_for_model._meta.label
    parents = [parent for parent in opts.parents if not parent._meta.abstract]
    return parents[0]._meta.label if len(parents) == 1 else None


def _doc(model: type[models.Model]) -> str | None:
    doc = model.__dict__.get("__doc__")
    if not doc or not doc.strip():
        return None
    name = model.__name__
    if doc.startswith(f"{name}(") and doc.endswith(")"):
        return None
    return doc


def _invalid(model: type[models.Model], exc: ValueError) -> CodegenError:
    return CodegenError(f"{model._meta.label}: {exc}")


def schema_hash(model: type[models.Model], *, warn: bool = True) -> str:
    """The schema hash of the Django model ``model``, the one `adorm generate` writes into ``_meta``: it covers the
    label, primary key, parents, every concrete field (inherited ones included), the forward relations it generates
    and the managers with an ORM name or driver methods. With ``warn`` false it issues no `CodegenWarning`, without
    touching the process-wide warning filters.

    Raises `CodegenError` for a model it cannot describe (a composite primary key, a name no Python name maps to).
    """
    _check_model(model)
    token = _silent.set(True) if not warn else None
    try:
        return _schema_hash(model)
    except ValueError as exc:
        raise _invalid(model, exc) from exc
    finally:
        if token is not None:
            _silent.reset(token)


def _schema_hash(model: type[models.Model]) -> str:
    opts = model._meta
    fields = [field_spec(field) for field in opts.concrete_fields]
    relations = [r for r in (_forward_relation(f) for f in _forward_fields(opts, True)) if r is not None]
    parents = [parent._meta.label for parent in opts.get_parent_list()]
    if opts.proxy:
        parents.insert(0, opts.concrete_model._meta.label)
    managers = manager_specs(model)
    return compute_schema_hash(opts.label, python_name(opts.pk.attname), fields, relations, parents, managers)


_DELETE_CASCADES = frozenset(filter(None, (deletion.CASCADE, getattr(deletion, "DB_CASCADE", None))))
"""``on_delete`` handlers that delete the referring rows, whose own relations the deletion follows."""

_DELETE_READS_OR_UPDATES = frozenset(
    filter(
        None,
        (
            deletion.PROTECT,
            deletion.RESTRICT,
            deletion.SET_NULL,
            deletion.SET_DEFAULT,
            getattr(deletion, "DB_SET_NULL", None),
            getattr(deletion, "DB_SET_DEFAULT", None),
        ),
    )
)
"""``on_delete`` handlers that read the referring rows (``PROTECT``, ``RESTRICT``) or update them (``SET_NULL``,
``SET_DEFAULT``), without following their relations."""


def _is_set(on_delete: Any) -> bool:
    deconstruct = getattr(on_delete, "deconstruct", None)
    return callable(deconstruct) and deconstruct()[0] == "django.db.models.SET"


def delete_models_of(model: type[models.Model]) -> tuple[str, ...]:
    """The labels of the models whose tables deleting rows of the Django model ``model`` reads or writes, sorted: the
    model, its multi-table inheritance parents and, through Django's deletion collector followed by model, every model
    a relation to them cascades to (``CASCADE``, recursively), reads (``PROTECT``, ``RESTRICT``) or updates
    (``SET_NULL``, ``SET_DEFAULT``, ``SET(...)``, without recursion), the models of generic relations
    (``GenericRelation``, recursively) with ``contenttypes.ContentType``. ``DO_NOTHING`` relations are skipped. Another
    ``on_delete`` callable is application code: its model is listed and the relation reported as `CodegenWarning`.

    It follows the model graph, not rows: a model is listed whether or not any row refers to the deleted ones.
    """
    labels = {model._meta.label}
    seen: set[Any] = set()
    pending = [model._meta.concrete_model]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        opts = current._meta
        labels.add(opts.label)
        labels.update(parent._meta.label for parent in opts.get_parent_list())
        for related in deletion.get_candidate_relations_to_delete(opts):
            on_delete = related.field.remote_field.on_delete
            target = related.related_model._meta.concrete_model
            if on_delete is deletion.DO_NOTHING:
                continue
            if on_delete in _DELETE_CASCADES:
                pending.append(target)
                continue
            labels.add(target._meta.label)
            if on_delete not in _DELETE_READS_OR_UPDATES and not _is_set(on_delete):
                _warn(
                    f"{_where(related.field)}: on_delete={getattr(on_delete, '__name__', on_delete)!r} runs application"
                    f" code when {opts.label} rows are deleted; its tables are not followed"
                )
        for field in opts.private_fields:
            if hasattr(field, "bulk_related_objects"):
                pending.append(field.related_model._meta.concrete_model)
                labels.add("contenttypes.ContentType")
    return tuple(sorted(labels))


def model_spec(model: type[models.Model], *, included: Collection[str] | None = None) -> ModelSpec:
    """The `ModelSpec` of the Django model ``model``.

    ``included`` holds the labels of the models generated with it: the class derives from the generated class of
    its multi-table inheritance parent (or of the model a proxy stands for) when that is included, and reverse
    relations from models outside ``included`` are left out; a forward relation to a model outside it is generated
    and reported as `CodegenWarning`. ``None`` includes every model. Raises `CodegenError` for a model it cannot
    describe.
    """
    hashed = schema_hash(model, warn=False)
    try:
        return _model_spec(model, hashed, included)
    except ValueError as exc:
        raise _invalid(model, exc) from exc


def _model_spec(model: type[models.Model], hashed: str, included: Collection[str] | None) -> ModelSpec:
    opts = model._meta
    base = _base_label(model)
    if base is not None and included is not None and base not in included:
        base = None
    include_parents = base is None
    if opts.proxy and base is not None:
        fields = []
    else:
        fields = [field_spec(f) for f in (opts.concrete_fields if include_parents else opts.local_concrete_fields)]
    relations = _relations(model, include_parents, included)
    relations += _tenant_relations(model, include_parents, included)
    managers = manager_specs(model, local=not include_parents, included=included)
    return ModelSpec(
        label=opts.label,
        class_name=opts.object_name,
        pk=python_name(opts.pk.attname),
        fields=tuple(fields),
        relations=tuple(relations),
        schema_hash=hashed,
        base=base,
        doc=_doc(model),
        managers=managers,
        concrete_model=opts.concrete_model._meta.label if opts.proxy else None,
        delete_models=delete_models_of(model),
    )


def _app_config(label: str) -> Any:
    try:
        return apps.get_app_config(label)
    except LookupError:
        raise CodegenError(f"no installed Django app has the label {label!r}") from None


def _selected_model(config: Any, name: str) -> type[models.Model]:
    """The model ``name`` of the app ``config``; `CodegenError` for a model that is unknown or not generated."""
    try:
        model = config.get_model(name)
    except LookupError:
        raise CodegenError(f"the Django app {config.label!r} has no model {name!r}") from None
    opts = model._meta
    if opts.auto_created:
        raise CodegenError(f"{opts.label} is an automatic many-to-many through model, which is not generated")
    if opts.swapped:
        raise CodegenError(f"{opts.label} is swapped for {opts.swapped}: select that model instead")
    return model


def _selected_models(labels: Sequence[str] | None, exclude: Iterable[str]) -> list[tuple[str, list[Any]]]:
    """The models to generate by app label: a model label selects that model, an app label every model of the app."""
    if labels is None:
        labels = [config.label for config in apps.get_app_configs()]
    excluded = set(exclude)
    configs: dict[str, Any] = {}
    chosen_by_app: dict[str, set[Any] | None] = {}
    for label in dict.fromkeys(labels):
        parts = label.split(".") if isinstance(label, str) else []
        if not 1 <= len(parts) <= 2 or not all(part.isidentifier() for part in parts):
            raise CodegenError(f"{label!r} is neither an app label nor a model label ('app_label.Model')")
        if parts[0] not in configs:
            configs[parts[0]] = _app_config(parts[0])
        if len(parts) == 1:
            chosen_by_app[parts[0]] = None
            continue
        model = _selected_model(configs[parts[0]], parts[1])
        chosen = chosen_by_app.setdefault(parts[0], set())
        if chosen is not None:
            chosen.add(model)
    selected = []
    for app_label, chosen in chosen_by_app.items():
        if app_label in excluded:
            continue
        found = [
            model
            for model in configs[app_label].get_models()
            if model._meta.label not in excluded and (chosen is None or model in chosen)
        ]
        if found:
            selected.append((app_label, found))
    return selected


def introspect(labels: Sequence[str] | None = None, *, exclude: Iterable[str] = ()) -> tuple[AppSpec, ...]:
    """The `AppSpec` of each selected Django app: an app label selects every model of the app, a model label that
    model, and ``exclude`` wins. Raises `CodegenError` for a label it cannot select or a model it cannot describe;
    constructs that are not generated are reported as `CodegenWarning`.
    """
    selected = _selected_models(labels, exclude)
    included = {model._meta.label for _, chosen in selected for model in chosen}
    result = []
    for label, chosen in selected:
        specs = tuple(model_spec(model, included=included) for model in chosen)
        try:
            result.append(AppSpec(label, specs))
        except ValueError as exc:
            raise CodegenError(f"app {label}: {exc}") from exc
    return tuple(result)


def setup(settings: str | None = None) -> None:
    """Set up Django with the settings module ``settings`` (default: ``DJANGO_SETTINGS_MODULE``).

    Raises `CodegenError` when no settings module is given or Django cannot be set up with it.
    """
    if settings:
        os.environ["DJANGO_SETTINGS_MODULE"] = settings
    if not os.environ.get("DJANGO_SETTINGS_MODULE"):
        raise CodegenError("give --settings or set DJANGO_SETTINGS_MODULE")
    try:
        django.setup()
    except (ImportError, ImproperlyConfigured) as exc:
        raise CodegenError(f"cannot set up Django: {exc}") from exc
