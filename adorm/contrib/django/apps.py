"""The app configuration of ``adorm.contrib.django``."""

from collections.abc import Mapping

from django.apps import AppConfig
from django.conf import settings
from django.core.signals import setting_changed

from adorm.contrib.django.config import configure_from_settings

__all__ = ["AdormConfig"]


class AdormConfig(AppConfig):
    """Configures adorm from the Django settings in `ready()` and again whenever a test overrides ``ADORM``, or
    ``DATABASES`` while ``ADORM`` has no ``"profiles"``."""

    name = "adorm.contrib.django"
    label = "adorm"
    verbose_name = "adorm"

    def ready(self) -> None:
        configure_from_settings()
        setting_changed.connect(_reconfigure, dispatch_uid="adorm.contrib.django.reconfigure")


def _reconfigure(*, setting: str, **kwargs: object) -> None:  # noqa: ARG001
    """``setting_changed`` receiver: configures adorm again when the settings it is configured from change."""
    if setting == "ADORM" or (setting == "DATABASES" and _profiles_from_databases()):
        configure_from_settings()


def _profiles_from_databases() -> bool:
    """Whether ``ADORM`` has no ``"profiles"``, so the profiles come from ``DATABASES``; an invalid ``ADORM`` counts."""
    value = getattr(settings, "ADORM", None)
    return not isinstance(value, Mapping) or value.get("profiles") is None
