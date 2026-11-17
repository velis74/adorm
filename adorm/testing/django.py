"""Test mode of the Django driver: slots on the test connection of Django's ``TestCase``.

While Django's test environment is set up (``django.test.utils.setup_test_environment()``, which Django's test runner
and pytest-django call), the Django driver looks for the test's connection when it opens a slot's connection. Inside
a ``TestCase`` the slots share that connection; inside a ``TransactionTestCase`` (or outside any test) they open
connections of their own to the process's test database.
"""

from __future__ import annotations

import asyncio
import functools
import sys
import threading
import weakref

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from adorm.drivers.django.driver import _transaction_failed, DjangoConnection, notice_event
from adorm.drivers.django.vendors import PROGRESS_STEPS
from adorm.drivers.sql import set_options_sql
from adorm.events import NOTIFY
from adorm.exceptions import NotSupportedError, OperationalError, TransactionManagementError, TransactionOverlapError

if TYPE_CHECKING:
    from adorm.drivers.base import Profile
    from adorm.drivers.django.driver import DjangoDriver
    from adorm.events import Emit

__all__ = [
    "NO_NOTIFICATIONS",
    "QUERY_CANCELED",
    "SharedConnectionMixin",
    "SharedTestConnection",
    "TestDatabase",
    "django_connection",
    "find_test_database",
    "in_test_environment",
    "open_testcase_atomics",
    "shared_connection_class",
    "shared_test_connection",
]

QUERY_CANCELED = "57014"
"""The SQLSTATE of a call cancelled while it waited for the shared test connection, as of a cancelled statement."""

NO_NOTIFICATIONS = (
    "the shared connection of Django's TestCase receives no notifications: PostgreSQL delivers them when a "
    "transaction commits, and the test's transaction never does; use TransactionTestCase"
)
"""The message of the `NotSupportedError` raised for listening inside a ``TestCase``."""

_STATEMENT_CALLS = frozenset({"execute_sync"})
"""The calls that run with the connection options in effect."""

_UNHELD_CALLS = frozenset({"connect_sync"})
"""The calls that do not use the database connection, so they run without holding it."""


def in_test_environment() -> bool:
    """Whether Django's test environment is set up: ``django.test.utils.setup_test_environment()`` has run and
    ``teardown_test_environment()`` has not."""
    utils = sys.modules.get("django.test.utils")
    state = getattr(utils, "_TestState", None)
    return state is not None and hasattr(state, "saved_data")


def open_testcase_atomics(wrapper: Any) -> tuple[Any, ...]:
    """The atomic blocks that Django's ``TestCase`` entered on the connection wrapper ``wrapper``, outermost first."""
    return tuple(block for block in getattr(wrapper, "atomic_blocks", ()) if getattr(block, "_from_testcase", False))


@dataclass(frozen=True, eq=False)
class TestDatabase:
    """The test's connection wrapper of ``alias`` and the ``TestCase`` atomic blocks open on it when it was found."""

    __test__ = False  # not a pytest test class

    alias: str
    wrapper: Any
    atomics: tuple[Any, ...]

    @property
    def in_test_case(self) -> bool:
        """Whether a ``TestCase`` transaction was open: the slots share the wrapper."""
        return bool(self.atomics)

    def current(self) -> bool:
        """Whether the wrapper's ``TestCase`` atomic blocks are still those found (none for a wrapper found outside a
        ``TestCase``): false after the ``TestCase`` test that found it, and, for one found outside a ``TestCase``, while
        a ``TestCase`` transaction is open."""
        now = open_testcase_atomics(self.wrapper)
        return len(now) == len(self.atomics) and all(a is b for a, b in zip(now, self.atomics, strict=True))


def _wrapper(alias: str) -> Any:
    """The current thread's wrapper of ``alias``: Django's thread-local one, also while an event loop runs here (asgiref
    then gives each task a wrapper of its own)."""
    from django.db import connections

    loop = asyncio._get_running_loop()
    if loop is None:
        return connections[alias]
    asyncio._set_running_loop(None)
    try:
        return connections[alias]
    finally:
        asyncio._set_running_loop(loop)


