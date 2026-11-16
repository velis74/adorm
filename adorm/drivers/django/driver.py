"""The Django ORM driver: adorm operations executed with Django's synchronous ORM, one thread per connection."""

from __future__ import annotations

import contextlib
import contextvars
import logging
import sys
import threading

from collections.abc import Callable, Iterator
from typing import Any

from adorm.drivers.base import Capabilities, ThreadedConnection, ThreadedDriver
from adorm.drivers.django.translation import (
    AGGREGATES,
    aggregation,
    bulk_create_options,
    check_bulk_create,
    check_in_bulk_field,
    child_instance,
    clean_exclude,
    clean_instance,
    clean_needs_row,
    column_fields,
    django_model,
    field_kwargs,
    FUNCTIONS,
    instance_row,
    lookup_kwargs,
    model_queryset,
    operation_model,
    parent_link_field,
    pinned_alias,
    table_model,
    translate,
    validation_error,
)
from adorm.drivers.postgresql import integrity_details, references_key
from adorm.drivers.sql import POSTGRESQL_OPTIONS, set_options_sql
from adorm.exceptions import (
    FieldError,
    InterfaceError,
    MultipleObjectsReturned,
    NotSupportedError,
    ObjectDoesNotExist,
    ProgrammingError,
    ProtectedError,
    RestrictedError,
    TransactionManagementError,
    wrap_driver_error,
)
from adorm.operations import ModelRow

__all__ = [
    "CAPABILITIES",
    "READ_CAPABILITIES",
    "DjangoConnection",
    "DjangoDriver",
    "adopt_context_locals",
    "map_django_error",
    "model_row",
]

logger = logging.getLogger("adorm.drivers.django")

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
    "overlap",
)

READ_CAPABILITIES = Capabilities(
    operations={"select", "get", "first", "last", "count", "exists", "aggregate", "in_bulk"},
    constructs={f"lookup.{name}" for name in _LOOKUP_NAMES}
    | {f"function.{name}" for name in FUNCTIONS}
    | {f"aggregate.{name}" for name in AGGREGATES}
    | {
        "path.nested",
        "clause.annotate",
        "clause.alias",
        "clause.group_by",
        "clause.distinct",
        "clause.distinct_on",
        "clause.only",
        "clause.defer",
        "clause.select_related",
        "clause.manager",
        "clause.union",
        "clause.intersection",
        "clause.difference",
        "function.Trunc",
        "aggregate.distinct",
        "aggregate.filter",
        "expression.Case",
        "expression.Subquery",
        "expression.Exists",
        "expression.OuterRef",
        "expression.CombinedExpression",
    },
    cancellation=True,
)
"""The reads and constructs the Django query translation supports; the native PostgreSQL driver declares them too."""

CAPABILITIES = Capabilities(
    operations=READ_CAPABILITIES.operations
    | {
        "create",
        "bulk_create",
        "create_from_parent",
        "save",
        "update",
        "delete",
        "delete_instance",
        "get_or_create",
        "update_or_create",
        "full_clean",
    },
    constructs=READ_CAPABILITIES.constructs
    | {
        "clause.select_for_update",
        "clause.select_for_update.nowait",
        "clause.select_for_update.skip_locked",
        "clause.select_for_update.of",
        "clause.select_for_update.no_key",
        "clause.on_conflict.ignore",
        "clause.on_conflict.update",
    },
    connection_options=POSTGRESQL_OPTIONS,
    savepoints=True,
    cancellation=True,
)
"""What `DjangoDriver` supports: `READ_CAPABILITIES`, the writes, row locks, savepoints and the connection options of
`adorm.drivers.sql.POSTGRESQL_OPTIONS`."""


def model_row(instance: Any) -> ModelRow:
    """A Django model instance as a `ModelRow`: its label and `instance_row()`."""
    return ModelRow(instance._meta.label, instance_row(instance))


