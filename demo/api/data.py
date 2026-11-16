"""The demo's executed endpoints: the generated models read and write through sessions and transactions on the
executor.

Each endpoint runs in the session ``(<using>, <X-Adorm-Session header>)`` and answers `Traced`: its result, the
session, the driver and the calls the driver received (recorded by the memory driver; empty with another driver).
``using`` is ``default``, the profile of the active driver; a read endpoint runs on the profile of the available
driver the ``X-Adorm-Driver`` header names. ``/api/data/compare/members`` reads one page of members on every
available driver concurrently. Executor and lock errors answer `ExecutorError` (`install_error_handlers()`): 503
when no slot becomes free in time, 409 when the database detects a deadlock or a row lock is not available, 504 when
the request's `adorm.timeout()` expires and 501 when the driver does not support what the request needs.
"""

from __future__ import annotations

import asyncio
import datetime
import decimal

from collections import deque
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Annotated, Generic, TypeVar

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import adorm

from adorm.exceptions import TimeoutPhase
from adorm.settings import default_router
from demo.api import memory, operations
from demo.api.schemas import Id, Level, MemberChanges, PaymentIn, Slug
from demo.models import MODELS
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

DRIVER_HEADER = "X-Adorm-Driver"
DriverName = Annotated[
    str | None,
    Header(
        alias=DRIVER_HEADER,
        description=(
            "Name of an available driver (`GET /api/drivers`) to read on: the session's `using` is that driver's "
            "profile. Without it the read runs on the active driver's profile `default`."
        ),
    ),
]

T = TypeVar("T")
M = TypeVar("M", bound=adorm.Model)


@dataclass(frozen=True)
class Target:
    """Where an endpoint runs: the connection profile ``using`` and the name of its driver."""

    using: str
    driver: str


class Call(BaseModel):
    """A call the driver received."""

    slot: int = Field(description="Number of the slot's connection, from 1 in the order the connections opened.")
    session: str | None = Field(description="`using/session_name` of the session the slot was assigned to.")
    call: str = Field(description="`acquire`, `begin`, `<operation kind> <model label>`, `savepoint <name>` …")


class Traced(BaseModel, Generic[T]):
    """The result of an executed endpoint and how it ran."""

    result: T = Field(description="What the endpoint read or wrote.")
    session: str = Field(description="`using/session_name` of the session the request ran in.")
    driver: str = Field(description="Name of the driver that executed the operations.")
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


