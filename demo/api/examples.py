"""The examples the demo runs on every driver: queries and writes on the library models.

Each example is a coroutine function on the generated models; `EXAMPLES` holds them with their parameters. A write
example commits; its ``undo`` coroutine, which the demo runs unrecorded after it, restores the data set.
"""

from __future__ import annotations

import asyncio
import datetime
import inspect
import textwrap

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import adorm

from adorm import Avg, Count, Exists, F, Max, Min, OuterRef, Q, Subquery, Sum, TruncMonth
from demo.models.library import Author, Book, Edition, Loan, Reader, Tag

TODAY = datetime.date(2026, 3, 2)
"""The date the data set's loans are relative to."""

GENRES = ["", "novel", "poetry", "drama", "essay", "science"]


@dataclass(frozen=True)
class Param:
    """A parameter of an example: ``kind`` is ``text``, ``int`` (from ``minimum`` to ``maximum`` when they are given)
    or ``choice`` (one of ``choices``)."""

    name: str
    label: str
    kind: str
    default: Any
    choices: tuple[Any, ...] = ()
    minimum: int | None = None
    maximum: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "default": self.default,
            "choices": list(self.choices),
            "minimum": self.minimum,
            "maximum": self.maximum,
        }

    def parse(self, value: Any) -> Any:
        """``value`` as the example takes it: an ``int`` parameter an integer (not a bool) or a string of digits, a
        ``text`` or ``choice`` parameter a string; ``ValueError`` when it does not fit."""
        if self.kind == "int":
            if isinstance(value, str) and value.isascii() and value.isdigit():
                value = int(value)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or (self.minimum is not None and value < self.minimum)
                or (self.maximum is not None and value > self.maximum)
            ):
                raise ValueError(f"{self.name} is an integer from {self.minimum} to {self.maximum}")
            return value
        if not isinstance(value, str):
            raise ValueError(f"{self.name} is a string")
        if self.kind == "choice" and value not in self.choices:
            raise ValueError(f"{self.name} is one of {', '.join(map(repr, self.choices))}")
        return value


@dataclass(frozen=True)
class Example:
    """An example: ``function(**params)`` returns its result; ``undo(result)`` reverts the writes of one run."""

    id: str
    title: str
    description: str
    function: Callable[..., Awaitable[Any]]
    params: tuple[Param, ...] = ()
    undo: Callable[[Any], Awaitable[None]] | None = None
    code: str = field(default="", repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "code": self.code,
            "params": [param.as_dict() for param in self.params],
            "writes": self.undo is not None,
        }

    def arguments(self, values: dict[str, Any]) -> dict[str, Any]:
        """The arguments of ``function`` from ``values``: the defaults overridden by the values given."""
        unknown = sorted(set(values) - {param.name for param in self.params})
        if unknown:
            raise ValueError(f"unknown parameters: {', '.join(unknown)}")
        return {param.name: param.parse(values.get(param.name, param.default)) for param in self.params}


EXAMPLES: dict[str, Example] = {}


def source(function: Callable[..., Any]) -> str:
    """The source of ``function`` without its decorators."""
    lines = textwrap.dedent(inspect.getsource(function)).splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(("async def", "def")))
    return "\n".join(lines[start:])


