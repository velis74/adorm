import datetime
import decimal
import json
import logging

from dataclasses import FrozenInstanceError

import pytest

from adorm import (
    create_session,
    DatabaseError,
    DeadlockError,
    IntegrityError,
    InterfaceError,
    Model,
    ModelMeta,
    MultipleObjectsReturned,
    ObjectDoesNotExist,
    OperationalError,
    OperationTimeout,
    SessionTimeout,
    ValidationError,
)
from adorm.events import (
    BackendEvent,
    CallbackEvent,
    CancelledEvent,
    decode_error,
    encode_error,
    ErrorEvent,
    Event,
    EventBus,
    EventChannel,
    ON_COMMIT,
    ON_ROLLBACK,
    OUTCOME_EVENTS,
    RESERVED_CALLBACKS,
    ResultEvent,
)


def test_events_are_frozen_values():
    assert BackendEvent("notify", "ch", "x") == BackendEvent("notify", "ch", "x")
    assert BackendEvent("notice").channel is None
    assert CallbackEvent("on_commit").payload is None
    with pytest.raises(FrozenInstanceError):
        CallbackEvent("on_commit").name = "x"
    events = (ResultEvent(1, []), ErrorEvent(1, ValueError()), CancelledEvent(1, False), CallbackEvent("a"))
    for event in (*events, BackendEvent("b")):
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


# Encoded form


ROUND_TRIPS = [
    ResultEvent(1, None),
    ResultEvent(2, 42),
    ResultEvent("r-3", [{"id": 1, "name": "Ana", "joined": datetime.datetime(2026, 1, 2, 3, 4, 5)}]),
    ResultEvent(4, {"fee": decimal.Decimal("12.50"), "tags": ["a", "b"], "when": datetime.date(2026, 11, 15)}),
    CancelledEvent(5, started=False),
    CancelledEvent(6, started=True),
    CallbackEvent("on_commit"),
    CallbackEvent("cache_cleared", {"keys": ["a", "b"]}),
    BackendEvent("notify", "orders", "42"),
    BackendEvent("notice", payload={"severity": "WARNING", "message": "x"}),
]


@pytest.mark.parametrize("event", ROUND_TRIPS, ids=repr)
def test_events_round_trip_through_json(event):
    data = json.loads(json.dumps(event.to_dict()))
    assert data["event"] == event.kind
    assert Event.from_dict(data) == event


def test_error_events_round_trip_by_class_message_and_sqlstate():
    event = ErrorEvent(7, IntegrityError("duplicate key", sqlstate="23505"))
    decoded = Event.from_dict(json.loads(json.dumps(event.to_dict())))
    assert isinstance(decoded, ErrorEvent)
    assert decoded.request_id == 7
    assert type(decoded.error) is IntegrityError
    assert str(decoded.error) == "duplicate key"
    assert decoded.error.sqlstate == "23505"


def test_custom_value_codec_is_used_for_values_and_payloads():
    encoded = []

    def encode(value):
        encoded.append(value)
        return {"raw": value}

    def decode(data):
        return data["raw"]

    for event in (ResultEvent(1, "v"), CallbackEvent("c", "p"), BackendEvent("b", None, "q")):
        assert Event.from_dict(event.to_dict(encode), decode) == event
    assert encoded == ["v", "p", "q"]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([], "is a mapping"),
        ({"event": "telepathy"}, "unknown event"),
        ({"event": "result", "request_id": 1}, "needs 'value'"),
        ({"event": "cancelled", "request_id": 1, "started": "yes"}, "is a boolean"),
        ({"event": "backend", "name": "notify", "payload": {"value": None}}, "needs 'channel'"),
        ({"event": "result", "request_id": True, "value": {"value": None}}, "request id"),
        ({"event": "error", "request_id": [1], "error": {"class": "DatabaseError"}}, "request id"),
        ({"event": "cancelled", "request_id": 1.5, "started": True}, "request id"),
        ({"event": "result", "request_id": None, "value": {"value": None}}, "request id"),
        ({"event": "callback", "name": 5, "payload": {"value": None}}, "'name' of an encoded event is a string"),
        ({"event": "backend", "name": None, "channel": None, "payload": {"value": None}}, "'name'"),
        ({"event": "backend", "name": "notify", "channel": 7, "payload": {"value": None}}, "a string or null"),
    ],
)
def test_malformed_encoded_events_raise_value_error(data, message):
    with pytest.raises(ValueError, match=message):
        Event.from_dict(data)


@pytest.mark.parametrize("request_id", [object(), True, None, 1.5])
def test_only_string_and_integer_request_ids_encode(request_id):
    with pytest.raises(TypeError, match="request id"):
        ResultEvent(request_id, 1).to_dict()


def test_the_base_event_has_no_encoded_form():
    with pytest.raises(TypeError, match="no encoded form"):
        Event().to_dict()


