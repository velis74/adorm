# adorm

adorm (Async Django ORM, pronounced "adore") is an asynchronous ORM interface for Python. Applications build
queries and model operations with adorm's own API; a driver executes them on a target ORM or database. The
first drivers target the Django ORM and PostgreSQL directly.

The package is pre-release and has no public API yet.

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
`postgres://postgres@127.0.0.1:5432/postgres`). They create and drop their own `adorm_test` database on that
server and leave other databases untouched.

```bash
ADORM_TEST_DATABASE_URL=postgres://user:password@127.0.0.1:5432/postgres uv run pytest
```

## Licence

MIT, see [LICENSE](LICENSE).
