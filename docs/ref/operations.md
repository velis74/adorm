# Operations

The operation description, importable from `adorm.operations`. An operation is what the core hands a driver
connection: one read or write, described without I/O, models by their label and fields by their adorm names.

```python
from adorm import F, Now, Q
from adorm.operations import Count, Create, Delete, Get, GetOrCreate, Save, Select, Update
from adorm.query import Query
```

`Count` and `Exists` in this module are operations; the aggregate `Count` and the expression `Exists` are
`adorm.Count` and `adorm.Exists`.

Every operation is a frozen dataclass compared by value (`Update(q, {"age": 1}) != Update(q, {"age": True})`) and
hashable. Plain values are copied when the operation is built, so changing the caller's list afterwards does not
change it. Every operation satisfies the driver contract's [`Operation`](./drivers#adorm-drivers-operation)
protocol: `kind` names it in `Capabilities.operations` and `constructs()` names the constructs of its query and
expressions.

| Application code | Operation |
|---|---|
| `async for row in qs`, `await qs` | `Select(query)` |
| `qs.get(**lookups)` | `Get(query.filter(**lookups))` |
| `qs.first()`, `qs.last()` | `First(query)`, `Last(query)` |
| `qs.count()`, `qs.exists()` | `Count(query)`, `Exists(query)` |
| `Model.objects.create(**values)` | `Create(label, values)` |
| `obj.save(update_fields=...)` | `Save(label, pk, values, update_fields)` |
| `qs.update(**values)` | `Update(query, values)` |
| `qs.delete()`, `obj.delete()` | `Delete(query)`, `Delete(Query(label).filter(pk=pk))` |
| `qs.get_or_create(defaults=..., **lookups)` | `GetOrCreate(query, lookups, defaults)` |

`filter()`, `exclude()`, `order_by()`, `distinct()`, slicing, `values()`, `values_list()`, `only()`, `defer()`,
`none()`, `|`, `select_related()` and `select_for_update()` are part of the [`Query`](./query) an operation carries.

## Base class

### `adorm.operations.Operation`

Base class of operations. A subclass sets the class attribute `kind` and is registered in `OPERATIONS` under it;
a second class with a taken kind raises `TypeError`. Every operation has `model`, the label of the model it reads or
writes.

| Member | Meaning |
|---|---|
| `kind` | the operation's name for `Capabilities.operations` |
| `model` | the model label |
| `nodes()` | the query and the expressions of the operation |
| `constructs()` | construct names used by those nodes and their descendants, possibly repeated |
| `to_dict(schema=None)` | JSON-safe data: `{"operation": kind, ...}`, values encoded by the types `schema` gives them |
| `from_dict(data, schema=None)` | class method: the operation `to_dict()` produced with the same `schema` |

```python
operation = Update(Query("club.Member").filter(club__name="Chess"), {"fee": F("fee") * 2})
operation.kind                       # "update"
sorted(set(operation.constructs()))  # ['expression.CombinedExpression', 'lookup.exact']

data = operation.to_dict(schema)     # JSON-safe
assert Operation.from_dict(data, schema) == operation
```

`to_dict()` raises `ValueError` for a value that is not valid for its schema type and `TypeError` for a value
without a schema type whose type cannot be inferred. `from_dict()` raises `ValueError` for malformed data, an unknown
kind, or a kind that is not a subclass of the class it is called on (`Update.from_dict()` of a `select`), and
validates the decoded operation as its constructor does, raising `ValueError` where the constructor raises
`TypeError`. [Operation encoding](./encoding) documents the data and
the typing of values.

### `adorm.operations.OPERATIONS`

A read-only mapping of kind to operation class:

| Kind | Class | Result the driver returns |
|---|---|---|
| `select` | `Select` | a list of rows |
| `get` | `Get` | one row |
| `first` | `First` | a row or `None` |
| `last` | `Last` | a row or `None` |
| `count` | `Count` | an `int` |
| `exists` | `Exists` | a `bool` |
| `create` | `Create` | the row as stored |
| `save` | `Save` | the values the database set |
| `update` | `Update` | the number of rows |
| `delete` | `Delete` | `(total, {label: count})` |
| `get_or_create` | `GetOrCreate` | `(row, created)` |

A row is a dict keyed by adorm field names and annotation names, for every `row_format`: the core builds model
instances, tuples and named tuples from it.

## Reads

### `adorm.operations.Select(query)`

The rows of `query`: iterating, listing or awaiting a query. A query that is not a [`Query`](./query) raises
`TypeError`, as for every operation that takes one.

```python
Select(
    Query("club.Member")
    .filter(Q(name__icontains="ann") | Q(nick__icontains="ann"))
    .select_related("club")
    .order_by("name")[:20]
)
Select(Query("club.Member").values_list("pk", flat=True))
Select(Query("club.Member").filter(active=True) | Query("club.Member").filter(guest=True))
```