def test_outcome_events_and_reserved_callbacks():
    assert OUTCOME_EVENTS == (ResultEvent, ErrorEvent, CancelledEvent)
    assert {ON_COMMIT, ON_ROLLBACK} == RESERVED_CALLBACKS


# Encoded errors


class CustomIntegrityError(IntegrityError):
    pass


class Gadget(Model):
    _meta = ModelMeta("eventtest.Gadget")
    id: int | None = None


def round_trip(error):
    return decode_error(json.loads(json.dumps(encode_error(error))))


def test_database_errors_keep_class_message_and_sqlstate_but_not_the_original():
    original = RuntimeError("driver detail")
    decoded = round_trip(OperationalError("lost", original=original, sqlstate="08006"))
    assert type(decoded) is OperationalError
    assert (str(decoded), decoded.sqlstate, decoded.original) == ("lost", "08006", None)


def test_an_adorm_subclass_outside_the_hierarchy_decodes_as_its_nearest_adorm_class():
    decoded = round_trip(CustomIntegrityError("custom", sqlstate="23503"))
    assert type(decoded) is IntegrityError
    assert decoded.sqlstate == "23503"


def test_a_foreign_exception_decodes_as_database_error_naming_its_class():
    decoded = round_trip(ValueError("bad value"))
    assert type(decoded) is DatabaseError
    assert str(decoded) == "ValueError: bad value"


def test_executor_errors_keep_sessions_timeouts_and_phase():
    a, b = create_session("default", "ev-a"), create_session("default", "ev-b")
    deadlock = round_trip(DeadlockError(held_sessions=(a,), session=b, timeout=2.5))
    assert type(deadlock) is DeadlockError
    assert deadlock.held_sessions == (a,)
    assert deadlock.session is b
    assert deadlock.timeout == 2.5
    timeout = round_trip(SessionTimeout(session=a, timeout=1))
    assert (type(timeout), timeout.session, timeout.timeout) == (SessionTimeout, a, 1)
    expired = round_trip(OperationTimeout(phase="executing", operation="op", timeout=3))
    assert (type(expired), expired.phase, expired.timeout, expired.operation) == (
        OperationTimeout,
        "executing",
        3,
        None,
    )
    assert round_trip(SessionTimeout("plain")).session is None


def test_validation_errors_keep_their_messages():
    by_field = round_trip(ValidationError({"name": ["required"], "age": ["too low"]}))
    assert by_field.message_dict == {"name": ["required"], "age": ["too low"]}
    listed = round_trip(ValidationError(["one", "two"]))
    assert listed.messages == ["one", "two"]


def test_model_exceptions_decode_as_the_models_own():
    decoded = round_trip(Gadget.DoesNotExist("no gadget"))
    assert type(decoded) is Gadget.DoesNotExist
    assert str(decoded) == "no gadget"
    assert type(round_trip(Gadget.MultipleObjectsReturned("two"))) is Gadget.MultipleObjectsReturned


def test_a_model_exception_of_an_unknown_model_decodes_as_the_generic_one():
    unknown = decode_error({"class": "DoesNotExist", "model": "nowhere.Thing", "message": "x"})
    assert type(unknown) is ObjectDoesNotExist
    several = decode_error({"class": "MultipleObjectsReturned", "model": "nowhere.Thing", "message": "x"})
    assert type(several) is MultipleObjectsReturned


def test_an_unknown_error_class_decodes_as_database_error():
    decoded = decode_error({"class": "FluxCapacitorError", "message": "88 mph"})
    assert type(decoded) is DatabaseError
    assert str(decoded) == "88 mph"


@pytest.mark.parametrize("data", [None, {}, {"class": 1}])
def test_malformed_encoded_errors_raise_value_error(data):
    with pytest.raises(ValueError, match="encoded error"):
        decode_error(data)


# The channel


async def test_outcomes_settle_the_futures_awaiting_them():
    channel = EventChannel()
    result, error, cancelled = channel.expect(1), channel.expect(2), channel.expect(3)
    assert channel.awaited == 3
    channel.deliver(ResultEvent(1, [{"id": 1}]))
    channel.deliver(ErrorEvent(2, IntegrityError("dup")))
    channel.deliver(CancelledEvent(3, started=True))
    assert await result == [{"id": 1}]
    with pytest.raises(IntegrityError):
        await error
    assert cancelled.cancelled()
    assert channel.awaited == 0
    assert channel.events.empty()


async def test_unawaited_outcomes_go_to_the_queue_and_forget_stops_awaiting():
    channel = EventChannel()
    future = channel.expect(1)
    channel.forget(1)
    channel.forget(1)
    channel.deliver(ResultEvent(1, "late"))
    channel.deliver(CancelledEvent(2, started=False))
    assert not future.done()
    assert channel.events.get_nowait() == ResultEvent(1, "late")
    assert channel.events.get_nowait() == CancelledEvent(2, started=False)


