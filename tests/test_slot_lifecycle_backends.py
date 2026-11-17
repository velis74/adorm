"""The slot lifecycle on PostgreSQL: connections the server ended, unclean ends and idle connections purged, on a
threaded and a native driver; and on the Django driver, custom SQL that Django runs on the driver side and the
connection lifetime Django's own settings would otherwise set."""

import asyncio
import itertools
import sys

import asyncpg
import pytest

from django.db import connections, transaction
from django.db.backends import utils as django_utils
from django.db.models.signals import post_save

import adorm

from adorm import ConnectionProfile, create_session, DatabaseError, InterfaceError
from adorm.drivers import is_connection_error
from adorm.drivers.django import DjangoDriver as OrmDjangoDriver
from adorm.drivers.django.driver import _custom_sql_call, _ORM_SQL_MODULES
from adorm.executor import CloseReason, Pool, Router, set_runner
from tests.contract.drivers import AsyncpgDriver, DjangoDriver, Profile, RawQuery
from tests.djangoproject.library import models as dj
from tests.generated.library import Tag

pytestmark = pytest.mark.django_db(transaction=True)

REPEAT = range(3)
GUARD = 10
PID = RawQuery("SELECT pg_backend_pid() AS pid")
_names = itertools.count()


def session():
    return create_session("default", f"life-{next(_names)}")


async def until(predicate):
    async with asyncio.timeout(GUARD):
        while not await predicate():
            await asyncio.sleep(0.01)


@pytest.fixture
async def monitor(test_database_url):
    conn = await asyncpg.connect(test_database_url)
    try:
        yield conn
    finally:
        await conn.close()


async def backend_gone(monitor, pid):
    return not await monitor.fetchval("SELECT count(*) FROM pg_stat_activity WHERE pid = $1", pid)


@pytest.fixture(params=["django", "asyncpg"])
async def raw_router(request, test_database_url, django_db_blocker):
    """``make(**pool)`` returns a router with one pool of a raw-SQL driver: threaded (Django) or native (asyncpg)."""
    routers = []

    def make(**settings):
        if request.param == "django":
            pool = Pool(DjangoDriver(), Profile("default"), **settings)
        else:
            pool = Pool(AsyncpgDriver(), Profile("default", params={"dsn": test_database_url}), **settings)
        router = Router([pool])
        routers.append(router)
        return router, pool

    with django_db_blocker.unblock():
        yield make
        for router in routers:
            await router.close()


async def pid_of(router):
    return (await router.run(session(), PID))[0]["pid"]


@pytest.mark.parametrize("attempt", REPEAT)
async def test_a_connection_the_server_ended_fails_one_operation_then_is_replaced(raw_router, monitor, attempt):  # noqa: ARG001
    router, pool = raw_router(max_connections=1, acquire_timeout=5)
    pid = await pid_of(router)
    (slot,) = pool._slots
    await monitor.execute("SELECT pg_terminate_backend($1)", pid)
    await until(lambda: backend_gone(monitor, pid))
    # The connection may notice the end itself (asyncpg) and is then replaced before use; else one operation fails.
    lost = None
    try:
        await router.run(session(), PID)
    except (DatabaseError, InterfaceError) as exc:
        lost = exc
    if lost is None:
        assert slot.close_reason is CloseReason.UNUSABLE
    else:
        assert is_connection_error(lost)
        assert slot.close_reason is CloseReason.CONNECTION_ERROR
    assert await pid_of(router) != pid
    assert (pool.size, pool.in_use) == (1, 0)


