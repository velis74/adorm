"""Sessions, the current session and models bound to a session."""

from __future__ import annotations

import threading
import weakref

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol, runtime_checkable, TYPE_CHECKING

from adorm.exceptions import InterfaceError, TransactionManagementError
from adorm.executor.options import session_options, set_session_options
from adorm.executor.transactions import active_transaction, Atomic
from adorm.models.base import Model, set_executor_resolver
from adorm.models.managers import BoundModel
from adorm.models.registry import registry

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from adorm.executor.slot import SlotLease
    from adorm.operations import Operation

__all__ = [
    "BoundModel",
    "Runner",
    "Session",
    "TransactionRunner",
    "create_session",
    "current_session",
    "get_runner",
    "set_runner",
]


@runtime_checkable
class Runner(Protocol):
    """What runs an operation of a session outside a transaction: ``await runner.run(session, operation)`` acquires
    a slot for ``session``, executes the operation, releases the slot and returns the result."""

    async def run(self, session: Session, operation: Operation) -> Any: ...


@runtime_checkable
class TransactionRunner(Runner, Protocol):
    """A `Runner` that also holds a slot for a transaction: ``async with runner.slot(session) as lease:`` acquires a
    slot for ``session``, raising `SessionTimeout` or `DeadlockError`, and releases it on exit."""

    def slot(self, session: Session) -> AbstractAsyncContextManager[SlotLease]: ...


_runner: Runner | None = None


def set_runner(runner: Runner | None) -> Runner | None:
    """Set the `Runner` of operations outside a transaction. Returns the previous one; ``None`` removes it."""
    global _runner
    if runner is not None and not isinstance(runner, Runner):
        raise TypeError(f"a runner has an async run(session, operation), {type(runner).__name__} has none")
    previous, _runner = _runner, runner
    return previous


def get_runner() -> Runner | None:
    """The `Runner` of operations outside a transaction: the one `set_runner()` set, else, inside a running event
    loop and when adorm's settings have a connection profile or a provider, the loop's default router
    (`adorm.settings.default_router()`); ``None`` otherwise."""
    if _runner is not None:
        return _runner
    from adorm.settings import _default_runner

    return _default_runner()


def _check_name(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what} is a non-empty string, not {value!r}")
    return value


class Session:
    """A logical session: the pair ``(using, session_name)``. There is one object per pair while it is referenced:
    ``Session(using, session_name)`` returns the same object as `create_session()`.

    A session holds no slot. ``await session.execute(operation)`` runs the operation in the session's active
    transaction of the current context, or else on the configured `Runner`. ``session.Member`` (a model class name)
    and ``session.bind(model)`` return the model bound to this session; ``session.atomic()`` opens a transaction on
    it; ``session.set_options()`` sets its connection options.
    """

    __slots__ = ("__weakref__", "session_name", "using")

    using: str
    session_name: str

    def __new__(cls, using: str = "default", session_name: str = "default") -> Session:
        return create_session(using, session_name)

    @classmethod
    def _make(cls, using: str, session_name: str) -> Session:
        session = object.__new__(cls)
        object.__setattr__(session, "using", using)
        object.__setattr__(session, "session_name", session_name)
        return session

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"a session is immutable: cannot set {name!r}")

    def __reduce__(self) -> tuple[Any, ...]:
        return create_session, (self.using, self.session_name)

    def __copy__(self) -> Session:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> Session:
        return self

    @property
    def key(self) -> tuple[str, str]:
        """The pair ``(using, session_name)``."""
        return self.using, self.session_name

    def bind(self, model: type[Model] | str, manager: str = "objects") -> BoundModel:
        """The manager ``manager`` of ``model`` (a model class or a registered label) bound to this session."""
        if isinstance(model, str):
            model = registry.get(model)
        if not isinstance(model, type) or not issubclass(model, Model):
            raise TypeError(f"a bound model needs a Model subclass, not {model!r}")
        declared = model.__adorm_managers__.get(manager)
        if declared is None:
            raise AttributeError(f"{model.__name__} has no manager {manager!r}")
        return declared._bind(model, self)

    def __getattr__(self, name: str) -> BoundModel:
        if name.startswith("_"):
            raise AttributeError(name)
        matches = [model for model in registry if model.__name__ == name]
        if not matches:
            raise AttributeError(f"no registered model is named {name!r}")
        if len(matches) > 1:
            labels = ", ".join(sorted(m._meta.label for m in matches))  # type: ignore[union-attr]
            raise AttributeError(f"{name!r} names several models ({labels}): use bind() with a label")
        return self.bind(matches[0])

    @contextmanager
    def activate(self) -> Iterator[Session]:
        """Make this session the current session for the ``with`` block (a synchronous ``with``: it only sets a
        context variable). Tasks created inside the block inherit it."""
        token = _current.set(self)
        try:
            yield self
        finally:
            _current.reset(token)

    def atomic(self) -> Atomic:
        """An `Atomic` block on this session: ``async with session.atomic() as tx:``."""
        return Atomic(self)

    @property
    def options(self) -> Mapping[str, Any]:
        """The connection options set on the session (`set_options()`), a read-only mapping."""
        return session_options(self)

    def set_options(self, **options: Any) -> Mapping[str, Any]:
        """Replace the session's connection options, the layer between the profile and `adorm.connection_options()`
        blocks; without arguments it removes them. They belong to the pair ``(using, session_name)``, so a session
        created again for the pair has them too. Returns the checked options; raises `ValueError` as
        `adorm.executor.options.check_options()`. Thread-safe."""
        return set_session_options(self, options)

    async def execute(self, operation: Operation) -> Any:
        """Run ``operation``: in the transaction of this session active in the current context, or else on the
        `Runner` (`get_runner()`). Without a transaction and a runner it raises `InterfaceError`; a read of rows with
        ``select_for_update()`` without a transaction raises `TransactionManagementError`."""
        transaction = active_transaction(self)
        if transaction is not None:
            return await transaction.execute(operation)
        if operation.kind in _ROW_READS and operation.query.for_update is not None:  # type: ignore[attr-defined]
            raise TransactionManagementError("select_for_update() cannot be used outside of a transaction")
        runner = get_runner()
        if runner is None:
            raise InterfaceError(f"no runner executes operations outside a transaction (session {self!r})")
        return await runner.run(self, operation)

    def __repr__(self) -> str:
        return f"Session({self.using!r}, {self.session_name!r})"


_ROW_READS = frozenset({"select", "get", "first", "last", "in_bulk"})
"""Operation kinds that read rows and so lock them with ``select_for_update()``."""

_sessions: weakref.WeakValueDictionary[tuple[str, str], Session] = weakref.WeakValueDictionary()
_sessions_lock = threading.Lock()
_current: ContextVar[Session | None] = ContextVar("adorm_current_session", default=None)


def create_session(using: str = "default", session_name: str = "default") -> Session:
    """The open session ``(using, session_name)``, created when no live object holds that pair. Thread-safe."""
    key = (_check_name(using, "using"), _check_name(session_name, "session_name"))
    session = _sessions.get(key)
    if session is None:
        with _sessions_lock:
            session = _sessions.get(key)
            if session is None:
                session = _sessions[key] = Session._make(*key)
    return session


def current_session() -> Session:
    """The current session of the context: the one a `Session.activate()` block set, else
    ``create_session("default", "default")``."""
    session = _current.get()
    return create_session() if session is None else session


def _instance_executor(model: type[Model]) -> Session | None:  # noqa: ARG001 - every model uses the current session
    return current_session()


set_executor_resolver(_instance_executor)
