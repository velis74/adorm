"""``manage.py seed_demo``: fill the demo database with a fixed data set."""

import datetime

from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand
from django.db import transaction

from demo.backend.activities.models import Activity, Attendance, Competition, Tag
from demo.backend.billing.models import Invoice, InvoiceLine, Payment
from demo.backend.clubs.models import Club, Member, Membership

UTC = datetime.UTC

# (name, slug, founded, city, address, disciplines)
CLUBS = [
    (
        "Ljubljana Rowing Club",
        "ljubljana-rowing",
        datetime.date(1921, 5, 3),
        "Ljubljana",
        {"street": "Veslaška pot 1"},
        ["Sculling", "Coxed eights", "Fitness"],
    ),
    (
        "Maribor Climbing Club",
        "maribor-climbing",
        datetime.date(1987, 9, 14),
        "Maribor",
        {"street": "Stenska ulica 4"},
        ["Bouldering", "Lead", "Fitness"],
    ),
    ("Koper Sailing Club", "koper-sailing", datetime.date(2004, 4, 1), "Koper", {}, ["Dinghy", "Keelboat"]),
]
MEMBERS = [
    ("Ana", "Novak", "beginner", ["junior"]),
    ("Bojan", "Kovač", "advanced", ["coach"]),
    ("Cvetka", "Horvat", "intermediate", []),
    ("David", "Krajnc", "beginner", []),
    ("Eva", "Zupančič", "advanced", ["coach", "board"]),
    ("Filip", "Potočnik", "intermediate", ["junior"]),
    ("Gaja", "Kos", "beginner", []),
    ("Hana", "Vidmar", "intermediate", []),
    ("Igor", "Golob", "advanced", ["veteran"]),
    ("Jana", "Turk", "beginner", ["junior"]),
    ("Klemen", "Kralj", "intermediate", []),
    ("Lara", "Božič", "advanced", ["board"]),
]
# (club index, member index, role, fee)
MEMBERSHIPS = [
    (0, 0, "member", "120.00"),
    (0, 1, "coach", "0.00"),
    (0, 2, "member", "120.00"),
    (0, 3, "member", "120.00"),
    (0, 11, "board", "60.00"),
    (1, 4, "coach", "0.00"),
    (1, 5, "member", "95.50"),
    (1, 6, "member", "95.50"),
    (1, 7, "member", "95.50"),
    (1, 2, "member", "95.50"),
    (2, 8, "coach", "0.00"),
    (2, 9, "member", "150.00"),
    (2, 10, "member", "150.00"),
]
TAGS = ["outdoor", "indoor", "youth", "endurance", "technique"]
# (club index, coach member index, name, kind, day offset, hours, capacity, price, tags)
ACTIVITIES = [
    (0, 1, "Morning sculling", "training", 0, 2, 8, "0.00", ["outdoor", "endurance"]),
    (0, 1, "Ergometer session", "training", 2, 1, 12, "0.00", ["indoor", "endurance"]),
    (0, None, "Spring regatta", "competition", 20, 6, 30, "25.00", ["outdoor"]),
    (1, 4, "Bouldering basics", "training", 1, 2, 10, "10.00", ["indoor", "youth", "technique"]),
    (1, 4, "Crag day", "social", 6, 8, 15, "15.00", ["outdoor"]),
    (2, 8, "Dinghy handling", "training", 3, 3, 6, "20.00", ["outdoor", "technique", "youth"]),
]
# (activity index, discipline, entry deadline day offset): the competition is added to an existing activity row
COMPETITION_OF_ACTIVITY = (2, "Coxed eights", 10)
# (club index, coach member index, name, day offset, hours, capacity, price, tags, discipline, deadline day offset)
COMPETITIONS = [
    (1, 4, "Regional bouldering cup", 13, 5, 40, "12.00", ["indoor", "youth"], "Bouldering", None),
]
# (activity index, member index, status)
ATTENDANCE = [
    (0, 0, "attended"),
    (0, 2, "attended"),
    (0, 3, "cancelled"),
    (1, 0, "registered"),
    (2, 0, "registered"),
    (2, 2, "registered"),
    (2, 11, "registered"),
    (3, 5, "attended"),
    (3, 6, "attended"),
    (4, 5, "registered"),
    (4, 7, "registered"),
    (5, 9, "attended"),
    (5, 10, "registered"),
]
START = datetime.datetime(2026, 3, 2, 7, 0, tzinfo=UTC)


