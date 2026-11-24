"""The ORM-independent description of generated models: types, fields, relations, managers, models and apps.

A source (`adorm.codegen.django`) introspects the ORM's models into these values; `adorm.codegen.render` turns them
into Python modules.
"""

from __future__ import annotations

import builtins
import decimal
import hashlib
import inspect
import json
import keyword
import re

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
    "ManagerSpec",
    "MethodSpec",
    "ModelSpec",
    "NO_DEFAULT",
    "ParameterSpec",
    "QuerySetSpec",
    "RelationSpec",
    "ThirdPartyMethodsWarning",
    "TypeSpec",
    "compute_schema_hash",
    "python_name",
]

CONSTRAINTS = ("gt", "ge", "lt", "le", "min_length", "max_length", "pattern", "max_digits", "decimal_places")
"""The pydantic ``Field()`` constraints a `FieldSpec` may carry, in the order they are rendered."""


class CodegenError(Exception):
    """Generation cannot proceed: an unknown app or model, an unsupported model, a file the generator does not own."""


class CodegenWarning(UserWarning):
    """A construct of an ORM model that is not generated, or generated with a looser type."""


class ThirdPartyMethodsWarning(CodegenWarning):
    """The manager and query set methods of installed packages that a manager does not generate, one per manager."""


class _NoDefault:
    def __repr__(self) -> str:
        return "NO_DEFAULT"

    def __reduce__(self) -> str:
        return "NO_DEFAULT"


NO_DEFAULT: Any = _NoDefault()
"""The ``default`` of a `FieldSpec` without a default value."""


_MODEL_ATTRIBUTES = frozenset(dir(Model))
_LABEL = re.compile(r"[A-Za-z_]\w*\.[A-Za-z_]\w*")
_PATH = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*:[A-Za-z_]\w*")


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

    ``name`` is a builtin (``"int"``, ``"list"``), ``"None"``, a ``typing`` name (``"typing.Any"``,
    ``"typing.Literal"``) or a name in a module (``"datetime.date"``, ``"decimal.Decimal"``). ``args`` are type
    arguments (``list[int]``), ``values`` the values of a ``typing.Literal``; ``nullable`` adds ``| None``. With
    ``model``, ``name`` is the label of a generated model and the type is its class (a driver method's return type).
    """

    name: str
    args: tuple[TypeSpec, ...] = ()
    values: tuple[Any, ...] = ()
    nullable: bool = False
    model: bool = False

    def __post_init__(self) -> None:
        if self.values and self.name != "typing.Literal":
            raise ValueError(f"only typing.Literal takes values, not {self.name}")
        if self.name == "typing.Literal" and not self.values:
            raise ValueError("typing.Literal needs at least one value")
        if self.model:
            if self.args or self.values or not _LABEL.fullmatch(self.name):
                raise ValueError(f"a model type is a model label without arguments, not {self.name!r}")
        elif self.name != "None" and "." not in self.name and not isinstance(getattr(builtins, self.name, None), type):
            raise ValueError(f"{self.name!r} is neither a builtin type nor a dotted name")

    @property
    def is_any(self) -> bool:
        return self.name == "typing.Any"

    def with_nullable(self, nullable: bool = True) -> TypeSpec:
        return TypeSpec(self.name, self.args, self.values, nullable, self.model)

    def __str__(self) -> str:
        if self.model:
            return f"model:{self.name}" + (" | None" if self.nullable else "")
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


RelationKind = Literal[
    "foreign_key", "reverse_foreign_key", "reverse_one_to_one", "many_to_many", "generic_foreign_key", "tenant"
]

SchemaSource = tuple[Any, ...]
"""The expression of a tenant relation's schema as a tree: ``("F", path)``, ``("Value", value)`` (a ``str`` or an
``int``), ``("Cast", argument, max_length)`` (a cast to ``str``; ``max_length`` ``None`` or an ``int``) or
``(class name, (argument, ...))`` for ``Concat``, ``Lower``, ``Upper``, ``Replace``, ``Left``, ``Right``, ``LPad`` and
``Coalesce``; paths are adorm names."""


@dataclass(frozen=True)
class RelationSpec:
    """A relation of a generated model, rendered as the descriptor its ``kind`` names.

    ``foreign_key``: `adorm.ForeignKey` stored in the field ``column``; ``reverse_foreign_key`` and
    ``reverse_one_to_one``: `adorm.ReverseForeignKey` / `adorm.ReverseOneToOne` on the field ``column`` of
    ``target``; ``many_to_many``: `adorm.ManyToMany` through the model ``through``, linking ``source_column`` to
    ``target_column``; ``reverse`` marks the reverse side of a many-to-many relation declared on ``target``;
    ``generic_foreign_key``: `adorm.GenericForeignKey` with the content type in the foreign key ``ct_field`` (to
    ``target``, the content type model) and the object id in the field ``column``; ``for_concrete_model`` is its
    ``for_concrete_model``; ``tenant``: `adorm.TenantRelation` to the rows of ``target`` whose field ``column`` holds
    the owner's key, in the schemas the path ``schema`` (adorm names) or the expression ``schema_source`` (a
    `SchemaSource`) gives, with the tables of the models ``models`` (sorted labels) in each schema and the session
    function ``session``. ``name`` is the Python name, ``orm_name`` the ORM's.
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
    ct_field: str | None = None
    for_concrete_model: bool = True
    schema: str | None = None
    schema_source: SchemaSource | None = None
    models: tuple[str, ...] = ()
    session: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "generic_foreign_key" and not self.ct_field:
            raise ValueError(f"the generic foreign key {self.name} needs ct_field")
        if self.kind == "tenant":
            if (self.schema is None) == (self.schema_source is None):
                raise ValueError(f"the tenant relation {self.name} needs schema or schema_source")
            if self.target not in self.models or list(self.models) != sorted(set(self.models)):
                raise ValueError(f"the models of the tenant relation {self.name} are sorted labels with its target")
        if self.kind == "many_to_many":
            if not (self.through and self.source_column and self.target_column):
                raise ValueError(f"the many-to-many relation {self.name} needs through, source_column, target_column")
        elif not self.column:
            raise ValueError(f"the relation {self.name} needs a column")

    @property
    def is_forward(self) -> bool:
        """Whether the relation is declared on this model's side (a foreign key or a declared many-to-many)."""
        return self.kind == "foreign_key" or (self.kind == "many_to_many" and not self.reverse)


