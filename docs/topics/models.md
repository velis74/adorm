# Models

An adorm model is a pydantic `BaseModel` subclass of [`adorm.Model`](/ref/models#adorm-model). The same class
describes a database row for adorm and a schema for pydantic and fastapi-viewsets. A model class declares its fields,
its identity (`_meta`) and its relations; this page shows what a model class contains and how its instances behave.

```python
import decimal

from adorm import ForeignKey, Model, ModelMeta


class Club(Model):
    _meta = ModelMeta("club.Club")
    id: int | None = None
    name: str


class Member(Model):
    _meta = ModelMeta("club.Member")
    id: int | None = None
    name: str
    fee: decimal.Decimal = decimal.Decimal("10")
    active: bool = True
    club_id: int | None = None
    club = ForeignKey("club.Club")
```

## Generated models

Models of an application whose schema Django owns are not written by hand: [`adorm generate`](/howto/generating-models)
writes one module per Django app, with the same labels, field names and relations as the Django models, and checks in
CI that the modules are up to date.

```sh
adorm generate --settings myproject.settings --output myproject/adorm_models clubs billing library
```

A generated model is an ordinary adorm model. It also carries the hash of the Django model's schema in
`_meta.schema_hash`, which a driver compares with its own model's at use ([schema checks](/topics/schema-checks)), and a
field whose name clashes with pydantic (`json`, `copy`, `model_dump` …) gets a `_` suffix
and an alias with the original name: code and queries use `json_`, JSON and the JSON schema use `json`.

```python
from myproject.adorm_models.library import Note

note = await Note.objects.get(json_="{}")
note.json_                     # "{}"
note.model_dump()["json"]      # "{}"
```

## Bound models

`Model.objects` returns the model bound to the current [session](/topics/sessions); its query methods build query sets
that run on that session:

```python
member = await Member.objects.get(pk=1)
active = [m async for m in Member.objects.filter(active=True)]
```

`objects` is a class attribute, not a field: instances and abstract models do not have it. It is the model's
default [manager](/topics/managers); a model declares other managers, and the methods of its managers and query sets,
the same way.

## Model identity

`_meta` names the model by its label (`"app_label.ModelName"`), the name operations and drivers use, and names
the primary key field (`"id"` unless given). A class with `_meta` registers in `adorm.models.registry`, which
finds model classes by label and gives the [operation encoding](/ref/encoding) the type of every field.

```python
from adorm.models import registry

registry.get("club.Member")          # Member
registry.model_schema("club.Member").fields["fee"]   # decimal.Decimal
```

## Instances from rows

A row from the database becomes an instance through `from_row()`, built with pydantic's `model_construct()`:
no validation runs, the values are the ones the driver returned. The fields the row contained are the instance's
`model_fields_set`.

```python
member = Member.from_row({"id": 1, "name": "Ana", "fee": decimal.Decimal(12), "club_id": 7})
```

Instances built by the application go through pydantic validation as usual:

```python
member = Member(name="Ana", fee="12.50")       # fee == Decimal("12.50")
Member(name="Ana", fee="abc")                  # pydantic.ValidationError
```

## Deferred fields

A query with `only()` or `defer()` loads some fields; the others are deferred. Reading a deferred field raises
`AttributeError`; adorm never loads a field implicitly, because every load is an awaited operation. Assigning a
deferred field sets it.

```python
member = Member.from_row({"id": 1, "name": "Ana"})     # e.g. from .only("name")
member.name                    # "Ana"
member.fee                     # AttributeError: Member.fee is deferred
member.get_deferred_fields()   # {"fee", "active", "club_id"}
```

## Annotations and other extra values

Names that are neither fields nor relations are pydantic extra values (`extra="allow"`): annotations from a query,
the `to_attr` of a prefetch, or attributes the application sets. They are dumped with the fields and never saved. An
extra value cannot take the name of a class attribute such as `save` or `pk`.

```python
member = Member.from_row({"id": 1, "name": "Ana", "payments_total": decimal.Decimal(30)})
member.payments_total          # Decimal("30")
```

## Relations

A relation is a descriptor, not a field: it is not validated, not dumped and not in the JSON schema. Reading a
`ForeignKey` returns an awaitable, because loading the related row is an operation.

```python
club = await member.club       # loads the club, then caches it
club = await member.club       # from the cache
member.club = other_club       # sets member.club_id
member.club = None             # clears member.club_id
```

A row loaded with `select_related("club")` carries the related row and fills the cache, so the first `await`
needs no operation. Changing `club_id` makes the next `await` load the new club.

A `ReverseOneToOne` (the other side of a one-to-one foreign key) returns an awaitable too; no related row raises the
target's `DoesNotExist`:

```python
restaurant = await place.restaurant
```

A `ReverseForeignKey` (the other side of a foreign key) and a `ManyToMany` return a
[`QuerySet`](/ref/queryset) of the related rows on the owner's session instead, from the target model's `objects`
manager ([related rows](/topics/managers#related-rows)); building it needs no connection, and its operations find
that session's transaction when they run.

```python
club.members.query             # Query("club.Member").filter(club_id=club.pk)
activity.tags.query            # Query("club.Tag").filter(pk__in=<link rows of the activity>)
async for member in club.members.filter(active=True):
    ...
```

In Python input a relation name stands for its column: `Member(name="Ana", club=club)` sets `club_id` and caches
`club`. JSON input carries the column (`"club_id": 7`); a relation name in it is a validation error.

After [`prefetch_related()`](/topics/queries#prefetching-related-objects) the relations hold the prefetched objects:
an `await` of a foreign key runs no operation, and the query set of a reverse or many-to-many relation reads the
prefetched rows for `async for`, `count()` and `exists()`.

### Generic foreign keys

A [`GenericForeignKey`](/ref/models#adorm-genericforeignkey-ct-field-content-type-fk-field-object-id-for-concrete-model-true) points at a
row of any model: the content type field (a foreign key to Django's `ContentType`) names the model, the object id
field holds the row's primary key. `adorm generate` writes one for every Django `GenericForeignKey`. Awaiting it
finds the registered adorm model for the content type and loads the row; the object is cached while both fields keep
their values. A missing row gives `None`, as in Django.

```python
class InvoiceLine(Model):
    _meta = ModelMeta("billing.InvoiceLine")
    id: int | None = None
    content_type_id: int
    object_id: int
    content_type = ForeignKey("contenttypes.ContentType")
    item = GenericForeignKey()


line = await InvoiceLine.objects.first()
item = await line.item                 # a Membership or an Activity
line.item = None                       # clears content_type_id and object_id

line.item = activity                   # object_id = activity.pk; content_type_id when it is cached
await line.save()                      # reads Activity's content type id first when it is not
line = await InvoiceLine.objects.create(invoice_id=1, item=membership, description="Fee", unit_price=120)
```

Assigning an instance sets the object id at once. The content type id is a row of the database: the next `save()`,
`full_clean()`, `create()` or `bulk_create()` takes it through [`adorm.content_types`](/ref/models#adorm-contenttypes)
on the session that writes (from the cache when it is there). Until then `content_type_id` holds the id cached for the
instance's session, or, when none is cached, is unset: reading it raises `AttributeError` and `model_dump()` leaves it
out. `get_or_create()` and `update_or_create()` take an instance in `defaults` too. Details are in the
[reference](/ref/models#generic-assignment). Query paths do not traverse a generic foreign key.

### Content types

[`adorm.content_types`](/ref/models#adorm-contenttypes) is adorm's `ContentType.objects.get_for_model()`: the content
type id of a model, read once per database and cached, and the model of a content type id. A filter on the
fields of a generic foreign key takes the id from it, without a query on every call:

```python
from adorm import content_types

location_type = await content_types.get_id(Location)          # a Get the first time, then from the cache
links = TaggedItem.objects.filter(content_type_id=location_type, object_id__in=ids)
await content_types.get_model(location_type)                  # Location
row = await content_types.get_for_model(Location)             # an instance of the generated ContentType model
await content_types.get_id(LocationProxy)                     # Location's id: a proxy uses its concrete model's
```

A lookup inside a transaction reads on the transaction's slot. The rows are created by Django's `migrate`; adorm
does not create them, and a model without a row raises `ContentType.DoesNotExist`. `migrate` and `flush` (after each
`TransactionTestCase` test) create the rows again, and the Django and asyncpg drivers clear the cache on Django's
`post_migrate` signal; `content_types.clear()` clears it otherwise.

### Multi-table inheritance

A child model of multi-table inheritance derives from its parent's adorm model, with its own label and a primary key
that is the parent link (`activity_ptr_id`). Reading and saving a child reads and writes the rows of both tables.
[`create_from_parent()`](/ref/queryset#async-create-from-parent-parent-values) adds the child's row to a parent row
that exists, leaving the parent's row as it is:

```python
crag = await Activity.objects.get(name="Crag day")
competition = await Competition.objects.create_from_parent(crag, discipline="Climbing")
competition.capacity             # 15, the parent's value
```

## Equality

Instances are equal when they are rows of the same model with the same primary key, whatever their other values,
as in Django. Instances without a primary key are equal only to themselves and cannot be put in a set.

```python
Member.from_row({"id": 1, "name": "Ana"}) == Member.from_row({"id": 1, "name": "Bea"})   # True
```

## Saving

`await instance.save()` writes the fields in `model_fields_set`: the fields the row was loaded with and the fields
assigned since. For an instance loaded with deferred fields, this writes only the loaded ones, as Django does. For a
new instance, it writes the fields given to the constructor or assigned; the target fills in the rest with its
defaults.

```python
member = Member(name="Ana")
await member.save()                          # inserts name; id comes back from the database
member.active = False
await member.save(update_fields=["active"])  # updates one column
```

`save()` builds a [`Save`](/ref/operations) operation and runs it on the instance's executor: the session of the query
set that loaded the instance, or else the [current session](/topics/sessions#the-current-session). See the [Model API](/ref/models) for every option and error.

### Creating many instances

[`bulk_create()`](/ref/queryset#async-bulk-create-objs-batch-size-none-ignore-conflicts-false-update-conflicts-false-update-fields-none-unique-fields-none)
inserts new instances with one operation instead of one `save()` each. Each instance writes the same fields `save()`
would write for it; the target fills in the rest with its defaults. The instances come back with the values the
database set, primary keys included, and are stored instances from then on. As in Django, no `save()` override runs
and no signal fires, and a multi-table inheritance child cannot be inserted this way.

```python
clubs = await Club.objects.bulk_create(Club(name=name) for name in ("Koper", "Piran", "Izola"))
[club.pk for club in clubs]                  # [7, 8, 9]
clubs[0].name = "Koper Sailing"
await clubs[0].save()                        # an UPDATE: the instance is stored

# 10 000 rows, 1 000 per INSERT; one transaction, so a failing row stores none of them.
await Member.objects.bulk_create(members, batch_size=1000)

# Rows whose primary key exists are skipped; with update_conflicts they update the stored row instead.
await Member.objects.bulk_create(members, ignore_conflicts=True)
await Member.objects.bulk_create(members, update_conflicts=True, update_fields=["fee"], unique_fields=["pk"])
```

Under `ignore_conflicts` the database reports no primary keys, so the instances keep `None` for a generated one.

## Validating

Pydantic validates the values given to the constructor. `await instance.full_clean()` validates the instance as the
target does before a save: on the Django driver Django's `Model.full_clean()` runs the field validators, the model's
`clean()`, the unique checks and the constraints, against the database in the current transaction. Errors raise
`adorm.ValidationError` with the errors by adorm field name, each with Django's message, `code` and `params`;
`exclude` leaves fields out:

```python
invoice.due = datetime.date(2025, 1, 1)
try:
    await invoice.full_clean()
except adorm.ValidationError as exc:
    exc.message_dict              # {"due": ["The due date cannot be before the issue date."]}
    [error] = exc.error_dict["due"]
    error.code                    # None: clean() raised the error without a code
await invoice.full_clean(exclude=["due"])

club.name = "x" * 101
try:
    await club.full_clean()
except adorm.ValidationError as exc:
    [error] = exc.error_dict["name"]
    error.code, error.params["limit_value"]       # ("max_length", 100)
```

A model's `save()` that calls `full_clean()` raises the same errors from `save()` and `objects.create()` on the
Django driver.

See [`full_clean()`](/ref/models#full-clean-exclude-none-validate-unique-true-validate-constraints-true) in the Model
API for the instance it validates and the error keys.

## Deleting

`await instance.delete()` deletes the instance's row and the rows that cascade from it, and returns
`(total, {label: count})`. When rows were deleted, the instance's primary key becomes `None`. On the Django driver the
Django model's own `delete()` runs, so an override (a soft delete) applies; the native PostgreSQL driver refuses the
delete of a model with an override.

```python
total, per_model = await member.delete()     # (3, {"clubs.Member": 1, "clubs.Membership": 2}); member.pk is None
```

A `PROTECT` or `RESTRICT` reference forbids the delete: it raises `ProtectedError` or `RestrictedError` with the
referencing objects as instances of the registered models, and the instance keeps its primary key. See
[`delete()`](/ref/models#delete) in the Model API.
