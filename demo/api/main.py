"""The demo API: FastAPI endpoints on the generated adorm models of ``demo.models``.

Every ``/api/operations/...`` endpoint builds an operation without executing it and answers an `OperationPlan`
(``POST .../payments`` answers a list of two): the operation, encoded as JSON with the models' types, the construct
names it uses and, for every available driver, the names that driver does not support. The ``/api/data/...``
endpoints (`demo.api.data`) execute operations through sessions and transactions: the writes on the active driver,
the reads on the active driver or on the available driver a request names.

    uv run python demo.py
"""

from __future__ import annotations

import asyncio
import datetime

from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Body, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field

import adorm

from adorm import FieldError, PoolSettings
from adorm.drivers import Driver
from adorm.models import registry
from adorm.operations import Operation
from adorm.settings import close_pools
from demo.api import aggregates, data, drivers, instances, operations
from demo.api.schemas import AttendanceIn, Id, Kind, Level, MemberChanges, PaymentIn, Slug
from demo.models import MODELS
from demo.models.clubs import Member


class OperationPlan(BaseModel):
    """An adorm operation built by an endpoint."""

    operation: dict[str, Any] = Field(description="The operation as `Operation.to_dict()` encodes it.")
    constructs: list[str] = Field(description="Construct names the operation uses, sorted, without repeats.")
    unsupported: dict[str, list[str]] = Field(
        description="Per available driver: the operation kind and constructs that driver does not support."
    )


class DriverInfo(BaseModel):
    """A driver the demo knows."""

    name: str = Field(description="Driver name, the value of `demo.py --driver`.")
    target: str = Field(description="Import path of the driver class.")
    available: bool = Field(description="Whether the driver's module imports.")
    active: bool = Field(description="Whether the API runs on this driver.")
    mode: str | None = Field(None, description="`threaded` or `async`; `null` for a driver that is not available.")
    operations: list[str] | None = Field(None, description="Operation kinds the driver supports.")


class Health(BaseModel):
    """State of the demo API."""

    status: str = Field(description="`ok` once the API has started.")
    adorm_version: str = Field(description="Version of the adorm package.")
    driver: str | None = Field(description="The active driver; `null` when no driver is available.")


def invalid(msg: str, loc: tuple[str | int, ...] = ("body",), value: Any = None) -> RequestValidationError:
    """A 422 answer in FastAPI's validation error shape."""
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": value}])


def unprocessable(description: str) -> dict[str, Any]:
    """``openapi_extra`` describing when an endpoint answers 422 besides failed request validation."""
    return {
        "responses": {"422": {"description": f"Validation error: the request does not validate, or {description}."}}
    }


