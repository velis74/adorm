"""Writes whose Django signals (``pre_save``, ``post_save``, ``pre_delete``, ``post_delete``) have receivers.

The Django driver fires the signals on the slot's thread. The native PostgreSQL driver refuses such a write with
`NotSupportedError` before anything is written, naming the signal, the model and the receiver, unless the profile's
``covered_receivers`` lists the receiver by dotted path (``module.qualname``). A delete is refused for every model
Django's collector would delete, cascades and multi-table inheritance parents included; a ``SET_NULL`` update sends
no signal. ``bulk_create()`` and ``update()`` send no signals and are never refused. The memory driver runs no
receiver and refuses nothing.
"""

import datetime
import decimal
import gc
import re

import pytest

from django.db.models.signals import post_delete, post_save, pre_delete, pre_save

import adorm

from adorm import ConnectionProfile, NotSupportedError
from tests.clubmodels.activities import Activity, Competition, Tag
from tests.clubmodels.clubs import Club, Member, Membership
from tests.djangoproject.activities.models import Activity as DjangoActivity, Tag as DjangoTag

pytestmark = pytest.mark.django_db(transaction=True)

SETTING = "covered_receivers"
JOINED = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
SIGNAL_NAMES = {pre_save: "pre_save", post_save: "post_save", pre_delete: "pre_delete", post_delete: "post_delete"}

RECEIVED: list[tuple[str, str, str]] = []
"""``(signal, model label, instance name)`` of every call of the receivers below."""


def record(signal, sender, instance, **kwargs):  # noqa: ARG001 - Django's receiver signature
    RECEIVED.append((SIGNAL_NAMES[signal], sender._meta.label, getattr(instance, "name", None)))


def on_tag_saved(signal, sender, instance, **kwargs):
    record(signal, sender, instance, **kwargs)


def on_tag_pre_saved(signal, sender, instance, **kwargs):
    record(signal, sender, instance, **kwargs)


def on_activity_deleted(signal, sender, instance, **kwargs):
    record(signal, sender, instance, **kwargs)


def on_any_model_saved(signal, sender, instance, **kwargs):
    record(signal, sender, instance, **kwargs)


class Recorder:
    """A receiver as a bound method, which Django holds by a weak reference."""

    def saved(self, signal, sender, instance, **kwargs):
        record(signal, sender, instance, **kwargs)


def path(receiver) -> str:
    """The dotted path of a receiver, ``module.qualname``."""
    return f"{receiver.__module__}.{receiver.__qualname__}"


def covered(*receivers) -> dict:
    """Profile arguments that list ``receivers`` (functions or dotted paths) as covered on the adorm side."""
    return {SETTING: [receiver if isinstance(receiver, str) else path(receiver) for receiver in receivers]}


def refusal(signal: str, model: str, receiver) -> str:
    """A ``match`` pattern of the refusal: it names ``signal``, ``model`` and ``receiver``, in any order."""
    return rf"(?s)(?=.*\b{signal}\b)(?=.*{re.escape(model)})(?=.*{re.escape(path(receiver))})"


@pytest.fixture
def connect():
    """``connect(signal, receiver, sender, **kwargs)`` connects a receiver until the end of the test."""
    connected = []
    RECEIVED.clear()

    def do(signal, receiver, sender=None, **kwargs):
        signal.connect(receiver, sender=sender, **kwargs)
        connected.append((signal, receiver, sender, kwargs.get("dispatch_uid")))

    yield do
    for signal, receiver, sender, dispatch_uid in connected:
        signal.disconnect(receiver, sender=sender, dispatch_uid=dispatch_uid)
    RECEIVED.clear()


async def tag_names(prefix="sig"):
    return sorted([tag.name async for tag in Tag.objects.filter(name__startswith=prefix)])


async def club_with_activities(slug: str, *, activities: int = 2) -> tuple[Club, Member]:
    """A club with ``activities`` activities and one membership of a new member."""
    club = await Club.objects.create(name=f"Club {slug}", slug=slug, founded=datetime.date(2001, 2, 3), city="Kranj")
    member = await Member.objects.create(
        first_name="Signal", last_name=slug, email=f"{slug}@example.com", joined=JOINED
    )
    await Membership.objects.create(
        club_id=club.pk, member_id=member.pk, since=datetime.date(2026, 1, 1), fee=decimal.Decimal("10.00")
    )
    for number in range(activities):
        await Activity.objects.create(
            club_id=club.pk,
            name=f"{slug} activity {number}",
            starts_at=JOINED,
            duration=datetime.timedelta(hours=1),
            capacity=5,
        )
    return club, member


