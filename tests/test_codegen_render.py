"""Rendering of model descriptions as Python modules: content, names, values, line splitting and formatter stability."""

import datetime
import decimal
import importlib
import inspect
import subprocess
import sys
import uuid

import pytest

from adorm import ForeignKey, ManyToMany, Model, ReverseForeignKey, ReverseOneToOne
from adorm.codegen import (
    AppSpec,
    FieldSpec,
    HEADER,
    ModelSpec,
    plan,
    RelationSpec,
    render_module,
    render_package,
    TypeSpec,
)
from adorm.codegen.render import can_render_value
from adorm.models import registry

INT = TypeSpec("int")
STR = TypeSpec("str")
ID = FieldSpec("id", "id", INT, db_filled=True, primary_key=True)


def model(label, fields=(), relations=(), **kwargs):
    class_name = kwargs.pop("class_name", label.rpartition(".")[2])
    return ModelSpec(label, class_name, kwargs.pop("pk", "id"), (ID, *fields), tuple(relations), "abcd", **kwargs)


@pytest.fixture
def load(tmp_path, monkeypatch):
    """Writes the files of a package to ``tmp_path/<name>`` and imports it; unregisters its models afterwards."""
    loaded = []

    def load_package(name, files):
        package = tmp_path / name
        package.mkdir()
        for filename, content in files.items():
            (package / filename).write_text(content, encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        module = importlib.import_module(name)
        loaded.append(module)
        return module

    yield load_package
    for module in loaded:
        for cls in module.MODELS:
            registry.unregister(cls._meta.label)
        for name in list(sys.modules):
            if name == module.__name__ or name.startswith(f"{module.__name__}."):
                del sys.modules[name]


def test_module_starts_with_the_header_and_a_docstring():
    source = render_module(AppSpec("shop", (model("shop.Item"),)))
    lines = source.splitlines()
    assert lines[0] == HEADER
    assert lines[1] == '"""adorm models of the Django app ``shop``."""'
    assert source.endswith("\n")
    assert '__all__ = ["Item"]' in source


def test_field_forms():
    fields = (
        FieldSpec("name", "name", STR, constraints={"max_length": 20}),
        FieldSpec("nick", "nick", STR, default=""),
        FieldSpec("born", "born", TypeSpec("datetime.date"), nullable=True),
        FieldSpec("added", "added", TypeSpec("datetime.datetime"), db_filled=True),
        FieldSpec("tags", "tags", TypeSpec("list", (STR,)), default_factory="builtins.list"),
        FieldSpec("code", "code", TypeSpec("uuid.UUID"), default_factory="uuid.uuid4"),
        FieldSpec("data", "data", TypeSpec("typing.Any"), nullable=True),
        FieldSpec("json_", "json", STR, description="Raw text."),
        FieldSpec("level", "level", TypeSpec("typing.Literal", values=("a", "b")), default="a"),
        FieldSpec("rate", "rate", TypeSpec("decimal.Decimal"), default=decimal.Decimal("1.50"), nullable=True),
    )
    source = render_module(AppSpec("shop", (model("shop.Item", fields),)), line_length=120)
    expected = [
        "    id: int | None = None",
        "    name: str = Field(max_length=20)",
        '    nick: str = ""',
        "    born: datetime.date | None = None",
        "    added: datetime.datetime | None = None",
        "    tags: list[str] = Field(default_factory=list)",
        "    code: uuid.UUID = Field(default_factory=uuid.uuid4)",
        "    data: Any = None",
        '    json_: str = Field(alias="json", description="Raw text.")',
        '    level: Literal["a", "b"] = "a"',
        '    rate: decimal.Decimal | None = decimal.Decimal("1.50")',
    ]
    for line in expected:
        assert line in source.splitlines(), line
    for line in ("import datetime", "import decimal", "import uuid", "from typing import Any, Literal"):
        assert line in source.splitlines()


def test_meta_lists_pk_hash_and_orm_names():
    fields = (FieldSpec("copy_", "copy", INT),)
    relations = (RelationSpec("foreign_key", "json_", "json", "shop.Item", column="json_id"),)
    spec = ModelSpec("shop.Item", "Item", "code", (FieldSpec("code", "code", STR), *fields), relations, "ffff")
    source = render_module(AppSpec("shop", (spec,)), line_length=200)
    meta = '_meta = ModelMeta("shop.Item", pk="code", schema_hash="ffff", orm_names={"copy_": "copy", "json_": "json"})'
    assert f"    {meta}" in source


def test_relation_forms():
    relations = (
        RelationSpec("foreign_key", "club", "club", "shop.Club", column="club_id"),
        RelationSpec("foreign_key", "copy_", "copy", "shop.Club", column="copy_id"),
        RelationSpec("reverse_foreign_key", "notes", "notes", "shop.Note", column="item_id"),
        RelationSpec("reverse_one_to_one", "detail", "detail", "shop.Detail", column="item_ptr_id"),
        RelationSpec(
            "many_to_many", "tags", "tags", "shop.Tag", through="shop.T", source_column="a", target_column="b"
        ),
    )
    fields = (FieldSpec("club_id", "club_id", INT), FieldSpec("copy_id", "copy_id", INT))
    source = render_module(AppSpec("shop", (model("shop.Item", fields, relations),)), line_length=120)
    for line in (
        '    club = ForeignKey("shop.Club")',
        '    copy_ = ForeignKey("shop.Club", column="copy_id")',
        '    notes = ReverseForeignKey("shop.Note", column="item_id")',
        '    detail = ReverseOneToOne("shop.Detail", column="item_ptr_id")',
        '    tags = ManyToMany("shop.Tag", through="shop.T", source_column="a", target_column="b")',
        "from adorm import ForeignKey, ManyToMany, Model, ModelMeta, ReverseForeignKey, ReverseOneToOne",
    ):
        assert line in source.splitlines(), line


def test_imported_names_that_clash_with_model_names_get_an_alias():
    models = (
        model("shop.Field", (FieldSpec("born", "born", TypeSpec("datetime.date")),)),
        model("shop.Model"),
        model("shop.datetime", class_name="datetime"),
        model("shop.Literal", (FieldSpec("kind", "kind", TypeSpec("typing.Literal", values=("x",))),)),
    )
    source = render_module(AppSpec("shop", models), line_length=120)
    lines = source.splitlines()
    assert "import datetime as _datetime" in lines
    assert "from typing import Literal as _Literal" in lines
    assert "from adorm import Model as _Model, ModelMeta" in lines
    assert "class Field(_Model):" in lines
    assert "class datetime(_Model):" in lines
    assert "    born: _datetime.date" in lines
    assert '    kind: _Literal["x"]' in lines


def test_bases_in_the_module_and_in_other_modules():
    child = model("shop.Special", base="shop.Item", fields=(FieldSpec("extra", "extra", INT),))
    remote = model("sale.Offer", base="shop.Item")
    source = render_module(AppSpec("shop", (model("shop.Item"), child)))
    assert "class Special(Item):" in source
    other = render_module(AppSpec("sale", (remote,)), classes={"shop.Item": ("shop", "Item")})
    assert "from .shop import Item" in other.splitlines()
    assert "class Offer(Item):" in other


def test_docstrings_are_escaped():
    doc = '\n    A "quoted" \\ backslash.\n\n    Second """ paragraph."\n    '
    source = render_module(AppSpec("shop", (model("shop.Item", doc=doc),)))
    assert '    """A "quoted" \\\\ backslash.' in source
    assert '    Second \\"\\"\\" paragraph."\n    """\n' in source
    single = render_module(AppSpec("shop", (model("shop.Item", doc='Ends with "quotes"'),)))
    assert '    """Ends with "quotes" """' in single
    tree = compile(source, "doc.py", "exec", flags=0x400)  # ast.PyCF_ONLY_AST
    assert inspect.cleandoc(tree.body[3].body[0].value.value) == inspect.cleandoc(doc)


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (None, "None"),
        (True, "True"),
        (3, "3"),
        (1.5, "1.5"),
        (float("inf"), 'float("inf")'),
        ("plain", '"plain"'),
        ('say "hi"', "'say \"hi\"'"),
        ("it's", '"it\'s"'),
        ("both ' and \"", '"both \' and \\""'),
        ("tab\tnew\nline", '"tab\\tnew\\nline"'),
        (b"raw", 'b"raw"'),
        (decimal.Decimal("0.10"), 'decimal.Decimal("0.10")'),
        (uuid.UUID(int=1), 'uuid.UUID("00000000-0000-0000-0000-000000000001")'),
        (datetime.date(2026, 1, 2), "datetime.date(2026, 1, 2)"),
        (datetime.time(8, 30), "datetime.time(8, 30)"),
        (datetime.timedelta(days=1), "datetime.timedelta(days=1)"),
        (datetime.datetime(2026, 1, 2, 3, 4), "datetime.datetime(2026, 1, 2, 3, 4)"),
        (
            datetime.datetime(2026, 1, 2, tzinfo=datetime.UTC),
            "datetime.datetime(2026, 1, 2, 0, 0, tzinfo=datetime.UTC)",
        ),
        ([1, "a"], '[1, "a"]'),
        ((1,), "(1,)"),
        ({"a": [1]}, '{"a": [1]}'),
    ],
)
def test_default_values(value, text):
    assert can_render_value(value)
    field = FieldSpec("value", "value", TypeSpec("typing.Any"), default=value)
    source = render_module(AppSpec("shop", (model("shop.Item", (field,)),)), line_length=200)
    assert f"    value: Any = {text}" in source.splitlines()
    namespace = {"datetime": datetime, "decimal": decimal, "uuid": uuid}
    assert eval(text, namespace) == value  # noqa: S307 - the text is a literal this test wrote


