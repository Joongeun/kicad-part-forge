"""Mechanical corrections to part YAML, applied only where the answer is forced.

`kifab check` reports; this applies. The two are kept apart on purpose — a
validator that silently edits your file is a validator you stop reading.

The whole design of this module is the line between the two prefix lists below.
SCH002 fires on a *name*, and a name determines the electrical type only
sometimes: `VDD` is a supply input on every part that has one, while `VOUT` is
an output on a regulator and an input on nothing. Rewriting the second class
would produce a file that passes every check and mistypes a power rail — the
exact failure this project exists to prevent, arrived at by way of a
convenience feature. So the ambiguous names stay warnings for a human.

Edits are made on the text, line by line, never by round-tripping the YAML:
`parts/*.yaml` carries `# NOTE:` comments recording what an importer could not
normalise, and a dump-and-reload would delete them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .validate.schema import POWER_PIN_TYPES, _clean_pin_name

#: Names whose electrical type is not in doubt: on any part, these are pins the
#: board drives power *into*. Sorted longest-first so `AVDD` wins over `VDD`.
UNAMBIGUOUS_RAIL_PREFIXES = (
    "AVCC",
    "AVDD",
    "AVSS",
    "DGND",
    "DVDD",
    "DVSS",
    "AGND",
    "PGND",
    "VDDA",
    "VSSA",
    "VCC",
    "VDD",
    "VSS",
    "VEE",
    "VPP",
    "VIN",
    "GND",
)

#: Names SCH002 also flags, deliberately left alone. Each one is a pin that is
#: genuinely a source on some parts and a sink on others, and the datasheet —
#: not the name — is what settles it.
AMBIGUOUS_RAIL_PREFIXES = ("VOUT", "VREF", "VBAT", "VBUS")

_PIN_LINE = re.compile(r"^\s*-\s*\{.*\bnumber:.*\bname:.*\btype:.*\}")
_NAME = re.compile(r"\bname:\s*(?:\"([^\"]*)\"|'([^']*)'|([^,}\s]+))")
_TYPE = re.compile(r"\btype:\s*(?:\"([^\"]*)\"|'([^']*)'|([^,}\s]+))")


@dataclass(frozen=True)
class Change:
    """One rewritten line, kept so the caller can print what it did."""

    line_no: int
    pin: str
    old_type: str
    new_type: str

    def describe(self) -> str:
        return (
            f"line {self.line_no}: pin {self.pin} "
            f"{self.old_type} -> {self.new_type}"
        )


def _value(match: re.Match[str] | None) -> str:
    if match is None:
        return ""
    return next((g for g in match.groups() if g is not None), "")


def fix_power_pin_types(text: str) -> tuple[str, list[Change]]:
    """Type unambiguous supply pins as `power_in`. Returns the new text."""
    lines = text.splitlines(keepends=True)
    changes: list[Change] = []

    for index, line in enumerate(lines):
        if not _PIN_LINE.match(line):
            continue
        name = _clean_pin_name(_value(_NAME.search(line)))
        if not name.startswith(UNAMBIGUOUS_RAIL_PREFIXES):
            continue
        old = _value(_TYPE.search(line))
        if not old or old in {t.value for t in POWER_PIN_TYPES}:
            continue
        lines[index] = _TYPE.sub("type: power_in", line, count=1)
        changes.append(Change(index + 1, name, old, "power_in"))

    return "".join(lines), changes


def fix_file(path: Path, *, dry_run: bool = False) -> list[Change]:
    """Apply every fixer to one part file. Writes only if something changed."""
    text = path.read_text(encoding="utf-8")
    fixed, changes = fix_power_pin_types(text)
    if changes and not dry_run:
        path.write_text(fixed, encoding="utf-8")
    return changes


def fix_paths(targets: list[Path], *, dry_run: bool = False) -> dict[Path, list[Change]]:
    """Fix every `*.yaml` under the given files or directories."""
    files: list[Path] = []
    for target in targets:
        if target.is_dir():
            files.extend(sorted(target.rglob("*.yaml")))
        elif target.suffix in (".yaml", ".yml"):
            files.append(target)

    results: dict[Path, list[Change]] = {}
    for file in files:
        changes = fix_file(file, dry_run=dry_run)
        if changes:
            results[file] = changes
    return results
