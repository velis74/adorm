"""Every driver under nested atomic blocks and connection options: what it declares works, what it does not
declare is refused with `NotSupportedError` and leaves the transaction, the connection and the pool usable."""

import pytest

import adorm

from adorm import ConnectionProfile, IntegrityError, NotSupportedError
from adorm.operations import Count
from adorm.query import Query
from demo.models.clubs import Club

pytestmark = pytest.mark.django_db(transaction=True)

REPEAT = range(3)

OPTIONS: dict[str, object] = {"statement_timeout": 5, "lock_timeout": 2, "idle_in_transaction_session_timeout": 60}
"""A value for every connection option a demo driver declares; `test_set_options` sets each one."""

UNDECLARED_OPTION = ("work_mem", "1MB")


class RolledBackError(Exception):
    """Raised to roll a block back."""


async def test_nested_atomic_blocks(driver, pool):
    for n in REPEAT:
        if driver.capabilities.savepoints:
            await _savepoints_commit_and_roll_back(f"Koper {n}")
        else:
            await _savepoints_are_refused(driver)
        assert (pool.size, pool.in_use) == (1, 0)


async def _savepoints_commit_and_roll_back(city):
    koper = Club.objects.filter(slug="koper-sailing")
    async with adorm.atomic():
        async with adorm.atomic():
            assert await koper.update(city=city) == 1
        with pytest.raises(RolledBackError):
            async with adorm.atomic():
                assert await koper.update(city="Lost") == 1
                assert await Club.objects.filter(city="Lost").count() == 1
                raise RolledBackError
        assert await Club.objects.filter(city=city).count() == 1
    assert await Club.objects.filter(city=city).count() == 1
    assert await Club.objects.filter(city="Lost").count() == 0


async def test_a_database_error_inside_a_savepoint_leaves_the_transaction_usable(driver, pool):
    if not driver.capabilities.savepoints or "create" not in driver.capabilities.operations:
        pytest.skip(f"the {driver.name} driver does not declare savepoints and create")
    koper = Club.objects.filter(slug="koper-sailing")
    for n in REPEAT:
        existing = await koper.get()
        async with adorm.atomic():
            assert await koper.update(city=f"Before {n}") == 1
            with pytest.raises(IntegrityError):
                async with adorm.atomic():
                    await Club.objects.create(
                        pk=existing.pk, name="Twin", slug="twin", founded=existing.founded, city="Piran"
                    )
            assert await koper.update(name=f"Koper {n}") == 1
        stored = await koper.get()
        assert (stored.city, stored.name) == (f"Before {n}", f"Koper {n}")
        assert await Club.objects.filter(slug="twin").count() == 0
        assert (pool.size, pool.in_use) == (1, 0)


async def _savepoints_are_refused(driver):
    async with adorm.atomic():
        assert await Club.objects.count() == 3
        with pytest.raises(NotSupportedError) as raised:
            async with adorm.atomic():
                pass
        assert raised.value.driver == driver.name
        assert raised.value.unsupported == ("savepoints",)
        assert await Club.objects.filter(city="Koper").count() == 1
    assert await Club.objects.count() == 3


@pytest.mark.parametrize("option", sorted(OPTIONS))
async def test_set_options(driver, option):
    if option not in driver.capabilities.connection_options:
        pytest.skip(f"the {driver.name} driver does not declare {option}")
    connection = await driver.open_connection(ConnectionProfile("default", driver), lambda _event: None)
    try:
        await connection.set_options({option: OPTIONS[option]}, local=False)
        assert await connection.execute(Count(Query("clubs.Club"))) == 3
    finally:
        await connection.close()


async def test_an_undeclared_connection_option_is_refused(driver):
    name, value = UNDECLARED_OPTION
    assert name not in driver.capabilities.connection_options
    connection = await driver.open_connection(ConnectionProfile("default", driver), lambda _event: None)
    try:
        for local in (False, True):
            with pytest.raises(NotSupportedError) as raised:
                await connection.set_options({name: value}, local=local)
            assert raised.value.driver == driver.name
            assert raised.value.unsupported == (name,)
            assert connection.is_usable()
            assert await connection.execute(Count(Query("clubs.Club"))) == 3
    finally:
        await connection.close()


def test_every_declared_connection_option_has_a_value():
    from demo.api.drivers import DRIVERS

    declared = set().union(*(driver.driver_class().capabilities.connection_options for driver in DRIVERS))
    assert declared <= set(OPTIONS)