@pytest.mark.parametrize(
    "value",
    [object(), {1: 2}, datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone(datetime.timedelta(hours=1)))],
)
def test_values_that_cannot_be_written(value):
    assert not can_render_value(value)


def test_long_calls_are_split_one_argument_per_line():
    relation = RelationSpec(
        "many_to_many",
        "tags",
        "tags",
        "shop.Tag",
        through="shop.Item_tags",
        source_column="item_id",
        target_column="tag_id",
    )
    source = render_module(AppSpec("shop", (model("shop.Item", relations=(relation,)),)), line_length=60)
    assert (
        "    tags = ManyToMany(\n"
        '        "shop.Tag",\n'
        '        through="shop.Item_tags",\n'
        '        source_column="item_id",\n'
        '        target_column="tag_id",\n'
        "    )\n"
    ) in source


def test_long_descriptions_are_split_into_implicit_concatenation():
    text = "A long description of the field that does not fit on one line of the generated module at all."
    field = FieldSpec("name", "name", STR, description=text, constraints={"max_length": 9})
    source = render_module(AppSpec("shop", (model("shop.Item", (field,)),)), line_length=60)
    assert all(len(line) <= 60 for line in source.splitlines() if not line.startswith("#"))
    assert "        description=(\n" in source
    joined = eval(source.split("description=")[1].split("),")[0] + ")")  # noqa: S307 - the text is a literal
    assert joined == text


