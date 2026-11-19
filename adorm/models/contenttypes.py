"""Content types: the content type row of a model and the model of a content type row, cached per database.

A content type row (Django's ``ContentType``) names a model by ``app_label`` and ``model`` (the lowercased class
name). Its primary key is what a `GenericForeignKey` stores. The rows are created by the target (Django's
``migrate``); adorm only reads them.
"""

from __future__ import annotations

import threading
import weakref

from collections.abc import Hashable, Iterable
from typing import Any, TYPE_CHECKING

from adorm.exceptions import ObjectDoesNotExist
from adorm.query.query import Query

if TYPE_CHECKING:
    from adorm.models.base import Executor, Model

__all__ = ["CONTENT_TYPE_MODEL", "ContentTypes", "content_types", "natural_key"]

CONTENT_TYPE_MODEL = "contenttypes.ContentType"
"""The label of the content type model."""

_BATCH_SIZE = 500
_MISSING = object()

NaturalKey = tuple[str, str]


def natural_key(model: type[Model] | Model, *, for_concrete_model: bool = True) -> NaturalKey:
    """``(app_label, model)`` of the content type of ``model`` (a model class or instance): the parts of its label,
    the model name lowercased. With ``for_concrete_model``, a proxy model gives the key of its concrete model
    (`ModelMeta.concrete_model`)."""
    meta = _model_class(model)._require_meta()
    label = (meta.concrete_model or meta.label) if for_concrete_model else meta.label
    app_label, _, name = label.partition(".")
    return app_label, name.lower()


def _model_class(model: Any) -> type[Model]:
    from adorm.models.base import Model

    if isinstance(model, Model):
        return type(model)
    if isinstance(model, type) and issubclass(model, Model):
        return model
    raise TypeError(f"a content type is looked up for a model class or instance, not {model!r}")


def _executor(session: Executor | None) -> Executor:
    if session is not None:
        return session
    from adorm.executor.sessions import current_session

    return current_session()


def _database_key(executor: Executor) -> Any:
    """What identifies the content type cache of ``executor``: for a session, its profile driver's
    `Driver.database_key()`, else its registered connection profile (the name of its profile while none is
    registered), with the value of the profile's ``content_types_key`` when it has one; otherwise the executor itself.
    Raises `TypeError` when ``content_types_key`` returns an unhashable value."""
    from adorm.executor.sessions import Session

    if isinstance(executor, Session):
        from adorm.settings import get_profile

        profile = get_profile(executor.using)
        if profile is None:
            return ("profile", executor.using)
        try:
            key = profile.get_driver().database_key(profile)
        except Exception:  # noqa: BLE001 - a driver that cannot tell keeps a cache per profile
            key = None
        database = profile if key is None else ("database", key)
        if profile.content_types_key is None:
            return database
        part = profile.content_types_key(profile, executor)
        if not isinstance(part, Hashable):
            raise TypeError(
                f"content_types_key of profile {profile.name!r} returns a hashable value, not {type(part).__name__}"
            )
        return ("split", database, part)
    return executor


class _Database:
    """The cached content types of one database: ids by ``(content type model, natural key)`` and natural keys by
    ``(content type model, id)``."""

    __slots__ = ("ids", "keys")

    def __init__(self) -> None:
        self.ids: dict[tuple[str, NaturalKey], Any] = {}
        self.keys: dict[tuple[str, Any], NaturalKey] = {}


