# Testing

`adorm.testing` holds helpers that wait until an operation has reached the database or waits there for a lock, defined
in `adorm.testing.waits`. `adorm.testing.django` is the test mode of the [Django driver](/topics/django-driver): what
lets adorm run inside Django's `TestCase` and `TransactionTestCase` without changes to the test classes. The
[testing topic](/topics/testing) shows both in use. The package `adorm.testing` imports no ORM; importing
`adorm.testing.django` imports Django.

```python
from adorm.testing import LockWait, running_operations, wait_for_lock, wait_for_operation
from adorm.testing.django import find_test_database, in_test_environment
```

## Waiting for the database

The helpers select operations by the task that sent them (`task`, the task that awaits the operation, also inside a
transaction) and by their session (`session`); without either, any operation of the running event loop counts. An
operation awaited through `asyncio.gather()`, a `TaskGroup` or, on Python 3.11, `asyncio.wait_for()` is sent by a
child task and matches only that task, not its parent; `session` selects it. An operation runs from the moment its
slot hands it to the driver connection ([running requests](/ref/pool#running-requests)); `begin`, `commit` and
savepoint calls do not count. On a connection that shares its database session with others (the shared test connection
of Django's `TestCase`), it has reached the database only once its call holds that session
([`RunningRequest.in_session`](/ref/pool#adorm-executor-runningrequest)).

### `adorm.testing.running_operations(task=None, *, session=None)`

The [`RunningRequest`](/ref/pool#adorm-executor-runningrequest) of every selected operation whose driver connection call
runs now on a slot of the running event loop, in the order they started.

```python
assert running_operations(session=reports) == ()      # no report runs now
```

### `adorm.testing.wait_for_operation(task=None, *, session=None, timeout=10.0, interval=0.01)`

A coroutine: waits until a selected operation has been sent to the database (its driver connection call has started
and has its database session, `RunningRequest.reached_session`) and returns its `RunningRequest`, the first such of
`running_operations()` when one runs already. A started operation that still waits for a shared session is checked
again every `interval` seconds; one that ends without it is not returned. An operation that starts and ends while the
helper waits is still returned; `running` tells whether it still runs. Raises `RuntimeError` when `task` finishes first (chained to
the task's exception when it raised one) and `TimeoutError` after `timeout` seconds. Works on every driver.

```python
task = asyncio.create_task(Member.objects.filter(pk=ann.pk).update(active=False))
running = await wait_for_operation(task)
running.operation          # Update(...)
task.cancel()              # cancelled once the update is in the database, not while it waits for a slot
```

### `adorm.testing.wait_for_lock(task=None, *, session=None, timeout=10.0, interval=0.01)`

A coroutine: waits until a selected operation waits for a lock in the database
([`BackendActivity.waiting_for_lock`](/ref/drivers#adorm-drivers-backendactivity)) and returns the
[`LockWait`](#adorm-testing-lockwait). Each check reads the database sessions
([`backend_pid`](/ref/drivers#backend-pid)) of the selected operations that run then with their database session
(`RunningRequest.in_session`) through a
[`DatabaseMonitor`](/ref/drivers#adorm-drivers-databasemonitor) of their pool's driver, one per pool, opened on first
use and closed when the helper returns, raises or is cancelled. Checks repeat every `interval` seconds and whenever a
selected operation starts. A wait read for an operation that ended, or whose connection released or took its shared
session again, during the read is not reported: the session may already run another call.

| Raises | When |
|---|---|
| `NotSupportedError` | the driver of a selected operation does not report its connections' sessions (`backend_pid` is `None`) or cannot open a monitor ([`Driver.open_monitor()`](/ref/drivers#async-open-monitor-profile)) |
| `RuntimeError` | `task` finishes first, for example because it did not wait at all; chained to the task's exception when it raised one |
| `TimeoutError` | no selected operation waited for a lock within `timeout` seconds |

The Django and native PostgreSQL drivers report sessions and monitor them on a connection of their own (`pg_stat_activity`),
also inside Django's `TestCase`, where the monitor's connection is not the test's.

```python
async with adorm.atomic():
    await Account.objects.select_for_update().get(pk=1)              # holds the row lock
    task = asyncio.create_task(transfer(1, 2, 100), context=contextvars.Context())
    wait = await wait_for_lock(task)
    wait.operation        # the transfer's select_for_update() read of account 1
    wait.blocking_pids    # (pid of this transaction's session,)
```

### `adorm.testing.LockWait`

A frozen dataclass: an operation waiting for a lock.

| Member | Value |
|---|---|
| `running` | the operation's [`RunningRequest`](/ref/pool#adorm-executor-runningrequest) |
| `activity` | its session's [`BackendActivity`](/ref/drivers#adorm-drivers-backendactivity) as the monitor read it |
| `operation` | `running.operation` |
| `blocking_pids` | `activity.blocking_pids`: the sessions holding the locks it waits for |

## Finding the test's connection

### `adorm.testing.django.in_test_environment()`

Whether Django's test environment is set up: `django.test.utils.setup_test_environment()` has run and
`teardown_test_environment()` has not. Django's test runner, its parallel workers and pytest-django set it up. Outside
it the drivers look for no test connection.

```python
assert in_test_environment()      # inside `manage.py test` or pytest with pytest-django
```

### `adorm.testing.django.open_testcase_atomics(wrapper)`

The atomic blocks that Django's `TestCase` entered on the connection wrapper `wrapper` (those marked
`_from_testcase`), outermost first: the class's block and the test's. Atomic blocks of the code under test are left out.

### `adorm.testing.django.TestDatabase`

A frozen dataclass: the test's connection wrapper of a Django alias and the `TestCase` atomic blocks open on it when it
was found.

| Attribute | Value |
|---|---|
| `alias` | the Django database alias |
| `wrapper` | Django's connection wrapper of the thread that runs the test |
| `atomics` | the `TestCase` atomic blocks open on `wrapper` ([`open_testcase_atomics()`](#adorm-testing-django-open-testcase-atomics-wrapper)), empty outside a `TestCase` |
| `in_test_case` | whether `atomics` is not empty: the slots share `wrapper` |

`current()` returns whether `wrapper`'s `TestCase` atomic blocks are still those in `atomics` (none, for a wrapper found
outside a `TestCase`): false after the `TestCase` test that found it, and, for one found outside a `TestCase`, while a
`TestCase` transaction is open. A connection opened in a Django test reports itself unusable (`is_usable()`) while
`current()` is false, so a pool that outlives a `TestCase` test opens new connections in the next test.

### `adorm.testing.django.find_test_database(alias)`

A coroutine: the [`TestDatabase`](#adorm-testing-django-testdatabase) of `alias`; `None` outside Django's test environment and for an alias
without a `DATABASES` entry. The wrapper is that of the thread that runs the test:

- under Django's async test methods (asgiref's `async_to_sync()`) the thread that waits for the event loop, reached
  with `sync_to_async(thread_sensitive=True)`;
- otherwise (pytest-asyncio) the event loop's own thread, read as Django's thread-local wrapper.

```python
test = await find_test_database("default")
if test is not None and test.in_test_case:
    ...   # inside a TestCase: the slots share test.wrapper
```

## The shared connection

### `adorm.testing.django.SharedTestConnection(test)`

The test connection of a `TestCase` as the slots of every pool share it, one per test
([`shared_test_connection()`](#adorm-testing-django-shared-test-connection-test)).

| Member | Does |
|---|---|
| `test`, `wrapper` | the [`TestDatabase`](#adorm-testing-django-testdatabase) and its wrapper |
| `owner` | the connection whose call holds the shared connection, `None` when free |
| `transactions` | the connections with an open transaction, innermost last |
| `acquire(connection)` / `release(connection)` | wait until no other connection runs a call, then hold it (counted in the connection's `session_holds`); give it up. A call cancelled while it waits raises `OperationalError` with SQLSTATE [`QUERY_CANCELED`](#adorm-testing-django-query-canceled), which the threaded bridge reports as a cancellation |
| `hold(connection)` | a `with` block between `acquire()` and `release()` |
| `cancel(connection, cancel)` | cancels `connection`'s running call: `cancel()` (the driver's statement cancel) while it holds the shared connection, else it ends its wait. The owner cannot change meanwhile, so a cancel never reaches another connection's statement |
| `apply_options(desired)` | sets the connection options `desired` with `SET LOCAL` and resets the others set before (`SET LOCAL … TO DEFAULT`); only differences from what is known to be set are sent |
| `forget_options()` | treats the options as unknown, after a rollback to a savepoint may have undone a `SET LOCAL`; the next `apply_options()` sets every option it ever set |
| `unwind(connection, error)` | rolls back `connection`'s transaction and those opened after it; the later ones raise `error` on their next call |
| `check_isolated(what)` | raises [`TransactionOverlapError`](/ref/exceptions#adorm-transactionoverlaperror) while a transaction is open that the caller's context is not inside (no `atomic()` block of it is active there) |
| `attach()` / `detach()` | count the connections that use the shared connection. While one does, an execute wrapper, the wrapper's outermost, makes each statement that another thread runs through the wrapper (Django ORM code of the test or of the code under test) hold the shared connection, run without connection options and pass `check_isolated()` first |

### `adorm.testing.django.shared_test_connection(test)`

The [`SharedTestConnection`](#adorm-testing-django-sharedtestconnection-test) of `test.wrapper` for the current test: the same
object while the wrapper's `TestCase` atomic blocks are those of `test`, a new one for the next test.

### `adorm.testing.django.SharedConnectionMixin`

Makes a [`DjangoConnection`](/ref/drivers#adorm-drivers-django-djangoconnection) class use the shared connection of a
`TestCase`; [`shared_connection_class()`](#adorm-testing-django-shared-connection-class-cls) puts it in front of the driver's
`connection_class`. Created with `(driver, profile, emit, shared)`.

| Call | Does |
|---|---|
| every call but `connect_sync()` | holds the shared connection ([`hold()`](#adorm-testing-django-sharedtestconnection-test)) while it runs; `execute_sync()` first applies the connection's options ([`apply_options()`](#adorm-testing-django-sharedtestconnection-test)) |
| `connect_sync()` | takes the test's wrapper: Django's `inc_thread_sharing()` and `connections[alias] = wrapper` on the slot's thread, and [`attach()`](#adorm-testing-django-sharedtestconnection-test)es. A profile with a `connect` hook, `params` that differ from the wrapper's settings, or an `on_acquire` or `on_release` hook raises `NotSupportedError`: a hook's changes to the session (`SET ROLE`) would reach every slot |
| `begin_sync()` | enters `transaction.atomic(using=alias)`, a savepoint of the test's transaction. Raises [`TransactionOverlapError`](/ref/exceptions#adorm-transactionoverlaperror) while a transaction of another connection is open that the caller's context is not inside |
| `execute_sync(operation)` | outside a transaction: raises `TransactionOverlapError` as `begin_sync()` does, else runs the operation in a savepoint of its own. Inside a transaction: raises `TransactionOverlapError` while a transaction begun inside this one is open; a failure that leaves the transaction failed rolls back to its innermost savepoint at once; after Django marked it for rollback, further operations raise `TransactionManagementError` until a rollback |
| `savepoint_sync()`, `release_savepoint_sync()`, `rollback_to_savepoint_sync()` | as `DjangoConnection`; raise `TransactionOverlapError` while a transaction begun inside this one is open |
| `commit_sync()`, `rollback_sync()` | leave the savepoint. Django's `on_commit()` callbacks registered inside the transaction stay on the wrapper after the commit, as after Django's nested `atomic()`: they run only when the test runs them (`captureOnCommitCallbacks(execute=True)`). While a transaction begun inside this one is open, both transactions roll back and `TransactionOverlapError` is raised; the inner one raises it on its next call (its rollback then does nothing) |
| `set_options_sync(options, local)` | records the options: the connection's own and, with `local`, the transaction's (restored by a rollback to a savepoint, dropped at the end of the transaction) |
| `cancel_sync()` | [`SharedTestConnection.cancel()`](#adorm-testing-django-sharedtestconnection-test) |
| `close_sync()` | rolls back an open transaction while the test runs, together with the transactions begun inside it, which raise `TransactionOverlapError` on their next call; restores the thread's previous wrapper, calls `dec_thread_sharing()` and [`detach()`](#adorm-testing-django-sharedtestconnection-test)es; the test's connection stays open |
| `holds_session` | whether the running call holds the shared connection now (`SharedTestConnection.owner` is this connection) |
| `session_holds` | how many times the connection has taken the shared connection |

### `adorm.testing.django.shared_connection_class(cls)`

`cls`, a `DjangoConnection` class, with [`SharedConnectionMixin`](#adorm-testing-django-sharedconnectionmixin) in front;
one class per `cls`. Anything else raises `TypeError`.

```python
class AuditedConnection(DjangoConnection):
    ...

shared_connection_class(AuditedConnection)    # what a TestCase's slots of a driver with that class use
```

### `adorm.testing.django.django_connection(driver, profile, emit)`

A coroutine: the new, unstarted connection [`DjangoDriver.open_connection()`](/ref/drivers#adorm-drivers-django-djangodriver)
starts: inside a `TestCase` of `shared_connection_class(driver.connection_class)`, sharing the test's connection; in
another Django test a `driver.connection_class` connection that becomes unusable once a `TestCase` transaction is open
on the test's connection; outside Django's test environment the driver's plain connection.

### `adorm.testing.django.QUERY_CANCELED`

`"57014"`: the SQLSTATE of a call cancelled while it waited for the shared connection, the same as PostgreSQL's for a
cancelled statement.
