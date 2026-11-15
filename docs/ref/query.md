# Query

The immutable query, importable from `adorm.query` (defined in `adorm.query.query`). [Making
queries](/topics/queries) shows it in context.

```python
from adorm import F, Lower, Q, Sum
from adorm.query import ForUpdate, Query, as_query
```

### `adorm.query.Query(model, *, manager=None)`

The rows of one model that a read returns. `model` is the model's label, `"app_label.ModelName"`; anything else
raises `ValueError`. `manager` names the target ORM's manager the driver starts from (a public identifier, otherwise
`ValueError`); [managers](/topics/managers) set it. A `Query` is a [node](./expressions#adorm-query-node): immutable, compared by value and
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
| `is_sliced` | whether a slice limits the rows |
| `is_empty` | whether the query returns no rows: `is_none`, or the slice leaves none (`high_mark <= low_mark`) |

#### `filter(*args, **kwargs)`

Rows that also match the conditions, arguments as for [`Q`](./expressions#adorm-q-args-kwargs): appends
`Q(*args, **kwargs)` to `filters`, unless it is empty. Calls chain with AND; as in Django, a multi-valued relation
(a reverse foreign key, a many-to-many relation) is joined once per call, so the conditions of separate calls may
match different related rows. On a sliced query raises `TypeError`.

```python
Query("library.Book").filter(authors__name="Ann").filter(authors__name="Cid")   # an author Ann and an author Cid
Query("library.Book").filter(authors__name="Ann", authors__mentor__name="Cid")  # an author Ann mentored by Cid
```

#### `exclude(*args, **kwargs)`

Rows that also do not match the conditions: appends `~Q(*args, **kwargs)` to `filters`, unless the condition is
empty. On a sliced query raises `TypeError`.

#### `annotate(**annotations)`

Adds computed columns, name to expression. A name already used, a name with `__` or one that is not an identifier
raises `ValueError`; a value that is not an expression raises `TypeError`. Later calls of `filter()`,
`order_by()` and `values()` refer to annotations by name. A condition on an annotation that contains an aggregate
filters the aggregated rows (SQL `HAVING`).

On model rows an aggregate is computed per row:

```python
Query("clubs.Club").annotate(headcount=Count("memberships")).filter(headcount__gt=3)
```

After `values()` or `values_list()` with fields, the new names are added to `select`, and the first annotation that
contains an aggregate (directly or through `F` of another such annotation) groups the rows: `group_by` becomes the
names selected so far plus the new annotations without an aggregate, and `group_mark` the number of annotations
before the call. Later `values()` calls keep the grouping, and an annotation without an aggregate added later joins
`group_by`. An aggregate annotated before `values()` does not group: it stays per row.

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

A condition on a field filters the rows before they are aggregated (SQL `WHERE`), a condition on an aggregate filters
the groups (`HAVING`). Filters and annotations apply in the order they were added (`filter_marks`), as in Django: an
aggregate annotation added after a condition on a multi-valued relation aggregates only the related rows that matched
it, and a condition added after the annotation joins the relation again, so each row repeats once per matching
related row and the aggregate multiplies. An aggregate's `filter=` limits it whatever the order.

```python
Query("clubs.Club").filter(memberships__role="member").annotate(n=Count("memberships"))   # members per club
Query("clubs.Club").annotate(n=Count("memberships")).filter(memberships__role="member")   # memberships × members
```

#### `alias(**annotations)`

As `annotate()`, but the names go to `aliases`: conditions, orderings and other expressions refer to them, and the
rows do not include them. Selecting an alias with `values()` or `values_list()` raises `ValueError`. An alias with an
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

Loads only `fields` (and the primary key) of model rows; the other fields are deferred. It replaces an earlier
`only()`; fields deferred before are left out. After `values()` or `values_list()` raises `TypeError`.

#### `defer(*fields)`

Leaves `fields` unloaded until they are accessed. Calls add up; after `only()` it removes the fields from the loaded
set. `defer(None)` loads every field again. After `values()` or `values_list()` raises `TypeError`.

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

Rows matching either query (`|`) or both (`&`). Both queries are on the same model and manager, unsliced, and agree
on distinctness, `select`, `row_format`, annotations, aliases, grouping, `only()`, `defer()` and
`select_for_update()`; otherwise
`TypeError`. `&` keeps the filters of both queries and their `filter_marks`; `|` has one filter, the `where` of both
joined with OR, placed after the annotations either query's filters followed. The result
has the ordering of `other`, or of `query` when `other` has none, and the `select_related()` paths of both. A query
without conditions matches every row, so `|` with it has no conditions;
`|` with a `none()` query returns the other query, `&` returns the empty one.

`Query` has no set operations: `union()`, `intersection()` and `difference()` are not supported.

```python
active = Query("club.Member").filter(active=True)
guests = Query("club.Member").filter(guest=True)
active | guests            # where: active=True OR guest=True
active & guests            # where: active=True AND guest=True
```

#### Construct names

`constructs()` yields the construct names of the filters, the annotations and the ordering, plus
`clause.annotate` (an annotation that is not an alias), `clause.alias`, `clause.group_by`, `clause.distinct` or
`clause.distinct_on`, `clause.only`, `clause.defer`, `clause.select_related`, `clause.manager` and the names of
`for_update` for the clauses in use, and [`path.nested`](#adorm-query-nested-path) when a field of `values()`,
`values_list()`, `group_by`, `only()`, `defer()` or `distinct()` has several segments. `values()` and `values_list()` of plain fields, `none()` and slicing have none.

```python
set(Query("library.Loan", manager="objects").filter(archived=False).constructs())
# {"clause.manager", "lookup.exact"}
set(Query("clubs.Member").filter(memberships__club__slug="koper-sailing").constructs())
# {"lookup.exact", "path.nested"}
```

#### Encoding

`to_dict(schema=None)` and `Query.from_dict(data, schema=None)` convert a query to JSON-safe data and back; lookup
values are typed by the queried model's fields (see [Operation encoding](./encoding)).

### `adorm.query.ForUpdate(*, nowait=False, skip_locked=False, of=(), no_key=False)`

The row locks of `select_for_update()`, a node. `nowait` fails instead of waiting for a locked row, `skip_locked`
skips locked rows, `of` names the relations whose rows are locked (`"self"` for the queried model; duplicates are
dropped) and `no_key` takes the weaker `FOR NO KEY UPDATE` lock. `nowait` with `skip_locked` raises `ValueError`, a
string `of` raises `TypeError`.

Its construct names are `clause.select_for_update`, plus `clause.select_for_update.<option>` for each option set
(`nowait`, `skip_locked`, `of`, `no_key`).

### `adorm.query.CLAUSES`

The construct names of `Query` clauses and `ForUpdate` options, a tuple: `clause.annotate`, `clause.alias`,
`clause.group_by`, `clause.distinct`,
`clause.distinct_on`, `clause.only`, `clause.defer`, `clause.select_related`, `clause.manager`,
`clause.select_for_update` and `clause.select_for_update.<option>` for `nowait`, `skip_locked`, `of` and `no_key`.

### `adorm.query.NESTED_PATH`

`"path.nested"`: the construct name of a field path with several segments (`club__name`, `address__street`), which
crosses a relation or applies a transform. [`F`](./expressions) reports it for such a path, and so does a query for
its `values()`, `only()`, `defer()` and `distinct()` fields. A driver that evaluates only a model's own columns
leaves it out of its capabilities.

```python
list(F("club__name").constructs())     # ['path.nested']
list(F("name").constructs())           # []
```

### `adorm.query.construct_names()`

Returns every construct name an operation can report, as a frozenset: `CLAUSES`, `NESTED_PATH`,
`aggregate.distinct`, `aggregate.filter`, and the construct of every node class defined so far, custom
[lookups](./lookups) and [functions](./expressions) included. A driver's declared constructs are a subset of it.

```python
"lookup.icontains" in construct_names()                        # True
DjangoDriver.capabilities.constructs <= construct_names()      # True
```

### `adorm.query.as_query(value)`

`value` when it is a `Query`, its `query` attribute when that is a `Query`, else `None`. Lookups and `Subquery` use
it to accept any object that carries a query.
