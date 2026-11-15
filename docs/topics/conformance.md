# Driver conformance suite

The conformance suite (`tests/conformance`) runs every driver of the [demo application](/intro/demo) (Django, native
PostgreSQL, memory) over the same schema and data: the demo's apps `clubs`, `activities` and `billing`, filled by
its seed command (`manage.py seed_demo`) in the test database. It is part of `uv run pytest` and so of CI.

A driver states what it executes in its [capabilities](/topics/drivers#capabilities). The suite holds it to that
statement in both directions:

- an operation whose kind and constructs the driver declares returns the same result on every driver;
- an operation that uses a kind or construct the driver does not declare raises `NotSupportedError` naming what is
  missing, before it reaches a connection, and leaves the pool and the transaction usable.

## The catalogue

`tests/conformance/cases.py` holds the cases. Each one is a coroutine that runs operations through the public API,
the names those operations report (operation kinds and construct names) and the expected result:

```python
@case("select", "lookup.lt", expected=["Regional bouldering cup"])
async def exclude_keeps_rows_whose_column_is_null():
    return await names(Competition.objects.exclude(entry_deadline__lt=datetime.date(2026, 4, 1)))


@case("select", "lookup.exact", "path.nested", expected=["Igor", "Jana", "Klemen"])
async def lookups_across_relations():
    return await first_names(Member.objects.filter(memberships__club__slug="koper-sailing"))
```

The positional names of `@case` are the case's `uses`: the operation kinds and construct names its operations
report. Every case runs on every driver (`test_case[<driver>-<case>]`):

| The driver declares | The case must |
|---|---|
| every name in `uses` | return `expected`; the operations it sent report exactly `uses` |
| not all of them | raise `NotSupportedError` whose `driver` is the driver's name and whose `unsupported` names only what the refused operation uses and the driver lacks |

After each case no slot is in use, and the pool serves the next operation on the slot it kept. A case lists, in
`refusals`, a driver that refuses it while executing, for a limit its names cannot show because it depends on the
schema or the rows; the memory driver refuses a delete that would cascade, and a condition on a reverse or
many-to-many relation name such as `clubs=club`:

```python
@case("delete", "count", "lookup.exact", expected=(...), refusals={"memory": "does not cascade"}, django=...)
async def delete_cascades():
    ...
```

A case's `django` is a synchronous mirror in the Django ORM. `test_django_mirror[<case>]` runs it over the seeded
database in a transaction it rolls back and compares its result with `expected`, so a case is held to Django's
result even when no driver runs it to completion. Every write case has one, and so has every case that no demo
driver runs to completion; a coverage test fails for a case with neither.

The cases cover the reads (row types, `get`, `first`, `last`, `count`, `exists`, `none()`), every lookup, `Q`
combinations and negation of NULL fields, comparison with fields, arithmetic and model instances, ordering with NULL
placement, slicing, `distinct()` and `DISTINCT ON`, the row formats, `only()` and `defer()`, foreign keys, reverse and
many-to-many relations, multi-table inheritance, generic relations, JSON keys, subqueries, annotations, every function
and aggregate, `select_for_update()` with its options, and the writes (`create`, `save` also with arithmetic,
`update`, `delete` of a query set and of an instance, a delete refused with `ProtectedError`, also caught inside a
transaction that then commits, `get_or_create`, `update_or_create`).

Coverage tests fail when:

- an operation kind of [`OPERATIONS`](/ref/operations#adorm-operations-operations) (other than `call`, which needs a
  driver method of the model) or a construct of adorm's
  [`construct_names()`](/ref/query#adorm-query-construct-names) has no case;
- a driver declares a name adorm does not know (a misspelt construct would otherwise never match);
- a driver declares a name no case uses;
- a demo driver has no entry in the suite;
- a case neither runs to completion on a demo driver nor has a Django mirror;
- a demo driver declares a connection option the transaction tests have no value for.

## Transactions and connection options

`tests/conformance/test_transactions.py` runs every driver through the capabilities that are not operations:

| Test | What holds |
|---|---|
| nested `atomic()` blocks, on a driver with `savepoints` | a nested block commits on normal exit and rolls back on an exception; the outer block keeps its own writes |
| a database error (`IntegrityError` of a duplicate primary key) in a nested block, on a driver with `savepoints` and `create` | the nested block rolls back; the outer block writes again and commits both writes; one slot, released |
| nested `atomic()` blocks, on a driver without `savepoints` | `NotSupportedError` with the driver's name and `unsupported == ("savepoints",)`; the outer block goes on reading and commits; one slot, released |
| a connection option the driver declares | `set_options()` accepts the value in `OPTIONS` and the connection executes the next operation |
| a connection option the driver does not declare | `NotSupportedError` naming it, with `local` false and true; the connection stays usable |

## Failures

`tests/conformance/test_failures.py` runs every driver through what goes wrong in production, each concurrency test
repeated:

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
| cancelling inside a transaction | the transaction rolls back; no other connection stays in a transaction |
| a server connection terminated mid-operation, also inside a transaction | a connection error; the slot is dropped, no other connection stays in a transaction and the next operation gets a new one |

Each driver's `Blocker` (`tests/conformance/drivers.py`) makes an operation wait without sleeping: the SQL drivers
read clubs while another connection holds an exclusive lock on their table, the memory driver updates a club while
another transaction holds its row lock. `open_transactions()` counts the connections other than the blocker's in
an open transaction: on PostgreSQL every backend of the test database with a transaction start time, on the memory
driver every open connection the driver created with a transaction. Lost connections apply to drivers with a server
connection.

## Running it

```console
$ ADORM_TEST_DATABASE_URL=postgres://postgres@127.0.0.1:5432/postgres uv run pytest tests/conformance
$ uv run pytest tests/conformance -k memory               # one driver
$ uv run pytest tests/conformance -k exclude_keeps_rows   # one case on every driver
```

A driver that is not installed is skipped with the reason. [Writing a driver](/howto/writing-a-driver#_7-run-the-conformance-suite)
shows how a new driver joins the suite.
