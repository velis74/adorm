"""adorm's exception hierarchy, modelled on Django's.

Database errors follow PEP 249 as Django's ``django.db`` errors do. Executor errors report slot acquisition,
transaction and client-side timeout failures.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any, Literal

__all__ = [
    "AdormError",
    "DatabaseError",
    "DataError",
    "DeadlockError",
    "Error",
    "FieldError",
    "IntegrityError",
    "InterfaceError",
    "InternalError",
    "ModelExceptionsMixin",
    "MultipleObjectsReturned",
    "NotSupportedError",
    "ObjectDoesNotExist",
    "OperationalError",
    "OperationTimeout",
    "ProgrammingError",
    "ProtectedError",
    "RestrictedError",
    "SchemaCheckError",
    "SchemaMismatchError",
    "SessionTimeout",
    "TimeoutPhase",
    "TransactionClosed",
    "TransactionManagementError",
    "TransactionOverlapError",
    "ValidationError",
    "wrap_driver_error",
]


class AdormError(Exception):
    """Base class of every exception adorm raises."""


# Objects


class ObjectDoesNotExist(AdormError):
    """A query expected an object and found none. Base class of every model's ``DoesNotExist``."""


class MultipleObjectsReturned(AdormError):
    """A query expected one object and found several. Base class of every model's ``MultipleObjectsReturned``."""


class ModelExceptionsMixin:
    """Gives every subclass its own ``DoesNotExist`` and ``MultipleObjectsReturned``.

    A subclass's exceptions derive from its parents' exceptions, or from `ObjectDoesNotExist` and
    `MultipleObjectsReturned` at the top of the hierarchy. A class that defines either name itself keeps it.
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for name, root in (("DoesNotExist", ObjectDoesNotExist), ("MultipleObjectsReturned", MultipleObjectsReturned)):
            if name not in cls.__dict__:
                setattr(cls, name, _model_exception(cls, name, root))


def _model_exception(cls: type, name: str, root: type[AdormError]) -> type[AdormError]:
    candidates: list[type] = []
    for base in cls.__bases__:
        parent = getattr(base, name, None)
        if isinstance(parent, type) and issubclass(parent, root) and parent not in candidates:
            candidates.append(parent)
    # A parent exception that another candidate already derives from would make the MRO inconsistent.
    bases = tuple(c for c in candidates if not any(o is not c and issubclass(o, c) for o in candidates)) or (root,)
    return type(name, bases, {"__module__": cls.__module__, "__qualname__": f"{cls.__qualname__}.{name}"})


# Validation


class ValidationError(AdormError):
    """Data failed validation.

    ``message`` is a string, a list of messages or errors, a dict mapping field names to messages, or another
    `ValidationError`. ``code`` and ``params`` apply to a single string message; ``params`` fill ``%``
    placeholders in it.
    """

    error_dict: dict[str, list[ValidationError]]
    error_list: list[ValidationError]

    def __init__(self, message: Any, code: str | None = None, params: Mapping[str, Any] | None = None) -> None:
        super().__init__(message, code, params)
        if isinstance(message, ValidationError):
            if hasattr(message, "error_dict"):
                message = message.error_dict
            elif not hasattr(message, "message"):
                message = message.error_list
            else:
                message, code, params = message.message, message.code, message.params

        if isinstance(message, dict):
            self.error_dict = {}
            for field, messages in message.items():
                error = messages if isinstance(messages, ValidationError) else ValidationError(messages)
                self.error_dict[field] = error.error_list
        elif isinstance(message, list):
            self.error_list = []
            for item in message:
                error = item if isinstance(item, ValidationError) else ValidationError(item)
                if hasattr(error, "error_dict"):
                    self.error_list.extend(e for errors in error.error_dict.values() for e in errors)
                else:
                    self.error_list.extend(error.error_list)
        else:
            self.message = message
            self.code = code
            self.params = params
            self.error_list = [self]

    @property
    def message_dict(self) -> dict[str, list[str]]:
        """Messages by field name; ``AttributeError`` when the error was not built from a dict."""
        return {field: [str(m) for e in errors for m in e] for field, errors in self.error_dict.items()}

    @property
    def messages(self) -> list[str]:
        """Every message as text, with ``params`` filled in."""
        if hasattr(self, "error_dict"):
            return [m for messages in self.message_dict.values() for m in messages]
        return list(self)

    def __iter__(self) -> Iterator[Any]:
        """Yields ``(field, messages)`` pairs for a dict of errors, otherwise each message."""
        if hasattr(self, "error_dict"):
            for field, errors in self.error_dict.items():
                yield field, [m for e in errors for m in e]
            return
        for error in self.error_list:
            message = str(error.message)
            yield message % error.params if error.params else message

    def __str__(self) -> str:
        if hasattr(self, "error_dict"):
            return repr(self.message_dict)
        return repr(self.messages)

    def __repr__(self) -> str:
        return f"ValidationError({self})"


# Database (PEP 249)


class Error(AdormError):
    """Base class of database errors (PEP 249 ``Error``).

    ``connection_lost`` is set by the driver connection that raised the error: whether it left the connection unusable
    (`adorm.drivers.DriverConnection.is_connection_lost()`); ``None`` when no connection judged it.
    """

    def __init__(
        self, message: str | None = None, *, original: BaseException | None = None, sqlstate: str | None = None
    ) -> None:
        if message is None:
            message = (str(original) or type(original).__name__) if original is not None else type(self).__name__
        super().__init__(message)
        self.original = original
        self.sqlstate = sqlstate
        self.connection_lost: bool | None = None


class InterfaceError(Error):
    """An error of the database interface rather than the database (PEP 249)."""


class DatabaseError(Error):
    """An error of the database. A driver error adorm does not recognise becomes this class."""


class DataError(DatabaseError):
    """A value is invalid for its column: out of range, too long, wrong format (SQLSTATE class 22)."""


class OperationalError(DatabaseError):
    """The database failed to operate: failed or lost connection, cancelled statement, serialization failure."""


class IntegrityError(DatabaseError):
    """A constraint was violated: unique, foreign key, not null, check, exclusion (SQLSTATE class 23).

    ``constraint_name``, ``table_name`` and ``schema_name`` name the violated constraint and its table (for a foreign
    key, the referencing table), ``detail`` is the database's detail message. ``columns`` are the database columns of
    the violated key, or the column of a not-null violation; ``values`` are the key's values as text, in the order of
    ``columns``, as the database prints them. ``model`` is the label of the model whose table is ``table_name``, and
    ``fields`` are that model's adorm field names of ``columns``, in their order, without a column that is no field
    (an index expression). What is not known is ``None``, or empty for ``columns``, ``values`` and ``fields``.
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        constraint_name: str | None = None,
        table_name: str | None = None,
        schema_name: str | None = None,
        columns: Iterable[str] = (),
        values: Iterable[str] = (),
        detail: str | None = None,
        model: str | None = None,
        fields: Iterable[str] = (),
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.constraint_name = constraint_name
        self.table_name = table_name
        self.schema_name = schema_name
        self.columns = tuple(columns)
        self.values = tuple(values)
        self.detail = detail
        self.model = model
        self.fields = tuple(fields)

    @property
    def key(self) -> dict[str, str]:
        """The violated key, column to value; empty when the values are not known."""
        return dict(zip(self.columns, self.values, strict=True)) if len(self.values) == len(self.columns) else {}