def map_django_error(exc: BaseException) -> BaseException:
    """The adorm exception for ``exc``, raised by Django or its database library; what has no Django-specific mapping
    goes to `wrap_driver_error`, an `IntegrityError` with the violation PostgreSQL reports
    (`adorm.drivers.postgresql.integrity_details()`), the label of the model whose table it names as ``model`` and
    that model's adorm field names of the key's columns as ``fields``. The objects of Django's ``ProtectedError`` and
    ``RestrictedError`` become `model_row()` values. The result carries ``exc`` as ``__cause__``."""
    from django.core import exceptions as django_exceptions
    from django.db import transaction
    from django.db.models import deletion

    mapped: BaseException
    if isinstance(exc, deletion.ProtectedError):
        mapped = ProtectedError(exc.args[0], tuple(map(model_row, exc.protected_objects)), original=exc)
    elif isinstance(exc, deletion.RestrictedError):
        mapped = RestrictedError(exc.args[0], tuple(map(model_row, exc.restricted_objects)), original=exc)
    elif isinstance(exc, django_exceptions.ObjectDoesNotExist):
        mapped = ObjectDoesNotExist(*exc.args)
    elif isinstance(exc, django_exceptions.MultipleObjectsReturned):
        mapped = MultipleObjectsReturned(*exc.args)
    elif isinstance(exc, django_exceptions.FieldError | django_exceptions.FieldDoesNotExist):
        mapped = FieldError(str(exc), original=exc)
    elif isinstance(exc, django_exceptions.ValidationError):
        mapped = validation_error(None, exc)
    elif isinstance(exc, transaction.TransactionManagementError):
        mapped = TransactionManagementError(str(exc), original=exc)
    elif isinstance(exc, django_exceptions.SynchronousOnlyOperation):
        mapped = InterfaceError(str(exc), original=exc)
    else:
        return wrap_driver_error(exc, details=_violation)
    mapped.__cause__ = exc
    return mapped


def _violation(exc: BaseException) -> dict[str, Any]:
    details = integrity_details(exc)
    table = details.get("table_name")
    model = None if table is None else table_model(table)
    if model is not None:
        details["model"] = model._meta.label
        # The key of a referenced row is the referenced table's, which PostgreSQL does not name.
        if not references_key(details["detail"]):
            details["fields"] = column_fields(model, details["columns"])
    return details


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


_INERROR = 3
"""libpq's ``PQTRANS_INERROR``: the open transaction failed (psycopg 3 ``TransactionStatus``, psycopg 2 constants)."""


_CURSOR_MODULE = "django.db.backends.utils"
_ORM_SQL_MODULES = ("django.db.models.sql.compiler", "django.db.backends.")
_CALLBACK_RUNNERS = frozenset({"on_commit", "run_and_clear_commit_hooks", "run_commit_hooks_on_set_autocommit_on"})
"""Functions of Django's backend that call application callbacks."""
_SIGNAL_MODULE = "django.dispatch.dispatcher"


def _custom_sql_call(frame: Any, trusted: tuple[str, ...], entries: frozenset[Any]) -> bool:
    """Whether the statement Django's cursor wrapper is executing, seen from ``frame`` inside the wrapper, comes from
    code other than Django's query compiler and database backend (the modules whose names start with ``trusted``).

    The frames above the cursor wrapper are searched, up to a frame running one of the ``entries`` code objects. The
    first one in a ``trusted`` module decides for the compiler or backend; frames of other modules in between
    (instrumentation wrapping the cursor) are passed. A signal dispatch or a backend function that runs application
    callbacks met first decides for custom SQL, as does reaching the end of the search."""
    while frame is not None and frame.f_globals.get("__name__") != _CURSOR_MODULE:
        frame = frame.f_back
    while frame is not None and frame.f_code not in entries:
        module = str(frame.f_globals.get("__name__", ""))
        if module == _SIGNAL_MODULE:
            return True
        if module != _CURSOR_MODULE and module.startswith(trusted):
            return frame.f_code.co_name in _CALLBACK_RUNNERS
        frame = frame.f_back
    return True


