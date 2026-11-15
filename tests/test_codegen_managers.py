"""Managers in `adorm generate`: specs, rendering, Django introspection and the generated classes."""

import datetime
import subprocess
import sys
import textwrap
import warnings

from pathlib import Path

import pytest

from django.db import models
from django.test import override_settings

from adorm import client_base, expose, Manager, QuerySet
from adorm.codegen import (
    AppSpec,
    CodegenError,
    CodegenWarning,
    compute_schema_hash,
    FieldSpec,
    ManagerSpec,
    MethodSpec,
    ModelSpec,
    ParameterSpec,
    plan,
    QuerySetSpec,
    render_module,
    TypeSpec,
)
from adorm.codegen.django import exposed_methods, manager_specs, model_spec
from adorm.models import DriverMethod, get_managers, model_schema
from adorm.operations import Call, ModelRow
from adorm.porting import CLIENT_BASE_ATTRIBUTE, get_client_base, is_exposed
from adorm.query import Query
from tests.djangoproject.library import models as django_library
from tests.generated import auth, library

INT = TypeSpec("int")
ID = FieldSpec("id", "id", INT, db_filled=True, primary_key=True)


def model(*managers: ManagerSpec, label: str = "shop.Order", class_name: str = "Order") -> ModelSpec:
    return ModelSpec(label, class_name, "id", (ID,), (), "0" * 16, managers=managers)


def method(name: str = "close", target: str = "queryset", *parameters: ParameterSpec, **kwargs) -> MethodSpec:
    return MethodSpec(name, target, tuple(parameters), **kwargs)  # type: ignore[arg-type]


def body(source: str, start: str) -> str:
    """The lines of ``source`` from the line starting with ``start`` to the next blank line pair."""
    text = source[source.index(start) :]
    return text.split("\n\n\n")[0].rstrip("\n")


# The porting declarations


def test_expose_marks_a_function():
    def archive(self): ...

    assert expose(archive) is archive
    assert is_exposed(archive)
    assert not is_exposed(lambda: None)
    with pytest.raises(TypeError, match="marks a function"):
        expose(3)  # type: ignore[arg-type]


def test_client_base_marks_a_class_and_is_inherited():
    @client_base("shop.adorm:OrderQuerySetBase")
    class Orders:
        pass

    class MoreOrders(Orders):
        pass

    assert getattr(Orders, CLIENT_BASE_ATTRIBUTE) == "shop.adorm:OrderQuerySetBase"
    assert get_client_base(MoreOrders) == "shop.adorm:OrderQuerySetBase"
    assert get_client_base(int) is None
    with pytest.raises(ValueError, match="is not a 'package.module:ClassName' path"):
        client_base("shop.adorm.OrderQuerySetBase")
    with pytest.raises(TypeError, match="decorates a class"):
        client_base("shop:Base")(len)  # type: ignore[arg-type]


# Specs


def test_type_spec_model_and_none():
    assert str(TypeSpec("shop.Order", model=True)) == "model:shop.Order"
    assert str(TypeSpec("shop.Order", model=True).with_nullable()) == "model:shop.Order | None"
    assert str(TypeSpec("None")) == "None"
    with pytest.raises(ValueError, match="a model type is a model label"):
        TypeSpec("Order", model=True)
    with pytest.raises(ValueError, match="a model type is a model label"):
        TypeSpec("shop.Order", (INT,), model=True)


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: ParameterSpec("class"), "is not a parameter name"),
        (lambda: ParameterSpec("a", "keyword"), "unknown parameter kind"),
        (lambda: ParameterSpec("a", type=TypeSpec("shop.Order", model=True)), "cannot take a model instance"),
        (lambda: ParameterSpec("a", "var_keyword", default=1), "cannot have a default"),
        (lambda: method("_close"), "is not a public method name"),
        (lambda: method("close", "model"), "the target of close is"),
        (lambda: method("close", "manager", queryset_only=True), "only a query set method is queryset_only"),
        (lambda: method("close", "queryset", ParameterSpec("a", default=1), ParameterSpec("b")), "default"),
        (lambda: QuerySetSpec("1Orders"), "is not a class name"),
        (lambda: QuerySetSpec("Orders", base="shop.Base"), "is not a 'package.module:ClassName' path"),
        (lambda: QuerySetSpec("Orders", (method("x", "manager"),)), "is not a query set method"),
        (lambda: QuerySetSpec("Orders", (method("x"), method("x"))), "several methods named x"),
        (lambda: ManagerSpec("_objects"), "is not a manager name"),
        (lambda: ManagerSpec("save"), "would hide an attribute of adorm.Model"),
        (lambda: ManagerSpec("open", "_x"), "is not an ORM manager name"),
        (lambda: ManagerSpec("open", method_manager="_x"), "is not an ORM manager name"),
        (lambda: ManagerSpec("open", "open", methods=(method("x", "manager"),)), "needs a class name"),
        (lambda: ManagerSpec("open", "open", "Open", (method("x"),)), "is not a manager method"),
        (lambda: ManagerSpec("open", None, "Open", (method("x", "manager"),)), "needs an ORM name"),
        (lambda: ManagerSpec("open", None, queryset=QuerySetSpec("Q", (method("x"),))), "needs an ORM name"),
        (lambda: model(ManagerSpec("id")), "several fields or relations named id"),
    ],
)
def test_spec_validation(build, message):
    with pytest.raises((ValueError, TypeError), match=message):
        build()


