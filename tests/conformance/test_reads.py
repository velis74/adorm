"""The conformance suite: adorm reads on every bundled driver, against the same PostgreSQL data."""

import asyncio
import datetime

from decimal import Decimal

import pytest

from django.contrib.contenttypes.models import ContentType as DjangoContentType
from django.db import models as django_models
from django.db.models import Q as DjangoQ
from django.utils import translation

import adorm

from adorm import (
    ConnectionProfile,
    DatabaseError,
    DataError,
    F,
    Lower,
    NotSupportedError,
    OperationalError,
    ProgrammingError,
    Q,
    Upper,
    ValidationError,
)
from adorm.drivers import is_connection_error
from adorm.executor import Pool, Router, set_runner
from adorm.operations import Count as CountOperation, Select, Update
from adorm.query import Count, Query, Subquery, Value
from adorm.query.lookups import Exact, IStartsWith
from demo.models.contenttypes import ContentType
from tests.djangoproject.library import models as dj
from tests.generated.library import Author, Book, BookProxy, Edition, Loan, Note, Placement, Reader, Shelf, Tagging
from tests.library_data import (
    DUNE,
    DUNE_2,
    in_thread,
    names,
    NOTE,
    PHYSICS,
    POEMS,
    titles,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(params=["django", "asyncpg"])
def driver_class(request):
    """The driver class under test."""
    if request.param == "django":
        from adorm.drivers.django import DjangoDriver

        return DjangoDriver
    from adorm.drivers.asyncpg import AsyncpgDriver

    return AsyncpgDriver


def connection_class(driver_class):
    from adorm.drivers.asyncpg import AsyncpgConnection, AsyncpgDriver

    return AsyncpgConnection if driver_class is AsyncpgDriver else driver_class.connection_class


@pytest.fixture
async def executor(driver_class):
    """``make(**pool)`` sets up a router with one pool of the driver's slots for the profile ``default``."""
    routers = []

    def make(**kwargs):
        driver = driver_class()
        pool = Pool(driver, ConnectionProfile("default", driver), **kwargs)
        router = Router([pool])
        routers.append(router)
        set_runner(router)
        return pool

    previous = set_runner(None)
    try:
        yield make
    finally:
        set_runner(previous)
        for router in routers:
            await router.close()


@pytest.fixture
async def pool(executor):
    return executor(max_connections=2)


@pytest.fixture
def record_operations(monkeypatch, driver_class):
    """``record_operations()`` returns the kinds of the operations the driver's connections execute from now on."""

    def record():
        kinds = []
        cls = connection_class(driver_class)
        original = cls._execute

        async def _execute(self, operation):
            kinds.append(operation.kind)
            return await original(self, operation)

        monkeypatch.setattr(cls, "_execute", _execute)
        return kinds

    return record


async def test_rows_are_model_instances_with_every_field(library, pool):  # noqa: ARG001
    books = [book async for book in Book.objects.order_by("isbn")]
    assert [type(book) for book in books] == [Book] * 4
    dune = books[0]
    assert (dune.isbn, dune.title, dune.pages, dune.price) == (DUNE, "Dune", 412, Decimal("9.99"))
    assert dune.keywords == ["sand", "spice"]
    assert dune.extra == {"series": "dune"}
    assert dune.get_deferred_fields() == set()
    assert books[1].schema_ == "v2"
    assert books[1].model_dump(include={"schema_"}) == {"schema": "v2"}


@pytest.mark.parametrize(
    ("lookups", "expected"),
    [
        ({"name": "Ann"}, ["Ann"]),
        ({"name__exact": "Ann"}, ["Ann"]),
        ({"name__iexact": "DORA"}, ["dora"]),
        ({"rating__gt": 3}, ["Ann", "dora"]),
        ({"rating__gte": 3}, ["Ann", "Bob", "dora"]),
        ({"rating__lt": 3}, ["Cid"]),
        ({"rating__lte": 3}, ["Bob", "Cid"]),
        ({"rating__in": [1, 5]}, ["Ann", "Cid"]),
        ({"rating__in": {4}}, ["dora"]),
        ({"rating__range": (3, 4)}, ["Bob", "dora"]),
        ({"born__isnull": True}, ["Cid"]),
        ({"born__isnull": False}, ["Ann", "Bob", "dora"]),
        ({"born": None}, ["Cid"]),
        ({"born__gte": datetime.date(1980, 1, 1)}, ["Bob", "dora"]),
        ({"name__contains": "o"}, ["Bob", "dora"]),
        ({"name__icontains": "D"}, ["Cid", "dora"]),
        ({"name__startswith": "d"}, ["dora"]),
        ({"name__istartswith": "D"}, ["dora"]),
        ({"name__endswith": "b"}, ["Bob"]),
        ({"name__iendswith": "B"}, ["Bob"]),
        ({"name__regex": "^[AB]"}, ["Ann", "Bob"]),
        ({"name__iregex": "^[ab]"}, ["Ann", "Bob"]),
        ({"mentor__name": "Ann"}, ["Bob"]),
        ({"mentor__mentor__name": "Ann"}, ["Cid"]),
        ({"mentor__isnull": True}, ["Ann", "dora"]),
        ({"pupils__name": "Bob"}, ["Ann"]),
        ({"books__title": "Physics"}, ["Ann", "Cid"]),
        ({"books__tagging_set__weight": 3}, ["Ann"]),
        ({"pk": 0}, []),
    ],
)
async def test_lookups_and_relation_paths_select_what_django_selects(library, pool, lookups, expected):  # noqa: ARG001
    # Django names a reverse relation without related_name by its query name, adorm by its accessor name.
    django_lookups = {key.replace("tagging_set", "tagging"): value for key, value in lookups.items()}
    django_names = await in_thread(
        lambda: sorted(dj.Author.objects.filter(**django_lookups).values_list("name", flat=True))
    )
    assert sorted(await names(Author.objects.filter(**lookups))) == django_names == expected


@pytest.mark.parametrize(
    ("lookups", "expected"),
    [
        ({"keywords__contains": ["spice"]}, [DUNE]),
        ({"extra__contains": {"series": "dune"}}, [DUNE]),
        ({"schema_": "v2"}, [POEMS]),
        ({"copy_notes__schema_": "v3"}, [DUNE]),
        ({"tagging_set__tag__name": "rare"}, [DUNE, POEMS]),
        ({"edition__number": 2}, [DUNE_2]),
        ({"price__gt": Decimal("5")}, [DUNE]),
        ({"in_print": False}, [PHYSICS]),
    ],
)
async def test_lookups_on_fields_django_types_and_renamed_names(library, pool, lookups, expected):  # noqa: ARG001
    assert [book.isbn async for book in Book.objects.filter(**lookups).order_by("isbn")] == expected


async def test_renamed_relation_and_field_on_the_other_side(library, pool):  # noqa: ARG001
    note = await Note.objects.get(copy___title="Dune", json_="{}", schema_="v3")
    assert (note.code, note.json_, note.schema_, note.copy_id, note.model_dump_) == (NOTE, "{}", "v3", DUNE, False)
    assert await Note.objects.filter(copy___tagging_set__weight=3).count() == 1


async def test_exclude_and_q_combinations(library, pool):  # noqa: ARG001
    assert sorted(await names(Author.objects.exclude(rating__gte=3))) == ["Cid"]
    assert sorted(await names(Author.objects.filter(Q(name="Ann") | Q(mentor__name="Bob")))) == ["Ann", "Cid"]
    assert sorted(await names(Author.objects.filter(~Q(name="Ann"), rating__gte=3))) == ["Bob", "dora"]
    assert sorted(await names(Author.objects.filter(~(Q(rating__lt=3) | Q(born__lt=datetime.date(1975, 1, 1)))))) == [
        "Bob",
        "dora",
    ]
    # NULL is neither excluded nor kept by a negated comparison on it: Django's exclude() keeps the row.
    assert sorted(await names(Author.objects.exclude(born__lt=datetime.date(1985, 1, 1)))) == ["Cid", "dora"]


async def test_lookups_compare_with_expressions_and_instances(library, pool):  # noqa: ARG001
    assert await names(Author.objects.filter(slug=Lower("name")).order_by("name")) == ["Ann", "Bob", "Cid", "dora"]
    assert await names(Author.objects.filter(rating__gt=F("mentor__rating"))) == []
    assert await names(Author.objects.filter(rating__lt=F("mentor__rating")).order_by("name")) == ["Bob", "Cid"]
    assert await names(Author.objects.filter(Exact(Lower("name"), "dora"))) == ["dora"]
    assert await names(Author.objects.filter(Q(IStartsWith(Upper("slug"), "a")) | Q(name="Cid")).order_by("name")) == [
        "Ann",
        "Cid",
    ]
    ann = await Author.objects.get(name="Ann")
    assert await names(Author.objects.filter(mentor=ann)) == ["Bob"]
    assert await names(Author.objects.filter(name=Value("Bob"))) == ["Bob"]


async def test_each_filter_call_joins_a_multi_valued_relation_of_its_own(library, pool):  # noqa: ARG001
    def django_titles(queryset):
        return lambda: sorted(queryset.values_list("title", flat=True))

    await in_thread(lambda: dj.Tagging.objects.create(book_id=DUNE, tag=dj.Tag.objects.create(name="new"), weight=5))
    cases = [
        (
            Book.objects.filter(authors__name="Ann").filter(authors__name="Cid"),
            dj.Book.objects.filter(authors__name="Ann").filter(authors__name="Cid"),
        ),
        (
            Book.objects.filter(Q(authors__name="Ann") & Q(authors__name="Cid")),
            dj.Book.objects.filter(DjangoQ(authors__name="Ann") & DjangoQ(authors__name="Cid")),
        ),
        (
            Book.objects.filter(authors__name="Ann") & Book.objects.filter(authors__name="Cid"),
            dj.Book.objects.filter(authors__name="Ann") & dj.Book.objects.filter(authors__name="Cid"),
        ),
        (
            Book.objects.filter(tagging_set__weight=3).filter(tagging_set__weight=5),
            dj.Book.objects.filter(tagging__weight=3).filter(tagging__weight=5),
        ),
        (
            Book.objects.filter(tagging_set__weight=3, tagging_set__tag__name="new"),
            dj.Book.objects.filter(tagging__weight=3, tagging__tag__name="new"),
        ),
        (
            Book.objects.filter(tagging_set__weight=3).filter(tagging_set__tag__name="new"),
            dj.Book.objects.filter(tagging__weight=3).filter(tagging__tag__name="new"),
        ),
        (
            Book.objects.exclude(authors__name="Ann").exclude(authors__name="Bob"),
            dj.Book.objects.exclude(authors__name="Ann").exclude(authors__name="Bob"),
        ),
    ]
    for adorm_queryset, django_queryset in cases:
        expected = await in_thread(django_titles(django_queryset))
        assert await titles(adorm_queryset) == expected
    assert await titles(Book.objects.filter(authors__name="Ann").filter(authors__name="Cid")) == ["Physics"]
    assert await titles(Book.objects.filter(Q(authors__name="Ann") & Q(authors__name="Cid"))) == []
    physics = await Book.objects.filter(authors__name="Ann").get(authors__name="Cid")
    assert physics.title == "Physics"


async def test_an_instance_stands_for_the_field_a_foreign_keys_to_field_names(library, pool):  # noqa: ARG001
    def shelves():
        first = dj.Shelf.objects.create(code="first")
        # The code of the second shelf is the first shelf's primary key.
        second = dj.Shelf.objects.create(code=str(first.pk))
        dj.Placement.objects.create(shelf=first, book_id=DUNE)
        return first.pk, second.pk

    first_pk, second_pk = await in_thread(shelves)
    first, second = await Shelf.objects.get(pk=first_pk), await Shelf.objects.get(pk=second_pk)
    for lookup, value, pks in [
        ("shelf", first, first_pk),
        ("shelf", second, second_pk),
        ("shelf__in", [first, second], [first_pk, second_pk]),
        ("shelf__in", [second], [second_pk]),
    ]:

        def count(lookup=lookup, pks=pks):
            if isinstance(pks, list):
                instances = list(dj.Shelf.objects.filter(pk__in=pks))
            else:
                instances = dj.Shelf.objects.get(pk=pks)
            return dj.Placement.objects.filter(**{lookup: instances}).count()

        assert await Placement.objects.filter(**{lookup: value}).count() == await in_thread(count)
    assert await Placement.objects.filter(shelf=first).count() == 1
    assert await Placement.objects.filter(shelf=second).count() == 0


async def test_a_typed_value_has_a_django_field(library, pool):  # noqa: ARG001
    rows = Author.objects.order_by("name").annotate(rank=Value(None, output_type=int)).values("name", "rank")
    assert [row async for row in rows][:2] == [{"name": "Ann", "rank": None}, {"name": "Bob", "rank": None}]
    ann = await Author.objects.annotate(rank=Value(None, output_type=int), bonus=Value(2, output_type=int)).get(
        name="Ann"
    )
    assert (ann.rank, ann.bonus) == (None, 2)
    assert await names(Author.objects.filter(rating__gt=Value(4, output_type=int))) == ["Ann"]
    with pytest.raises(NotSupportedError, match="output type tuple"):
        await Author.objects.annotate(pair=Value((1, 2), output_type=tuple)).first()


async def test_a_query_as_subquery_in_in(library, pool):  # noqa: ARG001
    mentors = Author.objects.filter(pupils__isnull=False)
    assert sorted(await names(Author.objects.filter(pk__in=mentors))) == ["Ann", "Bob"]
    assert sorted(await names(Author.objects.filter(pk__in=mentors.values("pk")))) == ["Ann", "Bob"]
    noted = Note.objects.values_list("copy_id", flat=True)
    assert [book.title async for book in Book.objects.filter(isbn__in=noted)] == ["Dune"]
    poets = Author.objects.filter(books__genre="poetry")
    assert sorted(await names(Author.objects.filter(mentor__in=poets))) == ["Cid"]
    assert sorted(await names(Author.objects.exclude(pk__in=mentors.values_list("id")))) == ["Cid", "dora"]
    assert await names(Author.objects.filter(pk__in=mentors.none())) == []
    assert await names(Author.objects.filter(pk__in=Author.objects.order_by("-rating")[:1])) == ["Ann"]


async def test_a_subquery_as_a_value(library, pool):  # noqa: ARG001
    best = Subquery(Author.objects.order_by("-rating").values("rating")[:1])
    assert await names(Author.objects.filter(rating=best)) == ["Ann"]


async def test_ordering(library, pool):  # noqa: ARG001
    assert await names(Author.objects.order_by("name")) == ["Ann", "Bob", "Cid", "dora"]
    assert await names(Author.objects.order_by(Lower("name").desc())) == ["dora", "Cid", "Bob", "Ann"]
    assert await names(Author.objects.order_by(Lower("name"))) == ["Ann", "Bob", "Cid", "dora"]
    assert await names(Author.objects.order_by("-rating", "name")) == ["Ann", "dora", "Bob", "Cid"]
    assert await names(Author.objects.order_by(F("born").asc(nulls_first=True))) == ["Cid", "Ann", "Bob", "dora"]
    assert await names(Author.objects.order_by(F("born").desc(nulls_last=True))) == ["dora", "Bob", "Ann", "Cid"]
    assert await names(Author.objects.order_by("mentor__name", "name")) == ["Bob", "Cid", "Ann", "dora"]
    assert [t.weight async for t in Tagging.objects.order_by("-book__title")] == [1, 3]


async def test_distinct_and_distinct_on(library, pool):  # noqa: ARG001
    assert await Author.objects.filter(books__authors__isnull=False).count() == 6
    assert await Author.objects.filter(books__authors__isnull=False).distinct().count() == 3
    genres = Book.objects.order_by("genre").values_list("genre", flat=True).distinct()
    assert [genre async for genre in genres] == ["fiction", "poetry", "science"]
    first_per_genre = Book.objects.order_by("genre", "-pages").distinct("genre")
    assert [book.title async for book in first_per_genre] == ["Dune II", "Poems", "Physics"]


async def test_slicing(library, pool):  # noqa: ARG001
    ordered = Author.objects.order_by("name")
    assert await names(ordered[1:3]) == ["Bob", "Cid"]
    assert await names(ordered[2:]) == ["Cid", "dora"]
    assert await names(ordered[:1]) == ["Ann"]
    assert await names(ordered[1:3][1:]) == ["Cid"]
    assert await ordered[1:].count() == 3
    assert await ordered[4:].exists() is False
    assert (await ordered[1:].first()).name == "Bob"
    with pytest.raises(TypeError, match="sliced query without an ordering"):
        await Author.objects.all()[1:].first()


async def test_values_and_values_list(library, pool):  # noqa: ARG001
    rows = [row async for row in Author.objects.order_by("name").values("name", "mentor__name", "pk")[:2]]
    assert rows == [
        {"name": "Ann", "mentor__name": None, "pk": library["ann"]},
        {"name": "Bob", "mentor__name": "Ann", "pk": library["bob"]},
    ]
    assert [r async for r in Author.objects.order_by("name").values_list("name", "rating")[:2]] == [
        ("Ann", 5),
        ("Bob", 3),
    ]
    assert [r async for r in Author.objects.order_by("name").values_list("slug", flat=True)] == [
        "ann",
        "bob",
        "cid",
        "dora",
    ]
    named = await Author.objects.values_list("name", "mentor", named=True).get(name="Bob")
    assert (named.name, named.mentor) == ("Bob", library["ann"])
    note = await Note.objects.values().get()
    assert note == {
        "code": NOTE,
        "content_type_id": note["content_type_id"],
        "object_id": DUNE,
        "json_": "{}",
        "copy_id": DUNE,
        "schema_": "v3",
        "model_dump_": False,
        "score": Decimal("2.5"),
    }
    assert list(note) == list(Note.model_fields)
    assert await Book.objects.values_list("schema_", flat=True).get(isbn=POEMS) == "v2"


async def test_only_and_defer(library, pool):  # noqa: ARG001
    ann = await Author.objects.only("name").get(slug="ann")
    assert ann.get_deferred_fields() == {"slug", "born", "rating", "website", "account_id", "mentor_id"}
    assert (ann.id, ann.name) == (library["ann"], "Ann")
    with pytest.raises(AttributeError, match="deferred"):
        _ = ann.slug
    bob = await Author.objects.defer("born", "website").get(slug="bob")
    assert bob.get_deferred_fields() == {"born", "website"}
    with_mentor = await Author.objects.only("name", "mentor").get(slug="bob")
    assert with_mentor.mentor_id == library["ann"]
    assert (await with_mentor.mentor).name == "Ann"
    note = await Note.objects.defer("json_", "copy_").get()
    assert note.get_deferred_fields() == {"json_", "copy_id"}


async def test_none_and_combined_query_sets(library, pool):  # noqa: ARG001
    session = adorm.create_session()
    assert await session.execute(Select(Query("library.Author").none())) == []
    assert await session.execute(CountOperation(Query("library.Author").filter(name="Ann").none())) == 0
    either = Author.objects.filter(name="Ann") | Author.objects.filter(rating__lt=2)
    assert sorted(await names(either)) == ["Ann", "Cid"]
    both = Author.objects.filter(rating__gte=3) & Author.objects.filter(born__gte=datetime.date(1975, 1, 1))
    assert sorted(await names(both)) == ["Bob", "dora"]


async def test_get_first_last_exists_count(library, pool):  # noqa: ARG001
    assert (await Author.objects.get(slug="cid")).mentor_id == library["bob"]
    with pytest.raises(Author.DoesNotExist, match="Author matching query does not exist"):
        await Author.objects.get(slug="nobody")
    with pytest.raises(Author.MultipleObjectsReturned, match="returned more than one Author -- it returned 2"):
        await Author.objects.get(rating__gte=4)
    assert (await Author.objects.first()).name == "Ann"  # ordered by the primary key
    assert (await Author.objects.last()).name == "dora"
    assert (await Author.objects.order_by("-name").first()).name == "dora"
    assert (await Author.objects.order_by(Lower("name")).last()).name == "dora"
    assert await Author.objects.filter(slug="nobody").first() is None
    assert await Author.objects.filter(slug="nobody").last() is None
    assert await Author.objects.count() == 4
    assert await Author.objects.filter(mentor__isnull=False).count() == 2
    assert await Author.objects.filter(name="Ann").exists() is True
    assert await Author.objects.filter(name="Zed").exists() is False


async def test_get_reports_more_than_twenty_rows_as_django_does(library, pool):  # noqa: ARG001
    await in_thread(lambda: dj.Reader.objects.bulk_create(dj.Reader(name=f"r{n}") for n in range(23)))
    with pytest.raises(
        Reader.MultipleObjectsReturned, match="returned more than one Reader -- it returned more than 20!"
    ):
        await Reader.objects.get()
    with pytest.raises(Reader.MultipleObjectsReturned, match="it returned 2!"):
        await Reader.objects.get(name__in=["r1", "r2"])


async def test_iteration_on_another_session_and_in_a_transaction(library, pool):  # noqa: ARG001
    session = adorm.create_session("default", "reader")
    assert sorted([a.name async for a in session.Author.filter(rating__gte=3)]) == ["Ann", "Bob", "dora"]
    async with adorm.atomic():
        assert await Author.objects.count() == 4
        assert [a.name async for a in Author.objects.filter(name="Ann")] == ["Ann"]


async def test_select_related_fills_the_relation_cache_from_one_query(library, pool, record_operations):  # noqa: ARG001
    kinds = record_operations()
    cid = await Author.objects.select_related("mentor__mentor").get(slug="cid")
    assert kinds == ["get"]
    bob = await cid.mentor
    ann = await bob.mentor
    assert (bob.name, ann.name, await ann.mentor) == ("Bob", "Ann", None)
    assert kinds == ["get"]
    rows = [a async for a in Author.objects.select_related("mentor").order_by("name")]
    assert [None if r._state.related["mentor"] is None else r._state.related["mentor"].name for r in rows] == [
        None,
        "Ann",
        "Bob",
        None,
    ]
    assert kinds == ["get", "select"]


async def test_select_related_without_paths_follows_non_null_foreign_keys(library, pool):  # noqa: ARG001
    note = await Note.objects.select_related().get()
    assert set(note._state.related) == {"content_type"}
    assert (await note.content_type).model == "book"
    tagging = await Tagging.objects.select_related().get(weight=3)
    assert set(tagging._state.related) == {"book", "tag"}
    assert (await tagging.book).title == "Dune"


async def test_select_related_with_only_restricts_the_related_row(library, pool):  # noqa: ARG001
    bob = await Author.objects.select_related("mentor").only("name", "mentor", "mentor__name").get(slug="bob")
    ann = await bob.mentor
    assert ann.name == "Ann"
    assert ann.get_deferred_fields() == {"slug", "born", "rating", "website", "account_id", "mentor_id"}
    deferred = await Author.objects.select_related("mentor").defer("mentor__born").get(slug="bob")
    assert (await deferred.mentor).get_deferred_fields() == {"born"}


async def test_select_related_reverse_one_to_one_and_renamed_relation(library, pool):  # noqa: ARG001
    books = {b.isbn: b async for b in Book.objects.select_related("edition")}
    assert (await books[DUNE_2].edition).number == 2
    with pytest.raises(Edition.DoesNotExist):
        await books[DUNE].edition
    note = await Note.objects.select_related("copy_").get()
    assert note._state.related["copy_"].title == "Dune"


@pytest.mark.parametrize("path", ["name", "books", "pupils", "nothing"])
async def test_select_related_refuses_what_is_not_a_single_valued_relation(library, pool, path):  # noqa: ARG001
    with pytest.raises(ProgrammingError, match="select_related"):
        await Author.objects.select_related(path).first()
    assert pool.in_use == 0


async def test_foreign_keys_load_on_await(library, pool, record_operations):  # noqa: ARG001
    cid = await Author.objects.get(slug="cid")
    kinds = record_operations()
    bob = await cid.mentor
    assert (type(bob), bob.name) == (Author, "Bob")
    assert await cid.mentor is bob  # cached
    assert kinds == ["get"]
    edition = await Edition.objects.get(isbn=DUNE_2)
    assert (await edition.book_ptr).title == "Dune II"


async def test_reverse_relations_are_queries(library, pool):  # noqa: ARG001
    ann = await Author.objects.get(slug="ann")
    assert await names(ann.pupils) == ["Bob"]
    assert await ann.pupils.count() == 1
    assert [b.title async for b in ann.books.order_by("title")] == ["Dune", "Physics"]
    physics = await Book.objects.get(isbn=PHYSICS)
    assert sorted(await names(physics.authors)) == ["Ann", "Cid"]
    reader = await Reader.objects.get()
    assert [loan.book_id async for loan in reader.loans] == [DUNE]  # the manager hides archived loans
    assert await Loan.all_loans.filter(reader_id=reader.id).count() == 2
    assert await Loan.objects.count() == 1


async def test_content_type_ids(library, pool):  # noqa: ARG001
    expected = await in_thread(lambda: DjangoContentType.objects.get_for_model(dj.Book).id)
    found = await ContentType.objects.values_list("id", flat=True).get(app_label="library", model="book")
    assert found == expected
    note = await Note.objects.get(content_type__app_label="library", content_type__model="book")
    assert note.content_type_id == expected
    assert (await note.content_type).id == expected


async def test_multi_table_inheritance_and_proxy_models(library, pool):  # noqa: ARG001
    edition = await Edition.objects.get(title="Dune II")
    assert (edition.isbn, edition.book_ptr_id, edition.number, edition.pages) == (DUNE_2, DUNE_2, 2, 500)
    assert await Edition.objects.filter(released__gte=datetime.date(2020, 1, 1), title__startswith="Dune").count() == 1
    assert sorted([b.title async for b in BookProxy.objects.filter(genre="fiction")]) == ["Dune", "Dune II"]
    assert type(await BookProxy.objects.get(isbn=POEMS)) is BookProxy


# Errors


async def reusable(pool):
    """Whether the pool's single connection survived: the next read runs on it."""
    size = pool.size
    return await Author.objects.filter(name="Ann").count() == 1 and pool.size == size == 1 and pool.in_use == 0


async def test_django_errors_become_adorm_errors_and_keep_the_connection(library, executor):  # noqa: ARG001
    pool = executor(max_connections=1)
    with pytest.raises(ProgrammingError, match="Cannot resolve keyword 'nope'") as raised:
        await Author.objects.filter(nope=1).count()
    assert type(raised.value.original).__name__ == "FieldError"
    assert await reusable(pool)
    with pytest.raises(ValidationError) as invalid:
        await Note.objects.filter(code="not-a-uuid").exists()
    assert "is not a valid UUID" in invalid.value.messages[0]
    assert await reusable(pool)
    with pytest.raises(DataError) as regex:
        await Author.objects.filter(name__regex="(").count()
    assert regex.value.sqlstate == "2201B"
    assert await reusable(pool)
    with pytest.raises(DatabaseError) as unknown:
        await Author.objects.filter(rating="many").count()
    assert type(unknown.value) is DatabaseError
    assert isinstance(unknown.value.original, ValueError)
    assert await reusable(pool)


async def test_unknown_models_managers_and_paths(library, pool):  # noqa: ARG001
    session = adorm.create_session()
    with pytest.raises(ProgrammingError, match="no Django model is installed as 'library.Nothing'"):
        await session.execute(Select(Query("library.Nothing")))
    with pytest.raises(ProgrammingError, match="has no Django manager 'nothing'"):
        await session.execute(Select(Query("library.Author", manager="nothing")))
    with pytest.raises(ProgrammingError):
        await Author.objects.order_by("mentor__nothing").first()
    assert pool.in_use == 0


async def test_unsupported_operations_and_constructs_are_refused_before_execution(library, pool, record_operations):  # noqa: ARG001
    kinds = record_operations()
    with pytest.raises(NotSupportedError, match="aggregate.Count"):
        await Author.objects.annotate(n=Count("books")).first()
    with pytest.raises(NotSupportedError, match="'update' operation"):
        await adorm.create_session().execute(Update(Query("library.Author"), {"rating": 1}))
    with pytest.raises(NotSupportedError, match="clause.select_for_update"):
        async with adorm.atomic():
            await Author.objects.select_for_update().first()
    mentor = Author.objects.filter(pk=adorm.OuterRef("mentor")).values("rating")
    with pytest.raises(NotSupportedError, match="expression.OuterRef"):
        await Author.objects.filter(rating=Subquery(mentor)).first()
    assert kinds == []
    assert await Author.objects.count() == 4


# Context and concurrency


class LanguageManager(django_models.Manager):
    """The readers named like the active language."""

    def get_queryset(self):
        return super().get_queryset().filter(name=translation.get_language())


@pytest.fixture
def by_language():
    """``library.Reader`` gets the Django manager ``by_language`` for the test."""
    manager = LanguageManager()
    manager.contribute_to_class(dj.Reader, "by_language")
    try:
        yield
    finally:
        dj.Reader._meta.local_managers.remove(manager)
        delattr(dj.Reader, "by_language")
        dj.Reader._meta._expire_cache()


async def readers_in(language):
    query = Query("library.Reader", manager="by_language")
    with translation.override(language):
        rows = await adorm.create_session().execute(Select(query))
    return [row["name"] for row in rows]


async def test_the_driver_sees_the_callers_active_language(executor, by_language):  # noqa: ARG001
    await in_thread(lambda: [dj.Reader.objects.create(name=name) for name in ("sl", "de", "en")])
    executor(max_connections=2)
    assert await readers_in("sl") == ["sl"]
    for _ in range(20):
        languages = ["sl", "de", "en"] * 4
        results = await asyncio.gather(*(readers_in(language) for language in languages))
        assert results == [[language] for language in languages]
    with translation.override("de"):
        async with asyncio.TaskGroup() as group:
            inherited = group.create_task(
                adorm.create_session().execute(Select(Query("library.Reader", manager="by_language")))
            )
    assert [row["name"] for row in inherited.result()] == ["de"]


class ExistingAuthorManager(django_models.Manager):
    """The readers named like an author, whose names it reads from the database."""

    def get_queryset(self):
        return super().get_queryset().filter(name__in=list(dj.Author.objects.values_list("name", flat=True)))


@pytest.fixture
def named_like_authors():
    """``library.Reader`` gets the Django manager ``named_like_authors`` for the test."""
    manager = ExistingAuthorManager()
    manager.contribute_to_class(dj.Reader, "named_like_authors")
    try:
        yield
    finally:
        dj.Reader._meta.local_managers.remove(manager)
        delattr(dj.Reader, "named_like_authors")
        dj.Reader._meta._expire_cache()


@pytest.mark.usefixtures("library", "named_like_authors")
async def test_a_manager_that_reads_the_database_while_building_a_query(executor, driver_class):
    from adorm.drivers.asyncpg import AsyncpgDriver

    await in_thread(lambda: [dj.Reader.objects.create(name=name) for name in ("Ann", "Nobody")])
    pool = executor(max_connections=1)
    query = Query("library.Reader", manager="named_like_authors")
    assert await Author.objects.count() == 4
    for _ in range(3):
        if driver_class is AsyncpgDriver:
            # Django refuses its own database access on the event loop; the connection is not at fault
            with pytest.raises(NotSupportedError, match="get_queryset\\(\\) must only build a query"):
                await adorm.create_session().execute(Select(query))
        else:
            assert [row["name"] for row in await adorm.create_session().execute(Select(query))] == ["Ann"]
        assert (pool.size, pool.in_use) == (1, 0)
    assert await Author.objects.count() == 4
    assert pool.size == 1


async def test_concurrent_reads_on_a_small_pool(library, executor):  # noqa: ARG001
    pool = executor(max_connections=3)
    expected = {n: sum(1 for rating in (5, 3, 1, 4) if rating >= n) for n in range(7)}
    for _ in range(5):
        counts = await asyncio.gather(*(Author.objects.filter(rating__gte=n % 7).count() for n in range(40)))
        assert counts == [expected[n % 7] for n in range(40)]
    assert pool.size <= 3
    assert pool.in_use == 0


async def test_concurrent_tasks_share_the_transactions_slot(library, executor):  # noqa: ARG001
    pool = executor(max_connections=1)
    for _ in range(5):
        async with adorm.atomic():
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(Author.objects.filter(rating__gte=n % 6).count()) for n in range(20)]
                first = group.create_task(Author.objects.order_by("name").first())
        assert [t.result() for t in tasks] == [[4, 4, 3, 3, 2, 1][n % 6] for n in range(20)]
        assert first.result().name == "Ann"
    assert (pool.size, pool.in_use) == (1, 0)


