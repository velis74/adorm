# Pool and router

::: info Status
The installed version provides pools, the router, slots and leases, used by [transactions](/topics/transactions)
and built from [connection profiles](/topics/profiles), with [connection options](/topics/options) and the
[slot lifecycle](#slot-lifecycle). The [Django driver](/topics/django-driver) and the [native PostgreSQL driver](/topics/postgresql-driver) execute
reads, writes, row locks and savepoints. Any `Driver` implementation (see [Writing a driver](/howto/writing-a-driver)) works.
:::

A **pool** holds the slots of one connection profile. A **slot** is an exclusive channel to one driver connection;
for a threaded driver (the Django ORM) it also has its own thread. The **router** assigns slots to the sessions
that ask for one: a session's `using` names the pool.

`adorm.configure()` is all an application needs: the default router of each event loop creates the pool of a
profile when a session first names it (see [connection profiles](/topics/profiles)). Pools and routers can also be
built in code, as on the rest of this page:

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

When the pool's profile has [hooks](/topics/profiles#hooks), `acquire()` runs `on_acquire` before it returns the
lease, within the acquisition's timeout, and `release()` runs `on_release` before the slot goes to the next request,
within the pool's `release_timeout`. A hook that fails takes its slot out of the pool.

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

A slot inside a transaction waits at most the lease's `commit_timeout` for the next request of its lease: the
pool's `commit_timeout` (60 seconds by default, `None` for no limit), or the `commit_timeout`
[connection option](/topics/options) in effect when the transaction acquired the slot. When none arrives, the slot rolls the transaction back and leaves the pool; the lease is then
`aborted`, and its requests raise `TransactionClosed`. This frees the slot of a client that is gone.

```python
pool = Pool(AsyncpgDriver(), profile, commit_timeout=60)
```

A driver method that does not return a coroutine fails its request with `InterfaceError`. When the slot's serving
task itself fails, every queued request fails with `InterfaceError` and the slot leaves the pool on release.

## Slot lifecycle

adorm decides how long a connection lives, not the target ORM (Django's `CONN_MAX_AGE` does not apply). Acquiring a
slot does nothing to its connection: no ping, no reset, no query; the profile's `on_acquire` hook is the only work
before a lease's first request. The connection's state is kept clean by closing it instead: when a lease ends, the
slot closes its connection and leaves the pool, and the next request gets a fresh connection, when

- the session ended without a commit or rollback: its client is gone, a `commit` or `rollback` failed, or a lease
  inside a transaction was released (a transaction the slot aborts after `commit_timeout` leaves the same way);
- a request failed with a connection error ([`is_connection_lost()`](/ref/drivers#adorm-drivers-driverconnection),
  such as PostgreSQL's `57P01` after `pg_terminate_backend()`), even when the driver still reports the connection
  usable;
- the connection ran custom SQL, SQL that adorm did not generate, such as a Django signal receiver's
  `cursor.execute()` ([`ran_custom_sql`](/ref/drivers#ran-custom-sql)), also when the transaction committed: that SQL
  may have changed the connection's state;
- the connection still listens on a notification channel
  ([`listening`](/ref/drivers#adorm-drivers-driverconnection)), as after an
  [`adorm.listen()`](/topics/events#listening-for-notifications) block that did not unlisten: a later lease would
  receive its notifications;
- the connection reports itself unusable, or the lease was released with `discard=True`.

```python
from django.db import connection
from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender=Member)
def audit(instance, **kwargs):                  # runs on the slot's thread, inside the driver's save()
    with connection.cursor() as cursor:
        cursor.execute("SET search_path TO audit, public")
        cursor.execute("INSERT INTO log (member_id) VALUES (%s)", [instance.pk])


async with adorm.atomic():
    await member.save()                         # the receiver's SQL marks the connection
# committed; the slot then closes its connection, and the next session gets a fresh one
```

An idle slot closes its connection and leaves the pool after `purge_stale_connections_at` seconds (300 by default,
`None` to keep idle slots until the pool closes). A request takes the idle slot returned last, so under a smaller
load the slots it no longer needs stay idle and reach that age; a request never takes a slot past it, also when the
loop is too busy to run the purge on time.

```python
pool = Pool(DjangoDriver(), profile, purge_stale_connections_at=60)
adorm.configure(profiles={...}, pool={"purge_stale_connections_at": 60})       # the default of every profile
```

`slot.close_reason` tells why a slot left the pool ([`CloseReason`](/ref/pool#adorm-executor-closereason)), and the
`adorm.executor` logger records it at debug level.

## Requests and events

A lease is a pair of queues, like the reader and writer of an asyncio stream: `lease.requests` takes
[`Request`](/ref/pool#adorm-executor-request-method-args-request-id-options-none) objects, and `lease.events` delivers what the
slot sends back that the lease's [event channel](/topics/events) (`lease.channel`) does not interpret itself. The slot runs the requests one at a time, in the order they were put, each in the context of the
task that put it.

```python
from adorm.events import CallbackEvent, ErrorEvent, ResultEvent
from adorm.executor import Request

begin, insert, commit = Request("begin"), Request("execute", (operation,)), Request("commit")
for request in (begin, insert, commit):
    lease.requests.put_nowait(request)

for _ in range(4):
    match await lease.events.get():
        case ResultEvent(request_id, value): ...
        case ErrorEvent(request_id, error): ...
        case CallbackEvent("on_commit"): ...       # before the commit's ResultEvent
```

`lease.requests.cancel(request)` cancels a request: a queued one never runs, a running one is cancelled in the driver
connection. Its outcome is a `CancelledEvent`, or its result or error when it finished first. The `CancelledEvent` of
a queued request arrives as soon as it is cancelled, ahead of the outcomes of earlier requests. Cancelling the task
that awaits `lease.call()` cancels its request the same way; the raised `CancelledError` carries the request's
[`CancellationOutcome`](/ref/pool#adorm-cancellationoutcome) ([cancellation](/topics/cancellation)).

`lease.call(method, *args)` sends one request and returns its result or raises its error; the channel settles
the call with the outcome, so it does not appear on `events`. Concurrent calls on one lease queue behind each other. A request that the driver or the slot
cancels without the caller cancelling it fails with `InterfaceError`.

`release()` waits until every queued request has run, then returns the slot. It completes even when the releasing
task is cancelled meanwhile, so a cancelled flow does not leak its slot.

A request runs from the moment the slot hands it to the driver connection, after its connection options are set,
until the connection call ends. `slot.running` is that [`RunningRequest`](/ref/pool#adorm-executor-runningrequest):
the request, the session, the task that sent it and the slot. [`running_requests()`](/ref/pool#adorm-executor-running-requests)
lists those of the event loop's slots, and [`on_request_started()`](/ref/pool#adorm-executor-on-request-started-callback)
calls a function as each one starts. On a connection that shares its database session with others, as in Django's
`TestCase`, a running call may still wait for that session; `in_session` tells whether it has it now. The
[test helpers](/topics/testing#waiting-until-an-operation-is-in-the-database) build on them.

```python
stop = on_request_started(lambda running: print(running.session, running.operation))
await Member.objects.count()          # Session('default', 'default') Count(...)
stop()
```

## Timeouts

Every acquisition has a timeout: the `timeout` argument, else the `acquire_timeout` [connection option](/topics/options)
in effect, which is the pool's `acquire_timeout` (30 seconds by default) unless the session or a
`connection_options()` block sets one.

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
`InterfaceError`. `close()` returns once the idle slots, the slots being discarded and the released slots whose
connections are still settling (within `release_timeout`) are closed.

## Events

The pool and router emit no events of their own; the start of a request's connection call reaches the functions
registered with `on_request_started()`, not an event channel. A slot delivers events to the
[event channel](/topics/events) of its lease:

| Event | Delivered by | Where it goes |
|---|---|---|
| `ResultEvent`, `ErrorEvent`, `CancelledEvent` | the slot, exactly one per request | the result of `call()`, else the lease's `events` queue |
| `CallbackEvent("on_commit")`, `CallbackEvent("on_rollback")` | the slot, before the outcome of a successful `commit` or `rollback`; `on_rollback` also when the slot aborts a transaction | the handlers registered with `lease.channel.on()`, else the lease's `events` queue |
| other `CallbackEvent` values | the driver connection | the same; the pool's `events` bus while the slot is idle |
| `BackendEvent` (such as PostgreSQL `NOTIFY`) | the driver connection | the lease's `events` queue; the pool's `events` bus while the slot is idle |

When the lease is released its channel closes: callbacks and backend events left unread go to the pool's `events`
bus, unread outcomes are dropped. A `CallbackEvent` named `on_commit` or `on_rollback` that a connection emits is
dropped and logged.
