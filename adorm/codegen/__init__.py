"""``adorm generate``: adorm model modules generated from the target ORM's models.

The ORM-independent part lives here: the model description (`adorm.codegen.spec`), its rendering as Python
(`adorm.codegen.render`) and the generated files (`adorm.codegen.files`). `adorm.codegen.django` introspects Django
models and is imported only when used.
"""

from adorm.codegen.files import check, Difference, plan, SyncResult, write
from adorm.codegen.render import HEADER, render_module, render_package
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
    TypeSpec,
)

__all__ = [
    "AppSpec",
    "CodegenError",
    "CodegenWarning",
    "Difference",
    "FieldSpec",
    "HEADER",
    "ManagerSpec",
    "MethodSpec",
    "ModelSpec",
    "NO_DEFAULT",
    "ParameterSpec",
    "QuerySetSpec",
    "RelationSpec",
    "SyncResult",
    "TypeSpec",
    "check",
    "compute_schema_hash",
    "plan",
    "python_name",
    "render_module",
    "render_package",
    "write",
]