def _transaction_failed(db: Any) -> bool:
    """Whether the database connection of Django's wrapper ``db`` is in a failed transaction."""
    info = getattr(db.connection, "info", None)
    status = getattr(info, "transaction_status", None)
    return status is not None and int(status) == _INERROR


class DjangoConnection(ThreadedConnection):
    """A Django database connection, the thread's own ``connections[profile.alias]``.

    The alias is a PostgreSQL database (``NotSupportedError`` otherwise). ``profile.params`` replace keys of the
    alias's ``DATABASES`` entry for this connection; ``profile.connect``, when set, returns the DB-API connection that
    Django's wrapper uses instead of opening one (for psycopg 3, opened with the wrapper's
    ``get_connection_params()``), and Django configures it as its own (isolation level, time zone, role).
    ``target`` (what profile hooks receive) is Django's connection wrapper. Every call sees Django's active language and
    time zone of the caller (`adopt_context_locals()`) and runs its ORM calls on the profile's alias.

    The slot decides how long the connection lives: the wrapper runs without ``CONN_MAX_AGE`` and
    ``CONN_HEALTH_CHECKS``, and opens its connection directly also when the alias uses Django's connection pool. When
    the connection closes, the wrapper gets the alias's settings back (for a ``direct`` driver, the event loop thread's
    wrapper). A statement that an operation, or one of Django's ``on_commit()`` callbacks, runs through the wrapper's
    cursor from code other than Django's query compiler and database backend (raw SQL of a signal receiver,
    ``Manager.raw()``, ``cursor.execute()``) marks the connection with `mark_custom_sql()`; SQL sent past Django's
    execute wrappers (``callproc()``, the DB-API connection) is not seen. SQL of profile hooks and of
    ``set_options()`` does not count.

    A transaction is an outermost ``transaction.atomic()`` block of the alias, entered by ``begin()`` and left by
    ``commit()`` or ``rollback()``; a savepoint is a Django savepoint inside it, so Django's ``on_commit()`` callbacks
    and its own atomic blocks behave as in an atomic block of Django code. Inside Django's ``TestCase`` the driver uses
    a subclass that shares the test's connection (`adorm.testing.django`).
    """

    driver: DjangoDriver
    db: Any
    _atomic: Any = None
    _savepoints: list[tuple[str, str]]
    _base_ids: tuple[Any, ...] = ()
    _test: Any = None

    @property
    def target(self) -> Any:
        return self.db

    @property
    def alias(self) -> str:
        """The Django database alias of the connection, ``profile.alias``."""
        return self.profile.alias

    def prepare_call_sync(self) -> None:
        adopt_context_locals()

    def connect_sync(self) -> None:
        from django.db import connections

        db = connections[self.profile.alias]
        if db.vendor != "postgresql":
            raise NotSupportedError(f"the Django driver supports PostgreSQL databases, not {db.vendor!r}")
        if db.connection is not None and db.settings_dict.get("OPTIONS", {}).get("pool"):
            # A direct driver's thread holds a connection of Django's pool: it goes back to that pool.
            db.close()
        self._restore = (db.settings_dict, db.Database, db.health_check_enabled, db.close_at)
        # The slot's connection is opened directly, never taken from Django's pool, and lives until the slot closes.
        settings = {**db.settings_dict, **self.profile.params, "CONN_HEALTH_CHECKS": False, "CONN_MAX_AGE": None}
        settings["OPTIONS"] = {key: value for key, value in settings.get("OPTIONS", {}).items() if key != "pool"}
        db.settings_dict = settings
        connect = self.profile.connect
        if connect is not None:
            db.Database = _HookDatabase(db.Database, lambda: connect(self.profile))
        self._adopt(db)
        db.ensure_connection()
        # Also for a connection the thread's wrapper already had open.
        db.health_check_enabled = False
        db.close_at = None

    def _adopt(self, db: Any) -> None:
        """Use Django's connection wrapper ``db``."""
        self.db = db
        self._savepoints = []
        package = type(db).__module__.rpartition(".")[0]
        self._trusted = (*_ORM_SQL_MODULES, f"{package}.") if package else _ORM_SQL_MODULES

    def is_usable(self) -> bool:
        """Also false once the ``TestCase`` atomic blocks on the test's connection differ from those open when the
        connection was opened (`adorm.testing.django.TestDatabase.current()`): after the ``TestCase`` test it was opened
        in, and, for one opened outside a ``TestCase``, while a ``TestCase`` transaction is open."""
        return super().is_usable() and (self._test is None or self._test.current())

    def _watch_sql(self, execute: Any, sql: Any, params: Any, many: bool, context: Any) -> Any:
        if not self.ran_custom_sql and _custom_sql_call(sys._getframe(1), self._trusted, _ENTRIES):
            self.mark_custom_sql()
        return execute(sql, params, many, context)

    def execute_sync(self, operation: Any) -> Any:
        with self.db.execute_wrapper(self._watch_sql):
            return self._execute_operation(operation)

    def _execute_operation(self, operation: Any) -> Any:
        from django.core.exceptions import ValidationError as DjangoValidationError

        try:
            return self._run_operation(operation)
        except DjangoValidationError as exc:
            raise validation_error(operation_model(operation), exc) from exc

    def _run_operation(self, operation: Any) -> Any:
        kind = operation.kind
        write = _WRITES.get(kind)
        if write is not None:
            return write(self.alias, operation)
        if kind == "aggregate":
            queryset, expressions = aggregation(operation.query, operation.aggregations)
            return queryset.using(self.alias).aggregate(**expressions)
        if kind == "in_bulk":
            check_in_bulk_field(operation)
            translated = translate(operation.select_query())
            return translated.rows(translated.queryset.using(self.alias))
        translated = translate(operation.query)
        queryset = translated.queryset.using(self.alias)
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
            if operation.query.is_combined:
                # Django's exists() selects a constant in the queries of a union only, not in a nested combination.
                return bool(queryset[:1])
            return queryset.exists()
        raise NotSupportedError(f"the Django driver does not execute {kind!r} operations")

    # Transactions

    def _require_transaction(self) -> Any:
        if self._atomic is None:
            raise TransactionManagementError("no transaction is open on this connection")
        return self._atomic

    def _sync_savepoint_ids(self) -> None:
        self.db.savepoint_ids = [*self._base_ids, *(sid for _, sid in self._savepoints)]

    def _savepoint_index(self, name: str) -> int:
        self._require_transaction()
        for index, (savepoint, _) in enumerate(self._savepoints):
            if savepoint == name:
                return index
        raise TransactionManagementError(f"no savepoint {name!r} is open")

    def _leave(self, atomic: Any, *, rollback: bool) -> None:
        """Leave the outermost atomic block ``atomic``: commit, or roll back with ``rollback``."""
        self._atomic = None
        self._savepoints = []
        self.db.savepoint_ids = list(self._base_ids)
        if rollback:
            self.db.needs_rollback = True
        atomic.__exit__(None, None, None)

    def begin_sync(self) -> None:
        from django.db import transaction

        if self._atomic is not None:
            raise TransactionManagementError("a transaction is already open on this connection")
        if self.db.in_atomic_block:
            # A direct connection on a thread inside Django's own atomic block.
            raise TransactionManagementError("the thread's Django connection is inside an atomic block")
        atomic = transaction.atomic(using=self.alias)
        atomic.__enter__()
        self._atomic = atomic
        self._savepoints = []
        # The savepoint ids of the enclosing blocks: none for an outermost block.
        self._base_ids = tuple(self.db.savepoint_ids)

    def savepoint_sync(self, name: str) -> None:
        self._require_transaction()
        if any(savepoint == name for savepoint, _ in self._savepoints):
            raise TransactionManagementError(f"the savepoint {name!r} is already open")
        sid = self.db.savepoint()
        self._savepoints.append((name, sid))
        self._sync_savepoint_ids()

    def release_savepoint_sync(self, name: str) -> None:
        index = self._savepoint_index(name)
        # Releasing a savepoint releases the savepoints opened after it.
        self.db.savepoint_commit(self._savepoints[index][1])
        del self._savepoints[index:]
        self._sync_savepoint_ids()

    def rollback_to_savepoint_sync(self, name: str) -> None:
        index = self._savepoint_index(name)
        # Django refuses every query of a transaction marked for rollback, ROLLBACK TO SAVEPOINT included.
        self.db.needs_rollback = False
        # Django drops the on_commit callbacks registered after the savepoint.
        self.db.savepoint_rollback(self._savepoints[index][1])
        del self._savepoints[index + 1 :]
        self._sync_savepoint_ids()

    def commit_sync(self) -> None:
        atomic = self._require_transaction()
        db = self.db
        if db.closed_in_transaction:
            self._leave(atomic, rollback=True)
            raise InterfaceError("the connection was closed inside the transaction")
        if db.needs_rollback or _transaction_failed(db):
            self._leave(atomic, rollback=True)
            raise TransactionManagementError("the transaction failed earlier, so it was rolled back")
        callbacks = self._take_commit_callbacks()
        self._leave(atomic, rollback=False)
        if callbacks:
            # After the commit's outcome: cancelling the caller cannot reach them or turn the commit into a cancel.
            self.after_call_sync(lambda: self._run_on_commit(callbacks))

    def _take_commit_callbacks(self) -> list[tuple[Any, Callable[[], Any], bool]]:
        """Remove Django's ``on_commit()`` callbacks that the commit runs from the wrapper and return them."""
        callbacks, self.db.run_on_commit = self.db.run_on_commit, []
        return callbacks

    def _run_on_commit(self, callbacks: list[tuple[Any, Callable[[], Any], bool]]) -> None:
        """Run Django's ``on_commit()`` callbacks of a committed transaction in order. A failure is logged, since the
        commit stands; a callback without ``robust`` that fails stops the remaining ones, as in Django."""
        with self.db.execute_wrapper(self._watch_sql):
            for _, func, robust in callbacks:
                try:
                    func()
                except Exception:
                    logger.exception("Django on_commit callback %r failed", func)
                    if not robust:
                        return

    def rollback_sync(self) -> None:
        atomic = self._atomic
        if atomic is not None:
            self._leave(atomic, rollback=True)

    def set_options_sync(self, options: Any, local: bool) -> None:
        """Set PostgreSQL's time limits with `set_options_sql()` on a cursor of the connection."""
        sql = set_options_sql(options, local)
        if sql:
            with self.db.cursor() as cursor:
                cursor.execute(sql)

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
        if db is None:
            return
        atomic, self._atomic = self._atomic, None
        self._savepoints = []
        try:
            if atomic is None:
                db.close()
                return
            # Closing inside the transaction leaves its rollback to the database; leaving the block resets Django's
            # transaction state of the wrapper, which a direct driver's next connection on this thread reuses.
            db.savepoint_ids = []
            try:
                db.close()
            finally:
                atomic.__exit__(None, None, None)
        finally:
            # A direct driver's thread uses the wrapper with the alias's own settings again.
            db.settings_dict, db.Database, db.health_check_enabled, db.close_at = self._restore

    def map_error(self, exc: BaseException) -> BaseException:
        return map_django_error(exc)


