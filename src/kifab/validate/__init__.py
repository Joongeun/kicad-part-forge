"""The validation layer: what makes a generated part trustworthy.

Four families of check, each individually testable and each explicit about
which representation it reads:

| module        | layer            | what it catches |
|---------------|------------------|-----------------|
| `schema`      | the IR           | statements that are well-typed and jointly contradictory |
| `geometry`    | emitted files    | copper, courtyard, silk and grid defects |
| `klc`         | emitted files    | KiCad Library Convention deviations that are real defects |
| `roundtrip`   | emitted files    | KiCad's own parser accepting, and canonicalising to, our bytes |

An IR part is checked at *both* layers: `check_part` renders it and reads the
result back, because silkscreen, courtyard and layer names do not exist
upstream of the emitter. A file with no IR behind it (an adopted KiCad part, a
vendor download) is checked at the file layer alone.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from ..emit.footprint import render_footprint
from ..emit.symbol import render_library
from ..ir import Part, load_part
from . import geometry, klc, schema
from .parse import ParsedFootprint, ParsedSymbol, ParsedSymbolLib, ParseError
from .report import (
    LAYER_FOOTPRINT,
    LAYER_IR,
    LAYER_SYMBOL,
    Finding,
    Report,
    Severity,
)
from .roundtrip import Conformance, find_kicad_cli

__all__ = [
    "LAYER_FOOTPRINT",
    "LAYER_IR",
    "LAYER_SYMBOL",
    "Conformance",
    "Finding",
    "ParseError",
    "ParsedFootprint",
    "ParsedSymbol",
    "ParsedSymbolLib",
    "Report",
    "Severity",
    "check_footprint",
    "check_footprint_file",
    "check_part",
    "check_parts",
    "check_path",
    "check_paths",
    "check_symbol",
    "check_symbol_library_file",
    "find_kicad_cli",
    "geometry",
    "klc",
    "schema",
]


# --------------------------------------------------------------------------
# File-layer entry points
# --------------------------------------------------------------------------


def check_footprint(fp: ParsedFootprint) -> Report:
    """Every file-layer rule for one footprint."""
    report = Report()
    report.extend(geometry.check_footprint_geometry(fp))
    report.extend(klc.check_footprint_klc(fp))
    return report


def check_symbol(symbol: ParsedSymbol) -> Report:
    """Every file-layer rule for one symbol."""
    report = Report()
    report.extend(geometry.check_symbol_geometry(symbol))
    report.extend(klc.check_symbol_klc(symbol))
    return report


def check_footprint_file(path: Path, conformance: Conformance | None = None) -> Report:
    path = Path(path)
    report = Report()
    try:
        fp = ParsedFootprint.from_path(path)
    except (ParseError, OSError) as exc:
        report.add(
            "PARSE",
            Severity.ERROR,
            str(exc),
            subject=str(path),
            layer=LAYER_FOOTPRINT,
        )
        return report
    report.extend(check_footprint(fp), subject=str(path))
    if conformance is not None:
        report.extend(conformance.check_footprints([path]))
    return report


def check_symbol_library_file(
    path: Path, conformance: Conformance | None = None
) -> Report:
    path = Path(path)
    report = Report()
    try:
        lib = ParsedSymbolLib.from_path(path)
    except (ParseError, OSError) as exc:
        report.add(
            "PARSE", Severity.ERROR, str(exc), subject=str(path), layer=LAYER_SYMBOL
        )
        return report
    for symbol in lib.symbols:
        report.extend(check_symbol(symbol), subject=f"{path}#{symbol.name}")
    if conformance is not None:
        report.extend(conformance.check_symbol_library(path))
    return report


# --------------------------------------------------------------------------
# IR entry points — checked at both layers
# --------------------------------------------------------------------------


def check_part(
    part: Part,
    *,
    subject: str | None = None,
    conformance: Conformance | None = None,
) -> Report:
    """Check one part at the IR layer and at the emitted-file layer."""
    label = subject or part.mpn
    report = Report()
    report.extend(schema.check_schema(part), subject=label)

    footprint_text = render_footprint(part)
    symbol_text = render_library([part])
    fp = ParsedFootprint.from_text(footprint_text, label)
    lib = ParsedSymbolLib.from_text(symbol_text, label)
    symbol = next((s for s in lib.symbols if s.name == part.symbol_name), None)

    report.extend(check_footprint(fp), subject=label)
    if symbol is not None:
        report.extend(check_symbol(symbol), subject=label)
        report.extend(geometry.check_pin_sets(symbol, fp), subject=label)

    if conformance is not None:
        report.extend(
            conformance.check_footprint_text(footprint_text, part.footprint.name),
            subject=label,
        )
        report.extend(
            conformance.check_symbol_text(symbol_text, part.library), subject=label
        )
    return report


def check_parts(
    parts: list[tuple[str, Part]] | list[Part],
    *,
    conformance: Conformance | None = None,
) -> Report:
    """Check several parts, batching the `kicad-cli` gate into two calls.

    One subprocess per part per half would dominate the runtime of a corpus
    check; `fp upgrade` takes a whole `.pretty` directory and `sym upgrade`
    takes a whole library, which is exactly the shape a built library already
    has.
    """
    labelled: list[tuple[str, Part]] = [
        (p[0], p[1]) if isinstance(p, tuple) else (p.mpn, p) for p in parts
    ]
    report = Report()
    for label, part in labelled:
        report.extend(check_part(part, subject=label))

    if conformance is None or not labelled:
        return report

    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        pretty = root / "check.pretty"
        pretty.mkdir()
        written: dict[Path, str] = {}
        for label, part in labelled:
            path = pretty / f"{part.footprint.name}.kicad_mod"
            if path not in written:
                path.write_text(render_footprint(part), encoding="utf-8")
                written[path] = label
        report.extend(_relabel(conformance.check_footprints(list(written)), written))

        libraries: dict[str, list[Part]] = {}
        for _, part in labelled:
            libraries.setdefault(part.library, []).append(part)
        for library, members in sorted(libraries.items()):
            lib_path = root / f"{library}.kicad_sym"
            lib_path.write_text(render_library(members), encoding="utf-8")
            report.extend(
                _relabel(
                    conformance.check_symbol_library(lib_path),
                    {lib_path: f"{library}.kicad_sym"},
                )
            )
    return report


def _relabel(report: Report, labels: dict[Path, str]) -> Report:
    """Swap temp-file subjects for the part labels a human recognises."""
    by_name = {str(path): label for path, label in labels.items()}
    out = Report()
    for finding in report:
        out.add(
            finding.check,
            finding.severity,
            finding.message,
            subject=by_name.get(finding.subject, finding.subject),
            where=finding.where,
            layer=finding.layer,
            at=finding.at,
        )
    return out


# --------------------------------------------------------------------------
# Path dispatch — what `kifab check <thing>` calls
# --------------------------------------------------------------------------


def _is_pretty(path: Path) -> bool:
    return path.is_dir() and path.suffix == ".pretty"


def _collect(path: Path) -> tuple[list[Path], list[Path], list[Path]]:
    """(part YAML, symbol libraries, footprints) reachable from `path`."""
    path = Path(path)
    if path.is_file():
        if path.suffix in (".yaml", ".yml"):
            return ([path], [], [])
        if path.suffix == ".kicad_sym":
            return ([], [path], [])
        if path.suffix == ".kicad_mod":
            return ([], [], [path])
        return ([], [], [])
    if _is_pretty(path):
        return ([], [], sorted(path.glob("*.kicad_mod")))
    parts = sorted(p for p in path.glob("*.y*ml"))
    symbols = sorted(path.glob("*.kicad_sym"))
    footprints = sorted(path.glob("*.kicad_mod"))
    for child in sorted(path.iterdir()):
        if _is_pretty(child):
            footprints += sorted(child.glob("*.kicad_mod"))
    return (parts, symbols, footprints)


def check_path(path: Path, *, conformance: Conformance | None = None) -> Report:
    """Check whatever `path` names: a part, a library, a footprint, a tree."""
    return check_paths([path], conformance=conformance)


def check_paths(
    paths: list[Path], *, conformance: Conformance | None = None
) -> Report:
    report = Report()
    part_files: list[Path] = []
    symbol_files: list[Path] = []
    footprint_files: list[Path] = []
    for path in paths:
        path = Path(path)
        if not path.exists():
            report.add("PATH", Severity.ERROR, "no such file or directory", subject=str(path))
            continue
        found = _collect(path)
        part_files += found[0]
        symbol_files += found[1]
        footprint_files += found[2]

    if not (part_files or symbol_files or footprint_files):
        if not report.errors:
            report.add(
                "PATH",
                Severity.ERROR,
                "nothing to check: expected part YAML, .kicad_sym, .kicad_mod "
                "or a .pretty directory",
                subject=", ".join(str(p) for p in paths),
            )
        return report

    loaded: list[tuple[str, Part]] = []
    for path in part_files:
        try:
            loaded.append((str(path), load_part(path)))
        except ValueError as exc:
            report.add(
                "PARSE", Severity.ERROR, str(exc), subject=str(path), layer=LAYER_IR
            )
    if loaded:
        report.extend(check_parts(loaded, conformance=conformance))

    symbols: list[ParsedSymbol] = []
    for path in symbol_files:
        report.extend(check_symbol_library_file(path, conformance))
        try:
            symbols += ParsedSymbolLib.from_path(path).symbols
        except (ParseError, OSError):
            pass

    footprints: dict[str, ParsedFootprint] = {}
    for path in footprint_files:
        report.extend(check_footprint_file(path, None))
        try:
            fp = ParsedFootprint.from_path(path)
        except (ParseError, OSError):
            continue
        footprints[fp.name] = fp
    if footprint_files and conformance is not None:
        report.extend(conformance.check_footprints(footprint_files))

    # A built library holds both halves, so the one cross-file rule can run.
    for symbol in symbols:
        pointer = symbol.properties.get("Footprint", "")
        name = pointer.partition(":")[2] or pointer
        fp = footprints.get(name)
        if fp is not None:
            report.extend(
                geometry.check_pin_sets(symbol, fp),
                subject=f"{symbol.name} + {fp.name}",
            )
    return report