async def club_rows(club: Club) -> tuple[int, int, int]:
    """The rows of the club, its activities and its memberships."""
    return (
        await Club.objects.filter(pk=club.pk).count(),
        await Activity.objects.filter(club_id=club.pk).count(),
        await Membership.objects.filter(club_id=club.pk).count(),
    )


def skip_without_cascades(conformance_driver):
    if conformance_driver.driver_code == "not run":
        pytest.skip("the memory driver does not cascade deletes")


# Saves


async def test_create_with_a_post_save_receiver(conformance_driver, router, connect):
    connect(post_save, on_tag_saved, sender=DjangoTag)
    code = conformance_driver.driver_code
    if code == "refused":
        with pytest.raises(NotSupportedError, match=refusal("post_save", "activities.Tag", on_tag_saved)):
            await Tag.objects.create(name="sig-night")
        assert await tag_names() == []
    else:
        created = await Tag.objects.create(name="sig-night")
        assert created.name == "sig-night"
        assert await tag_names() == ["sig-night"]
    assert RECEIVED == ([("post_save", "activities.Tag", "sig-night")] if code == "runs" else [])
    assert router.pools[0].in_use == 0


async def test_create_with_a_covered_receiver(conformance_driver, make_router, connect):
    router = make_router(profile=covered(on_tag_saved), max_connections=2, acquire_timeout=5)
    connect(post_save, on_tag_saved, sender=DjangoTag)
    created = await Tag.objects.create(name="sig-night")
    assert created.pk is not None
    assert await tag_names() == ["sig-night"]
    # The Django driver ignores the setting: the receiver runs there.
    runs = conformance_driver.driver_code == "runs"
    assert RECEIVED == ([("post_save", "activities.Tag", "sig-night")] if runs else [])
    assert router.pools[0].in_use == 0


@pytest.mark.parametrize("signal", [pre_save, post_save], ids=["pre_save", "post_save"])
async def test_every_save_write_is_refused(conformance_driver, router, connect, signal):
    existing = await Tag.objects.create(name="sig-existing")
    connect(signal, on_tag_saved, sender=DjangoTag)
    name = SIGNAL_NAMES[signal]
    writes = {
        "create": lambda: Tag.objects.create(name="sig-created"),
        "save of a new instance": lambda: Tag(name="sig-new").save(),
        "save of a stored row": lambda: Tag(id=existing.pk, name="sig-renamed").save(),
        "get_or_create of a new row": lambda: Tag.objects.get_or_create(name="sig-got"),
        "update_or_create of a new row": lambda: Tag.objects.update_or_create(
            name="sig-upserted", defaults={"name": "sig-upserted"}
        ),
        "update_or_create of a stored row": lambda: Tag.objects.update_or_create(
            name="sig-existing", defaults={"name": "sig-updated"}
        ),
    }
    for label, write in writes.items():
        RECEIVED.clear()
        if conformance_driver.driver_code == "refused":
            with pytest.raises(NotSupportedError, match=refusal(name, "activities.Tag", on_tag_saved)):
                await write()
            assert await tag_names() == ["sig-existing"], label
        else:
            await write()
            assert (RECEIVED != []) == (conformance_driver.driver_code == "runs"), label
    if conformance_driver.driver_code == "refused":
        assert (await Tag.objects.get(pk=existing.pk)).name == "sig-existing"
    assert router.pools[0].in_use == 0


async def test_only_the_uncovered_receiver_is_named(conformance_driver, make_router, connect):
    make_router(profile=covered(on_tag_saved), max_connections=2, acquire_timeout=5)
    connect(post_save, on_tag_saved, sender=DjangoTag)
    connect(pre_save, on_tag_pre_saved, sender=DjangoTag)
    if conformance_driver.driver_code == "refused":
        with pytest.raises(NotSupportedError, match=refusal("pre_save", "activities.Tag", on_tag_pre_saved)) as caught:
            await Tag.objects.create(name="sig-night")
        assert path(on_tag_saved) not in str(caught.value)
        assert await tag_names() == []
    else:
        await Tag.objects.create(name="sig-night")
        assert await tag_names() == ["sig-night"]


async def test_receivers_of_another_model_do_not_refuse(router, connect):
    connect(post_save, on_tag_saved, sender=DjangoActivity)
    connect(post_delete, on_activity_deleted, sender=DjangoActivity)
    tag = await Tag.objects.create(name="sig-night")
    tag.name = "sig-nightly"
    await tag.save()
    assert await tag_names() == ["sig-nightly"]
    assert await tag.delete() == (1, {"activities.Tag": 1})
    assert await tag_names() == []
    assert RECEIVED == []
    assert router.pools[0].in_use == 0


