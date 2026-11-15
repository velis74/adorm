# Making queries

::: info Status
The installed version provides the query building blocks on this page: `Q`, `F`, lookups, functions, aggregates,
other expressions, the immutable [`Query`](/ref/query), the [operations](/ref/operations) that read and write
with it and the [`QuerySet`](/ref/queryset) that runs them on a session. The [Django driver](/topics/django-driver) and
the [native PostgreSQL driver](/topics/postgresql-driver) execute all of them: the reads (`select`, `get`, `first`,
`last`, `count`, `exists`, `aggregate`), annotations, aliases, grouping, functions, aggregates, subqueries, the writes
and `select_for_update()`.
:::

A query is built from adorm's own building blocks, never from the target ORM's. Building one is plain Python: it
opens no connection, imports no ORM and needs no model class. The result is a description that a driver translates
into its target's constructs (Django lookups and expressions, SQL, SQLAlchemy Core).

```python
from adorm import Count, F, Lower, Q
from adorm.query import Query

members = (
    Query("club.Member")
    .filter(Q(name__icontains="ann") | Q(nick__icontains="ann"), active=True)
    .exclude(club__isnull=True)
    .annotate(payments=Count("payment"))
    .order_by(Lower("name"), "-joined")
)[:20]
```

A model is named by its label, `"app_label.ModelName"`, and fields by their adorm names. The driver resolves both
when it executes the query.

## Immutability

Every building block is immutable. A method of `Query` returns a new query and leaves the original unchanged, so a
query can be stored at module level, shared between tasks and refined independently:

```python
active = Query("club.Member").filter(active=True)
adults = active.filter(age__gte=18)
minors = active.filter(age__lt=18)

assert active == Query("club.Member").filter(active=True)   # unchanged
```

Nodes compare by value and are hashable, so equal queries are equal keys:

```python
cache = {adults: "adults"}
assert cache[Query("club.Member").filter(active=True).filter(age__gte=18)] == "adults"
```

Setting an attribute of a node raises `AttributeError`. Iterables passed to `in` and `range` are copied into tuples,
so changing the original list afterwards does not change the query.

## Field lookups

A keyword argument of `filter()`, `exclude()` or `Q` is a field lookup: a field path, `__`, and a lookup name.
Without a lookup name the lookup is `exact`.

```python
Query("club.Member").filter(name="Ann")                 # exact
Query("club.Member").filter(name__iexact="ann")
Query("club.Member").filter(age__gte=18, age__lt=65)
Query("club.Member").filter(name__startswith="A")
Query("club.Member").filter(email__icontains="@example.org")
Query("club.Member").filter(code__regex=r"^[A-Z]{3}-\d+$")
Query("club.Member").filter(pk__in=[1, 2, 3])
Query("club.Member").filter(joined__range=(start, end))
Query("club.Member").filter(club__isnull=True)
```

`name=None` and `name__iexact=None` become `name__isnull=True`; any other lookup with `None` raises `ValueError`.
The [lookup reference](/ref/lookups) lists every lookup.

### Relation traversal

A field path crosses relations with `__`, as in Django. The last segment is the lookup when it names a registered
lookup; otherwise the whole key is the field path:

```python
Query("club.Member").filter(club__name="Chess")               # club → name, exact
Query("club.Member").filter(club__owner__email__iendswith=".si")
Query("club.Club").filter(member__age__lt=18)                  # reverse relation
```

A field whose name equals a lookup name takes an explicit lookup: `filter(range__exact=5)`.

As in Django, conditions on a multi-valued relation (a reverse foreign key, a many-to-many relation) in one
`filter()` call apply to the same related row; conditions in separate calls may each match a different one:

```python
Query("club.Club").filter(member__age__lt=18, member__guest=True)    # a member who is a minor guest
Query("club.Club").filter(member__age__lt=18).filter(member__guest=True)  # a minor member and a guest member
```

### Queries as subqueries

`in` takes a query instead of a list; the query becomes a subquery. It selects one column with `values()`, or the
primary key when it selects none:

```python
active_clubs = Query("club.Club").filter(active=True)
Query("club.Member").filter(club__in=active_clubs)                       # club in (select id ...)
Query("club.Member").filter(email__in=Query("club.Invite").values("email"))
```

