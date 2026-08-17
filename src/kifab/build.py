"""Turn a set of parts into KiCad libraries on disk.

Kept out of `cli.py` so the build is callable and testable without a process
boundary. The CLI is argument parsing and printing; this is the work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .emit.footprint import render_footprint
from .emit.symbol import render_library
from .ir import Part


@dataclass
class BuildResult:
    """What a build wrote, in a form tests and the CLI can both read."""

    symbol_libraries: dict[str, Path] = field(default_factory=dict)
    footprints: dict[str, Path] = field(default_factory=dict)

    @property
    def paths(self) -> list[Path]:
        return sorted([*self.symbol_libraries.values(), *self.footprints.values()])


def _check_unique(parts: list[Part]) -> None:
    """Reject part sets that would silently overwrite each other."""
    seen_symbols: dict[tuple[str, str], Part] = {}
    for part in parts:
        key = (part.library, part.symbol_name)
        if key in seen_symbols:
            raise ValueError(
                f"two parts both define symbol {part.symbol_name!r} in library "
                f"{part.library!r}"
            )
        seen_symbols[key] = part

    # Two parts may legitimately share a footprint (that is the point of naming
    # footprints after packages) — but only if they generate the same bytes.
    rendered: dict[tuple[str, str], tuple[str, str]] = {}
    for part in parts:
        key = (part.library, part.footprint.name)
        text = render_footprint(part)
        if key in rendered:
            previous_mpn, previous_text = rendered[key]
            if previous_text != text:
                raise ValueError(
                    f"parts {previous_mpn!r} and {part.mpn!r} both define "
                    f"footprint {part.footprint.name!r} in library "
                    f"{part.library!r}, but with different geometry"
                )
        else:
            rendered[key] = (part.mpn, text)


def build(parts: list[Part], out_dir: Path) -> BuildResult:
    """Write `<library>.kicad_sym` and `<library>.pretty/*.kicad_mod`."""
    _check_unique(parts)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = BuildResult()

    libraries: dict[str, list[Part]] = {}
    for part in parts:
        libraries.setdefault(part.library, []).append(part)

    for library, members in sorted(libraries.items()):
        sym_path = out_dir / f"{library}.kicad_sym"
        sym_path.write_text(render_library(members), encoding="utf-8")
        result.symbol_libraries[library] = sym_path

        pretty = out_dir / f"{library}.pretty"
        pretty.mkdir(exist_ok=True)
        for part in members:
            mod_path = pretty / f"{part.footprint.name}.kicad_mod"
            mod_path.write_text(render_footprint(part), encoding="utf-8")
            result.footprints[f"{library}:{part.footprint.name}"] = mod_path

    return result


def discover(paths: list[Path]) -> list[Path]:
    """Expand a mix of YAML files and directories into a sorted file list."""
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            found += sorted(
                p for p in path.iterdir() if p.suffix in (".yaml", ".yml")
            )
        else:
            found.append(path)
    missing = [p for p in found if not p.is_file()]
    if missing:
        raise FileNotFoundError(f"no such part file: {missing[0]}")
    return found