_ENTRIES = frozenset({DjangoConnection.execute_sync.__code__, DjangoConnection._run_on_commit.__code__})


# Writes: each runs on the connection's thread and returns plain data.


def _manager(model: Any, name: str | None) -> Any:
    if name is None:
        return model._base_manager
    from django.db import models

    manager = getattr(model, name, None)
    if not isinstance(manager, models.Manager):
        raise ProgrammingError(f"{model._meta.label} has no Django manager {name!r}")
    return manager


def _create(alias: str, operation: Any) -> dict[str, Any]:
    model = django_model(operation.model)
    kwargs = field_kwargs(operation.model, operation.values)
    instance = _manager(model, operation.manager).db_manager(alias).create(**kwargs)
    return instance_row(instance)


def _bulk_create(alias: str, operation: Any) -> list[dict[str, Any]]:
    model = django_model(operation.model)
    check_bulk_create(operation)
    objs = [model(**field_kwargs(operation.model, row)) for row in operation.rows]
    _manager(model, operation.manager).db_manager(alias).bulk_create(objs, **bulk_create_options(operation))
    return [instance_row(instance) for instance in objs]


def _insert(alias: str, model: Any, kwargs: dict[str, Any], pk: Any, force: bool) -> dict[str, Any]:
    instance = model(**kwargs)
    if pk is not None:
        instance.pk = pk
    instance.save(using=alias, force_insert=force)
    return instance_row(instance)


