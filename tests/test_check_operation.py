"""The ``check_operation`` profile hook's configuration (a plain function, imported from its dotted path in the
``ADORM`` setting), the hook in Django's ``TestCase``, and the models a delete reaches as `adorm generate` computes
them (`ModelMeta.delete_models`)."""

from __future__ import annotations

import contextvars

import pytest

from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.test import override_settings, TestCase
from django.test.utils import isolate_apps

import adorm

from adorm import ConnectionProfile, ModelMeta
from adorm.codegen import CodegenWarning
from adorm.codegen.django import delete_models_of, model_spec
from adorm.contrib.django import configure_from_settings, DEFAULT_DRIVER, settings_configuration
from adorm.drivers.django import DjangoDriver
from adorm.executor import Pool, Router, set_runner
from tests import memory_driver as memory
from tests.clubmodels.clubs import Club
from tests.djangoproject.library import models as dj
from tests.generated.library import Tag


def check_operation(operation, session):  # noqa: ARG001 - the hook's signature
    return None


async def async_check_operation(operation, session):  # noqa: ARG001 - the hook's signature
    return None


NOT_CALLABLE = 42


# Configuration


def test_a_profile_takes_a_plain_function_or_none():
    driver = memory.MemoryDriver(None)
    assert ConnectionProfile("default", driver).check_operation is None
    profile = ConnectionProfile("default", driver, check_operation=check_operation)
    assert profile.check_operation is check_operation
    assert profile.replace(check_operation=None).check_operation is None
    assert profile.same_as(ConnectionProfile("default", driver, check_operation=check_operation))
    assert not profile.same_as(ConnectionProfile("default", driver))


@pytest.mark.parametrize("hook", [async_check_operation, NOT_CALLABLE, "tests.test_check_operation.check_operation"])
def test_a_profile_refuses_a_hook_that_is_not_a_plain_function(hook):
    with pytest.raises(TypeError, match="check_operation of profile 'default' is a plain function or None"):
        ConnectionProfile("default", memory.MemoryDriver(None), check_operation=hook)


@override_settings(
    ADORM={
        "profiles": {
            "default": {"driver": DEFAULT_DRIVER, "check_operation": "tests.test_check_operation.check_operation"}
        }
    }
)
def test_the_setting_imports_the_hook_from_its_path():
    assert settings_configuration()["profiles"]["default"].check_operation is check_operation


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("tests.no_such_module.check_operation", r"\['check_operation'\]: No module"),
        ("tests.test_check_operation.missing", r"\['check_operation'\]: module"),
        ("tests.test_check_operation.NOT_CALLABLE", "a hook is a callable"),
        ("tests.test_check_operation.async_check_operation", "check_operation of profile 'default' is a plain"),
    ],
)
def test_a_bad_hook_path_in_the_setting_is_improperly_configured(path, message):
    value = {"profiles": {"default": {"driver": DEFAULT_DRIVER, "check_operation": path}}}
    with override_settings(ADORM=value), pytest.raises(ImproperlyConfigured, match=message):
        configure_from_settings()


# Django's TestCase


class RefusedError(Exception):
    pass


class CheckOperationInTestCaseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        dj.Tag.objects.create(name="seed")

    async def test_a_refusal_leaves_the_test_transaction_usable(self):
        refuse = contextvars.ContextVar("refuse", default=False)
        kinds = []

        def hook(operation, session):  # noqa: ARG001 - the hook's signature
            kinds.append(operation.kind)
            if refuse.get():
                raise RefusedError(operation.kind)

        driver = DjangoDriver()
        pool = Pool(driver, ConnectionProfile("default", driver, check_operation=hook), max_connections=2)
        router = Router([pool])
        previous = set_runner(router)
        try:
            for name in ("after", "after-again"):
                token = refuse.set(True)
                try:
                    with pytest.raises(RefusedError):
                        await Tag.objects.create(name=f"refused-{name}")
                finally:
                    refuse.reset(token)
                await Tag.objects.create(name=name)
            async with adorm.atomic():
                token = refuse.set(True)
                try:
                    with pytest.raises(RefusedError):
                        await Tag.objects.create(name="refused-in-block")
                finally:
                    refuse.reset(token)
                await Tag.objects.create(name="in-block")
            names = sorted([tag.name async for tag in Tag.objects.all()])
            assert names == ["after", "after-again", "in-block", "seed"]
            assert kinds.count("create") == 6
        finally:
            set_runner(previous)
            await router.close()


# The models a delete reaches (adorm generate)


def labels(found):
    return set(found)