POOL = PoolSettings(max_connections=4, acquire_timeout=5.0)
"""The pool settings of every demo connection profile; each profile has a pool of its own."""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Selects the active driver and loads every available driver (each one's ``setup`` runs on a worker thread, in
    the order of `drivers.DRIVERS`). The active driver becomes adorm's connection profile ``default``, and every
    loaded driver also a profile named after it, which the read endpoints run on when a request names that driver. At
    shutdown the pools close and the profiles are removed."""
    active = drivers.selected()
    app.state.driver = active
    loaded: dict[str, Driver] = {}
    for name in drivers.available():
        loaded[name] = await asyncio.to_thread(drivers.get(name).load)
    app.state.drivers = loaded
    if active is not None:
        profiles = {"default": drivers.get(active).profile(loaded[active])}
        profiles.update({name: drivers.get(name).profile(driver, name) for name, driver in loaded.items()})
        adorm.configure(profiles=profiles, pool=POOL)
    try:
        yield
    finally:
        if active is not None:
            await close_pools()
            adorm.configure()


app = FastAPI(
    title="adorm demo",
    version=adorm.__version__,
    description="Clubs, members, activities and billing on adorm models.",
    lifespan=lifespan,
)
app.include_router(aggregates.router)
app.include_router(instances.router)
app.include_router(data.router)
app.include_router(data.executor_router)
data.install_error_handlers(app)


def loaded_drivers(request: Request) -> dict[str, Driver]:
    """The available drivers by name, loaded."""
    return request.app.state.drivers


def plan(request: Request, operation: Operation) -> OperationPlan:
    return OperationPlan(
        operation=operation.to_dict(registry),
        constructs=sorted(set(operation.constructs())),
        unsupported={
            name: driver.capabilities.unsupported(operation) for name, driver in loaded_drivers(request).items()
        },
    )


@app.get("/api/health", tags=["demo"], summary="API state")
async def health(request: Request) -> Health:
    """Whether the API runs, the adorm version and the active driver."""
    return Health(status="ok", adorm_version=adorm.__version__, driver=request.app.state.driver)


@app.get("/api/drivers", tags=["demo"], summary="Drivers the demo knows")
async def driver_list(request: Request) -> list[DriverInfo]:
    """Every driver the demo can run on, whether it is available and which one is active."""
    result = []
    loaded = loaded_drivers(request)
    for demo_driver in drivers.DRIVERS:
        driver = loaded.get(demo_driver.name)
        result.append(
            DriverInfo(
                name=demo_driver.name,
                target=demo_driver.target,
                available=driver is not None,
                active=demo_driver.name == request.app.state.driver,
                mode=None if driver is None else driver.mode,
                operations=None if driver is None else sorted(driver.capabilities.operations),
            )
        )
    return result


@app.get("/api/models", tags=["demo"], summary="JSON schemas of the adorm models")
async def model_schemas() -> dict[str, dict[str, Any]]:
    """The JSON schema of every demo model by label; relations are not part of it."""
    return {model._meta.label: model.model_json_schema() for model in MODELS}


@app.get("/api/operations/members", tags=["reads"], summary="Search members")
async def search_members(
    request: Request,
    club: Annotated[str | None, Query(description="Slug of a club the member actively belongs to.")] = None,
    level: Annotated[Level | None, Query(description="Skill level.")] = None,
    search: Annotated[str | None, Query(description="Fragment of the first or last name, any case.")] = None,
    joined_after: Annotated[datetime.date | None, Query(description="Joined on or after this date (UTC).")] = None,
    ordering: Annotated[operations.MemberOrdering, Query(description="Sort order; `-` reverses.")] = "name",
    offset: Annotated[int, Query(ge=0, description="Rows to skip.")] = 0,
    limit: Annotated[int, Query(ge=1, le=100, description="Page size.")] = 20,
) -> OperationPlan:
    """A `Select` of members: filters with `Q` objects across relations, `distinct()`, ordering and a slice."""
    return plan(
        request,
        operations.member_search(
            club=club,
            level=level,
            search=search,
            joined_after=joined_after,
            ordering=ordering,
            offset=offset,
            limit=limit,
        ),
    )


@app.get("/api/operations/clubs/{slug}/report", tags=["reads"], summary="Club report")
async def club_report(request: Request, slug: Slug) -> OperationPlan:
    """A `Get` of one club with a filtered `Count`, `Subquery` aggregates over its memberships and invoices, `Exists`
    and `Case` annotations."""
    return plan(request, operations.club_report(slug))


@app.get("/api/operations/activities", tags=["reads"], summary="Activity schedule")
async def activity_schedule(
    request: Request,
    club: Annotated[str | None, Query(description="Slug of the club running the activity.")] = None,
    kind: Annotated[Kind | None, Query(description="Kind of activity.")] = None,
    starts_after: Annotated[
        datetime.datetime | None, Query(description="Activities starting at or after this time.")
    ] = None,
) -> OperationPlan:
    """A `Select` of activities with `select_related()`, a filtered `Count`, `F` arithmetic and `TruncMonth`."""
    return plan(request, operations.activity_schedule(club=club, kind=kind, starts_after=starts_after))


@app.get("/api/operations/invoices/outstanding", tags=["reads"], summary="Outstanding invoices by club")
async def outstanding_invoices(
    request: Request,
    today: Annotated[datetime.date, Query(description="Invoices due before this date are overdue.")],
) -> OperationPlan:
    """A grouped `Select`: `values()` followed by `annotate()` with `Sum` and `Count`."""
    return plan(request, operations.outstanding_by_club(today))


@app.post(
    "/api/operations/members",
    tags=["writes"],
    summary="Create a member",
    status_code=201,
    openapi_extra=unprocessable("the body has fields `Member` does not declare"),
)
async def create_member(request: Request, member: Member) -> OperationPlan:
    """A `Create` of a member. The request body is validated by the adorm model `Member` itself."""
    if member.model_extra:
        raise RequestValidationError(
            [
                {
                    "type": "extra_forbidden",
                    "loc": ("body", name),
                    "msg": "Extra inputs are not permitted",
                    "input": value,
                }
                for name, value in sorted(member.model_extra.items())
            ]
        )
    return plan(request, operations.create_member(member))


@app.patch(
    "/api/operations/members/{pk}",
    tags=["writes"],
    summary="Change a member",
    openapi_extra=unprocessable("the body has no fields, or `null` for a field other than `birth_date`"),
)
async def change_member(request: Request, pk: Id, changes: MemberChanges) -> OperationPlan:
    """A `Save` with `update_fields`: only the fields in the request body are written."""
    return plan(request, operations.update_member(pk, changes.model_dump(exclude_unset=True)))


@app.post("/api/operations/activities/{pk}/attendances", tags=["writes"], summary="Register for an activity")
async def register_attendance(request: Request, pk: Id, attendance: AttendanceIn) -> OperationPlan:
    """A `GetOrCreate` of the member's attendance."""
    return plan(request, operations.register_attendance(pk, attendance.member_id))


@app.post("/api/operations/attendances/{pk}/cancel", tags=["writes"], summary="Cancel an attendance")
async def cancel_attendance(request: Request, pk: Id) -> OperationPlan:
    """An `Update` of one attendance, excluding one the member already attended."""
    return plan(request, operations.cancel_attendance(pk))


@app.post("/api/operations/invoices/{pk}/payments", tags=["writes"], summary="Pay an invoice")
async def pay_invoice(request: Request, pk: Id, payment: PaymentIn) -> list[OperationPlan]:
    """A `Create` of the payment, then an `Update` marking the invoice paid."""
    built = operations.pay_invoice(pk, payment.amount, payment.reference, payment.paid_at)
    return [plan(request, operation) for operation in built]


@app.delete("/api/operations/clubs/{slug}/draft-invoices", tags=["writes"], summary="Delete draft invoices")
async def delete_draft_invoices(request: Request, slug: Slug) -> OperationPlan:
    """A `Delete` of the club's draft invoices."""
    return plan(request, operations.delete_draft_invoices(slug))


@app.post(
    "/api/operations/decode",
    tags=["demo"],
    summary="Decode an operation",
    openapi_extra=unprocessable(
        "`Operation.from_dict()` rejects the body (the message is its `ValueError` or `FieldError`)"
    ),
)
async def decode_operation(
    request: Request,
    data: Annotated[dict[str, Any], Body(description="An operation as `Operation.to_dict()` encodes it.")],
) -> OperationPlan:
    """Decode an encoded operation with the models' schema and encode it again; invalid data answers 422."""
    try:
        operation = Operation.from_dict(data, registry)
    except (ValueError, FieldError) as exc:
        raise invalid(str(exc), value=data) from None
    return plan(request, operation)
