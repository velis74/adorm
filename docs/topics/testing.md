# Testing

Code that uses adorm is tested with Django's test classes unchanged. While Django's test environment is set up
(Django's test runner, its parallel workers, pytest-django), the [Django driver](/topics/django-driver) looks up the
test's database connection whenever a slot opens a connection:

| Test | Slots |
|---|---|
| `TestCase` (pytest-django's `django_db`) | share the test's connection: they see the test's data, and everything they write is rolled back with the test |
| `TransactionTestCase` (`django_db(transaction=True)`) | open connections of their own to the process's test database, as in production |

No setting, base class or test runner is needed; the parts are in [`adorm.testing.django`](/ref/testing).

## Inside a `TestCase`

The pool works as configured, with several slots, and every slot's connection is the test's connection. A slot holds
it for one call at a time (an operation, a `BEGIN`, a `COMMIT`); flows take turns on it. Concurrent operations work,
and `setUpTestData()`, the test and adorm's writes see the same data:

```python
import asyncio

from asgiref.sync import sync_to_async
from django.test import TestCase

import adorm
from club import models as orm
from club.adorm_models import Member


class MemberTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        orm.Member.objects.create(name="Ann")

    async def test_rename(self):
        member = await Member.objects.get(name="Ann")      # the row setUpTestData() created
        member.name = "Anna"
        await member.save()
        # Django's ORM on the test's thread sees the change; the test's rollback removes it.
        assert (await sync_to_async(orm.Member.objects.get)(pk=member.pk)).name == "Anna"
        assert await Member.objects.filter(name="Anna").count() == 1

    async def test_concurrent_reads(self):
        counts = await asyncio.gather(*(Member.objects.count() for _ in range(10)))   # several slots
        assert counts == [1] * 10
```

An operation outside a transaction runs in a savepoint of its own, so a failing one (an `IntegrityError`, for
example) leaves the test's transaction usable.

Django ORM code of the test or of the code under test uses the same connection from its own thread
(`sync_to_async()`):

- Each statement holds the connection while it runs, as an adorm call does, without adorm's connection options.
- While an adorm transaction is open, a Django statement outside the transaction's context waits until it ends, as an
  adorm operation outside a transaction does ([below](#transactions-in-a-testcase)); one inside it joins the
  transaction's savepoint and is rolled back with it.
- A Django `atomic()` block spans several statements, and adorm calls can run between them. Run such a block while no
  adorm operation of the test is running.

## Transactions in a `TestCase`

An `atomic()` block is a savepoint in the test's transaction. Its commit makes its writes visible to the rest of the
test, its rollback undoes them, and the end of the test undoes everything. adorm's `on_commit()` callbacks run after
its commit. Django's `on_commit()` callbacks (registered by a `save()` override or a signal receiver) stay with the
test's transaction, like those of Django's nested `atomic()` blocks, and `captureOnCommitCallbacks()` sees them:

```python
def test_welcome_mail(self):
    with self.captureOnCommitCallbacks(execute=True) as callbacks:
        async_to_sync(signup)("ann")      # creates the member in an adorm transaction; a receiver calls on_commit()
    assert len(callbacks) == 1
    assert len(mail.outbox) == 1
```

```python
async def test_transfer(self):
    with self.assertRaises(InsufficientFunds):
        async with adorm.atomic():
            await debit(account, 100)
            await credit(other, 100)
            raise InsufficientFunds
    assert (await Account.objects.get(pk=account.pk)).balance == account.balance     # rolled back
```

One connection cannot isolate transactions, so a transaction runs alone. While one is open, a transaction or an
operation outside a transaction that another flow starts waits until it ends, then runs. Flows that wait start in the
order they came; consecutive waiting operations outside a transaction run together. Two flows that race run one after
the other:

```python
async def signup(name):
    async with adorm.atomic():
        if not await Member.objects.filter(name=name).exists():
            await Member.objects.create(name=name)


async def test_two_signups(self):
    await asyncio.gather(signup("ann"), signup("ann"))      # the second transaction begins after the first commits
    assert await Member.objects.filter(name="ann").count() == 1
```

Work inside the open transaction's context nests in it and runs at once, including tasks created inside the block
and transactions of other sessions:

```python
async with adorm.atomic():                               # session ("default", "default")
    await Member.objects.create(name="Bob")
    audit = adorm.create_session(session_name="audit")
    async with audit.atomic():                           # nested in the open transaction
        await audit.bind(AuditEntry).create(action="created Bob")
    await asyncio.gather(notify(1), notify(2))           # tasks inherit the transaction's context
```

- Nested work belongs to the outer transaction's savepoint and rolls back with it, also the work of another session,
  which production commits on its own connection.
- A task created inside the block that outlives it is outside the transaction once it has ended: its operations of
  other sessions wait for an open transaction like any other flow; those of the ended transaction's session raise
  `TransactionClosed`.
- While a transaction begun inside it is open, an operation of the outer transaction raises
  `TransactionOverlapError`: it would run inside the inner transaction's savepoint.
- A transaction that ends, or whose slot closes, while a transaction begun inside it is open (a task that outlives
  the block) rolls back both and raises `TransactionOverlapError`; the inner transaction raises it on its next
  operation.

### Waits that cannot end

A wait for an open transaction raises [`DeadlockError`](/ref/exceptions#adorm-deadlockerror-message-none-held-sessions-session-none-timeout-none) when it cannot end:

| The open transaction | Raised |
|---|---|
| awaits the waiting flow's task, directly, through another task or through `asyncio.gather()` | at once |
| stays open longer than the acquisition timeout (the `acquire_timeout` [connection option](/topics/options) in effect) | at the timeout |
| waits for a Django statement that waits for it, or for a turn of another alias, or for a slot that the waiting flow holds | at the timeout |

```python
async with adorm.atomic():
    await Member.objects.create(name="Cid")
    task = asyncio.create_task(report(), context=contextvars.Context())   # not inside the transaction
    with self.assertRaises(adorm.DeadlockError):
        await task                                       # its first operation waits for this transaction
```

The wait counts as a wait for a slot: an enclosing [`adorm.timeout()`](/topics/options#adorm-timeout) ends it with
`OperationTimeout` in the phase `"waiting_for_slot"`, and cancelling the flow ends it without running anything.

A test that needs isolated concurrent transactions (two flows racing for a row, a lock that one flow waits for while
the other holds it) uses `TransactionTestCase`.

## Failures and cancellation

A failed operation leaves PostgreSQL's transaction failed, which on the shared connection would fail every flow. The
slot rolls the failed transaction back to its innermost savepoint at once. The adorm transaction refuses further
operations until it, or that savepoint, is rolled back, as in production.

- Cancelling a flow while its operation runs cancels the statement on the test's connection and rolls back to the
  operation's savepoint.
- Cancelling it while it waits for its turn ends the wait; nothing runs.

The other flows continue in both cases. A slot that aborts an abandoned transaction
([`commit_timeout`](/topics/pool#slot-lifecycle)) rolls back its savepoint and frees the connection for the next
transaction.

## Connection options and profile hooks

[Connection options](/topics/options) are per slot: a slot's options are set with `SET LOCAL` before each of its
operations, and the options another slot set return to those the connection starts with (the `role` of
`OPTIONS["assume_role"]`, [`Driver.connection_defaults()`](/ref/drivers#connection-defaults-profile)); Django code of the test runs with these.
They do not leak between flows or out of a transaction. The
options an `on_acquire` hook returns, such as a `role` or a custom option for row-level security, are applied the
same way, so a profile with such a hook runs under `TestCase` as in production.

```python
with adorm.connection_options(statement_timeout=2):
    await Member.objects.count()          # runs with a 2 s limit
await Member.objects.count()              # runs without one, on whichever slot


def on_acquire(connection, profile, session):
    return {"app.user_id": str(current_user.get().pk)}     # SET LOCAL "app"."user_id" = '…' before each operation
```

A statement a profile hook runs on the test's connection raises `NotSupportedError`: its effect on the session would
reach every slot. These profiles cannot share the test's connection either:

- a profile with `params` that differ from the alias's `DATABASES` entry;
- a profile with a `connect` hook.

The first operation of such a profile raises `NotSupportedError`; test such profiles with `TransactionTestCase`.

## Inside a `TransactionTestCase`

The slots open connections of their own to the process's test database. Transactions are isolated from each other
and commit; the test case empties the tables afterwards.

```python
from django.test import TransactionTestCase


class RaceTests(TransactionTestCase):
    async def test_one_of_two_signups_wins(self):
        results = await asyncio.gather(signup("ann"), signup("ann"), return_exceptions=True)
        assert sum(isinstance(r, adorm.IntegrityError) for r in results) == 1
```

The [native PostgreSQL driver](/topics/postgresql-driver) works in a `TransactionTestCase`. Inside a `TestCase` it
raises `NotSupportedError`: its own connection can neither see nor roll back the test's transaction.

The test case's `flush` deletes the content type rows and Django's `post_migrate` creates them again with other ids.
Both drivers clear [`adorm.content_types`](/ref/models#adorm-contenttypes) on that signal, so a content type id read in
one test is not used in the next.

## Waiting until an operation is in the database

[`adorm.testing`](/ref/testing#waiting-for-the-database) waits until a flow's operation has reached the database, or
waits there for a lock another flow holds. A race or cancellation test uses it before it releases the lock, cancels
the flow or starts a competitor:

| Helper | Returns once |
|---|---|
| `wait_for_operation(task)` | an operation of `task` has been sent to the database: its slot handed it to the driver connection and it has its database session |
| `wait_for_lock(task)` | an operation of `task` waits for a lock in the database; the result names the sessions that hold it |
| `running_operations(task)` | at once: the operations of `task` running now |

- `task` is the task that awaits the operation, also inside a transaction. `session=` selects by session instead;
  without either, any operation counts.
- An operation awaited through `asyncio.gather()`, a `TaskGroup` or, on Python 3.11, `asyncio.wait_for()` runs in a
  child task and does not match the flow's task: select it with `session=`.
- Both waits raise `RuntimeError` when the task finishes first, so a flow that did not block fails the test at once,
  and `TimeoutError` after `timeout` seconds (default 10).

```python
from adorm.testing import wait_for_lock


class RaceTests(TransactionTestCase):
    async def test_a_rename_waits_for_the_row_lock_and_wins_afterwards(self):
        holding, release = asyncio.Event(), asyncio.Event()

        async def hold():
            async with adorm.atomic():
                await Tag.objects.select_for_update().get(pk=tag.pk)
                holding.set()
                await release.wait()

        holder = asyncio.create_task(hold())
        await holding.wait()
        rename = asyncio.create_task(update_tag(tag.pk, name="renamed"))
        wait = await wait_for_lock(rename)         # the rename's select_for_update() waits for the holder
        assert wait.blocking_pids                  # the holder's database session
        release.set()
        await holder
        assert (await rename).name == "renamed"

    async def test_cancelling_a_flow_that_waits_for_a_lock_rolls_it_back(self):
        ...
        rename = asyncio.create_task(update_tag(tag.pk, name="renamed"))
        await wait_for_lock(rename)
        rename.cancel()                             # cancels the waiting statement; the transaction rolls back
        with self.assertRaises(asyncio.CancelledError):
            await rename
```

`wait_for_lock()` reads the database sessions of the selected operations through the driver's monitor connection
([`Driver.open_monitor()`](/ref/drivers#async-open-monitor-profile)), opened for the wait and closed afterwards. The
Django and native PostgreSQL drivers read `pg_stat_activity`.

- In a `TestCase` the slots' statements share the test's connection; a statement there waits for a lock held by a
  connection the test opened itself.
- A statement waiting for the shared connection while another slot's statement holds it has not reached the database;
  neither helper reports it until it holds the connection.
- `wait_for_operation()` needs no database access and works on every driver.

## pytest

pytest-django sets up Django's test environment, so the same applies:

```python
import pytest


@pytest.mark.django_db                    # a TestCase: slots share the test's connection
async def test_rename(member):
    member.name = "Anna"
    await member.save()


@pytest.mark.django_db(transaction=True)  # a TransactionTestCase: connections of their own
async def test_race():
    ...
```

With pytest-asyncio the event loop runs on the test's thread; with Django's async test methods it runs on its own
thread while the test's thread waits. adorm finds the test's connection in both cases, also with the `direct` Django
driver ([`find_test_database()`](/ref/testing#adorm-testing-django-find-test-database-alias)).

## Parallel runs and pools across tests

`manage.py test --parallel` gives each worker its own clone of the test database; the worker's slots use it.

A pool belongs to its event loop. Django's async test methods and pytest-asyncio's default function-scoped loop give
each test a loop, and the default router's pools close with it. When a pool outlives its test (a session-scoped loop):

- a slot opened in a `TestCase` test reports itself unusable once that test has ended, and the next request opens a
  slot for the current test;
- a slot opened outside a `TestCase` (in a `TransactionTestCase`) stays usable in later tests of that kind and reports
  itself unusable while a `TestCase` test runs.
