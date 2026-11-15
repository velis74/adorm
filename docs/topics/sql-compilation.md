# SQL compilation for asyncpg

The native PostgreSQL driver executes SQL on asyncpg. This page describes how that SQL is built: what Django's
query compiler does without a database connection, how its output differs from what asyncpg accepts, and what
SQLAlchemy Core offers instead.

## Design

The [native PostgreSQL driver](/topics/postgresql-driver) builds its SQL with
[`DjangoCompiler`](/ref/drivers#adorm-drivers-asyncpg-compiler-djangocompiler-alias-default-server-version-params-none): it
turns an adorm operation into a Django query, compiles it on a database wrapper that never connects, converts
placeholders and parameters for asyncpg, and applies Django's result converters to the rows asyncpg returns.
Lookups, functions, custom fields and their converters are Django's own. The `asyncpg` extra therefore installs
Django and psycopg 3 along with asyncpg.

SQLAlchemy Core can build the SQL for applications without Django models; it needs table metadata and its own
translation of every adorm construct. adorm does not provide such a builder.

```python
compiler = DjangoCompiler("default", server_version=int(await conn.fetchval("SHOW server_version_num")))
statement = compiler.compile(Club.objects.filter(name__icontains="chess").values("id", "name"))
rows = statement.rows(await conn.fetch(statement.sql, *statement.params))
```

## Django's compiler

`queryset.query.get_compiler(using).as_sql()` returns `(sql, params)`. It builds the SQL from the database
wrapper's `ops`, `features` and `vendor`, none of which opens a connection, with these exceptions:

| Construct | Why it connects |
|---|---|
| `AnyValue`, `JSONObject`, `JSONArray`, `UUID7` | `features.is_postgresql_16` / `_18` read `pg_version`, a cached property that opens a temporary connection to ask the server |
| `select_for_update()` | the compiler calls `get_autocommit()` to refuse locking outside a transaction, and that ensures a connection |
| `SearchHeadline` with options | the options are quoted with psycopg's `mogrify()`, which needs a connection |
| an update that sets fields of a multi-table inheritance parent (`related_updates`) | `pre_sql_setup()` selects the affected keys with `execute_sql()` on `connections[alias]`; the parent tables' updates run only in `execute_sql()` |
| executing (`execute_sql()`, `results_iter()` without `results`) | runs the statement |

`pg_version` is a cached property, so a value stored on the wrapper (`wrapper.__dict__["pg_version"]`) replaces
the query. With it, a server version decides the SQL: `JSONObject` compiles to `JSON_OBJECT(... RETURNING JSONB)`
from PostgreSQL 16 on and to `JSONB_BUILD_OBJECT(...)` before; `AnyValue` raises Django's `NotSupportedError`
before 16. `DjangoCompiler` stores the version it is given, answers `get_autocommit()` with `False` and checks
`select_for_update()` against the caller's `in_transaction` itself; it refuses the parent-field update and
`SearchHeadline` with options. The native PostgreSQL driver runs the parent-field update itself: it selects the keys
as `pre_sql_setup()` does, then compiles one update per table filtered by those keys.

`get_compiler(using)` takes the wrapper from `django.db.connections`, which is thread-local: each thread has its own
wrapper and its own cached `pg_version`. `get_compiler(connection=wrapper)` takes any wrapper instead, including
one built from a `DATABASES` entry with `load_backend(engine).DatabaseWrapper(settings_dict, alias)` and never
connected. Neither `get_compiler()` nor `as_sql()` is one of Django's `async_unsafe` calls, so compiling inside a
running event loop works; only an attempt to connect raises `SynchronousOnlyOperation` there. Compiling only fills
the wrapper's cached properties, with the same values in every thread, so one wrapper serves concurrent threads.

`as_sql()` of a select, update, delete or aggregate query returns one statement; an insert returns a list of
statements, one per batch, and one statement for a single row on PostgreSQL. With the compiler's `returning_fields`
set, an insert or update ends with `RETURNING`, which PostgreSQL supports for both; Django's insert reads the
primary key and the values the database sets that way, and Django 6 also the values of expressions a save writes.
`DjangoCompiler.compile_insert()` and `compile(..., returning=...)` set them, and
[`DjangoStatement.returned()`](/ref/drivers#returned-records) applies the fields' converters to the returned rows.

```python
query = InsertQuery(Tag)
query.insert_values([Tag._meta.get_field("name")], [Tag(name="night")])
statement = compiler.compile_insert(query, [Tag._meta.pk])
statement.sql      # 'INSERT INTO "library_tag" ("name") VALUES ($1) RETURNING "library_tag"."id"'
```

### Queries without a statement

Django executes some queries without SQL:

- A filter that can match nothing (`pk__in=[]`) raises `EmptyResultSet` while compiling, and `execute_sql()`
  returns no rows. With `elide_empty=False` a select compiles to `... WHERE 0 = 1` instead, which also returns the
  right aggregate values over no rows; `DjangoCompiler` compiles that way.
- An update or delete with such a filter raises `EmptyResultSet` regardless, and an update without values compiles
  to an empty string. `DjangoCompiler` returns an empty statement for both
  ([`DjangoStatement.empty`](/ref/drivers#empty)); it affects no rows and is not executed.
- `aggregate()` over such a query returns each aggregate's value over no rows without SQL. The compiled aggregation
  has an empty statement and that result.

## Aggregation

Django's `aggregate()` decides inside `Query.get_aggregation()` whether the aggregates run directly on the query or
over it as a subquery (a slice, `distinct()`, a `values().annotate()` grouping or an aggregate annotation), builds
that statement, executes it on `connections[using]` and converts the row. No public method returns the statement
without executing it. `DjangoCompiler.compile_aggregate()` runs `get_aggregation()` on its own wrapper, registered
under an alias of its own in the calling thread's `connections` for the duration of the call, whose cursor raises
with the statement instead of executing it. After asyncpg has fetched the row, `DjangoAggregation.result()` runs
`get_aggregation()` again with a cursor that returns that row, so Django's own converters and names shape the
result. Both runs are synchronous and leave the alias and the cursor behind in no thread and no task.

```python
aggregation = compiler.compile_aggregate(Book.objects.order_by("-pages")[:2], {"pages": Sum("pages")})
aggregation.statement.sql     # 'SELECT SUM("__col1") FROM (SELECT ... LIMIT 2) subquery'
aggregation.result(await conn.fetchrow(aggregation.statement.sql, *aggregation.statement.params))
```

## Placeholders

Django's compiler emits the PEP 249 `format` paramstyle: `%s` for a parameter and `%%` for a literal percent sign
(its pattern escaping for `contains` with an expression produces `REPLACE(..., E'%%', E'\\%%')`). Values are never
inlined: `Value("a%s")` stays a parameter. `LIMIT` and `OFFSET` are inlined as integer literals. psycopg interprets
`%` everywhere in the statement, inside quoted literals and identifiers too.

asyncpg accepts only numbered placeholders, `$1` … `$n`.
[`numbered_placeholders()`](/ref/drivers#adorm-drivers-sql-numbered-placeholders-sql-casts-none) applies psycopg's
rule: `%s` becomes the next `$n`, `%%` becomes `%`, and anything else after `%` is an error.

```text
UPPER("library_book"."title"::text) LIKE UPPER(%s) AND "library_book"."pages" >= %s
UPPER("library_book"."title"::text) LIKE UPPER($1) AND "library_book"."pages" >= $2::int4
```

## Parameter types: psycopg and asyncpg

The two libraries bind parameters differently:

- **psycopg with client-side binding**, Django's default, interpolates each value into the statement as a literal.
  Numbers are untyped literals: an integer is `int4`, `int8` or `numeric` by size, a finite float is `numeric`,
  infinity and NaN are `'Infinity'::float8`. Other values carry a cast (`'2024-01-01'::date`, `'{1,2}'::int2[]`);
  a `str` is an untyped literal.
- **psycopg with server-side binding** (`OPTIONS["server_side_binding"]`) sends each parameter with the type OID of
  its Python type: an integer as the smallest of `int2`, `int4`, `int8`, `numeric`, a float as `float8`, a `str` as
  `unknown`.
- **asyncpg** prepares the statement first and lets the server infer each parameter's type from the context; then
  it requires a Python value of exactly that type's encoder. Where the context gives no type (`SELECT $1`), the
  server infers `text`.

Django prepares parameters for psycopg: integer field values are wrapped in psycopg's `Int2`, `Int4` and `Int8`,
JSON values in `Jsonb`, binary values in `Binary`, IP addresses become `ipaddress` objects and ranges psycopg's
`Range`. On asyncpg this fails in two ways:

| Case | asyncpg's error |
|---|---|
| a parameter without a typing context: `Exists()` compiles to `SELECT %s AS "a"` with `Int4(1)`, and `Value(1)`, `Value(True)`, `Value(datetime)` in a select list | `invalid input for query argument $1: Int4(1) (expected str, got Int4)` |
| a psycopg wrapper asyncpg has no encoder for | `expected str, got Jsonb`; `a bytes-like object is required, not 'Binary'` |

[`bind_param()`](/ref/drivers#adorm-drivers-asyncpg-compiler-bind-param-value-server-side-binding-false) unwraps each parameter and casts its
placeholder to the type it has under psycopg in the alias's binding mode (`$1::int4`, `$2::jsonb` with the JSON
text), so the server resolves the statement as under psycopg; a `str` stays uncast. A cast to the parameter's own
type changes nothing where the context already types it. Where it does not, the binding mode decides the result:
`Value(30000) + Value(30000)` is `int4` arithmetic with client-side binding and overflows `int2` with server-side
binding, and `Value(0.1) + Value(0.2)` is `numeric` `0.3` with client-side binding and `float8`
`0.30000000000000004` with server-side binding.

Naive datetimes differ as well. psycopg sends one as `timestamp`, which the server converts to `timestamptz` in the
session's time zone; asyncpg's `timestamptz` encoder reads it as UTC. With the cast to `timestamp` the session's
time zone applies, as under psycopg.

asyncpg sends at most 32767 parameters with one statement and raises `InterfaceError` above that. `DjangoCompiler`
refuses a statement with more and sets its wrapper's `features.max_query_params` to that limit, so on Django 6
`compiler.connection.ops.bulk_batch_size(fields, objs)` returns batch sizes within it (Django 5.2's PostgreSQL backend
does not read it).

## Results

asyncpg returns the same Python types as psycopg under Django for most fields (`Decimal`, `timedelta`, `bytes`,
lists for arrays, aware `datetime` for `timestamptz`). `json` / `jsonb` columns come back as text on both: Django's
psycopg setup loads JSON as text and decodes it in `JSONField.from_db_value()`, so
`compiler.results_iter(results=[rows])`, which applies the fields' converters to rows fetched elsewhere, decodes
asyncpg's text the same way.

Three differences remain in what asyncpg returns:

| Column | Django on psycopg | asyncpg |
|---|---|---|
| `inet`, `cidr` | `str` (`GenericIPAddressField` has no converter) | `ipaddress` objects |
| `uuid` | `uuid.UUID` | `asyncpg.pgproto.pgproto.UUID`, a subclass that compares equal |
| columns added for ordering (`distinct()` with `order_by()` on an expression) or by `extra(select=)` | dropped by `execute_sql()`, which trims rows to `col_count` | returned |

[`DjangoStatement.rows()`](/ref/drivers#rows-records) applies the converters and removes these differences: it
trims each row to `col_count` when the compiler has extra select columns and turns `ipaddress` values into text and
asyncpg's UUIDs into `uuid.UUID`, in arrays too.

Django's psycopg loader relabels every `timestamptz` result with the connection's time zone (UTC under `USE_TZ`)
without converting it, so it is correct only while the session's time zone is the one Django set; asyncpg returns
the instant correctly in any session time zone.

## SQLAlchemy Core

SQLAlchemy's asyncpg dialect compiles a Core statement without an engine or a connection
(`statement.compile(dialect=asyncpg.dialect())`). Its paramstyle is `numeric_dollar`, and it renders a cast for every
parameter from the column or literal type (`$1::INTEGER`, `$2::JSONB`, `$3::TIMESTAMP WITH TIME ZONE`), the same
technique `bind_param()` applies. The compiled object lists parameters by name in `positiontup`; types with a bind
processor (JSON) need it applied before the value goes to raw asyncpg, as SQLAlchemy does when it executes the
statement itself.

```python
compiled = update(book).where(book.c.isbn == "x").values(pages=book.c.pages + 1).compile(dialect=asyncpg.dialect())
str(compiled)
# 'UPDATE library_book SET pages=(library_book.pages + $1::INTEGER) WHERE library_book.isbn = $2::VARCHAR'
[compiled.params[name] for name in compiled.positiontup]   # [1, 'x']
```

SQLAlchemy needs `Table` metadata with column types, which adorm's model schema can supply, and its own translation
of every adorm lookup, function and aggregate; Django's custom lookups, fields and converters are not available
through it.
