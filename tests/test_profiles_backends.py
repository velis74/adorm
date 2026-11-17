"""Connection profiles on real backends, on the Django ORM (threaded) and asyncpg (async): the recipe for several
database users over one login. The user reaches the connection as a connection option: the profile's ``options``
or the options its ``on_acquire`` hook returns. The user is the custom option ``adorm_test.app_user``, and a table
under row level security shows only that user's rows. The tests that switch users with the ``role`` option create
roles and run only when ``ADORM_TEST_CREATE_ROLES=1``."""

import asyncio
import contextvars
import os
import secrets

from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest

import adorm

from adorm import ConnectionProfile, create_session, DatabaseError, PoolSettings, ProgrammingError
from adorm.executor import Router
from adorm.executor.options import resolve_options
from adorm.settings import ProfileRegistry
from tests.contract.drivers import AsyncpgDriver, DjangoDriver, RawQuery

SETTING = "adorm_test.app_user"
TABLE = "profile_note"
NOTES = RawQuery(f"SELECT owner, note FROM {TABLE} ORDER BY note")
ALICE_NOTES = [{"owner": "alice", "note": "for alice"}]
REPEAT = range(3)

current_user: contextvars.ContextVar[str | None] = contextvars.ContextVar("test_current_user", default=None)


@dataclass(frozen=True)
class Identity:
    """How a user reaches the connection: the connection option and its value for a user."""

    option: str
    value: Callable[[str], str]

    def options(self, user: str | None) -> dict[str, str] | None:
        return None if user is None else {self.option: self.value(user)}


BY_SETTING = Identity(option=SETTING, value=lambda user: user)
# NULL before the option is first set on a connection, '' after a reset: both mean no user.
WHO = RawQuery(f"SELECT nullif(current_setting('{SETTING}', true), '') AS who")


def per_user_router(kind, identity, database_url, login=None):
    """A router whose provider creates the profile ``user:<name>`` at run time with the user in its ``options``, each
    slot logged in as ``login`` (``(user, password)``; the test database user when ``None``)."""
    registry = ProfileRegistry(pool=PoolSettings(max_connections=1, acquire_timeout=10))

    def provider(name):
        if not name.startswith("user:"):
            return None
        options = identity.options(name.removeprefix("user:"))
        if kind == "django":
            params = {} if login is None else {"USER": login[0], "PASSWORD": login[1]}
            return ConnectionProfile(name, DjangoDriver(), params=params, options=options)
        dsn = database_url if login is None else login_url(database_url, *login)
        return ConnectionProfile(name, AsyncpgDriver(), params={"dsn": dsn}, options=options)

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
    router = per_user_router(request.param, BY_SETTING, test_database_url)
    with django_db_blocker.unblock():
        try:
            yield router
        finally:
            await router.close()


async def test_each_user_profile_runs_as_its_user(per_user):
    for _ in REPEAT:
        assert await per_user.run(create_session("user:alice", "web"), WHO) == [{"who": "alice"}]
        assert await per_user.run(create_session("user:bob", "web"), WHO) == [{"who": "bob"}]


async def test_row_level_security_follows_the_user(per_user):
    assert await per_user.run(create_session("user:alice", "web"), NOTES) == ALICE_NOTES
    assert await per_user.run(create_session("user:bob", "web"), NOTES) == [{"owner": "bob", "note": "for bob"}]
    with pytest.raises(ProgrammingError) as info:
        await per_user.run(create_session("user:bob", "web"), RawQuery(f"INSERT INTO {TABLE} VALUES ('alice', 'x')"))
    assert info.value.sqlstate == "42501"


async def test_the_user_holds_for_a_whole_transaction_and_a_block_restores_it(per_user):
    session = create_session("user:alice", "tx")
    pool = await per_user.get_pool("user:alice")
    for _ in REPEAT:
        async with per_user.slot(session) as lease:
            options = resolve_options(session, pool)
            await lease.call("begin", options=options.for_lease(lease))
            assert await lease.call("execute", WHO, options=options.for_lease(lease)) == [{"who": "alice"}]
            with adorm.connection_options(**{SETTING: "bob"}):
                inner = resolve_options(session, pool)
                assert await lease.call("execute", WHO, options=inner.for_lease(lease)) == [{"who": "bob"}]
            assert await lease.call("execute", NOTES, options=options.for_lease(lease)) == ALICE_NOTES
            await lease.call("commit", options=options.for_lease(lease))
    assert pool.size == 1


