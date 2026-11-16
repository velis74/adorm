# Model API

The model base class, model identity, relation descriptors, managers and the model registry, importable from
`adorm.models`. `Model`, `ModelMeta`, `Manager`, `driver_method`, the relation descriptors `ForeignKey`,
`ReverseForeignKey`, `ReverseOneToOne`, `ManyToMany` and `GenericForeignKey`, and the content type cache
`content_types` (`ContentTypes`) are also importable from `adorm`.

```python
from adorm import driver_method, ForeignKey, Manager, Model, ModelMeta, ReverseOneToOne
from adorm.models import get_managers, get_relations, model_schema, registry, set_executor_resolver
```

The examples on this page use these models:

```python
import datetime
import decimal

from pydantic import Field

from adorm import ForeignKey, Model, ModelMeta


class Club(Model):
    _meta = ModelMeta("club.Club")
    id: int | None = None
    name: str
    founded: datetime.date | None = None


class Member(Model):
    _meta = ModelMeta("club.Member")
    id: int | None = None
    name: str
    fee: decimal.Decimal = decimal.Decimal("10")
    active: bool = True
    json_: str | None = Field(default=None, alias="json")
    club_id: int | None = None
    club = ForeignKey("club.Club")
```

## Models

### `adorm.Model`

Base class of adorm models, a subclass of pydantic `BaseModel`. A model declares `_meta` (a
[`ModelMeta`](#adorm-modelmeta-label-pk-id-schema-hash-none-orm-names-none-concrete-model-none)), its fields as pydantic fields and its relations as [`Relation`](#adorm-models-relation-target)
descriptors. A subclass without its own `_meta` inherits its parent's; a model without any `_meta` is abstract: it
cannot load or save rows and its instances compare by identity.

The model configuration:

| Setting | Value | Effect |
|---|---|---|
| `extra` | `"allow"` | names that are neither fields nor relations (annotations, the `to_attr` of a prefetch) are extra values |
| `ignored_types` | `(Relation, <the objects descriptor>)` | relations and `objects` are class attributes, absent from validation, serialisation and the JSON schema |
| `serialize_by_alias` | `True` | a field with an alias is dumped under the alias |
| `validate_by_name`, `validate_by_alias` | `True` | input may use the field name or the alias |
| `protected_namespaces` | `()` | field names may start with `model_` (a name equal to a pydantic attribute still raises) |

Input (the constructor, `model_validate()`) also accepts:

- `pk` for the primary key field: `Club(pk=5, name="Chess")` sets `id`. Giving both `pk` and the field is a
  `ValidationError`.
- A relation name, from Python input only; see [`ForeignKey`](#adorm-foreignkey-target-column-none).

An extra value may not have the name of a class attribute (`save`, `pk`, `model_config` …), which would hide it:
such input is a `ValidationError`.

A field whose name clashes with a pydantic method (`model_dump`, `json` …) has a `_` suffix and the original name
as its alias: code and queries use `json_`, JSON and the schema use `json`.

```python
member = Member.model_validate({"name": "Ana", "json": "{}"})
member.json_                    # "{}"
member.model_dump()["json"]     # "{}"
"club" in Member.model_json_schema()["properties"]   # False
```

Class creation raises `TypeError` when `_meta` is not a `ModelMeta` or is declared as a private attribute
(`_meta: ModelMeta = ...`; annotate it `ClassVar[ModelMeta]` or leave it unannotated), when the primary key named
by `_meta` is not a field, and when a `ForeignKey` column is not a field. A model with `_meta` in its own class body
registers in [`registry`](#adorm-models-registry).

Every model class has its own `DoesNotExist` and `MultipleObjectsReturned`, derived from its parents' and at the top
from [`ObjectDoesNotExist`](./exceptions) and `MultipleObjectsReturned`.

#### `from_row(row, *, executor=None)`

Class method: an instance of a stored row, built with `model_construct()` (no validation). `row` maps field names
or aliases to values; `pk` stands for the primary key field.

- A field missing from `row` is deferred.
- A key that is a relation name holds the related row: a mapping (built with the related model's `from_row()`), an
  instance of the target (`TypeError` for another model), or `None` for no related row. It fills the relation's
  cache, as `select_related()` does.
- Any other key is an extra value. A key that names a class attribute raises `ValueError`, as do both `pk` and the
  primary key field.

`executor` runs the instance's later operations (`save()`, relation loads) and those of the related instances
built from `row`. The row must contain the primary key (`ValueError`); an abstract model raises `TypeError`.

```python
member = Member.from_row({"id": 1, "name": "Ana", "club_id": 7, "visits": 3})
member.model_fields_set     # {"id", "name", "club_id"}
member.visits               # 3, an extra value
member.fee                  # AttributeError: Member.fee is deferred
```

#### Deferred fields

A field the row did not load (a query with `only()` or `defer()`) is deferred. Reading it raises
`AttributeError`, so `hasattr()` and `getattr(obj, name, default)` treat it as missing. Assigning it sets it and
adds it to `model_fields_set`. `model_dump()` and `repr()` leave deferred fields out.

```python
member = Member.from_row({"id": 1, "name": "Ana"})
member.get_deferred_fields()   # {"fee", "active", "json_", "club_id"}
member.fee = decimal.Decimal(12)
member.get_deferred_fields()   # {"active", "json_", "club_id"}
member.model_dump()            # {"id": 1, "name": "Ana", "fee": Decimal("12")}
```

#### `objects`

Class attribute: the model's default [`Manager`](#adorm-manager-orm-name-none-method-manager-none), `Manager()` unless the class declares
its own. Reading it returns the model bound to the [current session](./sessions#adorm-current-session), a
[`BoundModel`](./sessions#adorm-boundmodel), evaluated at every access. Reading it on an instance or on an abstract
model raises `AttributeError`. A class that sets `objects` to something other than a manager raises `TypeError`.

```python
await Member.objects.filter(active=True).count()
clubs = Club.objects            # stays bound to the session current now
```

#### `get_deferred_fields()`

The names of the fields the instance was loaded without and that were not assigned since.

#### `pk`

The primary key value, read and written through the field `_meta.pk` names.

#### Equality and hash

Two instances are equal when they belong to the same model (the same `_meta.label`, so a subclass instance equals
its model's instance) and have the same primary key value that is not `None`. Other field values do not take part.
An instance without a primary key value equals only itself and is unhashable (`TypeError`). The hash is
`hash((label, pk))`. Instances of an abstract model compare and hash by identity.

```python
a = Member.from_row({"id": 1, "name": "Ana"})
b = Member.from_row({"id": 1, "name": "Bea"})
a == b               # True
len({a, b})          # 1
Member(name="Ana") == Member(name="Ana")   # False
```

#### `save(*, update_fields=None, force_insert=False, force_update=False)`

Coroutine: write the instance with a [`Save`](./operations#adorm-operations-save-model-pk-values-update-fields-none-force-insert-false-force-update-false) operation on the instance's
executor.

- The values written are the fields in `model_fields_set` (the loaded and the assigned ones), the primary key
  excepted. Fields the instance never set are left to the target, so its defaults apply on insert.
- Without a primary key value the row is inserted; with one it is updated, or inserted when no row has that key.
- `update_fields` limits the update to those fields and never inserts. A relation name stands for its column, a
  generic foreign key's name for both of its fields, the primary key (`"pk"` or its field name) is ignored, and an
  empty `update_fields` saves nothing. A named field the instance has but never set (a default) is written too.
- A content type field of a pending [`GenericForeignKey`](#generic-assignment) assignment is resolved first, on the
  instance's executor.
- `force_insert` always inserts; `force_update` never inserts.
- The values the target returns (the primary key, database defaults) are set on the instance and added to
  `model_fields_set`; the instance keeps the executor for later operations.

```python
member = Member(name="Ana")              # model_fields_set == {"name"}
await member.save()                      # Save("club.Member", None, {"name": "Ana"})
member.id                                # set from the result

member = Member.from_row({"id": 1, "name": "Ana"}, executor=executor)   # loaded with only("name")
member.active = False
await member.save()                      # Save("club.Member", 1, {"name": "Ana", "active": False})
await member.save(update_fields=["active"])
```

Raises `TypeError` for a string `update_fields` and a result that is not a mapping, `ValueError` for an
`update_fields` name that is not a field, one that is deferred, and `update_fields` or `force_update` without a
primary key value, and [`InterfaceError`](./exceptions) when no executor is available. A pending generic foreign key
assignment whose model has no content type row raises the content type model's `DoesNotExist`
([`ObjectDoesNotExist`](./exceptions) when that model is not registered) before the `Save` operation. A failed or
cancelled save leaves the instance unchanged, except for a content type field it resolved before the `Save`
operation.

#### `full_clean(*, exclude=None, validate_unique=True, validate_constraints=True)`

Coroutine: validate the instance as the target validates one before a save, with a
[`FullClean`](./operations#adorm-operations-fullclean-model-pk-values-exclude-validate-unique-true-validate-constraints-true-adding-true)
operation on the instance's executor. On the Django driver it is Django's `Model.full_clean()`: field validation
(`clean_fields()`), the model's `clean()`, the unique checks (`validate_unique()`) and the model's constraints
(`validate_constraints()`). Every query of the validation runs on the session's slot, on the profile's database
alias, whatever the project's Django routers choose, so it sees the session's open transaction.

- The instance validated is the one `save()` would write: its loaded and assigned fields over the stored row (for an
  instance with a primary key that is not `_state.adding`; the row is read when fields are missing), otherwise over
  the target's defaults. A unique check of a stored instance leaves its own row out.
- `exclude` names fields (or relations, for their column; `"pk"` for the primary key) that are not validated.
  `validate_unique` and `validate_constraints` switch the unique checks and the constraints off.
- Errors raise [`ValidationError`](./exceptions) with `error_dict` (and `message_dict`) by adorm name: a field's
  errors under the field name, a foreign key's under its relation's name (`"club"`, not `"club_id"`), errors of the
  whole model (`clean()`, a constraint that is not a unique constraint over one field) under `"__all__"`.
- Each error keeps Django's `message`, `code` and `params`: `"blank"`, `"max_length"` with `limit_value` and
  `show_value`, `"unique"` (one field) or `"unique_together"` (several), a constraint's `violation_error_code`. The
  field names of the `unique_check` param are adorm names; a Django model instance or class in `params` (`model`,
  `model_class`) is the model's label, a lazy translation is text. Messages are in the caller's active language; a
  plural message (Django's `ngettext_lazy`, as in `max_length`) is formatted.
- Without errors, the values the validation cleaned (Django's `to_python()`, changes `clean()` made) are set on the
  instance's loaded fields; deferred fields stay deferred and `model_fields_set` does not change.

```python
invoice = await Invoice.objects.get(number="2026-0001")
invoice.due = datetime.date(2025, 1, 1)
await invoice.full_clean()
# ValidationError({'due': ['The due date cannot be before the issue date.']})

copy = Membership(club_id=board.club_id, member_id=board.member_id, since=today, fee=1)
await copy.full_clean()
# ValidationError({'__all__': ['Membership with this Club and Member already exists.']})
await copy.full_clean(exclude=["member"])          # the constraint names an excluded field: no error

try:
    await copy.full_clean()
except adorm.ValidationError as exc:
    [error] = exc.error_dict["__all__"]
    error.code                    # 'unique_together'
    error.params["unique_check"]  # ('club', 'member')
```

Raises `ValueError` for an `exclude` name that is neither a field nor a relation, `TypeError` for a string
`exclude`, and [`InterfaceError`](./exceptions) when no executor is available. A driver that does not declare
`full_clean` raises `NotSupportedError` (the demo's memory driver).

#### `delete()`

Coroutine: delete the instance's row and the rows that cascade from it with a
[`DeleteInstance`](./operations#adorm-operations-deleteinstance-model-pk) operation on the instance's executor, which
runs the target's delete of one instance (on the Django driver the model's `delete()`, an override included; the
native PostgreSQL driver raises `NotSupportedError` for a model with an override). Returns
`(total, {label: count})`; when rows were deleted it sets the primary key to `None`, otherwise (a missing row, a
`delete()` override that deletes nothing) the instance keeps it.

```python
payment = await Payment.objects.get(reference="SI00 0001")
await payment.delete()        # (1, {"billing.Payment": 1}); payment.pk is None
```

Raises `ValueError` without a primary key value and [`InterfaceError`](./exceptions) when no executor is available. A
reference that forbids the delete raises `ProtectedError` or `RestrictedError` with instances of the registered
models as its objects, as [`QuerySet.delete()`](./queryset#async-delete) does; the instance keeps its primary key.

### `adorm.ModelMeta(label, pk="id", *, schema_hash=None, orm_names=None, concrete_model=None)`

The identity of a model: its `label` (`"app_label.ModelName"`) and the name of its primary key field. A frozen
dataclass; an invalid label or pk name raises `ValueError`.

- `schema_hash`: the hash [`adorm generate`](/howto/generating-models) computed from the ORM model
  ([`adorm.codegen.compute_schema_hash`](./codegen#adorm-codegen-compute-schema-hash-label-pk-fields-relations-parents-managers)),
  `None` for a model written by hand. An empty or non-string value raises `ValueError`.
- `orm_names`: the target ORM's name of each field or relation whose Python name differs, by Python name; `None` is an
  empty mapping. A generated model fills it for the names it renames. A model class's `_meta` also holds the entries
  of its model bases for the fields and relations it inherits, so a multi-table inheritance child or a proxy maps an
  inherited name like its parent. It is stored read-only; a name that is not an identifier raises `ValueError`, a
  model class whose `orm_names` names no field or relation of it raises `TypeError`.
- `concrete_model`: the label of the concrete model a proxy model stands for (Django's `concrete_model`), `None` for a
  concrete model. [`adorm generate`](/howto/generating-models) sets it on every proxy. The content type of a proxy is
  its concrete model's ([`natural_key()`](#adorm-models-natural-key-model-for-concrete-model-true)). An invalid label,
  or the model's own label, raises `ValueError`.
- `orm_name(name)`: the ORM name of the field or relation `name`, `name` itself when `orm_names` does not list it.

```python
class Payment(Model):
    _meta = ModelMeta("club.Payment", pk="code")
    code: str | None = None
    amount: decimal.Decimal


class Note(Model):
    _meta = ModelMeta("club.Note", schema_hash="387e7a453a16eb5e", orm_names={"json_": "json"})
    id: int | None = None
    json_: str = Field(default="", alias="json")


Note._meta.orm_name("json_")    # "json"
Note._meta.orm_name("id")       # "id"


class SignedNote(Note):
    _meta = ModelMeta("club.SignedNote", pk="note_ptr_id")
    note_ptr_id: int | None = None


SignedNote._meta.orm_name("json_")    # "json", inherited from Note


class PinnedNote(Note):
    _meta = ModelMeta("club.PinnedNote", concrete_model="club.Note")    # a proxy of Note
```

### `adorm.models.ModelState`

Per-instance state outside the fields, the instance's private attribute `_state`:

| Attribute | Meaning |
|---|---|
| `adding` | `True` until the instance is loaded or saved |
| `executor` | the executor that loaded or last saved the instance, `None` otherwise |
| `related` | cached related instances by relation name; the prefetched rows of a reverse or many-to-many relation; for a generic foreign key the content type, the object id and the instance they were loaded for |
| `generic` | by generic foreign key name, the assigned instance whose content type the next write resolves, with the object id and the content type id set at assignment (`None` when it was not cached) |

A copy (`copy.copy()`, `model_copy()`) gets its own state with the same executor and a copy of the cache and the
pending assignments; a deep copy deep-copies them. A pickled instance comes back without its executor.

## Relations

### `adorm.models.Relation(target)`

Base class of relation descriptors. `target` is the label of the related model, `name` the attribute name the
descriptor is assigned to. `target_model()` returns the registered class of `target` (`LookupError` when none is
registered). A relation is not a pydantic field: it is not validated, dumped or part of the JSON schema. A bare
`Relation` names the target only: query paths traverse it (`memberships__active`), but it carries no join columns,
and on an instance the attribute is the descriptor itself.

### `adorm.ForeignKey(target, *, column=None)`

A many-to-one (or one-to-one) relation stored in the field `column`, by default `<name>_id`. The column is an
ordinary field; the relation reads and writes it.

- `await instance.rel` returns the related instance, or `None` when the column is `None`. The instance is loaded
  with a `Get` operation (`Query(target).filter(pk=column value)`) on the owner's executor and cached on the owner.
  The cache serves later awaits while the cached instance's primary key equals the column; a row loaded with
  `select_related()` fills it.
- A missing row raises the target's `DoesNotExist`.
- Assigning an instance of the target sets the column to its primary key and caches the instance; assigning `None`
  clears both. Another model's instance raises `TypeError`, an instance without a primary key value `ValueError`.
- Reading the relation while the column is deferred raises `AttributeError`.
- On the class, the attribute is the descriptor.

```python
class Member(Model):
    _meta = ModelMeta("club.Member")
    id: int | None = None
    club_id: int | None = None
    club = ForeignKey("club.Club")
    sponsor_ref: int | None = None
    sponsor = ForeignKey("club.Club", column="sponsor_ref")


club = await member.club          # Get(Query("club.Club").filter(pk=member.club_id))
club is await member.club         # True, from the cache
member.club = other_club          # member.club_id == other_club.pk
member.club = None                # member.club_id is None
Member.club.column                # "club_id"
```

A relation name in Python input (the constructor, `model_validate()`) sets the column before the fields are
validated, so it fills a required column too, and caches the instance: `Member(name="Ana", club=club)` sets
`club_id`. Its value is a saved instance of the target or `None`. Anything else, the relation together with its
column, and a relation name in JSON input (`model_validate_json()`) are a `ValidationError`.

```python
Member(name="Ana", club=club).club_id                         # club.pk
Member.model_validate_json('{"name": "Ana", "club": 5}')      # ValidationError: give its column 'club_id'
Member.model_validate_json('{"name": "Ana", "club_id": 5}')   # club_id == 5
```

### `adorm.ReverseForeignKey(target, *, column)`

The reverse side of a `ForeignKey`: the rows of `target` whose field `column` holds the owner's primary key. On an
instance it returns those rows from the target's [`objects`](#objects) manager on the owner's executor (the session
that loaded it, else the current session): a query set of the manager's `queryset_class` that starts from its
`get_queryset()` and carries its ORM manager, as Django's related managers derive from the default manager. An
executor that is not a `Session` gets a query set of that class with the ORM manager but without a client-side
`get_queryset()`. An instance without a primary key value raises `ValueError`; `column` that is not an identifier
raises `ValueError`.

```python
class Club(Model):
    _meta = ModelMeta("club.Club")
    id: int | None = None
    members = ReverseForeignKey("club.Member", column="club_id")


club.members.query           # Query("club.Member").filter(club_id=club.pk), plus the manager's conditions
await club.members.count()   # Count on the club's session
Club.members.column          # "club_id"
```

### `adorm.ReverseOneToOne(target, *, column)`

The reverse side of a one-to-one `ForeignKey`: the row of `target` whose field `column` holds the owner's primary key
(Django: the other side of a `OneToOneField`, also the parent's side of multi-table inheritance).

- `await instance.rel` returns that row as an instance of `target`, loaded with a `Get` operation
  (`Query(target).filter(<column>=instance.pk)`) on the owner's executor and cached on the owner. A row loaded with
  `select_related()` fills the cache.
- No such row raises the target's `DoesNotExist`, also when `select_related()` cached no row.
- An instance without a primary key value raises `ValueError`; `column` that is not an identifier raises
  `ValueError`. On the class, the attribute is the descriptor.

```python
class Place(Model):
    _meta = ModelMeta("club.Place")
    id: int | None = None
    restaurant = ReverseOneToOne("club.Restaurant", column="place_id")


class Restaurant(Model):
    _meta = ModelMeta("club.Restaurant", pk="place_id")
    place_id: int | None = None
    place = ForeignKey("club.Place")


restaurant = await place.restaurant   # Get(Query("club.Restaurant").filter(place_id=place.pk))
restaurant is await place.restaurant  # True, from the cache
```

### `adorm.ManyToMany(target, *, through, source_column, target_column)`

A many-to-many relation through the model labelled `through`. Each `through` row links the owner in `source_column`
with a `target` row in `target_column`. For a link table without a model of its own, `through` is the label of the
ORM's generated through model (Django: `"<app_label>.<Model>_<field>"`, e.g. `"club.Activity_tags"`). On an instance
it returns the related rows from the target's `objects` manager on the owner's executor, as `ReverseForeignKey` does;
an instance without a primary key value raises `ValueError`. A forward `ForeignKey` and a `ReverseOneToOne` load
their row without the target's manager, as Django's base manager does.

```python
class Activity(Model):
    _meta = ModelMeta("club.Activity")
    id: int | None = None
    tags = ManyToMany("club.Tag", through="club.Activity_tags", source_column="activity_id", target_column="tag_id")


activity.tags.query
# Query("club.Tag").filter(
#     pk__in=Query("club.Activity_tags").filter(activity_id=activity.pk).values("tag_id"))
```

### Prefetched rows

After [`prefetch_related()`](./queryset#prefetch-related-lookups) of a `ReverseForeignKey` or a `ManyToMany`, the
query set the relation returns on the instance holds the prefetched rows: `async for`, `count()` and `exists()` read
them without an operation. Its building methods (`filter()`, `order_by()` …) return query sets that run again, and so
do its other terminal methods. A prefetched `ForeignKey`, `ReverseOneToOne` or `GenericForeignKey` fills the
relation's cache, so `await instance.rel` runs no operation.

```python
club = await Club.objects.prefetch_related("memberships").get(slug="koper-sailing")
[m.role async for m in club.memberships]          # no operation
await club.memberships.count()                    # 3, no operation
await club.memberships.filter(active=True).count()   # a Count operation
```

### `adorm.GenericForeignKey(ct_field="content_type", fk_field="object_id", *, for_concrete_model=True)`

A relation to a row of any model (Django's `GenericForeignKey`): the content type's primary key in the content type
field and the row's primary key in the field `fk_field`. The content type field is the `ForeignKey` named `ct_field`,
whose column and target are used, or a field `<ct_field>_id` holding a primary key of
[`CONTENT_TYPE_MODEL`](#adorm-models-content-type-model). `for_concrete_model` (a `bool`, else `TypeError`) makes an
assigned proxy instance store its concrete model's content type, as in Django.

- `await instance.rel` returns the related instance. The content type id names the registered model through
  [`content_types.get_model()`](#adorm-contenttypes) (cached; a miss is a `Get` operation), or through the content
  type relation's cache (`select_related("content_type")`); its row with the primary key in `fk_field` (converted to
  the primary key's type, `"7"` to `7`) is loaded with a `Get` operation on the owner's executor.
- The instance is cached on the owner for the values of both fields it was loaded for, and serves while they keep
  them; a field changed while a load or prefetch runs leaves the cache for the old values. A prefetch fills the
  cache.
- Either field `None`, or no row with that primary key, gives `None`. A content type of a model that is not
  registered raises `LookupError`; reading the relation while either field is deferred raises `AttributeError`.
- It is not a `Relation`: query paths do not traverse it, it is not part of the
  [schema](#adorm-models-model-schema-model), and [`get_relations()`](#adorm-models-get-relations-model) leaves it out.
  A model class checks that both fields exist (`TypeError`).

```python
class InvoiceLine(Model):
    _meta = ModelMeta("billing.InvoiceLine")
    id: int | None = None
    content_type_id: int
    object_id: int
    content_type = ForeignKey("contenttypes.ContentType")
    item = GenericForeignKey()            # GenericForeignKey("content_type", "object_id")


item = await line.item                    # a Membership or an Activity
item is await line.item                   # True, from the cache
line.object_id = 7                        # the next await loads again
lines = InvoiceLine.objects.prefetch_related("item")    # one query per content type
```

#### Assignment {#generic-assignment}

Assigning an instance, as an attribute or as a constructor keyword, points the relation at it:

- `fk_field` gets the instance's primary key, validated as the field's type, or `str(pk)` when the key itself does
  not validate (an integer key in a text field).
- The content type is pending until the owner's next write:
  [`save()`](#save-update-fields-none-force-insert-false-force-update-false),
  [`full_clean()`](#full-clean-exclude-none-validate-unique-true-validate-constraints-true),
  [`create()`](./queryset#async-create-values) or [`bulk_create()`](./queryset#async-bulk-create-objs-batch-size-none-ignore-conflicts-false-update-conflicts-false-update-fields-none-unique-fields-none).
  The write sets the content type field to the content type id of the instance's model (its concrete model's with
  `for_concrete_model`), taken with [`content_types.get_id()`](#adorm-contenttypes) on the executor that writes (the
  instance's for `save()`, the query set's for `create()`; from the cache when it is there). A row written through
  another session therefore gets that database's id.
- Until that write the content type field holds the id [`content_types`](#adorm-contenttypes) has cached for the
  owner's executor. When none is cached, the field is unset: reading it raises `AttributeError`, and `model_dump()`
  and `repr()` leave it out, as they do a deferred field. `await instance.rel` gives the assigned instance without
  an operation.
- No content type row for the assigned model makes the write raise the content type model's `DoesNotExist`
  ([`ObjectDoesNotExist`](./exceptions) when that model is not registered); nothing is written. A write that fails
  or is cancelled while it resolves leaves the assignment pending and writes nothing.
- An assignment that changes while a write resolves it (another instance or `None` assigned, a field set, a
  concurrent write resolving it first) is resolved anew or left as it is; the write stores the pair that is current.
- Assigning either field afterwards drops the pending content type.
- As an attribute: an instance without a primary key value or whose key fits `fk_field` neither way raises
  `ValueError`, a value that is not an instance of a model with `_meta` `TypeError`; `None` sets both fields to
  `None`.
- As a constructor or `create()` keyword, every refusal raises pydantic's `ValidationError`: a value that is not a
  stored model instance, the relation given together with either field, and `None` for a content type or object id
  field that is not nullable. JSON input (`model_validate_json()`) gives the two fields instead.
- [`get_or_create()`](./queryset#async-get-or-create-defaults-none-lookups) and
  [`update_or_create()`](./queryset#async-update-or-create-defaults-none-create-defaults-none-lookups) take the
  relation in `defaults` and `create_defaults` and write both fields; the content type id is taken on the query
  set's executor before the operation. The relation in their lookups raises `FieldError`, as a query path.

```python
line = await InvoiceLine.objects.create(invoice_id=1, item=membership, description="Fee", unit_price=120)
line.content_type_id                      # the id of clubs.Membership, read once and cached

line.item = activity                      # content_type_id readable at once when Activity's id is cached
await line.save()                         # a Get of the content type when it is not cached, then the Save
await line.save(update_fields=["item"])   # writes content_type_id and object_id
```

Methods of the descriptor:

- `object_id(model, value)`: the value `fk_field` of `model` holds for the instance `value`; raises `TypeError` and
  `ValueError` as attribute assignment does.
- `cached_content_type(model, executor, value)`: the content type id of `value`'s model as
  [`content_types`](#adorm-contenttypes) has it cached for `executor`, `None` when it is not cached.
- `assigned(instance)`: the instance whose content type the next write resolves, `None` when no assignment is pending.
- `await resolve(instance, executor)`: sets the content type field of a pending assignment with
  [`content_types.get_id()`](#adorm-contenttypes) on `executor`; nothing when no assignment is pending.

### `adorm.models.CONTENT_TYPE_MODEL`

`"contenttypes.ContentType"`: the content type model a `GenericForeignKey` reads when its content type field is not a
`ForeignKey` of the model, and the default `content_type_model` of [`content_types`](#adorm-contenttypes).

## Content types

A content type row (Django's `ContentType`) names a model by `app_label` and `model`, the lowercased class name; a
generic foreign key stores its primary key. The rows are created by the target (Django's `migrate`); adorm only reads
them.

### `adorm.ContentTypes`

Content type ids by model and models by content type id, cached per database: Django's
`ContentType.objects.get_for_model()` and `get_for_id()`. Every method but `clear()` takes:

- `session`: the [`Session`](./sessions) (or another [executor](#adorm-models-executor)) whose database it reads, by
  default the current session. Inside a transaction of that session it reads on the transaction's slot, also from a
  task that inherits the context, so a lookup never waits for a second slot.
- `content_type_model`: the label of the content type model, by default
  [`CONTENT_TYPE_MODEL`](#adorm-models-content-type-model).
- `for_concrete_model` (where a model is given): a proxy model uses its concrete model's content type
  ([`natural_key()`](#adorm-models-natural-key-model-for-concrete-model-true)).

The cache belongs to the database a session's profile connects to, as its driver names it
([`Driver.database_key()`](./drivers#database-key-profile)): profiles whose drivers name the same database share it, such as per-user
profiles that differ only in hooks or options. The Django and asyncpg drivers name the host, port and database name of
the profile's `DATABASES` entry with its `params` applied, and none for a profile with a `connect` hook. For a profile
whose driver names no database the cache belongs to the profile: sessions of that profile share it, and a profile that
is replaced or unregistered starts with an empty one. A session whose profile is not registered uses the profile's
name, any other executor the executor object. Each lookup that misses runs its own operation; concurrent lookups do not
wait for one another, and a cancelled one leaves the others running. The Django and asyncpg drivers connect Django's
`post_migrate` signal to `clear()` (`migrate` and `flush`, which a `TransactionTestCase` runs after each test,
create the rows again with other ids). Thread-safe.

| Method | Result |
|---|---|
| `await get_id(model, *, for_concrete_model=True, session=None, content_type_model=...)` | the content type id of a model class or instance: cached, or read with a `Get` operation (`app_label`, `model`) and cached |
| `await get_for_model(model, *, for_concrete_model=True, session=None, content_type_model=...)` | the row as a new instance of the registered content type model, with the primary key, `app_label` and `model` loaded |
| `await get_model(content_type, *, session=None, content_type_model=...)` | the registered model of a content type id |
| `await natural_keys(content_types, *, session=None, content_type_model=...)` | `{id: (app_label, model)}` for the ids that have a row; one missing id is read with a `Get`, several with `Select` operations of up to 500 ids; `None` ids are skipped |
| `cached_id(model, *, for_concrete_model=True, session=None, content_type_model=...)` | the cached id, `None` when it is not cached; runs nothing |
| `clear()` | forgets every cached content type; a lookup running meanwhile does not store what it read |

No content type row raises the content type model's `DoesNotExist` when that model is registered,
[`ObjectDoesNotExist`](./exceptions) otherwise, and nothing is cached. `get_model()` of a content type whose model
is not registered raises `LookupError`, and so does `get_for_model()` when the content type model is not registered.
A model class or instance that is not one raises `TypeError`.

```python
from adorm import content_types

type_id = await content_types.get_id(Membership)            # one Get, then cached
await content_types.get_id(membership)                      # an instance works too, no operation
content_types.cached_id(Membership)                         # the id, or None before the first lookup
await content_types.get_model(type_id)                      # Membership
row = await content_types.get_for_model(Membership)         # ContentType(id=..., app_label="clubs", model="membership")
await content_types.get_id(Membership, session=reporting)   # the id in the database of another profile

links = TaggedItem.objects.filter(content_type_id=await content_types.get_id(Location), object_id__in=ids)
```

### `adorm.content_types`

The process's `ContentTypes` instance, used by generic foreign keys and prefetches.

### `adorm.models.natural_key(model, *, for_concrete_model=True)`

`(app_label, model)` of the content type of the model class or instance `model`: the parts of its label, the model
name lowercased. With `for_concrete_model` a proxy gives its concrete model's key
([`ModelMeta.concrete_model`](#adorm-modelmeta-label-pk-id-schema-hash-none-orm-names-none-concrete-model-none)).
Raises `TypeError` for anything but a model class or instance.

```python
natural_key(Membership)                               # ("clubs", "membership")
natural_key(PinnedNote)                               # ("club", "note")
natural_key(PinnedNote, for_concrete_model=False)     # ("club", "pinnednote")
```

### `adorm.models.get_generic_foreign_keys(model)`

The `GenericForeignKey` descriptors of the model class `model`, inherited ones included, by attribute name.

```python
get_generic_foreign_keys(InvoiceLine)    # {"item": InvoiceLine.item}
```

### `adorm.models.parent_link(model)`

The `ForeignKey` stored in the primary key field of the model class `model`, the link of a multi-table inheritance
child to its parent; `None` when the primary key is no relation's column. It names the parent model for
[`create_from_parent()`](./queryset#async-create-from-parent-parent-values).

```python
parent_link(Competition).target    # "activities.Activity"
parent_link(Activity)              # None
```

### `adorm.models.inheritance.own_fields(model, parent)`

The fields of the model class `model` that the model class `parent` does not have, the primary key excepted: the
fields a multi-table inheritance child declares itself.

```python
own_fields(Competition, Activity)     # ["discipline", "entry_deadline"]
```

### `adorm.models.inheritance.child_values(model, values)`

`(parent_link(model), parent model class, validated values)` for `create_from_parent()`: `values` validated with the
field types of the child's own fields; a `ForeignKey` of the child given by name stands for its column (an instance
for its primary key). Raises `TypeError` when `model` is not a multi-table inheritance child, `ValueError` for a name
that is not one of the child's own fields or relations and for a value its type rejects (the message names the field).

```python
child_values(Competition, {"discipline": "Sprint", "entry_deadline": "2026-05-01"})
# (Competition.activity_ptr, Activity, {"discipline": "Sprint", "entry_deadline": datetime.date(2026, 5, 1)})
```

### `adorm.models.prefetch.normalize_lookups(current, lookups, prefix="")`

`current` (a tuple of `Prefetch` objects) followed by `lookups` (relation paths or `Prefetch` objects) as `Prefetch`
objects, with `prefix` put before each new path; what `prefetch_related()` keeps. A path that is already there is kept
once; a new one with a query set or `to_attr` whose `prefetch_to` is already there raises `ValueError`, a lookup that
is neither a string nor a `Prefetch` `TypeError`.

### `adorm.models.RelatedObject`

The awaitable a `ForeignKey`, `ReverseOneToOne` or `GenericForeignKey` returns on an instance. Nothing runs until it
is awaited, so reading the relation without awaiting it leaves no coroutine behind; each `await` checks the cache
again.

### `adorm.models.get_relations(model)`

The relation descriptors of the model class `model`, inherited ones included, by attribute name. A subclass removes
an inherited relation by assigning the name to something else.

```python
get_relations(Member)    # {"club": Member.club, "sponsor": Member.sponsor}
```

## Managers

The [managers topic](/topics/managers) explains which methods run in the client and which on the driver.

### `adorm.Manager(orm_name=None, *, method_manager=None)`

A manager declared in a model class body. `orm_name` names the target ORM's manager the driver applies to every
operation of this manager ([`Query.manager`](./query#adorm-query-query-model-manager-none)): its rows (Django:
`get_queryset()`) and the overrides of its query set (`create()`, `delete()` …). `None` reads and writes the
model's rows without manager logic, on every driver. `method_manager` names the target ORM's manager the [driver
methods](#adorm-driver-method-func-queryset-only-false) run on and defaults to `orm_name`; a manager with a
`method_manager` and no `orm_name` sends its queries without `Query.manager`, so they run on every driver, and only
its driver methods need a driver that runs `call` operations. A name that is not a public identifier raises
`ValueError`.

Reading the manager on a model class returns a copy of it bound to that class and the current session (a
[`BoundModel`](./sessions#adorm-boundmodel)); reading it on an instance or on an abstract model raises
`AttributeError`. Subclasses of the model inherit its managers, bound to themselves. A manager instance is declared
under one name (`TypeError` otherwise), and a manager named like a field or relation raises `TypeError`.

A subclass adds methods: client-side ones that build on `get_queryset()`, [driver
methods](#adorm-driver-method-func-queryset-only-false), an overridden `get_queryset()`, and `queryset_class`, the
[`QuerySet`](./queryset) subclass of its query sets.

```python
class OpenLoans(Manager):
    queryset_class = LoanQuerySet

    def get_queryset(self):
        return super().get_queryset().filter(archived=False)


class Loan(Model):
    _meta = ModelMeta("library.Loan")
    id: int | None = None
    archived: bool = False

    objects = OpenLoans("objects")
    all_loans = Manager()


class User(Model):
    ...
    objects = UserManager(method_manager="objects")   # create_user() on the driver, queries on every driver
```

#### `Manager.from_queryset(queryset_class, class_name=None)`

Class method: a subclass of the manager class with `queryset_class` set; its name is `class_name`, or
`<Manager class name>From<QuerySet class name>`. A class that is not a `QuerySet` subclass raises `TypeError`.

```python
LoanManager = Manager.from_queryset(LoanQuerySet)
objects = LoanManager("objects")
```

### `adorm.driver_method(func, *, queryset_only=False)`

Declares an `async def` method of a `Manager` or `QuerySet` subclass as a method the driver runs, returning a
[`DriverMethod`](#adorm-models-drivermethod). Used bare (`@driver_method`) or with arguments
(`@driver_method(queryset_only=True)`). The declaration's signature is the method's: a call checks its arguments
against it (`TypeError` before anything is sent), and the annotations type the arguments' encoding. The body does not
run. `queryset_only` keeps a query set method off the manager, as Django's `queryset_only` does.

A function that is not a coroutine function or has no `self` parameter raises `TypeError`; a name starting with `_`
raises `ValueError`; so does assigning it to a class attribute of another name.

```python
class LoanQuerySet(QuerySet):
    @driver_method
    async def archive(self, reason: str = "") -> int:
        """Archive the loans; returns their number."""


class LoanManager(Manager):
    queryset_class = LoanQuerySet

    @driver_method
    async def lend(self, reader_id: int, book_id: str, *, days: int = 14) -> "Loan": ...


await Loan.objects.lend(7, "9780000000001")
await Loan.objects.filter(reader_id=7).archive("moved")
```

### `adorm.models.DriverMethod`

What `driver_method` returns: a descriptor that, read on an instance, gives a bound method returning a coroutine.

| Member | Meaning |
|---|---|
| `func` | the declared function |
| `name` | the method name |
| `signature` | the declared `inspect.Signature` |
| `queryset_only` | whether the manager leaves it to its query sets |
| `schema` | the [`MethodSchema`](./encoding#adorm-schema-methodschema-positional-keyword-var-positional-none-var-keyword-none) of the parameters after `self`; a string annotation is evaluated in the function's module, and one that does not evaluate types nothing |

Calling it on a bound manager sends `Call(Query(label, manager=orm_name), name, "manager", args, kwargs,
method_manager)`; on a query set, `Call(qs.query, name, "queryset", args, kwargs, manager)`, where `manager` is the
query's [`Query.manager`](./query#adorm-query-query-model-manager-none) or else the `method_manager` of the model's
first manager whose `queryset_class` declares the method
([`Call`](./operations#adorm-operations-call-query-method-target-queryset-args-kwargs-manager-none)). No such
manager raises `TypeError`, and so does an instance of any other class. The result is the driver's, with every
[`ModelRow`](./operations#adorm-operations-modelrow-model-row), also inside lists, tuples and the values of mappings,
turned into an instance of the registered model on the same session; a row of a model that is not registered raises `InterfaceError`. A
`DoesNotExist` or `MultipleObjectsReturned` the driver raises becomes the model's.

```python
LoanManager.lend.schema.kwarg_type("days")       # int
```

### `adorm.models.get_managers(model)`

The manager declarations of `model` (inherited ones included) by attribute name, unbound.

```python
list(get_managers(Loan))         # ["objects", "all_loans"]
```

### `adorm.models.driver_methods(model)`

The [`DriverMethod`](#adorm-models-drivermethod) of every manager of `model` that has a `method_manager`, by
`(method_manager, target, method)`: the manager's own methods with the target `"manager"`, the methods of its
`queryset_class` with `"queryset"`.

```python
dict(driver_methods(Loan))
# {("objects", "manager", "lend"): LoanManager.lend, ("objects", "queryset", "archive"): LoanQuerySet.archive}
```

### `adorm.models.find_driver_method(label, manager, target, method)`

The driver method `method` declared for `target` of the ORM manager `manager` on the registered model `label`.
[`DriverConnection.execute()`](./drivers#adorm-drivers-driverconnection) calls it for every `Call`, so a driver runs
only declared methods. A model that is not registered, a `manager` of `None` and a method the model does not declare
raise `NotSupportedError`.

```python
find_driver_method("library.Loan", "objects", "queryset", "archive")      # LoanQuerySet.archive
find_driver_method("library.Loan", "objects", "queryset", "delete")       # NotSupportedError
```

## Executors

### `adorm.models.Executor`

The protocol of what runs operations for model instances: `await executor.execute(operation)` returns the
operation's result (a row for `Get`, the stored values for `Save`). An instance uses its own executor
(`_state.executor`); without one it asks the executor resolver.

```python
class Recorder:
    def __init__(self):
        self.operations = []

    async def execute(self, operation):
        self.operations.append(operation)
        return {"id": 1}
```

### `adorm.models.set_executor_resolver(resolver)`

Set the function that gives an instance without its own executor one: `resolver(model_class)` returns an
`Executor` or `None`. Returns the previous resolver; `None` removes it. Importing `adorm` installs a resolver that
returns the [current session](./sessions#adorm-current-session). Without an executor, `save()` and relation loads
raise `InterfaceError`.

```python
previous = set_executor_resolver(lambda model: executor)
await Member(name="Ana").save()       # runs on executor
set_executor_resolver(previous)
```

## Registry

### `adorm.models.ModelRegistry`

Model classes by label, and a [`SchemaSource`](./encoding#adorm-schema-schemasource) serving each registered model's
schema.

| Member | Meaning |
|---|---|
| `register(model)` | register under `model._meta.label`; a class with the same module and qualified name replaces the registered one, another class raises `ValueError` |
| `unregister(label)` | remove the model; an unknown label is ignored |
| `get(label)` | the model class; `LookupError` when none is registered |
| `get_for_content_type(app_label, model_name)` | the model class whose label has the app part `app_label` and the model part `model_name`, compared case-insensitively (Django's `ContentType.model`); `LookupError` when none is registered |
| `schema_hashes()` | the schema hash of every registered model that has one (`_meta.schema_hash`), as a dict by label: what a client sends a driver of another process to check its models against ([`Driver.check()`](/ref/drivers#check-operation-schema-hashes-none)) |
| `model_schema(label)` | the model's [`ModelSchema`](./encoding#adorm-schema-modelschema-label-pk-fields-relations-orm-names-methods), `None` for an unknown label; cached once the model class is complete (its forward references resolved) |
| `label in registry`, `iter(registry)`, `len(registry)` | membership, the model classes, their number |

### `adorm.models.registry`

The `ModelRegistry` every model with `_meta` registers in. It types operation values for
[encoding](./encoding):

```python
operation = Save("club.Member", 3, {"fee": decimal.Decimal("1.50")})
data = operation.to_dict(registry)          # {"fee": {"value": "1.50"}, ...}
assert Operation.from_dict(data, registry) == operation
registry.schema_hashes()                    # {"clubs.Club": "e8c2eed312ab7b68", ...}
```

### `adorm.models.model_schema(model)`

The [`ModelSchema`](./encoding#adorm-schema-modelschema-label-pk-fields-relations-orm-names-methods) of the model class `model`: its label, primary key, the
type of every field (with its pydantic constraints), the target label of every relation, `_meta.orm_names` and the
`MethodSchema` of every [driver method](#adorm-models-driver-methods-model). An abstract model raises `TypeError`.

```python
schema = model_schema(Member)
schema.fields["club_id"]     # int | None
schema.relations             # {"club": "club.Club"}
```
