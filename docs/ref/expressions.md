# Expressions, functions and aggregates

Query expressions, importable from `adorm.query` (defined in `adorm.query.expressions`). The names most queries use
are also importable from `adorm`. [Making queries](/topics/queries) shows them in context.

```python
from adorm import Case, Count, Exists, F, Lower, OuterRef, Q, Subquery, Sum, Value, When
from adorm.query import CombinedExpression, Node, OrderBy, Star
```

Every node is immutable: setting or deleting an attribute raises `AttributeError`. Two nodes are equal when they
have the same class and equal attributes (`Value(1) != Value(True)`), and equal nodes have equal hashes. Building a
node does no I/O.

Each node has a construct name for driver [capabilities](/topics/drivers#capabilities), or none:

| Node | Construct name |
|---|---|
| `F` | none; [`path.nested`](./query#adorm-query-nested-path) for a path of several segments |
| `Value`, `Star`, `Q`, `When`, `OrderBy` | none |
| `OuterRef` | `expression.OuterRef`, plus `path.nested` for a path of several segments |
| `CombinedExpression` | `expression.CombinedExpression` |
| `Case`, `Subquery`, `Exists` | `expression.<class name>` |
| a `Func` subclass | `function.<class name>` |
| an `Aggregate` subclass | `aggregate.<class name>`, plus `aggregate.distinct` and `aggregate.filter` when used |
| a lookup | `lookup.<lookup name>` (see [Lookups](./lookups)) |

## Base classes

### `adorm.query.Node`

Base class of every node, including [`Query`](./query).

| Member | Meaning |
|---|---|
| `get_source_expressions()` | the child nodes |
| `walk()` | this node and all its descendants, depth first |
| `constructs()` | construct names used by this node and its descendants, possibly repeated |
| `to_dict(schema=None)` | the node as JSON-safe data, values encoded by the types `schema` gives them |
| `from_dict(data, schema=None)` | class method: the node `to_dict()` produced with the same `schema` |

```python
node = Lower(Concat("first", "last"))
list(node.walk())          # [Lower(...), Concat(...), F('first'), F('last')]
set(node.constructs())     # {'function.Lower', 'function.Concat'}

node.to_dict()             # {'node': 'Lower', 'expressions': [{'node': 'Concat', ...}], 'output_type': None}
assert Node.from_dict(node.to_dict()) == node
```

`from_dict()` on a subclass raises `ValueError` when the data encodes a node of another class. [Operation
encoding](./encoding) documents the data and how values are typed.

### `adorm.Expression`

Base class of expressions.

| Attribute | Meaning |
|---|---|
| `output_type` | the Python type of the result, `None` when it follows from the source; types the values compared with the expression when it is encoded. Where a constructor takes it, it must be a type [`adorm.codec.type_name()`](./encoding#adorm-codec-type-name-tp) names, else `TypeError` |
| `conditional` | true for conditions: `Q`, lookups, `Exists` |
| `contains_aggregate` | whether an aggregate occurs outside a subquery |

Arithmetic operators build a [`CombinedExpression`](#adorm-query-combinedexpression-lhs-connector-rhs-output-type-none);
a plain operand, a string included, becomes a `Value`. `&`, `|` and `~` combine a conditional expression into a `Q`
and raise `TypeError` on any other expression.

```python
F("price") * F("quantity")          # (F('price') * F('quantity'))
2 * F("fee") - 5
-F("balance")                       # (F('balance') * Value(-1))
Exists(payments) & Q(active=True)   # <Q: (AND: Exists(...), Exact(F('active'), True))>
```

#### `asc(*, nulls_first=None, nulls_last=None)`

#### `desc(*, nulls_first=None, nulls_last=None)`

An [`OrderBy`](#adorm-query-orderby-expression-descending-false-nulls-first-none-nulls-last-none) for this
expression.

## Fields and values

### `adorm.F(name)`

A reference to a field of the queried model. `name` is a field path: identifiers joined with `__`, relations first
(`"club__owner__name"`); anything else raises `ValueError`, a non-string `TypeError`. `path` is the path as a tuple.
A path of several segments uses the construct `path.nested`.

```python
F("club__name").path                  # ('club', 'name')
list(F("club__name").constructs())    # ['path.nested']
```

### `adorm.OuterRef(name)`

A reference to a field of the outer query inside a [`Subquery`](#adorm-subquery-query-output-type-none) or
[`Exists`](#adorm-exists-query-negated-false); a subclass of `F`. `name` is a field path or another `OuterRef`:
`OuterRef(OuterRef("pk"))` refers to the query that encloses the outer query. `levels` is the number of enclosing
queries the reference goes out (`1` for a field path); the Django and PostgreSQL drivers raise `ProgrammingError`
when there are fewer enclosing queries.

```python
invoiced = Query("billing.Invoice").filter(member=OuterRef("member"), club=OuterRef(OuterRef("pk")))
memberships = Query("clubs.Membership").filter(Exists(invoiced), club=OuterRef("pk"))
Query("clubs.Club").filter(Exists(memberships))
OuterRef(OuterRef("pk")).levels   # 2
```

### `adorm.Value(value, output_type=None)`

A literal value. Wrapping a node raises `TypeError`.

- `output_type` types the value when the node is encoded and must be a type
  [`adorm.codec.type_name()`](./encoding#adorm-codec-type-name-tp) names, else `TypeError`. Without it, the value
  takes the type of its position: a field's value, a lookup's right-hand side, a `Case` result or a `Coalesce`
  argument.
- The value is copied when the node is built; later changes to the caller's list or dict do not affect the node.
- A value that cannot be compared (a `bytearray`, an object without `__hash__` that is not a list, tuple, set or
  dict) raises `TypeError`.

```python
Concat("first_name", Value(" "), "last_name")
```

### `adorm.query.Star()`

All rows, as in `Count("*")`.

### `adorm.query.CombinedExpression(lhs, connector, rhs, output_type=None)`

`lhs <connector> rhs`. `connector` is `+`, `-`, `*`, `/`, `%` or `^` (power); any other raises `ValueError`. Plain
operands become `Value`. Arithmetic operators on expressions return it.

### `adorm.query.OrderBy(expression, *, descending=False, nulls_first=None, nulls_last=None)`

Ordering by `expression` (a string is a field path). `nulls_first` and `nulls_last` are `True` or `None`, at most
one of them `True`. `OrderBy` is a node, not an expression: it appears only in an ordering.

```python
F("joined").desc(nulls_last=True)
OrderBy(Lower("name"))
```

## Functions

### `adorm.Func(*expressions, output_type=None)`

Base class of functions. String arguments are field references; other plain arguments become `Value`. A subclass
sets `arity` (the exact number of arguments) or `min_args` and `max_args`, `default_output_type` and `output_from`.
A wrong number of arguments raises `TypeError`, as does instantiating `Func`. `output_type`, here and on aggregates,
types the values compared with the function when it is encoded and must be a type
[`adorm.codec.type_name()`](./encoding#adorm-codec-type-name-tp) names, else `TypeError`. A subclass has the construct
name `function.<class name>`.

A subclass that is not an aggregate declares its SQL with the class attributes of Django's `Func`; the drivers that
render SQL (the Django driver and the native PostgreSQL driver) run it and declare its construct name on the vendors
it has a template for, also when the class is defined after the driver was imported:

| Attribute | Meaning |
|---|---|
| `function` | the SQL function's name; alone, it renders `%(function)s(%(expressions)s)` |
| `template` | the SQL template: `%(expressions)s` is the arguments' SQL joined with `arg_joiner`, `%(function)s` the `function`; a literal `%` is `%%` |
| `templates` | a mapping from Django's vendor names (`"postgresql"`, `"sqlite"` …) to templates; a vendor without one falls back to `template`, then to `function` |
| `arg_joiner` | what joins the arguments in `%(expressions)s`; `", "` by default |

A wrong type (a template that is not a string, `templates` that is not a mapping of strings) raises `TypeError` when
the class is defined. On a vendor without SQL the function is not declared, so the query raises `NotSupportedError`
naming it before it runs. A function without SQL runs on a driver that translates it: a driver that adds it, or a
translation registered with
[`register_translation()`](/ref/drivers#adorm-drivers-django-register-translation-cls-vendors-none) for the Django
drivers. Recipe: [Own expressions](/howto/own-expressions).

```python
from adorm.query import Exact, Func, Query


class Initials(Func):
    arity = 2
    default_output_type = str
    template = "SUBSTR(%(expressions)s, 1, 1)"
    arg_joiner = ", 1, 1) || SUBSTR("


class YearMonth(Func):
    arity = 1
    default_output_type = str
    templates = {
        "postgresql": "TO_CHAR(%(expressions)s, 'YYYY-MM')",
        "sqlite": "STRFTIME('%%Y-%%m', %(expressions)s)",
    }


Query("club.Member").filter(Exact(Initials("first_name", "last_name"), "AN"))   # constructs: function.Initials, lookup.exact
```

#### `sql_template(vendor)`

A class method: the template on databases of `vendor`: `templates[vendor]`, else `template`, else
`%(function)s(%(expressions)s)` when the class names a `function`; `None` without SQL for the vendor.

### `adorm.query.sql_constructs(vendor)`

The construct names of the functions (not aggregates), lookups and transforms defined so far whose
`sql_template(vendor)` is not `None`, as a `frozenset`: those a driver that renders SQL templates declares on
databases of `vendor` (with [`Constructs`](/ref/drivers#adorm-drivers-constructs-names-extra-none), so that classes
defined later count too).

```python
sql_constructs("sqlite")    # frozenset({'function.Initials', 'function.YearMonth', ...})
sql_constructs("mysql")     # frozenset({'function.Initials', ...})
```

`output_from` gives the type of a function without an `output_type`, used to encode the values compared with it:

| `output_from` | Type |
|---|---|
| `None` | `default_output_type` |
| `"arguments"` | the first argument of a known type (`Coalesce`, `Greatest`, `Least`, `NullIf`) |
| `"first"` | the type of the first argument (`Abs`, `Round`, `Trim` …) |
| `"numeric"` | `Decimal` when an argument is a `Decimal`, else `float` (`Sqrt`, `Power`, `Mod` …) |

A `Value` argument of an `"arguments"` function without its own `output_type` is encoded with the function's
`output_type`, else with the type of the function's position; the types of the other arguments do not type it. Django
refuses arguments of different types (`NullIf("price", Value(0))` on a `Decimal` field raises `FieldError`): give the
`Value` or the function an `output_type` (`NullIf("price", Value(0), output_type=Decimal)`).

Every function below is importable from `adorm` and `adorm.query` and stands wherever an expression can:
`annotate()`, `alias()`, `filter()` (a lookup's left-hand side or value), `order_by()`, `update()` and an aggregate's
argument. `values()` selects a function's result by its annotation name (`annotate(y=ExtractYear("d")).values("y")`).
The drivers that translate queries with Django run Django's function of the same name, with its SQL and its limits on
each database.

| Function | Arguments | Result |
|---|---|---|
| `Lower(expression)` | 1 | the string in lower case |
| `Upper(expression)` | 1 | the string in upper case |
| `Length(expression)` | 1 | the number of characters, `int` |
| `Coalesce(*expressions)` | 2 or more | the first argument that is not NULL |
| `Concat(*expressions)` | 2 or more | the arguments concatenated; a NULL argument counts as empty |
| `Now()` | none | the database's current date and time, `datetime.datetime` |
| `Trunc(expression, kind, output_type=None)` | 1 and a kind | a date or time truncated to `kind` |
| `Cast(expression, output_type, ...)` | 1 and a type | the value converted to `output_type` |
| `Collate(expression, collation)` | 1 and a name | the value with another collation |
| `Greatest(*expressions)`, `Least(*expressions)` | 2 or more | the largest / smallest argument |
| `NullIf(expression1, expression2)` | 2 | NULL when the arguments are equal, else the first |
| `JSONArray(*expressions)` | any | a JSON array, `list` |
| `JSONObject(**fields)` | any, by key | a JSON object, `dict` |
| `Extract(expression, lookup_name)`, `ExtractYear(expression)` … | 1 (and a name) | a date or time component, `int` |
| `Abs`, `Ceil`, `Floor`, `Sign`, `Round(expression, precision=0)` | 1 (and a precision) | a number of the argument's type |
| `ACos`, `ASin`, `ATan`, `Cos`, `Cot`, `Degrees`, `Exp`, `Ln`, `Radians`, `Sin`, `Sqrt`, `Tan` | 1 | a `float`, or a `Decimal` for a `Decimal` |
| `ATan2`, `Log`, `Mod`, `Power` | 2 | a `float`, or a `Decimal` when an argument is a `Decimal` |
| `Pi()`, `Random()` | none | `float` |
| `Chr(expression)` | 1 | the character of a code point, `str` |
| `Ord(expression)` | 1 | the code point of the first character, `int` |
| `Left`, `Right`, `LPad`, `RPad`, `Repeat`, `Replace`, `StrIndex`, `Substr` | 2 or 3 | part of a string, a changed string or a position |
| `LTrim`, `RTrim`, `Trim`, `Reverse` | 1 | the string trimmed or reversed |
| `MD5`, `SHA1`, `SHA224`, `SHA256`, `SHA384`, `SHA512` | 1 | the hash in hexadecimal, `str` |
| `UUID4()`, `UUID7(shift=None)` | none or a shift | a new UUID per row, `uuid.UUID` |

### `adorm.Lower(expression)`

```python
Query("club.Member").order_by(Lower("name"))
```

### `adorm.Upper(expression)`

```python
Query("club.Member").filter(code=Upper(Value("lj-01")))
```

### `adorm.Length(expression)`

```python
Query("club.Member").annotate(name_length=Length("name")).filter(name_length__gt=20)
```

### `adorm.Coalesce(*expressions)`

```python
Coalesce("nick", "name", Value("?"))
```

### `adorm.Concat(*expressions)`

```python
Query("club.Member").annotate(full_name=Concat("first_name", Value(" "), "last_name"))
```

### `adorm.Now()`

On the Django and native PostgreSQL drivers: the start time of the statement (`STATEMENT_TIMESTAMP()`), the same
value for every row of one statement.

```python
Query("club.Activity").filter(starts_at__gte=Now())
```

### `adorm.Trunc(expression, kind, output_type=None)`

`kind` is `"year"`, `"quarter"`, `"month"`, `"week"` (the Monday), `"day"`, `"hour"`, `"minute"`, `"second"`,
`"date"` or `"time"`; any other raises `ValueError`. `"date"` defaults `output_type` to `datetime.date`, `"time"`
to `datetime.time`. The subclasses fix the kind:

### `adorm.TruncYear(expression, output_type=None)`

### `adorm.TruncQuarter(expression, output_type=None)`

### `adorm.TruncMonth(expression, output_type=None)`

### `adorm.TruncWeek(expression, output_type=None)`

### `adorm.TruncDay(expression, output_type=None)`

### `adorm.TruncHour(expression, output_type=None)`

### `adorm.TruncMinute(expression, output_type=None)`

### `adorm.TruncSecond(expression, output_type=None)`

### `adorm.TruncDate(expression, output_type=None)`

### `adorm.TruncTime(expression, output_type=None)`

```python
Query("club.Payment").annotate(month=TruncMonth("paid_at")).filter(month__gte=start)
```

`TruncMonth("d")` and `Trunc("d", "month")` are different nodes with different construct names
(`function.TruncMonth`, `function.Trunc`).

### Comparison and conversion

#### `adorm.Cast(expression, output_type, *, max_length=None, max_digits=None, decimal_places=None)`

The value converted to `output_type` in the database (SQL `CAST`). `output_type` is required and must be a type
[`adorm.codec.type_name()`](./encoding#adorm-codec-type-name-tp) names, else `TypeError`; the Django-translating
drivers cast to the column type of the Django field for it (`str` → `TextField`, `int` → `IntegerField` …).
`max_length` makes a cast to `str` a `CharField(max_length)` (`varchar(n)` on PostgreSQL); `max_digits` and
`decimal_places`, given together, make a cast to `Decimal` a `DecimalField` with that precision and scale. An option
for another type, or only one of `max_digits` and `decimal_places`, raises `TypeError`; a negative size or
`decimal_places` above `max_digits` raises `ValueError`.

```python
Query("activities.Activity").annotate(c=Cast("capacity", str)).filter(c__startswith="1")
Query("clubs.Member").annotate(day=Cast("joined", datetime.date))
Query("billing.Invoice").annotate(amount=Cast("total", Decimal, max_digits=6, decimal_places=0))
```

#### `adorm.Collate(expression, collation)`

The value with the collation `collation` (SQL `COLLATE`), to order and compare by other rules than the column's.
`collation` is a name of letters, digits, `_` and `-`, else `ValueError`; it must exist in the database.

```python
Query("clubs.Member").order_by(Collate("last_name", "C"))
```

#### `adorm.Greatest(*expressions)`

#### `adorm.Least(*expressions)`

The largest and the smallest of two or more arguments. On PostgreSQL a NULL argument is ignored (the result is NULL
only when all are); on SQLite, MySQL and Oracle a NULL argument makes the result NULL.

```python
Query("billing.Invoice").annotate(last_change=Greatest("issued", "due"))
```

#### `adorm.NullIf(expression1, expression2)`

NULL when the two arguments are equal, else the first one.

```python
Query("billing.Invoice").annotate(charged=NullIf("total", Value(Decimal(0))))
```

### JSON

#### `adorm.JSONArray(*expressions)`

A JSON array of the arguments' values, a `list` in Python.

#### `adorm.JSONObject(**fields)`

A JSON object with a key for each keyword argument, a `dict` in Python. The values are expressions; a string is a
field path. A value without a JSON type is converted by the database: a date becomes an ISO 8601 string.

```python
Query("clubs.Member").annotate(card=JSONObject(name="first_name", born="birth_date"))
Query("clubs.Member").annotate(pair=JSONArray("first_name", "last_name"))
```

### Date and time components

#### `adorm.Extract(expression, lookup_name, output_type=None)`

A component of a date, a time, a date and time or (on PostgreSQL) a duration, as an `int`. `lookup_name` is one of:

| `lookup_name` | Component |
|---|---|
| `"year"` | the year |
| `"iso_year"` | the ISO 8601 week-numbering year |
| `"quarter"` | the quarter, 1 to 4 |
| `"month"` | the month, 1 to 12 |
| `"week"` | the ISO 8601 week number, 1 to 53 |
| `"day"` | the day of the month |
| `"week_day"` | the day of the week, 1 (Sunday) to 7 (Saturday) |
| `"iso_week_day"` | the ISO day of the week, 1 (Monday) to 7 (Sunday) |
| `"hour"`, `"minute"`, `"second"` | the time component |

Any other name raises `ValueError`. A date and time is taken in the current time zone of the driver's ORM (Django's
`TIME_ZONE` with `USE_TZ`). A time component of a date (`ExtractHour` of a `DateField`) raises `DatabaseError` when the
query runs. The subclasses fix the name:

#### `adorm.ExtractYear(expression, output_type=None)`

#### `adorm.ExtractIsoYear(expression, output_type=None)`

#### `adorm.ExtractQuarter(expression, output_type=None)`

#### `adorm.ExtractMonth(expression, output_type=None)`

#### `adorm.ExtractWeek(expression, output_type=None)`

#### `adorm.ExtractDay(expression, output_type=None)`

#### `adorm.ExtractWeekDay(expression, output_type=None)`

#### `adorm.ExtractIsoWeekDay(expression, output_type=None)`

#### `adorm.ExtractHour(expression, output_type=None)`

#### `adorm.ExtractMinute(expression, output_type=None)`

#### `adorm.ExtractSecond(expression, output_type=None)`

```python
Query("clubs.Member").annotate(y=ExtractYear("birth_date")).filter(y__gte=2000)
Query("clubs.Member").annotate(age=ExtractYear("joined") - ExtractYear("birth_date"))
```

`ExtractYear("d")` and `Extract("d", "year")` are different nodes with different construct names
(`function.ExtractYear`, `function.Extract`).

### Math

A math function of NULL is NULL. A value outside a function's domain (`Sqrt` of a negative number, `Ln` of 0)
raises a database error.

#### `adorm.Abs(expression)`

The absolute value, of the argument's type.

#### `adorm.Ceil(expression)`

#### `adorm.Floor(expression)`

The smallest integer not less than, and the largest integer not greater than, the value; of the argument's type.

#### `adorm.Round(expression, precision=0, output_type=None)`

The value rounded to `precision` decimal places, of the argument's type; a negative precision rounds to tens,
hundreds … (not on SQLite, where the query raises `DatabaseError` when it runs).

#### `adorm.Sign(expression)`

-1, 0 or 1, of the argument's type.

#### `adorm.ACos(expression)`

#### `adorm.ASin(expression)`

#### `adorm.ATan(expression)`

#### `adorm.ATan2(expression1, expression2)`

#### `adorm.Cos(expression)`

#### `adorm.Cot(expression)`

#### `adorm.Sin(expression)`

#### `adorm.Tan(expression)`

Trigonometric functions of angles in radians; `ATan2(y, x)` is the arctangent of `y / x` in the quadrant of the
point `(x, y)`.

#### `adorm.Degrees(expression)`

#### `adorm.Radians(expression)`

Radians converted to degrees, and degrees to radians.

#### `adorm.Exp(expression)`

#### `adorm.Ln(expression)`

#### `adorm.Log(expression1, expression2)`

*e* raised to the value, the natural logarithm, and the logarithm of `expression2` to the base `expression1`.

#### `adorm.Mod(expression1, expression2)`

#### `adorm.Power(expression1, expression2)`

#### `adorm.Sqrt(expression)`

The remainder of `expression1 / expression2`, `expression1` raised to `expression2`, and the square root. These and
the trigonometric and logarithmic functions return a `float`, or a `Decimal` when an argument is a `Decimal`.

#### `adorm.Pi()`

The number π, a `float`.

#### `adorm.Random()`

A random `float` from 0 (included) to 1 (excluded), a new one for each row. `order_by(Random())` shuffles the rows.

```python
Query("activities.Activity").filter(capacity__lt=10).update(capacity=Round(Power("capacity", 2)))
Query("billing.Invoice").annotate(rounded=Round("total", 1)).order_by(Abs(F("total") - 100))
```

### Text

A text function of NULL is NULL. String arguments are field paths: a literal string is `Value("...")`.

#### `adorm.Chr(expression)`

The character with the Unicode code point the value gives, a `str`.

#### `adorm.Ord(expression)`

The Unicode code point of the first character, an `int`.

#### `adorm.Left(expression, length, output_type=None)`

#### `adorm.Right(expression, length, output_type=None)`

The first and the last `length` characters. A literal `length` below 1 raises `ValueError`.

#### `adorm.LPad(expression, length, fill_text=Value(" "), output_type=None)`

#### `adorm.RPad(expression, length, fill_text=Value(" "), output_type=None)`

The string padded on the left or on the right with `fill_text`, repeated, to `length` characters; a longer string is
cut to `length`. A literal negative `length` raises `ValueError`.

#### `adorm.LTrim(expression)`

#### `adorm.RTrim(expression)`

#### `adorm.Trim(expression)`

The string without leading, trailing, or leading and trailing spaces.

#### `adorm.Repeat(expression, number, output_type=None)`

The string repeated `number` times. A literal negative `number` raises `ValueError`.

#### `adorm.Replace(expression, text, replacement=Value(""), output_type=None)`

The string with every occurrence of `text` replaced by `replacement`, or removed; case sensitive.

#### `adorm.Reverse(expression)`

The characters in reverse order.

#### `adorm.StrIndex(string, substring)`

The position (from 1) of the first occurrence of `substring`, 0 when there is none, as an `int`; case sensitive.

#### `adorm.Substr(expression, pos, length=None, output_type=None)`

`length` characters from position `pos` (from 1), or the rest of the string without `length`. A literal `pos` below
1 raises `ValueError`.

```python
Query("clubs.Member").filter(first_name="Ana").update(last_name=LPad(Left("last_name", 1), 3, Value("*")))
Query("clubs.Member").annotate(at=StrIndex("email", Value("@"))).filter(at__gt=0)
Query("clubs.Member").annotate(initials=Concat(Substr("first_name", 1, 1), Substr("last_name", 1, 1)))
```

#### `adorm.MD5(expression)`

#### `adorm.SHA1(expression)`

#### `adorm.SHA224(expression)`

#### `adorm.SHA256(expression)`

#### `adorm.SHA384(expression)`

#### `adorm.SHA512(expression)`

The hash of the string's UTF-8 bytes in lower-case hexadecimal, a `str`. On PostgreSQL the SHA functions call
`DIGEST()` of the `pgcrypto` extension, which the database must have (`CREATE EXTENSION pgcrypto`); without it the
query raises `ProgrammingError`.

### UUIDs

#### `adorm.UUID4()`

A random version 4 UUID, a new one for each row, as a `uuid.UUID`.

#### `adorm.UUID7(shift=None)`

A version 7 (time-ordered) UUID, a new one for each row, as a `uuid.UUID`. `shift` (a `timedelta` or an expression
of a duration) moves its timestamp.

The Django driver and the native PostgreSQL driver run both on PostgreSQL and SQLite with every Django version they
support, with Django's function where Django and the database have it and with SQL or a SQLite function of adorm's
otherwise ([UUID4 and UUID7](/ref/drivers#uuid4-and-uuid7)). A `UUID7` value holds the Unix time in milliseconds in its
first 48 bits, so values of later milliseconds sort later. On SQLite `shift` raises `NotSupportedError`. Other
databases have them where Django does.

```python
Query("library.Note").update(code=UUID4())
```

## Aggregates

### `adorm.Aggregate(*expressions, distinct=False, filter=None, default=None, output_type=None)`

Base class of aggregates; instantiating it raises `TypeError`. An aggregate takes one argument; a string is a field
path.

| Argument | Meaning |
|---|---|
| `distinct` | aggregate distinct values; `TypeError` where the class does not allow it (`Min`, `Max`, `Count("*")`) |
| `filter` | a condition (`Q`, a lookup, `Exists`) limiting the aggregated rows; anything else raises `TypeError`; an empty `Q()` is no filter |
| `default` | the result instead of NULL; a string is a field path. `Count` does not take it |

An aggregate as the argument, in `filter` or in `default` raises `TypeError`; an aggregate inside a subquery is
allowed.

#### `default_alias`

The name [`aggregate()`](./queryset#async-aggregate-args-kwargs) gives an aggregate passed without a name:
`<field path>__<class name in lower case>`. An aggregate of anything but a field (`Count("*")`, an expression,
`OuterRef`) raises `TypeError`.

```python
Sum("fee").default_alias                  # "fee__sum"
Count("memberships__club").default_alias  # "memberships__club__count"
```

### `adorm.Count(expression, *, distinct=False, filter=None, output_type=None)`

The number of non-NULL values, or of rows for `Count("*")`. `output_type` defaults to `int`.

```python
Count("*")
Count("member", distinct=True)
Count("member", filter=Q(member__active=True))
```

### `adorm.Sum(expression, *, distinct=False, filter=None, default=None, output_type=None)`

### `adorm.Avg(expression, *, distinct=False, filter=None, default=None, output_type=None)`

### `adorm.Min(expression, *, distinct=False, filter=None, default=None, output_type=None)`

### `adorm.Max(expression, *, distinct=False, filter=None, default=None, output_type=None)`

`Sum` and `Avg` allow `distinct`; `Min` and `Max` raise `TypeError` for `distinct=True`.

```python
Sum("amount", filter=Q(confirmed=True), default=0)
Avg("age", distinct=True)
```

## Conditional expressions

### `adorm.When(condition=None, then=None, **lookups)`

A branch of `Case`. The condition is a `Q`, a lookup or `Exists`, keyword lookups, or a condition and keyword
lookups joined with AND. A missing or non-boolean condition raises `TypeError`, an empty `Q()` `ValueError`. A
string `then` is a field path; `Value("text")` is a literal string. Attributes: `condition`, `result`.

### `adorm.Case(*cases, default=None, output_type=None)`

The result of the first `When` whose condition holds, else `default` (a string is a field path; `None` is NULL).
Arguments other than `When` raise `TypeError`.

```python
Case(
    When(age__lt=18, then=Value("junior")),
    When(Q(age__gte=65) | Q(retired=True), then=Value("senior")),
    default=Value("adult"),
    output_type=str,
)
```

## Subqueries

### `adorm.Subquery(query, output_type=None)`

A query used as an expression. `query` is a [`Query`](./query) or an object whose `query` attribute is one;
anything else raises `TypeError`. A query selecting more than one column raises `ValueError`. `OuterRef` inside it
refers to the outer query. A subquery's aggregates do not make the outer expression an aggregate.

```python
newest = Query("club.Payment").filter(member=OuterRef("pk")).order_by("-paid_at").values("amount")[:1]
Query("club.Member").annotate(last_amount=Subquery(newest))
```

### `adorm.Exists(query, *, negated=False)`

Whether `query` returns a row; a condition with `output_type` `bool`. `~Exists(query)` negates it. The query may
select any columns.

```python
unpaid = Query("club.Payment").filter(member=OuterRef("pk"), confirmed=False)
Query("club.Member").filter(~Exists(unpaid))
```

## Conditions

### `adorm.Q(*args, **kwargs)`

A condition: its `children` joined by its `connector` (`Q.AND` or `Q.OR`), negated when `negated` is true. `Q` is a
conditional expression with `output_type` `bool`.

Keyword arguments are [lookups](./lookups), joined with AND in sorted key order. Positional arguments are
conditions (`Q`, lookups, `Exists`) or `(key, value)` pairs; anything else raises `TypeError`. An empty `Q`
argument is left out, and a `Q` argument that is not negated and has the same connector (or one child) contributes
its children, so `Q(Q(a=1)) == Q(a=1)` and `Q(Q())` is empty.

| Operation | Result |
|---|---|
| `a & b` | AND; children of operands that are already AND (or have one child) are merged |
| `a \| b` | OR, merged the same way |
| `~a` | the negation; `~~a == a` |
| `Q()` | empty and falsy; `Q() & a` and `a \| Q()` return `a` |

```python
Q(name__startswith="A") | Q(name__startswith="B")
Q(active=True) & ~Q(club__name="Chess")
Q(("name__icontains", term))                     # (key, value) pair
Q(Exists(unpaid), active=True)
```
