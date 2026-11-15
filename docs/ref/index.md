# API reference

This section documents every public name of the `adorm` package. A name is public when it is listed in
`adorm.__all__`; everything else is internal and may change without notice.

## Package

### `adorm.__version__`

The installed version as a string, matching `version` in `pyproject.toml`.

```python
import adorm

print(adorm.__version__)   # "0.0.1"
```

## Sections

Reference pages are grouped as in the Django documentation. A group links to its page once the package provides
the names it covers:

| Group | Covers |
|---|---|
| [QuerySet API](./queryset) | `QuerySet` methods, terminal coroutines, iteration |
| [Query](./query) | the immutable query description (`adorm.query.Query`) |
| [Operations](./operations) | the reads and writes a driver executes (`adorm.operations`) |
| [Operation encoding](./encoding) | model schemas and the JSON encoding of operations (`adorm.schema`, `adorm.codec`) |
| [Lookups](./lookups) | field lookups (`exact`, `icontains`, `in`, …) and relation traversal |
| [Expressions, functions and aggregates](./expressions) | `F`, `Value`, `Q`, `Func` and its functions, aggregates, `Case`, `Subquery` |
| [Model API](./models) | `Model`, `ModelMeta`, relation descriptors, the model registry |
| [Sessions](./sessions) | `create_session`, `current_session`, `Session`, `BoundModel`, the active transactions of a context (`adorm.executor`) |
| Transactions and timeouts | `atomic`, `connection_options`, `timeout` |
| [Exceptions](./exceptions) | the exception hierarchy |
| Settings | pool settings and connection profiles |
| [Drivers](./drivers) | the driver contract (`adorm.drivers`) |
| [Events](./events) | event types and their dispatch (`adorm.events`) |
