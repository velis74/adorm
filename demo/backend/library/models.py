"""The demo's library: authors, books, their editions, tags, readers and loans of edition copies."""

from django.core.validators import MinLengthValidator, RegexValidator
from django.db import models


class Author(models.Model):
    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=100, unique=True)
    born = models.SmallIntegerField(null=True, blank=True, help_text="Year of birth.")
    country = models.CharField(max_length=60, blank=True)


class Tag(models.Model):
    name = models.CharField(max_length=40, unique=True, validators=[MinLengthValidator(2)])


class Genre(models.TextChoices):
    NOVEL = "novel"
    POETRY = "poetry"
    DRAMA = "drama"
    ESSAY = "essay"
    SCIENCE = "science"


class Book(models.Model):
    title = models.CharField(max_length=200)
    genre = models.CharField(max_length=20, choices=Genre.choices, default=Genre.NOVEL)
    first_published = models.SmallIntegerField(help_text="Year of first publication.")
    pages = models.PositiveIntegerField()
    authors = models.ManyToManyField(Author, related_name="books")
    tags = models.ManyToManyField(Tag, related_name="books", blank=True)


class Format(models.TextChoices):
    HARDCOVER = "hardcover"
    PAPERBACK = "paperback"
    EBOOK = "ebook"


class Edition(models.Model):
    """A published edition of a book; the library owns ``copies`` copies, ``available`` of them on the shelf."""

    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name="editions")
    isbn = models.CharField(max_length=13, unique=True, validators=[RegexValidator(r"^\d{13}\Z")])
    publisher = models.CharField(max_length=100)
    year = models.SmallIntegerField()
    format = models.CharField(max_length=10, choices=Format.choices, default=Format.PAPERBACK)
    price = models.DecimalField(max_digits=8, decimal_places=2)
    copies = models.PositiveSmallIntegerField(default=1)
    available = models.PositiveSmallIntegerField(default=1)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(available__lte=models.F("copies")), name="library_edition_available_le_copies"
            )
        ]


class Reader(models.Model):
    name = models.CharField(max_length=100)
    email = models.EmailField(unique=True)
    joined = models.DateField()


class Loan(models.Model):
    """A copy of an edition lent to a reader; ``returned`` is empty while the reader has it."""

    edition = models.ForeignKey(Edition, on_delete=models.PROTECT, related_name="loans")
    reader = models.ForeignKey(Reader, on_delete=models.CASCADE, related_name="loans")
    borrowed = models.DateField()
    due = models.DateField()
    returned = models.DateField(null=True, blank=True)