class Settlement(BaseModel):
    """The payments a settlement stored."""

    payments: list[Payment] = Field(
        description="One payment of the full total per invoice settled, earliest due invoice first."
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


class DriverRead(BaseModel):
    """One driver's read of a `Comparison`."""

    driver: str = Field(description="Name of the driver.")
    session: str = Field(description="`using/session_name` of the session the read ran in.")
    result: list[Member] | None = Field(description="The members read; `null` when the read failed.")
    error: str | None = Field(description="`<adorm exception class>: <message>` when the read failed; else `null`.")


class Comparison(BaseModel):
    """The same page of members read on every available driver."""

    same: bool = Field(description="Whether every read succeeded and all of them answered the same members.")
    reads: list[DriverRead] = Field(description="One read per available driver, in the order of `GET /api/drivers`.")


class ExecutorError(BaseModel):
    """An error answer. Executor errors fill every field; other errors (no active driver, an invoice that is not
    open) answer `detail` only."""

    detail: str = Field(description="The error message.")
    error: str | None = Field(
        None,
        description=(
            "`SessionTimeout`, `DeadlockError`, `OperationalError`, `OperationTimeout` or `NotSupportedError`; absent "
            "in a `{detail}` answer."
        ),
    )
    session: str | None = Field(
        None, description="`using/session_name` of the session that waited or timed out, if known."
    )
    held_sessions: list[str] = Field(
        default_factory=list,
        description="`DeadlockError`: the sessions whose slots the waiting flow held; empty otherwise.",
    )
    sqlstate: str | None = Field(
        None, description="`OperationalError`: the SQLSTATE, `40P01` (deadlock) or `55P03` (lock not available)."
    )
    phase: TimeoutPhase | None = Field(
        None,
        description=(
            "`OperationTimeout`: `waiting_for_slot` or `executing`, what the interrupted operation was doing; `null` "
            "when no operation was in progress."
        ),
    )
    operation: str | None = Field(
        None,
        description="`OperationTimeout`: the interrupted operation, `<kind> <model label>` or a transaction step.",
    )
    outcome: adorm.CancellationOutcome | None = Field(
        None,
        description=(
            "`OperationTimeout`: whether the interrupted operation took effect (`cancellation_outcome()`). The "
            "transaction it ran in is rolled back, unless the operation is `commit` and its outcome is not "
            "`not_started` or `failed`."
        ),
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


class SchemaProblemOut(BaseModel):
    """A difference between a demo model and the database or the driver's model (`adorm.SchemaProblem`)."""

    model: str = Field(description="Label of the model.")
    kind: str = Field(
        description=(
            "`schema_hash`, `missing_table`, `missing_column`, `column_type`, `nullability`, `unknown_field` or "
            "`required_column`."
        )
    )
    table: str | None = Field(description="Database table; `null` where it does not apply.")
    column: str | None = Field(description="Database column; `null` where it does not apply.")
    field: str | None = Field(
        description="adorm name of the model's field or relation; `null` where it does not apply."
    )
    expected: str | None = Field(description="What the model expects; `null` where it does not apply.")
    actual: str | None = Field(description="What the database or the driver has; `null` where it does not apply.")
    message: str = Field(description="The problem in one line.")


class SchemaReport(BaseModel):
    """The result of `adorm.check_schema()` over the demo's models."""

    models: list[str] = Field(description="Labels of the models checked, in order.")
    problems: list[SchemaProblemOut] = Field(description="The differences found; empty when everything matches.")


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


def write_target(request: Request) -> Target:
    """The profile ``default`` of the active driver."""
    return Target("default", require_driver(request))


def read_target(request: Request, driver: DriverName = None) -> Target:
    """The profile of the driver the ``X-Adorm-Driver`` header names, or `write_target()` without the header; 422 for
    a name that is not an available driver."""
    if driver is None:
        return write_target(request)
    if driver not in request.app.state.drivers:
        names = ", ".join(request.app.state.drivers)
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("header", DRIVER_HEADER),
                    "msg": f"{driver!r} is not an available driver; the available drivers are {names}",
                    "input": driver,
                }
            ]
        )
    return Target(driver, driver)


ReadTarget = Annotated[Target, Depends(read_target)]
WriteTarget = Annotated[Target, Depends(write_target)]


router = APIRouter(prefix="/api/data", tags=["data"], dependencies=[Depends(require_driver)])
executor_router = APIRouter(tags=["demo"])

NOT_FOUND = {404: {"description": "No row has that primary key or slug."}}
NO_DRIVER = {503: {"description": "No driver is active."}}
LOCK_ERRORS = {
    409: {
        "model": ExecutorError,
        "description": (
            "The database detected a deadlock (SQLSTATE `40P01`), or the activity's row is locked and `nowait` or "
            "`lock_timeout` gave up waiting (SQLSTATE `55P03`); nothing is stored, retry."
        ),
    },
    501: {
        "model": ExecutorError,
        "description": "The active driver does not support a connection option the request sets (`lock_timeout`).",
    },
    504: {
        "model": ExecutorError,
        "description": (
            "The request's `timeout` expired (`OperationTimeout`). The transaction is rolled back, unless "
            "`operation` is `commit`: then `outcome` tells whether the commit took effect (`succeeded`, `unknown`)."
        ),
    },
}
"""Error responses of the endpoints that lock rows, wait with limits and set connection options."""
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
NOT_SUPPORTED = {
    501: {
        "model": ExecutorError,
        "description": "The driver does not support an operation or construct the request needs (`NotSupportedError`).",
    }
}
"""Error response of the endpoints that need operations or constructs the memory driver refuses."""
UNKNOWN_DRIVER = {
    "responses": {
        "422": {
            "description": (
                f"Validation error: the request does not validate, or `{DRIVER_HEADER}` names no available driver."
            )
        }
    }
}
"""``openapi_extra`` of the read endpoints."""


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
    return JSONResponse(body.model_dump(mode="json"), status_code=503)


