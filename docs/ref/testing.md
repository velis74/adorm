# Testing

`adorm.testing.django` is the test mode of the [Django driver](/topics/django-driver): what lets adorm run inside
Django's `TestCase` and `TransactionTestCase` without changes to the test classes. The [testing topic](/topics/testing)
shows it in use. The package `adorm.testing` imports no ORM; importing `adorm.testing.django` imports Django.

```python
from adorm.testing.django import find_test_database, in_test_environment
```

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
| `acquire(connection)` / `release(connection)` | wait until no other connection runs a call, then hold it; give it up. A call cancelled while it waits raises `OperationalError` with SQLSTATE [`QUERY_CANCELED`](#adorm-testing-django-query-canceled), which the threaded bridge reports as a cancellation |
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
