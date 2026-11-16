"""A small data set of the club domain for the memory driver, and the executor set up on it."""

import asyncio
import datetime

from collections.abc import Callable
from contextlib import asynccontextmanager
from decimal import Decimal

import adorm

from adorm.settings import close_pools
from tests import memory_driver as memory

UTC = datetime.UTC
START = datetime.datetime(2026, 3, 2, 7, 0, tzinfo=UTC)


def member(pk, first, last, level="beginner", birth_date=None, tags=()):
    return {
        "id": pk,
        "first_name": first,
        "last_name": last,
        "email": f"{first.lower()}@example.com",
        "birth_date": birth_date,
        "level": level,
        "joined": START - datetime.timedelta(days=pk),
        "tags": list(tags),
    }


def tables() -> dict[str, list[dict]]:
    """Two clubs, four members, three memberships, two activities (capacities 2 and 5), one attendance and three
    invoices (open, paid, draft)."""
    return {
        "clubs.Club": [
            {"id": 1, "name": "Rowing", "slug": "rowing", "founded": datetime.date(1921, 5, 3), "city": "Ljubljana",
             "address": {"street": "Veslaška pot 1"}, "disciplines": ["Sculling", "Fitness"]},
            {"id": 2, "name": "Sailing", "slug": "sailing", "founded": datetime.date(2004, 4, 1), "city": "Koper",
             "address": {}, "disciplines": ["Dinghy"]},
        ],
        "clubs.Member": [
            member(1, "Ana", "Novak", "beginner", datetime.date(2001, 1, 10), ["junior"]),
            member(2, "Bojan", "Kovač", "advanced", None, ["coach"]),
            member(3, "Cvetka", "Horvat", "intermediate", datetime.date(1990, 6, 1)),
            member(4, "David", "Novak", "beginner", None),
        ],
        "clubs.Membership": [
            {"id": 1, "club_id": 1, "member_id": 1, "role": "member", "since": datetime.date(2024, 1, 1),
             "fee": Decimal("120.00"), "active": True},
            {"id": 2, "club_id": 1, "member_id": 2, "role": "coach", "since": datetime.date(2024, 2, 1),
             "fee": Decimal("0.00"), "active": True},
            {"id": 3, "club_id": 1, "member_id": 3, "role": "member", "since": datetime.date(2024, 3, 1),
             "fee": Decimal("120.00"), "active": False},
        ],
        "activities.Activity": [
            {"id": 1, "club_id": 1, "coach_id": 2, "name": "Sculling", "kind": "training", "starts_at": START,
             "duration": datetime.timedelta(hours=2), "capacity": 2, "price": Decimal("0.00")},
            {"id": 2, "club_id": 1, "coach_id": None, "name": "Regatta", "kind": "competition",
             "starts_at": START + datetime.timedelta(days=20), "duration": datetime.timedelta(hours=6),
             "capacity": 5, "price": Decimal("25.00")},
        ],
        "activities.Attendance": [
            {"id": 1, "activity_id": 1, "member_id": 1, "status": "registered", "checked_in_at": None},
        ],
        "billing.Invoice": [
            {"id": 1, "club_id": 1, "member_id": 1, "number": "2026-0001", "issued": datetime.date(2026, 1, 15),
             "due": datetime.date(2026, 2, 14), "status": "open", "total": Decimal("120.00")},
            {"id": 2, "club_id": 1, "member_id": 3, "number": "2026-0002", "issued": datetime.date(2026, 1, 15),
             "due": datetime.date(2026, 2, 14), "status": "paid", "total": Decimal("120.00")},
            {"id": 3, "club_id": 2, "member_id": 4, "number": "2026-0003", "issued": datetime.date(2026, 1, 15),
             "due": datetime.date(2026, 2, 14), "status": "draft", "total": Decimal("80.00")},
        ],
        "billing.Payment": [],
    }  # fmt: skip


def generic_tables() -> dict[str, list[dict]]:
    """`tables()` with the content types of memberships and activities and two lines of the open invoice 1: one
    billing membership 1, one billing activity 2."""
    data = tables()
    data["contenttypes.ContentType"] = [
        {"id": 7, "app_label": "clubs", "model": "membership"},
        {"id": 8, "app_label": "activities", "model": "activity"},
    ]
    line = {"invoice_id": 1, "quantity": 1}
    data["billing.InvoiceLine"] = [
        {"id": 1, "content_type_id": 7, "object_id": 1, "description": "Fee", "unit_price": Decimal("120.00"), **line},
        {
            "id": 2,
            "content_type_id": 8,
            "object_id": 2,
            "description": "Regatta",
            "unit_price": Decimal("0.00"),
            **line,
        },
    ]
    return data


@asynccontextmanager
async def executor(database: memory.MemoryDatabase, **pool):
    """adorm configured with the profile ``default`` on a `MemoryDriver` of ``database``; pools closed on exit."""
    profile = adorm.ConnectionProfile(
        "default", memory.MemoryDriver(database), on_acquire=memory.on_acquire, on_release=memory.on_release
    )
    adorm.configure(profiles={"default": profile}, pool={"max_connections": 4, "acquire_timeout": 2.0, **pool})
    try:
        yield database
    finally:
        await close_pools()
        adorm.configure()


async def until(predicate: Callable[[], bool], steps: int = 10_000) -> None:
    """Yield to the event loop until ``predicate()`` is true; fail after ``steps`` turns."""
    for _ in range(steps):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("the condition was not reached")