def _create_from_parent(alias: str, operation: Any) -> dict[str, Any]:
    model = django_model(operation.model)
    parent = parent_link_field(model).related_model._base_manager.using(alias).get(pk=operation.parent_pk)
    instance = child_instance(operation, parent)
    for field in model._meta.local_concrete_fields:
        setattr(instance, field.attname, field.pre_save(instance, add=True))
    # A raw save inserts the child's own table only; the field values are prepared above.
    instance.save_base(using=alias, raw=True, force_insert=True)
    return instance_row(instance)


def _full_clean(alias: str, operation: Any) -> dict[str, Any]:
    model = django_model(operation.model)
    stored = None
    if clean_needs_row(operation):
        stored = model._base_manager.using(alias).filter(pk=operation.pk).first()
    instance = clean_instance(operation, alias, stored)
    # Every query of the validation runs on the profile's alias.
    with pinned_alias(alias):
        instance.full_clean(
            exclude=clean_exclude(operation),
            validate_unique=operation.validate_unique,
            validate_constraints=operation.validate_constraints,
        )
    return instance_row(instance)


def _save(alias: str, operation: Any) -> dict[str, Any]:
    model = django_model(operation.model)
    names = [name for name, _ in operation.values]
    kwargs = field_kwargs(operation.model, operation.values)
    if operation.pk is None or operation.force_insert:
        return _insert(alias, model, kwargs, operation.pk, operation.force_insert)
    opts = model._meta
    by_name = dict(zip(names, kwargs, strict=True))
    values = {opts.get_field(key).attname: value for key, value in kwargs.items()}
    # Every primary key on the way to the row (the parents' of a multi-table child too) has the instance's value.
    loaded = [
        (field.attname, operation.pk if field.primary_key else values[field.attname])
        for field in opts.concrete_fields
        if field.primary_key or field.attname in values
    ]
    instance = model.from_db(alias, [name for name, _ in loaded], [value for _, value in loaded])
    if operation.update_fields is None and len(loaded) < len(opts.concrete_fields):
        # Django saves a partly loaded instance with update_fields, which never inserts; the row is checked first.
        if not model._base_manager.using(alias).filter(pk=operation.pk).exists():
            if operation.force_update:
                from django.db import DatabaseError

                raise DatabaseError("Forced update did not affect any rows.")
            return _insert(alias, model, kwargs, operation.pk, True)
        if not values:
            return instance_row(instance)
    update_fields = None if operation.update_fields is None else [by_name[n] for n in operation.update_fields]
    instance.save(using=alias, update_fields=update_fields, force_update=operation.force_update)
    return instance_row(instance)