LOCK_CONFLICTS = frozenset({"40P01", "55P03"})
"""SQLSTATEs answered with 409: deadlock detected, lock not available."""


async def _operational_error(_request: Request, exc: adorm.OperationalError) -> JSONResponse:
    if exc.sqlstate not in LOCK_CONFLICTS:
        raise exc
    body = ExecutorError(detail=str(exc), error=type(exc).__name__, sqlstate=exc.sqlstate)
    return JSONResponse(body.model_dump(mode="json"), status_code=409)


def _describe(operation: object) -> str | None:
    if operation is None:
        return None
    kind, model = getattr(operation, "kind", None), getattr(operation, "model", None)
    return f"{kind} {model}" if kind and model else str(operation)


async def _operation_timeout(_request: Request, exc: adorm.OperationTimeout) -> JSONResponse:
    body = ExecutorError(
        detail=str(exc),
        error=type(exc).__name__,
        session=_session_name(exc.session),
        phase=exc.phase,
        operation=_describe(exc.operation),
        outcome=adorm.cancellation_outcome(exc),
    )
    return JSONResponse(body.model_dump(mode="json"), status_code=504)


async def _not_supported(_request: Request, exc: adorm.NotSupportedError) -> JSONResponse:
    body = ExecutorError(detail=str(exc), error=type(exc).__name__)
    return JSONResponse(body.model_dump(mode="json"), status_code=501)


def install_error_handlers(app: FastAPI) -> None:
    """Answer with an `ExecutorError` body: `adorm.SessionTimeout` (and `adorm.DeadlockError`) with 503, a deadlock or
    a lock that is not available (`adorm.OperationalError` with SQLSTATE ``40P01`` or ``55P03``) with 409,
    `adorm.OperationTimeout` with 504 and `adorm.NotSupportedError` with 501."""
    app.add_exception_handler(adorm.SessionTimeout, _session_timeout)
    app.add_exception_handler(adorm.OperationalError, _operational_error)
    app.add_exception_handler(adorm.OperationTimeout, _operation_timeout)
    app.add_exception_handler(adorm.NotSupportedError, _not_supported)


@asynccontextmanager
async def limits(timeout: float | None, lock_timeout: float | None) -> AsyncIterator[None]:
    """The request's limits: ``adorm.timeout(timeout)`` around the block and ``lock_timeout`` as a connection option
    of its operations; ``None`` sets no limit."""
    options = {} if lock_timeout is None else {"lock_timeout": lock_timeout}
    with adorm.connection_options(**options):
        async with adorm.timeout(timeout):
            yield


@contextmanager
def run(target: Target, session_name: str) -> Iterator[list[memory.TraceEntry]]:
    """Make ``(target.using, session_name)`` the current session and collect the driver's calls."""
    session = adorm.create_session(target.using, session_name)
    with session.activate(), memory.tracing() as calls:
        yield calls


def traced(result: T, target: Target, session_name: str, calls: list[memory.TraceEntry]) -> Traced[T]:
    return Traced(
        result=result,
        session=f"{target.using}/{session_name}",
        driver=target.driver,
        calls=[Call(slot=entry.slot, session=entry.session, call=entry.call) for entry in calls],
    )