def test_the_models_a_delete_of_a_library_model_reaches():
    assert labels(delete_models_of(dj.Book)) == {
        "library.Book",
        "library.Book_authors",
        "library.Edition",
        "library.Tagging",
        "library.Note",
        "library.Placement",
        "library.Loan",
        "library.Fine",
        "library.Reservation",
        "contenttypes.ContentType",
    }
    assert labels(delete_models_of(dj.Loan)) == {"library.Loan", "library.Fine"}
    assert labels(delete_models_of(dj.Author)) == {"library.Author", "library.Book_authors"}
    edition = labels(delete_models_of(dj.Edition))
    assert {"library.Edition", "library.Book", "library.Book_authors", "library.Loan", "library.Fine"} <= edition
    assert labels(delete_models_of(dj.BookProxy)) == labels(delete_models_of(dj.Book))


@isolate_apps("tests.djangoproject.library")
def test_the_closure_follows_on_delete():
    class Root(models.Model):
        class Meta:
            app_label = "library"

    class Cascaded(models.Model):
        root = models.ForeignKey(Root, on_delete=models.CASCADE)

        class Meta:
            app_label = "library"

    class BelowCascaded(models.Model):
        cascaded = models.ForeignKey(Cascaded, on_delete=models.CASCADE)

        class Meta:
            app_label = "library"

    class Protected(models.Model):
        root = models.ForeignKey(Root, on_delete=models.PROTECT)

        class Meta:
            app_label = "library"

    class BelowProtected(models.Model):
        protected = models.ForeignKey(Protected, on_delete=models.CASCADE)

        class Meta:
            app_label = "library"

    class Restricted(models.Model):
        root = models.ForeignKey(Root, on_delete=models.RESTRICT)

        class Meta:
            app_label = "library"

    class Nulled(models.Model):
        root = models.ForeignKey(Root, on_delete=models.SET_NULL, null=True)

        class Meta:
            app_label = "library"

    class BelowNulled(models.Model):
        nulled = models.ForeignKey(Nulled, on_delete=models.CASCADE)

        class Meta:
            app_label = "library"

    class Defaulted(models.Model):
        root = models.ForeignKey(Root, on_delete=models.SET_DEFAULT, null=True, default=None)

        class Meta:
            app_label = "library"

    class SetTo(models.Model):
        root = models.ForeignKey(Root, on_delete=models.SET(None), null=True)

        class Meta:
            app_label = "library"

    class Ignored(models.Model):
        root = models.ForeignKey(Root, on_delete=models.DO_NOTHING)

        class Meta:
            app_label = "library"

    assert labels(delete_models_of(Root)) == {
        "library.Root",
        "library.Cascaded",
        "library.BelowCascaded",
        "library.Protected",
        "library.Restricted",
        "library.Nulled",
        "library.Defaulted",
        "library.SetTo",
    }
    assert labels(delete_models_of(Ignored)) == {"library.Ignored"}
    assert labels(delete_models_of(BelowNulled)) == {"library.BelowNulled"}
    assert labels(delete_models_of(BelowProtected)) == {"library.BelowProtected"}


@isolate_apps("tests.djangoproject.library")
def test_a_custom_on_delete_handler_is_reported_and_its_model_included():
    def archive(collector, field, sub_objs, using):  # noqa: ARG001 - Django's signature
        pass

    class Owner(models.Model):
        class Meta:
            app_label = "library"

    class Archived(models.Model):
        owner = models.ForeignKey(Owner, on_delete=archive)

        class Meta:
            app_label = "library"

    with pytest.warns(CodegenWarning, match=r"library\.Archived\.owner"):
        found = labels(delete_models_of(Owner))
    assert found == {"library.Owner", "library.Archived"}


@isolate_apps("tests.djangoproject.library")
def test_a_many_to_many_delete_reaches_the_auto_created_through_table():
    class Label(models.Model):
        class Meta:
            app_label = "library"

    class Item(models.Model):
        labels = models.ManyToManyField(Label)

        class Meta:
            app_label = "library"

    assert labels(delete_models_of(Item)) == {"library.Item", "library.Item_labels"}
    assert labels(delete_models_of(Label)) == {"library.Label", "library.Item_labels"}


@pytest.mark.filterwarnings("ignore::adorm.codegen.CodegenWarning")
def test_the_model_spec_carries_the_closure():
    assert set(model_spec(dj.Book).delete_models) == labels(delete_models_of(dj.Book))
    assert set(model_spec(dj.Loan).delete_models) == {"library.Loan", "library.Fine"}


def test_model_meta_stores_delete_models_as_a_frozenset():
    meta = ModelMeta("library.Thing", delete_models=["library.Thing", "library.Other", "library.Thing"])
    assert meta.delete_models == frozenset({"library.Thing", "library.Other"})
    assert isinstance(meta.delete_models, frozenset)
    assert ModelMeta("library.Thing").delete_models is None
    with pytest.raises(ValueError):  # noqa: PT011 - the label check's message is its own
        ModelMeta("library.Thing", delete_models=["not a label"])


def test_a_model_written_by_hand_does_not_know_its_delete_models():
    assert Club._meta.delete_models is None
