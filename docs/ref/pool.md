# Pool and router

Pools of slots, the router that assigns their slots to sessions, and the leases and queues through which a flow
talks to a slot, importable from `adorm.executor`. `cancellation_outcome` and `CancellationOutcome` are also
importable from `adorm`. The [pool topic](/topics/pool) explains how they fit together.

```python
from adorm.executor import (
    cancellation_outcome,
    CancellationOutcome,
    CloseReason,
    held_leases,
    holding,
    on_request_started,
    Pool,
    Request,
    REQUEST_METHODS,
    RequestQueue,
    Router,
    running_requests,
    RunningRequest,
    Slot,
    SlotLease,
)
```

## Pool

### `adorm.executor.Pool(driver, profile, *, max_connections=0, acquire_timeout=30.0, commit_timeout=60.0, release_timeout=30.0, purge_stale_connections_at=300.0, check_schema=False)`

The slots of one connection profile. `driver` is a [`Driver`](/ref/drivers#adorm-drivers-driver), `profile` a
[`Profile`](/ref/drivers#adorm-drivers-profile), usually a [`ConnectionProfile`](/ref/settings#adorm-connectionprofile).
The pool serves the sessions whose `using` is `profile.name`. A [`Router`](#adorm-executor-router-pools-profiles-none)
with profiles creates its own pools.

| Argument | Meaning |
|---|---|
| `max_connections` | the maximum number of slots, a non-negative integer (`ValueError` otherwise). `0` opens new slots until the driver reports a refusal ([`Driver.is_refusal()`](/ref/drivers#is-refusal-exc)). After a refusal, with any `max_connections`, the pool holds at most as many slots as it had at that moment, until a slot leaves the pool |
| `acquire_timeout` | the default number of seconds an acquisition waits, positive and finite (`ValueError` otherwise); the profile layer of the `acquire_timeout` [connection option](/ref/options) |
| `commit_timeout` | the default number of seconds a slot inside a transaction waits for its lease's next request before it rolls the transaction back, ends the lease and leaves the pool; positive and finite (default 60), or `None` for no limit; the profile layer of the `commit_timeout` connection option |
| `release_timeout` | the number of seconds the profile's `on_release` hook may run before it is cancelled and the slot leaves the pool, and the number of seconds a released slot waits for its connection to settle ([`DriverConnection.settle()`](/ref/drivers#async-settle)); positive and finite (default 30) |
| `purge_stale_connections_at` | the number of seconds a slot may stay idle; then it closes its connection and leaves the pool ([`CloseReason.STALE`](#adorm-executor-closereason)). Positive and finite (default 300), or `None`: idle slots stay until the pool closes |
| `check_schema` | the start-up schema check: `False` (the default) none, `"warn"` or `"raise"` (below); any other value raises `ValueError` |

The profile's `on_acquire` and `on_release` attributes, when present and not `None`, are its
[hooks](/topics/profiles#hooks). A hook or `connect` that is not callable raises `TypeError`, and so does a coroutine
function with a threaded driver. The pool's `options` are the options the driver sets on every new connection
([`Driver.connection_defaults()`](/ref/drivers#connection-defaults-profile)), overridden by the profile's `options`
attribute when present and not `None`: the first layer of
[`connection_options()`](/ref/options#adorm-connection-options-options). A name the driver does not declare
([`Capabilities.check_options()`](/ref/drivers#check-options-options-driver-none)) raises `NotSupportedError`;
`acquire_timeout` or `commit_timeout` among them raises `ValueError`.

A threaded driver cancels a hook by asking its thread to stop the running call
([`ThreadedConnection`](/ref/drivers#adorm-drivers-threadedconnection)). A hook that does not stop extends the
release, or the acquisition, by up to the driver's `cancel_grace` seconds; the connection is then given up.

A request for a slot:

1. takes the idle slot returned last; an idle slot past `purge_stale_connections_at`, or not usable, met on the way
   leaves the pool;
2. else opens a new slot, when the limit allows and no other request waits;
3. else waits in a first-in, first-out queue. A request whose open was refused goes back to the head of the queue.

Taking an idle slot runs no query and no check beyond the connection's own `is_usable()`. Slots beyond what a
smaller load uses stay idle and close at `purge_stale_connections_at`. A pool serves one event loop and is used
through a [`Router`](#adorm-executor-router-pools-profiles-none).

With `check_schema`, the pool's first acquisition starts
[`check_models()`](/ref/checks#adorm-checks-check-models-models-execute) on every registered model, once per pool, in a
task of its own on one slot of the pool. No acquisition is served before it ends:

- acquisitions wait for it within their own timeout (`SessionTimeout`); a cancelled or timed-out acquisition leaves
  it running;
- `"warn"` logs the problems as one warning on the `adorm.executor` logger and serves the acquisitions;
- `"raise"` makes every acquisition of the pool raise
  [`SchemaCheckError`](/ref/exceptions#adorm-schemacheckerror-message-none-problems) with the problems, until the
  profile is replaced;
- a check that fails with an error (a driver without `check_table`, a lost connection) raises that error from the
  waiting acquisitions, and the next acquisition checks again.

```python
pool = Pool(DjangoDriver(), profile, max_connections=10, acquire_timeout=5, commit_timeout=60)
pool = Pool(AsyncpgDriver(), profile, purge_stale_connections_at=60)     # idle connections live a minute
pool = Pool(AsyncpgDriver(), profile, check_schema="raise")              # refuse to work on drifted tables
```

| Member | Meaning |
|---|---|
| `name` | the profile name |
| `driver`, `profile`, `max_connections`, `acquire_timeout`, `commit_timeout`, `release_timeout`, `purge_stale_connections_at`, `check_schema` | the constructor arguments |
| `on_acquire`, `on_release` | the profile's hooks, `None` when it has none |
| `connection_defaults` | the database options the driver sets on every new connection for the profile, checked |
| `options` | `connection_defaults` overridden by the profile's database connection options, checked, as a read-only mapping |
| `capabilities` | what the driver supports for the profile ([`Driver.capabilities_for(profile)`](/ref/drivers#capabilities-for-profile)), read once; operations, connection options and channels are checked against it before a slot is acquired |
| `limit` | the current maximum number of slots: the lower of `max_connections` and the slot count at the last refusal, `None` when neither applies |
| `size` | slots in the pool, opening ones included |
| `idle` | slots no lease holds |
| `in_use` | slots a lease holds or that are returning to the pool |
| `waiting` | requests waiting for a slot |
| `events` | an [`EventBus`](/ref/events#adorm-events-eventbus) for the callbacks and backend events a connection emits while no lease holds its slot, and for those a lease left unread |
| `closed` | whether `close()` was called |
| `retired` | whether its router stopped routing to it after a change of its profile |
| `router` | the router the pool was added to, `None` before |

#### `leases()`

The unreleased leases holding a slot of the pool.

#### `async wait_closed()`

Waits until `close()` is called; held slots may still be open then. A [`Listener`](/ref/events#adorm-listener) uses it
to end the waits of its `get()`.

#### `async close()`

Closes the idle slots; waiting and later acquisitions raise [`InterfaceError`](/ref/exceptions). Returns once the
idle slots, the slots being discarded and the released slots whose connections are settling are closed; the settling
ones are waited for at most the pool's `release_timeout`. A held slot closes when its lease is released; a slot still
opening closes once open, and its request raises `InterfaceError`. A second call does nothing.

## Router

### `adorm.executor.Router(pools=(), *, profiles=None)`

Assigns the slots of its pools, one pool per profile, to the sessions that request them. It is a
[`Runner`](/ref/sessions#adorm-executor-runner).

```python
router = Router([Pool(DjangoDriver(), default_profile), Pool(DjangoDriver(), reports_profile)])
set_runner(router)
```

With `profiles`, a [`ProfileRegistry`](/ref/settings#adorm-settings-profileregistry-pool-none), a session whose
`using` no added pool serves gets a pool created from the registry's profile of that name
([`resolve()`](/ref/settings#adorm-settings-profileregistry-pool-none), which may ask its providers), with the
profile's `pool` settings or the registry's. Concurrent first uses of a name create one pool.

The router retires a pool created from a profile (stops routing to it and closes it with
[`Pool.close()`](#async-close)) when the profile is unregistered or replaced by another object, or when the registry's
default pool settings change and the profile has none of its own. The next acquisition creates a pool from the
current profile. An acquisition that waits on a retired pool, or reaches it before it waits, continues on the pool
that serves the profile, within its original timeout; `run()` then checks the operation against that pool's driver.
Pools added with `add()` are not affected by profile changes. The default routers of the settings
([`default_router()`](/ref/settings#adorm-settings-default-router-create-true)) are built this way.

```python
router = Router(profiles=adorm.settings.profiles)
```

When the driver's connections for a profile share one database session
([`Driver.transaction_turns()`](/ref/drivers#async-transaction-turns-profile) returns
[`TransactionTurns`](#adorm-executor-turns-transactionturns-name-advice), as inside Django's `TestCase`), `slot()` takes the
exclusive turn and `run()` a shared one before the acquisition, within its timeout. A flow inside a transaction that
holds the turn takes none. The lease holds the turn until the slot has been returned or has left the pool.
`acquire()` takes no turn.

| Member | Meaning |
|---|---|
| `profiles` | the registry, `None` without one |

#### `add(pool)`

Routes the sessions of `pool.name` to `pool` and returns it. A pool for a name that is already routed, and a pool
that belongs to another router, raise `ValueError`; anything but a `Pool` raises `TypeError`.

#### `pool(using)`

The routed pool of the profile `using`; none raises `InterfaceError`. A pool the router has not created from its
profiles is not routed.

#### `async get_pool(using)`

The pool of the profile `using`, created from `profiles` when the router has none. Raises `InterfaceError` when there
is neither a pool nor a profile, a provider's error, and the error of creating the profile's driver
([`get_driver()`](/ref/settings#get-driver)).

#### `async remove(using)`

Stops routing the sessions of `using` to its pool, closes the pool and returns it. Raises `InterfaceError` when no
pool serves `using`. A router with `profiles` creates a new pool on the next acquisition.

#### `pools`

The routed pools, as a tuple.

#### `async acquire(session, *, timeout=None)`

Acquires a slot of the pool of `session.using` (`get_pool()`) for `session` and returns a
[`SlotLease`](#adorm-executor-slotlease) owned by the current task. `timeout` (seconds, positive and finite) defaults
to the `acquire_timeout` [connection option](/ref/options) in effect: the pool's `acquire_timeout`, unless the session
or an enclosing `connection_options()` block sets one. The lease's `commit_timeout` is the `commit_timeout` option in
effect. A database option in effect that the driver does not declare raises `NotSupportedError` before the
acquisition waits. When the profile has an `on_acquire` hook, the acquisition calls it with the connection's
[`target`](/ref/drivers#adorm-drivers-driverconnection), the profile and `session`, in the current task's context.
The mapping of database options it returns, checked with
[`check_hook_options()`](/ref/options#adorm-executor-options-check-hook-options-result-hook-profile-name), becomes the
lease's `hook_options`, and the acquisition sets the options in effect with them
([`ResolvedOptions.for_lease()`](/ref/options#adorm-executor-options-resolvedoptions)) before it returns the lease.
After a hook that ran custom SQL every option the slot remembers counts as unknown and is set again.

| Outcome | Raised |
|---|---|
| no slot within `timeout` | [`SessionTimeout`](/ref/exceptions) with `session` and `timeout` |
| no slot within `timeout`, while the flow holds a slot | [`DeadlockError`](/ref/exceptions) with `held_sessions` |
| every slot of the pool is held, through `slot()` or `run()`, by tasks that wait for slots that cannot become free | `DeadlockError` at once, with `session`, `timeout` and `held_sessions` |
| the database refuses a connection, the pool has other slots and `timeout` passes | `SessionTimeout` or `DeadlockError` |
| the pool is closed, or no pool or profile serves `session.using` | `InterfaceError` |
| opening a connection fails other than by a refusal | the driver's error |
| `on_acquire` raises | its error, mapped by the connection; the slot leaves the pool |
| `on_acquire` returns neither `None` nor a mapping, or a mapping `check_hook_options()` refuses | `TypeError` or `ValueError`; the slot leaves the pool |
| `on_acquire` returns an option the driver does not declare | `NotSupportedError`; the slot leaves the pool |
| setting the options fails (a role the login may not become) | the driver's error; the slot leaves the pool |
| `on_acquire` and setting the options do not finish within `timeout` | `SessionTimeout` or `DeadlockError` whose message names the hook; the hook is cancelled and the slot leaves the pool |
| `slot()` or `run()`: the turn on a shared database session is not granted within `timeout`, or the transaction that holds it awaits the waiting flow | `DeadlockError` ([`TransactionTurns.take()`](#adorm-executor-turns-transactionturns-name-advice)) |

The flow holds a slot when the current task acquired an unreleased lease, or when the current context holds one
([`held_leases()`](#adorm-executor-held-leases)). A deadlock raised at once goes to the newest of the waiting tasks
that holds a slot of a pool one of them waits for; the others keep waiting. Only leases taken through `slot()` and
`run()` count for it; any task may release a lease returned by `acquire()`, so a wait on it ends at the timeout.
Cancelling the acquisition, or an enclosing `asyncio.timeout()`, removes the request from the queue; a slot handed to
it meanwhile goes to the next request.

```python
lease = await router.acquire(session, timeout=2)
try:
    rows = await lease.call("execute", operation)
finally:
    await lease.release()
```

#### `slot(session, *, timeout=None)`

An asynchronous context manager: `acquire()`, record the lease with [`holding()`](#adorm-executor-holding-lease)
for the block, release it on exit. The lease counts for deadlocks raised at once. On a shared database session it
first takes the exclusive turn (a transaction's).

```python
async with router.slot(session) as lease:
    await lease.call("begin")
    await lease.call("execute", operation)
    await lease.call("commit")
```

#### `held_sessions(task=None)`

The sessions of the unreleased leases a flow holds, without duplicates: those `task` acquired and, when `task` is the
current task (the default), those of `held_leases()`.

#### `async run(session, operation)`

Acquires a slot for `session`, executes `operation`, releases the slot and returns the result. The slot is released
also after an error or a cancellation.

- An operation the pool's driver does not support for the profile (`capabilities`), and a database connection option in effect
  ([`resolve_options()`](/ref/options#adorm-executor-options-resolve-options-session-pool)) that it does not declare,
  raise `NotSupportedError` ([`Driver.check()`](/ref/drivers#check-operation-schema-hashes-none-capabilities-none)) before a slot is
  acquired.
- The operation's request carries the database options in effect; the slot sets them before it executes the
  operation.
- On a shared database session it first takes a shared turn: it waits while a transaction it is not inside holds
  the turn.
- Enclosing [`adorm.timeout()`](/ref/options#adorm-timeout-delay) blocks see the operation in the phase
  `"waiting_for_slot"` until the slot is acquired, then `"executing"`.
- A cancellation raises `CancelledError` with the operation's [`CancellationOutcome`](#adorm-cancellationoutcome). It
  waits for the slot to be returned, so the outcome is final when raised, also after repeated cancellations:
  `NOT_STARTED` while waiting for a slot, else that of [`call()`](#async-call-method-args-options-none), or the
  operation's own outcome (`SUCCEEDED`, `FAILED`, `UNKNOWN` for a lost connection) when only the release was
  cancelled.

```python
await router.run(session, Select(Member.objects.annotate(n=Length("first_name")).query))
# NotSupportedError: 'select' operation needs unsupported clause.annotate, function.Length (driver 'memory')
# raised before a slot is acquired
```

```python
rows = await router.run(create_session("reports", "nightly"), operation)
```

#### `async close()`

Closes every pool, including those retired after a profile change.

## Held slots

### `adorm.executor.holding(lease)`

A synchronous context manager that records for its block that the current context holds `lease`. Tasks created
inside the block inherit the record. Anything but a `SlotLease` raises `TypeError`.

```python
lease = await router.acquire(session)
with holding(lease):
    await asyncio.gather(work_a(), work_b())   # a timeout here is a DeadlockError
```

### `adorm.executor.held_leases()`

The unreleased leases of the enclosing `holding()` and `Router.slot()` blocks of the current context, as a tuple.

## Transaction turns

`adorm.executor.turns` serialises the transactions on a database session that several slots share, such as the test
connection of Django's `TestCase` ([test mode](/topics/testing#transactions-in-a-testcase)).

```python
from adorm.executor.turns import awaits, DEADLOCK_CHECK_INTERVAL, TransactionTurns, Turn
```

### `adorm.executor.turns.TransactionTurns(name, advice="")`

The turns of one shared database session. A transaction holds the turn alone; operations outside a transaction hold
it together. Turns are granted in the order they were asked for: an operation asked for after a waiting transaction
waits for it. `name` describes the session in error messages; `advice`, when not empty, ends the message of every
`DeadlockError` raised for a wait. One object serves turns on several event loops one after another (the
`async_to_sync()` calls of a `TestCase` test): the turns of a closed loop are dropped, and the periodic check runs on
the loop of the latest wait.

| Member | Meaning |
|---|---|
| `name`, `advice` | the description of the session and the advice of the error messages |
| `holders` | the [`Turn`](#adorm-executor-turns-turn) objects held now, in the order they were granted |
| `waiting` | how many turns wait |
| `enclosed()` | whether the current context holds the unreleased lease of a transaction that holds the turn ([`held_leases()`](#adorm-executor-held-leases)); the router then takes no turn |

#### `async take(*, exclusive, session, deadline, timeout, held_sessions=...)`

Waits for a turn, exclusive for a transaction, and returns the [`Turn`](#adorm-executor-turns-turn). `deadline` is the
event loop time the wait ends at; `session`, `timeout` and `held_sessions(task)` (the router's
[`held_sessions()`](#held-sessions-task-none)) describe the error.

| Outcome | Raised |
|---|---|
| `deadline` passes | [`DeadlockError`](/ref/exceptions#adorm-deadlockerror-message-none-held-sessions-session-none-timeout-none) with `session`, `timeout` and `held_sessions` |
| the task of a transaction that holds the turn awaits the waiting task ([`awaits()`](#adorm-executor-turns-awaits-task-target)) | `DeadlockError` at once when the wait starts, else at the next check, every [`DEADLOCK_CHECK_INTERVAL`](#adorm-executor-turns-deadlock-check-interval) seconds |
| the waiting task is cancelled | `CancelledError`; a turn granted meanwhile goes to the next waiting turns |

```python
turns = TransactionTurns("the shared session")
turn = await turns.take(exclusive=True, session=session, deadline=loop.time() + 30, timeout=30)
try:
    ...                                  # the transaction
finally:
    turn.release()
```

#### `take_blocking(what, timeout=None)`

From a thread other than the event loop's: blocks until a shared turn is granted and returns the `Turn`, which
`release()` gives up from that thread. `what` describes the waiting work in the error. Returns `None` without waiting
when no turn has been taken on a running event loop yet, or when called on that loop's thread. `timeout` defaults to
the longest timeout of the transactions that hold or wait for the turn, else 30 seconds; past it the wait raises
`DeadlockError`. The task of the transaction that holds the turn is not followed to the thread, so a deadlock with a
thread is raised at the timeout.

### `adorm.executor.turns.Turn`

A flow's turn on a `TransactionTurns`.

| Member | Meaning |
|---|---|
| `exclusive` | `True` for a transaction's turn |
| `session`, `task`, `timeout` | the session, the task that asked for the turn and the acquisition's timeout; `session` and `task` are `None` for a turn of `take_blocking()` |
| `held` | whether the turn is granted and not released |
| `release()` | gives up the turn; the next waiting turns that fit are granted. A second call does nothing. From another thread, the release is scheduled on the turn's event loop, unless that loop is closed |

### `adorm.executor.turns.awaits(task, target)`

Whether `task` is `target` or waits for it: it awaits `target`, or a task or an `asyncio.gather()` that waits for it.
A wait through a plain future (`asyncio.wait()`, a `TaskGroup`, an `Event`) is not followed.

```python
async def both():
    await asyncio.gather(other(), target)


target = asyncio.create_task(work())
parent = asyncio.create_task(both())
await asyncio.sleep(0)
awaits(parent, target)       # True
```

### `adorm.executor.turns.DEADLOCK_CHECK_INTERVAL`

`0.05`: the seconds between the checks of waiting turns for a transaction whose task awaits the waiting task.

## Leases and queues

### `adorm.executor.SlotLease`

A slot handed to one flow by `Router.acquire()`. Use it as an asynchronous context manager to release it on exit.

| Member | Meaning |
|---|---|
| `requests` | the [`RequestQueue`](#adorm-executor-requestqueue) to write requests to |
| `channel` | the lease's [`EventChannel`](/ref/events#adorm-events-eventchannel-fallback-none), with the pool's `events` bus as fallback; it closes when the lease is released |
| `events` | `channel.events`, an `asyncio.Queue` of the [events](/ref/events) the channel does not interpret itself: the outcome (`ResultEvent`, `ErrorEvent` or `CancelledEvent`) of each request not sent with `call()`, in request order except that the `CancelledEvent` of a queued request arrives as soon as it is cancelled, the slot's `CallbackEvent("on_commit")` / `("on_rollback")` and the callbacks and backend events the connection emits while the lease is held, unless a handler registered with `channel.on()` takes them |
| `session` | the session the slot was acquired for |
| `pool`, `slot` | the pool and the [`Slot`](#adorm-executor-slot) |
| `owner` | the task that acquired the lease |
| `released` | whether `release()` was called or the slot aborted the lease's transaction |
| `aborted` | whether the slot rolled back the lease's transaction because no request arrived within `commit_timeout`; requests then raise [`TransactionClosed`](/ref/exceptions#adorm-transactionclosed) |
| `commit_timeout` | the seconds the slot waits for the lease's next request inside a transaction, `None` for no limit: the pool's `commit_timeout`, which `acquire()` replaces with the `commit_timeout` connection option in effect |
| `hook_options` | the database options the profile's `on_acquire` hook returned for the lease, a read-only mapping (empty without a hook): the layer between the profile's options and the session's |

```python
async with await router.acquire(session) as lease:
    lease.requests.put_nowait(Request("begin"))
    lease.requests.put_nowait(Request("execute", (operation,)))
    lease.requests.put_nowait(Request("commit"))
    for _ in range(4):
        event = await lease.events.get()       # three outcomes, and CallbackEvent("on_commit") before the last
```

#### `async call(method, *args, options=None)`

Sends `Request(method, args, options=options)` and returns its result or raises its error. The lease's channel
consumes the request's outcome, also after the caller has left; it does not appear on `events`. A lease the slot
aborted raises `TransactionClosed`, a released lease `InterfaceError`.

Cancelling the caller cancels the request: a queued request never runs; a running one is cancelled in the driver
connection. The raised `CancelledError` carries the request's [`CancellationOutcome`](#adorm-cancellationoutcome):
`NOT_STARTED` for a queued request, `SUCCEEDED` or `FAILED` when its outcome had arrived, else `UNKNOWN`, which
becomes `SUCCEEDED` or `FAILED` when the request finishes first.

A request the driver cancels on its own, a `CancelledEvent` for a request the caller did not cancel, and a driver
method that returns no coroutine fail with `InterfaceError`.

```python
await lease.call("execute", operation)
await lease.call("savepoint", "s1")
```

#### `async release(*, discard=False)`

Returns the slot to the pool once every queued request has run. The return completes also when the caller is
cancelled meanwhile.

- The slot closes its connection and leaves the pool instead ([`Slot.close_reason`](#adorm-executor-slot)) with
  `discard=True`, when a `begin` request left a transaction no successful `commit` or `rollback` ended, when a request
  failed with an error that leaves the connection unusable, when the connection ran custom SQL (also after a
  successful commit), and when the connection reports itself unusable.
- The lease's channel closes: callbacks and backend events left unread on `events` go to the pool's `events` bus;
  unread outcomes are dropped.
- A slot that stays in the pool first runs the profile's `on_release` hook with the connection's target, the profile
  and the lease's session; its return value is ignored. When the hook raises or runs longer than the pool's
  `release_timeout`, the error is logged and the slot leaves the pool; when it runs custom SQL, the slot leaves the
  pool too. The next request gets the slot after the hook has finished.
- When the connection has not settled ([`DriverConnection.settled`](/ref/drivers#settled)), `release()` returns at
  once and the slot decides once it has.
- After the first call the lease takes no requests (`InterfaceError`); a second call does nothing.

### `adorm.cancellation_outcome(exc)`

The [`CancellationOutcome`](#adorm-cancellationoutcome) of the adorm operation or request whose cancellation raised
`exc`: the `CancelledError` itself, or an exception it caused through `__cause__`, such as the `TimeoutError` of
`asyncio.timeout()`. `None` for any other exception. Set by [`call()`](#async-call-method-args-options-none), by
[`Router.run()`](#async-run-session-operation) (`NOT_STARTED` while it waits for a slot), by entering
[`atomic()`](/ref/transactions#adorm-atomic) (`NOT_STARTED` while it waits for a slot) and by
[`Atomic.execute()`](/ref/transactions#async-execute-operation) (`NOT_STARTED` while it waits for a savepoint of another
task). `UNKNOWN` turns into the request's outcome when the slot delivers it later. The value is final once the slot
has finished the request; releasing the lease waits for that. See [cancellation](/topics/cancellation).

```python
try:
    await Member.objects.filter(pk=1).update(active=False)
except asyncio.CancelledError as exc:
    if adorm.cancellation_outcome(exc) is adorm.CancellationOutcome.UNKNOWN:
        log.warning("member 1 may or may not be deactivated")
    raise
```

### `adorm.CancellationOutcome`

A `StrEnum` of what a cancelled request did in the database.

| Member | Value | Meaning |
|---|---|---|
| `NOT_STARTED` | `"not_started"` | it never reached the driver connection: no effect |
| `SUCCEEDED` | `"succeeded"` | it finished with a result before the cancellation reached it: it took effect |
| `FAILED` | `"failed"` | it finished with an error, other than a lost connection, before the cancellation reached it: no effect |
| `UNKNOWN` | `"unknown"` | it was cancelled while it ran, or failed with a lost connection ([`is_connection_error()`](/ref/drivers#adorm-drivers-is-connection-error-exc)): it may or may not have taken effect |

### `adorm.executor.slot.cancelled_before_start(exc)`

Whether `cancellation_outcome(exc)` is `CancellationOutcome.NOT_STARTED`: `exc` is the cancellation of a request that
never reached the driver connection.

```python
try:
    await lease.call("execute", operation)
except asyncio.CancelledError as exc:
    reached_database = not cancelled_before_start(exc)
    raise
```

### `adorm.executor.RequestQueue`

The writing end of a lease. Requests run on the slot one at a time, in the order they were put, each in the context
of the flow that put it.

| Method | Meaning |
|---|---|
| `put_nowait(request)` | queue a [`Request`](#adorm-executor-request-method-args-request-id-options-none); a lease the slot aborted raises `TransactionClosed`, a released lease `InterfaceError`, anything but a `Request` `TypeError` |
| `async put(request)` | the same; the queue is unbounded and never waits |
| `cancel(request)` | cancel a request of this lease: a queued one never runs, a running one is cancelled in the driver connection; its outcome on `events` is a `CancelledEvent`, or its result or error when it finished before the cancellation reached it. The `CancelledEvent` of a queued request arrives at once, ahead of the outcomes of earlier requests; results and errors keep the order of the requests. Returns whether the request was still queued; a request neither queued nor running is left alone |
| `async join()` | wait until every request queued so far has run |
| `qsize()` | queued requests that have not started |

### `adorm.executor.Request(method, args=(), request_id=..., *, options=None)`

A call of the driver connection's `method` with `args`, a frozen dataclass. `request_id` defaults to a number
unique in the process; the outcome event carries it. A method outside `REQUEST_METHODS` raises `ValueError`.
`options`, when not `None`, are the database connection options the call runs with, copied into a read-only mapping
(anything but a mapping raises `TypeError`). Before the call the slot sets those that differ from what it set on the
connection ([`Slot`](#adorm-executor-slot)). A `set_options` request sets options that the slot does not remember.

```python
Request("execute", (operation,))
Request("execute", (operation,), options={"statement_timeout": 5})
Request("set_options", ({"statement_timeout": 5}, True))
```

### `adorm.executor.REQUEST_METHODS`

The [`DriverConnection`](/ref/drivers#adorm-drivers-driverconnection) methods a request can call: `execute`,
`begin`, `commit`, `rollback`, `savepoint`, `release_savepoint`, `rollback_to_savepoint`, `set_options`, `listen`
and `unlisten`.

### `adorm.executor.Slot`

An exclusive channel to one driver connection of a pool, created by the pool. A task of its own runs the requests
of the current lease; for a threaded driver each connection also has its own thread. When that task fails, the
queued and later requests fail with `InterfaceError` and the slot is unusable.

The slot delivers to its lease's channel exactly one outcome per request: `ResultEvent`, `ErrorEvent`, or
`CancelledEvent` for a request cancelled before it finished (`started=False` when it was still queued or was cancelled
before the slot handed it to the connection, as while its connection options were set outside a transaction; inside a
transaction a cancelled `SET LOCAL` gives `started=True`).
Before the outcome of a successful `commit` it delivers `CallbackEvent("on_commit")`, before that of a successful
`rollback` `CallbackEvent("on_rollback")`. Events the connection emits go to the lease's channel, or to the pool's
`events` bus while no lease holds the slot. The slot accepts only `CallbackEvent` and `BackendEvent` from the
connection; any other event, and a `CallbackEvent` named `on_commit` or `on_rollback`, is dropped and logged as an
error.

While its lease is inside a transaction (a successful `begin` not yet ended by a successful `commit` or `rollback`)
and no request is queued or running, the slot waits at most the lease's `commit_timeout` for the next request. Then it
calls the connection's `rollback()` (a failure is logged; one that does not finish within `commit_timeout` is
cancelled and the connection's `cancel()` is called), delivers `CallbackEvent("on_rollback")`, marks the lease as
aborted and released, and leaves the pool with its connection closed.

The slot remembers the database connection options it set on its connection:

- Before a request with `options` it calls the connection's `set_options()` once, with every option whose value
  differs from the remembered one and `None` (the database's default) for every remembered option the request lacks;
  when all match, it calls nothing.
- Outside a transaction it sets them for the connection (`local=False`, `SET`). Inside a transaction it sets them for
  the transaction (`local=True`, `SET LOCAL`) and forgets them when the transaction ends. A rollback to a savepoint
  restores what was remembered when the savepoint was created; a released savepoint keeps them.
- A setting that fails or is cancelled leaves those options unknown; the next request with options sets them again.
  The request's call does not run; its outcome is the error, or `CancelledEvent(started=False)` for a cancellation.
- A new connection starts with the pool's `connection_defaults` remembered.
- After an `on_acquire` hook that ran custom SQL, every remembered option is unknown.
- Options that a `set_options` request or the driver set are not remembered.

When its lease ends, the slot waits at most the pool's `release_timeout` for the connection to settle
([`DriverConnection.settle()`](/ref/drivers#async-settle)), then decides whether it stays in the pool. It closes its
connection and leaves the pool for the first of these reasons that applies:

| Reason | When |
|---|---|
| `DISCARDED` | the lease was released with `discard=True` |
| `ABORTED` | the slot aborted the lease's transaction (`commit_timeout`) |
| `POOL_CLOSED` | the pool is closed |
| `CONNECTION_ERROR` | a request of the lease failed with an error that [`is_connection_lost()`](/ref/drivers#adorm-drivers-driverconnection) accepts (a detector that raises counts as accepting), whether or not the connection reports itself unusable |
| `UNCLEAN_END` | the lease ended inside a transaction: no successful `commit` or `rollback` ended its `begin` |
| `CUSTOM_SQL` | the connection ran SQL adorm did not generate ([`ran_custom_sql`](/ref/drivers#ran-custom-sql)), even when the transaction committed |
| `UNUSABLE` | the connection reports itself unusable, the slot's serving task stopped, or deciding raised an unexpected error |
| `LISTENING` | the connection still listens on a notification channel ([`listening`](/ref/drivers#adorm-drivers-driverconnection)) |
| `UNSETTLED` | the connection did not settle within `release_timeout` |
| `HOOK_FAILED` | the profile's `on_release` hook failed or timed out |

A lease whose `on_acquire` hook failed ends with `HOOK_FAILED` before any of these. A slot that leaves the pool skips
the `on_release` hook. The profile's `on_acquire` hook and the options it returns, when there is one, are the only work
done on the connection for a new lease before its first request.

```python
lease = await router.acquire(session)
await lease.call("begin")
await lease.call("execute", operation)
await lease.release()                       # no commit: the connection closes
lease.slot.close_reason                     # CloseReason.UNCLEAN_END
```

```python
await lease.call("execute", count, options={"statement_timeout": 5})   # set_options({"statement_timeout": 5}, False)
await lease.call("execute", count, options={"statement_timeout": 5})   # nothing to set
await lease.call("execute", count, options={})                         # set_options({"statement_timeout": None}, False)
```

| Member | Meaning |
|---|---|
| `slot_id` | a number unique in the process |
| `pool` | the pool |
| `connection` | the [`DriverConnection`](/ref/drivers#adorm-drivers-driverconnection) |
| `lease` | the current lease, `None` while idle |
| `state` | `"idle"`, `"leased"`, `"finishing"` or `"closed"` |
| `idle_since` | the event loop time (`loop.time()`) the slot became idle, `None` while it is not idle |
| `close_reason` | the [`CloseReason`](#adorm-executor-closereason) the slot left the pool for, `None` while it is open |
| `options` | the database options in effect by what the slot set: those set for the connection, replaced inside a transaction by those set for it; `None` stands for the database's default, unknown ones are left out |
| `running` | the [`RunningRequest`](#adorm-executor-runningrequest) whose driver connection call runs now, `None` when none does |

#### `async open()`

Opens the driver connection for the pool's profile and starts serving requests. The pool calls it.

#### `async set_options_for(desired)`

Sets the database options `desired` on the connection outside a request, as before a request with `options`: only
the differences. The slot must be leased and idle.

#### `forget_options()`

Makes every option the slot remembers unknown, so the next request with options sets it again.

#### `async wait_closed()`

Waits until the slot is closed (`close()`). A [`Listener`](/ref/events#adorm-listener) waits for it to end the waits
of its `get()`.

#### `is_usable()`

Whether the slot can serve another lease: not closed, its connection usable and its serving task running.

#### `async close(reason=CloseReason.POOL_CLOSED)`

Stops serving and closes the connection; `close_reason` becomes `reason`. The running, queued and later requests
fail with `InterfaceError`; a lease holding the slot frees the pool's capacity when it is released. Errors are logged,
not raised; a second call does nothing.

## Running requests

A request runs from when the slot hands it to the driver connection, after setting its connection options, until
the connection call ends by a result, an error, a cancellation or the slot closing. A request cancelled while queued
or while its options are set never runs. [`adorm.testing`](/ref/testing#waiting-for-the-database) uses these to wait
in tests until an operation is in the database.

### `adorm.executor.RunningRequest`

A frozen dataclass, compared by identity: a request whose driver connection call has started.

| Member | Value |
|---|---|
| `request` | the [`Request`](#adorm-executor-request-method-args-request-id-options-none) |
| `session` | the session of the lease the request was sent on |
| `sender` | the task that sent the request (for an operation, the task that awaits it, also inside a transaction), `None` when none did |
| `slot` | the [`Slot`](#adorm-executor-slot) it runs on |
| `operation` | the operation of an `execute` request, `None` for `begin`, `commit` and the other methods |
| `backend_pid` | the slot connection's [`backend_pid`](/ref/drivers#backend-pid) |
| `running` | whether the call has not ended yet |
| `in_session` | whether the call runs and has its database session now ([`holds_session`](/ref/drivers#holds-session-session-holds)); on a connection with a session of its own, `running` |
| `reached_session` | whether the call has had its database session, while it runs or before it ended; always for a connection with a session of its own, and for one that shares its session only after the call took it ([`session_holds`](/ref/drivers#holds-session-session-holds)) |

### `adorm.executor.running_requests()`

The `RunningRequest` of every request whose driver connection call runs now on a slot of the running event loop, in
the order they started; those of other event loops are left out. Raises `RuntimeError` outside a running event loop.

```python
task = asyncio.create_task(Member.objects.count())
...
[r.operation for r in running_requests() if r.sender is task]     # [Count(...)] while the count runs
```

### `adorm.executor.on_request_started(callback)`

Calls `callback(running)` with the `RunningRequest` whenever the driver connection call of a request starts on a slot
of the running event loop, and returns a function that stops it; calling that function again does nothing. The
callback runs on the loop, in the slot's task, before the connection method runs; an exception it raises is logged on
`adorm.executor` and the request still runs. Callbacks registered on another event loop are not called. Raises
`RuntimeError` outside a running event loop and `TypeError` for a `callback` that is not callable.

```python
sent = []
stop = on_request_started(lambda running: sent.append(running.operation))
try:
    await Member.objects.filter(active=True).update(active=False)
finally:
    stop()
sent      # [Update(...)]
```

### `adorm.executor.CloseReason`

A `StrEnum` of why a slot closed its connection and left the pool, the value of
[`Slot.close_reason`](#adorm-executor-slot).

| Member | Value | Meaning |
|---|---|---|
| `DISCARDED` | `"discarded"` | its lease was released with `discard=True` |
| `ABORTED` | `"aborted"` | it rolled back a transaction that received no request within the lease's `commit_timeout` |
| `UNCLEAN_END` | `"unclean_end"` | its lease ended inside a transaction |
| `CONNECTION_ERROR` | `"connection_error"` | a request failed with an error that leaves the connection unusable |
| `CUSTOM_SQL` | `"custom_sql"` | the connection ran SQL adorm did not generate |
| `UNUSABLE` | `"unusable"` | the connection reported itself unusable, or the slot stopped serving requests |
| `LISTENING` | `"listening"` | the connection still listened for notifications when its lease ended |
| `UNSETTLED` | `"unsettled"` | the connection did not finish the work of its last call within the pool's `release_timeout` |
| `HOOK_FAILED` | `"hook_failed"` | running the profile's `on_acquire` hook and setting the options it returned, or running its `on_release` hook, failed or timed out |
| `STALE` | `"stale"` | it stayed idle for the pool's `purge_stale_connections_at` seconds |
| `POOL_CLOSED` | `"pool_closed"` | its pool was closed |
