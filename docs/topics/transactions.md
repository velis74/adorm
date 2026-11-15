# Transactions

::: info Status
The installed version provides `atomic()` blocks, savepoints, the transaction in the context and the slot's commit
deadline and `on_commit` callbacks. Transactions run on the [router](/topics/pool) set as the runner. Connection
options (`connection_options()`) and `adorm.timeout()` are not part of it yet. Neither are the Django ORM and
native PostgreSQL drivers used in the examples; any `Driver` implementation (see
[Writing a driver](/howto/writing-a-driver)) works.
:::

A transaction is an `async with` block. It holds one slot of its session's pool from the start of the block to the
end, commits when the block exits normally and rolls back when it exits with an exception:

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

## Choosing the session

`atomic()` picks its session the same way [`objects`](/topics/sessions#bound-models) does: `adorm.atomic()` opens a
transaction on the current session, `session.atomic()` on the given one. The session is fixed when `atomic()` is
called.

```python
reports = adorm.create_session(using="reports", session_name="nightly")

async with reports.atomic():
    await reports.Member.filter(active=False).update(fee=0)      # in the transaction
    await Member.objects.count()                                 # the current session: outside it
```

Operations find the transaction on their own. When an operation runs, its session looks up its active transaction
in the current context; a bound model, a query set and a model instance need no reference to the block. A
transaction of one session does not affect another session, even on the same profile.

## Taking the slot

The block acquires its slot on entry and sends `BEGIN` there. When no slot becomes free within the pool's
`acquire_timeout`, entry raises [`SessionTimeout`](/ref/exceptions#adorm-sessiontimeout-message-none-session-none-timeout-none);
the body does not run. Operations inside the block take no further slot, so a transaction does not wait for slots
halfway through.

```python
try:
    async with reports.atomic():
        ...
except adorm.SessionTimeout as exc:
    log.warning("no slot for %r within %g s", exc.session, exc.timeout)
```

A flow that is inside a transaction and opens a transaction of another session waits for a second slot while it
holds the first. When that wait times out, entry raises
[`DeadlockError`](/ref/exceptions#adorm-deadlockerror-message-none-held-sessions-session-none-timeout-none), whose
`held_sessions` names the sessions whose slots the flow holds. When the wait cannot end (for example every slot of
the pool is held by flows that themselves wait for a slot), `DeadlockError` is raised at once.

```python
audit = adorm.create_session(session_name="audit")

async with adorm.atomic():                 # holds a slot of "default"
    async with audit.atomic():             # a second slot of "default": its own transaction
        ...
```

## Ending early

`tx.commit()` commits before the end of the block, `tx.rollback()` rolls back. Both wait for the operations
already sent and return the slot to the pool. Operations of the block after that raise
[`TransactionClosed`](/ref/exceptions#adorm-transactionclosed); the end of the block does nothing more.

```python
async with adorm.atomic() as tx:
    await Member.objects.filter(pk=1).update(active=False)
    await tx.commit()                      # visible to other connections from here on
    await notify_board(member_id=1)        # holds no slot
```

## Savepoints

An `atomic()` block inside a transaction of the same session is a savepoint on the transaction's slot. An exception
that leaves the inner block rolls back to its savepoint; the outer block continues and commits what remains.

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
[capabilities](/ref/drivers#adorm-drivers-capabilities) include them; otherwise entering the inner block raises
`NotSupportedError` and the outer transaction stays usable.

## Running code after the commit

`tx.on_commit(func)` registers a function that runs once the transaction has committed: sending a mail, clearing a
cache, starting a background job that reads the new rows. `await adorm.on_commit(func)` does the same for the
transaction of the current session, so code deep inside a call chain needs no reference to the block.

```python
async def deactivate(member_id):
    await Member.objects.filter(pk=member_id).update(active=False)
    await adorm.on_commit(lambda: mailer.send_goodbye(member_id))     # an async function is awaited too

async with adorm.atomic():
    await deactivate(1)
    await deactivate(2)
# both mails go out here, after COMMIT
```

The callbacks run in the order they were registered, after the slot has returned to the pool and outside the
transaction: an operation inside a callback takes a slot of its own and sees the committed rows. They run in the
context of the block, so other context variables keep their values. An explicit `tx.commit()` runs them before it
returns.

| End of the transaction | Callbacks |
|---|---|
| commit | run |
| rollback, an exception, a failed operation, an abort after the [commit deadline](#the-commit-deadline) | dropped |
| a savepoint is released | move to the enclosing block |
| a savepoint rolls back | the savepoint's callbacks, including those of savepoints inside it, are dropped |

A callback that raises stops the callbacks after it, and the end of the block (or `commit()`) raises its exception;
the transaction is committed at that point. `robust=True` logs the exception instead and continues. Outside a
transaction `adorm.on_commit()` runs the function at once.

```python
async with adorm.atomic() as tx:
    await Member.objects.create(name="Ana")
    tx.on_commit(search_index.refresh, robust=True)     # a failing refresh does not fail the request
```

The block runs the callbacks when the slot reports the commit with the `on_commit` [event](/topics/events), the
same event a remote slot sends.

## A failed operation

An operation that fails with a database error leaves the database transaction aborted, and so does a cancelled one.
After such a failure the block takes no further operations (`TransactionManagementError`), and its end rolls back.
When the block exits normally after catching the error, its end raises `TransactionManagementError`, with the
original error as `__cause__`, so a lost write does not pass as a commit:

```python
async with adorm.atomic():
    try:
        await Member.objects.create(name="Ana")          # IntegrityError
    except adorm.IntegrityError:
        pass
# TransactionManagementError: the transaction was rolled back
```

Catch the error around a nested `atomic()` instead, as in [Savepoints](#savepoints). Errors raised on the client
side, such as `DoesNotExist` from `get()` and a `NotSupportedError` without a SQLSTATE or driver exception (an
operation the driver does not support), do not affect the transaction. A `NotSupportedError` from the server
(SQLSTATE class 0A) does. An operation cancelled while it waits behind another one in the slot never reaches the
database and does not affect the transaction either.

## Tasks inside a transaction

The transaction is a context variable. Tasks created inside the block (`asyncio.create_task()`, `TaskGroup`,
`gather()`) inherit it and run their operations on the transaction's slot, one at a time, in the order they are
sent.

```python
async with adorm.atomic():
    async with asyncio.TaskGroup() as group:
        for name in ("Ana", "Bea", "Cene"):
            group.create_task(Member.objects.create(name=name))
# one transaction, one slot, three inserts
```

An `atomic()` block in such a task is a savepoint of the transaction. Savepoints opened by tasks inside the same
block run one after another: a second one waits until the first ends. Operations that other tasks send to the outer
block while a savepoint is open wait until it ends too, so a rollback to the savepoint never undoes them.

A task that waits for a savepoint, which in turn waits for that task (a task created before the savepoint and
awaited inside it), would wait for ever. After the pool's `acquire_timeout` the waiting task raises
`DeadlockError` instead.

```python
async with adorm.atomic():
    task = asyncio.create_task(Member.objects.create(name="Ana"))
    async with adorm.atomic():          # opens before the task sends its insert
        await task                      # DeadlockError in the task after acquire_timeout
```

### The end of the block

Exiting the block waits for the operations already sent by any task, then commits. An operation that a task sends
after that raises `TransactionClosed`; it does not run outside the transaction instead.

```python
async with adorm.atomic():
    task = asyncio.create_task(log_activity(member))    # inherits the transaction

await task       # TransactionClosed when log_activity() ran its operation after the commit
```

A savepoint block that another task still holds open when its enclosing block ends is closed with it: the
enclosing block rolls back to that savepoint before it commits, so the unfinished savepoint's writes are discarded.
Its later operations raise `TransactionClosed`, and so does its own end when it exits normally.

When another task ends the block with `tx.commit()` or `tx.rollback()`, the exit of the block waits for that end to
finish; when it failed, a normal exit raises its error.

In debug mode, the end of a block warns with a `RuntimeWarning` about tasks that ran operations in it and are still
running. `adorm.executor.set_debug(True)` turns debug mode on and `set_debug(False)` off; by default it follows
asyncio's debug mode (`asyncio.run(..., debug=True)` or `PYTHONASYNCIODEBUG=1`).

## Cancellation

Cancelling the task inside a block cancels the statement running in the slot; the block rolls back on its way out
and the slot returns to the pool. Cancelling the task while the block commits leaves the outcome of the commit to
the database; the slot then closes its connection and leaves the pool.

## The commit deadline

A slot inside a transaction expects its next request (an operation, the commit or the rollback) within the pool's
`commit_timeout`. When none arrives in time, for example because the client that held the block is gone, the slot
rolls the transaction back, ends the lease and leaves the pool, so the next request gets a fresh connection. A
request that is running does not count against the deadline. `commit_timeout` is 60 seconds by default; `None`
sets no deadline. A rollback that does not finish within `commit_timeout` either is cancelled, and the slot leaves
the pool all the same.

```python
pool = Pool(AsyncpgDriver(), profile, commit_timeout=60)

async with adorm.atomic():
    await Member.objects.filter(pk=1).update(active=False)
    await asyncio.sleep(120)               # the slot rolls back after 60 s
    await Member.objects.count()           # TransactionClosed
```

The end of such a block raises `TransactionClosed` when it exits normally.

## Events

Transactions emit no [events](/topics/events) of their own. They consume:

| Event | Interpretation |
|---|---|
| `ResultEvent`, `ErrorEvent`, `CancelledEvent` of each request they send (`begin`, `execute`, `savepoint`, `commit` …) | the result, error or cancellation of the awaited call |
| `CallbackEvent("on_commit")`, delivered by the slot before the outcome of a successful `commit` | the block runs its `on_commit` callbacks once the slot is back in the pool |

Other callbacks and backend events that the connection emits during the transaction reach the lease's `events`
queue, and the pool's `events` bus when the lease is released. `CallbackEvent("on_rollback")`, which the slot
delivers after a rollback and after an abort, goes the same way.