def _update(alias: str, operation: Any) -> int:
    queryset = model_queryset(operation.query).using(alias)
    return queryset.update(**field_kwargs(operation.query, operation.values))


def _delete(alias: str, operation: Any) -> tuple[int, dict[str, int]]:
    with _complete_error_objects(alias):
        total, counts = model_queryset(operation.query).using(alias).delete()
    return total, dict(counts)


def _delete_instance(alias: str, operation: Any) -> tuple[int, dict[str, int]]:
    model = django_model(operation.model)
    try:
        instance = model._base_manager.using(alias).get(pk=operation.pk)
    except model.DoesNotExist:
        return 0, {}
    with _complete_error_objects(alias):
        result = instance.delete(using=alias)
    # A delete() override (a soft delete) may return something else; it deleted nothing then.
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], int):
        return result[0], dict(result[1])
    return 0, {}


@contextlib.contextmanager
def _complete_error_objects(alias: str) -> Iterator[None]:
    """Replace the objects of a ``ProtectedError`` or ``RestrictedError`` raised in the block with complete ones."""
    from django.db.models import deletion

    try:
        yield
    except deletion.ProtectedError as exc:
        exc.protected_objects = _reloaded(alias, exc.protected_objects)
        raise
    except deletion.RestrictedError as exc:
        exc.restricted_objects = _reloaded(alias, exc.restricted_objects)
        raise