class Command(BaseCommand):
    help = "Fill the demo database with a fixed data set; --reset replaces existing demo data."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="delete the demo data first")

    def handle(self, *args, **options):  # noqa: ARG002 - Django's signature
        with transaction.atomic():
            if options["reset"]:
                for model in (
                    Payment,
                    InvoiceLine,
                    Invoice,
                    Attendance,
                    Competition,
                    Activity,
                    Tag,
                    Membership,
                    Member,
                    Club,
                ):
                    model.objects.all().delete()
            elif Club.objects.exists():
                self.stdout.write("Demo data already present.")
                return
            self.seed()
        self.stdout.write(self.style.SUCCESS("Demo data created."))

    def seed(self) -> None:
        clubs = [
            Club.objects.create(
                name=name, slug=slug, founded=founded, city=city, address=address, disciplines=disciplines
            )
            for name, slug, founded, city, address, disciplines in CLUBS
        ]
        members = [
            Member.objects.create(
                first_name=first,
                last_name=last,
                email=f"{first.lower()}.{index}@example.com",
                birth_date=datetime.date(1970 + 3 * index, 1 + index, 10),
                level=level,
                joined=START - datetime.timedelta(days=40 * index),
                tags=tags,
            )
            for index, (first, last, level, tags) in enumerate(MEMBERS)
        ]
        memberships = [
            Membership.objects.create(
                club=clubs[c], member=members[m], role=role, since=datetime.date(2024, 1 + m % 12, 1), fee=Decimal(fee)
            )
            for c, m, role, fee in MEMBERSHIPS
        ]
        memberships[3].active = False
        memberships[3].save(update_fields=["active"])
        tags = {name: Tag.objects.create(name=name) for name in TAGS}
        activities = []
        for c, coach, name, kind, day, hours, capacity, price, tag_names in ACTIVITIES:
            activity = Activity.objects.create(
                club=clubs[c],
                coach=None if coach is None else members[coach],
                name=name,
                kind=kind,
                starts_at=START + datetime.timedelta(days=day),
                duration=datetime.timedelta(hours=hours),
                capacity=capacity,
                price=Decimal(price),
            )
            activity.tags.set([tags[t] for t in tag_names])
            activities.append(activity)
        index, discipline, deadline = COMPETITION_OF_ACTIVITY
        parent = activities[index]
        Competition(
            activity_ptr=parent,
            discipline=discipline,
            entry_deadline=(START + datetime.timedelta(days=deadline)).date(),
        ).save_base(raw=True)
        for c, coach, name, day, hours, capacity, price, tag_names, discipline, deadline in COMPETITIONS:
            competition = Competition.objects.create(
                club=clubs[c],
                coach=members[coach],
                name=name,
                kind="competition",
                starts_at=START + datetime.timedelta(days=day),
                duration=datetime.timedelta(hours=hours),
                capacity=capacity,
                price=Decimal(price),
                discipline=discipline,
                entry_deadline=None if deadline is None else (START + datetime.timedelta(days=deadline)).date(),
            )
            competition.tags.set([tags[t] for t in tag_names])
        for a, m, status in ATTENDANCE:
            activity = activities[a]
            checked_in = activity.starts_at if status == "attended" else None
            Attendance.objects.create(activity=activity, member=members[m], status=status, checked_in_at=checked_in)
        self.seed_billing(memberships, activities)

    def seed_billing(self, memberships: list[Membership], activities: list[Activity]) -> None:
        membership_type = ContentType.objects.get_for_model(Membership)
        activity_type = ContentType.objects.get_for_model(Activity)
        number = 0
        for membership in memberships:
            if membership.fee == 0:
                continue
            number += 1
            issued = datetime.date(2026, 1, 15)
            invoice = Invoice.objects.create(
                club_id=membership.club_id,
                member_id=membership.member_id,
                number=f"2026-{number:04d}",
                issued=issued,
                due=issued + datetime.timedelta(days=30),
                status="paid" if number % 3 else "open",
                total=membership.fee,
            )
            InvoiceLine.objects.create(
                invoice=invoice,
                content_type=membership_type,
                object_id=membership.pk,
                description="Membership fee 2026",
                unit_price=membership.fee,
            )
            if invoice.status == "paid":
                Payment.objects.create(
                    invoice=invoice,
                    amount=invoice.total,
                    paid_at=datetime.datetime(2026, 2, 1, 12, 0, tzinfo=UTC),
                    reference=f"SI00 {number:04d}",
                )
        for activity in activities:
            if activity.price == 0:
                continue
            for attendance in activity.attendances.exclude(status="cancelled"):
                number += 1
                issued = activity.starts_at.date()
                invoice = Invoice.objects.create(
                    club_id=activity.club_id,
                    member_id=attendance.member_id,
                    number=f"2026-{number:04d}",
                    issued=issued,
                    due=issued + datetime.timedelta(days=15),
                    status="open",
                    total=activity.price,
                )
                InvoiceLine.objects.create(
                    invoice=invoice,
                    content_type=activity_type,
                    object_id=activity.pk,
                    description=activity.name,
                    unit_price=activity.price,
                )