class ContentTypes:
    """Content type ids by model and models by content type id, cached per database (Django's
    ``ContentType.objects.get_for_model()`` and ``get_for_id()``).

    Each lookup method takes ``session``, the `Session` (or another executor) whose database it reads, by default the
    current session; inside a transaction of that session it reads on the transaction's slot. The cache belongs to
    the database the session's profile names (`Driver.database_key()`): profiles whose drivers name the same database
    share it. For a profile whose driver names none, it belongs to the profile: sessions of that profile share it, and
    a profile that is replaced or unregistered starts with an empty one. A profile's ``content_types_key`` splits the
    cache further: lookups share one only when it returns equal values. ``content_type_model`` is the label of the
    content type model. The cache holds what was read; a lookup that misses runs a query on every call until one
    succeeds. Thread-safe.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._weak: weakref.WeakKeyDictionary[Any, _Database] = weakref.WeakKeyDictionary()
        self._named: dict[Any, _Database] = {}
        self._generation = 0

    def _database(self, executor: Executor, *, create: bool) -> _Database | None:
        key = _database_key(executor)
        try:
            database = self._weak.get(key)
            if database is None and create:
                database = self._weak.setdefault(key, _Database())
        except TypeError:
            # A key that takes no weak reference: a profile name, or an executor without __weakref__.
            try:
                database = self._named.get(key)
                if database is None and create:
                    database = self._named.setdefault(key, _Database())
            except TypeError:
                return None
        return database

    def _store(self, executor: Executor, label: str, rows: Iterable[tuple[Any, NaturalKey]], generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            database = self._database(executor, create=True)
            if database is None:
                return
            for content_type, key in rows:
                database.ids[(label, key)] = content_type
                database.keys[(label, content_type)] = key

    def clear(self) -> None:
        """Forget every cached content type. A lookup running meanwhile does not store what it reads."""
        with self._lock:
            self._generation += 1
            self._weak.clear()
            self._named.clear()

    def cached_id(
        self,
        model: type[Model] | Model,
        *,
        for_concrete_model: bool = True,
        session: Executor | None = None,
        content_type_model: str = CONTENT_TYPE_MODEL,
    ) -> Any:
        """The cached content type id of ``model`` (a model class or instance), ``None`` when it is not cached. Runs
        nothing."""
        key = natural_key(model, for_concrete_model=for_concrete_model)
        with self._lock:
            database = self._database(_executor(session), create=False)
            return None if database is None else database.ids.get((content_type_model, key))

    async def get_id(
        self,
        model: type[Model] | Model,
        *,
        for_concrete_model: bool = True,
        session: Executor | None = None,
        content_type_model: str = CONTENT_TYPE_MODEL,
    ) -> Any:
        """The content type id of ``model`` (a model class or instance; a proxy model's is its concrete model's with
        ``for_concrete_model``): cached, or read with a `Get` operation and cached. No content type row raises the
        content type model's ``DoesNotExist`` (`ObjectDoesNotExist` when that model is not registered)."""
        from adorm.models.registry import registry
        from adorm.operations import Get

        key = natural_key(model, for_concrete_model=for_concrete_model)
        executor = _executor(session)
        cached = self.cached_id(
            model, for_concrete_model=for_concrete_model, session=executor, content_type_model=content_type_model
        )
        if cached is not None:
            return cached
        generation = self._generation
        pk = _pk_name(content_type_model)
        query = Query(content_type_model).filter(app_label=key[0], model=key[1]).values(pk)
        try:
            row = await executor.execute(Get(query))
        except ObjectDoesNotExist as exc:
            if type(exc) is not ObjectDoesNotExist:
                raise
            message = f"no {content_type_model} row names {key[0]}.{key[1]}"
            if content_type_model in registry:
                raise registry.get(content_type_model).DoesNotExist(message) from exc
            raise ObjectDoesNotExist(message) from exc
        self._store(executor, content_type_model, [(row[pk], key)], generation)
        return row[pk]

    async def get_for_model(
        self,
        model: type[Model] | Model,
        *,
        for_concrete_model: bool = True,
        session: Executor | None = None,
        content_type_model: str = CONTENT_TYPE_MODEL,
    ) -> Model:
        """The content type row of ``model`` as an instance of the registered content type model, with its primary
        key, ``app_label`` and ``model`` loaded and its other fields deferred; `get_id()` gives the id. A new instance
        on every call. Raises ``LookupError`` when the content type model is not registered, and as `get_id()`."""
        from adorm.models.registry import registry

        target = registry.get(content_type_model)
        executor = _executor(session)
        content_type = await self.get_id(
            model, for_concrete_model=for_concrete_model, session=executor, content_type_model=content_type_model
        )
        app_label, name = natural_key(model, for_concrete_model=for_concrete_model)
        row = {target._require_meta().pk: content_type, "app_label": app_label, "model": name}
        return target.from_row(row, executor=executor)

    async def get_model(
        self, content_type: Any, *, session: Executor | None = None, content_type_model: str = CONTENT_TYPE_MODEL
    ) -> type[Model]:
        """The registered model of the content type id ``content_type``: its natural key cached, or read with a `Get`
        operation and cached. No such row raises the content type model's ``DoesNotExist``
        (`ObjectDoesNotExist` when that model is not registered); a content type of a model that is not registered
        ``LookupError``."""
        from adorm.models.registry import registry

        keys = await self.natural_keys([content_type], session=session, content_type_model=content_type_model)
        if content_type not in keys:
            message = f"no {content_type_model} row has the primary key {content_type!r}"
            if content_type_model in registry:
                raise registry.get(content_type_model).DoesNotExist(message)
            raise ObjectDoesNotExist(message)
        return registry.get_for_content_type(*keys[content_type])

    async def natural_keys(
        self,
        content_types: Iterable[Any],
        *,
        session: Executor | None = None,
        content_type_model: str = CONTENT_TYPE_MODEL,
    ) -> dict[Any, NaturalKey]:
        """``(app_label, model)`` of each of the content type ids ``content_types`` that has a row, by id: cached, the
        others read and cached: one id with a `Get` operation, several with `Select` operations of up to 500 ids each.
        ``None`` ids are skipped."""
        from adorm.operations import Get, Select

        executor = _executor(session)
        wanted = list(dict.fromkeys(value for value in content_types if value is not None))
        found: dict[Any, NaturalKey] = {}
        with self._lock:
            database = self._database(executor, create=False)
            if database is not None:
                for content_type in wanted:
                    key = database.keys.get((content_type_model, content_type), _MISSING)
                    if key is not _MISSING:
                        found[content_type] = key
        missing = [content_type for content_type in wanted if content_type not in found]
        if not missing:
            return found
        generation = self._generation
        pk = _pk_name(content_type_model)
        rows: list[tuple[Any, NaturalKey]] = []
        if len(missing) == 1:
            query = Query(content_type_model).filter(pk=missing[0]).values(pk, "app_label", "model")
            try:
                found_rows = [await executor.execute(Get(query))]
            except ObjectDoesNotExist as exc:
                if type(exc) is not ObjectDoesNotExist:
                    raise
                found_rows = []
        else:
            found_rows = []
            for start in range(0, len(missing), _BATCH_SIZE):
                query = Query(content_type_model).filter(pk__in=missing[start : start + _BATCH_SIZE])
                found_rows += await executor.execute(Select(query.values(pk, "app_label", "model")))
        for row in found_rows:
            rows.append((row[pk], (row["app_label"], row["model"])))
        self._store(executor, content_type_model, rows, generation)
        found.update(rows)
        return found


def _pk_name(label: str) -> str:
    from adorm.models.registry import registry

    return registry.get(label)._require_meta().pk if label in registry else "id"


content_types = ContentTypes()
"""The content type cache of the process."""
