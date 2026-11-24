"""The profile hook ``check_operation(operation, session)`` on every driver: it sees every operation a public call
sends, once, in the caller's task and thread, before a slot is taken; what it raises propagates unchanged and nothing
reaches the database; a refusal inside a transaction leaves the transaction usable. ``Operation.models`` names the
models whose tables an operation of the club models reads or writes."""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import inspect
import threading

from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, ClassVar

import pytest

import adorm

from adorm import Count, Exists, NotSupportedError, OuterRef, Subquery, Sum
from adorm.models.registry import registry
from adorm.operations import _OPERATIONS, Operation
from adorm.query.query import NESTED_PATH
from tests.clubmodels.activities import Activity, Attendance, Competition, Tag
from tests.clubmodels.billing import Invoice
from tests.clubmodels.clubs import Club, Member, Membership

pytestmark = pytest.mark.django_db(transaction=True)

CLUB = "clubs.Club"
MEMBER = "clubs.Member"
MEMBERSHIP = "clubs.Membership"
ACTIVITY = "activities.Activity"
COMPETITION = "activities.Competition"
ATTENDANCE = "activities.Attendance"
TAG = "activities.Tag"
ACTIVITY_TAGS = "activities.Activity_tags"
INVOICE = "billing.Invoice"
INVOICE_LINE = "billing.InvoiceLine"
PAYMENT = "billing.Payment"

KOPER = "koper-sailing"


class RefusedError(Exception):
    """What the test hook raises for an operation it refuses."""


_refuse: ContextVar[Callable[[Operation, adorm.Session], bool] | None] = ContextVar("refuse", default=None)
_mark: ContextVar[str | None] = ContextVar("mark", default=None)


@contextmanager
def refusing(predicate: Callable[[Operation, adorm.Session], bool] = lambda _operation, _session: True):
    """The test hook refuses the operations ``predicate`` selects that are sent from this context."""
    token = _refuse.set(predicate)
    try:
        yield
    finally:
        _refuse.reset(token)


def creates(operation: Operation, _session: adorm.Session) -> bool:
    return operation.kind == "create"


@dataclass(frozen=True)
class HookCall:
    operation: Operation
    session: adorm.Session
    thread: int
    mark: str | None


class Hook:
    """A ``check_operation`` hook that records every call and raises a new `RefusedError` for an operation that the
    caller's `refusing()` selects."""

    def __init__(self) -> None:
        self.calls: list[HookCall] = []
        self.raised: list[RefusedError] = []

    def __call__(self, operation: Operation, session: adorm.Session) -> None:
        self.calls.append(HookCall(operation, session, threading.get_ident(), _mark.get()))
        predicate = _refuse.get()
        if predicate is not None and predicate(operation, session):
            error = RefusedError(operation.kind)
            self.raised.append(error)
            raise error

    @property
    def kinds(self) -> list[str]:
        return [call.operation.kind for call in self.calls]


@pytest.fixture
def hook():
    return Hook()


@pytest.fixture
def acquired():
    """The sessions the profile's ``on_acquire`` hook saw, in order."""
    return []


@pytest.fixture
async def guarded(make_router, hook, acquired):
    """The pool of the profile ``default`` with the test hook as ``check_operation`` and a recording ``on_acquire``."""

    def on_acquire(connection, profile, session):  # noqa: ARG001 - the hook's signature
        acquired.append(session)

    profile = {"check_operation": hook, "on_acquire": on_acquire}
    return make_router(profile=profile, max_connections=2, acquire_timeout=5).pools[0]


async def listed(queryset) -> list[Any]:
    return [row async for row in queryset]


async def models_of(hook: Hook, make: Callable[[], Any]) -> frozenset[str] | None:
    """``Operation.models`` of the one operation ``make()`` sends, which the hook refuses before it reaches the
    database."""
    hook.calls.clear()
    with refusing(), pytest.raises(RefusedError):
        await make()
    (call,) = hook.calls
    return call.operation.models