async def test_concurrent_users_never_see_each_others_user(per_user):
    async def check(user, i):
        who = await per_user.run(create_session(f"user:{user}", f"s{i}"), WHO)
        notes = await per_user.run(create_session(f"user:{user}", f"s{i}"), NOTES)
        return user, who[0]["who"], {row["owner"] for row in notes}

    results = await asyncio.gather(*(check(user, i) for i in range(6) for user in ("alice", "bob")))
    assert all(who == user and owners == {user} for user, who, owners in results)


@pytest.mark.parametrize("kind", ["django", "asyncpg"])
async def test_a_custom_option_whose_name_has_sql_keywords_is_set_and_reset(kind, test_database_url, django_db_blocker):
    option = "adorm_test.user"
    router = per_user_router(kind, Identity(option=option, value=lambda user: user), test_database_url)
    who = RawQuery(f"SELECT nullif(current_setting('{option}', true), '') AS who")
    session = create_session("user:alice", "web")
    with django_db_blocker.unblock():
        try:
            for _ in REPEAT:
                assert await router.run(session, who) == [{"who": "alice"}]
                with adorm.connection_options(**{option: "bob"}):
                    assert await router.run(session, who) == [{"who": "bob"}]
                with adorm.connection_options(**{option: None}):
                    assert await router.run(session, who) == [{"who": None}]
                assert await router.run(session, who) == [{"who": "alice"}]
        finally:
            await router.close()


# One shared profile: on_acquire returns the user of the acquiring task's context


@pytest.fixture(params=["django", "asyncpg"])
async def shared(request, notes, test_database_url, django_db_blocker):  # noqa: ARG001
    """One profile and one pool of a single slot; ``on_acquire`` returns the user in ``current_user``. The profile's
    ``connect`` opens the connection itself."""
    acquired = []

    def on_acquire(connection, profile, session):  # noqa: ARG001
        acquired.append(current_user.get())
        return BY_SETTING.options(current_user.get())

    if request.param == "django":

        def connect(profile):
            from django.db import connections

            wrapper = connections[profile.alias]
            return type(wrapper).get_new_connection(wrapper, wrapper.get_connection_params())

        driver = DjangoDriver()
    else:

        async def connect(profile):  # noqa: ARG001
            return await asyncpg.connect(test_database_url)

        driver = AsyncpgDriver()
    registry = ProfileRegistry(pool=PoolSettings(max_connections=1, acquire_timeout=10))
    registry.register(ConnectionProfile("app", driver, connect=connect, on_acquire=on_acquire))
    router = Router(profiles=registry)
    with django_db_blocker.unblock():
        try:
            yield router, acquired
        finally:
            await router.close()


async def as_user(router, user, query):
    token = current_user.set(user)
    try:
        return await router.run(create_session("app", f"request-{user}"), query)
    finally:
        current_user.reset(token)


async def test_users_alternating_on_one_shared_slot_each_get_their_own_user(shared):
    router, acquired = shared
    for user in ("alice", "bob", "alice", "bob"):
        assert await as_user(router, user, WHO) == [{"who": user}]
    assert router.pool("app").size == 1
    assert acquired == ["alice", "bob", "alice", "bob"]


async def test_bob_never_gets_alices_rows_on_her_slot(shared):
    router, _ = shared
    for _ in REPEAT:
        assert await as_user(router, "alice", NOTES) == ALICE_NOTES
        assert await as_user(router, "bob", NOTES) == [{"owner": "bob", "note": "for bob"}]
        assert await as_user(router, None, WHO) == [{"who": None}]
        assert await as_user(router, None, NOTES) == []
    assert router.pool("app").size == 1


async def test_concurrent_users_on_a_shared_pool_never_see_each_others_user(shared):
    router, acquired = shared

    async def check(user):
        rows = await as_user(router, user, WHO)
        return user, rows[0]["who"]

    users = ("alice", "bob", None)
    results = await asyncio.gather(*(check(user) for _ in range(5) for user in users))
    assert all(who == user for user, who in results)
    assert len(acquired) == 5 * len(users)
    assert router.pool("app").size == 1


