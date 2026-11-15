"""Constants of the ``library`` fixture's data and helpers shared by the read tests of every driver."""

import asyncio
import uuid

from django.db import connections

DUNE = "9780000000001"
POEMS = "9780000000002"
PHYSICS = "9780000000003"
DUNE_2 = "9780000000004"
NOTE = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def in_thread(fn):
    """``fn()`` with the Django ORM on a worker thread, whose connection is closed afterwards."""

    def run():
        try:
            return fn()
        finally:
            connections.close_all()

    return await asyncio.to_thread(run)


async def names(queryset):
    return [author.name async for author in queryset]


async def titles(queryset):
    return sorted([book.title async for book in queryset])


WAITING = (
    "SELECT pid FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND query LIKE '%library_author%'"
    " AND pid <> pg_backend_pid()"
)