@pytest.fixture
def third_party():
    """An operation class of a third-party driver, which no driver of the suite declares; its kind is registered in
    `OPERATIONS` for the test only."""

    @dataclass(frozen=True)
    class ThirdParty(Operation):
        kind: ClassVar[str] = "test_check_operation_third_party"
        model: str = CLUB

    yield ThirdParty
    del _OPERATIONS[ThirdParty.kind]


def supports(driver, kind: str) -> bool:
    return kind in driver.capabilities.operations


def follows_relations(driver) -> bool:
    """Whether the driver supports field paths through relations (`NESTED_PATH`)."""
    return NESTED_PATH in driver.capabilities.constructs


# Every operation reaches the hook once


async def test_every_operation_of_a_public_call_reaches_the_hook_once(driver, guarded, hook, recorded):  # noqa: ARG001
    session = adorm.current_session()

    async def step(kind: str | None, make: Callable[[], Any]) -> Any:
        hook.calls.clear()
        recorded.clear()
        result = await make()
        assert len(recorded) >= 1
        assert [call.operation for call in hook.calls] == recorded
        assert all(call.operation is operation for call, operation in zip(hook.calls, recorded, strict=True))
        assert all(call.session is session for call in hook.calls)
        if kind is not None:
            assert hook.kinds == [kind]
        return result

    clubs = Club.objects.order_by("slug")
    assert await step("count", lambda: Club.objects.count()) > 0
    assert await step("select", lambda: listed(clubs))
    koper = await step("get", lambda: Club.objects.get(slug=KOPER))
    await step("first", clubs.first)
    await step("last", clubs.last)
    assert await step("exists", Club.objects.filter(slug=KOPER).exists)
    assert list(await step("in_bulk", lambda: Club.objects.in_bulk([koper.pk]))) == [koper.pk]
    if supports(driver, "aggregate"):
        await step("aggregate", lambda: Membership.objects.aggregate(total=Sum("fee")))
    tag = await step("create", lambda: Tag.objects.create(name="hooked"))
    await step("bulk_create", lambda: Tag.objects.bulk_create([Tag(name="hooked-1"), Tag(name="hooked-2")]))
    tag.name = "hooked-renamed"
    await step("save", tag.save)
    membership = await Membership.objects.filter(club_id=koper.pk).order_by("id").first()
    if supports(driver, "full_clean"):
        await step("full_clean", membership.full_clean)
    assert await step("update", lambda: Tag.objects.filter(name="hooked-1").update(name="hooked-one")) == 1
    await step("delete", Tag.objects.filter(name="hooked-2").delete)
    await step("delete_instance", tag.delete)
    await step("get_or_create", lambda: Tag.objects.get_or_create(name="hooked-got"))
    await step("update_or_create", lambda: Tag.objects.update_or_create(name="hooked-got", defaults={}))
    await step(None, koper.refresh_from_db)
    assert (await step(None, lambda: membership.club)).slug == KOPER
    if supports(driver, "check_table"):
        await step("check_table", lambda: adorm.check_schema([Club]))


async def test_the_hook_receives_the_session_as_given(guarded, hook):  # noqa: ARG001
    tenant = adorm.Session("default", "tenant_a")
    assert await tenant.bind(Club).filter(slug=KOPER).count() == 1
    (call,) = hook.calls
    assert call.session is tenant
    assert (call.session.using, call.session.session_name) == ("default", "tenant_a")
    hook.calls.clear()
    with tenant.activate():
        await Club.objects.count()
    assert [call.session for call in hook.calls] == [tenant]


# A refusal


