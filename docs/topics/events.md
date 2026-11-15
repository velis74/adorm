# Events

::: info Status
The installed version provides the event types, their encoded form, the client's event channel on every slot lease,
the slot's own callbacks and `on_commit` callbacks of [transactions](/topics/transactions). The remote driver that
carries encoded events between processes is not part of it yet.
:::

A slot answers the client with events. Every request it runs ends with one outcome event; a commit or rollback adds
a callback event; the driver connection adds callbacks and events the database raised on its own. On the client
an **event channel** interprets each event: it returns a result to the code that awaits it, runs a callback, or
calls the functions subscribed to it.

```text
client                        slot                             DriverConnection
  ── Request ──▶ request queue ──▶ runs requests in order ───▶ execute(), commit() …
                                     │                               │
  ◀── EventChannel ◀── event queue ◀─┤ one outcome per request       │
                                     │ ON_COMMIT, ON_ROLLBACK        │
                                     └ emitted events ◀───────────── emit(CallbackEvent | BackendEvent)

EventChannel
  ├── outcome of an awaited request ──▶ the awaiting call returns, raises or ends cancelled
  ├── callback with handlers ─────────▶ the handlers run (on_commit callbacks of a transaction)
  └── anything else ──────────────────▶ lease.events; the pool's EventBus once the lease ends
```

## The events

