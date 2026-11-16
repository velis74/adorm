# Transactions

Transactions run on the [router](/topics/pool) set as the runner, with the [connection options](/topics/options) in
effect (`SET LOCAL`). The [Django driver](/topics/django-driver), the
[native PostgreSQL driver](/topics/postgresql-driver) and any other `Driver` implementation (see
[Writing a driver](/howto/writing-a-driver)) run them.

A transaction is an `async with` block. It holds one slot of its session's pool from the start of the block to the
end. It commits when the block exits normally and rolls back when it exits with an exception:

```python
import adorm

from club.models import Activity, Member

async with adorm.atomic():
    member = await Member.objects.get(pk=1)
    member.active = False
    await member.save()
    await Activity.objects.create(member_id=1, kind="deactivated")
# committed here; an exception inside the block would have rolled both writes back
```

As a decorator, `atomic()` runs every call of a coroutine function in a transaction of its own:

```python
@adorm.atomic()
async def lend(book_id: int) -> Book:
    book = await Book.objects.select_for_update().get(pk=book_id)
    if book.copies == 0:
        raise LookupError(f"no copy of {book.title!r} left")
    book.copies -= 1
    await book.save(update_fields=["copies"])
    return book
```

Concurrent calls run in separate transactions. A call inside an open transaction of the same session runs in a
savepoint, as a nested `async with` block does.

## Choosing the session

