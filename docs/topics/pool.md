# Pool and router

::: info Status
The installed version provides pools, the router, slots and leases. Pools are built in code; connection profiles in
adorm's settings, transactions (`atomic()`), connection options and slot lifetimes are not part of it yet. Neither
are the Django ORM and native PostgreSQL drivers used in the examples; any `Driver` implementation (see
[Writing a driver](/howto/writing-a-driver)) works.
:::

A **pool** holds the slots of one connection profile. A **slot** is an exclusive channel to one driver connection;
for a threaded driver (the Django ORM) it also has its own thread. The **router** assigns slots to the sessions
that ask for one: a session's `using` names the pool.

```python
from dataclasses import dataclass, field

from adorm import create_session
from adorm.executor import Pool, Router, set_runner


@dataclass(frozen=True)
class Profile:                        # any object with these attributes is a profile
    name: str
    alias: str = "default"
    params: dict = field(default_factory=dict)
    connect: object = None


default_profile, reports_profile = Profile("default"), Profile("reports")

router = Router([
    Pool(DjangoDriver(), default_profile, max_connections=10),
    Pool(DjangoDriver(), reports_profile, max_connections=2, acquire_timeout=5),
])
set_runner(router)                    # operations outside a transaction run on the router

await Member.objects.count()          # acquires a slot of "default", executes, releases
```

## Acquiring a slot

`router.acquire(session)` returns a **lease**: the slot, handed to the current task until `release()`. A request
takes an idle slot, or opens a new one when the pool's limit allows, or waits in a first-in, first-out queue until
a lease returns a slot.

```python
lease = await router.acquire(create_session("reports", "nightly"))
try:
    rows = await lease.call("execute", operation)
finally:
    await lease.release()
```

