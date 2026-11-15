"""Connection profiles on real backends: several database users through one login role, ``SET ROLE`` in
``on_acquire`` and ``RESET ROLE`` in ``on_release``, on the Django ORM (threaded) and asyncpg (async)."""

import asyncio
import contextvars
import secrets

from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest

from adorm import ConnectionProfile, create_session, PoolSettings, ProgrammingError
from adorm.executor import Router
from adorm.settings import ProfileRegistry
from tests.contract.drivers import AsyncpgDriver, DjangoDriver, quote as quote_name, RawQuery

LOGIN = "adorm_test_login"
USERS = ("adorm_test_alice", "adorm_test_bob")
TABLE = "profile_secret"
WHO = RawQuery("SELECT current_user AS who, session_user AS login")
SECRET = RawQuery(f"SELECT note FROM {TABLE}")

current_role: contextvars.ContextVar[str | None] = contextvars.ContextVar("test_current_role", default=None)


@pytest.fixture
async def roles(test_database_url):
    """A login role that may become every user role; only alice may read the table. Yields the login password."""
    password = secrets.token_hex(16)
    conn = await asyncpg.connect(test_database_url)

    async def drop():
        await conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = $1 AND pid <> pg_backend_pid()",
            LOGIN,
        )
        for role in (LOGIN, *USERS):
            await conn.execute(f"DROP ROLE IF EXISTS {role}")

    await drop()
    for role in USERS:
        await conn.execute(f"CREATE ROLE {role} NOLOGIN")
    await conn.execute(f"CREATE ROLE {LOGIN} LOGIN PASSWORD '{password}' NOINHERIT IN ROLE {', '.join(USERS)}")
    await conn.execute(f"CREATE TABLE {TABLE} (note text)")
    await conn.execute(f"INSERT INTO {TABLE} VALUES ('for alice')")
    await conn.execute(f"GRANT SELECT ON {TABLE} TO {USERS[0]}")
    try:
        yield password
    finally:
        await drop()
        await conn.close()


def login_url(url, password):
    parts = urlsplit(url)
    return urlunsplit(parts._replace(netloc=f"{LOGIN}:{quote(password)}@{parts.hostname}:{parts.port or 5432}"))


class Seen:
    """Records the current user each ``on_acquire`` finds before it sets the role."""

    def __init__(self):
        self.before: list[str] = []


def profile_role(profile):
    return profile.params.get("ROLE") or profile.params.get("role")


def django_hooks(seen, role_of=profile_role):
    def on_acquire(connection, profile, session):  # noqa: ARG001
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            seen.before.append(cursor.fetchone()[0])
            role = role_of(profile)
            if role is not None:
                cursor.execute(f"SET ROLE {connection.ops.quote_name(role)}")

    def on_release(connection, profile, session):  # noqa: ARG001
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")

    return on_acquire, on_release


def asyncpg_hooks(seen, role_of=profile_role):
    async def on_acquire(connection, profile, session):  # noqa: ARG001
        seen.before.append(await connection.fetchval("SELECT current_user"))
        role = role_of(profile)
        if role is not None:
            await connection.execute(f"SET ROLE {quote_name(role)}")

    async def on_release(connection, profile, session):  # noqa: ARG001
        await connection.execute("RESET ROLE")

    return on_acquire, on_release


@pytest.fixture(params=["django", "asyncpg"])
async def per_user(request, roles, test_database_url, django_db_blocker):
    """A router whose provider creates the profile ``user:<name>`` at run time; all share one login role."""
    seen = Seen()
    registry = ProfileRegistry(pool=PoolSettings(max_connections=1, acquire_timeout=10))

    def provider(name):
        if not name.startswith("user:"):
            return None
        role = f"adorm_test_{name.removeprefix('user:')}"
        if request.param == "django":
            on_acquire, on_release = django_hooks(seen)
            params = {"USER": LOGIN, "PASSWORD": roles, "ROLE": role}
            return ConnectionProfile(name, DjangoDriver(), params=params, on_acquire=on_acquire, on_release=on_release)
        on_acquire, on_release = asyncpg_hooks(seen)
        params = {"dsn": login_url(test_database_url, roles), "role": role}
        return ConnectionProfile(name, AsyncpgDriver(), params=params, on_acquire=on_acquire, on_release=on_release)

    registry.add_provider(provider)
    router = Router(profiles=registry)
    with django_db_blocker.unblock():
        try:
            yield router, seen
        finally:
            await router.close()


async def test_each_user_profile_runs_as_its_role_over_one_login(per_user):
    router, _ = per_user
    alice = await router.run(create_session("user:alice", "web"), WHO)
    bob = await router.run(create_session("user:bob", "web"), WHO)
    assert alice == [{"who": USERS[0], "login": LOGIN}]
    assert bob == [{"who": USERS[1], "login": LOGIN}]


