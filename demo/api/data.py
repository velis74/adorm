"""The demo's executed endpoints: the generated models read and write through sessions and transactions on the
executor, with the active driver.

Each endpoint runs in the session ``("default", <X-Adorm-Session header>)`` and answers `Traced`: its result, the
session and the calls the driver received (recorded by the memory driver; empty with another driver). Executor and
lock errors answer `ExecutorError` (`install_error_handlers()`): 503 when no slot becomes free in time, 409 when the
database detects a deadlock.
"""

from __future__ import annotations

import asyncio
import datetime
import decimal

from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Generic, TypeVar

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import adorm

from adorm.settings import default_router
from demo.api import memory, operations
from demo.api.schemas import Id, Level, MemberChanges, PaymentIn, Slug
from demo.models.activities import Activity, Attendance
from demo.models.billing import Invoice, Payment
from demo.models.clubs import Club, Member, Membership

SESSION_HEADER = "X-Adorm-Session"
SessionName = Annotated[
    str,
    Header(
        alias=SESSION_HEADER,
        pattern=r"^[-\w]{1,40}$",
        description="Name of the session (`session_name`) the request runs in; the session's `using` is `default`.",
    ),
]

T = TypeVar("T")


class Call(BaseModel):
    """A call the driver received."""

    slot: int = Field(description="Number of the slot's connection, from 1 in the order the connections opened.")
    session: str | None = Field(description="`using/session_name` of the session the slot was assigned to.")
    call: str = Field(description="`acquire`, `begin`, `<operation kind> <model label>`, `savepoint <name>` …")


class Traced(BaseModel, Generic[T]):
    """The result of an executed endpoint and how it ran."""

    result: T = Field(description="What the endpoint read or wrote.")
    session: str = Field(description="`using/session_name` of the session the request ran in.")
    calls: list[Call] = Field(description="The calls the driver received, in order; empty unless it records them.")


class Enrolment(BaseModel):
    """A new member joining a club."""

    model_config = {"extra": "forbid"}

    first_name: str = Field(max_length=60, description="First name.")
    last_name: str = Field(max_length=60, description="Last name.")
    email: str = Field(max_length=254, description="E-mail address.")
    level: Level = Field("beginner", description="Skill level.")
    fee: decimal.Decimal = Field(ge=0, max_digits=8, decimal_places=2, examples=["120.00"], description="Yearly fee.")


class Enrolled(BaseModel):
    """The member and membership an enrolment created."""

    member: Member = Field(description="The new member.")
    membership: Membership = Field(description="The new member's membership of the club.")


class Registrations(BaseModel):
    """Members to register for an activity."""

    model_config = {"extra": "forbid"}

    member_ids: list[int] = Field(min_length=1, max_length=50, description="Primary keys of the members.")


class Registered(BaseModel):
    """The outcome of registering members for an activity."""

    registered: list[int] = Field(description="Members registered by this request.")
    skipped: dict[int, str] = Field(
        description=(
            "Members not registered, with the reason: `unknown member`, `already registered` (the member has an "
            "attendance of the activity) or `full`."
        )
    )


class InvoiceDetail(BaseModel):
    """An invoice with its payments."""

    invoice: Invoice = Field(description="The invoice.")
    payments: list[Payment] = Field(description="Its payments, oldest first.")


class ClubSummary(BaseModel):
    """Counts of one club, read by three concurrent tasks."""

    club: Club = Field(description="The club.")
    active_memberships: int = Field(description="Its active memberships.")
    activities: int = Field(description="Its activities.")
    open_invoices: int = Field(description="Its open invoices.")


class ExecutorError(BaseModel):
    """An error answer. Executor errors fill every field; other errors (no active driver, an invoice that is not
    open) answer `detail` only."""

    detail: str = Field(description="The error message.")
    error: str | None = Field(
        None, description="`SessionTimeout`, `DeadlockError` or `OperationalError`; absent in a `{detail}` answer."
    )
    session: str | None = Field(None, description="`using/session_name` of the session that waited, if any.")
    held_sessions: list[str] = Field(
        default_factory=list,
        description="`DeadlockError`: the sessions whose slots the waiting flow held; empty otherwise.",
    )


class OutboxMessage(BaseModel):
    """A message queued by an `on_commit` callback."""

    queued_at: datetime.datetime = Field(description="When the transaction committed and the callback ran.")
    text: str = Field(description="The message.")


