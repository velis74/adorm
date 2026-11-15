"""Declarations in the target ORM's code that `adorm generate` reads: methods of managers and query sets that become
driver methods (`expose`), and the hand-written adorm classes the generated manager and query set classes derive
from (`client_base`)."""

from __future__ import annotations

import re

from collections.abc import Callable
from typing import Any, TypeVar

__all__ = ["CLIENT_BASE_ATTRIBUTE", "EXPOSED_ATTRIBUTE", "client_base", "expose", "get_client_base", "is_exposed"]

EXPOSED_ATTRIBUTE = "__adorm_exposed__"
"""The function attribute `expose` sets to ``True``."""

CLIENT_BASE_ATTRIBUTE = "__adorm_client_base__"
"""The class attribute `client_base` sets to the base's path."""

_PATH = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*:[A-Za-z_]\w*")

F = TypeVar("F", bound=Callable[..., Any])
C = TypeVar("C", bound=type)


def expose(func: F, /) -> F:
    """Mark a method of a manager or query set class of the target ORM as a driver method.

    `adorm generate` declares every exposed method on the generated manager or query set class with
    `adorm.driver_method`, with the method's signature; the driver runs the original. Returns ``func`` unchanged
    apart from the mark, which Django's ``as_manager()`` and ``from_queryset()`` copy onto the manager's method.
    """
    if not callable(func):
        raise TypeError(f"expose() marks a function, not {func!r}")
    setattr(func, EXPOSED_ATTRIBUTE, True)
    return func


def is_exposed(func: Any) -> bool:
    """Whether ``func`` carries the mark of `expose`."""
    return getattr(func, EXPOSED_ATTRIBUTE, False) is True


def client_base(path: str) -> Callable[[C], C]:
    """A class decorator for a manager or query set class of the target ORM: the class `adorm generate` writes for it
    derives from the adorm class at ``path`` (``"package.module:ClassName"``) instead of `adorm.Manager` or
    `adorm.QuerySet`.

    The base holds the methods ported by hand that run on the client: methods that build queries, such as a filter
    the target's method applies. It derives from `adorm.Manager` or `adorm.QuerySet`; the generator checks that.
    """
    if not isinstance(path, str) or not _PATH.fullmatch(path):
        raise ValueError(f"{path!r} is not a 'package.module:ClassName' path")

    def decorate(cls: C) -> C:
        if not isinstance(cls, type):
            raise TypeError(f"client_base() decorates a class, not {cls!r}")
        setattr(cls, CLIENT_BASE_ATTRIBUTE, path)
        return cls

    return decorate


def get_client_base(cls: type) -> str | None:
    """The path `client_base` set on ``cls`` or the nearest base class that has one, ``None`` without one."""
    value = getattr(cls, CLIENT_BASE_ATTRIBUTE, None)
    return value if isinstance(value, str) else None
