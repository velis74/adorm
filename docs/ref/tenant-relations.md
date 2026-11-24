# Tenant relations

A tenant relation reads, from an instance of a shared model, the rows of a model that lives in several tenant
schemas: the person's enrolments in every organisation's schema, the user's memberships in every club's schema. The
declaration is on the Django model and `adorm generate` emits it on the generated model. Reading runs two operations:
one lists the instance's schemas from shared tables, the other reads the rows of every schema in one statement
(`UNION ALL`), whose ordering, slice, count and aggregates the database computes. The
[schema-per-tenant how-to](/howto/schema-per-tenant#reading-across-tenants) shows it in an application.

## Declaration

### `adorm.contrib.django.TenantRelation(to, *, field, schema, tenant_models=None, session=None)`

A class attribute of a shared Django model that declares the relation. Django ignores it: it adds no column, no
migration and no system check, and reading it (on the class or an instance) returns the declaration itself. Importing
it does not touch Django's app registry.

| Argument | Meaning |
|---|---|
| `to` | the tenant model's label, `"app_label.Model"` |
| `field` | the name of the tenant model's `ForeignKey` (or `OneToOneField`) to the declaring model's primary key |
| `schema` | where the schemas of an instance come from: a Django query path from the declaring model to a field that holds a schema name, or a Django expression of the declaring model's rows that gives one |
| `tenant_models` | model labels and app labels whose tables live in each tenant schema; an app label stands for all its models, automatic many-to-many through models included. By default the app of `to`; `to` is always included |
| `session` | `None`, or the dotted import path of a function `session(schema, executor)` that returns the executor of the instances read from `schema` |

```python
from django.db import models

from adorm.contrib.django import TenantRelation


class Person(models.Model):
    name = models.CharField(max_length=100)
    enrolments = TenantRelation(
        "tenancy.Enrolment",
        field="person",
        schema="organisations__organisation__schema_name",
        tenant_models=["tenancy.Team", "tenancy.Enrolment"],
    )
```

The constructor checks the shape of the arguments: a `to` that is not a model label, a `field` that is not an
identifier, a `schema` path with an empty or private segment, a `tenant_models` entry that is neither label and a
`session` that is not a dotted path raise `ValueError`; a `schema` that is neither a string nor an expression and a
string `tenant_models` raise `TypeError`.

`adorm generate` checks the declaration against the installed models and raises
[`CodegenError`](./codegen#adorm-codegen-codegenerror) naming `Model.attribute` when `to` is not installed, `field` is
not a foreign key of `to` to the declaring model's primary key, the `schema` path does not lead to a field that is not
a relation, a `tenant_models` entry names no installed app or model, or the `schema` expression uses a class outside
`F`, `Value` (of a `str` or an `int`), `Concat`, `Lower`, `Upper`, `Replace`, `Left`, `Right`, `LPad`, `Coalesce` and
`Cast` to `CharField` or `TextField`. A path segment is a field name, a reverse relation's query name or its accessor.
A tenant model that is not generated with the declaring model is reported as `CodegenWarning`. A concrete model gets
the declarations of its own class and of its abstract bases; a multi-table inheritance child inherits its parent's
through the generated parent class.

### `adorm.TenantRelation(target, *, column, schema, models, session=None)`

The descriptor `adorm generate` emits on the generated model:

```python
class Person(Model):
    ...
    enrolments = TenantRelation(
        "tenancy.Enrolment",
        column="person_id",
        schema="organisations__organisation__schema_name",
        models=("tenancy.Enrolment", "tenancy.Team"),
    )
```

- `target`: the label of the tenant model.
- `column`: the tenant model's field that holds the owner's primary key.
- `schema`: the adorm field path from the owner to the schema name, or an adorm expression of the owner's rows that
  gives it (`Concat(Value("club_"), Cast(F("clubs__club_id"), str))`).
- `models`: the labels of the models whose tables live in each schema, stored as a `frozenset`; without `target`
  `ValueError`.
- `session`: `None`, or the dotted path of the function that gives the executor of a returned instance; imported on
  first use and cached. Without it, instances are bound to the executor that read them.

It is not a [`Relation`](./models#adorm-models-relation-target): query paths do not traverse it and
[`get_relations()`](./models#adorm-models-get-relations-model) leaves it out;
[`get_tenant_relations()`](./models#adorm-models-get-tenant-relations-model) lists it. It is absent from validation,
serialisation and the JSON schema. On the class it returns itself; on an instance it returns a
[`TenantQuerySet`](#adorm-tenantqueryset) of the instance's rows of `target`. An instance without a primary key value
raises `ValueError`.

## Reading

### `adorm.TenantQuerySet`

The rows of the tenant model that belong to the owner in every tenant schema the owner belongs to, read on the owner's
executor. Its attributes are `relation`, `owner`, `model` (the tenant model class), `executor` and `query`, the query of
one schema's rows: the target's `objects` manager filtered by the relation's column. It is immutable; building does no
I/O.

```python
person = await Person.objects.get(name="Ana")
async for enrolment in person.enrolments.filter(role="coach").order_by("-joined")[:10]:
    ...
await person.enrolments.count()
await person.enrolments.aggregate(Sum("fee"), coaches=Count("id", filter=Q(role="coach")))
[team async for team in person.enrolments.values_list("team__name", flat=True)]
```

The building methods return a new tenant query set: `all()`, `filter()`, `exclude()`, `annotate()`, `alias()`,
`order_by()`, `values()`, `values_list(flat=, named=)`, `only()`, `defer()`, `select_related()`, `none()` and slicing,
as on a [`QuerySet`](./queryset). An annotation that contains an aggregate raises `NotSupportedError`: grouping per
schema is not grouping across schemas. A name starting with [`HIDDEN_PREFIX`](#adorm-query-tenants-hidden-prefix)
raises `ValueError`.

The terminal methods are coroutines (`async for` iterates):

| Method | Result |
|---|---|
| `async for` | the rows, formatted as the row format says: instances, dicts, tuples, values or named tuples |
| `first()`, `last()` | the first or last row by the ordering (the primary key without one), `None` when there is none; `last()` on a sliced tenant query set raises `TypeError` |
| `get(*args, **kwargs)` | the one row matching the conditions; none raises the target's `DoesNotExist`, several its `MultipleObjectsReturned`. The same primary key in two schemas counts as two rows |
| `count()`, `exists()` | the number of rows of all schemas together, whether there is one |
| `aggregate(*args, **kwargs)` | the aggregates over the rows of all schemas together, arguments as for [`QuerySet.aggregate()`](./queryset) |
| `schemas()` | the owner's schema names, sorted and distinct |

Each terminal method runs, on the owner's executor:

1. a [`Select`](./operations#adorm-operations-select-query) on the owner's model that annotates the `schema` path or
   expression, leaves out `NULL`, and returns the distinct schema names, sorted. Each name is checked as a
   [`TenantSchema`](./query#adorm-tenantschema-schema-models) name; one that is not raises `ValueError` before anything
   else runs;
2. when there are schemas, one more operation: a `Select`, `Get`, `Count`, `Exists` or
   [`Aggregate`](./operations#adorm-operations-aggregate-query-aggregations) over the union (`UNION ALL`) of the query
   in each schema ([`in_tenant_schema()`](./query#in-tenant-schema-schema-models)); for one schema, the query itself.

Without schemas the result is empty and the second operation does not run: no rows, `0`, `False`, `None`, the
target's `DoesNotExist`, and for `aggregate()` the values over no rows (`0` for `Count`, the `default` or `None`).
`none()` and a slice that leaves no rows run no operation at all.

The ordering and the slice apply to the rows of all schemas together; the target model's `Meta.ordering` does not
order them, so without `order_by()` the order is the database's. Each ordering key, a field path through any
relation or an expression, is computed in each schema's query as a hidden column, and the union is ordered by those
columns. Model rows carry the schema they come from as a hidden column too. An aggregate's argument is computed in each
schema's query (its `filter` folded in as `Case(When(filter, then=argument))`) and aggregated over the union. A
schema's query that joins a model of `models` reads that schema's table; a join to another model reads the table the
connection resolves (`public`).

Writes through the relation raise `NotSupportedError` ("… is not supported on a tenant relation") when called,
before any I/O: `create()`, `bulk_create()`, `get_or_create()`, `update_or_create()`, `create_from_parent()`,
`update()`, `delete()`, `in_bulk()`, `add()`, `remove()`, `clear()` and `set()`. So do `select_for_update()`,
`distinct()`, `prefetch_related()`, `union()`, `intersection()`, `difference()`, `|` and `&`.

### `adorm.query.tenants.HIDDEN_PREFIX`

`"_adorm_"`: the prefix of the hidden columns a `TenantQuerySet` adds to the rows it reads and leaves out of the rows
it returns.

## Returned instances

An instance a tenant query set returns has its schema in
[`ModelState.tenant_schema`](./models#adorm-models-modelstate): a `TenantSchema` of the schema and the relation's
`models`. Its own operations run in that schema, on any executor, since the qualification is in the SQL:

- `save()`, `delete()` (cascades included) and `full_clean()` (unique checks included) write and validate the
  schema's row;
- `refresh_from_db()` reads it;
- `await enrolment.team`, `team.enrolment_set` and `select_related()` rows of a model of `models` read the schema's
  rows; `await enrolment.person` reads the shared row;
- the writes of such a relation's query set (`team.enrolment_set.create()`, `bulk_create()`, `create_from_parent()`,
  `update()`, `delete()`, `get_or_create()`, `update_or_create()`) and its `in_bulk()` raise `NotSupportedError`
  without running an operation.

Instances with the same primary key in two schemas are not equal and hash differently
([Equality and hash](./models#equality-and-hash)). With `session`, each instance is bound to the executor
`session(schema, executor)` returns, for instance the tenant's session of the
[schema-per-tenant how-to](/howto/schema-per-tenant#the-tenant-in-the-session).

## Errors

- `NotSupportedError`: a write or another refused method through the relation, at the call; a driver without the
  constructs `clause.tenant_schema` (and `aggregate.combined` for `aggregate()`), before the union reaches a slot (the
  listing runs first); a qualification the backend's SQL does not allow
  ([schema qualification](./drivers#adorm-drivers-django-tenants)).
- `ValueError`: an owner without a primary key value; a schema name from the listing that is not a valid
  `TenantSchema` name.
- A schema or a table that does not exist: the database's error (`ProgrammingError` on PostgreSQL,
  `OperationalError` on SQLite). No schema is skipped.
- A profile's [`check_operation`](/topics/profiles#refusing-operations) hook sees both operations and can refuse either.