def test_schema_hash_covers_managers_with_an_orm_name_or_methods():
    plain = compute_schema_hash("shop.Order", "id", [ID], [])
    assert compute_schema_hash("shop.Order", "id", [ID], [], managers=[ManagerSpec("everything")]) == plain
    bound = compute_schema_hash("shop.Order", "id", [ID], [], managers=[ManagerSpec("objects", "objects")])
    assert bound != plain
    close = method("close", "queryset", ParameterSpec("when", type=TypeSpec("datetime.date")))
    with_method = [ManagerSpec("objects", "objects", queryset=QuerySetSpec("Orders", (close,)))]
    assert compute_schema_hash("shop.Order", "id", [ID], [], managers=with_method) not in (plain, bound)
    documented = MethodSpec("close", "queryset", close.parameters, TypeSpec("int"), doc="Close.")
    same = [ManagerSpec("objects", "objects", queryset=QuerySetSpec("Orders", (documented,), "shop.a:B"))]
    assert compute_schema_hash("shop.Order", "id", [ID], [], managers=same) == compute_schema_hash(
        "shop.Order", "id", [ID], [], managers=with_method
    )


# Rendering


def test_render_managers_and_classes():
    close = method(
        "close",
        "queryset",
        ParameterSpec("when", type=TypeSpec("datetime.date")),
        ParameterSpec("reason", type=TypeSpec("str"), default=""),
        returns=TypeSpec("int"),
        doc="Close the orders.",
    )
    reopen = method("reopen", "queryset", ParameterSpec("days", "keyword_only", INT), queryset_only=True)
    place = method(
        "place",
        "manager",
        ParameterSpec("customer", "positional_only", INT),
        ParameterSpec("items", "var_positional", TypeSpec("str")),
        ParameterSpec("note", "keyword_only", default=None),
        ParameterSpec("extra", "var_keyword"),
        returns=TypeSpec("shop.Order", model=True),
    )
    orders = QuerySetSpec("OrderQuerySet", (close, reopen), "shop.adorm:OrderQuerySetBase")
    managers = (
        ManagerSpec("objects", "objects", "OrderManager", (place,), orders),
        ManagerSpec("open_orders", "open_orders", queryset=orders),
        ManagerSpec("everything"),
        ManagerSpec("hidden", "hidden"),
    )
    source = render_module(AppSpec("shop", (model(*managers),)), line_length=120)
    assert "from adorm import driver_method, Manager, Model, ModelMeta, QuerySet" not in source
    assert "from adorm import Manager, Model, ModelMeta, driver_method" in source
    assert "from shop.adorm import OrderQuerySetBase" in source
    assert body(source, "class OrderQuerySet") == textwrap.dedent('''\
        class OrderQuerySet(OrderQuerySetBase):
            @driver_method
            async def close(self, when: datetime.date, reason: str = "") -> int:
                """Close the orders."""

            @driver_method(queryset_only=True)
            async def reopen(self, *, days: int): ...''')
    assert body(source, "class OrderManager") == textwrap.dedent("""\
        class OrderManager(Manager):
            queryset_class = OrderQuerySet

            @driver_method
            async def place(self, customer: int, /, *items: str, note=None, **extra) -> "Order": ...""")
    assert body(source, "class Order(") == textwrap.dedent("""\
        class Order(Model):
            _meta = ModelMeta("shop.Order", schema_hash="0000000000000000")
            id: int | None = None
            objects = OrderManager("objects")
            open_orders = OrderQuerySet.as_manager("open_orders")
            everything = Manager()
            hidden = Manager("hidden")""")
    assert source.index("class OrderQuerySet") < source.index("class OrderManager") < source.index("class Order(")


