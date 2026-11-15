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
`_meta.schema_hash`, and a field whose name clashes with pydantic (`json`, `copy`, `model_dump` …) gets a `_` suffix
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

## Deleting

`await instance.delete()` deletes the instance's row and the rows that cascade from it, and returns
`(total, {label: count})`. When rows were deleted, the instance's primary key becomes `None`. On the Django driver the
Django model's own `delete()` runs, so an override (a soft delete) applies.

```python
total, per_model = await member.delete()     # (3, {"clubs.Member": 1, "clubs.Membership": 2}); member.pk is None
```

A `PROTECT` or `RESTRICT` reference forbids the delete: it raises `ProtectedError` or `RestrictedError` with the
referencing objects as instances of the registered models, and the instance keeps its primary key. See
[`delete()`](/ref/models#delete) in the Model API.