A query passed to any other lookup also becomes a `Subquery`; it must return a single row.

## Combining conditions with `Q`

`Q` holds lookups joined with AND. `&` (AND), `|` (OR) and `~` (NOT) combine them:

```python
from adorm import Q

Q(name__startswith="A") | Q(name__startswith="B")
Q(active=True) & ~Q(club__name="Chess")
Query("club.Member").filter(Q(age__lt=18) | Q(guest=True), active=True)
```

Positional arguments of `filter()` are conditions: `Q` objects, lookups and `Exists`. Keyword arguments follow them
and are joined with AND. `exclude()` adds the negation of its conditions.

`Q()` is empty and falsy; combining it with another condition returns the other one. That makes building
conditions in a loop simple:

```python
condition = Q()
for word in words:
    condition &= Q(name__icontains=word)
members = Query("club.Member").filter(condition)
```

## Lookup keys built at run time

A lookup key is a string, so a filter form, a query string or a viewset can build it at run time. It goes into
`filter()`, `exclude()`, `get()` or `Q` as a keyword argument or as a `(key, value)` pair, and builds the same lookup
as the literal keyword:

```python
field, op = "club__name", "icontains"
Member.objects.filter(**{f"{field}__{op}": "chess"})        # same as filter(club__name__icontains="chess")
Member.objects.exclude(Q((f"{field}__{op}", "chess")))
await Member.objects.get(**{"email__iexact": email})
```

Pairs keep their order, keyword arguments are sorted; `_connector=Q.OR` joins them with OR instead of AND:

```python
params = {"first_name__istartswith": "e", "level__in": ["advanced"], "birth_date__gte": date(1980, 1, 1)}
Member.objects.filter(**params)                             # all three
Member.objects.filter(Q(*params.items(), _connector=Q.OR))  # any of them
```

Declared filters (field → allowed lookups) become a condition in a loop:

```python
DECLARED = {"first_name": ["icontains", "istartswith"], "level": ["in"], "birth_date": ["gte", "lte"]}

condition = Q()
for field, ops in DECLARED.items():
    for op in ops:
        key = f"{field}__{op}"
        if key in params:
            condition &= Q((key, params[key]))
members = Member.objects.filter(condition)
```

