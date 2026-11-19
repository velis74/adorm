# Connection options and timeouts

adorm has two kinds of limits:

- **connection options**: session parameters of the database connection (`statement_timeout`, `lock_timeout`,
  `role`, `search_path`, custom options such as `app.user_id`, …) and limits adorm enforces on slots (`acquire_timeout`,
  `commit_timeout`), set in layers;
- **`adorm.timeout()`**: a client-side limit on everything a block of code does with adorm; on expiry it reports
  the phase the operation was in.

The [reference](/ref/options) lists every name.

## Connection options

Options apply in four layers; for each option the last layer that sets it wins:

| Layer | Set with | Applies to |
|---|---|---|
| profile | `ConnectionProfile(options=...)` for database options, the profile's `pool` settings for `acquire_timeout` and `commit_timeout` | every session of the profile |
| hook | the mapping the profile's [`on_acquire` hook](/topics/profiles#hooks) returns | the session's use of one slot, from acquisition to release |
| session | `session.set_options(...)` | the session `(using, session_name)` |
| block | `with adorm.connection_options(...):` | every operation in the block, in tasks created inside it too |

```python
import adorm
from adorm import ConnectionProfile, PoolSettings

adorm.configure(
    profiles={
        "default": ConnectionProfile(
            "default",
            "adorm.drivers.asyncpg.AsyncpgDriver",
            options={"statement_timeout": 30, "lock_timeout": 5},     # seconds
            pool=PoolSettings(acquire_timeout=10, commit_timeout=60),
        ),
    }
)

reports = adorm.create_session("default", "reports")
reports.set_options(statement_timeout=600)          # reports may run longer

with adorm.connection_options(lock_timeout=0.5):
    await Member.objects.filter(pk=member_id).update(fee=0)   # gives up on a held row lock
```

`connection_options()` is a synchronous `with`: it only sets a context variable, like
[`Session.activate()`](/topics/sessions). Time limits are seconds:

