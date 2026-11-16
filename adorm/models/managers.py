"""Managers: models bound to a session, manager declarations and driver methods."""

from __future__ import annotations

import functools
import inspect
import types

from collections.abc import Callable, Iterator, Mapping
from typing import Any, ClassVar, Literal, overload, TYPE_CHECKING

from adorm.exceptions import InterfaceError, NotSupportedError
from adorm.query.query import Query
from adorm.query.queryset import QuerySet
from adorm.schema import MethodKey, MethodSchema

if TYPE_CHECKING:
    from adorm.executor.sessions import Session
    from adorm.models.base import Model
    from adorm.operations import Operation

__all__ = ["BoundModel", "DriverMethod", "Manager", "driver_method", "driver_methods", "find_driver_method"]

Target = Literal["manager", "queryset"]


def _check_model(model: Any) -> None:
    from adorm.models.base import Model

    if not isinstance(model, type) or not issubclass(model, Model):
        raise TypeError(f"a bound model needs a Model subclass, not {model!r}")
    if model._meta is None:
        raise TypeError(f"{model.__name__} is abstract: it has no _meta")


def _check_session(session: Any) -> None:
    from adorm.executor.sessions import Session

    if not isinstance(session, Session):
        raise TypeError(f"a bound model needs a Session, not {type(session).__name__}")


def _slot_names(cls: type) -> Iterator[str]:
    for klass in cls.__mro__:
        slots = klass.__dict__.get("__slots__", ())
        yield from (slots,) if isinstance(slots, str) else slots


def _rebind(model: type[Model], name: str, session: Session) -> BoundModel:
    return model.__adorm_managers__[name]._bind(model, session)


