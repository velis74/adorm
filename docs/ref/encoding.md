# Operation encoding

Operations and query nodes convert to JSON-safe data and back, for sending an operation across a process boundary.
A value is encoded with a pydantic `TypeAdapter` for the type of the field it is compared with or written to, taken
from the model schema, as in the argument transport of [`celery_viewset`](:fastapi_viewsets:/) in fastapi-viewsets.

```python
from adorm.codec import decode_value, encode_value
from adorm.operations import Create, Operation
from adorm.query import Exists, OuterRef, Query
from adorm.query.lookups import Exact
from adorm.schema import ModelSchema, Schema, resolve_field_type
```

## Encoding an operation

```python
import datetime
import decimal

schema = Schema([
    ModelSchema(
        "club.Member",
        pk="id",
        fields={"id": int, "name": str, "fee": decimal.Decimal, "joined": datetime.datetime, "club_id": int | None},
        relations={"club": "club.Club"},
    ),
    ModelSchema("club.Club", pk="id", fields={"id": int, "name": str}, relations={"member": "club.Member"}),
])

operation = Create("club.Member", {"name": "Ann", "fee": "12.50", "joined": datetime.datetime(2026, 11, 14)})
data = operation.to_dict(schema)
# {"operation": "create", "model": "club.Member",
#  "values": {"name": {"value": "Ann"}, "fee": {"value": "12.50"}, "joined": {"value": "2026-11-14T00:00:00"}}}

restored = Operation.from_dict(data, schema)
dict(restored.values)["fee"]          # Decimal('12.50')
```

Encoding and decoding use the same schema. Data encoded with a schema decodes only with a schema that types the
same positions; decoding a typed value without its type raises `ValueError`.

## The data

| Element | Encoded as |
|---|---|
| an operation | `{"operation": <kind>, ...}`: `query`, `model`, `values`, `pk`, `lookups` … as the operation has them |
| a node | `{"node": <node name>, <attribute>: <encoded attribute>, ...}` for every attribute in the node's `_fields` |
| a typed value | `{"value": <JSON>}` |
| an untyped value | `{"type": <type name>, "value": <JSON>}` |
| the values of `in`, `range` and `overlap` | `{"items": [<node or value>, ...]}` |
| field values of a write | `{<field name>: <node or value>, ...}` |

Node attributes that are not values are strings, integers, booleans, `null`, nested nodes or lists of them. An
`output_type` is encoded as its type name.

```python
Query("club.Member").filter(fee__gt=10).to_dict(schema)["filters"]
# [{"node": "Q", "connector": "AND", "negated": False,
#   "children": [{"node": "lookup.gt", "lhs": {"node": "F", "name": "fee"}, "rhs": {"value": "10"}}]}]
```

### Typed and untyped positions

A value is typed by:

