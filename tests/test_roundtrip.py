"""Phase 0a gate: is the S-expression layer safe to build the emitter on?

Three invariants, in decreasing order of how much they matter:

1. `test_semantic_roundtrip_broad`  - NO DATA LOSS across the whole shipped
   corpus. This is the real gate. If it fails, the parser is wrong.
2. `test_idempotent_broad`          - emit -> parse -> emit is stable, so our
   own golden files can never drift.
3. `test_byte_exact_on_generated_shapes` - byte-exact for the node shapes this
   package actually emits (pads, lines, rects, arcs, small polys).

Byte-exact round-trip of *arbitrary* third-party files is explicitly NOT
asserted; see the sexpr module docstring for the measurements behind that.

These tests read the KiCad installation and skip cleanly when it is absent.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from kifab.emit import sexpr

SHARED = Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport")
FOOTPRINTS = SHARED / "footprints"
SYMBOLS = SHARED / "symbols"

pytestmark = pytest.mark.skipif(
    not SHARED.is_dir(), reason="KiCad installation not found on this machine"
)

# Fixed seed so a failure is reproducible from the report alone.
SAMPLE_SEED = 20241229


def _sample(paths: list[Path], count: int) -> list[Path]:
    ordered = sorted(paths)
    rng = random.Random(SAMPLE_SEED)
    return ordered if len(ordered) <= count else rng.sample(ordered, count)


def _first_diff(a: str, b: str) -> str:
    a_lines, b_lines = a.splitlines(), b.splitlines()
    for i, (x, y) in enumerate(zip(a_lines, b_lines), start=1):
        if x != y:
            return f"line {i}:\n  expected {x!r}\n  got      {y!r}"
    return f"line count differs: {len(a_lines)} vs {len(b_lines)}"


# --------------------------------------------------------------------------
# 1. No data loss — the gate that decides whether we own the emitter.
# --------------------------------------------------------------------------


def test_semantic_roundtrip_broad() -> None:
    """Parsing then re-emitting must preserve the tree exactly, for 400 files."""
    paths = _sample(list(FOOTPRINTS.rglob("*.kicad_mod")), 400)
    assert paths, "no footprints found — is the KiCad install complete?"

    failures = []
    for path in paths:
        tree = sexpr.parse(path.read_text(encoding="utf-8"))
        reparsed = sexpr.parse(sexpr.dumps(tree))
        if reparsed != tree:
            failures.append(path.name)

    assert not failures, f"{len(failures)} files lost data: {failures[:10]}"


def test_semantic_roundtrip_symbol_libraries() -> None:
    """Same invariant across whole symbol libraries (thousands of symbols)."""
    paths = _sample(list(SYMBOLS.glob("*.kicad_sym")), 10)
    assert paths, "no symbol libraries found"

    failures = []
    for path in paths:
        tree = sexpr.parse(path.read_text(encoding="utf-8"))
        if sexpr.parse(sexpr.dumps(tree)) != tree:
            failures.append(path.name)

    assert not failures, f"symbol libraries lost data: {failures}"


# --------------------------------------------------------------------------
# 2. Idempotence — our golden files can never drift.
# --------------------------------------------------------------------------


def test_idempotent_broad() -> None:
    """emit -> parse -> emit must be byte-stable for 400 footprints."""
    paths = _sample(list(FOOTPRINTS.rglob("*.kicad_mod")), 400)

    failures = []
    for path in paths:
        once = sexpr.dumps(sexpr.parse(path.read_text(encoding="utf-8")))
        twice = sexpr.dumps(sexpr.parse(once))
        if once != twice:
            failures.append(f"{path.name}\n{_first_diff(once, twice)}")

    assert not failures, "not idempotent:\n\n" + "\n\n".join(failures[:5])


# --------------------------------------------------------------------------
# 3. Byte-exact for the shapes we generate.
# --------------------------------------------------------------------------

GENERATED_SHAPES = """\
(footprint "TEST-1"
	(version 20241229)
	(generator "kifab")
	(layer "F.Cu")
	(attr smd)
	(property "Reference" "REF**"
		(at 0 -2.55 0)
		(layer "F.SilkS")
		(effects
			(font
				(size 1 1)
				(thickness 0.15)
			)
		)
	)
	(fp_line
		(start -1 -1.61)
		(end 1 -1.61)
		(stroke
			(width 0.12)
			(type solid)
		)
		(layer "F.SilkS")
	)
	(fp_poly
		(pts
			(xy -0.74 -0.54) (xy -1.02 -0.54) (xy -0.74 -0.82) (xy -0.74 -0.54)
		)
		(stroke
			(width 0.12)
			(type solid)
		)
		(fill yes)
		(layer "F.SilkS")
	)
	(pad "1" smd roundrect
		(at -1.4 0)
		(size 1.06 0.65)
		(layers "F.Cu" "F.Paste" "F.Mask")
		(roundrect_rratio 0.234375)
	)
	(pad "13" smd rect
		(at 0 0)
		(size 0.64 2.4)
		(layers "F.Cu" "F.Mask")
		(thermal_bridge_angle 45)
	)
)
"""


def test_byte_exact_on_generated_shapes() -> None:
    """Every node shape the emitter produces must round-trip byte-for-byte."""
    rendered = sexpr.dumps(sexpr.parse(GENERATED_SHAPES))
    assert rendered == GENERATED_SHAPES, _first_diff(GENERATED_SHAPES, rendered)


def test_pts_packing_respects_ceiling() -> None:
    """`pts` packs children, never exceeding the observed 8-per-line ceiling."""
    pts = ["pts"] + [["xy", sexpr.fmt_num(i), "0"] for i in range(30)]
    out = sexpr.write([*["fp_poly"], pts], 0)
    rows = [ln for ln in out.splitlines() if "(xy " in ln]
    assert rows, "expected packed xy rows"
    assert all(ln.count("(xy ") <= sexpr.PACKED_CHILDREN["pts"] for ln in rows)
    assert any(ln.count("(xy ") > 1 for ln in rows), "should pack, not one-per-line"


# --------------------------------------------------------------------------
# Formatting primitives.
# --------------------------------------------------------------------------


def test_fmt_num_matches_kicad_style() -> None:
    assert sexpr.fmt_num(0) == "0"
    assert sexpr.fmt_num(-0.0) == "0"
    assert sexpr.fmt_num(1.0) == "1"
    assert sexpr.fmt_num(1.27) == "1.27"
    assert sexpr.fmt_num(-5.85) == "-5.85"
    assert sexpr.fmt_num(0.234375) == "0.234375"


def test_quote_escapes() -> None:
    assert sexpr.quote("plain") == '"plain"'
    assert sexpr.quote('has "quotes"') == '"has \\"quotes\\""'
    assert sexpr.quote("back\\slash") == '"back\\\\slash"'


def test_parse_rejects_malformed() -> None:
    for bad in ["(unbalanced", "(a) (b)", '(unterminated "string)', ")"]:
        with pytest.raises(sexpr.SexprError):
            sexpr.parse(bad)


def test_find_helpers() -> None:
    tree = sexpr.parse('(footprint "X" (layer "F.Cu") (pad "1") (pad "2"))')
    assert sexpr.find(tree, "layer") == ["layer", '"F.Cu"']
    assert len(sexpr.find_all(tree, "pad")) == 2
    assert sexpr.find(tree, "nope") is None