class PoolState(BaseModel):
    """The pool of one connection profile."""

    using: str = Field(description="The profile's name.")
    limit: int | None = Field(description="The most slots the pool opens; `null` for no limit.")
    size: int = Field(description="Slots in the pool, opening ones included.")
    idle: int = Field(description="Slots not assigned to a session.")
    in_use: int = Field(description="Slots assigned to a session or returning to the pool.")
    waiting: int = Field(description="Requests waiting for a slot.")


class ExecutorState(BaseModel):
    """The executor of the API process."""

    driver: str | None = Field(description="The active driver; `null` when there is none.")
    pools: list[PoolState] = Field(description="The pools the executor has created, one per profile used so far.")


OUTBOX: deque[OutboxMessage] = deque(maxlen=50)
"""Messages queued by committed enrolments, newest last."""


def require_driver(request: Request) -> str:
    """The active driver's name; 503 when there is none."""
    driver = request.app.state.driver
    if driver is None:
        raise HTTPException(503, "no driver is active: the data endpoints execute operations on one")
    return driver


router = APIRouter(prefix="/api/data", tags=["data"], dependencies=[Depends(require_driver)])
executor_router = APIRouter(tags=["demo"])

NOT_FOUND = {404: {"description": "No row has that primary key or slug."}}
NO_DRIVER = {503: {"description": "No driver is active."}}
EXECUTOR_ERRORS = {
    409: {"model": ExecutorError, "description": "The database detected a deadlock (SQLSTATE `40P01`); retry."},
    503: {
        "model": ExecutorError,
        "description": (
            "No driver is active (body `{detail}`), or no slot became free within the pool's `acquire_timeout` "
            "(`SessionTimeout`; `DeadlockError` when the flow held slots while waiting)."
        ),
    },
}
"""Error responses of every endpoint that executes operations; extends `NO_DRIVER`."""


def _session_name(session: object) -> str | None:
    if session is None:
        return None
    return f"{getattr(session, 'using', '?')}/{getattr(session, 'session_name', '?')}"


async def _session_timeout(_request: Request, exc: adorm.SessionTimeout) -> JSONResponse:
    body = ExecutorError(
        detail=str(exc),
        error=type(exc).__name__,
        session=_session_name(exc.session),
        held_sessions=[_session_name(held) or "" for held in getattr(exc, "held_sessions", ())],
    )
    return JSONResponse(body.model_dump(), status_code=503)


async def _operational_error(_request: Request, exc: adorm.OperationalError) -> JSONResponse:
    if exc.sqlstate != "40P01":
        raise exc
    body = ExecutorError(detail=str(exc), error=type(exc).__name__)
    return JSONResponse(body.model_dump(), status_code=409)


def install_error_handlers(app: FastAPI) -> None:
    """Answer `adorm.SessionTimeout` (and `adorm.DeadlockError`) with 503 and a deadlock (`adorm.OperationalError`
    with SQLSTATE ``40P01``) with 409, both with an `ExecutorError` body."""
    app.add_exception_handler(adorm.SessionTimeout, _session_timeout)
    app.add_exception_handler(adorm.OperationalError, _operational_error)


@contextmanager
def run(session_name: str) -> Iterator[list[memory.TraceEntry]]:
    """Make ``("default", session_name)`` the current session and collect the driver's calls."""
    session = adorm.create_session("default", session_name)
    with session.activate(), memory.tracing() as calls:
        yield calls


def traced(result: T, session_name: str, calls: list[memory.TraceEntry]) -> Traced[T]:
    return Traced(
        result=result,
        session=f"default/{session_name}",
        calls=[Call(slot=entry.slot, session=entry.session, call=entry.call) for entry in calls],
    )


def not_found(exc: adorm.ObjectDoesNotExist) -> HTTPException:
    return HTTPException(404, str(exc))


@router.get("/members", summary="List members", responses=EXECUTOR_ERRORS)
async def list_members(
    session_name: SessionName = "default",
    level: Annotated[Level | None, Query(description="Skill level.")] = None,
    search: Annotated[str | None, Query(description="Fragment of the first or last name, any case.")] = None,
    ordering: Annotated[operations.MemberOrdering, Query(description="Sort order; `-` reverses.")] = "name",
    offset: Annotated[int, Query(ge=0, description="Rows to skip.")] = 0,
    limit: Annotated[int, Query(ge=1, le=100, description="Page size.")] = 20,
) -> Traced[list[Member]]:
    """A page of members: one `select`, run on a slot acquired for it and released after it."""
    with run(session_name) as calls:
        members = Member.objects.all()
        if level is not None:
            members = members.filter(level=level)
        if search:
            members = members.filter(adorm.Q(first_name__icontains=search) | adorm.Q(last_name__icontains=search))
        members = members.order_by(*operations.MEMBER_ORDERING[ordering])[offset : offset + limit]
        result = [member async for member in members]
    return traced(result, session_name, calls)


