"""The ``adorm`` command line: ``adorm generate`` and ``adorm check-schema`` (also ``python -m adorm ...``)."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import sys
import warnings

from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

from adorm.codegen.files import check, plan, write
from adorm.codegen.spec import CodegenError, CodegenWarning, ThirdPartyMethodsWarning

__all__ = ["main"]

EXIT_OK = 0
EXIT_STALE = 1
EXIT_ERROR = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="adorm", description="adorm command line tools.")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    generate = commands.add_parser(
        "generate",
        help="generate adorm model modules from Django models",
        description=(
            "Write one module of adorm models per Django app, and an __init__.py importing them, into the package "
            "directory OUTPUT. An app label selects every model of the app, a model label (app_label.Model) that "
            "model. With --check, write nothing and exit with status 1 when a file is missing, stale, "
            "obsolete or not written by the generator."
        ),
    )
    generate.add_argument(
        "labels",
        nargs="*",
        metavar="label",
        help="app label or model label (app_label.Model) to generate (default: every app)",
    )
    generate.add_argument("-o", "--output", required=True, type=Path, help="package directory of the modules")
    generate.add_argument("--settings", help="Django settings module (default: DJANGO_SETTINGS_MODULE)")
    generate.add_argument(
        "--pythonpath",
        action="append",
        default=[],
        help="directory to put on the import path after the current directory (repeatable)",
    )
    generate.add_argument(
        "-e", "--exclude", action="append", default=[], metavar="LABEL", help="app or model label to leave out"
    )
    generate.add_argument("--check", action="store_true", help="exit with status 1 when the files are not up to date")
    generate.add_argument(
        "--ignore-third-party",
        action="store_true",
        help="do not warn about manager and query set methods of installed packages",
    )
    generate.add_argument("--line-length", type=int, default=88, help="longest line before splitting (default: 88)")
    check_schema = commands.add_parser(
        "check-schema",
        help="check that the database tables match the adorm models",
        description=(
            "Configure adorm from the Django settings (--settings or DJANGO_SETTINGS_MODULE) and the --configure "
            "modules, import the adorm model modules MODULE, then compare each selected model with the driver of the "
            "connection profile USING: its schema hash with the driver's model, its tables and columns with the "
            "database. An app label selects every registered model of the app, a model label (app_label.Model) that "
            "model; without labels every registered model is checked. Exit with status 1 when a problem is found."
        ),
    )
    check_schema.add_argument(
        "labels", nargs="*", metavar="label", help="app label or model label to check (default: every model)"
    )
    check_schema.add_argument(
        "-m",
        "--models",
        action="append",
        required=True,
        metavar="MODULE",
        help="module of adorm models to import, e.g. the package adorm generate wrote (repeatable)",
    )
    check_schema.add_argument("--using", default="default", help="connection profile (default: default)")
    check_schema.add_argument(
        "--settings", help="Django settings module to configure adorm from (default: DJANGO_SETTINGS_MODULE)"
    )
    check_schema.add_argument(
        "--configure",
        action="append",
        default=[],
        metavar="MODULE",
        help="module that calls adorm.configure(), imported before the model modules (repeatable)",
    )
    check_schema.add_argument(
        "--pythonpath",
        action="append",
        default=[],
        help="directory to put on the import path after the current directory (repeatable)",
    )
    return parser


def _import_path(pythonpath: Sequence[str]) -> None:
    """Put the current directory, then each ``pythonpath`` directory, at the front of `sys.path`, moving entries
    that name the same directory there, so an installed module of the same name does not shadow them."""
    front = list(dict.fromkeys(os.path.abspath(path) for path in (os.curdir, *pythonpath)))
    rest = [entry for entry in sys.path if os.path.abspath(entry or os.curdir) not in front]
    sys.path[:] = [*front, *rest]


def _source(settings: str | None, pythonpath: Sequence[str], command: str) -> ModuleType:
    """`adorm.codegen.django`, with Django set up; ``command`` names the command in the error without Django."""
    _import_path(pythonpath)
    try:
        source = importlib.import_module("adorm.codegen.django")
    except ModuleNotFoundError as exc:
        if exc.name is None or exc.name.partition(".")[0] != "django":
            raise
        raise CodegenError(f"adorm {command} needs Django: install adorm[django]") from None
    source.setup(settings)
    return source


def _generate(args: argparse.Namespace) -> int:
    source = _source(args.settings, args.pythonpath, args.command)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", CodegenWarning)
        if args.ignore_third_party:
            warnings.simplefilter("ignore", ThirdPartyMethodsWarning)
        apps = source.introspect(args.labels or None, exclude=args.exclude)
    for warning in caught:
        if issubclass(warning.category, CodegenWarning):
            print(f"warning: {warning.message}", file=sys.stderr)
        else:
            warnings.showwarning(warning.message, warning.category, warning.filename, warning.lineno)
    files = plan(apps, line_length=args.line_length)
    if args.check:
        differences = check(args.output, files)
        for difference in differences:
            print(difference, file=sys.stderr)
        if differences:
            print("generated models are not up to date: run adorm generate without --check", file=sys.stderr)
            return EXIT_STALE
        print(f"{len(files)} generated files are up to date.")
        return EXIT_OK
    result = write(args.output, files)
    for path in result.written:
        print(f"wrote {path}")
    for path in result.removed:
        print(f"removed {path}")
    print(f"{len(result.written)} written, {len(result.removed)} removed, {len(result.unchanged)} unchanged.")
    return EXIT_OK


def _checked_models(labels: Sequence[str]) -> list[Any]:
    """The registered models the labels select: an app label every model of the app, a model label that model."""
    from adorm.models.registry import registry

    if not labels:
        return list(registry)
    selected: list[Any] = []
    for label in labels:
        if "." in label:
            try:
                found = [registry.get(label)]
            except LookupError:
                raise CodegenError(f"no adorm model is registered as {label!r}") from None
        else:
            found = [model for model in registry if model._meta.label.partition(".")[0] == label]
            if not found:
                raise CodegenError(f"no adorm model of the app {label!r} is registered")
        selected.extend(model for model in found if model not in selected)
    return selected


async def _run_check(models: list[Any], using: str) -> tuple[Any, ...]:
    import adorm

    from adorm.settings import close_pools

    try:
        return await adorm.check_schema(models, session=adorm.create_session(using))
    finally:
        await close_pools()


def _check_schema(args: argparse.Namespace) -> int:
    from adorm.settings import profiles

    _import_path(args.pythonpath)
    if args.settings or os.environ.get("DJANGO_SETTINGS_MODULE"):
        _source(args.settings, args.pythonpath, args.command)
        importlib.import_module("adorm.contrib.django.config").ensure_configured()
    for module in (*args.configure, *args.models):
        importlib.import_module(module)
    if profiles.is_empty():
        raise CodegenError("no connection profile is configured: pass --settings or --configure")
    models = _checked_models(args.labels)
    problems = asyncio.run(_run_check(models, args.using))
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(f"{_count(len(problems), 'problem')} in {_count(len(models), 'model')}.", file=sys.stderr)
        return EXIT_STALE
    print(f"{_count(len(models), 'model')} checked, no problems found.")
    return EXIT_OK


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}{'' if number == 1 else 's'}"


_COMMANDS = {"generate": _generate, "check-schema": _check_schema}


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``adorm`` command with ``argv`` (default: ``sys.argv[1:]``) and return its exit status: 0 on success,
    1 when ``generate --check`` finds files that are not up to date or ``check-schema`` finds problems, 2 on an
    error."""
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return _COMMANDS[args.command](args)
    except CodegenError as exc:
        print(f"adorm {args.command}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - status 1 means stale files or schema problems, every failure is 2
        print(f"adorm {args.command}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR
