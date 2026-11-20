# Making queries

A query is built from adorm's own building blocks (`Q`, `F`, lookups, functions, aggregates, other expressions)
into an immutable [`Query`](/ref/query). [Operations](/ref/operations) read and write with it, and a
[`QuerySet`](/ref/queryset) runs them on a session. Building a query opens no connection, imports no ORM and needs no
model class. A driver translates the query into its target's constructs (Django lookups and expressions, SQL,
SQLAlchemy Core). The [Django driver](/topics/django-driver) and the
[native PostgreSQL driver](/topics/postgresql-driver) execute every construct on this page.

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

Every building block is immutable. A method of `Query` returns a new query and leaves the original unchanged; a
query can be stored at module level, shared between tasks and refined independently:

```python
active = Query("club.Member").filter(active=True)
adults = active.filter(age__gte=18)
minors = active.filter(age__lt=18)

assert active == Query("club.Member").filter(active=True)   # unchanged
```

Nodes compare by value and are hashable:

```python
cache = {adults: "adults"}
assert cache[Query("club.Member").filter(active=True).filter(age__gte=18)] == "adults"
```

Setting an attribute of a node raises `AttributeError`. Iterables passed to `in`, `range` and `overlap` are copied
into tuples; changing the original list does not change the query.

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
Query("club.Club").filter(disciplines__overlap=["Lead", "Dinghy"])   # an array field sharing an item
```

`name=None` and `name__iexact=None` match NULL, as `name__isnull=True` does; at a key inside a JSON field they match
JSON `null` (below). Any other lookup with `None` raises `ValueError`.
The [lookup reference](/ref/lookups) lists every lookup.

### Transforms

A date, time or datetime field path continues with a transform before the lookup, as in Django: `year`, `iso_year`,
`quarter`, `month`, `week`, `week_day`, `iso_week_day`, `day`, `hour`, `minute`, `second`, and `date` and `time`
of a datetime. Transforms chain, and also apply in `F()`, `values()`, `values_list()` and `order_by()`:

```python
Member.objects.filter(birth_date__year=1982)
Member.objects.filter(birth_date__year__gte=1990, birth_date__month__lte=6)
Member.objects.exclude(birth_date__month__in=[1, 2])        # keeps rows whose birth_date is NULL
Member.objects.filter(joined__date=date(2026, 3, 2))
Member.objects.filter(joined__hour__in=[7, 8], joined__date__year=2026)
Member.objects.filter(joined__month=F("birth_date__month"))
Member.objects.values_list("first_name", "birth_date__year")
Member.objects.order_by("-joined__hour")
```

A transform that does not apply to the field (`birth_date__hour` on a date, `first_name__year`) and an unknown name
raise `FieldError` with a suggestion (`joined__yeer`: `perhaps you meant 'year'`). The
[transform reference](/ref/lookups#transforms) lists them and shows how to register a custom one.

### JSON fields

A path continues into the value of a JSON field with keys and list indexes; `has_key`, `has_keys`, `has_any_keys`,
`contains` and `contained_by` test the value's structure:

```python
Club.objects.filter(address__owner__name="Ana")
Club.objects.filter(address__owner__name__icontains="an")
Club.objects.filter(address__items__0="boat")              # the first item of a list
Club.objects.filter(address__items__1__isnull=True)        # a list with fewer than two items, or no list
Club.objects.filter(address__has_key="owner")
Club.objects.filter(address__has_any_keys=["note", "rating"])
Club.objects.filter(address__contains={"owner": {"name": "Ana"}})
Member.objects.filter(tags__contained_by=["junior", "coach"])
Club.objects.values_list("slug", "address__owner__name")
```

After a JSON field every segment but a trailing lookup name is a key, a key named like a transform included
(`address__year`). `address__note=None` matches a key holding JSON `null`, `address__note__isnull=True` a missing key.
On the JSON field itself, `address=None` matches SQL NULL, as `address__isnull=True` does.

### Relation traversal

A field path crosses relations with `__`, as in Django. The last segment is the lookup when it names a registered
lookup; otherwise the whole key is the field path:

```python
Query("club.Member").filter(club__name="Chess")               # club → name, exact
Query("club.Member").filter(club__owner__email__iendswith=".si")
Query("club.Club").filter(member__age__lt=18)                  # reverse relation
```

A field whose name equals a lookup name takes an explicit lookup: `filter(range__exact=5)`.

Conditions on a multi-valued relation (a reverse foreign key, a many-to-many relation) in one `filter()` call apply
to the same related row; conditions in separate calls may each match a different one, as in Django:

```python
Query("club.Club").filter(member__age__lt=18, member__guest=True)    # a member who is a minor guest
Query("club.Club").filter(member__age__lt=18).filter(member__guest=True)  # a minor member and a guest member
```

### Queries as subqueries

`in` and `overlap` take a query instead of a list; the query becomes a subquery. It selects one column with
`values()`, or the primary key when it selects none:

```python
active_clubs = Query("club.Club").filter(active=True)
Query("club.Member").filter(club__in=active_clubs)                       # club in (select id ...)
Query("club.Member").filter(email__in=Query("club.Invite").values("email"))
Query("club.Club").filter(disciplines__overlap=Query("club.Event").values("discipline"))   # && array(select ...)
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

