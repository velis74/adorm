# Generating models

`adorm generate` writes adorm models from Django models: one module per Django app, with the same labels, field
names, types and relations, and a package `__init__.py` that imports them. The modules are checked into the
application's repository; `adorm generate --check` fails a CI job when they differ from the Django models.

## Generate the modules

Django must be installed (`pip install "adorm[django]"`). The command sets Django up with the settings module from
`--settings` (or `DJANGO_SETTINGS_MODULE`) and writes the package directory given by `--output`:

```sh
adorm generate --settings myproject.settings --output myproject/adorm_models clubs billing
```

```text
wrote myproject/adorm_models/__init__.py
wrote myproject/adorm_models/clubs.py
wrote myproject/adorm_models/billing.py
3 written, 0 removed, 0 unchanged.
```

Without app labels every installed app with models is generated, in `INSTALLED_APPS` order. `python -m adorm generate`
is the same command. The import path starts with the current directory, then each `--pythonpath DIR`, ahead of
installed packages and editable installs, so also under `uv run` the project's packages are imported from the current
directory:

```sh
cd myproject-repo
uv run adorm generate --settings myproject.settings --output myproject/adorm_models
uv run adorm generate --settings settings --pythonpath src --output src/adorm_models  # settings in src/
```

Importing the package imports every module, which registers every model. `MODELS` lists the classes:

```python
from myproject.adorm_models import MODELS, clubs

member = await clubs.Member.objects.get(email="ana@example.com")
schemas = {model._meta.label: model.model_json_schema() for model in MODELS}
```

| Option | Effect |
|---|---|
| `label …` | an app label (`clubs`) for every model of the app, or a model label (`clubs.Member`) for that model; default: every app with models |
| `-o`, `--output DIR` | the package directory; created when missing |
| `--settings MODULE` | the Django settings module; default: `DJANGO_SETTINGS_MODULE` |
| `--pythonpath DIR` | a directory put on the import path after the current directory (repeatable) |
| `-e`, `--exclude LABEL` | an app label (`billing`) or model label (`billing.Payment`) to leave out (repeatable) |
| `--check` | write nothing; exit with status 1 when a file is missing, stale, obsolete or not generated |
| `--ignore-third-party` | do not warn about manager and query set methods of installed packages |
| `--line-length N` | the longest line before a statement is split (default 88) |

## Generate selected models

A model label (`app_label.Model`) selects one model; an app label selects every model of its app. Both may appear in
one command. The module of each app holds only the selected models, in the Django order:

```sh
adorm generate --settings myproject.settings --output myproject/adorm_models clubs.Club clubs.Member billing
```

```text
warning: clubs.Club.country: geo.Country not generated, so the relation fails when used
wrote myproject/adorm_models/__init__.py
wrote myproject/adorm_models/clubs.py
wrote myproject/adorm_models/billing.py
3 written, 0 removed, 0 unchanged.
```

- An app also named by its app label is generated whole, whatever model labels name it; `--exclude` leaves models out
  of it.
- The model name matches case-insensitively (`clubs.member`).
- These stop the command with an error: an unknown app or model, a label that is neither an app label nor a model
  label (`clubs.Member.email`, `clubs.`), an automatic many-to-many through model, a swapped model (`auth.User` with
  a custom `AUTH_USER_MODEL`).

Relations to models outside the selection:

| Relation | Generated |
|---|---|
| a foreign key to a model that is not selected | the column field (`country_id`) and the `ForeignKey`, reported as a warning; reading the relation raises `LookupError` (no model is registered under the label) unless that model is registered some other way |
| a many-to-many relation to a model, or through a declared through model, that is not selected | the `ManyToMany`, reported the same way |
| the reverse side of a relation declared on a model that is not selected | nothing |
| a multi-table inheritance parent or a proxy's model that is not selected | the class derives from `Model` and declares every field |

The column field gives the key without the related model:

```python
from myproject.adorm_models import clubs

club = await clubs.Club.objects.get(slug="chess")
country_id = club.country_id  # the key; await club.country raises LookupError
members = [member async for member in clubs.Member.objects.filter(club_id=club.id)]
```

The selection determines the output; run `--check` with the same labels.

## Check the modules in CI

The check compares every file with what the command would write. Run it with the arguments of the command that wrote
the files:

```yaml
- run: uv run adorm generate --check --settings myproject.settings --output myproject/adorm_models clubs billing
```

