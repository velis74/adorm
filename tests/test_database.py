import asyncpg
import pytest

from django.db import connection

from tests.djangoproject.settings import TEST_DATABASE_NAME


@pytest.mark.django_db
def test_django_uses_test_database():
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database()")
        assert cursor.fetchone()[0].startswith(TEST_DATABASE_NAME)


@pytest.mark.django_db
def test_django_migrations_applied():
    assert "auth_user" in connection.introspection.table_names()


async def test_asyncpg_reaches_test_database(test_database_url):
    conn = await asyncpg.connect(test_database_url)
    try:
        assert (await conn.fetchval("SELECT current_database()")).startswith(TEST_DATABASE_NAME)
    finally:
        await conn.close()