- a lookup's right-hand side: the type of the expression on the left (see `expression_type()` below), passed through
  the lookup's [`rhs_type()`](./lookups#adorm-query-lookup-lhs-rhs):
  - `isnull`: `bool`;
  - `regex`, `iregex` and the text pattern lookups (`iexact`, `contains`, `icontains`, `startswith`, `istartswith`,
    `endswith`, `iendswith`): `str`, except `contains` on a container field (the field's type) or a record field
    (untyped);
  - the others: the field's type; each value of `in` and `range` has that type;
  - each value of `overlap`: the item type of the array field (`str` for `list[str]`);
  - an expression in place of the values (`disciplines__overlap=Value(["Lead"])`): the type of the expression on the
    left;
- a field value of `Create`, each row of `BulkCreate`, `Save`, `Update` and the defaults of `GetOrCreate` and
  `UpdateOrCreate`: the field's type;
- the primary key of `Save` and `DeleteInstance`: the primary key's type;
- `Value(value, output_type=...)`: the `output_type`;
- a `Value` without `output_type`: the type of its position. A `Case` passes its `output_type`, or else its own
  position's type, to the results of its `When` branches and its `default`; `Coalesce`, `Greatest`, `Least` and
  `NullIf` (a function whose `output_from` is `"arguments"`) pass it to their arguments. Other expressions
  (arithmetic, other functions, conditions) pass no type to their operands.

The type of an expression is, in this order:

- for `F`, the field's type in the queried model; for `OuterRef`, in the enclosing query's model;
- for an annotation name, the annotation's type;
- its `output_type`;
- for a function, the type its `output_from` gives (see [`Func`](./expressions#adorm-func-expressions-output-type-none));
- the first known type of a `Case` result;
- for a one-column `Subquery`, the type of the selected field.

The combined queries of a [set operation](./query#union-others-all-false) are typed by their own models at the level
of the combined query; an `OuterRef` in them refers to the query that encloses the combination.

```python
inner = Query("club.Club").filter(Exact(OuterRef("joined"), "2026-01-01T00:00:00"))
combined = inner.union(Query("club.Club").filter(name="Chess"))
Query("club.Member").filter(Exists(combined)).to_dict(schema)
# the OuterRef value is typed by club.Member's joined, the name by club.Club's name
```

```python
Update(Query("club.Member"), {"level": Case(When(age__gt=5, then=Value(Level.MASTER)), default=F("level"))})
# the Value is encoded as {"value": "master"}, typed by the field level
```

A path that ends at a relation (`club=5`) has the type of the target's primary key. In an untyped position (no
schema, a model the schema does not know, a key of a JSON field, an expression without a type) the value is encoded
with its inferred type, named in the data. A field the schema of a known model does not have raises `ValueError`.

Encoding with a type validates the value with the `TypeAdapter` in pydantic's lax mode: `"12.50"` for a `Decimal`
field encodes as `"12.50"` and decodes as `Decimal("12.50")`; `"5"` for an `int` field becomes `5`. `ValueError` is
raised for a value not valid for its type and for a NaN or infinite float, typed or not (JSON cannot represent it).
`None` encodes as `{"value": null}` in every position. Values decode by their field's type; a decoded operation
equals the original when its values had their fields' types.

## `adorm.schema`

### `adorm.schema.ModelSchema(label, pk, fields, relations={}, *, orm_names={}, methods={})`

The schema of the model `label`, a frozen dataclass; the mappings are copied into read-only views.

| Field | Contents |
|---|---|
| `pk` | the primary key field name |
| `fields` | every concrete field name, including a foreign key's column field such as `club_id`, to its type hint |
| `relations` | every relation name (forward, reverse and many-to-many) to the target model's label |
| `orm_names` | a field or relation name to the target ORM's name where they differ, as [`ModelMeta.orm_names`](./models#adorm-modelmeta-label-pk-id-schema-hash-none-orm-names-none-concrete-model-none-delete-models-none) |
| `methods` | the [`MethodKey`](#adorm-schema-methodkey) of every driver method of the model's managers to its [`MethodSchema`](#adorm-schema-methodschema-positional-keyword-var-positional-none-var-keyword-none); a [`Call`](./operations#adorm-operations-call-query-method-target-queryset-args-kwargs-manager-none) encodes its arguments with those types |

Raises `ValueError` for a label (also a relation target) that is not `"app_label.ModelName"`, a `pk` not in
`fields`, a name that is both a field and a relation, an `orm_names` key that is neither, and a `methods` key that is
not a `(manager, "manager" | "queryset", method)` triple; `TypeError` for a `methods` value that is not a
`MethodSchema`.

```python
ModelSchema("club.Payment", "code", {"code": uuid.UUID, "amount": decimal.Decimal}, {"member": "club.Member"})
ModelSchema("club.Note", "id", {"id": int, "json_": str}, orm_names={"json_": "json"})
```

Any hint pydantic adapts can be a field type: scalars, `X | None`, `Literal`, enums, `list[str]`, pydantic models.

### `adorm.schema.MethodKey`

The type of a key of `ModelSchema.methods`: `tuple[str, str, str]`, the ORM manager name, the target (`"manager"` or
`"queryset"`) and the method name.

```python
("objects", "queryset", "archive")
```

### `adorm.schema.MethodSchema(positional=(), keyword={}, var_positional=None, var_keyword=None)`

The parameter types of a driver method, after `self`; a frozen dataclass. `positional`: the types of the
parameters that take positional arguments, in order. `keyword`: the types of those that take keyword arguments, by
name. `var_positional`, `var_keyword`: the types of `*args` and `**kwargs`. `None` marks an untyped parameter, whose
values are encoded with their inferred type.

| Method | Returns |
|---|---|
| `arg_type(index)` | the type of the positional argument at `index`: `positional[index]`, or `var_positional` past its end |
| `kwarg_type(name)` | the type of the keyword argument `name`: `keyword[name]`, or `var_keyword` |

```python
# async def lend(self, reader_id: int, book_id: str, *, days: int = 14, **options: str)
schema = MethodSchema((int, str), {"reader_id": int, "book_id": str, "days": int}, None, str)
schema.arg_type(1)          # str
schema.kwarg_type("colour") # str
```

### `adorm.schema.SchemaSource`

A runtime-checkable protocol for anything that returns schemas: `model_schema(label)` returns the `ModelSchema` of a
label, or `None` for a model it does not know.

### `adorm.schema.Schema(models=())`

A registry of `ModelSchema` values by label, a `SchemaSource`. `add(model)` registers a schema and replaces an
earlier one of the same label; a value that is not a `ModelSchema` raises `TypeError`. `label in schema`,
`len(schema)` and iterating over the schemas work as for a collection.

### `adorm.schema.resolve_field_type(source, label, path)`

The type hint of the field at `path` (the segments of a field path) from the model `label`, resolved as
[`resolve_lookup()`](./lookups#adorm-query-resolve-lookup-target-key-annotations-source-none) resolves a field path.

- Every segment but the last is a relation, until a JSON or record field; the following segments are keys inside
  its value.
- `pk` stands for the primary key at any segment.
- A path that ends at a relation resolves to the target's primary key type.
- `None` when `source` is `None`, does not know a model on the way, or the path continues into a JSON field's value.
- A segment that is neither a field nor a relation of a model `source` knows, and segments after a field that is
  neither a relation nor a JSON field, raise
  [`FieldError`](./exceptions#adorm-fielderror-message-none-model-none-key-none-original-none-sqlstate-none).

```python
resolve_field_type(schema, "club.Member", ["club", "name"])   # str
resolve_field_type(schema, "club.Member", ["club"])           # int (club.Club's primary key)
resolve_field_type(schema, "club.Member", ["pk"])             # int
resolve_field_type(schema, "club.Member", ["nickname"])       # FieldError: club.Member has no field or relation
resolve_field_type(schema, "club.Guest", ["nickname"])        # None (a model the schema does not know)
```

## `adorm.codec`

### `adorm.codec.encode_value(value, hint=None)`

`value` encoded with the type `hint` as `{"value": ...}`, or, when `hint` is `None` or `Any`, with its inferred type
as `{"type": ..., "value": ...}`. `bytes` are base64 strings at any depth of `Annotated`, unions and generic
containers in the hint. A union whose members can have the same JSON form (two members of the same JSON kind,
such as `bytes | str`, `str | datetime.date` or `int | float`, unless both are enumerations of different values)
records the member a value belongs to, as `{"branch": <index>, "value": ...}`, the index counting the members other
than `None` in the order of the hint; the member is the first that takes the value strictly with its own type, else
the first that takes it strictly, else the first that takes it. The metadata of an `Annotated` union, such as a length
limit, applies to each member. `None`, the values of other unions and those of a union with a pydantic discriminator
are encoded as the member's value. Bytes inside a pydantic model, dataclass or `TypedDict` are base64 with
`ser_json_bytes="base64"` and `val_json_bytes="base64"` in that type's config, else UTF-8 text.

Raises `ValueError` when the value is not valid for `hint`, contains NaN or infinity, or holds non-UTF-8 bytes in
such a type without the base64 config. Raises `TypeError` when an untyped value is not of a type in `TYPES`, nor a
list, tuple, set, frozenset or dict with string keys of such values.

```python
encode_value(decimal.Decimal("1.50"), decimal.Decimal)   # {"value": "1.50"}
encode_value(b"\x00\xff", bytes)                          # {"value": "AP8="}
encode_value("YWJj", bytes | str)                         # {"value": {"branch": 1, "value": "YWJj"}}
encode_value(5, int | str | None)                         # {"value": 5}
encode_value(datetime.date(2026, 11, 14))                 # {"type": "date", "value": "2026-11-14"}
encode_value([1, "a"])                                    # {"type": "list", "value": [{"type": "int", ...}, ...]}
```

### `adorm.codec.decode_value(data, hint=None)`

The value `encode_value()` encoded. Data that names its type decodes by that type, whatever `hint` is; otherwise
`hint` must be the type it was encoded with. Raises `ValueError` for malformed data, an unknown type name, a value not
valid for its type, or typed data without `hint`.

### `adorm.codec.TYPES`

The scalar types an untyped value may have, by type name, checked in this order: `bool`, `int`, `float`, `str`,
`decimal`, `datetime`, `date`, `time`, `timedelta`, `uuid`, `bytes`. `True` and `1` keep their types, as in node
equality.

### `adorm.codec.type_name(tp)`

The name of a type in `TYPES`, or of `list`, `tuple`, `set`, `frozenset` or `dict`; any other type raises
`TypeError`. Node `output_type` values are encoded with it. The constructors of `Value`, `CombinedExpression`, `Func`
and its subclasses, `Case` and `Subquery` raise `TypeError` for an `output_type` of another type.

### `adorm.codec.type_from_name(name)`

The type `type_name()` names; an unknown name raises `ValueError`.

### `adorm.codec.adapter_for(hint)`

The cached `TypeAdapter` that encodes and decodes values of type `hint`, with `bytes` as base64 and unions recorded
with their member as in `encode_value()`. Its validator takes the encoded form: a recorded union validates
`{"branch": <index>, "value": ...}` only.

### `adorm.codec.node_name(cls)`

The name a node class is encoded under: `lookup.<lookup name>` for a registered lookup, the class name for adorm's
own nodes, `<module>.<qualified name>` for any other class. Decoding looks the name up among the imported subclasses
of `Node`; a custom function or lookup must be imported on the decoding side. A name that matches more than one
class raises `ValueError`.

```python
node_name(Exact)       # "lookup.exact"
node_name(Lower)       # "Lower"
node_name(Reverse)     # "myapp.functions.Reverse"
```

### `adorm.codec.encode_node(node, schema=None)`

`node.to_dict(schema)`: the node as JSON-safe data.

### `adorm.codec.decode_node(data, schema=None)`

The node `encode_node()` encoded with the same `schema`. Nodes are rebuilt without calling their constructors, with
the constructors' checks: field paths, model labels, connectors, truncation kinds, `ForUpdate` options, the types of
a query's attributes, the kinds of child nodes (a `Q`'s children and a `When`'s condition are conditions, a `Case`
holds `When` nodes, a `Subquery` a `Query`, a lookup's left-hand side is an expression), the flags of `OrderBy` and
aggregates, and the number of a function's arguments. Raises `ValueError` for malformed data, an unknown node name
or a node its constructor refuses.

### `adorm.codec.Encoder(schema)`

Encodes the nodes and operands of one top-level value, resolving field types against `schema`. `to_dict()` of
operations, custom ones included, uses it:

| Member | Meaning |
|---|---|
| `node(node, hint=None)` | a node and its descendants as data; `hint` is the type of the node's position (it types a `Value` without `output_type`) |
| `operand(value, hint=None, *, sequence=False)` | a node, or a value encoded with `hint`; with `sequence`, a tuple of them as `{"items": [...]}` |
| `scope(model, annotations=())` | context manager: resolve field paths against the model `model` and the names of the `(name, expression)` pairs `annotations` inside the block |
| `field_type(path, *, outer=False)` | the type of a field path in the current model (`outer`: the enclosing query's), `None` if unknown; `FieldError` for a field a known model does not have |
| `expression_type(expression)` | the type of an expression, as described under [typed positions](#typed-and-untyped-positions) |
| `child_hint(cls, output_type, name, hint)` | static: the type a `cls` node in a position of type `hint` passes to the children in its attribute `name` |
| `lookup_value_type(lookup, lhs)` | the type of a right-hand side value of the lookup class `lookup` applied to `lhs` |

### `adorm.codec.Decoder(schema)`

Decodes what `Encoder` produced; `schema` must type values as the encoding schema did. It has the members of
`Encoder`; `node(data, hint=None)` and `operand(data, hint=None, *, sequence=False)` decode.
