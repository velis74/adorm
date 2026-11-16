"""Python code of the target ORM that a write would call: a model's ``save()`` and ``delete()`` overrides, query set
overrides of ``create()``, ``update()`` and ``bulk_create()``, custom ``on_delete`` handlers, Django's signals and
``on_commit()`` callbacks. ``bulk_create()`` calls no ``save()`` and sends no signal on any driver. The Django driver
runs the rest on the driver side; the native PostgreSQL driver runs none of it and refuses a write that would call an
override or a custom handler, before anything is written; the memory driver runs none of it. Either way the client
sees no signal and the pool and the transaction stay usable. ``pre_init`` and ``post_init`` fire on the drivers that
build Django model instances."""

import asyncio
import datetime

import pytest

import adorm

from adorm import NotSupportedError
from tests.clubmodels.activities import Activity, Tag
from tests.clubmodels.clubs import Club, Member
from tests.conformance.drivers import DRIVER_CODE
from tests.djangoproject.activities.models import Activity as DjangoActivity, Tag as DjangoTag

pytestmark = pytest.mark.django_db(transaction=True)

REPEAT = range(3)


@pytest.fixture
def calls():
    """The calls of the Django model ``Tag``'s overrides, in order."""
    return []


@pytest.fixture
def save_override(monkeypatch, calls, router):  # noqa: ARG001 - installed after the data is seeded
    """An override of ``Tag.save()`` that stores the name in upper case."""

    def save(self, *args, **kwargs):
        calls.append(("save", self.name))
        self.name = self.name.upper()
        super(DjangoTag, self).save(*args, **kwargs)

    monkeypatch.setattr(DjangoTag, "save", save)


@pytest.fixture
def delete_override(monkeypatch, calls):
    """``install()`` overrides ``Tag.delete()`` with one that deletes nothing."""

    def delete(self, *args, **kwargs):  # noqa: ARG001 - Django's signature
        calls.append(("delete", self.name))

    return lambda: monkeypatch.setattr(DjangoTag, "delete", delete)


async def names():
    return sorted([tag.name async for tag in Tag.objects.filter(name__istartswith="n")])


@pytest.mark.usefixtures("save_override")
async def test_a_models_save_override(conformance_driver, router, calls):
    pool = router.pools[0]
    code = conformance_driver.driver_code
    expected = {
        "runs": (["NIGHT", "NOON"], [("save", "night"), ("save", "NIGHT"), ("save", "noon")]),
        "refused": ([], []),
        "not run": (["night", "noon"], []),
    }[code]
    for _ in REPEAT:
        await Tag.objects.filter(name__istartswith="n").delete()
        calls.clear()
        if code == "refused":
            for write in (Tag.objects.create(name="night"), Tag(name="night").save()):
                with pytest.raises(NotSupportedError, match=r"overrides save\(\)"):
                    await write
            with pytest.raises(NotSupportedError, match=r"overrides save\(\)"):
                await Tag.objects.get_or_create(name="noon")
        else:
            night = await Tag.objects.create(name="night")
            await night.save()
            await Tag.objects.get_or_create(name="noon")
        assert (await names(), calls) == expected
        assert pool.in_use == 0
    async with adorm.atomic():
        if code == "refused":
            with pytest.raises(NotSupportedError):
                await Tag.objects.create(name="night")
        assert await Tag.objects.filter(name="indoor").update(name="Indoor") == 1
    assert await Tag.objects.filter(name="Indoor").count() == 1


@pytest.mark.usefixtures("save_override")
async def test_bulk_create_calls_no_save_override_and_sends_no_signal_on_any_driver(router, calls):
    from django.db.models.signals import post_save, pre_save

    received = []

    def receiver(signal, sender, instance, **kwargs):  # noqa: ARG001
        received.append(instance.name)

    for signal in (pre_save, post_save):
        signal.connect(receiver, sender=DjangoTag, weak=False)
    try:
        for _ in REPEAT:
            await Tag.objects.filter(name__istartswith="n").delete()
            created = await Tag.objects.bulk_create([Tag(name="night"), Tag(name="noon")])
            assert [tag.name for tag in created] == ["night", "noon"]
            assert (await names(), calls, received) == (["night", "noon"], [], [])
    finally:
        for signal in (pre_save, post_save):
            signal.disconnect(receiver, sender=DjangoTag)
    assert router.pools[0].in_use == 0


