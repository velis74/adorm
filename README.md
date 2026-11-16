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

The tests read the test database URL from `ADORM_TEST_DATABASE_URL` (default
`postgres://postgres@127.0.0.1:5432/adorm_test`). They create and drop the database it names (`adorm_test` when it names
`postgres` or none) and their own `adorm_test_demo`, `adorm_test_limited`, `adorm_test_parallel` and
`adorm_test_parallel_<n>` (the clones of `manage.py test --parallel`) databases on that server and leave other
databases and the server's roles untouched. Run them as a dedicated user with `CREATEDB`, not as a superuser (a
database's connection limit does not apply to a superuser, so the test of the pool's refusal handling is skipped):

```sql
CREATE ROLE adorm LOGIN CREATEDB PASSWORD '...';
```

With `ADORM_TEST_CREATE_ROLES=1` the tests of `SET ROLE` in connection profiles also run: they create and drop the
roles `adorm_test_login`, `adorm_test_alice` and `adorm_test_bob`, which need `CREATEROLE`. Roles belong to the whole
server and outlive the test databases, so set it only on a server of the tests' own (CI sets it).

The demo reads its database URL from `ADORM_DEMO_DATABASE_URL`. Both variables can be set in the file `.env` in the
repository root (ignored by git; `.env.example` lists them); variables set in the environment take precedence.

```bash
cp .env.example .env    # then fill in the user and password
uv run pytest
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
npm install
npm run docs:dev
```

## Licence

MIT, see [LICENSE](LICENSE).
