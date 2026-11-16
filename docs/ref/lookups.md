# Lookups

Field lookups, importable from `adorm.query` (defined in `adorm.query.lookups`). [Making
queries](/topics/queries#field-lookups) shows them in context.

```python
from adorm.query import (
    LOOKUPS, Exact, IContains, In, ResolvedLookup, build_lookup, check_lookups, register_lookup, resolve_lookup,
    split_lookup,
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
| `joined__year` | `joined__year` | `exact` (`year` is not a registered lookup, so the path continues past the scalar field `joined`, which every driver refuses with `FieldError`) |

The last segment is the lookup name when the key has more than one segment and the segment is a registered lookup.
A field whose name equals a lookup name therefore takes an explicit lookup (`range__exact`). Segments are
identifiers; a key with an empty segment (`a__`) or a segment containing `__` (`a____b`) raises `ValueError`.

A separator is the last `__` of a run of underscores, so a segment may end in `_`, as the names of
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

The lookup node for `filter(key=value)`. `exact` and `iexact` with `None` become `IsNull(path, True)`. `Q` and
`Query.filter()` build their lookups with it.

```python
build_lookup("club__name__icontains", "chess")   # IContains(F('club__name'), 'chess')
build_lookup("club", None)                       # IsNull(F('club'), True)
```

### Keys built at run time

A key is a plain string, so it can be built at run time and passed as a keyword argument or as a `(key, value)`
pair; both build the same lookup as the literal keyword:

```python
key = f"{field}__{op}"                           # "club__name__icontains"
Query("club.Member").filter(**{key: value})
Query("club.Member").exclude(Q((key, value)))
Q(*params.items(), _connector=Q.OR)              # pairs keep their order; keyword arguments are sorted
```

Splitting and building check only the key's syntax. Whether its fields exist is known to the model's schema:
[`resolve_lookup()`](#adorm-query-resolve-lookup-target-key-annotations-source-none) and
[`check_lookups()`](#adorm-query-check-lookups-target-condition-annotations-source-none) check a key or a condition
against it without running an operation; otherwise the driver raises
[`FieldError`](./exceptions#adorm-fielderror-message-none-model-none-key-none-original-none-sqlstate-none) when it
builds the statement. A misspelled lookup name is not a lookup, so it is parsed as a field: `first_name__icontain` is
the path `first_name__icontain` with `exact`, which the schema then refuses.

## Resolving keys

### `adorm.query.resolve_lookup(target, key, *, annotations=(), source=None)`

Resolves the lookup key `key` against `target` and returns a
[`ResolvedLookup`](#adorm-query-resolvedlookup); runs no operation and needs no session.

| Argument | Meaning |
|---|---|
| `target` | a model class, a model label, or a [`Query`](./query) or [`QuerySet`](./queryset), whose annotation and alias names a key may also start with; anything else raises `TypeError`, a malformed label `ValueError` |
| `annotations` | further annotation names, or a mapping of annotation names to their types |
| `source` | the [`SchemaSource`](./encoding#adorm-schema-schemasource) the models are resolved in; the model registry (every model class with `_meta`) by default |

Every segment of the field path but the last is a relation, until a JSON or record field (`dict`, `list`, `Any`, a
pydantic model or a `TypedDict`, also inside `Optional`): the segments after it are keys inside its value. `pk` stands
for the primary key at any segment. A key that is not a lookup key raises `ValueError`, as in `split_lookup()`.

[`FieldError`](./exceptions#adorm-fielderror-message-none-model-none-key-none-original-none-sqlstate-none) is raised
for:

| Key | Message |
|---|---|
| `frist_name__icontains` | `clubs.Member has no field or relation 'frist_name'; perhaps you meant 'first_name'` |
| `memberships__rol` | `clubs.Membership has no field or relation 'rol'; perhaps you meant 'role'` |
| `first_name__icontain` | `unsupported lookup 'icontain' for clubs.Member.first_name; perhaps you meant 'icontains'` |
| `joined__year__gte` | `clubs.Member.joined is neither a relation nor a JSON field; 'year' cannot follow` |
| `memberships__club__icontains` | `unsupported lookup 'icontains' for the relation clubs.Membership.club; a relation takes exact, gt, gte, lt, lte, in, isnull` |
| `first_name__overlap`, `tags__overlap` | `unsupported lookup 'overlap' for clubs.Member.first_name` (`.tags`): a lookup whose `applies_to()` refuses the field |

A path that ends at a relation compares the target's primary key, so it takes only the lookups whose `on_relations`
is true: `exact`, `gt`, `gte`, `lt`, `lte`, `in` and `isnull` (`club__in`, `memberships__isnull`). The operation
codec types field paths with the same resolution
([`resolve_field_type()`](./encoding#adorm-schema-resolve-field-type-source-label-path)).

A model `source` does not know ends the check: the rest of the path is accepted unresolved. A relation the model
does not declare (one [`adorm generate`](/howto/generating-models) leaves out) cannot be resolved, so the check
refuses a key the driver might still run.

```python
from adorm.query import resolve_lookup

resolved = resolve_lookup(Member, "memberships__club__city__icontains")
resolved.model, resolved.field, resolved.lookup     # ('clubs.Club', 'city', IContains)
resolve_lookup(Club, "address__street").keys        # ('street',): a key inside the JSON field address
resolve_lookup(Member.objects.annotate(n=Count("memberships")), "n__gte").field   # None: an annotation
```

A filter built from request parameters checks each key before it builds the query, so a wrong key is a client error
and never reaches a slot:

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
| `field_type` | the type of the compared values: the field's type hint, the target's primary key type for a relation, an annotation's given type; `None` when unknown, always when `keys` is not empty |
| `keys` | the segments after a JSON or record field, `()` otherwise |
| `value_type` | a property: the type of a right-hand side value (of each value for `in`, `range` and `overlap`), `lookup.rhs_type(field_type)` |

```python
resolve_lookup(Member, "pk__in")
# ResolvedLookup(key='pk__in', path=('pk',), lookup=In, model='clubs.Member', field='id',
#                field_type=int | None, keys=())
resolve_lookup(Member, "birth_date__isnull").value_type    # bool
resolve_lookup(Member, "joined__startswith").value_type    # str: a text pattern
resolve_lookup(Club, "disciplines__overlap").value_type    # str: an item of the list[str] field
```

### `adorm.query.check_lookups(target, condition, *, annotations=(), source=None)`

Resolves every lookup and field reference of `condition` (a `Q`, a lookup or another expression) against `target`,
with the arguments of `resolve_lookup()`, and raises
[`FieldError`](./exceptions#adorm-fielderror-message-none-model-none-key-none-original-none-sqlstate-none) for the
first that does not resolve; anything other than a query node raises `TypeError`. A lookup's left-hand side `F` is
resolved with the lookup, every other `F` (a right-hand side, a function argument, a `When` condition) as a field
path. An `OuterRef` and the nodes of a query inside the condition (a `Subquery`, `Exists`, an `in` query) refer to
other models and are not checked.

```python
from adorm.query import check_lookups

condition = Q(**{f"{field}__{op}": value}) | Q(last_name=F("email"))
check_lookups(Member, condition)                  # FieldError when a key does not resolve
members = Member.objects.filter(condition)
```

## Built-in lookups

| Lookup | Class | Condition | Right-hand side |
|---|---|---|---|
| `exact` | `Exact` | equal | any value; `None` becomes `isnull` |
| `iexact` | `IExact` | equal, ignoring case | any value; `None` becomes `isnull` |
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

Except for `isnull`, `regex` and `iregex`, a right-hand side may also be an expression (`F("other")`,
`Lower("name")` …) or a query (a [`Query`](./query) or an object whose `query` attribute is one), which becomes a
[`Subquery`](./expressions#subqueries). `None` raises `ValueError` except where the table says otherwise. A plain
value is copied when the lookup is built, so changing the caller's list or dict afterwards does not change the
lookup; a value that cannot be compared (a `bytearray`, an object without `__hash__` that is not a list, tuple, set
or dict) raises `TypeError`.

### `adorm.query.Lookup(lhs, rhs)`

Base class of lookups: a condition (`conditional` true, `output_type` `bool`) on `lhs` with the right-hand side
`rhs`. `lhs` is an expression; a string is a field path. Instantiating `Lookup` itself raises `TypeError`. A
subclass sets `lookup_name`, which gives it the construct name `lookup.<lookup_name>`, and may override
`prepare_rhs(value)` to validate and normalise the right-hand side.

Two class members tell the [operation codec](./encoding) how to type the right-hand side:

| Member | Meaning |
|---|---|
| `rhs_is_sequence` | `True` when `rhs` is a tuple of values, each typed on its own (`in`, `range`, `overlap`) |
| `applies_to(field_type, keys)` | whether the lookup applies to a field of `field_type` (`None` when unknown) or to the keys `keys` inside a JSON or record field; `True` by default, for `overlap` only an array field or a field of unknown type; `resolve_lookup()` and `check_lookups()` raise `FieldError` when it is false |
| `rhs_type(field_type)` | the type of a right-hand side value for a field of `field_type` (which may be `None`); `field_type` itself by default, `bool` for `isnull`, `str` for `regex`, `iregex` and the text pattern lookups (`None` for them when `field_type` is `None`), the item type for `overlap`; see `Contains` and `Overlap` for their exceptions |

The class attribute `on_relations` tells whether the lookup applies to a path that ends at a relation (compared by the
target's primary key): `True` for `exact`, `gt`, `gte`, `lt`, `lte`, `in` and `isnull`, `False` by default.
[`resolve_lookup()`](#adorm-query-resolve-lookup-target-key-annotations-source-none) refuses the others on a
relation.

A lookup class is itself a condition: it goes where `Q` goes, with any expression on the left.

```python
Query("club.Member").filter(Exact(Lower("name"), "ann"))
Query("club.Member").filter(GreaterThan("ends", F("starts")) | IsNull("ends", True))
```

### `adorm.query.Exact(lhs, rhs)`

### `adorm.query.IExact(lhs, rhs)`

### `adorm.query.GreaterThan(lhs, rhs)`

### `adorm.query.GreaterThanOrEqual(lhs, rhs)`

### `adorm.query.LessThan(lhs, rhs)`

### `adorm.query.LessThanOrEqual(lhs, rhs)`

### `adorm.query.In(lhs, rhs)`

`rhs` is an iterable of values or expressions, or a query selecting one column (no column selected means the primary
key). A string, bytes, a mapping or another expression raises `TypeError`. An empty iterable matches no row.

The values are stored as a tuple. `None` is left out, since it never matches; a set is sorted when its values are
orderable, so equal sets give equal lookups. Each value is prepared like a single right-hand side: a query becomes a
`Subquery`, and a condition (`Q`, a lookup, `Exists`) or another node that is not an expression raises
`TypeError`.

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
compare the field as text, so their right-hand side is typed as a string whatever the field's type
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

A string, bytes, a mapping or a condition (`Q`, a lookup, `Exists`) raises `TypeError`, and so does `None`. The
values are stored as a tuple as for [`In`](#adorm-query-in-lhs-rhs): `None` is left out, since a NULL item matches
nothing, and a set is sorted when its values are orderable. Items compare by equality, so text is case-sensitive.

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
(`extra__overlap`: `unsupported lookup 'overlap' for library.Book.extra`), so every driver refuses the key before it
takes a slot. The Django and the native PostgreSQL drivers also refuse it on an annotation that refers to a JSON field
(`annotate(e=F("extra"))`), where Django would read the name as a key.

## Registry

### `adorm.query.LOOKUPS`

A read-only mapping of lookup name to class, holding the built-in lookups and every registered one.

### `adorm.query.register_lookup(cls)`

Registers a `Lookup` subclass under its `lookup_name` and returns it, so it works as a class decorator. Registering
the same class again does nothing; a different class under a taken name raises `ValueError`, as does a name that is
not an identifier or contains `__`. A registered name is parsed as a lookup in every key from then on, and a driver
must support its construct name.

```python
@register_lookup
class Unaccent(Lookup):
    lookup_name = "unaccent"


Query("club.Member").filter(name__unaccent="Zoe")   # constructs: lookup.unaccent
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
