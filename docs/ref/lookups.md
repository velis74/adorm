# Lookups

Field lookups and transforms, importable from `adorm.query` (defined in `adorm.query.lookups`). [Making
queries](/topics/queries#field-lookups) shows them in context.

```python
from adorm.query import (
    LOOKUPS, TRANSFORMS, Exact, IContains, In, ResolvedLookup, Transform, build_lookup, check_lookups,
    register_lookup, register_transform, resolve_lookup, split_lookup,
)
```

## Lookup keys

A lookup key is a field path followed by an optional lookup name, joined with `__`:

| Key | Field path | Lookup |
|---|---|---|
| `name` | `name` | `exact` |
| `name__icontains` | `name` | `icontains` |
| `club__name` | `club__name` | `exact` |
| `club__owner__name__startswith` | `club__owner__name` | `startswith` |
| `range` | `range` | `exact` (a single segment is always a field) |
| `range__exact` | `range` | `exact` |
| `joined__year` | `joined__year` (the field `joined`, then the [transform](#transforms) `year`) | `exact` |
| `joined__date__gte` | `joined__date` | `gte` |
| `address__owner__name` | `address__owner__name` (the keys `owner`, `name` inside the JSON field `address`) | `exact` |
| `tags__0` | `tags__0` (the first item of the JSON list `tags`) | `exact` |
| `address__has_key` | `address` | `has_key` |

The last segment is the lookup name when the key has more than one segment and the segment is a registered lookup.
A field named like a lookup needs an explicit lookup (`range__exact`). Segments are identifiers; a segment after the
first may also be a list index (digits: `tags__0`). A key with an empty segment (`a__`), a segment containing `__`
(`a____b`) or a first segment that is not an identifier (`0__name`) raises `ValueError`.

A separator is the last `__` of a run of underscores, so a segment may end in `_`, as names in
[generated models](/howto/generating-models#name-clashes) do:

| Key | Field path | Lookup |
|---|---|---|
| `json___icontains` | `json_` | `icontains` |
| `copy___title` | `copy___title` (`copy_`, then `title`) | `exact` |

### `adorm.query.split_lookup(key)`

Splits a key into its field path and lookup name.

```python
split_lookup("club__name__icontains")   # ('club__name', 'icontains')
split_lookup("club__name")              # ('club__name', 'exact')
```

### `adorm.query.build_lookup(key, value)`

The lookup node for `filter(key=value)`. `Q` and `Query.filter()` build their lookups with it.

```python
build_lookup("club__name__icontains", "chess")   # IContains(F('club__name'), 'chess')
build_lookup("club", None)                       # Exact(F('club'), None)
```

### Keys built at run time

A key built at run time is passed as a keyword argument or as a `(key, value)` pair; both build the same lookup
as the literal keyword:

```python
key = f"{field}__{op}"                           # "club__name__icontains"
Query("club.Member").filter(**{key: value})
Query("club.Member").exclude(Q((key, value)))
Q(*params.items(), _connector=Q.OR)              # pairs keep their order; keyword arguments are sorted
```

Splitting and building check only the key's syntax.
[`resolve_lookup()`](#adorm-query-resolve-lookup-target-key-annotations-source-none) and
[`check_lookups()`](#adorm-query-check-lookups-target-condition-annotations-source-none) check a key or a condition
against the model's schema without running an operation; otherwise the driver raises
[`FieldError`](./exceptions#adorm-fielderror-message-none-model-none-key-none-original-none-sqlstate-none) when it
builds the statement. A misspelt lookup name is parsed as a field: `first_name__icontain` is the path
`first_name__icontain` with `exact`, which the schema refuses.

## Resolving keys

### `adorm.query.resolve_lookup(target, key, *, annotations=(), source=None)`

Resolves `key` against `target` and returns a [`ResolvedLookup`](#adorm-query-resolvedlookup). Runs no operation
and needs no session.

| Argument | Meaning |
|---|---|
| `target` | a model class, a model label, or a [`Query`](./query) or [`QuerySet`](./queryset), whose annotation and alias names a key may also start with; anything else raises `TypeError`, a malformed label `ValueError` |
| `annotations` | further annotation names, or a mapping of annotation names to their types |
| `source` | the [`SchemaSource`](./encoding#adorm-schema-schemasource) the models are resolved in; the model registry (every model class with `_meta`) by default |

The field path is relations, then a field. After a JSON or record field (`dict`, `list`, `Any`, a pydantic model or a
`TypedDict`, also inside `Optional`) the segments are keys inside its value, a key named like a transform included
(`address__year`). After any other field they are [transforms](#transforms), each applying to the value the previous
one gives (`joined__date__year`). A path starting with an annotation continues with transforms, unless the
annotation's type is a JSON or record type, or is unknown and a following segment is not a transform name: then with
keys. `pk` stands for the primary key at any segment. A key that is not a lookup key raises `ValueError`, as in
`split_lookup()`.

[`FieldError`](./exceptions#adorm-fielderror-message-none-model-none-key-none-original-none-sqlstate-none) is raised
for:

| Key | Message |
|---|---|
| `frist_name__icontains` | `clubs.Member has no field or relation 'frist_name'; perhaps you meant 'first_name'` |
| `memberships__rol` | `clubs.Membership has no field or relation 'rol'; perhaps you meant 'role'` |
| `first_name__icontain` | `unsupported lookup 'icontain' for clubs.Member.first_name; perhaps you meant 'icontains'` |
| `joined__yeer` | `unsupported lookup 'yeer' for clubs.Member.joined; perhaps you meant 'year'` |
| `joined__yeer__gte`, `joined__yeer__exact` | `unsupported transform 'yeer' for clubs.Member.joined; perhaps you meant 'year'` |
| `birth_date__year__gtee` | `unsupported lookup 'gtee' for clubs.Member.birth_date__year; perhaps you meant 'gte'` |
| `birth_date__hour`, `first_name__year` | `unsupported transform 'hour' for clubs.Member.birth_date`: a transform whose `applies_to()` refuses the field (`hour` of a date) |
| `first_name__has_key` | `unsupported lookup 'has_key' for clubs.Member.first_name` |
| `memberships__club__icontains` | `unsupported lookup 'icontains' for the relation clubs.Membership.club; a relation takes exact, gt, gte, lt, lte, in, isnull` |
| `first_name__overlap`, `tags__overlap` | `unsupported lookup 'overlap' for clubs.Member.first_name` (`.tags`): a lookup whose `applies_to()` refuses the field |

A suggestion names a transform that applies at that point, or for the last segment of a key without an explicit lookup
also a lookup. A misspelt JSON lookup after a JSON field is a key (`address__has_kye` compares the key `has_kye`), as
in Django.

A path that ends at a relation compares the target's primary key and takes only the lookups whose `on_relations`
is true: `exact`, `gt`, `gte`, `lt`, `lte`, `in` and `isnull` (`club__in`, `memberships__isnull`). The operation
codec types field paths with the same resolution
([`resolve_field_type()`](./encoding#adorm-schema-resolve-field-type-source-label-path)).

At a model `source` does not know, the check stops and accepts the rest of the path unresolved. A relation the model
does not declare (one [`adorm generate`](/howto/generating-models) leaves out) does not resolve: the check refuses
such a key even when the driver could run it.

```python
from adorm.query import resolve_lookup

resolved = resolve_lookup(Member, "memberships__club__city__icontains")
resolved.model, resolved.field, resolved.lookup     # ('clubs.Club', 'city', IContains)
resolve_lookup(Club, "address__street").keys        # ('street',): a key inside the JSON field address
resolve_lookup(Member, "joined__date__gte").transforms   # ('date',)
resolve_lookup(Member.objects.annotate(n=Count("memberships")), "n__gte").field   # None: an annotation
```

A filter built from request parameters, checking each key before it builds the query; a wrong key is a 400
response and never reaches a slot:

```python
from pydantic import TypeAdapter

async def search(params: dict[str, str]) -> list[Member]:
    condition = Q()
    for key, raw in params.items():
        try:
            resolved = resolve_lookup(Member, key)
        except adorm.FieldError as exc:
            raise HTTPException(400, str(exc)) from exc
        value_type = str if resolved.value_type is None else resolved.value_type   # None: a key inside JSON
        if resolved.lookup.rhs_is_sequence:
            value = TypeAdapter(list[value_type]).validate_python(raw.split(","))
        else:
            value = TypeAdapter(value_type).validate_python(raw)
        condition &= Q((key, value))
    return [member async for member in Member.objects.filter(condition)]
```

### `adorm.query.ResolvedLookup`

A frozen dataclass, the result of `resolve_lookup()`:

| Attribute | Value |
|---|---|
| `key` | the key as given |
| `path` | the segments of the key's field path (`pk` as given) |
| `lookup` | the lookup class |
| `model` | the label of the model whose field or relation the path ends at; `None` for an annotation or after a model the source does not know |
| `field` | that field or relation (`pk` resolved to the primary key's name), `None` likewise |
| `field_type` | the type of the compared values: the field's type hint, the target's primary key type for a relation, an annotation's given type, the `output_type()` of the last transform; `None` when unknown, always when `keys` is not empty |
| `keys` | the segments after a JSON or record field, `()` otherwise |
| `transforms` | the names of the transforms after the field or annotation, in order; `()` without |
| `value_type` | a property: the type of a right-hand side value (of each value for `in`, `range` and `overlap`), `lookup.rhs_type(field_type)` |

```python
resolve_lookup(Member, "pk__in")
# ResolvedLookup(key='pk__in', path=('pk',), lookup=In, model='clubs.Member', field='id',
#                field_type=int | None, keys=())
resolve_lookup(Member, "birth_date__isnull").value_type    # bool
resolve_lookup(Member, "joined__startswith").value_type    # str: a text pattern
resolve_lookup(Club, "disciplines__overlap").value_type    # str: an item of the list[str] field
resolve_lookup(Member, "joined__year__in").value_type      # int: a year
resolve_lookup(Member, "joined__date").value_type          # datetime.date
```

### `adorm.query.check_lookups(target, condition, *, annotations=(), source=None)`

Resolves every lookup and field reference of `condition` (a `Q`, a lookup or another expression) against `target`,
with the arguments of `resolve_lookup()`, and raises
[`FieldError`](./exceptions#adorm-fielderror-message-none-model-none-key-none-original-none-sqlstate-none) for the
first that does not resolve; anything other than a query node raises `TypeError`. A lookup's left-hand side `F` is
resolved with the lookup, every other `F` (a right-hand side, a function argument, a `When` condition) as a field
path, transforms included (`F("joined__year")`). An `OuterRef` and the nodes of a query inside the condition (a
`Subquery`, `Exists`, an `in` query) are not checked.

```python
from adorm.query import check_lookups

condition = Q(**{f"{field}__{op}": value}) | Q(last_name=F("email"))
check_lookups(Member, condition)                  # FieldError when a key does not resolve
members = Member.objects.filter(condition)
```

## Built-in lookups

| Lookup | Class | Condition | Right-hand side |
|---|---|---|---|
| `exact` | `Exact` | equal | any value; `None`: NULL, JSON `null` at a JSON key |
| `iexact` | `IExact` | equal, ignoring case | any value; `None` as for `exact` |
| `gt` | `GreaterThan` | greater than | any value |
| `gte` | `GreaterThanOrEqual` | greater than or equal | any value |
| `lt` | `LessThan` | less than | any value |
| `lte` | `LessThanOrEqual` | less than or equal | any value |
| `in` | `In` | one of the values | an iterable (stored as a tuple without `None`) or a query |
| `range` | `Range` | between two values, inclusive | an iterable of exactly two values |
| `isnull` | `IsNull` | NULL (`True`) or not NULL (`False`) | `True` or `False` |
| `startswith` | `StartsWith` | starts with | any value |
| `istartswith` | `IStartsWith` | starts with, ignoring case | any value |
| `endswith` | `EndsWith` | ends with | any value |
| `iendswith` | `IEndsWith` | ends with, ignoring case | any value |
| `contains` | `Contains` | contains | any value |
| `icontains` | `IContains` | contains, ignoring case | any value |
| `regex` | `Regex` | matches the regular expression (the database's syntax) | a string or an expression |
| `iregex` | `IRegex` | matches the regular expression, ignoring case | a string or an expression |
| `overlap` | `Overlap` | an array with at least one item in common | an iterable (stored as a tuple without `None`), a query or an array expression |
| `contained_by` | `ContainedBy` | a JSON value or array contained in the right-hand side | a JSON value or a list |
| `has_key` | `HasKey` | a JSON object with the key | a key name |
| `has_keys` | `HasKeys` | a JSON object with every key | an iterable of key names |
| `has_any_keys` | `HasAnyKeys` | a JSON object with at least one of the keys | an iterable of key names |

Except for `isnull`, `regex`, `iregex`, `has_key`, `has_keys` and `has_any_keys`, a right-hand side may also be an
expression (`F("other")`,
`Lower("name")` …) or a query (a [`Query`](./query) or an object whose `query` attribute is one), which becomes a
[`Subquery`](./expressions#subqueries). `None` raises `ValueError` except where the table says otherwise. A plain
value is copied when the lookup is built; later changes to the caller's list or dict do not affect the lookup. A
value that cannot be compared (a `bytearray`, an object without `__hash__` that is not a list, tuple, set or dict)
raises `TypeError`.

### `adorm.query.Lookup(lhs, rhs)`

Base class of lookups: a condition (`conditional` true, `output_type` `bool`) on `lhs` with the right-hand side
`rhs`. `lhs` is an expression; a string is a field path. Instantiating `Lookup` itself raises `TypeError`. A
subclass sets `lookup_name`, which gives it the construct name `lookup.<lookup_name>`, and may override
`prepare_rhs(value)` to validate and normalise the right-hand side.

Class members that type the right-hand side for the [operation codec](./encoding) and check the field:

| Member | Meaning |
|---|---|
| `rhs_is_sequence` | `True` when `rhs` is a tuple of values, each typed on its own (`in`, `range`, `overlap`, `has_keys`, `has_any_keys`) |
| `applies_to(field_type, keys)` | whether the lookup applies to a field of `field_type` (`None` when unknown) or to the keys `keys` inside a JSON or record field; `True` by default, for `overlap` only an array field or a field of unknown type; `resolve_lookup()` and `check_lookups()` raise `FieldError` when it is false |
| `rhs_type(field_type)` | the type of a right-hand side value for a field of `field_type` (which may be `None`); `field_type` itself by default, `bool` for `isnull`, `str` for `regex`, `iregex` and the text pattern lookups (`None` for them when `field_type` is `None`), the item type for `overlap`; see `Contains` and `Overlap` for their exceptions |

The class attribute `on_relations` states whether the lookup applies to a path that ends at a relation (compared
by the target's primary key): `True` for `exact`, `gt`, `gte`, `lt`, `lte`, `in` and `isnull`, `False` by default.
[`resolve_lookup()`](#adorm-query-resolve-lookup-target-key-annotations-source-none) refuses the others on a
relation. The class attribute `rhs_accepts_none` states whether `None` is a right-hand side: `True` for `exact` and
`iexact`, `False` by default.

A lookup instance is a condition and is accepted wherever `Q` is, with any expression on the left.

```python
Query("club.Member").filter(Exact(Lower("name"), "ann"))
Query("club.Member").filter(GreaterThan("ends", F("starts")) | IsNull("ends", True))
```

A lookup of the application's own declares its SQL in `template`, with `%(lhs)s` and `%(rhs)s` (a literal `%` is
`%%`), or in `templates`, a mapping from Django's vendor names to templates with `template` as the fallback; the
class method `sql_template(vendor)` returns the one for a vendor, `None` without one. The drivers that render SQL
(the Django driver and the native PostgreSQL driver) run it, with the parameters of each side in the order the
template names them, and declare `lookup.<lookup_name>` on the vendors it has a template for. A template that is not
a string raises `TypeError` when the class is defined.

```python
@register_lookup
class SameStart(Lookup):
    lookup_name = "same_start"
    template = "SUBSTR(%(lhs)s, 1, 1) = SUBSTR(%(rhs)s, 1, 1)"


Query("club.Member").filter(name__same_start="Gregor")   # constructs: lookup.same_start
```

### `adorm.query.Exact(lhs, rhs)`

The attribute `implicit` is `True` on an `Exact` that `build_lookup()` built from a key without a lookup name
(`joined__yeer`); an unknown last segment of its path is then reported as a lookup. It is not part of the node's value
(equality, hash, encoding).

### `adorm.query.IExact(lhs, rhs)`

### `adorm.query.GreaterThan(lhs, rhs)`

### `adorm.query.GreaterThanOrEqual(lhs, rhs)`

### `adorm.query.LessThan(lhs, rhs)`

### `adorm.query.LessThanOrEqual(lhs, rhs)`

### `adorm.query.In(lhs, rhs)`

`rhs` is an iterable of values or expressions, or a query selecting one column (no column selected means the primary
key). A string, bytes, a mapping or another expression raises `TypeError`. An empty iterable matches no row.

The values are stored as a tuple without `None`, which never matches. A set is sorted when its values are
orderable, so equal sets give equal lookups. Each value is prepared like a single right-hand side: a query becomes a
`Subquery`; a condition (`Q`, a lookup, `Exists`) or another node that is not an expression raises `TypeError`.

```python
Query("club.Member").filter(pk__in=[1, 2, 3])
Query("club.Member").filter(club__in=Query("club.Club").filter(active=True))
Query("club.Member").filter(email__in=Query("club.Invite").values("email"))
```

### `adorm.query.Range(lhs, rhs)`

```python
Query("club.Member").filter(joined__range=(date(2026, 1, 1), date(2026, 6, 30)))
```

### `adorm.query.IsNull(lhs, rhs)`

Any value other than `True` or `False` raises `ValueError`.

```python
Query("club.Member").filter(birth_date__isnull=True)
```

### `adorm.query.StartsWith(lhs, rhs)`

### `adorm.query.IStartsWith(lhs, rhs)`

### `adorm.query.EndsWith(lhs, rhs)`

### `adorm.query.IEndsWith(lhs, rhs)`

### `adorm.query.Contains(lhs, rhs)`

### `adorm.query.IContains(lhs, rhs)`

The text pattern lookups `iexact`, `startswith`, `istartswith`, `endswith`, `iendswith`, `contains` and `icontains`
compare the field as text; their right-hand side is typed as a string for any field type
(`token__startswith="3f2a"` on a UUID field). `contains` on a container field (a list, tuple, set, dict or `Any`)
takes a value of the field's type; on a record field (a pydantic model or `TypedDict`) a partial record, encoded with
its inferred types.

```python
Query("club.Member").filter(last_name__istartswith="nov", email__endswith="@example.com")
```

### `adorm.query.Regex(lhs, rhs)`

### `adorm.query.IRegex(lhs, rhs)`

A right-hand side that is neither a string nor an expression raises `TypeError`.

```python
Query("club.Member").filter(code__regex=r"^[A-Z]{3}-\d+$")
```

### `adorm.query.Overlap(lhs, rhs)`

An array field (Django's `ArrayField`, a `list[...]` field of a generated model) that has at least one item in
common with `rhs`; PostgreSQL's `&&`. `rhs` is one of:

| Right-hand side | Compared with |
|---|---|
| an iterable of values or expressions | the array of those values; an empty iterable matches no row |
| a query selecting one column (no column selected means the primary key) | the array of the column's values |
| an expression that is not a condition (`F("other_array")`, `OuterRef`, `Value`) | the expression's array |

A string, bytes, a mapping, a condition (`Q`, a lookup, `Exists`) or `None` raises `TypeError`. The values are
stored as a tuple as for [`In`](#adorm-query-in-lhs-rhs): without `None` (a NULL item matches nothing), a set sorted
when its values are orderable. Items compare by equality; text comparison is case-sensitive.

The [operation codec](./encoding) encodes each value as an item of the field's type: `rhs_type()` returns `X` for a
`list[X]`, `set[X]`, `frozenset[X]` or `tuple[X, ...]` field (also inside `Optional` or `Annotated`), else `None`.

```python
Club.objects.filter(disciplines__overlap=["Lead", "Keelboat"])
Club.objects.filter(disciplines__overlap={"Fitness"})
Club.objects.exclude(disciplines__overlap=["Fitness"])          # clubs without Fitness
Club.objects.filter(disciplines__overlap=Competition.objects.values("discipline"))
Book.objects.filter(keywords__overlap=["spice", F("title")])   # a value may be an expression
Book.objects.filter(keywords__overlap=Value(["spice", "worm"]))
```

`exclude()` keeps a row whose array is NULL, as in Django. A `Value` with `output_type=list` is sent to PostgreSQL as
`jsonb`, which is not an array; a `Value` without `output_type` is sent as an array.

`overlap` applies only to a field whose type is a list, set, frozenset or `tuple[X, ...]` (also inside `Optional` or
`Annotated`), or whose type is unknown. On another field, a JSON field (typed `Any`) or a key inside one,
`resolve_lookup()` and `check_lookups()` raise
[`FieldError`](./exceptions#adorm-fielderror-message-none-model-none-key-none-original-none-sqlstate-none)
(`extra__overlap`: `unsupported lookup 'overlap' for library.Book.extra`), and every driver refuses the key before it
takes a slot. The Django and the native PostgreSQL drivers also refuse it on an annotation that refers to a JSON field
(`annotate(e=F("extra"))`), where Django would read the name as a key.

### `adorm.query.ContainedBy(lhs, rhs)`

A JSON field whose value is contained in `rhs` (PostgreSQL's `<@`: every key and value of an object, every item of a
list, occurs in `rhs`), or an array field whose items all occur in the list `rhs`. It applies to a JSON, record or
array field, a key inside a JSON field and a field of unknown type; `resolve_lookup()` refuses it elsewhere. The
operation codec encodes `rhs` with the field's type for a container field, else with its inferred types.

```python
Club.objects.filter(address__contained_by={"street": "Stenska ulica 4", "city": "Maribor"})
Member.objects.filter(tags__contained_by=["junior", "coach"])     # also members without tags
```

### `adorm.query.HasKey(lhs, rhs)`

### `adorm.query.HasKeys(lhs, rhs)`

### `adorm.query.HasAnyKeys(lhs, rhs)`

A JSON object that has the key `rhs` (`has_key`), every key of `rhs` (`has_keys`) or at least one key of `rhs`
(`has_any_keys`). A key holding JSON `null` counts as present. `has_key` takes a string; `has_keys` and `has_any_keys`
take an iterable of strings, stored as a tuple; anything else raises `TypeError`. They apply to a JSON field (`Any`, a
dict or a record), a key inside one (`address__owner__has_key`) and a field of unknown type; on another field
`resolve_lookup()` raises `FieldError`. A misspelt name after a JSON field is read as a key, as in Django.

```python
Club.objects.filter(address__has_key="owner")
Club.objects.filter(address__owner__has_key="since")
Club.objects.filter(address__has_keys=["owner", "items"])
Club.objects.exclude(address__has_any_keys=["note", "rating"])
```

`contains` on a JSON field (`address__contains={"owner": {"name": "Ana"}}`) is the [`Contains`](#adorm-query-contains-lhs-rhs)
lookup: an object containing the given keys and values, a list containing the given items.

## Transforms

A transform is a function of a field's value named in a field path after the field: in a lookup key before the lookup
(`joined__year__gte`), in `F()`, `values()`, `values_list()`, `order_by()` and grouping. Transforms chain: each
applies to the value the previous one gives (`joined__date__year`).

```python
Member.objects.filter(birth_date__year=1982)
Member.objects.exclude(birth_date__month__in=[1, 2])               # also keeps members without a birth date
Member.objects.filter(joined__date=date(2026, 3, 2), joined__hour__in=[7, 8])
Member.objects.filter(birth_date__year__lt=F("joined__year") - 40)
Member.objects.values("joined__year").annotate(n=Count("pk")).order_by("joined__year")
Member.objects.order_by("-joined__hour", "first_name")
```

| Transform | Class | Applies to | Value |
|---|---|---|---|
| `year` | `Year` | a date or datetime | the year (`int`) |
| `iso_year` | `IsoYear` | a date or datetime | the ISO 8601 week-numbering year |
| `quarter` | `Quarter` | a date or datetime | the quarter, 1 to 4 |
| `month` | `Month` | a date or datetime | the month, 1 to 12 |
| `week` | `Week` | a date or datetime | the ISO 8601 week number, 1 to 53 |
| `week_day` | `WeekDay` | a date or datetime | the day of the week, 1 (Sunday) to 7 (Saturday) |
| `iso_week_day` | `IsoWeekDay` | a date or datetime | the ISO 8601 day of the week, 1 (Monday) to 7 (Sunday) |
| `day` | `Day` | a date or datetime | the day of the month |
| `hour` | `Hour` | a time or datetime | the hour, 0 to 23 |
| `minute` | `Minute` | a time or datetime | the minute |
| `second` | `Second` | a time or datetime | the second |
| `date` | `Date` | a datetime | the date (`datetime.date`) |
| `time` | `Time` | a datetime | the time (`datetime.time`) |

Every transform of a datetime is computed in the current time zone (Django's active time zone): under
`timezone.override("Asia/Tokyo")`, a member who joined at 23:30 UTC has `joined__hour` 8 and `joined__date` the next
day. Every transform also applies to a value of unknown type (an annotation without a known type). The value of a
transform of NULL is NULL: `filter(birth_date__year=None)` and `birth_date__year__isnull=True` match a NULL birth
date, and `exclude()` keeps such rows, as in Django.

Each transform has the construct name `transform.<name>`. An operation reports it for every transform a field path of
its query applies (resolved against the model registry), next to `path.nested`; a driver that does not declare it
refuses the operation with [`NotSupportedError`](./exceptions). The Django driver and the native PostgreSQL driver
declare the built-in transforms on every database they run on.

### `adorm.query.Transform`

Base class of transforms. A transform is named in field paths, not instantiated. A subclass sets `lookup_name`,
which gives it the construct name `transform.<lookup_name>`, and may override:

| Class method | Meaning |
|---|---|
| `applies_to(field_type)` | whether the transform applies to a value of `field_type`, the type without `Annotated` and `Optional` (`None` when unknown); `True` by default; `resolve_lookup()`, `check_lookups()` and the drivers raise `FieldError` when it is false |
| `output_type(field_type)` | the type of the transformed value, which types the right-hand side of the lookup after it; `None` (unknown) by default |
| `sql_template(vendor)` | the SQL template on databases of `vendor`: `templates[vendor]`, else `template`, else `%(function)s(%(expressions)s)` with a `function`; `None` without one |

A transform of the application's own declares its SQL as a function does: `function`, `template` with
`%(expressions)s` (the transformed value) or `templates` by vendor. The drivers that render SQL apply it, and the
transforms after it, as expressions, and declare `transform.<lookup_name>` on the vendors it has a template for. The
built-in transforms have no SQL attributes: the drivers apply them by name.

### `adorm.query.Year`

### `adorm.query.IsoYear`

### `adorm.query.Quarter`

### `adorm.query.Month`

### `adorm.query.Week`

### `adorm.query.WeekDay`

### `adorm.query.IsoWeekDay`

### `adorm.query.Day`

### `adorm.query.Hour`

### `adorm.query.Minute`

### `adorm.query.Second`

### `adorm.query.Date`

### `adorm.query.Time`

The built-in transforms of the table above.

### `adorm.query.TRANSFORMS`

A read-only mapping of transform name to class, holding the built-in transforms and every registered one.

### `adorm.query.register_transform(cls)`

Registers a `Transform` subclass under its `lookup_name` and returns it; usable as a class decorator. Registering the
same class again does nothing. A different class under a taken name, a name a lookup is registered under, and a name
that is not an identifier or contains `__` raise `ValueError` (`register_lookup()` likewise refuses a transform's
name). A driver must declare a transform's construct name to apply it: the Django driver and the native PostgreSQL
driver declare a transform with SQL of its own and pass one without SQL to Django by its name, which needs a Django
transform registered under that name on the field's class.

```python
@register_transform
class Unaccented(Transform):
    lookup_name = "unaccent"
    templates = {"postgresql": "UNACCENT(%(expressions)s)"}  # the unaccent extension

    @classmethod
    def applies_to(cls, field_type):
        return field_type in (str, None)

    @classmethod
    def output_type(cls, field_type):
        return str


Member.objects.filter(last_name__unaccent__icontains="zupancic")   # constructs: transform.unaccent
```

## Registry

### `adorm.query.LOOKUPS`

A read-only mapping of lookup name to class, holding the built-in lookups and every registered one.

### `adorm.query.register_lookup(cls)`

Registers a `Lookup` subclass under its `lookup_name` and returns it; usable as a class decorator. Registering the
same class again does nothing. A different class under a taken name raises `ValueError`, as does a name that is not
an identifier or contains `__`. After registration the name is parsed as a lookup in every key; a driver must declare
its construct name to run it.

```python
@register_lookup
class Similar(Lookup):
    lookup_name = "similar"


Query("club.Member").filter(name__similar="Zoe")   # constructs: lookup.similar
```

A lookup whose right-hand side has a type of its own overrides `rhs_type()`:

```python
@register_lookup
class LongerThan(Lookup):
    lookup_name = "longer_than"

    @classmethod
    def rhs_type(cls, field_type):
        return int                                     # a length, whatever the field's type


Query("club.Member").filter(name__longer_than=3)
```

A lookup that applies to relations sets `on_relations`:

```python
@register_lookup
class NotEqual(Lookup):
    lookup_name = "ne"
    on_relations = True


resolve_lookup(Membership, "club__ne")   # resolves: the relation takes ne
```