# Cancellation and lost connections


async def test_cancelling_a_waiting_read_cancels_its_statement(library, executor, locker):  # noqa: ARG001
    lock, unlock, waiting, _ = locker
    pool = executor(max_connections=1)
    assert await Author.objects.count() == 4
    for _ in range(3):
        await lock()
        task = asyncio.create_task(Author.objects.count())
        await waiting()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await waiting(present=False)  # the statement no longer waits, though the lock is still held
        assert (pool.size, pool.in_use) == (1, 0)
        await unlock()
        assert await Author.objects.count() == 4
        assert pool.size == 1


async def test_cancellation_racing_completion_leaves_the_slot_usable(library, executor, locker):  # noqa: ARG001
    lock, unlock, waiting, _ = locker
    pool = executor(max_connections=1)
    outcomes = set()
    for attempt in range(20):
        await lock()
        task = asyncio.create_task(Author.objects.count())
        await waiting()
        await unlock()
        for _ in range(attempt % 4):
            await asyncio.sleep(0)
        task.cancel()
        try:
            outcomes.add(await task)
        except asyncio.CancelledError:
            outcomes.add("cancelled")
        assert pool.in_use == 0
        assert await Author.objects.filter(rating__gte=4).count() == 2
    assert outcomes <= {4, "cancelled"}
    assert pool.size == 1


