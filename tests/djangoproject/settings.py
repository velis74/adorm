"""Django settings for the test suite.

The test database is ``ADORM_TEST_DATABASE_URL``, from the environment or the file ``.env`` in the repository root:
pytest-django creates and drops the database the URL names on that server (``adorm_test`` when the URL names none or
``postgres``; ``ADORM_TEST_DATABASE_NAME`` overrides it). Django connects to the server's ``postgres`` database only
to create and drop it. The alias ``second`` is the same test database.
"""

import os

from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[2] / ".env")

DEFAULT_DATABASE_URL = "postgres://postgres@127.0.0.1:5432/adorm_test"
MAINTENANCE_DATABASES = ("", "postgres", "template0", "template1")


def test_database_name(url: str) -> str:
    """The name of the test database: ``ADORM_TEST_DATABASE_NAME``, else the database ``url`` names, else
    ``adorm_test``; never one of the server's maintenance databases."""
    name = os.environ.get("ADORM_TEST_DATABASE_NAME") or unquote(urlsplit(url).path.lstrip("/"))
    return "adorm_test" if name in MAINTENANCE_DATABASES else name


TEST_DATABASE_URL = os.environ.get("ADORM_TEST_DATABASE_URL", DEFAULT_DATABASE_URL)
TEST_DATABASE_NAME = test_database_name(TEST_DATABASE_URL)


def database_from_url(url: str) -> dict:
    """Return a Django ``DATABASES`` entry for a ``postgres://`` or ``postgresql://`` URL.

    Query parameters ``host`` and ``port`` fill ``HOST`` and ``PORT`` when the URL authority lacks them; other
    query parameters are libpq keywords and go to ``OPTIONS``.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("postgres", "postgresql"):
        raise ValueError(f"Unsupported database URL scheme: {parts.scheme!r}")
    options = dict(parse_qsl(parts.query))
    host = options.pop("host", "")
    port = options.pop("port", "")
    entry = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "postgres",
        "USER": unquote(parts.username or ""),
        "PASSWORD": unquote(parts.password or ""),
        "HOST": parts.hostname or host,
        "PORT": str(parts.port or port),
        "TEST": {"NAME": TEST_DATABASE_NAME},
    }
    if options:
        entry["OPTIONS"] = options
    return entry


SECRET_KEY = "adorm-test-secret-key"  # noqa: S105 - test fixture, not a credential
DEBUG = False
USE_TZ = True
TIME_ZONE = "UTC"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.postgres",
    "tests.djangoproject.library",
    # The club schema, for the driver conformance suite.
    "tests.djangoproject.clubs",
    "tests.djangoproject.activities",
    "tests.djangoproject.billing",
]

ADORM_EXPOSE = ["auth.User.objects.create_user"]

DATABASES = {"default": database_from_url(TEST_DATABASE_URL)}
# The test database under a second alias, with connections of its own.
DATABASES["second"] = {**DATABASES["default"], "TEST": {"MIRROR": "default"}}
