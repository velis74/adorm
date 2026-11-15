# Operation encoding

Operations and query nodes convert to JSON-safe data and back, so an operation can cross a process boundary: from
the application to an adorm server (over [muxws](:muxws:/) or HTTP) that executes it with an ORM driver. Values are encoded by the type
of the field they are compared with or written to, taken from the model schema, with a pydantic `TypeAdapter`: the
same concept as the argument transport of [`celery_viewset`](:fastapi_viewsets:/) in fastapi-viewsets.

```python
from adorm.codec import decode_value, encode_value
from adorm.operations import Create, Operation
from adorm.query import Query
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

The encoding and the decoding side use the same schema. Data encoded with a schema decodes only with a schema
that types the same positions: decoding a typed value without its type raises `ValueError`.

## The data

| Element | Encoded as |
|---|---|
| an operation | `{"operation": <kind>, ...}`: `query`, `model`, `values`, `pk`, `lookups` … as the operation has them |
| a node | `{"node": <node name>, <attribute>: <encoded attribute>, ...}` for every attribute in the node's `_fields` |
| a typed value | `{"value": <JSON>}` |
| an untyped value | `{"type": <type name>, "value": <JSON>}` |
| the values of `in` and `range` | `{"items": [<node or value>, ...]}` |
| field values of a write | `{<field name>: <node or value>, ...}` |

Node attributes that are not values are strings, integers, booleans, `null`, nested nodes or lists of them. An
`output_type` is encoded as its type name.

```python
Query("club.Member").filter(fee__gt=10).to_dict(schema)["where"]
# {"node": "Q", "connector": "AND", "negated": False,
#  "children": [{"node": "lookup.gt", "lhs": {"node": "F", "name": "fee"}, "rhs": {"value": "10"}}]}
```

### Typed and untyped positions

A value is typed by:

- a lookup's right-hand side: the type of the expression on the left (see `expression_type()` below), passed through
  the lookup's [`rhs_type()`](./lookups#adorm-query-lookup-lhs-rhs): `isnull` takes a `bool`; `regex`, `iregex` and
  the text pattern lookups (`iexact`, `contains`, `icontains`, `startswith`, `istartswith`, `endswith`, `iendswith`) a
  string, except `contains` on a container field (the field's type) or a record field (untyped); the others the
  field's type; each value of `in` and `range` has that type;
- a field value of `Create`, `Save`, `Update` and the defaults of `GetOrCreate`: the field's type; the primary key of
  `Save`: the primary key's type;
- `Value(value, output_type=...)`: the `output_type`;
- a `Value` without `output_type`: the type of its position. A `Case` passes its `output_type`, or else its own
  position's type, to the results of its `When` branches and its `default`; a `Coalesce` passes it to its arguments.
  Other expressions (arithmetic, other functions, conditions) pass no type to their operands.

The type of an expression is its field's type for `F` (against the queried model) and `OuterRef` (against the
enclosing query's model), the annotation's type for an annotation name, else its `output_type`, else the first known
type of a `Coalesce` argument or a `Case` result, or the type of the field a one-column `Subquery` selects.

```python
Update(Query("club.Member"), {"level": Case(When(age__gt=5, then=Value(Level.MASTER)), default=F("level"))})
# the Value is encoded as {"value": "master"}, typed by the field level
```

A path that ends at a relation (`club=5`) has the type of the target's primary key. A position the schema does not
type (no schema, a model the schema does not know, a key of a JSON field, an expression without a type) encodes the
value with the type inferred from it and names that type in the data. A field the schema of a known model does not
have raises `ValueError`.

Encoding with a type validates the value with the `TypeAdapter`, in pydantic's lax mode: `"12.50"` for a `Decimal`
field encodes as `"12.50"` and decodes as `Decimal("12.50")`, `"5"` for an `int` field becomes `5`. A value that is not
valid for its type raises `ValueError`, and so does a float that is NaN or infinite, typed or not, since JSON has no
form for it. `None` encodes as `{"value": null}` in every position. Values decode by their field's type, so a decoded
operation equals the original when its values already had their fields' types.

## `adorm.schema`

### `adorm.schema.ModelSchema(label, pk, fields, relations={}, *, orm_names={})`

The schema of the model `label`: `fields` maps every concrete field name (a foreign key's column field such as
`club_id` included) to its type hint, `relations` maps every relation name (forward, reverse and many-to-many) to the
target model's label, and `pk` names the primary key field. `orm_names` maps a field or relation name to the target
ORM's name where the two differ, as [`ModelMeta.orm_names`](./models#adorm-modelmeta-label-pk-id-schema-hash-none-orm-names-none)
does. A frozen dataclass; the mappings are copied into read-only views.

A label that is not `"app_label.ModelName"` (also among the relation targets), a `pk` that is not one of `fields`, a
name that is both a field and a relation and an `orm_names` key that is neither raise `ValueError`.

```python
ModelSchema("club.Payment", "code", {"code": uuid.UUID, "amount": decimal.Decimal}, {"member": "club.Member"})
ModelSchema("club.Note", "id", {"id": int, "json_": str}, orm_names={"json_": "json"})
```

Any hint pydantic adapts can be a field type: scalars, `X | None`, `Literal`, enums, `list[str]`, pydantic models.

### `adorm.schema.SchemaSource`

A runtime-checkable protocol for anything that returns schemas: `model_schema(label)` returns the `ModelSchema` of a
label, or `None` for a model it does not know.

### `adorm.schema.Schema(models=())`

A registry of `ModelSchema` values by label, a `SchemaSource`. `add(model)` registers a schema and replaces an
earlier one of the same label; a value that is not a `ModelSchema` raises `TypeError`. `label in schema`,
`len(schema)` and iterating over the schemas work as for a collection.

### `adorm.schema.resolve_field_type(source, label, path)`

The type hint of the field at `path` (the segments of a field path) from the model `label`. Every segment but the
last is a relation, `pk` stands for the primary key at any segment, and a path that ends at a relation resolves to
the target's primary key type. `None` when `source` is `None`, does not know a model on the way, or the path continues
past a field (a key of a JSON field). A segment that is neither a field nor a relation of a model `source` knows
raises `ValueError`.

```python
resolve_field_type(schema, "club.Member", ["club", "name"])   # str
resolve_field_type(schema, "club.Member", ["club"])           # int (club.Club's primary key)
resolve_field_type(schema, "club.Member", ["pk"])             # int
resolve_field_type(schema, "club.Member", ["nickname"])       # ValueError: club.Member has no field or relation
resolve_field_type(schema, "club.Guest", ["nickname"])        # None (a model the schema does not know)
```

## `adorm.codec`

### `adorm.codec.encode_value(value, hint=None)`

`value` encoded with the type `hint` as `{"value": ...}`, or with its inferred type as `{"type": ..., "value": ...}`
when `hint` is `None` or `Any`. `bytes` are base64 strings at any depth of `Annotated`, unions and generic containers
in the hint. Bytes inside a pydantic model, dataclass or `TypedDict` follow that type's config: with
`ser_json_bytes="base64"` and `val_json_bytes="base64"` they are base64, without it UTF-8 text. Raises `ValueError`
when the value is not valid for `hint`, contains NaN or infinity, or holds non-UTF-8 bytes in such a type without
the base64 config; `TypeError` when an untyped value's type is not in `TYPES`, a list, tuple, set, frozenset or a
dict with string keys of such values.

```python
encode_value(decimal.Decimal("1.50"), decimal.Decimal)   # {"value": "1.50"}
encode_value(b"\x00\xff", bytes)                          # {"value": "AP8="}
encode_value(datetime.date(2026, 11, 14))                 # {"type": "date", "value": "2026-11-14"}
encode_value([1, "a"])                                    # {"type": "list", "value": [{"type": "int", ...}, ...]}
```

### `adorm.codec.decode_value(data, hint=None)`

The value `encode_value()` encoded. Data that names its type decodes by that type, whatever `hint` is; otherwise
`hint` must be the type it was encoded with. Raises `ValueError` for malformed data, an unknown type name, a value not
valid for its type, or typed data without `hint`.

### `adorm.codec.TYPES`

The scalar types an untyped value may have, by type name, checked in this order: `bool`, `int`, `float`, `str`,
`decimal`, `datetime`, `date`, `time`, `timedelta`, `uuid`, `bytes`. `bool` comes before `int`, so `True` and `1`
keep their types, as they do in node equality.

### `adorm.codec.type_name(tp)`

The name of a type in `TYPES`, or of `list`, `tuple`, `set`, `frozenset` or `dict`; any other type raises
`TypeError`. Node `output_type` values are encoded with it, so an `output_type` must be one of these types: the
constructors of `Value`, `CombinedExpression`, `Func` (and its subclasses), `Case` and `Subquery` raise `TypeError`
for any other.

### `adorm.codec.type_from_name(name)`

The type `type_name()` names; an unknown name raises `ValueError`.

### `adorm.codec.adapter_for(hint)`

The cached `TypeAdapter` values of type `hint` are encoded and decoded with (with `bytes` as base64, as for
`encode_value()`).

### `adorm.codec.node_name(cls)`

The name a node class is encoded under: `lookup.<lookup name>` for a registered lookup, the class name for adorm's
own nodes, and `<module>.<qualified name>` for any other class. Decoding finds the class among the imported
subclasses of `Node`, so a custom function or lookup must be imported on the decoding side; a name that matches more
than one class raises `ValueError`.

```python
node_name(Exact)       # "lookup.exact"
node_name(Lower)       # "Lower"
node_name(Reverse)     # "myapp.functions.Reverse"
```

### `adorm.codec.encode_node(node, schema=None)`

`node.to_dict(schema)`: the node as JSON-safe data.

### `adorm.codec.decode_node(data, schema=None)`

The node `encode_node()` encoded with the same `schema`. It rebuilds nodes without calling their constructors and
checks what the constructors check: field paths, model labels, connectors, truncation kinds, `ForUpdate` options,
the types of a query's attributes, the kinds of child nodes (a `Q`'s children and a `When`'s condition are
conditions, a `Case` holds `When` nodes, a `Subquery` a `Query`, a lookup's left-hand side is an expression), the
flags of `OrderBy` and aggregates and the number of a function's arguments. Raises `ValueError` for malformed data,
an unknown node name or a node its constructor would refuse.

### `adorm.codec.Encoder(schema)`

Encodes the nodes and operands of one top-level value, resolving field types against `schema`. Operations use it in
`to_dict()`; a custom operation uses it the same way:

| Member | Meaning |
|---|---|
| `node(node, hint=None)` | a node and its descendants as data; `hint` is the type of the node's position (it types a `Value` without `output_type`) |
| `operand(value, hint=None, *, sequence=False)` | a node, or a value encoded with `hint`; with `sequence`, a tuple of them as `{"items": [...]}` |
| `scope(model, annotations=())` | context manager: resolve field paths against the model `model` and the names of the `(name, expression)` pairs `annotations` inside the block |
| `field_type(path, *, outer=False)` | the type of a field path in the current model (`outer`: the enclosing query's), `None` if unknown; `ValueError` for a field a known model does not have |
| `expression_type(expression)` | the type of an expression, as described under [typed positions](#typed-and-untyped-positions) |
| `child_hint(cls, output_type, name, hint)` | static: the type a `cls` node in a position of type `hint` passes to the children in its attribute `name` |
| `lookup_value_type(lookup, lhs)` | the type of a right-hand side value of the lookup class `lookup` applied to `lhs` |

### `adorm.codec.Decoder(schema)`

Decodes what `Encoder` produced; `schema` must type values as it did when they were encoded. It has the members of
`Encoder`, with `node(data, hint=None)` and `operand(data, hint=None, *, sequence=False)` decoding instead of
encoding.
