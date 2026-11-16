# Sessions and bound models

A session names where operations run: the pair `(using, session_name)`. `using` is the name of a connection
profile, like a Django database alias; `session_name` separates independent flows on the same profile. A model bound
to a session (a **bound model**) builds query sets whose operations run on that session.

An operation outside a transaction runs on the [runner](/ref/sessions#adorm-executor-set-runner-runner) set with
`set_runner()`, such as the [router](/topics/pool) of the slot pools. With connection profiles configured and no
runner set, it runs on the event loop's default router ([connection profiles](/topics/profiles#the-default-router)).

```python
import adorm

from club.models import Member

reports = adorm.create_session(using="reports", session_name="nightly")

await Member.objects.filter(active=True).count()     # the current session
await reports.Member.filter(active=True).count()     # the "reports" session
```

## Creating sessions

`create_session()` returns the open session with the given pair, or creates it. A process has one `Session` object
per pair, so sessions compare by identity:

```python
a = adorm.create_session("reports", "nightly")
b = adorm.create_session(using="reports", session_name="nightly")
assert a is b

adorm.create_session()          # ("default", "default")
```

A session holds no slot and no connection; creating one does no I/O.

## The current session

A context variable holds the current session; the default is `("default", "default")`. `session.activate()` makes a
session current for a synchronous `with` block; it only sets the context variable:

```python
adorm.current_session()                       # Session('default', 'default')

with reports.activate():
    adorm.current_session()                   # Session('reports', 'nightly')
    await Member.objects.count()              # runs on "reports"

adorm.current_session()                       # Session('default', 'default')
```

Tasks inherit the context they are created in (`asyncio.create_task()`, `TaskGroup`, `gather()`) and with it the
current session, also after the block that set it has ended. A session activated inside a task does not change the
session of its parent.

```python
with reports.activate():
    task = asyncio.create_task(Member.objects.count())    # runs on "reports"
await task
```

## Sessions on several profiles

A session's `using` selects the [connection profile](/topics/profiles), and with it the driver and the pool, of every
operation in it. The same query code runs on another profile when another session is current, for example one a
request selects:

```python
async def member(pk: int, using: str) -> Member:
    with adorm.create_session(using, "api").activate():
        return await Member.objects.get(pk=pk)

await member(3, "django")      # a slot of the "django" profile's pool
await member(3, "asyncpg")     # a slot of the "asyncpg" profile's pool
```

Tasks that activate different sessions run concurrently on their profiles' pools; each task keeps its own session:

```python
async def page(using: str) -> list[Member]:
    with adorm.create_session(using, "compare").activate():
        return [member async for member in Member.objects.order_by("last_name")[:20]]

async with asyncio.TaskGroup() as group:
    on_django = group.create_task(page("django"))
    on_asyncpg = group.create_task(page("asyncpg"))
assert [m.model_dump() for m in on_django.result()] == [m.model_dump() for m in on_asyncpg.result()]
```

Each profile has its own pool; a full pool of one profile does not delay operations on another. The
[demo](/intro/demo#choosing-the-driver-of-a-read) selects the profile of a read from a request header and compares a
page read on every driver.

## Bound models

`Model.objects` returns a `BoundModel`: the model bound to the session that is current when `objects` is read.
`session.Member` (the class name of a registered model) and `session.bind(Member)` bind to the given session.
`bind()` also takes a model label, which is required when two registered models share a class name.

```python
Member.objects                      # <BoundModel club.Member on Session('default', 'default')>
reports.Member                      # <BoundModel club.Member on Session('reports', 'nightly')>
reports.bind("club.Member")         # the same
reports.Member.objects              # the bound model itself, for Django-like spelling
reports.Loan.all_loans              # another manager of the model, on the same session
reports.bind(Loan, "all_loans")     # the same
```

Binding takes no slot. The binding is fixed when `objects` is read: a bound model stored at module level stays bound
to the session current at import.

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

Instances a query set returns run their operations (`save()`, relation loads) on the same session. An instance the
application creates without a query set uses the current session.

## Transactions are looked up when an operation runs

A transaction is not part of a binding. When an operation runs, the session looks up its active transaction in the
current context. With one bound, the operation runs in it; otherwise the runner acquires a slot for that operation
alone. The same bound model runs inside or outside a transaction depending on where it is awaited.

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

- A transaction of one session does not affect another session, also on the same profile.
- Tasks created inside the block inherit its transaction with the context, also after the block ends. The transaction
  handles an operation arriving then (an `atomic()` block raises `TransactionClosed`).
- An operation's target is chosen when it starts. An operation running when the block ends stays in the transaction;
  an `atomic()` block waits for it before it commits.

## Events

Sessions and bound models emit and consume no [events](/ref/events); operation results reach the session through the
runner or the transaction that executes them.
