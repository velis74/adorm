# Model generator

`adorm generate` and the Python API behind it. The [how-to guide](/howto/generating-models) shows the command in use
and what the generated modules contain. `adorm.codegen` imports no ORM; `adorm.codegen.django` imports Django.

```python
from adorm.codegen import check, plan, write
from adorm.codegen.django import introspect, schema_hash, setup
```

## Command line

### `adorm generate`

```text
adorm generate [-h] -o OUTPUT [--settings SETTINGS] [--pythonpath PYTHONPATH] [-e LABEL] [--check]
               [--line-length LINE_LENGTH] [app_label ...]
```

Writes one module per Django app and a package `__init__.py` into `OUTPUT`; with `--check`, writes nothing and
reports the differences. `python -m adorm generate` is the same command. Warnings (constructs that are not
generated, forward relations to models that are not generated) go to standard error as `warning: <message>`.

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

### `adorm.codegen.ModelSpec(label, class_name, pk, fields, relations, schema_hash, base=None, doc=None)`

A generated class: its label, class name, primary key field, own `fields` and `relations`, `schema_hash`, the label of
the generated model it derives from (`base`, `None` for `Model`) and its docstring. `orm_names` gives the ORM names of
its renamed fields and relations. Two fields or relations with one name raise `ValueError`.

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

### `adorm.codegen.RelationSpec(kind, name, orm_name, target, column=None, through=None, source_column=None, target_column=None, reverse=False)`

A relation, rendered as `ForeignKey` (`kind="foreign_key"`), `ReverseForeignKey` (`"reverse_foreign_key"`),
`ReverseOneToOne` (`"reverse_one_to_one"`) or `ManyToMany` (`"many_to_many"`, with `through`, `source_column` and
`target_column`; `reverse` for the side that did not declare it). `is_forward` is true for a foreign key and a
declared many-to-many relation. A missing column raises `ValueError`.

### `adorm.codegen.TypeSpec(name, args=(), values=(), nullable=False)`

A type: a builtin (`"int"`), a `typing` name (`"typing.Any"`) or a dotted name in a module (`"datetime.date"`), with
type arguments (`TypeSpec("list", (TypeSpec("str"),))`), `typing.Literal` values and `| None`. `str()` gives the
annotation text used in the schema hash.

```python
str(TypeSpec("list", (TypeSpec("int", nullable=True),)))     # "list[int | None]"
str(TypeSpec("typing.Literal", values=("a", "b")))           # "Literal['a', 'b']"
```

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

### `adorm.codegen.compute_schema_hash(label, pk, fields, relations, parents=())`

16 hexadecimal digits of the SHA-256 of a canonical JSON form of the label, the primary key, the parent labels, each
field's name, ORM name, type, nullability and constraints, and the forward relations. Defaults, descriptions and
reverse relations do not change it.

### `adorm.codegen.CodegenError`

Generation cannot proceed: an unknown app label, a model with a composite primary key or a name no Python name maps
to, two fields of a model with the same Python name, a cycle of base classes between modules, a file to overwrite
that the generator did not write, no settings module.

### `adorm.codegen.CodegenWarning`

A `UserWarning` for a construct that is not generated (a generic foreign key or relation, a relation that references a
field other than the target's primary key) or is generated as `Any` (an unknown field type), for a validator pattern
pydantic cannot compile and for a forward relation to a model that is not generated.

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

### `adorm.codegen.django.field_spec(field)`

The `FieldSpec` of a concrete Django field, by the rules in the
[how-to guide](/howto/generating-models#fields).

### `adorm.codegen.django.schema_hash(model)`

The schema hash of a Django model class, the value `adorm generate` writes into `_meta.schema_hash`. It does not
depend on which other models are generated. A model it cannot describe raises `CodegenError`.

```python
from django.apps import apps

schema_hash(apps.get_model("clubs.Member"))   # "4b133aa8953d898b"
```
