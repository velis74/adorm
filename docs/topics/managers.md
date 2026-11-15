# Managers and driver methods

A Django model carries logic in its managers and query sets: a `get_queryset()` that hides soft-deleted rows, a
`create()` that drops an argument, a `delete()` that refuses bulk deletion, a `create_user()` that hashes a
password. adorm brings that logic into its API with the mechanism that brings the model definitions: the managers
and their methods are declared on the adorm model, and `adorm generate` writes the declarations from the Django
code.

## Where a method runs

| Method | Runs | Declared by |
|---|---|---|
| builds a query (a filter, an ordering, a reusable condition) | in the client, on every driver | a method of an adorm `QuerySet` or `Manager` subclass, written by hand; for a generated model in the class named by [`@client_base`](#client-side-methods-of-generated-models) |
| overrides what adorm runs as an operation: `get_queryset()`, `create()`, `delete()`, `update()`, `get_or_create()` … | on the driver, through the ORM manager | nothing: the manager gets its ORM name, and every operation it builds names that manager |
| does other work on the database (`create_user()`, `archive()`) | on the driver, on the manager's `method_manager`, as a [`Call`](/ref/operations#adorm-operations-call-query-method-target-queryset-args-kwargs-manager-none) | [`@expose`](/ref/codegen#adorm-expose-func) or the `ADORM_EXPOSE` setting in the Django code; [`@driver_method`](/ref/models#adorm-driver-method-func-queryset-only-false) on the adorm class |

A query-building method stays in the client because building a query needs no connection: the result is a query
set that further methods extend. A method of the driver side returns data, not a query set, so it is the last step
of a chain.

Driver-side logic needs a driver that runs the target ORM's code: the Django driver does; a native driver, which
builds SQL itself, cannot. A query with an ORM manager uses the construct `clause.manager` and a driver method is a
`call` operation, so a driver without them refuses both with `NotSupportedError` before anything runs. A manager
that only has driver methods keeps its queries off the ORM manager, so on a native driver only its driver methods
are refused.

## Managers

Every model has `objects`. A manager declared in the class body is another way into the model's rows:

```python
from adorm import Manager, Model, ModelMeta


class Loan(Model):
    _meta = ModelMeta("library.Loan")
    id: int | None = None
    due: datetime.date
    archived: bool = False

    objects = Manager("objects")      # Django's objects: its get_queryset() and overrides apply
    all_loans = Manager()             # the plain rows, on every driver
```

`Manager(orm_name)` names the target ORM's manager. Every query, `create()`, `update()`, `delete()` and
`get_or_create()` of `Loan.objects` carries that name ([`Query.manager`](/ref/query#adorm-query-query-model-manager-none)),
so the driver starts from that manager: Django's `Loan.objects.get_queryset()` decides the rows and Django's
query set methods run the writes. `Manager()` without a name reads and writes the rows without manager logic.

`method_manager` names the target ORM's manager the [driver methods](#driver-methods) run on; it defaults to
`orm_name`. A manager whose target only adds methods, such as Django's `UserManager` with `create_user()`, declares
`method_manager` alone: its queries carry no ORM manager and run on every driver.

```python
class User(Model):
    ...
    objects = UserManager(method_manager="objects")   # create_user() on Django's User.objects
```

A manager is accessed like `objects`, on the model class (bound to the current session) or on a session:

```python
await Loan.objects.count()                 # rows Django's manager returns
await Loan.all_loans.count()               # every row
await session.Loan.all_loans.count()       # on another session
session.bind(Loan, "all_loans")            # the same
```

## Query set classes

A manager's `queryset_class` is the class of the query sets it returns. Its public methods are also methods of the
manager, as with Django's `as_manager()`:

```python
from adorm import Manager, QuerySet


class LoanQuerySet(QuerySet):
    def overdue(self, today):
        return self.filter(due__lt=today)


class LoanManager(Manager):
    queryset_class = LoanQuerySet


class Loan(Model):
    ...
    objects = LoanManager("objects")
    late = LoanQuerySet.as_manager("objects")   # Manager.from_queryset(LoanQuerySet)("objects")


await Loan.objects.overdue(today).filter(reader_id=7).count()
```

Building methods return the same class, so client-side methods chain in any order. A manager that overrides
`get_queryset()` adds its conditions in the client, on every driver:

```python
class OpenLoans(Manager):
    def get_queryset(self):
        return super().get_queryset().filter(archived=False)
```

## Related rows

A reverse foreign key and a many-to-many relation return their rows from the target model's `objects` manager, as
Django's related managers derive from the default manager: the query set has the manager's `queryset_class`, starts
from its `get_queryset()` and carries its ORM manager. A forward foreign key and a reverse one-to-one load their row
without the target's manager, as Django's base manager does.

```python
await reader.loans.count()                              # Loan.objects: archived loans are left out
await reader.loans.overdue(today).archive("late")       # the query set methods of Loan.objects
await loan.reader                                        # the reader, whatever Reader.objects filters
```

## Driver methods

A driver method is declared with [`@driver_method`](/ref/models#adorm-driver-method-func-queryset-only-false) on an
`async def` whose signature is the target method's; the body does not run:

```python
from adorm import driver_method


class LoanQuerySet(QuerySet):
    @driver_method
    async def archive(self, reason: str = "") -> int:
        """Archive the loans with ``reason`` as their note; returns the number of loans."""


class LoanManager(Manager):
    queryset_class = LoanQuerySet

    @driver_method
    async def lend(self, reader_id: int, book_id: str, *, days: int = 14) -> "Loan":
        """Lend a book for ``days`` days."""


loan = await Loan.objects.lend(reader.pk, book.pk, days=7)          # runs LoanManager.lend in Django
count = await Loan.objects.overdue(today).archive("late")           # runs LoanQuerySet.archive on that query
```

A call checks its arguments against the signature and sends one `Call` operation to the manager's `method_manager`:
a manager method with the manager's plain query, a query set method with the query set's query. The annotations type
the arguments' encoding, as field types type the values of other operations. The driver returns plain data; every
instance of a model in it, also inside lists, tuples and the values of mappings, comes back as an instance of the
registered adorm model.

The driver runs only the methods the registered adorm model declares: a `Call` naming any other method, a private
one or one of another manager raises `NotSupportedError` before it reaches the driver. Arguments are plain values:
a model instance raises `TypeError`, so pass its primary key.

A driver method is one operation. Outside a transaction it runs in the driver's autocommit mode; a method that
writes several rows and must not stop halfway runs inside `adorm.atomic()` or opens its own transaction in the
target ORM. Signals and `transaction.on_commit()` callbacks of the target ORM run on the driver side.

## Generated managers

`adorm generate` writes the managers of every Django model (see
[generating models](/howto/generating-models#managers)). For these Django models:

```python
from adorm import client_base, expose


class ReaderQuerySet(models.QuerySet):
    def create(self, **kwargs):
        kwargs.pop("username", None)
        return super().create(**kwargs)


class Reader(models.Model):
    name = models.CharField(max_length=100)
    objects = ReaderQuerySet.as_manager()


class LoanPolicyQuerySet(models.QuerySet):
    def delete(self):
        raise NotSupportedError("loan policies are deleted one at a time")


class LoanPolicy(models.Model):
    name = models.CharField(max_length=40, unique=True)
    objects = LoanPolicyQuerySet.as_manager()


@client_base("library.adorm_bases:LoanQuerySetBase")
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
    ...
    objects = LoanManager()
    all_loans = models.Manager()
```

and the setting `ADORM_EXPOSE = ["auth.User.objects.create_user"]`, the generated modules contain:

```python
class LoanQuerySet(LoanQuerySetBase):
    @driver_method
    async def archive(self, reason: str = "") -> int:
        """Archive the loans with ``reason`` as their note; returns the number of loans."""


class LoanManager(Manager):
    queryset_class = LoanQuerySet

    @driver_method
    async def lend(self, reader_id: int, book_id: str, *, days: int = 14) -> "Loan":
        """Lend a book for ``days`` days."""


class Reader(Model):
    ...
    objects = Manager("objects")


class LoanPolicy(Model):
    ...
    objects = Manager("objects")


class Loan(Model):
    ...
    objects = LoanManager("objects")
    all_loans = Manager()
```

```python
class UserManager(Manager):
    @driver_method
    async def create_user(self, username, email=None, password=None, **extra_fields): ...


class User(Model):
    ...
    objects = UserManager(method_manager="objects")
```

With them:

```python
reader = await Reader.objects.create(name="Ann", username="ann")   # Django's create() drops username
await LoanPolicy.objects.filter(max_days__gt=30).delete()          # NotSupportedError from the query set
await Loan.objects.count()                                         # loans that are not archived
await Loan.all_loans.count()                                       # every loan
user = await User.objects.create_user("ann", password="secret")   # Django hashes the password
```

`create()` on a manager with an ORM name sends the values that are not fields too, so an override that takes such
arguments, like `username` above, receives them. `UserManager` overrides no operation, so `User.objects.filter()`
and the other queries of `User` run on every driver; only `create_user()` needs the Django driver.

A manager gets its ORM name only for an override of a method the driver calls on the Django manager
([`ROUTED_METHODS`](/ref/codegen#adorm-codegen-django-routed-methods)) that its client base does not define too;
see [generating models](/howto/generating-models#managers).

### Client-side methods of generated models

A generated module is rewritten on every run, so methods ported by hand live in a module of their own. A Django
manager or query set class decorated with `@client_base("package.module:ClassName")` gets a generated class that
derives from that adorm class:

```python
# library/adorm_bases.py
from adorm import QuerySet


class LoanQuerySetBase(QuerySet):
    def overdue(self, today):
        return self.filter(due__lt=today)
```

```python
await Loan.objects.overdue(today).archive("late")   # overdue() in the client, archive() on the driver
```

A method of the client base that the Django class overrides, such as `get_queryset()`, replaces the Django one: the
generator does not route the manager through the ORM manager for it. A public Django method that is neither
exposed nor ported is not generated and is reported as a warning.
