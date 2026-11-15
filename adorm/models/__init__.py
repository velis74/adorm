"""Models: the pydantic base class, model identity, relation descriptors and the model registry."""

from adorm.models.base import Executor, get_relations, Model, ModelState, set_executor_resolver
from adorm.models.meta import ModelMeta
from adorm.models.registry import model_schema, ModelRegistry, registry
from adorm.models.relations import ForeignKey, ManyToMany, RelatedObject, Relation, ReverseForeignKey

__all__ = [
    "Executor",
    "ForeignKey",
    "ManyToMany",
    "Model",
    "ModelMeta",
    "ModelRegistry",
    "ModelState",
    "RelatedObject",
    "Relation",
    "ReverseForeignKey",
    "get_relations",
    "model_schema",
    "registry",
    "set_executor_resolver",
]
