# Model generator

`adorm generate` and the Python API behind it. The [how-to guide](/howto/generating-models) shows the command in use
and what the generated modules contain. `adorm.codegen` imports no ORM; `adorm.codegen.django` imports Django. The
declarations the generator reads in the ORM's code are in [`adorm.porting`](#declarations-in-the-orm-s-code).

```python
from adorm.codegen import check, plan, write
from adorm.codegen.django import introspect, schema_hash, setup
from adorm.porting import client_base, expose
```

## Command line

### `adorm generate`

```text
adorm generate [-h] -o OUTPUT [--settings SETTINGS] [--pythonpath PYTHONPATH] [-e LABEL] [--check]
               [--line-length LINE_LENGTH] [app_label ...]
```

Writes one module per Django app and a package `__init__.py` into `OUTPUT`; with `--check`, writes nothing and
reports the differences. `python -m adorm generate` is the same command. Before Django is set up, the current
directory and then each `--pythonpath` directory, as absolute paths, are put at the front of `sys.path`; entries that
name the same directories further back are moved there. Warnings (constructs that are not generated, forward
relations to models that are not generated) go to standard error as `warning: <message>`.

| Exit status | Meaning |
|---|---|
| 0 | the files were written, or `--check` found them up to date |
| 1 | `--check` found a missing, stale, obsolete or foreign file |
| 2 | an error: a usage error, no settings module, Django missing or not set up, an unknown app, an unsupported model, a foreign file to overwrite, any other exception (printed as `adorm generate: <type>: <message>`) |

### `adorm.cli.main(argv=None)`

Runs the `adorm` command with `argv` (default: `sys.argv[1:]`) and returns its exit status; a usage error raises
`SystemExit(2)`. The `adorm` console script calls it.

```python
from adorm.cli import main

status = main(["generate", "--check", "--settings", "myproject.settings", "-o", "myproject/adorm_models"])
```

## Model description

### `adorm.codegen.AppSpec(label, models)`

The models of one app, rendered as the module `<label>.py`. A label that is not an identifier, or is a keyword,
raises `ValueError`.

### `adorm.codegen.ModelSpec(label, class_name, pk, fields, relations, schema_hash, base=None, doc=None, managers=())`

A generated class: its label, class name, primary key field, own `fields` and `relations`, `schema_hash`, the label of
the generated model it derives from (`base`, `None` for `Model`), its docstring and the `ManagerSpec` of every manager
it declares (a plain `objects` is inherited from `Model` and not listed). `orm_names` gives the ORM names of its
renamed fields and relations. Two fields, relations or managers with one name raise `ValueError`.

### `adorm.codegen.FieldSpec(name, orm_name, type, nullable=False, default=NO_DEFAULT, default_factory=None, db_filled=False, constraints={}, description=None, primary_key=False)`

A field. `name` is the Python name, `orm_name` the ORM's (`alias` is `orm_name` when the two differ, else `None`).
`default_factory` is a dotted path (`"builtins.dict"`, `"uuid.uuid4"`). `db_filled` makes the field `<type> | None =
None`. `constraints` are pydantic `Field()` constraints among `gt`, `ge`, `lt`, `le`, `min_length`, `max_length`,
`pattern`, `max_digits` and `decimal_places`, kept in that order. An unknown constraint, a name that is not an
identifier, and both a default and a factory raise `ValueError`.

```python
FieldSpec("json_", "json", TypeSpec("str"), default="", constraints={"max_length": 20})
# json_: str = Field(default="", alias="json", max_length=20)
```

### `adorm.codegen.RelationSpec(kind, name, orm_name, target, column=None, through=None, source_column=None, target_column=None, reverse=False, ct_field=None)`

A relation, rendered as `ForeignKey` (`kind="foreign_key"`), `ReverseForeignKey` (`"reverse_foreign_key"`),
`ReverseOneToOne` (`"reverse_one_to_one"`), `ManyToMany` (`"many_to_many"`, with `through`, `source_column` and
`target_column`; `reverse` for the side that did not declare it) or `GenericForeignKey` (`"generic_foreign_key"`:
the content type foreign key `ct_field` to `target`, the object id field `column`). `is_forward` is true for a foreign
key and a declared many-to-many relation; the schema hash covers only those. A missing column, and a generic foreign
key without `ct_field`, raise `ValueError`.

```python
RelationSpec("generic_foreign_key", "item", "item", "contenttypes.ContentType", column="object_id",
             ct_field="content_type")
# renders:  item = GenericForeignKey()
```

### `adorm.codegen.TypeSpec(name, args=(), values=(), nullable=False, model=False)`