async def find_test_database(alias: str) -> TestDatabase | None:
    """The `TestDatabase` of the Django alias ``alias``; ``None`` outside Django's test environment
    (`in_test_environment()`) and for an alias without a ``DATABASES`` entry.

    The wrapper is that of the thread that runs the test: the thread that waits in asgiref's ``async_to_sync()``
    for the event loop (Django's async test methods), else the event loop's own thread (pytest-asyncio)."""
    if not in_test_environment():
        return None
    from asgiref.sync import AsyncToSync, sync_to_async
    from django.db import connections

    if alias not in connections.settings:
        return None
    executor = getattr(AsyncToSync.executors, "current", None)
    if executor is None:
        executor = AsyncToSync.loop_thread_executors.get(asyncio.get_running_loop())
    if executor is None:
        wrapper = _wrapper(alias)
    else:
        wrapper = await sync_to_async(_wrapper, thread_sensitive=True)(alias)
    return TestDatabase(alias, wrapper, open_testcase_atomics(wrapper))


class SharedTestConnection:
    """The test connection of a ``TestCase`` as the slots share it.

    One connection at a time runs a call on it (`hold()`). The transactions of the connections are savepoints in the
    test's transaction; ``transactions`` lists the connections with an open one, innermost last. The connection
    options of each call are applied with ``SET LOCAL`` (`apply_options()`), since every statement runs inside the
    test's transaction. ``defaults`` are the options a connection of the alias starts with
    (`Driver.connection_defaults()`, recorded by `add_defaults()`).

    While connections use it (`attach()`), statements that other threads run through the wrapper (Django ORM code of the
    test or of the code under test) hold it too, with ``defaults`` as their connection options, and raise
    `TransactionOverlapError` as an operation outside a transaction does (`check_isolated()`).
    """

    def __init__(self, test: TestDatabase) -> None:
        self.test = test
        self.owner: SharedConnectionMixin | _Foreign | None = None
        self.transactions: list[SharedConnectionMixin] = []
        self._condition = threading.Condition()
        self.defaults: dict[str, Any] = {}
        self._options: dict[str, Any] | None = {}
        self._touched: set[str] = set()
        self._holding = threading.local()
        self._attached = 0
        self._guard_wrapper = self._guard
        self._watched_raw: Any = None

    @property
    def wrapper(self) -> Any:
        """Django's connection wrapper of the test."""
        return self.test.wrapper

    def acquire(self, connection: SharedConnectionMixin | _Foreign) -> None:
        """Wait until no other connection runs a call. Raises `OperationalError` with SQLSTATE `QUERY_CANCELED` when
        ``connection``'s running call is cancelled meanwhile."""
        with self._condition:
            while True:
                cancelled = connection._cancel_waiting
                if cancelled is not None and cancelled is connection._current:
                    connection._cancel_waiting = None
                    raise OperationalError(
                        "canceling statement due to user request while waiting for the shared test connection",
                        sqlstate=QUERY_CANCELED,
                    )
                if self.owner is None:
                    self.owner = connection
                    if isinstance(connection, SharedConnectionMixin):
                        connection._holds += 1
                    return
                self._condition.wait()

    def release(self, connection: SharedConnectionMixin | _Foreign) -> None:
        with self._condition:
            if self.owner is connection:
                self.owner = None
                self._condition.notify_all()

    def watch_backend(self) -> None:
        """Have the test's DB-API connection report its notices to the connection that holds it (psycopg 3), or stop a
        statement of a cancelled call of that connection (SQLite's progress handler)."""
        raw = self.wrapper.connection
        if raw is None or raw is self._watched_raw:
            return
        self._watched_raw = raw
        if hasattr(raw, "add_notice_handler"):
            raw.add_notice_handler(self._on_notice)
        elif self.wrapper.vendor == "sqlite":
            raw.set_progress_handler(self._owner_cancelled, PROGRESS_STEPS)

    def _owner_cancelled(self) -> bool:
        owner = self.owner
        return isinstance(owner, SharedConnectionMixin) and owner._cancelled()  # type: ignore[attr-defined]

    def _on_notice(self, diagnostic: Any) -> None:
        owner = self.owner
        if isinstance(owner, SharedConnectionMixin):
            owner.emit(notice_event(diagnostic))  # type: ignore[attr-defined]

    def hold(self, connection: SharedConnectionMixin | _Foreign) -> _Hold:
        """``with shared.hold(connection):`` runs the block as ``connection``'s call (`acquire()`, `release()`)."""
        return _Hold(self, connection)

    def cancel(self, connection: SharedConnectionMixin, cancel: Callable[[], None]) -> None:
        """Cancel ``connection``'s running call: ``cancel()`` while it runs on the connection, else end its wait."""
        with self._condition:
            if self.owner is connection:
                cancel()  # the owner cannot change meanwhile, so it cannot reach another connection's statement
            else:
                connection._cancel_waiting = connection._current
                self._condition.notify_all()

    def apply_options(self, desired: Mapping[str, Any]) -> None:
        """Set the connection options ``desired`` (values other than ``None``) with ``SET LOCAL``, resetting the
        others set before; only the differences from what is known to be set are sent."""
        current = self._options
        names = desired.keys() | (self._touched if current is None else current.keys())
        changes = {
            name: desired.get(name)
            for name in sorted(names)
            if current is None or current.get(name) != desired.get(name)
        }
        if changes:
            self._options = None
            with self.wrapper.cursor() as cursor:
                cursor.execute(set_options_sql(changes, local=True))
            self._touched |= changes.keys()
        self._options = dict(desired)

    def add_defaults(self, defaults: Mapping[str, Any]) -> None:
        """Record the options a connection of the alias starts with; until a ``SET LOCAL`` is sent they are the
        options known to be set."""
        with self._condition:
            self.defaults.update(defaults)
            if self.owner is None and not self._touched and self._options is not None:
                self._options = dict(self.defaults)

    def attach(self) -> None:
        """Count a connection that uses the shared connection; the first installs the guard of other threads'
        statements as the wrapper's outermost execute wrapper."""
        with self._condition:
            self._attached += 1
            if self._attached == 1:
                self.wrapper.execute_wrappers.insert(0, self._guard_wrapper)

    def detach(self) -> None:
        """Undo `attach()`; the last one removes the guard."""
        with self._condition:
            self._attached -= 1
            if self._attached == 0:
                try:
                    self.wrapper.execute_wrappers.remove(self._guard_wrapper)
                except ValueError:
                    pass

    def _guard(self, execute: Any, sql: Any, params: Any, many: bool, context: Any) -> Any:
        """Run a statement of a thread that does not hold the shared connection as a call of its own."""
        if getattr(self._holding, "active", False) or not self.test.current():
            return execute(sql, params, many, context)
        with self.hold(_Foreign()):
            self.check_isolated("statement of Django code")
            self.apply_options(self.defaults)
            try:
                return execute(sql, params, many, context)
            except BaseException:
                self.forget_options()
                raise

    def check_isolated(self, what: str) -> None:
        """Raise `TransactionOverlapError` while a transaction is open that the caller's context is not inside."""
        enclosing = _enclosing_connections()
        if any(other not in enclosing for other in self.transactions):
            raise TransactionOverlapError(
                f"another transaction is open on the shared connection of Django's TestCase and this {what} does not "
                "run inside it; what overlaps a transaction without nesting in it is not isolated there: use "
                "TransactionTestCase"
            )

    def forget_options(self) -> None:
        """Treat the options as unknown, after a rollback to a savepoint may have undone a ``SET LOCAL``."""
        self._options = None

    def unwind(self, connection: SharedConnectionMixin, error: TransactionOverlapError | None) -> None:
        """Roll back ``connection``'s transaction and those opened after it; the later ones get ``error``."""
        index = self.transactions.index(connection)
        try:
            for other in reversed(self.transactions[index:]):
                other._abandon(None if other is connection else error)
        finally:
            del self.transactions[index:]
            self.forget_options()


