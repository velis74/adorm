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

Base class of expressions. Attributes: `output_type` (the Python type of the result, or `None` when it follows
from the source; where a constructor takes it, a type [`adorm.codec.type_name()`](./encoding#adorm-codec-type-name-tp)
names, else `TypeError`; it types the values compared with the expression when it is encoded), `conditional` (true for conditions: `Q`, lookups, `Exists`) and `contains_aggregate` (whether an
aggregate occurs outside a subquery).

Arithmetic operators build a [`CombinedExpression`](#adorm-query-combinedexpression-lhs-connector-rhs-output-type-none); a plain operand, a string
included, becomes a `Value`. A conditional expression combines with `&`, `|` and `~` into a `Q`; on any other
expression these operators raise `TypeError`.

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

A reference to a field of the outer query, used inside a [`Subquery`](#adorm-subquery-query-output-type-none) or
[`Exists`](#adorm-exists-query-negated-false). A subclass of `F`. `name` is a field path, or another `OuterRef`:
`OuterRef(OuterRef("pk"))` refers to the query that encloses the outer query. `levels` counts how many queries out
the reference goes (`1` for a field path); the Django and PostgreSQL drivers raise `ProgrammingError` when there are not that many
enclosing queries.

```python
invoiced = Query("billing.Invoice").filter(member=OuterRef("member"), club=OuterRef(OuterRef("pk")))
memberships = Query("clubs.Membership").filter(Exists(invoiced), club=OuterRef("pk"))
Query("clubs.Club").filter(Exists(memberships))
OuterRef(OuterRef("pk")).levels   # 2
```

### `adorm.Value(value, output_type=None)`

A literal value; `output_type` optionally names its Python type. Wrapping a node raises `TypeError`. `output_type`
types the value when the node is encoded and must be a type [`adorm.codec.type_name()`](./encoding#adorm-codec-type-name-tp)
names; any other type raises `TypeError`. Without it, the value takes the type of its position (a field's value, a
lookup's right-hand side, a `Case` result or a `Coalesce` argument there). The value is
copied when the node is built, so changing the caller's list or dict afterwards does not change the node; a value
that cannot be compared (a `bytearray`, an object without `__hash__` that is not a list, tuple, set or dict) raises
`TypeError`.

```python
Concat("first_name", Value(" "), "last_name")
```

### `adorm.query.Star()`

All rows, as in `Count("*")`.

### `adorm.query.CombinedExpression(lhs, connector, rhs, output_type=None)`

`lhs <connector> rhs`. `connector` is `+`, `-`, `*`, `/`, `%` or `^` (power); any other raises `ValueError`. Plain
operands become `Value`. Arithmetic operators on expressions build it.

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
sets `arity` (the exact number of arguments) or `min_args`, and `default_output_type`; a wrong number of arguments
raises `TypeError`, as does instantiating `Func` itself. `output_type`, here and on aggregates, types the values
compared with the function when it is encoded and must be a type
[`adorm.codec.type_name()`](./encoding#adorm-codec-type-name-tp) names; any other type raises `TypeError`. A subclass
gets the construct name
`function.<class name>`, so a function a driver adds is declared like this:

```python
from adorm.query import Exact, Func, Query


class Unaccent(Func):
    arity = 1


Query("club.Member").filter(Exact(Unaccent("name"), "Zoe"))   # constructs: function.Unaccent, lookup.exact
```

| Function | Arguments | Result |
|---|---|---|
| `Lower(expression)` | 1 | the string in lower case |
| `Upper(expression)` | 1 | the string in upper case |
| `Length(expression)` | 1 | the number of characters, `int` |
| `Coalesce(*expressions)` | 2 or more | the first argument that is not NULL |
| `Concat(*expressions)` | 2 or more | the arguments concatenated; a NULL argument counts as empty |
| `Now()` | none | the database's current date and time, `datetime.datetime` |
| `Trunc(expression, kind, output_type=None)` | 1 and a kind | a date or time truncated to `kind` |

### `adorm.Lower(expression)`

```python
Query("club.Member").order_by(Lower("name"))
```

### `adorm.Upper(expression)`

### `adorm.Length(expression)`

```python
Query("club.Member").annotate(name_length=Length("name")).filter(name_length__gt=20)
```

### `adorm.Coalesce(*expressions)`

```python
Coalesce("nick", "name", Value("?"))
```

### `adorm.Concat(*expressions)`

### `adorm.Now()`

The Django and native PostgreSQL drivers give the start time of the statement (`STATEMENT_TIMESTAMP()`), so
every row of one statement gets the same value.

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

## Aggregates

### `adorm.Aggregate(*expressions, distinct=False, filter=None, default=None, output_type=None)`

Base class of aggregates; instantiating it raises `TypeError`. An aggregate takes one argument (a string is a field
path).

| Argument | Meaning |
|---|---|
| `distinct` | aggregate distinct values; `TypeError` where the class does not allow it (`Min`, `Max`, `Count("*")`) |
| `filter` | a condition (`Q`, a lookup, `Exists`) limiting the aggregated rows; anything else raises `TypeError`; an empty `Q()` is no filter |
| `default` | the result instead of NULL; a string is a field path. `Count` does not take it |

An aggregate cannot take an aggregate as its argument, nor contain one in `filter` or `default` (`TypeError`); an
aggregate inside a subquery is allowed.

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
lookups joined with AND. A missing or non-boolean condition raises `TypeError`, an empty `Q()` `ValueError`. A string `then` is a field path;
`Value("text")` is a literal string. Attributes: `condition`, `result`.

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
