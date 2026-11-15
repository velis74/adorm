"""The drivers the demo runs against.

Each entry names an adorm driver class by import path. A driver is available when its module imports; it is not
available when the module does not exist or needs a third-party package that is not installed, and any other import
error propagates. The active driver comes from ``ADORM_DEMO_DRIVER`` (``demo.py --driver``); without one the first
available driver is active, and with none available there is no active driver. The API reports the active driver; it
does not execute operations.
"""

from __future__ import annotations

import importlib
import os

from collections.abc import Callable
from dataclasses import dataclass

from adorm.drivers import Driver

DRIVER_ENV = "ADORM_DEMO_DRIVER"


def setup_django() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "demo.backend.settings")
    import django

    django.setup()


@dataclass(frozen=True)
class DemoDriver:
    """A driver of the demo: ``target`` is ``"module:ClassName"``; ``setup`` prepares the process for it."""

    name: str
    target: str
    setup: Callable[[], None] | None = None

    @property
    def module(self) -> str:
        return self.target.partition(":")[0]

    def unavailable_reason(self) -> str | None:
        """Why the driver is not available; ``None`` when its module imports.

        A ``ModuleNotFoundError`` for the module itself or for a package outside adorm makes the driver unavailable;
        any other import error propagates.
        """
        try:
            importlib.import_module(self.module)
        except ModuleNotFoundError as exc:
            if exc.name == self.module:
                return f"{self.module} does not exist"
            if exc.name and exc.name.partition(".")[0] not in ("adorm", "demo"):
                return f"{self.module} needs {exc.name}, which is not installed"
            raise
        return None

    def is_available(self) -> bool:
        return self.unavailable_reason() is None

    def driver_class(self) -> type[Driver]:
        """The driver class, imported without running ``setup``."""
        module, _, name = self.target.partition(":")
        return getattr(importlib.import_module(module), name)

    def load(self) -> Driver:
        """Run ``setup`` and return an instance of the driver class."""
        if self.setup is not None:
            self.setup()
        return self.driver_class()()


DRIVERS: tuple[DemoDriver, ...] = (
    DemoDriver("django", "adorm.drivers.django:DjangoDriver", setup_django),
    DemoDriver("asyncpg", "adorm.drivers.asyncpg:AsyncpgDriver"),
)


def get(name: str) -> DemoDriver:
    """The demo driver called ``name``; ``LookupError`` for an unknown name."""
    for driver in DRIVERS:
        if driver.name == name:
            return driver
    raise LookupError(f"unknown driver {name!r}; the demo knows {', '.join(d.name for d in DRIVERS)}")


def available() -> list[str]:
    """Names of the available drivers, in the order of `DRIVERS`."""
    return [driver.name for driver in DRIVERS if driver.is_available()]


def selected(environ: dict[str, str] | None = None) -> str | None:
    """The active driver's name: ``ADORM_DEMO_DRIVER``, else the first available, else ``None``.

    Raises ``LookupError`` for an unknown name and ``RuntimeError`` for a driver that is not available.
    """
    name = (os.environ if environ is None else environ).get(DRIVER_ENV, "")
    if not name:
        names = available()
        return names[0] if names else None
    reason = get(name).unavailable_reason()
    if reason is not None:
        raise RuntimeError(f"the {name} driver is not available: {reason}")
    return name