### `adorm.operations.Get(query)`

The single row of `query`. The driver raises `ObjectDoesNotExist` when there is none and `MultipleObjectsReturned`
when there is more than one; the core turns them into the model's `DoesNotExist` and `MultipleObjectsReturned`.

```python
Get(Query("club.Member").filter(pk=5))
```

### `adorm.operations.First(query)`

The first row of `query`, or `None`. Without an ordering on the query or the model, rows are ordered by the primary
key.

### `adorm.operations.Last(query)`

The last row of `query`, or `None`: the ordering reversed, or the descending primary key without one. A sliced query
raises `TypeError`.

### `adorm.operations.Count(query)`

The number of rows of `query`, an `int`.

### `adorm.operations.Exists(query)`

Whether `query` has a row, a `bool`.

## Writes

Field values are a mapping or `(name, value)` pairs, kept as a tuple of pairs in the order given. A name is a field
name without `__`; anything else, or a name given twice, raises `ValueError`. A value is a plain value or an
expression; an expression containing an aggregate, or a node that is not an expression, raises `TypeError`.

### `adorm.operations.Create(model, values=())`

Inserts a row of the model `model` (a label) with `values`. An expression value may not refer to a field of the new
row, an `F` outside a subquery or an `OuterRef` of the value's own subquery: `TypeError`. A subquery's own fields are
allowed. Result: the row as stored, including the values the database set (the primary key,
database defaults).

```python
Create("club.Member", {"name": "Ann", "joined": Now()})
```

### `adorm.operations.Save(model, pk, values=(), update_fields=None, force_insert=False, force_update=False)`

Saves an instance: updates the row with primary key `pk`, or inserts a row when `pk` is `None` or no row has it.
`values` holds the instance's loaded and assigned fields; expressions such as `F("age") + 1` are allowed.

- `update_fields`, a subset of the names in `values`, limits the update to those fields and never inserts; an empty
  `update_fields` saves nothing. Duplicates are dropped.
- `force_insert` always inserts, `force_update` never inserts.

Result: the values the database set, at least the primary key. `pk` as an expression raises `TypeError`, and so
does a value that refers to a field of the row when the save inserts (`pk` is `None` or `force_insert`). A name in
`update_fields` without a value, `force_insert` with `force_update` or `update_fields`, and `force_update` without a
`pk` raise `ValueError`.

```python
Save("club.Member", 5, {"name": "Ann", "age": 31}, update_fields=["age"])
Save("club.Member", None, {"name": "New"})                  # insert
Save("club.Member", 5, {"age": F("age") + 1}, force_update=True)
```

### `adorm.operations.Update(query, values=())`

Sets `values` on every row of `query`. Values may refer to fields. A sliced query and a query distinct on fields
raise `TypeError`. Result: the number of rows matched.

```python
Update(Query("club.Member").filter(club__name="Chess"), {"fee": F("fee") * 1.1, "active": True})
```

### `adorm.operations.Delete(query)`

Deletes the rows of `query` and the rows that cascade from them. An instance is deleted with
`Delete(Query(label).filter(pk=pk))`. A sliced query, a query distinct on fields and one after `values()` or
`values_list()` raise `TypeError`. Result: `(total, {label: count})`, the rows deleted per model.

```python
Delete(Query("club.Member").filter(active=False, joined__lt=cutoff))
```

### `adorm.operations.GetOrCreate(query, lookups=(), defaults=())`

The row of `query` matching `lookups`, or a new row when there is none. The new row has the values of the lookups
whose keys have no `__`, updated with `defaults`. A sliced query raises `TypeError`. Result: `(row, created)`.

`lookups` is a mapping or pairs of lookup keys and values, prepared as `filter()` prepares them: `None` stays (it
means `isnull`), an `in` list becomes a tuple without `None`, a query becomes a `Subquery`; an invalid key or value
raises as `filter()` does, a key given twice raises `ValueError`. A lookup on an annotation name of `query` is typed
by the annotation. `defaults` are field values that may not refer to fields of the new row, as for `Create`.

```python
GetOrCreate(
    Query("club.Member").filter(club_id=3),
    {"email": "ann@example.org"},
    defaults={"name": "Ann"},
)
```

## Events

Operations emit and consume no events: they are the requests a slot executes. The slot answers each one with a
[`ResultEvent`](./events#adorm-events-resultevent-request-id-value) carrying the result above, or an
[`ErrorEvent`](./events#adorm-events-errorevent-request-id-error) carrying an adorm exception. A write inside a transaction may lead
to `on_commit` callbacks, reported as [`CallbackEvent`](./events#adorm-events-callbackevent-name-payload-none) when the transaction
commits; signals of the target ORM run on the driver side only.
