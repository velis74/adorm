# Cancellation

Cancelling the task that awaits an adorm operation cancels the operation. A request still waiting for a slot or
queued on one never runs; a statement running in the slot is cancelled in the database (psycopg's `cancel_safe()` in
the [Django driver](/topics/django-driver#cancellation), asyncpg's cancellation in the
[native PostgreSQL driver](/topics/postgresql-driver)). The cancelled task gets `asyncio.CancelledError` as usual, so
`asyncio.timeout()`, `TaskGroup` and `gather()` behave as they do with any other coroutine.

```python
task = asyncio.create_task(Member.objects.filter(club_id=1).update(active=False))
...
task.cancel()          # the UPDATE is cancelled in the database
```

## The slot after a cancellation

The slot never leaks: releasing it completes also when the task is cancelled again meanwhile.

| Where the cancellation lands | The slot |
|---|---|
| waiting for a slot | the request leaves the queue; a slot handed to it just before goes to the next request |
| an operation outside a transaction | returns to the pool once the connection has stopped the statement; it is dropped when the driver reports the connection unusable (the native PostgreSQL driver after every statement cancelled before it finished) |
| an operation inside `atomic()` | the block rolls back on its way out; the slot returns to the pool, or is dropped when the driver reports the connection unusable |
| the commit at the end of `atomic()` | returns to the pool when the commit succeeded; otherwise it closes its connection, which ends the transaction in the database, and leaves the pool |

## Outcome of a cancelled operation

A cancellation can race the completion of the statement: the database may have finished it, or not, by the time the
cancellation reaches it. [`adorm.cancellation_outcome(exc)`](/ref/pool#adorm-cancellation-outcome-exc) tells what
is known about the cancelled operation from the `CancelledError` it raised:

| [`CancellationOutcome`](/ref/pool#adorm-cancellationoutcome) | Meaning |
|---|---|
| `NOT_STARTED` | it waited for a slot or was queued on one: it never reached the database |
| `SUCCEEDED` | it finished with a result before the cancellation reached it: it took effect |
| `FAILED` | it finished with an error before the cancellation reached it: it had no effect |
| `UNKNOWN` | it was cancelled while it ran, or its connection was lost: it may or may not have taken effect |

```python
try:
    await Payment.objects.filter(pk=pk).update(state="captured")
except asyncio.CancelledError as exc:
    match adorm.cancellation_outcome(exc):
        case adorm.CancellationOutcome.NOT_STARTED | adorm.CancellationOutcome.FAILED:
            pass                                   # nothing changed
        case adorm.CancellationOutcome.SUCCEEDED:
            audit.captured(pk)                     # the update is in the database
        case adorm.CancellationOutcome.UNKNOWN:
            reconcile.later(pk)                    # read the row again before acting on it
    raise
```

The value is final when the `CancelledError` reaches the caller of an operation outside a transaction: the
operation's slot is released first, which waits for the slot to finish the request, also when the caller is cancelled
again meanwhile.

What a driver reports for a statement cancelled while it ran decides between `SUCCEEDED`, `FAILED` and `UNKNOWN`
([driver contract](/ref/drivers#cancellation)):

| Driver | A statement cancelled while it ran |
|---|---|
| [Django](/topics/django-driver#cancellation) | `SUCCEEDED` or `FAILED` when it finished on the connection's thread before the cancellation reached the database; `UNKNOWN` when the database cancelled it |
| [native PostgreSQL](/topics/postgresql-driver#cancellation) | `UNKNOWN`, also when it completed just before the cancellation; `SUCCEEDED` or `FAILED` only when its result reached the slot first |

`cancellation_outcome()` also accepts an exception the cancellation caused, such as the `TimeoutError` of
`asyncio.timeout()`, which carries the `CancelledError` as `__cause__`:

```python
try:
    async with asyncio.timeout(2):
        await Member.objects.filter(level="beginner").update(level="intermediate")
except TimeoutError as exc:
    outcome = adorm.cancellation_outcome(exc)      # NOT_STARTED while waiting for a slot, else as above
```

It returns `None` for an exception that no cancelled adorm operation raised. The same holds for the
`OperationTimeout` of [`adorm.timeout()`](/topics/options#adorm-timeout), which also tells whether the operation was
waiting for a slot or executing:

```python
try:
    async with adorm.timeout(2):
        await Member.objects.filter(level="beginner").update(level="intermediate")
except adorm.OperationTimeout as exc:
    outcome = adorm.cancellation_outcome(exc)      # NOT_STARTED when exc.phase == "waiting_for_slot"
```

A set of [connection options](/topics/options) that a cancellation interrupts is sent again with the next operation
that runs with them; the cancelled operation itself did not run.

Inside an atomic block the outcome describes the request in the open transaction. A cancelled block rolls back, so
none of its operations takes effect, whatever their outcome.

## Outcome of a cancelled transaction

[`Atomic.outcome`](/ref/transactions#atomic-class) tells how the block's transaction ended:

| [`TransactionOutcome`](/ref/transactions#adorm-transactionoutcome) | When |
|---|---|
| `COMMITTED` | the commit succeeded, also when the task was cancelled while it ran |
| `ROLLED_BACK` | the block rolled back, the slot aborted the transaction, the commit failed, or the cancellation arrived before the commit was sent |
| `UNKNOWN` | the commit was cancelled while it ran, or its connection was lost: the transaction may have committed |

```python
block = adorm.atomic()
try:
    async with block:
        await Order.objects.filter(pk=pk).update(state="paid")
        await Invoice.objects.create(order_id=pk)
except asyncio.CancelledError:
    if block.outcome is adorm.TransactionOutcome.UNKNOWN:
        reconcile.later(pk)
    raise
```

`outcome` is `None` while the transaction is open and for a block that never began. A savepoint block reports
`ROLLED_BACK` when it, or a savepoint around it, was rolled back, and the outcome of its outermost block otherwise. It
is final once the end of the block has returned or raised.

When the commit succeeded but the end of the block was cancelled, the block's `on_commit()` callbacks still run, in a
task of their own outside the transaction; an exception a callback raises is logged on the `adorm.executor` logger.
When another task ends the block with `commit()` and is cancelled before the commit is confirmed, the end of the
owner's `async with` raises `TransactionManagementError`, whose `__cause__` is that `CancelledError`.

## Events

Cancellation emits no events of its own. It consumes:

| Event | Interpretation |
|---|---|
| `CancelledEvent(request_id, started=False)` | the cancelled request never ran: `NOT_STARTED` |
| `CancelledEvent(request_id, started=True)` | the request was cancelled while it ran: `UNKNOWN` |
| `ResultEvent` or `ErrorEvent` of a request whose caller was cancelled | the request finished first: `SUCCEEDED`, or `FAILED` (`UNKNOWN` for a connection error) |
| `CallbackEvent("on_commit")` before the outcome of a cancelled `commit` | the transaction committed: `TransactionOutcome.COMMITTED` |

The slot delivers the outcome of a request whose caller was cancelled to the lease's channel as usual, which
completes the `CancellationOutcome` of the caller's `CancelledError`.
