"""The Django driver per database vendor: the capabilities of each backend, read without connecting and checked before
a slot is taken; errors of SQLite, MySQL and Oracle mapped without SQLSTATEs; the databases profiles name."""

import sqlite3

from types import SimpleNamespace

import pytest

from django.db.utils import IntegrityError as DjangoIntegrityError, OperationalError as DjangoOperationalError

import adorm

from adorm import ConnectionProfile, F, NotSupportedError
from adorm.drivers.django import DjangoDriver
from adorm.drivers.django.driver import database_key, map_django_error, profile_vendor
from adorm.drivers.django.vendors import (
    CAPABILITIES,
    features_capabilities,
    is_refusal,
    LOCK_WAIT_CANCELLED,
    MYSQL_CANCEL_CONNECT_TIMEOUT,
    MYSQL_CAPABILITIES,
    ORACLE_CAPABILITIES,
    PostgreSQL,
    POSTGRESQL_ONLY,
    READ_CAPABILITIES,
    SQLite,
    SQLITE_CAPABILITIES,
    sqlite_error_code,
    sqlite_integrity_details,
    Vendor,
    vendor,
)
from adorm.exceptions import IntegrityError, InterfaceError, OperationalError, wrap_driver_error
from adorm.executor import Pool, Router, set_runner
from tests.clubmodels.clubs import Club

SQLITE_ENGINE = {"ENGINE": "django.db.backends.sqlite3", "NAME": "/srv/data/clubs.sqlite3"}


@pytest.fixture(autouse=True)
def _local_alias(monkeypatch):
    """The alias ``local``: a SQLite database in ``DATABASES``, which nothing connects to."""
    from django.db import connections

    monkeypatch.setitem(connections.settings, "local", {**connections.settings["default"], **SQLITE_ENGINE})


def sqlite_profile(**params):
    return ConnectionProfile("default", DjangoDriver, alias="local", params=params)


# Capabilities per vendor


def test_postgresql_declares_everything_the_translation_supports():
    assert vendor("postgresql").capabilities() == CAPABILITIES == DjangoDriver().capabilities
    assert POSTGRESQL_ONLY <= CAPABILITIES.constructs
    assert READ_CAPABILITIES.constructs <= CAPABILITIES.constructs


@pytest.mark.parametrize("capabilities", [SQLITE_CAPABILITIES, MYSQL_CAPABILITIES, ORACLE_CAPABILITIES])
def test_other_databases_declare_no_postgresql_features(capabilities):
    assert capabilities.operations == CAPABILITIES.operations
    assert not capabilities.constructs & POSTGRESQL_ONLY
    assert capabilities.constructs < CAPABILITIES.constructs
    assert capabilities.connection_options == frozenset()
    assert capabilities.backend_events == frozenset()
    assert capabilities.max_channel_length is None
    assert capabilities.savepoints
    assert capabilities.cancellation


def test_sqlite_has_no_row_locks_and_no_case_sensitive_like():
    constructs = SQLITE_CAPABILITIES.constructs
    assert not {name for name in constructs if name.startswith("clause.select_for_update")}
    assert not {"lookup.contains", "lookup.startswith", "lookup.endswith"} & constructs
    assert {"lookup.icontains", "lookup.istartswith", "lookup.iendswith", "lookup.regex"} <= constructs
    assert {"clause.on_conflict.ignore", "clause.on_conflict.update", "clause.intersection"} <= constructs


def test_mysql_and_oracle_lock_rows_without_no_key_update():
    for capabilities in (MYSQL_CAPABILITIES, ORACLE_CAPABILITIES):
        assert "clause.select_for_update.skip_locked" in capabilities.constructs
        assert "clause.select_for_update.no_key" not in capabilities.constructs
    assert "clause.on_conflict.ignore" in MYSQL_CAPABILITIES.constructs
    assert "clause.on_conflict.ignore" not in ORACLE_CAPABILITIES.constructs
    assert "clause.intersection" not in MYSQL_CAPABILITIES.constructs
    # MariaDB has no FOR UPDATE OF; ON DUPLICATE KEY UPDATE takes no unique_fields.
    assert not {"clause.select_for_update.of", "clause.on_conflict.update"} & MYSQL_CAPABILITIES.constructs
    assert "clause.select_for_update.of" in ORACLE_CAPABILITIES.constructs


