"""The ORM-independent description of generated models: types, fields, relations, models and apps.

A source (`adorm.codegen.django`) introspects the ORM's models into these values; `adorm.codegen.render` turns them
into Python modules.
"""

from __future__ import annotations

import builtins
import decimal
import hashlib
import json
import keyword

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from adorm.models.base import Model

__all__ = [
    "AppSpec",
    "CodegenError",
    "CodegenWarning",
    "FieldSpec",
    "ModelSpec",
    "NO_DEFAULT",
    "RelationSpec",
    "TypeSpec",
    "compute_schema_hash",
    "python_name",
]

CONSTRAINTS = ("gt", "ge", "lt", "le", "min_length", "max_length", "pattern", "max_digits", "decimal_places")
"""The pydantic ``Field()`` constraints a `FieldSpec` may carry, in the order they are rendered."""


class CodegenError(Exception):
    """Generation cannot proceed: an unknown app, an unsupported model, a file the generator does not own."""


class CodegenWarning(UserWarning):
    """A construct of an ORM model that is not generated, or generated with a looser type."""


class _NoDefault:
    def __repr__(self) -> str:
        return "NO_DEFAULT"

    def __reduce__(self) -> str:
        return "NO_DEFAULT"


NO_DEFAULT: Any = _NoDefault()
"""The ``default`` of a `FieldSpec` without a default value."""


_MODEL_ATTRIBUTES = frozenset(dir(Model))


def _clashes(name: str) -> bool:
    return keyword.iskeyword(name) or name in _MODEL_ATTRIBUTES


def python_name(orm_name: str) -> str:
    """The name of a generated field or relation for the ORM name ``orm_name``.

    A name that clashes with an attribute of `adorm.Model` (pydantic's ``model_dump``, ``model_validate``, ``json``,
    ``copy``, ``schema`` …, adorm's ``save``, ``objects``, ``pk`` …) or with a Python keyword gets ``_`` appended until
    it no longer clashes. A name starting with ``_``, which pydantic treats as private, loses its leading underscores
    and gets ``_`` appended (``_order`` is ``order_``). Any other name is returned unchanged. Raises ``ValueError`` for
    a name that is not an identifier, consists of underscores only, or gives a name containing ``__``.
    """
    if not isinstance(orm_name, str) or not orm_name.isidentifier():
        raise ValueError(f"{orm_name!r} is not an identifier")
    name = orm_name
    if name.startswith("_"):
        name = name.lstrip("_")
        if not name:
            raise ValueError(f"{orm_name!r} consists of underscores only")
        if name[0].isdigit():
            raise ValueError(f"{orm_name!r} is not an identifier without its leading underscores")
        name += "_"
    while _clashes(name):
        name += "_"
    if "__" in name:
        raise ValueError(f"{orm_name!r} gives {name!r}, which a field path cannot contain: it separates segments")
    return name


@dataclass(frozen=True)
class TypeSpec:
    """A Python type of a generated field.

    ``name`` is a builtin (``"int"``, ``"list"``), a ``typing`` name (``"typing.Any"``, ``"typing.Literal"``) or a
    name in a module (``"datetime.date"``, ``"decimal.Decimal"``). ``args`` are type arguments (``list[int]``),
    ``values`` the values of a ``typing.Literal``; ``nullable`` adds ``| None``.
    """

    name: str
    args: tuple[TypeSpec, ...] = ()
    values: tuple[Any, ...] = ()
    nullable: bool = False

    def __post_init__(self) -> None:
        if self.values and self.name != "typing.Literal":
            raise ValueError(f"only typing.Literal takes values, not {self.name}")
        if self.name == "typing.Literal" and not self.values:
            raise ValueError("typing.Literal needs at least one value")
        if "." not in self.name and not isinstance(getattr(builtins, self.name, None), type):
            raise ValueError(f"{self.name!r} is neither a builtin type nor a dotted name")

    @property
    def is_any(self) -> bool:
        return self.name == "typing.Any"

    def with_nullable(self, nullable: bool = True) -> TypeSpec:
        return TypeSpec(self.name, self.args, self.values, nullable)

    def __str__(self) -> str:
        if self.values:
            text = f"{self.name.rpartition('.')[2]}[{', '.join(repr(value) for value in self.values)}]"
        else:
            text = self.name.rpartition(".")[2] if self.name.startswith("typing.") else self.name
            if self.args:
                text += f"[{', '.join(str(arg) for arg in self.args)}]"
        return f"{text} | None" if self.nullable and not self.is_any else text


