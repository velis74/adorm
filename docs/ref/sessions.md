# Sessions

Sessions, the current session, bound models and the runner of operations outside a transaction, importable from
`adorm.executor`. `Session`, `BoundModel`, `create_session` and `current_session` are also importable from `adorm`.
The [sessions topic](/topics/sessions) explains how they fit together; transactions and the active transactions of a
context are on the [transactions page](/ref/transactions).

```python
from adorm import BoundModel, create_session, current_session, Session
from adorm.executor import get_runner, Runner, set_runner, TransactionRunner
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

#### `bind(model, manager="objects")`

The [manager](./models#adorm-manager-orm-name-none-method-manager-none) `manager` of `model` (a model class or the label of a registered
model) bound to this session, as a `BoundModel`. A label that is not registered raises `LookupError`; an abstract
model and a `model` that is not a model class raise `TypeError`; a model without that manager raises
`AttributeError`.

```python
reports.bind(Member)
reports.bind("club.Member")
reports.bind(Loan, "all_loans")
```

#### `session.<ModelName>`

The registered model with that class name, bound to this session: `bind(model)`. No such model raises
`AttributeError`; so do two registered models with that class name (bind one of them by label instead).

#### `activate()`

A synchronous context manager that makes this session the current session for its block and restores the previous
one on exit, also on an exception. Tasks created inside the block inherit the session.

```python
with reports.activate():
    await Member.objects.count()       # runs on reports
```

#### `atomic()`

An [`Atomic`](/ref/transactions#atomic-class) block on this session: a transaction, or a savepoint when the session
has an active `Atomic` transaction in the context.

```python
async with reports.atomic() as tx:
    await reports.Member.filter(active=False).update(fee=0)
```

#### `set_options(**options)`

Replaces the session's [connection options](/ref/options), the layer between the profile's options and
[`connection_options()`](/ref/options#adorm-connection-options-options) blocks; without arguments it removes them.
They belong to the pair `(using, session_name)`, so a session object created again for the pair has them too.
Returns the checked options as a read-only mapping. Values are checked as in `connection_options()` (`ValueError`);
a name the session's driver does not declare raises `NotSupportedError` when an operation runs. Thread-safe.

```python
reports.set_options(statement_timeout=300, acquire_timeout=60)
reports.options                       # {'statement_timeout': 300, 'acquire_timeout': 60.0}
reports.set_options()                 # removes them
```

#### `options`

The connection options set on the session with `set_options()`, a read-only mapping; empty when none are set.

#### `async execute(operation)`

Run `operation` and return its result: in this session's transaction active in the current context
([`active_transaction()`](/ref/transactions#adorm-executor-active-transaction-session)), or else on the
[runner](#adorm-executor-runner).
Without a transaction and without a runner it raises [`InterfaceError`](./exceptions). Without a transaction, a read
of rows (`select`, `get`, `first`, `last`, `in_bulk`) whose query has `select_for_update()` raises `TransactionManagementError`
before it reaches the runner. Errors of the transaction or the runner propagate unchanged.

```python
from adorm.operations import Count
from adorm.query import Query

