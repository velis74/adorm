import importlib
import json
import re

from pathlib import Path

import pytest

import adorm
import adorm.codec
import adorm.codegen
import adorm.codegen.django
import adorm.drivers
import adorm.events
import adorm.executor
import adorm.models
import adorm.operations
import adorm.query
import adorm.schema
import adorm.settings

DOCS = Path(__file__).parent.parent / "docs"
CONFIG = DOCS / ".vitepress" / "config.ts"
LINK = re.compile(r"""link:\s*'(/[^'#]*)(?:#[^']*)?'""")
FRONTMATTER_LINK = re.compile(r"^\s*link:\s*(/[^\s#]*)", re.MULTILINE)


def internal_links() -> list[str]:
    return sorted(set(LINK.findall(CONFIG.read_text(encoding="utf-8"))))


def landing_page_links() -> list[str]:
    return sorted(set(FRONTMATTER_LINK.findall((DOCS / "index.md").read_text(encoding="utf-8"))))


def has_reference_heading(name: str, reference: str) -> bool:
    """Whether `reference` has a heading for the public name, e.g. ``### `adorm.name` ``."""
    pattern = rf"^#{{2,4}} `(adorm\.)?{re.escape(name)}(\(.*\))?`"
    return re.search(pattern, reference, re.MULTILINE) is not None


def reference_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in (DOCS / "ref").rglob("*.md"))


def page_for(link: str) -> Path:
    """Markdown source VitePress serves for an internal link."""
    path = link.lstrip("/")
    if not path or path.endswith("/"):
        return DOCS / path / "index.md"
    return DOCS / f"{path.removesuffix('.html')}.md"


def test_config_has_links():
    assert "/ref/" in internal_links()


@pytest.mark.parametrize("link", internal_links())
def test_sidebar_and_nav_links_have_pages(link):
    assert page_for(link).is_file(), f"{link} has no page"


def test_landing_page_has_links():
    assert "/ref/" in landing_page_links()


@pytest.mark.parametrize("link", landing_page_links())
def test_landing_page_links_have_pages(link):
    assert page_for(link).is_file(), f"{link} has no page"


@pytest.mark.parametrize(
    ("link", "expected"),
    [("/", "index.md"), ("/ref/", "ref/index.md"), ("/topics/concepts", "topics/concepts.md"), ("/a.html", "a.md")],
)
def test_page_for(link, expected):
    assert page_for(link) == DOCS / expected


def test_every_page_is_linked():
    linked = {page_for(link) for link in internal_links()}
    pages = {p for p in DOCS.rglob("*.md") if "node_modules" not in p.parts and ".vitepress" not in p.parts}
    pages -= {DOCS / "readme.md"}
    assert pages - linked == set()


@pytest.mark.parametrize("name", adorm.__all__)
def test_public_names_are_documented(name):
    assert has_reference_heading(name, reference_text()), f"no reference heading for {name}"


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("### `adorm.atomic`", True),
        ("## `atomic`", True),
        ("#### `adorm.atomic(using=None)`", True),
        ("| Sessions | `atomic` |", False),
        ("### `adorm.atomic_block`", False),
        ("Use `adorm.atomic` here.", False),
    ],
)
def test_has_reference_heading(reference, expected):
    assert has_reference_heading("atomic", reference) is expected


@pytest.mark.parametrize(
    ("module", "name"),
    [
        (m.__name__, n)
        for m in (
            adorm.codec,
            adorm.codegen,
            adorm.drivers,
            adorm.events,
            adorm.operations,
            adorm.schema,
            adorm.settings,
        )
        for n in m.__all__
        if not (m is adorm.settings and n in adorm.__all__)
    ],
)
def test_submodule_public_names_are_documented(module, name):
    pattern = rf"^#{{2,4}} `{re.escape(module)}\.{re.escape(name)}(\(.*\))?`"
    assert re.search(pattern, reference_text(), re.MULTILINE), f"no reference heading for {module}.{name}"


SUBMODULES = (
    "adorm.cli",
    "adorm.codegen.files",
    "adorm.codegen.render",
    "adorm.codegen.spec",
    "adorm.drivers.asyncpg.compiler",
    "adorm.drivers.base",
    "adorm.drivers.sql",
    "adorm.exceptions",
    "adorm.executor.debug",
    "adorm.executor.pool",
    "adorm.executor.sessions",
    "adorm.executor.slot",
    "adorm.executor.transactions",
    "adorm.models.base",
    "adorm.models.managers",
    "adorm.models.meta",
    "adorm.models.registry",
    "adorm.models.relations",
    "adorm.porting",
    "adorm.query.expressions",
    "adorm.query.lookups",
    "adorm.query.query",
    "adorm.query.queryset",
)


def documented_paths(module: str, name: str) -> list[str]:
    """The dotted paths under which a public name may be documented: its own module, or a package or the top-level
    package that re-exports the same object."""
    obj = getattr(importlib.import_module(module), name)
    paths = [module]
    parts = module.split(".")
    for end in range(len(parts) - 1, 0, -1):
        parent = importlib.import_module(".".join(parts[:end]))
        if name in getattr(parent, "__all__", ()) and getattr(parent, name) is obj:
            paths.append(parent.__name__)
    return paths


