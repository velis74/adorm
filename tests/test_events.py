import logging

from dataclasses import FrozenInstanceError

import pytest

from adorm.events import BackendEvent, CallbackEvent, ErrorEvent, Event, EventBus, ResultEvent


def test_events_are_frozen_values():
    assert BackendEvent("notify", "ch", "x") == BackendEvent("notify", "ch", "x")
    assert BackendEvent("notice").channel is None
    assert CallbackEvent("on_commit").payload is None
    with pytest.raises(FrozenInstanceError):
        CallbackEvent("on_commit").name = "x"
    for event in (ResultEvent(1, []), ErrorEvent(1, ValueError()), CallbackEvent("a"), BackendEvent("b")):
        assert isinstance(event, Event)


def test_dispatch_by_type_name_and_channel():
    bus = EventBus()
    seen = []
    bus.subscribe(Event, lambda e: seen.append(("any", e)))
    bus.subscribe(BackendEvent, lambda e: seen.append(("backend", e)))
    bus.subscribe(BackendEvent, lambda e: seen.append(("notify", e)), name="notify")
    bus.subscribe(BackendEvent, lambda e: seen.append(("orders", e)), name="notify", channel="orders")
    bus.subscribe(CallbackEvent, lambda e: seen.append(("commit", e)), name="on_commit")

    orders = BackendEvent("notify", "orders", "1")
    assert bus.dispatch(orders) == 4
    assert [tag for tag, _ in seen] == ["any", "backend", "notify", "orders"]
    seen.clear()
    assert bus.dispatch(BackendEvent("notify", "users")) == 3
    assert bus.dispatch(CallbackEvent("on_commit")) == 2
    assert bus.dispatch(CallbackEvent("on_rollback")) == 1


def test_unsubscribe():
    bus = EventBus()
    seen = []
    unsubscribe = bus.subscribe(CallbackEvent, seen.append)
    bus.subscribe(CallbackEvent, seen.append)
    unsubscribe()
    unsubscribe()
    assert bus.dispatch(CallbackEvent("a")) == 1
    assert len(seen) == 1


def test_same_handler_subscribed_twice_unsubscribes_separately():
    bus = EventBus()
    seen = []
    first = bus.subscribe(CallbackEvent, seen.append)
    bus.subscribe(CallbackEvent, seen.append)
    first()
    bus.dispatch(CallbackEvent("a"))
    assert len(seen) == 1


def test_failing_handler_is_logged_and_others_run(caplog):
    bus = EventBus()
    seen = []

    def broken(_event):
        raise RuntimeError("boom")

    bus.subscribe(Event, broken)
    bus.subscribe(Event, seen.append)
    with caplog.at_level(logging.ERROR, logger="adorm.events"):
        assert bus.dispatch(CallbackEvent("a")) == 2
    assert seen == [CallbackEvent("a")]
    assert "boom" in caplog.text


def test_changes_during_dispatch_apply_to_the_next_event():
    bus = EventBus()
    seen = []
    unsubscribe_later = None

    def first(event):
        seen.append(("first", event.name))
        bus.subscribe(CallbackEvent, lambda e: seen.append(("added", e.name)))
        unsubscribe_later()

    bus.subscribe(CallbackEvent, first)
    unsubscribe_later = bus.subscribe(CallbackEvent, lambda e: seen.append(("removed", e.name)))
    bus.dispatch(CallbackEvent("one"))
    assert seen == [("first", "one")]
    seen.clear()
    bus.dispatch(CallbackEvent("two"))
    assert seen == [("first", "two"), ("added", "two")]
