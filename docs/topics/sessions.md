# Sessions and bound models

::: info Status
The installed version provides sessions, the current session, bound models, query sets and
[transactions](/topics/transactions). An operation outside a transaction runs on the
[runner](/ref/sessions#adorm-executor-set-runner-runner) set with `set_runner()`, such as the [router](/topics/pool) of
the slot pools, or, once connection profiles are configured, on the event loop's default router (see
[connection profiles](/topics/profiles#the-default-router)).
:::

A session names where operations run: the pair `(using, session_name)`. `using` is the name of a connection
profile, as a database alias is in Django; `session_name` separates independent flows on the same profile. A
model bound to a session (a **bound model**) builds query sets whose operations run on that session.

```python
import adorm

from club.models import Member

reports = adorm.create_session(using="reports", session_name="nightly")

await Member.objects.filter(active=True).count()     # the current session
await reports.Member.filter(active=True).count()     # the "reports" session
```

## Creating sessions

`create_session()` returns the open session with the given pair, or creates it on the first call. There is one
`Session` object per pair in a process, so sessions compare by identity:

```python
a = adorm.create_session("reports", "nightly")
b = adorm.create_session(using="reports", session_name="nightly")
assert a is b

adorm.create_session()          # ("default", "default")
```

A session holds no slot and no connection. Creating one is cheap and does no I/O.

## The current session

A context variable holds the current session. Without one, it is the default session `("default", "default")`.
`session.activate()` makes a session current for a `with` block; it is a synchronous `with`, because it only sets
the context variable:

```python
adorm.current_session()                       # Session('default', 'default')

with reports.activate():
    adorm.current_session()                   # Session('reports', 'nightly')
    await Member.objects.count()              # runs on "reports"

adorm.current_session()                       # Session('default', 'default')
```

Tasks inherit the context in which they are created (`asyncio.create_task()`, `TaskGroup`, `gather()`), so they
inherit the current session, also after the block that set it has ended. A session activated inside a task does
not change the session of its parent.

```python
with reports.activate():
    task = asyncio.create_task(Member.objects.count())    # runs on "reports"
await task
```

## Sessions on several profiles

A session's `using` decides the [connection profile](/topics/profiles), and so the driver and the pool, of every
operation that runs in it. The same query code runs on another profile when another session is current, for example
one a request chooses:

```python
async def member(pk: int, using: str) -> Member:
    with adorm.create_session(using, "api").activate():
        return await Member.objects.get(pk=pk)

await member(3, "django")      # a slot of the "django" profile's pool
await member(3, "asyncpg")     # a slot of the "asyncpg" profile's pool
```

Tasks that activate different sessions run on their profiles' pools at the same time; each task's session stays its
own:

```python
async def page(using: str) -> list[Member]:
    with adorm.create_session(using, "compare").activate():
        return [member async for member in Member.objects.order_by("last_name")[:20]]

async with asyncio.TaskGroup() as group:
    on_django = group.create_task(page("django"))
    on_asyncpg = group.create_task(page("asyncpg"))
assert [m.model_dump() for m in on_django.result()] == [m.model_dump() for m in on_asyncpg.result()]
```

Each profile has a pool of its own, so a full pool of one profile does not delay operations on another. The
[demo](/intro/demo#choosing-the-driver-of-a-read) chooses the profile of a read from a request header and compares a
page read on every driver.

## Bound models

`Model.objects` returns a `BoundModel`: the model bound to the session that is current when `objects` is read.
`session.Member` (the class name of a registered model) and `session.bind(Member)` bind to the given session; a
model label works with `bind()`, and is needed when two registered models share a class name.

```python
Member.objects                      # <BoundModel club.Member on Session('default', 'default')>
reports.Member                      # <BoundModel club.Member on Session('reports', 'nightly')>
reports.bind("club.Member")         # the same
reports.Member.objects              # the bound model itself, for Django-like spelling
reports.Loan.all_loans              # another manager of the model, on the same session
reports.bind(Loan, "all_loans")     # the same
```

Binding takes no slot. The binding is fixed when `objects` is read: a bound model stored at module level stays bound
to the session that was current when the module was imported.

```python
# club/queries.py
members = Member.objects             # bound to the session current at import

# elsewhere
with reports.activate():
    await members.count()            # still runs on the import-time session
    await Member.objects.count()     # runs on "reports"
```

## Query sets

A bound model's query methods return a [`QuerySet`](/ref/queryset): a [`Query`](/ref/query) together with the
session that runs it. Building a query set does no I/O; each terminal coroutine runs one operation.

```python
active = Member.objects.filter(active=True).order_by("name")

members = [m async for m in active[:20]]          # Select
ana = await active.get(name="Ana")                # Get
count = await active.count()                      # Count
await active.filter(fee__lt=5).update(fee=5)      # Update
new = await Member.objects.create(name="Bea")     # Create
```

Instances a query set returns run their own operations (`save()`, relation loads) on the same session. An
instance created by the application without a query set uses the current session.

## Transactions are looked up when an operation runs

A transaction is not part of a binding. When an operation runs, the session looks up its own active transaction in
the current context: when one is bound, the operation runs in it; otherwise it goes to the runner, which acquires a
slot for that operation alone. The same bound model therefore runs inside or outside a transaction depending on
where it is awaited.

An [`atomic()`](/topics/transactions) block binds its transaction to its session for the block:

```python
from adorm.executor import active_transaction

members = reports.Member                        # bound outside any transaction

async with reports.atomic() as tx:
    active_transaction(reports)                 # tx
    await members.filter(pk=1).update(fee=0)    # tx.execute(Update(...))
await members.count()                           # runner.run(reports, Count(...))
```

[`bind_transaction()`](/ref/transactions#adorm-executor-bind-transaction-session-transaction) binds any object with an
`async execute(operation)` the same way.

A transaction of one session does not affect another session, even one on the same profile. Tasks created inside
the block inherit its transaction with the context, also after the block ends; the transaction decides what an
operation arriving then does (an `atomic()` block raises `TransactionClosed`). The target of an operation is chosen
when it starts: an operation already running when the block ends stays in the transaction; an `atomic()` block waits
for it before it commits.

## Events

Sessions and bound models emit and consume no [events](/ref/events): operation results reach the session through
the runner or the transaction that executes them.
