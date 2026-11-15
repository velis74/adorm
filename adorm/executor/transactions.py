"""Transactions: ``atomic()`` blocks and the active transactions of the current context, one per session."""

from __future__ import annotations

import asyncio
import contextvars
import enum
import inspect
import itertools
import logging
import warnings
import weakref

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from types import MappingProxyType, TracebackType
from typing import Any, cast, Protocol, runtime_checkable, TYPE_CHECKING

from adorm.drivers.base import is_connection_error
from adorm.events import ON_COMMIT
from adorm.exceptions import (
    DatabaseError,
    DeadlockError,
    InterfaceError,
    NotSupportedError,
    ProtectedError,
    RestrictedError,
    TransactionClosed,
    TransactionManagementError,
)
from adorm.executor.debug import debug_enabled
from adorm.executor.options import resolve_options, ResolvedOptions, tracking
from adorm.executor.slot import (
    cancellation_outcome,
    CancellationOutcome,
    cancelled_before_start,
    mark_cancellation,
)

if TYPE_CHECKING:
    from adorm.executor.sessions import Session
    from adorm.executor.slot import SlotLease
    from adorm.operations import Operation

__all__ = [
    "Atomic",
    "Transaction",
    "TransactionOutcome",
    "active_transaction",
    "atomic",
    "bind_transaction",
    "on_commit",
]

logger = logging.getLogger("adorm.executor")

SessionKey = tuple[str, str]
Callback = Callable[[], Any]

_EMPTY: Mapping[SessionKey, Any] = MappingProxyType({})
_active: ContextVar[Mapping[SessionKey, Transaction]] = ContextVar("adorm_active_transactions", default=_EMPTY)


@runtime_checkable
class Transaction(Protocol):
    """What runs the operations of a session inside a transaction: ``await transaction.execute(operation)``."""

    async def execute(self, operation: Operation) -> Any: ...


def active_transaction(session: Session) -> Transaction | None:
    """The transaction bound to ``session`` in the current context, ``None`` when there is none."""
    return _active.get().get(session.key)


def _bind(session: Session, transaction: Transaction) -> Token:
    if not isinstance(transaction, Transaction):
        raise TypeError(f"a transaction has an async execute(operation), {type(transaction).__name__} has none")
    return _active.set(MappingProxyType({**_active.get(), session.key: transaction}))


@contextmanager
def bind_transaction(session: Session, transaction: Transaction) -> Iterator[Transaction]:
    """Make ``transaction`` the active transaction of ``session`` for the ``with`` block.

    The binding is a context variable: tasks created inside the block inherit it, also after the block ends. A
    binding inside the block for the same session replaces this one until its own block ends; other sessions keep
    theirs.
    """
    token = _bind(session, transaction)
    try:
        yield transaction
    finally:
        _active.reset(token)


def atomic() -> Atomic:
    """An `Atomic` block on the current session: ``async with adorm.atomic() as tx:``."""
    from adorm.executor.sessions import current_session

    return Atomic(current_session())


async def on_commit(func: Callback, *, robust: bool = False, session: Session | None = None) -> None:
    """Run ``func()`` once the transaction of ``session`` (default: the current session) active in this context
    commits (`Atomic.on_commit()`); without an active transaction, run it now. An awaitable ``func()`` returns is
    awaited. With ``robust``, an exception ``func`` raises is logged instead of raised."""
    from adorm.executor.sessions import current_session

    if not callable(func):
        raise TypeError(f"an on_commit callback is callable, not {func!r}")
    session = current_session() if session is None else session
    transaction = active_transaction(session)
    if isinstance(transaction, Atomic):
        transaction.on_commit(func, robust=robust)
    elif transaction is not None:
        raise TransactionManagementError(
            f"the active transaction of {session!r} is {transaction!r}, which runs no callbacks"
        )
    else:
        await _run_callbacks([(func, robust)])


