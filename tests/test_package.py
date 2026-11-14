import tomllib

from pathlib import Path

import adorm


def test_version_matches_pyproject():
    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8"))
    assert adorm.__version__ == pyproject["project"]["version"]


def test_package_is_typed():
    assert (Path(adorm.__file__).parent / "py.typed").is_file()
