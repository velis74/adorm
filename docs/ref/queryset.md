# QuerySet API

### `adorm.QuerySet`

`QuerySet(model, executor, query=None)`: the rows of `query` on `model` (a model class), run by `executor` (an
object with `async execute(operation)`, normally a [session](./sessions)). `query` defaults to every row of the
model; a query on another model raises `ValueError`, an abstract model `TypeError`. Also importable from
`adorm.query`.

Query sets usually come from a [bound model](./sessions#adorm-boundmodel):

```python
qs = Member.objects.filter(active=True)          # QuerySet on the current session
qs = reports.Member.order_by("name")             # QuerySet on reports
```

A query set is immutable: a copy and a deep copy are the same object, and it pickles when its executor does (a
session does). Building methods return a new query set and do no I/O; terminal methods are coroutines
that run one operation each on `executor`.

| Attribute | Meaning |
|---|---|
| `model` | the model class |
| `executor` | what runs the operations |
| `query` | the [`Query`](./query) |

Because `query` is a `Query`, a query set stands wherever a query is accepted:

```python
Member.objects.filter(club_id__in=Club.objects.filter(name__startswith="A").values("id"))
```

### Subclasses

A subclass adds methods: client-side methods that build on the building methods, and
[driver methods](./models#adorm-driver-method-func-queryset-only-false). Building methods return an instance of the
subclass, and a copy pickles as one. A manager's `queryset_class` names the subclass (see the
[managers topic](/topics/managers)).

```python
class LoanQuerySet(QuerySet):
    def overdue(self, today):
        return self.filter(due__lt=today)

    @driver_method
    async def archive(self, reason: str = "") -> int: ...


await Loan.objects.overdue(today).order_by("due").archive("late")
```

#### `QuerySet.as_manager(orm_name=None, *, method_manager=None)`

Class method: a [`Manager`](./models#adorm-manager-orm-name-none-method-manager-none) whose `queryset_class` is this
class, `Manager.from_queryset(cls)(orm_name, method_manager=method_manager)`.

```python
class Loan(Model):
    ...
    objects = LoanQuerySet.as_manager("objects")
```

## Building methods

Each method applies the [`Query`](./query) method of the same name and returns a new query set on the same
executor:

| Method | Query method |
|---|---|
| `all()` | a copy |
| `filter(*args, **kwargs)`, `exclude(*args, **kwargs)` | conditions as for `Q` |
| `annotate(**annotations)` | computed columns |
| `order_by(*keys)` | ordering |
| `values(*fields)`, `values_list(*fields, flat=False, named=False)` | rows as dicts, tuples, values or named tuples |
| `only(*fields)`, `defer(*fields)` | loaded fields |
| `select_related(*fields)` | related rows in the same query |
| `distinct(*fields)` | duplicate removal |
| `none()` | no rows |
| `select_for_update(nowait=, skip_locked=, of=, no_key=)` | row locks; reading the rows outside a transaction raises `TransactionManagementError` |
| `qs[start:stop]` | slice; an integer index raises `TypeError` |
| `qs1 \| qs2`, `qs1 & qs2` | OR / AND of the conditions |

```python
recent = (
    Member.objects.filter(active=True)
    .exclude(club__isnull=True)
    .select_related("club")
    .order_by("-joined")
)[:20]
```

Combining query sets of different executors raises `TypeError`.

## Terminal methods

Each terminal method builds one [operation](./operations), awaits `executor.execute(operation)` and shapes the
result. On a query set built with `none()`, `async for`, `get()`, `first()`, `last()`, `count()`, `exists()`,
`update()` and `delete()` return an empty result (`get()` raises `DoesNotExist`) without running an operation;
`create()`, `get_or_create()` and `update_or_create()` run their operation.

A generic `ObjectDoesNotExist` or `MultipleObjectsReturned` from the executor (not a model's own) is raised as the
model's `DoesNotExist` or `MultipleObjectsReturned`, with the original as `__cause__`. A model's own exception, also
another model's, passes unchanged.

### Rows

Rows come back by the query's row format:

| Row format | Set by | Row |
|---|---|---|
| `model` | default | a model instance built with [`from_row()`](./models#from-row-row-executor-none), bound to `executor` |
| `dict` | `values()` | a dict |
| `tuple` | `values_list()` | a tuple in the order of the fields (of the row when no fields are given) |
| `flat` | `values_list(field, flat=True)` | the value |
| `named` | `values_list(named=True)` | a named tuple |

#### `async for row in qs`

The rows of a `Select` operation.

```python
async for member in Member.objects.filter(active=True):
    print(member.name)

names = [n async for n in Member.objects.values_list("name", flat=True)]
```

#### `async get(*args, **kwargs)`

The single row matching the query and the conditions (as for `filter()`), with a `Get` operation. No row raises the
model's `DoesNotExist`, several rows its `MultipleObjectsReturned`.

```python
try:
    member = await Member.objects.get(pk=5)
except Member.DoesNotExist:
    ...
```

#### `async first()`, `async last()`

The first or last row (`First` / `Last`), `None` when there is none. `last()` on a sliced query set raises
`TypeError`, and so does `first()` on a sliced query set without `order_by()`.

#### `async count()`

The number of rows (`Count`), an `int`.

#### `async exists()`

Whether the query has a row (`Exists`), a `bool`.

### Writes

#### `async create(**values)`

A new instance validated from `values` by the model constructor (a relation name takes an instance and sets its
column), inserted with a `Create` operation of the fields given (the primary key only when it is not `None`). The
instance gets the values the target returns (primary key, defaults) and is bound to `executor`. Invalid values raise
pydantic's `ValidationError` before any operation runs.

When the query carries an ORM manager ([`Query.manager`](./query#adorm-query-query-model-manager-none)), the
operation carries it too and the values that are not fields go along, so the target manager's `create()` receives
them; an override of it may take such arguments. Without one, those values stay extra values of the instance.

```python
member = await Member.objects.create(name="Ana", club=club)
member.pk          # set by the database
reader = await Reader.objects.create(name="Ann", username="ann")   # Create(..., manager="objects")
```

#### `async get_or_create(defaults=None, **lookups)`

The instance matching `lookups`, or a new one from the lookups without `__` and `defaults`, with a `GetOrCreate`
operation. Returns `(instance, created)`.

```python
member, created = await Member.objects.get_or_create(name="Ana", defaults={"fee": 5})
```

#### `async update_or_create(defaults=None, create_defaults=None, **lookups)`

Set `defaults` on the instance matching `lookups`, or create one from the lookups without `__` and
`create_defaults` (`defaults` when `create_defaults` is `None`), with an `UpdateOrCreate` operation. Returns
`(instance, created)`; the instance has the row as stored.

```python
member, created = await Member.objects.update_or_create(
    email="ana@example.org",
    defaults={"level": "advanced"},
    create_defaults={"name": "Ana", "level": "beginner"},
)
```

#### `async update(**values)`

Set `values` (field name → value or expression) on the rows with an `Update` operation; returns the number of rows.
No values, a sliced query set, one distinct on fields or a value containing an aggregate raises `TypeError`; a field
name with `__` raises `ValueError`.

```python
await Member.objects.filter(active=False).update(fee=F("fee") * 0)
```

#### `async delete()`

Delete the rows and the rows that cascade from them with a `Delete` operation; returns `(total, {label: count})`. A
sliced query set, one distinct on fields or one after `values()` raises `TypeError`. A reference that forbids the
delete (`on_delete=PROTECT` or `RESTRICT`) raises [`ProtectedError`](./exceptions) or `RestrictedError`; their
`protected_objects` / `restricted_objects` are instances of the registered models, bound to the query set's executor
(a row of a model that is not registered stays a [`ModelRow`](./operations#adorm-operations-modelrow-model-row)).
The refusal comes before any row is deleted, so a transaction that catches it goes on. No model's own `delete()` runs;
[`Model.delete()`](./models#delete) runs it for one instance.

```python
total, per_model = await Member.objects.filter(active=False).delete()

try:
    await Member.objects.filter(pk=member_id).delete()
except adorm.ProtectedError as exc:
    numbers = [invoice.number for invoice in exc.protected_objects]
```
