"""Format conformance — KiCad's own parser judging our output.

This is the gate `scripts/verify.sh` used to implement inline in shell. It is
here instead so it is callable **per part, from code**, by tests, by the CLI
and by CI, and so its verdict lands in the same `Report` as every other check.

Two statements, in increasing strength:

1. `kicad-cli … upgrade --force` exits 0 — the file parses. Failure is an
   **error**: nothing else about the part matters if KiCad will not read it.
2. What it writes back is byte-identical to what we wrote, apart from the
   `(generator …)` token it stamps with its own name — i.e. we emit KiCad's
   *canonical* form, not merely a form it accepts. Deviation is a **warning**,
   because a third-party file that is merely acceptable is still usable; for
   kifab's own output `tests/test_conformance.py` holds it at error strength.

`upgrade` rewrites in place, so everything runs on a copy in a temp directory:
the gate must never mutate the artefact it is judging.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .report import LAYER_FOOTPRINT, LAYER_SYMBOL, Report, Severity

#: Where KiCad 9 puts the CLI on macOS. Overridable by `KICAD_CLI`.
DEFAULT_KICAD_CLI = Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")

#: The only line kicad-cli is expected to change.
_GENERATOR_PREFIX = "(generator "

#: How many rewritten lines to quote in a finding before truncating.
_MAX_QUOTED_DIFFS = 3


def find_kicad_cli(explicit: str | Path | None = None) -> Path | None:
    """Locate `kicad-cli`: explicit, then `$KICAD_CLI`, then PATH, then macOS."""
    for candidate in (explicit, os.environ.get("KICAD_CLI")):
        if candidate:
            path = Path(candidate)
            return path if path.exists() else None
    found = shutil.which("kicad-cli")
    if found:
        return Path(found)
    return DEFAULT_KICAD_CLI if DEFAULT_KICAD_CLI.exists() else None


@dataclass(frozen=True)
class Conformance:
    """A reusable handle on the round-trip gate."""

    cli: Path | None

    @classmethod
    def discover(cls, explicit: str | Path | None = None) -> Conformance:
        return cls(find_kicad_cli(explicit))

    @property
    def available(self) -> bool:
        return self.cli is not None

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        assert self.cli is not None
        return subprocess.run(
            [str(self.cli), *args], capture_output=True, text=True, check=False
        )

    def _unavailable(self, subject: str, layer: str) -> Report:
        report = Report()
        report.add(
            "CLI000",
            Severity.INFO,
            "kicad-cli not found, so the format conformance gate did not run "
            "(set KICAD_CLI to override)",
            subject=subject,
            where="kicad-cli",
            layer=layer,
        )
        return report

    # -- footprints -------------------------------------------------------

    def check_footprints(self, paths: list[Path]) -> Report:
        """Run the gate over any number of `.kicad_mod` files, in one call."""
        report = Report()
        paths = [Path(p) for p in paths]
        if not paths:
            return report
        if not self.available:
            for path in paths:
                report.extend(self._unavailable(str(path), LAYER_FOOTPRINT))
            return report

        with tempfile.TemporaryDirectory() as scratch:
            pretty = Path(scratch) / "check.pretty"
            pretty.mkdir()
            before: dict[Path, str] = {}
            copies: dict[Path, Path] = {}
            for path in paths:
                copy = pretty / path.name
                shutil.copy(path, copy)
                copies[path] = copy
                before[path] = copy.read_text(encoding="utf-8")

            run = self._run("fp", "upgrade", "--force", str(pretty))
            if run.returncode != 0:
                message = (run.stderr or run.stdout).strip().splitlines()
                for path in paths:
                    report.add(
                        "CLI001",
                        Severity.ERROR,
                        "KiCad's own parser rejected this library: "
                        + (message[0] if message else "kicad-cli failed"),
                        subject=str(path),
                        where="kicad-cli fp upgrade",
                        layer=LAYER_FOOTPRINT,
                    )
                return report

            for path in paths:
                report.extend(
                    _canonical_findings(
                        before[path],
                        copies[path].read_text(encoding="utf-8"),
                        subject=str(path),
                        layer=LAYER_FOOTPRINT,
                        tool="fp upgrade",
                    )
                )
        return report

    def check_footprint_text(self, text: str, name: str) -> Report:
        """Same gate, for a footprint that only exists in memory."""
        if not self.available:
            return self._unavailable(name, LAYER_FOOTPRINT)
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / f"{name}.kicad_mod"
            path.write_text(text, encoding="utf-8")
            report = self.check_footprints([path])
        return _restamp(report, name)

    # -- symbols ----------------------------------------------------------

    def check_symbol_library(self, path: Path) -> Report:
        path = Path(path)
        if not self.available:
            return self._unavailable(str(path), LAYER_SYMBOL)
        report = Report()
        with tempfile.TemporaryDirectory() as scratch:
            copy = Path(scratch) / path.name
            shutil.copy(path, copy)
            before = copy.read_text(encoding="utf-8")
            run = self._run("sym", "upgrade", "--force", str(copy))
            if run.returncode != 0:
                message = (run.stderr or run.stdout).strip().splitlines()
                report.add(
                    "CLI001",
                    Severity.ERROR,
                    "KiCad's own parser rejected this library: "
                    + (message[0] if message else "kicad-cli failed"),
                    subject=str(path),
                    where="kicad-cli sym upgrade",
                    layer=LAYER_SYMBOL,
                )
                return report
            report.extend(
                _canonical_findings(
                    before,
                    copy.read_text(encoding="utf-8"),
                    subject=str(path),
                    layer=LAYER_SYMBOL,
                    tool="sym upgrade",
                )
            )
        return report

    def check_symbol_text(self, text: str, name: str) -> Report:
        if not self.available:
            return self._unavailable(name, LAYER_SYMBOL)
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / f"{name}.kicad_sym"
            path.write_text(text, encoding="utf-8")
            report = self.check_symbol_library(path)
        return _restamp(report, name)


def _restamp(report: Report, subject: str) -> Report:
    """Replace the temp-file subject with something a human recognises."""
    out = Report()
    for finding in report:
        out.add(
            finding.check,
            finding.severity,
            finding.message,
            subject=subject,
            where=finding.where,
            layer=finding.layer,
            at=finding.at,
        )
    return out


def _canonical_findings(
    before: str, after: str, *, subject: str, layer: str, tool: str
) -> Report:
    report = Report()
    a, b = before.splitlines(), after.splitlines()
    if len(a) != len(b):
        report.add(
            "CLI002",
            Severity.WARNING,
            f"`kicad-cli {tool}` rewrote the file to a different length "
            f"({len(a)} -> {len(b)} lines): this is not KiCad's canonical form, "
            "so opening it in the GUI will produce a spurious diff",
            subject=subject,
            where=tool,
            layer=layer,
        )
        return report

    changed = [(x, y) for x, y in zip(a, b) if x != y]
    unexpected = [
        (x, y) for x, y in changed if not x.strip().startswith(_GENERATOR_PREFIX)
    ]
    if unexpected:
        quoted = "; ".join(
            f"{x.strip()!r} -> {y.strip()!r}" for x, y in unexpected[:_MAX_QUOTED_DIFFS]
        )
        more = (
            f" (+{len(unexpected) - _MAX_QUOTED_DIFFS} more)"
            if len(unexpected) > _MAX_QUOTED_DIFFS
            else ""
        )
        report.add(
            "CLI002",
            Severity.WARNING,
            f"not in KiCad's canonical form; `kicad-cli {tool}` rewrote "
            f"{len(unexpected)} line(s): {quoted}{more}",
            subject=subject,
            where=tool,
            layer=layer,
        )
    return report
