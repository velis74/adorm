# Exceptions

::: info Status
The installed version provides the exception classes, `wrap_driver_error`, the [driver contract](/ref/drivers),
whose connections raise these exceptions, the [pool and router](/ref/pool), [`atomic()`](/ref/transactions),
[`adorm.timeout()`](/ref/options#adorm-timeout-delay), the [Django driver](/topics/django-driver) and the
[native PostgreSQL driver](/topics/postgresql-driver).
:::

adorm has its own exceptions, modelled on Django's. Applications catch adorm's classes, never the target ORM's or
the database library's: a driver converts the errors it gets with
[`wrap_driver_error`](#adorm-exceptions-wrap-driver-error-exc). Every class in the tree below is importable from
`adorm` and from `adorm.exceptions`; `ModelExceptionsMixin`, `wrap_driver_error` and `TimeoutPhase` only from
`adorm.exceptions`.

```text
AdormError
├── ObjectDoesNotExist                  <Model>.DoesNotExist
├── MultipleObjectsReturned             <Model>.MultipleObjectsReturned
├── ValidationError
├── Error
│   ├── InterfaceError
│   └── DatabaseError
│       ├── DataError
│       ├── OperationalError
│       ├── IntegrityError
│       │   ├── ProtectedError
│       │   └── RestrictedError
│       ├── InternalError
│       ├── NotSupportedError
│       └── ProgrammingError
│           ├── FieldError
│           └── TransactionManagementError
│               ├── TransactionClosed
│               └── TransactionOverlapError
├── SessionTimeout
│   └── DeadlockError
└── OperationTimeout                    also a TimeoutError
```

## Base class

### `adorm.AdormError`

Base class of every exception adorm raises.

```python
try:
    await member.save()
except adorm.AdormError:
    log.exception("database work failed")
    raise
```

## Objects

### `adorm.ObjectDoesNotExist`

A query that expects one object found none. Base class of every model's `DoesNotExist`; catch it to handle a
missing object of any model.

```python
try:
    club = await Club.objects.get(slug=slug)
except adorm.ObjectDoesNotExist:
    club = None
```

### `adorm.MultipleObjectsReturned`

A query that expects one object found several. Base class of every model's `MultipleObjectsReturned`.

### Per-model exceptions

Every model class has its own `DoesNotExist` and `MultipleObjectsReturned`, as in Django. A model's exceptions
derive from its parent model's exceptions, so `except Member.DoesNotExist` also catches `Coach.DoesNotExist` when
`Coach` subclasses `Member`.

```python
try:
    member = await Member.objects.get(email=email)
except Member.DoesNotExist:
    member = None
except Member.MultipleObjectsReturned:
    raise adorm.ValidationError({"email": "Several members share this address."})
```

```python
try:
    club = await member.club
except adorm.ObjectDoesNotExist:          # any model
    club = None
```

#### `adorm.exceptions.ModelExceptionsMixin`

Adds the two classes to every subclass in `__init_subclass__`; `Model` inherits it. Their
`__qualname__` is `<Model>.DoesNotExist` in the model's module, so they pickle when the model is defined at module
level. A class that defines `DoesNotExist` or `MultipleObjectsReturned` itself keeps its own; the class must then
derive from `ObjectDoesNotExist` or `MultipleObjectsReturned` for subclasses to inherit from it.

```python
from adorm.exceptions import ModelExceptionsMixin


class Member(ModelExceptionsMixin): ...
class Coach(Member): ...


assert issubclass(Coach.DoesNotExist, Member.DoesNotExist)
assert issubclass(Member.DoesNotExist, adorm.ObjectDoesNotExist)
```

## Validation

### `adorm.ValidationError(message, code=None, params=None)`

Data failed validation. Mirrors `django.core.exceptions.ValidationError`. `message` is one of:

| `message` | Result |
|---|---|
| a string | one error; `code` and `params` apply to it, `params` fill `%` placeholders |
| a list of strings or `ValidationError`s | `error_list`, flattened |
| a dict of field name → message(s) | `error_dict` of field name → list of `ValidationError`s |
| a `ValidationError` | a copy of its message, list or dict |

| Attribute | Value |
|---|---|
| `messages` | every message as text, with `params` filled in |
| `message_dict` | field name → list of messages; `AttributeError` when not built from a dict |
| `error_list` | list of single-message `ValidationError`s (not set when built from a dict) |
| `error_dict` | field name → list of `ValidationError`s (only when built from a dict) |
| `message`, `code`, `params` | the message, its code and parameters (only when built from a single message) |

Iterating a dict error yields `(field, messages)` pairs; iterating any other yields the messages.

```python
exc = adorm.ValidationError("At most %(limit)d characters.", code="max_length", params={"limit": 30})
exc.messages                  # ['At most 30 characters.']

exc = adorm.ValidationError({"name": "Required.", "age": ["Too low."]})
exc.message_dict              # {'name': ['Required.'], 'age': ['Too low.']}
dict(exc)                     # the same
```

## Database errors

The database errors follow PEP 249, as Django's `django.db` errors do. Each one carries:

| Attribute | Value |
|---|---|
| `original` | the exception the driver got from its ORM or database library, or `None` |
| `sqlstate` | the five-character SQLSTATE code the database reported, or `None` |

The original exception is also the `__cause__`, so a traceback shows both.

```python
try:
    await Member.objects.create(email=email)
except adorm.IntegrityError as exc:
    if exc.sqlstate == "23505":           # unique_violation
        raise adorm.ValidationError({"email": "Already registered."}) from exc
    raise
```

### `adorm.Error(message=None, *, original=None, sqlstate=None)`

Base class of the database errors. Without `message`, the message is the text of `original`, or of its class name
when the text is empty. With neither, the message is the exception's class name.

### `adorm.InterfaceError`

An error of the database interface rather than of the database, such as using a closed connection. Not a
`DatabaseError`.

### `adorm.DatabaseError`

An error of the database. **An error adorm does not recognise becomes a `DatabaseError`**, with the driver's
exception in `original`.

```python
try:
    await Report.objects.filter(year=2026).delete()
except adorm.DatabaseError as exc:
    log.error("delete failed: %r (driver error %r)", exc, exc.original)
```

### `adorm.DataError`

A value is invalid for its column: out of range, too long, wrong format, division by zero (SQLSTATE class `22`).

### `adorm.OperationalError`

The database failed to perform the operation: failed authentication, missing database, lost connection, statement
cancelled by `statement_timeout`, serialization failure, a deadlock between database transactions, lock not
available. An `OSError` from the driver, such as an unknown host or a connect timeout, also becomes an
`OperationalError`. A deadlock the database
detects is an `OperationalError`; `DeadlockError` (see [Executor errors](#executor-errors)) is a different
condition, between slots of adorm's pool.

### `adorm.IntegrityError`

A constraint was violated: unique, foreign key, not null, check, exclusion (SQLSTATE class `23`).

### `adorm.ProtectedError(message=None, protected_objects=(), **kwargs)`

A delete was refused because `on_delete=PROTECT` references point at the objects being deleted.
`protected_objects` holds the referencing objects: a driver raises it with
[`ModelRow`](./operations#adorm-operations-modelrow-model-row) values, which `QuerySet.delete()` and `Model.delete()`
turn into instances of the registered models (a row of a model that is not registered stays a `ModelRow`). Without
a SQLSTATE it leaves an open transaction usable: the references are checked before any statement changes data.

```python
try:
    await club.delete()
except adorm.ProtectedError as exc:
    names = [m.name for m in exc.protected_objects]
```

### `adorm.RestrictedError(message=None, restricted_objects=(), **kwargs)`

A delete was refused because `on_delete=RESTRICT` references point at the objects being deleted.
`restricted_objects` holds the referencing objects, as `protected_objects` of `ProtectedError` does.

### `adorm.InternalError`

The database reported an internal error or an invalid cursor or transaction state (SQLSTATE classes `XX`, `24`,
`25`, `2B`, `2D`).

### `adorm.ProgrammingError`

The statement is wrong: missing table or column, syntax error, insufficient privilege, an `ON CONFLICT` that
affects a row twice, `RAISE EXCEPTION` in PL/pgSQL (SQLSTATE classes `42`, `21`, `P0` and others, see
[the mapping](#adorm-exceptions-wrap-driver-error-exc)).

### `adorm.FieldError(message=None, *, model=None, key=None, original=None, sqlstate=None)`

A field path or lookup key does not resolve on its model, as Django's `FieldError`: a segment that is neither a field
nor a relation, a segment after a field that is neither a relation nor a JSON field (an unknown lookup name such as
`first_name__icontain`, a Django transform such as `joined__year`), or a lookup a relation does not take
(`club__icontains`).
`model` is the label of the model the failing segment was resolved on and `key` the path or key; a driver that
reports the error without them leaves them `None`.

[`resolve_lookup()`](/ref/lookups#adorm-query-resolve-lookup-target-key-annotations-source-none) and
[`check_lookups()`](/ref/lookups#adorm-query-check-lookups-target-condition-annotations-source-none) raise it
before any operation runs; every driver raises it for a path it cannot resolve while it builds the statement, and
[`Driver.check()`](/ref/drivers#check-operation) raises it in place of `NotSupportedError` for an operation that also
uses a construct the driver lacks. A
`FieldError` without a SQLSTATE does not affect the [transaction](/topics/transactions#a-failed-operation) it is
raised in.

```python
try:
    resolve_lookup(Member, "first_name__icontain")
except adorm.FieldError as exc:
    exc.model, exc.key    # ('clubs.Member', 'first_name__icontain')
    str(exc)              # "unsupported lookup 'icontain' for clubs.Member.first_name; perhaps you meant 'icontains'"
```

### `adorm.NotSupportedError`

`NotSupportedError(message=None, *, driver=None, unsupported=(), original=None, sqlstate=None)`

The database or the driver does not support the requested feature (SQLSTATE class `0A`). A driver's capability
check ([`Driver.check()`](/ref/drivers#check-operation), [`Capabilities`](/ref/drivers#adorm-drivers-capabilities))
sets `driver` to the driver's name and `unsupported` to the tuple of names it lacks: an operation kind, construct
names, connection option names or `"savepoints"`. It raises before anything reaches the connection, so a
transaction it is raised in stays usable. Otherwise `driver` is `None` and `unsupported` is empty.

```python
class Unaccent(Func):
    arity = 1


try:
    await Member.objects.annotate(n=Unaccent("first_name")).count()
except adorm.NotSupportedError as exc:
    exc.driver          # 'django'
    exc.unsupported     # ('function.Unaccent',)
```

### `adorm.TransactionManagementError`

A transaction was used incorrectly. An [`atomic()`](/ref/transactions) block raises it for an operation after an
operation of the block failed, at a normal end after such a failure (once it has rolled back, with the failure as
`__cause__`), when it is entered a second time, and when it would nest in an active transaction that is not an
`Atomic`. A session raises it for a read of rows with `select_for_update()` outside a transaction.

```python
async with adorm.atomic():
    try:
        await Member.objects.create(name="Ana")        # IntegrityError
    except adorm.IntegrityError:
        pass
# TransactionManagementError: rolled back because an operation failed
```

### `adorm.TransactionClosed`

An operation arrived for a transaction that has already ended, typically from a task that inherited the
transaction's context and outlived the `atomic()` block. It is also raised by operations after `tx.commit()` or
`tx.rollback()`, by the requests of a lease whose slot aborted the transaction (its `commit_timeout`), by the
normal end of a block whose transaction the slot aborted, and by the normal end of a savepoint block that its
enclosing block closed first.

```python
async with adorm.atomic():
    task = asyncio.create_task(log_activity(member))   # inherits the transaction
# the block has committed

try:
    await task
except adorm.TransactionClosed:
    ...
```

### `adorm.TransactionOverlapError`

Two transactions that are not nested overlapped on a connection that cannot isolate them: the test connection that the
slots share inside Django's `TestCase` ([test mode](/topics/testing)). A transaction, or an operation outside a
transaction, raises it when it starts while another flow's transaction is open; the end of a transaction raises it
while a transaction begun inside it is still open, and so does that inner transaction's next operation. A test that
needs concurrent transactions isolated uses `TransactionTestCase`.

```python
async def other_flow():
    async with adorm.atomic():
        await Member.objects.create(name="bob")


class SignupTests(TestCase):
    async def test_a_second_writer(self):
        async with adorm.atomic():
            await Member.objects.create(name="ann")
            # A flow that does not run inside this transaction: its own transaction overlaps it.
            task = asyncio.create_task(other_flow(), context=contextvars.Context())
            with self.assertRaises(adorm.TransactionOverlapError):
                await task
```

## Executor errors

### `adorm.SessionTimeout(message=None, *, session=None, timeout=None)`

No slot became free within the acquisition timeout. `session` is the session that asked for the slot and
`timeout` the limit in seconds.

```python
try:
    async with reports.atomic():
        ...
except adorm.SessionTimeout as exc:
    log.warning("no free slot for %r after %s s", exc.session, exc.timeout)
```

### `adorm.DeadlockError(message=None, *, held_sessions=(), session=None, timeout=None)`

A `SessionTimeout` raised while the waiting flow already holds slots, so waiting can make it wait on itself. It is
raised when the acquisition times out while the flow holds a slot, and at once when every slot is held by a flow
that itself waits for a slot. Inside a transaction, an operation or a nested `atomic()` that waits longer than the
`acquire_timeout` connection option in effect for a savepoint of another task to end raises it too, with the
transaction's session in `held_sessions`. `held_sessions` is a tuple of the sessions whose slots the flow holds.

```python
audit = adorm.create_session(using="default", session_name="audit")

async with adorm.atomic():                 # holds a slot of the "default" pool
    try:
        async with audit.atomic():         # needs a second slot of the same pool
            ...
    except adorm.DeadlockError as exc:
        log.error("waiting for %r while holding %r", exc.session, exc.held_sessions)
```

### `adorm.OperationTimeout(message=None, *, phase=None, operation=None, session=None, timeout=None)`

An [`adorm.timeout()`](/ref/options#adorm-timeout-delay) block expired. A subclass of `TimeoutError`, so
`except TimeoutError` catches it too. It is raised from the block's `TimeoutError`, so
[`cancellation_outcome()`](/ref/pool#adorm-cancellation-outcome-exc) tells what the interrupted request did.

| Attribute | Value |
|---|---|
| `phase` | `"waiting_for_slot"` or `"executing"`, `None` when no adorm operation was in progress; any other value raises `ValueError` |
| `operation` | the operation in progress: an adorm operation, or the name of a transaction request (`"begin"`, `"commit"` …); `None` when none was |
| `session` | the session of that operation, `None` when none was |
| `timeout` | the limit in seconds |

```python
try:
    async with adorm.timeout(5):
        rows = [m async for m in Member.objects.all()]
except adorm.OperationTimeout as exc:
    if exc.phase == "waiting_for_slot":
        ...                               # the pool is saturated
    else:
        log.warning("slow query: %r", exc.operation)
```

## For driver authors

### `adorm.exceptions.wrap_driver_error(exc)`

Returns the adorm exception for an exception a driver got from its ORM or database library. The caller raises the
result:

```python
from adorm.exceptions import AdormError, wrap_driver_error


async def execute(self, operation):
    try:
        return await self._run(operation)
    except AdormError:
        raise
    except Exception as exc:
        raise wrap_driver_error(exc) from exc
```

The class is chosen in this order:

1. An adorm exception, and an exception that is not an `Exception` (`asyncio.CancelledError`,
   `KeyboardInterrupt`, `SystemExit`, `GeneratorExit`), is returned unchanged, so a cancelled task stays cancelled.
2. The SQLSTATE code from the `sqlstate` (asyncpg, psycopg 3) or `pgcode` (psycopg 2) attribute of the exception
   or of an exception in its `__cause__` chain (Django sets the database library's exception as the cause). When
   a SQLSTATE is found, its class alone decides, so every driver maps the same database error to the same class:

   | SQLSTATE class | Exception |
   |---|---|
   | `22` | `DataError` |
   | `23` | `IntegrityError` |
   | `0A` | `NotSupportedError` |
   | `10`, `20`, `21`, `26`, `34`, `3F`, `42`, `44`, `P*` | `ProgrammingError` |
   | `24`, `25`, `2B`, `2D`, `X*` | `InternalError` |
   | `08`, `27`, `28`, `2F`, `38`, `39`, `3B`, `3D`, `40`, `53`, `54`, `55`, `57`, `58`, `F*`, `H*` | `OperationalError` |
   | any other | `DatabaseError` |

   The table is psycopg's, except class `3D` (missing database): psycopg reports a failed connection as
   `OperationalError` without a SQLSTATE, and asyncpg reports it with SQLSTATE `3D000`.

3. Without a SQLSTATE, a PEP 249 class name in the exception's MRO (`DataError`, `OperationalError`,
   `IntegrityError`, `InternalError`, `ProgrammingError`, `NotSupportedError`, `InterfaceError`, `DatabaseError`),
   as Django's and psycopg's errors have.
4. An `OSError` (including `ConnectionError`, `TimeoutError` and `socket.gaierror`) becomes `OperationalError`.
5. Anything else becomes `DatabaseError`.

The result carries the exception in `original` and `__cause__`, and the SQLSTATE in `sqlstate`. Errors with data
of their own, such as Django's `ProtectedError` and `ValidationError`, are converted by the driver before it calls
`wrap_driver_error`, because their objects must become adorm data.

### `adorm.exceptions.TimeoutPhase`

`Literal["waiting_for_slot", "executing"]`, the type of `OperationTimeout.phase`.
