"""Django settings of the demo project.

The database is the PostgreSQL URL in ``ADORM_DEMO_DATABASE_URL`` (default: database ``adorm_demo`` on
``postgres://postgres@127.0.0.1:5432``). Django owns the schema (``manage.py migrate``).
"""

import os

from demo.database import DEFAULT_DATABASE_NAME, DEFAULT_SERVER_URL, django_database, with_database

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