def member_page(
    level: Level | None, search: str | None, ordering: operations.MemberOrdering, offset: int, limit: int
) -> adorm.QuerySet:
    """A page of members on the current session."""
    members = Member.objects.all()
    if level is not None:
        members = members.filter(level=level)
    if search:
        members = members.filter(adorm.Q(first_name__icontains=search) | adorm.Q(last_name__icontains=search))
    return members.order_by(*operations.MEMBER_ORDERING[ordering])[offset : offset + limit]


def without_extras(instance: M) -> M:
    """A copy of ``instance`` without its extra values (annotations, the ``to_attr`` of a prefetch)."""
    copy = instance.model_copy()
    copy.__pydantic_extra__ = {}
    return copy


def not_found(exc: adorm.ObjectDoesNotExist) -> HTTPException:
    return HTTPException(404, str(exc))


MemberLevel = Annotated[Level | None, Query(description="Skill level.")]
Timeout = Annotated[
    float | None,
    Query(
        gt=0,
        le=60,
        description="Seconds the whole request may take (`adorm.timeout()`), waiting for a slot included; 504 after.",
    ),
]
LockTimeout = Annotated[
    float | None,
    Query(
        gt=0,
        le=60,
        description=(
            "Seconds a statement may wait for a row lock (connection option `lock_timeout`); 409 after. Not supported "
            "by the memory driver (501)."
        ),
    ),
]
MemberSearch = Annotated[str | None, Query(description="Fragment of the first or last name, any case.")]
MemberOrder = Annotated[operations.MemberOrdering, Query(description="Sort order; `-` reverses.")]
Offset = Annotated[int, Query(ge=0, description="Rows to skip.")]
Limit = Annotated[int, Query(ge=1, le=100, description="Page size.")]


@router.get("/members", summary="List members", responses=EXECUTOR_ERRORS, openapi_extra=UNKNOWN_DRIVER)
async def list_members(
    target: ReadTarget,
    session_name: SessionName = "default",
    level: MemberLevel = None,
    search: MemberSearch = None,
    ordering: MemberOrder = "name",
    offset: Offset = 0,
    limit: Limit = 20,
) -> Traced[list[Member]]:
    """A page of members: one `select`, run on a slot acquired for it and released after it."""
    with run(target, session_name) as calls:
        result = [member async for member in member_page(level, search, ordering, offset, limit)]
    return traced(result, target, session_name, calls)


@router.get("/compare/members", summary="Compare a page of members on every driver", responses=NO_DRIVER)
async def compare_members(
    request: Request,
    session_name: SessionName = "default",
    level: MemberLevel = None,
    search: MemberSearch = None,
    ordering: MemberOrder = "name",
    offset: Offset = 0,
    limit: Limit = 20,
) -> Comparison:
    """The page of `GET /api/data/members`, read on every available driver by concurrent tasks of an
    `asyncio.TaskGroup`, each in the session `(<driver>, <session name>)` on a slot of that driver's profile. An adorm
    error of one read is reported in its `error` and does not stop the others."""
    names = list(request.app.state.drivers)

    async def read(name: str) -> DriverRead:
        session = f"{name}/{session_name}"
        with adorm.create_session(name, session_name).activate():
            try:
                members = [member async for member in member_page(level, search, ordering, offset, limit)]
            except adorm.AdormError as exc:
                return DriverRead(driver=name, session=session, result=None, error=f"{type(exc).__name__}: {exc}")
        return DriverRead(driver=name, session=session, result=members, error=None)

    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(read(name)) for name in names]
    reads = [task.result() for task in tasks]
    dumped = [None if r.result is None else [m.model_dump(mode="json") for m in r.result] for r in reads]
    same = None not in dumped and all(page == dumped[0] for page in dumped)
    return Comparison(same=same, reads=reads)


@router.get(
    "/members/{pk}", summary="Get a member", responses=NOT_FOUND | EXECUTOR_ERRORS, openapi_extra=UNKNOWN_DRIVER
)
async def get_member(pk: Id, target: ReadTarget, session_name: SessionName = "default") -> Traced[Member]:
    """One member, with `get`."""
    with run(target, session_name) as calls:
        try:
            member = await Member.objects.get(pk=pk)
        except Member.DoesNotExist as exc:
            raise not_found(exc) from None
    return traced(member, target, session_name, calls)


