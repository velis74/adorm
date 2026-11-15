# Pool and router

Pools of slots, the router that assigns their slots to sessions, and the leases and queues through which a flow
talks to a slot, importable from `adorm.executor`. The [pool topic](/topics/pool) explains how they fit together.

```python
from adorm.executor import (
    held_leases,
    holding,
    Pool,
    Request,
    REQUEST_METHODS,
    RequestQueue,
    Router,
    Slot,
    SlotLease,
)
```

## Pool

### `adorm.executor.Pool(driver, profile, *, max_connections=0, acquire_timeout=30.0, commit_timeout=60.0, release_timeout=30.0)`

The slots of one connection profile. `driver` is a [`Driver`](/ref/drivers#adorm-drivers-driver), `profile` a
[`Profile`](/ref/drivers#adorm-drivers-profile), usually a [`ConnectionProfile`](/ref/settings#adorm-connectionprofile);
the pool serves the sessions whose `using` is `profile.name`. A [`Router`](#adorm-executor-router-pools-profiles-none)
with profiles creates its pools itself.

| Argument | Meaning |
|---|---|
| `max_connections` | the maximum number of slots, a non-negative integer (`ValueError` otherwise). `0` opens new slots until the driver reports a refusal ([`Driver.is_refusal()`](/ref/drivers#is-refusal-exc)). After a refusal, with any `max_connections`, the pool holds at most as many slots as it had at that moment, until a slot leaves the pool |
| `acquire_timeout` | the default number of seconds an acquisition waits, positive and finite (`ValueError` otherwise) |
| `commit_timeout` | the number of seconds a slot inside a transaction waits for its lease's next request before it rolls the transaction back, ends the lease and leaves the pool; positive and finite (default 60), or `None` for no limit |
| `release_timeout` | the number of seconds the profile's `on_release` hook may run before it is cancelled and the slot leaves the pool; positive and finite (default 30) |

The profile's `on_acquire` and `on_release` attributes, when present and not `None`, are its
[hooks](/topics/profiles#hooks); a profile without them has none. A hook or `connect` that is not callable raises
`TypeError`, and so does a coroutine function with a threaded driver.

A threaded driver cancels a hook by asking its thread to stop the running call
([`ThreadedConnection`](/ref/drivers#adorm-drivers-threadedconnection)). A hook that does not stop extends the
release, or the acquisition, by up to the driver's `cancel_grace` seconds; the connection is then given up.

Slots open on demand: a request takes an idle slot, else opens a new one when the limit allows and no other
request waits, else waits in a first-in, first-out queue; a request whose open was refused goes back to the head of
the queue. A pool serves one event loop and is used through a [`Router`](#adorm-executor-router-pools-profiles-none).

```python
pool = Pool(DjangoDriver(), profile, max_connections=10, acquire_timeout=5, commit_timeout=60)
```

| Member | Meaning |
|---|---|
| `name` | the profile name |
| `driver`, `profile`, `max_connections`, `acquire_timeout`, `commit_timeout`, `release_timeout` | the constructor arguments |
| `on_acquire`, `on_release` | the profile's hooks, `None` when it has none |
| `limit` | the current maximum number of slots: the lower of `max_connections` and the slot count at the last refusal, `None` when neither applies |
| `size` | slots in the pool, opening ones included |
| `idle` | slots no lease holds |
| `in_use` | slots a lease holds or that are returning to the pool |
| `waiting` | requests waiting for a slot |
| `events` | an [`EventBus`](/ref/events#adorm-events-eventbus) for the callbacks and backend events a connection emits while no lease holds its slot, and for those a lease left unread |
| `closed` | whether `close()` was called |
| `router` | the router the pool was added to, `None` before |

#### `leases()`

The unreleased leases holding a slot of the pool.

#### `async close()`

Closes the idle slots and refuses further acquisitions with [`InterfaceError`](/ref/exceptions); waiting requests
raise `InterfaceError`. Returns once the idle slots and the slots being discarded are closed. A held slot closes when
its lease is released; a slot still opening closes once open, and its request raises `InterfaceError`. A second call
does nothing.

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
profile's `pool` settings or the registry's. Concurrent first uses of a name create one pool. When the profile is
unregistered or replaced by another object, the router stops routing to its pool and closes it
([`Pool.close()`](#async-close)); the next acquisition creates a pool from the current profile. Pools added with
`add()` are not affected by profile changes. The default routers of the settings
([`default_router()`](/ref/settings#adorm-settings-default-router-create-true)) are built this way.

```python
router = Router(profiles=adorm.settings.profiles)
```

| Member | Meaning |
|---|---|
| `profiles` | the registry, `None` without one |

#### `add(pool)`

Routes the sessions of `pool.name` to `pool` and returns it. A pool for a name that is already routed, and a pool
that belongs to another router, raise `ValueError`; anything but a `Pool` raises `TypeError`.

#### `pool(using)`

The routed pool of the profile `using`; none raises `InterfaceError`. A pool the router has not created from its
profiles yet is not routed.

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
to the pool's `acquire_timeout`. When the profile has an `on_acquire` hook, the acquisition calls it with the
connection's [`target`](/ref/drivers#adorm-drivers-driverconnection), the profile and `session`, in the current task's
context, before it returns the lease.

| Outcome | Raised |
|---|---|
| no slot within `timeout` | [`SessionTimeout`](/ref/exceptions) with `session` and `timeout` |
| no slot within `timeout`, while the flow holds a slot | [`DeadlockError`](/ref/exceptions) with `held_sessions` |
| every slot of the pool is held, through `slot()` or `run()`, by tasks that wait for slots that cannot become free | `DeadlockError` at once, with `session`, `timeout` and `held_sessions` |
| the database refuses a connection, the pool has other slots and `timeout` passes | `SessionTimeout` or `DeadlockError` |
| the pool is closed, or no pool or profile serves `session.using` | `InterfaceError` |
| opening a connection fails other than by a refusal | the driver's error |
| `on_acquire` raises | its error, mapped by the connection; the slot leaves the pool |
| `on_acquire` does not finish within `timeout` | `SessionTimeout` or `DeadlockError` whose message names the hook; the hook is cancelled and the slot leaves the pool |

The flow holds a slot when the current task acquired an unreleased lease, or when the current context holds one
([`held_leases()`](#adorm-executor-held-leases)). A deadlock raised at once goes to the newest of the waiting
tasks that holds a slot of a pool one of them waits for; the others keep waiting. Only leases taken through `slot()`
and `run()` count for it: a lease returned by `acquire()` may be released by any task, so a wait on it ends at the
timeout. Cancelling the acquisition, or an enclosing `asyncio.timeout()`, removes the
request from the queue; a slot handed to it meanwhile goes to the next request.

```python
lease = await router.acquire(session, timeout=2)
try:
    rows = await lease.call("execute", operation)
finally:
    await lease.release()
```

#### `slot(session, *, timeout=None)`

An asynchronous context manager: `acquire()`, record the lease with [`holding()`](#adorm-executor-holding-lease)
for the block, release it on exit. The lease counts for deadlocks raised at once.

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

Acquires a slot for `session`, executes `operation`, releases the slot and returns the result; the slot is released
also after an error or a cancellation. An operation the pool's driver does not support raises `NotSupportedError`
([`Driver.check()`](/ref/drivers#check-operation)) before a slot is acquired.

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
| `aborted` | whether the slot rolled back the lease's transaction because no request arrived within the pool's `commit_timeout`; requests then raise [`TransactionClosed`](/ref/exceptions#adorm-transactionclosed) |

```python
async with await router.acquire(session) as lease:
    lease.requests.put_nowait(Request("begin"))
    lease.requests.put_nowait(Request("execute", (operation,)))
    lease.requests.put_nowait(Request("commit"))
    for _ in range(4):
        event = await lease.events.get()       # three outcomes, and CallbackEvent("on_commit") before the last
```

#### `async call(method, *args)`

Sends `Request(method, args)` and returns its result or raises its error: the lease's channel awaits the
request's outcome, which therefore does not appear on `events`. A lease the slot aborted raises `TransactionClosed`,
a released lease `InterfaceError`. Cancelling the caller cancels the request: a queued request never runs; a running one is
cancelled in the driver connection. Either way its `CancelledEvent` is consumed by the channel. On the raised `CancelledError`,
[`cancelled_before_start()`](#adorm-executor-slot-cancelled-before-start-exc) tells the two apart. A request the
driver cancels on its own, a `CancelledEvent` for a request the caller did not cancel, and a driver method that
returns no coroutine fail with `InterfaceError`.

```python
await lease.call("execute", operation)
await lease.call("savepoint", "s1")
```

#### `async release(*, discard=False)`

Returns the slot to the pool once every queued request has run. `discard=True` closes the slot's connection
instead, as does a connection that reports itself unusable, and one that a `begin` request left in a transaction no
successful `commit` or `rollback` ended; the slot then leaves the pool. The lease's channel then closes: callbacks
and backend events left unread on `events` go to the pool's `events` bus; unread outcomes are dropped. The return completes
also when the caller is cancelled meanwhile. A slot that stays in the pool first runs the profile's `on_release` hook
with the connection's target, the profile and the lease's session; when the hook raises or runs longer than the
pool's `release_timeout`, the error is logged and the slot leaves the pool. The next request gets the slot only after
the hook has finished. After the first call, the lease takes no requests (`InterfaceError`) and a second call does
nothing.

### `adorm.executor.slot.cancelled_before_start(exc)`

Whether `exc` is the `CancelledError` of a [`call()`](#async-call-method-args) whose request was still queued, so
that it never reached the driver connection. `False` for a request cancelled while it ran, and for any other
exception.

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
| `put_nowait(request)` | queue a [`Request`](#adorm-executor-request-method-args-request-id); a lease the slot aborted raises `TransactionClosed`, a released lease `InterfaceError`, anything but a `Request` `TypeError` |
| `async put(request)` | the same; the queue is unbounded, so it never waits |
| `cancel(request)` | cancel a request of this lease: a queued one never runs, a running one is cancelled in the driver connection; its outcome on `events` is a `CancelledEvent`, or its result or error when it finished before the cancellation reached it. The `CancelledEvent` of a queued request arrives at once, ahead of the outcomes of earlier requests; results and errors keep the order of the requests. Returns whether the request was still queued; a request neither queued nor running is left alone |
| `async join()` | wait until every request queued so far has run |
| `qsize()` | queued requests that have not started |

### `adorm.executor.Request(method, args=(), request_id=...)`

A call of the driver connection's `method` with `args`, a frozen dataclass. `request_id` defaults to a number
unique in the process; the outcome event carries it. A method outside `REQUEST_METHODS` raises `ValueError`.

```python
Request("execute", (operation,))
Request("set_options", ({"statement_timeout": 5}, True))
```

### `adorm.executor.REQUEST_METHODS`

The [`DriverConnection`](/ref/drivers#adorm-drivers-driverconnection) methods a request can call: `execute`,
`begin`, `commit`, `rollback`, `savepoint`, `release_savepoint`, `rollback_to_savepoint` and `set_options`.

### `adorm.executor.Slot`

An exclusive channel to one driver connection of a pool, created by the pool. A task of its own runs the requests
of the current lease; for a threaded driver each connection also has its own thread. When that task fails, the
queued requests fail with `InterfaceError` and the slot is no longer usable.

The slot delivers to its lease's channel exactly one outcome per request: `ResultEvent`, `ErrorEvent`, or
`CancelledEvent` for a request cancelled before it finished (`started=False` when it was still queued).
Before the outcome of a successful `commit` it delivers `CallbackEvent("on_commit")`, before that of a successful
`rollback` `CallbackEvent("on_rollback")`. Events the connection emits go to the lease's channel, or to the pool's
`events` bus while no lease holds the slot. The slot accepts only `CallbackEvent` and `BackendEvent` from the
connection; any other event, and a `CallbackEvent` named `on_commit` or `on_rollback`, is dropped and logged as an
error.

While its lease is inside a transaction (a successful `begin` not yet ended by a successful `commit` or `rollback`)
and no request is queued or running, the slot waits at most the pool's `commit_timeout` for the next request. Then it
calls the connection's `rollback()` (a failure is logged; one that does not finish within `commit_timeout` is
cancelled and the connection's `cancel()` is called), delivers `CallbackEvent("on_rollback")`, marks the lease as
aborted and released, and leaves the pool with its connection closed.

| Member | Meaning |
|---|---|
| `slot_id` | a number unique in the process |
| `pool` | the pool |
| `connection` | the [`DriverConnection`](/ref/drivers#adorm-drivers-driverconnection) |
| `lease` | the current lease, `None` while idle |
| `state` | `"idle"`, `"leased"`, `"finishing"` or `"closed"` |

#### `async open()`

Opens the driver connection for the pool's profile and starts serving requests. The pool calls it.

#### `is_usable()`

Whether the slot can serve another lease: not closed, its connection usable and its serving task running.

#### `async close()`

Stops serving and closes the connection; logs errors instead of raising them.