class ProtectedError(IntegrityError):
    """A delete was refused because ``on_delete=PROTECT`` references point at the deleted objects."""

    def __init__(self, message: str | None = None, protected_objects=(), **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.protected_objects = protected_objects


class RestrictedError(IntegrityError):
    """A delete was refused because ``on_delete=RESTRICT`` references point at the deleted objects."""

    def __init__(self, message: str | None = None, restricted_objects=(), **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.restricted_objects = restricted_objects


class InternalError(DatabaseError):
    """The database reported an internal error or an invalid cursor or transaction state."""


class ProgrammingError(DatabaseError):
    """The statement is wrong: missing table or column, syntax error, insufficient privilege, PL/pgSQL error."""


class FieldError(ProgrammingError):
    """A field path or lookup key does not resolve on its model (Django's ``FieldError``): a segment that is neither
    a field nor a relation, or a segment after a field that takes none (an unknown lookup name).

    ``model`` is the label of the model the failing segment was resolved on and ``key`` the field path or lookup key;
    either is ``None`` when a driver reports the error without them.
    """

    def __init__(
        self, message: str | None = None, *, model: str | None = None, key: str | None = None, **kwargs: Any
    ) -> None:
        super().__init__(message, **kwargs)
        self.model = model
        self.key = key


class NotSupportedError(DatabaseError):
    """The database or the driver does not support the requested feature (SQLSTATE class 0A).

    A driver's capability check sets ``driver`` to the driver's name and ``unsupported`` to the names it does not
    support (an operation kind, construct names, connection option names or ``"savepoints"``); otherwise ``driver`` is
    ``None`` and ``unsupported`` empty.
    """

    def __init__(
        self, message: str | None = None, *, driver: str | None = None, unsupported: Iterable[str] = (), **kwargs: Any
    ) -> None:
        super().__init__(message, **kwargs)
        self.driver = driver
        self.unsupported = tuple(unsupported)


class SchemaMismatchError(ProgrammingError):
    """The adorm model ``model`` (a label) was generated from another schema than the driver's target has: its schema
    hash ``expected`` differs from the driver's ``actual``. ``actual`` is ``None`` when the driver has no hash for the
    model, and ``reason`` then says why (the target has no such model, or the error computing the hash). ``driver``
    names the driver. Raised before the operation reaches the database."""

    def __init__(
        self,
        message: str | None = None,
        *,
        model: str | None = None,
        expected: str | None = None,
        actual: str | None = None,
        driver: str | None = None,
        reason: str | None = None,
        **kwargs: Any,
    ) -> None:
        if message is None:
            message = f"the adorm model {model} does not match the model of the {driver} driver"
        super().__init__(message, **kwargs)
        self.model = model
        self.expected = expected
        self.actual = actual
        self.driver = driver
        self.reason = reason


class SchemaCheckError(ProgrammingError):
    """`adorm.check_schema()` found ``problems`` (`adorm.SchemaProblem` values) between the models and the database."""

    def __init__(self, message: str | None = None, *, problems: Iterable[Any] = (), **kwargs: Any) -> None:
        problems = tuple(problems)
        if message is None:
            count = len(problems)
            lines = "".join(f"\n  {problem}" for problem in problems)
            message = f"the database does not match the models ({count} problem{'' if count == 1 else 's'}):{lines}"
        super().__init__(message, **kwargs)
        self.problems = problems


class TransactionManagementError(ProgrammingError):
    """A transaction was used incorrectly."""


class TransactionClosed(TransactionManagementError):
    """An operation arrived for a transaction that has already ended."""


class TransactionOverlapError(TransactionManagementError):
    """Work overlapped a transaction it is not nested in, on a connection that cannot isolate them: the test connection
    the slots share inside Django's ``TestCase`` (`adorm.testing.django`)."""


# SQLSTATE class (first two characters, else the first) to exception, as psycopg maps them, except class 3D:
# a missing database is a connection failure, which psycopg reports as OperationalError without a SQLSTATE.
_SQLSTATE_CLASSES: dict[str, type[DatabaseError]] = {
    "08": OperationalError,  # connection exception
    "0A": NotSupportedError,  # feature not supported
    "10": ProgrammingError,  # XQuery error
    "20": ProgrammingError,  # case not found
    "21": ProgrammingError,  # cardinality violation
    "22": DataError,  # data exception
    "23": IntegrityError,  # integrity constraint violation
    "24": InternalError,  # invalid cursor state
    "25": InternalError,  # invalid transaction state
    "26": ProgrammingError,  # invalid SQL statement name
    "27": OperationalError,  # triggered data change violation
    "28": OperationalError,  # invalid authorization specification
    "2B": InternalError,  # dependent privilege descriptors still exist
    "2D": InternalError,  # invalid transaction termination
    "2F": OperationalError,  # SQL routine exception
    "34": ProgrammingError,  # invalid cursor name
    "38": OperationalError,  # external routine exception
    "39": OperationalError,  # external routine invocation exception
    "3B": OperationalError,  # savepoint exception
    "3D": OperationalError,  # invalid catalog name
    "3F": ProgrammingError,  # invalid schema name
    "40": OperationalError,  # transaction rollback: serialization failure, deadlock detected
    "42": ProgrammingError,  # syntax error or access rule violation
    "44": ProgrammingError,  # WITH CHECK OPTION violation
    "53": OperationalError,  # insufficient resources
    "54": OperationalError,  # program limit exceeded
    "55": OperationalError,  # object not in prerequisite state
    "57": OperationalError,  # operator intervention: query cancelled, admin shutdown
    "58": OperationalError,  # system error
    "F": OperationalError,  # configuration file error
    "H": OperationalError,  # foreign data wrapper error
    "P": ProgrammingError,  # PL/pgSQL error
    "X": InternalError,  # internal error
}

_PEP249_NAMES: dict[str, type[Error]] = {
    cls.__name__: cls
    for cls in (
        DataError,
        OperationalError,
        IntegrityError,
        InternalError,
        ProgrammingError,
        NotSupportedError,
        InterfaceError,
        DatabaseError,
    )
}


def _sqlstate(exc: BaseException) -> tuple[BaseException, str] | None:
    """The first exception on ``exc``'s ``__cause__`` chain that carries a SQLSTATE, and the SQLSTATE."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for attribute in ("sqlstate", "pgcode"):
            value = getattr(current, attribute, None)
            if isinstance(value, str) and len(value) == 5:
                return current, value
        current = current.__cause__
    return None


def wrap_driver_error(
    exc: BaseException, details: Callable[[BaseException], Mapping[str, Any]] | None = None
) -> BaseException:
    """Return the adorm exception for an exception raised by a driver's ORM or database library.

    An adorm exception, and an exception that is not an `Exception` (``asyncio.CancelledError``,
    ``KeyboardInterrupt``), is returned unchanged. Otherwise a SQLSTATE found on the exception or its ``__cause__``
    chain (``sqlstate`` or ``pgcode``) decides the class, `DatabaseError` for an unknown SQLSTATE class. Without a
    SQLSTATE, a PEP 249 class name in the MRO decides, then an `OSError` becomes `OperationalError`; anything else
    becomes `DatabaseError`. The result carries the exception in ``original`` and ``__cause__``. For an
    `IntegrityError`, ``details(exc)`` returns the keyword arguments that describe the violation.
    """
    if isinstance(exc, AdormError) or not isinstance(exc, Exception):
        return exc
    found = _sqlstate(exc)
    sqlstate = found[1] if found is not None else None
    cls: type[Error] | None
    if sqlstate is not None:
        cls = _SQLSTATE_CLASSES.get(sqlstate[:2]) or _SQLSTATE_CLASSES.get(sqlstate[:1]) or DatabaseError
    else:
        cls = next((_PEP249_NAMES[k.__name__] for k in type(exc).__mro__ if k.__name__ in _PEP249_NAMES), None)
    if cls is None:
        cls = OperationalError if isinstance(exc, OSError) else DatabaseError
    described = details(exc) if details is not None and issubclass(cls, IntegrityError) else {}
    wrapped = cls(original=exc, sqlstate=sqlstate, **described)
    wrapped.__cause__ = exc
    return wrapped


# Executor


def _slot_message(session: Any, timeout: float | None) -> str:
    message = "no slot became free"
    if session is not None:
        message += f" for session {session!r}"
    if timeout is not None:
        message += f" within {timeout:g} s"
    return message


class SessionTimeout(AdormError):
    """No slot became free for ``session`` within ``timeout`` seconds."""

    def __init__(self, message: str | None = None, *, session: Any = None, timeout: float | None = None) -> None:
        super().__init__(_slot_message(session, timeout) if message is None else message)
        self.session = session
        self.timeout = timeout


class DeadlockError(SessionTimeout):
    """A slot acquisition failed, or cannot succeed: the flow holds slots, or it waits for a transaction on a shared
    database session that cannot end (`adorm.executor.turns`).

    ``held_sessions`` lists the sessions whose slots the waiting flow holds.
    """

    def __init__(self, message: str | None = None, *, held_sessions=(), **kwargs: Any) -> None:
        held_sessions = tuple(held_sessions)
        if message is None:
            message = _slot_message(kwargs.get("session"), kwargs.get("timeout"))
            if held_sessions:
                message += f" while holding {', '.join(map(repr, held_sessions))}"
        super().__init__(message, **kwargs)
        self.held_sessions = held_sessions


TimeoutPhase = Literal["waiting_for_slot", "executing"]
_PHASES = ("waiting_for_slot", "executing")


class OperationTimeout(AdormError, TimeoutError):
    """An ``adorm.timeout()`` block expired.

    ``phase`` tells whether the operation was waiting for a free slot or executing; ``operation`` is the
    operation that was in progress (an adorm operation or the name of a transaction request), ``session`` its session;
    both are ``None`` when no operation was in progress.
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        phase: TimeoutPhase | None = None,
        operation: Any = None,
        session: Any = None,
        timeout: float | None = None,
    ) -> None:
        if phase is not None and phase not in _PHASES:
            raise ValueError(f"phase must be one of {_PHASES}, not {phase!r}")
        if message is None:
            message = "operation timed out"
            if timeout is not None:
                message += f" after {timeout:g} s"
            if phase is not None:
                message += f" while {phase.replace('_', ' ')}"
            if session is not None:
                message += f" on {session!r}"
            if operation is not None:
                message += f": {operation!r}"
        super().__init__(message)
        self.phase = phase
        self.operation = operation
        self.session = session
        self.timeout = timeout