async def test_a_receiver_for_every_sender(conformance_driver, make_router, connect):
    connect(post_save, on_any_model_saved)
    code = conformance_driver.driver_code
    make_router(max_connections=2, acquire_timeout=5)
    if code == "refused":
        with pytest.raises(NotSupportedError, match=refusal("post_save", "activities.Tag", on_any_model_saved)):
            await Tag.objects.create(name="sig-night")
        with pytest.raises(NotSupportedError, match=refusal("post_save", "clubs.Club", on_any_model_saved)):
            await Club.objects.create(name="Signals", slug="signals", founded=datetime.date(2001, 1, 1), city="Bled")
        assert await tag_names() == []
        assert await Club.objects.filter(slug="signals").count() == 0
    # Covered, it refuses no model.
    make_router(profile=covered(on_any_model_saved), max_connections=2, acquire_timeout=5)
    RECEIVED.clear()
    await Tag.objects.create(name="sig-night")
    await Club.objects.create(name="Signals", slug="signals", founded=datetime.date(2001, 1, 1), city="Bled")
    assert await tag_names() == ["sig-night"]
    assert RECEIVED == (
        [("post_save", "activities.Tag", "sig-night"), ("post_save", "clubs.Club", "Signals")] if code == "runs" else []
    )


async def test_a_receiver_connected_with_a_lazy_sender_and_a_dispatch_uid(conformance_driver, router, connect):
    connect(post_save, on_tag_saved, sender="activities.Tag", dispatch_uid="sig-tag-saved")
    if conformance_driver.driver_code == "refused":
        with pytest.raises(NotSupportedError, match=refusal("post_save", "activities.Tag", on_tag_saved)):
            await Tag.objects.create(name="sig-night")
        assert await tag_names() == []
    else:
        await Tag.objects.create(name="sig-night")
        assert await tag_names() == ["sig-night"]
    assert router.pools[0].in_use == 0


@pytest.mark.usefixtures("router")
async def test_a_bound_method_receiver_is_named_by_its_class(conformance_driver, connect):
    recorder = Recorder()
    connect(post_save, recorder.saved, sender=DjangoTag)
    if conformance_driver.driver_code == "refused":
        with pytest.raises(NotSupportedError, match=refusal("post_save", "activities.Tag", Recorder.saved)):
            await Tag.objects.create(name="sig-night")
        assert await tag_names() == []
    else:
        await Tag.objects.create(name="sig-night")
    assert RECEIVED == (
        [("post_save", "activities.Tag", "sig-night")] if conformance_driver.driver_code == "runs" else []
    )


async def test_a_bound_method_receiver_can_be_covered(make_router, connect):
    make_router(profile=covered(Recorder.saved), max_connections=2, acquire_timeout=5)
    recorder = Recorder()
    connect(post_save, recorder.saved, sender=DjangoTag)
    await Tag.objects.create(name="sig-night")
    assert await tag_names() == ["sig-night"]


async def test_a_dead_weak_receiver_does_not_refuse(router):
    RECEIVED.clear()

    def short_lived(signal, sender, instance, **kwargs):
        record(signal, sender, instance, **kwargs)

    post_save.connect(short_lived, sender=DjangoTag)  # weak by default
    del short_lived
    gc.collect()
    await Tag.objects.create(name="sig-night")
    assert await tag_names() == ["sig-night"]
    assert RECEIVED == []
    assert router.pools[0].in_use == 0


async def test_a_disconnected_receiver_does_not_refuse(router):
    post_save.connect(on_tag_saved, sender=DjangoTag)
    assert post_save.disconnect(on_tag_saved, sender=DjangoTag)
    await Tag.objects.create(name="sig-night")
    assert await tag_names() == ["sig-night"]
    assert router.pools[0].in_use == 0


async def test_a_refusal_in_a_transaction_leaves_it_usable(conformance_driver, router, connect):
    connect(post_save, on_tag_saved, sender=DjangoTag)
    refused = conformance_driver.driver_code == "refused"
    async with adorm.atomic():
        if refused:
            with pytest.raises(NotSupportedError):
                await Tag.objects.create(name="sig-night")
        assert await Tag.objects.filter(name="indoor").update(name="Indoor") == 1
        await Tag.objects.bulk_create([Tag(name="sig-bulk")])
    assert await Tag.objects.filter(name="Indoor").count() == 1
    assert await tag_names() == ["sig-bulk"]
    assert router.pools[0].in_use == 0


