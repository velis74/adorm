"""The drivers the demo runs against.

Each entry names an adorm driver class by import path. A driver is available when its module imports and defines the
class; it is not available when the module does not exist, needs a third-party package that is not installed or lacks
the class, and any other import error propagates. The active driver comes from ``ADORM_DEMO_DRIVER``
(``demo.py --driver``); without one the first available driver is active, and with none available there is no active
driver. The API loads every available driver and registers it as adorm's connection profile named after it
(`DemoDriver.profile()`); the active driver is also the profile ``default``. The ``/api/data`` endpoints execute on
``default``, a read endpoint on the profile of the driver its request names.
"""

from __future__ import annotations

import importlib
import os

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from adorm import ConnectionProfile
from adorm.drivers import Driver
from demo.api import memory
from demo.database import is_sqlite

DRIVER_ENV = "ADORM_DEMO_DRIVER"
DATABASE_ENV = "ADORM_DEMO_DATABASE_URL"


def setup_django() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "demo.backend.settings")
    import django

    django.setup()


def setup_memory() -> None:
    """Fill the memory driver's database with the demo database's rows, read through Django."""
    setup_django()
    from demo.models import MODELS

    memory.DATABASE.load(memory.django_tables(MODELS))


Hook = Callable[[Any, ConnectionProfile, Any], Any]


@dataclass(frozen=True)
class DemoDriver:
    """A driver of the demo: ``target`` is ``"module:ClassName"``; ``setup`` prepares the process for it (it runs
    synchronously, outside the event loop). ``on_acquire`` and ``on_release`` are the hooks of its connection
    profile; a ``postgresql_only`` driver is not available on a SQLite demo database."""

    name: str
    target: str
    setup: Callable[[], None] | None = None
    on_acquire: Hook | None = None
    on_release: Hook | None = None
    postgresql_only: bool = False

    @property
    def module(self) -> str:
        return self.target.partition(":")[0]

    def unavailable_reason(self, environ: Mapping[str, str] | None = None) -> str | None:
        """Why the driver is not available; ``None`` when its module imports and defines the class, and, for a
        ``postgresql_only`` driver, ``ADORM_DEMO_DATABASE_URL`` in ``environ`` (default ``os.environ``) is not SQLite.

        A ``ModuleNotFoundError`` for the module itself or for a package outside adorm makes the driver unavailable;
        any other import error propagates.
        """
        url = (os.environ if environ is None else environ).get(DATABASE_ENV, "")
        if self.postgresql_only and is_sqlite(url):
            return "the demo database is SQLite"
        try:
            module = importlib.import_module(self.module)
        except ModuleNotFoundError as exc:
            if exc.name == self.module:
                return f"{self.module} does not exist"
            if exc.name and exc.name.partition(".")[0] not in ("adorm", "demo"):
                return f"{self.module} needs {exc.name}, which is not installed"
            raise
        name = self.target.partition(":")[2]
        if not hasattr(module, name):
            return f"{self.module} does not define {name}"
        return None

    def is_available(self, environ: Mapping[str, str] | None = None) -> bool:
        return self.unavailable_reason(environ) is None

    def driver_class(self) -> type[Driver]:
        """The driver class, imported without running ``setup``."""
        module, _, name = self.target.partition(":")
        return getattr(importlib.import_module(module), name)

    def load(self) -> Driver:
        """Run ``setup`` and return an instance of the driver class."""
        if self.setup is not None:
            self.setup()
        return self.driver_class()()

    def profile(self, driver: Driver, name: str = "default") -> ConnectionProfile:
        """The connection profile ``name`` running on ``driver`` with this driver's hooks."""
        return ConnectionProfile(name, driver, on_acquire=self.on_acquire, on_release=self.on_release)


DRIVERS: tuple[DemoDriver, ...] = (
    DemoDriver("django", "adorm.drivers.django:DjangoDriver", setup_django),
    DemoDriver("asyncpg", "adorm.drivers.asyncpg:AsyncpgDriver", setup_django, postgresql_only=True),
    DemoDriver(
        "memory",
        "demo.api.memory:MemoryDriver",
        setup_memory,
        on_acquire=memory.on_acquire,
        on_release=memory.on_release,
    ),
)


def get(name: str) -> DemoDriver:
    """The demo driver called ``name``; ``LookupError`` for an unknown name."""
    for driver in DRIVERS:
        if driver.name == name:
            return driver
    raise LookupError(f"unknown driver {name!r}; the demo knows {', '.join(d.name for d in DRIVERS)}")


def available(environ: Mapping[str, str] | None = None) -> list[str]:
    """Names of the available drivers, in the order of `DRIVERS`."""
    return [driver.name for driver in DRIVERS if driver.is_available(environ)]


def selected(environ: dict[str, str] | None = None) -> str | None:
    """The active driver's name: ``ADORM_DEMO_DRIVER``, else the first available, else ``None``.

    Raises ``LookupError`` for an unknown name and ``RuntimeError`` for a driver that is not available.
    """
    name = (os.environ if environ is None else environ).get(DRIVER_ENV, "")
    if not name:
        names = available(environ)
        return names[0] if names else None
    reason = get(name).unavailable_reason(environ)
    if reason is not None:
        raise RuntimeError(f"the {name} driver is not available: {reason}")
    return name
