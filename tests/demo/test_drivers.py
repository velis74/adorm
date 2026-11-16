"""The demo's drivers cover adorm's bundled drivers, and the capture records the SQL of each."""

import pytest

from adorm import PoolSettings
from adorm.settings import import_object
from demo.api.capture import Capture
from demo.api.drivers import DRIVERS
from tests.bundled_drivers import bundled_driver_classes


def test_the_demo_runs_every_bundled_driver():
    assert bundled_driver_classes() <= {import_object(driver.target, "driver class") for driver in DRIVERS}


@pytest.mark.parametrize("driver", DRIVERS, ids=[driver.name for driver in DRIVERS])
def test_the_capture_records_every_demo_driver(driver):
    profile = Capture().profile(driver.name, driver.load(), PoolSettings(max_connections=1))
    assert profile.on_acquire is not None


def test_the_capture_refuses_a_driver_it_cannot_record():
    class Other:
        name = "other"

    with pytest.raises(NotImplementedError, match="'other'"):
        Capture().profile("other", Other(), PoolSettings(max_connections=1))
