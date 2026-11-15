import asyncio
import pickle
import socket

from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pydantic
import pytest

from django.db import (
    connection,
    DatabaseError as DjangoDatabaseError,
    IntegrityError as DjangoIntegrityError,
    transaction,
)
from django.db.models import ProtectedError as DjangoProtectedError

import adorm

from adorm.exceptions import (
    AdormError,
    DatabaseError,
    DataError,
    DeadlockError,
    Error,
    IntegrityError,
    InterfaceError,
    InternalError,
    ModelExceptionsMixin,
    MultipleObjectsReturned,
    NotSupportedError,
    ObjectDoesNotExist,
    OperationalError,
    OperationTimeout,
    ProgrammingError,
    ProtectedError,
    RestrictedError,
    SessionTimeout,
    TransactionClosed,
    TransactionManagementError,
    ValidationError,
    wrap_driver_error,
)


class Club(ModelExceptionsMixin):
    pass


class Member(ModelExceptionsMixin):
    pass


class Coach(Member):
    pass


class Named(ModelExceptionsMixin):
    pass


class NamedCoach(Coach, Named):
    pass


class Activity(ModelExceptionsMixin, pydantic.BaseModel):
    kind: str


class CustomError(ObjectDoesNotExist):
    pass


class Custom(ModelExceptionsMixin):
    DoesNotExist = CustomError


# Hierarchy


@pytest.mark.parametrize(
    ("cls", "parent"),
    [
        (Error, AdormError),
        (InterfaceError, Error),
        (DatabaseError, Error),
        (DataError, DatabaseError),
        (OperationalError, DatabaseError),
        (IntegrityError, DatabaseError),
        (ProtectedError, IntegrityError),
        (RestrictedError, IntegrityError),
        (InternalError, DatabaseError),
        (ProgrammingError, DatabaseError),
        (NotSupportedError, DatabaseError),
        (TransactionManagementError, ProgrammingError),
        (TransactionClosed, TransactionManagementError),
        (ObjectDoesNotExist, AdormError),
        (MultipleObjectsReturned, AdormError),
        (ValidationError, AdormError),
        (SessionTimeout, AdormError),
        (DeadlockError, SessionTimeout),
        (OperationTimeout, AdormError),
        (OperationTimeout, TimeoutError),
    ],
)
def test_hierarchy(cls, parent):
    assert issubclass(cls, parent)


def test_interface_error_is_not_a_database_error():
    assert not issubclass(InterfaceError, DatabaseError)


def test_session_timeout_is_not_a_timeout_error():
    assert not issubclass(SessionTimeout, TimeoutError)


@pytest.mark.parametrize(
    "name", sorted(set(adorm.exceptions.__all__) - {"ModelExceptionsMixin", "wrap_driver_error", "TimeoutPhase"})
)
def test_package_exports_exceptions(name):
    assert name in adorm.__all__
    assert getattr(adorm, name) is getattr(adorm.exceptions, name)


# Per-model exceptions


def test_each_model_has_its_own_exceptions():
    assert Club.DoesNotExist is not Member.DoesNotExist
    assert Club.MultipleObjectsReturned is not Member.MultipleObjectsReturned
    assert issubclass(Club.DoesNotExist, ObjectDoesNotExist)
    assert issubclass(Club.MultipleObjectsReturned, MultipleObjectsReturned)
    assert not issubclass(Club.DoesNotExist, Member.DoesNotExist)


def test_model_exception_names():
    assert Club.DoesNotExist.__name__ == "DoesNotExist"
    assert Club.DoesNotExist.__qualname__ == "Club.DoesNotExist"
    assert Club.MultipleObjectsReturned.__qualname__ == "Club.MultipleObjectsReturned"
    assert Club.DoesNotExist.__module__ == __name__


def test_subclass_exceptions_derive_from_parent_exceptions():
    assert issubclass(Coach.DoesNotExist, Member.DoesNotExist)
    assert issubclass(Coach.MultipleObjectsReturned, Member.MultipleObjectsReturned)
    with pytest.raises(Member.DoesNotExist):
        raise Coach.DoesNotExist("no coach")


def test_multiple_parents():
    assert issubclass(NamedCoach.DoesNotExist, Coach.DoesNotExist)
    assert issubclass(NamedCoach.DoesNotExist, Named.DoesNotExist)
    assert issubclass(NamedCoach.DoesNotExist, Member.DoesNotExist)


def test_redundant_parent_exception_is_dropped():
    class Both(Coach, Member):
        pass

    assert Both.DoesNotExist.__bases__ == (Coach.DoesNotExist,)


def test_class_keeps_its_own_definition():
    assert Custom.DoesNotExist is CustomError
    assert issubclass(Custom.MultipleObjectsReturned, MultipleObjectsReturned)