async def test_a_multi_table_child_save_signals_only_the_child(conformance_driver, router, connect):
    """Django sends a child's save signals with the child as sender, none for its parents."""
    if conformance_driver.driver_code == "not run":
        pytest.skip("the memory driver does not insert multi-table inheritance children")
    club = await Club.objects.order_by("pk").first()
    connect(pre_save, on_activity_deleted, sender=DjangoActivity)
    connect(post_save, on_activity_deleted, sender=DjangoActivity)
    competition = await Competition.objects.create(
        club_id=club.pk,
        name="sig-cup",
        starts_at=JOINED,
        duration=datetime.timedelta(hours=2),
        capacity=10,
        discipline="sprint",
    )
    assert await Competition.objects.filter(pk=competition.pk).count() == 1
    assert RECEIVED == []
    assert router.pools[0].in_use == 0


# Writes that send no signals


async def test_bulk_create_and_update_are_not_refused(router, connect):
    for signal in (pre_save, post_save, pre_delete, post_delete):
        connect(signal, on_tag_saved, sender=DjangoTag)
    created = await Tag.objects.bulk_create([Tag(name="sig-night"), Tag(name="sig-noon")])
    assert [tag.name for tag in created] == ["sig-night", "sig-noon"]
    assert await Tag.objects.filter(name="sig-noon").update(name="sig-noonish") == 1
    assert await Tag.objects.filter(name="sig-absent").update(name="sig-none") == 0
    assert await Tag.objects.bulk_create([]) == []
    assert await tag_names() == ["sig-night", "sig-noonish"]
    assert RECEIVED == []
    assert router.pools[0].in_use == 0


# Deletes


@pytest.mark.parametrize("signal", [pre_delete, post_delete], ids=["pre_delete", "post_delete"])
async def test_delete_of_a_model_with_a_receiver(conformance_driver, router, connect, signal):
    night = await Tag.objects.create(name="sig-night")
    await Tag.objects.create(name="sig-noon")
    connect(signal, on_tag_saved, sender=DjangoTag)
    code = conformance_driver.driver_code
    if code == "refused":
        with pytest.raises(NotSupportedError, match=refusal(SIGNAL_NAMES[signal], "activities.Tag", on_tag_saved)):
            await night.delete()
        with pytest.raises(NotSupportedError, match=refusal(SIGNAL_NAMES[signal], "activities.Tag", on_tag_saved)):
            await Tag.objects.filter(name__startswith="sig").delete()
        assert await tag_names() == ["sig-night", "sig-noon"]
    else:
        assert await night.delete() == (1, {"activities.Tag": 1})
        assert await Tag.objects.filter(name__startswith="sig").delete() == (1, {"activities.Tag": 1})
        assert await tag_names() == []
    expected = [
        (SIGNAL_NAMES[signal], "activities.Tag", "sig-night"),
        (SIGNAL_NAMES[signal], "activities.Tag", "sig-noon"),
    ]
    assert RECEIVED == (expected if code == "runs" else [])
    assert router.pools[0].in_use == 0


@pytest.mark.parametrize("signal", [pre_delete, post_delete], ids=["pre_delete", "post_delete"])
async def test_a_cascaded_childs_receiver_refuses_the_parents_delete(conformance_driver, router, connect, signal):
    skip_without_cascades(conformance_driver)
    first, _ = await club_with_activities("sig-first")
    second, _ = await club_with_activities("sig-second")
    connect(signal, on_activity_deleted, sender=DjangoActivity)
    name = SIGNAL_NAMES[signal]
    if conformance_driver.driver_code == "refused":
        with pytest.raises(NotSupportedError, match=refusal(name, "activities.Activity", on_activity_deleted)):
            await first.delete()
        with pytest.raises(NotSupportedError, match=refusal(name, "activities.Activity", on_activity_deleted)):
            await Club.objects.filter(slug=second.slug).delete()
        assert await club_rows(first) == (1, 2, 1)
        assert await club_rows(second) == (1, 2, 1)
        assert RECEIVED == []
    else:
        counts = (4, {"clubs.Club": 1, "activities.Activity": 2, "clubs.Membership": 1})
        assert await first.delete() == counts
        assert await Club.objects.filter(slug=second.slug).delete() == counts
        assert await club_rows(first) == (0, 0, 0)
        assert await club_rows(second) == (0, 0, 0)
        assert sorted(RECEIVED) == sorted(
            (name, "activities.Activity", f"{slug} activity {number}")
            for slug in ("sig-first", "sig-second")
            for number in range(2)
        )
    assert router.pools[0].in_use == 0