ParameterKind = Literal["positional_only", "positional_or_keyword", "var_positional", "keyword_only", "var_keyword"]
_KINDS: dict[str, inspect._ParameterKind] = {
    "positional_only": inspect.Parameter.POSITIONAL_ONLY,
    "positional_or_keyword": inspect.Parameter.POSITIONAL_OR_KEYWORD,
    "var_positional": inspect.Parameter.VAR_POSITIONAL,
    "keyword_only": inspect.Parameter.KEYWORD_ONLY,
    "var_keyword": inspect.Parameter.VAR_KEYWORD,
}


@dataclass(frozen=True)
class ParameterSpec:
    """A parameter of a driver method: its ``name``, ``kind`` (as ``inspect.Parameter`` kinds, in snake case), its
    ``type`` (``None`` for a parameter without an annotation) and its ``default`` (`NO_DEFAULT` for none)."""

    name: str
    kind: ParameterKind = "positional_or_keyword"
    type: TypeSpec | None = None
    default: Any = NO_DEFAULT

    def __post_init__(self) -> None:
        if not self.name.isidentifier() or keyword.iskeyword(self.name):
            raise ValueError(f"{self.name!r} is not a parameter name")
        if self.kind not in _KINDS:
            raise ValueError(f"unknown parameter kind {self.kind!r}")
        if self.type is not None and self.type.model:
            raise ValueError(f"the parameter {self.name} cannot take a model instance")
        if self.default is not NO_DEFAULT and self.kind in ("var_positional", "var_keyword"):
            raise ValueError(f"the parameter {self.name} cannot have a default")


