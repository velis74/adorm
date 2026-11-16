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
[`encode_error()`](#adorm-events-encode-error-error-encode-encode-value), which encodes validation params with the
event's `encode`.

### `adorm.events.CancelledEvent(request_id, started)`

The request `request_id` was cancelled at the client's request. `started` is `False` when the request's call never
started: it was still queued, or it was cancelled before the slot handed it to the connection, as while the slot set its
connection options. It is `True` when it was
cancelled in the connection while it ran, so that whether it took effect is unknown. Kind `"cancelled"`.

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

### `adorm.events.NOTIFY`

`"notify"`: the `BackendEvent` name of a PostgreSQL notification (`NOTIFY`, `pg_notify()`) on a channel the
connection listens on ([`adorm.listen()`](#adorm-listen-channels-session-none)). `channel` is the notification's
channel, exactly as sent; `payload` its payload string, `""` when it has none.

```python
BackendEvent("notify", "orders", "42")      # SELECT pg_notify('orders', '42')
BackendEvent("notify", "Orders", "")        # NOTIFY "Orders"
```

### `adorm.events.NOTICE`

`"notice"`: the `BackendEvent` name of a message the server sends with a statement's result, such as PL/pgSQL's
`RAISE NOTICE` or a `WARNING`. `channel` is its untranslated severity (`"NOTICE"`, `"WARNING"`, `"INFO"`, `"DEBUG"`,
`"LOG"`); `payload` a dict with `severity`, `sqlstate`, `message`, `detail` and `hint`, `None` for a field the server
did not send. The server's `client_min_messages` decides which severities it sends (`NOTICE` and above by default).
It arrives before the outcome of the request whose statement caused it.

```python
# RAISE WARNING 'check the clubs' USING ERRCODE = '01000';
BackendEvent("notice", "WARNING", {"severity": "WARNING", "sqlstate": "01000", "message": "check the clubs",
                                   "detail": None, "hint": None})
```

### `adorm.events.CONNECTION_LOST`

`"connection_lost"`: the `BackendEvent` name a connection emits when its database connection ends without adorm
closing it, as when the server terminates the session; at most once per connection. A driver that declares it notices
the end at least while the connection listens and no call runs; the native PostgreSQL driver notices it at any time,
the Django driver only while the connection listens. `channel` is `None`, `payload` the reason as text. The connection is no longer usable; a [`Listener`](#adorm-listener) raises
`InterfaceError` once it reaches the event.

```python
BackendEvent("connection_lost", None, "the server connection ended")
```

### `adorm.events.OUTCOME_EVENTS`

`(ResultEvent, ErrorEvent, CancelledEvent)`: the event types that end a request. A slot delivers exactly one of
them for every request it accepted.

### `adorm.events.ON_COMMIT`

`"on_commit"`: the `CallbackEvent` name a slot delivers when a `commit` request succeeds, before the request's
outcome.

### `adorm.events.ON_ROLLBACK`

`"on_rollback"`: the `CallbackEvent` name a slot delivers when a `rollback` request succeeds, before the request's
outcome, and when it rolls back a transaction whose client sent no request within the lease's
[`commit_timeout`](/ref/pool#adorm-executor-slotlease).

### `adorm.events.RESERVED_CALLBACKS`

`frozenset({ON_COMMIT, ON_ROLLBACK})`. A slot drops a `CallbackEvent` with one of these names that its driver
connection emits, and logs an error on the `adorm.executor` logger.

### `adorm.events.Emit`

The type of the `emit` callable: `Callable[[Event], None]`. A slot accepts only `CallbackEvent` and `BackendEvent`
through it and drops any other event with an error on the `adorm.executor` logger.

## Encoded errors

### `adorm.events.encode_error(error, encode=encode_value)`

`error` as JSON-safe data, `{"class": <name>, "message": <text>, ...}`:

| Exception | Encoded |
|---|---|
| a class of [`adorm.exceptions`](/ref/exceptions) | its class name and message |
| a subclass defined elsewhere | the nearest class of `adorm.exceptions` it derives from |
| a model's `DoesNotExist` or `MultipleObjectsReturned` | `"class"` plus `"model"`, the model's label |
| `Error` and its subclasses | also `sqlstate` |
| `IntegrityError` and its subclasses | also `constraint_name`, `table_name`, `schema_name`, `detail` and `model` when set, and `columns`, `values` and `fields` as lists when not empty |
| `SessionTimeout`, `DeadlockError` | also `timeout` and the sessions as `(using, session_name)` pairs |
| `OperationTimeout` | also `phase`, `timeout` and the session as a `(using, session_name)` pair |
| `ValidationError` | the messages by field, or the list of messages; each error's `message`, `code` and `params` under `"errors"` (by field, or a list), or under `"error"` for a single error; `encode` encodes each param, and a param it cannot encode becomes its text |
| any other exception | `DatabaseError`, with the message `"<class name>: <message>"` |

The driver's `original` exception, the objects of a `ProtectedError` or `RestrictedError` and the operation an
`OperationTimeout` names are not encoded.

### `adorm.events.decode_error(data, decode=decode_value)`

The exception `encode_error()` encoded; `decode` decodes validation params. Sessions are restored with
`create_session()`; a model label that is not registered gives the generic `ObjectDoesNotExist` or
`MultipleObjectsReturned`; an unknown class gives `DatabaseError`. Data without a string `"class"`, and a validation
error entry without a string `message`, with a `code` that is not a string or with `params` that are not a mapping,
raise `ValueError`, and so does an `IntegrityError` whose `constraint_name`, `table_name`, `schema_name`, `detail` or
`model` is not a string or whose `columns`, `values` or `fields` are not a list of strings.

```python
data = encode_error(IntegrityError("duplicate key", sqlstate="23505"))
# {"class": "IntegrityError", "message": "duplicate key", "sqlstate": "23505"}
error = decode_error(data)          # IntegrityError("duplicate key"), error.sqlstate == "23505"

data = encode_error(map_django_error(unique_violation))
# {"class": "IntegrityError", "message": "duplicate key value violates unique constraint \"clubs_member_email_key\"…",
#  "sqlstate": "23505", "constraint_name": "clubs_member_email_key", "table_name": "clubs_member",
#  "schema_name": "public", "detail": "Key (email)=(ana@example.com) already exists.", "model": "clubs.Member",
#  "columns": ["email"], "values": ["ana@example.com"], "fields": ["email"]}
decode_error(data).key              # {'email': 'ana@example.com'}

data = encode_error(ValidationError({"name": [ValidationError("Taken.", "unique", {"unique_check": ("name",)})]}))
# {"class": "ValidationError", "message": {"name": ["Taken."]},
#  "errors": {"name": [{"message": "Taken.", "code": "unique",
#                       "params": {"unique_check": {"type": "tuple", "value": [{"type": "str", "value": "name"}]}}}]}}
decode_error(data).error_dict["name"][0].code     # 'unique'

data = encode_error(ValidationError("“%(value)s” is not a valid UUID.", "invalid", {"value": "x"}))
# {"class": "ValidationError", "message": ["“x” is not a valid UUID."],
#  "error": {"message": "“%(value)s” is not a valid UUID.", "code": "invalid",
#            "params": {"value": {"type": "str", "value": "x"}}}}
decode_error(data).code                           # 'invalid'
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

## Listening for notifications

### `adorm.listen(*channels, session=None)`

A [`Listener`](#adorm-listener) for `channels` on `session`, the current session by default. Each channel name is
checked with [`check_channel()`](/ref/drivers#adorm-drivers-check-channel-channel) at once, and against the driver's
[`max_channel_length`](/ref/drivers#check-channel-channel) before a slot is taken.

```python
async with adorm.listen("orders", "invoices") as listener:
    async for event in listener:
        print(event.channel, event.payload)        # orders 42
```

### `adorm.Listener`

`Listener(*channels, session=None)`

Backend notifications (PostgreSQL's `LISTEN` / `NOTIFY`) received on a slot the `async with` block holds.

Entering acquires a slot for `session` from the [`TransactionRunner`](/ref/sessions#adorm-executor-transactionrunner),
as [`atomic()`](/ref/transactions#adorm-atomic) does: the acquisition timeout raises `SessionTimeout`, a flow holding
another slot `DeadlockError`. A driver that cannot listen ([`Driver.check_listen()`](/ref/drivers#async-check-listen-profile):
`notify` missing from its [`backend_events`](/ref/drivers#adorm-drivers-capabilities), Django's `TestCase`) raises
`NotSupportedError` before a slot is taken. The
connection then listens on each channel; a failure or cancellation there releases the slot and raises. While the
block runs, its flow holds the slot, so an operation of that flow that needs another slot of a full pool raises
`DeadlockError`. Leaving the block releases the slot; the slot closes its connection while it still listens on a
channel ([`CloseReason.LISTENING`](/ref/pool#adorm-executor-closereason)) and returns it to the pool otherwise. Only the
task that entered the block may leave it. A listener is entered once.

| Member | Does |
|---|---|
| `async get()` | the next notification, `BackendEvent("notify", channel, payload)`, waiting for it. Notifications arrive in the order the connection received them; callbacks and other backend events of the slot go to the pool's [`EventBus`](#adorm-events-eventbus). A cancelled wait loses no notification. Once the connection is lost ([`CONNECTION_LOST`](#adorm-events-connection-lost)), the slot closes or its pool closes, the notifications received before are returned first, then it raises `InterfaceError` in every waiting call and every later one. Several tasks may wait at once; each notification goes to one of them |
| `async for event in listener` | `get()` in a loop; ends with the `InterfaceError` of a lost connection |
| `async listen(*channels)` | listens on `channels` too; a channel already listened on is skipped |
| `async unlisten(*channels)` | stops listening on `channels`; a channel not listened on is skipped. Notifications already received stay queued |
| `channels` | the channels the connection listens on ([`DriverConnection.listening`](/ref/drivers#adorm-drivers-driverconnection)), in the order they were added; a channel of a cancelled `listen()` counts while the connection counts it |
| `lease` | the [`SlotLease`](/ref/pool#adorm-executor-slotlease) of the held slot; `None` outside the block |
| `session` | the session the slot is acquired for |
| `async close()` | releases the slot, as leaving the block does; a `get()` another task waits in raises `InterfaceError`. A second call does nothing |

`get()`, `listen()` and `unlisten()` outside the block raise `InterfaceError`.

```python
async def watch_orders():
    async with adorm.listen("orders") as listener:
        await listener.listen("invoices")
        while True:
            try:
                event = await listener.get()
            except adorm.InterfaceError:
                return                          # connection lost: the caller listens again
            if event.channel == "orders":
                await refresh_order(int(event.payload))
```