async def test_a_lease_ended_inside_a_transaction_rolls_back_and_closes_the_connection(raw_router, monitor):
    router, pool = raw_router(max_connections=1, acquire_timeout=5)
    await monitor.execute("DROP TABLE IF EXISTS lifecycle_item")
    await monitor.execute("CREATE TABLE lifecycle_item (name text)")
    try:
        lease = await router.acquire(session())
        pid = (await lease.call("execute", PID))[0]["pid"]
        await lease.call("begin")
        await lease.call("execute", RawQuery("INSERT INTO lifecycle_item VALUES ('lost')"))
        await lease.release()
        assert lease.slot.close_reason is CloseReason.UNCLEAN_END
        await until(lambda: backend_gone(monitor, pid))
        assert await monitor.fetchval("SELECT count(*) FROM lifecycle_item") == 0
        assert await pid_of(router) != pid
    finally:
        await monitor.execute("DROP TABLE IF EXISTS lifecycle_item")


async def test_an_idle_connection_is_closed_after_purge_stale_connections_at(raw_router, monitor):
    router, pool = raw_router(purge_stale_connections_at=0.05)
    pid = await pid_of(router)
    (slot,) = pool._slots
    await until(lambda: backend_gone(monitor, pid))
    assert slot.close_reason is CloseReason.STALE
    assert pool.size == 0
    assert await pid_of(router) != pid


# The Django ORM driver


@pytest.fixture
async def executor():
    """``make(profile=None, **pool)`` sets up a router with one pool of the Django ORM driver."""
    routers = []

    def make(profile=None, **settings):
        driver = OrmDjangoDriver()
        pool = Pool(driver, profile or ConnectionProfile("default", driver), **settings)
        router = Router([pool])
        routers.append(router)
        set_runner(router)
        return pool

    previous = set_runner(None)
    try:
        yield make
    finally:
        set_runner(previous)
        for router in routers:
            await router.close()


@pytest.fixture
def receiver():
    """``receiver(fn, sender)`` connects ``fn`` to Django's ``post_save`` for the test."""
    connected = []

    def connect(fn, sender):
        post_save.connect(fn, sender=sender, weak=False)
        connected.append((fn, sender))

    yield connect
    for fn, sender in connected:
        post_save.disconnect(fn, sender=sender)


def raw_sql(sql, params=()):
    with connections["default"].cursor() as cursor:
        cursor.execute(sql, params)


async def _application_name():
    async with adorm.atomic() as tx:
        connection = tx.lease.slot.connection
        return await connection.call_hook(lambda db, *_: _setting(db, "application_name"))


def _setting(db, name):
    """``name``'s value on Django's wrapper ``db``, read on its psycopg connection past Django's execute wrappers, so
    that the read is not custom SQL."""
    return db.connection.execute("SELECT current_setting(%s)", [name]).fetchone()[0]


def only_slot(pool):
    (slot,) = pool._slots
    return slot


async def test_raw_sql_of_a_signal_receiver_closes_the_slot(library, executor, receiver):  # noqa: ARG001
    pool = executor(max_connections=1, acquire_timeout=5)

    def receive(instance, **_):
        if instance.name == "tainting":
            raw_sql("SET application_name TO 'tainted'")

    receiver(receive, dj.Tag)
    await Tag.objects.create(name="plain")
    slot = only_slot(pool)
    await Tag.objects.create(name="tainting")
    assert slot.close_reason is CloseReason.CUSTOM_SQL
    assert pool.size == 0
    # The next session never sees the state the raw SQL left.
    assert await _application_name() != "tainted"


async def test_raw_sql_inside_a_transaction_commits_then_closes_the_slot(library, executor, receiver):  # noqa: ARG001
    pool = executor(max_connections=1, acquire_timeout=5)
    receiver(lambda instance, **kwargs: raw_sql("SET search_path TO pg_catalog, public"), dj.Tag)  # noqa: ARG005
    async with adorm.atomic():
        await Tag.objects.create(name="committed")
        slot = only_slot(pool)
        assert await Tag.objects.filter(name="committed").count() == 1  # the same connection serves the block
    assert slot.close_reason is CloseReason.CUSTOM_SQL
    assert sorted([t.name async for t in Tag.objects.all()]) == ["committed", "rare"]