@router.get("/members/{pk}", summary="Get a member", responses=NOT_FOUND | EXECUTOR_ERRORS)
async def get_member(pk: Id, session_name: SessionName = "default") -> Traced[Member]:
    """One member, with `get`."""
    with run(session_name) as calls:
        try:
            member = await Member.objects.get(pk=pk)
        except Member.DoesNotExist as exc:
            raise not_found(exc) from None
    return traced(member, session_name, calls)


@router.patch("/members/{pk}", summary="Change a member", responses=NOT_FOUND | EXECUTOR_ERRORS)
async def change_member(pk: Id, changes: MemberChanges, session_name: SessionName = "default") -> Traced[Member]:
    """Load the member, assign the fields in the body and `save()` only those (`update_fields`), in one
    transaction."""
    values = changes.model_dump(exclude_unset=True)
    with run(session_name) as calls:
        async with adorm.atomic():
            try:
                member = await Member.objects.get(pk=pk)
            except Member.DoesNotExist as exc:
                raise not_found(exc) from None
            for name, value in values.items():
                setattr(member, name, value)
            await member.save(update_fields=tuple(values))
    return traced(member, session_name, calls)


@router.post(
    "/clubs/{slug}/members", summary="Enrol a new member", status_code=201, responses=NOT_FOUND | EXECUTOR_ERRORS
)
async def enrol_member(slug: Slug, enrolment: Enrolment, session_name: SessionName = "default") -> Traced[Enrolled]:
    """Create a member and their membership of the club in one transaction. A welcome message goes to the outbox
    when the transaction commits (`on_commit`)."""
    with run(session_name) as calls:
        async with adorm.atomic() as tx:
            try:
                club = await Club.objects.get(slug=slug)
            except Club.DoesNotExist as exc:
                raise not_found(exc) from None
            member = await Member.objects.create(
                first_name=enrolment.first_name,
                last_name=enrolment.last_name,
                email=enrolment.email,
                level=enrolment.level,
                joined=datetime.datetime.now(datetime.UTC),
            )
            membership = await Membership.objects.create(
                club_id=club.id, member_id=member.id, since=datetime.date.today(), fee=enrolment.fee
            )
            text = f"Welcome to {club.name}, {member.first_name}!"
            tx.on_commit(lambda: OUTBOX.append(OutboxMessage(queued_at=datetime.datetime.now(datetime.UTC), text=text)))
    return traced(Enrolled(member=member, membership=membership), session_name, calls)


@router.get("/invoices/{pk}", summary="Get an invoice and its payments", responses=NOT_FOUND | EXECUTOR_ERRORS)
async def get_invoice(pk: Id, session_name: SessionName = "default") -> Traced[InvoiceDetail]:
    """The invoice with `get`, its payments through the reverse relation `Invoice.payments`."""
    with run(session_name) as calls:
        try:
            invoice = await Invoice.objects.get(pk=pk)
        except Invoice.DoesNotExist as exc:
            raise not_found(exc) from None
        payments = [payment async for payment in invoice.payments.order_by("paid_at")]
    return traced(InvoiceDetail(invoice=invoice, payments=payments), session_name, calls)


@router.post(
    "/invoices/{pk}/payments",
    summary="Pay an invoice",
    status_code=201,
    responses=NOT_FOUND
    | EXECUTOR_ERRORS
    | {
        409: {
            "model": ExecutorError,
            "description": (
                "The invoice is not open and nothing is stored (body `{detail}`), or the database detected a "
                "deadlock (SQLSTATE `40P01`); retry."
            ),
        }
    },
)
async def pay_invoice(pk: Id, payment: PaymentIn, session_name: SessionName = "default") -> Traced[Payment]:
    """Store the payment and mark the invoice paid in one transaction. The update matches only an open invoice; when
    it matches none, the block raises and the payment is rolled back."""
    with run(session_name) as calls:
        async with adorm.atomic():
            if not await Invoice.objects.filter(pk=pk).exists():
                raise HTTPException(404, f"no invoice has the primary key {pk}")
            stored = await Payment.objects.create(
                invoice_id=pk, amount=payment.amount, reference=payment.reference, paid_at=payment.paid_at
            )
            if not await Invoice.objects.filter(pk=pk, status="open").update(status="paid"):
                raise HTTPException(409, f"invoice {pk} is not open")
    return traced(stored, session_name, calls)