@router.patch("/members/{pk}", summary="Change a member", responses=NOT_FOUND | EXECUTOR_ERRORS)
async def change_member(
    pk: Id, changes: MemberChanges, target: WriteTarget, session_name: SessionName = "default"
) -> Traced[Member]:
    """Load the member, assign the fields in the body and `save()` only those (`update_fields`), in one
    transaction."""
    values = changes.model_dump(exclude_unset=True)
    with run(target, session_name) as calls:
        async with adorm.atomic():
            try:
                member = await Member.objects.get(pk=pk)
            except Member.DoesNotExist as exc:
                raise not_found(exc) from None
            for name, value in values.items():
                setattr(member, name, value)
            await member.save(update_fields=tuple(values))
    return traced(member, target, session_name, calls)


@router.post(
    "/clubs/{slug}/members", summary="Enrol a new member", status_code=201, responses=NOT_FOUND | EXECUTOR_ERRORS
)
async def enrol_member(
    slug: Slug, enrolment: Enrolment, target: WriteTarget, session_name: SessionName = "default"
) -> Traced[Enrolled]:
    """Create a member and their membership of the club in one transaction. A welcome message goes to the outbox
    when the transaction commits (`on_commit`)."""
    with run(target, session_name) as calls:
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
    return traced(Enrolled(member=member, membership=membership), target, session_name, calls)


@router.get(
    "/invoices/{pk}",
    summary="Get an invoice and its payments",
    responses=NOT_FOUND | EXECUTOR_ERRORS,
    openapi_extra=UNKNOWN_DRIVER,
)
async def get_invoice(pk: Id, target: ReadTarget, session_name: SessionName = "default") -> Traced[InvoiceDetail]:
    """The invoice with `get`, its payments through the reverse relation `Invoice.payments`."""
    with run(target, session_name) as calls:
        try:
            invoice = await Invoice.objects.get(pk=pk)
        except Invoice.DoesNotExist as exc:
            raise not_found(exc) from None
        payments = [payment async for payment in invoice.payments.order_by("paid_at")]
    return traced(InvoiceDetail(invoice=invoice, payments=payments), target, session_name, calls)


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
async def pay_invoice(
    pk: Id, payment: PaymentIn, target: WriteTarget, session_name: SessionName = "default"
) -> Traced[Payment]:
    """Store the payment and mark the invoice paid in one transaction. The update matches only an open invoice; when
    it matches none, the block raises and the payment is rolled back."""
    with run(target, session_name) as calls:
        async with adorm.atomic():
            if not await Invoice.objects.filter(pk=pk).exists():
                raise HTTPException(404, f"no invoice has the primary key {pk}")
            stored = await Payment.objects.create(
                invoice_id=pk, amount=payment.amount, reference=payment.reference, paid_at=payment.paid_at
            )
            if not await Invoice.objects.filter(pk=pk, status="open").update(status="paid"):
                raise HTTPException(409, f"invoice {pk} is not open")
    return traced(stored, target, session_name, calls)


@router.post(
    "/activities/{pk}/registrations",
    summary="Register members",
    responses=NOT_FOUND | EXECUTOR_ERRORS | LOCK_ERRORS,
)
async def register_members(
    pk: Id,
    registrations: Registrations,
    target: WriteTarget,
    session_name: SessionName = "default",
    nowait: Annotated[
        bool, Query(description="Answer 409 at once when another transaction holds the activity's row lock.")
    ] = False,
    timeout: Timeout = None,
    lock_timeout: LockTimeout = None,
) -> Traced[Registered]:
    """Register members for the activity in one transaction, each in a nested `atomic()` block (a savepoint): a
    registration that would exceed the capacity is rolled back to its savepoint, the others stay. The transaction
    reads the activity with `select_for_update()`, which locks its row: concurrent registrations for the activity run
    one after another. A request that gives up (`nowait`, `lock_timeout`, `timeout`, or the client going away) before
    the commit rolls the transaction back and stores nothing; a `timeout` during the commit answers 504 with
    `operation` `commit`, and its `outcome` tells whether the registrations were stored."""
    registered: list[int] = []
    skipped: dict[int, str] = {}
    with run(target, session_name) as calls:
        async with limits(timeout, lock_timeout), adorm.atomic():
            try:
                activity = await Activity.objects.select_for_update(nowait=nowait).get(pk=pk)
            except Activity.DoesNotExist as exc:
                raise not_found(exc) from None
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
    return traced(Registered(registered=registered, skipped=skipped), target, session_name, calls)


