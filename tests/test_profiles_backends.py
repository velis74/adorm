"""Connection profiles on real backends, on the Django ORM (threaded) and asyncpg (async): the recipe for several
database users over one login. ``on_acquire`` puts the user on the connection and ``on_release`` takes it off. The
user is the custom setting ``adorm_test.app_user``, and a table under row level security shows only that user's rows.
The tests that switch users with ``SET ROLE`` create roles and run only when ``ADORM_TEST_CREATE_ROLES=1``."""

import asyncio
import contextvars
import os
import secrets

from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest

from adorm import ConnectionProfile, create_session, PoolSettings, ProgrammingError
from adorm.executor import Router
from adorm.settings import ProfileRegistry
from tests.contract.drivers import AsyncpgDriver, DjangoDriver, quote as quote_name, RawQuery

SETTING = "adorm_test.app_user"
TABLE = "profile_note"
NOTES = RawQuery(f"SELECT owner, note FROM {TABLE} ORDER BY note")
ALICE_NOTES = [{"owner": "alice", "note": "for alice"}]

current_user: contextvars.ContextVar[str | None] = contextvars.ContextVar("test_current_user", default=None)


@dataclass(frozen=True)
class Identity:
    """How the hooks put a user on a connection, take it off and read it."""

    read: str
    set: Callable[[str], str]
    reset: str


# NULL before the first SET on a connection, '' after a RESET: both mean no user.
BY_SETTING = Identity(
    read=f"SELECT nullif(current_setting('{SETTING}', true), '')",
    set=lambda user: f"SET {SETTING} = '{user}'",
    reset=f"RESET {SETTING}",
)
WHO = RawQuery(f"SELECT nullif(current_setting('{SETTING}', true), '') AS who")


class Seen:
    """Records the user each ``on_acquire`` finds on the connection before it sets one."""

    def __init__(self):
        self.before: list[str | None] = []


def profile_user(profile):
    return profile.params.get("APP_USER") or profile.params.get("app_user")


def django_hooks(seen, identity, user_of=profile_user):
    def on_acquire(connection, profile, session):  # noqa: ARG001
        with connection.cursor() as cursor:
            cursor.execute(identity.read)
            seen.before.append(cursor.fetchone()[0])
            user = user_of(profile)
            if user is not None:
                cursor.execute(identity.set(user))

    def on_release(connection, profile, session):  # noqa: ARG001
        with connection.cursor() as cursor:
            cursor.execute(identity.reset)

    return on_acquire, on_release


def asyncpg_hooks(seen, identity, user_of=profile_user):
    async def on_acquire(connection, profile, session):  # noqa: ARG001
        seen.before.append(await connection.fetchval(identity.read))
        user = user_of(profile)
        if user is not None:
            await connection.execute(identity.set(user))

    async def on_release(connection, profile, session):  # noqa: ARG001
        await connection.execute(identity.reset)

    return on_acquire, on_release


def per_user_router(kind, seen, identity, database_url, login=None):
    """A router whose provider creates the profile ``user:<name>`` at run time, each slot logged in as ``login``
    (``(user, password)``; the test database user when ``None``)."""
    registry = ProfileRegistry(pool=PoolSettings(max_connections=1, acquire_timeout=10))

    def provider(name):
        if not name.startswith("user:"):
            return None
        user = name.removeprefix("user:")
        if kind == "django":
            on_acquire, on_release = django_hooks(seen, identity)
            params = {"APP_USER": user}
            if login is not None:
                params |= {"USER": login[0], "PASSWORD": login[1]}
            return ConnectionProfile(name, DjangoDriver(), params=params, on_acquire=on_acquire, on_release=on_release)
        on_acquire, on_release = asyncpg_hooks(seen, identity)
        dsn = database_url if login is None else login_url(database_url, *login)
        params = {"dsn": dsn, "app_user": user}
        return ConnectionProfile(name, AsyncpgDriver(), params=params, on_acquire=on_acquire, on_release=on_release)

    registry.add_provider(provider)
    return Router(profiles=registry)


def login_url(url, user, password):
    parts = urlsplit(url)
    return urlunsplit(parts._replace(netloc=f"{user}:{quote(password)}@{parts.hostname}:{parts.port or 5432}"))


@pytest.fixture
async def notes(test_database_url):
    """A table of notes under row level security, forced for its owner too: a user sees and adds only notes it owns."""
    conn = await asyncpg.connect(test_database_url)
    await conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
    await conn.execute(f"CREATE TABLE {TABLE} (owner text, note text)")
    await conn.execute(f"INSERT INTO {TABLE} VALUES ('alice', 'for alice'), ('bob', 'for bob')")
    await conn.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    await conn.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    await conn.execute(f"CREATE POLICY own_notes ON {TABLE} USING (owner = current_setting('{SETTING}', true))")
    try:
        yield
    finally:
        await conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
        await conn.close()


