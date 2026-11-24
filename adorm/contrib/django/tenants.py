"""The declaration of a read-only reverse relation to a model whose rows live in tenant schemas, on a Django model.

`TenantRelation` is an attribute of a shared Django model that ``adorm generate`` emits as `adorm.TenantRelation` on
the generated model. Django ignores it: it adds no column, no migration and no system check. Importing this module
does not touch Django's app registry.
"""

from __future__ import annotations

import re

from collections.abc import Iterable
from typing import Any

__all__ = ["TenantRelation"]

_MODEL_LABEL = re.compile(r"[A-Za-z_]\w*\.[A-Za-z_]\w*")
_APP_LABEL = re.compile(r"[A-Za-z_]\w*")


class TenantRelation:
    """A read-only reverse relation from the shared model it is declared on to ``to``, a model whose rows live in
    several tenant schemas.

    ``to`` is the tenant model's label (``"app_label.Model"``) and ``field`` the name of its ``ForeignKey`` (or
    ``OneToOneField``) to the declaring model's primary key. ``schema`` names where the schemas of an instance come
    from: a Django query path from the declaring model to the field that holds a schema name
    (``"organisations__organisation__schema_name"``), or a Django expression of the declaring model's rows that gives
    it. ``tenant_models`` holds the model labels and app labels whose tables live in each tenant schema (an app label
    stands for all its models, automatic many-to-many through models included); by default the app of ``to``. ``to``
    is always one of them. ``session`` is ``None`` or the dotted import path of a function ``session(schema,
    executor)`` that returns the executor of the instances read from ``schema``.

    The constructor checks the shape of the arguments (``TypeError``, ``ValueError``); ``adorm generate`` checks them
    against the installed models and raises ``CodegenError`` for one that names nothing. Reading the attribute returns
    the declaration itself, on the class and on instances.
    """

    name: str

    def __init__(
        self,
        to: str,
        *,
        field: str,
        schema: str | Any,
        tenant_models: Iterable[str] | None = None,
        session: str | None = None,
    ) -> None:
        if not isinstance(to, str) or not _MODEL_LABEL.fullmatch(to):
            raise ValueError(f"to is a model label ('app_label.Model'), not {to!r}")
        if not isinstance(field, str) or not field.isidentifier():
            raise ValueError(f"field is a field name, not {field!r}")
        if isinstance(schema, str):
            if not all(segment.isidentifier() and not segment.startswith("_") for segment in schema.split("__")):
                raise ValueError(f"schema is a query path ('relation__field') or an expression, not {schema!r}")
        elif not hasattr(schema, "resolve_expression"):
            raise TypeError(f"schema is a query path or a Django expression, not {type(schema).__name__}")
        if tenant_models is None:
            labels: tuple[str, ...] = (to.partition(".")[0],)
        elif isinstance(tenant_models, str):
            raise TypeError("tenant_models is an iterable of model and app labels, not a string")
        else:
            labels = tuple(tenant_models)
            for label in labels:
                if not isinstance(label, str) or not (_MODEL_LABEL.fullmatch(label) or _APP_LABEL.fullmatch(label)):
                    raise ValueError(f"tenant_models holds model labels and app labels, not {label!r}")
        if session is not None and (
            not isinstance(session, str)
            or len(session.split(".")) < 2
            or not all(part.isidentifier() for part in session.split("."))
        ):
            raise ValueError(f"session is a dotted import path, not {session!r}")
        self.to = to
        self.field = field
        self.schema = schema
        self.tenant_models = labels
        self.session = session

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    def __get__(self, instance: Any, owner: type | None = None) -> TenantRelation:
        return self

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.to!r}, field={self.field!r}, schema={self.schema!r})"
