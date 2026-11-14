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

Reference pages are grouped as in the Django documentation. Each group gets its pages as the package provides
the names it covers:

| Group | Covers |
|---|---|
| QuerySet API | `QuerySet` methods, terminal coroutines, iteration |
| Lookups | field lookups (`exact`, `icontains`, `in`, …) and relation traversal |
| Expressions, functions and aggregates | `F`, `Value`, `Q`, `Func` and its functions, aggregates |
| Model API | `Model`, `objects` and BoundModel, relation descriptors |
| Sessions and transactions | `create_session`, `atomic`, `connection_options`, `timeout` |
| Exceptions | the exception hierarchy |
| Settings | pool settings and connection profiles |
| Drivers | the driver contract and the bundled drivers |
