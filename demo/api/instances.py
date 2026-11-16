"""The demo's endpoints on instances and their related objects: prefetching, rows by key, generic foreign keys and
validation, executed on the executor.

They are ``/api/data`` endpoints: each runs in the session ``(<using>, <X-Adorm-Session header>)`` and answers
`demo.api.data.Traced`; the reads run on the profile of the driver ``X-Adorm-Driver`` names (the active driver without
it), the invoice line and the validations on the active driver. A driver that does not support an operation the request
needs answers 501 (the memory driver refuses ``in_bulk()`` by a field other than the primary key and ``full_clean()``).
"""

from __future__ import annotations

import datetime
import decimal

from contextlib import AsyncExitStack
from typing import Annotated, Any, Generic, Literal, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field

import adorm

from adorm import Prefetch
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
    WriteTarget,
)
from demo.api.schemas import Id, Slug
from demo.models.activities import Activity
from demo.models.billing import Invoice, InvoiceLine
from demo.models.clubs import Club, Member, Membership

router = APIRouter(prefix="/api/data", tags=["instances"], dependencies=[Depends(require_driver)])

M = TypeVar("M", bound=adorm.Model)

ITEM_MODELS: dict[str, type[Membership] | type[Activity]] = {
    "clubs.Membership": Membership,
    "activities.Activity": Activity,
}
"""The models an invoice line bills, by label."""
MAX_QUANTITY = 2_147_483_647
"""The largest quantity the column holds."""
MAX_TOTAL = decimal.Decimal("99999999.99")
"""The largest total an invoice holds (10 digits, 2 decimal places)."""
ItemLabel = Literal["clubs.Membership", "activities.Activity"]
InvoiceStatus = Invoice.model_fields["status"].annotation
Role = Membership.model_fields["role"].annotation


class RosterMember(BaseModel):
    """An active member of a club."""

    member: Member = Field(description="The member.")
    role: str = Field(description="The membership's role.")
    since: datetime.date = Field(description="The membership's start.")
    fee: decimal.Decimal = Field(description="The membership's yearly fee.")


class RosterActivity(BaseModel):
    """An activity of a club."""

    activity: Activity = Field(description="The activity.")
    participants: list[str] = Field(
        description="`<first name> <last name>` of the members with an attendance of any status, by last name."
    )


class Roster(BaseModel):
    """A club with its active members and its activities, read with `prefetch_related()`."""

    club: Club = Field(description="The club.")
    members: list[RosterMember] = Field(description="Its active memberships, oldest first, with their members.")
    activities: list[RosterActivity] = Field(description="Its activities, earliest first, with their participants.")


class MembersByKey(BaseModel):
    """Members read by primary key with `in_bulk()`."""

    found: dict[int, Member] = Field(description="The members found, by primary key.")
    missing: list[int] = Field(description="The requested keys no member has, in request order.")


class ClubsByKey(BaseModel):
    """Clubs read by slug with `in_bulk(field_name='slug')`."""

    found: dict[str, Club] = Field(description="The clubs found, by slug.")
    missing: list[str] = Field(description="The requested slugs no club has, in request order.")


class BilledLine(BaseModel):
    """An invoice line with the object its generic foreign key `item` points at."""

    line: InvoiceLine = Field(description="The line.")
    model: ItemLabel | None = Field(description="Label of the item's model; `null` when the item's row is gone.")
    item: Membership | Activity | None = Field(description="The billed membership or activity, or `null`.")
    club: str | None = Field(description="Slug of the item's club (its foreign key `club`), or `null`.")


class ItemRef(BaseModel):
    """A row an invoice line bills."""

    model_config = {"extra": "forbid"}

    model: ItemLabel = Field(description="Label of the row's model.")
    id: int = Field(ge=1, description="Primary key of the row.")


class LineIn(BaseModel):
    """A line to add to an invoice."""

    model_config = {"extra": "forbid"}

    item: ItemRef = Field(description="The membership or activity the line bills.")
    description: str = Field(max_length=200, description="Text of the line.")
    quantity: int = Field(1, ge=1, le=MAX_QUANTITY, description="Number of units.")
    unit_price: decimal.Decimal = Field(
        ge=0, max_digits=8, decimal_places=2, examples=["25.00"], description="Price of one unit."
    )


