"""The demo refuses a superuser: its tests that need a database skip when the test database's user is one."""

import psycopg
import pytest

from demo.database import MAINTENANCE_DATABASE, with_database
from tests.djangoproject.settings import TEST_DATABASE_URL


@pytest.fixture(scope="session")
def demo_role():
    """Skips the test when the user of ``ADORM_TEST_DATABASE_URL`` is a superuser."""
    with psycopg.connect(with_database(TEST_DATABASE_URL, MAINTENANCE_DATABASE)) as conn:
        superuser = conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[0]
    if superuser:
        pytest.skip("the demo refuses a superuser")
