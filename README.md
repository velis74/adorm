# adorm

adorm (asynchronous database ORM, pronounced "adore") is an asynchronous ORM for Python. Applications build
queries and model operations with adorm's API; a driver executes them natively on PostgreSQL (asyncpg) or through
an existing ORM (Django). The API is modelled on the Django ORM.

The package is pre-release; the API reference on the documentation site lists what it provides.

## Requirements

- Python 3.11 or newer
- PostgreSQL for the test suite
- the conformance run on SQLite needs no database server (Python's `sqlite3`, Django 5.1 or newer)

## Development

The project uses [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

The tests read the test database URL from `ADORM_TEST_DATABASE_URL` (default
`postgres://postgres@127.0.0.1:5432/adorm_test`). On that server they create and drop the database it names
(`adorm_test` when it names `postgres` or none) and the databases `adorm_test_demo`, `adorm_test_limited`,
`adorm_test_parallel` and `adorm_test_parallel_<n>` (the clones of `manage.py test --parallel`). Other databases and
the server's roles are left untouched. Run the tests as a dedicated user with `CREATEDB`. Under a superuser the test of
the pool's refusal handling is skipped, because a database's connection limit does not apply to a superuser:

```sql
CREATE ROLE adorm LOGIN CREATEDB PASSWORD '...';
```

`ADORM_TEST_CREATE_ROLES=1` also runs the tests of `SET ROLE` in connection profiles. They create and drop the roles
`adorm_test_login`, `adorm_test_alice` and `adorm_test_bob` and need `CREATEROLE`. Roles belong to the whole server and
outlive the test databases: set the variable only on a server dedicated to the tests (CI sets it).

The demo reads its database URL from `ADORM_DEMO_DATABASE_URL`. Both variables can be set in `.env` in the repository
root (ignored by git; `.env.example` lists them). Variables set in the environment take precedence.

```bash
cp .env.example .env    # then fill in the user and password
uv run pytest
```

The conformance suite also runs the Django driver on SQLite, in a database file in a temporary directory, without
PostgreSQL:

```bash
uv run pytest --ds=tests.djangoproject.settings_sqlite tests/conformance
```

## Demo

`demo.py` runs the demo application on PostgreSQL:

- `demo/backend`: a Django project with a club domain;
- `demo/models`: the adorm models generated from it;
- `demo/api`: a FastAPI app that uses them; its `/api/data` endpoints run sessions and transactions on the executor.

It creates, migrates and seeds the database `ADORM_DEMO_DATABASE_URL` (default
`postgres://postgres@127.0.0.1:5432/adorm_demo`) and serves the API on <http://127.0.0.1:8000/docs>.

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