async def test_an_outcome_for_a_future_already_cancelled_by_its_awaiter_is_consumed():
    channel = EventChannel()
    future = channel.expect(1)
    future.cancel()
    channel.deliver(ResultEvent(1, "finished anyway"))
    assert channel.awaited == 0
    assert channel.events.empty()


async def test_expect_rejects_an_awaited_id_and_a_closed_channel():
    channel = EventChannel()
    channel.expect(1)
    with pytest.raises(ValueError, match="already awaited"):
        channel.expect(1)
    channel.close()
    with pytest.raises(InterfaceError, match="no more outcomes"):
        channel.expect(2)


async def test_callback_handlers_run_in_registration_order_instead_of_queueing():
    channel = EventChannel()
    seen = []
    channel.on("on_commit", lambda e: seen.append(("first", e)))
    remove = channel.on("on_commit", lambda e: seen.append(("second", e)))
    channel.deliver(CallbackEvent("on_commit", 1))
    assert seen == [("first", CallbackEvent("on_commit", 1)), ("second", CallbackEvent("on_commit", 1))]
    assert channel.events.empty()
    remove()
    remove()
    seen.clear()
    channel.deliver(CallbackEvent("on_commit", 2))
    assert [tag for tag, _ in seen] == ["first"]
    channel.deliver(CallbackEvent("on_rollback"))
    assert channel.events.get_nowait() == CallbackEvent("on_rollback")


async def test_the_same_handler_registered_twice_is_removed_separately():
    channel = EventChannel()
    seen = []
    remove_first = channel.on("x", seen.append)
    channel.on("x", seen.append)
    remove_first()
    channel.deliver(CallbackEvent("x"))
    assert seen == [CallbackEvent("x")]


async def test_a_failing_callback_handler_is_logged_and_the_others_run(caplog):
    channel = EventChannel()
    seen = []

    def broken(_event):
        raise RuntimeError("boom")

    channel.on("x", broken)
    channel.on("x", seen.append)
    with caplog.at_level(logging.ERROR, logger="adorm.events"):
        channel.deliver(CallbackEvent("x"))
    assert seen == [CallbackEvent("x")]
    assert "boom" in caplog.text


async def test_handlers_must_be_callable_and_events_must_be_events():
    channel = EventChannel()
    with pytest.raises(TypeError, match="callable"):
        channel.on("x", "not callable")
    with pytest.raises(TypeError, match="takes events"):
        channel.deliver({"event": "result"})


async def test_backend_events_are_queued_for_the_reader():
    channel = EventChannel()
    channel.deliver(BackendEvent("notify", "orders", "1"))
    assert channel.events.get_nowait() == BackendEvent("notify", "orders", "1")


async def test_close_hands_unread_events_to_the_fallback_and_drops_outcomes():
    bus = EventBus()
    seen = []
    bus.subscribe(Event, seen.append)
    channel = EventChannel(bus)
    channel.deliver(BackendEvent("notify", "a"))
    channel.deliver(ResultEvent(1, "unread"))
    channel.deliver(CallbackEvent("cache_cleared"))
    channel.close()
    assert channel.closed
    assert seen == [BackendEvent("notify", "a"), CallbackEvent("cache_cleared")]
    assert channel.events.empty()
    channel.deliver(BackendEvent("notify", "b"))
    channel.deliver(ResultEvent(2, "dropped"))
    assert seen[-1] == BackendEvent("notify", "b")
    assert len(seen) == 3
    assert channel.events.empty()


async def test_a_closed_channel_still_runs_callback_handlers():
    channel = EventChannel(EventBus())
    seen = []
    channel.on("on_rollback", seen.append)
    channel.close()
    channel.deliver(CallbackEvent("on_rollback"))
    assert seen == [CallbackEvent("on_rollback")]


async def test_close_without_a_fallback_discards_unread_events():
    channel = EventChannel()
    channel.deliver(BackendEvent("notify"))
    channel.close()
    channel.deliver(BackendEvent("notify"))
    assert channel.events.empty()


async def test_close_fails_awaited_futures_once():
    channel = EventChannel()
    default, other = channel.expect(1), channel.expect(2)
    other.cancel()
    channel.close()
    with pytest.raises(InterfaceError, match="no outcome for request 1"):
        await default
    channel.close()
    custom = EventChannel()
    future = custom.expect(1)
    custom.close(ConnectionResetError("peer gone"))
    with pytest.raises(ConnectionResetError):
        await future


async def test_decoded_events_are_interpreted_like_local_ones():
    channel = EventChannel()
    future = channel.expect(9)
    channel.deliver(Event.from_dict(json.loads(json.dumps(ResultEvent(9, {"n": 3}).to_dict()))))
    channel.deliver(Event.from_dict(BackendEvent("notify", "c", "p").to_dict()))
    assert await future == {"n": 3}
    assert channel.events.get_nowait() == BackendEvent("notify", "c", "p")
    assert "open" in repr(channel)