async def test_cancelling_a_read_inside_a_transaction_rolls_it_back(library, executor, locker):  # noqa: ARG001
    lock, unlock, waiting, monitor = locker
    pool = executor(max_connections=1)

    async def read_in_transaction():
        async with adorm.atomic():
            return await Author.objects.count()

    for _ in range(5):
        await lock()
        task = asyncio.create_task(read_in_transaction())
        await waiting()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await unlock()
        assert pool.in_use == 0
        async with asyncio.timeout(5):
            assert await Author.objects.count() == 4
        idle = await monitor.fetchval(
            "SELECT count(*) FROM pg_stat_activity"
            " WHERE datname = current_database() AND state LIKE 'idle in transaction%' AND pid <> pg_backend_pid()"
        )
        assert idle == 0
    assert pool.size <= 1


async def test_a_connection_lost_mid_read_is_dropped(library, executor, locker):  # noqa: ARG001
    lock, unlock, waiting, monitor = locker
    pool = executor(max_connections=1)
    await lock()
    task = asyncio.create_task(Author.objects.count())
    pid = await waiting()
    await monitor.execute("SELECT pg_terminate_backend($1)", pid)
    with pytest.raises(OperationalError) as lost:
        await task
    # psycopg reports the server's 57P01; asyncpg may see the socket close first (08003)
    assert lost.value.sqlstate in ("57P01", "08003")
    assert is_connection_error(lost.value)
    assert (pool.size, pool.in_use) == (0, 0)
    await unlock()
    assert await Author.objects.count() == 4
    assert pool.size == 1


async def test_a_lost_connection_inside_a_transaction_ends_it(library, executor, locker):  # noqa: ARG001
    lock, unlock, waiting, monitor = locker
    pool = executor(max_connections=1)
    await lock()

    async def read_in_transaction():
        async with adorm.atomic():
            return await Author.objects.count()

    task = asyncio.create_task(read_in_transaction())
    await monitor.execute("SELECT pg_terminate_backend($1)", await waiting())
    with pytest.raises(OperationalError):
        await task
    await unlock()
    assert (pool.size, pool.in_use) == (0, 0)
    assert await Author.objects.count() == 4
