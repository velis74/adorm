"""The test suite's Django settings with the ``adorm.contrib.django`` app and an ``ADORM`` setting, for a process
that calls ``django.setup()`` itself. ``ADORM_APP_DATABASE_NAME`` names the database it connects to."""

import os

from tests.djangoproject.settings import *  # noqa: F403
from tests.djangoproject.settings import DATABASES, INSTALLED_APPS

INSTALLED_APPS = [*INSTALLED_APPS, "adorm.contrib.django"]
DATABASES = {"default": {**DATABASES["default"], "NAME": os.environ.get("ADORM_APP_DATABASE_NAME", "adorm_test")}}

ADORM = {
    "profiles": {
        "default": {
            "driver": "adorm.drivers.django.DjangoDriver",
            "on_acquire": "tests.djangoproject.adorm_hooks.on_acquire",
            "on_release": "tests.djangoproject.adorm_hooks:on_release",
        },
    },
    "providers": ["tests.djangoproject.adorm_hooks.reports"],
    "pool": {"max_connections": 1, "acquire_timeout": 0.5},
}
