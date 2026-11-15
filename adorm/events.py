"""The event channel from a slot to the client: results, cancellation outcomes, callbacks and backend events.

A slot delivers exactly one outcome per request (`ResultEvent`, `ErrorEvent` or `CancelledEvent`), the
`CallbackEvent` values of its transaction ends (`ON_COMMIT`, `ON_ROLLBACK`) and the callbacks and backend events
its driver connection reports through ``emit``. On the client an `EventChannel` interprets each event: an outcome
settles the future of the request that awaits it, a callback runs the handlers registered for its name, and
whatever nobody claims is queued for the reader and handed to an `EventBus` when the channel closes.

Every event has a JSON-safe form (`Event.to_dict()`, `Event.from_dict()`, `encode_error()`, `decode_error()`). A
remote driver uses it to carry a server slot's events to its connection, which returns each outcome from the
connection method that ran the request and emits the server's other callbacks and backend events; the client's own
slot delivers them to its channel like those of any other connection.
"""

from __future__ import annotations

import asyncio
import logging

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, TypeVar

from adorm import exceptions
from adorm.codec import decode_value, encode_value

__all__ = [
    "ON_COMMIT",
    "ON_ROLLBACK",
    "OUTCOME_EVENTS",
    "RESERVED_CALLBACKS",
    "BackendEvent",
    "CallbackEvent",
    "CancelledEvent",
    "Emit",
    "ErrorEvent",
    "Event",
    "EventBus",
    "EventChannel",
    "ResultEvent",
    "decode_error",
    "encode_error",
]

logger = logging.getLogger("adorm.events")

ON_COMMIT = "on_commit"
"""The `CallbackEvent` name a slot delivers when a ``commit`` request succeeds, before the request's outcome."""

ON_ROLLBACK = "on_rollback"
"""The `CallbackEvent` name a slot delivers when a ``rollback`` request succeeds, before the request's outcome, and
when it rolls back an abandoned transaction itself."""

RESERVED_CALLBACKS = frozenset({ON_COMMIT, ON_ROLLBACK})
"""Callback names only a slot delivers; a slot drops a `CallbackEvent` with one of them that a connection emits."""

Encode = Callable[[Any], Any]
Decode = Callable[[Any], Any]


@dataclass(frozen=True, slots=True)
class Event:
    """Base class of every event."""

    kind: ClassVar[str] = ""

    def to_dict(self, encode: Encode = encode_value) -> dict[str, Any]:  # noqa: ARG002 - overridden
        """The event as JSON-safe data, ``{"event": <kind>, ...}``; ``encode`` encodes result values and payloads."""
        raise TypeError(f"{type(self).__name__} has no encoded form")

    @staticmethod
    def from_dict(data: Mapping[str, Any], decode: Decode = decode_value) -> Event:
        """The event `to_dict()` encoded; ``decode`` decodes result values and payloads."""
        if not isinstance(data, Mapping):
            raise ValueError(f"an encoded event is a mapping, not {data!r}")
        cls = _EVENT_TYPES.get(data.get("event"))  # type: ignore[arg-type]
        if cls is None:
            raise ValueError(f"unknown event {data.get('event')!r}; one of {', '.join(sorted(_EVENT_TYPES))}")
        try:
            return cls._decode(data, decode)
        except KeyError as exc:
            raise ValueError(f"an encoded {cls.kind} event needs {exc.args[0]!r}") from None

    @classmethod
    def _decode(cls, data: Mapping[str, Any], decode: Decode) -> Event:
        raise NotImplementedError


def _valid_request_id(request_id: Any) -> bool:
    return isinstance(request_id, (str, int)) and not isinstance(request_id, bool)


def _check_request_id(request_id: Any) -> Any:
    if not _valid_request_id(request_id):
        raise TypeError(f"an encoded request id is a string or an integer, not {request_id!r}")
    return request_id


def _decoded_request_id(data: Mapping[str, Any]) -> Any:
    request_id = data["request_id"]
    if not _valid_request_id(request_id):
        raise ValueError(f"an encoded request id is a string or an integer, not {request_id!r}")
    return request_id