def test_an_upsert_needs_a_conflict_target_on_a_backend_without_a_vendor_of_its_own():
    class Upserts:
        supports_update_conflicts = True

    class UpsertsWithTarget(Upserts):
        supports_update_conflicts_with_target = True

    assert "clause.on_conflict.update" not in features_capabilities(Upserts).constructs
    assert "clause.on_conflict.update" in features_capabilities(UpsertsWithTarget).constructs


def test_mysql_cancels_on_a_connection_of_the_profiles_connect_hook():
    statements, opened = [], []

    class Cursor:
        def execute(self, sql):
            statements.append(sql)

        def close(self):
            pass

    class Killer:
        closed = False

        def cursor(self):
            return Cursor()

        def close(self):
            self.closed = True

    def connect(profile):
        opened.append(profile.name)
        return Killer()

    class Raw:
        def thread_id(self):
            return 42

    def refuse(*_):
        raise AssertionError("the wrapper's parameters are not used")

    profile = ConnectionProfile("mysql", DjangoDriver, alias="local", connect=connect)
    db = type("Wrapper", (), {"get_new_connection": refuse, "get_connection_params": refuse})()
    connection = type("Connection", (), {"db": db, "profile": profile})()
    vendor("mysql").cancel(connection, Raw())
    assert (opened, statements) == (["mysql"], ["KILL QUERY 42"])


def test_mysql_cancels_on_a_connection_with_a_connect_timeout_and_loses_the_cancel_when_it_cannot_open():
    opened = []

    def unreachable(params):
        opened.append(params)
        raise OSError("Can't connect to MySQL server")

    db = type("Wrapper", (), {"get_new_connection": staticmethod(unreachable), "get_connection_params": lambda _: {}})()
    connection = type("Connection", (), {"db": db, "profile": None})()
    vendor("mysql").cancel(connection, None)
    assert opened == [{"connect_timeout": MYSQL_CANCEL_CONNECT_TIMEOUT}]


def test_another_backend_declares_what_its_feature_flags_allow():
    class Features:
        has_select_for_update = True
        has_select_for_update_nowait = True
        supports_ignore_conflicts = True
        supports_select_union = True
        uses_savepoints = True

        @property
        def has_select_for_update_skip_locked(self):  # computed per connection: not declared
            return True

    capabilities = features_capabilities(Features)
    assert {"clause.select_for_update", "clause.select_for_update.nowait", "clause.on_conflict.ignore"} <= (
        capabilities.constructs
    )
    assert "clause.select_for_update.skip_locked" not in capabilities.constructs
    assert not {"clause.intersection", "clause.difference", "clause.on_conflict.update"} & capabilities.constructs
    assert capabilities.savepoints
    assert not capabilities.cancellation

    class NoLocks:
        has_select_for_update_nowait = True

    bare = features_capabilities(NoLocks)
    assert not {name for name in bare.constructs if name.startswith("clause.select_for_update")}
    assert not bare.savepoints
    assert Vendor("cockroachdb", Features).capabilities() == capabilities
    assert vendor("cockroachdb", Features).name == "cockroachdb"


def test_a_profile_has_the_capabilities_of_its_databases_backend():
    driver = DjangoDriver()
    assert profile_vendor(sqlite_profile()) is vendor("sqlite")
    assert driver.capabilities_for(sqlite_profile()) == SQLITE_CAPABILITIES
    assert driver.capabilities_for(ConnectionProfile("default", driver)) == CAPABILITIES
    # An alias without a DATABASES entry, or one whose backend does not load: the PostgreSQL capabilities.
    assert profile_vendor(ConnectionProfile("default", driver, alias="nowhere")) is None
    assert driver.capabilities_for(ConnectionProfile("default", driver, alias="nowhere")) == CAPABILITIES
    # params do not change the backend
    assert profile_vendor(sqlite_profile(ENGINE="django.db.backends.postgresql")) is vendor("sqlite")


@pytest.fixture
def sqlite_pool():
    """A router with one pool of the Django driver on a SQLite profile; nothing may connect."""
    routers = []
    previous = set_runner(None)

    def make(**params) -> Pool:
        pool = Pool(DjangoDriver(), sqlite_profile(**params), acquire_timeout=5)
        router = Router([pool])
        routers.append(router)
        set_runner(router)
        return pool

    yield make
    set_runner(previous)