Building a query checks only the key's syntax: segments are identifiers joined with `__`, and a key that is not
(`"name__"`, `"tags__0"`) raises `ValueError`. Whether the fields exist is decided by the model's schema.
[`resolve_lookup()`](/ref/lookups#adorm-query-resolve-lookup-target-key-annotations-source-none) checks one key,
[`check_lookups()`](/ref/lookups#adorm-query-check-lookups-target-condition-annotations-source-none) a whole condition;
both raise [`FieldError`](/ref/exceptions#adorm-fielderror-message-none-model-none-key-none-original-none-sqlstate-none)
without running an operation, so a key taken from user input can be refused before the query takes a slot:

```python
from adorm.query import resolve_lookup

resolve_lookup(Member, "first_name__icontain")
# FieldError: unsupported lookup 'icontain' for clubs.Member.first_name; perhaps you meant 'icontains'
resolve_lookup(Member, "memberships__club__city__icontains").field_type   # str (with its max length)
resolve_lookup(Member, "level__in").value_type     # Literal['beginner', 'intermediate', 'advanced']
```

A path that ends at a relation takes only `exact`, `gt`, `gte`, `lt`, `lte`, `in` and `isnull` (`club__in`), and a
path cannot continue past a scalar field: adorm has no transforms, so `joined__year` raises `FieldError` on every
driver. A key that is not checked first and does not resolve raises `FieldError` from the driver when it builds the
statement.
That error does not affect the [transaction](/topics/transactions#a-failed-operation) it is raised in.

## Referring to fields with `F`

`F("field")` refers to a field of the queried row; it compares fields with each other and does arithmetic in the
database:

```python
from adorm import F

Query("club.Event").filter(ends__gt=F("starts"))
Query("club.Member").filter(paid__lt=F("fee") * 0.5)
Query("club.Member").annotate(age_next_year=F("age") + 1)
```

`F` paths cross relations as lookups do: `F("club__owner__name")`.

## Functions, aggregates and conditional expressions

Functions take field paths as strings; a literal string is `Value("...")`:

```python
from adorm import Coalesce, Concat, Lower, Value

Query("club.Member").order_by(Lower("name"))
Query("club.Member").annotate(display=Concat("first_name", Value(" "), "last_name"))
Query("club.Member").annotate(label=Coalesce("nick", "name"))
```

Aggregates take `distinct=`, `filter=` and `default=`:

```python
from adorm import Count, Sum

Query("club.Club").annotate(
    members=Count("member", distinct=True),
    paid=Sum("member__payment__amount", filter=Q(member__payment__confirmed=True), default=0),
)
```

`Case` and `When` choose a value per row; `Subquery`, `OuterRef` and `Exists` correlate a query with the outer one:

```python
from adorm import Case, Exists, OuterRef, Subquery, Value, When

Query("club.Member").annotate(
    group=Case(When(age__lt=18, then=Value("junior")), default=Value("senior")),
    last_payment=Subquery(
        Query("club.Payment").filter(member=OuterRef("pk")).order_by("-paid_at").values("paid_at")[:1]
    ),
).filter(Exists(Query("club.Payment").filter(member=OuterRef("pk"), confirmed=False)))
```

The [expression reference](/ref/expressions) documents every class; [Aggregation](/topics/aggregation) shows
`aggregate()`, per-row and per-group aggregates, `alias()` and how to port Django code that aggregates.

## Shaping the result

`values()` and `values_list()` return dicts or tuples instead of model instances:

```python
Query("club.Member").values("name", "club__name")
Query("club.Member").values_list("name", "age")
Query("club.Member").values_list("pk", flat=True)
Query("club.Member").values_list("name", "age", named=True)
```

`only()` and `defer()` choose which fields of model rows are loaded; a deferred field raises `AttributeError` on
access. `select_related()` loads related rows in the same query:

```python
Query("club.Member").only("name", "email")
Query("club.Member").defer("photo")
Query("club.Member").select_related("club", "club__owner")
```

`none()` returns no rows; it is useful as the start of a combination built in a loop.

## Combining queries

`|` and `&` combine two queries on the same model into one whose condition is the OR or the AND of theirs. `&`
keeps the filters of both, as chained `filter()` calls do; `|` joins both conditions into one filter:

```python
adults = Query("club.Member").filter(age__gte=18)
guests = Query("club.Member").filter(guest=True)
(adults | guests).order_by("name")

combined = Query("club.Member").none()
for club in clubs:
    combined |= Query("club.Member").filter(club__name=club)
```

## Locking rows

`select_for_update()` locks the selected rows until the end of the transaction that reads them:

```python
Query("club.Member").filter(pk=1).select_for_update(nowait=True)
Query("club.Member").select_related("club").select_for_update(of=("self",), skip_locked=True)
```

The [query reference](/ref/query) documents `nowait`, `skip_locked`, `of` and `no_key`.

## Running queries

`Model.objects` returns the model bound to the current [session](/topics/sessions); its query methods return a
[`QuerySet`](/ref/queryset), which has every building method of `Query` and adds the terminal coroutines that run
one operation each:

```python
members = Member.objects.filter(active=True).order_by("name")

async for member in members[:20]:            # Select
    ...
await members.get(pk=5)                      # Get
await members.count()                        # Count
await members.filter(age__lt=18).update(junior=True)   # Update
```

## From queries to operations

A query describes rows; an [operation](/ref/operations) says what to do with them. Each read or write is one
operation, the unit a driver executes:

```python
from adorm.operations import Count, Delete, Get, Select, Update

members = Query("club.Member").filter(active=True)

Select(members[:20])
Get(members.filter(pk=5))
Count(members)
Update(members.filter(age__lt=18), {"junior": True})
Delete(members.filter(joined__lt=cutoff))
```

An operation converts to JSON-safe data with its values typed by the model schema, so it can travel to an adorm
server, which decodes each value as its field's type; see [Operation encoding](/ref/encoding).

## Construct names

A query is a tree of nodes: `walk()` visits them and `constructs()` names every construct the tree uses, the names
a driver declares in `Capabilities.constructs` (see [Capabilities](/topics/drivers#capabilities)):

```python
query = Query("club.Member").filter(name__icontains="a").order_by(Lower("name")).distinct()
sorted(set(query.constructs()))
# ['clause.distinct', 'function.Lower', 'lookup.icontains']
```
