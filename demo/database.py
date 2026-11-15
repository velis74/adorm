"""The demo database: PostgreSQL URLs, the Django ``DATABASES`` entry and creating the database."""

from urllib.parse import quote, unquote, urlsplit, urlunsplit

DEFAULT_SERVER_URL = "postgres://postgres@127.0.0.1:5432/postgres"
DEFAULT_DATABASE_NAME = "adorm_demo"


def with_database(url: str, name: str) -> str:
    """``url`` with its database replaced by ``name``."""
    return urlunsplit(urlsplit(url)._replace(path="/" + quote(name)))


def database_name(url: str) -> str:
    """The database a ``postgres://`` URL names."""
    return unquote(urlsplit(url).path.lstrip("/")) or "postgres"


def django_database(url: str) -> dict:
    """A Django ``DATABASES`` entry for a ``postgres://`` or ``postgresql://`` URL."""
    parts = urlsplit(url)
    if parts.scheme not in ("postgres", "postgresql"):
        raise ValueError(f"unsupported database URL scheme: {parts.scheme!r}")
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": database_name(url),
        "USER": unquote(parts.username or ""),
        "PASSWORD": unquote(parts.password or ""),
        "HOST": parts.hostname or "",
        "PORT": str(parts.port or ""),
    }


def ensure_database(url: str) -> bool:
    """Create the database ``url`` names when the server lacks it. Returns whether it was created."""
    import psycopg

    from psycopg import sql

    name = database_name(url)
    with psycopg.connect(with_database(url, "postgres"), autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            return False
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return True


def drop_database(url: str) -> None:
    """Drop the database ``url`` names, closing its other connections."""
    import psycopg

    from psycopg import sql

    with psycopg.connect(with_database(url, "postgres"), autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database_name(url))))
