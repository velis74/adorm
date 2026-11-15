"""The ``adorm`` command line: ``adorm generate`` and its check mode."""

import os
import subprocess
import sys
import textwrap

from pathlib import Path

import pytest

from adorm.cli import _import_path, main
from adorm.codegen import HEADER

ROOT = Path(__file__).parent.parent


def run(*args, env=None, cwd=ROOT):
    environ = {key: value for key, value in os.environ.items() if key != "DJANGO_SETTINGS_MODULE"}
    environ.update(env or {})
    return subprocess.run(  # noqa: S603 - the interpreter running the suite
        [sys.executable, "-m", "adorm", *args], capture_output=True, text=True, cwd=cwd, env=environ, check=False
    )


def test_generate_writes_then_check_passes(tmp_path, capsys):
    output = tmp_path / "models"
    assert main(["generate", "-o", str(output), "library"]) == 0
    out, err = capsys.readouterr()
    assert f"wrote {output / 'library.py'}" in out
    assert "2 written, 0 removed, 0 unchanged." in out
    assert "warning: library.Book.reviews: GenericRelation is not generated" in err
    assert (output / "library.py").read_text(encoding="utf-8").startswith(HEADER)
    assert main(["generate", "--check", "-o", str(output), "library"]) == 0
    assert "2 generated files are up to date." in capsys.readouterr().out
    assert main(["generate", "-o", str(output), "library"]) == 0
    assert "0 written, 0 removed, 2 unchanged." in capsys.readouterr().out


def test_check_fails_on_stale_missing_and_obsolete_files(tmp_path, capsys):
    output = tmp_path / "models"
    main(["generate", "-o", str(output), "library", "auth"])
    library = output / "library.py"
    library.write_text(library.read_text(encoding="utf-8").replace("max_length=100", "max_length=99"), encoding="utf-8")
    (output / "__init__.py").unlink()
    capsys.readouterr()
    assert main(["generate", "--check", "-o", str(output), "library"]) == 1
    err = capsys.readouterr().err
    assert f"{output / '__init__.py'}: missing" in err
    assert f"{library}: stale" in err
    assert f"{output / 'auth.py'}: obsolete" in err
    assert "run adorm generate without --check" in err
    assert "max_length=99" in library.read_text(encoding="utf-8"), "check mode writes nothing"


def test_options_change_the_output(tmp_path, capsys):
    narrow, wide = tmp_path / "narrow", tmp_path / "wide"
    main(["generate", "-o", str(narrow), "--line-length", "60", "library"])
    main(["generate", "-o", str(wide), "--line-length", "200", "-e", "library.Note", "library"])
    capsys.readouterr()
    assert max(len(line) for line in (narrow / "library.py").read_text(encoding="utf-8").splitlines()[1:]) <= 60
    assert "class Note(" not in (wide / "library.py").read_text(encoding="utf-8")
    assert main(["generate", "--check", "-o", str(narrow), "library"]) == 1, "the line length is part of the output"


def test_unknown_app_is_an_error(tmp_path, capsys):
    assert main(["generate", "-o", str(tmp_path), "shop"]) == 2
    assert "adorm generate: no installed Django app has the label 'shop'" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_foreign_file_is_an_error(tmp_path, capsys):
    (tmp_path / "library.py").write_text("# mine\n", encoding="utf-8")
    assert main(["generate", "-o", str(tmp_path), "library"]) == 2
    assert "not overwriting files adorm generate did not write" in capsys.readouterr().err
    assert main(["generate", "--check", "-o", str(tmp_path), "library"]) == 1
    assert f"{tmp_path / 'library.py'}: foreign" in capsys.readouterr().err


def test_usage_errors_exit_with_status_2(capsys):
    with pytest.raises(SystemExit) as raised:
        main(["generate"])
    assert raised.value.code == 2
    assert "--output" in capsys.readouterr().err
    with pytest.raises(SystemExit) as raised:
        main([])
    assert raised.value.code == 2


def test_an_unexpected_error_exits_with_status_2(tmp_path, monkeypatch, capsys):
    import adorm.codegen.django

    def fail(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(adorm.codegen.django, "introspect", fail)
    assert main(["generate", "--check", "-o", str(tmp_path), "library"]) == 2
    assert "adorm generate: RuntimeError: boom" in capsys.readouterr().err


def test_a_forward_relation_to_an_app_not_generated_is_a_warning(tmp_path, capsys):
    assert main(["generate", "-o", str(tmp_path), "library"]) == 0
    err = capsys.readouterr().err
    assert "warning: library.Author.account: auth.User not generated, so the relation fails when used" in err


def test_missing_django_is_an_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "django", None)
    monkeypatch.delitem(sys.modules, "adorm.codegen.django", raising=False)
    assert main(["generate", "-o", str(tmp_path)]) == 2
    assert "adorm generate needs Django: install adorm[django]" in capsys.readouterr().err


