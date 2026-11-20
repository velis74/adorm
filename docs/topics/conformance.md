# Driver conformance suite

The conformance suite (`tests/conformance`) runs every bundled driver (Django, native PostgreSQL) and the test
suite's memory driver (`tests/memory_driver.py`) over the same schema and data: the test project's apps `clubs`,
`activities` and `billing` (`tests/djangoproject`, their adorm models in `tests/clubmodels`), filled by
`manage.py seed_clubs` in the test database. It runs in `uv run pytest` and in CI.

The suite also runs the Django driver on SQLite, with the settings `tests.djangoproject.settings_sqlite`: the club
schema in a SQLite database file in a temporary directory of the process, removed when it ends. The other drivers
are skipped there, and the tests that read PostgreSQL's catalogue or sessions, drive its locks or listen for its
notifications are skipped or replaced by `tests/conformance/test_sqlite.py`, which runs only on SQLite. CI runs both.

```bash
uv run pytest --ds=tests.djangoproject.settings_sqlite tests/conformance
```

On SQLite the `Blocker` is `SQLiteWriteLock`: a `BEGIN IMMEDIATE` transaction on a `sqlite3` connection of its own,
which an update waits for (`DjangoConnection.waiting_for_write_lock`).

The suite checks each driver against its [capabilities](/topics/drivers#capabilities):

- an operation whose kind and constructs the driver declares returns the same result on every driver;
- an operation with a kind or construct the driver does not declare raises `NotSupportedError` naming what is
  missing, before it reaches a connection; the pool and the transaction stay usable.

## The catalogue

`tests/conformance/cases.py` holds the cases. A case is a coroutine that runs operations through the public API,
decorated with `@case`. The positional arguments of `@case` are the case's `uses`: the operation kinds and construct
names its operations report. `expected` is the result:

```python
@case("select", "lookup.lt", expected=["Regional bouldering cup"])
async def exclude_keeps_rows_whose_column_is_null():
    return await names(Competition.objects.exclude(entry_deadline__lt=datetime.date(2026, 4, 1)))

@case("select", "lookup.exact", "path.nested", expected=["Igor", "Jana", "Klemen"])
async def lookups_across_relations():
    return await first_names(Member.objects.filter(memberships__club__slug="koper-sailing"))
```

Every case runs on every driver (`test_case[<driver>-<case>]`):

| The driver declares | The case must |
|---|---|
| every name in `uses` | return `expected`; the operations it sent report exactly `uses` |
| not all of them | raise `NotSupportedError` whose `driver` is the driver's name and whose `unsupported` names only what the refused operation uses and the driver lacks |

After each case no slot is in use, and the pool serves the next operation on the slot it kept.

`refusals` lists drivers that refuse the case during execution, for a limit that depends on the schema, the rows or
the database and so does not show in the names (the Django driver on SQLite refuses a sliced or ordered query in a
set operation). `on_vendor` holds the result on a database where it differs, by Django's vendor name: SQLite sorts `NULL`
first in ascending order. The memory driver refuses a delete that would cascade and a condition on a
reverse or many-to-many relation name such as `clubs=club`:

```python
@case("delete", "count", "lookup.exact", expected=(...), refusals={"memory": "does not cascade"}, django=...)
async def delete_cascades():
    ...
```

A case's `django` is a synchronous mirror in the Django ORM. `test_django_mirror[<case>]` runs it over the seeded
database in a rolled-back transaction and compares its result with `expected`, also for a case no driver runs to
completion. Every write case has a mirror, and so does every case that no driver of the suite runs to completion; a
coverage test fails for a case with neither.

The cases cover the reads (row types, `get`, `first`, `last`, `count`, `exists`, `none()`), every lookup and transform,
`Q` combinations and negation of NULL fields, comparison with fields, arithmetic and model instances, ordering with NULL
placement, slicing, `distinct()` and `DISTINCT ON`, the row formats, `only()` and `defer()`, foreign keys, reverse and
many-to-many relations, multi-table inheritance, generic relations, JSON keys, subqueries, annotations, every function
and aggregate, `select_for_update()` with its options, set operations (`union()` with and without `all`,
`intersection()`, `difference()`, combinations left with a single query after empty ones), `prefetch_related()`
(reverse, forward and many-to-many relations, a `Prefetch` query set into `to_attr`, across a generic foreign key),
generic foreign key objects, content type lookups and generic foreign key assignment (also in `get_or_create()` and
`update_or_create()` defaults), `in_bulk()`, `full_clean()`, and the writes (`create`, `bulk_create` with generated
primary keys, defaults, batches that insert every row or none, `ignore_conflicts` and `update_conflicts`,
`update_fields` and `unique_fields` ignored without `update_conflicts`, `create_from_parent`, `save` also with
arithmetic, `update`, `delete` of a query set and of an instance, a delete refused with `ProtectedError`, also caught
inside a transaction that then commits, `get_or_create`, `update_or_create`).

Coverage tests fail when:

- an operation kind of [`OPERATIONS`](/ref/operations#adorm-operations-operations) (other than `call`, which needs a
  driver method of the model) or a construct of adorm's
  [`construct_names()`](/ref/query#adorm-query-construct-names) has no case;
- a driver declares a name adorm does not know;
- a driver declares a name no case uses;
- a bundled driver has no entry in the suite;
- a case neither runs to completion on a driver of the suite nor has a Django mirror;
- a driver of the suite declares a connection option the transaction tests have no value for.

## Transactions and connection options

`tests/conformance/test_transactions.py` runs every driver through the capabilities that are not operations:

| Test | What holds |
|---|---|
| nested `atomic()` blocks, on a driver with `savepoints` | a nested block commits on normal exit and rolls back on an exception; the outer block keeps its own writes |
| a database error (`IntegrityError` of a duplicate primary key) in a nested block, on a driver with `savepoints` and `create` | the nested block rolls back; the outer block writes again and commits both writes; one slot, released |
| nested `atomic()` blocks, on a driver without `savepoints` | `NotSupportedError` with the driver's name and `unsupported == ("savepoints",)`; the outer block goes on reading and commits; one slot, released |
| a connection option the driver declares | `set_options()` accepts the value in `OPTIONS` (for `role`, the login role) and then `None`, and the connection executes the next operation after each |
| a custom option, on a driver that declares custom options | `set_options()` sets and resets a value with a quote and a backslash, with and without `local` |
| a connection option the driver does not declare | `NotSupportedError` naming it, with `local` false and true; the connection stays usable |

`tests/conformance/test_options.py` runs every driver through [connection options](/topics/options) and
`adorm.timeout()` on the executor, each test repeated. A blocked operation waits on the driver's `Blocker`
(below). "Waits" means it still waits after twice the limit the test set:

| Test | What holds |
|---|---|
| a blocked read in a `connection_options(lock_timeout=…)` or `(statement_timeout=…)` block | `OperationalError` with SQLSTATE `55P03` or `57014`; after the block the same slot waits again |
| a session's `set_options(lock_timeout=…)` | every operation of the session is limited; a block with `lock_timeout=None` lifts the limit |
| a profile's `options={"lock_timeout": …}` | every operation is limited; a block or a session's `set_options()` with `lock_timeout=None` lifts the limit, and it returns when they end |
| `idle_in_transaction_session_timeout` left on the only slot by one session, then another session's transaction that idles past it before its first operation | the transaction runs and commits: its `BEGIN` reset the option |
| a limited read in a savepoint that rolls back | the enclosing transaction waits again |
| a limit set inside a released savepoint, then lifted inside the same transaction | the transaction waits again (`SET LOCAL … TO DEFAULT` was sent) |
| a limit set inside a committed transaction | the next operation on the slot waits |
| `current_setting()` read in transactions with two options, with a block inside them, and with none | the options in effect at each read: the inner block's inside it, the outer block's after it, the defaults without options |
| an `on_acquire` hook that returns `statement_timeout`, around operations with and without a block, inside and outside a transaction, and with a block that sets `None` | the block's value inside the block, the hook's value without one, the database's default for `None`; the connection ran no custom SQL |
| an `on_acquire` hook that returns a custom option from a context variable, for users in turn and without one | each lease's value, `''` without one |
| an option the driver does not declare | `NotSupportedError` naming it before a slot is opened; `None` values run |
| `adorm.timeout()` around a blocked operation | `OperationTimeout` with `phase="executing"`, the operation and the session; the pool serves the next operation |
| `adorm.timeout()` while the only slot is held | `OperationTimeout` with `phase="waiting_for_slot"` and `CancellationOutcome.NOT_STARTED`; nothing stays queued |

## Python code of the target ORM

`tests/conformance/test_driver_code.py` runs every driver through the Python code of the target ORM that a write
calls. `ConformanceDriver.driver_code` (one of `DRIVER_CODE`) states what the driver does with that code:

| `driver_code` | Driver | A model's `save()` / `delete()` override, a query set's `create()` / `update()` override, a custom `on_delete` handler | Django's signals, Django's `on_commit()` callbacks |
|---|---|---|---|
| `"runs"` | Django | runs on the driver side | `pre_save`, `post_save`, `pre_delete`, `post_delete` fire; `on_commit()` callbacks run after the commit |
| `"refused"` | native PostgreSQL | the write raises `NotSupportedError` naming the override or handler; nothing is written | do not fire; none is registered |
| `"not run"` | memory | not run; the write goes ahead (a delete that would reach the handler is refused, as the driver does not cascade) | do not fire; none is registered |

`pre_init` and `post_init` fire on the Django and the native PostgreSQL driver, which build Django model instances,
and not on the memory driver.

After the data is seeded, the tests:

- override the Django model `Tag`'s `save()` (stores the name in upper case) and `delete()` (deletes nothing);
- override `create()`, `update()` and `bulk_create()` of `Tag`'s base manager's query set;
- replace the `on_delete` handler of `Activity.coach` with a custom one;
- connect signal receivers.

| Test | What holds |
|---|---|
| `create()`, `save()` and `get_or_create()` with the `save()` override | the stored names and the override's calls the driver's `driver_code` implies; afterwards a transaction refuses the write (`"refused"`) and still commits its other writes |
| `delete()` of an instance with the `delete()` override | `(0, {})` with the row kept (`"runs"`), `NotSupportedError` with the row kept (`"refused"`), the row deleted (`"not run"`); a query set delete runs no model's `delete()` on any driver |
| create, save and delete with signal receivers | the receivers see six calls on the Django driver and none on the others |
| `create()` and `update()` with the query set overrides | the override's calls and stored names on `"runs"`, `NotSupportedError` with nothing written on `"refused"`, the plain write on `"not run"` |
| `bulk_create()` with the query set's `bulk_create()` override | the same, for `bulk_create` |
| `bulk_create()` with the `save()` override and `pre_save` / `post_save` receivers | on every driver: no `save()` call, no signal, the names stored as given |
| deleting a coach with the custom `on_delete` handler | the handler runs and the coach is deleted (`"runs"`); `NotSupportedError` with nothing deleted (`"refused"`, `"not run"`) |
| a `post_save` receiver that registers `transaction.on_commit()` inside `adorm.atomic()` | the callback runs after the commit, not inside the block or after a rollback, on `"runs"`; never on the others |
| `create()` with `pre_init` and `post_init` receivers | both fire once on the Django and the native PostgreSQL driver, never on the memory driver |

```python
@pytest.mark.usefixtures("save_override")
async def test_a_models_save_override(conformance_driver, router, calls):
    ...
    if code == "refused":
        with pytest.raises(NotSupportedError, match=r"overrides save\(\)"):
            await Tag.objects.create(name="night")
```

## Integrity errors

`tests/conformance/test_integrity.py` violates constraints on every driver. The tests read constraint names from
`pg_constraint`. The memory driver enforces only the primary key and runs only the first test:

| Test | What holds |
|---|---|
| a duplicate primary key, repeated | `IntegrityError` with SQLSTATE `23505`, `columns`, `values`, `key`, `detail`, `model` and `fields` the same on every driver; `constraint_name`, `table_name` and `schema_name` on the SQL drivers; no slot in use, nothing written |
| a duplicate unique field in a nested `atomic()`, repeated | `IntegrityError` `23505` with the constraint, the table and the key; the outer transaction stays usable and commits |
| a duplicate composite key | the constraint `clubs_membership_unique`, the key of both columns and their adorm field names |
| a deferred foreign key, repeated | `IntegrityError` `23503` at the `COMMIT` of `adorm.atomic()` with the constraint, the key and the detail; the slot is dropped, nothing written |
| a concurrent insert of the same key, repeated, 2 slots | the second insert waits for the first transaction (seen in `pg_stat_activity`, not by sleeping); `IntegrityError` with the key when the first transaction commits, success when it rolls back; one row, `in_use == 0` |

## Failures

`tests/conformance/test_failures.py` runs every driver through refusals, concurrency, cancellation and lost
connections, each concurrency test repeated:

| Test | What holds |
|---|---|
| a refusal while the only slot is held | `NotSupportedError` at once, not a slot timeout; nothing queues |
| twenty concurrent refusals | no slot is opened |
| refusals in a transaction, alone and racing reads in `gather()` | the transaction goes on and commits; one slot |
| reads on a pool of three | every result right, at most three slots |
| `TaskGroup` and `gather()` tasks in a transaction | they inherit its slot |
| a second session inside a transaction on a full pool | `DeadlockError` at once |
| cancelling an operation that waits in the database | the operation stops waiting, the slot returns and serves the next operation |
| cancellation racing completion, cancelled after 0 to 2744 event loop iterations | both outcomes occur, the result and `CancelledError`; the released slot keeps no transaction or lock open, before anything else runs on it |
| a write cancelled racing its completion, after 0 to 2744 event loop iterations | the result, or a `CancellationOutcome` of `SUCCEEDED` or `UNKNOWN`; the row is changed after a result or `SUCCEEDED` |
| a block cancelled racing its commit, after 0 to 6859 event loop iterations | `TransactionOutcome.COMMITTED` and a cancelled commit both occur; the row is changed after `COMMITTED` and unchanged after `ROLLED_BACK`; the slot is released |
| cancelling inside a transaction | the transaction rolls back; no other connection stays in a transaction |
| a server connection terminated mid-operation, also inside a transaction | a connection error; the slot is dropped, no other connection stays in a transaction and the next operation gets a new one |

`tests/conformance/test_bulk_create.py` runs `bulk_create()` through the same failures. The tests that need a table
lock or a unique constraint other than the primary key run on the SQL drivers only:

| Test | What holds |
|---|---|
| a duplicate primary key in the last of three one-row batches, repeated | `IntegrityError`; none of the rows stored, the instances unchanged; one slot, released |
| a failing `bulk_create()` in a nested `atomic()` | the savepoint rolls back its rows; the outer transaction inserts again and commits |
| five `TaskGroup` tasks inserting in one transaction, repeated | every row stored with its own primary key, on the transaction's slot |
| eight concurrent calls on a pool of two | every row stored, at most two slots |
| cancelling a call that waits for a table lock | nothing inserted; the slot returns |
| a call of three one-row batches whose connection is terminated, or which is cancelled, while its second batch waits for a row another transaction inserted; with and without an outer `atomic()`, repeated | a connection error or `CancelledError`; the first batch's row is not stored, the slot returns and the next call inserts |
| a call of three statements cancelled racing its completion | all three rows or none, never one or two; a result or `SUCCEEDED` means three rows |
| `ignore_conflicts` racing a transaction that inserted the same unique key | the call waits for it (seen in `pg_stat_activity`); the row is skipped when the transaction commits and inserted when it rolls back; the other row is inserted either way |
| `update_conflicts` racing a transaction that inserted the same unique key | the call waits for it; it updates that row when the transaction commits and inserts its own when it rolls back |

Each driver's `Blocker` (`tests/conformance/drivers.py`) makes an operation wait without sleeping:

| | SQL drivers | memory driver |
|---|---|---|
| the waiting operation | reads clubs while another connection holds an exclusive lock on their table | updates a club while another transaction holds its row lock |
| `holder_pid()` | the holder connection's server pid | the holder connection's number |
| `open_transactions()`: connections other than the blocker's in an open transaction | every backend of the test database with a transaction start time | every open connection the driver created with a transaction |

The lost connection tests run only on drivers with a server connection.

## Lock waits

`tests/conformance/test_lock_waits.py` runs every driver through the [test helpers](/topics/testing#waiting-until-an-operation-is-in-the-database)
that wait until an operation is in the database:

| Test | What holds |
|---|---|
| an operation waiting for the blocker's lock, repeated | `wait_for_lock()` reports it with the holder's session in `blocking_pids` and its own slot connection's `backend_pid`; once released it finishes, no slot stays in use and nothing runs |
| cancelling an operation once it waits, repeated | the slot returns and the next operation runs once the lock is released |
| an operation sent by a task inside `atomic()`, cancelled once it waits, repeated | it is reported with that task as its sender; the transaction is `ROLLED_BACK` and no slot stays in use |
| an operation reaching the driver | `wait_for_operation()` returns it with its sender and an integer `backend_pid` |
| a `commit` of an insert whose deferred foreign key references a row another session locked, repeated (not on the memory driver) | `wait_for_lock()` reports the `commit` request (`operation` is `None`) with that session in `blocking_pids`; once released the insert is committed and no slot stays in use |
| a monitor reading the holder's session and an unknown pid | only the holder's session comes back, not waiting for a lock; a second `close()` does nothing |

## Running it

```bash
ADORM_TEST_DATABASE_URL=postgres://adorm@127.0.0.1:5432/adorm_test uv run pytest tests/conformance
uv run pytest tests/conformance -k memory               # one driver
uv run pytest tests/conformance -k exclude_keeps_rows   # one case on every driver
```

A driver that is not installed is skipped with the reason. [Writing a driver](/howto/writing-a-driver#_7-run-the-conformance-suite)
shows how a new driver joins the suite.
