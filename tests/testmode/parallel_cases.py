"""Plain Django test cases that use adorm, for ``manage.py test --parallel``: nothing in them is specific to adorm's
test mode."""

import asyncio

from asgiref.sync import sync_to_async
from django.db import connection
from django.test import TestCase, TransactionTestCase

import adorm

from adorm.settings import close_pools, default_router
from tests.djangoproject.library import models as dj
from tests.generated.library import Tag

PROFILES = {"default": {"driver": "adorm.drivers.django.DjangoDriver", "pool": {"max_connections": 3}}}


def database_name():
    return connection.settings_dict["NAME"]


def tag_names():
    return sorted(dj.Tag.objects.values_list("name", flat=True))


async def slot_databases():
    pool = default_router().pool("default")
    return {slot.connection.db.settings_dict["NAME"] for slot in pool._slots}


class AdormInTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        adorm.configure(profiles=PROFILES)
        dj.Tag.objects.create(name="seed")

    async def check(self, name):
        await asyncio.gather(*(Tag.objects.create(name=f"{name}-{n}") for n in range(3)))
        async with adorm.atomic():
            await Tag.objects.create(name=name)
        assert await Tag.objects.count() == 5
        assert await sync_to_async(tag_names)() == sorted(["seed", name, *(f"{name}-{n}" for n in range(3))])
        own = await sync_to_async(database_name)()
        assert own.startswith("adorm_test_parallel_")
        assert await slot_databases() == {own}
        await close_pools()

    async def test_first(self):
        await self.check("first")

    async def test_second(self):
        await self.check("second")


class AdormInTransactionTestCase(TransactionTestCase):
    def setUp(self):
        adorm.configure(profiles=PROFILES)

    async def check(self, name):
        await Tag.objects.create(name=name)
        assert await sync_to_async(tag_names)() == [name]
        own = await sync_to_async(database_name)()
        assert own.startswith("adorm_test_parallel_")
        assert await slot_databases() == {own}
        await close_pools()

    async def test_first(self):
        await self.check("first")

    async def test_second(self):
        await self.check("second")