@dataclass(frozen=True)
class MethodSpec:
    """A driver method of a generated manager or query set class.

    ``target`` is ``"manager"`` for a method of the manager, ``"queryset"`` for one of the query set. ``parameters``
    follow ``self``, in Python's order. ``returns`` is the return type, ``None`` for a method without a return
    annotation. ``doc`` is the docstring; ``queryset_only`` keeps a query set method off the manager.
    """

    name: str
    target: Literal["manager", "queryset"]
    parameters: tuple[ParameterSpec, ...] = ()
    returns: TypeSpec | None = None
    doc: str | None = None
    queryset_only: bool = False

    def __post_init__(self) -> None:
        if not self.name.isidentifier() or keyword.iskeyword(self.name) or self.name.startswith("_"):
            raise ValueError(f"{self.name!r} is not a public method name")
        if self.target not in ("manager", "queryset"):
            raise ValueError(f"the target of {self.name} is 'manager' or 'queryset', not {self.target!r}")
        if self.queryset_only and self.target != "queryset":
            raise ValueError(f"only a query set method is queryset_only, {self.name} is not")
        inspect.Signature(
            [inspect.Parameter("self", inspect.Parameter.POSITIONAL_ONLY)]
            + [
                inspect.Parameter(
                    p.name,
                    _KINDS[p.kind],
                    default=inspect.Parameter.empty if p.default is NO_DEFAULT else p.default,
                )
                for p in self.parameters
            ]
        )


def _check_path(path: str | None, what: str) -> None:
    if path is not None and not _PATH.fullmatch(path):
        raise ValueError(f"the {what} base {path!r} is not a 'package.module:ClassName' path")


@dataclass(frozen=True)
class QuerySetSpec:
    """A generated query set class: ``class_name`` derives from the hand-written class at ``base``
    (``"package.module:ClassName"``), or from `adorm.QuerySet` when ``base`` is ``None``, and declares ``methods``."""

    class_name: str
    methods: tuple[MethodSpec, ...] = ()
    base: str | None = None

    def __post_init__(self) -> None:
        if not self.class_name.isidentifier():
            raise ValueError(f"{self.class_name!r} is not a class name")
        _check_path(self.base, "query set")
        for method in self.methods:
            if method.target != "queryset":
                raise ValueError(f"{self.class_name}.{method.name} is not a query set method")
        _check_unique(self.class_name, [m.name for m in self.methods])


@dataclass(frozen=True)
class ManagerSpec:
    """A manager of a generated model, the class attribute ``name``.

    ``orm_name`` is the target ORM's manager the driver applies to every operation (`adorm.Manager`), ``None`` for
    one without manager logic; ``method_manager`` is the target ORM's manager the driver methods run on when it is not
    ``orm_name``. ``class_name``, when not ``None``, is a generated manager class deriving from ``base`` (or
    `adorm.Manager`) that declares ``methods``; ``queryset`` is the generated query set class of the manager. Without a
    class name the manager is ``Manager(orm_name, method_manager=...)``, or ``<queryset>.as_manager(...)`` with a
    query set class.
    """

    name: str
    orm_name: str | None = None
    class_name: str | None = None
    methods: tuple[MethodSpec, ...] = ()
    queryset: QuerySetSpec | None = None
    base: str | None = None
    method_manager: str | None = None

    def __post_init__(self) -> None:
        if not self.name.isidentifier() or self.name.startswith("_") or keyword.iskeyword(self.name):
            raise ValueError(f"{self.name!r} is not a manager name")
        if self.name != "objects" and self.name in _MODEL_ATTRIBUTES:
            raise ValueError(f"the manager {self.name!r} would hide an attribute of adorm.Model")
        for orm_name in (self.orm_name, self.method_manager):
            if orm_name is not None and (not orm_name.isidentifier() or orm_name.startswith("_")):
                raise ValueError(f"{orm_name!r} is not an ORM manager name")
        if self.class_name is None and (self.methods or self.base is not None):
            raise ValueError(f"the manager {self.name} declares methods or a base, so it needs a class name")
        if self.class_name is not None and not self.class_name.isidentifier():
            raise ValueError(f"{self.class_name!r} is not a class name")
        _check_path(self.base, "manager")
        for method in self.methods:
            if method.target != "manager":
                raise ValueError(f"{self.class_name}.{method.name} is not a manager method")
        _check_unique(str(self.class_name), [m.name for m in self.methods])
        if self.has_methods and self.orm_name is None and self.method_manager is None:
            raise ValueError(f"the manager {self.name} has driver methods, so it needs an ORM name")

    @property
    def has_methods(self) -> bool:
        """Whether the manager or its query set class declares driver methods."""
        return bool(self.methods or (self.queryset is not None and self.queryset.methods))

    @property
    def class_key(self) -> tuple[Any, ...]:
        """What the generated manager class depends on: two specs with the same key share one class."""
        return (self.class_name, self.methods, self.queryset, self.base)

    @property
    def is_plain(self) -> bool:
        """Whether the manager is ``Manager()``: no ORM names, no class, no query set class."""
        return (
            self.orm_name is None and self.method_manager is None and self.class_name is None and self.queryset is None
        )


