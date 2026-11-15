"""The core package imports no ORM or database library; drivers do, in their own subpackages, and so does the
model generator's Django source (`adorm.codegen.django`)."""

import ast
import json
import re
import subprocess
import sys
import tomllib

from pathlib import Path

import pytest

import adorm

PACKAGE = Path(adorm.__file__).parent
TARGETS = {"django", "sqlalchemy", "asyncpg", "psycopg", "psycopg2"}


ORM_SOURCES = {PACKAGE / "codegen" / "django.py"}


def core_modules() -> list[Path]:
    """Every module of the package outside a driver subpackage (``adorm/drivers/<name>/``) and the generator's ORM
    sources."""
    drivers = PACKAGE / "drivers"
    return sorted(
        p for p in PACKAGE.rglob("*.py") if not (p.parent != drivers and drivers in p.parents) and p not in ORM_SOURCES
    )


def imported_roots(path: Path) -> set[str]:
    roots = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_core_modules_found():
    names = {p.relative_to(PACKAGE).as_posix() for p in core_modules()}
    assert {"__init__.py", "exceptions.py", "events.py", "drivers/__init__.py", "drivers/base.py"} <= names
    assert {"cli.py", "codegen/__init__.py", "codegen/render.py"} <= names
    assert "codegen/django.py" not in names


@pytest.mark.parametrize("path", core_modules(), ids=lambda p: p.relative_to(PACKAGE).as_posix())
def test_core_module_imports_no_orm(path):
    assert imported_roots(path) & TARGETS == set()


def test_importing_the_core_loads_no_orm():
    modules = [
        ".".join(("adorm", *p.relative_to(PACKAGE).with_suffix("").parts)).removesuffix(".__init__")
        for p in core_modules()
    ]
    code = (
        "import importlib, json, sys\n"
        f"for name in {modules!r}: importlib.import_module(name)\n"
        f"print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] in {sorted(TARGETS)!r})))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)  # noqa: S603
    assert json.loads(result.stdout) == []


def test_imported_roots():
    sample = PACKAGE / "drivers" / "base.py"
    assert {"asyncio", "adorm"} <= imported_roots(sample)


def test_driver_extras_cover_driver_imports():
    """Every ORM or database library a driver subpackage imports is in the extra named after it."""
    extras = tomllib.loads((PACKAGE.parent / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]
    drivers = [p for p in (PACKAGE / "drivers").iterdir() if p.is_dir() and (p / "__init__.py").is_file()]
    assert drivers
    for driver in drivers:
        declared = {re.split(r"[\[<>=!~ ]", requirement)[0] for requirement in extras[driver.name]}
        imported = set().union(*(imported_roots(path) for path in driver.rglob("*.py"))) & TARGETS
        assert imported <= declared, f"{driver.name} imports {sorted(imported - declared)}"