def test_long_literals_become_type_aliases():
    values = tuple(f"choice_number_{n}" for n in range(6))
    field = FieldSpec("kind", "kind", TypeSpec("typing.Literal", values=values), default="choice_number_0")
    nullable = FieldSpec("other", "other", TypeSpec("typing.Literal", values=values), nullable=True)
    source = render_module(AppSpec("shop", (model("shop.Item", (field, nullable)),)), line_length=60)
    assert "ItemKind = Literal[\n" in source
    assert '    "choice_number_0",\n' in source
    assert '    kind: ItemKind = "choice_number_0"' in source
    assert "    other: ItemKind | None = None" in source
    assert all(len(line) <= 60 for line in source.splitlines() if not line.startswith("#"))


def test_long_imports_and_meta_are_split():
    relations = tuple(
        RelationSpec(kind, f"r{n}", f"r{n}", "shop.X", column="x_id")
        for n, kind in enumerate(["foreign_key", "reverse_foreign_key", "reverse_one_to_one"])
    )
    fields = (
        FieldSpec("x_id", "x_id", INT),
        FieldSpec("model_dump_", "model_dump", INT),
        FieldSpec("json_", "json", INT),
        FieldSpec("copy_", "copy", INT),
    )
    source = render_module(AppSpec("shop", (model("shop.Item", fields, relations),)), line_length=50)
    assert "from adorm import (\n    ForeignKey,\n" in source
    assert "        orm_names={\n" in source


