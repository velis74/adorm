# Testing

Code that uses adorm is tested with Django's test classes as they are. While Django's test environment is set up
(Django's test runner, its parallel workers, pytest-django), the [Django driver](/topics/django-driver) looks for the
test's database connection whenever a slot opens a connection:

| Test | Slots |
|---|---|
| `TestCase` (pytest-django's `django_db`) | share the test's connection: they see the test's data, and everything they write is rolled back with the test |
| `TransactionTestCase` (`django_db(transaction=True)`) | open connections of their own to the process's test database, as in production |

No setting, base class or test runner is needed; [`adorm.testing.django`](/ref/testing) holds the parts.

## Inside a `TestCase`

The pool works as configured, with several slots, but every slot's connection is the test's connection. A slot holds
it for one call at a time: an operation, a `BEGIN`, a `COMMIT`. Flows take turns on it, so concurrent operations
work, and the data of `setUpTestData()`, of the test and of adorm's writes is the same data:

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

An operation outside a transaction runs in a savepoint of its own: one that fails, an `IntegrityError` for example,
leaves the test's transaction usable, as it leaves nothing behind in production.

Django ORM code, of the test or of the code under test, uses the same connection from its own thread
(`sync_to_async()`). Each of its statements takes a turn on the connection as an operation outside a transaction does,
without adorm's connection options. While an adorm transaction is open, a statement of Django code that does not run
inside the transaction's context raises `TransactionOverlapError`; one that does joins the transaction's savepoint and
is rolled back with it. An `atomic()` block of Django code spans several statements, between which slots take turns;
run such a block while no adorm operation of the test is running.

## Transactions in a `TestCase`

An `atomic()` block is a savepoint in the test's transaction. Its commit makes its writes visible to the rest of the
test, its rollback undoes them, and the test's end undoes everything. adorm's `on_commit()` callbacks run after its
commit, as they do in production. Django's `on_commit()` callbacks (registered by a `save()` override or a signal
receiver) stay with the test's transaction, as those of Django's own nested `atomic()` blocks do, so
`captureOnCommitCallbacks()` sees them unchanged:

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

One connection cannot isolate transactions from each other. A transaction is therefore exclusive: while one is open,
a transaction or an operation outside a transaction that another flow starts raises
[`TransactionOverlapError`](/ref/exceptions#adorm-transactionoverlaperror). What runs inside the open transaction's
context, including tasks created inside the block and transactions of other sessions, nests in it and runs:

```python
async with adorm.atomic():                               # session ("default", "default")
    await Member.objects.create(name="Bob")
    audit = adorm.create_session(session_name="audit")
    async with audit.atomic():                           # nested in the open transaction: allowed
        await audit.bind(AuditEntry).create(action="created Bob")
    await asyncio.gather(notify(1), notify(2))           # tasks inherit the transaction's context: allowed


async with adorm.atomic():
    await Member.objects.create(name="Cid")
    task = asyncio.create_task(report(), context=contextvars.Context())   # not inside the transaction
    with self.assertRaises(adorm.TransactionOverlapError):
        await task                                       # its first operation raises
```

Nested work belongs to the outer transaction's savepoint: when the outer transaction rolls back, the nested work is
rolled back too, also that of another session, which production commits on its own connection. While a transaction
begun inside it is open, the outer transaction cannot run operations: they would run inside the inner transaction's
savepoint, so they raise `TransactionOverlapError`. A transaction that ends while a transaction begun inside it is
still open (a task that outlives the block), or whose slot closes then, rolls back both and raises
`TransactionOverlapError`; the inner transaction raises it on its next operation.

A test that needs concurrent transactions to be isolated (two flows racing for a row, a lock that one flow waits for)
uses `TransactionTestCase`.

## Failures and cancellation

A failed operation inside a transaction leaves PostgreSQL's transaction failed, and on a shared connection that would
fail every other flow too. The slot rolls the failed transaction back to its innermost savepoint at once; the
transaction itself refuses further operations until it, or that savepoint, is rolled back, as it does in production.

Cancelling a flow while its operation runs cancels the statement on the test's connection and rolls back to the
operation's savepoint; cancelling it while it waits for its turn ends the wait without running anything. Either way the
other flows go on. A slot that aborts an abandoned transaction ([`commit_timeout`](/topics/pool#slot-lifecycle)) rolls
back its savepoint and frees the connection for the next transaction.

## Connection options and profile hooks

[Connection options](/topics/options) stay per slot as in production: each slot's options are set with `SET LOCAL`
before each of its operations, and the options another slot set are reset, so they never leak from one flow to another
or out of a transaction.

```python
with adorm.connection_options(statement_timeout=2):
    await Member.objects.count()          # runs with a 2 s limit
await Member.objects.count()              # runs without one, on whichever slot
```

A profile with `on_acquire` or `on_release` hooks cannot share the test's connection: what a hook changes in the
session, a `SET ROLE` for example, would reach every slot. Neither can a profile with `params` that differ from the
alias's `DATABASES` entry, or with a `connect` hook. The first operation of such a profile raises `NotSupportedError`;
tests of such profiles use `TransactionTestCase`.

## Inside a `TransactionTestCase`

The slots open connections of their own to the process's test database. Transactions are real transactions,
isolated from each other, and commit; the test case empties the tables afterwards.

```python
from django.test import TransactionTestCase


class RaceTests(TransactionTestCase):
    async def test_one_of_two_signups_wins(self):
        results = await asyncio.gather(signup("ann"), signup("ann"), return_exceptions=True)
        assert sum(isinstance(r, adorm.IntegrityError) for r in results) == 1
```

The [native PostgreSQL driver](/topics/postgresql-driver) works here as anywhere; inside a `TestCase` it raises
`NotSupportedError`, since its own connection could neither see nor roll back the test's transaction.

The test case's `flush` deletes the content type rows and Django's `post_migrate` creates them again with other ids;
both drivers clear [`adorm.content_types`](/ref/models#adorm-contenttypes) on that signal, so a content type id read
in one test is not used in the next.

## pytest

pytest-django sets Django's test environment up, so the same holds there:

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

With pytest-asyncio the event loop runs on the test's own thread; with Django's async test methods it runs on a thread
of its own while the test's thread waits. adorm finds the test's connection in both cases
([`find_test_database()`](/ref/testing#adorm-testing-django-find-test-database-alias)), the `direct` Django driver included.

## Parallel runs and pools across tests

`manage.py test --parallel` runs each worker with its own clone of the test database; each worker's slots use that
worker's database. A pool belongs to its event loop: Django's async test methods and pytest-asyncio's default
function-scoped loop give each test a loop, and the default router's pools close with it. A pool that outlives its test
(a session-scoped loop) does not reuse a slot opened in a `TestCase` test in the next test: the slot reports itself
unusable once that test has ended, and the next request opens a slot for the current test. A slot opened outside a
`TestCase` (a `TransactionTestCase`) stays usable in later tests of that kind and reports itself unusable while a
`TestCase` test runs.
