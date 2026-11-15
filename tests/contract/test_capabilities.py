"""Capabilities, connection error classification and the Driver base class."""

import asyncio

from dataclasses import FrozenInstanceError

import pytest

from adorm.drivers import Capabilities, Driver, DriverConnection, is_connection_error, Operation, Profile
from adorm.exceptions import (
    DatabaseError,
    IntegrityError,
    InterfaceError,
    NotSupportedError,
    OperationalError,
    ProgrammingError,
)
from tests.contract.drivers import Profile as SimpleProfile, RawQuery


class Op:
    def __init__(self, kind, *constructs):
        self.kind = kind
        self._constructs = constructs

    def constructs(self):
        return iter(self._constructs)


CAPS = Capabilities(
    operations=["select", "update"],
    constructs=("lookup.exact", "function.Lower"),
    connection_options={"statement_timeout"},
)


def test_names_become_frozensets():
    assert CAPS.operations == frozenset({"select", "update"})
    assert isinstance(CAPS.constructs, frozenset)
    assert CAPS.backend_events == frozenset()
    # A driver declares only what it implements.
    assert CAPS.savepoints is False
    assert CAPS.cancellation is False


def test_capabilities_are_immutable_and_hashable():
    with pytest.raises(FrozenInstanceError):
        CAPS.operations = frozenset()
    assert hash(CAPS) == hash(Capabilities(**{f: getattr(CAPS, f) for f in CAPS.__dataclass_fields__}))


def test_a_string_is_not_a_set_of_names():
    with pytest.raises(TypeError, match="operations"):
        Capabilities(operations="select")


def test_supported_operation_passes():
    CAPS.check(Op("select", "lookup.exact", "function.Lower"))
    assert CAPS.unsupported(Op("update")) == []


def test_unsupported_lists_kind_then_sorted_constructs():
    op = Op("delete", "lookup.icontains", "aggregate.Count", "lookup.exact")
    assert CAPS.unsupported(op) == ["delete", "aggregate.Count", "lookup.icontains"]
    with pytest.raises(NotSupportedError, match=r"'delete' operation needs unsupported delete, aggregate.Count"):
        CAPS.check(op)


def test_unsupported_construct_of_supported_kind():
    with pytest.raises(NotSupportedError, match="clause.select_for_update"):
        CAPS.check(Op("select", "clause.select_for_update"))


def test_not_supported_is_a_database_error():
    assert issubclass(NotSupportedError, DatabaseError)
    error = NotSupportedError("no")
    assert (error.driver, error.unsupported, error.sqlstate, error.original) == (None, (), None, None)


def test_a_failed_check_names_the_driver_and_what_it_lacks():
    with pytest.raises(
        NotSupportedError, match=r"needs unsupported delete, aggregate.Count \(driver 'fake'\)$"
    ) as raised:
        CAPS.check(Op("delete", "aggregate.Count", "lookup.exact"), "fake")
    assert (raised.value.driver, raised.value.unsupported) == ("fake", ("delete", "aggregate.Count"))
    with pytest.raises(NotSupportedError, match=r"^savepoints are not supported \(driver 'fake'\)$") as raised:
        CAPS.check_savepoints("fake")
    assert raised.value.unsupported == ("savepoints",)
    with pytest.raises(NotSupportedError, match="work_mem \\(driver 'fake'\\)") as raised:
        CAPS.check_options({"work_mem": "1MB"}, "fake")
    assert raised.value.unsupported == ("work_mem",)
    with pytest.raises(NotSupportedError) as raised:
        CAPS.check(Op("delete"))
    assert raised.value.driver is None


def test_check_options():
    CAPS.check_options({"statement_timeout": 5})
    CAPS.check_options([])
    with pytest.raises(NotSupportedError, match="unsupported connection options: lock_timeout, work_mem"):
        CAPS.check_options({"work_mem": "1MB", "lock_timeout": 1, "statement_timeout": 1})


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (InterfaceError("closed"), True),
        (OperationalError("lost"), True),
        (OperationalError(sqlstate="08006"), True),
        (OperationalError(sqlstate="08001"), True),
        (OperationalError(sqlstate="57P01"), True),
        (OperationalError(sqlstate="57P02"), True),
        (OperationalError(sqlstate="57P03"), True),
        (OperationalError(sqlstate="57014"), False),
        (OperationalError(sqlstate="40001"), False),
        (OperationalError(sqlstate="40P01"), False),
        (IntegrityError(sqlstate="23505"), False),
        (ProgrammingError(sqlstate="42P01"), False),
        (DatabaseError("unknown"), False),
        (asyncio.CancelledError(), False),
        (KeyboardInterrupt(), True),
    ],
)
def test_is_connection_error(exc, expected):
    assert is_connection_error(exc) is expected


def test_driver_mode_is_validated():
    with pytest.raises(TypeError, match="mode must be one of"):

        class Bad(Driver):
            name = "bad"
            mode = "greenlet"


def test_driver_and_connection_are_abstract():
    with pytest.raises(TypeError):
        Driver()
    with pytest.raises(TypeError):
        DriverConnection(None)


