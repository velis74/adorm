# Own expressions

::: info Status
The [Django driver](/topics/django-driver) and the [native PostgreSQL driver](/topics/postgresql-driver) render the
SQL templates of this recipe and run the translations registered for them. Another driver runs an own expression
only when it declares the expression's construct name; otherwise the query raises `NotSupportedError` before it runs.
:::

This recipe adds database functions, lookups and transforms the bundled ones do not cover, without waiting for the
target ORM to have them:

- a [`Func`](/ref/expressions#adorm-func-expressions-output-type-none) subclass with a SQL `template`, or a template
  per database vendor in `templates`;
- a [`Lookup`](/ref/lookups#adorm-query-lookup-lhs-rhs) or [`Transform`](/ref/lookups#adorm-query-transform) subclass
  with a template;
- for SQL a template cannot express, a translation of a function, lookup or transform into a Django expression,
  registered with [`register_translation()`](/ref/drivers#adorm-drivers-django-register-translation-cls-vendors-none);
- a Python function on SQLite connections, registered with
  [`register_sqlite_function()`](/ref/drivers#adorm-drivers-django-register-sqlite-function-name-num-params-function-deterministic-false).

A driver that renders SQL declares the construct name (`function.<class name>`, `lookup.<name>`,
`transform.<name>`) of each class with a template for the database's vendor, including classes defined after the
driver was imported. The examples use a `Member` model with `first_name`, `last_name`, `email` and `birth_date`.

## 1. A function with one template

The template is Django's: `%(expressions)s` is the arguments' SQL joined with `arg_joiner` (`", "` by default),
`%(function)s` the class's `function`. A literal `%` is written `%%`. One template serves every vendor, and the
drivers declare the function on every vendor, so it holds SQL every database runs; SQL of one dialect (`||`, which
MySQL reads as OR) goes into the templates per vendor of section 2.

```python
from adorm.query import Func


class FirstLetter(Func):
    """The first character of a string, in upper case."""

    arity = 1
    default_output_type = str
    function = "SUBSTR"
    template = "UPPER(%(function)s(%(expressions)s, 1, 1))"
```

`FirstLetter("last_name")` renders `UPPER(SUBSTR(last_name, 1, 1))`. It stands wherever an expression can: in
`annotate()`, `filter()`, `order_by()` and `update()`.

```python
query = Member.objects.filter(first_name__in=["Ana", "Eva"]).annotate(f=FirstLetter("last_name"))
first_letters = [row async for row in query.order_by("first_name").values_list("first_name", "f")]
# [("Ana", "N"), ("Eva", "Z")]
```

A function that is a SQL function of the same arguments only names it: `function = "SOUNDEX"` renders
`SOUNDEX(%(expressions)s)`.

## 2. A template per database vendor

`templates` maps Django's vendor names (`"postgresql"`, `"sqlite"`, `"mysql"`, `"oracle"` …) to templates;
`template`, when given, serves the other vendors. On a vendor without a template the function is not among the
driver's capabilities: the query raises `NotSupportedError` naming `function.YearMonth` before it takes a connection.

```python
class YearMonth(Func):
    """A date as YYYY-MM."""

    arity = 1
    default_output_type = str
    templates = {
        "postgresql": "TO_CHAR(%(expressions)s, 'YYYY-MM')",
        "sqlite": "STRFTIME('%%Y-%%m', %(expressions)s)",
    }
```

```python
query = Member.objects.filter(first_name="Ana").annotate(month=YearMonth("birth_date"))
months = [month async for month in query.values_list("month", flat=True)]
# ["1970-01"]
```

## 3. A SQLite function in Python

SQLite has few built-in functions; a template can call a Python function registered on the Django driver's SQLite
connections. A connection that is already open gets it before its next operation.

```python
from adorm.drivers.django import register_sqlite_function


def reverse_text(text):
    return None if text is None else text[::-1]


register_sqlite_function("app_reverse", 1, reverse_text, deterministic=True)


class Reversed(Func):
    """The string reversed."""

    arity = 1
    default_output_type = str
    templates = {"postgresql": "REVERSE(%(expressions)s)", "sqlite": "APP_REVERSE(%(expressions)s)"}
```

```python
query = Member.objects.filter(first_name="Ana").annotate(r=Reversed("first_name"))
reversed_names = [name async for name in query.values_list("r", flat=True)]
# ["anA"]
```

## 4. A lookup and a transform

A lookup's template has `%(lhs)s` and `%(rhs)s`, a transform's `%(expressions)s` (or only a `function`).
Registration makes the name usable in keyword lookups.

```python
from adorm.query import Lookup, register_lookup, register_transform, Transform


@register_lookup
class SameStart(Lookup):
    """Strings that start with the same character."""

    lookup_name = "same_start"
    template = "SUBSTR(%(lhs)s, 1, 1) = SUBSTR(%(rhs)s, 1, 1)"


@register_transform
class Chars(Transform):
    """The number of characters of a string."""

    lookup_name = "chars"
    function = "LENGTH"

    @classmethod
    def output_type(cls, field_type):
        return int
```

```python
same_start = [name async for name in Member.objects.filter(first_name__same_start="Gregor").values_list(
    "first_name", flat=True
)]
# ["Gaja"]
short = [name async for name in Member.objects.filter(first_name__chars=3).order_by("first_name").values_list(
    "first_name", flat=True
)]
# ["Ana", "Eva"]
```

The lookup class also stands as a condition: `Member.objects.filter(SameStart("first_name", "Gregor"))`.

## 5. A translation for the Django drivers

A translation builds the node's Django expression, here from Django's functions, which already render SQL for each
database. `translate` translates the node's arguments (`node.get_source_expressions()`); the translation runs on both
Django-based drivers, and they declare `function.Masked` on every vendor (or on those given in `vendors=`).

```python
from django.db.models import TextField, Value
from django.db.models.functions import Concat, Left, StrIndex, Substr

from adorm.drivers.django import register_translation
from adorm.query import register_transform, Transform


class Masked(Func):
    """An e-mail address with the local part hidden: a***@example.com."""

    arity = 1
    default_output_type = str


@register_translation(Masked)
def translate_masked(node, translate):
    (email,) = (translate(argument) for argument in node.get_source_expressions())
    domain = Substr(email, StrIndex(email, Value("@")))
    return Concat(Left(email, 1), Value("***"), domain, output_field=TextField())
```

```python
query = Member.objects.filter(first_name="Ana").annotate(m=Masked("email"))
masked = [address async for address in query.values_list("m", flat=True)]
# ["a***@example.com"]
```

A transform's translation gets the Django expression of the transformed value and returns the transformed one. The
drivers declare `transform.domain`; a transform with SQL of its own or a translation may follow it in a path, a
built-in one (`year` …) may not.

```python
@register_transform
class Domain(Transform):
    """The domain of an e-mail address."""

    lookup_name = "domain"

    @classmethod
    def output_type(cls, field_type):
        return str


@register_translation(Domain)
def translate_domain(lhs):
    return Substr(lhs, StrIndex(lhs, Value("@")) + 1)
```

```python
domains = [domain async for domain in Member.objects.filter(email__domain="example.com", first_name="Ana").values_list(
    "email__domain", flat=True
)]
# ["example.com"]
```

A driver that does not translate with Django (another ORM's) refuses `Masked` until it declares the construct.

## 6. UUID4 and UUID7

The bundled [`UUID4`](/ref/expressions#adorm-uuid4) and
[`UUID7`](/ref/expressions#adorm-uuid7-shift-none) are translations registered the same way. Each gives Django's
function where the installed Django and the database have it, and otherwise the database's own function or a Python
function registered as in section 3:

| Function | PostgreSQL | SQLite |
|---|---|---|
| `UUID4` | `GEN_RANDOM_UUID()` | `ADORM_UUID4()`, a registered SQLite function (`uuid.uuid4()`) |
| `UUID7` | `UUIDV7()` from PostgreSQL 18; before 18 `NotSupportedError` | `ADORM_UUID7()`, a registered SQLite function ([`adorm.uuid7()`](/ref/expressions#adorm-uuid7)); a shift raises `NotSupportedError` |

```python
from adorm.query import UUID4

query = Member.objects.filter(first_name="Ana").annotate(code=UUID4())
codes = [code async for code in query.values_list("code", flat=True)]
# [UUID("6f1c…")], codes[0].version == 4
```

A column of version 7 UUIDs gets its values from a Python default: [`adorm.uuid7()`](/ref/expressions#adorm-uuid7)
generates one in the application (`uuid.uuid7()` from Python 3.14 on), so the value is filled on insert on every
database and driver. A database default (`db_default`) would need the database's own function, which PostgreSQL has
from version 18 on only.

```python
from django.db import models

import adorm

# a field of a Django model
code = models.UUIDField(default=adorm.uuid7, editable=False, unique=True)

adorm.uuid7().version   # 7
```