def _check_unique(owner: str, names: Sequence[str]) -> None:
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"{owner} declares several methods named {', '.join(duplicates)}")


@dataclass(frozen=True)
class ModelSpec:
    """A generated model class.

    ``class_name`` names the class, ``label`` is the ORM model's label, ``pk`` its primary key field. ``base`` is the
    label of the generated model the class derives from (a multi-table inheritance parent or the model a proxy
    stands for), ``None`` to derive from `adorm.Model`. ``fields`` and ``relations`` are the class's own; inherited
    ones are on the base. ``managers`` are the managers the class declares; a plain ``objects`` is inherited from
    `adorm.Model` and not listed. ``schema_hash`` is `compute_schema_hash()` of the whole model, inherited fields
    included. ``doc`` is the class docstring. ``concrete_model`` is the label of the concrete model of a proxy model,
    ``None`` for a concrete model. ``delete_models`` are the labels of `adorm.ModelMeta.delete_models`, sorted; empty
    when they are unknown.
    """

    label: str
    class_name: str
    pk: str
    fields: tuple[FieldSpec, ...]
    relations: tuple[RelationSpec, ...]
    schema_hash: str
    base: str | None = None
    doc: str | None = None
    managers: tuple[ManagerSpec, ...] = ()
    concrete_model: str | None = None
    delete_models: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        names = [f.name for f in self.fields] + [r.name for r in self.relations] + [m.name for m in self.managers]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"{self.label} has several fields or relations named {', '.join(duplicates)}")

    @property
    def orm_names(self) -> dict[str, str]:
        """ORM names of the class's own renamed fields and relations, by Python name."""
        relations = [(r.name, r.orm_name) for r in self.relations if r.kind not in ("generic_foreign_key", "tenant")]
        items = [(f.name, f.orm_name) for f in self.fields] + relations
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


def _manager_payload(manager: ManagerSpec) -> dict[str, Any]:
    methods = list(manager.methods) + list(manager.queryset.methods if manager.queryset is not None else ())
    return {
        "name": manager.name,
        "orm_name": manager.orm_name,
        "method_manager": manager.method_manager,
        "methods": [
            {
                "name": m.name,
                "target": m.target,
                "queryset_only": m.queryset_only,
                "parameters": [[p.name, p.kind, None if p.type is None else str(p.type)] for p in m.parameters],
            }
            for m in methods
        ],
    }


def compute_schema_hash(
    label: str,
    pk: str,
    fields: Sequence[FieldSpec],
    relations: Sequence[RelationSpec],
    parents: Sequence[str] = (),
    managers: Sequence[ManagerSpec] = (),
) -> str:
    """The schema hash of a model: 16 hexadecimal digits of the SHA-256 of a canonical JSON form of its label, primary
    key, parent labels, fields (name, ORM name, type, nullability, constraints), forward relations and the managers
    with an ORM name (name, ORM names, the driver methods' names, targets and parameter kinds and types).

    Defaults, descriptions, return types and reverse relations do not take part: they do not change the data a model
    and its driver exchange. A model whose managers all lack ORM names hashes as one without managers.
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
    hashed = [_manager_payload(m) for m in managers if m.orm_name is not None or m.method_manager is not None]
    if hashed:
        payload["managers"] = hashed
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]