@pytest.fixture(params=["django", "asyncpg"])
async def per_user(request, notes, test_database_url, django_db_blocker):  # noqa: ARG001
    seen = Seen()
    router = per_user_router(request.param, seen, BY_SETTING, test_database_url)
    with django_db_blocker.unblock():
        try:
            yield router, seen
        finally:
            await router.close()


async def test_each_user_profile_runs_as_its_user(per_user):
    router, _ = per_user
    assert await router.run(create_session("user:alice", "web"), WHO) == [{"who": "alice"}]
    assert await router.run(create_session("user:bob", "web"), WHO) == [{"who": "bob"}]


async def test_row_level_security_follows_the_user(per_user):
    router, _ = per_user
    assert await router.run(create_session("user:alice", "web"), NOTES) == ALICE_NOTES
    assert await router.run(create_session("user:bob", "web"), NOTES) == [{"owner": "bob", "note": "for bob"}]
    with pytest.raises(ProgrammingError) as info:
        await router.run(create_session("user:bob", "web"), RawQuery(f"INSERT INTO {TABLE} VALUES ('alice', 'x')"))
    assert info.value.sqlstate == "42501"


async def test_on_release_resets_the_user_before_the_slot_is_reused(per_user):
    router, seen = per_user
    for name in ("first", "second", "third"):
        assert await router.run(create_session("user:alice", name), WHO) == [{"who": "alice"}]
    pool = router.pool("user:alice")
    assert pool.size == 1
    assert seen.before == [None, None, None]


async def test_the_user_holds_for_a_whole_transaction(per_user):
    router, _ = per_user
    async with router.slot(create_session("user:alice", "tx")) as lease:
        await lease.call("begin")
        assert await lease.call("execute", WHO) == [{"who": "alice"}]
        assert await lease.call("execute", NOTES) == ALICE_NOTES
        assert await lease.call("execute", WHO) == [{"who": "alice"}]
        await lease.call("commit")


async def test_concurrent_users_never_see_each_others_user(per_user):
    router, _ = per_user

    async def check(user, i):
        who = await router.run(create_session(f"user:{user}", f"s{i}"), WHO)
        notes = await router.run(create_session(f"user:{user}", f"s{i}"), NOTES)
        return user, who[0]["who"], {row["owner"] for row in notes}

    results = await asyncio.gather(*(check(user, i) for i in range(6) for user in ("alice", "bob")))
    assert all(who == user and owners == {user} for user, who, owners in results)


# One shared profile: the user comes from the acquiring task's context


@pytest.fixture(params=["django", "asyncpg"])
async def shared(request, notes, test_database_url, django_db_blocker):  # noqa: ARG001
    """One profile and one pool of a single slot; ``on_acquire`` sets the user in ``current_user``. The profile's
    ``connect`` opens the connection itself."""
    seen = Seen()

    def user_of(profile):  # noqa: ARG001
        return current_user.get()

    if request.param == "django":
        on_acquire, on_release = django_hooks(seen, BY_SETTING, user_of)

        def connect(profile):
            from django.db import connections

            wrapper = connections[profile.alias]
            return type(wrapper).get_new_connection(wrapper, wrapper.get_connection_params())

        driver = DjangoDriver()
    else:
        on_acquire, on_release = asyncpg_hooks(seen, BY_SETTING, user_of)

        async def connect(profile):  # noqa: ARG001
            return await asyncpg.connect(test_database_url)

        driver = AsyncpgDriver()
    registry = ProfileRegistry(pool=PoolSettings(max_connections=1, acquire_timeout=10))
    registry.register(ConnectionProfile("app", driver, connect=connect, on_acquire=on_acquire, on_release=on_release))
    router = Router(profiles=registry)
    with django_db_blocker.unblock():
        try:
            yield router, seen
        finally:
            await router.close()


async def as_user(router, user, query):
    token = current_user.set(user)
    try:
        return await router.run(create_session("app", f"request-{user}"), query)
    finally:
        current_user.reset(token)


async def test_users_alternating_on_one_shared_slot_each_get_their_own_user(shared):
    router, seen = shared
    for user in ("alice", "bob", "alice", "bob"):
        assert await as_user(router, user, WHO) == [{"who": user}]
    assert router.pool("app").size == 1
    assert seen.before == [None] * 4


