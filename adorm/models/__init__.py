"""Models: the pydantic base class, model identity, relation descriptors, managers and the model registry."""

from adorm.models.base import Executor, get_managers, get_relations, Model, ModelState, set_executor_resolver
from adorm.models.managers import driver_method, driver_methods, DriverMethod, find_driver_method, Manager
from adorm.models.meta import ModelMeta
from adorm.models.registry import model_schema, ModelRegistry, registry
from adorm.models.relations import ForeignKey, ManyToMany, RelatedObject, Relation, ReverseForeignKey, ReverseOneToOne

__all__ = [
    "DriverMethod",
    "Executor",
    "ForeignKey",
    "Manager",
    "ManyToMany",
    "Model",
    "ModelMeta",
    "ModelRegistry",
    "ModelState",
    "RelatedObject",
    "Relation",
    "ReverseForeignKey",
    "ReverseOneToOne",
    "driver_method",
    "driver_methods",
    "find_driver_method",
    "get_managers",
    "get_relations",
    "model_schema",
    "registry",
    "set_executor_resolver",
]