async def _run_callbacks(callbacks: list[tuple[Callback, bool]]) -> None:
    for func, robust in callbacks:
        try:
            result = func()
            if inspect.isawaitable(result):
                await result
        except Exception:
            if not robust:
                raise
            logger.exception("on_commit callback %r failed", func)


def _unbind(session: Session) -> None:
    _active.set(MappingProxyType({k: v for k, v in _active.get().items() if k != session.key}))


_NEW, _OPEN, _CLOSING, _ENDED = "new", "open", "closing", "ended"


class TransactionOutcome(enum.StrEnum):
    """How a transaction ended in the database."""

    COMMITTED = "committed"
    """The slot confirmed the commit, also when the end of the block was cancelled while it ran."""
    ROLLED_BACK = "rolled_back"
    """The block rolled back, the slot aborted the transaction, the commit failed, or the end was cancelled before
    the commit was sent."""
    UNKNOWN = "unknown"
    """The commit was cancelled while it ran, or its connection was lost: the transaction may have committed."""


_UNSENT = object()  # the commit was not sent

_detached: set[asyncio.Task] = set()  # on_commit callbacks of a commit whose end was cancelled


def _detached_done(task: asyncio.Task) -> None:
    _detached.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error("on_commit callback failed", exc_info=task.exception())


def _leaves_transaction_failed(exc: BaseException) -> bool:
    """Whether ``exc``, raised by a request, may leave the database transaction aborted or in an unknown state."""
    if isinstance(exc, asyncio.CancelledError):
        return not cancelled_before_start(exc)
    if isinstance(exc, NotSupportedError) and exc.sqlstate is None and exc.original is None:
        return False  # a capability check on the client side
    if isinstance(exc, (ProtectedError, RestrictedError)) and exc.sqlstate is None:
        return False  # the delete's check of the references, before any statement changed data
    return isinstance(exc, (DatabaseError, InterfaceError))