def _reloaded(alias: str, objects: Any) -> list[Any]:
    """``objects`` (Django instances that may have deferred fields) read again with every field, one query per
    model, in the order of their primary keys."""
    by_model: dict[Any, list[Any]] = {}
    for instance in objects:
        by_model.setdefault(type(instance), []).append(instance.pk)
    reloaded: list[Any] = []
    for model, pks in by_model.items():
        reloaded.extend(model._base_manager.using(alias).filter(pk__in=pks).order_by("pk"))
    return reloaded


def _get_or_create(alias: str, operation: Any) -> tuple[dict[str, Any], bool]:
    queryset = model_queryset(operation.query).using(alias)
    instance, created = queryset.get_or_create(
        defaults=field_kwargs(operation.query, operation.defaults), **lookup_kwargs(operation.query, operation.lookups)
    )
    return instance_row(instance), created


def _update_or_create(alias: str, operation: Any) -> tuple[dict[str, Any], bool]:
    queryset = model_queryset(operation.query).using(alias)
    create_defaults = operation.create_defaults
    instance, created = queryset.update_or_create(
        defaults=field_kwargs(operation.query, operation.defaults),
        create_defaults=None if create_defaults is None else field_kwargs(operation.query, create_defaults),
        **lookup_kwargs(operation.query, operation.lookups),
    )
    return instance_row(instance), created


_WRITES: dict[str, Callable[[str, Any], Any]] = {
    "create": _create,
    "bulk_create": _bulk_create,
    "create_from_parent": _create_from_parent,
    "full_clean": _full_clean,
    "save": _save,
    "update": _update,
    "delete": _delete,
    "delete_instance": _delete_instance,
    "get_or_create": _get_or_create,
    "update_or_create": _update_or_create,
}


class DjangoDriver(ThreadedDriver):
    """Executes adorm reads and writes with Django's ORM.

    Each connection runs on a thread of its own with that thread's Django connection for the profile's ``alias``;
    only synchronous ORM methods are called, in a copy of the caller's context (Django's active language included).
    Django must be set up (``django.setup()``) before the first operation.
    """

    name = "django"
    connection_class = DjangoConnection
    capabilities = CAPABILITIES

    async def open_connection(self, profile: Any, emit: Any) -> ThreadedConnection:
        """Open a ``connection_class`` connection; inside a Django test, `adorm.testing.django.django_connection()`
        chooses the connection (the shared test connection of a ``TestCase``)."""
        from adorm.testing.django import django_connection

        connection = await django_connection(self, profile, emit)
        await connection.start()
        return connection