`router.slot(session)` does the same as an `async with` block and records the lease as held by the block's context
(see [deadlocks](#deadlocks)):

```python
async with router.slot(session) as lease:
    await lease.call("begin")
    await lease.call("execute", operation)
    await lease.call("commit")
```

`router.run(session, operation)` is one operation on a slot of its own, the way an operation outside a transaction
runs.

### Pool size

`max_connections` limits the slots of a pool. With `0`, the default, the pool opens new connections until the
database refuses one; the driver decides what a refusal is (`Driver.is_refusal()`, by default SQLSTATE class `53`,
such as PostgreSQL's `too many connections`). After a refusal the request waits for a slot instead of failing, and
the pool opens no more slots than it had at that moment until a slot leaves the pool. This also applies below a
non-zero `max_connections`, when the database allows fewer connections than configured. A refusal when the pool has
no slot at all raises the driver's error. A request whose open was refused keeps its place at the head of the
queue.

```python
pool = Pool(AsyncpgDriver(), profile)          # max_connections=0
pool.limit                                     # None: no refusal yet
# ... the database refuses the 21st connection
pool.limit                                     # 20
```

A slot leaves the pool when its lease is released and the connection reports itself unusable, or a `begin` request
left a transaction that no successful `commit` or `rollback` ended (including one whose request was cancelled); the
next request opens a fresh connection. `lease.release(discard=True)` drops the slot in any case.

A driver method that does not return a coroutine fails its request with `InterfaceError`. When the slot's serving
task itself fails, every queued request fails with `InterfaceError` and the slot leaves the pool on release.

## Requests and events

A lease is a pair of queues, like the reader and writer of an asyncio stream: `lease.requests` takes
[`Request`](/ref/pool#adorm-executor-request-method-args-request-id) objects, and `lease.events` delivers what the
slot sends back. The slot runs the requests one at a time, in the order they were put, each in the context of the
task that put it.

```python
from adorm.events import ErrorEvent, ResultEvent
from adorm.executor import Request

begin, insert, commit = Request("begin"), Request("execute", (operation,)), Request("commit")
for request in (begin, insert, commit):
    lease.requests.put_nowait(request)

for _ in range(3):
    match await lease.events.get():
        case ResultEvent(request_id, value): ...
        case ErrorEvent(request_id, error): ...
```

`lease.call(method, *args)` sends one request and returns its result or raises its error; its outcome does not
appear on `events`. Concurrent calls on one lease queue behind each other. A request that the driver cancels on its
own, without the caller cancelling it, fails with `InterfaceError`.

`release()` waits until every queued request has run, then returns the slot. It completes even when the releasing
task is cancelled meanwhile, so a cancelled flow does not leak its slot.

## Timeouts

Every acquisition has a timeout: the pool's `acquire_timeout` (30 seconds by default) or the `timeout` argument.

```python
try:
    lease = await router.acquire(session, timeout=2)
except adorm.SessionTimeout as exc:
    log.warning("no slot for %r within %g s", exc.session, exc.timeout)
```

Cancelling an acquisition, or an `asyncio.timeout()` around it, removes the request from the queue. A slot that was
handed to the request just before goes to the next one.

## Deadlocks

A flow that already holds a slot and waits for another one can wait forever: the slot it needs may be held by a flow
that waits for the slot it holds. The router reports such waits with `DeadlockError`, a `SessionTimeout` whose
`held_sessions` lists the sessions of the slots the flow holds.

A flow holds a slot when its task acquired an unreleased lease, or when its context holds one: `router.slot()` and
[`holding(lease)`](/ref/pool#adorm-executor-holding-lease) record the lease in a context variable, so tasks created
inside the block hold it too.

```python
reports = create_session("reports", "nightly")

async with router.slot(create_session()):            # holds a slot of "default"
    try:
        await router.acquire(reports, timeout=2)     # every "reports" slot is busy for longer
    except adorm.DeadlockError as exc:
        log.error("holding %s", exc.held_sessions)    # (Session('default', 'default'),)
```

When the wait cannot end, the router does not wait for the timeout. The simplest case is a task that holds the only
slot of a pool through `router.slot()` and asks that pool for another one. In general: every slot of the pool is
held through `router.slot()` or `router.run()` by tasks that themselves wait for a slot, the pool cannot open
another one, and the same holds for the pools those tasks wait for. Of these tasks, the newest one that holds a slot
of such a pool gets `DeadlockError` at once, with its `session` and `timeout`; when it releases its slots, the others
proceed.

```python
one, two = create_session("one"), create_session("two")   # pools with max_connections=1

async def flow(mine, theirs):
    async with router.slot(mine):
        await both_hold.wait()
        async with router.slot(theirs):                     # one of the two flows raises DeadlockError
            ...

await asyncio.gather(flow(one, two), flow(two, one), return_exceptions=True)
```

A flow whose held slot belongs to a task that is not waiting for a slot (for example a parent task that awaits its
children) waits until the timeout, then gets `DeadlockError`. So does a flow whose slot was taken with
`router.acquire()`: any task may release that lease, so the router cannot tell that the wait will not end.

## Closing

`await router.close()` closes every pool: idle slots close at once, waiting requests raise `InterfaceError`, and a
held slot closes when its lease is released. A slot still opening is closed once open, and its request raises
`InterfaceError`. `close()` returns once the idle slots and the slots being discarded are closed.

## Events

The pool and router emit no events of their own and consume none. A slot carries the
[event channel](/ref/events) of its connection:

| Event | Direction | Where it goes |
|---|---|---|
| `ResultEvent`, `ErrorEvent` | emitted by the slot, one per request | the lease's `events` queue, or the result of `call()` |
| `CallbackEvent` (such as `on_commit`) | emitted by the driver connection | the lease's `events` queue while held; the pool's `events` bus while idle or when left unread at release |
| `BackendEvent` (such as PostgreSQL `NOTIFY`) | emitted by the driver connection | the same as `CallbackEvent` |

A request whose `call()` was cancelled before it started has no outcome event.
