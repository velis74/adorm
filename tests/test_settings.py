import pytest

from tests.djangoproject.settings import database_from_url, TEST_DATABASE_NAME


def test_full_url():
    db = database_from_url("postgres://user:p%40ss@db.example:6543/app")
    assert db == {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "app",
        "USER": "user",
        "PASSWORD": "p@ss",
        "HOST": "db.example",
        "PORT": "6543",
        "TEST": {"NAME": TEST_DATABASE_NAME},
    }


def test_minimal_url_defaults():
    db = database_from_url("postgresql://localhost")
    assert db["NAME"] == "postgres"
    assert db["USER"] == ""
    assert db["PASSWORD"] == ""
    assert db["PORT"] == ""


def test_unsupported_scheme():
    with pytest.raises(ValueError, match="mysql"):
        database_from_url("mysql://localhost/app")


def test_query_parameters():
    db = database_from_url("postgres://user@/app?host=/var/run/postgresql&port=5433&sslmode=require")
    assert db["HOST"] == "/var/run/postgresql"
    assert db["PORT"] == "5433"
    assert db["OPTIONS"] == {"sslmode": "require"}


def test_authority_host_wins_over_query_host():
    db = database_from_url("postgres://db.example/app?host=/tmp")
    assert db["HOST"] == "db.example"
    assert "OPTIONS" not in db