async def test_privileges_follow_the_role(per_user):
    router, _ = per_user
    assert await router.run(create_session("user:alice", "web"), SECRET) == [{"note": "for alice"}]
    with pytest.raises(ProgrammingError) as info:
        await router.run(create_session("user:bob", "web"), SECRET)
    assert info.value.sqlstate == "42501"


async def test_on_release_resets_the_role_before_the_slot_is_reused(per_user):
    router, seen = per_user
    for name in ("first", "second", "third"):
        assert (await router.run(create_session("user:alice", name), WHO))[0]["who"] == USERS[0]
    pool = router.pool("user:alice")
    assert pool.size == 1
    assert seen.before == [LOGIN, LOGIN, LOGIN]


async def test_the_role_holds_for_a_whole_transaction(per_user):
    router, _ = per_user
    async with router.slot(create_session("user:alice", "tx")) as lease:
        await lease.call("begin")
        assert (await lease.call("execute", WHO))[0]["who"] == USERS[0]
        await lease.call("execute", RawQuery(f"SELECT count(*) AS n FROM {TABLE}"))
        await lease.call("commit")


async def test_concurrent_users_never_see_each_others_role(per_user):
    router, _ = per_user

    async def check(user, i):
        rows = await router.run(create_session(f"user:{user}", f"s{i}"), WHO)
        return user, rows[0]["who"]

    results = await asyncio.gather(*(check(user, i) for i in range(6) for user in ("alice", "bob")))
    assert all(who == f"adorm_test_{user}" for user, who in results)


# One shared profile: the role comes from the acquiring task's context


@pytest.fixture(params=["django", "asyncpg"])
async def shared(request, roles, test_database_url, django_db_blocker):
    """One profile and one pool of a single slot; ``on_acquire`` sets the role in ``current_role``. The profile's
    ``connect`` opens the login connection itself."""
    seen = Seen()

    def role_of(profile):  # noqa: ARG001
        user = current_role.get()
        return None if user is None else f"adorm_test_{user}"

    if request.param == "django":
        on_acquire, on_release = django_hooks(seen, role_of)

        def connect(profile):
            from django.db import connections

            wrapper = connections[profile.alias]
            params = {**wrapper.get_connection_params(), "user": LOGIN, "password": roles}
            return type(wrapper).get_new_connection(wrapper, params)

        driver = DjangoDriver()
    else:
        on_acquire, on_release = asyncpg_hooks(seen, role_of)
        dsn = login_url(test_database_url, roles)

        async def connect(profile):  # noqa: ARG001
            return await asyncpg.connect(dsn)

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
    token = current_role.set(user)
    try:
        return await router.run(create_session("app", f"request-{user}"), query)
    finally:
        current_role.reset(token)


async def test_users_alternating_on_one_shared_slot_each_get_their_own_role(shared):
    router, seen = shared
    for user in ("alice", "bob", "alice", "bob"):
        assert await as_user(router, user, WHO) == [{"who": f"adorm_test_{user}", "login": LOGIN}]
    assert router.pool("app").size == 1
    assert seen.before == [LOGIN] * 4


async def test_bob_never_gets_alices_privileges_on_her_slot(shared):
    router, seen = shared
    assert await as_user(router, "alice", SECRET) == [{"note": "for alice"}]
    with pytest.raises(ProgrammingError) as info:
        await as_user(router, "bob", SECRET)
    assert info.value.sqlstate == "42501"
    assert await as_user(router, None, WHO) == [{"who": LOGIN, "login": LOGIN}]
    assert router.pool("app").size == 1
    assert seen.before == [LOGIN] * 3


async def test_concurrent_users_on_a_shared_pool_never_see_each_others_role(shared):
    router, seen = shared

    async def check(user):
        rows = await as_user(router, user, WHO)
        return user, rows[0]["who"]

    results = await asyncio.gather(*(check(user) for _ in range(5) for user in ("alice", "bob")))
    assert all(who == f"adorm_test_{user}" for user, who in results)
    assert seen.before == [LOGIN] * 10


async def test_reset_all_keeps_the_role_and_reset_role_restores_the_login(roles, test_database_url):
    conn = await asyncpg.connect(login_url(test_database_url, roles))
    try:
        await conn.execute(f"SET ROLE {USERS[0]}")
        await conn.execute("RESET ALL")
        assert await conn.fetchval("SELECT current_user") == USERS[0]
        await conn.execute("RESET ROLE")
        assert await conn.fetchval("SELECT current_user") == LOGIN
    finally:
        await conn.close()