def test_pydantic_model_gets_exceptions():
    assert issubclass(Activity.DoesNotExist, ObjectDoesNotExist)
    assert "DoesNotExist" not in Activity.model_fields
    assert Activity(kind="x").model_dump() == {"kind": "x"}
    assert "DoesNotExist" not in Activity.model_json_schema()["properties"]


def test_model_exception_pickles():
    exc = pickle.loads(pickle.dumps(Member.DoesNotExist("missing")))
    assert type(exc) is Member.DoesNotExist
    assert exc.args == ("missing",)


# Database errors


def test_database_error_carries_original():
    original = RuntimeError("driver broke")
    exc = DatabaseError(original=original, sqlstate="XX000")
    assert exc.original is original
    assert exc.sqlstate == "XX000"
    assert str(exc) == "driver broke"


def test_database_error_message_defaults():
    assert str(DatabaseError()) == "DatabaseError"
    assert str(DatabaseError(original=RuntimeError())) == "RuntimeError"
    assert str(DatabaseError("explicit", original=RuntimeError("x"))) == "explicit"
    assert DatabaseError().original is None


def test_protected_and_restricted_objects():
    assert ProtectedError("refused", protected_objects=[1, 2]).protected_objects == [1, 2]
    assert RestrictedError("refused", restricted_objects=[3]).restricted_objects == [3]
    assert ProtectedError().protected_objects == ()


@pytest.mark.parametrize(
    "exc",
    [
        DatabaseError("boom", original=ValueError("v"), sqlstate="22000"),
        ProtectedError("refused", protected_objects=[1]),
        RestrictedError("refused", restricted_objects=[2]),
        TransactionClosed("late"),
        SessionTimeout(session=("default", "default"), timeout=2),
        DeadlockError(session="a", timeout=1, held_sessions=["b"]),
        OperationTimeout(phase="executing", operation="Get(Member)", timeout=5),
    ],
)
def test_pickle_round_trip(exc):
    restored = pickle.loads(pickle.dumps(exc))
    assert type(restored) is type(exc)
    assert str(restored) == str(exc)
    assert {k: repr(v) for k, v in vars(restored).items()} == {k: repr(v) for k, v in vars(exc).items()}


# wrap_driver_error


class FakeDriverError(Exception):
    def __init__(self, sqlstate=None, pgcode=None):
        super().__init__("fake")
        self.sqlstate = sqlstate
        self.pgcode = pgcode


@pytest.mark.parametrize(
    ("sqlstate", "expected"),
    [
        ("23505", IntegrityError),
        ("23503", IntegrityError),
        ("22001", DataError),
        ("22P02", DataError),
        ("42P01", ProgrammingError),
        ("42501", ProgrammingError),
        ("40001", OperationalError),
        ("40P01", OperationalError),
        ("57014", OperationalError),
        ("08006", OperationalError),
        ("55P03", OperationalError),
        ("0A000", NotSupportedError),
        ("25P02", InternalError),
        ("XX000", InternalError),
        ("P0001", ProgrammingError),
        ("21000", ProgrammingError),
        ("44000", ProgrammingError),
        ("24000", InternalError),
        ("2D000", InternalError),
        ("28P01", OperationalError),
        ("3D000", OperationalError),
        ("F0000", OperationalError),
        ("HV000", OperationalError),
        ("0B000", DatabaseError),
    ],
)
def test_wrap_by_sqlstate(sqlstate, expected):
    original = FakeDriverError(sqlstate=sqlstate)
    wrapped = wrap_driver_error(original)
    assert type(wrapped) is expected
    assert wrapped.original is original
    assert wrapped.__cause__ is original
    assert wrapped.sqlstate == sqlstate


def test_wrap_reads_pgcode():
    assert type(wrap_driver_error(FakeDriverError(pgcode="23505"))) is IntegrityError


def test_wrap_reads_sqlstate_from_cause():
    outer = RuntimeError("wrapped")
    outer.__cause__ = FakeDriverError(sqlstate="22003")
    wrapped = wrap_driver_error(outer)
    assert type(wrapped) is DataError
    assert wrapped.original is outer


def test_wrap_survives_cause_cycle():
    a, b = RuntimeError("a"), RuntimeError("b")
    a.__cause__, b.__cause__ = b, a
    assert type(wrap_driver_error(a)) is DatabaseError


def test_wrap_ignores_malformed_sqlstate():
    assert type(wrap_driver_error(FakeDriverError(sqlstate=23505))) is DatabaseError


@pytest.mark.parametrize(
    "name",
    [
        "DataError",
        "OperationalError",
        "IntegrityError",
        "InternalError",
        "ProgrammingError",
        "NotSupportedError",
        "InterfaceError",
        "DatabaseError",
    ],
)
def test_wrap_by_pep249_name(name):
    driver_cls = type(name, (Exception,), {})
    subclass = type("Specific" + name, (driver_cls,), {})
    assert type(wrap_driver_error(subclass("x"))) is getattr(adorm, name)