`Q()` is empty and falsy; combining it with another condition returns the other one:

```python
condition = Q()
for word in words:
    condition &= Q(name__icontains=word)
members = Query("club.Member").filter(condition)
```

## Lookup keys built at run time

A lookup key is a string built at run time, for example from a filter form, a query string or a viewset. It goes
into `filter()`, `exclude()`, `get()` or `Q` as a keyword argument or as a `(key, value)` pair and builds the same
lookup as the literal keyword:

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

Building a query checks only the key's syntax: segments are identifiers joined with `__`, a segment after the first
may also be a list index (`"tags__0"`); any other key (`"name__"`, `"0__name"`) raises `ValueError`. Whether the
fields exist is checked against the model's schema:
[`resolve_lookup()`](/ref/lookups#adorm-query-resolve-lookup-target-key-annotations-source-none) checks one key,
[`check_lookups()`](/ref/lookups#adorm-query-check-lookups-target-condition-annotations-source-none) a whole condition.
Both raise [`FieldError`](/ref/exceptions#adorm-fielderror-message-none-model-none-key-none-original-none-sqlstate-none)
without running an operation or taking a slot:

```python
from adorm.query import resolve_lookup

resolve_lookup(Member, "first_name__icontain")
# FieldError: unsupported lookup 'icontain' for clubs.Member.first_name; perhaps you meant 'icontains'
resolve_lookup(Member, "memberships__club__city__icontains").field_type   # str (with its max length)
resolve_lookup(Member, "level__in").value_type     # Literal['beginner', 'intermediate', 'advanced']
```

A path that ends at a relation takes only `exact`, `gt`, `gte`, `lt`, `lte`, `in` and `isnull` (`club__in`). After a
scalar field a path continues only with [transforms](#transforms) that apply to it. An unchecked key that does not
resolve raises `FieldError` from the driver when it builds the statement; the error does not affect the
[transaction](/topics/transactions#a-failed-operation) it is raised in.

## Referring to fields with `F`

`F("field")` refers to a field of the queried row, for comparisons between fields and arithmetic in the database:

```python
from adorm import F

Query("club.Event").filter(ends__gt=F("starts"))
Query("club.Member").filter(paid__lt=F("fee") * 0.5)
Query("club.Member").annotate(age_next_year=F("age") + 1)
```

`F` paths cross relations and apply transforms as lookups do: `F("club__owner__name")`, `F("joined__year")`.

## Functions, aggregates and conditional expressions

Functions take field paths as strings; a literal string is `Value("...")`:

```python
from adorm import Coalesce, Concat, Lower, Value

Query("club.Member").order_by(Lower("name"))
Query("club.Member").annotate(display=Concat("first_name", Value(" "), "last_name"))
Query("club.Member").annotate(label=Coalesce("nick", "name"))
```

adorm has the database functions of Django's `django.db.models.functions` except the window functions: comparison
and conversion (`Cast`, `Collate`, `Greatest`, `Least`, `NullIf`, `Coalesce`), JSON (`JSONArray`, `JSONObject`),
dates and times (`Extract…`, `Trunc…`, `Now`), math (`Abs`, `Round`, `Power`, `Sqrt` …), text (`Left`, `Substr`,
`Replace`, `LPad`, `MD5` …) and UUIDs (`UUID4`, `UUID7`). A function stands wherever an expression can: in
`annotate()`, `filter()`, `order_by()`, `update()` and as an aggregate's argument; `values()` selects its result by
the annotation name.

```python
from adorm import Cast, ExtractYear, Left, Round, Value

Query("club.Member").annotate(born=ExtractYear("birth_date")).filter(born__gte=2000)
Query("club.Member").order_by(Left("last_name", 1), Cast("joined", datetime.date))
Query("club.Activity").update(capacity=Round(F("capacity") * Value(1.1)))
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

### Own functions, lookups and transforms

An application adds functions, lookups and transforms of its own. A `Func`, `Lookup` or `Transform` subclass with a
SQL `template` (or `templates`, one per database vendor) runs on the drivers that render SQL; for SQL a template
cannot express, a translation into a Django expression registered with `register_translation()` runs on the Django
driver and the native PostgreSQL driver. A driver declares such a construct from the moment the class or the
translation exists; on a driver or vendor without it the query raises `NotSupportedError` before it runs.

```python
class Monogram(Func):
    arity = 2
    default_output_type = str
    template = "SUBSTR(%(expressions)s, 1, 1)"
    arg_joiner = ", 1, 1) || SUBSTR("


Member.objects.annotate(m=Monogram("first_name", "last_name"))
```

The recipe [Own expressions](/howto/own-expressions) shows each kind; `UUID4` and `UUID7` use the same mechanism.

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

`none()` returns no rows, for example as the start of a combination built in a loop.

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

## Set operations

`union()`, `intersection()` and `difference()` combine the rows of several queries as SQL's `UNION`, `INTERSECT`
and `EXCEPT`. Rows are compared column by column: the queries select the same number of columns of matching types,
on the same or different models. `union()` removes duplicate rows unless `all=True`;
`intersection()` and `difference()` always remove them:

```python
advanced = Member.objects.filter(level="advanced").values_list("first_name", flat=True)
coaches = Member.objects.filter(tags__contains=["coach"]).values_list("first_name", flat=True)

await advanced.union(coaches).count()
[name async for name in advanced.intersection(coaches)]
[name async for name in advanced.difference(coaches).order_by("first_name")]

cities = Club.objects.values_list("city", flat=True)
[value async for value in cities.union(advanced, all=True)]
```

The combined rows have the row format of the first query: model instances of its model when it selects model rows.
After a set operation only `order_by()`, slicing, `values()` / `values_list()` and the reads (`async for`, `get()`
without conditions, `first()`, `last()`, `count()`, `exists()`) apply to the combined rows; another set operation
nests the combination. `filter()`, `annotate()`, `distinct()`, `only()`, `select_related()`, `prefetch_related()`,
`aggregate()`, `update()`, `delete()` and the other methods raise `NotSupportedError`, as in Django:

```python
members = Member.objects.filter(level="advanced").union(Member.objects.filter(tags__contains=["junior"]))
[member.first_name async for member in members.order_by("-first_name")[:3]]
[name async for name in members.values_list("first_name", flat=True)]
members.filter(club_id=1)         # NotSupportedError: cannot call filter() after union()
```

`values()` or `values_list()` after a set operation selects those fields in every combined query that selects model
rows. After `values()` or `values_list()` the ordering names columns of the combined rows, by the names the last
`values()` gives; combined model rows are ordered by fields of the first query's model. Empty queries
(`none()`, `qs[0:0]`) are combined as Django combines them ([Empty queries](/ref/query#empty-queries)). A
combination inside a filter is a subquery; there each combined query of model rows selects its primary key:

```python
juniors = Member.objects.filter(tags__contains=["junior"]).values("pk")
Member.objects.filter(pk__in=Member.objects.filter(level="advanced").values("pk").union(juniors))
```

## Locking rows

`select_for_update()` locks the selected rows until the end of the transaction that reads them:

```python
Query("club.Member").filter(pk=1).select_for_update(nowait=True)
Query("club.Member").select_related("club").select_for_update(of=("self",), skip_locked=True)
```

The [query reference](/ref/query) documents `nowait`, `skip_locked`, `of` and `no_key`.

## Running queries

`Model.objects` returns the model bound to the current [session](/topics/sessions). Its query methods return a
[`QuerySet`](/ref/queryset): every building method of `Query` plus terminal coroutines that run one operation each
(and the prefetches of `prefetch_related()`):

```python
members = Member.objects.filter(active=True).order_by("name")

async for member in members[:20]:            # Select
    ...
await members.get(pk=5)                      # Get
await members.count()                        # Count
await members.filter(age__lt=18).update(junior=True)   # Update
```

## Prefetching related objects

`select_related()` joins forward relations into the query. `prefetch_related()` reads related objects of any
relation with separate operations after the query's own, one per relation and level, and caches them on the
instances; a loop over the instances runs no further operation:

```python
clubs = Club.objects.order_by("slug").prefetch_related("memberships__member", "activities")
async for club in clubs:                                   # clubs, memberships, members, activities
    async for membership in club.memberships:             # the prefetched rows
        member = await membership.member                  # from the cache
    total = await club.activities.count()                 # the prefetched rows, no Count
```

A [`Prefetch`](/ref/queryset#adorm-prefetch-lookup-queryset-none-to-attr-none) object chooses the related rows with a
query set and can store them under another name (`to_attr`, a list or an instance on each object); a later lookup
continues from that name:

```python
from adorm import Prefetch

attended = Attendance.objects.filter(status="attended").order_by("member_id")
activities = Activity.objects.prefetch_related(
    Prefetch("attendances", queryset=attended, to_attr="attended"),
    "attended__member",
)
async for activity in activities:
    names = [(await attendance.member).first_name for attendance in activity.attended]
```

A generic foreign key is prefetched with one operation per related model, after one for the content types that
[`adorm.content_types`](/ref/models#adorm-contenttypes) has not cached. A next segment continues on each model's
relation of that name.
[`GenericPrefetch`](/ref/queryset#adorm-genericprefetch-lookup-querysets-to-attr-none) gives a query set per model:

```python
lines = InvoiceLine.objects.prefetch_related(Prefetch("item", to_attr="billed"), "billed__club")
async for line in lines:
    print(type(line.billed).__name__, (await line.billed.club).slug)     # "Membership koper-sailing" …
```

A prefetch is a sequence of `Select` operations that run one after another on the query set's session. Inside a
transaction they use its slot and read its writes; outside one, each operation takes and returns a slot. A relation with
more keys than [`BATCH_SIZE`](/ref/queryset#adorm-models-prefetch-batch-size) is read with one operation per batch.
[`prefetch_related_objects()`](/ref/queryset#adorm-prefetch-related-objects-instances-lookups-executor-none) prefetches
for instances read earlier.

## Rows by key

`in_bulk()` returns the instances whose primary key (or another unique field) is in a list, as a dict by that value:

```python
clubs = await Club.objects.in_bulk([1, 2, 99])                       # {1: <Club>, 2: <Club>}
by_slug = await Club.objects.in_bulk(["koper-sailing"], field_name="slug")
every = await Club.objects.filter(city="Koper").in_bulk()            # every row, by primary key
```

## From queries to operations

A query describes rows; an [operation](/ref/operations) is one read or write of them, the unit a driver
executes:

```python
from adorm.operations import Count, Delete, Get, Select, Update

members = Query("club.Member").filter(active=True)

Select(members[:20])
Get(members.filter(pk=5))
Count(members)
Update(members.filter(age__lt=18), {"junior": True})
Delete(members.filter(joined__lt=cutoff))
```

An operation converts to JSON-safe data with its values typed by the model schema; decoding restores each value as
its field's type. See [Operation encoding](/ref/encoding).

## Construct names

A query is a tree of nodes. `walk()` visits them; `constructs()` names every construct the tree uses, as a driver
declares them in `Capabilities.constructs` (see [Capabilities](/topics/drivers#capabilities)):

```python
query = Query("club.Member").filter(name__icontains="a").order_by(Lower("name")).distinct()
sorted(set(query.constructs()))
# ['clause.distinct', 'function.Lower', 'lookup.icontains']
```

A set operation adds `clause.union`, `clause.intersection` or `clause.difference` to the construct names of its
queries, and `clause.combinator.slice` when one of them is sliced. A field path with several segments reports
`path.nested`, and each [transform](#transforms) it applies `transform.<name>`, resolved against the model registry:

```python
sorted(set(Member.objects.filter(birth_date__year__gte=1990).query.constructs()))
# ['lookup.gte', 'path.nested', 'transform.year']
```