@pytest.mark.parametrize(
    ("module", "name"),
    [(m, n) for m in SUBMODULES for n in importlib.import_module(m).__all__],
)
def test_every_submodule_public_name_is_documented(module, name):
    text = reference_text()
    paths = documented_paths(module, name)
    found = any(
        re.search(rf"^#{{2,4}} `{re.escape(path)}\.{re.escape(name)}(\(.*\))?`", text, re.MULTILINE)
        or re.search(rf"^#{{2,4}} .*`{re.escape(path)}\.{re.escape(name)}`", text, re.MULTILINE)
        for path in paths
    )
    assert found, f"no reference heading for {module}.{name} (looked for {', '.join(paths)})"


def test_every_public_module_is_checked():
    modules = {
        ".".join(("adorm", *p.relative_to(DOCS.parent / "adorm").with_suffix("").parts))
        for p in (DOCS.parent / "adorm").rglob("*.py")
        if p.name not in {"__init__.py", "__main__.py"}
    }
    checked = set(SUBMODULES) | {
        "adorm.codec",
        "adorm.codegen.django",
        "adorm.events",
        "adorm.operations",
        "adorm.schema",
        "adorm.settings",
    }
    assert modules - checked == set()


@pytest.mark.parametrize("name", adorm.query.__all__)
def test_query_names_are_documented(name):
    # a name adorm re-exports is documented as adorm.<name>
    module = "adorm" if name in adorm.__all__ else "adorm.query"
    pattern = rf"^#{{2,4}} `{re.escape(module)}\.{re.escape(name)}(\(.*\))?`"
    assert re.search(pattern, reference_text(), re.MULTILINE), f"no reference heading for adorm.query.{name}"


@pytest.mark.parametrize("name", adorm.models.__all__)
def test_model_names_are_documented(name):
    module = "adorm" if name in adorm.__all__ else "adorm.models"
    pattern = rf"^#{{2,4}} `{re.escape(module)}\.{re.escape(name)}(\(.*\))?`"
    assert re.search(pattern, reference_text(), re.MULTILINE), f"no reference heading for adorm.models.{name}"


@pytest.mark.parametrize("name", adorm.executor.__all__)
def test_executor_names_are_documented(name):
    module = "adorm" if name in adorm.__all__ else "adorm.executor"
    pattern = rf"^#{{2,4}} `{re.escape(module)}\.{re.escape(name)}(\(.*\))?`"
    assert re.search(pattern, reference_text(), re.MULTILINE), f"no reference heading for adorm.executor.{name}"


def test_crosslink_project_names_are_overridable():
    match = re.search(r"projects:\s*\{(.*?)\}", CONFIG.read_text(encoding="utf-8"), re.DOTALL)
    names = re.findall(r"^\s*(\w[\w-]*):", match.group(1), re.MULTILINE)
    assert names
    assert all(re.fullmatch(r"[a-z0-9_]+", n) for n in names), "use underscore names for CROSSLINKS_BASE_<NAME>"


def test_docs_package_builds_with_vitepress():
    package = json.loads((DOCS / "package.json").read_text(encoding="utf-8"))
    assert "docs:build" in package["scripts"]
    assert "vitepress-plugin-crosslinks" in package["devDependencies"]


HEADING = re.compile(r"^#{1,6} (.+?)(?: \{#([^}]+)\})?\s*$", re.MULTILINE)
FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
FRAGMENT_LINK = re.compile(r"\]\((/[^)#\s]*)?#([^)\s]+)\)")
SLUG_SEPARATORS = re.compile(r"""[\s~`!@#$%^&*()\-_+=\[\]{}|\;:"'“”‘’<>,.?/]+""")


def slugify(text: str) -> str:
    """The heading id VitePress generates."""
    slug = SLUG_SEPARATORS.sub("-", text).strip("-").lower()
    return f"_{slug}" if slug[:1].isdigit() else slug


def anchors(page: Path) -> set[str]:
    found: set[str] = set()
    for text, explicit in HEADING.findall(FENCE.sub("", page.read_text(encoding="utf-8"))):
        slug = explicit or slugify(text)
        candidate, number = slug, 0
        while candidate in found:
            number += 1
            candidate = f"{slug}-{number}"
        found.add(candidate)
    return found


def fragment_links() -> list[tuple[str, str, str]]:
    pages = [p for p in DOCS.rglob("*.md") if "node_modules" not in p.parts and ".vitepress" not in p.parts]
    links = []
    for page in pages:
        for target, fragment in FRAGMENT_LINK.findall(FENCE.sub("", page.read_text(encoding="utf-8"))):
            links.append((str(page.relative_to(DOCS)), target, fragment))
    return sorted(set(links))


@pytest.mark.parametrize(("source", "target", "fragment"), fragment_links())
def test_fragment_links_point_at_headings(source, target, fragment):
    page = page_for(target) if target else DOCS / source
    assert fragment in anchors(page), f"{source}: {target}#{fragment} has no heading"


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("`adorm.executor.Router(pools=())`", "adorm-executor-router-pools"),
        ("`is_refusal(exc)`", "is-refusal-exc"),
        ("`async close()`", "async-close"),
    ],
)
def test_slugify(heading, expected):
    assert slugify(heading) == expected


@pytest.mark.parametrize("name", adorm.codegen.django.__all__)
def test_codegen_django_names_are_documented(name):
    pattern = rf"^#{{2,4}} `adorm\.codegen\.django\.{re.escape(name)}\("
    assert re.search(pattern, reference_text(), re.MULTILINE), f"no reference heading for adorm.codegen.django.{name}"


def test_cli_main_is_documented():
    assert re.search(r"^### `adorm\.cli\.main\(", reference_text(), re.MULTILINE)
