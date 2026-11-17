# Concepts

::: info Status
adorm is pre-release. The [API reference](/ref/) lists what the installed version provides.
:::

An application describes a query or a model operation; a slot in a pool, served by a driver, runs it. This page covers
sessions, slots, the pool and drivers.

```text
application code
   │  Member.objects.filter(...)        builds an operation, no I/O
   ▼
session (using, session_name)            picks the connection profile
   │  await ...                           hands the operation over
   ▼
pool of the profile ── slot ── slot     one slot per connection, acquired per call or per transaction
          │
          ▼
       driver connection                  Django ORM (threaded) or asyncpg (async)
```

## Connection profiles

A connection profile is a named entry in adorm's settings; its name is the `using` value, like a Django database
alias. A profile names the driver, the database alias, extra driver parameters and optional hooks:

- `connect` creates the connection instead of the driver's default,
- `on_acquire` runs when a session gets a slot and returns connection options for that session's use of it (for
  example the current user's `role`),
- `on_release` runs when the slot returns to the pool.

Pool settings are `max_connections`, `acquire_timeout`, `commit_timeout`, `release_timeout` and
`purge_stale_connections_at` (see [Pool](#pool) and [Slots](#slots)); a profile's `options` are its database
[connection options](#connection-options-and-timeouts).

```python
adorm.configure(profiles={"default": {"driver": "adorm.drivers.django.DjangoDriver", "alias": "default"}})
```

A call carries no connection parameters; each database account is a profile of its own. The settings plugin API
registers profiles at run time, for example one per database user ([connection profiles](/topics/profiles)).

## Sessions

A session is logical: the pair `(using, session_name)`. `create_session()` returns the open session with that
pair, or creates it.

```python
reports = adorm.create_session(using="reports", session_name="nightly")
same = adorm.create_session(using="reports", session_name="nightly")
assert reports is same
```

A context variable holds the current session; the default is `("default", "default")`. `session.activate()` sets
it for a `with` block:

```python
with reports.activate():
    await Activity.objects.count()        # runs on "reports"
```

### Bound models

`Model.objects` returns a **BoundModel**: the model bound to a session. `Activity.objects` binds to the current
session; `session.Activity.objects` binds to the given one.

```python
await Activity.objects.count()            # current session
await reports.Activity.objects.count()    # the "reports" session
```

Binding takes no slot. A BoundModel stored at module level stays bound to the session current at import. The
transaction is not part of the binding: an operation uses the active transaction of its session in the current
context ([sessions](/topics/sessions)).

## Slots

A slot is an exclusive channel to one driver connection, with two queues:

- the request queue: the client writes operations to it;
- the event queue: the slot writes results, callbacks (such as `on_commit`) and backend events to it.

The client's event channel handles each event: it returns a result, runs a callback or calls a subscribed function
([events](/topics/events)).

- **An atomic operation** outside a transaction acquires a slot, executes and releases it.
- **A transaction** holds one slot for the whole block. A context variable records the held slot; operations inside
  the block, including those of tasks that inherit the context, use it.

```python
async with adorm.atomic() as tx:          # acquires a slot, BEGIN
    member = await Member.objects.get(pk=1)
    member.active = False
    await member.save()
    async with adorm.atomic():            # savepoint on the same slot
        await Activity.objects.create(member_id=1, kind="deactivated")
# commit on normal exit, rollback on exception
```

- `tx.commit()` commits before the end of the block; `session.atomic()` opens the transaction on a given session.
- Exiting the block waits for the operations already sent, then commits. An operation that arrives later (from a
  task that inherited the context) raises `TransactionClosed`.
- A slot aborts its transaction when no request (an operation, the commit or the rollback) arrives within its
  [`commit_timeout`](/topics/options#adorm-s-own-options), the value in effect when the transaction started.

See [transactions](/topics/transactions).

Acquiring a slot does nothing to the connection; adorm, not the ORM, decides how long a connection lives. A slot
leaves the pool, and the next request gets a new connection, when:

- a session ends without commit or rollback (a lost client);
- the driver gets a connection error;
- the slot ran custom SQL, also after a commit;
- it was inactive for `purge_stale_connections_at` seconds ([slot lifecycle](/topics/pool#slot-lifecycle)).

### Connection options and timeouts

Database limits (`statement_timeout`, `lock_timeout`, …) and adorm's limits (`acquire_timeout` for slot
acquisition, `commit_timeout` for the commit deadline) are connection options, held in a context variable. They
apply in layers, the last one wins: profile, session, block.

```python
with adorm.connection_options(statement_timeout=5, lock_timeout=2):
    await Member.objects.filter(active=False).delete()
```

The slot remembers the values set on its connection and sends `SET` only for a changed value, `SET LOCAL` inside a
transaction. A driver declares the options it supports; an unsupported option raises `NotSupportedError`.

`adorm.timeout()` is a client-side limit. On expiry it raises `OperationTimeout`, a `TimeoutError`. Its `phase` is
`"waiting_for_slot"` or `"executing"` and its `operation` is the operation in progress, also in tasks created inside
the block.

```python
try:
    async with adorm.timeout(5):
        rows = [m async for m in Member.objects.all()]
except adorm.OperationTimeout as exc:
    log.warning("timed out while %s: %r", exc.phase, exc.operation)
```

See [connection options](/topics/options).

Cancelling the client task cancels its request: a queued request never runs, a running one is cancelled in the driver
connection. The `CancelledError` tells whether the operation took effect, did not, or whether that is unknown because
the cancellation raced its completion. An atomic block reports whether its transaction committed
([cancellation](/topics/cancellation)).

## Pool

adorm keeps a pool of slots per connection profile; sessions with the same `using` share it. `max_connections`
limits its size (`0`: new connections until the database refuses one). When every slot is taken, a request waits in a
queue. Every acquisition has a timeout:

| Situation | Exception |
|---|---|
| no slot became free in time | `SessionTimeout` |
| no slot became free in time, and the flow already holds a slot | `DeadlockError` (subclass of `SessionTimeout`), `held_sessions` lists the held slots |
| every slot of the pool is held, through `router.slot()` or `router.run()`, by tasks that themselves wait for a slot that cannot become free | `DeadlockError`, raised at once ([deadlocks](/topics/pool#deadlocks)); otherwise `DeadlockError` at the timeout |

See [pool](/topics/pool). `atomic()` acquires its slot on entry, so a timeout or `DeadlockError` is raised there,
never inside the block.

```python
audit = adorm.create_session(using="default", session_name="audit")

async with adorm.atomic():                 # holds a slot of the "default" profile
    try:
        async with audit.atomic():         # needs a second slot of the same pool
            ...
    except adorm.DeadlockError as exc:
        log.error("holding %s", exc.held_sessions)
```

## Drivers

A driver executes adorm operations on its target and returns plain data (rows keyed by adorm field names, scalars,
counts); adorm builds the model instances. The core imports no ORM; it reaches a target only through the
[driver contract](/topics/drivers). A driver runs in one of two ways:

- **threaded**: a synchronous ORM runs on a dedicated thread per slot. The Django ORM driver is threaded, because a
  Django connection is bound to its thread; it calls only the synchronous Django ORM.
- **async**: native coroutines without threads. The PostgreSQL driver runs every database call through asyncpg on the
  event loop; it uses Django only without I/O (SQL compilation, model metadata, the schema hash).

A threaded driver can also run [**direct**](/topics/drivers#execution-mode): the ORM runs on the event loop's
thread, one task per event loop (for example a Celery task through `celery_viewset`). Django's `async_unsafe` check
accepts adorm's calls and applies to everything else.

A separate adorm server is another driver: the client sends the operation description to the server (over
[muxws](:muxws:/) or HTTP), which executes it with an ORM driver. The server holds the profiles and their credentials;
the client sends only the profile name.

Errors are adorm's own [exceptions](/ref/exceptions) modelled on Django's (`DoesNotExist`, `IntegrityError`, …).
An error adorm does not recognise becomes a `DatabaseError` that carries the driver's exception in `original`.