class LineAdded(BaseModel):
    """The line an invoice got and the invoice's new total."""

    line: InvoiceLine = Field(description="The new line; `content_type_id` names the item's model.")
    invoice: Invoice = Field(description="The invoice, its total raised by `quantity * unit_price`.")


class InvoiceChanges(BaseModel):
    """Values to validate on an invoice instead of the stored ones; fields left out keep their stored value."""

    model_config = {"extra": "forbid"}

    number: str | None = Field(None, description="Invoice number; must stay unique.")
    issued: datetime.date | None = Field(None, description="Issue date.")
    due: datetime.date | None = Field(None, description="Due date; not before the issue date (the model's `clean()`).")
    status: InvoiceStatus | None = Field(None, description="Status.")
    total: decimal.Decimal | None = Field(None, description="Total; the Django field's digits apply.")


class MembershipIn(BaseModel):
    """A membership to validate."""

    model_config = {"extra": "forbid"}

    club_id: int = Field(ge=1, description="Primary key of the club.")
    member_id: int = Field(ge=1, description="Primary key of the member.")
    role: Role = Field("member", description="Role.")
    since: datetime.date = Field(description="Start of the membership.")
    fee: decimal.Decimal = Field(description="Yearly fee; the Django field's digits and minimum apply.")


class FieldError(BaseModel):
    """One error of `full_clean()`."""

    message: str = Field(
        description="The message, its params filled in, in the Django project's language (`LANGUAGE_CODE`)."
    )
    code: str | None = Field(description="Django's error code (`max_length`, `unique_together` …); `null` without.")
    params: dict[str, Any] = Field(description="Django's params of the message, as JSON.")


class Validation(BaseModel, Generic[M]):
    """The outcome of `full_clean()`; nothing is stored."""

    valid: bool = Field(description="Whether the instance validated.")
    errors: dict[str, list[FieldError]] = Field(
        description="The errors by adorm field or relation name, `__all__` for those of the whole instance."
    )
    instance: M = Field(description="The instance validated; with `valid`, holding the values the validation cleaned.")


ExcludeFields = Annotated[
    list[str], Query(description="Names of fields (or relations) left out of the validation; repeat the parameter.")
]


def roster_of(slug: str) -> adorm.QuerySet:
    """The club ``slug`` on the current session, with its active memberships (``to_attr``), their members, its
    activities and their participants prefetched."""
    active = Membership.objects.filter(active=True).order_by("since", "id")
    return Club.objects.filter(slug=slug).prefetch_related(
        Prefetch("memberships", queryset=active, to_attr="active_memberships"),
        "active_memberships__member",
        Prefetch("activities", queryset=Activity.objects.order_by("starts_at", "id")),
        Prefetch("activities__participants", queryset=Member.objects.order_by("last_name", "first_name", "id")),
    )


@router.get(
    "/clubs/{slug}/roster",
    summary="Club roster",
    responses=NOT_FOUND | EXECUTOR_ERRORS,
    openapi_extra=UNKNOWN_DRIVER,
)
async def club_roster(
    slug: Slug,
    target: ReadTarget,
    session_name: SessionName = "default",
    atomic: Annotated[bool, Query(description="Read in one transaction (one slot), not a slot per operation.")] = False,
) -> Traced[Roster]:
    """The club with `prefetch_related()`: a `Prefetch` of the active memberships (a query set and `to_attr`), their
    members, the activities and their participants (a many-to-many relation through attendances). Five `select`
    operations after the `get`, one per relation and level (two for the many-to-many relation); reading the related
    objects afterwards runs none. Without `atomic` each operation takes a slot of its own."""
    with run(target, session_name) as calls:
        async with AsyncExitStack() as stack:
            if atomic:
                await stack.enter_async_context(adorm.atomic())
            try:
                club = await roster_of(slug).get()
            except Club.DoesNotExist as exc:
                raise not_found(exc) from None
            members = [
                RosterMember(
                    member=await membership.member, role=membership.role, since=membership.since, fee=membership.fee
                )
                for membership in club.active_memberships
            ]
            activities = [
                RosterActivity(
                    activity=activity,
                    participants=[f"{m.first_name} {m.last_name}" async for m in activity.participants],
                )
                async for activity in club.activities
            ]
    roster = Roster(club=without_extras(club), members=members, activities=activities)
    return traced(roster, target, session_name, calls)