async def test_a_query_set_override_of_bulk_create(conformance_driver, router, calls, monkeypatch):
    from django.db.models import QuerySet

    class TagQuerySet(QuerySet):
        def bulk_create(self, objs, *args, **kwargs):
            calls.append(("bulk_create", [obj.name for obj in objs]))
            for obj in objs:
                obj.name = obj.name.upper()
            return super().bulk_create(objs, *args, **kwargs)

    pool = router.pools[0]
    code = conformance_driver.driver_code
    monkeypatch.setattr(DjangoTag._base_manager, "_queryset_class", TagQuerySet)
    expected = {
        "runs": (["NIGHT", "NOON"], [("bulk_create", ["night", "noon"])]),
        "refused": ([], []),
        "not run": (["night", "noon"], []),
    }[code]
    for _ in REPEAT:
        calls.clear()
        if code == "refused":
            with pytest.raises(NotSupportedError, match=r"overrides bulk_create\(\)"):
                await Tag.objects.bulk_create([Tag(name="night"), Tag(name="noon")])
        else:
            created = await Tag.objects.bulk_create([Tag(name="night"), Tag(name="noon")])
            assert [tag.name for tag in created] == expected[0]
        assert (await names(), calls) == expected
        assert pool.in_use == 0
        await Tag.objects.filter(name__istartswith="n").delete()


async def test_a_models_delete_override(conformance_driver, router, calls, delete_override):
    pool = router.pools[0]
    code = conformance_driver.driver_code
    one = (1, {"activities.Tag": 1})
    night = await Tag.objects.create(name="night")
    delete_override()
    for _ in REPEAT:
        calls.clear()
        if code == "refused":
            with pytest.raises(NotSupportedError, match=r"overrides delete\(\)"):
                await night.delete()
        else:
            assert await night.delete() == ((0, {}) if code == "runs" else one)
        assert await names() == ([] if code == "not run" else ["night"])
        assert calls == ([("delete", "night")] if code == "runs" else [])
        if code == "not run":
            night = await Tag.objects.create(name="night")
    # A query set delete runs no model's delete() on any driver.
    assert await Tag.objects.filter(name__istartswith="n").delete() == one
    assert calls == ([("delete", "night")] if code == "runs" else [])
    assert pool.in_use == 0


async def test_signals_fire_on_the_django_driver_only(conformance_driver, router):
    from django.db.models.signals import post_delete, post_save, pre_delete, pre_save

    received = []

    def receiver(signal, sender, instance, **kwargs):  # noqa: ARG001
        received.append((signal, instance.name))

    signals = (pre_save, post_save, pre_delete, post_delete)
    for signal in signals:
        signal.connect(receiver, sender=DjangoTag, weak=False)
    try:
        for _ in REPEAT:
            received.clear()
            tag = await Tag.objects.create(name="night")
            tag.name = "nightly"
            await tag.save()
            await tag.delete()
            fired = [name for _, name in received]
            assert fired == (
                ["night", "night", "nightly", "nightly", "nightly", "nightly"]
                if conformance_driver.driver_code == "runs"
                else []
            )
    finally:
        for signal in signals:
            signal.disconnect(receiver, sender=DjangoTag)
    assert router.pools[0].in_use == 0


async def test_a_query_set_override_of_create_and_update(conformance_driver, router, calls, monkeypatch):
    from django.db.models import QuerySet

    class TagQuerySet(QuerySet):
        def create(self, **kwargs):
            calls.append(("create", kwargs["name"]))
            return super().create(**{**kwargs, "name": kwargs["name"].upper()})

        def update(self, **kwargs):
            calls.append(("update", kwargs["name"]))
            return super().update(**kwargs)

    pool = router.pools[0]
    code = conformance_driver.driver_code
    await Tag.objects.create(name="noon")
    monkeypatch.setattr(DjangoTag._base_manager, "_queryset_class", TagQuerySet)  # the manager adorm writes through
    expected = {
        "runs": (["NIGHT", "noonish"], [("create", "night"), ("update", "noonish")]),
        "refused": (["noon"], []),
        "not run": (["night", "noonish"], []),
    }[code]
    for _ in REPEAT:
        calls.clear()
        if code == "refused":
            with pytest.raises(NotSupportedError, match=r"overrides create\(\)"):
                await Tag.objects.create(name="night")
            with pytest.raises(NotSupportedError, match=r"overrides update\(\)"):
                await Tag.objects.filter(name="noon").update(name="noonish")
        else:
            await Tag.objects.create(name="night")
            await Tag.objects.filter(name="noon").update(name="noonish")
        assert (await names(), calls) == expected
        assert pool.in_use == 0
        if code != "refused":
            await Tag.objects.filter(name__iexact="night").delete()
            await Tag.objects.filter(name="noonish").update(name="noon")
            calls.clear()


