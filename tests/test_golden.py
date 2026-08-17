"""Byte-compare generated output against reviewed reference files.

Golden files are only meaningful because emission is a pure function of the IR
(derived UUIDs, sorted output). Any diff here is a real change in what a user's
board would be built from, so it must be looked at, not regenerated reflexively.

To update after a *reviewed* change:

    uv run pytest --update-golden

Then read `git diff tests/golden/` before committing it. A golden file
regenerated without being read is not a test.
"""

from __future__ import annotations

import difflib
from pathlib import Path

import pytest

from kifab.emit.footprint import render_footprint
from kifab.emit.symbol import render_library
from kifab.ir import load_part

ROOT = Path(__file__).resolve().parent.parent
PARTS_DIR = ROOT / "parts"
GOLDEN = Path(__file__).resolve().parent / "golden"


def _cases() -> list[tuple[str, str]]:
    """(golden filename, generated text) for everything the corpus produces."""
    parts = [load_part(p) for p in sorted(PARTS_DIR.glob("*.yaml"))]
    cases = [(f"{part.footprint.name}.kicad_mod", render_footprint(part)) for part in parts]

    libraries: dict[str, list] = {}
    for part in parts:
        libraries.setdefault(part.library, []).append(part)
    cases += [
        (f"{library}.kicad_sym", render_library(members))
        for library, members in sorted(libraries.items())
    ]
    return sorted(cases)


def _diff(expected: str, actual: str, name: str) -> str:
    return "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile=f"golden/{name}",
            tofile=f"generated/{name}",
        )
    )


@pytest.mark.parametrize("name,text", _cases(), ids=lambda v: v if isinstance(v, str) and len(v) < 60 else "")
def test_output_matches_golden(name: str, text: str) -> None:
    path = GOLDEN / name
    assert path.exists(), (
        f"no golden file for {name}; run `pytest --update-golden` and review "
        "the new file before committing it"
    )
    expected = path.read_text(encoding="utf-8")
    assert expected == text, (
        f"generated {name} differs from its reviewed golden file:\n"
        + _diff(expected, text, name)
    )


def test_golden_directory_has_no_orphans() -> None:
    """A golden file for a part that no longer exists is stale, not passing."""
    expected = {name for name, _ in _cases()}
    actual = {p.name for p in GOLDEN.iterdir() if p.suffix in (".kicad_mod", ".kicad_sym")}
    assert actual == expected, f"orphaned golden files: {sorted(actual - expected)}"


def update() -> None:
    """Rewrite every golden file. Called by `pytest --update-golden`."""
    GOLDEN.mkdir(exist_ok=True)
    live = {name for name, _ in _cases()}
    for stale in GOLDEN.iterdir():
        if stale.suffix in (".kicad_mod", ".kicad_sym") and stale.name not in live:
            stale.unlink()
    for name, text in _cases():
        (GOLDEN / name).write_text(text, encoding="utf-8")
