#!/usr/bin/env python3
"""Diff a generated footprint against a reference, land by land.

    uv run scripts/grade_footprint.py REFERENCE.kicad_mod CANDIDATE.kicad_mod \
        [--tolerance 0.05]

Used to grade a blind-holdout run once the reference exists — the vendor's own
footprint, a SnapMagic/Ultra Librarian download, or a land pattern measured off
the drawing by hand. It is a *comparison* tool and knows no geometry of its own,
which is what lets it live in the repository while the holdout is still blind.

Exit status is 0 only when every land matches within tolerance. Prints the
per-pad delta either way, because "it failed" is not useful and "pad 7 is
0.11 mm too far out" is.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kifab.emit import sexpr  # noqa: E402
from kifab.emit.sexpr import unquote  # noqa: E402

DEFAULT_TOLERANCE = 0.05  # mm — the plan's grading threshold


def pads(path: Path) -> dict[str, tuple[float, float, float, float]]:
    """Numbered copper lands: number -> (x, y, size_x, size_y). Apertures skipped."""
    tree = sexpr.parse(path.read_text(encoding="utf-8"))
    out: dict[str, tuple[float, float, float, float]] = {}
    for node in sexpr.find_all(tree, "pad"):
        number = unquote(str(node[1]))
        if not number:
            continue
        at, size = sexpr.find(node, "at"), sexpr.find(node, "size")
        if at is None or size is None:
            continue
        out[number] = (
            float(at[1]),
            float(at[2]),
            float(size[1]),
            float(size[2]),
        )
    return out


def grade(
    reference: Path, candidate: Path, tolerance: float = DEFAULT_TOLERANCE
) -> tuple[bool, list[str]]:
    want, got = pads(reference), pads(candidate)
    lines: list[str] = []
    ok = True

    missing = sorted(set(want) - set(got), key=str)
    extra = sorted(set(got) - set(want), key=str)
    if missing:
        ok = False
        lines.append(f"MISSING lands: {missing}")
    if extra:
        ok = False
        lines.append(f"EXTRA lands: {extra}")

    for number in sorted(set(want) & set(got), key=lambda n: (len(n), n)):
        w, g = want[number], got[number]
        deltas = [g[i] - w[i] for i in range(4)]
        worst = max(abs(d) for d in deltas)
        verdict = "ok  " if worst <= tolerance else "FAIL"
        if worst > tolerance:
            ok = False
        lines.append(
            f"  {verdict} pad {number:>3}  "
            f"dx={deltas[0]:+.4f} dy={deltas[1]:+.4f} "
            f"dw={deltas[2]:+.4f} dh={deltas[3]:+.4f}  (worst {worst:.4f} mm)"
        )
    return ok, lines


def identical(reference: Path, candidate: Path) -> bool:
    """Byte-identical files. For the holdout this means the trap was hit."""
    return reference.read_bytes() == candidate.read_bytes()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE)
    parser.add_argument(
        "--not-identical-to",
        type=Path,
        action="append",
        default=[],
        help="assert the candidate is NOT a byte-copy of this file. For the "
        "blind holdout, point it at the near-miss footprint: identity there "
        "means the trap was hit.",
    )
    args = parser.parse_args(argv)

    ok, lines = grade(args.reference, args.candidate, args.tolerance)
    print(f"reference: {args.reference}")
    print(f"candidate: {args.candidate}")
    print(f"tolerance: +/-{args.tolerance} mm")
    print("\n".join(lines))

    for other in args.not_identical_to:
        if other.exists() and identical(other, args.candidate):
            ok = False
            print(f"FAIL: the candidate is byte-identical to {other}")

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
