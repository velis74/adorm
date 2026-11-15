"""A controllable in-memory driver for executor tests."""

from __future__ import annotations

import asyncio

from collections.abc import Callable, Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from adorm.drivers import Capabilities, Driver, DriverConnection
from adorm.events import Emit

marker: ContextVar[str | None] = ContextVar("executor_fakes_marker", default=None)


@dataclass(frozen=True)
class Profile:
    name: str
    alias: str = "default"
    params: Mapping[str, Any] = field(default_factory=dict)
    connect: Callable[[Any], Any] | None = None


@dataclass(eq=False)
class Op:
    """An operation: ``started`` is set when it starts, it waits for ``gate``, then raises ``error`` or returns
    ``result`` (default: ``name``)."""

    name: str = "op"
    result: Any = None
    error: BaseException | None = None
    gate: asyncio.Event | None = None
    started: asyncio.Event = field(default_factory=asyncio.Event)
    effect: Callable[[FakeConnection], None] | None = None
    kind: str = "op"

    def constructs(self) -> Iterable[str]:
        return ()


class FakeConnection(DriverConnection):
    """``close_gate`` holds closing."""

    def __init__(self, driver: FakeDriver, emit: Emit, number: int) -> None:
        super().__init__(driver)
        self.emit = emit
        self.number = number
        self.calls: list[tuple[str, Any]] = []
        self.cancelled: list[Op] = []
        self.markers: list[str | None] = []
        self.usable = True
        self.closed = False
        self.close_gate: asyncio.Event | None = None
        self.running = 0

    async def _execute(self, operation: Op) -> Any:
        self.calls.append(("execute", operation.name))
        self.markers.append(marker.get())
        driver: FakeDriver = self.driver  # type: ignore[assignment]
        self.running += 1
        driver.running += 1
        driver.peak = max(driver.peak, driver.running)
        try:
            operation.started.set()
            if operation.gate is not None:
                await operation.gate.wait()
            if operation.effect is not None:
                operation.effect(self)
            if operation.error is not None:
                raise operation.error
            return operation.name if operation.result is None else operation.result
        except asyncio.CancelledError:
            self.cancelled.append(operation)
            raise
        finally:
            self.running -= 1
            driver.running -= 1

    async def begin(self) -> None:
        self.calls.append(("begin", None))

    async def commit(self) -> None:
        self.calls.append(("commit", None))

    async def rollback(self) -> None:
        self.calls.append(("rollback", None))

    async def _savepoint(self, name: str) -> None:
        self.calls.append(("savepoint", name))

    def cancel(self) -> None:
        pass

    def is_usable(self) -> bool:
        return self.usable and not self.closed

    async def close(self) -> None:
        if self.close_gate is not None:
            await self.close_gate.wait()
        self.closed = True


class FakeDriver(Driver):
    """``open_gate`` holds connection opening; ``open_errors`` are raised by the next openings, in order;
    ``attempts`` counts the openings."""

    name = "fake"
    mode = "async"
    capabilities = Capabilities(operations={"op"}, savepoints=True)

    def __init__(self) -> None:
        self.connections: list[FakeConnection] = []
        self.open_gate: asyncio.Event | None = None
        self.opening = asyncio.Event()
        self.open_errors: list[BaseException] = []
        self.running = 0
        self.peak = 0
        self.attempts = 0

    async def open_connection(self, profile: Any, emit: Emit) -> FakeConnection:  # noqa: ARG002
        self.attempts += 1
        self.opening.set()
        if self.open_gate is not None:
            await self.open_gate.wait()
        if self.open_errors:
            raise self.open_errors.pop(0)
        connection = FakeConnection(self, emit, len(self.connections) + 1)
        self.connections.append(connection)
        return connection