@dataclass(frozen=True)
class FieldSpec:
    """A field of a generated model.

    ``name`` is the Python name (`python_name()` of ``orm_name``); a renamed field gets ``orm_name`` as its alias.
    ``nullable`` is whether the column takes ``NULL``. ``default`` is a default value or `NO_DEFAULT`;
    ``default_factory`` a dotted path of a callable (``"builtins.dict"``, ``"uuid.uuid4"``). ``db_filled`` marks a
    value the target fills in when the field is not given (an automatic primary key, a default the generator cannot
    express): the field is then optional and ``None`` until the row is stored. ``constraints`` are pydantic
    ``Field()`` constraints (see `CONSTRAINTS`), ``description`` the field's description in the JSON schema.
    """

    name: str
    orm_name: str
    type: TypeSpec
    nullable: bool = False
    default: Any = NO_DEFAULT
    default_factory: str | None = None
    db_filled: bool = False
    constraints: Mapping[str, Any] = field(default_factory=dict, hash=False)
    description: str | None = None
    primary_key: bool = False

    def __post_init__(self) -> None:
        if not self.name.isidentifier():
            raise ValueError(f"{self.name!r} is not an identifier")
        unknown = sorted(set(self.constraints) - set(CONSTRAINTS))
        if unknown:
            raise ValueError(f"unknown constraints: {', '.join(unknown)}")
        if self.default is not NO_DEFAULT and self.default_factory is not None:
            raise ValueError(f"{self.name} has both a default and a default factory")
        ordered = {name: self.constraints[name] for name in CONSTRAINTS if name in self.constraints}
        object.__setattr__(self, "constraints", MappingProxyType(ordered))

    @property
    def alias(self) -> str | None:
        """The alias of a renamed field (its ORM name), ``None`` otherwise."""
        return self.orm_name if self.orm_name != self.name else None


RelationKind = Literal["foreign_key", "reverse_foreign_key", "reverse_one_to_one", "many_to_many"]


@dataclass(frozen=True)
class RelationSpec:
    """A relation of a generated model, rendered as the descriptor its ``kind`` names.

    ``foreign_key``: `adorm.ForeignKey` stored in the field ``column``; ``reverse_foreign_key`` and
    ``reverse_one_to_one``: `adorm.ReverseForeignKey` / `adorm.ReverseOneToOne` on the field ``column`` of
    ``target``; ``many_to_many``: `adorm.ManyToMany` through the model ``through``, linking ``source_column`` to
    ``target_column``; ``reverse`` marks the reverse side of a many-to-many relation declared on ``target``. ``name``
    is the Python name, ``orm_name`` the ORM's.
    """

    kind: RelationKind
    name: str
    orm_name: str
    target: str
    column: str | None = None
    through: str | None = None
    source_column: str | None = None
    target_column: str | None = None
    reverse: bool = False

    def __post_init__(self) -> None:
        if self.kind == "many_to_many":
            if not (self.through and self.source_column and self.target_column):
                raise ValueError(f"the many-to-many relation {self.name} needs through, source_column, target_column")
        elif not self.column:
            raise ValueError(f"the relation {self.name} needs a column")

    @property
    def is_forward(self) -> bool:
        """Whether the relation is declared on this model's side (a foreign key or a declared many-to-many)."""
        return self.kind == "foreign_key" or (self.kind == "many_to_many" and not self.reverse)


@dataclass(frozen=True)
class ModelSpec:
    """A generated model class.

    ``class_name`` names the class, ``label`` is the ORM model's label, ``pk`` its primary key field. ``base`` is the
    label of the generated model the class derives from (a multi-table inheritance parent or the model a proxy
    stands for), ``None`` to derive from `adorm.Model`. ``fields`` and ``relations`` are the class's own; inherited
    ones are on the base. ``schema_hash`` is `compute_schema_hash()` of the whole model, inherited fields included.
    ``doc`` is the class docstring.
    """

    label: str
    class_name: str
    pk: str
    fields: tuple[FieldSpec, ...]
    relations: tuple[RelationSpec, ...]
    schema_hash: str
    base: str | None = None
    doc: str | None = None

    def __post_init__(self) -> None:
        names = [f.name for f in self.fields] + [r.name for r in self.relations]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"{self.label} has several fields or relations named {', '.join(duplicates)}")

    @property
    def orm_names(self) -> dict[str, str]:
        """ORM names of the class's own renamed fields and relations, by Python name."""
        items = [(f.name, f.orm_name) for f in self.fields] + [(r.name, r.orm_name) for r in self.relations]
        return {name: orm_name for name, orm_name in items if name != orm_name}


@dataclass(frozen=True)
class AppSpec:
    """The models of one ORM application (Django app), rendered as one module named ``label``."""

    label: str
    models: tuple[ModelSpec, ...]

    def __post_init__(self) -> None:
        if not self.label.isidentifier() or keyword.iskeyword(self.label):
            raise ValueError(f"{self.label!r} cannot name a module")


def _canonical(value: Any) -> Any:
    if isinstance(value, decimal.Decimal | float | int) and not isinstance(value, bool):
        return str(value)
    return value


def compute_schema_hash(
    label: str, pk: str, fields: Sequence[FieldSpec], relations: Sequence[RelationSpec], parents: Sequence[str] = ()
) -> str:
    """The schema hash of a model: 16 hexadecimal digits of the SHA-256 of a canonical JSON form of its label, primary
    key, parent labels, fields (name, ORM name, type, nullability, constraints) and forward relations.

    Defaults, descriptions and reverse relations do not take part: they do not change the rows a client and a server
    exchange.
    """
    payload = {
        "label": label,
        "pk": pk,
        "parents": list(parents),
        "fields": [
            {
                "name": f.name,
                "orm_name": f.orm_name,
                "type": str(f.type),
                "nullable": f.nullable,
                "constraints": {key: _canonical(value) for key, value in f.constraints.items()},
            }
            for f in fields
        ],
        "relations": [
            {
                "kind": r.kind,
                "name": r.name,
                "orm_name": r.orm_name,
                "target": r.target,
                "column": r.column,
                "through": r.through,
                "source_column": r.source_column,
                "target_column": r.target_column,
            }
            for r in relations
            if r.is_forward
        ],
    }
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]