def test_module_entry_point_checks_the_checked_in_models():
    result = run(
        "generate", "--settings", "tests.djangoproject.settings", "--check", "-o", "tests/generated", "--line-length",
        "120", "auth", "library",
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert "3 generated files are up to date." in result.stdout


def test_settings_are_required():
    result = run("generate", "-o", "unused")
    assert result.returncode == 2
    assert "give --settings or set DJANGO_SETTINGS_MODULE" in result.stderr


def test_unimportable_settings_are_an_error():
    result = run("generate", "--settings", "no.such.settings", "-o", "unused")
    assert result.returncode == 2
    assert "cannot set up Django" in result.stderr


def test_pythonpath_makes_a_project_importable(tmp_path):
    project = tmp_path / "project"
    (project / "shopapp").mkdir(parents=True)
    (project / "shopapp" / "__init__.py").write_text("", encoding="utf-8")
    (project / "shopapp" / "models.py").write_text(
        textwrap.dedent(
            """
            from django.db import models


            class Item(models.Model):
                json = models.CharField(max_length=10)
            """
        ),
        encoding="utf-8",
    )
    (project / "shopsettings.py").write_text(
        'INSTALLED_APPS = ["shopapp"]\nDEFAULT_AUTO_FIELD = "django.db.models.AutoField"\n', encoding="utf-8"
    )
    output = tmp_path / "out"
    result = run(
        "generate", "--settings", "shopsettings", "--pythonpath", str(project), "-o", str(output), cwd=tmp_path
    )
    assert result.returncode == 0, result.stderr
    text = (output / "shopapp.py").read_text(encoding="utf-8")
    assert 'json_: str = Field(alias="json", max_length=10)' in text


def write_project(root, app, field):
    """A Django project in ``root``: settings module ``shopsettings`` and an app ``app`` with model ``Item``."""
    (root / app).mkdir(parents=True)
    (root / app / "__init__.py").write_text("", encoding="utf-8")
    (root / app / "models.py").write_text(
        f"from django.db import models\n\n\nclass Item(models.Model):\n    {field} = models.IntegerField()\n",
        encoding="utf-8",
    )
    (root / "shopsettings.py").write_text(
        f'INSTALLED_APPS = ["{app}"]\nDEFAULT_AUTO_FIELD = "django.db.models.AutoField"\n', encoding="utf-8"
    )


def run_script(*args, cwd, pythonpath):
    """The ``adorm`` console script, as ``uv run adorm`` starts it, with ``PYTHONPATH`` set to ``pythonpath``."""
    script = Path(sys.executable).with_name("adorm.exe" if os.name == "nt" else "adorm")
    assert script.is_file(), f"the adorm console script is not installed next to {sys.executable}"
    environ = {key: value for key, value in os.environ.items() if key != "DJANGO_SETTINGS_MODULE"}
    environ["PYTHONPATH"] = os.pathsep.join(str(path) for path in pythonpath)
    return subprocess.run(  # noqa: S603 - the console script of the environment running the suite
        [str(script), *args], capture_output=True, text=True, cwd=cwd, env=environ, check=False
    )


@pytest.mark.parametrize("listed", ["absent", "behind"])
def test_console_script_imports_the_project_from_the_current_directory(tmp_path, listed):
    """An editable install lists its repository root on the import path. A package of the same name there (adorm's
    own ``demo``) shadows the project's unless the current directory comes first, also when the current directory
    is listed further back (the project itself installed editable)."""
    project, other = tmp_path / "project", tmp_path / "other"
    write_project(project, "shopapp", "mine")
    write_project(other, "shopapp", "shadow")
    pythonpath = {"absent": [other], "behind": [other, project]}[listed]
    result = run_script("generate", "--settings", "shopsettings", "-o", "out", cwd=project, pythonpath=pythonpath)
    assert result.returncode == 0, result.stderr
    text = (project / "out" / "shopapp.py").read_text(encoding="utf-8")
    assert "mine: int" in text
    assert "shadow" not in text


def test_console_script_pythonpath_comes_after_the_current_directory(tmp_path):
    """``--pythonpath`` directories, relative ones resolved against the current directory, also come before a
    shadowing entry, in the order given and after the current directory."""
    work, other = tmp_path / "work", tmp_path / "other"
    work.mkdir()
    write_project(work / "first", "shopapp", "first")
    write_project(work / "second", "shopapp", "second")
    write_project(other, "shopapp", "shadow")
    (other / "shopsettings.py").unlink()
    result = run_script(
        "generate", "--settings", "shopsettings", "--pythonpath", "first", "--pythonpath", "second", "-o", "out",
        cwd=work, pythonpath=[other, work / "second"],
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert "first: int" in (work / "out" / "shopapp.py").read_text(encoding="utf-8")


def test_import_path_moves_entries_naming_the_front_directories(tmp_path, monkeypatch):
    """``''``, a trailing separator and a relative entry name the same directory as the front entries and are
    removed from further back; the other entries keep their order."""
    project = tmp_path / "project"
    (project / "sub").mkdir(parents=True)
    monkeypatch.chdir(project)
    entries = ["", "elsewhere", f"{project}{os.sep}", "sub", str(project / "sub"), "/opt/site"]
    monkeypatch.setattr(sys, "path", entries)
    _import_path(["sub"])
    assert sys.path == [str(project), str(project / "sub"), "elsewhere", "/opt/site"]


def test_generated_demo_models_match_the_demo_django_models(tmp_path):
    """The generator against the demo's independently written comparison (``demo.backend.model_check``)."""
    output = tmp_path / "gendemo"
    result = run("generate", "--settings", "demo.backend.settings", "-o", str(output))
    assert result.returncode == 0, result.stderr
    script = textwrap.dedent(
        """
        import django

        django.setup()
        import gendemo
        from demo.backend.model_check import compare

        problems = [problem for model in gendemo.MODELS for problem in compare(model)]
        print(len(gendemo.MODELS), problems)
        """
    )
    environ = {key: value for key, value in os.environ.items() if key != "DJANGO_SETTINGS_MODULE"}
    environ.update(DJANGO_SETTINGS_MODULE="demo.backend.settings", PYTHONPATH=f"{ROOT}{os.pathsep}{tmp_path}")
    checked = subprocess.run(  # noqa: S603 - the interpreter running the suite
        [sys.executable, "-c", script], capture_output=True, text=True, cwd=tmp_path, env=environ, check=False
    )
    assert checked.returncode == 0, checked.stderr
    assert checked.stdout.strip() == "11 []"
