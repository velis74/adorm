"""Django settings with only the club schema installed, for generating the models of ``tests.clubmodels``:

uv run adorm generate --settings tests.djangoproject.settings_clubs -o tests/clubmodels --line-length 120
"""

from tests.djangoproject.settings import *  # noqa: F403

ADORM_EXPOSE = []

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.postgres",
    "tests.djangoproject.clubs",
    "tests.djangoproject.activities",
    "tests.djangoproject.billing",
]