class Atomic:
    """A transaction block on one session: ``async with session.atomic() as tx:``.

    Entering the block acquires a slot for the session and begins a transaction; the acquisition's
    `SessionTimeout` or `DeadlockError` is raised there. Inside a transaction of the same session it creates a
    savepoint on the transaction's slot instead. The block is the session's active transaction for its context, so
    operations of the session, including those of tasks that inherit the context, run in it.

    Exiting the block waits for the operations already sent, then commits (or releases the savepoint); on an
    exception it rolls back (to the savepoint). `commit()` and `rollback()` end the block earlier. An operation
    that arrives after the end raises `TransactionClosed`. After an operation of the block failed with a database
    error or was cancelled, further operations raise `TransactionManagementError` and the end rolls back.

    While a savepoint is open, operations that other tasks send to an enclosing block wait until it ends.

    The slot sets the database connection options in effect (`resolve_options()`) before the ``BEGIN``, each
    operation and the ``COMMIT``.

    `on_commit()` registers callbacks that run after the transaction commits. The block learns of the commit from the
    slot's ``CallbackEvent(ON_COMMIT)``, the same event in process and from a remote slot.
    """

    __slots__ = (
        "__weakref__",
        "_callbacks",
        "_child",
        "_commit_error",
        "_committed",
        "_drained",
        "_end_error",
        "_ended",
        "_ender",
        "_failed",
        "_forced",
        "_ids",
        "_lease",
        "_pending",
        "_rolled_back",
        "_slot_cm",
        "_state",
        "_tasks",
        "_token",
        "parent",
        "savepoint",
        "session",
    )

    def __init__(self, session: Session) -> None:
        from adorm.executor.sessions import Session

        if not isinstance(session, Session):
            raise TypeError(f"an atomic block needs a Session, not {type(session).__name__}")
        self.session = session
        self.parent: Atomic | None = None
        self.savepoint: str | None = None
        self._state = _NEW
        self._forced = False
        self._failed: BaseException | None = None
        self._pending = 0
        self._drained = asyncio.Event()
        self._drained.set()
        self._ended = asyncio.Event()
        self._ender: asyncio.Task | None = None
        self._end_error: Exception | None = None
        self._child: Atomic | None = None
        self._tasks: weakref.WeakSet[asyncio.Task] = weakref.WeakSet()
        self._lease: SlotLease | None = None
        self._slot_cm: Any = None
        self._token: Token | None = None
        self._ids = itertools.count(1)
        self._callbacks: list[tuple[Callback, bool]] = []
        self._committed = False
        self._commit_error: BaseException | None | object = _UNSENT
        self._rolled_back = False

    def __repr__(self) -> str:
        kind = f"savepoint {self.savepoint!r}" if self.savepoint else "transaction"
        return f"<Atomic {kind} on {self.session!r}, {self._state}>"

    @property
    def root(self) -> Atomic:
        """The outermost block of the transaction: this block when it is not a savepoint."""
        block = self
        while block.parent is not None:
            block = block.parent
        return block

    @property
    def lease(self) -> SlotLease | None:
        """The lease of the transaction's slot; ``None`` before the block is entered."""
        return self.root._lease

    @property
    def closed(self) -> bool:
        """Whether the block no longer takes operations: it is ending or has ended."""
        return self._state in (_CLOSING, _ENDED)

    @property
    def outcome(self) -> TransactionOutcome | None:
        """How the transaction ended. ``None`` while it has not ended or when it never began.
        `TransactionOutcome.UNKNOWN` for a commit that was cancelled or lost its connection; final once the end of the
        block has returned or raised. A savepoint reports `TransactionOutcome.ROLLED_BACK` when it, or a savepoint
        around it, was rolled back, and the outermost block's outcome otherwise."""
        root = self.root
        if root._state != _ENDED or root._lease is None:
            return None
        if any(block._rolled_back or block._forced for block in self._chain() if block.parent is not None):
            return TransactionOutcome.ROLLED_BACK
        if root._committed:
            return TransactionOutcome.COMMITTED
        error = root._commit_error
        if error is _UNSENT:
            return TransactionOutcome.ROLLED_BACK
        if isinstance(error, asyncio.CancelledError):
            if cancellation_outcome(error) in (CancellationOutcome.NOT_STARTED, CancellationOutcome.FAILED):
                return TransactionOutcome.ROLLED_BACK
            return TransactionOutcome.UNKNOWN
        if error is None or is_connection_error(cast("BaseException", error)):
            return TransactionOutcome.UNKNOWN  # no confirmation, or the connection was lost during the commit
        return TransactionOutcome.ROLLED_BACK

    def _chain(self) -> Iterator[Atomic]:
        block: Atomic | None = self
        while block is not None:
            yield block
            block = block.parent

    def _check_open(self) -> None:
        lease = self.lease
        if lease is not None and lease._aborted is not None:
            raise TransactionClosed(lease._aborted)
        for block in self._chain():
            if block._state != _OPEN:
                raise TransactionClosed(f"the atomic block on {self.session!r} has ended")
        if self._failed is not None:
            raise TransactionManagementError(
                f"an operation of this atomic block failed ({self._failed!r}); "
                "no further operations run until the block ends"
            )

    def _hold(self) -> None:
        task = asyncio.current_task()
        for block in self._chain():
            block._pending += 1
            block._drained.clear()
            if task is not None:
                block._tasks.add(task)

    def _unhold(self) -> None:
        for block in self._chain():
            block._pending -= 1
            if block._pending == 0:
                block._drained.set()

    async def _request(
        self, method: str, *args: Any, options: Mapping[str, Any] | None = None, operation: Any = None
    ) -> Any:
        """Send a request to the slot, counted as pending in this block and its parents; a failure that may leave
        the transaction aborted marks this block failed. The enclosing `adorm.timeout()` blocks see ``operation``
        (default: ``method``) executing."""
        lease = cast("SlotLease", self.lease)
        self._hold()
        try:
            with tracking("executing", method if operation is None else operation, self.session):
                return await lease.call(method, *args, options=options)
        except BaseException as exc:
            if self._failed is None and _leaves_transaction_failed(exc):
                self._failed = exc
            raise
        finally:
            self._unhold()

    def _options(self) -> ResolvedOptions:
        """The options in effect for the block's session (`resolve_options()`). Raises `NotSupportedError` for a
        database option the driver does not declare."""
        pool = cast("SlotLease", self.lease).pool
        options = resolve_options(self.session, pool)
        pool.driver.capabilities.check_options(options.database, pool.driver.name)
        return options

    async def _wait_for_savepoint(self, operation: Any, timeout: float | None = None) -> None:
        """Wait until no savepoint is open directly inside this block, then check that the block is open. Raises
        `DeadlockError` when the wait exceeds ``timeout``, by default the ``acquire_timeout`` connection option in
        effect. The enclosing `adorm.timeout()` blocks see ``operation`` waiting for the slot."""
        while True:
            self._check_open()
            child = self._child
            if child is None or child._state == _ENDED:
                return
            if timeout is None:
                timeout = resolve_options(self.session, cast("SlotLease", self.lease).pool).acquire_timeout
            try:
                async with asyncio.timeout(timeout):
                    with tracking("waiting_for_slot", operation, self.session):
                        await child._ended.wait()
            except TimeoutError:
                raise DeadlockError(
                    f"{child!r} stayed open for {timeout:g} s while another task of {self!r} waited for it",
                    held_sessions=(self.session,),
                    session=self.session,
                    timeout=timeout,
                ) from None

    async def execute(self, operation: Operation) -> Any:
        """Run ``operation`` in this block, after a savepoint open inside it ends, with the database connection options
        in effect (`resolve_options()`). Raises `TransactionClosed` once the block ends,
        `TransactionManagementError` after an operation of the block failed and `NotSupportedError` for an option
        the driver does not declare."""
        try:
            self._check_open()
            options = self._options()
            await self._wait_for_savepoint(operation, options.acquire_timeout)
        except asyncio.CancelledError as exc:
            mark_cancellation(exc, CancellationOutcome.NOT_STARTED)
            raise
        return await self._request("execute", operation, options=options.database, operation=operation)

    def on_commit(self, func: Callback, *, robust: bool = False) -> None:
        """Run ``func()`` after the transaction commits, once the slot has been released, in registration order and
        outside the transaction: operations in ``func`` use a slot of their own. An awaitable ``func()`` returns is
        awaited. Callbacks are dropped when the transaction, or the savepoint they were registered in, rolls back or
        does not commit. With ``robust`` an exception ``func`` raises is logged; otherwise it is raised by the end of
        the outermost block (or `commit()`), after the commit, and the remaining callbacks do not run. After a
        confirmed commit whose end was cancelled, the callbacks run in a task of their own, which logs that exception.
        Raises `TransactionClosed` once the block has ended."""
        if not callable(func):
            raise TypeError(f"an on_commit callback is callable, not {func!r}")
        if self._state != _OPEN:
            raise TransactionClosed(f"{self!r} is not open")
        self._callbacks.append((func, robust))

    def _commit_confirmed(self, event: object) -> None:  # noqa: ARG002
        self._committed = True

    async def _run_on_commit(self) -> None:
        """Run the callbacks after a confirmed commit, in a context without this session's transaction."""
        callbacks, self._callbacks = self._callbacks, []
        if not (self._committed and callbacks):
            return
        await self._callbacks_task(callbacks)

    def _callbacks_task(self, callbacks: list[tuple[Callback, bool]]) -> asyncio.Task:
        context = contextvars.copy_context()
        context.run(_unbind, self.session)
        return asyncio.get_running_loop().create_task(_run_callbacks(callbacks), context=context)

    # Entering

    async def __aenter__(self) -> Atomic:
        if self._state != _NEW:
            raise TransactionManagementError(f"{self!r} was already entered")
        parent = active_transaction(self.session)
        if parent is None:
            await self._begin()
        elif isinstance(parent, Atomic):
            await self._begin_savepoint(parent)
        else:
            raise TransactionManagementError(
                f"the active transaction of {self.session!r} is {parent!r}, which takes no nested atomic blocks"
            )
        self._token = _bind(self.session, self)
        return self

    async def _begin(self) -> None:
        from adorm.executor.sessions import get_runner, TransactionRunner

        runner = get_runner()
        if not isinstance(runner, TransactionRunner):
            raise InterfaceError(f"no runner holds a slot for a transaction of {self.session!r} (runner {runner!r})")
        self._state = _CLOSING
        try:
            slot_cm = runner.slot(self.session)
            try:
                with tracking("waiting_for_slot", "begin", self.session):
                    lease = await slot_cm.__aenter__()
            except asyncio.CancelledError as exc:
                mark_cancellation(exc, CancellationOutcome.NOT_STARTED)
                raise
            try:
                with tracking("executing", "begin", self.session):
                    await lease.call("begin", options=resolve_options(self.session, lease.pool).database)
            except BaseException:
                # A begin that failed or was cancelled may have left the connection inside a transaction.
                await lease.release(discard=True)
                await slot_cm.__aexit__(None, None, None)
                raise
        except BaseException:
            self._state = _ENDED
            self._ended.set()
            raise
        lease.channel.on(ON_COMMIT, self._commit_confirmed)
        self._lease, self._slot_cm = lease, slot_cm
        self._state = _OPEN

    async def _begin_savepoint(self, parent: Atomic) -> None:
        self._state = _CLOSING
        try:
            await parent._wait_for_savepoint("savepoint")
            root = parent.root
            self.parent = parent
            self.savepoint = f"adorm_sp_{next(root._ids)}"
            parent._child = self
            try:
                await parent._request("savepoint", self.savepoint)
                parent._check_open()
            except BaseException:
                parent._child = None
                raise
        except BaseException:
            self._state = _ENDED
            self._ended.set()
            raise
        self._state = _OPEN

    # Ending

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> bool:
        try:
            if self._state == _OPEN:
                await self._end(commit=exc_type is None, explicit=False)
            elif self._forced:
                if exc_type is None:
                    raise TransactionClosed(f"the enclosing atomic block of {self!r} ended first")
            elif self._ender is not asyncio.current_task():
                # Another task ended the block with commit() or rollback().
                await self._ended.wait()
                if exc_type is None and self._end_error is not None:
                    raise self._end_error
        finally:
            if self._token is not None:
                _active.reset(self._token)
                self._token = None
            if self._slot_cm is not None:
                slot_cm, self._slot_cm = self._slot_cm, None
                await slot_cm.__aexit__(None, None, None)
        return False

    async def commit(self) -> None:
        """Commit the transaction (release the savepoint) now, after the operations already sent. Later operations
        of the block raise `TransactionClosed`; the end of the block does nothing more."""
        self._check_ended_once()
        await self._end(commit=True, explicit=True)

    async def rollback(self) -> None:
        """Roll back the transaction (to the savepoint) now. Later operations of the block raise
        `TransactionClosed`; the end of the block does nothing more."""
        self._check_ended_once()
        await self._end(commit=False, explicit=True)

    def _check_ended_once(self) -> None:
        if self._state != _OPEN:
            raise TransactionClosed(f"{self!r} is not open")

    def _close_descendants(self) -> str | None:
        """End the savepoints still open inside this block; return the name of the outermost one."""
        child = self._child
        forced = child.savepoint if child is not None and child._state == _OPEN else None
        while child is not None:
            if child._state == _OPEN:
                child._state = _ENDED
                child._forced = True
                child._ended.set()
            child._callbacks.clear()
            child = child._child
        self._child = None
        return forced

    def _warn_live_tasks(self) -> None:
        if not debug_enabled():
            return
        current = asyncio.current_task()
        live = sorted(task.get_name() for task in self._tasks if task is not current and not task.done())
        if live:
            warnings.warn(
                f"{self!r} ends while tasks that used it are still running: {', '.join(live)}",
                RuntimeWarning,
                stacklevel=4,
            )

    async def _end(self, *, commit: bool, explicit: bool) -> None:
        self._state = _CLOSING
        self._ender = asyncio.current_task()
        forced = self._close_descendants()
        self._warn_live_tasks()
        try:
            if self.parent is None:
                await self._end_transaction(commit, explicit, forced)
            else:
                await self._end_savepoint(commit, explicit, forced)
        except Exception as exc:
            self._end_error = exc
            self._callbacks.clear()
            raise
        except BaseException as exc:
            callbacks, self._callbacks = self._callbacks, []
            if self._committed:
                if callbacks:
                    task = self._callbacks_task(callbacks)
                    _detached.add(task)
                    task.add_done_callback(_detached_done)
            elif commit:
                # Raised by the owner's end of the block when another task's commit() was cancelled.
                outcome = self.outcome
                detail = "the enclosing block rolls back" if outcome is None else f"outcome {outcome}"
                self._end_error = TransactionManagementError(
                    f"the end of {self!r} was cancelled before it committed; {detail}"
                )
                self._end_error.__cause__ = exc
            raise
        finally:
            self._ended.set()
        if self.parent is None:
            await self._run_on_commit()

    async def _discard_forced(self, forced: str | None) -> BaseException | None:
        """Roll back to the savepoint ``forced`` that another task left open; return the error if that fails."""
        if forced is None:
            return None
        try:
            await cast("SlotLease", self.lease).call("rollback_to_savepoint", forced)
        except Exception as exc:
            logger.warning("rolling back the unfinished savepoint %r of %r failed", forced, self, exc_info=True)
            return exc
        return None

    async def _end_transaction(self, commit: bool, explicit: bool, forced: str | None) -> None:
        lease = cast("SlotLease", self._lease)
        failed = self._failed
        try:
            await self._drained.wait()
            failed = self._failed
            options: Mapping[str, Any] = {}
            if commit and failed is None and lease._aborted is None:
                failed = await self._discard_forced(forced)
                if failed is None:
                    try:
                        options = self._options().database
                    except NotSupportedError as exc:
                        failed = exc
            if lease._aborted is not None:
                failed = None
                if commit:
                    raise TransactionClosed(lease._aborted)
            elif commit and failed is None:
                self._commit_error = None
                try:
                    with tracking("executing", "commit", self.session):
                        await lease.call("commit", options=options)
                except BaseException as exc:
                    self._commit_error = exc
                    raise
            else:
                try:
                    with tracking("executing", "rollback", self.session):
                        await lease.call("rollback")
                except Exception:
                    if explicit and failed is None:
                        raise
                    logger.warning("rolling back %r failed", self, exc_info=True)
        finally:
            self._state = _ENDED
            await lease.release()
        if commit and failed is not None:
            raise TransactionManagementError(
                f"{self!r} was rolled back because an operation failed: {failed!r}"
            ) from failed

    async def _end_savepoint(self, commit: bool, explicit: bool, forced: str | None) -> None:
        parent = cast("Atomic", self.parent)
        failed = self._failed
        settled = False
        parent._hold()  # the parent ends only after this savepoint
        try:
            await self._drained.wait()
            failed = self._failed
            if commit and failed is None:
                failed = await self._discard_forced(forced)
            if commit and failed is None:
                await parent._request("release_savepoint", self.savepoint)
                parent._callbacks.extend(self._callbacks)
            else:
                self._rolled_back = True
                try:
                    await parent._request("rollback_to_savepoint", self.savepoint)
                    await parent._request("release_savepoint", self.savepoint)
                except Exception:
                    if explicit and failed is None:
                        raise
                    logger.warning("rolling back %r failed", self, exc_info=True)
            settled = True
        finally:
            if not settled and parent._failed is None:
                parent._failed = TransactionManagementError(f"{self!r} did not end cleanly")
            parent._unhold()
            self._callbacks.clear()
            self._state = _ENDED
            self._ended.set()
            if parent._child is self:
                parent._child = None
        if commit and failed is not None:
            raise TransactionManagementError(
                f"{self!r} was rolled back because an operation failed: {failed!r}"
            ) from failed