async def test_what_sqlite_cannot_do_is_refused_before_a_slot_is_taken(sqlite_pool):
    pool = sqlite_pool()
    assert pool.capabilities == SQLITE_CAPABILITIES
    refused = [
        (Club.objects.select_for_update().count(), "clause.select_for_update"),
        (Club.objects.order_by("city").distinct("city").count(), "clause.distinct_on"),
        (Club.objects.filter(disciplines__overlap=["Dinghy"]).count(), "lookup.overlap"),
        (Club.objects.filter(name__contains="Klub").count(), "lookup.contains"),
        (Club.objects.filter(name__startswith=F("city")).count(), "lookup.startswith"),
    ]
    for awaitable, construct in refused:
        with pytest.raises(NotSupportedError) as raised:
            await awaitable
        assert list(raised.value.unsupported) == [construct]
        assert raised.value.driver == "django"
    with pytest.raises(NotSupportedError, match="statement_timeout"):
        with adorm.connection_options(statement_timeout=5):
            await Club.objects.count()
    with pytest.raises(NotSupportedError, match="'notify'"):
        async with adorm.listen("orders"):
            pytest.fail("entered")
    assert pool.size == 0


async def test_database_options_of_a_sqlite_profile_are_refused_when_its_pool_is_created():
    profile = ConnectionProfile("default", DjangoDriver, alias="local", options={"lock_timeout": 2})
    with pytest.raises(NotSupportedError, match="lock_timeout"):
        Pool(DjangoDriver(), profile)


async def test_a_sqlite_profile_has_no_monitor():
    with pytest.raises(NotSupportedError, match="monitors PostgreSQL databases only, not 'sqlite'"):
        await DjangoDriver().open_monitor(sqlite_profile())


# The databases profiles name


def test_database_keys():
    assert database_key(sqlite_profile()) == ("sqlite", "/srv/data/clubs.sqlite3")
    assert database_key(sqlite_profile(NAME="/srv/data/../data/clubs.sqlite3")) == ("sqlite", "/srv/data/clubs.sqlite3")
    assert database_key(sqlite_profile(NAME=":memory:")) is None
    assert database_key(sqlite_profile(NAME="file:memdb?mode=memory")) is None
    assert database_key(sqlite_profile(NAME="file:memdb?mode=memory&cache=shared")) is None
    assert database_key(sqlite_profile(NAME="file::memory:")) is None
    assert database_key(sqlite_profile(NAME="file::memory:?cache=shared")) is None
    assert database_key(sqlite_profile(NAME="file:/srv/data/clubs.sqlite3?mode=ro")) == (
        "sqlite",
        "file:/srv/data/clubs.sqlite3?mode=ro",
    )
    assert database_key(ConnectionProfile("default", DjangoDriver, alias="nowhere")) is None
    entry = {"HOST": "db", "PORT": 3306, "NAME": "clubs"}
    assert vendor("mysql").database_key(entry) == ("mysql", "db", "3306", "clubs")
    assert vendor("oracle").database_key({"NAME": "xe"}) == ("oracle", "", "", "xe")


# SQLite errors


def sqlite_error(*statements: str) -> DjangoIntegrityError | DjangoOperationalError:
    """The Django exception of the last of ``statements`` on an in-memory database, chained as Django chains it."""
    db = sqlite3.connect(":memory:")
    try:
        for statement in statements:
            try:
                db.execute(statement)
            except sqlite3.DatabaseError as exc:
                cls = DjangoIntegrityError if isinstance(exc, sqlite3.IntegrityError) else DjangoOperationalError
                error = cls(*exc.args)
                error.__cause__ = exc
                return error
    finally:
        db.close()
    raise AssertionError("no statement failed")


CLUBS = "CREATE TABLE clubs_club (id integer PRIMARY KEY, name text NOT NULL, slug text UNIQUE)"


