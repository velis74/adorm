"""``manage.py seed_library``: fill the demo database with the fixed library data set."""

import datetime

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import connection, transaction

from demo.backend.library.models import Author, Book, Edition, Loan, Reader, Tag

TODAY = datetime.date(2026, 3, 2)
"""The date the data set's loans are relative to; the examples use it as today."""

# slug: (name, born, country)
AUTHORS = {
    "austen": ("Jane Austen", 1775, "United Kingdom"),
    "dickens": ("Charles Dickens", 1812, "United Kingdom"),
    "tolstoy": ("Leo Tolstoy", 1828, "Russia"),
    "dostoevsky": ("Fyodor Dostoevsky", 1821, "Russia"),
    "presern": ("France Prešeren", 1800, "Slovenia"),
    "cankar": ("Ivan Cankar", 1876, "Slovenia"),
    "shelley": ("Mary Shelley", 1797, "United Kingdom"),
    "darwin": ("Charles Darwin", 1809, "United Kingdom"),
    "woolf": ("Virginia Woolf", 1882, "United Kingdom"),
    "kafka": ("Franz Kafka", 1883, "Austria-Hungary"),
    "ibsen": ("Henrik Ibsen", 1828, "Norway"),
    "montaigne": ("Michel de Montaigne", 1533, "France"),
    "wollstonecraft": ("Mary Wollstonecraft", 1759, "United Kingdom"),
}
TAGS = ["classic", "romance", "society", "war", "philosophy", "horror", "nature", "slovenian", "modernism", "family"]
# (title, genre, first published, pages, author slugs, tags)
BOOKS = [
    ("Pride and Prejudice", "novel", 1813, 432, ["austen"], ["classic", "romance", "society"]),
    ("Emma", "novel", 1815, 474, ["austen"], ["classic", "romance"]),
    ("Persuasion", "novel", 1817, 249, ["austen"], ["romance"]),
    ("Great Expectations", "novel", 1861, 505, ["dickens"], ["classic", "society"]),
    ("Bleak House", "novel", 1853, 1017, ["dickens"], ["society"]),
    ("A Christmas Carol", "novel", 1843, 104, ["dickens"], ["classic", "family"]),
    ("War and Peace", "novel", 1869, 1225, ["tolstoy"], ["classic", "war", "family"]),
    ("Anna Karenina", "novel", 1878, 864, ["tolstoy"], ["classic", "romance", "society"]),
    ("Crime and Punishment", "novel", 1866, 671, ["dostoevsky"], ["classic", "philosophy"]),
    ("The Brothers Karamazov", "novel", 1880, 796, ["dostoevsky"], ["philosophy", "family"]),
    ("Poezije", "poetry", 1847, 216, ["presern"], ["slovenian", "romance"]),
    ("Krst pri Savici", "poetry", 1836, 64, ["presern"], ["slovenian", "classic"]),
    ("Na klancu", "novel", 1902, 248, ["cankar"], ["slovenian", "family"]),
    ("Hlapec Jernej in njegova pravica", "novel", 1907, 96, ["cankar"], ["slovenian", "society"]),
    ("Frankenstein", "novel", 1818, 280, ["shelley"], ["classic", "horror"]),
    ("On the Origin of Species", "science", 1859, 502, ["darwin"], ["nature", "classic"]),
    ("The Voyage of the Beagle", "science", 1839, 496, ["darwin"], ["nature"]),
    ("Mrs Dalloway", "novel", 1925, 194, ["woolf"], ["modernism", "society"]),
    ("To the Lighthouse", "novel", 1927, 209, ["woolf"], ["modernism", "family"]),
    ("The Trial", "novel", 1925, 255, ["kafka"], ["modernism", "philosophy"]),
    ("A Doll's House", "drama", 1879, 112, ["ibsen"], ["society", "family"]),
    ("Peer Gynt", "drama", 1867, 256, ["ibsen"], ["philosophy"]),
    ("Essays", "essay", 1580, 1344, ["montaigne"], ["philosophy", "classic"]),
    ("Frankenstein and Vindication: two radical works", "essay", 2001, 520, ["shelley", "wollstonecraft"], ["horror"]),
]
PUBLISHERS = ["Penguin Classics", "Oxford World's Classics", "Mladinska knjiga", "Vintage", "Wordsworth Editions"]
FORMATS = ["paperback", "hardcover", "ebook"]
READERS = [
    ("Ana Novak", "ana@example.com"),
    ("Bojan Kovač", "bojan@example.com"),
    ("Cvetka Horvat", "cvetka@example.com"),
    ("David Krajnc", "david@example.com"),
    ("Eva Zupančič", "eva@example.com"),
    ("Filip Potočnik", "filip@example.com"),
    ("Gaja Kos", "gaja@example.com"),
    ("Hana Vidmar", "hana@example.com"),
]