def test_package_imports_every_module_and_lists_models():
    apps = [AppSpec("shop", (model("shop.Item"),)), AppSpec("sale", (model("sale.Offer"), model("sale.Deal")))]
    source = render_package(apps)
    lines = source.splitlines()
    assert lines[0] == HEADER
    assert "from adorm import Model" in lines
    assert "from . import sale, shop" in lines
    assert "MODELS: tuple[type[Model], ...] = (shop.Item, sale.Offer, sale.Deal)" in lines


def test_package_of_one_model_is_a_one_tuple():
    assert "= (shop.Item,)" in render_package([AppSpec("shop", (model("shop.Item"),))])


def test_rendered_modules_import_and_behave(load):
    item = model(
        "rendertesta.Item",
        (
            FieldSpec("name", "name", STR, constraints={"max_length": 5}),
            FieldSpec("json_", "json", STR, default=""),
            FieldSpec("level", "level", TypeSpec("typing.Literal", values=("a", "b")), default="a"),
            FieldSpec("owner_id", "owner_id", INT, nullable=True),
        ),
        (
            RelationSpec("foreign_key", "owner", "owner", "rendertestb.Owner", column="owner_id"),
            RelationSpec("reverse_one_to_one", "special", "special", "rendertesta.Special", column="item_ptr_id"),
        ),
    )
    special = ModelSpec(
        "rendertesta.Special",
        "Special",
        "item_ptr_id",
        (FieldSpec("item_ptr_id", "item_ptr_id", INT, db_filled=True, primary_key=True),),
        (RelationSpec("foreign_key", "item_ptr", "item_ptr", "rendertesta.Item", column="item_ptr_id"),),
        "1234",
        base="rendertesta.Item",
    )
    owner = model(
        "rendertestb.Owner",
        relations=(RelationSpec("reverse_foreign_key", "items", "items", "rendertesta.Item", "owner_id"),),
    )
    remote = model("rendertestb.Featured", base="rendertesta.Item")
    apps = (AppSpec("rendertesta", (item, special)), AppSpec("rendertestb", (owner, remote)))
    package = load("rendered_pkg", plan(apps))
    module = importlib.import_module("rendered_pkg.rendertesta")
    assert [cls.__name__ for cls in package.MODELS] == ["Item", "Special", "Owner", "Featured"]
    assert registry.get("rendertesta.Item") is module.Item
    assert issubclass(module.Special, module.Item)
    assert issubclass(package.rendertestb.Featured, module.Item)
    assert isinstance(module.Item.owner, ForeignKey)
    assert isinstance(module.Item.special, ReverseOneToOne)
    assert isinstance(package.rendertestb.Owner.items, ReverseForeignKey)
    instance = module.Item(name="abc", json="text")
    assert instance.json_ == "text"
    assert instance.model_dump()["json"] == "text"
    assert "json" in module.Item.model_json_schema()["properties"]
    assert module.Item._meta.orm_name("json_") == "json"
    assert module.Item._meta.schema_hash == "abcd"
    with pytest.raises(ValueError, match="max_length|at most"):
        module.Item(name="toolong")
    assert issubclass(module.Item, Model)
    assert ManyToMany is not None


