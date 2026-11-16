"""The Django app ``adorm.contrib.django``: configures adorm from the ``ADORM`` setting when Django is set up.

Importing this package imports Django.
"""

from adorm.contrib.django.config import (
    configure_from_settings,
    DEFAULT_DRIVER,
    default_profiles,
    settings_configuration,
)

__all__ = ["DEFAULT_DRIVER", "configure_from_settings", "default_profiles", "settings_configuration"]