def _decoded_str(data: Mapping[str, Any], key: str, *, optional: bool = False) -> Any:
    value = data[key]
    if not (isinstance(value, str) or (optional and value is None)):
        expected = "a string or null" if optional else "a string"
        raise ValueError(f"{key!r} of an encoded event is {expected}, not {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class ResultEvent(Event):
    """The result of the request ``request_id``: rows as dicts, a scalar or a count."""

    kind: ClassVar[str] = "result"

    request_id: Any
    value: Any

    def to_dict(self, encode: Encode = encode_value) -> dict[str, Any]:
        return {"event": self.kind, "request_id": _check_request_id(self.request_id), "value": encode(self.value)}

    @classmethod
    def _decode(cls, data: Mapping[str, Any], decode: Decode) -> Event:
        return cls(_decoded_request_id(data), decode(data["value"]))


@dataclass(frozen=True, slots=True)
class ErrorEvent(Event):
    """The request ``request_id`` failed with ``error``, an adorm exception."""

    kind: ClassVar[str] = "error"

    request_id: Any
    error: BaseException

    def to_dict(self, encode: Encode = encode_value) -> dict[str, Any]:  # noqa: ARG002 - errors have their own form
        return {"event": self.kind, "request_id": _check_request_id(self.request_id), "error": encode_error(self.error)}

    @classmethod
    def _decode(cls, data: Mapping[str, Any], decode: Decode) -> Event:  # noqa: ARG003
        return cls(_decoded_request_id(data), decode_error(data["error"]))


@dataclass(frozen=True, slots=True)
class CancelledEvent(Event):
    """The request ``request_id`` was cancelled at the client's request.

    ``started`` is ``False`` when the request's call never started: it was still queued, or it was cancelled while
    the slot set its connection options. It is ``True`` when it was cancelled in the connection while running.
    """

    kind: ClassVar[str] = "cancelled"

    request_id: Any
    started: bool

    def to_dict(self, encode: Encode = encode_value) -> dict[str, Any]:  # noqa: ARG002
        return {"event": self.kind, "request_id": _check_request_id(self.request_id), "started": bool(self.started)}

    @classmethod
    def _decode(cls, data: Mapping[str, Any], decode: Decode) -> Event:  # noqa: ARG003
        request_id = _decoded_request_id(data)
        started = data["started"]
        if not isinstance(started, bool):
            raise ValueError(f"'started' of an encoded cancelled event is a boolean, not {started!r}")
        return cls(request_id, started)


@dataclass(frozen=True, slots=True)
class CallbackEvent(Event):
    """A point in a connection's life the client runs callbacks for, e.g. ``name="cache_cleared"``; the slot delivers
    `ON_COMMIT` and `ON_ROLLBACK` itself."""

    kind: ClassVar[str] = "callback"

    name: str
    payload: Any = None

    def to_dict(self, encode: Encode = encode_value) -> dict[str, Any]:
        return {"event": self.kind, "name": self.name, "payload": encode(self.payload)}

    @classmethod
    def _decode(cls, data: Mapping[str, Any], decode: Decode) -> Event:
        return cls(_decoded_str(data, "name"), decode(data["payload"]))


@dataclass(frozen=True, slots=True)
class BackendEvent(Event):
    """An event the database or the target ORM raised on its own, e.g. ``name="notify"`` for PostgreSQL NOTIFY.

    ``channel`` is the source within ``name`` (the NOTIFY channel), ``None`` when the event has none.
    """

    kind: ClassVar[str] = "backend"

    name: str
    channel: str | None = None
    payload: Any = None

    def to_dict(self, encode: Encode = encode_value) -> dict[str, Any]:
        return {"event": self.kind, "name": self.name, "channel": self.channel, "payload": encode(self.payload)}

    @classmethod
    def _decode(cls, data: Mapping[str, Any], decode: Decode) -> Event:
        return cls(_decoded_str(data, "name"), _decoded_str(data, "channel", optional=True), decode(data["payload"]))


_EVENT_TYPES: dict[str, type[Event]] = {
    cls.kind: cls for cls in (ResultEvent, ErrorEvent, CancelledEvent, CallbackEvent, BackendEvent)
}

OUTCOME_EVENTS: tuple[type[Event], ...] = (ResultEvent, ErrorEvent, CancelledEvent)
"""The event types that end a request: exactly one of them carries each request's id."""

Emit = Callable[[Event], None]
"""Receives the events a driver connection reports: `CallbackEvent` and `BackendEvent` values."""

E = TypeVar("E", bound=Event)


# Errors


def _error_classes() -> dict[str, type[BaseException]]:
    classes: dict[str, type[BaseException]] = {}
    for name in exceptions.__all__:
        value = getattr(exceptions, name)
        if isinstance(value, type) and issubclass(value, exceptions.AdormError):
            classes[name] = value
    return classes


def _model_of(cls: type[BaseException]) -> tuple[str, str] | None:
    from adorm.models.registry import registry

    for model in registry:
        for attribute in ("DoesNotExist", "MultipleObjectsReturned"):
            if getattr(model, attribute, None) is cls:
                return model._meta.label, attribute  # type: ignore[union-attr]
    return None


def _session_key(session: Any) -> list[str] | None:
    key = getattr(session, "key", None)
    return list(key) if isinstance(key, tuple) else None


def _session(key: Any) -> Any:
    if key is None:
        return None
    from adorm.executor.sessions import create_session

    return create_session(*key)


def encode_error(error: BaseException) -> dict[str, Any]:
    """``error`` as JSON-safe data: ``{"class": <name>, "message": <text>, ...}``.

    An adorm exception keeps its class (a model's ``DoesNotExist`` and ``MultipleObjectsReturned`` also its model
    label), ``sqlstate``, ``timeout``, ``phase``, the sessions it names and the messages of a `ValidationError`. The
    driver's ``original`` exception, the objects of a `ProtectedError` or `RestrictedError` and an operation it names
    stay behind. Any other exception becomes a
    `DatabaseError` whose message starts with the exception's class name.
    """
    cls = type(error)
    classes = _error_classes()
    if classes.get(cls.__name__) is not cls:
        model = _model_of(cls)
        if model is not None:
            return {"class": model[1], "model": model[0], "message": str(error)}
        if not isinstance(error, exceptions.AdormError) or not any(base in classes.values() for base in cls.__mro__):
            return {"class": "DatabaseError", "message": f"{cls.__name__}: {error}"}
        cls = next(base for base in cls.__mro__ if classes.get(base.__name__) is base)
    data: dict[str, Any] = {"class": cls.__name__, "message": str(error)}
    if isinstance(error, exceptions.ValidationError):
        data["message"] = error.message_dict if hasattr(error, "error_dict") else error.messages
    if isinstance(error, exceptions.Error) and error.sqlstate is not None:
        data["sqlstate"] = error.sqlstate
    if isinstance(error, (exceptions.SessionTimeout, exceptions.OperationTimeout)) and error.timeout is not None:
        data["timeout"] = error.timeout
    if (
        isinstance(error, (exceptions.SessionTimeout, exceptions.OperationTimeout))
        and _session_key(error.session) is not None
    ):
        data["session"] = _session_key(error.session)
    if isinstance(error, exceptions.DeadlockError):
        data["held_sessions"] = [k for k in map(_session_key, error.held_sessions) if k is not None]
    if isinstance(error, exceptions.OperationTimeout) and error.phase is not None:
        data["phase"] = error.phase
    return data


def decode_error(data: Mapping[str, Any]) -> BaseException:
    """The exception `encode_error` encoded. An unknown class decodes as `DatabaseError`."""
    if not isinstance(data, Mapping) or not isinstance(data.get("class"), str):
        raise ValueError(f"an encoded error is a mapping with 'class', not {data!r}")
    name, message = data["class"], data.get("message")
    if "model" in data:
        from adorm.models.registry import registry

        try:
            cls: type[BaseException] = getattr(registry.get(data["model"]), name)
        except (LookupError, AttributeError):
            cls = getattr(exceptions, "ObjectDoesNotExist" if name == "DoesNotExist" else "MultipleObjectsReturned")
        return cls(message)
    cls = _error_classes().get(name, exceptions.DatabaseError)
    if issubclass(cls, exceptions.ValidationError):
        return cls(message)
    if issubclass(cls, exceptions.Error):
        return cls(message, sqlstate=data.get("sqlstate"))
    if issubclass(cls, exceptions.DeadlockError):
        held = tuple(_session(k) for k in data.get("held_sessions", ()))
        return cls(message, held_sessions=held, session=_session(data.get("session")), timeout=data.get("timeout"))
    if issubclass(cls, exceptions.SessionTimeout):
        return cls(message, session=_session(data.get("session")), timeout=data.get("timeout"))
    if issubclass(cls, exceptions.OperationTimeout):
        return cls(message, phase=data.get("phase"), session=_session(data.get("session")), timeout=data.get("timeout"))
    return cls(message)


# Dispatch


@dataclass(slots=True, eq=False)
class _Subscription:
    event_type: type[Event]
    name: str | None
    channel: str | None
    handler: Callable[[Any], None]
    active: bool = True

    def matches(self, event: Event) -> bool:
        return (
            self.active
            and isinstance(event, self.event_type)
            and (self.name is None or getattr(event, "name", None) == self.name)
            and (self.channel is None or getattr(event, "channel", None) == self.channel)
        )


class EventBus:
    """Dispatches events to subscribed handlers.

    Handlers run synchronously in subscription order. A handler that raises is logged and the others still run. A
    handler subscribed during a dispatch first runs for the next event; one unsubscribed during a dispatch no
    longer runs for the current one.
    """

    def __init__(self) -> None:
        self._subscriptions: list[_Subscription] = []

    def subscribe(
        self,
        event_type: type[E],
        handler: Callable[[E], None],
        *,
        name: str | None = None,
        channel: str | None = None,
    ) -> Callable[[], None]:
        """Call ``handler`` for every dispatched instance of ``event_type``; return a function that unsubscribes.

        ``name`` and ``channel``, when given, must equal the event's attributes of the same name.
        """
        entry = _Subscription(event_type, name, channel, handler)
        self._subscriptions.append(entry)

        def unsubscribe() -> None:
            entry.active = False
            self._subscriptions = [s for s in self._subscriptions if s is not entry]

        return unsubscribe

    def dispatch(self, event: Event) -> int:
        """Call every handler subscribed to ``event``; return how many were called."""
        called = 0
        for entry in tuple(self._subscriptions):
            if not entry.matches(event):
                continue
            called += 1
            try:
                entry.handler(event)
            except Exception:
                logger.exception("event handler %r failed for %r", entry.handler, event)
        return called


# Cancel messages of a request future settled by a CancelledEvent.
CANCELLED_BEFORE_START = "the request was cancelled before it started"
_CANCELLED_RUNNING = "the request was cancelled while it ran"


def _settle(future: asyncio.Future, event: Event) -> None:
    if future.done():
        return
    if isinstance(event, ResultEvent):
        future.set_result(event.value)
    elif isinstance(event, ErrorEvent):
        future.set_exception(event.error)
    else:
        future.cancel(_CANCELLED_RUNNING if event.started else CANCELLED_BEFORE_START)  # type: ignore[attr-defined]


class EventChannel:
    """The client end of a slot's event queue. `deliver()` interprets each event:

    - an outcome (`OUTCOME_EVENTS`) of a request awaited with `expect()` settles its future: a `ResultEvent` sets
      the result, an `ErrorEvent` the exception, a `CancelledEvent` cancels it;
    - a `CallbackEvent` with handlers registered by `on()` for its name runs them, in registration order;
    - any other event is put on ``events`` for the reader.

    `close()` hands the callbacks and backend events still on ``events`` to ``fallback`` and fails the futures still
    awaiting an outcome; afterwards outcomes nobody awaits are dropped and unclaimed events go to ``fallback``
    directly. A handler that raises is logged and the others still run.
    """

    __slots__ = ("_callbacks", "_closed", "_pending", "events", "fallback")

    def __init__(self, fallback: EventBus | None = None) -> None:
        self.events: asyncio.Queue[Event] = asyncio.Queue()
        self.fallback = fallback
        self._pending: dict[Any, asyncio.Future] = {}
        self._callbacks: dict[str, list[Callable[[CallbackEvent], None]]] = {}
        self._closed = False

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        return f"<EventChannel {state}, {len(self._pending)} awaited, {self.events.qsize()} unread>"

    @property
    def closed(self) -> bool:
        """Whether `close()` was called."""
        return self._closed

    @property
    def awaited(self) -> int:
        """How many requests `expect()` awaits an outcome for."""
        return len(self._pending)

    def expect(self, request_id: Any) -> asyncio.Future:
        """A future the outcome of ``request_id`` settles. Raises `ValueError` when the id is already awaited and
        `InterfaceError` after `close()`."""
        if self._closed:
            raise exceptions.InterfaceError(f"{self!r} awaits no more outcomes")
        if request_id in self._pending:
            raise ValueError(f"request {request_id!r} is already awaited")
        future = self._pending[request_id] = asyncio.get_running_loop().create_future()
        return future

    def forget(self, request_id: Any) -> None:
        """Stop awaiting ``request_id``: its outcome, when it arrives, is treated as unawaited."""
        self._pending.pop(request_id, None)

    def on(self, name: str, handler: Callable[[CallbackEvent], None]) -> Callable[[], None]:
        """Run ``handler(event)`` for every `CallbackEvent` named ``name``, instead of queueing it; return a function
        that removes the handler."""
        if not callable(handler):
            raise TypeError(f"a callback handler is callable, not {handler!r}")

        def call(event: CallbackEvent) -> None:  # a distinct object per registration
            handler(event)

        self._callbacks.setdefault(name, []).append(call)

        def remove() -> None:
            current = self._callbacks.get(name, [])
            self._callbacks[name] = [h for h in current if h is not call]
            if not self._callbacks[name]:
                del self._callbacks[name]

        return remove

    def deliver(self, event: Event) -> None:
        """Interpret ``event``."""
        if not isinstance(event, Event):
            raise TypeError(f"a channel takes events, not {type(event).__name__}")
        if isinstance(event, OUTCOME_EVENTS):
            future = self._pending.pop(event.request_id, None)  # type: ignore[attr-defined]
            if future is not None:
                _settle(future, event)
            elif not self._closed:
                self.events.put_nowait(event)
            return
        if isinstance(event, CallbackEvent):
            handlers = tuple(self._callbacks.get(event.name, ()))
            if handlers:
                for handler in handlers:
                    try:
                        handler(event)
                    except Exception:
                        logger.exception("callback handler %r failed for %r", handler, event)
                return
        if self._closed:
            self._hand_over(event)
        else:
            self.events.put_nowait(event)

    def close(self, error: BaseException | None = None) -> None:
        """Hand unread callbacks and backend events to ``fallback``, drop unread outcomes and fail the awaited
        futures with ``error`` (default `InterfaceError`). A second call does nothing."""
        if self._closed:
            return
        self._closed = True
        while not self.events.empty():
            event = self.events.get_nowait()
            if not isinstance(event, OUTCOME_EVENTS):
                self._hand_over(event)
        pending, self._pending = self._pending, {}
        for request_id, future in pending.items():
            if not future.done():
                future.set_exception(
                    error if error is not None else exceptions.InterfaceError(f"no outcome for request {request_id!r}")
                )
                future.exception()  # an abandoned future must not log "exception was never retrieved"

    def _hand_over(self, event: Event) -> None:
        if self.fallback is not None:
            self.fallback.dispatch(event)
