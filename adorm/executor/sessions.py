"""Sessions, the current session and models bound to a session."""

from __future__ import annotations

import threading
import weakref

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol, runtime_checkable, TYPE_CHECKING

from adorm.exceptions import InterfaceError
from adorm.executor.transactions import active_transaction
from adorm.models.base import Model, set_executor_resolver
from adorm.models.registry import registry
from adorm.query.query import Query
from adorm.query.queryset import QuerySet

if TYPE_CHECKING:
    from adorm.operations import Operation

__all__ = ["BoundModel", "Runner", "Session", "create_session", "current_session", "set_runner"]


@runtime_checkable
class Runner(Protocol):
    """What runs an operation of a session outside a transaction: ``await runner.run(session, operation)`` acquires
    a slot for ``session``, executes the operation, releases the slot and returns the result."""

    async def run(self, session: Session, operation: Operation) -> Any: ...


_runner: Runner | None = None


def set_runner(runner: Runner | None) -> Runner | None:
    """Set the `Runner` of operations outside a transaction. Returns the previous one; ``None`` removes it."""
    global _runner
    if runner is not None and not isinstance(runner, Runner):
        raise TypeError(f"a runner has an async run(session, operation), {type(runner).__name__} has none")
    previous, _runner = _runner, runner
    return previous


def _check_name(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what} is a non-empty string, not {value!r}")
    return value


