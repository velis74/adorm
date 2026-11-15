"""Writing generated files and checking them for staleness."""

import os

import pytest

from adorm.codegen import AppSpec, check, CodegenError, Difference, FieldSpec, HEADER, ModelSpec, plan, TypeSpec, write

ID = FieldSpec("id", "id", TypeSpec("int"), db_filled=True, primary_key=True)


def app(label, *names):
    return AppSpec(label, tuple(ModelSpec(f"{label}.{name}", name, "id", (ID,), (), "h") for name in names))


@pytest.fixture
def files():
    return plan([app("shop", "Item"), app("sale", "Offer")])


def states(output, files):
    return {difference.path.name: difference.state for difference in check(output, files)}


def test_plan_names_one_module_per_app_and_the_package(files):
    assert list(files) == ["__init__.py", "shop.py", "sale.py"]
    assert all(content.startswith(HEADER + "\n") for content in files.values())


def test_check_reports_a_missing_directory_as_missing_files(tmp_path, files):
    assert states(tmp_path / "models", files) == {"__init__.py": "missing", "shop.py": "missing", "sale.py": "missing"}


def test_write_then_check_is_clean(tmp_path, files):
    output = tmp_path / "models"
    result = write(output, files)
    assert [path.name for path in result.written] == ["__init__.py", "shop.py", "sale.py"]
    assert result.removed == result.unchanged == []
    assert check(output, files) == []
    assert (output / "shop.py").read_text(encoding="utf-8") == files["shop.py"]


def test_write_leaves_unchanged_files_untouched(tmp_path, files):
    write(tmp_path, files)
    path = tmp_path / "shop.py"
    os.utime(path, (1_000_000, 1_000_000))
    result = write(tmp_path, files)
    assert result.written == []
    assert len(result.unchanged) == 3
    assert path.stat().st_mtime == 1_000_000


def test_check_reports_stale_and_obsolete_files(tmp_path, files):
    write(tmp_path, files)
    shop = tmp_path / "shop.py"
    shop.write_text(shop.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    newer = plan([app("shop", "Item")])
    assert states(tmp_path, newer) == {"shop.py": "stale", "__init__.py": "stale", "sale.py": "obsolete"}
    result = write(tmp_path, newer)
    assert sorted(path.name for path in result.written) == ["__init__.py", "shop.py"]
    assert [path.name for path in result.removed] == ["sale.py"]
    assert not (tmp_path / "sale.py").exists()
    assert check(tmp_path, newer) == []


def test_files_the_generator_did_not_write_are_left_alone(tmp_path, files):
    (tmp_path / "helpers.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    write(tmp_path, files)
    assert check(tmp_path, files) == []
    assert (tmp_path / "helpers.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_write_refuses_to_replace_a_foreign_file_and_writes_nothing(tmp_path, files):
    (tmp_path / "shop.py").write_text("# hand-written\n", encoding="utf-8")
    assert states(tmp_path, files)["shop.py"] == "foreign"
    with pytest.raises(CodegenError, match=r"not overwriting files adorm generate did not write: .*shop\.py"):
        write(tmp_path, files)
    assert not (tmp_path / "__init__.py").exists()
    assert (tmp_path / "shop.py").read_text(encoding="utf-8") == "# hand-written\n"


def test_an_undecodable_file_is_foreign(tmp_path, files):
    (tmp_path / "shop.py").write_bytes(b"\xff\xfe")
    assert states(tmp_path, files)["shop.py"] == "foreign"


def test_files_are_written_with_unix_newlines(tmp_path, files):
    write(tmp_path, files)
    assert b"\r\n" not in (tmp_path / "shop.py").read_bytes()


def test_difference_text(tmp_path):
    assert str(Difference(tmp_path / "a.py", "stale")) == f"{tmp_path / 'a.py'}: stale"


def test_plan_refuses_an_app_that_would_replace_the_package_module():
    with pytest.raises(CodegenError, match="cannot name a module"):
        plan([app("__init__", "Item")])


def test_plan_refuses_modules_that_import_each_others_base_classes():
    def model(label, name, base=None):
        return ModelSpec(f"{label}.{name}", name, "id", (ID,), (), "h", base=base)

    a = AppSpec("a", (model("a", "Root"), model("a", "Leaf", base="b.Middle")))
    b = AppSpec("b", (model("b", "Middle", base="a.Root"),))
    with pytest.raises(CodegenError, match=r"apps a -> b -> a import each other's base classes"):
        plan([a, b])
    c = AppSpec("c", (model("c", "Child", base="a.Root"),))
    assert set(plan([AppSpec("a", (model("a", "Root"),)), c])) == {"__init__.py", "a.py", "c.py"}
