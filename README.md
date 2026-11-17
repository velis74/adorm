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
(`<name>`: `ADORM_TEST_DATABASE_NAME` when set, `adorm_test` when the URL names `postgres` or none) and the databases
`<name>_demo`, `<name>_demo_owner`, `<name>_limited`, `<name>_parallel` and `<name>_parallel_<n>` (the clones of
`manage.py test --parallel`). Other databases and the server's roles are left untouched; runs with different names
on one server leave each other's databases alone. Run the tests as a dedicated user with `CREATEDB`. Under a
superuser the test of the pool's refusal handling is skipped, because a database's connection limit does not apply to
a superuser, and so are the tests of the demo, which refuses a superuser:

```sql
CREATE ROLE adorm LOGIN CREATEDB PASSWORD '...';
```

`ADORM_TEST_CREATE_ROLES=1` also runs the tests of the `role` connection option. They create and drop the roles
`<name>_login`, `<name>_alice` and `<name>_bob` and need `CREATEROLE`. Roles belong to the whole server and
outlive the test databases: set the variable only on a server dedicated to the tests. CI runs the suite with
`CREATEDB` only, then grants `CREATEROLE` and runs these tests separately.

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

`demo.py` runs the demo application, a library on PostgreSQL: every example and concurrency scenario on every
available driver, with the adorm code, the SQL each driver sent, the result and the time side by side.

- `demo/backend`: a Django project with the app `library`;
- `demo/models`: the adorm models generated from it;
- `demo/api`: a FastAPI app running the examples and scenarios;
- `demo/frontend`: the Vue frontend; `demo/e2e`: its Playwright tests.

It creates (owned by the user of `ADORM_DEMO_DATABASE_URL`, which must not be a superuser), migrates and seeds the
demo database, then serves the API and the Vite dev server on <http://127.0.0.1:5173/>.

```bash
npm install
uv run python demo.py
npm run test:e2e
```

## Documentation

The documentation site is in `docs/` (VitePress, published on docs.velis.si):

```bash
npm install
npm run docs:dev
```

## Licence

MIT, see [LICENSE](LICENSE).
