import json
import re

from pathlib import Path

import pytest

import adorm

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


def test_names_in_reference_tables_are_not_headings():
    assert not has_reference_heading("QuerySet", reference_text())


def test_crosslink_project_names_are_overridable():
    match = re.search(r"projects:\s*\{(.*?)\}", CONFIG.read_text(encoding="utf-8"), re.DOTALL)
    names = re.findall(r"^\s*(\w[\w-]*):", match.group(1), re.MULTILINE)
    assert names
    assert all(re.fullmatch(r"[a-z0-9_]+", n) for n in names), "use underscore names for CROSSLINKS_BASE_<NAME>"


def test_docs_package_builds_with_vitepress():
    package = json.loads((DOCS / "package.json").read_text(encoding="utf-8"))
    assert "docs:build" in package["scripts"]
    assert "vitepress-plugin-crosslinks" in package["devDependencies"]
