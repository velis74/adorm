"""Events a slot delivers to the client: results, callbacks and backend events.

A driver connection reports callbacks and backend events through the ``emit`` callable it gets when it opens; the
slot forwards them, together with the results of the operations it executes, over its event queue. `EventBus`
dispatches events to the handlers subscribed to them.
"""

from __future__ import annotations

import logging

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

__all__ = [
    "BackendEvent",
    "CallbackEvent",
    "Emit",
    "ErrorEvent",
    "Event",
    "EventBus",
    "ResultEvent",
]

logger = logging.getLogger("adorm.events")


@dataclass(frozen=True, slots=True)
class Event:
    """Base class of every event."""


@dataclass(frozen=True, slots=True)
class ResultEvent(Event):
    """The result of the request ``request_id``: rows as dicts, a scalar or a count."""

    request_id: Any
    value: Any


@dataclass(frozen=True, slots=True)
class ErrorEvent(Event):
    """The request ``request_id`` failed with ``error``, an adorm exception."""

    request_id: Any
    error: BaseException


@dataclass(frozen=True, slots=True)
class CallbackEvent(Event):
    """A point in a connection's life the client runs callbacks for, e.g. ``name="on_commit"``."""

    name: str
    payload: Any = None


@dataclass(frozen=True, slots=True)
class BackendEvent(Event):
    """An event the database or the target ORM raised on its own, e.g. ``name="notify"`` for PostgreSQL NOTIFY.

    ``channel`` is the source within ``name`` (the NOTIFY channel), ``None`` when the event has none.
    """

    name: str
    channel: str | None = None
    payload: Any = None


Emit = Callable[[Event], None]
"""Receives the events a driver connection reports."""

E = TypeVar("E", bound=Event)


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
