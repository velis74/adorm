"""The how-to recipe docs/howto/own-expressions.md on the SQL drivers: its class definitions run once, its queries on
each driver, and the values the queries give are those the recipe states."""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
import uuid

from pathlib import Path
from typing import Any

import pytest

from tests.clubmodels.clubs import Member

pytestmark = pytest.mark.django_db(transaction=True)

RECIPE = Path(__file__).parent.parent.parent / "docs" / "howto" / "own-expressions.md"
PYTHON_FENCE = re.compile(r"^( *)```(?:python|py)\b[^\n]*\n(.*?)^\1```", re.MULTILINE | re.DOTALL)


def blocks() -> list[Any]:
    """The compiled python blocks of the recipe, in order; a query block has top-level ``await``."""
    text = RECIPE.read_text(encoding="utf-8")
    return [
        compile(textwrap.dedent(match.group(2)), str(RECIPE), "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        for match in PYTHON_FENCE.finditer(text)
    ]


def is_query(code: Any) -> bool:
    return bool(code.co_flags & inspect.CO_COROUTINE)


_definitions: dict[str, Any] = {}


def definitions() -> dict[str, Any]:
    """The namespace of the recipe's definition blocks, run once: the classes register themselves globally."""
    if not _definitions:
        namespace: dict[str, Any] = {"__name__": "own_expressions_recipe"}
        for code in blocks():
            if not is_query(code):
                exec(code, namespace)  # noqa: S102 - the recipe's own code
        _definitions.update(namespace)
    return _definitions


@pytest.fixture
def sql_driver(conformance_driver):
    if conformance_driver.name == "memory":
        pytest.skip("the memory driver has no SQL")
    return conformance_driver.name


@pytest.mark.usefixtures("club_data", "sql_driver", "router")
async def test_the_recipe_s_queries_give_the_values_it_states():
    namespace = {**definitions(), "Member": Member}
    for code in blocks():
        if is_query(code):
            await eval(code, namespace)  # noqa: S307 - the recipe's own code
    assert namespace["monograms"] == [("Ana", "AN"), ("Eva", "EZ")]
    assert namespace["months"] == ["1970-01"]
    assert namespace["reversed_names"] == ["anA"]
    assert namespace["same_start"] == ["Gaja"]
    assert namespace["short"] == ["Ana", "Eva"]
    assert namespace["masked"] == ["a***@example.com"]
    (code,) = namespace["codes"]
    assert isinstance(code, uuid.UUID)
    assert code.version == 7