def test_sqlstate_wins_over_class_name():
    driver_cls = type("ProgrammingError", (Exception,), {})
    exc = driver_cls("x")
    exc.sqlstate = "23505"
    assert type(wrap_driver_error(exc)) is IntegrityError


def test_unknown_sqlstate_class_wins_over_class_name():
    driver_cls = type("ProgrammingError", (Exception,), {})
    exc = driver_cls("x")
    exc.sqlstate = "0B000"
    assert type(wrap_driver_error(exc)) is DatabaseError


@pytest.mark.parametrize(
    "exc",
    [ConnectionRefusedError("refused"), socket.gaierror("unknown host"), TimeoutError(), OSError("unreachable")],
)
def test_wrap_os_error(exc):
    assert type(wrap_driver_error(exc)) is OperationalError


@pytest.mark.parametrize("exc", [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit(1), GeneratorExit()])
def test_wrap_returns_base_exceptions_unchanged(exc):
    assert wrap_driver_error(exc) is exc


def test_wrap_unknown_error_becomes_database_error():
    original = KeyError("unexpected")
    wrapped = wrap_driver_error(original)
    assert type(wrapped) is DatabaseError
    assert wrapped.original is original
    assert wrapped.sqlstate is None


def test_wrap_returns_adorm_errors_unchanged():
    exc = Member.DoesNotExist("missing")
    assert wrap_driver_error(exc) is exc


async def test_wrap_asyncpg_errors(test_database_url):
    conn = await asyncpg.connect(test_database_url)
    try:
        await conn.execute("CREATE TEMP TABLE item (id int PRIMARY KEY, code varchar(3) NOT NULL)")
        await conn.execute("INSERT INTO item VALUES (1, 'a')")
        cases = [
            ("INSERT INTO item VALUES (1, 'b')", IntegrityError, "23505"),
            ("INSERT INTO item VALUES (2, NULL)", IntegrityError, "23502"),
            ("INSERT INTO item VALUES (3, 'abcd')", DataError, "22001"),
            ("SELECT 1/0", DataError, "22012"),
            ("SELECT * FROM missing_table", ProgrammingError, "42P01"),
        ]
        for sql, expected, sqlstate in cases:
            with pytest.raises(asyncpg.PostgresError) as info:
                await conn.execute(sql)
            wrapped = wrap_driver_error(info.value)
            assert type(wrapped) is expected, sql
            assert wrapped.sqlstate == sqlstate
            assert wrapped.original is info.value
    finally:
        await conn.close()


async def test_wrap_asyncpg_cancelled_statement(test_database_url):
    conn = await asyncpg.connect(test_database_url)
    try:
        await conn.execute("SET statement_timeout = 50")
        with pytest.raises(asyncpg.PostgresError) as info:
            await conn.execute("SELECT pg_sleep(1)")
        assert type(wrap_driver_error(info.value)) is OperationalError
    finally:
        await conn.close()


def _replace_url(url, **parts):
    return urlunsplit(urlsplit(url)._replace(**parts))


async def test_wrap_asyncpg_missing_database(test_database_url):
    with pytest.raises(asyncpg.PostgresError) as info:
        await asyncpg.connect(_replace_url(test_database_url, path="/adorm_test_missing"))
    assert type(wrap_driver_error(info.value)) is OperationalError


async def test_wrap_asyncpg_wrong_password(test_database_url):
    parts = urlsplit(test_database_url)
    netloc = f"{parts.username}:not-the-password@{parts.hostname}:{parts.port or 5432}"
    with pytest.raises(asyncpg.PostgresError) as info:
        await asyncpg.connect(_replace_url(test_database_url, netloc=netloc))
    assert type(wrap_driver_error(info.value)) is OperationalError


RAISE_SQL = "DO $$ BEGIN RAISE EXCEPTION 'refused'; END $$"


async def test_wrap_asyncpg_raise_exception(test_database_url):
    conn = await asyncpg.connect(test_database_url)
    try:
        with pytest.raises(asyncpg.PostgresError) as info:
            await conn.execute(RAISE_SQL)
    finally:
        await conn.close()
    wrapped = wrap_driver_error(info.value)
    assert type(wrapped) is ProgrammingError
    assert wrapped.sqlstate == "P0001"


@pytest.mark.django_db
def test_wrap_django_raise_exception():
    with pytest.raises(DjangoDatabaseError) as info, transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(RAISE_SQL)
    wrapped = wrap_driver_error(info.value)
    assert type(wrapped) is ProgrammingError
    assert wrapped.sqlstate == "P0001"