class BoundModel:
    """A model class bound to a session: what ``Model.objects``, ``Model.<manager>`` and ``session.Model`` return.

    Binding takes no slot. It does not include a transaction: each operation looks up the session's active
    transaction in the context it runs in. Query methods return a query set of ``queryset_class`` on this session,
    from `get_queryset()`. ``name`` is the manager's attribute name on the model, ``orm_name`` the target ORM's manager
    whose logic the driver applies to every operation (`Query.manager`) and ``method_manager`` the target ORM's manager
    the driver methods run on (`adorm.operations.Call`); ``None`` for none. ``objects`` returns the model's
    ``objects`` manager on the same session, so ``session.Member.objects`` is ``session.Member``; another manager of the
    model is an attribute too (``session.Member.all_members``). A public method of ``queryset_class`` that the base
    `QuerySet` does not define is a method of the bound model, applied to `get_queryset()`, unless it is marked
    ``queryset_only``.

    ``BoundModel(model, session)`` is a manager without manager logic: ``orm_name`` and ``method_manager`` are
    ``None`` and its query sets are plain `QuerySet` objects. The model's declared managers are reached with
    ``Model.<manager>``, ``session.<ModelName>`` and `adorm.Session.bind()`.
    """

    __slots__ = ("method_manager", "model", "name", "orm_name", "session")

    queryset_class: ClassVar[type[QuerySet]] = QuerySet
    """The class of the query sets the manager returns."""

    model: type[Model]
    session: Session
    name: str
    orm_name: str | None
    method_manager: str | None

    def __init__(self, model: type[Model], session: Session) -> None:
        _check_model(model)
        _check_session(session)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "session", session)
        object.__setattr__(self, "name", "objects")
        object.__setattr__(self, "orm_name", None)
        object.__setattr__(self, "method_manager", None)

    def _bind(self, model: type[Model], session: Session) -> BoundModel:
        """A copy of this manager bound to ``model`` and ``session``."""
        _check_model(model)
        _check_session(session)
        bound = object.__new__(type(self))
        for name in _slot_names(type(self)):
            if name != "__weakref__" and hasattr(self, name):
                object.__setattr__(bound, name, object.__getattribute__(self, name))
        if hasattr(self, "__dict__"):
            bound.__dict__.update(self.__dict__)
        object.__setattr__(bound, "model", model)
        object.__setattr__(bound, "session", session)
        return bound

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"a bound model is immutable: cannot set {name!r}")

    def _is_bound(self) -> bool:
        try:
            return object.__getattribute__(self, "model") is not None
        except AttributeError:
            return False

    def __reduce__(self) -> tuple[Any, ...]:
        if not self._is_bound():
            raise TypeError("a manager that is not bound to a model cannot be pickled")
        return _rebind, (self.model, self.name, self.session)

    def __copy__(self) -> BoundModel:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> BoundModel:
        return self

    def _key(self) -> tuple[Any, ...]:
        return (type(self), self.model, self.session, self.name, self.orm_name, self.method_manager)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, BoundModel):
            return NotImplemented
        if not self._is_bound() or not other._is_bound():
            return self is other
        return self._key() == other._key()

    def __hash__(self) -> int:
        if not self._is_bound():
            return object.__hash__(self)
        return hash((BoundModel, *self._key()))

    def __repr__(self) -> str:
        if not self._is_bound():
            orm = "" if self.orm_name is None else f" {self.orm_name!r}"
            if self.method_manager != self.orm_name:
                orm += f" method_manager={self.method_manager!r}"
            return f"<{type(self).__name__}{orm} (not bound)>"
        label = self.model._meta.label  # type: ignore[union-attr]
        name = "" if self.name == "objects" else f".{self.name}"
        return f"<BoundModel {label}{name} on {self.session!r}>"

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or not self._is_bound():
            raise AttributeError(name)
        declared = self.model.__adorm_managers__.get(name)
        if declared is not None:
            return declared._bind(self.model, self.session)
        if _forwarded(self.queryset_class, name):
            return getattr(self.get_queryset(), name)
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

    @property
    def objects(self) -> BoundModel:
        """The model's ``objects`` manager on the same session; the bound model itself when it is that manager."""
        if self.name == "objects":
            return self
        return self.model.__adorm_managers__["objects"]._bind(self.model, self.session)

    async def execute(self, operation: Operation) -> Any:
        """Run ``operation`` on the session (`Session.execute()`)."""
        return await self.session.execute(operation)

    def get_queryset(self) -> QuerySet:
        """A query set of ``queryset_class`` with every row of the model, on this session; its query carries
        ``orm_name`` as `Query.manager`. A subclass overrides it to start every query of the manager with more
        conditions."""
        if not self._is_bound():
            raise TypeError(f"{type(self).__name__} is not bound: access it through a model class")
        query = Query(self.model._meta.label, manager=self.orm_name)  # type: ignore[union-attr]
        return self.queryset_class(self.model, self.session, query)

    def all(self) -> QuerySet:
        """`get_queryset()`."""
        return self.get_queryset()

    def filter(self, *args: Any, **kwargs: Any) -> QuerySet:
        """``all().filter(...)``."""
        return self.all().filter(*args, **kwargs)

    def exclude(self, *args: Any, **kwargs: Any) -> QuerySet:
        """``all().exclude(...)``."""
        return self.all().exclude(*args, **kwargs)

    def annotate(self, **annotations: Any) -> QuerySet:
        """``all().annotate(...)``."""
        return self.all().annotate(**annotations)

    def alias(self, **annotations: Any) -> QuerySet:
        """``all().alias(...)``."""
        return self.all().alias(**annotations)

    def order_by(self, *keys: Any) -> QuerySet:
        """``all().order_by(...)``."""
        return self.all().order_by(*keys)

    def values(self, *fields: str) -> QuerySet:
        """``all().values(...)``."""
        return self.all().values(*fields)

    def values_list(self, *fields: str, flat: bool = False, named: bool = False) -> QuerySet:
        """``all().values_list(...)``."""
        return self.all().values_list(*fields, flat=flat, named=named)

    def only(self, *fields: str) -> QuerySet:
        """``all().only(...)``."""
        return self.all().only(*fields)

    def defer(self, *fields: str | None) -> QuerySet:
        """``all().defer(...)``."""
        return self.all().defer(*fields)

    def select_related(self, *fields: str | None) -> QuerySet:
        """``all().select_related(...)``."""
        return self.all().select_related(*fields)

    def prefetch_related(self, *lookups: Any) -> QuerySet:
        """``all().prefetch_related(...)``."""
        return self.all().prefetch_related(*lookups)

    def distinct(self, *fields: str) -> QuerySet:
        """``all().distinct(...)``."""
        return self.all().distinct(*fields)

    def none(self) -> QuerySet:
        """``all().none()``."""
        return self.all().none()

    def select_for_update(self, **options: Any) -> QuerySet:
        """``all().select_for_update(...)``."""
        return self.all().select_for_update(**options)

    def union(self, *others: QuerySet, all: bool = False) -> QuerySet:
        """``all().union(...)``."""
        return self.all().union(*others, all=all)

    def intersection(self, *others: QuerySet) -> QuerySet:
        """``all().intersection(...)``."""
        return self.all().intersection(*others)

    def difference(self, *others: QuerySet) -> QuerySet:
        """``all().difference(...)``."""
        return self.all().difference(*others)

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        """``await all().get(...)``."""
        return await self.all().get(*args, **kwargs)

    async def first(self) -> Any:
        """``await all().first()``."""
        return await self.all().first()

    async def last(self) -> Any:
        """``await all().last()``."""
        return await self.all().last()

    async def count(self) -> int:
        """``await all().count()``."""
        return await self.all().count()

    async def exists(self) -> bool:
        """``await all().exists()``."""
        return await self.all().exists()

    async def aggregate(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """``await all().aggregate(...)``."""
        return await self.all().aggregate(*args, **kwargs)

    async def in_bulk(self, id_list: Any = None, *, field_name: str = "pk") -> dict[Any, Any]:
        """``await all().in_bulk(...)``."""
        return await self.all().in_bulk(id_list, field_name=field_name)

    async def create(self, **values: Any) -> Any:
        """``await all().create(...)``."""
        return await self.all().create(**values)

    async def bulk_create(self, objs: Any, *args: Any, **kwargs: Any) -> list[Any]:
        """``await all().bulk_create(...)``."""
        return await self.all().bulk_create(objs, *args, **kwargs)

    async def create_from_parent(self, parent: Any, **values: Any) -> Any:
        """``await all().create_from_parent(...)``."""
        return await self.all().create_from_parent(parent, **values)

    async def get_or_create(self, defaults: Any = None, **lookups: Any) -> tuple[Any, bool]:
        """``await all().get_or_create(...)``."""
        return await self.all().get_or_create(defaults, **lookups)

    async def update_or_create(
        self, defaults: Any = None, create_defaults: Any = None, **lookups: Any
    ) -> tuple[Any, bool]:
        """``await all().update_or_create(...)``."""
        return await self.all().update_or_create(defaults, create_defaults, **lookups)

    async def update(self, **values: Any) -> int:
        """``await all().update(...)``."""
        return await self.all().update(**values)

    async def delete(self) -> tuple[int, dict[str, int]]:
        """``await all().delete()``."""
        return await self.all().delete()

    def __aiter__(self) -> Any:
        return self.all().__aiter__()


_QUERYSET_NAMES = frozenset(dir(QuerySet))


def _forwarded(queryset_class: type[QuerySet], name: str) -> bool:
    """Whether the manager forwards ``name`` to its query set: a public method a `QuerySet` subclass defines."""
    if name in _QUERYSET_NAMES:
        return False
    try:
        value = inspect.getattr_static(queryset_class, name)
    except AttributeError:
        return False
    if isinstance(value, DriverMethod):
        return not value.queryset_only
    return inspect.isfunction(value) and not getattr(value, "queryset_only", False)


class Manager(BoundModel):
    """A manager declared in a model class body: ``objects = Manager()``.

    ``orm_name`` names the target ORM's manager that every operation of this manager names (`Query.manager`); the
    driver applies its rows (Django: ``get_queryset()``) and treats the overrides of its query set (``create()``,
    ``delete()`` …) as its documentation states: the Django driver runs them, the native PostgreSQL driver refuses the
    write. ``None`` reads and writes the model's rows without manager logic, on every driver.
    ``method_manager`` names the target ORM's manager the driver methods (`driver_method`) run on; it defaults to
    ``orm_name``. A manager with ``method_manager`` and no ``orm_name`` sends its queries without `Query.manager`, so
    they run on every driver; only its driver methods need a driver that runs ``call`` operations.

    Accessing the manager on a model class returns a copy bound to that class and the current session; accessing it on
    an instance raises ``AttributeError``, so does accessing it on an abstract model. A subclass adds driver methods
    (`driver_method`), client-side methods and a ``queryset_class``. `adorm.Model` declares ``objects = Manager()``.
    """

    __slots__ = ()

    def __init__(self, orm_name: str | None = None, *, method_manager: str | None = None) -> None:
        for value in (orm_name, method_manager):
            if value is not None and (not isinstance(value, str) or not value.isidentifier() or value.startswith("_")):
                raise ValueError(f"{value!r} is not a manager name")
        object.__setattr__(self, "model", None)
        object.__setattr__(self, "session", None)
        object.__setattr__(self, "name", None)
        object.__setattr__(self, "orm_name", orm_name)
        object.__setattr__(self, "method_manager", orm_name if method_manager is None else method_manager)

    def __set_name__(self, owner: type, name: str) -> None:
        if self.name is None:
            object.__setattr__(self, "name", name)
        elif self.name != name:
            raise TypeError(f"a manager is declared once: it is already {self.name!r}, not {name!r}")

    @overload
    def __get__(self, instance: None, owner: type[Model]) -> BoundModel: ...

    @overload
    def __get__(self, instance: object, owner: type) -> BoundModel: ...

    def __get__(self, instance: Any, owner: type) -> BoundModel:
        if self._is_bound():
            return self
        if instance is not None:
            raise AttributeError(f"{self.name} is not accessible via {owner.__name__} instances")
        if getattr(owner, "_meta", None) is None:
            raise AttributeError(f"{owner.__name__} is abstract: it has no {self.name}")
        from adorm.executor.sessions import current_session

        return self._bind(owner, current_session())

    @classmethod
    def from_queryset(cls, queryset_class: type[QuerySet], class_name: str | None = None) -> type[Manager]:
        """A subclass of this manager class whose ``queryset_class`` is ``queryset_class``."""
        if not isinstance(queryset_class, type) or not issubclass(queryset_class, QuerySet):
            raise TypeError(f"from_queryset() takes a QuerySet subclass, not {queryset_class!r}")
        name = class_name or f"{cls.__name__}From{queryset_class.__name__}"
        return type(name, (cls,), {"__slots__": (), "queryset_class": queryset_class, "__module__": cls.__module__})


def _queryset_as_manager(
    cls: type[QuerySet], orm_name: str | None = None, method_manager: str | None = None
) -> Manager:
    return Manager.from_queryset(cls)(orm_name, method_manager=method_manager)


class DriverMethod:
    """A method of a manager or query set class that the driver runs on the target ORM's manager or query set.

    Created by `driver_method`. Calling it checks the arguments against the declared signature and sends a
    `adorm.operations.Call`: on a bound manager with the target ``"manager"``, on a query set with the target
    ``"queryset"`` and the query set's query. The result is the driver's, with every
    `adorm.operations.ModelRow` turned into an instance of the registered model. The call goes to the manager's
    ``method_manager``; on a query set, to that of the model's manager whose ``queryset_class`` declares the method,
    or to the query's `Query.manager`. No such manager raises ``TypeError``.
    """

    def __init__(self, func: Callable[..., Any], *, queryset_only: bool = False) -> None:
        if not inspect.iscoroutinefunction(func):
            raise TypeError(f"a driver method is declared with async def, {func!r} is not")
        signature = inspect.signature(func)
        parameters = list(signature.parameters.values())
        if not parameters or parameters[0].kind not in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            raise TypeError(f"the driver method {func.__name__} needs a self parameter")
        if func.__name__.startswith("_"):
            raise ValueError(f"a driver method is public, {func.__name__!r} is not")
        self.func = func
        self.name = func.__name__
        self.signature = signature
        self.queryset_only = bool(queryset_only)
        self._schema: MethodSchema | None = None
        functools.update_wrapper(self, func)

    def __set_name__(self, owner: type, name: str) -> None:
        if name != self.name:
            raise ValueError(f"the driver method {self.name} is assigned to {name!r}: declare it under its own name")

    def __get__(self, instance: Any, owner: type | None = None) -> Any:
        if instance is None:
            return self
        return types.MethodType(self, instance)

    def __repr__(self) -> str:
        return f"<driver method {self.func.__qualname__}>"

    @property
    def schema(self) -> MethodSchema:
        """The `MethodSchema` of the declared parameters; a string annotation is evaluated in the function's module,
        and one that does not evaluate leaves the parameter without a type."""
        if self._schema is None:
            positional: list[Any] = []
            keyword: dict[str, Any] = {}
            var_positional = var_keyword = None
            for parameter in list(self.signature.parameters.values())[1:]:
                hint = self._hint(parameter.annotation)
                kind = parameter.kind
                if kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
                    positional.append(hint)
                if kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
                    keyword[parameter.name] = hint
                if kind is inspect.Parameter.VAR_POSITIONAL:
                    var_positional = hint
                if kind is inspect.Parameter.VAR_KEYWORD:
                    var_keyword = hint
            self._schema = MethodSchema(tuple(positional), keyword, var_positional, var_keyword)
        return self._schema

    def _hint(self, annotation: Any) -> Any:
        if annotation is inspect.Parameter.empty:
            return None
        if isinstance(annotation, str):
            try:
                return eval(annotation, self.func.__globals__)  # noqa: S307 - an annotation of the declaring module
            except Exception:  # noqa: BLE001 - an annotation that does not evaluate types nothing
                return None
        return annotation

    async def __call__(self, owner: Any, /, *args: Any, **kwargs: Any) -> Any:
        from adorm.operations import Call

        try:
            bound = self.signature.bind(owner, *args, **kwargs)
        except TypeError as exc:
            raise TypeError(f"{self.name}(): {exc}") from None
        call_args, call_kwargs = bound.args[1:], tuple(bound.kwargs.items())
        if isinstance(owner, QuerySet):
            queryset = owner
            manager = owner.query.manager or _method_manager(owner.model, self)
            operation = Call(owner.query, self.name, "queryset", call_args, call_kwargs, manager)
        elif isinstance(owner, BoundModel):
            if not owner._is_bound():
                raise TypeError(f"{type(owner).__name__} is not bound: access it through a model class")
            if owner.method_manager is None:
                raise TypeError(f"the manager {owner.name} has no ORM manager to run {self.name} on")
            queryset = QuerySet(owner.model, owner.session)
            query = Query(owner.model._meta.label, manager=owner.orm_name)  # type: ignore[union-attr]
            operation = Call(query, self.name, "manager", call_args, call_kwargs, owner.method_manager)
        else:
            raise TypeError(f"the driver method {self.name} belongs to a manager or a query set")
        result = await queryset._run(operation)
        return convert_result(result, queryset.executor)


def _method_manager(model: type[Model], method: DriverMethod) -> str:
    """The ``method_manager`` of ``model``'s first manager whose query set class declares ``method``."""
    for manager in model.__adorm_managers__.values():
        if manager.method_manager is not None and any(m is method for m in _class_methods(manager.queryset_class)):
            return manager.method_manager
    raise TypeError(f"no manager of {model.__name__} runs the driver method {method.name}")


@overload
def driver_method(func: Callable[..., Any], /) -> DriverMethod: ...


@overload
def driver_method(*, queryset_only: bool = False) -> Callable[[Callable[..., Any]], DriverMethod]: ...


def driver_method(func: Callable[..., Any] | None = None, /, *, queryset_only: bool = False) -> Any:
    """Declare an ``async def`` method of a `Manager` or `QuerySet` subclass as a method the driver runs.

    The declaration's signature is the method's: arguments are checked against it before anything is sent, and its
    annotations type the arguments' encoding. The body is not run. ``queryset_only`` keeps a query set method off the
    manager, as Django's ``queryset_only`` does.
    """
    if func is None:
        return lambda f: DriverMethod(f, queryset_only=queryset_only)
    return DriverMethod(func, queryset_only=queryset_only)


def convert_result(value: Any, executor: Any) -> Any:
    """``value`` with every `adorm.operations.ModelRow` in it, also inside lists, tuples and the values of mappings,
    turned into an instance of the registered model, run by ``executor``. A label that is not registered raises
    `InterfaceError`."""
    from adorm.models.registry import registry
    from adorm.operations import ModelRow

    if isinstance(value, ModelRow):
        try:
            model = registry.get(value.model)
        except LookupError:
            raise InterfaceError(f"the driver returned a row of {value.model}, which is not registered") from None
        return model.from_row(value.row, executor=executor)
    if isinstance(value, list):
        return [convert_result(item, executor) for item in value]
    if isinstance(value, tuple) and not hasattr(value, "_fields"):
        return tuple(convert_result(item, executor) for item in value)
    if isinstance(value, Mapping):
        return {key: convert_result(item, executor) for key, item in value.items()}
    return value


def _class_methods(cls: type) -> Iterator[DriverMethod]:
    seen: set[str] = set()
    for klass in cls.__mro__:
        for name, value in vars(klass).items():
            if name in seen:
                continue
            seen.add(name)
            if isinstance(value, DriverMethod):
                yield value


def driver_methods(model: type[Model]) -> Mapping[MethodKey, DriverMethod]:
    """The driver methods of ``model``'s managers that have a ``method_manager``, by ``(method_manager, target,
    method)``."""
    methods: dict[MethodKey, DriverMethod] = {}
    for manager in model.__adorm_managers__.values():
        if manager.method_manager is None:
            continue
        for method in _class_methods(type(manager)):
            methods[(manager.method_manager, "manager", method.name)] = method
        for method in _class_methods(manager.queryset_class):
            methods[(manager.method_manager, "queryset", method.name)] = method
    return types.MappingProxyType(methods)


def find_driver_method(label: str, manager: str | None, target: str, method: str) -> DriverMethod:
    """The driver method ``method`` declared for ``target`` (``"manager"`` or ``"queryset"``) of the ORM manager
    ``manager`` on the registered model ``label``.

    A driver calls it before running a `adorm.operations.Call`, so that only declared methods run. Raises
    `NotSupportedError` when the model is not registered or declares no such method.
    """
    from adorm.models.registry import registry

    try:
        model = registry.get(label)
    except LookupError:
        raise NotSupportedError(f"cannot call {method}: no model is registered under {label!r}") from None
    found = driver_methods(model).get((manager, target, method)) if manager is not None else None
    if found is None:
        raise NotSupportedError(f"{label} declares no driver method {method!r} on the {target} of manager {manager!r}")
    return found