def test_render_splits_long_signatures_and_quotes_model_types():
    long = method(
        "a_method_with_a_rather_long_name",
        "manager",
        *(ParameterSpec(f"parameter_{n}", type=TypeSpec("str"), default="value") for n in range(4)),
        returns=TypeSpec("list", (TypeSpec("shop.Order", model=True),), nullable=True),
    )
    other = method("other", "manager", returns=TypeSpec("billing.Invoice", model=True))
    spec = model(ManagerSpec("objects", "objects", "OrderManager", (long, other)))
    source = render_module(AppSpec("shop", (spec,)), line_length=88)
    assert body(source, "class OrderManager") == textwrap.dedent("""\
        class OrderManager(Manager):
            @driver_method
            async def a_method_with_a_rather_long_name(
                self,
                parameter_0: str = "value",
                parameter_1: str = "value",
                parameter_2: str = "value",
                parameter_3: str = "value",
            ) -> "list[Order] | None": ...

            @driver_method
            async def other(self) -> Any: ...""")


def test_render_shares_one_class_and_renames_clashes():
    orders = QuerySetSpec("Order", (method("close"),))
    other = QuerySetSpec("Order", (method("open"),))
    first = model(ManagerSpec("objects", "objects", queryset=orders), ManagerSpec("again", "again", queryset=orders))
    second = model(ManagerSpec("objects", "objects", queryset=other), label="shop.Item", class_name="Item")
    source = render_module(AppSpec("shop", (first, second)), line_length=120)
    assert source.count("class Order_(QuerySet):") == 1
    assert "class Order__(QuerySet):" in source
    assert 'objects = Order_.as_manager("objects")' in source
    assert 'again = Order_.as_manager("again")' in source
    assert 'objects = Order__.as_manager("objects")' in source


def test_render_method_manager_and_one_class_per_content():
    lend = method("lend", "manager")
    people = ManagerSpec("people", "people", "PeopleManager", (lend,))
    objects = ManagerSpec("objects", "people", "PeopleManager", (lend,))
    hosted = ManagerSpec("hosted", None, "HostedManager", (method("ping", "manager"),), method_manager="hosted")
    orders = QuerySetSpec("Orders", (method("close"),))
    listed = ManagerSpec("listed", queryset=orders, method_manager="listed")
    source = render_module(AppSpec("shop", (model(objects, people, hosted, listed),)), line_length=120)
    assert source.count("class PeopleManager") == 1
    assert 'objects = PeopleManager("people")' in source
    assert 'people = PeopleManager("people")' in source
    assert 'hosted = HostedManager(method_manager="hosted")' in source
    assert 'listed = Orders.as_manager(method_manager="listed")' in source


