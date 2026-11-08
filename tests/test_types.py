"""The package and the tests type-check under `mypy --strict` (settings in
pyproject.toml), so a missing or wrong annotation fails the test run."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_mypy_strict() -> None:
    api = pytest.importorskip("mypy.api", reason="mypy not installed: pip install -e '.[test]'")
    out, err, status = api.run(["--config-file", str(ROOT / "pyproject.toml"),
                                str(ROOT / "quaderno"), str(ROOT / "tests")])
    assert status == 0, out + err
