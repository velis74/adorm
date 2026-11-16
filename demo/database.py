"""The demo database: PostgreSQL URLs, the Django ``DATABASES`` entry, and creating and dropping the database.

The demo runs as the dedicated user ``ADORM_DEMO_DATABASE_URL`` names, which owns the demo database. Setup refuses a
superuser and a database another role owns.
"""

from urllib.parse import quote, unquote, urlsplit, urlunsplit

DATABASE_ENV = "ADORM_DEMO_DATABASE_URL"
MAINTENANCE_DATABASE = "postgres"


class DatabaseSetupError(RuntimeError):
    """The demo database cannot be set up as the demo requires."""


def with_database(url: str, name: str) -> str:
    """``url`` with its database replaced by ``name``."""
    return urlunsplit(urlsplit(url)._replace(path="/" + quote(name)))


def database_name(url: str) -> str:
    """The database a ``postgres://`` URL names; empty when it names none."""
    return unquote(urlsplit(url).path.lstrip("/"))


def check_url(url: str) -> None:
    """Raise `DatabaseSetupError` unless ``url`` is a ``postgres://`` or ``postgresql://`` URL naming a user and a
    database other than the maintenance database."""
    if not url:
        raise DatabaseSetupError(f"{DATABASE_ENV} is not set; .env.example shows the form")
    parts = urlsplit(url)
    if parts.scheme not in ("postgres", "postgresql"):
        raise DatabaseSetupError(f"{DATABASE_ENV} is a postgres:// URL, not {parts.scheme}://")
    if not parts.username:
        raise DatabaseSetupError(f"{DATABASE_ENV} names no user")
    if database_name(url) in ("", MAINTENANCE_DATABASE, "template0", "template1"):
        raise DatabaseSetupError(f"{DATABASE_ENV} names no demo database")


def django_database(url: str) -> dict:
    """A Django ``DATABASES`` entry for a ``postgres://`` URL; an empty ``NAME`` for an empty URL."""
    parts = urlsplit(url)
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": database_name(url),
        "USER": unquote(parts.username or ""),
        "PASSWORD": unquote(parts.password or ""),
        "HOST": parts.hostname or "",
        "PORT": str(parts.port or ""),
    }


def role_problem(user: str, superuser: bool, database: str, owner: str | None) -> str | None:
    """Why ``user`` cannot run the demo on ``database`` (``owner`` is its owner, ``None`` when it does not exist);
    ``None`` when it can."""
    if superuser:
        return f"{user} is a superuser; the demo runs as a dedicated user with CREATEDB only"
    if owner is not None and owner != user:
        return f"database {database} is owned by {owner}, not by {user}"
    return None


def _inspect(conn, name: str) -> tuple[str, bool, str | None]:
    user, superuser = conn.execute(
        "SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    row = conn.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s", (name,)).fetchone()
    return user, superuser, None if row is None else row[0]


def ensure_database(url: str) -> bool:
    """Create the database ``url`` names, owned by the user it names, when the server lacks it. Returns whether it was
    created; raises `DatabaseSetupError` for a superuser or a database another role owns."""
    import psycopg

    from psycopg import sql

    check_url(url)
    name = database_name(url)
    with psycopg.connect(with_database(url, MAINTENANCE_DATABASE), autocommit=True) as conn:
        user, superuser, owner = _inspect(conn, name)
        problem = role_problem(user, superuser, name, owner)
        if problem is not None:
            raise DatabaseSetupError(problem)
        if owner is not None:
            return False
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return True


def drop_database(url: str) -> None:
    """Drop the database ``url`` names, closing its other connections; raises `DatabaseSetupError` as
    `ensure_database()` does."""
    import psycopg

    from psycopg import sql

    check_url(url)
    name = database_name(url)
    with psycopg.connect(with_database(url, MAINTENANCE_DATABASE), autocommit=True) as conn:
        user, superuser, owner = _inspect(conn, name)
        problem = role_problem(user, superuser, name, owner)
        if problem is not None:
            raise DatabaseSetupError(problem)
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