async def test_bob_never_gets_alices_rows_on_her_slot(shared):
    router, seen = shared
    assert await as_user(router, "alice", NOTES) == ALICE_NOTES
    assert await as_user(router, "bob", NOTES) == [{"owner": "bob", "note": "for bob"}]
    assert await as_user(router, None, WHO) == [{"who": None}]
    assert await as_user(router, None, NOTES) == []
    assert router.pool("app").size == 1
    assert seen.before == [None] * 4


async def test_concurrent_users_on_a_shared_pool_never_see_each_others_user(shared):
    router, seen = shared

    async def check(user):
        rows = await as_user(router, user, WHO)
        return user, rows[0]["who"]

    results = await asyncio.gather(*(check(user) for _ in range(5) for user in ("alice", "bob")))
    assert all(who == user for user, who in results)
    assert seen.before == [None] * 10


# Database roles: SET ROLE over one login role. Roles belong to the whole cluster, so these tests run only when
# ADORM_TEST_CREATE_ROLES=1 (and the test database user has CREATEROLE).

LOGIN = "adorm_test_login"
ROLE_PREFIX = "adorm_test_"
USERS = (f"{ROLE_PREFIX}alice", f"{ROLE_PREFIX}bob")
GRANTED = "profile_granted"

BY_ROLE = Identity(
    read="SELECT current_user",
    set=lambda user: f"SET ROLE {quote_name(ROLE_PREFIX + user)}",
    reset="RESET ROLE",
)
ROLE_WHO = RawQuery("SELECT current_user AS who, session_user AS login")


@pytest.fixture
async def roles(test_database_url):
    """A login role that may become every user role; only alice may read the table. Yields the login password."""
    if os.environ.get("ADORM_TEST_CREATE_ROLES") != "1":
        pytest.skip("creates database roles, which outlive the test databases; set ADORM_TEST_CREATE_ROLES=1")
    password = secrets.token_hex(16)
    conn = await asyncpg.connect(test_database_url)

    async def drop():
        await conn.execute(f"DROP TABLE IF EXISTS {GRANTED}")
        for role in (LOGIN, *USERS):
            await conn.execute(f"DROP ROLE IF EXISTS {role}")

    await drop()
    for role in USERS:
        await conn.execute(f"CREATE ROLE {role} NOLOGIN")
    await conn.execute(f"CREATE ROLE {LOGIN} LOGIN PASSWORD '{password}' NOINHERIT IN ROLE {', '.join(USERS)}")
    await conn.execute(f"CREATE TABLE {GRANTED} (note text)")
    await conn.execute(f"INSERT INTO {GRANTED} VALUES ('for alice')")
    await conn.execute(f"GRANT SELECT ON {GRANTED} TO {USERS[0]}")
    try:
        yield password
    finally:
        await drop()
        await conn.close()


@pytest.fixture(params=["django", "asyncpg"])
async def per_role(request, roles, test_database_url, django_db_blocker):
    seen = Seen()
    router = per_user_router(request.param, seen, BY_ROLE, test_database_url, login=(LOGIN, roles))
    with django_db_blocker.unblock():
        try:
            yield router, seen
        finally:
            await router.close()


async def test_each_user_profile_runs_as_its_role_over_one_login(per_role):
    router, seen = per_role
    for _ in range(2):
        assert await router.run(create_session("user:alice", "web"), ROLE_WHO) == [{"who": USERS[0], "login": LOGIN}]
        assert await router.run(create_session("user:bob", "web"), ROLE_WHO) == [{"who": USERS[1], "login": LOGIN}]
    assert seen.before == [LOGIN] * 4


async def test_privileges_follow_the_role(per_role):
    router, _ = per_role
    secret = RawQuery(f"SELECT note FROM {GRANTED}")
    assert await router.run(create_session("user:alice", "web"), secret) == [{"note": "for alice"}]
    with pytest.raises(ProgrammingError) as info:
        await router.run(create_session("user:bob", "web"), secret)
    assert info.value.sqlstate == "42501"


async def test_reset_all_keeps_the_role_and_reset_role_restores_the_login(roles, test_database_url):
    conn = await asyncpg.connect(login_url(test_database_url, LOGIN, roles))
    try:
        await conn.execute(f"SET ROLE {USERS[0]}")
        await conn.execute("RESET ALL")
        assert await conn.fetchval("SELECT current_user") == USERS[0]
        await conn.execute("RESET ROLE")
        assert await conn.fetchval("SELECT current_user") == LOGIN
    finally:
        await conn.close()
