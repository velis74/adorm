# Exceptions

adorm's exceptions are modelled on Django's. They are raised by the connections of the
[driver contract](/ref/drivers), the [pool and router](/ref/pool), [`atomic()`](/ref/transactions),
[`adorm.timeout()`](/ref/options#adorm-timeout-delay), the [Django driver](/topics/django-driver) and the
[native PostgreSQL driver](/topics/postgresql-driver). A driver converts the errors of the target ORM and the
database library with [`wrap_driver_error`](#adorm-exceptions-wrap-driver-error-exc-details-none), so applications
catch only adorm's classes. Every class in the tree below is importable from `adorm` and from `adorm.exceptions`;
`ModelExceptionsMixin`, `wrap_driver_error` and `TimeoutPhase` only from `adorm.exceptions`.

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
│           ├── SchemaMismatchError
│           ├── SchemaCheckError
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

Every model class has its own `DoesNotExist` and `MultipleObjectsReturned`, as in Django. They derive from the
parent model's: `except Member.DoesNotExist` also catches `Coach.DoesNotExist` when `Coach` subclasses `Member`.

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
level. A class that defines `DoesNotExist` or `MultipleObjectsReturned` itself keeps its own; subclasses inherit from
it only when it derives from `ObjectDoesNotExist` or `MultipleObjectsReturned`.

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

The drivers raise Django's validation errors with each error's `message`, `code` and `params`. An instance's errors
(`full_clean()`, a `save()` that validates) form a dict error keyed by the operation model's adorm field names (see
[`full_clean()`](/ref/models#full-clean-exclude-none-validate-unique-true-validate-constraints-true)); a value error,
such as an invalid UUID, stays a single error.

```python
exc = adorm.ValidationError("At most %(limit)d characters.", code="max_length", params={"limit": 30})
exc.messages                  # ['At most 30 characters.']

exc = adorm.ValidationError({"name": "Required.", "age": ["Too low."]})
exc.message_dict              # {'name': ['Required.'], 'age': ['Too low.']}
dict(exc)                     # the same

for field, errors in exc.error_dict.items():
    for error in errors:
        error.message, error.code, error.params   # ('Required.', None, None) …
```

## Database errors

The database errors follow PEP 249, as Django's `django.db` errors do. Each one carries:

| Attribute | Value |
|---|---|
| `original` | the exception the driver got from its ORM or database library, or `None` |
| `sqlstate` | the five-character SQLSTATE code the database reported, or `None` |

The original exception is also the `__cause__`.

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

`sqlstate` is the SQLSTATE the database reported, `None` for a database without SQLSTATEs (SQLite, and MySQL and
Oracle through the Django driver, which report their own error numbers). `connection_lost` is set by the driver
connection that raised the error: whether the error left that connection unusable
([`DriverConnection.is_connection_lost()`](/ref/drivers#adorm-drivers-driverconnection)); `None` when no connection
judged it.

```python
try:
    await Member.objects.count()
except adorm.OperationalError as exc:
    if exc.connection_lost:
        ...   # the slot leaves the pool; the next operation gets a new connection
```

### `adorm.InterfaceError`

An error of the database interface rather than of the database, such as using a closed connection. Not a
`DatabaseError`.

### `adorm.DatabaseError`

An error of the database. An error adorm does not recognise becomes a `DatabaseError`, with the driver's exception
in `original`.

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
`OperationalError`. [`DeadlockError`](#executor-errors) is a deadlock between slots of adorm's pool, not one the
database detects.

```python
for attempt in range(3):
    try:
        async with adorm.atomic():
            await transfer(source, target, amount)
        break
    except adorm.OperationalError as exc:
        if exc.sqlstate not in ("40001", "40P01"):   # serialization failure, deadlock detected
            raise
```

### `adorm.IntegrityError(message=None, *, constraint_name=None, table_name=None, schema_name=None, columns=(), values=(), detail=None, model=None, fields=(), **kwargs)`

A constraint was violated: unique, foreign key, not null, check, exclusion (SQLSTATE class `23`). Besides `original`
and `sqlstate` it describes the violation; every driver over PostgreSQL gives the same values:

| Attribute | Value |
|---|---|
| `constraint_name` | the name of the violated constraint or unique index; `None` for a not-null violation |
| `table_name` | the table of the constraint; for a foreign key, the referencing table |
| `schema_name` | the schema of that table |
| `columns` | the database columns of the violated key, or the column of a not-null violation; `()` for a check violation |
| `values` | the values of the key as text, in the order of `columns`, as PostgreSQL prints them; `()` when unknown |
| `detail` | PostgreSQL's detail message, e.g. `Key (email)=(ana@example.com) already exists.` |
| `model` | the label of the model whose table is `table_name`, e.g. `clubs.Membership` |
| `fields` | the adorm field names of `columns` on `model`, in their order; a column that is no field (an index expression) is left out |
| `key` | property: `dict(zip(columns, values))`, `{}` when `values` is empty |

An unknown value is `None`, or `()` for `columns`, `values` and `fields`. Values by violation:

| Violation | SQLSTATE | `constraint_name` | `columns` | `values` |
|---|---|---|---|---|
| unique, primary key | `23505` | constraint or index | key columns | key values |
| foreign key, inserting or updating the referencing row | `23503` | constraint | referencing columns | their values |
| foreign key, deleting or updating the referenced row | `23503` | constraint | referenced columns | their values |
| exclusion | `23P01` | constraint | constraint columns | the new row's values |
| not null | `23502` | `None` | the column | `()` |
| check | `23514` | constraint | `()` | `()` |

- `columns` and `values` are read from the `Key (…)=(…)` part of `detail`. Quoted column names are unquoted; a
  column of an expression index is the expression as PostgreSQL prints it (`lower((email)::text)`).
- The value of a one-column key is the whole text. PostgreSQL does not quote values, so `values` of a key of several
  columns is `()` when a value contains `", "` and the text splits into more parts than there are columns.
- `detail` is `None` when PostgreSQL leaves the detail out (the user may not read the key's columns). `columns` and
  `values` are then empty, also when PostgreSQL prints the detail in another language (`lc_messages`); the column of
  a not-null violation is reported in both cases.
- `columns` are database column names: a foreign key's column is `club_id`, a field with `db_column="book_isbn"` is
  `book_isbn`. `fields` are the adorm model's names: `book_id` for that foreign key, `copy_` for a field
  [renamed](/howto/generating-models#name-clashes) from `copy`.
- The Django and the native PostgreSQL drivers find `model` among the installed Django models by their table,
  preferring a model registered with adorm. When no installed model has the table, `model` is `None` and `fields`
  empty.
- For the key of a referenced row (a delete or update of the row), `fields` is empty: PostgreSQL does not name the
  referenced table.

```python
try:
    await Membership.objects.create(club_id=club.pk, member_id=member.pk, since=today, fee=0)
except adorm.IntegrityError as exc:
    if exc.constraint_name == "clubs_membership_unique":
        raise adorm.ValidationError("Already a member.", code="unique") from exc
    if exc.sqlstate == "23503" and exc.fields:  # foreign_key_violation
        raise adorm.ValidationError({exc.fields[0]: "Does not exist."}) from exc
    raise
```

```python
exc.constraint_name    # 'clubs_member_email_key'
exc.table_name         # 'clubs_member'
exc.columns            # ('email',)
exc.values             # ('ana@example.com',)
exc.key                # {'email': 'ana@example.com'}
exc.detail             # 'Key (email)=(ana@example.com) already exists.'
exc.model              # 'clubs.Member'
exc.fields             # ('email',)
```

`ProtectedError` and `RestrictedError` carry none of these values: drivers raise them from their check of the
references, before any statement runs.

### `adorm.ProtectedError(message=None, protected_objects=(), **kwargs)`

A delete was refused: `on_delete=PROTECT` references point at the objects being deleted. `protected_objects` holds
the referencing objects. A driver raises it with [`ModelRow`](./operations#adorm-operations-modelrow-model-row)
values; `QuerySet.delete()` and `Model.delete()` turn them into instances of the registered models, and a row of a
model that is not registered stays a `ModelRow`. Without a SQLSTATE it leaves an open transaction usable: the references
are checked before any statement changes data.

```python
try:
    await club.delete()
except adorm.ProtectedError as exc:
    names = [m.name for m in exc.protected_objects]
```

### `adorm.RestrictedError(message=None, restricted_objects=(), **kwargs)`

A delete was refused: `on_delete=RESTRICT` references point at the objects being deleted. `restricted_objects` holds
the referencing objects, as `protected_objects` of `ProtectedError` does.

### `adorm.InternalError`

The database reported an internal error or an invalid cursor or transaction state (SQLSTATE classes `XX`, `24`,
`25`, `2B`, `2D`).

### `adorm.ProgrammingError`

The statement is wrong: missing table or column, syntax error, insufficient privilege, an `ON CONFLICT` that
affects a row twice, `RAISE EXCEPTION` in PL/pgSQL (SQLSTATE classes `42`, `21`, `P0` and others, see
[the mapping](#adorm-exceptions-wrap-driver-error-exc-details-none)).

### `adorm.FieldError(message=None, *, model=None, key=None, original=None, sqlstate=None)`

A field path or lookup key does not resolve on its model, as Django's `FieldError`:

- a segment that is neither a field nor a relation;
- a segment after a field that is neither a relation nor a JSON field and is no transform applying there: an
  unknown lookup or transform name (`first_name__icontain`, `joined__yeer`), a transform of another type
  (`birth_date__hour` on a date);
- a lookup that does not apply to the field (`first_name__has_key`, `first_name__overlap`);
- a lookup a relation does not take (`club__icontains`).

`model` is the label of the model the failing segment was resolved on, `key` the path or key; both are `None` when
the driver reports the error without them.

Raised by [`resolve_lookup()`](/ref/lookups#adorm-query-resolve-lookup-target-key-annotations-source-none) and
[`check_lookups()`](/ref/lookups#adorm-query-check-lookups-target-condition-annotations-source-none) before any
operation runs, by every driver for a path it cannot resolve while building the statement, and by
[`Driver.check()`](/ref/drivers#check-operation-schema-hashes-none-capabilities-none) in place of `NotSupportedError`
for an operation that also uses a construct the driver lacks. A `FieldError` without a SQLSTATE does not affect the
[transaction](/topics/transactions#a-failed-operation) it is raised in.

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
check ([`Driver.check()`](/ref/drivers#check-operation-schema-hashes-none-capabilities-none), [`Capabilities`](/ref/drivers#adorm-drivers-capabilities))
sets `driver` to the driver's name and `unsupported` to the tuple of names it lacks: an operation kind, construct
names, connection option names or `"savepoints"`. The check raises before anything reaches the connection; a
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

### `adorm.SchemaMismatchError(message=None, *, model=None, expected=None, actual=None, driver=None, reason=None)`

A [`ProgrammingError`](#adorm-programmingerror): the adorm model `model` (a label) was generated from another schema
than the driver's target has.

| Attribute | Value |
|---|---|
| `expected` | the model's schema hash (`_meta.schema_hash`) |
| `actual` | the driver's hash; `None` when the target has no such model or the driver could not compute it |
| `reason` | when `actual` is `None`, why: `"the target has no model clubs.Club"`, or the error's type and message |
| `driver` | the driver's name |

[`Driver.check()`](/ref/drivers#check-operation-schema-hashes-none-capabilities-none) raises it for an operation on the mismatched
model or on any model the operation reaches ([`operation_models()`](/ref/drivers#adorm-drivers-operation-models-operation)),
before the operation waits for a slot or reaches the connection. Other models keep working; a transaction it is
raised in stays usable. A [`CheckTable`](/ref/operations#adorm-operations-checktable-model-fields-many-to-many) of a
model the target lacks raises it too, before any statement. See the [schema checks topic](/topics/schema-checks).

```python
try:
    await Club.objects.count()
except adorm.SchemaMismatchError as exc:
    exc.model, exc.expected, exc.actual   # ('clubs.Club', 'e8c2eed312ab7b68', '51f0c9a4b2d71e03')
    str(exc)
    # the adorm model clubs.Club was generated with schema hash e8c2eed312ab7b68, the django driver's model has
    # 51f0c9a4b2d71e03: run adorm generate
```

### `adorm.SchemaCheckError(message=None, *, problems=())`

A [`ProgrammingError`](#adorm-programmingerror) that
[`check_schema(raise_on_problems=True)`](/ref/checks#adorm-check-schema-models-none-session-none-raise-on-problems-false)
raises when it finds problems, and every acquisition of a pool whose
[start-up
check](/ref/pool#adorm-executor-pool-driver-profile-max-connections-0-acquire-timeout-30-0-commit-timeout-60-0-release-timeout-30-0-purge-stale-connections-at-300-0-check-schema-false)
with `check_schema="raise"` found problems. `problems` is the tuple of
[`SchemaProblem`](/ref/checks#adorm-schemaproblem) values; the default message names their number and lists one per
line.

```python
try:
    await adorm.check_schema(raise_on_problems=True)
except adorm.SchemaCheckError as exc:
    str(exc)
    # the database does not match the models (1 problem):
    #   clubs.Club.city: the column clubs_club.city does not exist
```

### `adorm.TransactionManagementError`

A transaction was used incorrectly. An [`atomic()`](/ref/transactions) block raises it:

- for an operation after an operation of the block failed;
- at a normal end after such a failure, once it has rolled back, with the failure as `__cause__`;
- when it is entered a second time;
- when it would nest in an active transaction that is not an `Atomic`.

A session raises it for a read of rows with `select_for_update()` outside a transaction.

```python
async with adorm.atomic():
    try:
        await Member.objects.create(name="Ana")        # IntegrityError
    except adorm.IntegrityError:
        pass
# TransactionManagementError: rolled back because an operation failed
```

### `adorm.TransactionClosed`

An operation arrived for a transaction that has ended. Raised by:

- an operation of a task that inherited the transaction's context and outlived the `atomic()` block;
- operations after `tx.commit()` or `tx.rollback()`;
- the requests of a lease whose slot aborted the transaction (its `commit_timeout`);
- the normal end of a block whose transaction the slot aborted;
- the normal end of a savepoint block that its enclosing block closed first.

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

Work overlapped a transaction it is not nested in, on a connection that cannot isolate them: the test connection the
slots share inside Django's `TestCase` ([test mode](/topics/testing#transactions-in-a-testcase)). A transaction or an
operation outside a transaction that another flow starts there, and a Django statement outside the open
transaction's context, wait for the open transaction instead. Raised by:

- an operation of a transaction, or a savepoint of it, while a transaction begun inside it is open;
- the end of a transaction while a transaction begun inside it is open, and that inner transaction's next operation;
- a `BEGIN` or an operation sent on a lease of [`Router.acquire()`](/ref/pool#async-acquire-session-timeout-none),
  which takes no turn, while a transaction it is not inside is open;
- a Django statement outside an open transaction's context that cannot wait: it runs on the event loop's thread, or
  no adorm flow has taken a turn there yet.

Tests that need isolated concurrent transactions use `TransactionTestCase`.

```python
audit = adorm.create_session(session_name="audit")


async def record(member, opened, done):
    async with audit.atomic():                    # a transaction of another session
        await audit.bind(AuditEntry).create(action=f"created {member.name}")
        opened.set()
        await done.wait()


class SignupTests(TestCase):
    async def test_a_nested_transaction_left_open(self):
        opened, done = asyncio.Event(), asyncio.Event()
        with self.assertRaises(adorm.TransactionOverlapError):
            async with adorm.atomic():
                member = await Member.objects.create(name="ann")
                task = asyncio.create_task(record(member, opened, done))   # nested in this transaction
                await opened.wait()
        # the end of the block rolled back both transactions
        done.set()
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

A `SessionTimeout` raised for a slot acquisition that cannot succeed: the flow holds slots, or it waits for a
transaction on a shared database session that cannot end. `held_sessions` is a tuple of the sessions whose slots the
flow holds. Raised:

- when the acquisition times out while the flow holds a slot;
- at once when every slot is held by a flow that itself waits for a slot;
- inside a transaction, when an operation or a nested `atomic()` waits for a savepoint of another task to end longer
  than the `acquire_timeout` connection option in effect; `held_sessions` holds the transaction's session;
- on a shared database session (Django's `TestCase`, [transaction turns](/ref/pool#transaction-turns)), when a
  transaction, an operation outside a transaction or a Django statement waits for the open transaction: at once when
  that transaction's task awaits the waiting task; else a transaction or an operation after the `acquire_timeout` in
  effect, and a Django statement after the longest acquisition timeout of the transactions that hold or wait for the
  turn ([`take_blocking()`](/ref/pool#take-blocking-what-timeout-none)).

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

An [`adorm.timeout()`](/ref/options#adorm-timeout-delay) block expired. A subclass of `TimeoutError`. It is raised
from the block's `TimeoutError`; [`cancellation_outcome()`](/ref/pool#adorm-cancellation-outcome-exc) tells what the
interrupted request did.

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

### `adorm.exceptions.wrap_driver_error(exc, details=None)`

Returns the adorm exception for an exception a driver got from its ORM or database library; the caller raises it:

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
   or of an exception in its `__cause__` chain (Django sets the database library's exception as the cause). A
   SQLSTATE's class alone decides, so every driver maps a database error to the same class:

   | SQLSTATE class | Exception |
   |---|---|
   | `22` | `DataError` |
   | `23` | `IntegrityError` |
   | `0A` | `NotSupportedError` |
   | `10`, `20`, `21`, `26`, `34`, `3F`, `42`, `44`, `P*` | `ProgrammingError` |
   | `24`, `25`, `2B`, `2D`, `X*` | `InternalError` |
   | `08`, `27`, `28`, `2F`, `38`, `39`, `3B`, `3D`, `40`, `53`, `54`, `55`, `57`, `58`, `F*`, `H*` | `OperationalError` |
   | any other | `DatabaseError` |

   The table is psycopg's, except class `3D` (missing database), mapped to `OperationalError`: psycopg reports a
   missing database as `OperationalError` without a SQLSTATE, asyncpg with SQLSTATE `3D000`.

3. Without a SQLSTATE, a PEP 249 class name in the exception's MRO (`DataError`, `OperationalError`,
   `IntegrityError`, `InternalError`, `ProgrammingError`, `NotSupportedError`, `InterfaceError`, `DatabaseError`),
   as Django's and psycopg's errors have.
4. An `OSError` (including `ConnectionError`, `TimeoutError` and `socket.gaierror`) becomes `OperationalError`.
5. Anything else becomes `DatabaseError`.

The result carries the exception in `original` and `__cause__`, and the SQLSTATE in `sqlstate`. For an
`IntegrityError`, `details(exc)` returns the keyword arguments that describe the violation; without `details` the
violation is not described. The drivers over PostgreSQL pass
[`adorm.drivers.postgresql.integrity_details`](/ref/drivers#adorm-drivers-postgresql-integrity-details-exc). Errors
that carry objects, such as Django's `ProtectedError` and `ValidationError`, are converted by the driver itself before
`wrap_driver_error`, so the objects become adorm data.

### `adorm.exceptions.TimeoutPhase`

`Literal["waiting_for_slot", "executing"]`, the type of `OperationTimeout.phase`.