def test_field_names_that_clash_with_imported_names_get_an_alias(load):
    when = TypeSpec("datetime.datetime")
    code = TypeSpec("uuid.UUID")
    price = TypeSpec("decimal.Decimal")
    fields = (
        FieldSpec("datetime", "datetime", when, nullable=True),
        FieldSpec("when", "when", when),
        FieldSpec("uuid", "uuid", code, default_factory="uuid.uuid4"),
        FieldSpec("other", "other", code, nullable=True),
        FieldSpec("decimal", "decimal", price, default=decimal.Decimal("1.5")),
        FieldSpec("cost", "cost", price, nullable=True),
        FieldSpec("Field", "Field", STR, default=""),
        FieldSpec("label", "label", STR, constraints={"max_length": 5}),
        FieldSpec("owner_id", "owner_id", INT, nullable=True),
    )
    relations = (
        RelationSpec("foreign_key", "ForeignKey", "ForeignKey", "shadowing.Item", column="owner_id"),
        RelationSpec("reverse_foreign_key", "items", "items", "shadowing.Item", column="owner_id"),
    )
    files = plan([AppSpec("shadowing", (model("shadowing.Item", fields, relations),))], line_length=120)
    lines = files["shadowing.py"].splitlines()
    assert "import datetime as _datetime" in lines
    assert "import uuid as _uuid" in lines
    assert "import decimal as _decimal" in lines
    assert "from pydantic import Field as _Field" in lines
    assert "    when: _datetime.datetime" in lines
    package = load("shadowing_pkg", files)
    item = package.shadowing.Item(when=datetime.datetime(2026, 1, 1), label="abc")
    assert item.decimal == decimal.Decimal("1.5")
    assert isinstance(item.uuid, uuid.UUID)
    assert isinstance(package.shadowing.Item.ForeignKey, ForeignKey)


@pytest.mark.parametrize("line_length", [60, 88, 120])
def test_rendered_modules_are_stable_under_ruff_format(tmp_path, line_length):
    values = tuple(f"choice_number_{n}" for n in range(8))
    fields = (
        FieldSpec("name", "name", STR, constraints={"max_length": 20, "pattern": r"^\w+$"}),
        FieldSpec("model_dump_", "model_dump", TypeSpec("bool"), default=False, description="x " * 60),
        FieldSpec("kind", "kind", TypeSpec("typing.Literal", values=values), default="choice_number_0"),
        FieldSpec("rate", "rate", TypeSpec("decimal.Decimal"), constraints={"max_digits": 8, "decimal_places": 2}),
        FieldSpec("tags", "tags", TypeSpec("list", (STR.with_nullable(),)), default_factory="builtins.list"),
        FieldSpec("quote", "quote", STR, default='He said "it\'s"'),
    )
    relations = (
        RelationSpec(
            "many_to_many",
            "copy_",
            "copy",
            "shop.Tag",
            through="shop.Item_tags",
            source_column="item_id",
            target_column="tag_id",
        ),
        RelationSpec("reverse_one_to_one", "detail", "detail", "shop.Detail", column="item_ptr_id"),
    )
    spec = model("shop.Item", fields, relations, doc="An item.\n\nSold in the shop.")
    files = plan([AppSpec("shop", (spec, model("shop.Tag")))], line_length=line_length)
    for name, content in files.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    result = subprocess.run(  # noqa: S603 - fixed command on files this test wrote
        [
            sys.executable,
            "-m",
            "ruff",
            "format",
            "--isolated",
            "--check",
            f"--line-length={line_length}",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    long_lines = [
        line
        for content in files.values()
        for line in content.splitlines()
        if len(line) > line_length and line != HEADER and not line.startswith('"""')
    ]
    assert long_lines == []
