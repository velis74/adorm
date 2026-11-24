"""Models: the pydantic base class, model identity, relation descriptors, managers and the model registry."""

from adorm.models.base import (
    Executor,
    get_generic_foreign_keys,
    get_managers,
    get_relations,
    get_tenant_relations,
    Model,
    ModelState,
    set_executor_resolver,
)
from adorm.models.contenttypes import CONTENT_TYPE_MODEL, content_types, ContentTypes, natural_key
from adorm.models.inheritance import parent_link
from adorm.models.managers import driver_method, driver_methods, DriverMethod, find_driver_method, Manager
from adorm.models.meta import ModelMeta
from adorm.models.prefetch import GenericPrefetch, Prefetch, prefetch_related_objects
from adorm.models.registry import model_schema, ModelRegistry, registry
from adorm.models.relations import (
    ForeignKey,
    GenericForeignKey,
    ManyToMany,
    RelatedObject,
    Relation,
    ReverseForeignKey,
    ReverseOneToOne,
    TenantRelation,
)

__all__ = [
    "CONTENT_TYPE_MODEL",
    "ContentTypes",
    "DriverMethod",
    "Executor",
    "ForeignKey",
    "GenericForeignKey",
    "GenericPrefetch",
    "Manager",
    "ManyToMany",
    "Model",
    "ModelMeta",
    "ModelRegistry",
    "ModelState",
    "Prefetch",
    "RelatedObject",
    "Relation",
    "ReverseForeignKey",
    "ReverseOneToOne",
    "TenantRelation",
    "content_types",
    "driver_method",
    "driver_methods",
    "find_driver_method",
    "get_generic_foreign_keys",
    "get_managers",
    "get_relations",
    "get_tenant_relations",
    "model_schema",
    "natural_key",
    "parent_link",
    "prefetch_related_objects",
    "registry",
    "set_executor_resolver",
]