class Session:
    """A logical session: the pair ``(using, session_name)``. There is one object per pair while it is referenced:
    ``Session(using, session_name)`` returns the same object as `create_session()`.

    A session holds no slot. ``await session.execute(operation)`` runs the operation in the session's active
    transaction of the current context, or else on the configured `Runner`. ``session.Member`` (a model class name)
    and ``session.bind(model)`` return the model bound to this session.
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

    def bind(self, model: type[Model] | str) -> BoundModel:
        """``model`` (a model class or a registered label) bound to this session."""
        if isinstance(model, str):
            model = registry.get(model)
        return BoundModel(model, self)

    def __getattr__(self, name: str) -> BoundModel:
        if name.startswith("_"):
            raise AttributeError(name)
        matches = [model for model in registry if model.__name__ == name]
        if not matches:
            raise AttributeError(f"no registered model is named {name!r}")
        if len(matches) > 1:
            labels = ", ".join(sorted(m._meta.label for m in matches))  # type: ignore[union-attr]
            raise AttributeError(f"{name!r} names several models ({labels}): use bind() with a label")
        return BoundModel(matches[0], self)

    @contextmanager
    def activate(self) -> Iterator[Session]:
        """Make this session the current session for the ``with`` block (a synchronous ``with``: it only sets a
        context variable). Tasks created inside the block inherit it."""
        token = _current.set(self)
        try:
            yield self
        finally:
            _current.reset(token)

    async def execute(self, operation: Operation) -> Any:
        """Run ``operation``: in the transaction of this session active in the current context, or else on the
        `Runner`. Without a transaction and a runner it raises `InterfaceError`."""
        transaction = active_transaction(self)
        if transaction is not None:
            return await transaction.execute(operation)
        runner = _runner
        if runner is None:
            raise InterfaceError(f"no runner executes operations outside a transaction (session {self!r})")
        return await runner.run(self, operation)

    def __repr__(self) -> str:
        return f"Session({self.using!r}, {self.session_name!r})"


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


class BoundModel:
    """A model class bound to a session: what ``Model.objects`` and ``session.Model`` return.

    Binding takes no slot. It does not include a transaction: each operation looks up the session's active
    transaction in the context it runs in. Query methods return a `QuerySet` on this session; ``objects`` returns
    the bound model itself, so ``session.Member.objects`` is ``session.Member``.
    """

    __slots__ = ("model", "session")

    model: type[Model]
    session: Session

    def __init__(self, model: type[Model], session: Session) -> None:
        if not isinstance(model, type) or not issubclass(model, Model):
            raise TypeError(f"a bound model needs a Model subclass, not {model!r}")
        if model._meta is None:
            raise TypeError(f"{model.__name__} is abstract: it has no _meta")
        if not isinstance(session, Session):
            raise TypeError(f"a bound model needs a Session, not {type(session).__name__}")
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "session", session)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"a bound model is immutable: cannot set {name!r}")

    def __reduce__(self) -> tuple[Any, ...]:
        return BoundModel, (self.model, self.session)

    def __copy__(self) -> BoundModel:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> BoundModel:
        return self

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, BoundModel):
            return NotImplemented
        return self.model is other.model and self.session is other.session

    def __hash__(self) -> int:
        return hash((BoundModel, self.model, self.session))

    def __repr__(self) -> str:
        return f"<BoundModel {self.model._meta.label} on {self.session!r}>"  # type: ignore[union-attr]

    @property
    def objects(self) -> BoundModel:
        """The bound model itself."""
        return self

    async def execute(self, operation: Operation) -> Any:
        """Run ``operation`` on the session (`Session.execute()`)."""
        return await self.session.execute(operation)

    def all(self) -> QuerySet:
        """A `QuerySet` of every row of the model, on this session."""
        return QuerySet(self.model, self.session, Query(self.model._meta.label))  # type: ignore[union-attr]

    def filter(self, *args: Any, **kwargs: Any) -> QuerySet:
        """``all().filter(...)``."""
        return self.all().filter(*args, **kwargs)

    def exclude(self, *args: Any, **kwargs: Any) -> QuerySet:
        """``all().exclude(...)``."""
        return self.all().exclude(*args, **kwargs)

    def annotate(self, **annotations: Any) -> QuerySet:
        """``all().annotate(...)``."""
        return self.all().annotate(**annotations)

    def order_by(self, *keys: Any) -> QuerySet:
        """``all().order_by(...)``."""
        return self.all().order_by(*keys)

    def values(self, *fields: str) -> QuerySet:
        """``all().values(...)``."""
        return self.all().values(*fields)

    def values_list(self, *fields: str, flat: bool = False, named: bool = False) -> QuerySet:
        """``all().values_list(...)``."""
        return self.all().values_list(*fields, flat=flat, named=named)

    def only(self, *fields: str) -> QuerySet:
        """``all().only(...)``."""
        return self.all().only(*fields)

    def defer(self, *fields: str | None) -> QuerySet:
        """``all().defer(...)``."""
        return self.all().defer(*fields)

    def select_related(self, *fields: str | None) -> QuerySet:
        """``all().select_related(...)``."""
        return self.all().select_related(*fields)

    def distinct(self, *fields: str) -> QuerySet:
        """``all().distinct(...)``."""
        return self.all().distinct(*fields)

    def none(self) -> QuerySet:
        """``all().none()``."""
        return self.all().none()

    def select_for_update(self, **options: Any) -> QuerySet:
        """``all().select_for_update(...)``."""
        return self.all().select_for_update(**options)

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        """``await all().get(...)``."""
        return await self.all().get(*args, **kwargs)

    async def first(self) -> Any:
        """``await all().first()``."""
        return await self.all().first()

    async def last(self) -> Any:
        """``await all().last()``."""
        return await self.all().last()

    async def count(self) -> int:
        """``await all().count()``."""
        return await self.all().count()

    async def exists(self) -> bool:
        """``await all().exists()``."""
        return await self.all().exists()

    async def create(self, **values: Any) -> Any:
        """``await all().create(...)``."""
        return await self.all().create(**values)

    async def get_or_create(self, defaults: Any = None, **lookups: Any) -> tuple[Any, bool]:
        """``await all().get_or_create(...)``."""
        return await self.all().get_or_create(defaults, **lookups)

    async def update(self, **values: Any) -> int:
        """``await all().update(...)``."""
        return await self.all().update(**values)

    async def delete(self) -> tuple[int, dict[str, int]]:
        """``await all().delete()``."""
        return await self.all().delete()

    def __aiter__(self) -> Any:
        return self.all().__aiter__()


def _instance_executor(model: type[Model]) -> Session | None:  # noqa: ARG001 - every model uses the current session
    return current_session()


set_executor_resolver(_instance_executor)