@pytest.mark.parametrize(
    "receive",
    [
        pytest.param(lambda **_: list(dj.Tag.objects.raw("SELECT id, name FROM library_tag")), id="raw"),
        pytest.param(
            lambda **_: connections["default"].cursor().execute("SELECT 1"),
            id="cursor",
        ),
    ],
)
async def test_every_way_to_run_sql_past_the_orm_counts(library, executor, receiver, receive, instrumented):  # noqa: ARG001
    pool = executor(max_connections=1, acquire_timeout=5)
    receiver(receive, dj.Tag)
    await Tag.objects.create(name="xx")
    assert pool.size == 0


async def test_raw_sql_of_an_on_commit_callback_run_at_once_counts(library, executor, receiver, instrumented):  # noqa: ARG001
    pool = executor(max_connections=1, acquire_timeout=5)
    # Outside a transaction Django runs the callback inside on_commit(), a function of its backend.
    receiver(lambda **_: transaction.on_commit(lambda: raw_sql("SELECT 1"), using="default"), dj.Tag)
    await Tag.objects.create(name="xx")
    assert pool.size == 0


async def test_orm_work_on_the_driver_side_keeps_the_slot(library, executor, receiver, instrumented):  # noqa: ARG001
    pool = executor(max_connections=1, acquire_timeout=5)

    def receive(instance, **kwargs):  # noqa: ARG001
        if instance.name.startswith("audit"):
            return
        # ORM queries, Django's own savepoint, a lock and a bulk write: all SQL of Django's compiler and backend.
        with transaction.atomic(using="default"):
            dj.Tag.objects.create(name=f"audit {instance.name}")
            list(dj.Tag.objects.select_for_update().filter(name=instance.name))
            dj.Tag.objects.filter(name=f"audit {instance.name}").update(name=f"audit {instance.name}!")
        dj.Tag.objects.bulk_create([dj.Tag(name=f"audit bulk {instance.name}")])

    receiver(receive, dj.Tag)
    await Tag.objects.create(name="first")
    slot = only_slot(pool)
    async with adorm.atomic():
        await Tag.objects.create(name="second")
    assert only_slot(pool) is slot
    assert slot.close_reason is None
    assert not slot.connection.ran_custom_sql


async def test_raw_sql_of_an_on_commit_callback_closes_the_slot_after_the_callbacks(library, executor, receiver):  # noqa: ARG001
    pool = executor(max_connections=1, acquire_timeout=5)

    def receive(instance, **kwargs):  # noqa: ARG001
        transaction.on_commit(lambda: raw_sql("SET application_name TO 'from callback'"), using="default")

    receiver(receive, dj.Tag)
    async with adorm.atomic():
        await Tag.objects.create(name="kept")
        slot = only_slot(pool)
    # The callback runs on the slot's thread after the commit; the slot leaves the pool once it ran.
    assert await _application_name() != "from callback"
    assert slot.close_reason is CloseReason.CUSTOM_SQL


async def test_connection_options_and_options_from_hooks_are_not_custom_sql(library, executor):  # noqa: ARG001
    driver = OrmDjangoDriver()
    seen = []
    profile = ConnectionProfile(
        "default",
        driver,
        on_acquire=lambda *_: {"adorm_test.app_user": "alice"},
        on_release=lambda *_: None,
        options={"statement_timeout": 5},
    )
    pool = executor(profile, max_connections=1, acquire_timeout=5)
    for _ in REPEAT:
        async with adorm.atomic() as tx:
            with adorm.connection_options(lock_timeout=2):
                assert await Tag.objects.count() == 1
                seen.append(await tx.lease.slot.connection.call_hook(lambda db, *_: _setting(db, "lock_timeout")))
            seen.append(await tx.lease.slot.connection.call_hook(lambda db, *_: _setting(db, "adorm_test.app_user")))
    assert seen == ["2s", "alice"] * len(REPEAT)
    slot = only_slot(pool)
    assert slot.close_reason is None
    assert not slot.connection.ran_custom_sql


