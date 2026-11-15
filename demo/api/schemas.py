"""Request bodies and parameter types shared by the demo's endpoints."""

from __future__ import annotations

import datetime
import decimal
import typing

from typing import Annotated

from fastapi import Path
from pydantic import BaseModel, Field, model_validator

from demo.models.activities import Activity
from demo.models.clubs import Member

Level = Member.model_fields["level"].annotation
"""The skill levels: the ``Literal`` of the generated `Member.level`."""
Kind = Activity.model_fields["kind"].annotation
"""The kinds of activity: the ``Literal`` of the generated `Activity.kind`."""

Slug = Annotated[str, Path(description="The club's slug.", pattern=r"^[-a-zA-Z0-9_]+$", examples=["ljubljana-rowing"])]
Id = Annotated[int, Path(description="Primary key.", ge=1)]


class MemberChanges(BaseModel):
    """Fields of a member to change; fields left out stay as they are."""

    model_config = {"extra": "forbid"}

    first_name: str | None = Field(None, max_length=60, description="First name.")
    last_name: str | None = Field(None, max_length=60, description="Last name.")
    email: str | None = Field(None, max_length=254, description="E-mail address.")
    birth_date: datetime.date | None = Field(None, description="Date of birth; `null` clears it.")
    level: Level | None = Field(None, description="Skill level.")
    tags: list[str] | None = Field(None, description="Free-form tags; replaces the member's tags.")

    @model_validator(mode="after")
    def _check_changes(self) -> MemberChanges:
        if not self.model_fields_set:
            raise ValueError("no fields to change")
        for name in sorted(self.model_fields_set):
            if getattr(self, name) is None and type(None) not in typing.get_args(Member.model_fields[name].annotation):
                raise ValueError(f"{name} cannot be null")
        return self


class PaymentIn(BaseModel):
    """A payment of an invoice."""

    model_config = {"extra": "forbid"}

    amount: decimal.Decimal = Field(
        gt=0, max_digits=10, decimal_places=2, examples=["120.00"], description="Amount paid, at most two decimals."
    )
    reference: str = Field("", max_length=40, description="Payment reference, e.g. the bank's.")
    paid_at: datetime.datetime = Field(description="Time of payment, with a time zone.")


class AttendanceIn(BaseModel):
    """A member registering for an activity."""

    model_config = {"extra": "forbid"}

    member_id: int = Field(ge=1, description="Primary key of the member.")