@router.post("/activities/{pk}/registrations", summary="Register members", responses=NOT_FOUND | EXECUTOR_ERRORS)
async def register_members(
    pk: Id, registrations: Registrations, session_name: SessionName = "default"
) -> Traced[Registered]:
    """Register members for the activity in one transaction, each in a nested `atomic()` block (a savepoint): a
    registration that would exceed the capacity is rolled back to its savepoint, the others stay. The transaction
    first updates the activity's row to itself, which locks it: concurrent registrations for the activity run one
    after another."""
    registered: list[int] = []
    skipped: dict[int, str] = {}
    with run(session_name) as calls:
        async with adorm.atomic():
            try:
                activity = await Activity.objects.get(pk=pk)
            except Activity.DoesNotExist as exc:
                raise not_found(exc) from None
            await Activity.objects.filter(pk=pk).update(capacity=adorm.F("capacity"))
            for member_id in registrations.member_ids:
                if member_id in registered or member_id in skipped:
                    continue
                if not await Member.objects.filter(pk=member_id).exists():
                    skipped[member_id] = "unknown member"
                    continue
                try:
                    async with adorm.atomic():
                        attendance, created = await Attendance.objects.get_or_create(
                            activity_id=activity.id, member_id=member_id, defaults={"status": "registered"}
                        )
                        if not created:
                            skipped[member_id] = "already registered"
                            continue
                        taken = (
                            await Attendance.objects.filter(activity_id=activity.id).exclude(status="cancelled").count()
                        )
                        if taken > activity.capacity:
                            raise _Full
                except _Full:
                    skipped[member_id] = "full"
                    continue
                registered.append(member_id)
    return traced(Registered(registered=registered, skipped=skipped), session_name, calls)


class _Full(Exception):  # noqa: N818 - control flow only
    """Rolls a registration back to its savepoint."""


@router.get("/clubs/{slug}/summary", summary="Club summary", responses=NOT_FOUND | EXECUTOR_ERRORS)
async def club_summary(
    slug: Slug,
    session_name: SessionName = "default",
    atomic: Annotated[bool, Query(description="Read in one transaction (one slot), not a slot per read.")] = False,
) -> Traced[ClubSummary]:
    """Three counts read by concurrent tasks of an `asyncio.TaskGroup`. The tasks inherit the current session and,
    with `atomic`, the transaction, so all three run on its slot; without it each count acquires a slot of its
    own."""

    async def counts(club: Club) -> ClubSummary:
        async with asyncio.TaskGroup() as group:
            members = group.create_task(Membership.objects.filter(club_id=club.id, active=True).count())
            activities = group.create_task(Activity.objects.filter(club_id=club.id).count())
            invoices = group.create_task(Invoice.objects.filter(club_id=club.id, status="open").count())
        return ClubSummary(
            club=club,
            active_memberships=members.result(),
            activities=activities.result(),
            open_invoices=invoices.result(),
        )

    with run(session_name) as calls:
        try:
            if atomic:
                async with adorm.atomic():
                    summary = await counts(await Club.objects.get(slug=slug))
            else:
                summary = await counts(await Club.objects.get(slug=slug))
        except Club.DoesNotExist as exc:
            raise not_found(exc) from None
    return traced(summary, session_name, calls)


@router.get("/outbox", summary="Messages queued on commit", responses=NO_DRIVER)
async def outbox() -> list[OutboxMessage]:
    """The messages `on_commit` callbacks of committed enrolments queued, newest last (at most 50)."""
    return list(OUTBOX)


@executor_router.get("/api/executor", summary="Executor state")
async def executor_state(request: Request) -> ExecutorState:
    """The active driver and the pools of the process's executor (adorm's default router of this event loop)."""
    router = default_router(create=False)
    pools = [] if router is None else router.pools
    return ExecutorState(
        driver=request.app.state.driver,
        pools=[
            PoolState(
                using=pool.name,
                limit=pool.limit,
                size=pool.size,
                idle=pool.idle,
                in_use=pool.in_use,
                waiting=pool.waiting,
            )
            for pool in pools
        ],
    )
