"""Models of the ``library`` test app: the Django constructs `adorm generate` translates."""

import uuid

from decimal import Decimal

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.contrib.postgres.fields import ArrayField
from django.core.validators import MaxValueValidator, MinLengthValidator, MinValueValidator, RegexValidator
from django.db import models
from django.utils import timezone


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