def example(
    title: str,
    description: str,
    *params: Param,
    undo: Callable[[Any], Awaitable[None]] | None = None,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Register the decorated coroutine function as an example named after it."""

    def register(function: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        EXAMPLES[function.__name__] = Example(
            function.__name__, title, description, function, params, undo, source(function)
        )
        return function

    return register


FIRST_ISBN = "9780000010018"
SEARCH = Param("search", "Title or author contains", "text", "the")
GENRE = Param("genre", "Genre", "choice", "", tuple(GENRES))
ISBN = Param("isbn", "ISBN", "text", FIRST_ISBN)


@example(
    "Filter, Q and ordering",
    "A condition across a many-to-many relation, an OR of two conditions, ordering and a slice. Building the query "
    "does no I/O; async for runs it.",
    SEARCH,
    GENRE,
)
async def search_books(search: str, genre: str) -> list[dict]:
    books = Book.objects.filter(Q(title__icontains=search) | Q(authors__name__icontains=search))
    if genre:
        books = books.filter(genre=genre)
    books = books.distinct().order_by("first_published", "title")[:10]
    return [
        {"id": book.id, "title": book.title, "genre": book.genre, "year": book.first_published} async for book in books
    ]


@example(
    "get() and relations",
    "get() with select_related() loads the edition and its book in one query; awaiting a relation that is not "
    "loaded runs a query, and a many-to-many relation is a query set.",
    ISBN,
)
async def edition_details(isbn: str) -> dict:
    edition = await Edition.objects.select_related("book").get(isbn=isbn)
    book = await edition.book  # loaded by select_related(): no query
    authors = [author.name async for author in book.authors.order_by("name")]
    other_editions = await book.editions.exclude(pk=edition.pk).count()
    return {
        "title": book.title,
        "authors": authors,
        "publisher": edition.publisher,
        "year": edition.year,
        "available": f"{edition.available} of {edition.copies}",
        "other_editions": other_editions,
    }


@example(
    "prefetch_related()",
    "One query for the authors and one for all their books, instead of one query per author.",
    Param("country", "Country", "choice", "United Kingdom", ("United Kingdom", "Russia", "Slovenia", "Norway")),
)
async def authors_with_books(country: str) -> list[dict]:
    authors = Author.objects.filter(country=country).prefetch_related("books").order_by("name")
    return [
        {"author": author.name, "books": ", ".join(sorted([book.title async for book in author.books.all()]))}
        async for author in authors
    ]


@example(
    "aggregate()",
    "Aggregates over the whole table in one query.",
)
async def stock() -> dict:
    return await Edition.objects.aggregate(
        editions=Count("id"),
        copies=Sum("copies"),
        available=Sum("available"),
        average_price=Avg("price"),
        cheapest=Min("price"),
        dearest=Max("price"),
    )


@example(
    "annotate() across relations",
    "Each author with the number of books, editions and loans, counted across the relations; Count(distinct=True) "
    "keeps the joins from multiplying the counts.",
)
async def busiest_authors() -> list[dict]:
    authors = Author.objects.annotate(
        books_count=Count("books", distinct=True),
        editions_count=Count("books__editions", distinct=True),
        loans_count=Count("books__editions__loans", distinct=True),
    ).order_by("-loans_count", "name")[:8]
    return [
        {"author": a.name, "books": a.books_count, "editions": a.editions_count, "loans": a.loans_count}
        async for a in authors
    ]


@example(
    "values() and grouping",
    "values() followed by annotate() groups the rows: loans per month, with how many are still out.",
)
async def loans_per_month() -> list[dict]:
    months = (
        Loan.objects.annotate(month=TruncMonth("borrowed"))
        .values("month")
        .annotate(loans=Count("id"), still_out=Count("id", filter=Q(returned=None)))
        .order_by("month")
    )
    return [row async for row in months]


@example(
    "Exists() and Subquery()",
    "Correlated subqueries: whether a book has a copy on the shelf, and the price of its cheapest edition.",
    GENRE,
)
async def books_on_the_shelf(genre: str) -> list[dict]:
    editions = Edition.objects.filter(book=OuterRef("pk"))
    books = Book.objects.annotate(
        on_shelf=Exists(editions.filter(available__gt=0)),
        cheapest=Subquery(editions.order_by("price").values("price")[:1]),
    ).order_by("title")
    if genre:
        books = books.filter(genre=genre)
    return [{"title": b.title, "on_shelf": b.on_shelf, "cheapest": b.cheapest} async for b in books]


@example(
    "Overdue loans",
    "select_related() over two foreign keys; values of the related rows come with the loans.",
)
async def overdue_loans() -> list[dict]:
    loans = Loan.objects.filter(returned=None, due__lt=TODAY).select_related("reader", "edition").order_by("due")
    return [
        {
            "reader": (await loan.reader).name,
            "isbn": (await loan.edition).isbn,
            "due": loan.due,
            "days_late": (TODAY - loan.due).days,
        }
        async for loan in loans
    ]


@example(
    "Concurrent queries",
    "asyncio.gather() runs three queries at once: outside a transaction each takes a slot of its own.",
)
async def dashboard() -> dict:
    books, out, readers = await asyncio.gather(
        Book.objects.count(),
        Loan.objects.filter(returned=None).count(),
        Reader.objects.filter(loans__returned=None).distinct().count(),
    )
    return {"books": books, "loans_out": out, "readers_with_loans": readers}


async def _return(result: dict) -> None:
    async with adorm.atomic():
        await Loan.objects.filter(pk=result["loan"]).delete()
        await Edition.objects.filter(isbn=result["isbn"]).update(available=F("available") + 1)


@example(
    "A transaction with a row lock",
    "Lending a copy: select_for_update() locks the edition until the transaction ends; the loan and the stock "
    "change commit together, or not at all.",
    ISBN,
    Param("reader", "Reader", "choice", "ana@example.com", ("ana@example.com", "bojan@example.com", "eva@example.com")),
    undo=_return,
)
async def lend(isbn: str, reader: str) -> dict:
    async with adorm.atomic():
        edition = await Edition.objects.select_for_update().get(isbn=isbn)
        if edition.available == 0:
            raise LookupError(f"no copy of {isbn} on the shelf")
        borrower = await Reader.objects.get(email=reader)
        loan = await Loan.objects.create(
            edition_id=edition.id, reader_id=borrower.id, borrowed=TODAY, due=TODAY + datetime.timedelta(days=21)
        )
        await Edition.objects.filter(pk=edition.pk).update(available=F("available") - 1)
    return {"loan": loan.id, "isbn": isbn, "available_before": edition.available}


async def _remove_tag(result: dict) -> None:
    if result["created"]:
        await Tag.objects.filter(name=result["tag"]).delete()


@example(
    "get_or_create()",
    "Reads the tag, or creates it when it does not exist.",
    Param("name", "Tag", "text", "adventure"),
    undo=_remove_tag,
)
async def ensure_tag(name: str) -> dict:
    tag, created = await Tag.objects.get_or_create(name=name)
    return {"tag": tag.name, "id": tag.id, "created": created}


async def _remove_readers(result: list[dict]) -> None:
    await Reader.objects.filter(email__in=[reader["email"] for reader in result]).delete()


@example(
    "bulk_create()",
    "Several rows in one INSERT; the models come back with their primary keys.",
    Param("count", "Readers", "int", 3, minimum=1, maximum=20),
    undo=_remove_readers,
)
async def enrol_readers(count: int) -> list[dict]:
    readers = await Reader.objects.bulk_create(
        [Reader(name=f"New reader {n}", email=f"new{n}@example.com", joined=TODAY) for n in range(1, count + 1)]
    )
    return [{"id": reader.id, "name": reader.name, "email": reader.email} for reader in readers]


@example(
    "Custom connection options",
    "The desk profile's on_acquire hook sets the custom option app.reader to the session's name for each lease; "
    "connection_options() overrides it inside a transaction (SET LOCAL) and resets it outside one.",
)
async def desk_options() -> dict:
    desk = adorm.create_session(f"{adorm.current_session().using}-desk", "example")
    with desk.activate():
        loans = await Loan.objects.count()  # SET "app"."reader" = 'example', from on_acquire
        async with adorm.atomic():
            with adorm.connection_options(**{"app.reader": "audit"}):
                overdue = await Loan.objects.filter(due__lt=TODAY, returned__isnull=True).count()  # SET LOCAL
        with adorm.connection_options(**{"app.reader": None}):
            readers = await Reader.objects.count()  # RESET "app"."reader"
    return {"loans": loans, "overdue": overdue, "readers": readers}
