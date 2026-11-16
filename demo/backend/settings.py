"""Django settings of the demo project.

The database is the PostgreSQL URL in ``ADORM_DEMO_DATABASE_URL``, from the environment or the file ``.env`` in the
repository root (default: database ``adorm_demo`` on ``postgres://postgres@127.0.0.1:5432``). Django owns the
schema (``manage.py migrate``).
"""

import os

from pathlib import Path

from dotenv import load_dotenv

from demo.database import DEFAULT_DATABASE_NAME, DEFAULT_SERVER_URL, django_database, with_database

load_dotenv(Path(__file__).parents[2] / ".env")

SECRET_KEY = "adorm-demo-secret-key"  # noqa: S105 - demo project, not a credential
DEBUG = False
USE_TZ = True
TIME_ZONE = "UTC"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.postgres",
    "demo.backend.clubs",
    "demo.backend.activities",
    "demo.backend.billing",
]

DATABASES = {
    "default": django_database(
        os.environ.get("ADORM_DEMO_DATABASE_URL") or with_database(DEFAULT_SERVER_URL, DEFAULT_DATABASE_NAME)
    )
}
