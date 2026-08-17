"""Shared pytest configuration."""

from __future__ import annotations

import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent


def pytest_addoption(parser) -> None:
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="rewrite tests/golden/ from the current emitters, then run the "
        "suite. Always read `git diff tests/golden/` afterwards — a golden "
        "file regenerated without being read is not a test.",
    )


def pytest_configure(config) -> None:
    if not config.getoption("--update-golden"):
        return
    # Golden cases are built at collection time, so the rewrite has to happen
    # before collection starts.
    spec = importlib.util.spec_from_file_location(
        "_golden_updater", HERE / "test_golden.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.update()
