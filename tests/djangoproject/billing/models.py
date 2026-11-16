"""Invoices for membership fees and activities, and their payments."""

import uuid

from decimal import Decimal

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models


class InvoiceStatus(models.TextChoices):
    DRAFT = "draft"
    OPEN = "open"
    PAID = "paid"
    VOID = "void"


class Invoice(models.Model):
    club = models.ForeignKey("clubs.Club", on_delete=models.CASCADE, related_name="invoices")
    member = models.ForeignKey("clubs.Member", on_delete=models.PROTECT, related_name="invoices")
    number = models.CharField(max_length=20, unique=True)
    issued = models.DateField()
    due = models.DateField()
    status = models.CharField(max_length=10, choices=InvoiceStatus.choices, default=InvoiceStatus.DRAFT)
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    def __str__(self) -> str:
        return self.number

    def clean(self) -> None:
        if self.due is not None and self.issued is not None and self.due < self.issued:
            raise ValidationError({"due": "The due date cannot be before the issue date."})


class InvoiceLine(models.Model):
    """A line billing a membership fee or an activity: ``item`` is a generic foreign key."""

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    content_type = models.ForeignKey(ContentType, on_delete=models.PROTECT)
    object_id = models.PositiveBigIntegerField()
    item = GenericForeignKey("content_type", "object_id")
    description = models.CharField(max_length=200)
    quantity = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    unit_price = models.DecimalField(max_digits=8, decimal_places=2)

    class Meta:
        indexes = [models.Index(fields=["content_type", "object_id"])]

    def __str__(self) -> str:
        return self.description


class Payment(models.Model):
    code = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="payments")
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    paid_at = models.DateTimeField()
    reference = models.CharField(max_length=40, blank=True)

    def __str__(self) -> str:
        return f"{self.amount} for {self.invoice}"