@pytest.mark.django_db
def test_wrap_django_errors():
    with connection.cursor() as cursor:
        cursor.execute("CREATE TEMP TABLE item (id int PRIMARY KEY)")
        cursor.execute("INSERT INTO item VALUES (1)")
        with pytest.raises(DjangoIntegrityError) as info, transaction.atomic():
            cursor.execute("INSERT INTO item VALUES (1)")
    wrapped = wrap_driver_error(info.value)
    assert type(wrapped) is IntegrityError
    assert wrapped.sqlstate == "23505"


def test_wrap_django_protected_error_by_name():
    assert isinstance(wrap_driver_error(DjangoProtectedError("refused", set())), IntegrityError)


# ValidationError


def test_validation_error_single_message():
    exc = ValidationError("Enter a value.", code="required")
    assert exc.message == "Enter a value."
    assert exc.code == "required"
    assert exc.messages == ["Enter a value."]
    assert exc.error_list == [exc]
    assert str(exc) == "['Enter a value.']"
    assert repr(exc) == "ValidationError(['Enter a value.'])"


def test_validation_error_params():
    exc = ValidationError("At most %(limit)d characters.", code="max_length", params={"limit": 3})
    assert exc.messages == ["At most 3 characters."]
    assert exc.params == {"limit": 3}


def test_validation_error_list():
    exc = ValidationError(["First.", ValidationError("Second.", code="x")])
    assert exc.messages == ["First.", "Second."]
    assert [e.code for e in exc.error_list] == [None, "x"]
    with pytest.raises(AttributeError):
        _ = exc.message_dict


def test_validation_error_dict():
    exc = ValidationError({"name": "Required.", "age": ["Too low.", "Not a number."]})
    assert exc.message_dict == {"name": ["Required."], "age": ["Too low.", "Not a number."]}
    assert exc.messages == ["Required.", "Too low.", "Not a number."]
    assert dict(exc) == exc.message_dict
    assert str(exc) == repr(exc.message_dict)


def test_validation_error_dict_of_errors():
    exc = ValidationError({"name": ValidationError("Required.", code="required")})
    assert exc.error_dict["name"][0].code == "required"


def test_validation_error_list_flattens_dict_errors():
    exc = ValidationError([ValidationError({"name": "Required."}), "Other."])
    assert exc.messages == ["Required.", "Other."]


def test_validation_error_wraps_validation_error():
    single = ValidationError(ValidationError("Bad.", code="bad", params={"a": 1}))
    assert (single.message, single.code, single.params) == ("Bad.", "bad", {"a": 1})
    assert ValidationError(ValidationError(["A.", "B."])).messages == ["A.", "B."]
    assert ValidationError(ValidationError({"f": "C."})).message_dict == {"f": ["C."]}


def test_validation_error_pickles():
    exc = pickle.loads(pickle.dumps(ValidationError({"name": ["Required."]})))
    assert exc.message_dict == {"name": ["Required."]}


# Executor errors


def test_session_timeout():
    exc = SessionTimeout(session=("default", "audit"), timeout=2.5)
    assert exc.session == ("default", "audit")
    assert exc.timeout == 2.5
    assert str(exc) == "no slot became free for session ('default', 'audit') within 2.5 s"
    assert str(SessionTimeout()) == "no slot became free"
    assert str(SessionTimeout("custom")) == "custom"


def test_deadlock_error():
    exc = DeadlockError(session="audit", timeout=1, held_sessions=iter(["default", "reports"]))
    assert exc.held_sessions == ("default", "reports")
    assert exc.session == "audit"
    assert str(exc) == "no slot became free for session 'audit' within 1 s while holding 'default', 'reports'"
    assert DeadlockError().held_sessions == ()
    with pytest.raises(SessionTimeout):
        raise exc


@pytest.mark.parametrize("phase", ["waiting_for_slot", "executing", None])
def test_operation_timeout_phases(phase):
    exc = OperationTimeout(phase=phase, operation="Count(Member)", timeout=5)
    assert exc.phase == phase
    assert exc.operation == "Count(Member)"
    assert exc.timeout == 5


def test_operation_timeout_message():
    exc = OperationTimeout(phase="waiting_for_slot", operation="Count(Member)", timeout=5)
    assert str(exc) == "operation timed out after 5 s while waiting for slot: 'Count(Member)'"
    assert str(OperationTimeout()) == "operation timed out"


def test_operation_timeout_rejects_unknown_phase():
    with pytest.raises(ValueError, match="phase"):
        OperationTimeout(phase="sleeping")


async def test_operation_timeout_is_caught_as_timeout_error():
    async def expire():
        raise OperationTimeout(phase="executing")

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(expire(), 1)


def test_transaction_closed_is_caught_as_database_error():
    with pytest.raises(DatabaseError):
        raise TransactionClosed("transaction has ended")
