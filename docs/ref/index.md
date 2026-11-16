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
| [Model API](./models) | `Model`, `ModelMeta`, relation descriptors, `Manager` and `driver_method`, the model registry |
| [Sessions](./sessions) | `create_session`, `current_session`, `Session`, `BoundModel`, the runner (`adorm.executor`) |
| [Pool and router](./pool) | `Pool`, `Router`, slots, leases and their request queues and event channels, held slots (`adorm.executor`) |
| [Transactions](./transactions) | `atomic`, `Atomic`, `on_commit`, the active transactions of a context (`adorm.executor`) |
| [Connection options and timeouts](./options) | `connection_options`, `timeout`, `Timeout`, the layers of options (`adorm.executor.options`) |
| [Schema checks](./checks) | `check_schema`, `SchemaProblem`, `adorm check-schema` (`adorm.checks`) |
| [Exceptions](./exceptions) | the exception hierarchy |
| [Settings](./settings) | `configure`, connection profiles, pool settings, run-time profiles (`adorm.settings`), the `ADORM` setting and the Django app (`adorm.contrib.django`) |
| [Drivers](./drivers) | the driver contract (`adorm.drivers`), the Django driver and its query translation (`adorm.drivers.django`, `adorm.drivers.django.translation`), the native PostgreSQL driver (`adorm.drivers.asyncpg`, its writes in `adorm.drivers.asyncpg.writes`), SQL statements (`adorm.drivers.sql`), Django's compiler for asyncpg (`adorm.drivers.asyncpg.compiler`) |
| [Events](./events) | event types, their encoded form, the client's event channel and dispatch (`adorm.events`) |
| [Model generator](./codegen) | `adorm generate`, the model description, rendering and files, `expose` and `client_base` (`adorm.codegen`, `adorm.cli`, `adorm.porting`) |
| [Testing](./testing) | waiting until an operation is in the database or waits for a lock (`wait_for_operation`, `wait_for_lock`, `running_operations`); test mode under Django's `TestCase` and `TransactionTestCase` (`adorm.testing.django`) |
