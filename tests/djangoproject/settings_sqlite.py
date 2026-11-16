"""Django settings for the driver conformance suite on SQLite.

The database is a file in a temporary directory of the process's own, removed when the process ends; pytest-django
creates and drops the test database ``adorm_test.sqlite3`` beside it. Only the demo's schema is installed: the test
library's models use PostgreSQL's array fields.

    uv run pytest --ds=tests.djangoproject.settings_sqlite tests/conformance
"""

import atexit
import shutil
import tempfile

from pathlib import Path

DIRECTORY = Path(tempfile.mkdtemp(prefix="adorm-sqlite-"))
atexit.register(shutil.rmtree, DIRECTORY, ignore_errors=True)

TEST_DATABASE_PATH = DIRECTORY / "adorm_test.sqlite3"

SECRET_KEY = "adorm-test-secret-key"  # noqa: S105 - test fixture, not a credential
DEBUG = False
USE_TZ = True
TIME_ZONE = "UTC"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "demo.backend.clubs",
    "demo.backend.activities",
    "demo.backend.billing",
]

ADORM_EXPOSE = ["auth.User.objects.create_user"]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(DIRECTORY / "adorm.sqlite3"),
        "TEST": {"NAME": str(TEST_DATABASE_PATH)},
    }
}
# The test database under a second alias, with connections of its own.
DATABASES["second"] = {**DATABASES["default"], "TEST": {"MIRROR": "default"}}
