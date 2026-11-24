# Operations

The operation description, importable from `adorm.operations`. An operation is one read or write that the core
hands a driver connection, described without I/O: models by their label, fields by their adorm names.

```python
from adorm import F, Now, Q
from adorm.models import registry
from adorm.operations import Aggregate, BulkCreate, Call, CheckTable, Count, Create, CreateFromParent, Delete, FullClean
from adorm.operations import Get, GetOrCreate, InBulk, ModelRow, Save, Select, Update, UpdateOrCreate
from adorm.query import Query
```

`Aggregate`, `Count` and `Exists` in this module are operations; the aggregate base class, the aggregate `Count` and
the expression `Exists` are `adorm.Aggregate`, `adorm.Count` and `adorm.Exists`.

Every operation is a hashable frozen dataclass compared by value
(`Update(q, {"age": 1}) != Update(q, {"age": True})`). Plain values are copied when the operation is built; later
changes to the caller's objects do not affect it. Every operation satisfies the driver contract's
[`Operation`](./drivers#adorm-drivers-operation) protocol: `kind` names it in `Capabilities.operations`,
`constructs()` names the constructs of its query and expressions.

| Application code | Operation |
|---|---|
| `async for row in qs`, `await qs` | `Select(query)` |
| `qs.get(**lookups)` | `Get(query.filter(**lookups))` |
| `qs.first()`, `qs.last()` | `First(query)`, `Last(query)` |
| `qs.count()`, `qs.exists()` | `Count(query)`, `Exists(query)` |
| `qs.aggregate(*args, **kwargs)` | `Aggregate(query, aggregations)` |
| `qs.in_bulk(id_list, field_name=...)` | `InBulk(query, field_name, ids)` |
| `qs.prefetch_related(...)` | no operation of its own: a sequence of `Select` operations after the query's own, one per relation, level and batch of keys, each with a slot of its own outside a transaction |
| `Model.objects.create(**values)` | `Create(label, values)`, `Create(label, values, manager)` on a manager with an ORM name |
| `Model.objects.bulk_create(objs, batch_size=..., ...)` | `BulkCreate(label, rows, batch_size, ...)`, one row per instance |
| `obj.save(update_fields=...)` | `Save(label, pk, values, update_fields)` |
| `qs.update(**values)` | `Update(query, values)` |
| `qs.delete()`, `obj.delete()` | `Delete(query)`, `DeleteInstance(label, pk)` |
| `qs.get_or_create(defaults=..., **lookups)` | `GetOrCreate(query, lookups, defaults)` |
| `qs.update_or_create(defaults=..., create_defaults=..., **lookups)` | `UpdateOrCreate(query, lookups, defaults, create_defaults)` |
| `Child.objects.create_from_parent(parent, **values)` | `CreateFromParent(label, parent_pk, values)` |
| `obj.full_clean(exclude=...)` | `FullClean(label, pk, values, exclude, validate_unique, validate_constraints, adding)` |
| [`adorm.check_schema(models)`](./checks) | `CheckTable(label, fields, many_to_many)`, one per model |
| `qs.archive(...)`, `Model.objects.lend(...)` (driver methods) | `Call(query, "archive", "queryset", ...)`, `Call(Query(label, manager=...), "lend", "manager", ...)` |

`filter()`, `exclude()`, `annotate()`, `alias()`, `order_by()`, `distinct()`, slicing, `values()`, `values_list()`,
`only()`, `defer()`, `none()`, `|`, `select_related()` and `select_for_update()` are part of the [`Query`](./query)
an operation carries.

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
| `models` | the labels of the models whose tables the operation reads or writes, `None` when adorm cannot know them ([below](#models)) |
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
`TypeError`. Both raise [`FieldError`](./exceptions#adorm-fielderror-message-none-model-none-key-none-original-none-sqlstate-none)
for a field path the schema does not resolve. [Operation encoding](./encoding) documents the data and
the typing of values.

#### `models`

The labels (`app_label.ModelName`, as [`ModelMeta.label`](./models#adorm-modelmeta-label-pk-id-schema-hash-none-orm-names-none-concrete-model-none-delete-models-none);
`"app.Model_field"` for an automatic many-to-many through model) of the models whose tables the operation reads or
writes, as a `frozenset`, by the model graph: a model counts when the operation can reach its table, whatever rows
there are. `None` when adorm cannot know them. A profile's
[`check_operation`](/topics/profiles#refusing-operations) hook reads it.

It is computed from the process's [model registry](./models#adorm-models-registry) on first access and cached; it
does no I/O and does not raise. The base class gives `None`, as do operations of third-party drivers; a subclass of a
bundled operation inherits that class's computation, and a subclass that touches other tables overrides `models`.

| Operation | `models` |
|---|---|
| `Select`, `Get`, `First`, `Last`, `Count`, `Exists`, `InBulk` | the reach of the query |
| `Aggregate` | the reach of the query and of the aggregate expressions |
| `Update` | the reach of the query and of the written values |
| `Create`, `BulkCreate`, `Save`, `CreateFromParent` | the model and the reach of the written values; a child's parents (for `CreateFromParent`, the parent whose row it extends) |
| `GetOrCreate`, `UpdateOrCreate` | the reach of the query, the lookups and the defaults |
| `FullClean` | the model, and the target of each of its foreign keys (`ForeignKey.validate` reads it) |
| `CheckTable` | the model and the `through` model of each relation named in `many_to_many` |
| `Delete` | the reach of the query and the model's [`ModelMeta.delete_models`](./models#adorm-modelmeta-label-pk-id-schema-hash-none-orm-names-none-concrete-model-none-delete-models-none); `None` when that is `None` |
| `DeleteInstance` | the model and its `ModelMeta.delete_models`; `None` when that is `None` |
| `Call` | `None`: a driver method runs code of the target ORM |

The reach of a query or an expression:

- the model of every query: the operation's own, each `Subquery`, `Exists`, `pk__in=queryset` and each part of a
  `union()`, `intersection()` or `difference()`;
- every model a relation path reaches: lookups and `F()` references in filters, annotations, ordering, aggregates and
  written values (an `OuterRef()` path from the query it refers to), and the paths of `values()`, `values_list()`,
  `only()`, `defer()`, `distinct()`, grouping, `select_for_update(of=...)` and `select_related()`;
- a many-to-many relation on a path adds its `through` model as well as its target, from either side;
- `select_related()` without fields adds the targets of the non-null foreign keys, five relations deep, as Django
  follows them;
- every listed model adds its concrete tables: a proxy model its concrete model, a multi-table inheritance child its
  parents, recursively.

A path segment that is not a relation (a field, a transform, a JSON key, an annotation) ends the path; a relation to a
model that is not registered adds its label and ends the path. A path the registry cannot resolve is left to the
driver, which raises `FieldError`.

```python
Select(Member.objects.filter(clubs__name="Chess").query).models
# frozenset({'clubs.Member', 'clubs.Membership', 'clubs.Club'})
Select(Edition.objects.select_related("book_ptr").query).models
# frozenset({'library.Edition', 'library.Book'})
Delete(Tag.objects.filter(name="old").query).models
# frozenset({'library.Tag', 'library.Tagging'})
```

Each prefetch query of `prefetch_related()` is an operation of its own with its own `models` (the link query of a
many-to-many prefetch reads its `through` model only), and so is each load of a related object, a generic foreign key
or a content type.

`models` describes what adorm's handling of the operation touches. Code of the application that the target ORM runs
during it is not part of it: on the Django driver, an overridden `save()`, `delete()` or `clean()`, signal receivers,
custom manager methods, field validators, `limit_choices_to` and `on_delete` callables other than Django's (`SET()` of
a callable included). The native PostgreSQL driver refuses most of these, or leaves their work to the application
(`covered_receivers`).

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
| `aggregate` | `Aggregate` | a dict of the values by name |
| `create` | `Create` | the row as stored |
| `bulk_create` | `BulkCreate` | a list of rows, one per row of the operation, in order |
| `save` | `Save` | the values the database set |
| `update` | `Update` | the number of rows |
| `delete` | `Delete` | `(total, {label: count})` |
| `delete_instance` | `DeleteInstance` | `(total, {label: count})` |
| `get_or_create` | `GetOrCreate` | `(row, created)` |
| `update_or_create` | `UpdateOrCreate` | `(row, created)` |
| `check_table` | `CheckTable` | a list of problem dicts |

A row is a dict keyed by adorm field names and annotation names, for every `row_format`. The core builds model
instances, tuples and named tuples from it.

## Reads

### `adorm.operations.Select(query)`

The rows of `query`: iterating, listing or awaiting a query. A query that is not a [`Query`](./query) raises
`TypeError`, as for every operation that takes one.

A query combined by a [set operation](./query#union-others-all-false) is accepted by `Select`, `Get`, `First`,
`Last`, `Count` and `Exists`. `InBulk`, `Aggregate`, `Update`, `Delete`, `GetOrCreate` and `UpdateOrCreate` raise
[`NotSupportedError`](./exceptions) for one (`cannot call update() after union()`).

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

```python
advanced = Query("clubs.Member").filter(level="advanced").values_list("first_name", flat=True)
Select(advanced.union(Query("clubs.Club").values_list("city", flat=True)).order_by("first_name"))
Count(advanced.difference(Query("clubs.Member").filter(tags__contains=["board"]).values_list("first_name", flat=True)))
```

### `adorm.operations.Get(query)`

The single row of `query`. The driver raises `ObjectDoesNotExist` when there is none and `MultipleObjectsReturned`
when there is more than one; the core turns them into the model's `DoesNotExist` and `MultipleObjectsReturned`.

```python
Get(Query("club.Member").filter(pk=5))
```

### `adorm.operations.First(query)`

The first row of `query`, or `None`. Without an ordering on the query or the model, rows are ordered by the primary
key. `TypeError` is raised for:

- a sliced query without an ordering of its own; the model's ordering does not count, as an adorm model does not
  carry it;
- a grouped query (`Query.is_grouped`) without an ordering, as ordering by the primary key would split its groups.

### `adorm.operations.Last(query)`

The last row of `query`, or `None`: the ordering reversed, or the descending primary key without one. A sliced query,
and a grouped query without an ordering, raise `TypeError`.

### `adorm.operations.Count(query)`

The number of rows of `query`, an `int`.

### `adorm.operations.Exists(query)`

Whether `query` has a row, a `bool`.

### `adorm.operations.InBulk(query, field_name="pk", ids=None)`

The rows of `query` whose field `field_name` holds one of `ids`, every row when `ids` is `None`; a list of rows, which
[`in_bulk()`](./queryset#async-in-bulk-id-list-none-field-name-pk) keys by the field's value. `select_query()` is
`query.filter(<field_name>__in=ids)` (`query` itself for `ids=None`), so with ids the operation also reports
`lookup.in`.

- `field_name` is `"pk"` or a field name without `__` (`ValueError`). The driver raises `ProgrammingError` for a
  field that is not the primary key, not unique on its own (`unique=True` or a single-field unique constraint
  without a condition) and not the only field the query is distinct on.
- `ids` is an iterable of plain values, not a string (`TypeError`), kept in order without repeats.
- A sliced query, one after `values()` and one that defers the field raise `TypeError`.
- The encoded form has `"field_name"` and `"ids"` (`null` or the values, typed as the field).

```python
InBulk(Query("clubs.Club"), ids=[1, 2])
InBulk(Query("clubs.Club"), "slug", ["koper-sailing"])
```

### `adorm.operations.Aggregate(query, aggregations)`

The values of `aggregations` over the rows of `query`, a dict by name.

- `aggregations` is a mapping or `(name, expression)` pairs, at least one (else `TypeError`), kept as a tuple of
  pairs in the order given.
- A name is a non-empty string without whitespace, control characters, quotes, backticks, brackets, `;`, `#`, `--`,
  `/*` or `*/`; it may contain `__` (`"fee__sum"`). Anything else, or a name given twice, raises `ValueError`.
- The expressions refer to the query's fields, annotations and aliases. An expression without an aggregate outside a
  subquery raises `TypeError`, and so does a query distinct on fields (`distinct("role")`).

Over a sliced, distinct (without fields) or grouped query, or one with an aggregating annotation, the expressions
aggregate the query's rows as a subquery. Over no rows `Count` gives `0`, the other aggregates their `default` or
`None`.

```python
Aggregate(Query("clubs.Membership").filter(active=True), {"fees": Sum("fee"), "rows": Count("*")})
Aggregate(Query("clubs.Membership").values("club_id").annotate(n=Count("*")), {"largest": Max("n")})
```

The encoded form has `"aggregations"`, a list of `[name, expression]` pairs; values in them are typed by the query's
fields and annotations.

## Writes

Field values are a mapping or `(name, value)` pairs, kept as a tuple of pairs in the order given. A name is a field
name without `__`; anything else, or a name given twice, raises `ValueError`. A value is a plain value or an
expression; an expression containing an aggregate, or a node that is not an expression, raises `TypeError`.

### `adorm.operations.Create(model, values=(), manager=None)`

Inserts a row of the model `model` (a label) with `values`. An expression value that refers to a field of the new
row (an `F` outside a subquery, an `OuterRef` of the value's own subquery) raises `TypeError`; a subquery's own fields
are allowed. Result: the row as stored, including the values the database set (primary key, database defaults).

`manager` names the target ORM's manager whose `create()` inserts the row, as
[`Query.manager`](./query#adorm-query-query-model-manager-none). With a manager:

- the operation uses the construct `clause.manager`;
- `values` may name arguments that are not fields; the encoding types them by their values;
- a value that is a model instance raises `TypeError` (pass its primary key).

The encoded form has `"manager"` only when there is one.

```python
Create("club.Member", {"name": "Ann", "joined": Now()})
Create("library.Reader", {"name": "Ann", "username": "ann"}, manager="objects")
```

### `adorm.operations.BulkCreate(model, rows=(), batch_size=None, ignore_conflicts=False, update_conflicts=False, update_fields=None, unique_fields=None, manager=None)`

Inserts a row of the model `model` (a label) for each item of `rows`, as the target inserts several instances at
once (Django's `bulk_create()`): no `save()`, no signals. Result: a list with one row per item of `rows`, in order,
each with its values and those the database set, such as a generated primary key. Under `ignore_conflicts` the
database returns nothing, and a generated primary key stays `None`.

- `rows` is an iterable of mappings or `(name, value)` pairs. Rows may name different fields; a field a row does not
  name gets the target's default. Values follow the rules of `Create`: an expression value that refers to a field
  of the row or contains an aggregate raises `TypeError`.
- `batch_size`, `None` or a positive `int` (`ValueError` otherwise, `bool` included), is the most rows one statement
  inserts. A driver may insert fewer per statement (the native PostgreSQL driver keeps every statement within
  asyncpg's parameter limit). Several statements run in one transaction, or inside an open one without a savepoint
  of their own; a failing row inserts none of the rows.
- `ignore_conflicts` skips a row that violates a unique constraint; construct `clause.on_conflict.ignore`.
- `update_conflicts` updates `update_fields` of the stored row a row conflicts with on `unique_fields`; construct
  `clause.on_conflict.update`. `unique_fields` may name `"pk"`.
- `manager` names the target ORM's manager whose `bulk_create()` inserts the rows, as
  [`Query.manager`](./query#adorm-query-query-model-manager-none); construct `clause.manager`. The encoded form has
  `"manager"` only when there is one.

`ignore_conflicts` with `update_conflicts`, and `update_conflicts` without `update_fields`, raise `ValueError`.
`update_fields` and `unique_fields` are iterables of field names without `__` (a string raises `TypeError`); repeats
are dropped. Without `update_conflicts` they are checked as names and then set to `None`, as Django's
`bulk_create()` ignores them.

The SQL drivers raise `ProgrammingError` for what Django's `bulk_create()` refuses on PostgreSQL
([`check_bulk_create()`](./drivers#adorm-drivers-django-translation-check-bulk-create-operation)): a multi-table
inheritance child, `update_conflicts` without `unique_fields`, a primary key in `update_fields`, a field without a
column of its own in `update_fields` or `unique_fields`. A name that is not a field raises `FieldError`.

```python
BulkCreate("club.Tag", [{"name": "night"}, {"name": "dawn"}])
BulkCreate("club.Tag", [{"name": "indoor"}], ignore_conflicts=True)
BulkCreate(
    "club.Member",
    [{"email": "ann@example.com", "level": "advanced"}],
    batch_size=500,
    update_conflicts=True,
    update_fields=["level"],
    unique_fields=["email"],
)
```

### `adorm.operations.Save(model, pk, values=(), update_fields=None, force_insert=False, force_update=False)`

Saves an instance: updates the row with primary key `pk`, or inserts a row when `pk` is `None` or no row has it.
`values` holds the instance's loaded and assigned fields; expressions such as `F("age") + 1` are allowed.

- `update_fields`, a subset of the names in `values`, limits the update to those fields and never inserts; an empty
  `update_fields` saves nothing. Duplicates are dropped.
- `force_insert` always inserts, `force_update` never inserts.

Result: the values the database set, at least the primary key.

- `TypeError`: `pk` as an expression; a value that refers to a field of the row when the save inserts (`pk` is `None`
  or `force_insert`).
- `ValueError`: a name in `update_fields` without a value; `force_insert` with `force_update` or `update_fields`;
  `force_update` without a `pk`.

```python
Save("club.Member", 5, {"name": "Ann", "age": 31}, update_fields=["age"])
Save("club.Member", None, {"name": "New"})                  # insert
Save("club.Member", 5, {"age": F("age") + 1}, force_update=True)
```

### `adorm.operations.CreateFromParent(model, parent_pk, values=())`

Inserts the row of the multi-table inheritance child `model` for the existing row of its parent whose primary key is
`parent_pk`: only the child's own table gets a row, with `values` (the child's own fields); the parent's row is not
written. Result: the child's row as stored, the parent's fields included.

`parent_pk` as `None` raises `ValueError`, as an expression `TypeError`; a value that refers to a field raises
`TypeError`. The driver raises:

- `ProgrammingError` for a model that is not a multi-table inheritance child and for a value of an inherited field;
- `ObjectDoesNotExist` when the parent has no row with `parent_pk`;
- `IntegrityError` when the child already has one.

The Django driver reads the parent and saves the child with `save_base(raw=True, force_insert=True)` after its
fields' `pre_save()`. The native PostgreSQL driver inserts the child's table as Django's save does, without the
model's methods and signals; it refuses the operation when the child's `pre_save` or `post_save` has a receiver the
profile does not cover.

```python
CreateFromParent("activities.Competition", 3, {"discipline": "Climbing"})
```

### `adorm.operations.FullClean(model, pk, values=(), exclude=(), validate_unique=True, validate_constraints=True, adding=True)`

Validates an instance of `model` as the target validates one before a save (Django: `Model.full_clean()`).

- The instance has the primary key `pk` and `values` (its loaded and assigned fields, plain values). The driver puts
  them over the stored row when `adding` is false and `pk` is not `None` (reading the row first when `values` lacks
  fields), otherwise over the target's defaults.
- `exclude` names fields or relations that are not validated.
- `validate_unique` and `validate_constraints` switch the unique checks and the model's constraints on or off.
- `adding` is true for an instance not stored yet. A unique check of a stored instance leaves out its own row.

Result: the instance's fields after cleaning, as a row. Errors raise `adorm.ValidationError` with the errors by
adorm name (a foreign key's under its relation's name, the model's under `"__all__"`), each with its message, `code`
and `params`. An expression as `pk` or value and a string `exclude` raise `TypeError`; an `exclude` name that is not
an identifier raises `ValueError`.

```python
FullClean("billing.Invoice", 3, {"due": datetime.date(2025, 1, 1)}, exclude=["club"], adding=False)
```

### `adorm.operations.Update(query, values=())`

Sets `values` on every row of `query`. Values may refer to fields. A sliced query, a query distinct on fields and a
grouped query raise `TypeError`. Result: the number of rows matched.

```python
Update(Query("club.Member").filter(club__name="Chess"), {"fee": F("fee") * 1.1, "active": True})
```

### `adorm.operations.Delete(query)`

Deletes the rows of `query` and the rows that cascade from them;
[`DeleteInstance`](#adorm-operations-deleteinstance-model-pk) deletes an instance. A sliced query, a query distinct
on fields and one after `values()` or `values_list()` raise `TypeError`. Result: `(total, {label: count})`, the rows
deleted per model.

```python
Delete(Query("club.Member").filter(active=False, joined__lt=cutoff))
```

### `adorm.operations.DeleteInstance(model, pk)`

Deletes the instance of `model` with primary key `pk` as the target deletes one instance, with the rows that cascade
from it. On the Django driver the model's `delete()` runs, an override included. The native PostgreSQL driver deletes
with Django's collector and raises `NotSupportedError` for a model with an override. A missing row deletes nothing.
`pk` as `None` raises `ValueError`, as an expression `TypeError`. Result: `(total, {label: count})`.

```python
DeleteInstance("billing.Payment", 7)          # Model.delete() of a Payment with pk 7
```

### `adorm.operations.GetOrCreate(query, lookups=(), defaults=())`

The row of `query` matching `lookups`, or a new row when there is none. The new row has the values of the lookups
whose keys have no `__`, updated with `defaults`. A sliced query raises `TypeError`. Result: `(row, created)`.

- `lookups` is a mapping or pairs of lookup keys and values, prepared as `filter()` prepares them: `None` stays (it
  means `isnull`), an `in` list becomes a tuple without `None`, a query becomes a `Subquery`. An invalid key or value
  raises as in `filter()`; a key given twice raises `ValueError`.
- A lookup on an annotation name of `query` is typed by the annotation.
- `defaults` are field values that may not refer to fields of the new row, as for `Create`.

```python
GetOrCreate(
    Query("club.Member").filter(club_id=3),
    {"email": "ann@example.org"},
    defaults={"name": "Ann"},
)
```

### `adorm.operations.UpdateOrCreate(query, lookups=(), defaults=(), create_defaults=None)`

Sets `defaults` on the row of `query` matching `lookups`, or inserts a new row when there is none. The new row has
the values of the lookups whose keys have no `__`, updated with `create_defaults`, or with `defaults` when
`create_defaults` is `None`. Result: `(row, created)`, the row as stored.

`lookups`, `defaults` and `create_defaults` follow the rules of `GetOrCreate`: neither defaults may refer to a field of
the row (`TypeError`), and a sliced query raises `TypeError`. `create_defaults` is kept as a tuple of pairs, or
`None`; the encoded form always has `"create_defaults"`, `null` for `None`.

```python
UpdateOrCreate(
    Query("club.Member"),
    {"email": "ann@example.org"},
    defaults={"level": "advanced"},
    create_defaults={"name": "Ann", "level": "beginner"},
)
```

## Driver methods

### `adorm.operations.Call(query, method, target="queryset", args=(), kwargs=(), manager=None)`

Calls the driver method `method` on the target ORM's manager `manager`.
[Driver methods](./models#adorm-driver-method-func-queryset-only-false) build it.

- `manager` defaults to [`query.manager`](./query#adorm-query-query-model-manager-none) and must equal it when the
  query carries one.
- `target` `"manager"`: the method runs on that manager; `query` is the model's plain query (`Query(model)` or
  `Query(model, manager=manager)`).
- `target` `"queryset"`: the method runs on the query set of `query` started from that manager.
- `args` are the positional arguments; `kwargs` the keyword arguments as a mapping or `(name, value)` pairs, kept as
  a tuple of pairs. Both hold plain values, copied as in other operations.

A query without `Query.manager` uses no `clause.manager`; only the `call` operation needs a driver that runs the
target ORM's code.

Result: the method's return value as plain data, with every instance of a model of the target ORM as a
[`ModelRow`](#adorm-operations-modelrow-model-row).

- `TypeError`: no manager; an expression or a model instance among the arguments.
- `ValueError`: a `manager` or `method` that is not a public identifier; a `manager` other than the query's; an
  unknown `target`; a manager call with a filtered query; an argument name that is not an identifier; a name given
  twice.

A driver runs only the methods the registered model declares
([`find_driver_method()`](./models#adorm-models-find-driver-method-label-manager-target-method)).

| Member | Meaning |
|---|---|
| `model` | the label of `query`'s model |
| `key` | the [`MethodKey`](./encoding#adorm-schema-methodkey) `(manager, target, method)` |

The encoding types the arguments with the model schema's
[`MethodSchema`](./encoding#adorm-schema-methodschema-positional-keyword-var-positional-none-var-keyword-none) for
`key`, and infers the types of arguments without one.

```python
Call(Query("library.Loan", manager="objects").filter(due__lt=today), "archive", args=("late",))
Call(Query("library.Loan", manager="objects"), "lend", "manager", (7, "9780000000001"), {"days": 3})
Call(Query("auth.User"), "create_user", "manager", ("ann",), {"password": "secret"}, manager="objects")

Call(Query("library.Loan", manager="objects"), "archive").to_dict(registry)
# {"operation": "call", "query": {...}, "manager": "objects", "method": "archive", "target": "queryset",
#  "args": [], "kwargs": {}}
```

### `adorm.operations.ModelRow(model, row)`

An instance of a model in the result of a `Call`: the row of the model `model` (a label) as a read-only mapping keyed
by adorm field names. A driver returns one for every instance of the target ORM's model in a driver method's result;
the caller builds an instance of the registered model from it. A frozen, hashable dataclass. A `model` that is not a
label raises `ValueError`; a `row` that is not a mapping raises `TypeError`.

```python
ModelRow("library.Loan", {"id": 9, "reader_id": 7, "book_id": "9780000000001", "due": date(2026, 3, 4)})
```

## Schema

### `adorm.operations.CheckTable(model, fields=(), many_to_many=())`

Compares the database tables of `model` (a label) with what the model expects of them. It reads the database's
catalogue and changes nothing. `fields` are the target ORM's names of the model's fields; `many_to_many` those of its
many-to-many relations, whose automatic link tables are checked too. Both are iterables of names (not a string),
kept without repeats. [`adorm.checks.table_check(model)`](./checks#adorm-checks-table-check-model) builds it from a model
class.

Result: a list of problem dicts with the keys `kind` (a
[`SchemaProblemKind`](./checks#adorm-checks-schemaproblemkind)), `table`, `column`, `field` (an ORM name of `fields`
or `many_to_many`), `expected` and `actual`, `None` where they do not apply; an empty list when the tables match. The
[Django and native PostgreSQL drivers](./drivers#table-checks) execute it.

```python
await session.execute(CheckTable("clubs.Club", ("id", "name", "slug", "founded", "city"), ()))
# [] or, for a table without the column city:
# [{'kind': 'missing_column', 'table': 'clubs_club', 'column': 'city', 'field': 'city', 'expected': None, 'actual': None}]
CheckTable("clubs.Club", ("id",)).to_dict()
# {'operation': 'check_table', 'model': 'clubs.Club', 'fields': ['id'], 'many_to_many': []}
```

## Events

Operations emit and consume no events; they are the requests a slot executes. The slot answers each one with a
[`ResultEvent`](./events#adorm-events-resultevent-request-id-value) carrying the result above, an
[`ErrorEvent`](./events#adorm-events-errorevent-request-id-error) carrying an adorm exception, or a
[`CancelledEvent`](./events#adorm-events-cancelledevent-request-id-started) when the client cancelled it. Callbacks
registered with [`on_commit()`](./transactions#adorm-on-commit-func-robust-false-session-none) run when the slot
reports the commit; signals of the target ORM run on the driver side only.