def test_protocols_are_structural():
    assert isinstance(RawQuery("SELECT 1"), Operation)
    assert isinstance(SimpleProfile("default"), Profile)
    assert not isinstance(object(), Operation)


def test_profile_has_a_connect_hook():
    class WithoutConnect:
        name = "default"
        alias = "default"
        params = {}

    assert not isinstance(WithoutConnect(), Profile)
    assert SimpleProfile("default").connect is None


def test_check_savepoints():
    Capabilities(savepoints=True).check_savepoints()
    with pytest.raises(NotSupportedError, match="savepoints"):
        CAPS.check_savepoints()


class BareConnection(DriverConnection):
    """An async connection that does no checks of its own; every hook records that it was reached."""

    def __init__(self, driver):
        super().__init__(driver)
        self.reached = []

    async def _execute(self, operation):
        self.reached.append(("execute", operation.kind))
        return "executed"

    async def _set_options(self, options, local):
        self.reached.append(("options", options, local))

    async def _savepoint(self, name):
        self.reached.append(("savepoint", name))

    async def _release_savepoint(self, name):
        self.reached.append(("release", name))

    async def _rollback_to_savepoint(self, name):
        self.reached.append(("rollback_to", name))

    async def begin(self):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass

    def cancel(self):
        pass

    def is_usable(self):
        return True

    async def close(self):
        pass


class BareDriver(Driver):
    name = "bare"
    mode = "async"

    def __init__(self, capabilities):
        self.capabilities = capabilities

    async def open_connection(self, profile, emit):  # noqa: ARG002
        return BareConnection(self)


async def test_async_connection_refuses_unsupported_calls_before_the_target():
    conn = await BareDriver(CAPS).open_connection(SimpleProfile("default"), print)
    with pytest.raises(NotSupportedError, match="lookup.icontains"):
        await conn.execute(Op("select", "lookup.icontains"))
    with pytest.raises(NotSupportedError, match="work_mem"):
        await conn.set_options({"work_mem": "1MB"}, local=False)
    for call in (conn.savepoint("s"), conn.release_savepoint("s"), conn.rollback_to_savepoint("s")):
        with pytest.raises(NotSupportedError, match="savepoints"):
            await call
    assert conn.reached == []


async def test_async_connection_passes_supported_calls_to_the_target():
    caps = Capabilities(operations={"select"}, connection_options={"statement_timeout"}, savepoints=True)
    conn = await BareDriver(caps).open_connection(SimpleProfile("default"), print)
    assert await conn.execute(Op("select")) == "executed"
    await conn.set_options({"statement_timeout": 1}, local=True)
    await conn.savepoint("s")
    await conn.release_savepoint("s")
    await conn.rollback_to_savepoint("s")
    assert conn.reached == [
        ("execute", "select"),
        ("options", {"statement_timeout": 1}, True),
        ("savepoint", "s"),
        ("release", "s"),
        ("rollback_to", "s"),
    ]


async def test_default_hooks_refuse_savepoints_and_options_declared_but_not_implemented():
    class Minimal(BareConnection):
        _savepoint = DriverConnection._savepoint
        _set_options = DriverConnection._set_options

    caps = Capabilities(connection_options={"statement_timeout"}, savepoints=True)
    conn = Minimal(BareDriver(caps))
    with pytest.raises(NotSupportedError, match="savepoints"):
        await conn.savepoint("s")
    with pytest.raises(NotSupportedError, match="statement_timeout"):
        await conn.set_options({"statement_timeout": 1}, local=False)
    await conn.set_options({}, local=False)


def test_is_connection_lost_defaults_to_is_connection_error():
    conn = BareConnection(BareDriver(CAPS))
    assert conn.is_connection_lost(OperationalError("no sqlstate")) is True
    assert conn.is_connection_lost(OperationalError(sqlstate="57014")) is False


def test_map_error_defaults_to_wrap_driver_error():
    error = Exception("x")
    error.sqlstate = "23505"
    mapped = BareConnection(BareDriver(CAPS)).map_error(error)
    assert isinstance(mapped, IntegrityError)
    assert mapped.original is error


async def test_driver_check_runs_before_the_connection_and_before_a_slot():
    from adorm import ConnectionProfile, create_session
    from adorm.executor import Pool, Router

    driver = BareDriver(CAPS)
    with pytest.raises(NotSupportedError, match=r"lookup.icontains \(driver 'bare'\)") as raised:
        driver.check(Op("select", "lookup.icontains"))
    assert raised.value.driver == "bare"
    driver.check(Op("select", "lookup.exact"))
    router = Router([Pool(driver, ConnectionProfile("default", driver), max_connections=1)])
    try:
        with pytest.raises(NotSupportedError, match="'delete' operation"):
            await router.run(create_session(), Op("delete"))
        assert router.pools[0].size == 0
        assert await router.run(create_session(), Op("select")) == "executed"
        assert router.pools[0].size == 1
    finally:
        await router.close()
