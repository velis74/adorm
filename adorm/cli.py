"""The ``adorm`` command line: ``adorm generate`` (also ``python -m adorm generate``)."""

from __future__ import annotations

import argparse
import importlib
import os
import sys
import warnings

from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from adorm.codegen.files import check, plan, write
from adorm.codegen.spec import CodegenError, CodegenWarning

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
            "directory OUTPUT. With --check, write nothing and exit with status 1 when a file is missing, stale, "
            "obsolete or not written by the generator."
        ),
    )
    generate.add_argument("app_labels", nargs="*", metavar="app_label", help="apps to generate (default: every app)")
    generate.add_argument("-o", "--output", required=True, type=Path, help="package directory of the modules")
    generate.add_argument("--settings", help="Django settings module (default: DJANGO_SETTINGS_MODULE)")
    generate.add_argument(
        "--pythonpath", action="append", default=[], help="directory to add to the import path (repeatable)"
    )
    generate.add_argument(
        "-e", "--exclude", action="append", default=[], metavar="LABEL", help="app or model label to leave out"
    )
    generate.add_argument("--check", action="store_true", help="exit with status 1 when the files are not up to date")
    generate.add_argument("--line-length", type=int, default=88, help="longest line before splitting (default: 88)")
    return parser


def _source(settings: str | None, pythonpath: Sequence[str]) -> ModuleType:
    """`adorm.codegen.django`, with Django set up."""
    for path in reversed([os.getcwd(), *pythonpath]):
        if path not in sys.path:
            sys.path.insert(0, path)
    try:
        source = importlib.import_module("adorm.codegen.django")
    except ModuleNotFoundError as exc:
        if exc.name is None or exc.name.partition(".")[0] != "django":
            raise
        raise CodegenError("adorm generate needs Django: install adorm[django]") from None
    source.setup(settings)
    return source


def _generate(args: argparse.Namespace) -> int:
    source = _source(args.settings, args.pythonpath)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", CodegenWarning)
        apps = source.introspect(args.app_labels or None, exclude=args.exclude)
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


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``adorm`` command with ``argv`` (default: ``sys.argv[1:]``) and return its exit status: 0 on success,
    1 when ``generate --check`` finds files that are not up to date, 2 on an error."""
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return _generate(args)
    except CodegenError as exc:
        print(f"adorm {args.command}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - status 1 means stale files only, so every other failure is status 2
        print(f"adorm {args.command}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR
