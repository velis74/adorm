# Connection options and timeouts

Two kinds of limits keep a slow database from holding up an application:

- **connection options**: limits the database enforces (`statement_timeout`, `lock_timeout`, …) and limits adorm
  enforces on slots (`acquire_timeout`, `commit_timeout`), set in layers;
- **`adorm.timeout()`**: a limit on the client side for everything a block of code does with adorm, which also tells
  where the time went when it expires.

The [reference](/ref/options) lists every name.

## Connection options

Options apply in three layers; for each option the last layer that sets it wins:

| Layer | Set with | Applies to |
|---|---|---|
| profile | `ConnectionProfile(options=...)` for database options, the profile's `pool` settings for `acquire_timeout` and `commit_timeout` | every session of the profile |
| session | `session.set_options(...)` | the session `(using, session_name)` |
| block | `with adorm.connection_options(...):` | every operation in the block, in tasks created inside it too |

```python
import adorm
from adorm import ConnectionProfile, PoolSettings

adorm.configure(
    profiles={
        "default": ConnectionProfile(
            "default",
            "adorm.drivers.asyncpg.AsyncpgDriver",
            options={"statement_timeout": 30, "lock_timeout": 5},     # seconds
            pool=PoolSettings(acquire_timeout=10, commit_timeout=60),
        ),
    }
)

reports = adorm.create_session("default", "reports")
reports.set_options(statement_timeout=600)          # reports may run longer

with adorm.connection_options(lock_timeout=0.5):
    await Member.objects.filter(pk=member_id).update(fee=0)   # gives up on a held row lock
```

`connection_options()` is a plain `with`: it only sets a context variable, like
[`Session.activate()`](/topics/sessions). Values are seconds; for the database's time limits `0` means no limit, the largest value is 2147483.647
(PostgreSQL's limit of 2147483647 ms) and `None` means the database's default, so a block can lift a limit that an outer layer sets:

```python
with adorm.connection_options(statement_timeout=None):
    await rebuild_statistics()
```

### What reaches the database

Every operation carries the database options in effect where it runs. The slot remembers what it set on its
connection and sends only the differences: an operation with the same options as the previous one on that slot
costs no extra statement, an option the previous operation set and this one lacks is reset.

| Where the operation runs | What the slot sends |
|---|---|
| outside a transaction | `SET name = value` for a changed option, `RESET name` for one no longer in effect; the values stay on the connection for the next operation |
| inside `atomic()` | `SET LOCAL name = value` and `SET LOCAL name TO DEFAULT`; they end with the transaction, so a rollback cannot leave the slot believing in a value the database discarded |
| in a savepoint that rolls back | the slot returns to the values it knew when the savepoint started, as the database does |

```python
async with adorm.atomic():
    with adorm.connection_options(lock_timeout=1):
        await Member.objects.filter(pk=1).update(active=False)   # SET LOCAL lock_timeout = 1000
    await Club.objects.count()                                   # SET LOCAL lock_timeout TO DEFAULT
```

A driver declares the options it supports in [`Capabilities.connection_options`](/ref/drivers#adorm-drivers-capabilities).
An operation run with an option its driver does not declare raises `NotSupportedError` before it takes a slot,
outside and inside a transaction, and leaves the transaction usable; an option whose value is `None` is not sent and
needs no declaration. The Django and the native PostgreSQL drivers declare `statement_timeout`, `lock_timeout` and
`idle_in_transaction_session_timeout`.

```python
with adorm.connection_options(statement_timeout=5):
    await Member.objects.count()
# NotSupportedError: unsupported connection options: statement_timeout (driver 'memory')
```

`atomic()` sends the options in effect with its `BEGIN`, as plain `SET` and `RESET` before the transaction starts,
so a transaction never runs with options another session left on the slot. The `COMMIT` runs with the options in
effect around the end of the block, which matters for deferred constraint triggers.

Profile hooks run on the same connection: `on_acquire` runs with the session's options already set, and after each
hook the slot sets its options again before the next operation, so a hook may set or reset them (`RESET ALL`,
`DISCARD ALL`). A value a hook sets holds until a layer sets that option; once that layer ends, the slot resets the
option to the database's default. The profile's `options` keep a value in every case.

### adorm's own options

`acquire_timeout` is how long an operation, or the entry of `atomic()`, waits for a slot before it raises
`SessionTimeout` (`DeadlockError` when the flow already holds a slot). `commit_timeout` is how long the slot of a
transaction waits for the next request before it rolls the transaction back; a transaction takes the value in effect
when it starts.

```python
with adorm.connection_options(acquire_timeout=0.5):
    try:
        await Member.objects.count()
    except adorm.SessionTimeout:
        return cached_count()

with adorm.connection_options(commit_timeout=600):
    async with adorm.atomic():                  # a slow import that awaits other services between writes
        ...
```

## `adorm.timeout()`

`async with adorm.timeout(5):` wraps `asyncio.timeout()`. When the deadline passes, the block's task is cancelled
as with `asyncio.timeout()`, which cancels a running statement in the database
([cancellation](/topics/cancellation)), and the block raises `OperationTimeout`, a `TimeoutError`. Operations in the
block, also those of tasks created in it, report their progress to it, so the error says where the time went:

| `OperationTimeout.phase` | The operation |
|---|---|
| `"waiting_for_slot"` | waited for a free slot: the pool is too small or other flows hold their slots too long |
| `"executing"` | was running on its slot: a slow query, a lock wait, a slow commit |
| `None` | no adorm operation was in progress: the time went elsewhere |

`operation` is the operation (or the transaction step, such as `"begin"` or `"commit"`) and `session` its session.
[`cancellation_outcome()`](/ref/pool#adorm-cancellation-outcome-exc) of the error is that operation's outcome, also
when it ran in a child task.

```python
try:
    async with adorm.timeout(2):
        async with asyncio.TaskGroup() as group:
            members = group.create_task(Member.objects.filter(active=True).count())
            clubs = group.create_task(Club.objects.count())
except adorm.OperationTimeout as exc:
    log.warning("%s on %r: %r", exc.phase, exc.session, exc.operation)
    outcome = adorm.cancellation_outcome(exc)       # NOT_STARTED, SUCCEEDED, FAILED or UNKNOWN
```

The two limits complement each other: `statement_timeout` is enforced by the database for each statement and works
even when the client is gone; `adorm.timeout()` bounds the whole block, waiting for slots included, and stops the
statement from the client side. A write that `adorm.timeout()` interrupts may or may not have taken effect;
[`cancellation_outcome()`](/ref/pool#adorm-cancellation-outcome-exc) tells which.
