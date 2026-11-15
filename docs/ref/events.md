# Events

Event types and their dispatch, importable from `adorm.events`. A driver connection reports callbacks and backend
events through the `emit` callable it receives when it opens; a slot forwards them, with the outcome of each
request, over its event queue. See [Events](/topics/drivers#events) in the drivers topic.

```python
from adorm.events import BackendEvent, CallbackEvent, EventBus
```

## Event types

Events are frozen dataclasses and compare by value.

### `adorm.events.Event`

Base class of every event.

### `adorm.events.ResultEvent(request_id, value)`

The result of the request `request_id`: rows as dicts, a scalar or a count.

### `adorm.events.ErrorEvent(request_id, error)`

The request `request_id` failed with `error`, an adorm exception.

### `adorm.events.CallbackEvent(name, payload=None)`

A point in a connection's life the client runs callbacks for, such as `name="on_commit"`.

### `adorm.events.BackendEvent(name, channel=None, payload=None)`

An event the database or the target raised on its own. `name` is the kind of event (`"notify"` for PostgreSQL
`NOTIFY`), `channel` its source within that kind (the `NOTIFY` channel) and `payload` its data. A driver lists the
names it emits in `Capabilities.backend_events`.

```python
# inside a driver connection, when PostgreSQL delivers NOTIFY orders, '42'
self.emit(BackendEvent("notify", "orders", "42"))
```

### `adorm.events.Emit`

The type of the `emit` callable: `Callable[[Event], None]`.

## Dispatch

### `adorm.events.EventBus`

Dispatches events to subscribed handlers. Handlers run synchronously, in subscription order. A handler that raises
is logged on the `adorm.events` logger and the remaining handlers still run. A handler subscribed during a
dispatch first runs for the next event; one unsubscribed during a dispatch no longer runs for the current one.

#### `subscribe(event_type, handler, *, name=None, channel=None)`

Calls `handler(event)` for every dispatched event that is an instance of `event_type` and, when given, whose
`name` and `channel` attributes equal `name` and `channel`. Returns a function that unsubscribes; calling it
again does nothing.

#### `dispatch(event)`

Calls every matching handler and returns how many were called.

```python
bus = EventBus()

unsubscribe = bus.subscribe(BackendEvent, lambda e: print("order", e.payload), name="notify", channel="orders")
bus.subscribe(CallbackEvent, lambda e: print("committed"), name="on_commit")

bus.dispatch(BackendEvent("notify", "orders", "42"))   # prints "order 42", returns 1
bus.dispatch(CallbackEvent("on_commit"))               # prints "committed", returns 1
unsubscribe()
```