class _Full(Exception):  # noqa: N818 - control flow only
    """Rolls a registration back to its savepoint."""


@router.post("/invoices/settlements", summary="Settle open invoices", responses=EXECUTOR_ERRORS | LOCK_ERRORS)
async def settle_invoices(
    target: WriteTarget,
    session_name: SessionName = "default",
    limit: Annotated[int, Query(ge=1, le=20, description="The most invoices to settle.")] = 5,
    timeout: Timeout = None,
) -> Traced[Settlement]:
    """Pay up to `limit` open invoices in full, earliest due first, in one transaction. The invoices are read with
    `select_for_update(skip_locked=True)`: an invoice whose row lock another transaction holds (another settlement, or a
    payment once it has marked the invoice paid) is left out instead of waited for, so concurrent settlements settle
    different invoices and none is paid twice."""
    paid_at = datetime.datetime.now(datetime.UTC)
    with run(target, session_name) as calls:
        async with limits(timeout, None), adorm.atomic():
            due = Invoice.objects.select_for_update(skip_locked=True).filter(status="open").order_by("due", "id")
            payments = []
            async for invoice in due[:limit]:
                payments.append(
                    await Payment.objects.create(
                        invoice_id=invoice.id,
                        amount=invoice.total,
                        reference=f"settled by {session_name}"[:40],
                        paid_at=paid_at,
                    )
                )
                invoice.status = "paid"
                await invoice.save(update_fields=("status",))
    return traced(Settlement(payments=payments), target, session_name, calls)


@router.get(
    "/clubs/{slug}/summary", summary="Club summary", responses=NOT_FOUND | EXECUTOR_ERRORS, openapi_extra=UNKNOWN_DRIVER
)
async def club_summary(
    slug: Slug,
    target: ReadTarget,
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

    with run(target, session_name) as calls:
        try:
            if atomic:
                async with adorm.atomic():
                    summary = await counts(await Club.objects.get(slug=slug))
            else:
                summary = await counts(await Club.objects.get(slug=slug))
        except Club.DoesNotExist as exc:
            raise not_found(exc) from None
    return traced(summary, target, session_name, calls)


@router.get(
    "/schema",
    summary="Check the database schema",
    responses=EXECUTOR_ERRORS
    | {501: {"model": ExecutorError, "description": "The driver does not check tables (the memory driver)."}},
    openapi_extra=UNKNOWN_DRIVER,
)
async def check_schema(target: ReadTarget, session_name: SessionName = "default") -> Traced[SchemaReport]:
    """Every demo model compared with the driver's database by `adorm.check_schema()`: one `check_table` per model,
    which also compares the model's schema hash with the driver's model. Nothing is changed."""
    with run(target, session_name) as calls:
        problems = await adorm.check_schema(MODELS)
    report = SchemaReport(
        models=[model._meta.label for model in MODELS],
        problems=[
            SchemaProblemOut(
                model=problem.model,
                kind=problem.kind,
                table=problem.table,
                column=problem.column,
                field=problem.field,
                expected=problem.expected,
                actual=problem.actual,
                message=str(problem),
            )
            for problem in problems
        ],
    )
    return traced(report, target, session_name, calls)


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
