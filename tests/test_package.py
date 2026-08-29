"""Smoke tests: the package installs, imports, and reports a consistent version."""

from importlib.metadata import version

import tokenaudit


def test_import() -> None:
    assert tokenaudit is not None


def test_version_matches_installed_metadata() -> None:
    assert version("tokenaudit") == tokenaudit.__version__
