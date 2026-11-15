"""Comparison of the demo's adorm models with the Django models they mirror.

Compared: the primary key name; every concrete field's presence, Python type, nullability, choices (against a
``Literal``), ``max_length``, ``max_digits`` and ``decimal_places``; fields Django lacks; ``Meta.ordering``, which
adorm models cannot express; every relation's kind, target and join columns (and the through model of a many-to-many
relation); Django foreign keys and many-to-many fields the adorm model lacks. Defaults, validators and generic foreign
keys are not compared, nor the reverse of a model's own parent link, which its class inherits.
"""

import datetime
import decimal
import types
import typing
import uuid

from typing import Annotated, Any, Literal

from django.apps import apps
from django.db import models

from adorm.models import ForeignKey, get_relations, ManyToMany, Model, Relation, ReverseForeignKey, ReverseOneToOne

PYTHON_TYPES: dict[str, type] = {
    "AutoField": int,
    "BigAutoField": int,
    "SmallAutoField": int,
    "IntegerField": int,
    "BigIntegerField": int,
    "SmallIntegerField": int,
    "PositiveIntegerField": int,
    "PositiveBigIntegerField": int,
    "PositiveSmallIntegerField": int,
    "CharField": str,
    "TextField": str,
    "SlugField": str,
    "BooleanField": bool,
    "FloatField": float,
    "DecimalField": decimal.Decimal,
    "DateField": datetime.date,
    "DateTimeField": datetime.datetime,
    "TimeField": datetime.time,
    "DurationField": datetime.timedelta,
    "UUIDField": uuid.UUID,
    "BinaryField": bytes,
}
"""Python types by Django internal type; a type missing here (``JSONField``) is not compared."""


def _strip(annotation: Any) -> tuple[Any, bool]:
    """``annotation`` without ``Annotated`` and ``None``, and whether ``None`` was part of it."""
    while typing.get_origin(annotation) is Annotated:
        annotation = typing.get_args(annotation)[0]
    origin = typing.get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        args = typing.get_args(annotation)
        rest = [arg for arg in args if arg is not type(None)]
        nullable = len(rest) < len(args)
        return (rest[0] if len(rest) == 1 else typing.Union[tuple(rest)]), nullable  # noqa: UP007 - built at run time
    return annotation, False


def _python_type(field: models.Field) -> type | None:
    if field.is_relation:
        return _python_type(field.target_field)
    return PYTHON_TYPES.get(field.get_internal_type())


def _metadata(info: Any, name: str) -> Any:
    for item in info.metadata:
        value = getattr(item, name, None)
        if value is not None:
            return value
    return None


def _compare_field(label: str, name: str, field: models.Field, info: Any) -> list[str]:
    problems = []
    annotation, nullable = _strip(info.annotation)
    if not field.primary_key and field.null != nullable:
        problems.append(f"{label}.{name}: null={field.null}, the adorm field disagrees")
    if field.choices:
        values = {value for value, _ in field.flatchoices}
        if typing.get_origin(annotation) is not Literal:
            problems.append(f"{label}.{name}: choices {sorted(values)}, the adorm field is not a Literal")
        elif set(typing.get_args(annotation)) != values:
            literal = sorted(typing.get_args(annotation))
            problems.append(f"{label}.{name}: choices {sorted(values)}, the adorm Literal has {literal}")
        return problems
    expected = _python_type(field)
    if expected is not None and annotation is not expected:
        problems.append(f"{label}.{name}: Django type {expected.__name__}, the adorm field is {annotation!r}")
    for attribute, constraint in (
        ("max_length", "max_length"),
        ("max_digits", "max_digits"),
        ("decimal_places", "decimal_places"),
    ):
        value = getattr(field, attribute, None) if expected in (str, decimal.Decimal) else None
        if value is not None and _metadata(info, constraint) != value:
            problems.append(f"{label}.{name}: {attribute}={value}, the adorm field has {_metadata(info, constraint)}")
    return problems


def _is_forward(field: Any) -> bool:
    return field.concrete or (field.many_to_many and not field.auto_created)


def _relation_name(field: Any) -> str:
    return field.name if _is_forward(field) else field.get_accessor_name()


def _compare_relation(label: str, name: str, relation: Relation, field: Any) -> list[str]:
    where = f"{label}.{name}"
    problems = []
    if relation.target != field.related_model._meta.label:
        problems.append(f"{where} targets {relation.target}, Django {field.related_model._meta.label}")
    if isinstance(relation, ForeignKey):
        if not isinstance(field, models.ForeignKey) or relation.column != field.attname:
            problems.append(f"{where} is a ForeignKey on {relation.column!r}, Django disagrees")
    elif isinstance(relation, ReverseForeignKey | ReverseOneToOne):
        kind = type(relation).__name__
        if (
            not isinstance(field, models.ForeignObjectRel)
            or field.many_to_many
            or field.one_to_one != isinstance(relation, ReverseOneToOne)
            or relation.column != field.field.attname
        ):
            problems.append(f"{where} is a {kind} on {relation.column!r}, Django disagrees")
    elif isinstance(relation, ManyToMany):
        if not field.many_to_many:
            problems.append(f"{where} is a ManyToMany, Django disagrees")
        else:
            forward = field if _is_forward(field) else field.field
            through = forward.remote_field.through._meta.label
            columns = (forward.m2m_column_name(), forward.m2m_reverse_name())
            if forward is not field:
                columns = columns[::-1]
            actual = (relation.through, relation.source_column, relation.target_column)
            if actual != (through, *columns):
                problems.append(f"{where} is a ManyToMany through {actual}, Django through {(through, *columns)}")
    else:
        problems.append(f"{where} is a bare Relation without join columns")
    return problems


def compare(model: type[Model]) -> list[str]:
    """Differences between the adorm model ``model`` and the Django model with the same label."""
    label = model._meta.label
    opts = apps.get_model(label)._meta
    problems: list[str] = []
    fields = model.__pydantic_fields__
    if model._meta.pk != opts.pk.attname:
        problems.append(f"{label}: primary key {model._meta.pk!r}, Django has {opts.pk.attname!r}")
    if opts.ordering:
        problems.append(f"{label}: Meta.ordering {list(opts.ordering)}, which adorm models cannot express")
    concrete = {field.attname: field for field in opts.concrete_fields}
    for name, field in concrete.items():
        info = fields.get(name)
        if info is None:
            problems.append(f"{label}.{name} is missing")
        else:
            problems.extend(_compare_field(label, name, field, info))
    problems.extend(f"{label}.{name} is not a Django field" for name in fields if name not in concrete)
    django_relations = {
        _relation_name(field): field
        for field in opts.get_fields()
        if field.is_relation and field.related_model is not None
    }
    relations = get_relations(model)
    for name, relation in relations.items():
        if isinstance(relation, ReverseOneToOne) and relation.target == label:
            continue  # the parent's reverse of this model's own parent link, inherited by the class
        field = django_relations.get(name)
        if field is None:
            problems.append(f"{label}.{name} is not a Django relation")
        else:
            problems.extend(_compare_relation(label, name, relation, field))
    problems.extend(
        f"{label}.{name}: the Django relation is missing"
        for name, field in django_relations.items()
        if _is_forward(field) and name not in relations
    )
    return problems
