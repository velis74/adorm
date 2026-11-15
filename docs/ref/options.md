# Connection options and timeouts

Connection options in layers and the client-side time limit of adorm operations, in `adorm.executor.options`.
`connection_options` and `timeout` are also importable from `adorm`, `Timeout` from `adorm.executor`. The
[connection options topic](/topics/options) explains how they fit together.

```python
import adorm
from adorm.executor import Timeout
from adorm.executor.options import (
    ADORM_OPTIONS,
    block_options,
    check_options,
    MAX_TIMEOUT_OPTION,
    resolve_options,
    ResolvedOptions,
    session_options,
    set_session_options,
    TIMEOUT_OPTIONS,
)
```

## Options

| Option | Kind | Value | Applied by |
|---|---|---|---|
| `acquire_timeout` | adorm | seconds a slot acquisition waits, positive and finite | the router, when a session acquires a slot; a transaction, when it waits for a savepoint of another task |
| `commit_timeout` | adorm | seconds a slot inside a transaction waits for the next request, positive and finite, or `None` for no limit | the slot of a transaction, with the value in effect when the transaction acquired it |
| `statement_timeout` | database | seconds from `0` (no limit) to `MAX_TIMEOUT_OPTION`, or `None` for the database's default | the database, through the driver |
| `lock_timeout` | database | as `statement_timeout` | the database, through the driver |
| `idle_in_transaction_session_timeout` | database | as `statement_timeout` | the database, through the driver |
| any other name | database | what the driver accepts, or `None` for the database's default | the database, through the driver |