- `0` means no limit;
- the largest value is 2147483.647 (PostgreSQL's limit of 2147483647 ms);
- `None` means the database's default, so a block can lift a limit an outer layer sets:

```python
with adorm.connection_options(statement_timeout=None):
    await rebuild_statistics()
```

When a layer's option ends, the operation runs with the value of the layer below it: a block inside a hook's `role`
restores that role when it ends. The database's default applies only when no layer sets the option, or a layer sets
it to `None`.

### Roles and custom options

`role` is the role the connection acts as (PostgreSQL's `SET ROLE`): privileges and row-level security policies
apply to it, while the connection stays logged in as its login role. A custom option is a name of lowercase
identifiers joined by dots, such as `app.user_id`; its value is a string that SQL reads with `current_setting()`.
Custom option names are not Python identifiers, so they are passed by unpacking a mapping:

```python
adorm.configure(
    profiles={
        "default": ConnectionProfile("default", "adorm.drivers.asyncpg.AsyncpgDriver", options={"role": "reporting"}),
    }
)

with adorm.connection_options(role="auditor"):
    await Payment.objects.count()                   # SET role = "auditor"; the profile's role after the block

with adorm.connection_options(**{"app.tenant_id": "17"}):
    await Invoice.objects.filter(paid=False).count()     # a policy reads current_setting('app.tenant_id')
```

`role=None` runs as the login role. The [multiple database users](/howto/multiple-database-users) recipe sets the
current user's role from an `on_acquire` hook.

### Search path

`search_path` names the schemas in which unqualified table names resolve, in order (PostgreSQL's `search_path`). Its
value is a list or tuple of schema names; each is quoted as an identifier. `None` is the database's default.

```python
with adorm.connection_options(search_path=("acme", "public")):
    await Member.objects.count()                    # SET search_path = "acme", "public"
```

The [schema per tenant](/howto/schema-per-tenant) recipe sets it from an `on_acquire` hook.

### Options from a profile hook

The profile's `on_acquire` hook returns the hook layer, or `None` for none. It runs in the context of the task that
acquires the slot, so it can read the current user from a context variable:

```python
def on_acquire(connection, profile, session):
    user = current_user.get()
    return None if user is None else {"role": user.db_role, "app.user_id": str(user.pk)}
```

- The slot sets the options in effect with the hook layer before the acquisition completes; a value the database
  refuses fails the acquisition and the slot leaves the pool.
- The hook layer ends with the lease: the next session on the slot runs with its own.
- Session parameters reach the connection only as options. SQL a hook runs on its `connection` argument is custom
  SQL: the slot does not know what it set, and leaves the pool when the lease ends. The Django driver sees the
  statements a hook runs through Django's execute wrappers (not those on the DB-API connection), the native
  PostgreSQL driver every statement but `COPY`.

### What reaches the database

Every operation carries the database options in effect where it runs. The slot remembers what it set on its
connection and sends only the differences: the same options as the previous operation on that slot send no statement;
an option the previous operation set and this one lacks is reset.

| Where the operation runs | What the slot sends |
|---|---|
| outside a transaction | `SET name = value` for a changed option, `RESET name` for one no longer in effect; the values stay on the connection for the next operation |
| inside `atomic()` | `SET LOCAL name = value` and `SET LOCAL name TO DEFAULT`; they end with the transaction, so after a rollback the slot's record matches the database |
| in a savepoint that rolls back | the slot returns to the values it knew when the savepoint started, as the database does |

```python
async with adorm.atomic():
    with adorm.connection_options(lock_timeout=1):
        await Member.objects.filter(pk=1).update(active=False)   # SET LOCAL lock_timeout = 1000
    await Club.objects.count()                                   # SET LOCAL lock_timeout TO DEFAULT
```

A driver declares the options it supports in [`Capabilities.connection_options`](/ref/drivers#adorm-drivers-capabilities).

- An operation with an option its driver does not declare raises `NotSupportedError` before it takes a slot, outside
  and inside a transaction; a transaction stays usable.
- An option whose value is `None` needs no declaration: on a driver that does not declare it nothing is sent; on one
  that does, it resets a value set earlier.
- The Django driver on PostgreSQL and the native PostgreSQL driver declare `statement_timeout`, `lock_timeout`,
  `idle_in_transaction_session_timeout`, `role`, `search_path` and custom options; the Django driver on other
  databases declares none ([databases](/topics/django-driver#databases)).
- On PostgreSQL, a `DATABASES` entry's `OPTIONS["assume_role"]` starts the profile's `role`: the drivers set it on
  every new connection, so a block that ends restores it.

```python
with adorm.connection_options(statement_timeout=5):
    await Member.objects.count()
# NotSupportedError: unsupported connection options: statement_timeout (driver 'memory')
```

`atomic()` sends the options in effect with its `BEGIN`, as plain `SET` and `RESET` before the transaction starts,
so a transaction never runs with options another session left on the slot. The `COMMIT` runs with the options in
effect at the end of the block (relevant to deferred constraint triggers).

Inside Django's `TestCase` the slots share the test's connection, and every operation sets its slot's options, the
hook layer included, with `SET LOCAL` ([testing](/topics/testing#connection-options-and-profile-hooks)).

### adorm's own options

- `acquire_timeout`: how long an operation, or the entry of `atomic()`, waits for a slot before it raises
  `SessionTimeout` (`DeadlockError` when the flow already holds a slot).
- `commit_timeout`: how long a transaction's slot waits for the next request before it rolls the transaction back. A
  transaction takes the value in effect when it starts.

```python
async def member_count():
    with adorm.connection_options(acquire_timeout=0.5):
        try:
            return await Member.objects.count()
        except adorm.SessionTimeout:
            return cached_count()


with adorm.connection_options(commit_timeout=600):
    async with adorm.atomic():                  # a slow import that awaits other services between writes
        ...
```

## `adorm.timeout()`

`async with adorm.timeout(5):` wraps `asyncio.timeout()`. At the deadline the block's task is cancelled, which cancels
a running statement in the database ([cancellation](/topics/cancellation)), and the block raises `OperationTimeout`, a
`TimeoutError`. Operations in the block, also those of tasks created in it, report their phase to it:

| `OperationTimeout.phase` | The operation |
|---|---|
| `"waiting_for_slot"` | waited for a free slot: the pool is too small or other flows hold their slots too long |
| `"executing"` | was running on its slot: a slow query, a lock wait, a slow commit |
| `None` | no adorm operation was in progress: the time went elsewhere |

`operation` is the operation (or the transaction step, such as `"begin"` or `"commit"`) and `session` its session.
[`cancellation_outcome()`](/ref/pool#adorm-cancellation-outcome-exc) of the error returns that operation's outcome,
also when it ran in a child task.

```python
try:
    async with adorm.timeout(2):
        async with asyncio.TaskGroup() as group:
            members = group.create_task(Member.objects.filter(active=True).count())
            clubs = group.create_task(Club.objects.count())
except adorm.OperationTimeout as exc:
    log.warning("%s on %r: %r", exc.phase, exc.session, exc.operation)
    outcome = adorm.cancellation_outcome(exc)       # NOT_STARTED, SUCCEEDED, FAILED or UNKNOWN
```

`statement_timeout` is enforced by the database per statement, also when the client is gone. `adorm.timeout()`
bounds the whole block, waiting for slots included, and cancels the statement from the client. A write that
`adorm.timeout()` interrupts may or may not have taken effect;
[`cancellation_outcome()`](/ref/pool#adorm-cancellation-outcome-exc) tells which.