| Event | Delivered by | When |
|---|---|---|
| [`ResultEvent(request_id, value)`](/ref/events#adorm-events-resultevent-request-id-value) | the slot | a request returned: rows, a scalar, a count |
| [`ErrorEvent(request_id, error)`](/ref/events#adorm-events-errorevent-request-id-error) | the slot | a request raised an adorm exception |
| [`CancelledEvent(request_id, started)`](/ref/events#adorm-events-cancelledevent-request-id-started) | the slot | a request was cancelled at the client's request |
| [`CallbackEvent("on_commit")`](/ref/events#adorm-events-on-commit) | the slot | a `commit` request succeeded, before its outcome |
| [`CallbackEvent("on_rollback")`](/ref/events#adorm-events-on-rollback) | the slot | a `rollback` request succeeded, before its outcome; the slot rolled back an abandoned transaction |
| [`CallbackEvent(name, payload)`](/ref/events#adorm-events-callbackevent-name-payload-none) | the driver connection | a point in the connection's life the client runs callbacks for |
| [`BackendEvent(name, channel, payload)`](/ref/events#adorm-events-backendevent-name-channel-none-payload-none) | the driver connection | the database or the target raised something on its own, such as PostgreSQL `NOTIFY` |

Exactly one of `ResultEvent`, `ErrorEvent` and `CancelledEvent` ends each request the slot accepted. Results and
errors arrive in the order of the requests; the `CancelledEvent` of a request that was still queued arrives as soon
as it is cancelled, ahead of the outcomes of earlier requests. An event a connection emits while a request runs
arrives before that request's outcome.

Signals of the target (Django's `post_save`, SQLAlchemy's ORM events) and the target's own `on_commit` hooks run on
the driver side; the client does not see them. A driver reports an effect the client must know about as a
`CallbackEvent` or a `BackendEvent` of its own. A connection emits nothing else: the slot drops and logs outcome
events and the names `on_commit` and `on_rollback` when a connection emits them, because only the slot delivers
those.

## How adorm interprets an event

**Return a result.** `lease.call()` and every operation that a session or a transaction runs await their
request's outcome on the channel: a `ResultEvent` returns the value, an `ErrorEvent` raises the error, a
`CancelledEvent` ends the call cancelled. A `CancelledEvent` for a call whose task was not cancelled raises
`InterfaceError`. Code that writes to the request queue directly reads the outcomes from
`lease.events` instead.

```python
async with await router.acquire(session) as lease:
    rows = await lease.call("execute", operation)                 # ResultEvent → return value

    lease.requests.put_nowait(request := Request("execute", (operation,)))
    match await lease.events.get():
        case ResultEvent(request_id, value): ...
        case ErrorEvent(request_id, error): ...
        case CancelledEvent(request_id, started): ...
```

**Run a callback.** A handler registered on the channel with `on(name, handler)` runs for every `CallbackEvent` of
that name, instead of the event being queued. An `atomic()` block registers one for `on_commit` and runs its
[`on_commit` callbacks](/topics/transactions#running-code-after-the-commit) when the slot reports the commit.

```python
async with adorm.atomic() as tx:
    await Member.objects.create(name="Ana")
    tx.on_commit(lambda: mailer.send_welcome("Ana"))     # runs when CallbackEvent("on_commit") arrives

async with await router.acquire(session) as lease:
    lease.channel.on("cache_cleared", lambda event: cache.drop(event.payload["keys"]))
```

**Call another function.** Each pool has an [`EventBus`](/ref/events#adorm-events-eventbus) as `events`. It receives
the callbacks and backend events nobody claimed: those a connection emits while its slot is idle, and those still
unread on `lease.events` when the lease is released. Subscribe by type, name and channel:

```python
pool = await router.get_pool("default")
unsubscribe = pool.events.subscribe(
    BackendEvent, lambda event: orders.refresh(event.payload), name="notify", channel="orders"
)
```

## Cancellation outcomes

A cancelled request ends with a `CancelledEvent`. `started=False` means the request was still queued and never
reached the database; `started=True` means it was cancelled in the driver connection while it ran, so part of its
work may have reached the database and an open transaction is unusable. Whether it took effect is unknown.

```python
lease.requests.put_nowait(request := Request("execute", (slow_report,)))
lease.requests.cancel(request)
match await lease.events.get():
    case CancelledEvent(_, started=False): ...         # never ran
    case CancelledEvent(_, started=True): ...          # cancelled while running
    case ResultEvent() | ErrorEvent(): ...             # finished before the cancellation reached it
```

A request that finishes before the cancellation reaches it delivers its result or error instead; either way it has
exactly one outcome. Cancelling the task that awaits `lease.call()` cancels its request the same way; the
`CancelledError` it raises carries the request's
[`CancellationOutcome`](/ref/pool#adorm-cancellationoutcome), which the request's outcome event completes when it
arrives after the caller has left (see [cancellation](/topics/cancellation#events)).

## In process and remote

The core has one path for events, whether the database is reached in the application's process or through a
separate adorm server. The server is reached through a remote driver: its connection sits behind an ordinary slot
of the application's pool, like any other driver connection.

```text
application                                               server
  slot ── Request ──▶ remote DriverConnection ── encoded ──▶ server slot ──▶ ORM driver connection
  slot ◀── return / raise / emit ◀──────────── encoded ◀─── server events
```

Every event has a JSON-safe form: `event.to_dict()` encodes it, `Event.from_dict()` decodes it, and errors travel by
class, message and their adorm attributes ([`encode_error()`](/ref/events#adorm-events-encode-error-error-encode-encode-value)). The
remote connection decodes each event of the server slot and maps it onto the driver contract:

| Server event | In the remote connection |
|---|---|
| `ResultEvent` of the request a method sent | the return value of that method (`execute()`, `commit()` …) |
| `ErrorEvent` of that request | the exception that method raises |
| `CancelledEvent` of that request | the method ends cancelled, as a cancelled driver call does |
| `CallbackEvent` other than `on_commit` / `on_rollback`, `BackendEvent` | passed to `emit` |
| `CallbackEvent("on_commit")`, `CallbackEvent("on_rollback")` | not forwarded: the application's slot delivers its own |

The application's slot then delivers one outcome per request and its own `on_commit` and `on_rollback`, exactly as
for a connection in the process. Server request ids never reach the application's channel.

Result values and payloads are encoded by their inferred type ([`encode_value()`](/ref/encoding#adorm-codec-encode-value-value-hint-none));
`to_dict(encode)` and `from_dict(data, decode)` take a codec that encodes them by the types of the model schema
instead.

## Events by component

| Component | Emits | Consumes |
|---|---|---|
| [Operations](/ref/operations#events) | nothing | nothing |
| [Sessions and bound models](/topics/sessions#events) | nothing | outcomes of their operations, through the runner or the transaction |
| [Transactions](/topics/transactions#events) | nothing | outcomes of their requests, `on_commit` |
| [Pool, router and slots](/topics/pool#events) | outcomes, `on_commit`, `on_rollback` | callbacks and backend events of idle connections and of released leases (the pool's `EventBus`) |
| [Connection profiles](/topics/profiles#events) | nothing | nothing; events emitted during `on_acquire` / `on_release` follow the pool's rules |
| [Drivers](/topics/drivers#events) | callbacks, backend events | nothing; a remote driver decodes the server's events into return values, errors and `emit` |
| [Django driver](/topics/django-driver#events) | nothing: outcomes only; Django's signals and `on_commit()` callbacks stay on the driver side | nothing |