```text
myproject/adorm_models/clubs.py: stale
generated models are not up to date: run adorm generate without --check
```

| State | Meaning |
|---|---|
| `missing` | the file does not exist |
| `stale` | a generated file with other content |
| `obsolete` | a generated file of an app no longer generated |
| `foreign` | a file the command would write that it did not generate |

Exit status 0 means up to date, 1 means at least one difference, 2 an error (an unknown app, Django that cannot be set
up, a model the generator cannot describe, a usage error).

A file is the generator's when its first line is the header `# Generated by adorm generate: …`. The command replaces
and removes only such files. Other files in the directory are left alone; a `foreign` file stops the command before
it writes anything.

## What a module contains

For the Django models

```python
class Club(models.Model):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True)
    address = models.JSONField(default=dict, blank=True)


class Member(models.Model):
    """A club member."""

    email = models.EmailField(unique=True)
    level = models.CharField(max_length=20, choices=Level.choices, default=Level.BEGINNER)
    fee = models.DecimalField(max_digits=8, decimal_places=2, validators=[MinValueValidator(0)])
    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="members")
```

the module `clubs.py` is

```python
# Generated by adorm generate: do not edit, run adorm generate again.
"""adorm models of the Django app ``clubs``."""

import decimal
from typing import Any, Literal

from adorm import ForeignKey, Model, ModelMeta, ReverseForeignKey
from pydantic import Field

__all__ = ["Club", "Member"]


class Club(Model):
    _meta = ModelMeta("clubs.Club", schema_hash="828c45bc363e1f53")
    id: int | None = None
    name: str = Field(max_length=100)
    slug: str = Field(max_length=100, pattern="^[-a-zA-Z0-9_]+$")
    address: Any = Field(default_factory=dict)
    members = ReverseForeignKey("clubs.Member", column="club_id")


class Member(Model):
    """A club member."""

    _meta = ModelMeta("clubs.Member", schema_hash="4b133aa8953d898b")
    id: int | None = None
    email: str = Field(max_length=254)
    level: Literal["beginner", "intermediate", "advanced"] = "beginner"
    fee: decimal.Decimal = Field(ge=0, max_digits=8, decimal_places=2)
    club_id: int
    club = ForeignKey("clubs.Club")
```

### Fields

Every concrete field becomes a pydantic field named after its attribute (`club_id` for a foreign key `club`), in the
Django order. `help_text` becomes the field's `description`; a model docstring that is not Django's generated one
becomes the class docstring.

| Django field | Type | Constraints |
|---|---|---|
| `AutoField`, `BigAutoField`, `SmallAutoField`, `IntegerField`, `BigIntegerField`, `SmallIntegerField` | `int` | |
| `PositiveIntegerField`, `PositiveBigIntegerField`, `PositiveSmallIntegerField` | `int` | `ge=0` |
| `CharField`, `TextField`, `EmailField`, `URLField`, `FileField`, `ImageField`, `FilePathField`, `GenericIPAddressField` | `str` | `max_length` |
| `SlugField` | `str` | `max_length`, `pattern` |
| `BooleanField` | `bool` | |
| `FloatField` | `float` | |
| `DecimalField` | `decimal.Decimal` | `max_digits`, `decimal_places` |
| `DateField`, `DateTimeField`, `TimeField`, `DurationField` | `datetime.date`, `datetime.datetime`, `datetime.time`, `datetime.timedelta` | |
| `UUIDField` | `uuid.UUID` | |
| `BinaryField` | `bytes` | `max_length` |
| `JSONField` | `Any` | |
| `ArrayField(base)` | `list[<type of base>]` | `max_length` from `size` |
| `HStoreField` | `dict[str, str \| None]` | |
| a field with `choices` | `Literal[<values>]` | none |
| a foreign key column | the type of the target field | none |
| `_order` of `Meta.order_with_respect_to` | `order_: int \| None = Field(default=None, alias="_order")` | none |
| any other field | `Any` (reported as a warning) | |

The validators `MinValueValidator` and `MaxValueValidator` become `ge` and `le`, `MinLengthValidator` and
`MaxLengthValidator` `min_length` and `max_length`, and a `RegexValidator` without flags or `inverse_match` becomes
`pattern` (`\Z` written as `$`; a pattern pydantic cannot compile is reported and left out). A validator with a
callable limit is left out.

A `null=True` field is `<type> | None`. The default follows Django's:

| Django | Generated |
|---|---|
| `default=<literal>` (number, string, `Decimal`, date, `UUID`, list, dict …) | `= <literal>` |
| `default=dict`, `list`, `set`, … or a callable from `uuid`, `datetime` or `decimal` (`uuid.uuid4`) | `Field(default_factory=…)` |
| no default, `null=True` | `= None` |
| no default, `blank=True` on a string field | `= ""` |
| no default | required |
| automatic primary key, parent link, `auto_now`, `auto_now_add`, `db_default`, `GeneratedField`, `_order`, any other callable default (`timezone.now`) | `<type> \| None = None` |

The last row covers values the database or Django fills in when the row is stored. The field is optional and `None`
until then; [`save()`](/ref/models#save-update-fields-none-force-insert-false-force-update-false) leaves a field it was
not given to the target.

### Relations

| Django | Generated |
|---|---|
| `ForeignKey`, `OneToOneField` | [`ForeignKey`](/ref/models#adorm-foreignkey-target-column-none) on the column field |
| the reverse side of a `ForeignKey` | [`ReverseForeignKey`](/ref/models#adorm-reverseforeignkey-target-column) named after the accessor (`related_name`, `<model>_set`) |
| the reverse side of a `OneToOneField` | [`ReverseOneToOne`](/ref/models#adorm-reverseonetoone-target-column) |
| `ManyToManyField`, either side | [`ManyToMany`](/ref/models#adorm-manytomany-target-through-source-column-target-column) through the declared or the automatic through model |
| `GenericForeignKey` | [`GenericForeignKey`](/ref/models#adorm-genericforeignkey-ct-field-content-type-fk-field-object-id-for-concrete-model-true) with the content type field and the object id field (no arguments for `content_type` and `object_id`), and `for_concrete_model=False` when Django's is false |

Reverse relations come only from models generated in the same run. A forward relation to a model that is not
generated (or through a declared through model that is not) keeps its column and descriptor and is reported as a
warning; the descriptor raises `LookupError` when it is used unless that model is registered some other way
([selected models](#generate-selected-models)). A relation hidden with `related_name="+"` is left out.

These are reported as warnings and left out; their columns stay ordinary fields:

- generic relations (`GenericRelation`, the reverse side of a generic foreign key);
- a foreign key with `to_field` naming a field other than the target's primary key, both its sides, and a
  many-to-many relation whose through model has such a foreign key (`shelf_id` stays, holding the `code` value).

```text
warning: library.Placement.shelf: the relation references library.Shelf.code, not a primary key, and is not generated
warning: auth.Permission.content_type: contenttypes.ContentType not generated, so the relation fails when used
```

### Inheritance and proxies

- A multi-table inheritance child derives from its parent's generated class and declares only its own fields; the
  parent link is its primary key.
- A proxy model derives from the class of the model it stands for, adds no fields and names its concrete model in
  `ModelMeta(..., concrete_model=...)`, which gives it the concrete model's content type.
- When the parent is not generated, the class derives from `Model` and declares every field.
- A parent in another app is imported from that app's module.

```python
class Competition(Activity):
    """An activity with results: a multi-table inheritance child of `Activity`."""

    _meta = ModelMeta(
        "activities.Competition",
        pk="activity_ptr_id",
        schema_hash="ee19dd5905415214",
    )
    activity_ptr_id: int | None = None
    discipline: str = Field(max_length=60)
    entry_deadline: datetime.date | None = None
    activity_ptr = ForeignKey("activities.Activity")
```

The parent gets the reverse side of the parent link, `competition = ReverseOneToOne("activities.Competition",
column="activity_ptr_id")`.

Not generated: abstract models (their fields are part of each concrete child) and automatic many-to-many through
models (a `ManyToMany` names them by label, e.g. `"activities.Activity_tags"`). A model with a composite primary key
stops the command with an error; leave it out with `--exclude`.

## Managers

The generator writes the managers of every model; see the [managers topic](/topics/managers) for what runs where.

- A manager whose class or query set class overrides a method the driver calls on the Django manager
  ([`ROUTED_METHODS`](/ref/codegen#adorm-codegen-django-routed-methods): `get_queryset()`, `create()`, `delete()`,
  `update()` …) gets its Django name as ORM name: `objects = Manager("objects")`. Every operation of that adorm
  manager runs through the Django manager, with the overrides. An override of another method
  (`contribute_to_class()`, `deconstruct()` …) does not count. Nor does one the `@client_base` class also defines:
  the ported method replaces the Django one, so `get_queryset()` in the client base keeps the manager's queries in
  the client, on every driver.
- A manager with exposed methods and no such override runs only its driver methods on the Django manager:
  `objects = UserManager(method_manager="objects")`. Its queries carry no ORM manager and run on every driver.
- Any other manager reads the plain rows: `all_loans = Manager()`. A plain `objects` is not written; `adorm.Model`
  declares it.
- When a model has no manager named `objects`, `objects` also stands for its default manager, unless that one is
  plain.
- A method marked with `@expose`, or listed in the `ADORM_EXPOSE` setting as `"app_label.Model.manager.method"`,
  becomes a driver method of a generated manager or query set class, with the method's signature and docstring.
  Methods defined on the manager go to the manager class, methods of the query set to the query set class.
- `@client_base("package.module:ClassName")` on a Django manager or query set class makes its generated class derive
  from that hand-written adorm class, where methods ported by hand live.
- A public method that is neither exposed, nor a method of Django's `Manager` or `QuerySet`, nor defined by the
  client base is not generated and is reported as a warning, unless it comes from Django's own modules or is
  `get_by_natural_key`, which only Django calls. Methods of installed packages are listed in one warning per manager
  ([below](#methods-of-installed-packages)).
- A multi-table child or proxy declares only its own managers; it inherits the parent's from the parent's class.

```python
from adorm import client_base, expose


@client_base("library.adorm_bases:LoanQuerySetBase")
class LoanQuerySet(models.QuerySet):
    @expose
    def archive(self, reason: str = "") -> int:
        """Archive the loans with ``reason`` as their note; returns the number of loans."""
        return self.update(archived=True, note=reason)


class LoanManager(models.Manager.from_queryset(LoanQuerySet)):
    def get_queryset(self):
        return super().get_queryset().filter(archived=False)

    @expose
    def lend(self, reader_id: int, book_id: str, *, days: int = 14) -> "Loan":
        """Lend a book for ``days`` days."""
        ...


class Loan(models.Model):
    ...
    objects = LoanManager()
    all_loans = models.Manager()
```

```python
# settings.py
ADORM_EXPOSE = ["auth.User.objects.create_user"]
```

`adorm generate` writes:

```python
class LoanQuerySet(LoanQuerySetBase):
    @driver_method
    async def archive(self, reason: str = "") -> int:
        """Archive the loans with ``reason`` as their note; returns the number of loans."""


class LoanManager(Manager):
    queryset_class = LoanQuerySet

    @driver_method
    async def lend(self, reader_id: int, book_id: str, *, days: int = 14) -> "Loan":
        """Lend a book for ``days`` days."""


class Loan(Model):
    ...
    objects = LoanManager("objects")
    all_loans = Manager()
```

and, in the `auth` module, `class UserManager(Manager)` with `create_user(self, username, email=None, password=None,
**extra_fields)` and `objects = UserManager(method_manager="objects")` on `User`.

Parameter and return annotations the generator writes: builtins, `datetime`, `decimal` and `uuid` types,
`X | None`, `Literal`, containers of these, and, as a return type, a model of the same module (quoted) or of another
module (`Any`). Anything else becomes `Any` with a warning. A parameter typed as a model also becomes `Any`: arguments
are plain values (pass the primary key).

Not generated, with a warning: a method that is a coroutine function, returns a query set (a query-building method
belongs to the client base) or has a default the generator cannot write as a literal; a manager whose name would hide
an attribute of `adorm.Model`.

### Methods of installed packages

Methods of a manager or query set class of an installed package (a file under `site-packages` or `dist-packages`,
such as django-model-utils or django-allauth) are reported as one warning per manager, grouped by the class that
defines them. Each method of the project's own classes gets a warning of its own:

```python
from model_utils.managers import InheritanceManager


class FilteringInheritanceManager(InheritanceManager):
    def filter_subclasses(self, *subclasses): ...


class Asset(models.Model):
    objects = FilteringInheritanceManager()
```

```text
warning: assets.Asset.objects.filter_subclasses: not generated; expose it or port it to a client base
warning: assets.Asset.objects: 6 methods of installed packages not generated: model_utils.managers.InheritanceManagerMixin (get_subclass, instance_of, select_subclasses), model_utils.managers.InheritanceQuerySet (instance_of), model_utils.managers.InheritanceQuerySetMixin (get_subclass, select_subclasses); expose or port the ones the application calls
```

A method listed in `ADORM_EXPOSE` or ported to the client base is not listed. `--ignore-third-party` leaves out this
warning and keeps every other:

```sh
adorm generate --ignore-third-party --settings myproject.settings --output myproject/adorm_models
```

Code whose file path has no `site-packages` or `dist-packages` directory, such as the project's own modules, an
editable install or a package vendored into the project's source tree, is not installed; packages in a virtualenv
inside the project are.

## Name clashes

A field or relation whose name is an attribute of `adorm.Model` (pydantic's `model_dump`, `model_validate`,
`model_fields`, `json`, `copy`, `schema`, `dict`, adorm's `save`, `objects`, `pk` …) or a Python keyword gets `_`
appended. A name starting with `_` (pydantic's private attributes) loses its leading underscores and gets `_` appended
(`_order` is `order_`). A renamed field gets its Django name as alias. `_meta.orm_names` maps every renamed name to the
Django one; a multi-table inheritance child or a proxy also maps the names it inherits.

A field name that is also a name the module imports (`datetime`, `uuid`, `decimal`, `Field`, `ForeignKey` …) makes
the module import it under an alias (`import datetime as _datetime`); the field would hide it inside the class body.

For the Django model

```python
class Note(models.Model):
    """A note on any object, with fields named like pydantic methods."""

    code = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.CharField(max_length=40)
    item = GenericForeignKey("content_type", "object_id")
    json = models.TextField(blank=True)
    copy = models.ForeignKey(Book, on_delete=models.CASCADE, null=True, blank=True, related_name="copy_notes")
    schema = models.CharField(max_length=20, default="v1")
    model_dump = models.BooleanField(default=False)
    score = models.DecimalField(max_digits=4, decimal_places=1, default=Decimal("2.5"))
```

the generated class is

```python
class Note(Model):
    """A note on any object, with fields named like pydantic methods."""

    _meta = ModelMeta(
        "library.Note",
        pk="code",
        schema_hash="387e7a453a16eb5e",
        orm_names={"json_": "json", "schema_": "schema", "model_dump_": "model_dump", "copy_": "copy"},
    )
    code: uuid.UUID = Field(default_factory=uuid.uuid4)
    content_type_id: int
    object_id: str = Field(max_length=40)
    json_: str = Field(default="", alias="json")
    copy_id: str | None = None
    schema_: str = Field(default="v1", alias="schema", max_length=20)
    model_dump_: bool = Field(default=False, alias="model_dump")
    score: decimal.Decimal = Field(default=decimal.Decimal("2.5"), max_digits=4, decimal_places=1)
    content_type = ForeignKey("contenttypes.ContentType")
    copy_ = ForeignKey("library.Book", column="copy_id")
    item = GenericForeignKey()
```

Code and queries use the suffixed name; serialisation and the JSON schema use the original one. The model base sets
`serialize_by_alias` and accepts both names on input:

```python
notes = Note.objects.filter(json___icontains="draft", copy___title="Dune")
note = Note(content_type_id=1, object_id="7", json="{}")   # or json_="{}"
note.json_                     # "{}"
note.model_dump()["json"]      # "{}"
"json" in Note.model_json_schema()["properties"]           # True
```

## Schema hash

`_meta.schema_hash` is 16 hexadecimal digits of a SHA-256 over the model's label, primary key, parents, every
concrete field (name, Django name, type, nullability, constraints; inherited fields included), its forward
relations and its managers with an ORM name or driver methods (names and the methods' parameters). Defaults,
descriptions, return types and reverse relations do not take part. When a model is used, a driver computes the hash
from the Django model with [`adorm.codegen.django.schema_hash()`](/ref/codegen#adorm-codegen-django-schema-hash-model-warn-true)
and compares it with the generated model's. A model generated from another version of the Django model raises
[`SchemaMismatchError`](/topics/schema-checks#the-schema-hash-at-use) until the modules are generated again:

```python
from django.apps import apps

from adorm.codegen.django import schema_hash

schema_hash(apps.get_model("clubs.Member")) == Member._meta.schema_hash
```

## Formatters and linters

The modules are formatted as ruff and black format them at the given `--line-length`. Imports are sorted for an
application that installs adorm: the standard library, then `adorm`, `pydantic` and client bases together, then a
parent class imported from another generated module. Exclude the package from tools that format or sort differently:

```toml
[tool.ruff]
extend-exclude = ["myproject/adorm_models"]
```
