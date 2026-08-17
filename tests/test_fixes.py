"""`kifab check --fix`.

The assertions that matter here are the ones about what is *not* fixed. A
fixer that rewrites everything SCH002 flags would mistype a regulator output
as an input and produce a file that passes every check — a silent wrong answer,
which is worse than the warning it replaced.
"""

from __future__ import annotations

from pathlib import Path

from kifab.fixes import (
    AMBIGUOUS_RAIL_PREFIXES,
    UNAMBIGUOUS_RAIL_PREFIXES,
    fix_file,
    fix_power_pin_types,
)

PART = """\
mpn: EXAMPLE
symbol:
  pins:
    # NOTE: this comment must survive the fixer.
    - { number: "1", name: GND, type: unspecified, slot: 0 }
    - { number: "2", name: VDD, type: bidirectional, slot: 1 }
    - { number: "3", name: AVCC, type: unspecified, slot: 2 }
    - { number: "4", name: VOUT, type: unspecified, slot: 3 }
    - { number: "5", name: VREF, type: unspecified, slot: 4 }
    - { number: "6", name: SDA, type: bidirectional, slot: 5 }
    - { number: "7", name: VCC, type: power_in, slot: 6 }
"""


def test_fixes_the_rails_whose_answer_is_forced() -> None:
    fixed, changes = fix_power_pin_types(PART)
    assert [c.pin for c in changes] == ["GND", "VDD", "AVCC"]
    assert all(c.new_type == "power_in" for c in changes)
    assert "name: GND, type: power_in" in fixed
    assert "name: AVCC, type: power_in" in fixed


def test_never_touches_a_pin_that_could_be_a_source() -> None:
    fixed, changes = fix_power_pin_types(PART)
    touched = {c.pin for c in changes}
    assert "VOUT" not in touched and "VREF" not in touched
    # Still in the file exactly as written, still a warning for a human.
    assert "name: VOUT, type: unspecified" in fixed
    assert "name: VREF, type: unspecified" in fixed


def test_leaves_signal_pins_and_already_correct_pins_alone() -> None:
    fixed, changes = fix_power_pin_types(PART)
    assert "SDA" not in {c.pin for c in changes}
    assert "VCC" not in {c.pin for c in changes}  # already power_in
    assert "name: SDA, type: bidirectional" in fixed


def test_comments_survive() -> None:
    fixed, _ = fix_power_pin_types(PART)
    assert "# NOTE: this comment must survive the fixer." in fixed


def test_the_two_lists_do_not_overlap() -> None:
    """The whole safety property, as one assertion."""
    assert not set(UNAMBIGUOUS_RAIL_PREFIXES) & set(AMBIGUOUS_RAIL_PREFIXES)
    for ambiguous in AMBIGUOUS_RAIL_PREFIXES:
        assert not ambiguous.startswith(tuple(UNAMBIGUOUS_RAIL_PREFIXES))


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "EXAMPLE.yaml"
    path.write_text(PART, encoding="utf-8")
    changes = fix_file(path, dry_run=True)
    assert changes
    assert path.read_text(encoding="utf-8") == PART

    fix_file(path, dry_run=False)
    assert path.read_text(encoding="utf-8") != PART


def test_fixing_twice_changes_nothing_the_second_time(tmp_path: Path) -> None:
    path = tmp_path / "EXAMPLE.yaml"
    path.write_text(PART, encoding="utf-8")
    assert fix_file(path)
    assert fix_file(path) == []
