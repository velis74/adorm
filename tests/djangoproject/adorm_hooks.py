"""Hooks and a profile provider that the ``ADORM`` setting of `tests.djangoproject.adorm_app_settings` names by
import path. The module imports Django models, so it can only be imported once Django's apps are ready."""

from adorm import ConnectionProfile
from tests.djangoproject.library.models import Tag

calls: list[tuple[str, str, str]] = []
"""``(hook, profile name, session name)`` of every hook call."""

MODEL = Tag


def on_acquire(connection, profile, session):  # noqa: ARG001
    calls.append(("on_acquire", profile.name, session.session_name))


def on_release(connection, profile, session):  # noqa: ARG001
    calls.append(("on_release", profile.name, session.session_name))


def reports(name):
    if name != "reports":
        return None
    return ConnectionProfile(name, "adorm.drivers.django.DjangoDriver", on_acquire=on_acquire)
