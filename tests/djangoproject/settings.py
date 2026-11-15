"""Django settings for the test suite.

The database comes from ``ADORM_TEST_DATABASE_URL``; pytest-django creates and drops the
``adorm_test`` database on that server.
"""

import os

from urllib.parse import parse_qsl, unquote, urlsplit

DEFAULT_DATABASE_URL = "postgres://postgres@127.0.0.1:5432/postgres"
TEST_DATABASE_NAME = "adorm_test"


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
        "NAME": unquote(parts.path.lstrip("/")) or "postgres",
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
]

ADORM_EXPOSE = ["auth.User.objects.create_user"]

DATABASES = {"default": database_from_url(os.environ.get("ADORM_TEST_DATABASE_URL", DEFAULT_DATABASE_URL))}