async def test_a_refused_operation_raises_the_hooks_error_and_takes_no_slot(guarded, hook, acquired):
    koper = await Club.objects.get(slug=KOPER)
    clubs_before = await Club.objects.count()
    memberships_before = await Membership.objects.filter(club_id=koper.pk).count()
    acquired.clear()
    size = guarded.size
    refused_calls = [
        lambda: Club.objects.count(),
        lambda: listed(Club.objects.all()),
        lambda: Club.objects.create(name="Refused", slug="refused", founded=datetime.date(2020, 1, 1), city="Nowhere"),
        lambda: Club.objects.bulk_create(
            [Club(name="Refused", slug="refused-bulk", founded=datetime.date(2020, 1, 1), city="Nowhere")]
        ),
        lambda: Club.objects.filter(slug=KOPER).update(city="Refused"),
        lambda: Club.objects.filter(slug=KOPER).delete(),
        koper.delete,
    ]
    for make in refused_calls:
        hook.raised.clear()
        with refusing(), pytest.raises(RefusedError) as raised:
            await make()
        (error,) = hook.raised
        assert raised.value is error
    assert acquired == []
    assert (guarded.size, guarded.in_use) == (size, 0)
    assert await Club.objects.count() == clubs_before
    assert await Club.objects.filter(slug__in=["refused", "refused-bulk"]).count() == 0
    assert (await Club.objects.get(slug=KOPER)).city == "Koper"
    assert await Membership.objects.filter(club_id=koper.pk).count() == memberships_before
    assert acquired, "allowed operations acquire a slot again"
    assert guarded.in_use == 0


async def test_a_hook_that_returns_an_awaitable_is_refused_and_nothing_runs(make_router, acquired):
    returned = []

    async def later():
        return None

    def returns_a_coroutine(operation, session):  # noqa: ARG001 - the hook's signature
        coroutine = later()
        returned.append(coroutine)
        return coroutine

    def on_acquire(connection, profile, session):  # noqa: ARG001 - the hook's signature
        acquired.append(session)

    pool = make_router(
        profile={"check_operation": returns_a_coroutine, "on_acquire": on_acquire}, max_connections=2, acquire_timeout=5
    ).pools[0]
    with pytest.raises(TypeError, match="check_operation of profile .default. returned an awaitable"):
        await Tag.objects.create(name="never")
    (coroutine,) = returned
    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED, "closed: no 'never awaited' warning"
    assert acquired == []
    assert (pool.size, pool.in_use) == (0, 0)


async def test_an_operation_adorm_cannot_know_has_no_models_and_the_hook_decides(driver, guarded, hook, third_party):  # noqa: ARG001
    session = adorm.current_session()
    with refusing(lambda operation, _session: operation.models is None), pytest.raises(RefusedError):
        await session.execute(third_party())
    assert hook.calls[-1].operation.models is None
    with pytest.raises(NotSupportedError):
        await session.execute(third_party())
    assert len(hook.calls) == 2, "the hook runs before the driver refuses an operation it does not declare"


# Inside a transaction


async def test_a_refusal_inside_a_transaction_leaves_it_usable(driver, guarded, hook):
    koper = Club.objects.filter(slug=KOPER)

    async def create_tag(name):
        return await Tag.objects.create(name=name)

    async with adorm.atomic():
        with refusing(), pytest.raises(RefusedError) as raised:
            await create_tag("refused")
        assert raised.value is hook.raised[-1]
        assert await koper.update(city="After refusal") == 1
        with refusing(creates), pytest.raises(RefusedError):
            await asyncio.create_task(create_tag("refused-in-task"))
        await asyncio.create_task(create_tag("kept-in-task"))
        if driver.capabilities.savepoints:
            async with adorm.atomic():
                with refusing(), pytest.raises(RefusedError):
                    await create_tag("refused-in-savepoint")
                await create_tag("kept-in-savepoint")
        await create_tag("kept")
    assert (await koper.get()).city == "After refusal"
    assert await Tag.objects.filter(name__in=["refused", "refused-in-task", "refused-in-savepoint"]).count() == 0
    kept = {"kept", "kept-in-task"} | ({"kept-in-savepoint"} if driver.capabilities.savepoints else set())
    created = Tag.objects.filter(name__in=["kept", "kept-in-task", "kept-in-savepoint"])
    assert {tag.name async for tag in created} == kept
    assert guarded.in_use == 0


