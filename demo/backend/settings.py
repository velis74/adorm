"""Django settings of the demo project.

The database is the PostgreSQL URL in ``ADORM_DEMO_DATABASE_URL``, from the environment or the file ``.env`` in the
repository root. Django owns the schema (``manage.py migrate``).
"""

import os

from pathlib import Path

from dotenv import load_dotenv

from demo.database import DATABASE_ENV, django_database

load_dotenv(Path(__file__).parents[2] / ".env")

SECRET_KEY = "adorm-demo-secret-key"  # noqa: S105 - demo project, not a credential
DEBUG = False
USE_TZ = True
TIME_ZONE = "UTC"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = ["demo.backend.library"]

DATABASES = {"default": django_database(os.environ.get(DATABASE_ENV, ""))}
