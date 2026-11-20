"""The documentation of own expressions: the how-to recipe is listed and covers each way of defining one, the
reference documents the registration of translations, and no page states the UUID limits the drivers no longer
have."""

import re

from pathlib import Path

DOCS = Path(__file__).parent.parent / "docs"
RECIPE = DOCS / "howto" / "own-expressions.md"
PYTHON_FENCE = re.compile(r"^( *)```(?:python|py)\b[^\n]*\n(.*?)^\1```", re.MULTILINE | re.DOTALL)


def pages() -> list[Path]:
    return [p for p in DOCS.rglob("*.md") if "node_modules" not in p.parts and ".vitepress" not in p.parts]


def recipe_code() -> str:
    return "\n".join(match.group(2) for match in PYTHON_FENCE.finditer(RECIPE.read_text(encoding="utf-8")))


def test_the_recipe_is_listed_in_the_how_to_index_and_the_sidebar():
    assert RECIPE.is_file()
    assert "(./own-expressions)" in (DOCS / "howto" / "index.md").read_text(encoding="utf-8")
    assert "'/howto/own-expressions'" in (DOCS / ".vitepress" / "config.ts").read_text(encoding="utf-8")


def test_the_recipe_shows_every_way_of_defining_an_own_expression():
    code = recipe_code()
    assert re.search(r"class \w+\(Func\):", code), "an own function"
    assert re.search(r"^\s+template\s*=", code, re.MULTILINE), "a function with one template"
    assert re.search(r"^\s+templates\s*=\s*\{", code, re.MULTILINE), "a function with per-vendor templates"
    assert "register_lookup" in code, "an own lookup"
    assert "register_translation" in code, "a translation for the Django drivers"
    text = RECIPE.read_text(encoding="utf-8")
    assert "UUID4" in text, "how UUID4 uses the mechanism"
    assert "UUID7" in text, "how UUID7 uses the mechanism"


def test_the_reference_documents_register_translation():
    reference = "\n".join(p.read_text(encoding="utf-8") for p in (DOCS / "ref").rglob("*.md"))
    assert re.search(r"^#{2,4} `adorm\.drivers\.django\.register_translation(\(.*\))?`", reference, re.MULTILINE)


def test_the_reference_documents_templates_of_functions_and_lookups():
    expressions = (DOCS / "ref" / "expressions.md").read_text(encoding="utf-8")
    lookups = (DOCS / "ref" / "lookups.md").read_text(encoding="utf-8")
    for attribute in ("template", "templates", "function"):
        assert f"`{attribute}`" in expressions, f"Func.{attribute} is not described"
    assert "`template`" in lookups


def test_no_page_states_the_old_uuid_limits():
    stale = [
        re.compile(r"UUID4`? and `?UUID7`? need Django 6"),
        re.compile(r"UUID7`? needs PostgreSQL 18"),
        re.compile(r"With an older Django they are not among the"),
    ]
    found = [
        f"{page.relative_to(DOCS)}: {pattern.pattern}"
        for page in pages()
        for pattern in stale
        if pattern.search(page.read_text(encoding="utf-8"))
    ]
    assert found == []