@pytest.mark.parametrize("hook", ["on_acquire", "on_release"])
async def test_sql_a_profile_hook_runs_is_custom_and_the_slot_leaves_the_pool(library, executor, hook):  # noqa: ARG001
    driver = OrmDjangoDriver()
    profile = ConnectionProfile(
        "default", driver, **{hook: lambda db, *_: raw_sql_on(db, "SET application_name TO 'adorm hook'")}
    )
    pool = executor(profile, max_connections=1, acquire_timeout=5)
    pids = set()
    for _ in REPEAT:
        async with adorm.atomic() as tx:
            assert await Tag.objects.count() == 1
            slot = tx.lease.slot
            pids.add(slot.connection.backend_pid)
        await asyncio.wait_for(slot.wait_closed(), GUARD)
        assert slot.close_reason is CloseReason.CUSTOM_SQL
    assert len(pids) == len(REPEAT)
    assert pool.size == 0


def raw_sql_on(db, sql):
    with db.cursor() as cursor:
        cursor.execute(sql)


POOLED = {"OPTIONS": {**connections.settings["default"].get("OPTIONS", {}), "pool": True}}


@pytest.mark.parametrize("pooled", [False, True], ids=["unpooled", "django-pool"])
async def test_djangos_conn_max_age_and_health_checks_do_not_apply(library, executor, receiver, pooled):  # noqa: ARG001
    driver = OrmDjangoDriver()
    params = {"CONN_MAX_AGE": 0, "CONN_HEALTH_CHECKS": True, **(POOLED if pooled else {})}
    profile = ConnectionProfile("default", driver, params=params)
    pool = executor(profile, max_connections=1, acquire_timeout=5)
    # What Django's request signals and Celery's Django fixup run between requests and tasks, here on the slot's thread.
    receiver(lambda **kwargs: connections["default"].close_if_unusable_or_obsolete(), dj.Tag)  # noqa: ARG005
    pids = []
    for name in ("aa", "bb", "cc"):
        await Tag.objects.create(name=name)
        async with adorm.atomic() as tx:
            db_settings = await tx.lease.slot.connection.call_hook(
                lambda db, *_: (db.connection.info.backend_pid, db.close_at, db.health_check_enabled)
            )
        pids.append(db_settings[0])
        assert db_settings[1:] == (None, False)
    assert len(set(pids)) == 1
    assert only_slot(pool).close_reason is None
    # The alias's own settings are unchanged.
    assert connections["default"].settings_dict["CONN_MAX_AGE"] == 0


@pytest.mark.parametrize("ending", ["custom_sql", "stale"])
async def test_with_djangos_pool_a_closed_slot_ends_its_backend(library, executor, receiver, monitor, ending):  # noqa: ARG001
    profile = ConnectionProfile("default", OrmDjangoDriver(), params={**POOLED, "CONN_MAX_AGE": 0})
    pool = executor(profile, max_connections=1, acquire_timeout=5, purge_stale_connections_at=0.05)
    if ending == "custom_sql":
        receiver(lambda **_: raw_sql("SET application_name TO 'tainted'"), dj.Tag)
    async with adorm.atomic() as tx:
        slot = tx.lease.slot
        pid = await slot.connection.call_hook(lambda db, *_: db.connection.info.backend_pid)
        if ending == "custom_sql":
            await Tag.objects.create(name="xx")
    await until(lambda: backend_gone(monitor, pid))
    assert slot.close_reason is (CloseReason.CUSTOM_SQL if ending == "custom_sql" else CloseReason.STALE)
    assert pool.size == 0