MemberIds = Annotated[list[int], Query(alias="id", min_length=1, max_length=100, description="Primary keys.")]
ClubSlugs = Annotated[list[str], Query(alias="slug", min_length=1, max_length=100, description="Club slugs.")]


@router.get("/members/in-bulk", summary="Members by key", responses=EXECUTOR_ERRORS, openapi_extra=UNKNOWN_DRIVER)
async def members_in_bulk(
    ids: MemberIds, target: ReadTarget, session_name: SessionName = "default"
) -> Traced[MembersByKey]:
    """The members with the given primary keys (`id`, repeated), in one `in_bulk` operation."""
    with run(target, session_name) as calls:
        found = await Member.objects.in_bulk(ids)
    missing = [pk for pk in dict.fromkeys(ids) if pk not in found]
    return traced(MembersByKey(found=dict(sorted(found.items())), missing=missing), target, session_name, calls)


@router.get(
    "/clubs/in-bulk",
    summary="Clubs by slug",
    responses=EXECUTOR_ERRORS | NOT_SUPPORTED,
    openapi_extra=UNKNOWN_DRIVER,
)
async def clubs_in_bulk(
    slugs: ClubSlugs, target: ReadTarget, session_name: SessionName = "default"
) -> Traced[ClubsByKey]:
    """The clubs with the given slugs (`slug`, repeated), in one `in_bulk` operation on the unique field `slug`."""
    with run(target, session_name) as calls:
        found = await Club.objects.in_bulk(slugs, field_name="slug")
    missing = [slug for slug in dict.fromkeys(slugs) if slug not in found]
    return traced(ClubsByKey(found=dict(sorted(found.items())), missing=missing), target, session_name, calls)


async def billed(line: InvoiceLine, item: Membership | Activity | None) -> BilledLine:
    club = None if item is None else (await item.club).slug
    model = None if item is None else type(item)._meta.label
    return BilledLine(line=without_extras(line), model=model, item=item, club=club)


@router.get(
    "/invoices/{pk}/lines",
    summary="Invoice lines and what they bill",
    responses=NOT_FOUND | EXECUTOR_ERRORS,
    openapi_extra=UNKNOWN_DRIVER,
)
async def invoice_lines(
    pk: Id,
    target: ReadTarget,
    session_name: SessionName = "default",
    prefetch: Annotated[
        bool,
        Query(
            description=(
                "Prefetch the items and their clubs (`Prefetch('item', to_attr='billed')`, `billed__club`); without "
                "it each line awaits its `item` and the item its `club`, one operation each."
            )
        ),
    ] = True,
) -> Traced[list[BilledLine]]:
    """The invoice's lines with the membership or activity each one bills (the generic foreign key `item`) and that
    object's club. After the lines, a prefetch reads the content types `adorm.content_types` has not cached, then the
    items and their clubs with one `select` per item model each."""
    with run(target, session_name) as calls:
        if not await Invoice.objects.filter(pk=pk).exists():
            raise HTTPException(404, f"no invoice has the primary key {pk}")
        lines = InvoiceLine.objects.filter(invoice_id=pk).order_by("id")
        if prefetch:
            lines = lines.prefetch_related(Prefetch("item", to_attr="billed"), "billed__club")
        result = [await billed(line, line.billed if prefetch else await line.item) async for line in lines]
    return traced(result, target, session_name, calls)


