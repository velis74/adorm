# QuerySet API

## QuerySet

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

A query set is immutable: a copy and a deep copy are the same object. It pickles when its executor does (a session
does). Building methods return a new query set and do no I/O. Terminal methods are coroutines that run one operation
each on `executor`, then the prefetch operations of [`prefetch_related()`](#prefetch-related-lookups).

| Attribute | Meaning |
|---|---|
| `model` | the model class |
| `executor` | what runs the operations |
| `query` | the [`Query`](./query) |
| `prefetch_lookups` | the [`Prefetch`](#adorm-prefetch-lookup-queryset-none-to-attr-none) lookups of `prefetch_related()`, a tuple |

A query set is accepted wherever a query is:

```python
Member.objects.filter(club_id__in=Club.objects.filter(name__startswith="A").values("id"))
```

### Subclasses

A subclass adds client-side methods built on the building methods, and
[driver methods](./models#adorm-driver-method-func-queryset-only-false). Building methods return an instance of the
subclass, and a copy pickles as one. A manager's `queryset_class` names the subclass
([managers topic](/topics/managers)).

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

Each method applies the [`Query`](./query) method of the same name and returns a new query set on the same executor.
Combining query sets of different executors raises `TypeError`.

| Method | Query method |
|---|---|
| `all()` | a copy |
| `filter(*args, **kwargs)`, `exclude(*args, **kwargs)` | conditions as for `Q` |
| `annotate(**annotations)` | computed columns; after `values()`, an aggregate groups the rows |
| `alias(**annotations)` | named expressions that are not returned |
| `order_by(*keys)` | ordering |
| `values(*fields)`, `values_list(*fields, flat=False, named=False)` | rows as dicts, tuples, values or named tuples |
| `only(*fields)`, `defer(*fields)` | loaded fields |
| `select_related(*fields)` | related rows in the same query |
| `distinct(*fields)` | duplicate removal |
| `none()` | no rows |
| `select_for_update(nowait=, skip_locked=, of=, no_key=)` | row locks; reading the rows outside a transaction raises `TransactionManagementError` |
| `qs[start:stop]` | slice; an integer index raises `TypeError` |
| `qs1 \| qs2`, `qs1 & qs2` | OR / AND of the conditions |
| `union(*others, all=False)`, `intersection(*others)`, `difference(*others)` | [set operations](#set-operations) |

```python
recent = (
    Member.objects.filter(active=True)
    .exclude(club__isnull=True)
    .select_related("club")
    .order_by("-joined")
)[:20]
```

### Set operations

`union(*others, all=False)`, `intersection(*others)` and `difference(*others)` apply the
[`Query` set operations](./query#union-others-all-false) to the query of the query set and of each of `others`.

- `others` are query sets on the same executor; anything else raises `TypeError`.
- The result is a query set of the first query set's class with the rows of the combined query, in the row format of
  the first query.
- Where the `Query` method returns one of its operands, the method returns that operand's query set, which may be of
  another class: `union()` on an empty query set (`none()`, or a slice that leaves no rows) with one non-empty query
  set returns that query set; `intersection()` with an empty query set among `others` returns the empty one.
- Empty query sets combine by the [rules for empty queries](./query#empty-queries).

```python
advanced = Member.objects.filter(level="advanced")
coaches = Member.objects.filter(tags__contains=["coach"])

[m.first_name async for m in advanced.intersection(coaches)]
await advanced.union(coaches, all=True).count()
[n async for n in advanced.difference(coaches).values_list("first_name", flat=True).order_by("first_name")]
await advanced.union(coaches).order_by("first_name").first()
```

After a set operation, as in Django:

- The building methods `order_by()`, slicing, `values()`, `values_list()`, `none()` and the set operations apply.
- The terminal methods `async for`, `get()` without conditions, `first()`, `last()`, `count()` and `exists()` apply.
- The other building methods, `prefetch_related()`, `get()` with conditions, `aggregate()`, `in_bulk()`, `update()`,
  `delete()`, `get_or_create()` and `update_or_create()` raise `NotSupportedError` without running an operation.
- `first()` and `last()` without `order_by()` order the combined rows by the primary key. Rows without the primary
  key column (`values()` / `values_list()` without it) make the driver raise `DatabaseError` (`ORDER BY term does not
  match any column in the result set.`).

### `prefetch_related(*lookups)`

Loads the related objects of the relation paths `lookups` for every instance the query set returns, with separate
operations after the query's own ([`prefetch_related_objects()`](#adorm-prefetch-related-objects-instances-lookups-executor-none)
runs them). A lookup is a relation path (`"memberships__member"`) or a [`Prefetch`](#adorm-prefetch-lookup-queryset-none-to-attr-none).

- The lookups are kept in `qs.prefetch_lookups`. They add up over calls; a path given again is kept once.
- Every building method keeps them; `prefetch_related(None)` clears them.
- `async for`, `get()`, `first()`, `last()` and [`in_bulk()`](#async-in-bulk-id-list-none-field-name-pk) run the
  prefetches on the instances they return. No instances, no prefetch operation.
- Each prefetch operation has its own [`models`](./operations#models) and passes the profile's
  [`check_operation`](/topics/profiles#refusing-operations) hook on its own; a refused one makes the call raise.

```python
clubs = Club.objects.order_by("slug").prefetch_related("memberships__member", "activities")
async for club in clubs:            # 4 Select operations: clubs, memberships, members, activities
    members = [(await m.member).first_name async for m in club.memberships]   # no operation
```

Errors:

- `ValueError`: a `Prefetch` with a query set or `to_attr` whose path (with `to_attr`) is already given.
- `TypeError`: `prefetch_related()` after `values()` or `values_list()`, and `values()` or `values_list()` after it.
- `NotSupportedError`: `prefetch_related()` after a set operation.

### `adorm.Prefetch(lookup, queryset=None, to_attr=None)`

A prefetch lookup: the relation path `lookup`, with the related rows of its last relation read from `queryset` and
stored in `to_attr`.

- `queryset` is a `QuerySet` (or a [`Query`](./query) on a registered model) on the last relation's target model.
  It filters, orders and selects the related rows (`only()`, `defer()`, `select_related()`); the join column is
  loaded even when `only()` leaves it out. Its own `prefetch_related()` lookups continue from the rows it returns.
  Its operations run on the executor of the instances prefetched for, not on the query set's. A query set with
  `values()` or a slice raises `ValueError`; one on another model raises `ValueError` when the prefetch runs.
- `to_attr` stores the related rows of the last relation as an extra value of each instance: a list for a
  `ReverseForeignKey` or `ManyToMany`, an instance or `None` otherwise. The relation's cache is not set. A later
  path may continue from it (`"attended__member"`). A `to_attr` that names an attribute of the model raises
  `ValueError` when the prefetch runs.
- `prefetch_through` is `lookup`; `prefetch_to` is `lookup` with `to_attr` in place of its last segment. Two prefetches
  are equal when their `prefetch_to` is.

```python
attended = Attendance.objects.filter(status="attended").order_by("member_id")
activities = Activity.objects.prefetch_related(
    Prefetch("attendances", queryset=attended, to_attr="attended"),
    "attended__member",
)
async for activity in activities:
    names = [(await a.member).first_name for a in activity.attended]
```

### `adorm.GenericPrefetch(lookup, querysets, to_attr=None)`

A prefetch lookup whose last relation is a [`GenericForeignKey`](./models#adorm-genericforeignkey-ct-field-content-type-fk-field-object-id-for-concrete-model-true),
with a query set per related model. The related rows of a content type are read from the query set on its model;
models without one give their plain rows. `ValueError` for two query sets on the same model, for a plain `Prefetch`
with a query set on a generic foreign key, and for a `GenericPrefetch` on another relation.

```python
lines = InvoiceLine.objects.prefetch_related(
    GenericPrefetch("item", [Membership.objects.filter(active=True)], to_attr="billed")
)
```

### `adorm.prefetch_related_objects(instances, *lookups, executor=None)`

Coroutine: loads the related objects of `lookups` (relation paths or `Prefetch` objects) for `instances`, model
instances of one or more models, and cache them on the instances.

| Relation | Operations | Cached |
|---|---|---|
| `ForeignKey` | `Select` of the targets by `pk__in` (without the target's manager) | the relation's cache |
| `ReverseOneToOne` | `Select` of the targets by `<column>__in` | the relation's cache; `None` for no row |
| `ReverseForeignKey` | `Select` from the target's `objects` manager by `<column>__in` | the rows, which the relation's query set reads; each row gets the owner in its foreign key's cache |
| `ManyToMany` | `Select` of the links (`values(source_column, target_column)`), then of the targets from the `objects` manager by `pk__in` | the rows |
| `GenericForeignKey` | the content types [`content_types`](./models#adorm-contenttypes) has not cached (a `Get` for one, a `Select` for several), then one `Select` per model by `pk__in` | the relation's cache; `None` for a missing row |

- Each relation on a path is read once for all the instances of its level. The rows of a generic foreign key, of
  several models, continue on each model's relation of the next segment.
- An instance whose relation is already cached (by `select_related()` or an earlier prefetch), or that has the
  prefetch's `to_attr`, is skipped.
- The operations run one after another on `executor`, by default the first instance's. Inside a transaction they
  use its slot and see its writes; outside one each operation takes a slot and returns it.
- Each operation filters on at most [`BATCH_SIZE`](#adorm-models-prefetch-batch-size) keys; more keys run the
  operations of a relation once per batch. For a many-to-many relation a batch is the targets of a group of owners
  with at most `BATCH_SIZE` targets in all, in the order of the query set; an owner with more gets them in that
  order per batch.
- A cancelled prefetch caches nothing for the relation it was reading.
- A generic foreign key's object is cached for the content type and object id read before its operations ran.

```python
members = [m async for m in Member.objects.filter(level="advanced")]
await adorm.prefetch_related_objects(members, "memberships__club")
```

A segment that names nothing on a model raises `AttributeError`; one that names anything but a relation raises
`ValueError`.

### `adorm.models.prefetch.BATCH_SIZE`

`10_000`: the most keys one `Select` of a prefetch filters on. It is within the parameter limit of a statement on
every driver.

## Terminal methods

Each terminal method builds one [operation](./operations), awaits `executor.execute(operation)` and shapes the
result. On a query set built with `none()`:

- `async for`, `get()`, `first()`, `last()`, `count()`, `exists()`, `in_bulk()` (`{}`), `update()` and `delete()`
  return an empty result without running an operation; `get()` raises `DoesNotExist`.
- `aggregate()` runs no operation when the result over no rows is known without the database (see
  [`aggregate()`](#async-aggregate-args-kwargs)).
- `create()`, `bulk_create()`, `get_or_create()` and `update_or_create()` run their operation.

A generic `ObjectDoesNotExist` or `MultipleObjectsReturned` from the executor is raised as the model's
`DoesNotExist` or `MultipleObjectsReturned`, with the original as `__cause__`. A model's own exception, of this or
another model, passes unchanged.

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

The rows of a `Select` operation, then the prefetches of `prefetch_related()`; the query set of a relation that
holds prefetched rows yields them without an operation.

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
`TypeError`, and so does `first()` on a sliced query set without `order_by()`. On a grouped query set
(`values()` followed by an aggregate annotation) both need `order_by()`, otherwise `TypeError`.

```python
newest = await Member.objects.order_by("joined").last()
```

#### `async count()`

The number of rows (`Count`), an `int`.

```python
active = await Membership.objects.filter(club_id=club.id, active=True).count()
```

#### `async exists()`

Whether the query has a row (`Exists`), a `bool`.

```python
if not await Invoice.objects.filter(pk=pk, status="open").exists():
    raise LookupError(pk)
```

#### `async in_bulk(id_list=None, *, field_name="pk")`

The instances whose field `field_name` holds one of `id_list` (every instance for `None`), as a dict keyed by that
field's value, with one [`InBulk`](./operations#adorm-operations-inbulk-query-field-name-pk-ids-none) operation, then
the prefetches of `prefetch_related()`.

- `field_name` is `"pk"`, a field that is unique on its own, or the only field of `distinct()`; for another field
  the driver raises `ProgrammingError`.
- The name of a unique [`ForeignKey`](./models#adorm-foreignkey-target-column-none) (a one-to-one relation) stands
  for its column; the dict is keyed by the related primary key.
- An empty `id_list` returns `{}` without an operation.

```python
clubs = await Club.objects.in_bulk([1, 2, 99])                     # {1: <Club>, 2: <Club>}
by_slug = await Club.objects.in_bulk(["koper-sailing"], field_name="slug")
by_activity = await Competition.objects.in_bulk([3], field_name="activity_ptr")  # field_name="activity_ptr_id"
```

A sliced query set, one after `values()` and a field the query set defers raise `TypeError`; outside a transaction a
query set with `select_for_update()` raises `TransactionManagementError`.

#### `async aggregate(*args, **kwargs)`

The values of aggregate expressions over the rows, as a dict by name, with one
[`Aggregate`](./operations#adorm-operations-aggregate-query-aggregations) operation.

- A keyword argument names its expression. A positional argument is an aggregate of one field named by its
  [`default_alias`](./expressions#default-alias) (`Sum("fee")` → `"fee__sum"`).
- An expression may combine aggregates (`Max("fee") - Min("fee")`) and refer to annotations and aliases.
- Without arguments the result is `{}` and no operation runs.
- `TypeError`: a positional argument that is not an aggregate or has no default name (`Count("*")`,
  `Sum(F("a") * 2)`); a query set distinct on fields (`distinct("role")`).
- `ValueError`: a name given twice, positional or keyword.

```python
await Membership.objects.aggregate(Sum("fee"), rows=Count("*"))
# {"fee__sum": Decimal("1102.00"), "rows": 13}

await Membership.objects.filter(active=True).aggregate(
    coach_fees=Sum("fee", filter=Q(role="coach"), default=0),
    spread=Max("fee") - Min("fee"),
)
```

Over a sliced, distinct (without fields) or grouped query set, or one whose annotations aggregate, the expressions
aggregate the query set's rows:

```python
await Membership.objects.order_by("-fee")[:3].aggregate(Sum("fee"))          # the three highest fees
await Club.objects.annotate(headcount=Count("memberships")).aggregate(Avg("headcount"))
await Membership.objects.values("club_id").annotate(n=Count("*")).aggregate(largest=Max("n"))
```

Over no rows `Count` gives `0` and the other aggregates their `default`, or `None`. On a query set built with
`none()` (or a slice that leaves no rows), the result is computed without an operation when every expression is a
plain aggregate with no `default` or a [`Value`](./expressions#adorm-value-value-output-type-none) `default`.

### Writes

#### `async create(**values)`

A new instance validated from `values` by the model constructor, inserted with a `Create` operation of the fields
given.

- A relation name takes an instance and sets its column.
- The primary key is inserted only when it is not `None`.
- The instance gets the values the target returns (primary key, defaults) and is bound to `executor`.
- Invalid values raise pydantic's `ValidationError` before any operation runs.
- A [generic foreign key](./models#generic-assignment) keyword whose content type is not cached has it read on
  `executor` first.
- When the query carries an ORM manager ([`Query.manager`](./query#adorm-query-query-model-manager-none)), the
  operation carries it, and values that are not fields are passed to the target manager's `create()`, whose
  override may take them. On the native PostgreSQL driver, a manager or query set that overrides `create()` raises
  `NotSupportedError`. Without an ORM manager, such values stay extra values of the instance.

```python
member = await Member.objects.create(name="Ana", club=club)
member.pk          # set by the database
line = await InvoiceLine.objects.create(invoice_id=1, item=membership, description="Fee", unit_price=120)
reader = await Reader.objects.create(name="Ann", username="ann")   # Create(..., manager="objects")
```

#### `async bulk_create(objs, batch_size=None, ignore_conflicts=False, update_conflicts=False, update_fields=None, unique_fields=None)`

Inserts the model instances `objs` (any iterable) with one
[`BulkCreate`](./operations#adorm-operations-bulkcreate-model-rows-batch-size-none-ignore-conflicts-false-update-conflicts-false-update-fields-none-unique-fields-none-manager-none)
operation and returns them as a list, in order.

- Each instance writes its loaded and assigned fields (`model_fields_set`); a primary key of `None` is left to the
  target. Instances may set different fields; the rest take the target's defaults.
- After the operation each instance has the values the target set, such as a generated primary key, and is bound
  to `executor`. A failing operation leaves the instances unchanged.
- It calls no `save()` and sends no signals, as Django's `bulk_create()`.
- The content types that [generic foreign key assignments](./models#generic-assignment) left unset are read on
  `executor` first.
- `batch_size`: the most rows one statement inserts. The statements of one call run in one transaction, or in the
  open transaction without a savepoint of their own: a failing row inserts none of the rows.
- `ignore_conflicts=True`: a row that violates a unique constraint is skipped. The database returns no primary keys,
  so a generated one stays `None`, also for the inserted rows.
- `update_conflicts=True`: a row that conflicts on `unique_fields` updates `update_fields` of the stored row, and an
  instance without a primary key gets that row's.
- When the query carries an ORM manager ([`Query.manager`](./query#adorm-query-query-model-manager-none)), that
  manager's `bulk_create()` inserts the rows. An override runs on the Django driver; the native PostgreSQL driver
  raises `NotSupportedError`.

`TypeError` for an object that is not an instance of the model and `ValueError` for a `batch_size` that is not a
positive integer, also for an empty `objs`. The other argument combinations `BulkCreate` refuses raise `ValueError`
when there are instances, as in Django. With no instances it returns `[]` without an operation and without the
driver's checks, also for a multi-table inheritance child, which Django refuses.

```python
tags = await Tag.objects.bulk_create([Tag(name="night"), Tag(name="dawn")])
[tag.pk for tag in tags]                                   # [7, 8]

await Tag.objects.bulk_create(new_tags, batch_size=500)   # 500 rows per INSERT, all in one transaction

await Tag.objects.bulk_create([Tag(name="indoor"), Tag(name="night")], ignore_conflicts=True)

members = await Member.objects.bulk_create(
    [
        Member(email="ana@example.com", level="advanced", fee=40, club_id=1),
        Member(email="new@example.com", fee=40, club_id=1),
    ],
    update_conflicts=True,
    update_fields=["level"],
    unique_fields=["email"],
)
```

#### `async create_from_parent(parent, **values)`

A new instance of a multi-table inheritance child for an existing row of its parent, with a
[`CreateFromParent`](./operations#adorm-operations-createfromparent-model-parent-pk-values) operation: only the
child's own table gets a row; the parent's row is not written (Django: `Child(parent_ptr=parent, ...).save_base(raw=True)`).

- `parent` is an instance of the parent model (the target of the
  [parent link](./models#adorm-models-parent-link-model), the `ForeignKey` stored in the primary key field) or its
  primary key.
- `values` are the child's own fields, validated with their field types; a `ForeignKey` of the child stands for its
  column. Fields not given take the target's defaults.
- The instance has the child's row as stored, the parent's fields included, and is bound to `executor`.

```python
crag = await Activity.objects.get(name="Crag day")
competition = await Competition.objects.create_from_parent(crag, discipline="Climbing")
competition.name, competition.capacity     # ("Crag day", 15), the parent's values
```

Errors:

- `TypeError`: a model that is not a multi-table inheritance child; a parent of another model.
- `ValueError`: a parent without a primary key value; a value of an inherited or unknown field, or one its type
  rejects.
- The parent model's `DoesNotExist`: the parent has no row.
- `IntegrityError`: the child already has a row.

#### `async get_or_create(defaults=None, **lookups)`

The instance matching `lookups`, or a new one from the lookups without `__` and `defaults`, with a `GetOrCreate`
operation. Returns `(instance, created)`. A [generic foreign key](./models#generic-assignment) in `defaults` takes a
stored instance and stands for both of its fields; its content type id is taken on the query set's executor first.

```python
member, created = await Member.objects.get_or_create(name="Ana", defaults={"fee": 5})
```

#### `async update_or_create(defaults=None, create_defaults=None, **lookups)`

Sets `defaults` on the instance matching `lookups`, or creates one from the lookups without `__` and
`create_defaults` (`defaults` when `create_defaults` is `None`), with an `UpdateOrCreate` operation. Returns
`(instance, created)`; the instance has the row as stored. A [generic foreign key](./models#generic-assignment) in
`defaults` or `create_defaults` takes a stored instance, as for `get_or_create()`.

```python
member, created = await Member.objects.update_or_create(
    email="ana@example.org",
    defaults={"level": "advanced"},
    create_defaults={"name": "Ana", "level": "beginner"},
)
```

#### `async update(**values)`

Sets `values` (field name → value or expression) on the rows with an `Update` operation; returns the number of rows.
No values, a sliced query set, one distinct on fields or a value containing an aggregate raises `TypeError`; a field
name with `__` raises `ValueError`.

```python
await Member.objects.filter(active=False).update(fee=F("fee") * 0)
```

#### `async delete()`

Deletes the rows and the rows that cascade from them with a `Delete` operation; returns `(total, {label: count})`.

- A sliced query set, one distinct on fields or one after `values()` raises `TypeError`.
- A reference with `on_delete=PROTECT` or `RESTRICT` raises [`ProtectedError`](./exceptions) or `RestrictedError`
  before any row is deleted; a transaction that catches it stays usable. `protected_objects` /
  `restricted_objects` are instances of the registered models, bound to the query set's executor; a row of a model
  that is not registered stays a [`ModelRow`](./operations#adorm-operations-modelrow-model-row).
- No model's own `delete()` runs; [`Model.delete()`](./models#delete) runs it for one instance.

```python
total, per_model = await Member.objects.filter(active=False).delete()

try:
    await Member.objects.filter(pk=member_id).delete()
except adorm.ProtectedError as exc:
    numbers = [invoice.number for invoice in exc.protected_objects]
```
