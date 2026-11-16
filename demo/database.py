"""The demo database: PostgreSQL or SQLite URLs, the Django ``DATABASES`` entry and creating the database.

A SQLite URL is ``sqlite:///relative/path`` or ``sqlite:////absolute/path``; the database file is created by
``manage.py migrate``.
"""

from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

DEFAULT_SERVER_URL = "postgres://postgres@127.0.0.1:5432/postgres"
DEFAULT_DATABASE_NAME = "adorm_demo"


def with_database(url: str, name: str) -> str:
    """``url`` with its database replaced by ``name``."""
    return urlunsplit(urlsplit(url)._replace(path="/" + quote(name)))


def database_name(url: str) -> str:
    """The database a ``postgres://`` URL names."""
    return unquote(urlsplit(url).path.lstrip("/")) or "postgres"


def is_sqlite(url: str) -> bool:
    """Whether ``url`` is a ``sqlite://`` URL."""
    return urlsplit(url).scheme == "sqlite"


def sqlite_path(url: str) -> Path:
    """The database file a ``sqlite:///path`` URL names."""
    return Path(unquote(urlsplit(url).path.removeprefix("/")))


def django_database(url: str) -> dict:
    """A Django ``DATABASES`` entry for a ``postgres://``, ``postgresql://`` or ``sqlite://`` URL. A SQLite write waits
    up to 20 seconds for the write lock."""
    if is_sqlite(url):
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": str(sqlite_path(url)), "OPTIONS": {"timeout": 20}}
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
    """Create the database ``url`` names when the server lacks it (for SQLite: the file's directory). Returns whether
    the database was missing."""
    if is_sqlite(url):
        path = sqlite_path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        return not path.exists()
    import psycopg

    from psycopg import sql

    name = database_name(url)
    with psycopg.connect(with_database(url, "postgres"), autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            return False
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return True


def drop_database(url: str) -> None:
    """Drop the database ``url`` names, closing its other connections (for SQLite: remove its files)."""
    if is_sqlite(url):
        path = sqlite_path(url)
        for suffix in ("", "-wal", "-shm"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)
        return
    import psycopg

    from psycopg import sql

    with psycopg.connect(with_database(url, "postgres"), autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database_name(url))))