@pytest.mark.parametrize(
    ("statements", "details"),
    [
        (
            [CLUBS, "INSERT INTO clubs_club VALUES (1, 'A', 'a')", "INSERT INTO clubs_club VALUES (2, 'B', 'a')"],
            {"table_name": "clubs_club", "columns": ("slug",), "model": "clubs.Club", "fields": ("slug",)},
        ),
        (
            [CLUBS, "INSERT INTO clubs_club (id, slug) VALUES (1, 'a')"],
            {"table_name": "clubs_club", "columns": ("name",), "model": "clubs.Club", "fields": ("name",)},
        ),
        (
            [
                "CREATE TABLE pair (a int, b int, UNIQUE (a, b))",
                "INSERT INTO pair VALUES (1, 2)",
                "INSERT INTO pair VALUES (1, 2)",
            ],
            {"table_name": "pair", "columns": ("a", "b"), "model": None, "fields": ()},
        ),
        (
            ["CREATE TABLE qty (n int CONSTRAINT positive CHECK (n > 0))", "INSERT INTO qty VALUES (0)"],
            {"constraint_name": "positive", "table_name": None, "columns": ()},
        ),
        (
            [
                "PRAGMA foreign_keys = ON",
                "CREATE TABLE p (id int PRIMARY KEY)",
                "CREATE TABLE c (p_id int REFERENCES p (id))",
                "INSERT INTO c VALUES (1)",
            ],
            {"constraint_name": None, "table_name": None, "detail": "FOREIGN KEY constraint failed"},
        ),
        (
            [
                "CREATE TABLE label (t text)",
                "CREATE UNIQUE INDEX label_lower ON label (lower(t))",
                "INSERT INTO label VALUES ('A')",
                "INSERT INTO label VALUES ('a')",
            ],
            {"table_name": None, "columns": (), "detail": "UNIQUE constraint failed: index 'label_lower'"},
        ),
    ],
    ids=["unique", "not null", "composite", "check", "foreign key", "expression index"],
)
def test_sqlite_integrity_errors_name_the_table_and_columns_sqlite_reports(statements, details):
    error = map_django_error(sqlite_error(*statements), vendor("sqlite"))
    assert type(error) is IntegrityError
    assert error.sqlstate is None
    assert error.values == ()
    for name, value in details.items():
        assert getattr(error, name) == value, name
    assert isinstance(error.original, DjangoIntegrityError)


def test_sqlite_constraint_messages():
    assert sqlite_integrity_details("UNIQUE constraint failed: t.a, t.b") == {
        "detail": "UNIQUE constraint failed: t.a, t.b",
        "table_name": "t",
        "columns": ("a", "b"),
    }
    assert sqlite_integrity_details("CHECK constraint failed: n > 0")["constraint_name"] == "n > 0"
    assert sqlite_integrity_details("something else") == {"detail": "something else"}
    # Columns of two tables (a view) name no table.
    assert "table_name" not in sqlite_integrity_details("UNIQUE constraint failed: t.a, u.b")


def test_sqlite_errors_leave_the_connection_usable_and_interrupts_are_cancellations():
    sqlite = vendor("sqlite")
    locked = wrap_driver_error(sqlite_error("CREATE TABLE t (x)", "CREATE TABLE t (x)"))
    assert type(locked) is OperationalError
    assert locked.sqlstate is None
    assert not sqlite.is_connection_lost(locked)
    assert not sqlite.is_cancellation(locked)
    assert sqlite.is_connection_lost(InterfaceError("connection already closed"))
    assert sqlite.is_connection_lost(KeyboardInterrupt())

    interrupted = sqlite3.OperationalError("interrupted")
    interrupted.sqlite_errorcode = sqlite3.SQLITE_INTERRUPT
    mapped = wrap_driver_error(interrupted)
    assert sqlite_error_code(mapped) == sqlite3.SQLITE_INTERRUPT
    assert sqlite.is_cancellation(mapped)
    assert sqlite.is_cancellation(OperationalError(LOCK_WAIT_CANCELLED))
    assert not sqlite.is_cancellation(OperationalError("canceling statement due to user request", sqlstate="57014"))
    assert PostgreSQL().is_cancellation(OperationalError("canceled", sqlstate="57014"))


# MySQL and Oracle errors, as their libraries raise them


class MySQLError(Exception):
    """An exception of ``MySQLdb``: the error number, then the message."""


class OracleErrorObject:
    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message


def mysql(errno: int, message: str, cls: type = DjangoOperationalError):
    error = cls(errno, message)
    error.__cause__ = MySQLError(errno, message)
    return wrap_driver_error(error)


def oracle(code: int, message: str, cls: type = DjangoOperationalError):
    error = cls(OracleErrorObject(code, message))
    return wrap_driver_error(error)


