"""The operations behind the demo's endpoints, built from request parameters without I/O."""

from __future__ import annotations

import datetime
import decimal

from collections.abc import Mapping
from typing import Any, Literal

from adorm import Case, Coalesce, Count, Exists, F, OuterRef, Q, Subquery, Sum, TruncMonth, Value, When
from adorm.operations import Create, Delete, Get, GetOrCreate, Save, Select, Update
from adorm.query import Query
from demo.models.activities import Activity, Attendance
from demo.models.billing import Invoice, Payment
from demo.models.clubs import Club, Member, Membership

MemberOrdering = Literal["name", "-name", "joined", "-joined"]
MEMBER_ORDERING: Mapping[str, tuple[str, ...]] = {
    "name": ("last_name", "first_name"),
    "-name": ("-last_name", "-first_name"),
    "joined": ("joined",),
    "-joined": ("-joined",),
}


def label(model: Any) -> str:
    return model._meta.label


def pk_name(model: Any) -> str:
    return model._meta.pk


def member_search(
    *,
    club: str | None = None,
    level: str | None = None,
    search: str | None = None,
    joined_after: datetime.date | None = None,
    ordering: MemberOrdering = "name",
    offset: int = 0,
    limit: int = 20,
) -> Select:
    """Members of a club (active memberships), by level, by a name fragment and joined on or after a date (UTC): a
    page of rows."""
    query = Query(label(Member))
    if club is not None:
        query = query.filter(memberships__club__slug=club, memberships__active=True)
    if level is not None:
        query = query.filter(level=level)
    if search:
        query = query.filter(Q(first_name__icontains=search) | Q(last_name__icontains=search))
    if joined_after is not None:
        query = query.filter(joined__gte=datetime.datetime.combine(joined_after, datetime.time(), datetime.UTC))
    query = query.distinct().order_by(*MEMBER_ORDERING[ordering])
    return Select(query[offset : offset + limit])


def club_report(slug: str) -> Get:
    """One club with aggregates over its memberships, invoices and activities.

    Only the active members are counted over a join; the fee and invoice totals are subqueries, since a second
    multi-valued join would repeat each membership row once per invoice.
    """
    fees = (
        Query(label(Membership))
        .filter(club=OuterRef("pk"), active=True)
        .values("club")
        .annotate(total=Sum("fee"))
        .values("total")
    )
    open_invoices = (
        Query(label(Invoice))
        .filter(club=OuterRef("pk"), status="open")
        .values("club")
        .annotate(number=Count("pk"))
        .values("number")
    )
    competitions = Query(label(Activity)).filter(club=OuterRef("pk"), kind="competition")
    next_activity = Query(label(Activity)).filter(club=OuterRef("pk")).order_by("starts_at").values("name")[:1]
    query = (
        Query(label(Club))
        .filter(slug=slug)
        .annotate(
            active_members=Count("memberships", filter=Q(memberships__active=True)),
            fees=Coalesce(Subquery(fees), Value(decimal.Decimal(0))),
            open_invoices=Coalesce(Subquery(open_invoices), Value(0)),
            has_competitions=Exists(competitions),
            next_activity=Subquery(next_activity),
        )
        .annotate(
            size=Case(
                When(active_members__gte=5, then=Value("large")),
                When(active_members__gte=3, then=Value("medium")),
                default=Value("small"),
            )
        )
    )
    return Get(query)


def activity_schedule(
    *, club: str | None = None, kind: str | None = None, starts_after: datetime.datetime | None = None
) -> Select:
    """Activities with their club and coach, attendance counts, free places and month: ordered by start."""
    not_cancelled = ~Q(attendances__status="cancelled")
    query = Query(label(Activity)).select_related("club", "coach")
    if club is not None:
        query = query.filter(club__slug=club)
    if kind is not None:
        query = query.filter(kind=kind)
    if starts_after is not None:
        query = query.filter(starts_at__gte=starts_after)
    query = (
        query.annotate(attendees=Count("attendances", filter=not_cancelled), month=TruncMonth("starts_at"))
        .annotate(free_places=F("capacity") - F("attendees"))
        .order_by("starts_at")
    )
    return Select(query)


def outstanding_by_club(today: datetime.date) -> Select:
    """Overdue open invoices grouped by club: the sum and number per club, largest first."""
    query = (
        Query(label(Invoice))
        .filter(status="open", due__lt=today)
        .values("club__name")
        .annotate(outstanding=Sum("total"), invoices=Count("id"))
        .order_by("-outstanding")
    )
    return Select(query)


def create_member(member: Member) -> Create:
    """Insert ``member`` with all its fields but the primary key (defaults included)."""
    pk = pk_name(Member)
    return Create(label(Member), {name: getattr(member, name) for name in Member.model_fields if name != pk})


def update_member(pk: int, changes: Mapping[str, Any]) -> Save:
    """Save the given fields of member ``pk``; only those fields are written."""
    return Save(label(Member), pk, dict(changes), update_fields=tuple(changes))


def register_attendance(activity_id: int, member_id: int) -> GetOrCreate:
    """The member's attendance of the activity, registered when there is none."""
    return GetOrCreate(
        Query(label(Attendance)),
        {"activity_id": activity_id, "member_id": member_id},
        {"status": "registered"},
    )


def cancel_attendance(pk: int) -> Update:
    """Mark attendance ``pk`` cancelled unless the member already attended."""
    query = Query(label(Attendance)).filter(pk=pk).exclude(status="attended")
    return Update(query, {"status": "cancelled", "checked_in_at": None})


def pay_invoice(pk: int, amount: decimal.Decimal, reference: str, paid_at: datetime.datetime) -> list[Any]:
    """Record a payment of invoice ``pk`` and mark the invoice paid when it is open."""
    return [
        Create(label(Payment), {"invoice_id": pk, "amount": amount, "reference": reference, "paid_at": paid_at}),
        Update(Query(label(Invoice)).filter(pk=pk, status="open"), {"status": "paid"}),
    ]


def delete_draft_invoices(club: str) -> Delete:
    """Delete the club's draft invoices (their lines cascade)."""
    return Delete(Query(label(Invoice)).filter(club__slug=club, status="draft"))
