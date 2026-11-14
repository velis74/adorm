import os

from urllib.parse import quote, urlsplit, urlunsplit

import pytest


@pytest.fixture(scope="session")
def database_url() -> str:
    """URL of the PostgreSQL server the suite runs against."""
    from tests.djangoproject.settings import DEFAULT_DATABASE_URL

    return os.environ.get("ADORM_TEST_DATABASE_URL", DEFAULT_DATABASE_URL)


@pytest.fixture(scope="session")
def test_database_url(database_url, django_db_setup) -> str:  # noqa: ARG001 - needs the test database created
    """``database_url`` pointing at the test database pytest-django created."""
    from django.db import connection

    parts = urlsplit(database_url)
    return urlunsplit(parts._replace(path="/" + quote(connection.settings_dict["NAME"])))
