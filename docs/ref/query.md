# Query

The immutable query, importable from `adorm.query` (defined in `adorm.query.query`). See [Making
queries](/topics/queries).

```python
from adorm import F, Lower, Q, Sum
from adorm.query import COMBINATORS, ForUpdate, Query, as_query
```

## Query

### `adorm.query.Query(model, *, manager=None)`

The rows of one model that a read returns. `model` is the model's label, `"app_label.ModelName"`. `manager` names
the target ORM's manager the driver starts from, a public identifier; [managers](/topics/managers) set it. Other
values raise `ValueError`. A `Query` is a [node](./expressions#adorm-query-node): immutable, compared by value and
hashable. Every method returns a new query; building does no I/O.

```python
q = (
    Query("club.Member")
    .filter(active=True, club__name__icontains="chess")
    .exclude(Q(age__lt=18) & ~Q(guest=True))
    .annotate(paid=Sum("payment__amount", default=0))
    .order_by("-paid", Lower("name"))
    .values("name", "paid")
)[:10]
```

| Attribute | Contents |
|---|---|
| `model` | the model label |
| `filters` | a tuple of non-empty [`Q`](./expressions#adorm-q-args-kwargs) objects, one per `filter()` or `exclude()` call; empty for all rows |
| `filter_marks` | for each filter, the number of annotations added before it |
| `where` | the `filters` joined with AND, a `Q`; empty for all rows |
| `annotations` | `(name, expression)` pairs in the order added, aliases included |
| `aliases` | the names of the annotations `alias()` added; rows do not include them |
| `group_by` | `None`, or the selected names the rows are grouped by (`values()` followed by an aggregate annotation) |
| `group_mark` | `None`, or the number of annotations added before the `annotate()` or `alias()` call that grouped the rows |
| `is_grouped` | whether `group_by` is set |
| `ordering` | a tuple of [`OrderBy`](./expressions#adorm-query-orderby-expression-descending-false-nulls-first-none-nulls-last-none) |
| `select` | the selected field paths and annotation names; empty for all fields |
| `row_format` | how rows are returned: `"model"` (model instances), `"dict"` (`values()`), `"tuple"`, `"flat"` or `"named"` (`values_list()`) |
| `is_distinct` | whether duplicate rows are removed |
| `distinct_fields` | the fields of `DISTINCT ON`; empty for plain `DISTINCT` |
| `low_mark`, `high_mark` | the slice; `high_mark` is `None` without a limit |
| `only_fields` | the fields `only()` loads, `None` when it was not called |
| `deferred_fields` | the fields `defer()` leaves unloaded |
| `select_related_fields` | `False`, `True` (every non-null relation) or a tuple of relation paths |
| `is_none` | whether `none()` was called |
| `for_update` | a [`ForUpdate`](#adorm-query-forupdate-nowait-false-skip-locked-false-of-no-key-false), or `None` |
| `manager` | the target ORM's manager whose rows and query set methods the driver uses (Django: the manager attribute, so its `get_queryset()` and its query set's `delete()` …), `None` for the model's rows without manager logic |
| `combinator` | `None`, or `"union"`, `"intersection"` or `"difference"`: the set operation that combines `combined_queries` |
| `combinator_all` | whether a union keeps duplicate rows (`union(all=True)`) |
| `combined_queries` | the combined queries, a tuple; empty without a set operation |
| `is_combined` | whether `combinator` is set |
| `is_sliced` | whether a slice limits the rows |
| `is_empty` | whether the query returns no rows: `is_none`, or the slice leaves none (`high_mark <= low_mark`) |

#### `filter(*args, **kwargs)`

Rows that also match the conditions, arguments as for [`Q`](./expressions#adorm-q-args-kwargs): appends
`Q(*args, **kwargs)` to `filters` unless it is empty. Calls chain with AND. As in Django, a multi-valued relation (a
reverse foreign key, a many-to-many relation) is joined once per call; conditions of separate calls may match
different related rows. On a sliced query raises `TypeError`.

```python
Query("library.Book").filter(authors__name="Ann").filter(authors__name="Cid")   # an author Ann and an author Cid
Query("library.Book").filter(authors__name="Ann", authors__mentor__name="Cid")  # an author Ann mentored by Cid
```

#### `exclude(*args, **kwargs)`

Rows that also do not match the conditions: appends `~Q(*args, **kwargs)` to `filters`, unless the condition is
empty. On a sliced query raises `TypeError`.

#### `annotate(**annotations)`

Adds computed columns, name to expression. A name already used, a name with `__` or a non-identifier raises
`ValueError`; a value that is not an expression raises `TypeError`. `filter()`, `order_by()` and `values()` refer
to annotations by name. A condition on an annotation that contains an aggregate filters the aggregated rows (SQL
`HAVING`).

On model rows an aggregate is computed per row:

```python
Query("clubs.Club").annotate(headcount=Count("memberships")).filter(headcount__gt=3)
```

After `values()` or `values_list()` with fields:

- the new names are added to `select`;
- the first annotation that contains an aggregate (directly or through `F` of another such annotation) groups the
  rows: `group_by` becomes the names selected so far plus the new annotations without an aggregate, `group_mark` the
  number of annotations before the call;
- later `values()` calls keep the grouping; an annotation without an aggregate added later joins `group_by`.

An aggregate annotated before `values()` does not group; it is computed per row.

```python
q = Query("clubs.Membership").values("role").annotate(n=Count("*"), fees=Sum("fee"))
q.group_by     # ("role",)
q.select       # ("role", "n", "fees")

months = (
    Query("billing.Invoice")
    .annotate(month=TruncMonth("issued"))
    .values("month")
    .annotate(total=Sum("total"))
    .filter(total__gt=100)
    .order_by("month")
)
months.group_by   # ("month",)

q.annotate(club=F("club__slug")).group_by   # ("role", "club")
```

A condition on a field filters the rows before aggregation (SQL `WHERE`), a condition on an aggregate filters the
groups (`HAVING`). Filters and annotations apply in the order added (`filter_marks`), as in Django:

- an aggregate annotation added after a condition on a multi-valued relation aggregates only the related rows that
  matched it;
- a condition added after the annotation joins the relation again; each row repeats once per matching related row
  and the aggregate multiplies.

An aggregate's `filter=` limits it regardless of order.

```python
Query("clubs.Club").filter(memberships__role="member").annotate(n=Count("memberships"))   # members per club
Query("clubs.Club").annotate(n=Count("memberships")).filter(memberships__role="member")   # memberships × members
```

#### `alias(**annotations)`

As `annotate()`, but the names go to `aliases`: conditions, orderings and other expressions refer to them; rows do
not include them. Selecting an alias with `values()` or `values_list()` raises `ValueError`. An alias with an
aggregate after `values()` groups the rows as an annotation does.

```python
Query("clubs.Club").alias(headcount=Count("memberships")).filter(headcount__gt=3).order_by("-headcount")
```

#### `aggregate_names()`

The names of the annotations (aliases included) that contain an aggregate outside a subquery, directly or through
`F` of another such annotation, as a frozenset.

#### `order_by(*keys)`

Replaces the ordering. A key is a field path (`"-name"` for descending), an expression or an `OrderBy`; anything else
raises `TypeError`. `order_by()` without keys clears the ordering. On a sliced query raises `TypeError`.

```python
Query("club.Member").order_by("-joined", Lower("name"), F("nick").asc(nulls_last=True))
```

#### `values(*fields)`

Rows as dicts of `fields` (field paths or annotation names); `values()` without fields returns every field. Sets
`row_format` to `"dict"`. A query used as a subquery selects at most one column.

#### `values_list(*fields, flat=False, named=False)`

Rows as tuples of `fields`, in the order given (`row_format` `"tuple"`). `flat=True` returns the values of the single
field (`"flat"`), `named=True` named tuples (`"named"`). `flat` with more than one field, or with `named`, raises
`TypeError`.

```python
Query("club.Member").values("name", "club__name")          # [{"name": ..., "club__name": ...}, ...]
Query("club.Member").values_list("name", "age")            # [("Ann", 30), ...]
Query("club.Member").values_list("pk", flat=True)          # [1, 2, ...]
Query("club.Member").values_list("name", "age", named=True)
```

#### `only(*fields)`

Loads only `fields` and the primary key of model rows; the other fields are deferred. Replaces an earlier `only()`;
fields deferred before are left out. After `values()` or `values_list()` raises `TypeError`.

```python
Query("club.Member").only("name", "email")          # pk, name and email loaded
Query("club.Member").defer("biography")            # every field but biography
```

#### `defer(*fields)`

Leaves `fields` unloaded until accessed. Calls add up; after `only()` it removes the fields from the loaded set.
`defer(None)` loads every field again. After `values()` or `values_list()` raises `TypeError`.

```python
Query("club.Member").only("name", "club__name")
Query("club.Member").defer("photo", "bio")
Query("club.Member").defer("bio").only("bio", "name")     # only_fields == ("name",)
```

#### `select_related(*fields)`

Loads the related rows of the relation paths `fields` in the same query. Paths add up over calls;
`select_related()` without paths follows every non-null relation and `select_related(None)` clears the setting.
After `values()` or `values_list()` raises `TypeError`.

```python
Query("club.Member").select_related("club", "club__owner")
```

#### `none()`

A query that returns no rows. Further filters keep it empty; combined with `|` it yields the other query.

#### `select_for_update(*, nowait=False, skip_locked=False, of=(), no_key=False)`

Locks the selected rows until the end of the transaction. The options are those of
[`ForUpdate`](#adorm-query-forupdate-nowait-false-skip-locked-false-of-no-key-false); `nowait` with `skip_locked`
raises `ValueError`.

```python
Query("club.Member").filter(pk=1).select_for_update(nowait=True)
Query("club.Member").select_related("club").select_for_update(of=("self",), skip_locked=True)
```

#### `distinct(*fields)`

Removes duplicate rows. With `fields`, keeps one row per distinct value of those fields (PostgreSQL's
`DISTINCT ON`). On a sliced query raises `TypeError`.

#### `query[start:stop]`

Limits the rows; slicing a sliced query slices its result again. Bounds are non-negative integers, with no step;
otherwise `TypeError` (an index or non-integer bound) or `ValueError` (a negative bound or a step).

```python
Query("club.Member")[10:20][5:]      # rows 15 to 19
```

#### `query | other`, `query & other`

Rows matching either query (`|`) or both (`&`). Both queries must be on the same model and manager, unsliced, not
combined by a set operation, and agree on distinctness, `select`, `row_format`, annotations, aliases, grouping,
`only()`, `defer()` and `select_for_update()`; otherwise `TypeError`.

- `&` keeps the filters of both queries and their `filter_marks`.
- `|` has one filter, the `where` of both joined with OR, placed after the annotations either query's filters
  followed. `|` with a query without conditions has no conditions.
- With a `none()` query, `|` returns the other query, `&` the empty one.
- The result has the ordering of `other`, or of `query` when `other` has none, and the `select_related()` paths of
  both.

```python
active = Query("club.Member").filter(active=True)
guests = Query("club.Member").filter(guest=True)
active | guests            # where: active=True OR guest=True
active & guests            # where: active=True AND guest=True
```

#### `union(*others, all=False)`

The rows of the query and of the queries `others` (SQL `UNION`); `all=True` keeps duplicate rows. Returns a combined
query: `combinator` `"union"`, `combined_queries` the query followed by `others`, and the query's `select` and
`row_format`. The queries select the same number of columns of matching types, column by column, and may be on
different models; the database refuses other columns. The query keeps its ordering and slice; each of `others` keeps
them when it is sliced or distinct on fields, and loses its ordering otherwise. An argument that is not a `Query`
raises `TypeError`.

- Without `others`, returns the query itself.
- An empty query (`is_empty`: `none()`, or a slice that leaves no rows) among `others` stays among the combined
  queries; the database leaves it out (see [Empty queries](#empty-queries)).
- On an empty query, the non-empty queries of `others` are combined instead; a single one is returned as it is, and
  with none the query is returned.

```python
adults = Query("club.Member").filter(age__gte=18).values_list("name", flat=True)
coaches = Query("club.Member").filter(tags__contains=["coach"]).values_list("name", flat=True)
adults.union(coaches)
adults.union(coaches, all=True).order_by("name")[:10]
adults.union(Query("club.Club").values_list("name", flat=True))   # names of members and clubs
```

#### `intersection(*others)`

The rows the query and every one of `others` return (SQL `INTERSECT`), without duplicates; a combined query with
`combinator` `"intersection"`, built as `union()` builds it. Without `others` the query alone is combined. When the
query or one of `others` is empty (`is_empty`), returns that query.

#### `difference(*others)`

The rows of the query that none of `others` returns (SQL `EXCEPT`), without duplicates; a combined query with
`combinator` `"difference"`, built as `union()` builds it. Without `others` the query alone is combined. An empty
query among `others` stays among the combined queries; on an empty query returns the query itself.

```python
adults.intersection(coaches)
adults.difference(coaches).order_by("-name")
adults.union(coaches).difference(Query("club.Member").filter(guest=True).values_list("name", flat=True))
```

#### After a set operation

A combined query has no filters, annotations or other clauses of its own. These methods apply to the combined rows:
`order_by()`, slicing, `values()` and `values_list()`, `none()`, and another set operation, which nests the
combination.

- `values()` and `values_list()` select their fields in every combined query that selects model rows. A combined
  query with its own `values()` keeps its columns; the combined rows are keyed by the names given, column by column.
- After `values()` or `values_list()`, `order_by()` keys name columns of the combined rows, including names only the
  later `values()` gives. A key that names no column raises [`DatabaseError`](./exceptions)
  (`ORDER BY term does not match any column in the result set.`).
- Combined model rows are ordered by the fields of the first combined query's model.
- `filter()`, `exclude()`, `annotate()`, `alias()`, `distinct()`, `only()`, `defer()`, `select_related()` and
  `select_for_update()` raise [`NotSupportedError`](./exceptions) (`cannot call filter() after union()`), as in
  Django.

```python
combined = adults.union(coaches)
combined.order_by("-name")[:5].values_list("name", flat=True)
combined.filter(age=30)       # NotSupportedError: cannot call filter() after union()
combined.values_list("label", flat=True).order_by("label")   # the one column, named label
```

#### Empty queries

The drivers combine empty queries as Django compiles them:

- an empty query is left out of a union, and of a difference after its first query;
- an empty query in an intersection makes the result empty;
- a combination left with a single query reads that query's rows as they are, with duplicates, also for
  `intersection()` and `difference()`; a sliced union removes duplicate rows unless `all=True`;
- that single query is placed in parentheses before the combination's own ordering and slice. PostgreSQL refuses an
  ordering, offset or limit given to both with `ProgrammingError` (`multiple ORDER BY clauses not allowed`); an
  ordering of the single query counts only when it is sliced.

```python
first_two = adults.order_by("name")[:2]
first_two.union(Query("club.Member").none()).order_by("-name")[:5]   # a union: distinct names
first_two.union(Query("club.Member").none()).order_by("-name")       # ProgrammingError when read
```

#### `check_not_combined(action)`

Raises `NotSupportedError` (`cannot call <action>() after <combinator>()`) when the query is combined.

#### Construct names

`constructs()` yields:

- the construct names of the filters, the annotations and the ordering;
- for the clauses in use: `clause.annotate` (an annotation that is not an alias), `clause.alias`, `clause.group_by`,
  `clause.distinct` or `clause.distinct_on`, `clause.only`, `clause.defer`, `clause.select_related`,
  `clause.manager` and the names of `for_update`;
- for a set operation: `clause.union`, `clause.intersection` or `clause.difference`, `clause.combinator.slice` when a
  combined query is sliced, followed by the construct names of the combined queries;
- [`path.nested`](#adorm-query-nested-path) when a field of `values()`, `values_list()`, `group_by`, `only()`,
  `defer()` or `distinct()` has several segments.

`values()` and `values_list()` of plain fields, `none()` and slicing have none.

```python
set(Query("library.Loan", manager="objects").filter(archived=False).constructs())
# {"clause.manager", "lookup.exact"}
set(Query("clubs.Member").filter(memberships__club__slug="koper-sailing").constructs())
# {"lookup.exact", "path.nested"}
```

#### Encoding

`to_dict(schema=None)` and `Query.from_dict(data, schema=None)` convert a query to JSON-safe data and back; lookup
values are typed by the queried model's fields (see [Operation encoding](./encoding)). The combined queries of a
set operation are encoded under `combined_queries`, each typed by its own model.

### `adorm.query.ForUpdate(*, nowait=False, skip_locked=False, of=(), no_key=False)`

The row locks of `select_for_update()`, a node.

- `nowait`: fail instead of waiting for a locked row.
- `skip_locked`: skip locked rows.
- `of`: the relations whose rows are locked (`"self"` for the queried model); duplicates are dropped.
- `no_key`: take the weaker `FOR NO KEY UPDATE` lock.

`nowait` with `skip_locked` raises `ValueError`, a string `of` raises `TypeError`.

Its construct names are `clause.select_for_update`, plus `clause.select_for_update.<option>` for each option set
(`nowait`, `skip_locked`, `of`, `no_key`).

### `adorm.query.CLAUSES`

The construct names of `Query` clauses and set operations, `ForUpdate` options and the conflict handling of
[`BulkCreate`](./operations#adorm-operations-bulkcreate-model-rows-batch-size-none-ignore-conflicts-false-update-conflicts-false-update-fields-none-unique-fields-none-manager-none),
a tuple: `clause.annotate`, `clause.alias`, `clause.group_by`, `clause.distinct`, `clause.distinct_on`,
`clause.only`, `clause.defer`, `clause.select_related`, `clause.manager`, `clause.select_for_update` and
`clause.select_for_update.<option>` for `nowait`, `skip_locked`, `of` and `no_key`, `clause.on_conflict.ignore`,
`clause.on_conflict.update`, `clause.union`, `clause.intersection`, `clause.difference` and
`clause.combinator.slice`.

### `adorm.query.COMBINATORS`

`("union", "intersection", "difference")`: the values of `Query.combinator` for a set operation.

### `adorm.query.NESTED_PATH`

`"path.nested"`: the construct name of a field path with several segments (`club__name`, `address__street`), which
crosses a relation or goes into a JSON field. [`F`](./expressions) reports it for such a path, a query for such
`values()`, `only()`, `defer()` and `distinct()` fields. A driver that evaluates only a model's own columns leaves
it out of its capabilities.

```python
list(F("club__name").constructs())     # ['path.nested']
list(F("name").constructs())           # []
```

### `adorm.query.construct_names()`

Every construct name an operation can report, as a frozenset: `CLAUSES`, `NESTED_PATH`, `aggregate.distinct`,
`aggregate.filter`, and the construct of every node class defined at the time of the call, including custom
[lookups](./lookups) and [functions](./expressions). A driver's declared constructs are a subset of it.

```python
"lookup.icontains" in construct_names()                        # True
DjangoDriver.capabilities.constructs <= construct_names()      # True
```

### `adorm.query.as_query(value)`

`value` when it is a `Query`, its `query` attribute when that is a `Query`, else `None`. Lookups and `Subquery`
accept any object for which it returns a query.
