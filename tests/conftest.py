import os

from urllib.parse import quote, urlsplit, urlunsplit

import pytest


@pytest.fixture(scope="session")
def database_url() -> str:
    """URL of the PostgreSQL server the suite runs against."""
    from tests.djangoproject.settings import DEFAULT_DATABASE_URL

    return os.environ.get("ADORM_TEST_DATABASE_URL", DEFAULT_DATABASE_URL)


def pytest_collection_modifyitems(items):
    # pytest-django creates the test database only for tests marked django_db.
    for item in items:
        if "test_database_url" in getattr(item, "fixturenames", ()) and not item.get_closest_marker("django_db"):
            item.add_marker(pytest.mark.django_db(transaction=True))


@pytest.fixture(scope="session")
def test_database_url(database_url, django_db_setup) -> str:  # noqa: ARG001 - needs the test database created
    """``database_url`` pointing at the test database pytest-django created."""
    from django.db import connection

    from tests.djangoproject.settings import TEST_DATABASE_NAME

    name = connection.settings_dict["NAME"]
    if not name.startswith(TEST_DATABASE_NAME):
        pytest.fail(f"the test database was not created; Django points at {name!r}")
    parts = urlsplit(database_url)
    return urlunsplit(parts._replace(path="/" + quote(name)))
