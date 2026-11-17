---
title: Pure async database ORM
description: 'adorm is a pure async ORM for Python: one async API, executed by drivers natively on the event loop or by synchronous database libraries on pooled threads.'
layout: home

hero:
  name: adorm
  text: Pure async database ORM
  tagline: One async API for queries and models. Drivers run it natively on the event loop, or run synchronous database libraries on pooled threads
  actions:
    - theme: brand
      text: Introduction
      link: /intro/
    - theme: alt
      text: Concepts
      link: /topics/concepts
    - theme: alt
      text: API reference
      link: /ref/
    - theme: alt
      text: GitHub
      link: https://github.com/velis74/adorm

features:
  - title: Pure async
    details: Everything that touches the database is a coroutine or returns an awaitable; the API has no synchronous form. Queries, saves, relation access and iteration use the Django ORM's names without an "a" prefix.
    link: /intro/
  - title: Synchronous libraries, async API
    details: A threaded driver runs a synchronous database library or ORM on a thread owned by one pool slot, with that slot's connection. The event loop awaits the slot and never runs synchronous database code.
    link: /topics/pool
  - title: Queries without a connection
    details: Building a query (Q, F, aggregates, lookups) is pure Python and does no I/O. It produces an adorm operation description that a driver executes.
    link: /topics/queries
  - title: Pluggable drivers
    details: A driver translates operations to its target and executes them, the PostgreSQL driver natively on asyncpg and the Django driver on the Django ORM. The application's code is the same on both.
  - title: Sessions, slots and a pool
    details: Logical sessions name a connection profile. Atomic operations borrow a slot for one call, transactions hold one for the whole block, with timeouts and deadlock detection.
  - title: pydantic models
    details: Generated models are pydantic BaseModel subclasses, so the same schema serves the database layer and pydantic validation and serialisation.
    link: /topics/models
---