async def test_a_refusal_that_leaves_a_transaction_rolls_it_back(guarded):
    koper = Club.objects.filter(slug=KOPER)

    async def refused_in_block():
        async with adorm.atomic():
            assert await koper.update(city="Rolled back") == 1
            with refusing():
                await Tag.objects.create(name="refused")

    with pytest.raises(RefusedError):
        await refused_in_block()
    assert (await koper.get()).city == "Koper"
    assert guarded.in_use == 0


# Context and thread


async def test_the_hook_runs_in_the_callers_task_and_thread(guarded, hook):  # noqa: ARG001
    async def flow(mark):
        _mark.set(mark)
        await Club.objects.count()
        async with adorm.atomic():
            await Club.objects.filter(slug=KOPER).exists()
        return threading.get_ident()

    threads = set(await asyncio.gather(flow("a"), flow("b")))
    assert sorted(call.mark for call in hook.calls) == ["a", "a", "b", "b"]
    assert {call.thread for call in hook.calls} == threads == {threading.get_ident()}


# The start-up schema check


async def test_the_start_up_schema_check_does_not_reach_the_hook(driver, make_router, hook, monkeypatch):
    if not supports(driver, "check_table"):
        pytest.skip(f"the {driver.name} driver does not check tables")
    monkeypatch.setattr(registry, "_models", {model._meta.label: model for model in (Club, Member, Membership)})
    make_router(profile={"check_operation": hook}, max_connections=2, acquire_timeout=5, check_schema="warn")
    assert await Club.objects.filter(slug=KOPER).count() == 1
    assert hook.kinds == ["count"]
    assert await adorm.check_schema([Club]) == ()
    assert hook.kinds == ["count", "check_table"]


# Operation.models


async def test_models_of_relation_paths(guarded, hook):  # noqa: ARG001
    cases = [
        (lambda: Membership.objects.filter(club__city="Koper").count(), {MEMBERSHIP, CLUB}),
        (lambda: Club.objects.filter(memberships__role="coach").count(), {CLUB, MEMBERSHIP}),
        (lambda: Club.objects.exclude(invoices__status="paid").count(), {CLUB, INVOICE}),
        (lambda: Club.objects.filter(members__level="advanced").count(), {CLUB, MEMBER, MEMBERSHIP}),
        (lambda: Member.objects.filter(clubs__city="Koper").count(), {MEMBER, CLUB, MEMBERSHIP}),
        (lambda: Activity.objects.filter(tags__name="sea").count(), {ACTIVITY, TAG, ACTIVITY_TAGS}),
        (lambda: Tag.objects.filter(activities__club__city="Koper").count(), {TAG, ACTIVITY_TAGS, ACTIVITY, CLUB}),
        (lambda: listed(Membership.objects.order_by("member__last_name")), {MEMBERSHIP, MEMBER}),
        (lambda: listed(Membership.objects.values("club__name", "member__email")), {MEMBERSHIP, CLUB, MEMBER}),
        (
            lambda: listed(Attendance.objects.values_list("activity__club__city", flat=True)),
            {ATTENDANCE, ACTIVITY, CLUB},
        ),
        (lambda: listed(Club.objects.annotate(n=Count("activities"))), {CLUB, ACTIVITY}),
        (lambda: listed(Club.objects.distinct()), {CLUB}),
        (lambda: Club.objects.get(slug=KOPER), {CLUB}),
        (
            lambda: Club.objects.filter(activities__competition__discipline="Laser").exists(),
            {
                CLUB,
                ACTIVITY,
                COMPETITION,
            },
        ),
    ]
    for make, expected in cases:
        assert await models_of(hook, make) == frozenset(expected)


async def test_models_of_aggregations_over_relations(guarded, hook):  # noqa: ARG001
    assert await models_of(hook, lambda: Club.objects.aggregate(total=Sum("invoices__total"))) == {CLUB, INVOICE}
    assert await models_of(hook, lambda: Member.objects.aggregate(n=Count("activities"))) == {
        MEMBER,
        ACTIVITY,
        ATTENDANCE,
    }


async def test_models_of_subqueries(guarded, hook):  # noqa: ARG001
    first_email = Subquery(Membership.objects.filter(club=OuterRef("pk")).order_by("since").values("member__email")[:1])
    assert await models_of(hook, lambda: listed(Club.objects.annotate(first_email=first_email))) == {
        CLUB,
        MEMBERSHIP,
        MEMBER,
    }
    by_club_name = Invoice.objects.filter(number=OuterRef("activity__club__name"))
    assert await models_of(hook, lambda: Attendance.objects.filter(Exists(by_club_name)).count()) == {
        ATTENDANCE,
        INVOICE,
        ACTIVITY,
        CLUB,
    }
    attended = Attendance.objects.filter(activity__kind="training").values("member_id")
    assert await models_of(hook, lambda: listed(Member.objects.filter(pk__in=attended))) == {
        MEMBER,
        ATTENDANCE,
        ACTIVITY,
    }
    union = Club.objects.filter(members__level="advanced").union(Club.objects.filter(activities__kind="social"))
    assert await models_of(hook, lambda: listed(union)) == {CLUB, MEMBER, MEMBERSHIP, ACTIVITY}


async def test_models_of_select_related(guarded, hook):  # noqa: ARG001
    assert await models_of(hook, lambda: listed(Attendance.objects.select_related("activity__club"))) == {
        ATTENDANCE,
        ACTIVITY,
        CLUB,
    }
    # Without arguments: the non-null foreign keys, so not the activity's coach.
    assert await models_of(hook, lambda: listed(Attendance.objects.select_related())) == {
        ATTENDANCE,
        ACTIVITY,
        MEMBER,
        CLUB,
    }
    assert await models_of(hook, lambda: listed(Activity.objects.select_related())) == {ACTIVITY, CLUB}


async def test_models_of_a_multi_table_inheritance_child_include_its_parent(guarded, hook):  # noqa: ARG001
    assert await models_of(hook, lambda: Competition.objects.count()) == {COMPETITION, ACTIVITY}
    assert await models_of(hook, lambda: Competition.objects.filter(club__city="Koper").count()) == {
        COMPETITION,
        ACTIVITY,
        CLUB,
    }
    assert await models_of(hook, lambda: Competition.objects.filter(discipline="Laser").update(capacity=3)) == {
        COMPETITION,
        ACTIVITY,
    }


async def test_models_of_writes(driver, guarded, hook):  # noqa: ARG001
    koper = await Club.objects.get(slug=KOPER)
    member = await Member.objects.order_by("id").first()
    assert await models_of(hook, lambda: Tag.objects.create(name="never")) == {TAG}
    assert await models_of(hook, lambda: Tag.objects.bulk_create([Tag(name="never")])) == {TAG}
    assert await models_of(hook, koper.save) == {CLUB}
    assert await models_of(hook, lambda: Membership.objects.filter(club__slug=KOPER).update(role="coach")) == {
        MEMBERSHIP,
        CLUB,
    }
    assert await models_of(hook, lambda: Tag.objects.get_or_create(name="never")) == {TAG}
    assert await models_of(hook, lambda: Tag.objects.update_or_create(name="never", defaults={})) == {TAG}

    def competition():
        return Competition.objects.create(
            club_id=koper.pk,
            name="Never",
            starts_at=datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC),
            duration=datetime.timedelta(hours=2),
            capacity=10,
            discipline="Laser",
        )

    assert await models_of(hook, competition) == {COMPETITION, ACTIVITY}
    membership = Membership(club_id=koper.pk, member_id=member.pk, since=datetime.date(2026, 1, 1), fee=Decimal(0))
    assert await models_of(hook, membership.full_clean) == {MEMBERSHIP, CLUB, MEMBER}


async def test_deletes_reach_the_models_adorm_generate_found(guarded, hook):  # noqa: ARG001
    koper = await Club.objects.get(slug=KOPER)
    reached = {CLUB, MEMBERSHIP, ACTIVITY, ACTIVITY_TAGS, ATTENDANCE, COMPETITION, INVOICE, INVOICE_LINE, PAYMENT}
    assert koper._meta.delete_models == reached
    assert await models_of(hook, Club.objects.filter(slug=KOPER).delete) == reached
    assert await models_of(hook, koper.delete) == reached