The layers, the last one wins: the profile (its pool settings for the adorm options, its
[`options`](/ref/settings#adorm-connectionprofile) for the database options), the session
([`Session.set_options()`](/ref/sessions#set-options-options)), then the enclosing `connection_options()` blocks,
innermost last. A database option is sent only to a driver that declares it in
[`Capabilities.connection_options`](/ref/drivers#adorm-drivers-capabilities); an operation run with an option its
driver does not declare raises [`NotSupportedError`](/ref/exceptions) before it takes a slot. An option whose value
is `None` is not sent and needs no declaration. The [Django driver](/topics/django-driver) and the
[native PostgreSQL driver](/topics/postgresql-driver) declare `statement_timeout`, `lock_timeout` and
`idle_in_transaction_session_timeout`.

### `adorm.connection_options(**options)`

A synchronous context manager that sets connection options for its block: it only sets a context variable, so it
uses `with`, not `async with`. Its options replace those of enclosing blocks, of the session and of the profile with
the same names; on exit the previous block layer is restored, also on an exception. Tasks created inside the block
inherit its options, also when they outlive it. `as` binds the block layer in effect inside the block, a read-only
mapping. Values are checked as in [`check_options()`](#adorm-executor-options-check-options-options); a refused value
raises `ValueError` and sets nothing.

```python
with adorm.connection_options(statement_timeout=5, lock_timeout=2):
    await Member.objects.filter(active=False).delete()

with adorm.connection_options(acquire_timeout=1):
    count = await Member.objects.count()          # SessionTimeout after 1 s without a slot

with adorm.connection_options(statement_timeout=60) as layer:
    with adorm.connection_options(statement_timeout=None):
        ...                                       # the database's default
```

Outside a transaction the slot sets database options for its connection (`SET`) and resets those a later operation
does not run with (`RESET`); inside a transaction it sets them for the transaction (`SET LOCAL`). It sends nothing
when the connection already has the values (see [`Slot`](/ref/pool#adorm-executor-slot)).

### `adorm.timeout(delay)`

A [`Timeout`](#adorm-executor-timeout) block of `delay` seconds; `None` sets no limit. A `delay` that is neither a
number nor `None`, or is NaN, raises `ValueError`.

```python
try:
    async with adorm.timeout(5):
        members = [m async for m in Member.objects.filter(active=True)]
except adorm.OperationTimeout as exc:
    if exc.phase == "waiting_for_slot":
        ...                                  # the pool is saturated
    else:
        log.warning("slow query on %r: %r", exc.session, exc.operation)
```

### `adorm.executor.Timeout`

A time limit for the adorm operations of an `async with` block. It wraps `asyncio.timeout()`: on expiry the task
running the block is cancelled, and the block raises [`OperationTimeout`](/ref/exceptions#adorm-operationtimeout-message-none-phase-none-operation-none-session-none-timeout-none)
instead of `TimeoutError`, from that `TimeoutError`. Other exceptions, a `TimeoutError` the block raises itself
included, pass through unchanged.

The operations of the block, including those of tasks that inherit its context (`asyncio.create_task()`,
`TaskGroup`, `gather()`), report what they do to it:

| `OperationTimeout` attribute | Value |
|---|---|
| `phase` | `"waiting_for_slot"` while the operation waits for a slot (or for a savepoint of another task in its transaction), `"executing"` once its request is sent |
| `operation` | the adorm operation; for a transaction's own requests their name: `"begin"`, `"commit"`, `"rollback"`, `"savepoint"`, `"release_savepoint"`, `"rollback_to_savepoint"` |
| `session` | the operation's session |
| `timeout` | `delay` |

The reported operation is the first one the expiry interrupted (its cancellation reached it), else the oldest one
still in progress, as for a task that is not awaited inside the block; with none, `phase`, `operation` and `session`
are `None`. [`cancellation_outcome()`](/ref/pool#adorm-cancellation-outcome-exc) of the error reports what the
interrupted operation did, also when it ran in a child task; for an operation still in progress it reports what the
`CancelledError` of the block reports. Nested
blocks each see the operations inside them; the block that expired raises.

| Method | Meaning |
|---|---|
| `when()` | the deadline in the event loop's time; `None` for no limit or before the block is entered |
| `reschedule(when)` | moves the deadline to `when` (the loop's time, `None` for no limit); `RuntimeError` before the block is entered |
| `expired()` | whether the deadline passed and cancelled the block |

A block is entered once; entering it again raises `RuntimeError`.

```python
async with adorm.timeout(10) as t:
    await prepare()
    t.reschedule(asyncio.get_running_loop().time() + 2)   # two more seconds from now
    await Member.objects.filter(pk=1).update(active=False)
```

## Layers and resolution

### `adorm.executor.options.check_options(options)`

`options`, a mapping, checked, as a read-only mapping. Raises `ValueError` for a name that is not an identifier, an
`acquire_timeout` that is not a positive, finite number of seconds, a `commit_timeout` that is neither that nor
`None`, and a value of `TIMEOUT_OPTIONS` that is neither a number of seconds from 0 to `MAX_TIMEOUT_OPTION` nor
`None`;
`TypeError` for anything but a mapping. Other values are checked by the driver that sets them.

```python
check_options({"statement_timeout": 5, "work_mem": "64MB"})   # mappingproxy({...})
check_options({"lock_timeout": -1})                           # ValueError
```

### `adorm.executor.options.block_options()`

The options of the enclosing `connection_options()` blocks of the current context, a read-only mapping.

### `adorm.executor.options.session_options(session)`

The options set on `session`, the same as [`Session.options`](/ref/sessions#options).

### `adorm.executor.options.set_session_options(session, options)`

Replaces the options of `session` with the mapping `options`, checked; an empty mapping removes them. Returns the
checked options. Thread-safe.

### `adorm.executor.options.resolve_options(session, pool)`

The [`ResolvedOptions`](#adorm-executor-options-resolvedoptions) in effect in the current context for `session` on
`pool`: the pool's `acquire_timeout`, `commit_timeout` and `options`, replaced by the session's options, replaced by
those of the enclosing blocks.

```python
with adorm.connection_options(statement_timeout=5):
    resolve_options(session, pool)
# ResolvedOptions(acquire_timeout=30.0, commit_timeout=60.0, database=mappingproxy({'statement_timeout': 5}))
```

### `adorm.executor.options.ResolvedOptions`

A frozen dataclass of the options in effect.

| Field | Meaning |
|---|---|
| `acquire_timeout` | seconds an acquisition waits |
| `commit_timeout` | seconds the slot of a transaction waits for its next request, `None` for no limit |
| `database` | the database options to set, without those whose value is `None` |

### `adorm.executor.options.ADORM_OPTIONS`

`frozenset({"acquire_timeout", "commit_timeout"})`: the options adorm applies itself; they never reach a driver.

### `adorm.executor.options.TIMEOUT_OPTIONS`

`frozenset({"statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout"})`: the database options whose
value `check_options()` checks as a number of seconds.

### `adorm.executor.options.MAX_TIMEOUT_OPTION`

`2147483.647`: the largest value of a `TIMEOUT_OPTIONS` option in seconds, PostgreSQL's limit of 2147483647
milliseconds.
