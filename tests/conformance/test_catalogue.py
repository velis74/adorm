"""Every driver runs the conformance catalogue: what it declares returns the catalogue's results, what it does not
declare is refused with `NotSupportedError` before it reaches a connection."""

import re

import pytest

from django.db import connection

import adorm

from adorm.operations import OPERATIONS
from adorm.query import construct_names, Node
from tests.conformance.cases import CASES
from tests.conformance.drivers import DRIVERS
from tests.library_data import in_thread

pytestmark = pytest.mark.django_db(transaction=True)

# A call needs a driver method the registered model declares; tests/test_driver_calls.py covers it.
UNCATALOGUED_KINDS = frozenset({"call"})


def names_of(operation) -> set[str]:
    return {operation.kind, *operation.constructs()}


def declared(driver) -> frozenset[str]:
    return driver.capabilities.operations | driver.capabilities.constructs


def adorm_constructs() -> set[str]:
    """The construct names of adorm's own node classes, without those the test suite defines."""
    names = set(construct_names())
    pending = [Node]
    while pending:
        cls = pending.pop()
        pending.extend(cls.__subclasses__())
        if cls.construct and not cls.__module__.startswith("adorm."):
            names.discard(cls.construct)
    return names


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
async def test_case(driver, router, recorded, case):
    missing = case.uses - declared(driver)
    pool = router.pools[0]
    refusal = case.refusals.get(driver.name)
    if not missing:
        try:
            result = await case.run()
        except adorm.NotSupportedError as exc:
            if refusal is None or not re.search(refusal, str(exc)):
                raise
        else:
            assert result == case.expected_on(connection.vendor)
            assert set().union(*map(names_of, recorded)) == case.uses
    else:
        with pytest.raises(adorm.NotSupportedError) as raised:
            await case.run()
        refused = recorded[-1]
        assert raised.value.driver == driver.name
        assert raised.value.unsupported
        assert set(raised.value.unsupported) == names_of(refused) - declared(driver)
        assert set(raised.value.unsupported) <= missing
        assert all(name in str(raised.value) for name in raised.value.unsupported)
        assert set().union(*map(names_of, recorded)) <= case.uses
    assert pool.in_use == 0
    # The pool still serves the next operation, on a slot that was not given up.
    size = pool.size
    assert await adorm.create_session().execute(_count_clubs()) == 3
    assert pool.size == max(size, 1)


MIRRORED = [case for case in CASES if case.django is not None]


@pytest.mark.parametrize("case", MIRRORED, ids=[case.id for case in MIRRORED])
@pytest.mark.usefixtures("demo_data")
async def test_django_mirror(case):
    """The Django ORM returns the case's ``expected`` over the seeded database; the changes are rolled back."""

    def mirror():
        from django.db import transaction

        with transaction.atomic():
            result = case.django()
            transaction.set_rollback(True)
        return result

    from django.db import DatabaseError

    try:
        result = await in_thread(mirror)
    except DatabaseError as exc:
        # Django refuses the mirror on this database before it runs (a JSON contains, a sliced compound query).
        if exc.__cause__ is not None or connection.vendor == "postgresql":
            raise
        pytest.skip(f"Django refuses the mirror on {connection.vendor}: {exc}")
    assert result == case.expected_on(connection.vendor)


def test_every_case_completes_on_a_driver_or_in_its_django_mirror():
    from demo.api.drivers import DRIVERS as DEMO_DRIVERS

    capabilities = {driver.name: driver.driver_class().capabilities for driver in DEMO_DRIVERS}
    unchecked = [
        case.id
        for case in CASES
        if case.django is None
        and not any(
            case.uses <= caps.operations | caps.constructs and name not in case.refusals
            for name, caps in capabilities.items()
        )
    ]
    assert unchecked == []


def _count_clubs():
    from adorm.operations import Count
    from adorm.query import Query

    return Count(Query("clubs.Club"))


def test_case_ids_are_unique():
    ids = [case.id for case in CASES]
    assert len(ids) == len(set(ids))


def test_cases_name_known_operations_and_constructs():
    known = set(OPERATIONS) | construct_names()
    assert set().union(*(case.uses for case in CASES)) <= known


def test_every_operation_kind_and_construct_has_a_case():
    covered = set().union(*(case.uses for case in CASES))
    assert (set(OPERATIONS) - UNCATALOGUED_KINDS) - covered == set()
    assert adorm_constructs() - covered == set()


@pytest.mark.parametrize("conformance", DRIVERS, ids=[driver.name for driver in DRIVERS])
def test_every_driver_declares_only_known_names_and_each_has_a_case(conformance):
    if conformance.unavailable_reason() is not None:
        pytest.skip(conformance.unavailable_reason())
    from demo.api.drivers import get

    capabilities = get(conformance.name).driver_class().capabilities
    assert capabilities.operations <= set(OPERATIONS)
    assert capabilities.constructs <= construct_names()
    covered = set().union(*(case.uses for case in CASES))
    assert (capabilities.operations - UNCATALOGUED_KINDS) - covered == set()
    assert capabilities.constructs - covered == set()


def test_the_suite_runs_every_demo_driver():
    from demo.api.drivers import DRIVERS as DEMO_DRIVERS

    assert [driver.name for driver in DRIVERS] == [driver.name for driver in DEMO_DRIVERS]
