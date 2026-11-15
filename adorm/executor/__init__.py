"""The executor: sessions, models bound to a session, transactions and the active transactions of a context, the
pools and router that assign slots to sessions, connection options and the time limit of operations."""

from adorm.executor.debug import debug_enabled, set_debug
from adorm.executor.options import connection_options, Timeout, timeout
from adorm.executor.pool import held_leases, holding, Pool, Router
from adorm.executor.sessions import (
    BoundModel,
    create_session,
    current_session,
    get_runner,
    Runner,
    Session,
    set_runner,
    TransactionRunner,
)
from adorm.executor.slot import (
    cancellation_outcome,
    CancellationOutcome,
    Request,
    REQUEST_METHODS,
    RequestQueue,
    Slot,
    SlotLease,
)
from adorm.executor.transactions import (
    active_transaction,
    Atomic,
    atomic,
    bind_transaction,
    on_commit,
    Transaction,
    TransactionOutcome,
)

__all__ = [
    "REQUEST_METHODS",
    "Atomic",
    "BoundModel",
    "CancellationOutcome",
    "Pool",
    "Request",
    "RequestQueue",
    "Router",
    "Runner",
    "Session",
    "Slot",
    "SlotLease",
    "Timeout",
    "Transaction",
    "TransactionOutcome",
    "TransactionRunner",
    "active_transaction",
    "atomic",
    "bind_transaction",
    "cancellation_outcome",
    "connection_options",
    "create_session",
    "current_session",
    "debug_enabled",
    "get_runner",
    "held_leases",
    "holding",
    "on_commit",
    "set_debug",
    "set_runner",
    "timeout",
]