def test_rendered_module_runs_and_passes_ruff(tmp_path):
    close = method("close", "queryset", ParameterSpec("when", type=TypeSpec("datetime.date")), returns=INT)
    place = method("place", "manager", ParameterSpec("n", type=INT), returns=TypeSpec("shop.Order", model=True))
    spec = model(
        ManagerSpec("objects", "objects", "OrderManager", (place,), QuerySetSpec("OrderQuerySet", (close,))),
        label="rendertest.Order",
    )
    files = plan([AppSpec("rendertest", (spec,))], line_length=100)
    package = tmp_path / "rendered"
    package.mkdir()
    for name, content in files.items():
        (package / name).write_text(content, encoding="utf-8")
    for command in (["ruff", "check", "--select", "E,F,W,I", "--ignore", "I001"], ["ruff", "format", "--check"]):
        result = subprocess.run(  # noqa: S603 - fixed arguments
            [sys.executable, "-m", *command, "--line-length", "100", str(package)], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stdout + result.stderr
    namespace: dict = {}
    exec(compile(files["rendertest.py"], "rendertest.py", "exec"), namespace)  # noqa: S102 - generated test module
    order = namespace["Order"]
    assert isinstance(get_managers(order)["objects"], namespace["OrderManager"])
    assert set(model_schema(order).methods) == {("objects", "manager", "place"), ("objects", "queryset", "close")}


# Django introspection


def test_library_managers():
    assert manager_specs(django_library.Reader) == (ManagerSpec("objects", "objects"),)
    assert manager_specs(django_library.LoanPolicy) == (ManagerSpec("objects", "objects"),)
    objects, all_loans = manager_specs(django_library.Loan)
    assert all_loans == ManagerSpec("all_loans")
    assert (objects.name, objects.orm_name, objects.class_name, objects.base) == (
        "objects",
        "objects",
        "LoanManager",
        None,
    )
    assert objects.methods == (
        MethodSpec(
            "lend",
            "manager",
            (
                ParameterSpec("reader_id", type=INT),
                ParameterSpec("book_id", type=TypeSpec("str")),
                ParameterSpec("days", "keyword_only", INT, 14),
            ),
            TypeSpec("library.Loan", model=True),
            doc="Lend a book for ``days`` days.",
        ),
    )
    assert objects.queryset == QuerySetSpec(
        "LoanQuerySet",
        (
            MethodSpec(
                "archive",
                "queryset",
                (ParameterSpec("reason", type=TypeSpec("str"), default=""),),
                INT,
                doc="Archive the loans with ``reason`` as their note; returns the number of loans.",
            ),
        ),
        "tests.djangoproject.library.adorm_bases:LoanQuerySetBase",
    )
    assert manager_specs(django_library.Book) == ()


def test_adorm_expose_setting_exposes_third_party_methods():
    from django.contrib.auth.models import User

    assert exposed_methods() == {"auth.User.objects": {"create_user"}}
    (objects,) = manager_specs(User)
    assert (objects.orm_name, objects.method_manager, objects.class_name) == (None, "objects", "UserManager")
    assert [m.name for m in objects.methods] == ["create_user"]
    assert [(p.name, p.kind, p.default) for p in objects.methods[0].parameters] == [
        ("username", "positional_or_keyword", ParameterSpec("x").default),
        ("email", "positional_or_keyword", None),
        ("password", "positional_or_keyword", None),
        ("extra_fields", "var_keyword", ParameterSpec("x").default),
    ]
    with override_settings(ADORM_EXPOSE=[]):
        assert manager_specs(User) == ()


@pytest.mark.parametrize(
    ("setting", "message"),
    [
        ("auth.User.objects.create_user", "is a list of"),
        (["auth.User.create_user"], "is not 'app_label.Model.manager.method'"),
        (["auth.Robot.objects.create_user"], "names no installed model"),
        (["auth.User.people.create_user"], "has no manager 'people'"),
    ],
)
def test_adorm_expose_errors(setting, message):
    with override_settings(ADORM_EXPOSE=setting), pytest.raises(CodegenError, match=message):
        exposed_methods()


def test_adorm_expose_of_an_unknown_method():
    from django.contrib.auth.models import User

    with (
        override_settings(ADORM_EXPOSE=["auth.User.objects.fly"]),
        pytest.raises(CodegenError, match="no method 'fly'"),
    ):
        manager_specs(User)


class TemporaryModels:
    """Django models defined inside a test, in an app of their own that is removed afterwards."""

    def __init__(self) -> None:
        from django.apps import apps

        self.apps = apps
        self.before = dict(apps.all_models["library"])

    def close(self) -> None:
        self.apps.all_models["library"].clear()
        self.apps.all_models["library"].update(self.before)
        self.apps.clear_cache()


@pytest.fixture
def temporary():
    models_ = TemporaryModels()
    yield models_
    models_.close()


def test_methods_that_cannot_be_generated_are_reported(temporary):  # noqa: ARG001
    class OddQuerySet(models.QuerySet):
        @expose
        async def asynchronous(self): ...

        @expose
        def chained(self) -> models.QuerySet:
            return self

        @expose
        def odd_default(self, when=datetime.date.today): ...

        @expose
        def typed(self, reader: django_library.Reader, many: tuple[int, ...], either: int | str, n: None) -> object: ...

        @expose
        def returns(self) -> list[django_library.Reader | None]: ...

    class Odd(models.Model):
        objects = OddQuerySet.as_manager()

        class Meta:
            app_label = "library"

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", CodegenWarning)
        (objects,) = manager_specs(Odd)
    messages = sorted(str(w.message) for w in caught)
    assert any("asynchronous: it is a coroutine function" in m and m.endswith("(not generated)") for m in messages)
    assert any("chained: it returns a query set" in m for m in messages)
    assert any("odd_default: the default of when cannot be written as a literal" in m for m in messages)
    assert any("typed(reader): a library.Reader parameter is generated as typing.Any" in m for m in messages)
    assert any("typed(many): the annotation tuple[int, ...] is generated as typing.Any" in m for m in messages)
    assert any("typed(either): the annotation int | str is generated as typing.Any" in m for m in messages)
    assert any("typed(n): the annotation None is generated as typing.Any" in m for m in messages)
    typed, returns = objects.queryset.methods
    assert typed.name == "typed"
    assert [p.type for p in typed.parameters] == [TypeSpec("typing.Any")] * 4
    assert typed.returns == TypeSpec("typing.Any")
    assert returns.returns == TypeSpec("list", (TypeSpec("library.Reader", model=True, nullable=True),))
    assert (objects.orm_name, objects.method_manager) == (None, "objects")


def test_overrides_bind_the_orm_manager_and_bases_are_checked(temporary):  # noqa: ARG001
    class Counting(models.Manager):
        def count(self):
            return 0

    class People(models.Manager):
        pass

    @client_base("tests.djangoproject.library.adorm_bases:LoanQuerySetBase")
    class BadBase(models.Manager):
        pass

    @client_base("tests.djangoproject.library.adorm_bases:Missing")
    class MissingBase(models.QuerySet):
        pass

    class Counted(models.Model):
        people = People()
        counting = Counting()

        class Meta:
            app_label = "library"

    assert manager_specs(Counted) == (ManagerSpec("people"), ManagerSpec("counting", "counting"))

    class Based(models.Model):
        objects = BadBase()

        class Meta:
            app_label = "library"

    with pytest.raises(CodegenError, match="is not a subclass of adorm.Manager"):
        manager_specs(Based)

    class Missed(models.Model):
        objects = MissingBase.as_manager()

        class Meta:
            app_label = "library"

    with pytest.raises(CodegenError, match="the client base .*Missing does not import"):
        manager_specs(Missed)


def test_only_routed_overrides_bind_the_orm_manager(temporary):  # noqa: ARG001
    class Deconstructed(models.Manager):
        use_in_migrations = True

        def deconstruct(self):
            return super().deconstruct()

        def contribute_to_class(self, cls, name, **kwargs):
            super().contribute_to_class(cls, name, **kwargs)

    @client_base("tests.djangoproject.library.adorm_bases:VisibleManagerBase")
    class PortedVisible(models.Manager):
        def get_queryset(self):
            return super().get_queryset().filter(id__gt=0)

    class Routed(models.Model):
        objects = Deconstructed()
        visible = PortedVisible()

        class Meta:
            app_label = "library"

    base = "tests.djangoproject.library.adorm_bases:VisibleManagerBase"
    assert manager_specs(Routed) == (ManagerSpec("visible", class_name="PortedVisible", base=base),)


def test_methods_that_are_neither_exposed_nor_ported_are_reported(temporary):  # noqa: ARG001
    @client_base("tests.djangoproject.library.adorm_bases:LoanQuerySetBase")
    class PortedQuerySet(models.QuerySet):
        def overdue(self, today):
            return self.filter(due__lt=today)

        def forgotten(self):
            return self

    class Forgetting(models.Manager.from_queryset(PortedQuerySet)):
        def also_forgotten(self):
            return 1

    class Forgetful(models.Model):
        objects = Forgetting()

        class Meta:
            app_label = "library"

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", CodegenWarning)
        manager_specs(Forgetful)
    assert sorted(str(w.message) for w in caught) == [
        "library.Forgetful.objects.PortedQuerySet.forgotten: not generated; expose it or port it to a client base",
        "library.Forgetful.objects.also_forgotten: not generated; expose it or port it to a client base",
    ]


def test_objects_stands_for_a_bound_default_manager(temporary):  # noqa: ARG001
    class Visible(models.Manager):
        def get_queryset(self):
            return super().get_queryset().filter(id__gt=0)

    class Hidden(models.Model):
        visible = Visible()
        everything = models.Manager()

        class Meta:
            app_label = "library"

    assert manager_specs(Hidden) == (
        ManagerSpec("objects", "visible"),
        ManagerSpec("visible", "visible"),
        ManagerSpec("everything"),
    )

    class Shadowing(models.Model):
        save_all = models.Manager()
        model_fields = models.Manager()

        class Meta:
            app_label = "library"

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", CodegenWarning)
        assert manager_specs(Shadowing) == (ManagerSpec("save_all"),)
    assert [str(w.message) for w in caught] == [
        "library.Shadowing.model_fields: the manager would hide an attribute of adorm.Model (not generated)"
    ]


@pytest.mark.filterwarnings("ignore::adorm.codegen.CodegenWarning")
def test_model_spec_of_a_child_takes_its_own_managers():
    spec = model_spec(django_library.Edition, included={"library.Book", "library.Edition"})
    assert spec.managers == ()
    assert model_spec(django_library.Loan).managers == manager_specs(django_library.Loan)


# The generated classes


def test_generated_loan_managers():
    assert type(get_managers(library.Loan)["objects"]) is library.LoanManager
    assert library.LoanManager.queryset_class is library.LoanQuerySet
    assert issubclass(library.LoanQuerySet, QuerySet)
    assert isinstance(library.LoanManager.__dict__["lend"], DriverMethod)
    assert get_managers(library.Loan)["all_loans"].orm_name is None
    assert get_managers(library.Reader)["objects"].orm_name == "objects"
    assert type(get_managers(library.Reader)["objects"]) is Manager
    assert type(get_managers(auth.User)["objects"]) is auth.UserManager
    assert set(model_schema(library.Loan).methods) == {
        ("objects", "manager", "lend"),
        ("objects", "queryset", "archive"),
    }


def test_generated_methods_build_operations():
    day = datetime.date(2026, 3, 1)
    overdue = library.Loan.objects.overdue(day)
    assert type(overdue) is library.LoanQuerySet
    assert overdue.query == Query("library.Loan", manager="objects").filter(due__lt=day)
    assert library.Loan.all_loans.all().query == Query("library.Loan")


async def test_generated_methods_send_calls():
    from adorm import create_session
    from adorm.executor import bind_transaction

    class Recorder:
        def __init__(self, *results):
            self.operations, self.results = [], list(results)

        async def execute(self, operation):
            self.operations.append(operation)
            return self.results.pop(0)

    executor = Recorder(ModelRow("library.Loan", {"id": 1, "reader_id": 2, "book_id": "b", "due": "x"}), 4, None)
    session = create_session("default", "generated-calls")
    with bind_transaction(session, executor):
        loan = await session.Loan.lend(2, "b", days=7)
        assert await session.Loan.overdue(datetime.date(2026, 3, 1)).archive("late") == 4
        await session.User.create_user("ann", password="secret")  # noqa: S106 - a test value
    assert isinstance(loan, library.Loan)
    assert executor.operations[0] == Call(
        Query("library.Loan", manager="objects"), "lend", "manager", (2, "b"), {"days": 7}
    )
    assert executor.operations[1].method == "archive"
    assert executor.operations[2] == Call(
        Query("auth.User"), "create_user", "manager", ("ann",), {"password": "secret"}, "objects"
    )


def test_generated_hashes_follow_the_managers():
    from django.apps import apps

    from adorm.codegen.django import schema_hash

    assert library.Loan._meta.schema_hash == schema_hash(apps.get_model("library.Loan"))
    with override_settings(ADORM_EXPOSE=[]):
        assert auth.User._meta.schema_hash != schema_hash(apps.get_model("auth.User"))


def test_generated_package_is_up_to_date_with_the_exposed_methods():
    source = (Path(__file__).parent / "generated" / "auth.py").read_text(encoding="utf-8")
    assert "async def create_user(self, username, email=None, password=None, **extra_fields): ..." in source
