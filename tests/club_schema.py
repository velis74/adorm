"""A schema of the club test domain, for encoding operations by type."""

import datetime
import decimal
import enum
import uuid

from typing import Literal

from pydantic import BaseModel

from adorm.schema import ModelSchema, Schema


class Level(enum.Enum):
    BEGINNER = "beginner"
    MASTER = "master"


class Address(BaseModel):
    street: str
    city: str


CLUB = ModelSchema(
    "club.Club",
    "id",
    {"id": int, "name": str, "founded": datetime.date, "owner_id": int | None, "address": Address | None},
    {"owner": "club.Member", "member": "club.Member"},
)
MEMBER = ModelSchema(
    "club.Member",
    "id",
    {
        "id": int,
        "name": str,
        "age": int,
        "joined": datetime.datetime,
        "fee": decimal.Decimal,
        "photo": bytes | None,
        "token": uuid.UUID,
        "level": Level,
        "tags": list[str],
        "status": Literal["active", "left"],
        "club_id": int | None,
    },
    {"club": "club.Club", "payment": "club.Payment"},
)
PAYMENT = ModelSchema(
    "club.Payment",
    "code",
    {"code": uuid.UUID, "amount": decimal.Decimal, "paid_at": datetime.datetime, "member_id": int},
    {"member": "club.Member"},
)

SCHEMA = Schema([CLUB, MEMBER, PAYMENT])
