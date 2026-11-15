"""The Django ORM driver: adorm reads executed with Django's synchronous ORM, one thread per connection."""

from __future__ import annotations

import contextvars
import threading

from typing import Any

from adorm.drivers.base import Capabilities, ThreadedConnection, ThreadedDriver
from adorm.drivers.django.translation import translate
from adorm.exceptions import (
    InterfaceError,
    MultipleObjectsReturned,
    NotSupportedError,
    ObjectDoesNotExist,
    ProgrammingError,
    TransactionManagementError,
    ValidationError,
    wrap_driver_error,
)

__all__ = ["CAPABILITIES", "DjangoConnection", "DjangoDriver", "adopt_context_locals", "map_django_error"]

_LOOKUP_NAMES = (
    "exact",
    "iexact",
    "gt",
    "gte",
    "lt",
    "lte",
    "in",
    "range",
    "isnull",
    "contains",
    "icontains",
    "startswith",
    "istartswith",
    "endswith",
    "iendswith",
    "regex",
    "iregex",
)

CAPABILITIES = Capabilities(
    operations={"select", "get", "first", "last", "count", "exists"},
    constructs={f"lookup.{name}" for name in _LOOKUP_NAMES}
    | {
        "path.nested",
        "clause.annotate",
        "clause.distinct",
        "clause.distinct_on",
        "clause.only",
        "clause.defer",
        "clause.select_related",
        "clause.manager",
        "function.Lower",
        "function.Upper",
        "expression.Subquery",
    },
    cancellation=True,
)
"""What `DjangoDriver` supports."""


def map_django_error(exc: BaseException) -> BaseException:
    """The adorm exception for ``exc``, raised by Django or its database library; what has no Django-specific mapping
    goes to `wrap_driver_error`. The result carries ``exc`` as ``__cause__``."""
    from django.core import exceptions as django_exceptions
    from django.db import transaction

    mapped: BaseException
    if isinstance(exc, django_exceptions.ObjectDoesNotExist):
        mapped = ObjectDoesNotExist(*exc.args)
    elif isinstance(exc, django_exceptions.MultipleObjectsReturned):
        mapped = MultipleObjectsReturned(*exc.args)
    elif isinstance(exc, django_exceptions.FieldError | django_exceptions.FieldDoesNotExist):
        mapped = ProgrammingError(str(exc), original=exc)
    elif isinstance(exc, django_exceptions.ValidationError):
        if hasattr(exc, "error_dict"):
            mapped = ValidationError(exc.message_dict)
        elif hasattr(exc, "message"):
            mapped = ValidationError(exc.message, exc.code, exc.params)
        else:
            mapped = ValidationError(exc.messages)
    elif isinstance(exc, transaction.TransactionManagementError):
        mapped = TransactionManagementError(str(exc), original=exc)
    elif isinstance(exc, django_exceptions.SynchronousOnlyOperation):
        mapped = InterfaceError(str(exc), original=exc)
    else:
        return wrap_driver_error(exc)
    mapped.__cause__ = exc
    return mapped


def adopt_context_locals() -> None:
    """Make the values of asgiref ``Local`` objects in the current context visible on the current thread.

    Django keeps its active language and time zone in asgiref ``Local`` objects. asgiref (3.13 and later) tags their
    values with the thread that set them, so in a copy of an event loop task's context run on another thread they
    read as unset; this re-tags each such value with the current thread, as asgiref's ``sync_to_async()`` does. A
    value without the tag (earlier asgiref versions) and other context variables are left alone.
    """
    thread = threading.get_ident()
    for variable, value in contextvars.copy_context().items():
        if variable.name != "asgiref.local" or not hasattr(value, "thread_id") or not hasattr(value, "data"):
            continue
        if value.thread_id != thread:
            variable.set(type(value)(thread, value.data))


class _HookDatabase:
    """A Django backend's DB-API module whose ``connect()`` returns the connection of a profile's ``connect`` hook."""

    def __init__(self, module: Any, connect: Any) -> None:
        self._module = module
        self._connect = connect

    def connect(self, *_args: Any, **_kwargs: Any) -> Any:
        return self._connect()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._module, name)


class DjangoConnection(ThreadedConnection):
    """A Django database connection, the thread's own ``connections[profile.alias]``.

    The alias is a PostgreSQL database (``NotSupportedError`` otherwise). ``profile.params`` replace keys of the
    alias's ``DATABASES`` entry for this connection; ``profile.connect``, when set, returns the DB-API connection that
    Django's wrapper uses instead of opening one (for psycopg 3, opened with the wrapper's
    ``get_connection_params()``), and Django configures it as its own (isolation level, time zone, role).
    ``target`` (what profile hooks receive) is Django's connection wrapper. Every call sees Django's active language
    and time zone of the caller (`adopt_context_locals()`).
    """

    driver: DjangoDriver
    db: Any

    @property
    def target(self) -> Any:
        return self.db

    def prepare_call_sync(self) -> None:
        adopt_context_locals()

    def connect_sync(self) -> None:
        from django.db import connections

        db = connections[self.profile.alias]
        if db.vendor != "postgresql":
            raise NotSupportedError(f"the Django driver supports PostgreSQL databases, not {db.vendor!r}")
        if self.profile.params:
            # The wrapper belongs to this thread; the alias's own settings stay as they are.
            db.settings_dict = {**db.settings_dict, **self.profile.params}
        connect = self.profile.connect
        if connect is not None:
            if db.settings_dict.get("OPTIONS", {}).get("pool"):
                raise NotSupportedError("a profile's connect hook cannot be combined with Django's connection pool")
            db.Database = _HookDatabase(db.Database, lambda: connect(self.profile))
        self.db = db
        db.ensure_connection()

    def execute_sync(self, operation: Any) -> Any:
        kind = operation.kind
        translated = translate(operation.query)
        queryset = translated.queryset
        if kind == "select":
            return translated.rows(queryset)
        if kind == "get":
            return translated.row(queryset.get())
        if kind in ("first", "last"):
            values = queryset.first() if kind == "first" else queryset.last()
            return None if values is None else translated.row(values)
        if kind == "count":
            return queryset.count()
        if kind == "exists":
            return queryset.exists()
        raise NotSupportedError(f"the Django driver does not execute {kind!r} operations")

    def begin_sync(self) -> None:
        self.db.set_autocommit(False)

    def commit_sync(self) -> None:
        self.db.commit()
        self.db.set_autocommit(True)

    def rollback_sync(self) -> None:
        self.db.rollback()
        self.db.set_autocommit(True)

    def cancel_sync(self) -> None:
        # May run before connect_sync() has opened the connection.
        db = getattr(self, "db", None)
        connection = getattr(db, "connection", None)
        if connection is None:
            return
        cancel = getattr(connection, "cancel_safe", None) or connection.cancel
        cancel()

    def close_sync(self) -> None:
        db = getattr(self, "db", None)
        if db is not None:
            db.close()

    def map_error(self, exc: BaseException) -> BaseException:
        return map_django_error(exc)


class DjangoDriver(ThreadedDriver):
    """Executes adorm reads with Django's ORM.

    Each connection runs on a thread of its own with that thread's Django connection for the profile's ``alias``;
    only synchronous ORM methods are called, in a copy of the caller's context (Django's active language included).
    Django must be set up (``django.setup()``) before the first operation.
    """

    name = "django"
    connection_class = DjangoConnection
    capabilities = CAPABILITIES