def test_mysql_errors():
    mysql_vendor = vendor("mysql")
    assert mysql_vendor.is_cancellation(mysql(1317, "Query execution was interrupted"))
    assert mysql_vendor.is_connection_lost(mysql(2006, "MySQL server has gone away"))
    assert mysql_vendor.is_connection_lost(mysql(2013, "Lost connection to MySQL server during query"))
    deadlock = mysql(1213, "Deadlock found when trying to get lock; try restarting transaction")
    assert not mysql_vendor.is_connection_lost(deadlock)
    assert not mysql_vendor.is_cancellation(deadlock)
    assert mysql_vendor.is_connection_lost(InterfaceError("closed"))
    assert is_refusal(mysql(1040, "Too many connections"))
    assert DjangoDriver().is_refusal(mysql(1040, "Too many connections"))
    assert not is_refusal(deadlock)

    duplicate = mysql(1062, "Duplicate entry 'koper' for key 'clubs_club.slug'", DjangoIntegrityError)
    assert mysql_vendor.integrity_details(duplicate) == {
        "detail": "Duplicate entry 'koper' for key 'clubs_club.slug'",
        "constraint_name": "slug",
        "table_name": "clubs_club",
        "values": ("koper",),
    }
    null = mysql(1048, "Column 'name' cannot be null", DjangoIntegrityError)
    assert mysql_vendor.integrity_details(null)["columns"] == ("name",)
    foreign = mysql(
        1452,
        "Cannot add or update a child row: a foreign key constraint fails (`demo`.`clubs_membership`, CONSTRAINT"
        " `clubs_membership_club_id_fk` FOREIGN KEY (`club_id`) REFERENCES `clubs_club` (`id`))",
        DjangoIntegrityError,
    )
    details = mysql_vendor.integrity_details(foreign)
    assert (details["table_name"], details["constraint_name"], details["columns"]) == (
        "clubs_membership",
        "clubs_membership_club_id_fk",
        ("club_id",),
    )


def test_oracle_errors():
    oracle_vendor = vendor("oracle")
    assert oracle_vendor.is_cancellation(oracle(1013, "ORA-01013: user requested cancel of current operation"))
    assert oracle_vendor.is_connection_lost(oracle(3113, "ORA-03113: end-of-file on communication channel"))
    assert oracle_vendor.is_connection_lost(OperationalError("DPY-4011: the database or network closed the connection"))
    assert not oracle_vendor.is_connection_lost(oracle(60, "ORA-00060: deadlock detected while waiting for resource"))
    assert is_refusal(oracle(12516, "ORA-12516: TNS:listener could not find available handler"))

    unique = oracle(1, "ORA-00001: unique constraint (DEMO.CLUBS_CLUB_SLUG_UNIQ) violated", DjangoIntegrityError)
    assert oracle_vendor.integrity_details(unique) == {
        "detail": "ORA-00001: unique constraint (DEMO.CLUBS_CLUB_SLUG_UNIQ) violated",
        "constraint_name": "CLUBS_CLUB_SLUG_UNIQ",
        "schema_name": "DEMO",
    }
    null = oracle(1400, 'ORA-01400: cannot insert NULL into ("DEMO"."CLUBS_CLUB"."NAME")', DjangoIntegrityError)
    details = oracle_vendor.integrity_details(null)
    assert (details["schema_name"], details["table_name"], details["columns"]) == ("DEMO", "CLUBS_CLUB", ("NAME",))


def test_a_backend_without_a_vendor_of_its_own_follows_postgresqls_connection_rules():
    other = Vendor("cockroachdb")
    assert other.is_connection_lost(OperationalError("server closed the connection"))
    assert not other.is_cancellation(OperationalError("canceled", sqlstate="57014"))
    assert other.integrity_details(IntegrityError("duplicate")) == {}
    assert other.backend_pid(object()) is None
    assert isinstance(vendor("sqlite"), SQLite)


class _Database:
    """A DB-API module of a backend: its ``OperationalError`` takes MySQL's ``(errno, message)`` as well."""

    class OperationalError(Exception):
        pass


@pytest.mark.parametrize("name", ["postgresql", "sqlite", "mysql", "oracle", "cockroachdb"])
def test_the_error_of_a_cancelled_calls_statement_is_its_vendors_cancellation(name):
    """`cancelled_error()` raised under Django's ``wrap_database_errors`` and mapped is a cancellation; a lock error or
    a deadlock is not."""
    current = vendor(name)
    db = SimpleNamespace(Database=_Database)
    error = current.cancelled_error(db)
    wrapped = DjangoOperationalError(*error.args)
    wrapped.__cause__ = error
    assert current.is_cancellation(map_django_error(wrapped, current))
    other = DjangoOperationalError("deadlock detected")
    other.__cause__ = _Database.OperationalError("deadlock detected")
    assert not current.is_cancellation(map_django_error(other, current))
