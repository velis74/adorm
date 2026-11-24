"""The Django app ``adorm.contrib.django``: configures adorm from the ``ADORM`` setting when Django is set up, and
declares tenant relations on Django models (`TenantRelation`).

Importing this package imports Django.
"""

from adorm.contrib.django.config import (
    configure_from_settings,
    DEFAULT_DRIVER,
    default_profiles,
    ensure_configured,
    settings_configuration,
)
from adorm.contrib.django.tenants import TenantRelation

__all__ = [
    "DEFAULT_DRIVER",
    "TenantRelation",
    "configure_from_settings",
    "default_profiles",
    "ensure_configured",
    "settings_configuration",
]
