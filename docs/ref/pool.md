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

### `adorm.executor.Pool(driver, profile, *, max_connections=0, acquire_timeout=30.0)`

The slots of one connection profile. `driver` is a [`Driver`](/ref/drivers#adorm-drivers-driver), `profile` a
[`Profile`](/ref/drivers#adorm-drivers-profile); the pool serves the sessions whose `using` is `profile.name`.

| Argument | Meaning |
|---|---|
| `max_connections` | the maximum number of slots, a non-negative integer (`ValueError` otherwise). `0` opens new slots until the driver reports a refusal ([`Driver.is_refusal()`](/ref/drivers#is-refusal-exc)). After a refusal, with any `max_connections`, the pool holds at most as many slots as it had at that moment, until a slot leaves the pool |
| `acquire_timeout` | the default number of seconds an acquisition waits, positive and finite (`ValueError` otherwise) |

Slots open on demand: a request takes an idle slot, else opens a new one when the limit allows and no other
request waits, else waits in a first-in, first-out queue; a request whose open was refused goes back to the head of
the queue. A pool serves one event loop and is used through a [`Router`](#adorm-executor-router-pools).

```python
pool = Pool(DjangoDriver(), profile, max_connections=10, acquire_timeout=5)
```

| Member | Meaning |
|---|---|
| `name` | the profile name |
| `driver`, `profile`, `max_connections`, `acquire_timeout` | the constructor arguments |
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

### `adorm.executor.Router(pools=())`

Assigns the slots of its pools, one pool per profile, to the sessions that request them. It is a
[`Runner`](/ref/sessions#adorm-executor-runner).

```python
router = Router([Pool(DjangoDriver(), default_profile), Pool(DjangoDriver(), reports_profile)])
set_runner(router)
```

#### `add(pool)`

Routes the sessions of `pool.name` to `pool` and returns it. A pool for a name that is already routed, and a pool
that belongs to another router, raise `ValueError`; anything but a `Pool` raises `TypeError`.

#### `pool(using)`

The pool of the profile `using`; none raises `InterfaceError`.

#### `pools`

The routed pools, as a tuple.

#### `async acquire(session, *, timeout=None)`

Acquires a slot of the pool of `session.using` for `session` and returns a [`SlotLease`](#adorm-executor-slotlease)
owned by the current task. `timeout` (seconds, positive and finite) defaults to the pool's `acquire_timeout`.

| Outcome | Raised |
|---|---|
| no slot within `timeout` | [`SessionTimeout`](/ref/exceptions) with `session` and `timeout` |
| no slot within `timeout`, while the flow holds a slot | [`DeadlockError`](/ref/exceptions) with `held_sessions` |
| every slot of the pool is held, through `slot()` or `run()`, by tasks that wait for slots that cannot become free | `DeadlockError` at once, with `session`, `timeout` and `held_sessions` |
| the database refuses a connection, the pool has other slots and `timeout` passes | `SessionTimeout` or `DeadlockError` |
| the pool is closed, or no pool serves `session.using` | `InterfaceError` |
| opening a connection fails other than by a refusal | the driver's error |

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
also after an error or a cancellation.

```python
rows = await router.run(create_session("reports", "nightly"), operation)
```

#### `async close()`

Closes every pool.

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
| `events` | an `asyncio.Queue` of [events](/ref/events): a `ResultEvent` or `ErrorEvent` per request, in request order, and the `CallbackEvent` and `BackendEvent` values the connection emits while the lease is held |
| `session` | the session the slot was acquired for |
| `pool`, `slot` | the pool and the [`Slot`](#adorm-executor-slot) |
| `owner` | the task that acquired the lease |
| `released` | whether `release()` was called |

```python
async with await router.acquire(session) as lease:
    lease.requests.put_nowait(Request("begin"))
    lease.requests.put_nowait(Request("execute", (operation,)))
    lease.requests.put_nowait(Request("commit"))
    for _ in range(3):
        event = await lease.events.get()       # ResultEvent(request_id, value) or ErrorEvent(request_id, error)
```

#### `async call(method, *args)`

Sends `Request(method, args)` and returns its result or raises its error. Its outcome does not appear on `events`.
Cancelling the caller cancels the request: a queued request never runs and has no outcome; a running one is
cancelled in the driver connection. A request the driver cancels on its own, and a driver method that returns no
coroutine, fail with `InterfaceError`.

```python
await lease.call("execute", operation)
await lease.call("savepoint", "s1")
```

#### `async release(*, discard=False)`

Returns the slot to the pool once every queued request has run. `discard=True` closes the slot's connection
instead, as does a connection that reports itself unusable, and one that a `begin` request left in a transaction no
successful `commit` or `rollback` ended; the slot then leaves the pool. Callbacks and backend
events left unread on `events` go to the pool's `events` bus; unread results are dropped. The return completes
also when the caller is cancelled meanwhile. After the first call, the lease takes no requests (`InterfaceError`)
and a second call does nothing.

### `adorm.executor.RequestQueue`

The writing end of a lease. Requests run on the slot one at a time, in the order they were put, each in the context
of the flow that put it.

| Method | Meaning |
|---|---|
| `put_nowait(request)` | queue a [`Request`](#adorm-executor-request-method-args-request-id); a released lease raises `InterfaceError`, anything but a `Request` `TypeError` |
| `async put(request)` | the same; the queue is unbounded, so it never waits |
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
