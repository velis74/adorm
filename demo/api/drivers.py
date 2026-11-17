"""The drivers the demo runs on and their connection profiles.

A driver is available when its module imports; a ``ModuleNotFoundError`` of a package outside adorm makes it
unavailable, any other import error propagates. Every available driver has three connection profiles: one named after
it with `POOL`, ``<name>-small`` with `SMALL_POOL`, and ``<name>-desk`` with `SMALL_POOL`, whose ``on_acquire`` hook
sets a custom option. Their connections are recorded by `demo.api.capture.CAPTURE`.
"""

from __future__ import annotations

import importlib

from dataclasses import dataclass

from adorm import ConnectionProfile, PoolSettings
from adorm.drivers import Driver
from adorm.settings import import_object
from demo.api.capture import CAPTURE

POOL = PoolSettings(max_connections=6, acquire_timeout=5.0)
SMALL_POOL = PoolSettings(max_connections=2, acquire_timeout=1.0)


@dataclass(frozen=True)
class DemoDriver:
    """A driver of the demo: ``target`` is its class as ``"module:Name"``."""

    name: str
    target: str
    description: str

    def unavailable_reason(self) -> str | None:
        """Why the driver is not available; ``None`` when its module imports."""
        module = self.target.partition(":")[0]
        try:
            importlib.import_module(module)
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.partition(".")[0] not in ("adorm", "demo"):
                return f"{module} needs {exc.name}, which is not installed"
            raise
        return None

    def load(self) -> Driver:
        """An instance of the driver class."""
        return import_object(self.target, "driver class")()

    def profiles(self, driver: Driver) -> dict[str, ConnectionProfile]:
        """The connection profiles ``name``, ``name-small`` and ``name-desk`` on ``driver``."""
        return {
            self.name: CAPTURE.profile(self.name, driver, POOL),
            f"{self.name}-small": CAPTURE.profile(f"{self.name}-small", driver, SMALL_POOL),
            f"{self.name}-desk": CAPTURE.profile(f"{self.name}-desk", driver, SMALL_POOL, desk=True),
        }


DRIVERS: tuple[DemoDriver, ...] = (
    DemoDriver("django", "adorm.drivers.django:DjangoDriver", "Django's ORM on a pool of threads"),
    DemoDriver("asyncpg", "adorm.drivers.asyncpg:AsyncpgDriver", "native PostgreSQL on asyncpg, without threads"),
)


def available() -> list[DemoDriver]:
    """The available drivers, in the order of `DRIVERS`."""
    return [driver for driver in DRIVERS if driver.unavailable_reason() is None]
