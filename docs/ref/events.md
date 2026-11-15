# Events

Event types, their encoded form, the client end of a slot's event queue and event dispatch, importable from
`adorm.events`. The [events topic](/topics/events) describes the path from a slot to the client and how adorm
interprets each event.

```python
from adorm.events import BackendEvent, CallbackEvent, EventBus, EventChannel
```

## Event types

Events are frozen dataclasses and compare by value. Each class has a `kind`, the value of `"event"` in its encoded
form.

### `adorm.events.Event`

Base class of every event.

#### `to_dict(encode=encode_value)`

The event as JSON-safe data, `{"event": <kind>, ...}`. `encode` encodes result values and payloads; the default is
[`adorm.codec.encode_value`](/ref/encoding#adorm-codec-encode-value-value-hint-none), which infers each value's
type. Request ids are strings or integers; any other id raises `TypeError`. The base class has no encoded form
and raises `TypeError`.

#### `from_dict(data, decode=decode_value)`

A static method: the event `to_dict()` encoded. `decode` decodes result values and payloads. Data that is not a
mapping, an unknown `"event"`, a missing field and a field of the wrong type (a request id that is not a string or
an integer, a `name` that is not a string, a `channel` that is neither a string nor `null`, a `started` that is not a
boolean) raise `ValueError`.

```python
event = ResultEvent(12, [{"id": 1, "name": "Ana"}])
data = event.to_dict()
# {"event": "result", "request_id": 12,
#  "value": {"type": "list", "value": [{"type": "dict", "value": {"id": {"type": "int", "value": 1}, ...}}]}}
assert Event.from_dict(json.loads(json.dumps(data))) == event
```

### `adorm.events.ResultEvent(request_id, value)`

The result of the request `request_id`: rows as dicts, a scalar or a count. Kind `"result"`.

### `adorm.events.ErrorEvent(request_id, error)`

The request `request_id` failed with `error`, an adorm exception. Kind `"error"`; the error is encoded with
[`encode_error()`](#adorm-events-encode-error-error).

### `adorm.events.CancelledEvent(request_id, started)`

The request `request_id` was cancelled at the client's request. `started` is `False` when the request was still
queued and never reached the driver connection, `True` when it was cancelled in the connection while it ran. Kind
`"cancelled"`.

### `adorm.events.CallbackEvent(name, payload=None)`

A point in a connection's life the client runs callbacks for. Kind `"callback"`. The slot itself delivers the
names in [`RESERVED_CALLBACKS`](#adorm-events-reserved-callbacks); a driver connection emits other names.

```python
# inside a driver connection, after the target cleared a cache the client keeps a copy of
self.emit(CallbackEvent("cache_cleared", {"keys": ["club:1"]}))
```

### `adorm.events.BackendEvent(name, channel=None, payload=None)`

An event the database or the target raised on its own. `name` is the kind of event (`"notify"` for PostgreSQL
`NOTIFY`), `channel` its source within that kind (the `NOTIFY` channel) and `payload` its data. A driver lists the
names it emits in `Capabilities.backend_events`. Kind `"backend"`.

```python
# inside a driver connection, when PostgreSQL delivers NOTIFY orders, '42'
self.emit(BackendEvent("notify", "orders", "42"))
```

### `adorm.events.OUTCOME_EVENTS`

`(ResultEvent, ErrorEvent, CancelledEvent)`: the event types that end a request. A slot delivers exactly one of
them for every request it accepted.

### `adorm.events.ON_COMMIT`

`"on_commit"`: the `CallbackEvent` name a slot delivers when a `commit` request succeeds, before the request's
outcome.

### `adorm.events.ON_ROLLBACK`

`"on_rollback"`: the `CallbackEvent` name a slot delivers when a `rollback` request succeeds, before the request's
outcome, and when it rolls back a transaction whose client sent no request within the pool's `commit_timeout`.

### `adorm.events.RESERVED_CALLBACKS`

`frozenset({ON_COMMIT, ON_ROLLBACK})`. A slot drops a `CallbackEvent` with one of these names that its driver
connection emits, and logs an error on the `adorm.executor` logger.

### `adorm.events.Emit`

The type of the `emit` callable: `Callable[[Event], None]`. A slot accepts only `CallbackEvent` and `BackendEvent`
through it and drops any other event with an error on the `adorm.executor` logger.

## Encoded errors

### `adorm.events.encode_error(error)`

`error` as JSON-safe data, `{"class": <name>, "message": <text>, ...}`:

| Exception | Encoded |
|---|---|
| a class of [`adorm.exceptions`](/ref/exceptions) | its class name and message |
| a subclass defined elsewhere | the nearest class of `adorm.exceptions` it derives from |
| a model's `DoesNotExist` or `MultipleObjectsReturned` | `"class"` plus `"model"`, the model's label |
| `Error` and its subclasses | also `sqlstate` |
| `SessionTimeout`, `DeadlockError` | also `timeout` and the sessions as `(using, session_name)` pairs |
| `OperationTimeout` | also `phase` and `timeout` |
| `ValidationError` | the messages by field, or the list of messages |
| any other exception | `DatabaseError`, with the message `"<class name>: <message>"` |

The driver's `original` exception and the operation an `OperationTimeout` names are not encoded.

### `adorm.events.decode_error(data)`

The exception `encode_error()` encoded. Sessions are restored with `create_session()`; a model label that is not
registered gives the generic `ObjectDoesNotExist` or `MultipleObjectsReturned`; an unknown class gives
`DatabaseError`. Data without a string `"class"` raises `ValueError`.

```python
data = encode_error(IntegrityError("duplicate key", sqlstate="23505"))
# {"class": "IntegrityError", "message": "duplicate key", "sqlstate": "23505"}
error = decode_error(data)          # IntegrityError("duplicate key"), error.sqlstate == "23505"
```

## The client end

### `adorm.events.EventChannel(fallback=None)`

The client end of a slot's event queue. Each [`SlotLease`](/ref/pool#adorm-executor-slotlease) has one as
`channel`, with the pool's `events` bus as `fallback`. `deliver()` interprets each event:

| Event | Interpretation |
|---|---|
| an outcome of a request awaited with `expect()` | settles its future: `ResultEvent` sets the result, `ErrorEvent` the exception, `CancelledEvent` cancels it |
| an outcome nobody awaits | put on `events`; dropped after `close()` |
| a `CallbackEvent` with handlers registered by `on()` for its name | runs the handlers, in registration order |
| any other `CallbackEvent`, a `BackendEvent` | put on `events`; dispatched to `fallback` after `close()` |

A handler that raises is logged on the `adorm.events` logger and the others still run.

| Member | Meaning |
|---|---|
| `events` | an `asyncio.Queue` of the events nobody claimed, in delivery order |
| `fallback` | the `EventBus` that receives unclaimed callbacks and backend events once the channel closes |
| `closed` | whether `close()` was called |
| `awaited` | how many requests `expect()` awaits an outcome for |

#### `expect(request_id)`

Returns an `asyncio.Future` that the outcome of `request_id` settles. An id that is already awaited raises
`ValueError`; a closed channel raises `InterfaceError`.

#### `forget(request_id)`

Stops awaiting `request_id`; its outcome, when it arrives, is treated as one nobody awaits.

#### `on(name, handler)`

Runs `handler(event)` for every `CallbackEvent` named `name` instead of queueing it. Returns a function that removes
this registration; calling it again does nothing. A handler that is not callable raises `TypeError`.

#### `deliver(event)`

Interprets `event`. Anything but an `Event` raises `TypeError`.

#### `close(error=None)`

Dispatches the callbacks and backend events still on `events` to `fallback`, drops the outcomes still there, and
fails the futures still awaiting an outcome with `error`, by default `InterfaceError`. A second call does nothing.

```python
channel = EventChannel(fallback=pool.events)
future = channel.expect(request.request_id)
channel.on("cache_cleared", lambda event: cache.drop(event.payload["keys"]))

channel.deliver(CallbackEvent("cache_cleared", {"keys": ["club:1"]}))    # runs the handler
channel.deliver(ResultEvent(request.request_id, 3))
assert await future == 3
channel.close()
```

## Dispatch

### `adorm.events.EventBus`

Dispatches events to subscribed handlers. Handlers run synchronously, in subscription order. A handler that raises
is logged on the `adorm.events` logger and the remaining handlers still run. A handler subscribed during a
dispatch first runs for the next event; one unsubscribed during a dispatch no longer runs for the current one.
Each pool has one as `events`.

#### `subscribe(event_type, handler, *, name=None, channel=None)`

Calls `handler(event)` for every dispatched event that is an instance of `event_type` and, when given, whose
`name` and `channel` attributes equal `name` and `channel`. Returns a function that unsubscribes; calling it
again does nothing.

#### `dispatch(event)`

Calls every matching handler and returns how many were called.

```python
bus = EventBus()

unsubscribe = bus.subscribe(BackendEvent, lambda e: print("order", e.payload), name="notify", channel="orders")
bus.subscribe(CallbackEvent, lambda e: print("cache cleared"), name="cache_cleared")

bus.dispatch(BackendEvent("notify", "orders", "42"))   # prints "order 42", returns 1
bus.dispatch(CallbackEvent("cache_cleared"))           # prints "cache cleared", returns 1
unsubscribe()
```
