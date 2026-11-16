# Quick start

A Django project with PostgreSQL, a FastAPI application that reads and writes its data with adorm on asyncpg.

## Install

```bash
pip install "adorm[asyncpg]" fastapi uvicorn
```

The `asyncpg` extra installs asyncpg, Django and psycopg 3. For a database other than PostgreSQL install the `django`
extra and use the Django driver (see [Configure adorm](#configure-adorm)).

## Django models

The schema is defined and migrated by Django, as usual:

```python
# mysite/library/models.py
from django.db import models


class Author(models.Model):
    name = models.CharField(max_length=100)
    born = models.DateField(null=True, blank=True)


class Book(models.Model):
    title = models.CharField(max_length=200)
    author = models.ForeignKey(Author, on_delete=models.CASCADE, related_name="books")
    published = models.DateField()
    copies = models.PositiveIntegerField(default=1)
```

```bash
python manage.py makemigrations library
python manage.py migrate
```

## Generate the adorm models

```bash
adorm generate --settings mysite.settings --output mysite/adorm_models library
```

```text
wrote mysite/adorm_models/__init__.py
wrote mysite/adorm_models/library.py
2 written, 0 removed, 0 unchanged.
```

The generated module is checked into the repository and regenerated after every model change;
`adorm generate --check` fails when it is stale ([Generating models](/howto/generating-models)).
`mysite/adorm_models/library.py`:

```python
class Author(Model):
    _meta = ModelMeta("library.Author", schema_hash="5bbba558fb8a2821")
    id: int | None = None
    name: str = Field(max_length=100)
    born: datetime.date | None = None
    books = ReverseForeignKey("library.Book", column="author_id")


class Book(Model):
    _meta = ModelMeta("library.Book", schema_hash="1820a538fa801396")
    id: int | None = None
    title: str = Field(max_length=200)
    author_id: int
    published: datetime.date
    copies: int = Field(default=1, ge=0)
    author = ForeignKey("library.Author")
```

The models are pydantic models: `Book.model_json_schema()` and FastAPI response models work on them. `author` and
`books` are relations: `await book.author` loads the author, `author.books` is a query.

## Configure adorm

Once per process: set Django up, then register a connection profile. The profile `default` uses Django's database
alias `default` from `DATABASES`.

```python
# mysite/main.py
import os
from contextlib import asynccontextmanager

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mysite.settings")
django.setup()

import adorm
from fastapi import FastAPI, HTTPException

from mysite.library_service import author_overview, lend

adorm.configure(profiles={"default": {"driver": "adorm.drivers.asyncpg.AsyncpgDriver", "alias": "default"}})


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await adorm.settings.close_pools()


app = FastAPI(lifespan=lifespan)


@app.get("/authors/{name}")
async def get_author(name: str) -> dict:
    return await author_overview(name)


@app.post("/books/{book_id}/loans")
async def post_loan(book_id: int) -> dict:
    try:
        book = await lend(book_id)
    except LookupError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"title": book.title, "copies_left": book.copies}
```

| Driver | Profile | Runs |
|---|---|---|
| native PostgreSQL | `"driver": "adorm.drivers.asyncpg.AsyncpgDriver"` | on the event loop through asyncpg, no threads |
| Django ORM | `"driver": "adorm.drivers.django.DjangoDriver"` | the synchronous Django ORM on one thread per slot, on any database Django supports |

The application code is the same on both drivers. [Connection profiles](/topics/profiles) covers pools, several
database users and the Django setting `ADORM`.

## Write queries

```python
# mysite/library_service.py
import asyncio
import datetime

import adorm

from adorm import Count, F, Q, Sum

from mysite.adorm_models.library import Author, Book


async def author_overview(name: str) -> dict:
    author = await Author.objects.get(name__iexact=name)
    books = author.books.order_by("-published")                  # a query; nothing runs yet

    total, shelf = await asyncio.gather(                         # two queries at once, two slots
        books.count(),
        Book.objects.aggregate(titles=Count("id"), on_loan=Count("id", filter=Q(copies=0)), copies=Sum("copies")),
    )
    newest = await books.select_related("author").first()
    recent = books.filter(published__gte=datetime.date(2000, 1, 1)).values_list("title", flat=True)
    return {
        "author": author.name,
        "titles": total,
        "since_2000": [title async for title in recent],
        "newest": f"{newest.title} by {(await newest.author).name}" if newest else None,
        "shelf": shelf,
    }


async def lend(book_id: int) -> Book:
    async with adorm.atomic():                                   # one slot, one transaction
        book = await Book.objects.select_for_update().get(pk=book_id)
        if book.copies == 0:
            raise LookupError(f"no copy of {book.title!r} left")
        await Book.objects.filter(pk=book.pk).update(copies=F("copies") - 1)
    return await Book.objects.get(pk=book_id)
```

- Building a query (`filter()`, `order_by()`, `select_related()`) does no I/O. `await` runs it: `get()`, `first()`,
  `count()`, `aggregate()`, `update()`; `async for` iterates the rows.
- A call outside a transaction takes a slot from the pool for that call only. `async with adorm.atomic()` holds one
  slot until the block ends and commits, or rolls back on an exception.
- `select_for_update()` locks the row until the transaction ends: two concurrent `lend()` calls for the last copy
  run one after the other, and the second raises `LookupError`.

With two books of Ursula K. Le Guin in the database (*The Dispossessed*, 1974, two copies; *The Telling*, 2000,
one copy):

```bash
uvicorn mysite.main:app
```

```console
$ curl localhost:8000/authors/ursula%20k.%20le%20guin
{"author":"Ursula K. Le Guin","titles":2,"since_2000":["The Telling"],"newest":"The Telling by Ursula K. Le Guin","shelf":{"titles":2,"on_loan":0,"copies":3}}
$ curl -X POST localhost:8000/books/2/loans
{"title":"The Telling","copies_left":0}
$ curl -X POST localhost:8000/books/2/loans
{"detail":"no copy of 'The Telling' left"}
```

## Next

- [Making queries](/topics/queries): lookups, `Q`, `F`, relations, set operations.
- [Transactions](/topics/transactions): savepoints, `on_commit()`, timeouts.
- [Sessions](/topics/sessions) and [the pool](/topics/pool): slots, acquisition timeouts, deadlock detection.
- [Testing](/topics/testing): adorm under Django's `TestCase`.
