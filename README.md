# adorm

adorm (Async Django ORM, pronounced "adore") is an asynchronous ORM interface for Python. Applications build
queries and model operations with adorm's own API; a driver executes them on a target ORM or database. The
first drivers target the Django ORM and PostgreSQL directly.

The package is pre-release; the API reference on the documentation site lists what it provides.

## Requirements

- Python 3.11 or newer
- PostgreSQL for the test suite

## Development

The project uses [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

The tests read the PostgreSQL server URL from `ADORM_TEST_DATABASE_URL` (default
`postgres://postgres@127.0.0.1:5432/postgres`). They create and drop their own `adorm_test` and `adorm_test_demo`
databases on that server and leave other databases untouched; the pool tests also create and drop the login role
`adorm_test_limited`.

```bash
ADORM_TEST_DATABASE_URL=postgres://user:password@127.0.0.1:5432/postgres uv run pytest
```

## Demo

`demo.py` runs the demo application: a Django project with a club domain (`demo/backend`), the adorm models generated
from it (`demo/models`), a FastAPI app using them (`demo/api`) and PostgreSQL. It creates, migrates and seeds the
database `ADORM_DEMO_DATABASE_URL` (default `postgres://postgres@127.0.0.1:5432/adorm_demo`), then serves the API on
<http://127.0.0.1:8000/docs>; its `/api/data` endpoints run sessions and transactions on the executor.

```bash
uv run python demo.py --list-drivers
uv run python demo.py
```

## Documentation

The documentation site is in `docs/` (VitePress, published on docs.velis.si):

```bash
cd docs
npm install
npm run docs:dev
```

## Licence

MIT, see [LICENSE](LICENSE).
