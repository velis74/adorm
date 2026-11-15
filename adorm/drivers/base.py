"""The driver contract.

A driver executes adorm operations on its target (an ORM or a database library). The core hands a driver
connection one operation at a time and gets plain data back; callbacks and backend events travel through the
``emit`` callable the connection receives when it opens. A driver declares what it supports in `Capabilities` and
how it runs in ``mode``. Every error a connection raises is an adorm exception.
"""

from __future__ import annotations

import asyncio
import contextvars
import enum
import inspect
import itertools
import logging
import queue
import threading

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, fields
from typing import Any, cast, ClassVar, Literal, Protocol, runtime_checkable

from adorm.events import Emit, Event
from adorm.exceptions import InterfaceError, NotSupportedError, OperationalError, wrap_driver_error

__all__ = [
    "Capabilities",
    "Driver",
    "DriverConnection",
    "ExecutionMode",
    "Operation",
    "Profile",
    "ThreadedConnection",
    "ThreadedDriver",
    "is_connection_error",
    "is_refusal",
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
    ``hook(connection, profile, session)`` with the connection's `DriverConnection.target`.
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
    ``set_options()`` accepts and ``backend_events`` the `BackendEvent` names the driver emits. ``savepoints`` and
    ``cancellation`` tell whether the driver implements savepoints and cancels a running statement; both default
    to ``False``, so a driver declares only what it implements.
    """

    operations: frozenset[str] = frozenset()
    constructs: frozenset[str] = frozenset()
    connection_options: frozenset[str] = frozenset()
    backend_events: frozenset[str] = frozenset()
    savepoints: bool = False
    cancellation: bool = False

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, bool):
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

    def check_options(self, options: Iterable[str], driver: str | None = None) -> None:
        """Raise `NotSupportedError` naming every option the driver does not support."""
        missing = sorted(set(options) - self.connection_options)
        if missing:
            raise NotSupportedError(
                f"unsupported connection options: {', '.join(missing)}{_on(driver)}", driver=driver, unsupported=missing
            )


def _on(driver: str | None) -> str:
    return "" if driver is None else f" (driver {driver!r})"


_CONNECTION_LOST_SQLSTATES = frozenset({"57P01", "57P02", "57P03"})


def is_connection_error(exc: BaseException) -> bool:
    """Whether ``exc``, an adorm exception, leaves a PostgreSQL connection unusable.

    True for `InterfaceError`, for `OperationalError` without a SQLSTATE or with a connection exception (class 08)
    or shutdown SQLSTATE (57P01-57P03), and for exceptions that are not an `Exception` except
    ``asyncio.CancelledError``. It is the default of `DriverConnection.is_connection_lost`.
    """
    if isinstance(exc, asyncio.CancelledError):
        return False
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
    connection.

    ``execute()``, ``set_options()`` and the savepoint calls check the driver's `Capabilities` and raise
    `NotSupportedError` before calling the subclass's ``_execute()``, ``_set_options()`` and ``_savepoint()``
    family.
    """

    def __init__(self, driver: Driver) -> None:
        self.driver = driver

    async def execute(self, operation: Operation) -> Any:
        """Execute ``operation``; return plain data: rows as dicts keyed by adorm field names, a scalar or a count.

        `Driver.check()` runs first, so an unsupported operation never reaches ``_execute()``.
        """
        self.driver.check(operation)
        return await self._execute(operation)

    async def savepoint(self, name: str) -> None:
        """Create the savepoint ``name`` in the open transaction."""
        self.driver.capabilities.check_savepoints(self.driver.name)
        await self._savepoint(name)

    async def release_savepoint(self, name: str) -> None:
        """Release the savepoint ``name``."""
        self.driver.capabilities.check_savepoints(self.driver.name)
        await self._release_savepoint(name)

    async def rollback_to_savepoint(self, name: str) -> None:
        """Roll back to the savepoint ``name``."""
        self.driver.capabilities.check_savepoints(self.driver.name)
        await self._rollback_to_savepoint(name)

    async def set_options(self, options: Mapping[str, Any], local: bool) -> None:
        """Set connection options; ``local`` limits them to the open transaction (``SET LOCAL``)."""
        self.driver.capabilities.check_options(options, self.driver.name)
        await self._set_options(dict(options), local)

    @property
    def target(self) -> Any:
        """The target's connection that profile hooks receive. Defaults to the driver connection itself."""
        return self

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
        """Set supported connection options. Raises `NotSupportedError` for any option unless overridden."""
        if options:
            raise NotSupportedError(f"unsupported connection options: {', '.join(sorted(options))}")

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

    def is_connection_lost(self, exc: BaseException) -> bool:
        """Whether ``exc``, a mapped adorm exception, leaves this connection unusable.

        Defaults to `is_connection_error`, which follows PostgreSQL's SQLSTATEs; a driver for a target that reports
        statement errors as `OperationalError` without a SQLSTATE overrides it.
        """
        return is_connection_error(exc)


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

    def check(self, operation: Operation) -> None:
        """Raise `NotSupportedError` when this driver does not support ``operation``: its kind or a construct it uses
        is missing from `capabilities`, or it is an `adorm.operations.Call` of a method the registered model does not
        declare (`adorm.models.find_driver_method()`). Does no I/O."""
        self.capabilities.check(operation, self.name)
        if operation.kind == "call":
            _check_call(operation)

    def is_refusal(self, exc: BaseException) -> bool:
        """Whether ``exc``, raised by `open_connection()`, means the database refuses more connections.

        A pool with ``max_connections=0`` stops opening connections on a refusal and queues the request instead.
        Defaults to `is_refusal`, which needs a SQLSTATE; a driver whose library reports connection failures
        without one overrides it.
        """
        return is_refusal(exc)

    @abstractmethod
    async def open_connection(self, profile: Profile, emit: Emit) -> DriverConnection:
        """Open a connection for ``profile``; the connection reports events through ``emit``.

        When ``profile.connect`` is set, the driver opens the target's connection with it instead of its default.
        """


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
    outcome is delivered, before the next call.
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
        self._stuck = False
        self._closed = False
        self._requests: queue.SimpleQueue[_Call | None] = queue.SimpleQueue()
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
        """Set connection options. Raises `NotSupportedError` for any option unless overridden."""
        if options:
            raise NotSupportedError(f"unsupported connection options: {', '.join(sorted(options))}")

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

    async def _execute(self, operation: Operation) -> Any:
        return await self._call(self.execute_sync, operation)

    async def begin(self) -> None:
        await self._call(self.begin_sync)

    async def _savepoint(self, name: str) -> None:
        await self._call(self.savepoint_sync, name)

    async def _release_savepoint(self, name: str) -> None:
        await self._call(self.release_savepoint_sync, name)

    async def _rollback_to_savepoint(self, name: str) -> None:
        await self._call(self.rollback_to_savepoint_sync, name)

    async def commit(self) -> None:
        await self._call(self.commit_sync)

    async def rollback(self) -> None:
        await self._call(self.rollback_sync)

    async def _set_options(self, options: dict[str, Any], local: bool) -> None:
        await self._call(self.set_options_sync, options, local)

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

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._direct:
            ok, value = self._run_now(self.close_sync, map_errors=False)
            if not ok:
                logger.error("closing %s connection failed", self.driver.name, exc_info=value)
            return
        if not self._thread.is_alive():
            return
        call = _Call(self.close_sync, (), self._loop.create_future())
        self._requests.put(call)
        self._requests.put(None)
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
        self._requests.put(_Call(self.close_sync, (), None))
        self._requests.put(None)
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

    async def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        if self._closed:
            raise InterfaceError(f"{self.driver.name} connection is closed")
        if self._stuck:
            raise InterfaceError(f"{self.driver.name} connection is still running a cancelled call")
        if self._direct:
            return self._call_inline(fn, *args)
        call = _Call(fn, args, self._loop.create_future())
        self._requests.put(call)
        try:
            return await asyncio.shield(call.future)
        except asyncio.CancelledError:
            with self._lock:
                if call.state is _State.PENDING:
                    call.state = _State.CANCELLED
                    raise
            self._cancel_in_background(call)
            await self._drain(call)
            raise

    def _call_inline(self, fn: Callable[..., Any], *args: Any) -> Any:
        events: list[Event] = []
        self._inline_events = events
        call = _Call(fn, args, None)
        try:
            ok, value = cast("tuple[bool, Any]", self._invoke(call, map_errors=True))
        finally:
            self._inline_events = None
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
        self._requests.put(close)
        self._requests.put(None)

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
        while True:
            call = self._requests.get()
            if call is None:
                return
            outcome = self._invoke(call, map_errors=True)
            if outcome is None:
                continue
            ok, value = outcome
            if call.future is None:
                if not ok:
                    logger.error("closing %s connection failed", self.driver.name, exc_info=value)
            else:
                try:
                    self._loop.call_soon_threadsafe(_resolve, call.future, ok, value)
                except RuntimeError:
                    logger.warning("event loop closed, dropping the result of a %s call", self.driver.name)
            self._run_after(call.context)

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
        return mapped
