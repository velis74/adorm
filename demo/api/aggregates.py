"""The demo's aggregation endpoints: annotations, aggregates and grouping executed on the executor.

They are ``/api/data`` read endpoints: each runs in the session ``(<using>, <X-Adorm-Session header>)`` on the profile
of the driver ``X-Adorm-Driver`` names (the active driver without it) and answers `demo.api.data.Traced`. The queries
come from `demo.api.operations` or are built here. A driver that does not support a construct a query uses answers 501
(the memory driver refuses annotations, aggregates and grouping).
"""

from __future__ import annotations

import datetime
import decimal

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from adorm import Avg, Count, Max, Min, Q, Sum, TruncMonth
from demo.api import operations
from demo.api.data import (
    EXECUTOR_ERRORS,
    NOT_FOUND,
    not_found,
    NOT_SUPPORTED,
    ReadTarget,
    require_driver,
    run,
    SessionName,
    Traced,
    traced,
    UNKNOWN_DRIVER,
    without_extras,
)
from demo.api.schemas import Kind, Slug
from demo.models.activities import Activity
from demo.models.billing import Invoice
from demo.models.clubs import Club, Membership

router = APIRouter(prefix="/api/data", tags=["aggregates"], dependencies=[Depends(require_driver)])

READ_ERRORS = EXECUTOR_ERRORS | NOT_SUPPORTED


class ClubReport(BaseModel):
    """A club with aggregates over its memberships, invoices and activities."""

    club: Club = Field(description="The club.")
    active_members: int = Field(description="Its active memberships: `Count` with `filter`.")
    fees: decimal.Decimal = Field(
        description="The fees of its active memberships: `Sum` in a grouped `Subquery`, `0` without any (`Coalesce`)."
    )
    open_invoices: int = Field(description="Its open invoices: `Count` in a grouped `Subquery`.")
    has_competitions: bool = Field(description="Whether it runs a competition: `Exists`.")
    next_activity: str | None = Field(description="Name of its earliest activity: `Subquery`; `null` without any.")
    size: Literal["small", "medium", "large"] = Field(
        description="`Case` / `When` over `active_members`: `large` from 5, `medium` from 3."
    )


class FeeTotals(BaseModel):
    """Aggregates over the active memberships (`aggregate()`)."""

    memberships: int = Field(description="`Count('*')`.")
    clubs: int = Field(description="`Count('club', distinct=True)`.")
    paying: int = Field(description="`Count('pk', filter=Q(fee__gt=0))`.")
    total: decimal.Decimal = Field(description="`Sum('fee', default=0)`.")
    average: decimal.Decimal | None = Field(description="`Avg('fee')`; `null` over no rows.")
    lowest: decimal.Decimal | None = Field(description="`Min('fee')`; `null` over no rows.")
    highest: decimal.Decimal | None = Field(description="`Max('fee')`; `null` over no rows.")
    spread: decimal.Decimal | None = Field(description="`Max('fee') - Min('fee')`; `null` over no rows.")


class ScheduledActivity(BaseModel):
    """An activity with attendance counts."""

    activity: Activity = Field(description="The activity.")
    club: str = Field(description="Name of its club, read by `select_related()`.")
    coach: str | None = Field(description="`<first name> <last name>` of its coach, read by `select_related()`.")
    attendees: int = Field(description="Its attendances that are not cancelled: `Count` with `filter`.")
    free_places: int = Field(description="`F('capacity') - F('attendees')`; negative when overbooked.")
    month: datetime.datetime = Field(description="`TruncMonth('starts_at')`, in UTC.")


class Outstanding(BaseModel):
    """Overdue open invoices of one club."""

    club: str = Field(description="Name of the club: the grouping column `club__name`.")
    outstanding: decimal.Decimal = Field(description="`Sum('total')` of its overdue open invoices.")
    invoices: int = Field(description="`Count('id')` of its overdue open invoices.")


class InvoiceMonth(BaseModel):
    """The invoices issued in one month."""

    month: datetime.date = Field(description="First day of the month: the grouping annotation `TruncMonth('issued')`.")
    invoices: int = Field(description="`Count('pk')`.")
    billed: decimal.Decimal = Field(description="`Sum('total')` of every invoice.")
    open: int = Field(description="`Count('pk', filter=Q(status='open'))`.")
    paid: decimal.Decimal = Field(description="`Sum('total', filter=Q(status='paid'), default=0)`.")


ClubFilter = Annotated[
    str | None, Query(description="Slug of a club; without it, every club.", pattern=r"^[-a-zA-Z0-9_]+$")
]


def club_report(club: Club) -> ClubReport:
    return ClubReport(
        club=without_extras(club),
        active_members=club.active_members,
        fees=club.fees,
        open_invoices=club.open_invoices,
        has_competitions=club.has_competitions,
        next_activity=club.next_activity,
        size=club.size,
    )


@router.get("/clubs/reports", summary="Report of every club", responses=READ_ERRORS, openapi_extra=UNKNOWN_DRIVER)
async def club_reports(
    target: ReadTarget,
    session_name: SessionName = "default",
    min_active_members: Annotated[
        int, Query(ge=0, description="Leave out clubs with fewer active members: a condition on an aggregate.")
    ] = 0,
) -> Traced[list[ClubReport]]:
    """Every club annotated as `GET /api/operations/clubs/{slug}/report` annotates one
    (`operations.club_report_rows()`), in one `select`; `min_active_members` filters on the `Count` annotation (SQL
    `HAVING`). Largest first."""
    with run(target, session_name) as calls:
        clubs = operations.club_report_rows(Club.objects.all())
        if min_active_members:
            clubs = clubs.filter(active_members__gte=min_active_members)
        result = [club_report(club) async for club in clubs.order_by("-active_members", "slug")]
    return traced(result, target, session_name, calls)


