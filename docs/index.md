---
title: Async ORM interface
description: 'adorm is a fully asynchronous ORM interface for Python: Django-style queries and models, executed by pluggable drivers.'
layout: home

hero:
  name: adorm
  text: Async ORM interface
  tagline: Django-style queries and models with one async API, executed by pluggable drivers
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
  - title: One async API
    details: Everything that touches the database is a coroutine or returns an awaitable. Queries, saves, relation access and iteration use the Django ORM's names without an "a" prefix.
  - title: Queries without a connection
    details: Building a query (Q, F, aggregates, lookups) is pure Python and does no I/O. It produces an adorm operation description that a driver executes.
  - title: Pluggable drivers
    details: A driver translates operations to its target. The Django ORM driver runs the synchronous ORM in a dedicated thread per slot; the PostgreSQL driver runs natively on asyncpg.
  - title: Sessions, slots and a pool
    details: Logical sessions name a connection profile. Atomic operations borrow a slot for one call, transactions hold one for the whole block, with timeouts and deadlock detection.
  - title: pydantic models
    details: Generated models are pydantic BaseModel subclasses, so the same schema serves the database layer and fastapi-viewsets endpoints.
---