A type: a builtin (`"int"`), `"None"`, a `typing` name (`"typing.Any"`) or a dotted name in a module
(`"datetime.date"`), with type arguments (`TypeSpec("list", (TypeSpec("str"),))`), `typing.Literal` values and
`| None`. With `model`, `name` is the label of a generated model and the type is its class, as a driver method's
return type; it takes no arguments (`ValueError`). `str()` gives the annotation text used in the schema hash.

```python
str(TypeSpec("list", (TypeSpec("int", nullable=True),)))     # "list[int | None]"
str(TypeSpec("typing.Literal", values=("a", "b")))           # "Literal['a', 'b']"
str(TypeSpec("library.Loan", model=True))                    # "model:library.Loan"
```

### `adorm.codegen.ManagerSpec(name, orm_name=None, class_name=None, methods=(), queryset=None, base=None, method_manager=None)`

A manager of a generated model, the class attribute `name`. `orm_name` is the ORM manager the driver applies to every
operation and `method_manager` the ORM manager the driver methods run on when it is not `orm_name`
([`adorm.Manager`](./models#adorm-manager-orm-name-none-method-manager-none)). `class_name`, when set, is a generated
manager class that derives from `base` (a `"package.module:ClassName"` path) or `adorm.Manager` and declares
`methods` (target `"manager"`); `queryset` is the generated query set class. It renders as
`ClassName(orm_name, method_manager=...)`, `QuerySetClass.as_manager(...)` or `Manager(...)`, with
`method_manager=` only when it is set and differs from `orm_name`. Specs with the same `class_key` (`class_name`,
`methods`, `queryset`, `base`) share one generated class. `has_methods` is true when the manager or its query set
class declares driver methods; `is_plain` is true without ORM names, class and query set class.

A name that is not a public identifier or that would hide an attribute of `adorm.Model` (other than `objects`), an
`orm_name` or `method_manager` that is not a public identifier, methods or a base without a class name, a method of
the wrong target, two methods with one name, and driver methods without either ORM name raise `ValueError`.

```python
ManagerSpec("all_loans")                                            # all_loans = Manager()
ManagerSpec("objects", "objects", "LoanManager", (lend,), loans)    # objects = LoanManager("objects")
ManagerSpec("objects", None, "UserManager", (create_user,), method_manager="objects")
# objects = UserManager(method_manager="objects")
```

### `adorm.codegen.QuerySetSpec(class_name, methods=(), base=None)`

A generated query set class: `class_name` derives from the hand-written class at `base` (`"package.module:ClassName"`)
or from `adorm.QuerySet`, and declares `methods` (target `"queryset"`). A class name that is not an identifier, a path
in another form, a method of the wrong target and two methods with one name raise `ValueError`.

### `adorm.codegen.MethodSpec(name, target, parameters=(), returns=None, doc=None, queryset_only=False)`

A driver method, rendered with [`@driver_method`](./models#adorm-driver-method-func-queryset-only-false).
`target` is `"manager"` or `"queryset"`; `parameters` are the `ParameterSpec` values after `self`; `returns` the return
type, `None` for no annotation; `doc` the docstring (without one the body is `...`). `queryset_only` renders
`@driver_method(queryset_only=True)` and only a query set method takes it. A name that is not public, an unknown
target and parameters in an order Python refuses raise `ValueError`.

```python
MethodSpec("archive", "queryset", (ParameterSpec("reason", type=TypeSpec("str"), default=""),), TypeSpec("int"))
# @driver_method
# async def archive(self, reason: str = "") -> int: ...
```

### `adorm.codegen.ParameterSpec(name, kind="positional_or_keyword", type=None, default=NO_DEFAULT)`

A parameter of a driver method: `kind` is `"positional_only"`, `"positional_or_keyword"`, `"var_positional"`,
`"keyword_only"` or `"var_keyword"`; `type` is a `TypeSpec` or `None` for no annotation; `default` a value the
renderer writes as a literal. A name that is not an identifier, an unknown kind, a model type and a default on
`*args` or `**kwargs` raise `ValueError`.

### `adorm.codegen.NO_DEFAULT`

The `default` of a field without a default value.

### `adorm.codegen.python_name(orm_name)`

The generated name for an ORM name: `orm_name` with `_` appended while it is an attribute of `adorm.Model` or a Python
keyword. A name starting with `_` loses its leading underscores and gets `_` appended. A name that is not an
identifier, consists of underscores only, starts with a digit once they are removed, or gives a name containing `__`
(the field path separator) raises `ValueError`.

```python
python_name("title")        # "title"
python_name("model_dump")   # "model_dump_"
python_name("json")         # "json_"
python_name("class")        # "class_"
python_name("_order")       # "order_"
```

### `adorm.codegen.compute_schema_hash(label, pk, fields, relations, parents=(), managers=())`

16 hexadecimal digits of the SHA-256 of a canonical JSON form of the label, the primary key, the parent labels, each
field's name, ORM name, type, nullability and constraints, the forward relations, and the managers with an ORM name or
driver methods: their names, ORM names and the methods' names, targets, `queryset_only` and parameter names, kinds and
types. Defaults, descriptions, return types, client bases and reverse relations do not change it; nor do managers
without an ORM name and methods.

### `adorm.codegen.CodegenError`

Generation cannot proceed: an unknown app label, a model with a composite primary key or a name no Python name maps
to, two fields of a model with the same Python name, a cycle of base classes between modules, a file to overwrite
that the generator did not write, no settings module.

### `adorm.codegen.CodegenWarning`

A `UserWarning` for a construct that is not generated (a generic relation, a relation that references a
field other than the target's primary key, a manager that would hide an attribute of `adorm.Model`, an exposed method
that is a coroutine function, returns a query set or has a default that is not a literal) or is generated as `Any`
(an unknown field type, a method annotation it cannot write), for a validator pattern pydantic cannot compile and for a
forward relation to a model that is not generated.

## Rendering

### `adorm.codegen.HEADER`

The first line of every generated file. A file that starts with it belongs to the generator.

### `adorm.codegen.render_module(app, *, classes=None, line_length=88, source="Django")`

The source of `app`'s module. `classes` maps the labels of models in other modules to `(module, class name)`, for a
base class in another app (imported as `from .<module> import <class>`). A statement longer than `line_length` is
split one argument per line with a trailing comma, a long description into implicitly concatenated strings and a long
`Literal` into a module-level alias (`MemberLevel = Literal[...]`). Imported modules and names that clash with a class,
field or relation name of the module are imported under an alias (`from adorm import Model as _Model`,
`import datetime as _datetime`).

### `adorm.codegen.render_package(apps, *, line_length=88)`

The source of `__init__.py`: it imports every app module and lists every class in `MODELS`.

## Files

### `adorm.codegen.plan(apps, *, line_length=88, source="Django")`

The generated files by name: `__init__.py` and `<app_label>.py` per app. An app labelled `__init__`, and apps whose
modules would import each other's base classes in a cycle (a model of app A derives from one of app B, and one of B
from one of A), raise `CodegenError`.

### `adorm.codegen.check(output, files)`

The list of `Difference` between the directory `output` and `files`; empty when the directory is up to date. Files
compare byte for byte.

### `adorm.codegen.write(output, files)`

Creates `output` when missing, writes the files whose content differs, removes generated files that `files` no longer
holds, and returns a `SyncResult`. A file to write that exists and does not start with `HEADER` raises `CodegenError`
before anything is written. Files are written in UTF-8 with `\n` line endings.

```python
files = plan(introspect(["clubs"]), line_length=120)
result = write(Path("myproject/adorm_models"), files)
result.written, result.removed, result.unchanged
```

### `adorm.codegen.Difference(path, state)`

A file that does not match: `state` is `"missing"`, `"stale"`, `"obsolete"` or `"foreign"`. `str()` gives
`"<path>: <state>"`.

### `adorm.codegen.SyncResult`

What `write()` did: the paths in `written`, `removed` and `unchanged`.

## Django source

### `adorm.codegen.django.setup(settings=None)`

Sets Django up with the settings module `settings`, default `DJANGO_SETTINGS_MODULE`. No settings module, or one
Django cannot import or use, raises `CodegenError`.

### `adorm.codegen.django.introspect(app_labels=None, *, exclude=())`

A tuple of `AppSpec`, one per selected app with models: every app in `INSTALLED_APPS` order for `None`, else the given
apps in the given order. `exclude` holds app labels and model labels. Swapped models and automatic through models are
not included. An unknown app label, an app label that cannot name a module and an unsupported model raise
`CodegenError`; constructs that are not generated and forward relations to models outside the selection are reported
as `CodegenWarning`.

```python
setup("myproject.settings")
apps = introspect(["clubs", "billing"], exclude=["billing.Payment"])
```

### `adorm.codegen.django.model_spec(model, *, included=None)`

The `ModelSpec` of a Django model class. `included` holds the labels generated with it: a multi-table child or a
proxy derives from its parent's class only when the parent is included, and reverse relations come only from included
models, while a forward relation to a model outside it is generated and reported as `CodegenWarning`; `None` includes
every model. A composite primary key, and a model whose names give two fields or relations the same Python name, raise
`CodegenError`.

### `adorm.codegen.django.manager_specs(model, *, local=False, included=None, exposed=None)`

The `ManagerSpec` of every manager of a Django model class, in declaration order, by the rules in the
[how-to guide](/howto/generating-models#managers). `local` takes only the managers the model declares itself;
`included` holds the labels a driver method's return type may name (`None` for every model); `exposed` the
`ADORM_EXPOSE` methods, by default `exposed_methods()`. A client base that does not import or is not a subclass of
`adorm.Manager` or `adorm.QuerySet`, and an `ADORM_EXPOSE` method the manager does not have, raise `CodegenError`. A
public method outside Django's own modules that is neither exposed, nor a method of Django's `Manager` or `QuerySet`,
nor defined by the client base is not generated and is reported as `CodegenWarning`.

```python
manager_specs(apps.get_model("library.Loan"))
# (ManagerSpec("objects", "objects", "LoanManager", (...,), QuerySetSpec("LoanQuerySet", ...)), ManagerSpec("all_loans"))
```

### `adorm.codegen.django.ROUTED_METHODS`

The Django manager and query set methods whose override gives a generated manager its ORM name, so the driver runs
its operations through the Django manager: `get_queryset`, `all`, `filter`, `exclude`, `annotate`, `aggregate`,
`order_by`, `values`, `values_list`, `only`, `defer`, `select_related`, `prefetch_related`, `distinct`, `none`,
`select_for_update`, `get`, `first`, `last`, `count`, `exists`, `iterator`, `create`, `get_or_create`,
`update_or_create`, `update`, `delete`, `bulk_create`, `bulk_update` and `in_bulk`. An override of any other method
(`contribute_to_class`, `deconstruct`, `check` …) does not.

### `adorm.codegen.django.exposed_methods()`

The `ADORM_EXPOSE` setting as method names by `"app_label.Model.manager"`. An entry that is not
`"app_label.Model.manager.method"`, a string in place of the list, a model Django does not know and a manager the
model does not have raise `CodegenError`.

```python
# settings.py: ADORM_EXPOSE = ["auth.User.objects.create_user"]
exposed_methods()      # {"auth.User.objects": {"create_user"}}
```

### `adorm.codegen.django.field_spec(field)`

The `FieldSpec` of a concrete Django field, by the rules in the
[how-to guide](/howto/generating-models#fields).

### `adorm.codegen.django.schema_hash(model)`

The schema hash of a Django model class, the value `adorm generate` writes into `_meta.schema_hash`, managers included.
It does not depend on which other models are generated. A model it cannot describe raises `CodegenError`.

```python
from django.apps import apps

schema_hash(apps.get_model("clubs.Member"))   # "4b133aa8953d898b"
```

## Declarations in the ORM's code

`adorm.porting` holds the decorators the ORM's code applies and the generator reads. Importing it imports no ORM.

### `adorm.expose(func)`

Marks a method of a manager or query set class of the target ORM as a driver method and returns it. The generator
declares it on the generated class with [`@driver_method`](./models#adorm-driver-method-func-queryset-only-false) and
the method's signature; the driver runs the original. Django's `as_manager()` and `from_queryset()` copy the mark onto
the manager's method. Something that is not callable raises `TypeError`.

```python
class LoanQuerySet(models.QuerySet):
    @expose
    def archive(self, reason: str = "") -> int:
        return self.update(archived=True, note=reason)
```

A method of a manager the project cannot decorate, such as Django's `UserManager.create_user`, is listed in the
`ADORM_EXPOSE` setting instead:

```python
ADORM_EXPOSE = ["auth.User.objects.create_user"]
```

### `adorm.client_base(path)`

A class decorator for a manager or query set class of the target ORM: the generated class for it derives from the
adorm class at `path` (`"package.module:ClassName"`) instead of `adorm.Manager` or `adorm.QuerySet`. The base holds the
methods ported by hand that run in the client. Subclasses inherit the declaration. A path in another form raises
`ValueError`; decorating something that is not a class raises `TypeError`.

```python
@client_base("library.adorm_bases:LoanQuerySetBase")
class LoanQuerySet(models.QuerySet):
    def overdue(self, today):
        return self.filter(due__lt=today)
```

### `adorm.porting.is_exposed(func)`

Whether `func` carries the mark of `expose`.

### `adorm.porting.get_client_base(cls)`

The path `client_base` set on `cls` or its nearest base class that has one, `None` without one.

### `adorm.porting.EXPOSED_ATTRIBUTE`, `adorm.porting.CLIENT_BASE_ATTRIBUTE`

The attribute names the decorators set: `"__adorm_exposed__"` on a function, `"__adorm_client_base__"` on a class.