class _Foreign:
    """The holder of a statement that another thread runs through the wrapper."""

    __slots__ = ("_cancel_waiting", "_current")

    def __init__(self) -> None:
        self._cancel_waiting = None
        self._current = None


class _Hold:
    __slots__ = ("connection", "previous", "shared")

    def __init__(self, shared: SharedTestConnection, connection: SharedConnectionMixin | _Foreign) -> None:
        self.shared = shared
        self.connection = connection
        self.previous = False

    def __enter__(self) -> None:
        self.shared.acquire(self.connection)
        holding = self.shared._holding
        self.previous = getattr(holding, "active", False)
        holding.active = True

    def __exit__(self, *exc_info: object) -> None:
        self.shared._holding.active = self.previous
        self.shared.release(self.connection)


_shared: weakref.WeakKeyDictionary[Any, SharedTestConnection] = weakref.WeakKeyDictionary()
_shared_lock = threading.Lock()


def shared_test_connection(test: TestDatabase) -> SharedTestConnection:
    """The `SharedTestConnection` of ``test``'s wrapper for the current ``TestCase`` test; one per test."""
    with _shared_lock:
        shared = _shared.get(test.wrapper)
        if shared is None or not _same(shared.test.atomics, test.atomics):
            shared = _shared[test.wrapper] = SharedTestConnection(test)
        return shared