@pytest.mark.parametrize("kind", ["django", "asyncpg"])
async def test_an_option_the_database_refuses_fails_the_acquisition_and_discards_the_slot(
    kind,
    notes,  # noqa: ARG001
    test_database_url,
    django_db_blocker,  # noqa: ARG001
):
    """A role that does not exist: setting it needs no role to be created."""

    def on_acquire(connection, profile, session):  # noqa: ARG001
        user = current_user.get()
        return {"role": "adorm_test_no_such_role"} if user == "nobody" else BY_SETTING.options(user)

    if kind == "django":
        profile = ConnectionProfile("app", DjangoDriver(), on_acquire=on_acquire)
    else:
        profile = ConnectionProfile("app", AsyncpgDriver(), params={"dsn": test_database_url}, on_acquire=on_acquire)
    registry = ProfileRegistry(pool=PoolSettings(max_connections=1, acquire_timeout=10))
    registry.register(profile)
    router = Router(profiles=registry)
    with django_db_blocker.unblock():
        try:
            for _ in REPEAT:
                assert await as_user(router, "alice", WHO) == [{"who": "alice"}]
                pid = await as_user(router, "alice", RawQuery("SELECT pg_backend_pid() AS pid"))
                with pytest.raises(DatabaseError, match="adorm_test_no_such_role"):
                    await as_user(router, "nobody", WHO)
                assert router.pool("app").size == 0
                assert await as_user(router, "bob", WHO) == [{"who": "bob"}]
                assert await as_user(router, "bob", RawQuery("SELECT pg_backend_pid() AS pid")) != pid
        finally:
            await router.close()


# Database roles over one login role. Roles belong to the whole cluster, so these tests run only when
# ADORM_TEST_CREATE_ROLES=1 (and the test database user has CREATEROLE).

LOGIN = "adorm_test_login"
ROLE_PREFIX = "adorm_test_"
USERS = (f"{ROLE_PREFIX}alice", f"{ROLE_PREFIX}bob")
GRANTED = "profile_granted"

BY_ROLE = Identity(option="role", value=lambda user: ROLE_PREFIX + user)
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
    router = per_user_router(request.param, BY_ROLE, test_database_url, login=(LOGIN, roles))
    with django_db_blocker.unblock():
        try:
            yield router
        finally:
            await router.close()


async def test_each_user_profile_runs_as_its_role_over_one_login(per_role):
    for _ in REPEAT:
        assert await per_role.run(create_session("user:alice", "web"), ROLE_WHO) == [{"who": USERS[0], "login": LOGIN}]
        assert await per_role.run(create_session("user:bob", "web"), ROLE_WHO) == [{"who": USERS[1], "login": LOGIN}]


async def test_privileges_follow_the_role(per_role):
    secret = RawQuery(f"SELECT note FROM {GRANTED}")
    assert await per_role.run(create_session("user:alice", "web"), secret) == [{"note": "for alice"}]
    with pytest.raises(ProgrammingError) as info:
        await per_role.run(create_session("user:bob", "web"), secret)
    assert info.value.sqlstate == "42501"


async def test_a_block_switches_the_role_and_restores_the_profiles(per_role):
    session = create_session("user:alice", "web")
    for _ in REPEAT:
        with adorm.connection_options(role=USERS[1]):
            assert await per_role.run(session, ROLE_WHO) == [{"who": USERS[1], "login": LOGIN}]
        with adorm.connection_options(role=None):
            assert await per_role.run(session, ROLE_WHO) == [{"who": LOGIN, "login": LOGIN}]
        assert await per_role.run(session, ROLE_WHO) == [{"who": USERS[0], "login": LOGIN}]
    assert per_role.pool("user:alice").size == 1


@pytest.fixture(params=["django", "asyncpg"])
async def shared_login(request, roles, test_database_url, django_db_blocker):
    """One profile logged in as the login role; ``on_acquire`` returns the role of the user in ``current_user``."""

    def on_acquire(connection, profile, session):  # noqa: ARG001
        return BY_ROLE.options(current_user.get())

    login = (LOGIN, roles)
    if request.param == "django":
        profile = ConnectionProfile(
            "app", DjangoDriver(), params={"USER": login[0], "PASSWORD": login[1]}, on_acquire=on_acquire
        )
    else:
        dsn = login_url(test_database_url, *login)
        profile = ConnectionProfile("app", AsyncpgDriver(), params={"dsn": dsn}, on_acquire=on_acquire)
    registry = ProfileRegistry(pool=PoolSettings(max_connections=1, acquire_timeout=10))
    registry.register(profile)
    router = Router(profiles=registry)
    with django_db_blocker.unblock():
        try:
            yield router
        finally:
            await router.close()


async def test_the_hooks_role_holds_for_the_lease_and_a_lease_without_one_runs_as_the_login(shared_login):
    secret = RawQuery(f"SELECT note FROM {GRANTED}")
    for _ in REPEAT:
        for user, role in (("alice", USERS[0]), ("bob", USERS[1]), (None, LOGIN), ("alice", USERS[0])):
            assert await as_user(shared_login, user, ROLE_WHO) == [{"who": role, "login": LOGIN}]
        assert await as_user(shared_login, "alice", secret) == [{"note": "for alice"}]
        with pytest.raises(ProgrammingError):
            await as_user(shared_login, "bob", secret)
    assert shared_login.pool("app").size == 1


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
