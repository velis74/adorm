"""The demo database's checks: the URL, the role and the owner."""

import psycopg
import pytest

from demo.database import check_url, DatabaseSetupError, drop_database, ensure_database, role_problem, with_database
from tests.djangoproject.settings import DEMO_OWNER_DATABASE, TEST_DATABASE_URL


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("", "is not set"),
        ("sqlite:///demo.sqlite3", "is a postgres:// URL"),
        ("postgres://127.0.0.1/adorm_demo", "names no user"),
        ("postgres://adorm@127.0.0.1/", "names no demo database"),
        ("postgres://adorm@127.0.0.1/postgres", "names no demo database"),
    ],
)
def test_check_url_refuses(url, message):
    with pytest.raises(DatabaseSetupError, match=message):
        check_url(url)


def test_role_problem():
    assert role_problem("adorm", False, "adorm_demo", None) is None
    assert role_problem("adorm", False, "adorm_demo", "adorm") is None
    assert "is a superuser" in role_problem("postgres", True, "adorm_demo", None)
    assert "is a superuser" in role_problem("postgres", True, "adorm_demo", "postgres")
    assert (
        role_problem("adorm", False, "adorm_demo", "postgres")
        == "database adorm_demo is owned by postgres, not by adorm"
    )


@pytest.mark.usefixtures("demo_role")
def test_ensure_database_creates_it_owned_by_the_user():
    url = with_database(TEST_DATABASE_URL, DEMO_OWNER_DATABASE)
    drop_database(url)
    try:
        assert ensure_database(url) is True
        assert ensure_database(url) is False
        with psycopg.connect(with_database(TEST_DATABASE_URL, "postgres")) as conn:
            owner, user = conn.execute(
                "SELECT pg_get_userbyid(datdba), current_user FROM pg_database WHERE datname = %s",
                (DEMO_OWNER_DATABASE,),
            ).fetchone()
        assert owner == user
    finally:
        drop_database(url)
