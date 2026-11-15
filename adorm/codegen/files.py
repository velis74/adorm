"""The generated files: planning them from `AppSpec` values, writing them and checking them for staleness."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from adorm.codegen.render import HEADER, render_module, render_package
from adorm.codegen.spec import AppSpec, CodegenError

__all__ = ["Difference", "SyncResult", "check", "plan", "write"]


def _check_cycles(apps: Sequence[AppSpec], classes: Mapping[str, tuple[str, str]]) -> None:
    """Raise `CodegenError` when modules import each other's classes as bases in a cycle."""
    imports: dict[str, set[str]] = {app.label: set() for app in apps}
    for app in apps:
        for model in app.models:
            if model.base is not None and model.base in classes and classes[model.base][0] != app.label:
                imports[app.label].add(classes[model.base][0])
    done: set[str] = set()

    def visit(label: str, path: list[str]) -> None:
        if label in path:
            cycle = path[path.index(label) :] + [label]
            raise CodegenError(f"the modules of apps {' -> '.join(cycle)} import each other's base classes")
        if label in done:
            return
        for imported in sorted(imports.get(label, ())):
            visit(imported, [*path, label])
        done.add(label)

    for label in imports:
        visit(label, [])


def plan(apps: Sequence[AppSpec], *, line_length: int = 88, source: str = "Django") -> dict[str, str]:
    """The generated files by name: ``__init__.py`` and ``<app_label>.py`` per app.

    Raises `CodegenError` when two apps share a label or their modules would import each other's base classes in a
    cycle (a model of app A derives from one of app B and a model of app B from one of app A).
    """
    classes = {model.label: (app.label, model.class_name) for app in apps for model in app.models}
    _check_cycles(apps, classes)
    files = {"__init__.py": render_package(apps, line_length=line_length)}
    for app in apps:
        if f"{app.label}.py" in files:
            raise CodegenError(f"the app label {app.label!r} cannot name a module")
        files[f"{app.label}.py"] = render_module(app, classes=classes, line_length=line_length, source=source)
    return files


def _is_generated(path: Path) -> bool:
    try:
        with path.open(encoding="utf-8") as handle:
            return handle.readline().rstrip("\r\n") == HEADER
    except (OSError, UnicodeDecodeError):
        return False


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


@dataclass(frozen=True)
class Difference:
    """A file that does not match the generated content.

    ``state``: ``"missing"`` (not on disk), ``"stale"`` (other content), ``"obsolete"`` (a generated file no longer
    generated) or ``"foreign"`` (a file the generator would replace that it did not write).
    """

    path: Path
    state: Literal["missing", "stale", "obsolete", "foreign"]

    def __str__(self) -> str:
        return f"{self.path}: {self.state}"


def check(output: Path, files: Mapping[str, str]) -> list[Difference]:
    """The differences between the directory ``output`` and the generated ``files``; empty when it is up to date."""
    output = Path(output)
    differences = []
    for name, content in files.items():
        path = output / name
        current = _read(path)
        if current is None:
            differences.append(Difference(path, "missing"))
        elif current != content.encode("utf-8"):
            differences.append(Difference(path, "stale" if _is_generated(path) else "foreign"))
    if output.is_dir():
        for path in sorted(output.glob("*.py")):
            if path.name not in files and _is_generated(path):
                differences.append(Difference(path, "obsolete"))
    return differences


@dataclass
class SyncResult:
    """What `write()` did: the files it wrote, removed and left unchanged."""

    written: list[Path] = field(default_factory=list)
    removed: list[Path] = field(default_factory=list)
    unchanged: list[Path] = field(default_factory=list)


def write(output: Path, files: Mapping[str, str]) -> SyncResult:
    """Bring the directory ``output`` up to date with ``files``: write the files that differ, remove the generated
    files no longer generated, leave the rest untouched.

    Raises `CodegenError`, before writing anything, when a file to write exists and was not generated.
    """
    output = Path(output)
    differences = check(output, files)
    foreign = [str(difference.path) for difference in differences if difference.state == "foreign"]
    if foreign:
        raise CodegenError(f"not overwriting files adorm generate did not write: {', '.join(foreign)}")
    output.mkdir(parents=True, exist_ok=True)
    result = SyncResult()
    changed = {difference.path: difference.state for difference in differences}
    for name, content in files.items():
        path = output / name
        if path in changed:
            path.write_text(content, encoding="utf-8", newline="\n")
            result.written.append(path)
        else:
            result.unchanged.append(path)
    for path, state in changed.items():
        if state == "obsolete":
            path.unlink()
            result.removed.append(path)
    return result
