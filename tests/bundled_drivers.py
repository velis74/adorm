"""The driver classes adorm bundles."""

from __future__ import annotations

import importlib
import pkgutil

import adorm.drivers

from adorm.drivers import Driver


def bundled_driver_classes() -> set[type[Driver]]:
    """Every `Driver` subclass defined in a module under ``adorm.drivers`` other than ``adorm.drivers.base``; imports
    every such module."""
    for module in pkgutil.walk_packages(adorm.drivers.__path__, "adorm.drivers."):
        importlib.import_module(module.name)

    def subclasses(cls: type[Driver]):
        for sub in cls.__subclasses__():
            yield sub
            yield from subclasses(sub)

    return {
        cls
        for cls in subclasses(Driver)
        if cls.__module__.startswith("adorm.drivers.") and cls.__module__ != "adorm.drivers.base"
    }
