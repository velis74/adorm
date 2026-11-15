"""adorm models of the demo domain, one per Django model in ``demo.backend`` and ``contenttypes.ContentType``, with
the same labels, field names and relation columns."""

import datetime
import decimal
import uuid

from typing import Any, Literal

from pydantic import Field

from adorm import ForeignKey, ManyToMany, Model, ModelMeta, ReverseForeignKey

Level = Literal["beginner", "intermediate", "advanced"]
Role = Literal["member", "coach", "board"]
Kind = Literal["training", "competition", "social"]
AttendanceStatus = Literal["registered", "attended", "cancelled"]
InvoiceStatus = Literal["draft", "open", "paid", "void"]


class ContentType(Model):
    _meta = ModelMeta("contenttypes.ContentType")
    id: int | None = None
    app_label: str = Field(max_length=100)
    model: str = Field(max_length=100)


class Club(Model):
    _meta = ModelMeta("clubs.Club")
    id: int | None = None
    name: str = Field(max_length=100)
    slug: str = Field(max_length=100, pattern=r"^[-a-zA-Z0-9_]+$")
    founded: datetime.date
    city: str = Field(max_length=100)
    address: dict[str, Any] = Field(default_factory=dict)
    memberships = ReverseForeignKey("clubs.Membership", column="club_id")
    members = ManyToMany("clubs.Member", through="clubs.Membership", source_column="club_id", target_column="member_id")
    activities = ReverseForeignKey("activities.Activity", column="club_id")
    invoices = ReverseForeignKey("billing.Invoice", column="club_id")


class Member(Model):
    _meta = ModelMeta("clubs.Member")
    id: int | None = None
    first_name: str = Field(max_length=60)
    last_name: str = Field(max_length=60)
    email: str = Field(max_length=254)
    birth_date: datetime.date | None = None
    level: Level = "beginner"
    joined: datetime.datetime
    tags: list[str] = Field(default_factory=list)
    memberships = ReverseForeignKey("clubs.Membership", column="member_id")
    clubs = ManyToMany("clubs.Club", through="clubs.Membership", source_column="member_id", target_column="club_id")
    attendances = ReverseForeignKey("activities.Attendance", column="member_id")
    activities = ManyToMany(
        "activities.Activity", through="activities.Attendance", source_column="member_id", target_column="activity_id"
    )
    coached_activities = ReverseForeignKey("activities.Activity", column="coach_id")
    invoices = ReverseForeignKey("billing.Invoice", column="member_id")


class Membership(Model):
    _meta = ModelMeta("clubs.Membership")
    id: int | None = None
    club_id: int
    member_id: int
    role: Role = "member"
    since: datetime.date
    fee: decimal.Decimal = Field(ge=0, max_digits=8, decimal_places=2)
    active: bool = True
    club = ForeignKey("clubs.Club")
    member = ForeignKey("clubs.Member")


class Tag(Model):
    _meta = ModelMeta("activities.Tag")
    id: int | None = None
    name: str = Field(max_length=40)
    activities = ManyToMany(
        "activities.Activity", through="activities.Activity_tags", source_column="tag_id", target_column="activity_id"
    )


class Activity(Model):
    _meta = ModelMeta("activities.Activity")
    id: int | None = None
    club_id: int
    coach_id: int | None = None
    name: str = Field(max_length=100)
    kind: Kind = "training"
    starts_at: datetime.datetime
    duration: datetime.timedelta
    capacity: int = Field(ge=1)
    price: decimal.Decimal = Field(default=decimal.Decimal(0), max_digits=8, decimal_places=2)
    club = ForeignKey("clubs.Club")
    coach = ForeignKey("clubs.Member")
    tags = ManyToMany(
        "activities.Tag", through="activities.Activity_tags", source_column="activity_id", target_column="tag_id"
    )
    attendances = ReverseForeignKey("activities.Attendance", column="activity_id")
    participants = ManyToMany(
        "clubs.Member", through="activities.Attendance", source_column="activity_id", target_column="member_id"
    )


class Competition(Activity):
    """Multi-table inheritance: a child of `Activity` whose primary key is the link to its parent row."""

    _meta = ModelMeta("activities.Competition", pk="activity_ptr_id")
    activity_ptr_id: int | None = None
    discipline: str = Field(max_length=60)
    entry_deadline: datetime.date | None = None
    activity_ptr = ForeignKey("activities.Activity")


class Attendance(Model):
    _meta = ModelMeta("activities.Attendance")
    id: int | None = None
    activity_id: int
    member_id: int
    status: AttendanceStatus = "registered"
    checked_in_at: datetime.datetime | None = None
    activity = ForeignKey("activities.Activity")
    member = ForeignKey("clubs.Member")


class Invoice(Model):
    _meta = ModelMeta("billing.Invoice")
    id: int | None = None
    club_id: int
    member_id: int
    number: str = Field(max_length=20)
    issued: datetime.date
    due: datetime.date
    status: InvoiceStatus = "draft"
    total: decimal.Decimal = Field(default=decimal.Decimal(0), max_digits=10, decimal_places=2)
    club = ForeignKey("clubs.Club")
    member = ForeignKey("clubs.Member")
    lines = ReverseForeignKey("billing.InvoiceLine", column="invoice_id")
    payments = ReverseForeignKey("billing.Payment", column="invoice_id")


class InvoiceLine(Model):
    _meta = ModelMeta("billing.InvoiceLine")
    id: int | None = None
    invoice_id: int
    content_type_id: int
    object_id: int = Field(ge=0)
    description: str = Field(max_length=200)
    quantity: int = Field(default=1, ge=1)
    unit_price: decimal.Decimal = Field(max_digits=8, decimal_places=2)
    invoice = ForeignKey("billing.Invoice")
    content_type = ForeignKey("contenttypes.ContentType")


class Payment(Model):
    _meta = ModelMeta("billing.Payment", pk="code")
    code: uuid.UUID | None = None
    invoice_id: int
    amount: decimal.Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    paid_at: datetime.datetime
    reference: str = Field(default="", max_length=40)
    invoice = ForeignKey("billing.Invoice")


MODELS: tuple[type[Model], ...] = (
    ContentType,
    Club,
    Member,
    Membership,
    Tag,
    Activity,
    Competition,
    Attendance,
    Invoice,
    InvoiceLine,
    Payment,
)
