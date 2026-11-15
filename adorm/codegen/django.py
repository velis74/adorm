"""Introspection of Django models into `adorm.codegen.spec` values.

Needs a configured Django (``django.setup()`` done). Concrete, non-swapped models are generated; auto-created
many-to-many through models are not (a `ManyToMany` names them by label).
"""

from __future__ import annotations

import enum
import os
import re
import warnings

from collections.abc import Collection, Iterable, Sequence
from decimal import Decimal
from typing import Annotated, Any

import django
import pydantic

from django.apps import apps
from django.core import validators as django_validators
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.db.models.fields import NOT_PROVIDED
from django.db.models.fields.proxy import OrderWrt

from adorm.codegen.render import can_render_value
from adorm.codegen.spec import (
    AppSpec,
    CodegenError,
    CodegenWarning,
    compute_schema_hash,
    FieldSpec,
    ModelSpec,
    NO_DEFAULT,
    python_name,
    RelationSpec,
    TypeSpec,
)

__all__ = ["field_spec", "introspect", "model_spec", "schema_hash", "setup"]

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


def _warn(message: str) -> None:
    warnings.warn(message, CodegenWarning, stacklevel=3)


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
        elif field.is_relation and not field.concrete and not field.many_to_many:
            _warn(f"{opts.label}.{field.name}: {type(field).__name__} is not generated")
    return relations


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


def schema_hash(model: type[models.Model]) -> str:
    """The schema hash of the Django model ``model``, the one `adorm generate` writes into ``_meta``: it covers the
    label, primary key, parents, every concrete field (inherited ones included) and the forward relations it
    generates.

    Raises `CodegenError` for a model it cannot describe (a composite primary key, a name no Python name maps to).
    """
    _check_model(model)
    try:
        return _schema_hash(model)
    except ValueError as exc:
        raise _invalid(model, exc) from exc


def _schema_hash(model: type[models.Model]) -> str:
    opts = model._meta
    fields = [field_spec(field) for field in opts.concrete_fields]
    relations = [r for r in (_forward_relation(f) for f in _forward_fields(opts, True)) if r is not None]
    parents = [parent._meta.label for parent in opts.get_parent_list()]
    if opts.proxy:
        parents.insert(0, opts.concrete_model._meta.label)
    return compute_schema_hash(opts.label, python_name(opts.pk.attname), fields, relations, parents)


def model_spec(model: type[models.Model], *, included: Collection[str] | None = None) -> ModelSpec:
    """The `ModelSpec` of the Django model ``model``.

    ``included`` holds the labels of the models generated with it: the class derives from the generated class of
    its multi-table inheritance parent (or of the model a proxy stands for) when that is included, and reverse
    relations from models outside ``included`` are left out; a forward relation to a model outside it is generated
    and reported as `CodegenWarning`. ``None`` includes every model. Raises `CodegenError` for a model it cannot
    describe.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", CodegenWarning)
        hashed = schema_hash(model)
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
    return ModelSpec(
        label=opts.label,
        class_name=opts.object_name,
        pk=python_name(opts.pk.attname),
        fields=tuple(fields),
        relations=tuple(relations),
        schema_hash=hashed,
        base=base,
        doc=_doc(model),
    )


def _selected_models(app_labels: Sequence[str] | None, exclude: Iterable[str]) -> list[tuple[str, list[Any]]]:
    excluded = set(exclude)
    if app_labels is None:
        configs = list(apps.get_app_configs())
    else:
        configs = []
        for label in dict.fromkeys(app_labels):
            try:
                configs.append(apps.get_app_config(label))
            except LookupError:
                raise CodegenError(f"no installed Django app has the label {label!r}") from None
    selected = []
    for config in configs:
        if config.label in excluded:
            continue
        chosen = [model for model in config.get_models() if model._meta.label not in excluded]
        if chosen:
            selected.append((config.label, chosen))
    return selected


def introspect(app_labels: Sequence[str] | None = None, *, exclude: Iterable[str] = ()) -> tuple[AppSpec, ...]:
    """The `AppSpec` of each selected Django app, in ``INSTALLED_APPS`` order for ``app_labels=None``, else in the
    given order.

    ``app_labels=None`` selects every app with models. ``exclude`` holds app labels and model labels to leave out; an
    app left without models is skipped. Raises `CodegenError` for an unknown app label, an app label that cannot name
    a module and an unsupported model. Constructs that are not generated (generic relations, relations that reference
    a field other than the target's primary key, unknown field types, patterns pydantic cannot compile), types
    generated as ``typing.Any`` and forward relations to models that are not generated are reported as
    `CodegenWarning`.
    """
    selected = _selected_models(app_labels, exclude)
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
