"""Models of the ``library`` test app: the Django constructs `adorm generate` translates."""

import uuid

from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.contrib.postgres.fields import ArrayField
from django.core.validators import MaxValueValidator, MinLengthValidator, MinValueValidator, RegexValidator
from django.db import models, NotSupportedError
from django.utils import timezone

from adorm import client_base, expose


class Genre(models.TextChoices):
    FICTION = "fiction"
    POETRY = "poetry"
    SCIENCE = "science"


class Author(models.Model):
    """A person who writes books."""

    name = models.CharField(max_length=100, help_text="Full name as printed on the cover.")
    slug = models.SlugField(max_length=100, unique=True)
    born = models.DateField(null=True, blank=True)
    rating = models.PositiveSmallIntegerField(default=3, validators=[MaxValueValidator(5)])
    website = models.URLField(blank=True)
    account = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    mentor = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="pupils")


class Tag(models.Model):
    name = models.CharField(max_length=40, unique=True, validators=[MinLengthValidator(2)])


class Book(models.Model):
    isbn = models.CharField(max_length=13, primary_key=True, validators=[RegexValidator(r"^\d{13}\Z")])
    title = models.CharField(max_length=200)
    genre = models.CharField(max_length=20, choices=Genre.choices, default=Genre.FICTION)
    pages = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    price = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    weight = models.FloatField(null=True, blank=True)
    published = models.DateTimeField(default=timezone.now)
    added = models.DateTimeField(auto_now_add=True)
    reading_time = models.DurationField(null=True, blank=True)
    cover = models.BinaryField(null=True, blank=True)
    keywords = ArrayField(models.CharField(max_length=30), default=list, blank=True, size=10)
    extra = models.JSONField(default=dict, blank=True)
    in_print = models.BooleanField(default=True)
    schema = models.CharField(max_length=10, blank=True)
    authors = models.ManyToManyField(Author, related_name="books")
    tags = models.ManyToManyField(Tag, through="Tagging", related_name="books")
    reviews = GenericRelation("library.Note")


class Edition(Book):
    """A later edition: multi-table inheritance from `Book`."""

    number = models.PositiveSmallIntegerField(default=2)
    released = models.DateField()


class BookProxy(Book):
    class Meta:
        proxy = True


class Tagging(models.Model):
    book = models.ForeignKey(Book, on_delete=models.CASCADE, db_column="book_isbn")
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE, related_name="taggings")
    weight = models.SmallIntegerField(choices=[(1, "low"), (2, "medium"), (3, "high")], default=1)


class Note(models.Model):
    """A note on any object, with fields named like pydantic methods."""

    code = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.CharField(max_length=40)
    item = GenericForeignKey("content_type", "object_id")
    json = models.TextField(blank=True)
    copy = models.ForeignKey(Book, on_delete=models.CASCADE, null=True, blank=True, related_name="copy_notes")
    schema = models.CharField(max_length=20, default="v1")
    model_dump = models.BooleanField(default=False)
    score = models.DecimalField(max_digits=4, decimal_places=1, default=Decimal("2.5"))


class Shelf(models.Model):
    code = models.CharField(max_length=10, unique=True)


class Placement(models.Model):
    """A book on a shelf: a foreign key to a field other than the primary key, and ``order_with_respect_to``."""

    shelf = models.ForeignKey(Shelf, on_delete=models.CASCADE, to_field="code", related_name="placements")
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name="placements")

    class Meta:
        order_with_respect_to = "book"


class ReaderQuerySet(models.QuerySet):
    def create(self, **kwargs):
        """Create a reader; ``username`` names the reader's account and is not stored."""
        kwargs.pop("username", None)
        return super().create(**kwargs)


class Reader(models.Model):
    """A library member: the query set's ``create()`` overrides Django's."""

    name = models.CharField(max_length=100)
    email = models.EmailField(blank=True)

    objects = ReaderQuerySet.as_manager()


class LoanPolicyQuerySet(models.QuerySet):
    def delete(self):
        raise NotSupportedError("loan policies are deleted one at a time")


class LoanPolicy(models.Model):
    """Lending rules: the query set forbids deleting several at once."""

    name = models.CharField(max_length=40, unique=True)
    max_days = models.PositiveSmallIntegerField(default=14)

    objects = LoanPolicyQuerySet.as_manager()


@client_base("tests.djangoproject.library.adorm_bases:LoanQuerySetBase")
class LoanQuerySet(models.QuerySet):
    def overdue(self, today):
        return self.filter(due__lt=today)

    @expose
    def archive(self, reason: str = "") -> int:
        """Archive the loans with ``reason`` as their note; returns the number of loans."""
        return self.update(archived=True, note=reason)


class LoanManager(models.Manager.from_queryset(LoanQuerySet)):
    def get_queryset(self):
        return super().get_queryset().filter(archived=False)

    @expose
    def lend(self, reader_id: int, book_id: str, *, days: int = 14) -> "Loan":
        """Lend a book for ``days`` days."""
        return self.create(reader_id=reader_id, book_id=book_id, due=timezone.localdate() + timedelta(days=days))


class Loan(models.Model):
    """A book lent to a reader: the default manager hides archived loans and has driver methods."""

    reader = models.ForeignKey(Reader, on_delete=models.CASCADE, related_name="loans")
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name="loans")
    due = models.DateField()
    archived = models.BooleanField(default=False)
    note = models.CharField(max_length=200, blank=True)

    objects = LoanManager()
    all_loans = models.Manager()


class Fine(models.Model):
    """A fine for a loan: the loan cannot be deleted while it has fines, and deleting a fine waives it."""

    loan = models.ForeignKey(Loan, on_delete=models.RESTRICT, related_name="fines")
    amount = models.DecimalField(max_digits=6, decimal_places=2)
    waived = models.BooleanField(default=False)

    def delete(self, using=None, keep_parents=False):  # noqa: ARG002 - Django's signature
        self.waived = True
        self.save(using=using, update_fields=["waived"])


class Reservation(models.Model):
    """A hold on a copy of a book: model constraints, and a ``save()`` that validates the instance first."""

    reader = models.ForeignKey(Reader, on_delete=models.CASCADE, related_name="reservations")
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name="reservations")
    copy = models.PositiveSmallIntegerField(default=1)
    starts = models.DateField()
    ends = models.DateField()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(ends__gte=models.F("starts")),
                name="library_reservation_ends_after_start",
                violation_error_code="ends_before_start",
                violation_error_message="A reservation cannot end before it starts.",
            ),
            models.UniqueConstraint(fields=["book", "copy"], name="library_reservation_one_per_copy"),
        ]

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
