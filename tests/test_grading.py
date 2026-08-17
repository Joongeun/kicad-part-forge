"""The grading tool used to score a blind-holdout run against a reference.

It knows no geometry of its own — it compares two files — which is what lets it
live in the repository while the holdout is still blind.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from conftest import write_footprint

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "grade_footprint.py"

_spec = importlib.util.spec_from_file_location("grade_footprint", SCRIPT)
grading = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grading)


def _pads(offset: float = 0.0, size: float = 0.7):
    return [
        ("1", -1.0 + offset, -0.5, size, 0.3),
        ("2", -1.0 + offset, 0.5, size, 0.3),
        ("3", 1.0, 0.5, size, 0.3),
        ("4", 1.0, -0.5, size, 0.3),
    ]


def test_identical_footprints_pass(tmp_path: Path) -> None:
    a = write_footprint(tmp_path / "a", "REF", _pads())
    b = write_footprint(tmp_path / "b", "REF", _pads())
    ok, lines = grading.grade(a, b)
    assert ok
    assert all("FAIL" not in line for line in lines)


def test_a_land_outside_tolerance_fails_and_says_which(tmp_path: Path) -> None:
    a = write_footprint(tmp_path / "a", "REF", _pads())
    b = write_footprint(tmp_path / "b", "REF", _pads(offset=0.11))
    ok, lines = grading.grade(a, b, tolerance=0.05)
    assert not ok
    failing = [line for line in lines if "FAIL" in line]
    assert len(failing) == 2  # pads 1 and 2 moved
    assert "pad   1" in failing[0]


def test_a_land_inside_tolerance_passes(tmp_path: Path) -> None:
    a = write_footprint(tmp_path / "a", "REF", _pads())
    b = write_footprint(tmp_path / "b", "REF", _pads(offset=0.04))
    ok, _ = grading.grade(a, b, tolerance=0.05)
    assert ok


def test_a_missing_land_fails(tmp_path: Path) -> None:
    a = write_footprint(tmp_path / "a", "REF", _pads())
    b = write_footprint(tmp_path / "b", "REF", _pads()[:3])
    ok, lines = grading.grade(a, b)
    assert not ok
    assert any("MISSING lands: ['4']" in line for line in lines)


def test_byte_identity_is_detectable(tmp_path: Path) -> None:
    """For the holdout, identity with the near-miss part means the trap was hit."""
    a = write_footprint(tmp_path / "a", "REF", _pads())
    b = write_footprint(tmp_path / "b", "REF", _pads())
    c = write_footprint(tmp_path / "c", "REF", _pads(size=0.9))
    assert grading.identical(a, b)
    assert not grading.identical(a, c)


def test_cli_exits_nonzero_on_a_mismatch(tmp_path: Path, capsys) -> None:
    a = write_footprint(tmp_path / "a", "REF", _pads())
    b = write_footprint(tmp_path / "b", "REF", _pads(offset=0.2))
    assert grading.main([str(a), str(b)]) == 1
    assert grading.main([str(a), str(a)]) == 0
    assert grading.main([str(a), str(a), "--not-identical-to", str(a)]) == 1