@router.get(
    "/clubs/{slug}/report",
    summary="Club report",
    responses=NOT_FOUND | READ_ERRORS,
    openapi_extra=UNKNOWN_DRIVER,
)
async def club_report_of(slug: Slug, target: ReadTarget, session_name: SessionName = "default") -> Traced[ClubReport]:
    """The `Get` of `GET /api/operations/clubs/{slug}/report`, executed: one club with its annotations."""
    with run(target, session_name) as calls:
        try:
            club = await operations.club_report_rows(Club.objects.filter(slug=slug)).get()
        except Club.DoesNotExist as exc:
            raise not_found(exc) from None
    return traced(club_report(club), target, session_name, calls)


@router.get("/memberships/fees", summary="Fee totals", responses=READ_ERRORS, openapi_extra=UNKNOWN_DRIVER)
async def fee_totals(
    target: ReadTarget, session_name: SessionName = "default", club: ClubFilter = None
) -> Traced[FeeTotals]:
    """Aggregates over the active memberships, of one club or of all: one `aggregate` operation. Over no rows the
    counts are `0`, `total` its `default` and the others `null`."""
    with run(target, session_name) as calls:
        memberships = Membership.objects.filter(active=True)
        if club is not None:
            memberships = memberships.filter(club__slug=club)
        values = await memberships.aggregate(
            memberships=Count("*"),
            clubs=Count("club", distinct=True),
            paying=Count("pk", filter=Q(fee__gt=0)),
            total=Sum("fee", default=decimal.Decimal(0)),
            average=Avg("fee"),
            lowest=Min("fee"),
            highest=Max("fee"),
            spread=Max("fee") - Min("fee"),
        )
    return traced(FeeTotals(**values), target, session_name, calls)


@router.get("/activities/schedule", summary="Activity schedule", responses=READ_ERRORS, openapi_extra=UNKNOWN_DRIVER)
async def activity_schedule(
    target: ReadTarget,
    session_name: SessionName = "default",
    club: ClubFilter = None,
    kind: Annotated[Kind | None, Query(description="Kind of activity.")] = None,
    starts_after: Annotated[
        datetime.datetime | None, Query(description="Activities starting at or after this time.")
    ] = None,
) -> Traced[list[ScheduledActivity]]:
    """The `Select` of `GET /api/operations/activities`, executed (`operations.activity_schedule_rows()`): one
    `select` that joins the club and the coach, so reading them runs no further operation."""
    result = []
    with run(target, session_name) as calls:
        activities = operations.activity_schedule_rows(
            Activity.objects.all(), club=club, kind=kind, starts_after=starts_after
        )
        async for activity in activities:
            club_row, coach = await activity.club, await activity.coach
            result.append(
                ScheduledActivity(
                    activity=without_extras(activity),
                    club=club_row.name,
                    coach=None if coach is None else f"{coach.first_name} {coach.last_name}",
                    attendees=activity.attendees,
                    free_places=activity.free_places,
                    month=activity.month,
                )
            )
    return traced(result, target, session_name, calls)


@router.get(
    "/invoices/outstanding", summary="Outstanding invoices by club", responses=READ_ERRORS, openapi_extra=UNKNOWN_DRIVER
)
async def outstanding_invoices(
    target: ReadTarget,
    today: Annotated[datetime.date, Query(description="Invoices due before this date are overdue.")],
    session_name: SessionName = "default",
) -> Traced[list[Outstanding]]:
    """The grouped `Select` of `GET /api/operations/invoices/outstanding`, executed (`operations.outstanding_rows()`):
    `values('club__name')` followed by `annotate()`, one row per club."""
    with run(target, session_name) as calls:
        rows = operations.outstanding_rows(Invoice.objects.all(), today)
        result = [
            Outstanding(club=row["club__name"], outstanding=row["outstanding"], invoices=row["invoices"])
            async for row in rows
        ]
    return traced(result, target, session_name, calls)


@router.get("/invoices/months", summary="Invoices by month", responses=READ_ERRORS, openapi_extra=UNKNOWN_DRIVER)
async def invoice_months(
    target: ReadTarget, session_name: SessionName = "default", club: ClubFilter = None
) -> Traced[list[InvoiceMonth]]:
    """Invoices grouped by a computed value: `annotate(month=TruncMonth('issued'))`, then `values('month')` and
    `annotate()` with aggregates, filtered ones included. Oldest month first."""
    with run(target, session_name) as calls:
        invoices = Invoice.objects.all()
        if club is not None:
            invoices = invoices.filter(club__slug=club)
        months = (
            invoices.annotate(month=TruncMonth("issued"))
            .values("month")
            .annotate(
                invoices=Count("pk"),
                billed=Sum("total"),
                open=Count("pk", filter=Q(status="open")),
                paid=Sum("total", filter=Q(status="paid"), default=decimal.Decimal(0)),
            )
            .order_by("month")
        )
        result: list[dict[str, Any]] = [row async for row in months]
    return traced([InvoiceMonth(**row) for row in result], target, session_name, calls)
