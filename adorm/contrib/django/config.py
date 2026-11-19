"""adorm's configuration from the Django settings ``ADORM`` and ``DATABASES``."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

import adorm

from adorm.exceptions import NotSupportedError
from adorm.settings import ConnectionProfile, import_object, PoolSettings

__all__ = [
    "DEFAULT_DRIVER",
    "configure_from_settings",
    "default_profiles",
    "ensure_configured",
    "settings_configuration",
]

DEFAULT_DRIVER = "adorm.drivers.django.DjangoDriver"
"""The driver of the profiles `default_profiles()` builds."""

_KEYS = ("profiles", "providers", "pool", "debug")
_HOOKS = ("connect", "on_acquire", "on_release", "content_types_key")
_ERRORS = (TypeError, ValueError, ImportError, NotSupportedError)


def default_profiles() -> dict[str, dict[str, Any]]:
    """One profile per alias of ``DATABASES``, named after the alias, on the Django driver."""
    return {alias: {"driver": DEFAULT_DRIVER, "alias": alias} for alias in settings.DATABASES}


def _import(path: str, where: str, what: str) -> Any:
    try:
        return import_object(path, what)
    except Exception as exc:
        raise ImproperlyConfigured(f"{where}: {exc}") from exc


def _callable(value: Any, where: str, what: str) -> Any:
    if isinstance(value, str):
        value = _import(value, where, what)
    if not callable(value):
        raise ImproperlyConfigured(f"{where}: a {what} is a callable or its import path, not {value!r}")
    return value


def _profile(name: Any, value: Any) -> ConnectionProfile:
    where = f"ADORM['profiles'][{name!r}]"
    if isinstance(value, Mapping):
        value = dict(value)
        for hook in _HOOKS:
            if value.get(hook) is not None:
                value[hook] = _callable(value[hook], f"{where}[{hook!r}]", "hook")
    try:
        profile = ConnectionProfile.coerce(name, value)
    except _ERRORS as exc:
        raise ImproperlyConfigured(f"{where}: {exc}") from exc
    if isinstance(profile.driver, str):
        # Checks the hooks and options against the driver class; the profile keeps the path.
        driver = _import(profile.driver, f"{where}['driver']", "driver")
        try:
            profile.replace(driver=driver)
        except _ERRORS as exc:
            raise ImproperlyConfigured(f"{where}: {exc}") from exc
    return profile


def settings_configuration() -> dict[str, Any]:
    """The keyword arguments of `adorm.configure()` that the Django settings describe.

    ``ADORM`` is a mapping with the keys of `adorm.configure()`; a missing or ``None`` setting is an empty mapping.
    Without ``"profiles"`` the profiles are `default_profiles()`. Hooks of a profile mapping and providers may be
    import paths, imported here. Every profile and the pool settings are built and checked, a profile's driver given
    by import path imported for the check; an invalid value raises `ImproperlyConfigured` naming where it is.
    """
    value = getattr(settings, "ADORM", None)
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise ImproperlyConfigured(f"ADORM is a mapping, not {type(value).__name__}")
    unknown = sorted(str(key) for key in value if key not in _KEYS)
    if unknown:
        raise ImproperlyConfigured(f"ADORM has unknown keys {unknown}; the keys are {list(_KEYS)}")
    profiles = value.get("profiles")
    if profiles is None:
        profiles = default_profiles()
    if not isinstance(profiles, Mapping):
        raise ImproperlyConfigured(
            f"ADORM['profiles'] is a mapping of names to profiles, not {type(profiles).__name__}"
        )
    providers = value.get("providers", ())
    if isinstance(providers, (str, Mapping)) or not hasattr(providers, "__iter__"):
        raise ImproperlyConfigured(f"ADORM['providers'] is a list of providers, not {type(providers).__name__}")
    pool = value.get("pool")
    if pool is not None:
        try:
            pool = PoolSettings.coerce(pool)
        except _ERRORS as exc:
            raise ImproperlyConfigured(f"ADORM['pool']: {exc}") from exc
    debug = value.get("debug")
    if debug is not None and not isinstance(debug, bool):
        raise ImproperlyConfigured(f"ADORM['debug'] is a bool or None, not {debug!r}")
    return {
        "profiles": {name: _profile(name, profile) for name, profile in profiles.items()},
        "providers": [
            _callable(provider, f"ADORM['providers'][{index}]", "profile provider")
            for index, provider in enumerate(providers)
        ],
        "pool": pool,
        "debug": debug,
    }


def configure_from_settings() -> None:
    """Configure adorm from the Django settings (`settings_configuration()`), replacing its whole configuration as
    `adorm.configure()` does. Raises `ImproperlyConfigured` and then changes nothing."""
    configuration = settings_configuration()
    try:
        adorm.configure(**configuration)
    except _ERRORS as exc:
        raise ImproperlyConfigured(f"ADORM: {exc}") from exc


def ensure_configured() -> None:
    """Configure adorm from the Django settings (`configure_from_settings()`) unless the app ``adorm.contrib.django``
    is installed, which has configured it when Django was set up. Needs a set-up Django."""
    from django.apps import apps

    if not apps.is_installed("adorm.contrib.django"):
        configure_from_settings()