def _same(first: tuple[Any, ...], second: tuple[Any, ...]) -> bool:
    return len(first) == len(second) and all(a is b for a, b in zip(first, second, strict=True))


def _enclosing_connections() -> set[Any]:
    """The connections of the open transactions active in the current context (of every session)."""
    from adorm.executor.transactions import _active, Atomic

    found = set()
    for transaction in _active.get().values():
        if isinstance(transaction, Atomic) and not transaction.root.closed and transaction.lease is not None:
            found.add(transaction.lease.slot.connection)
    return found


class SharedConnectionMixin:
    """Makes a `DjangoConnection` class use the shared test connection of a ``TestCase`` (`shared_connection_class()`).

    The connection is the test's wrapper, shared with the slot's thread (Django's ``inc_thread_sharing()``); the test's
    transaction stays open and nothing closes the wrapper. Every call but ``connect_sync()`` holds the shared connection
    (`SharedTestConnection.hold()`); a cancelled call that waits for it raises a cancellation error.

    A transaction is a savepoint in the test's transaction. ``begin`` and an operation outside a transaction raise
    `TransactionOverlapError` while another connection's transaction is open, unless they come from inside that
    transaction (an ``atomic()`` block of it is active in the caller's context); what runs inside it belongs to its
    savepoint and is rolled back with it. Ending a transaction while one begun inside it is still open rolls back both
    and raises `TransactionOverlapError`; the inner one raises it on its next call. A transaction that is not the
    innermost open one cannot run operations or create or end savepoints.

    An operation outside a transaction runs in a savepoint of its own, so that a failure leaves the test's transaction
    usable. A failed operation inside the innermost transaction rolls back to that transaction's innermost savepoint
    at once, so that other connections can go on; the transaction refuses further operations until it, or that
    savepoint, is rolled back, as Django does. Django's ``on_commit()`` callbacks stay with the test's transaction, as
    those of Django's nested ``atomic()`` blocks do: they run only when the test runs them
    (``captureOnCommitCallbacks(execute=True)``).

    Connection options are kept per connection and applied with ``SET LOCAL`` before each operation, those an
    ``on_acquire`` hook returns included. A profile with ``params`` that differ from the wrapper's settings or a
    ``connect`` hook raises `NotSupportedError` on connecting. A statement a profile hook runs raises
    `NotSupportedError`: its effect on the session would reach every connection.

    A notice is emitted by the connection that holds the shared connection when the server sends it. ``listen()``
    raises `NotSupportedError`: notifications arrive when a transaction commits, which the test's transaction never
    does.
    """

    _shared: SharedTestConnection
    _current: Any
    db: Any
    alias: str
    profile: Any
    _atomic: Any
    _savepoints: list[tuple[str, str]]
    _base_ids: tuple[Any, ...]

    def __init__(self, driver: Any, profile: Any, emit: Any, shared: SharedTestConnection) -> None:
        super().__init__(driver, profile, emit)  # type: ignore[call-arg]
        self._shared = shared
        self._test = shared.test
        self._cancel_waiting: Any = None
        self._holds = 0
        self._overlap: TransactionOverlapError | None = None
        self._needs_rollback = False
        self._session_options: dict[str, Any] = {}
        self._local_options: dict[str, Any] = {}
        self._option_savepoints: list[tuple[str, dict[str, Any]]] = []
        self._sharing = False
        self._attached = False
        self._previous: Any = None

    @property
    def holds_session(self) -> bool:
        """Whether the running call holds the shared connection now."""
        return self._shared.owner is self

    @property
    def session_holds(self) -> int:
        """How many times the connection has taken the shared connection."""
        return self._holds

    # The bridge

    def _run_prepared(self, fn: Callable[..., Any], args: tuple[Any, ...]) -> Any:
        shared = self._shared
        if getattr(fn, "__name__", None) in _UNHELD_CALLS:
            return super()._run_prepared(fn, args)  # type: ignore[misc]
        with shared.hold(self):
            try:
                if getattr(fn, "__name__", None) in _STATEMENT_CALLS and getattr(self, "db", None) is not None:
                    shared.apply_options(self._options_in_effect())
                return super()._run_prepared(fn, args)  # type: ignore[misc]
            except BaseException:
                shared.forget_options()
                raise

    def after_call_sync(self, fn: Callable[[], Any]) -> None:
        def held() -> None:
            with self._shared.hold(self):
                fn()

        super().after_call_sync(held)  # type: ignore[misc]

    def cancel_sync(self) -> None:
        self._shared.cancel(self, super().cancel_sync)  # type: ignore[misc]

    def _watch_backend(self) -> None:
        self._shared.watch_backend()

    def is_cancellation(self, exc: BaseException) -> bool:
        """Also the error of a cancelled wait for the shared connection (SQLSTATE `QUERY_CANCELED`)."""
        return getattr(exc, "sqlstate", None) == QUERY_CANCELED or super().is_cancellation(exc)  # type: ignore[misc]

    def listen_sync(self, channel: str) -> None:  # noqa: ARG002 - the contract's signature
        driver = self.driver.name  # type: ignore[attr-defined]
        raise NotSupportedError(NO_NOTIFICATIONS, driver=driver, unsupported=[NOTIFY])

    def _hook_sql(self, execute: Any, sql: Any, params: Any, many: bool, context: Any) -> Any:
        if not self._runs_hook():  # type: ignore[attr-defined]
            return execute(sql, params, many, context)
        raise NotSupportedError(
            f"a hook of profile {self.profile.name!r} ran SQL on the shared connection of Django's TestCase, where its "
            "effect would reach every slot; return connection options from the hook or use TransactionTestCase",
            driver=self.driver.name,  # type: ignore[attr-defined]
        )

    # Connecting

    def connect_sync(self) -> None:
        from django.db import connections

        db = self._shared.wrapper
        profile = self.profile
        if profile.connect is not None or any(db.settings_dict.get(k) != v for k, v in profile.params.items()):
            raise NotSupportedError(
                f"profile {profile.name!r} sets params or a connect hook, which the shared connection of Django's "
                "TestCase cannot apply; use TransactionTestCase"
            )
        self._previous = getattr(connections._connections, self.alias, None)
        db.inc_thread_sharing()
        self._sharing = True
        connections[self.alias] = db
        self._adopt(db)  # type: ignore[attr-defined]
        defaults = self.driver.connection_defaults(profile)  # type: ignore[attr-defined]
        self._session_options = dict(defaults)
        self._shared.add_defaults(defaults)
        self._shared.attach()
        self._attached = True

    def close_sync(self) -> None:
        db = getattr(self, "db", None)
        if db is None:
            return
        from django.db import connections

        try:
            if self._atomic is not None and self._shared.test.current() and self in self._shared.transactions:
                self._shared.unwind(
                    self,
                    TransactionOverlapError(
                        "the transaction this one began inside ended with its slot; both were rolled back"
                    ),
                )
        finally:
            self._atomic = None
            self._savepoints = []
            if self._attached:
                self._attached = False
                self._shared.detach()
            if self._previous is None:
                try:
                    del connections[self.alias]
                except AttributeError:
                    pass
            elif self._previous is not db:
                connections[self.alias] = self._previous
            if self._sharing:
                self._sharing = False
                db.dec_thread_sharing()

    # Operations

    def _check_overlap(self) -> None:
        error = self._overlap
        if error is not None:
            raise TransactionOverlapError(str(error))

    def _check_isolated(self, what: str) -> None:
        self._shared.check_isolated(what)

    def _innermost(self) -> bool:
        transactions = self._shared.transactions
        return bool(transactions) and transactions[-1] is self

    def execute_sync(self, operation: Any) -> Any:
        self._check_overlap()
        if self._atomic is not None:
            self._check_innermost("an operation of this transaction cannot run")
            if self._needs_rollback:
                raise TransactionManagementError(
                    "An error occurred in the current transaction. You can't execute queries until the end of the "
                    "'atomic' block."
                )
            try:
                return super().execute_sync(operation)  # type: ignore[misc]
            except BaseException:
                needs_rollback = self.db.needs_rollback
                if self._transaction_ended:  # type: ignore[attr-defined]
                    raise
                if self._innermost() and (needs_rollback or _transaction_failed(self.db)):
                    self._recover()
                    # What Django's flag would refuse until a rollback, this connection refuses.
                    self._needs_rollback = needs_rollback
                raise
        from django.db import transaction

        self._check_isolated("operation")
        with transaction.atomic(using=self.alias):
            return super().execute_sync(operation)  # type: ignore[misc]

    def _recover(self) -> None:
        """Roll back to the innermost savepoint of the failed transaction, so that the test's transaction is usable."""
        sid = self._savepoints[-1][1] if self._savepoints else self._begin_sid()
        if sid is None:
            return
        self.db.needs_rollback = False
        self.db.savepoint_rollback(sid)

    def _begin_sid(self) -> Any:
        return self._base_ids[-1] if self._base_ids else None

    def _take_commit_callbacks(self) -> list[tuple[Any, Callable[[], Any], bool]]:
        # They stay on the wrapper, attributed to the enclosing savepoints, as after Django's nested atomic().
        return []

    # Transactions

    def begin_sync(self) -> None:
        from django.db import transaction

        self._overlap = None
        self._needs_rollback = False
        if self._atomic is not None:
            raise TransactionManagementError("a transaction is already open on this connection")
        shared = self._shared
        self._check_isolated("transaction")
        atomic = transaction.atomic(using=self.alias)
        atomic.__enter__()
        self._atomic = atomic
        self._savepoints = []
        self._base_ids = tuple(self.db.savepoint_ids)
        self._local_options = {}
        self._option_savepoints = []
        shared.transactions.append(self)

    def _end_check(self) -> None:
        """Raise `TransactionOverlapError` after rolling back, when a transaction begun inside this one is open."""
        if self._atomic is None or self._innermost():
            return
        error = TransactionOverlapError(
            "a transaction that began inside this one on the shared connection of Django's TestCase is still open; "
            "both were rolled back"
        )
        self._shared.unwind(self, error)
        raise error

    def commit_sync(self) -> None:
        if self._overlap is not None:
            self._overlap, error = None, self._overlap
            raise TransactionOverlapError(str(error))
        self._end_check()
        try:
            if self._needs_rollback and self._atomic is not None:
                self.db.needs_rollback = True  # the commit rolls back and raises
            super().commit_sync()  # type: ignore[misc]
        finally:
            self._ended()

    def rollback_sync(self) -> None:
        if self._overlap is not None:
            self._overlap = None  # rolled back already
            return
        self._end_check()
        try:
            super().rollback_sync()  # type: ignore[misc]
        finally:
            self._ended()
            self._shared.forget_options()

    def _ended(self) -> None:
        self._needs_rollback = False
        if self in self._shared.transactions:
            self._shared.transactions.remove(self)
        self._local_options = {}
        self._option_savepoints = []

    def _abandon(self, error: TransactionOverlapError | None) -> None:
        """Roll back the open transaction; ``error`` is raised by the next call."""
        self._overlap = error
        self._needs_rollback = False
        atomic = self._atomic
        self._local_options = {}
        self._option_savepoints = []
        if atomic is not None:
            self._leave(atomic, rollback=True)  # type: ignore[attr-defined]

    def _check_innermost(self, what: str) -> None:
        """Raise `TransactionOverlapError` while a transaction begun inside this one is open."""
        if self._atomic is not None and not self._innermost():
            raise TransactionOverlapError(
                f"{what} while a transaction that began inside it is open on the shared connection of Django's TestCase"
            )

    def _check_savepoint_order(self) -> None:
        self._check_overlap()
        self._check_innermost("a savepoint of this transaction cannot be created or ended")

    def savepoint_sync(self, name: str) -> None:
        self._check_savepoint_order()
        super().savepoint_sync(name)  # type: ignore[misc]
        self._option_savepoints.append((name, dict(self._local_options)))

    def release_savepoint_sync(self, name: str) -> None:
        self._check_savepoint_order()
        super().release_savepoint_sync(name)  # type: ignore[misc]
        index = self._option_index(name)
        if index is not None:
            del self._option_savepoints[index:]

    def rollback_to_savepoint_sync(self, name: str) -> None:
        self._check_savepoint_order()
        try:
            super().rollback_to_savepoint_sync(name)  # type: ignore[misc]
        finally:
            self._shared.forget_options()
        self._needs_rollback = False
        index = self._option_index(name)
        if index is not None:
            self._local_options = dict(self._option_savepoints[index][1])
            del self._option_savepoints[index + 1 :]

    def _option_index(self, name: str) -> int | None:
        return next((i for i, (saved, _) in enumerate(self._option_savepoints) if saved == name), None)

    # Connection options

    def set_options_sync(self, options: Mapping[str, Any], local: bool) -> None:
        """Record the options; `SharedTestConnection.apply_options()` sets them before each operation."""
        set_options_sql(options, local)  # validates them
        if local:
            self._local_options.update(options)
            return
        for name, value in options.items():
            if value is None:
                self._session_options.pop(name, None)
            else:
                self._session_options[name] = value

    def _options_in_effect(self) -> dict[str, Any]:
        options = dict(self._session_options)
        if self._atomic is not None:
            options.update(self._local_options)
        return {name: value for name, value in options.items() if value is not None}


@functools.cache
def shared_connection_class(cls: type[DjangoConnection]) -> type[DjangoConnection]:
    """``cls``, a `DjangoConnection` class, with `SharedConnectionMixin`; one class per ``cls``."""
    if not (isinstance(cls, type) and issubclass(cls, DjangoConnection)):
        raise TypeError(f"a shared test connection extends a DjangoConnection class, not {cls!r}")
    return type(f"Shared{cls.__name__}", (SharedConnectionMixin, cls), {"__module__": cls.__module__})


async def django_connection(driver: DjangoDriver, profile: Profile, emit: Emit) -> DjangoConnection:
    """A new, unstarted connection of ``driver.connection_class`` for ``profile``.

    Inside a ``TestCase`` (`find_test_database()`) it is of `shared_connection_class()` and shares the test's
    connection; in another test it opens its own connection and becomes unusable once a ``TestCase`` transaction is
    open on the test's connection; outside Django's test environment it is the driver's plain connection."""
    cls = driver.connection_class
    test = await find_test_database(profile.alias)
    if test is None or not test.in_test_case:
        connection = cls(driver, profile, emit)
        connection._test = test
        return connection
    return shared_connection_class(cls)(driver, profile, emit, shared_test_connection(test))  # type: ignore[call-arg]