async def test_a_cascaded_delete_with_the_receiver_covered(conformance_driver, make_router, connect):
    skip_without_cascades(conformance_driver)
    router = make_router(profile=covered(on_activity_deleted), max_connections=2, acquire_timeout=5)
    club, _ = await club_with_activities("sig-covered")
    connect(post_delete, on_activity_deleted, sender=DjangoActivity)
    assert await club.delete() == (4, {"clubs.Club": 1, "activities.Activity": 2, "clubs.Membership": 1})
    assert await club_rows(club) == (0, 0, 0)
    assert len(RECEIVED) == (2 if conformance_driver.driver_code == "runs" else 0)
    assert router.pools[0].in_use == 0


async def test_a_delete_in_a_transaction_is_refused_before_anything_is_deleted(conformance_driver, router, connect):
    skip_without_cascades(conformance_driver)
    if conformance_driver.driver_code != "refused":
        pytest.skip("only the native PostgreSQL driver refuses")
    club, _ = await club_with_activities("sig-atomic")
    connect(post_delete, on_activity_deleted, sender=DjangoActivity)
    async with adorm.atomic():
        with pytest.raises(NotSupportedError, match=refusal("post_delete", "activities.Activity", on_activity_deleted)):
            await club.delete()
        assert await club_rows(club) == (1, 2, 1)
        assert await Club.objects.filter(pk=club.pk).update(city="Bled") == 1
    assert (await Club.objects.get(pk=club.pk)).city == "Bled"
    assert await club_rows(club) == (1, 2, 1)
    assert router.pools[0].in_use == 0


async def test_a_set_null_update_sends_no_signal(conformance_driver, router, connect):
    skip_without_cascades(conformance_driver)
    club, _ = await club_with_activities("sig-coach", activities=0)
    coach = await Member.objects.create(
        first_name="Coach", last_name="Signal", email="sig-coach-coach@example.com", joined=JOINED
    )
    activity = await Activity.objects.create(
        club_id=club.pk,
        coach_id=coach.pk,
        name="sig-drills",
        starts_at=JOINED,
        duration=datetime.timedelta(hours=1),
        capacity=5,
    )
    for signal in (pre_save, post_save, pre_delete, post_delete):
        connect(signal, on_activity_deleted, sender=DjangoActivity)
    assert await coach.delete() == (1, {"clubs.Member": 1})
    assert (await Activity.objects.get(pk=activity.pk)).coach_id is None
    assert RECEIVED == []
    assert router.pools[0].in_use == 0


async def test_a_parents_receiver_refuses_a_multi_table_childs_delete(conformance_driver, router, connect):
    """Django's collector deletes a child's parent rows and sends their signals."""
    skip_without_cascades(conformance_driver)
    club = await Club.objects.order_by("pk").first()
    competition = await Competition.objects.create(
        club_id=club.pk,
        name="sig-cup",
        starts_at=JOINED,
        duration=datetime.timedelta(hours=2),
        capacity=10,
        discipline="sprint",
    )
    connect(pre_delete, on_activity_deleted, sender=DjangoActivity)
    if conformance_driver.driver_code == "refused":
        with pytest.raises(NotSupportedError, match=refusal("pre_delete", "activities.Activity", on_activity_deleted)):
            await competition.delete()
        assert await Competition.objects.filter(pk=competition.pk).count() == 1
        assert await Activity.objects.filter(pk=competition.pk).count() == 1
    else:
        assert await competition.delete() == (2, {"activities.Competition": 1, "activities.Activity": 1})
        assert await Activity.objects.filter(pk=competition.pk).count() == 0
        assert RECEIVED == [("pre_delete", "activities.Activity", "sig-cup")]
    assert router.pools[0].in_use == 0


# The setting


def test_the_setting_is_kept_on_the_profile():
    profile = ConnectionProfile(
        "default", "adorm.drivers.asyncpg.AsyncpgDriver", **covered(on_tag_saved, Recorder.saved)
    )
    assert set(getattr(profile, SETTING)) == {path(on_tag_saved), path(Recorder.saved)}
    assert set(getattr(ConnectionProfile("default", "adorm.drivers.asyncpg.AsyncpgDriver"), SETTING)) == set()


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ([42], TypeError),
        ([None], TypeError),
        ([on_tag_saved], TypeError),
        ("tests.conformance.test_signal_receivers.on_tag_saved", TypeError),
        ([""], ValueError),
        (["on_tag_saved"], ValueError),
    ],
    ids=["int", "None", "function", "bare string", "empty", "no module"],
)
@pytest.mark.parametrize("driver", ["adorm.drivers.asyncpg.AsyncpgDriver", "adorm.drivers.django.DjangoDriver"])
def test_invalid_entries_are_rejected(value, error, driver):
    with pytest.raises(error, match=SETTING):
        ConnectionProfile("default", driver, **{SETTING: value})
