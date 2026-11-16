"""Fixtures of the driver conformance suite: the demo's data set in the test database and every driver over it."""

from __future__ import annotations

import pytest

from adorm import ConnectionProfile
from adorm.executor import Pool, Router, Session, set_runner
from tests.conformance.drivers import DRIVERS


@pytest.fixture(params=[driver.name for driver in DRIVERS])
def conformance_driver(request):
    """The `ConformanceDriver` under test; one that is not available is skipped."""
    driver = next(driver for driver in DRIVERS if driver.name == request.param)
    reason = driver.unavailable_reason()
    if reason is not None:
        pytest.skip(f"the {driver.name} driver is not available: {reason}")
    return driver


@pytest.fixture
async def driver(conformance_driver, demo_data):
    """An instance of the driver under test over the demo's data set."""
    return await conformance_driver.create(demo_data)


@pytest.fixture
async def make_router(driver):
    """``make_router(profile=None, **pool)`` routes the profile ``default`` (built with the keyword arguments in
    ``profile``) to one pool of the driver's slots and makes the router adorm's runner; the routers are closed
    afterwards."""
    routers = []

    def make(*, profile: dict | None = None, **pool) -> Router:
        router = Router([Pool(driver, ConnectionProfile("default", driver, **(profile or {})), **pool)])
        routers.append(router)
        set_runner(router)
        return router

    previous = set_runner(None)
    try:
        yield make
    finally:
        set_runner(previous)
        for router in routers:
            await router.close()


@pytest.fixture
async def router(make_router):
    return make_router(max_connections=2, acquire_timeout=5)


@pytest.fixture
async def pool(router):
    return router.pools[0]


@pytest.fixture
def recorded(monkeypatch):
    """The operations sessions execute from now on, in the order they are sent, in and outside transactions."""
    operations = []
    execute = Session.execute

    async def recording(self, operation):
        operations.append(operation)
        return await execute(self, operation)

    monkeypatch.setattr(Session, "execute", recording)
    return operations


@pytest.fixture
async def blocker(conformance_driver, driver, demo_data):
    """The driver's `Blocker`, released and closed afterwards."""
    blocker = conformance_driver.blocker(driver, demo_data)
    try:
        yield blocker
    finally:
        await blocker.close()


@pytest.fixture
async def notifier(conformance_driver, driver, demo_data):
    """The driver's `Notifier`, closed afterwards."""
    notifier = conformance_driver.notifier(driver, demo_data)
    try:
        yield notifier
    finally:
        await notifier.close()
