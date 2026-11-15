"""The active transactions of the current context, one per session."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable, TYPE_CHECKING

if TYPE_CHECKING:
    from adorm.executor.sessions import Session
    from adorm.operations import Operation

__all__ = ["Transaction", "active_transaction", "bind_transaction"]

SessionKey = tuple[str, str]

_EMPTY: Mapping[SessionKey, Any] = MappingProxyType({})
_active: ContextVar[Mapping[SessionKey, Transaction]] = ContextVar("adorm_active_transactions", default=_EMPTY)


@runtime_checkable
class Transaction(Protocol):
    """What runs the operations of a session inside a transaction: ``await transaction.execute(operation)``."""

    async def execute(self, operation: Operation) -> Any: ...


def active_transaction(session: Session) -> Transaction | None:
    """The transaction bound to ``session`` in the current context, ``None`` when there is none."""
    return _active.get().get(session.key)


@contextmanager
def bind_transaction(session: Session, transaction: Transaction) -> Iterator[Transaction]:
    """Make ``transaction`` the active transaction of ``session`` for the ``with`` block.

    The binding is a context variable: tasks created inside the block inherit it, also after the block ends. A
    binding inside the block for the same session replaces this one until its own block ends; other sessions keep
    theirs.
    """
    if not isinstance(transaction, Transaction):
        raise TypeError(f"a transaction has an async execute(operation), {type(transaction).__name__} has none")
    token = _active.set(MappingProxyType({**_active.get(), session.key: transaction}))
    try:
        yield transaction
    finally:
        _active.reset(token)