async def test_a_direct_connection_leaves_the_threads_wrapper_settings_as_they_were(library):  # noqa: ARG001
    class DirectDjangoDriver(OrmDjangoDriver):
        direct = True

    driver = DirectDjangoDriver()
    profile = ConnectionProfile("default", driver, params={"CONN_MAX_AGE": 60})
    connection = await driver.open_connection(profile, lambda _event: None)
    wrapper = connection.target
    try:
        assert (wrapper.settings_dict["CONN_MAX_AGE"], wrapper.settings_dict["CONN_HEALTH_CHECKS"]) == (None, False)
    finally:
        await connection.close()
    assert wrapper.settings_dict is connections.settings["default"]
    assert wrapper.connection is None


# Instrumentation that wraps Django's cursor


def _in_module(module, source, **names):
    """The function ``source`` defines, compiled as code of ``module``."""
    namespace = {"__name__": module, **names}
    exec(source, namespace)  # noqa: S102
    return namespace[source.split("(")[0].removeprefix("def ").strip()]


@pytest.fixture(params=["plain", "sentry", "tracer"])
def instrumented(request, monkeypatch):
    """Django's ``CursorWrapper.execute`` as it is, replaced the way Sentry does, or wrapped by a tracer's helper."""
    original = django_utils.CursorWrapper.execute
    if request.param == "sentry":
        execute = _in_module(
            "sentry_sdk.integrations.django",
            "def execute(self, sql, params=None):\n    return real_execute(self, sql, params)\n",
            real_execute=original,
        )
        monkeypatch.setattr(django_utils.CursorWrapper, "execute", execute)
    elif request.param == "tracer":
        trace = _in_module("ddtrace.contrib.dbapi", "def _trace_method(method, *args):\n    return method(*args)\n")
        execute = _in_module(
            "ddtrace.contrib.dbapi",
            "def execute(self, sql, params=None):\n    return _trace(real_execute, self, sql, params)\n",
            real_execute=original,
            _trace=trace,
        )
        monkeypatch.setattr(django_utils.CursorWrapper, "execute", execute)
    return request.param


def _frame_below(modules):
    """A frame inside Django's cursor wrapper, called through functions of ``modules``, outermost first."""

    def capture():
        return sys._getframe()

    call = _in_module("django.db.backends.utils", "def at_cursor():\n    return capture()\n", capture=capture)
    for index, module in enumerate(reversed(modules)):
        call = _in_module(module, f"def f{index}():\n    return inner()\n", inner=call)
    return call()


GIS = "django.contrib.gis.db.backends.postgis."


@pytest.mark.parametrize(
    ("modules", "trusted", "custom"),
    [
        (["django.db.models.query", "django.db.models.sql.compiler"], (), False),
        (["django.db.models.sql.compiler", "sentry_sdk.integrations.django"], (), False),
        (["django.db.backends.base.base", "ddtrace.contrib.dbapi", "ddtrace.contrib.dbapi"], (), False),
        (["app.signals"], (), True),
        (["django.db.models.sql.compiler", "django.dispatch.dispatcher", "app.signals"], (), True),
        (["django.db.models.sql.query"], (), True),
        ([GIS + "operations"], (), True),
        ([GIS + "operations"], (GIS,), False),
        (["thirdparty.backend.operations", "sentry_sdk.integrations.django"], ("thirdparty.backend.",), False),
    ],
)
def test_the_caller_of_the_cursor_decides_what_counts_as_custom_sql(modules, trusted, custom):
    frame = _frame_below(modules)
    assert _custom_sql_call(frame, (*_ORM_SQL_MODULES, *trusted), frozenset()) is custom


def test_the_search_for_the_caller_stops_at_the_drivers_entry():
    frame = _frame_below(["django.db.models.sql.compiler", "adorm.entry", "app.code"])
    assert _custom_sql_call(frame, _ORM_SQL_MODULES, frozenset()) is False
    entry = frame
    while entry.f_globals["__name__"] != "adorm.entry":
        entry = entry.f_back
    assert _custom_sql_call(frame, _ORM_SQL_MODULES, frozenset({entry.f_code})) is True