async def test_deletes_of_models_written_by_hand_have_no_models(guarded, hook, monkeypatch):  # noqa: ARG001
    monkeypatch.setattr(Club, "_meta", dataclasses.replace(Club._meta, schema_hash=None, delete_models=None))
    koper = await Club.objects.get(slug=KOPER)
    assert koper._meta.delete_models is None
    assert await models_of(hook, Club.objects.filter(slug=KOPER).delete) is None
    assert await models_of(hook, koper.delete) is None


async def test_each_prefetch_query_is_an_operation_of_its_own(driver, guarded, hook):  # noqa: ARG001
    def models_per_call(start: int = 0) -> list[list[str]]:
        return sorted(sorted(call.operation.models) for call in hook.calls[start:])

    clubs = await listed(Club.objects.filter(slug=KOPER).prefetch_related("members"))
    assert [club.slug for club in clubs] == [KOPER]
    assert models_per_call() == sorted([[CLUB], [MEMBERSHIP], [MEMBER]])
    hook.calls.clear()
    await listed(Club.objects.filter(slug=KOPER).prefetch_related("memberships"))
    assert models_per_call() == sorted([[CLUB], [MEMBERSHIP]])
    if follows_relations(driver):
        hook.calls.clear()
        await listed(Membership.objects.filter(club__slug=KOPER).prefetch_related("club"))
        assert models_per_call() == sorted([[CLUB], [CLUB, MEMBERSHIP]])


async def test_a_refused_prefetch_query_fails_the_whole_call(guarded, hook):  # noqa: ARG001
    got = None
    with refusing(lambda operation, _session: operation.models == {MEMBER}), pytest.raises(RefusedError):
        got = await listed(Club.objects.filter(slug=KOPER).prefetch_related("members"))
    assert got is None
    assert [call.operation.models for call in hook.calls][-1] == {MEMBER}


# A tenant guard as the schema-per-tenant how-to builds it

TENANT_TABLES = frozenset({MEMBER, MEMBERSHIP, ACTIVITY, COMPETITION, ATTENDANCE, TAG, ACTIVITY_TAGS, INVOICE})
"""The club models' tables that the guard treats as a tenant's; ``clubs.Club`` is shared."""


class NoTenantError(Exception):
    """A tenant's table without a tenant."""


def tenant_guard(operation, session):
    if session.session_name.startswith("tenant_"):
        return
    models = operation.models
    if models is None or models & TENANT_TABLES:
        raise NoTenantError(f"{operation.kind} without a tenant")


async def test_the_tenant_guard_refuses_tenant_tables_without_a_tenant(driver, make_router, acquired):
    def on_acquire(connection, profile, session):  # noqa: ARG001 - the hook's signature
        acquired.append(session)

    pool = make_router(
        profile={"check_operation": tenant_guard, "on_acquire": on_acquire}, max_connections=2, acquire_timeout=5
    ).pools[0]
    tenant = adorm.Session("default", "tenant_a")
    assert await tenant.bind(Member).count() > 0
    if follows_relations(driver):
        assert await tenant.bind(Club).filter(members__level="advanced").exists()
    assert await Club.objects.filter(slug=KOPER).count() == 1
    acquired.clear()
    for make in (
        lambda: Member.objects.count(),
        lambda: Club.objects.filter(members__level="advanced").exists(),
        lambda: listed(Club.objects.annotate(n=Count("activities"))),
        lambda: Club.objects.filter(slug=KOPER).delete(),
    ):
        with pytest.raises(NoTenantError):
            await make()
    assert acquired == []
    # The clubs' query reads only the shared table and runs; the guard refuses the memberships' prefetch query.
    with pytest.raises(NoTenantError):
        await listed(Club.objects.filter(slug=KOPER).prefetch_related("memberships"))
    assert acquired == [adorm.current_session()]
    assert pool.in_use == 0
    assert await Club.objects.filter(slug=KOPER).count() == 1
