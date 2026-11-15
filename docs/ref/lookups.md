# Lookups

Field lookups, importable from `adorm.query` (defined in `adorm.query.lookups`). [Making
queries](/topics/queries#field-lookups) shows them in context.

```python
from adorm.query import LOOKUPS, Exact, IContains, In, build_lookup, register_lookup, split_lookup
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
| `joined__year` | `joined__year` | `exact` (`year` is not a registered lookup) |

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
| `rhs_is_sequence` | `True` when `rhs` is a tuple of values, each typed on its own (`in`, `range`) |
| `rhs_type(field_type)` | the type of a right-hand side value for a field of `field_type` (which may be `None`); `field_type` itself by default, `bool` for `isnull`, `str` for `regex`, `iregex` and the text pattern lookups (`None` for them when `field_type` is `None`); see `Contains` for its exceptions |

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

### `adorm.query.Regex(lhs, rhs)`

### `adorm.query.IRegex(lhs, rhs)`

A right-hand side that is neither a string nor an expression raises `TypeError`.

```python
Query("club.Member").filter(code__regex=r"^[A-Z]{3}-\d+$")
```

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
