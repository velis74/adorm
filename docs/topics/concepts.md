# Concepts

::: info Status
adorm is pre-release. The [API reference](/ref/) lists what the installed version provides.
:::

adorm separates **what** an application asks for (a query or a model operation) from **where and how** it runs
(a slot in a pool, served by a driver). This page introduces the four pieces: sessions, slots, the pool and
drivers.

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

A connection profile is a named entry in adorm's settings. Its name is the `using` value, as a database alias
is in Django. A profile names the driver, the database alias, extra driver parameters and optional hooks:

- `connect` creates the connection instead of the driver's default,
- `on_acquire` runs when a session gets a slot (for example `SET ROLE`),
- `on_release` resets session state when the slot returns to the pool.

Pool settings are `max_connections`, `acquire_timeout`, `commit_timeout` and `release_timeout` (see [Pool](#pool)
and [Slots](#slots)).

```python
adorm.configure(profiles={"default": {"driver": "myproject.drivers.DjangoDriver", "alias": "default"}})
```

A call never carries connection parameters; different database accounts are different profiles. Profiles can
also be registered at run time through the settings plugin API, for example one per database user. The
[connection profiles topic](/topics/profiles) describes them.

## Sessions

A session is logical: the pair `(using, session_name)`. `create_session()` returns the open session with that
pair, or creates it.

```python
reports = adorm.create_session(using="reports", session_name="nightly")
same = adorm.create_session(using="reports", session_name="nightly")
assert reports is same
```

A context variable holds the current session. Without one, operations use the default session
`("default", "default")`. `session.activate()` sets it for a `with` block:

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

Binding takes no slot. A BoundModel stored at module level stays bound to the session that was current when the
module was imported. The transaction is not part of the binding: when an operation runs, the BoundModel looks up
the active transaction of its session in the current context. The [sessions topic](/topics/sessions) covers both
in detail.

## Slots

A slot is an exclusive channel to one driver connection. Work reaches a slot over a pair of queues: a request
queue the client writes operations to, and an event queue the slot writes results, callbacks (such as
`on_commit`) and backend events to. On the client an event channel interprets each event: it returns a result,
runs a callback or calls a subscribed function. The [events topic](/topics/events) follows an event from the slot
to the client, in process and through a remote driver.

- **An atomic operation** outside a transaction acquires a slot, executes and releases the slot at once.
- **A transaction** holds one slot for the whole block. A context variable records that the slot is held, so
  operations inside the block, including those from tasks that inherit the context, reuse it.

```python
async with adorm.atomic() as tx:          # acquires a slot, BEGIN
    member = await Member.objects.get(pk=1)
    member.active = False
    await member.save()
    async with adorm.atomic():            # savepoint on the same slot
        await Activity.objects.create(member_id=1, kind="deactivated")
# commit on normal exit, rollback on exception
```

`tx.commit()` commits before the end of the block. `session.atomic()` opens the transaction on a given
session. Exiting the block waits for operations already
sent, then commits; an operation that arrives after the end (from a task that inherited the context) raises
`TransactionClosed`. A slot aborts its transaction when no request (an operation, the commit or the rollback)
arrives within the pool's `commit_timeout`. The [transactions topic](/topics/transactions) covers all of this.

Acquiring a slot does nothing to the connection. A slot leaves the pool, and the next request gets a fresh
connection, when a session ends without commit or rollback, when the driver reports that the connection is no
longer usable, or when a lease is released with `discard=True`. Idle slots are not closed after a period of
inactivity yet (`purge_stale_connections_at`).

### Connection options and timeouts

Database-side limits (`statement_timeout`, `lock_timeout`, …) set per block (`adorm.connection_options()`) and a
client-side limit for adorm operations (`adorm.timeout()`) are not provided yet. A driver already declares the
options it supports in `Capabilities.connection_options`, and `DriverConnection.set_options()` sets them (see
[drivers](/ref/drivers)). `OperationTimeout` is defined for `adorm.timeout()`; nothing raises it yet.

Cancelling the client task cancels its request: a queued request never runs, and a running one is cancelled in
the driver connection (see [the pool topic](/topics/pool)).

## Pool

adorm keeps a pool of slots per connection profile. Sessions with the same `using` share its slots. Its size is
limited by `max_connections` (`0` opens new connections until the database refuses one). When every slot is
taken, a request waits in a queue.

Every acquisition has a timeout:

| Situation | Exception |
|---|---|
| no slot became free in time | `SessionTimeout` |
| no slot became free in time, and the flow already holds a slot | `DeadlockError` (subclass of `SessionTimeout`), `held_sessions` lists the held slots |
| every slot of the pool is held, through `router.slot()` or `router.run()`, by tasks that themselves wait for a slot that cannot become free | `DeadlockError`, raised at once ([deadlocks](/topics/pool#deadlocks)); otherwise `DeadlockError` at the timeout |

The [pool topic](/topics/pool) describes acquisition, the request and event queues and deadlock detection.

`atomic()` acquires its slot on entry, so a timeout or `DeadlockError` happens there, never in the middle of the
block.

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

A driver turns adorm operations into work on its target and reports results as plain data (rows keyed by adorm
field names, scalars, counts); adorm builds the model instances. The core imports no ORM: everything it knows
about a target comes through the [driver contract](/topics/drivers). The driver decides how it runs:

- **threaded**: a synchronous ORM runs on a dedicated thread per slot. The Django ORM driver works this way,
  because Django connections are bound to their thread, and calls only the synchronous Django ORM.
- **async**: native coroutines without threads. The PostgreSQL driver uses asyncpg.

A threaded driver can also run [**direct**](/topics/drivers#execution-mode): the ORM runs on the event loop's
thread, one task per event loop (for example a Celery task through `celery_viewset`); Django's `async_unsafe`
check accepts adorm's calls and still applies to everything else.

A separate adorm server is one more driver: the application side sends the operation description to the server
(over [muxws](:muxws:/) or HTTP), and the server executes it with an ORM driver. The server knows the profiles and
their credentials; the client sends only the profile name.

Errors are adorm's own [exceptions](/ref/exceptions) modelled on Django's (`DoesNotExist`, `IntegrityError`, …).
An error adorm does not recognise becomes a `DatabaseError` that carries the driver's exception in `original`.