@router.post(
    "/invoices/{pk}/lines",
    summary="Add an invoice line",
    status_code=201,
    responses=NOT_FOUND
    | EXECUTOR_ERRORS
    | {
        409: {
            "description": "The invoice is neither a draft nor open, or its total would exceed 99999999.99; nothing is "
            "stored (body `{detail}`)."
        }
    },
)
async def add_invoice_line(
    pk: Id, body: LineIn, target: WriteTarget, session_name: SessionName = "default"
) -> Traced[LineAdded]:
    """Add a line billing a membership or an activity and raise the invoice's total, in one transaction that locks the
    invoice (`select_for_update()`). Assigning the item to the generic foreign key sets `object_id`; `create()`
    sets `content_type_id` through `adorm.content_types`, which reads the content type once and caches it."""
    with run(target, session_name) as calls:
        async with adorm.atomic():
            try:
                invoice = await Invoice.objects.select_for_update().get(pk=pk)
                item = await ITEM_MODELS[body.item.model].objects.get(pk=body.item.id)
            except adorm.ObjectDoesNotExist as exc:
                raise not_found(exc) from None
            if invoice.status not in ("draft", "open"):
                raise HTTPException(409, f"invoice {pk} is {invoice.status}")
            if invoice.total + body.quantity * body.unit_price > MAX_TOTAL:
                raise HTTPException(409, f"the total of invoice {pk} would exceed {MAX_TOTAL}")
            line = await InvoiceLine.objects.create(
                invoice_id=invoice.id,
                item=item,
                description=body.description,
                quantity=body.quantity,
                unit_price=body.unit_price,
            )
            invoice.total += line.quantity * line.unit_price
            await invoice.save(update_fields=("total",))
    return traced(LineAdded(line=line, invoice=invoice), target, session_name, calls)


def validation(instance: M, error: adorm.ValidationError | None) -> Validation[M]:
    errors = {
        name: [
            FieldError(
                message=next(iter(item)),
                code=item.code,
                params=jsonable_encoder(dict(item.params or {})),
            )
            for item in items
        ]
        for name, items in ({} if error is None else error.error_dict).items()
    }
    return Validation[type(instance)](valid=error is None, errors=errors, instance=instance)


VALIDATION_ERRORS = EXECUTOR_ERRORS | NOT_SUPPORTED


@router.post(
    "/invoices/{pk}/validation",
    summary="Validate invoice changes",
    responses=NOT_FOUND | VALIDATION_ERRORS,
    openapi_extra={
        "responses": {
            "422": {
                "description": "Validation error: the request does not validate, or `exclude` names "
                "a name that is no field or relation of `Invoice`."
            }
        }
    },
)
async def validate_invoice(
    pk: Id,
    changes: InvoiceChanges,
    target: WriteTarget,
    session_name: SessionName = "default",
    exclude: ExcludeFields = [],  # noqa: B006 - FastAPI copies the default
) -> Traced[Validation[Invoice]]:
    """Read the invoice, assign the values in the body and `full_clean()` it as Django validates it before a save:
    the fields, the model's `clean()` (the due date not before the issue date) and the unique number. Nothing is
    stored."""
    with run(target, session_name) as calls:
        try:
            invoice = await Invoice.objects.get(pk=pk)
        except Invoice.DoesNotExist as exc:
            raise not_found(exc) from None
        for name, value in changes.model_dump(exclude_unset=True).items():
            setattr(invoice, name, value)
        result = await clean(invoice, exclude)
    return traced(result, target, session_name, calls)


@router.post(
    "/memberships/validation",
    summary="Validate a new membership",
    responses=VALIDATION_ERRORS,
    openapi_extra={
        "responses": {
            "422": {
                "description": "Validation error: the request does not validate, or `exclude` names "
                "a name that is no field or relation of `Membership`."
            }
        }
    },
)
async def validate_membership(
    body: MembershipIn,
    target: WriteTarget,
    session_name: SessionName = "default",
    exclude: ExcludeFields = [],  # noqa: B006 - FastAPI copies the default
) -> Traced[Validation[Membership]]:
    """`full_clean()` a membership that is not stored: the fields (the fee's digits and minimum),
    the constraint that a member joins a club once and the foreign keys' rows. Nothing is stored."""
    membership = Membership.model_construct(**body.model_dump())
    with run(target, session_name) as calls:
        result = await clean(membership, exclude)
    return traced(result, target, session_name, calls)


async def clean(instance: M, exclude: list[str]) -> Validation[M]:
    """`full_clean()` of ``instance``; a name in ``exclude`` that names nothing answers 422."""
    try:
        await instance.full_clean(exclude=exclude)
    except adorm.ValidationError as exc:
        return validation(instance, exc)
    except ValueError as exc:
        error = {"type": "value_error", "loc": ("query", "exclude"), "msg": str(exc), "input": exclude}
        raise RequestValidationError([error]) from None
    return validation(instance, None)