async def test_a_custom_on_delete_handler(conformance_driver, router, calls, monkeypatch):
    def unassign(collector, field, sub_objs, using):  # noqa: ARG001 - Django's handler signature
        calls.append(("on_delete", sorted(activity.name for activity in sub_objs)))
        collector.add_field_update(field, None, sub_objs)

    pool = router.pools[0]
    code = conformance_driver.driver_code
    club = await Club.objects.order_by("pk").first()
    monkeypatch.setattr(DjangoActivity._meta.get_field("coach").remote_field, "on_delete", unassign)
    joined = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    for attempt in REPEAT:
        calls.clear()
        coach = await Member.objects.create(
            first_name="Coach", last_name=str(attempt), email=f"coach{attempt}@example.com", joined=joined
        )
        activity = await Activity.objects.create(
            club_id=club.pk,
            coach_id=coach.pk,
            name=f"Drills {attempt}",
            starts_at=joined,
            duration=datetime.timedelta(hours=1),
            capacity=5,
        )
        if code == "runs":
            assert await Member.objects.filter(pk=coach.pk).delete() == (1, {"clubs.Member": 1})
        else:
            # The native driver refuses the handler; the memory driver does not cascade deletes.
            match = r"on_delete handlers" if code == "refused" else "does not cascade"
            with pytest.raises(NotSupportedError, match=match):
                await Member.objects.filter(pk=coach.pk).delete()
        assert calls == ([("on_delete", [activity.name])] if code == "runs" else [])
        assert await Member.objects.filter(pk=coach.pk).exists() == (code != "runs")
        assert (await Activity.objects.get(pk=activity.pk)).coach_id == (None if code == "runs" else coach.pk)
        assert pool.in_use == 0


async def test_djangos_on_commit_callbacks_run_on_the_django_driver_only(conformance_driver, router):
    from django.db import transaction
    from django.db.models.signals import post_save

    committed = []

    def receiver(sender, instance, **kwargs):  # noqa: ARG001
        transaction.on_commit(lambda name=instance.name: committed.append(name))

    async def rolled_back(name):
        async with adorm.atomic():
            await Tag.objects.create(name=name)
            raise RuntimeError

    post_save.connect(receiver, sender=DjangoTag, weak=False)
    runs = conformance_driver.driver_code == "runs"
    try:
        for attempt in REPEAT:
            committed.clear()
            async with adorm.atomic():
                await Tag.objects.create(name=f"night{attempt}")
                assert committed == []
            if runs:
                # The callbacks run on the slot's thread after the commit's outcome is delivered.
                async with asyncio.timeout(5):
                    while not committed:
                        await asyncio.sleep(0)
            assert committed == ([f"night{attempt}"] if runs else [])
            # A rolled-back transaction runs none.
            with pytest.raises(RuntimeError):
                await rolled_back(f"noon{attempt}")
            assert committed == ([f"night{attempt}"] if runs else [])
    finally:
        post_save.disconnect(receiver, sender=DjangoTag)
    assert await names() == [f"night{attempt}" for attempt in REPEAT]
    assert router.pools[0].in_use == 0


async def test_pre_init_and_post_init_fire_where_django_instances_are_built(conformance_driver, router):
    from django.db.models.signals import post_init, pre_init

    received = []

    def on_pre_init(kwargs, **_):
        received.append(("pre_init", kwargs.get("name")))

    def on_post_init(instance, **_):
        received.append(("post_init", instance.name))

    pre_init.connect(on_pre_init, sender=DjangoTag, weak=False)
    post_init.connect(on_post_init, sender=DjangoTag, weak=False)
    builds = conformance_driver.name in ("django", "asyncpg")
    try:
        for attempt in REPEAT:
            received.clear()
            await Tag.objects.create(name=f"night{attempt}")
            fired = [("pre_init", f"night{attempt}"), ("post_init", f"night{attempt}")]
            assert received == (fired if builds else [])
    finally:
        pre_init.disconnect(on_pre_init, sender=DjangoTag)
        post_init.disconnect(on_post_init, sender=DjangoTag)
    assert router.pools[0].in_use == 0


def test_every_driver_states_what_it_does_with_driver_code():
    from tests.conformance.drivers import DRIVERS

    assert {driver.driver_code for driver in DRIVERS} <= set(DRIVER_CODE)