await reports.execute(Count(Query("club.Member")))
```

## Bound models

### `adorm.BoundModel`

A model class bound to a session: what [`Model.objects`](./models#objects), another
[manager](./models#adorm-manager-orm-name-none-method-manager-none) of the model and `session.<ModelName>` return. A declared `Manager` is
a `BoundModel` that is not bound yet; reading it on a model class gives a bound copy of the same class, so the
methods of a `Manager` subclass are methods of the bound model. Immutable; equal when class, model, session, manager
name and ORM names are the same; a copy and a deep copy are the same object, and it pickles by model class, manager
name and session. `BoundModel(model, session)` is a manager without manager logic (no ORM names, plain `QuerySet`
query sets) and takes a concrete model class and a `Session` (`TypeError` otherwise); the declared managers come from
`Model.<manager>`, `session.<ModelName>` and [`bind()`](#bind-model-manager-objects).

Binding takes no slot and includes no transaction: every operation looks up the session's active transaction when
it runs.

| Member | Meaning |
|---|---|
| `model` | the model class |
| `session` | the `Session` |
| `name` | the manager's attribute name on the model (`"objects"`) |
| `orm_name` | the target ORM's manager the driver applies ([`Query.manager`](./query#adorm-query-query-model-manager-none)), `None` for none |
| `method_manager` | the target ORM's manager the driver methods run on, `None` for none |
| `queryset_class` | class attribute: the class of the query sets, [`QuerySet`](./queryset) unless a subclass sets another |
| `objects` | the model's `objects` manager on the same session; the bound model itself when it is that manager |
| `<manager name>` | another manager of the model, on the same session (`session.Loan.all_loans`) |
| `get_queryset()` | a `queryset_class` query set of every row, on `session`, whose query carries `orm_name`; a subclass overrides it to add conditions |
| `all()` | `get_queryset()` |
| `async execute(operation)` | `session.execute(operation)` |

The query methods of [`QuerySet`](./queryset) are also methods of the bound model, applied to `all()`: `filter`,
`exclude`, `annotate`, `alias`, `order_by`, `values`, `values_list`, `only`, `defer`, `select_related`,
`prefetch_related`, `distinct`, `none`, `select_for_update`, `union`, `intersection`, `difference`, and the coroutines `get`, `first`, `last`, `count`, `exists`,
`in_bulk`, `aggregate`, `create`, `bulk_create`, `create_from_parent`, `get_or_create`, `update_or_create`, `update` and `delete`. `async for` iterates over every row. A public method of `queryset_class` that `QuerySet`
does not define is a method of the bound model too, applied to `get_queryset()`, unless it is a driver method
declared `queryset_only` or a function with a true `queryset_only` attribute.

```python
members = Member.objects
await members.get(pk=1)
await members.filter(active=False).delete()
async for member in members:
    ...

await Loan.objects.overdue(today).count()       # a method of Loan's queryset_class
await Loan.objects.all_loans.count()            # another manager, same session
```

## Runner

### `adorm.executor.Runner`

The protocol of what runs an operation of a session outside a transaction: `await runner.run(session, operation)`
acquires a slot for `session`, executes the operation, releases the slot and returns the result.
[`Router`](/ref/pool#adorm-executor-router-pools-profiles-none) is one.

```python
class Direct:
    def __init__(self, connection):
        self.connection = connection

    async def run(self, session, operation):
        return await self.connection.execute(operation)
```

### `adorm.executor.TransactionRunner`

The protocol of a `Runner` that also holds a slot for a transaction: `runner.slot(session)` returns an async context
manager that acquires a slot for `session` on entry, raising `SessionTimeout` or `DeadlockError`, yields its
[`SlotLease`](/ref/pool#adorm-executor-slotlease) and releases it on exit.
[`Router`](/ref/pool#adorm-executor-router-pools-profiles-none) is one. [`atomic()`](/ref/transactions#adorm-atomic) needs one as
the runner.

```python
assert isinstance(router, TransactionRunner)

async with router.slot(session) as lease:
    await lease.call("execute", operation)
```

### `adorm.executor.set_runner(runner)`

Set the `Runner` that `Session.execute()` uses outside a transaction. Returns the previous runner; `None` removes it.
An object without an `async run(session, operation)` raises `TypeError`.

```python
previous = set_runner(Direct(connection))
```

### `adorm.executor.get_runner()`

The `Runner` set with `set_runner()`. When none is set, inside a running event loop, and when the
[settings](/ref/settings) have a connection profile or a provider, the loop's default router
([`default_router()`](/ref/settings#adorm-settings-default-router-create-true)); `None` otherwise.
[`atomic()`](/ref/transactions#adorm-atomic) takes its slots from it when it is a `TransactionRunner`.

```python
adorm.configure(profiles={"default": {"driver": DjangoDriver}})
get_runner()          # inside a coroutine: the event loop's default router
```

## Instances

Importing `adorm` installs an [executor resolver](./models#adorm-models-set-executor-resolver-resolver) that returns
the current session, so an instance without its own executor (one the application built) runs `save()` and relation
loads on the session current when they run. Instances a query set returns keep that query set's session.