`adorm.atomic()` opens a transaction on the current session, `session.atomic()` on the given one, as
[`objects`](/topics/sessions#bound-models) chooses its session. The session is fixed when `atomic()` is called; a
function decorated with `@adorm.atomic()` uses the session current at each call, one decorated with
`@session.atomic()` always uses `session`.

```python
reports = adorm.create_session(using="reports", session_name="nightly")

async with reports.atomic():
    await reports.Member.filter(active=False).update(fee=0)      # in the transaction
    await Member.objects.count()                                 # the current session: outside it
```

An operation's session looks up its active transaction in the current context when the operation runs. Bound models,
query sets and model instances need no reference to the block. A transaction of one session does not affect another
session, even on the same profile.

## Taking the slot

The block acquires its slot on entry and sends `BEGIN` there. When no slot becomes free within the `acquire_timeout`
[connection option](/topics/options) in effect, entry raises
[`SessionTimeout`](/ref/exceptions#adorm-sessiontimeout-message-none-session-none-timeout-none) and the body does not
run. Operations inside the block take no further slot.

```python
try:
    async with reports.atomic():
        ...
except adorm.SessionTimeout as exc:
    log.warning("no slot for %r within %g s", exc.session, exc.timeout)
```

A flow inside a transaction that opens a transaction of another session waits for a second slot while it holds the
first. When that wait times out, entry raises
[`DeadlockError`](/ref/exceptions#adorm-deadlockerror-message-none-held-sessions-session-none-timeout-none); its
`held_sessions` names the sessions whose slots the flow holds. When the wait cannot end, for example when every slot
of the pool is held by flows that wait for a slot, `DeadlockError` is raised at once.

```python
audit = adorm.create_session(session_name="audit")

async with adorm.atomic():                 # holds a slot of "default"
    async with audit.atomic():             # a second slot of "default": its own transaction
        ...
```

## Ending early

`tx.commit()` commits before the end of the block, `tx.rollback()` rolls back. Both wait for the operations already
sent and return the slot to the pool. Later operations of the block raise
[`TransactionClosed`](/ref/exceptions#adorm-transactionclosed); the end of the block does nothing more.

```python
async with adorm.atomic() as tx:
    await Member.objects.filter(pk=1).update(active=False)
    await tx.commit()                      # visible to other connections from here on
    await notify_board(member_id=1)        # holds no slot
```

## Savepoints

An `atomic()` block inside a transaction of the same session is a savepoint on the transaction's slot. An exception
that leaves the inner block rolls back to its savepoint; the outer block continues and commits the rest.

```python
async with adorm.atomic():
    await Member.objects.create(name="Ana")
    try:
        async with adorm.atomic():
            await Member.objects.create(name="Ana")      # violates a unique constraint
    except adorm.IntegrityError:
        pass                                          # only the inner write is undone
    await Member.objects.create(name="Bea")
# commits Ana and Bea
```

`commit()` on a savepoint releases it, `rollback()` rolls back to it. Savepoints need a driver whose
[capabilities](/ref/drivers#adorm-drivers-capabilities) include them. Otherwise entering the inner block raises
`NotSupportedError` and the outer transaction stays usable.

## Running code after the commit

`tx.on_commit(func)` registers a function that runs after the transaction commits, for example to send a mail, clear a
cache or start a background job that reads the new rows. `await adorm.on_commit(func)` registers it on the
transaction of the current session.

```python
async def deactivate(member_id):
    await Member.objects.filter(pk=member_id).update(active=False)
    await adorm.on_commit(lambda: mailer.send_goodbye(member_id))     # an async function is awaited too

async with adorm.atomic():
    await deactivate(1)
    await deactivate(2)
# both mails go out here, after COMMIT
```

The callbacks run in the order they were registered, after the slot has returned to the pool, outside the
transaction. An operation inside a callback takes a slot of its own and sees the committed rows. The callbacks run in
the context of the block, so other context variables keep their values. An explicit `tx.commit()` runs them before it
returns.

| End of the transaction | Callbacks |
|---|---|
| commit | run |
| rollback, an exception, a failed operation, an abort after the [commit deadline](#the-commit-deadline) | dropped |
| a savepoint is released | move to the enclosing block |
| a savepoint rolls back | the savepoint's callbacks, including those of savepoints inside it, are dropped |

A callback that raises stops the callbacks after it, and the end of the block (or `commit()`) raises its exception;
the transaction is committed. With `robust=True` the exception is logged and the next callback runs. Outside a
transaction `adorm.on_commit()` runs the function at once.

```python
async with adorm.atomic() as tx:
    await Member.objects.create(name="Ana")
    tx.on_commit(search_index.refresh, robust=True)     # a failing refresh does not fail the request
```

The block runs the callbacks when the slot reports the commit with the `on_commit` [event](/topics/events), the
event a remote slot sends as well.

## A failed operation

An operation that fails with a database error, or is cancelled, leaves the database transaction aborted. The block
then refuses further operations (`TransactionManagementError`) and its end rolls back. When the block exits normally
after catching the error, its end raises `TransactionManagementError` with the original error as `__cause__`:

```python
async with adorm.atomic():
    try:
        await Member.objects.create(name="Ana")          # IntegrityError
    except adorm.IntegrityError:
        pass
# TransactionManagementError: the transaction was rolled back
```

To continue after an error, catch it around a nested `atomic()`, as in [Savepoints](#savepoints).

These errors do not affect the transaction:

- errors raised on the client side, such as `DoesNotExist` from `get()`;
- a `NotSupportedError` without a SQLSTATE or driver exception (an operation the driver does not support); a
  `NotSupportedError` from the server (SQLSTATE class 0A) does affect it;
- cancellation of an operation that waits behind another one in the slot; it never reaches the database;
- `ProtectedError` and `RestrictedError` without a SQLSTATE: the delete checks the references before any statement
  changes data, as in Django. A database-level `RESTRICT` violation (SQLSTATE `23001`) is an `IntegrityError` and
  does affect it;
- a [`FieldError`](/ref/exceptions#adorm-fielderror-message-none-model-none-key-none-original-none-sqlstate-none)
  without a SQLSTATE, which the driver raises while it builds the statement for a field path or
  [lookup key](/topics/queries#lookup-keys-built-at-run-time) it cannot resolve.

A block that catches such an error continues and commits:

```python
async with adorm.atomic():
    try:
        await member.delete()
    except adorm.ProtectedError:
        await Member.objects.filter(pk=member.pk).update(active=False)
# COMMIT
```

```python
async with adorm.atomic():
    try:
        count = await Member.objects.filter(**{f"{field}__{op}": value}).count()
    except adorm.FieldError:
        count = None
    await Report.objects.create(filter=field, count=count)
# COMMIT
```

A read of rows with `select_for_update()` (`get()`, `first()`, `last()`, `in_bulk()`, iteration) outside a transaction
raises `TransactionManagementError` before it is sent; the lock would end with the statement.

## Tasks inside a transaction

The transaction is a context variable. Tasks created inside the block (`asyncio.create_task()`, `TaskGroup`,
`gather()`) inherit it and run their operations on the transaction's slot, one at a time, in the order they are sent.

```python
async with adorm.atomic():
    async with asyncio.TaskGroup() as group:
        for name in ("Ana", "Bea", "Cene"):
            group.create_task(Member.objects.create(name=name))
# one transaction, one slot, three inserts
```

An `atomic()` block in such a task is a savepoint of the transaction. Savepoints opened by tasks inside the same block
run one after another. Operations that other tasks send to the outer block while a savepoint is open wait until it
ends, so a rollback to the savepoint never undoes them.

A task that waits for a savepoint which waits for that task (a task created before the savepoint and awaited inside
it) raises `DeadlockError` after the `acquire_timeout` in effect.

```python
async with adorm.atomic():
    task = asyncio.create_task(Member.objects.create(name="Ana"))
    async with adorm.atomic():          # opens before the task sends its insert
        await task                      # DeadlockError in the task after acquire_timeout
```

### The end of the block

Exiting the block waits for the operations already sent by any task, then commits. An operation a task sends after
that raises `TransactionClosed`; it does not run outside the transaction.

```python
async with adorm.atomic():
    task = asyncio.create_task(log_activity(member))    # inherits the transaction

await task       # TransactionClosed when log_activity() ran its operation after the commit
```

A savepoint block that another task holds open when its enclosing block ends is rolled back before the commit. Its
later operations raise `TransactionClosed`, and so does its own end when it exits normally.

When another task ends the block with `tx.commit()` or `tx.rollback()`, the exit of the block waits for that end; when
it failed, a normal exit raises its error.

In debug mode the end of a block warns with a `RuntimeWarning` about tasks that ran operations in it and are still
running. Debug mode is the executor's [debug switch](/ref/transactions#debug-mode). `True` turns it on and `False`
off, set with any of:

- [`adorm.configure(debug=...)`](/ref/settings#adorm-configure-profiles-none-providers-pool-none-debug-none);
- the `debug` key of the [`ADORM`](/ref/settings#adorm) Django setting, which the Django app passes to
  `adorm.configure()`;
- [`adorm.executor.set_debug()`](/ref/transactions#adorm-executor-set-debug-value).

`None`, the default, follows asyncio's debug mode (`asyncio.run(..., debug=True)` or `PYTHONASYNCIODEBUG=1`). An
`adorm.configure()` call without `debug` that does not raise, and an `ADORM` setting without the key, set the switch
to `None`.

## Cancellation

Cancelling the task inside a block cancels the statement running in the slot; the block rolls back on exit and the
slot returns to the pool. Cancelling the task while the block commits cancels the commit in the driver connection.
The end raises `CancelledError` once the slot has finished the commit, if it was sent. The block's `outcome` is:

- `COMMITTED`: the commit completed first;
- `UNKNOWN`: the commit was cancelled while it ran; the slot closes its connection and leaves the pool;
- `ROLLED_BACK`: the cancellation came before the commit was sent.

After a confirmed commit the block's `on_commit()` callbacks run in a task of their own; otherwise they are dropped. A
block cancelled while it waits for a slot on entry raises `CancelledError` with the
[cancellation outcome](/ref/pool#adorm-cancellation-outcome-exc) `NOT_STARTED`.

```python
block = adorm.atomic()
task = asyncio.create_task(transfer(block))      # async with block: ...
task.cancel()
with contextlib.suppress(asyncio.CancelledError):
    await task
block.outcome      # TransactionOutcome.COMMITTED, ROLLED_BACK or UNKNOWN; None if the block never began
```

[Cancellation](/topics/cancellation) covers the outcome of cancelled operations.

## The commit deadline

A slot inside a transaction expects its next request (an operation, the commit or the rollback) within the
`commit_timeout` in effect when the block was entered: the pool's, unless the session or a
[`connection_options()`](/topics/options) block sets one. When none arrives in time, the slot rolls the transaction
back, ends the lease and leaves the pool; the next request gets a fresh connection. A running request does not count
against the deadline. `commit_timeout` is 60 seconds by default; `None` sets no deadline. A rollback that does not
finish within `commit_timeout` is cancelled, and the slot leaves the pool.

```python
pool = Pool(AsyncpgDriver(), profile, commit_timeout=60)

async with adorm.atomic():
    await Member.objects.filter(pk=1).update(active=False)
    await asyncio.sleep(120)               # the slot rolls back after 60 s
    await Member.objects.count()           # TransactionClosed
```

The end of such a block raises `TransactionClosed` when it exits normally.

```python
with adorm.connection_options(commit_timeout=600):
    async with adorm.atomic():             # this transaction may wait up to 10 minutes between requests
        ...
```

## Connection options in a transaction

Operations in a transaction run with the [connection options](/topics/options) in effect where they are awaited. The
slot sets them with `SET LOCAL`, so they end with the transaction, and a rollback to a savepoint restores the values
from before the savepoint. An option the driver does not declare raises `NotSupportedError` and leaves the
transaction usable.

```python
async with adorm.atomic():
    with adorm.connection_options(lock_timeout=0.5):
        try:
            async with adorm.atomic():
                await Member.objects.filter(pk=1).update(fee=0)    # SET LOCAL lock_timeout = 500
        except adorm.OperationalError:
            ...                                                   # the row stayed locked; savepoint rolled back
    await Club.objects.count()                                    # SET LOCAL lock_timeout TO DEFAULT
```

## Events

Transactions emit no [events](/topics/events) of their own. They consume:

| Event | Interpretation |
|---|---|
| `ResultEvent`, `ErrorEvent`, `CancelledEvent` of each request they send (`begin`, `execute`, `savepoint`, `commit` …) | the result, error or cancellation of the awaited call |
| `CallbackEvent("on_commit")`, delivered by the slot before the outcome of a successful `commit` | the block runs its `on_commit` callbacks once the slot is back in the pool; its `outcome` is `COMMITTED` |

Other callbacks and backend events the connection emits during the transaction, and `CallbackEvent("on_rollback")`
(after a rollback or an abort), reach the lease's `events` queue, and the pool's `events` bus when the lease is
released.
