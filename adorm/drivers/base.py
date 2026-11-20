"""The driver contract.

A driver executes adorm operations on its target (an ORM or a database library). The core hands a driver
connection one operation at a time and gets plain data back; callbacks and backend events travel through the
``emit`` callable the connection receives when it opens. A driver declares what it supports in `Capabilities` and
how it runs in ``mode``. Every error a connection raises is an adorm exception.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import enum
import inspect
import itertools
import logging
import queue
import re
import selectors
import socket
import threading

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, fields
from typing import Any, cast, ClassVar, Literal, Protocol, runtime_checkable, TYPE_CHECKING

from adorm.events import BackendEvent, CONNECTION_LOST, Emit, Event, NOTIFY
from adorm.exceptions import (
    Error,
    InterfaceError,
    NotSupportedError,
    OperationalError,
    SchemaMismatchError,
    wrap_driver_error,
)

if TYPE_CHECKING:
    from adorm.executor.turns import TransactionTurns

__all__ = [
    "BackendActivity",
    "Capabilities",
    "DatabaseMonitor",
    "Driver",
    "DriverConnection",
    "ExecutionMode",
    "Operation",
    "Profile",
    "ThreadedConnection",
    "ThreadedDriver",
    "check_channel",
    "is_connection_error",
    "is_custom_option",
    "is_refusal",
    "operation_models",
]

logger = logging.getLogger("adorm.drivers")

ExecutionMode = Literal["threaded", "async"]
_MODES = ("threaded", "async")


@runtime_checkable
class Operation(Protocol):
    """What a driver needs to know about an operation before executing it.

    ``kind`` names the operation (``"select"``, ``"update"`` …); ``constructs()`` names every construct it uses
    (``"lookup.icontains"``, ``"function.Lower"``, ``"clause.select_for_update"`` …).
    """

    @property
    def kind(self) -> str: ...

    def constructs(self) -> Iterable[str]: ...


@runtime_checkable
class Profile(Protocol):
    """The connection profile a driver opens a connection for.

    ``name`` is the ``using`` value, ``alias`` the target's database alias and ``params`` the driver parameters.
    ``connect``, when not ``None``, opens the target's connection in place of the driver's default:
    ``connect(profile)`` returns the target's connection; an async driver awaits the result. The pool also reads the
    optional ``on_acquire`` and ``on_release`` attributes, when present and not ``None``: hooks called as
    ``hook(connection, profile, session)`` with the connection's `DriverConnection.target`, and the optional
    ``options`` attribute, the profile's database connection options.
    """

    @property
    def name(self) -> str: ...

    @property
    def alias(self) -> str: ...

    @property
    def params(self) -> Mapping[str, Any]: ...

    @property
    def connect(self) -> Callable[[Profile], Any] | None: ...


@dataclass(frozen=True)
class Capabilities:
    """What a driver supports.

    ``operations`` and ``constructs`` are the names `Operation` reports, ``connection_options`` the options
    ``set_options()`` accepts and ``backend_events`` the `BackendEvent` names the driver emits. ``custom_options``
    tells whether ``set_options()`` also accepts custom options (`is_custom_option()`, names
    such as ``app.user_id``); it defaults to ``False``. ``savepoints`` and
    ``cancellation`` tell whether the driver implements savepoints and cancels a running statement; both default
    to ``False``, so a driver declares only what it implements. ``max_channel_length`` is the longest notification
    channel name in bytes of UTF-8 the backend accepts, ``None`` (the default) for no limit.
    """

    operations: frozenset[str] = frozenset()
    constructs: frozenset[str] = frozenset()
    connection_options: frozenset[str] = frozenset()
    backend_events: frozenset[str] = frozenset()
    savepoints: bool = False
    cancellation: bool = False
    max_channel_length: int | None = None
    custom_options: bool = False

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if value is None or isinstance(value, (bool, int)):
                continue
            if isinstance(value, str):
                raise TypeError(f"{field.name} must be an iterable of names, not a string")
            object.__setattr__(self, field.name, frozenset(value))

    def unsupported(self, operation: Operation) -> list[str]:
        """Names ``operation`` uses that the driver does not support: its kind first, then sorted constructs."""
        missing = [] if operation.kind in self.operations else [operation.kind]
        return missing + sorted(set(operation.constructs()) - self.constructs)

    def check(self, operation: Operation, driver: str | None = None) -> None:
        """Raise `NotSupportedError` when the driver does not support ``operation`` or a construct it uses.

        ``driver`` is the driver's name, which the message and `NotSupportedError.driver` carry.
        """
        missing = self.unsupported(operation)
        if missing:
            raise NotSupportedError(
                f"{operation.kind!r} operation needs unsupported {', '.join(missing)}{_on(driver)}",
                driver=driver,
                unsupported=missing,
            )

    def check_savepoints(self, driver: str | None = None) -> None:
        """Raise `NotSupportedError` when the driver does not implement savepoints."""
        if not self.savepoints:
            raise NotSupportedError(
                f"savepoints are not supported{_on(driver)}", driver=driver, unsupported=["savepoints"]
            )

    def check_backend_event(self, name: str, driver: str | None = None) -> None:
        """Raise `NotSupportedError` when the driver does not declare the `BackendEvent` name ``name``."""
        if name not in self.backend_events:
            raise NotSupportedError(
                f"the backend event {name!r} is not supported{_on(driver)}", driver=driver, unsupported=[name]
            )

    def check_channel(self, channel: Any) -> str:
        """Return ``channel`` when it is a valid channel name (`check_channel()`) of at most ``max_channel_length``
        bytes in UTF-8; raise `TypeError` or `ValueError` otherwise."""
        check_channel(channel)
        limit = self.max_channel_length
        if limit is not None and len(channel.encode()) > limit:
            raise ValueError(f"the channel name {channel!r} is longer than {limit} bytes")
        return channel

    def check_options(self, options: Iterable[str], driver: str | None = None) -> None:
        """Raise `NotSupportedError` naming every option the driver does not support."""
        missing = sorted(
            name
            for name in set(options) - self.connection_options
            if not (self.custom_options and isinstance(name, str) and is_custom_option(name))
        )
        if missing:
            raise NotSupportedError(
                f"unsupported connection options: {', '.join(missing)}{_on(driver)}", driver=driver, unsupported=missing
            )


_CUSTOM_OPTION = re.compile(r"[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)+")


def is_custom_option(name: str) -> bool:
    """Whether ``name`` is a custom connection option: lowercase ASCII identifiers joined by dots, such as
    ``app.user_id`` (PostgreSQL's customized options)."""
    return _CUSTOM_OPTION.fullmatch(name) is not None


def _on(driver: str | None) -> str:
    return "" if driver is None else f" (driver {driver!r})"


_schema_hashes_lock = threading.Lock()

_QUERY_CANCELED = "57014"
_CONNECTION_LOST_SQLSTATES = frozenset({"57P01", "57P02", "57P03"})


def check_channel(channel: Any) -> str:
    """Return ``channel``, a notification channel name: a non-empty string without NUL; raise `TypeError` or
    `ValueError` otherwise. The name is used exactly as given: ``"Orders"`` and ``"orders"`` are different channels.
    A driver's length limit is checked by `Capabilities.check_channel()`."""
    if not isinstance(channel, str):
        raise TypeError(f"a channel name is a string, not {type(channel).__name__}")
    if not channel or "\x00" in channel:
        raise ValueError(f"a channel name is a non-empty string without NUL, not {channel!r}")
    return channel


def is_connection_error(exc: BaseException) -> bool:
    """Whether ``exc``, an adorm exception, leaves a PostgreSQL connection unusable.

    An `adorm.exceptions.Error` whose ``connection_lost`` a connection set gives that value. Otherwise true for
    `InterfaceError`, for `OperationalError` without a SQLSTATE or with a connection exception (class 08)
    or shutdown SQLSTATE (57P01-57P03), and for exceptions that are not an `Exception` except
    ``asyncio.CancelledError``. It is the default of `DriverConnection.is_connection_lost`.
    """
    if isinstance(exc, asyncio.CancelledError):
        return False
    judged = getattr(exc, "connection_lost", None) if isinstance(exc, Error) else None
    if judged is not None:
        return judged
    if not isinstance(exc, Exception) or isinstance(exc, InterfaceError):
        return True
    if isinstance(exc, OperationalError):
        return exc.sqlstate is None or exc.sqlstate.startswith("08") or exc.sqlstate in _CONNECTION_LOST_SQLSTATES
    return False


def is_refusal(exc: BaseException) -> bool:
    """Whether ``exc``, an adorm exception raised while opening a connection, means the database refuses more
    connections: an `OperationalError` with a SQLSTATE of class 53 (insufficient resources, such as 53300
    ``too_many_connections``). It is the default of `Driver.is_refusal`."""
    return isinstance(exc, OperationalError) and exc.sqlstate is not None and exc.sqlstate.startswith("53")


def _check_keys(operation: Operation) -> None:
    from adorm.query import check_lookups, F, Query

    query = getattr(operation, "query", None)
    if not isinstance(query, Query):
        return
    if query.where:
        check_lookups(query, query.where)
    annotations = {name for name, _ in query.annotations}
    for _, expression in query.annotations:
        check_lookups(query, expression)
    for order in query.ordering:
        check_lookups(query, order)
    for path in (*query.select, *(query.group_by or ()), *query.distinct_fields):
        if path not in annotations:
            check_lookups(query, F(path))


def operation_models(operation: Any) -> tuple[str, ...]:
    """The labels of the models whose tables ``operation`` reads or writes: its ``model``, the model of every query
    inside it (subqueries, ``pk__in=queryset``, the parts of a ``union()``) and every model a relation path of those
    queries reaches (lookups and ``F()`` references of filters, annotations, ordering and written values,
    ``values()``, ``only()``, ``defer()``, ``distinct()`` and ``select_related()`` paths), as far as the model
    registry resolves them. A path of an ``OuterRef()`` is resolved in the query it refers to. Empty for an operation
    without a ``model``."""
    from adorm.models.registry import registry
    from adorm.query.expressions import F, OuterRef
    from adorm.query.query import Query

    label = getattr(operation, "model", None)
    if not isinstance(label, str):
        return ()
    labels = [label]

    def add(start: str, path: str) -> None:
        current: str | None = start
        for segment in path.split("__"):
            schema = registry.model_schema(current) if current is not None else None
            current = None if schema is None else schema.relations.get(segment)
            if current is None:
                return
            if current not in labels:
                labels.append(current)

    # (node, the models of the enclosing queries, innermost last)
    nodes = getattr(operation, "nodes", None)
    pending: list[tuple[Any, tuple[str, ...]]] = [(node, (label,)) for node in (nodes() if callable(nodes) else ())]
    while pending:
        node, owners = pending.pop()
        if isinstance(node, Query):
            if node.model not in labels:
                labels.append(node.model)
            owners = (*owners, node.model)
            paths = (*node.select, *node.distinct_fields, *(node.only_fields or ()), *node.deferred_fields)
            if isinstance(node.select_related_fields, tuple):
                paths += node.select_related_fields
            for path in paths:
                add(node.model, path)
        elif isinstance(node, OuterRef):
            if len(owners) > 1:
                add(owners[-2], node.name)
            continue
        elif isinstance(node, F):
            add(owners[-1], node.name)
            continue
        pending.extend((child, owners) for child in node.get_source_expressions())
    return tuple(labels)


def _registered_schema_hash(label: str) -> str | None:
    from adorm.models.registry import registry

    try:
        meta = registry.get(label)._meta
    except LookupError:
        return None
    return None if meta is None else meta.schema_hash


def _check_call(operation: Operation) -> None:
    from adorm.models.managers import find_driver_method
    from adorm.operations import Call

    if not isinstance(operation, Call):
        raise NotSupportedError(f"a 'call' operation is an adorm.operations.Call, not {type(operation).__name__}")
    find_driver_method(operation.model, operation.manager, operation.target, operation.method)


class DriverConnection(ABC):
    """One connection of a driver, owned by exactly one slot.

    The slot awaits one call at a time from one event loop. Methods raise adorm exceptions only;
    ``asyncio.CancelledError`` passes through. Cancelling the task that awaits ``execute()`` cancels the running
    statement; the connection stays usable. A slot whose lease ends inside a transaction closes the
    connection, as does one whose request failed with an error ``is_connection_lost()`` accepts and one whose
    connection ran custom SQL (``mark_custom_sql()``).

    ``execute()``, ``set_options()`` and the savepoint calls check the driver's `Capabilities` and raise
    `NotSupportedError` before calling the subclass's ``_execute()``, ``_set_options()`` and ``_savepoint()``
    family.
    """

    def __init__(self, driver: Driver) -> None:
        self.driver = driver
        self._custom_sql = False
        self._listening: frozenset[str] = frozenset()

    @property
    def capabilities(self) -> Capabilities:
        """What the connection supports: `Driver.capabilities_for()` of its ``profile`` attribute, the driver's
        ``capabilities`` for a connection without one."""
        profile = getattr(self, "profile", None)
        return self.driver.capabilities if profile is None else self.driver.capabilities_for(profile)

    async def execute(self, operation: Operation) -> Any:
        """Execute ``operation``; return plain data: rows as dicts keyed by adorm field names, a scalar or a count.

        `Driver.check()` with `capabilities` runs first, so an unsupported operation never reaches ``_execute()``.
        """
        self.driver.check(operation, capabilities=self.capabilities)
        return await self._execute(operation)

    async def savepoint(self, name: str) -> None:
        """Create the savepoint ``name`` in the open transaction."""
        self.capabilities.check_savepoints(self.driver.name)
        await self._savepoint(name)

    async def release_savepoint(self, name: str) -> None:
        """Release the savepoint ``name``."""
        self.capabilities.check_savepoints(self.driver.name)
        await self._release_savepoint(name)

    async def rollback_to_savepoint(self, name: str) -> None:
        """Roll back to the savepoint ``name``."""
        self.capabilities.check_savepoints(self.driver.name)
        await self._rollback_to_savepoint(name)

    async def set_options(self, options: Mapping[str, Any], local: bool) -> None:
        """Set connection options; ``local`` limits them to the open transaction (``SET LOCAL``). A value of ``None``
        resets the option to its default."""
        self.capabilities.check_options(options, self.driver.name)
        await self._set_options(dict(options), local)

    async def listen(self, channel: str) -> None:
        """Listen for notifications on ``channel`` (`Capabilities.check_channel()`): each one the database delivers is
        emitted as ``BackendEvent(NOTIFY, channel, payload)``, also while no call runs. Raises `NotSupportedError`
        unless the driver declares the backend event `NOTIFY`. The channel counts in ``listening`` from the start of
        the call, and stays there when the call is cancelled."""
        self.capabilities.check_channel(channel)
        self.capabilities.check_backend_event(NOTIFY, self.driver.name)
        listening = self._listening
        self._listening = listening | {channel}
        try:
            await self._listen(channel)
        except asyncio.CancelledError:
            raise
        except BaseException:
            if channel not in listening:
                self._listening = self._listening - {channel}
            raise

    async def unlisten(self, channel: str) -> None:
        """Stop listening on ``channel``; nothing happens for a channel the connection does not listen on. A
        notification already received may still be emitted afterwards."""
        self.capabilities.check_channel(channel)
        if channel not in self._listening:
            return
        await self._unlisten(channel)
        self._listening = self._listening - {channel}

    @property
    def listening(self) -> frozenset[str]:
        """The channels the connection listens on (`listen()`). The slot closes a connection that still listens when
        its lease ends."""
        return getattr(self, "_listening", frozenset())

    @property
    def target(self) -> Any:
        """The target's connection that profile hooks receive. Defaults to the driver connection itself."""
        return self

    @property
    def ran_custom_sql(self) -> bool:
        """Whether the connection ran SQL that adorm did not generate (`mark_custom_sql()`)."""
        return getattr(self, "_custom_sql", False)

    def mark_custom_sql(self) -> None:
        """Record that the connection ran SQL that adorm did not generate, such as SQL of code the target runs on the
        driver side; such SQL may have changed the connection's state. The slot closes the connection when its lease
        ends, also after a commit or rollback. Callable from any thread; there is no way to clear the mark."""
        self._custom_sql = True

    @property
    def settled(self) -> bool:
        """Whether the connection has finished the work of its last call that runs after the call's outcome
        (`ThreadedConnection.after_call_sync()`). Always ``True`` unless overridden."""
        return True

    async def settle(self) -> None:  # noqa: B027 - optional hook
        """Wait until the connection is ``settled``. The slot awaits it before it decides whether the connection stays
        in the pool; a lease's release does not wait for it. Returns at once unless overridden."""

    async def call_hook(self, hook: Callable[..., Any], *args: Any) -> Any:
        """Call ``hook(self.target, *args)``, await its result when it is awaitable, and return it.

        An exception the hook raises is mapped with ``map_error()``.
        """
        try:
            result = hook(self.target, *args)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            mapped = self.map_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc
        return result

    @abstractmethod
    async def _execute(self, operation: Operation) -> Any:
        """Execute a supported ``operation``."""

    async def _savepoint(self, name: str) -> None:  # noqa: ARG002 - subclasses use it
        """Create a savepoint. Raises `NotSupportedError` unless overridden."""
        raise NotSupportedError("savepoints are not supported")

    async def _release_savepoint(self, name: str) -> None:  # noqa: ARG002 - subclasses use it
        """Release a savepoint. Raises `NotSupportedError` unless overridden."""
        raise NotSupportedError("savepoints are not supported")

    async def _rollback_to_savepoint(self, name: str) -> None:  # noqa: ARG002 - subclasses use it
        """Roll back to a savepoint. Raises `NotSupportedError` unless overridden."""
        raise NotSupportedError("savepoints are not supported")

    async def _set_options(self, options: dict[str, Any], local: bool) -> None:  # noqa: ARG002 - subclasses use it
        """Set supported connection options, ``None`` values to their default. Raises `NotSupportedError` for any
        option unless overridden."""
        if options:
            raise NotSupportedError(f"unsupported connection options: {', '.join(sorted(options))}")

    async def _listen(self, channel: str) -> None:  # noqa: ARG002 - subclasses use it
        """Start listening on ``channel``. Raises `NotSupportedError` unless overridden."""
        raise NotSupportedError(f"the {self.driver.name} driver does not listen for notifications")

    async def _unlisten(self, channel: str) -> None:  # noqa: ARG002 - subclasses use it
        """Stop listening on ``channel``. Raises `NotSupportedError` unless overridden."""
        raise NotSupportedError(f"the {self.driver.name} driver does not listen for notifications")

    @abstractmethod
    async def begin(self) -> None:
        """Start a transaction."""

    @abstractmethod
    async def commit(self) -> None:
        """Commit the open transaction."""

    @abstractmethod
    async def rollback(self) -> None:
        """Roll back the open transaction."""

    @abstractmethod
    def cancel(self) -> None:
        """Cancel the running statement, if any. Callable from any thread; returns without waiting for the call."""

    @abstractmethod
    def is_usable(self) -> bool:
        """Whether the connection can serve further calls. Does no I/O."""

    @abstractmethod
    async def close(self) -> None:
        """Close the connection. Logs errors instead of raising them; a second call does nothing."""

    def map_error(self, exc: BaseException) -> BaseException:
        """Return the adorm exception for ``exc``, raised by the target. Defaults to `wrap_driver_error`."""
        return wrap_driver_error(exc)

    @property
    def backend_pid(self) -> int | None:
        """The process id of the connection's session on the database server (PostgreSQL's ``pg_backend_pid()``), as
        `DatabaseMonitor.activity()` takes it; ``None`` (the default) when the driver does not know it. Does no I/O."""
        return None

    @property
    def holds_session(self) -> bool:
        """Whether the running call has the connection's database session now: false while it waits for a session it
        shares with other connections. ``True`` by default. Does no I/O; callable from any thread."""
        return True

    @property
    def session_holds(self) -> int:
        """How many times the connection has taken a database session it shares with other connections; ``0`` (the
        default) for a connection with a session of its own. Callable from any thread."""
        return 0

    def is_connection_lost(self, exc: BaseException) -> bool:
        """Whether ``exc``, a mapped adorm exception, leaves this connection unusable.

        Defaults to `is_connection_error`, which follows PostgreSQL's SQLSTATEs; a driver for a target that reports
        statement errors as `OperationalError` without a SQLSTATE overrides it.
        """
        return is_connection_error(exc)

    def is_cancellation(self, exc: BaseException) -> bool:
        """Whether ``exc``, a mapped adorm exception, is the error of a statement the target cancelled. Defaults to
        PostgreSQL's SQLSTATE ``57014`` (``query_canceled``)."""
        return getattr(exc, "sqlstate", None) == _QUERY_CANCELED


class Driver(ABC):
    """A driver: its ``name``, execution ``mode``, `Capabilities` and the factory of its connections."""

    name: ClassVar[str]
    mode: ClassVar[ExecutionMode]
    capabilities: Capabilities

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        mode = cls.__dict__.get("mode")
        if mode is not None and mode not in _MODES:
            raise TypeError(f"{cls.__name__}.mode must be one of {_MODES}, not {mode!r}")

    def capabilities_for(self, profile: Profile) -> Capabilities:  # noqa: ARG002 - subclasses use it
        """What the driver supports on connections for ``profile``: `capabilities` unless overridden, for a driver
        whose support depends on the database a profile names. Does no I/O."""
        return self.capabilities

    def check(
        self,
        operation: Operation,
        *,
        schema_hashes: Mapping[str, str | None] | None = None,
        capabilities: Capabilities | None = None,
    ) -> None:
        """Raise `NotSupportedError` when this driver does not support ``operation``: its kind or a construct it uses
        is missing from ``capabilities`` (default `capabilities`; a pool passes those of its profile), or it is an
        `adorm.operations.Call` of a method the registered model does not declare (`adorm.models.find_driver_method()`).
        Does no I/O.

        When a construct is missing and a lookup or field path of the operation's query (its filters, annotations,
        ordering, ``values()``, grouping and ``distinct()`` fields) does not resolve against the model registry
        (`adorm.query.check_lookups()`), raises that `adorm.FieldError` instead. Then compares the schema hash the
        client expects of every model of `operation_models()` with the driver's (`check_schema_hash()`), which raises
        `SchemaMismatchError`. ``schema_hashes`` holds the client's hashes by label, as a client sends them when it
        connects (`adorm.models.ModelRegistry.schema_hashes()`); ``None`` takes them from the model registry of this
        process. A label it does not hold is not checked.
        """
        try:
            (self.capabilities if capabilities is None else capabilities).check(operation, self.name)
        except NotSupportedError:
            _check_keys(operation)
            raise
        if operation.kind == "call":
            _check_call(operation)
        expected = _registered_schema_hash if schema_hashes is None else schema_hashes.get
        for label in operation_models(operation):
            self.check_schema_hash(label, expected(label))

    def schema_hash(self, label: str) -> str | None:  # noqa: ARG002 - subclasses use it
        """The schema hash of the model ``label`` as the driver's target defines it, computed as `adorm generate`
        computes the hash it writes into a generated model's `adorm.ModelMeta`. ``None`` (the default) when the driver
        does not compute schema hashes: its models are not checked. Raises ``LookupError`` when the target has no model
        ``label``. Does no I/O."""
        return None

    def check_schema_hash(self, label: str, expected: str | None) -> None:
        """Raise `SchemaMismatchError` when ``expected``, the schema hash the client's model ``label`` was generated
        with, differs from `schema_hash()` of ``label``, or the target has no such model or its hash cannot be computed
        (the error's ``reason`` says which). An ``expected`` of ``None`` and a driver whose `schema_hash()` is ``None``
        pass. The driver's hash of a label is computed once and kept, an error of `schema_hash()` too; callable from
        any thread. Does no I/O."""
        if expected is None:
            return
        actual, reason = self._target_schema_hash(label)
        if actual == expected or (actual is None and reason is None):
            return
        if reason is None:
            message = (
                f"the adorm model {label} was generated with schema hash {expected}, the {self.name} driver's model has"
                f" {actual}: run adorm generate"
            )
        else:
            message = f"the adorm model {label} cannot be checked against the {self.name} driver: {reason}"
        raise SchemaMismatchError(
            message, model=label, expected=expected, actual=actual, driver=self.name, reason=reason
        )

    def _target_schema_hash(self, label: str) -> tuple[str | None, str | None]:
        """`schema_hash()` of ``label``, kept per driver: ``(hash, None)``, or ``(None, reason)`` when it failed."""
        known = self.__dict__.get("_schema_hashes")
        if known is not None and label in known:
            return known[label]
        with _schema_hashes_lock:
            known = self.__dict__.setdefault("_schema_hashes", {})
            if label not in known:
                try:
                    known[label] = (self.schema_hash(label), None)
                except LookupError:
                    known[label] = (None, f"the target has no model {label}")
                except Exception as exc:  # noqa: BLE001 - reported as the reason of the mismatch
                    known[label] = (None, f"{type(exc).__name__}: {exc}")
            return known[label]

    async def check_listen(self, profile: Profile) -> None:
        """Raise `NotSupportedError` when a connection for ``profile`` cannot listen for notifications. Defaults to
        checking that `capabilities_for()` ``profile`` declares the backend event `NOTIFY`. Opens no connection."""
        self.capabilities_for(profile).check_backend_event(NOTIFY, self.name)

    def is_refusal(self, exc: BaseException) -> bool:
        """Whether ``exc``, raised by `open_connection()`, means the database refuses more connections.

        A pool with ``max_connections=0`` stops opening connections on a refusal and queues the request instead.
        Defaults to `is_refusal`, which needs a SQLSTATE; a driver whose library reports connection failures
        without one overrides it.
        """
        return is_refusal(exc)

    def connection_defaults(self, profile: Profile) -> Mapping[str, Any]:  # noqa: ARG002 - subclasses use it
        """The database connection options the driver sets on every new connection for ``profile``, such as a role
        from its settings; the pool starts the profile's layer of options with them. Empty unless overridden. Does
        no I/O."""
        return {}

    def database_key(self, profile: Profile) -> Any:  # noqa: ARG002 - subclasses use it
        """A hashable value naming the database ``profile`` connects to, or ``None`` (the default) when the driver
        cannot tell. Profiles with equal keys share what adorm caches per database (`adorm.content_types`); a profile
        whose key is ``None`` has a cache of its own. Does no I/O."""
        return None

    async def transaction_turns(self, profile: Profile) -> TransactionTurns | None:  # noqa: ARG002 - subclasses use it
        """The `TransactionTurns` of the database session that the connections for ``profile`` share, when they share
        one; ``None`` (the default) when each connection is a session of its own. The router takes a turn on it before
        it acquires a slot for a transaction or an operation outside a transaction."""
        return None

    @abstractmethod
    async def open_connection(self, profile: Profile, emit: Emit) -> DriverConnection:
        """Open a connection for ``profile``; the connection reports events through ``emit``.

        When ``profile.connect`` is set, the driver opens the target's connection with it instead of its default.
        """

    async def open_monitor(self, profile: Profile) -> DatabaseMonitor:  # noqa: ARG002 - subclasses use it
        """Open a `DatabaseMonitor` on a connection of its own to ``profile``'s database, outside every pool. Raises
        `NotSupportedError` unless overridden."""
        raise NotSupportedError(f"the {self.name} driver cannot monitor database sessions")


@dataclass(frozen=True, slots=True)
class BackendActivity:
    """What the database reports about one session (PostgreSQL's ``pg_stat_activity``).

    ``state`` is the session's state (``"active"``, ``"idle"``, ``"idle in transaction"`` …), ``wait_event_type`` and
    ``wait_event`` what it waits for (``"Lock"`` and ``"transactionid"`` for a row lock), ``None`` when it waits for
    nothing; ``blocking_pids`` are the sessions holding the locks it waits for (``pg_blocking_pids()``).
    """

    pid: int
    state: str | None = None
    wait_event_type: str | None = None
    wait_event: str | None = None
    blocking_pids: tuple[int, ...] = ()

    @property
    def waiting_for_lock(self) -> bool:
        """Whether the session waits for a heavyweight lock: a row, a table, a transaction id, an advisory lock."""
        return self.wait_event_type == "Lock"


class DatabaseMonitor(ABC):
    """A connection of its own that reads what the database reports about other sessions (`Driver.open_monitor()`).

    Used from one event loop; an ``async with`` block closes it.
    """

    @abstractmethod
    async def activity(self, pids: Iterable[int]) -> dict[int, BackendActivity]:
        """The `BackendActivity` of each session in ``pids`` (`DriverConnection.backend_pid` values), read now; a pid
        without a session is left out."""

    @abstractmethod
    async def close(self) -> None:
        """Close the monitor's connection. Logs errors instead of raising them; a second call does nothing."""

    async def __aenter__(self) -> DatabaseMonitor:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()


class _State(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    CANCELLED = "cancelled"


class _Call:
    __slots__ = ("args", "context", "fn", "future", "state")

    def __init__(self, fn: Callable[..., Any], args: tuple[Any, ...], future: asyncio.Future | None) -> None:
        self.fn = fn
        self.args = args
        self.future = future
        self.context = contextvars.copy_context()
        self.state = _State.PENDING


def _resolve(future: asyncio.Future, ok: bool, value: Any) -> None:
    if future.done():
        return
    if ok:
        future.set_result(value)
    else:
        future.set_exception(value)


def _finished_uncancelled(future: asyncio.Future, connection: DriverConnection) -> bool:
    """Whether the cancelled call behind ``future`` finished with a result, or an error other than its cancellation
    (`DriverConnection.is_cancellation()`)."""
    if not future.done() or future.cancelled():
        return False
    error = future.exception()
    return error is None or not connection.is_cancellation(error)


def _consume(future: asyncio.Future) -> None:
    if not future.cancelled():
        future.exception()


_thread_ids = itertools.count(1)


class ThreadedDriver(Driver):
    """Base class of drivers for a synchronous ORM: each connection runs on its own thread.

    Subclasses set ``connection_class``. ``cancel_grace`` is how many seconds a cancelled call may take to finish
    before its connection is given up. With ``direct`` set, the ``*_sync`` methods run on the event loop's thread
    instead (one task per event loop, e.g. a Celery task); each call blocks the loop while it runs.
    """

    mode: ClassVar[ExecutionMode] = "threaded"
    connection_class: ClassVar[type[ThreadedConnection]]
    cancel_grace: float = 5.0
    direct: bool = False

    async def open_connection(self, profile: Profile, emit: Emit) -> ThreadedConnection:
        connection = self.connection_class(self, profile, emit)
        await connection.start()
        return connection


class ThreadedConnection(DriverConnection):
    """A connection whose synchronous ``*_sync`` methods all run on one thread.

    The thread is the connection's own, or the event loop's thread when the driver is ``direct``. Each ``*_sync``
    call runs in a copy of the awaiting task's context. Errors are mapped with ``map_error()`` on that thread.
    ``cancel_sync()`` runs on another thread while a call is running, and no further call starts until it
    returns. ``emit()`` may be called from any thread. Work a call hands to ``after_call_sync()`` runs once the call's
    outcome is delivered, before the next call. While no call runs, the thread watches ``idle_fileno()`` and runs
    ``idle_sync()`` when it becomes readable (for a ``direct`` driver, the event loop watches it).
    """

    driver: ThreadedDriver

    def __init__(self, driver: ThreadedDriver, profile: Profile, emit: Emit) -> None:
        super().__init__(driver)
        self.profile = profile
        self._emit = emit
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        self._direct = driver.direct
        self._lock = threading.Lock()
        self._current: _Call | None = None
        self._inline_events: list[Event] | None = None
        self._after: list[Callable[[], Any]] = []
        self._usable = True
        self._connected = False
        self._stuck = False
        self._closed = False
        self._settling: concurrent.futures.Future[None] | None = None
        self._requests: queue.SimpleQueue[_Call | None] = queue.SimpleQueue()
        # Watching idle_fileno(): a wake-up socket pair, the selector and the watched descriptor.
        self._waker: tuple[socket.socket, socket.socket] | None = None
        self._selector: selectors.BaseSelector | None = None
        self._watched: int | None = None
        self._idle_waiting = False
        if self._direct:
            self._thread = threading.current_thread()
        else:
            self._thread = threading.Thread(
                target=self._serve, name=f"adorm-{driver.name}-{next(_thread_ids)}", daemon=True
            )

    @property
    def thread(self) -> threading.Thread:
        """The thread every ``*_sync`` method runs on."""
        return self._thread

    # Synchronous implementation, run on the connection's thread.

    @abstractmethod
    def connect_sync(self) -> None:
        """Open the target's connection for ``self.profile``, with ``self.profile.connect`` when it is set."""

    @abstractmethod
    def execute_sync(self, operation: Operation) -> Any:
        """Execute ``operation`` and return plain data."""

    @abstractmethod
    def begin_sync(self) -> None:
        """Start a transaction."""

    @abstractmethod
    def commit_sync(self) -> None:
        """Commit the open transaction."""

    @abstractmethod
    def rollback_sync(self) -> None:
        """Roll back the open transaction."""

    @abstractmethod
    def close_sync(self) -> None:
        """Close the target's connection."""

    def savepoint_sync(self, name: str) -> None:  # noqa: ARG002 - subclasses use it
        """Create a savepoint. Raises `NotSupportedError` unless overridden."""
        raise NotSupportedError("savepoints are not supported")

    def release_savepoint_sync(self, name: str) -> None:  # noqa: ARG002 - subclasses use it
        """Release a savepoint. Raises `NotSupportedError` unless overridden."""
        raise NotSupportedError("savepoints are not supported")

    def rollback_to_savepoint_sync(self, name: str) -> None:  # noqa: ARG002 - subclasses use it
        """Roll back to a savepoint. Raises `NotSupportedError` unless overridden."""
        raise NotSupportedError("savepoints are not supported")

    def set_options_sync(self, options: Mapping[str, Any], local: bool) -> None:  # noqa: ARG002 - subclasses use it
        """Set connection options, ``None`` values to their default. Raises `NotSupportedError` for any option unless
        overridden."""
        if options:
            raise NotSupportedError(f"unsupported connection options: {', '.join(sorted(options))}")

    def listen_sync(self, channel: str) -> None:  # noqa: ARG002 - subclasses use it
        """Start listening on ``channel``. Raises `NotSupportedError` unless overridden."""
        raise NotSupportedError(f"the {self.driver.name} driver does not listen for notifications")

    def unlisten_sync(self, channel: str) -> None:  # noqa: ARG002 - subclasses use it
        """Stop listening on ``channel``. Raises `NotSupportedError` unless overridden."""
        raise NotSupportedError(f"the {self.driver.name} driver does not listen for notifications")

    def idle_fileno(self) -> int | None:
        """A file descriptor to watch while no call runs, ``None`` (the default) for none. Asked on the connection's
        thread after every call once ``connect_sync()`` has succeeded; it is not watched once the connection is closed
        or unusable."""
        return None

    def idle_sync(self) -> None:  # noqa: B027 - optional hook
        """Read what arrived on ``idle_fileno()``, such as notifications; runs on the connection's thread between
        calls, when the descriptor is readable. An exception it raises makes the connection unusable and is emitted as
        ``BackendEvent(CONNECTION_LOST, payload=<the mapped error's text>)``. Does nothing unless overridden."""

    def prepare_call_sync(self) -> None:  # noqa: B027 - optional hook
        """Prepare the call's context; runs on the connection's thread, in the call's copy of the caller's context,
        before every ``*_sync`` call and hook. An exception it raises is the call's. Does nothing unless overridden."""

    def cancel_sync(self) -> None:  # noqa: B027 - optional hook
        """Ask the target to cancel the running statement; runs on another thread. Does nothing unless overridden.

        It returns once the request is delivered, so that it cannot reach a later statement, and never waits for
        the running call to finish. It may run while ``connect_sync()`` runs.
        """

    def after_call_sync(self, fn: Callable[[], Any]) -> None:
        """Run ``fn()`` on the connection's thread once the running call has ended and its outcome is delivered,
        before the next call starts. It runs in the call's context, ``cancel()`` never reaches it, and an exception it
        raises is logged. Called from a ``*_sync`` method."""
        self._after.append(fn)

    # The contract.

    async def start(self) -> None:
        """Run ``connect_sync()`` on the connection's thread; on failure run ``close_sync()`` and stop the thread."""
        if not self._direct:
            self._thread.start()
        try:
            await self._call(self.connect_sync)
        except BaseException:
            self._closed = True
            self._shut_down()
            raise
        if self._direct:
            self._update_reader()

    async def _execute(self, operation: Operation) -> Any:
        return await self._request(self.execute_sync, operation)

    async def begin(self) -> None:
        await self._request(self.begin_sync)

    async def _savepoint(self, name: str) -> None:
        await self._request(self.savepoint_sync, name)

    async def _release_savepoint(self, name: str) -> None:
        await self._request(self.release_savepoint_sync, name)

    async def _rollback_to_savepoint(self, name: str) -> None:
        await self._request(self.rollback_to_savepoint_sync, name)

    async def commit(self) -> None:
        await self._request(self.commit_sync)

    async def rollback(self) -> None:
        await self._request(self.rollback_sync)

    async def _set_options(self, options: dict[str, Any], local: bool) -> None:
        await self._request(self.set_options_sync, options, local)

    async def _listen(self, channel: str) -> None:
        await self._request(self.listen_sync, channel)

    async def _unlisten(self, channel: str) -> None:
        await self._request(self.unlisten_sync, channel)

    async def call_hook(self, hook: Callable[..., Any], *args: Any) -> Any:
        """Call ``hook(self.target, *args)`` on the connection's thread and return its result. The hook is a plain
        function: one that returns an awaitable raises `InterfaceError`."""
        return await self._call(self._run_hook, hook, args)

    def _run_hook(self, hook: Callable[..., Any], args: tuple[Any, ...]) -> Any:
        result = hook(self.target, *args)
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise InterfaceError(f"hook {hook!r} of a threaded {self.driver.name} connection returned an awaitable")
        return result

    def cancel(self) -> None:
        """Have ``cancel_sync()`` run for the running call, if any.

        On the event loop's thread ``cancel_sync()`` runs on a separate thread; elsewhere on the calling thread.
        """
        call = self._current
        if call is None:
            return
        if threading.get_ident() == self._loop_thread:
            self._cancel_in_background(call)
        else:
            self._cancel_call(call)

    def is_usable(self) -> bool:
        return self._usable and not self._closed and self._thread.is_alive()

    @property
    def settled(self) -> bool:
        """Whether the work the last call handed to ``after_call_sync()`` has run."""
        settling = self._settling
        return settling is None or settling.done()

    async def settle(self) -> None:
        """Wait until the work the last call handed to ``after_call_sync()`` has run on the connection's thread."""
        settling = self._settling
        if settling is not None and not settling.done():
            await asyncio.shield(asyncio.wrap_future(settling))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._direct:
            self._update_reader()
            ok, value = self._run_now(self.close_sync, map_errors=False)
            if not ok:
                logger.error("closing %s connection failed", self.driver.name, exc_info=value)
            return
        if not self._thread.is_alive():
            return
        call = _Call(self.close_sync, (), self._loop.create_future())
        self._put(call)
        self._put(None)
        if self._stuck:
            call.future.add_done_callback(_consume)
            return
        try:
            await asyncio.shield(call.future)
        except asyncio.CancelledError:
            call.future.add_done_callback(_consume)
            raise
        except Exception:
            logger.exception("closing %s connection failed", self.driver.name)

    def _close_detached(self) -> bool:
        """Close the connection without its event loop: queue ``close_sync()`` on the connection's thread and stop
        the thread. Returns whether the close was queued."""
        if self._closed:
            return True
        if self._direct or self._stuck or not self._thread.is_alive():
            return False
        self._closed = True
        self._put(_Call(self.close_sync, (), None))
        self._put(None)
        return True

    def emit(self, event: Event) -> None:
        """Report ``event`` to the slot; callable from any thread.

        The event is delivered on the event loop, before the result of the call that emitted it.
        """
        if self._inline_events is not None and threading.get_ident() == self._loop_thread:
            self._inline_events.append(event)
            return
        try:
            self._loop.call_soon_threadsafe(self._emit, event)
        except RuntimeError:
            logger.warning("event loop closed, dropping %r", event)

    # Bridge between the event loop and the thread.

    async def _request(self, fn: Callable[..., Any], *args: Any) -> Any:
        """``_call()`` for a slot's request: a cancelled call that finished before the cancellation reached it
        returns its result or raises its error; only one the target cancelled (`is_cancellation()`) or one still
        running after ``cancel_grace`` raises ``CancelledError``."""
        return await self._call(fn, *args, report_drained=True)

    async def _call(self, fn: Callable[..., Any], *args: Any, report_drained: bool = False) -> Any:
        if self._closed:
            raise InterfaceError(f"{self.driver.name} connection is closed")
        if self._stuck:
            raise InterfaceError(f"{self.driver.name} connection is still running a cancelled call")
        if self._direct:
            return self._call_inline(fn, *args)
        call = _Call(fn, args, self._loop.create_future())
        self._put(call)
        try:
            return await asyncio.shield(call.future)
        except asyncio.CancelledError:
            with self._lock:
                if call.state is _State.PENDING:
                    call.state = _State.CANCELLED
                    raise
            self._cancel_in_background(call)
            await self._drain(call)
            if not (report_drained and _finished_uncancelled(call.future, self)):
                raise
        return call.future.result()

    def _call_inline(self, fn: Callable[..., Any], *args: Any) -> Any:
        events: list[Event] = []
        self._inline_events = events
        call = _Call(fn, args, None)
        try:
            ok, value = cast("tuple[bool, Any]", self._invoke(call, map_errors=True))
        finally:
            self._inline_events = None
            self._update_reader()
        for event in events:
            try:
                self._emit(event)
            except Exception:
                logger.exception("delivering %r failed", event)
        self._run_after(call.context)
        if ok:
            return value
        raise value

    def _run_after(self, context: contextvars.Context) -> None:
        """Run the work handed to ``after_call_sync()`` in ``context``."""
        after, self._after = self._after, []
        if not after:
            return
        running = asyncio._get_running_loop() if self._direct else None
        if running is not None:
            asyncio._set_running_loop(None)
        try:
            for fn in after:
                try:
                    context.run(fn)
                except Exception:
                    logger.exception("work after a %s call failed", self.driver.name)
        finally:
            if running is not None:
                asyncio._set_running_loop(running)

    def _run_now(self, fn: Callable[..., Any], *args: Any, map_errors: bool) -> tuple[bool, Any]:
        # A new call is never cancelled, so there is always an outcome.
        return cast("tuple[bool, Any]", self._invoke(_Call(fn, args, None), map_errors))

    def _invoke(self, call: _Call, map_errors: bool) -> tuple[bool, Any] | None:
        """Run ``call`` on the current thread; return ``(True, result)``, ``(False, exception)`` or ``None``.

        ``None`` means the call was cancelled before it started.
        """
        with self._lock:
            if call.state is _State.CANCELLED:
                return None
            call.state = _State.RUNNING
            self._current = call
        running = asyncio._get_running_loop() if self._direct else None
        if running is not None:
            # Hidden so that a sync-only check (Django's async_unsafe) accepts the call.
            asyncio._set_running_loop(None)
        try:
            ok, value = True, call.context.run(self._run_prepared, call.fn, call.args)
            if call.fn == self.connect_sync:
                self._connected = True
        except Exception as exc:
            ok, value = False, self._map(exc) if map_errors else exc
        except BaseException as exc:
            if self._direct:
                self._usable = False
                raise
            ok, value = False, self._map(exc) if map_errors else exc
        finally:
            if running is not None:
                asyncio._set_running_loop(running)
            with self._lock:
                call.state = _State.DONE
                self._current = None
        return ok, value

    def _run_prepared(self, fn: Callable[..., Any], args: tuple[Any, ...]) -> Any:
        self.prepare_call_sync()
        return fn(*args)

    def _shut_down(self) -> None:
        """Run ``close_sync()`` and stop the thread after a failed ``connect_sync()``."""
        if self._direct:
            ok, value = self._run_now(self.close_sync, map_errors=False)
            if not ok:
                logger.error("closing %s connection failed", self.driver.name, exc_info=value)
            return
        close = _Call(self.close_sync, (), self._loop.create_future())
        close.future.add_done_callback(_consume)
        self._put(close)
        self._put(None)

    def _cancel_in_background(self, call: _Call) -> None:
        # cancel_sync() may block on I/O; a thread of its own keeps it off the event loop and the shared executor.
        thread = threading.Thread(
            target=self._cancel_call, args=(call,), name=f"adorm-{self.driver.name}-cancel", daemon=True
        )
        try:
            thread.start()
        except RuntimeError:
            self._cancel_call(call)

    def _cancel_call(self, call: _Call) -> None:
        with self._lock:
            # Held while cancel_sync() runs: the thread cannot finish the call and start the next one meanwhile.
            if self._current is call:
                try:
                    self.cancel_sync()
                except Exception:
                    logger.exception("cancelling the running %s call failed", self.driver.name)

    async def _drain(self, call: _Call) -> None:
        deadline = self._loop.time() + self.driver.cancel_grace
        while not call.future.done():
            remaining = deadline - self._loop.time()
            if remaining <= 0:
                self._stuck = True
                self._usable = False
                call.future.add_done_callback(_consume)
                grace = self.driver.cancel_grace
                logger.warning("cancelled %s call did not finish within %g s", self.driver.name, grace)
                return
            try:
                await asyncio.wait({call.future}, timeout=remaining)
            except asyncio.CancelledError:
                continue
        _consume(call.future)

    def _serve(self) -> None:
        try:
            self._serve_calls()
        finally:
            self._stop_watching()

    def _serve_calls(self) -> None:
        while True:
            call = self._next_call()
            if call is None:
                return
            outcome = self._invoke(call, map_errors=True)
            if outcome is None:
                continue
            ok, value = outcome
            settling: concurrent.futures.Future[None] | None = None
            if self._after:
                # Set before the outcome is delivered, so that settle() after the outcome sees it.
                settling = self._settling = concurrent.futures.Future()
            if call.future is None:
                if not ok:
                    logger.error("closing %s connection failed", self.driver.name, exc_info=value)
            else:
                try:
                    self._loop.call_soon_threadsafe(_resolve, call.future, ok, value)
                except RuntimeError:
                    logger.warning("event loop closed, dropping the result of a %s call", self.driver.name)
            try:
                self._run_after(call.context)
            finally:
                if settling is not None:
                    settling.set_result(None)

    # Watching idle_fileno() between calls.

    def _put(self, item: _Call | None) -> None:
        """Queue ``item`` for the thread and wake the thread when it watches ``idle_fileno()``."""
        self._requests.put(item)
        # Read after the put: the thread sets the flag before it checks the queue a last time.
        if self._idle_waiting:
            waker = self._waker
            if waker is not None:
                try:
                    waker[1].send(b"\0")
                except OSError:  # full: wake-ups are pending already
                    pass

    def _watched_fileno(self) -> int | None:
        """``idle_fileno()`` of a connected, usable, open connection; ``None`` when it fails."""
        if not self._connected or self._closed or not self._usable:
            return None
        try:
            return self.idle_fileno()
        except Exception:
            logger.exception("%s.idle_fileno failed", type(self).__name__)
            return None

    def _next_call(self) -> _Call | None:
        """The next queued item; while there is none, run ``idle_sync()`` whenever ``idle_fileno()`` is readable."""
        while True:
            fd = self._watched_fileno()
            if fd is None:
                self._unwatch()
                return self._requests.get()
            try:
                return self._requests.get_nowait()
            except queue.Empty:
                pass
            if self._wait_idle(fd):
                self._run_idle()

    def _wait_idle(self, fd: int) -> bool:
        """Wait until ``fd`` is readable or an item is queued; return whether ``fd`` is readable."""
        selector = self._selector
        if selector is None:
            reader, writer = socket.socketpair()
            reader.setblocking(False)
            writer.setblocking(False)
            selector = selectors.DefaultSelector()
            selector.register(reader, selectors.EVENT_READ, None)
            self._waker, self._selector = (reader, writer), selector
        if self._watched != fd:
            self._unwatch()
            selector.register(fd, selectors.EVENT_READ, fd)
            self._watched = fd
        self._idle_waiting = True
        try:
            if not self._requests.empty():
                return False
            ready = selector.select()
        finally:
            self._idle_waiting = False
        readable = False
        for key, _events in ready:
            if key.data is None:
                try:
                    while cast("socket.socket", key.fileobj).recv(4096):
                        pass
                except OSError:  # drained
                    pass
            else:
                readable = True
        return readable

    def _run_idle(self) -> None:
        """Run ``idle_sync()``; a failure makes the connection unusable and is emitted as `CONNECTION_LOST`."""
        try:
            self.idle_sync()
        except Exception as exc:
            mapped = self._map(exc)
            self._usable = False
            logger.warning("%s connection failed while idle: %s", self.driver.name, mapped)
            self.emit(BackendEvent(CONNECTION_LOST, payload=str(mapped)))

    def _unwatch(self) -> None:
        if self._watched is not None and self._selector is not None:
            try:
                self._selector.unregister(self._watched)
            except (KeyError, ValueError, OSError):
                pass
        self._watched = None

    def _stop_watching(self) -> None:
        self._unwatch()
        selector, self._selector = self._selector, None
        waker, self._waker = self._waker, None
        if selector is not None:
            selector.close()
        if waker is not None:
            for end in waker:
                end.close()

    def _update_reader(self) -> None:
        """For a ``direct`` driver: have the event loop watch ``idle_fileno()`` (`_read_idle()`)."""
        fd = self._watched_fileno()
        if fd == self._watched:
            return
        if self._watched is not None:
            self._loop.remove_reader(self._watched)
        self._watched = fd
        if fd is not None:
            self._loop.add_reader(fd, self._read_idle)

    def _read_idle(self) -> None:
        self._run_idle()
        self._update_reader()

    def _map(self, exc: BaseException) -> BaseException:
        try:
            mapped = self.map_error(exc)
        except Exception:
            logger.exception("%s.map_error failed", type(self).__name__)
            mapped = wrap_driver_error(exc)
        if not isinstance(mapped, Exception):
            # KeyboardInterrupt or SystemExit on the thread must not propagate into the event loop.
            mapped = InterfaceError(f"{self.driver.name} call raised {type(exc).__name__}", original=exc)
            mapped.__cause__ = exc
        try:
            lost = self.is_connection_lost(mapped)
        except Exception:
            logger.exception("%s.is_connection_lost failed", type(self).__name__)
            lost = True
        if lost:
            self._usable = False
        if isinstance(mapped, Error) and mapped.connection_lost is None:
            mapped.connection_lost = lost
        return mapped