def isbn(book: int, edition: int) -> str:
    """The data set's ISBN of edition ``edition`` of book ``book`` (both counted from 0)."""
    return f"978000{book:03d}{edition:03d}{(book * 7 + edition) % 10}"


def edition_count(book: int) -> int:
    """How many editions book ``book`` has: one to three."""
    return 1 + (book * 5) % 3


def seed() -> None:
    """Insert the data set into an empty database."""
    authors = {
        slug: Author.objects.create(name=name, slug=slug, born=born, country=country)
        for slug, (name, born, country) in AUTHORS.items()
    }
    tags = {name: Tag.objects.create(name=name) for name in TAGS}
    editions = []
    for index, (title, genre, published, pages, slugs, book_tags) in enumerate(BOOKS):
        book = Book.objects.create(title=title, genre=genre, first_published=published, pages=pages)
        book.authors.set([authors[slug] for slug in slugs])
        book.tags.set([tags[name] for name in book_tags])
        for number in range(edition_count(index)):
            copies = 1 + (index + number) % 4
            editions.append(
                Edition.objects.create(
                    book=book,
                    isbn=isbn(index, number),
                    publisher=PUBLISHERS[(index + number) % len(PUBLISHERS)],
                    year=max(published, 1990) + 7 * number + index % 7,
                    format=FORMATS[(index + number) % len(FORMATS)],
                    price=Decimal(9 + (index * 3 + number * 5) % 21) + Decimal("0.99"),
                    copies=copies,
                    available=copies,
                )
            )
    readers = [
        Reader.objects.create(name=name, email=email, joined=TODAY - datetime.timedelta(days=40 * (i + 1)))
        for i, (name, email) in enumerate(READERS)
    ]
    for i in range(30):
        edition = editions[(i * 7) % len(editions)]
        borrowed = TODAY - datetime.timedelta(days=3 + i * 6)
        returned = None if i % 3 == 0 or edition.available == 0 else borrowed + datetime.timedelta(days=10 + i % 9)
        if returned is None and edition.available == 0:
            continue
        Loan.objects.create(
            edition=edition,
            reader=readers[i % len(readers)],
            borrowed=borrowed,
            due=borrowed + datetime.timedelta(days=21),
            returned=returned,
        )
        if returned is None:
            edition.available -= 1
            edition.save(update_fields=["available"])


def clear() -> None:
    """Delete every row of the library app and restart its sequences."""
    with connection.cursor() as cursor:
        tables = ", ".join(
            connection.ops.quote_name(model._meta.db_table)
            for model in (Loan, Reader, Edition, Book.tags.through, Book.authors.through, Book, Tag, Author)
        )
        cursor.execute(f"TRUNCATE {tables} RESTART IDENTITY")  # noqa: S608 - the app's own table names


class Command(BaseCommand):
    help = "Fill the demo database with the library data set; --reset replaces the data first."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="delete the library data first")

    def handle(self, *args, **options):  # noqa: ARG002 - Django's signature
        with transaction.atomic():
            if options["reset"]:
                clear()
            elif Author.objects.exists():
                self.stdout.write("Library data already present.")
                return
            seed()
        self.stdout.write(self.style.SUCCESS("Library data created."))
