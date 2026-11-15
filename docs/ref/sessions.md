# Sessions

Sessions, the current session, bound models and the active transactions of a context, importable from
`adorm.executor`. `Session`, `BoundModel`, `create_session` and `current_session` are also importable from `adorm`.
The [sessions topic](/topics/sessions) explains how they fit together.

```python
from adorm import BoundModel, create_session, current_session, Session
from adorm.executor import active_transaction, bind_transaction, Runner, set_runner, Transaction
```

## Sessions

### `adorm.create_session(using="default", session_name="default")`

The open session `(using, session_name)`, created when no live object holds that pair; while a session is
referenced, every call for its pair returns that object. A session nothing references is freed. Both names are non-empty strings (`ValueError` otherwise). Thread-safe: concurrent first calls for one pair
return one session.

```python
reports = create_session(using="reports", session_name="nightly")
assert create_session("reports", "nightly") is reports
```

### `adorm.current_session()`

The current session of the context: the session of the innermost [`Session.activate()`](#activate) block, else
`create_session("default", "default")`.

```python
current_session()            # Session('default', 'default')
```

### `adorm.Session`

A logical session, the pair `(using, session_name)`. `Session(using="default", session_name="default")` returns
`create_session(using, session_name)`; one object per pair, immutable. A copy, a deep copy and an unpickled session
are the same object.

| Member | Meaning |
|---|---|
| `using` | the connection profile name |
| `session_name` | the session name |
| `key` | the pair `(using, session_name)` |

```python
reports.key                  # ("reports", "nightly")
reports.Member               # <BoundModel club.Member on Session('reports', 'nightly')>
```

#### `bind(model)`

`model` (a model class or the label of a registered model) bound to this session, as a `BoundModel`. A label that is
not registered raises `LookupError`; an abstract model raises `TypeError`.

```python
reports.bind(Member)
reports.bind("club.Member")
```

#### `session.<ModelName>`

The registered model with that class name, bound to this session. No such model raises `AttributeError`; so do two
registered models with that class name (bind one of them by label instead).

#### `activate()`

A synchronous context manager that makes this session the current session for its block and restores the previous
one on exit, also on an exception. Tasks created inside the block inherit the session.

```python
with reports.activate():
    await Member.objects.count()       # runs on reports
```

#### `async execute(operation)`

Run `operation` and return its result: in this session's transaction active in the current context
([`active_transaction()`](#adorm-executor-active-transaction-session)), or else on the [runner](#adorm-executor-runner).
Without a transaction and without a runner it raises [`InterfaceError`](./exceptions). Errors of the transaction or the
runner propagate unchanged.

```python
from adorm.operations import Count
from adorm.query import Query

await reports.execute(Count(Query("club.Member")))
```

## Bound models

### `adorm.BoundModel`

A model class bound to a session: what [`Model.objects`](./models#objects) and `session.<ModelName>` return.
Immutable; equal when model and session are the same; a copy and a deep copy are the same object, and it pickles
by model class and session. `BoundModel(model, session)` takes a concrete model class and a
`Session` (`TypeError` otherwise).

Binding takes no slot and includes no transaction: every operation looks up the session's active transaction when
it runs.

| Member | Meaning |
|---|---|
| `model` | the model class |
| `session` | the `Session` |
| `objects` | the bound model itself |
| `all()` | a [`QuerySet`](./queryset) of every row, on `session` |
| `async execute(operation)` | `session.execute(operation)` |

The query methods of [`QuerySet`](./queryset) are also methods of the bound model, applied to `all()`: `filter`,
`exclude`, `annotate`, `order_by`, `values`, `values_list`, `only`, `defer`, `select_related`, `distinct`, `none`,
`select_for_update`, and the coroutines `get`, `first`, `last`, `count`, `exists`, `create`, `get_or_create`,
`update` and `delete`. `async for` iterates over every row.

```python
members = Member.objects
await members.get(pk=1)
await members.filter(active=False).delete()
async for member in members:
    ...
```

## Runner

### `adorm.executor.Runner`

The protocol of what runs an operation of a session outside a transaction: `await runner.run(session, operation)`
acquires a slot for `session`, executes the operation, releases the slot and returns the result.
[`Router`](/ref/pool#adorm-executor-router-pools) is one.

```python
class Direct:
    def __init__(self, connection):
        self.connection = connection

    async def run(self, session, operation):
        return await self.connection.execute(operation)
```

### `adorm.executor.set_runner(runner)`

Set the `Runner` that `Session.execute()` uses outside a transaction. Returns the previous runner; `None` removes it.
An object without an `async run(session, operation)` raises `TypeError`.

```python
previous = set_runner(Direct(connection))
```

## Transactions in the context

### `adorm.executor.Transaction`

The protocol of what runs the operations of a session inside a transaction: `await transaction.execute(operation)`.

### `adorm.executor.active_transaction(session)`

The transaction bound to `session` in the current context, `None` when there is none.

### `adorm.executor.bind_transaction(session, transaction)`

A synchronous context manager that makes `transaction` the active transaction of `session` for its block and yields
it. The binding is a context variable: tasks created inside the block inherit it, also after the block ends. A
binding for the same session inside the block replaces it until the inner block ends; bindings of other sessions are
unaffected. An object without an `async execute(operation)` raises `TypeError`.

```python
with bind_transaction(reports, transaction):
    assert active_transaction(reports) is transaction
    await reports.Member.filter(pk=1).update(fee=0)      # transaction.execute(...)
assert active_transaction(reports) is None
```

## Instances

Importing `adorm` installs an [executor resolver](./models#adorm-models-set-executor-resolver-resolver) that returns
the current session, so an instance without its own executor (one the application built) runs `save()` and relation
loads on the session current when they run. Instances a query set returns keep that query set's session.
