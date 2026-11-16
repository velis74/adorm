# Transactions

`atomic()` blocks, `on_commit` callbacks and the active transactions of a context, in `adorm.executor`. `atomic`,
`Atomic`, `on_commit` and `TransactionOutcome` are also importable from `adorm`. See the
[transactions topic](/topics/transactions).

```python
from adorm import atomic, Atomic, on_commit, TransactionOutcome
from adorm.executor import active_transaction, bind_transaction, debug_enabled, set_debug, Transaction
```

## Atomic blocks

### `adorm.atomic()`

An [`Atomic`](#atomic-class) block on the current session ([`current_session()`](/ref/sessions#adorm-current-session)
when `atomic()` is called). [`Session.atomic()`](/ref/sessions#atomic) returns one on a given session.

```python
async with adorm.atomic() as tx:
    await Member.objects.filter(pk=1).update(active=False)
```

### `adorm.Atomic` {#atomic-class}

A transaction block on one session, used with `async with`. `Atomic(session)` takes a
[`Session`](/ref/sessions#adorm-session) (`TypeError` otherwise); `atomic()` and `Session.atomic()` create it. A
block is entered once; entering it again raises `TransactionManagementError`.

**Entering** (`__aenter__`, returns the block itself):

- When the session has no active transaction in the context, the block acquires a slot through the runner's
  `slot(session)` ([`Router.slot()`](/ref/pool#slot-session-timeout-none)) and sends `begin` with the database
  connection options in effect; the slot sets them (`SET`, `RESET`) before the `BEGIN`.
  - The acquisition raises `SessionTimeout` or `DeadlockError` after the `acquire_timeout`
    [connection option](/ref/options) in effect.
  - The lease's `commit_timeout` is the `commit_timeout` option in effect at entry.
  - A runner that is not a [`TransactionRunner`](/ref/sessions#adorm-executor-transactionrunner), or no runner, raises
    `InterfaceError`.
  - When `begin` fails or is cancelled, the slot is discarded and the error propagates.
- When the active transaction of the session is an `Atomic`, the block is a savepoint of it, on the same slot.
  - It waits until a savepoint another task opened in the same enclosing block ends; after the `acquire_timeout`
    connection option in effect the wait raises `DeadlockError` with the session in `held_sessions`.
  - An enclosing block that has ended raises `TransactionClosed`; one with a failed operation raises
    `TransactionManagementError`.
  - A driver without savepoints raises `NotSupportedError`.
- Any other active transaction ([`bind_transaction()`](#adorm-executor-bind-transaction-session-transaction)) raises
  `TransactionManagementError`.

The entered block is the session's active transaction for the context of the `async with` statement and of tasks
created inside it.

**Exiting** (`__aexit__`), in order:

1. closes the block to further operations and new savepoints;
2. closes savepoint blocks of other tasks still open inside it;
3. waits for the operations already sent;
4. rolls back to the outermost savepoint it closed, when it would commit or release;
5. ends the block:

| Exit | Transaction | Savepoint |
|---|---|---|
| normal | `commit` | `release_savepoint` |
| with an exception | `rollback` | `rollback_to_savepoint`, then `release_savepoint` |
| normal, after a failed operation | `rollback`, then raises `TransactionManagementError` from the failure | `rollback_to_savepoint`, `release_savepoint`, then raises `TransactionManagementError` |
| after the slot aborted the transaction | nothing; a normal exit raises `TransactionClosed` | the requests raise `TransactionClosed`: a normal exit raises it and marks the enclosing block as failed, an exit with an exception logs it |
| after `commit()` or `rollback()` | nothing | nothing |
| while `commit()` or `rollback()` of another task runs | waits for it; a normal exit raises its error | waits for it; a normal exit raises its error |
| normal, after the enclosing block closed it | | raises `TransactionClosed` |

- The `commit` carries the database connection options in effect where the block ends; the slot sets them
  (`SET LOCAL`) before the `COMMIT`. With an option the driver does not declare, the transaction rolls back instead
  and the exit raises `TransactionManagementError` from the `NotSupportedError`.
- A failing `commit` or `release_savepoint` propagates.
- A failing rollback on an exit with an exception is logged and the block's exception propagates.
- A failing rollback to a savepoint closed by the end is logged and the block rolls back to its own savepoint, as
  after a failed operation.
- A transaction block returns its slot to the pool at the end; a slot whose commit or rollback did not succeed leaves
  the pool.
- A savepoint whose end failed or was cancelled marks the enclosing block as failed.

| Member | Meaning |
|---|---|
| `session` | the `Session` |
| `parent` | the enclosing `Atomic` of a savepoint, `None` for a transaction |
| `root` | the outermost block of the transaction |
| `savepoint` | the savepoint name, `None` for a transaction |
| `lease` | the [`SlotLease`](/ref/pool#adorm-executor-slotlease) of the transaction's slot, `None` before entry |
| `closed` | whether the block no longer takes operations: it is ending or has ended |
| `outcome` | the [`TransactionOutcome`](#adorm-transactionoutcome) of the transaction; for a savepoint `ROLLED_BACK` when it or a savepoint around it was rolled back, else the outermost block's; `None` while the transaction is open and for a block that never began; final once the end of the block has returned or raised |

- When the end of the block is cancelled during the commit, it raises `CancelledError` once the slot has finished a
  sent commit; `outcome` tells whether it committed. After a confirmed commit the `on_commit()` callbacks run in a task
  of their own, which logs an exception a callback raises on the `adorm.executor` logger.
- When another task's `commit()` ended the block and was cancelled before the commit was confirmed, the end of the
  owner's `async with` raises `TransactionManagementError` with that `CancelledError` as `__cause__`.

See [cancellation](/topics/cancellation).

```python
block = adorm.atomic()
try:
    async with block:
        await Member.objects.filter(pk=1).update(active=False)
except asyncio.CancelledError:
    if block.outcome is TransactionOutcome.UNKNOWN:
        ...                                   # the update may or may not be committed
    raise
```

#### `async execute(operation)`

Runs `operation` on the transaction's slot and returns its result, with the database connection options in effect
([`resolve_options()`](/ref/options#adorm-executor-options-resolve-options-session-pool)), which the slot sets with
`SET LOCAL`. Operations of a session's bound models reach the block through
[`Session.execute()`](/ref/sessions#async-execute-operation).

- An option the driver does not declare raises `NotSupportedError` before anything is sent.
- While a savepoint block of another task is open directly inside this block, it first waits until that savepoint
  ends; after the `acquire_timeout` connection option in effect it raises `DeadlockError`.
- Raises `TransactionClosed` once the block (or an enclosing block) is closed or the slot aborted the transaction, and
  `TransactionManagementError` after an operation of the block failed.
- An operation that fails with a `DatabaseError` or `InterfaceError`, or is cancelled after it reached the slot's
  connection, marks the block as failed. A `NotSupportedError` without `sqlstate` and `original` (a client-side
  capability check) and a cancellation of a queued operation do not.
- A cancellation while the operation waits for a savepoint carries
  [`CancellationOutcome.NOT_STARTED`](/ref/pool#adorm-cancellationoutcome); a cancellation after the request was sent
  carries the outcome [`SlotLease.call()`](/ref/pool#async-call-method-args-options-none) gives it.
- Enclosing [`adorm.timeout()`](/ref/options#adorm-timeout-delay) blocks see the operation as `"waiting_for_slot"`
  while it waits for the savepoint, then `"executing"`, and the block's own requests by name (`"begin"`,
  `"savepoint"`, `"commit"`, `"rollback"` …).

#### `async commit()`

Ends the block as a normal exit does: commits (releases the savepoint) after the operations already sent.
Raises `TransactionClosed` when the block is not open, and `TransactionManagementError` after a failed operation
(once it has rolled back).

```python
async with adorm.atomic() as tx:
    await Member.objects.create(name="Ana")
    await tx.commit()
```

#### `async rollback()`

Ends the block as an exit with an exception does: rolls back (to the savepoint). Raises `TransactionClosed` when the
block is not open. A failing rollback propagates; after a failed operation of the block it is logged instead.

```python
async with adorm.atomic() as tx:
    invoice = await Invoice.objects.create(member_id=member.id, total=total)
    if not await charge(invoice):
        await tx.rollback()          # the invoice is not kept; the block ends here
```

#### `on_commit(func, *, robust=False)`

Registers `func` to run after the transaction commits; a synchronous method. `func()` is called without arguments;
an awaitable it returns is awaited.

- The callbacks run after the commit succeeded and the slot is back in the pool, in registration order, in a copy
  of the block's context without this session's transaction; an operation in a callback takes a slot of its own.
  The block runs them when the slot delivers
  [`CallbackEvent(ON_COMMIT)`](/ref/events#adorm-events-on-commit) for its `commit` request.
- A savepoint passes its callbacks to the enclosing block when it is released and drops them when it rolls back or
  the enclosing block closes it.
- A rollback, an exit with an exception, a failed operation and an abort by the slot drop the callbacks. So does a
  commit cancelled before the slot confirmed it. After a confirmed commit whose end was cancelled, the callbacks run
  in a task of their own, which logs an exception a callback raises on the `adorm.executor` logger.
- A callback that raises stops the remaining ones; the exception propagates from the end of the outermost block, or
  from `commit()`, after the commit. It does not reach other tasks waiting for that end. With `robust=True` the
  exception is logged on the `adorm.executor` logger and the next callback runs.
- Cancelling the task that ends the block while a callback runs cancels the callback.

A `func` that is not callable raises `TypeError`; a block that is not open raises `TransactionClosed`.

```python
async with adorm.atomic() as tx:
    member = await Member.objects.create(name="Ana")
    tx.on_commit(lambda: mailer.send_welcome(member.pk))
```

### `adorm.on_commit(func, *, robust=False, session=None)`

A coroutine function: registers `func` with [`Atomic.on_commit()`](#on-commit-func-robust-false) on the active
transaction of `session` (default: [`current_session()`](/ref/sessions#adorm-current-session)) in the current
context, the innermost savepoint when one is open. Without an active transaction it calls `func()` now and awaits
the result; `robust=True` then logs an exception instead of raising it. An active transaction that is not an
`Atomic` raises `TransactionManagementError`; a `func` that is not callable raises `TypeError`.

```python
async def close_account(member_id):
    await Member.objects.filter(pk=member_id).update(active=False)
    await adorm.on_commit(lambda: billing.cancel_subscription(member_id))   # after COMMIT, or now outside one
```

### `adorm.TransactionOutcome`

A `StrEnum` of how a transaction ended in the database, the value of [`Atomic.outcome`](#atomic-class).

| Member | Value | When |
|---|---|---|
| `COMMITTED` | `"committed"` | the slot delivered `CallbackEvent("on_commit")`: the commit succeeded, also when the end of the block was cancelled while it ran |
| `ROLLED_BACK` | `"rolled_back"` | the block rolled back, the slot aborted the transaction, the commit failed with an error other than a lost connection, or the end was cancelled before the commit reached the driver connection |
| `UNKNOWN` | `"unknown"` | the commit was cancelled after it reached the slot, or failed with a lost connection ([`is_connection_error()`](/ref/drivers#adorm-drivers-is-connection-error-exc)): the transaction may have committed |

```python
tx = adorm.atomic()
try:
    async with tx:
        await payment.save()
finally:
    if tx.outcome is adorm.TransactionOutcome.UNKNOWN:
        log.warning("payment %s may have been committed", payment.pk)
```

## Transactions in the context

### `adorm.executor.Transaction`

The protocol of an object that runs a session's operations inside a transaction:
`await transaction.execute(operation)`. `Atomic` implements it.

### `adorm.executor.active_transaction(session)`

The transaction bound to `session` in the current context, `None` when there is none.

```python
async with reports.atomic() as tx:
    assert active_transaction(reports) is tx
```

### `adorm.executor.bind_transaction(session, transaction)`

A synchronous context manager that makes `transaction` the active transaction of `session` for its block and yields
it. The binding is a context variable; tasks created inside the block inherit it, also after the block ends. A
binding for the same session inside the block replaces it until the inner block ends; other sessions' bindings are
unaffected. An object without an `async execute(operation)` raises `TypeError`.

```python
with bind_transaction(reports, transaction):
    assert active_transaction(reports) is transaction
    await reports.Member.filter(pk=1).update(fee=0)      # transaction.execute(...)
assert active_transaction(reports) is None
```

## Warnings

In debug mode ([`debug_enabled()`](#adorm-executor-debug-enabled)), the end of a block issues a `RuntimeWarning`
naming the tasks that ran operations in it, or in its savepoints, and are still running.

## Debug mode

The executor's debug checks run when its debug switch is on. The switch is set by:

- [`adorm.configure(debug=...)`](/ref/settings#adorm-configure-profiles-none-providers-pool-none-debug-none);
- the `debug` key of the [`ADORM`](/ref/settings#adorm) Django setting, which the
  [Django app](/ref/settings#django-app) passes to `adorm.configure()` on `django.setup()` and when a test overrides
  `ADORM`;
- [`set_debug()`](#adorm-executor-set-debug-value).

`True` turns the checks on and `False` off. `None` makes them follow the running event loop's debug mode
(`asyncio.run(..., debug=True)`, `loop.set_debug(True)` or `PYTHONASYNCIODEBUG=1`); it is the value before the
switch is set. Every `adorm.configure()` call sets the switch, to `None` when `debug` is not given; the value set last
applies.

### `adorm.executor.set_debug(value)`

Turns the executor's debug checks on (`True`) or off (`False`); `None` makes them follow the running event loop's
debug mode. Returns the previous value. Anything else raises `TypeError`.

```python
from adorm.executor import set_debug

set_debug(True)
```

### `adorm.executor.debug_enabled()`

Whether the executor's debug checks run: the debug switch when it is `True` or `False`, or else `loop.get_debug()` of
the running event loop (`False` outside one).
