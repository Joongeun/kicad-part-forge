"""Phase 0b gate: does our IPC-7351B maths reproduce KiCad's official geometry?

This is the check that catches the class of error that scraps boards. For each
package we feed in the *same* input dimensions the KiCad library team used
(taken from their published size-definition YAML, cited per case) and assert the
resulting land geometry matches the shipped .kicad_mod exactly.

If this fails, the plan's fallback is to vendor kicad-library-generators
wholesale rather than owning the arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from kifab.emit import sexpr
from kifab.ipc.rules import get_class, gullwing_class, land_pattern
from kifab.ipc.toleranced import Tol

SHARED = Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints")

pytestmark = pytest.mark.skipif(
    not SHARED.is_dir(), reason="KiCad installation not found on this machine"
)

# Land geometry is quoted to 4 decimals in the libraries; anything beyond
# 0.1 um is floating-point noise, not a real disagreement.
TOL_MM = 1e-4


@dataclass(frozen=True)
class Case:
    """One package: inputs from KiCad's size definitions, output from its library."""

    footprint: str
    library: str
    ipc_class: str
    lead_outside: Tol
    lead_width: Tol
    lead_len: Tol
    source: str
    # Which pad orientation to compare (x-axis row: length along x).
    expect_length: float
    expect_width: float
    expect_centre: float


CASES = [
    Case(
        # data/package/gullwing/qfp_lqfp_tqfp_vqfp_wqfp.yaml :: TQFP-48_7x7mm_P0.5mm
        # (LQFP-48_7x7mm_P0.5mm inherits it). overall_size 9.00 is a JEDEC
        # basic dimension, i.e. exact with no tolerance.
        footprint="LQFP-48_7x7mm_P0.5mm",
        library="Package_QFP.pretty",
        ipc_class=gullwing_class(0.5),
        lead_outside=Tol.exact(9.00),
        lead_width=Tol.span(0.17, 0.27),
        lead_len=Tol.span(0.45, 0.75),
        source="JEDEC MS-026 BBC",
        expect_length=1.475,
        expect_width=0.30,
        expect_centre=4.1625,
    ),
    Case(
        # data/package/gullwing/soic.yaml :: SOIC-8_3.9x4.9mm_P1.27mm
        footprint="SOIC-8_3.9x4.9mm_P1.27mm",
        library="Package_SO.pretty",
        ipc_class=gullwing_class(1.27),
        lead_outside=Tol.plus_minus(6.0, 0.2),
        lead_width=Tol.span(0.31, 0.51),
        lead_len=Tol.span(0.40, 1.27),
        source="JEDEC MS-012AA",
        expect_length=1.95,
        expect_width=0.60,
        expect_centre=2.475,
    ),
    Case(
        # data/package/no_lead/qfn-3x.yaml :: QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm
        # No-lead: the terminals are flush with the body, so lead_outside is
        # the body size.
        footprint="QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm",
        library="Package_DFN_QFN.pretty",
        ipc_class="ipc_spec_flat_no_lead",
        lead_outside=Tol.plus_minus(5.0, 0.1),
        lead_width=Tol.plus_minus(0.25, 0.05),
        lead_len=Tol.plus_minus(0.40, 0.1),
        source="LTC QFN_32_05-08-1693",
        expect_length=0.875,
        expect_width=0.25,
        expect_centre=2.4375,
    ),
    Case(
        # data/package/gullwing/sot.yaml :: SOT-23-5
        footprint="SOT-23-5",
        library="Package_TO_SOT_SMD.pretty",
        ipc_class=gullwing_class(0.95),
        lead_outside=Tol.exact(2.8),
        lead_width=Tol.span(0.30, 0.50),
        lead_len=Tol.span(0.30, 0.60),
        source="JEDEC MO-178 Var AA",
        expect_length=1.325,
        expect_width=0.60,
        expect_centre=1.1375,
    ),
]


def _official_pads(footprint: str, library: str) -> list[tuple[float, float, float, float]]:
    """(centre_x, centre_y, size_x, size_y) for every pad in a shipped footprint."""
    path = SHARED / library / f"{footprint}.kicad_mod"
    assert path.exists(), f"missing reference footprint: {path}"
    tree = sexpr.parse(path.read_text(encoding="utf-8"))
    pads = []
    for pad in sexpr.find_all(tree, "pad"):
        at = sexpr.find(pad, "at")
        size = sexpr.find(pad, "size")
        assert at and size
        pads.append(
            (float(at[1]), float(at[2]), float(size[1]), float(size[2]))
        )
    return pads


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.footprint)
def test_land_geometry_matches_official(case: Case) -> None:
    """Computed land geometry must equal the shipped footprint's."""
    result = land_pattern(
        device_class=get_class(case.ipc_class),
        lead_outside=case.lead_outside,
        lead_width=case.lead_width,
        lead_len=case.lead_len,
        density="nominal",
    )

    assert result.length == pytest.approx(case.expect_length, abs=TOL_MM), (
        f"{case.footprint}: land length {result.length:.4f} != "
        f"official {case.expect_length} (Zmax={result.Zmax}, Gmin={result.Gmin})"
    )
    assert result.Xmax == pytest.approx(case.expect_width, abs=TOL_MM), (
        f"{case.footprint}: land width {result.Xmax:.4f} != official {case.expect_width}"
    )
    assert result.centre == pytest.approx(case.expect_centre, abs=TOL_MM), (
        f"{case.footprint}: land centre {result.centre:.4f} != "
        f"official {case.expect_centre}"
    )


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.footprint)
def test_expectations_actually_match_the_shipped_library(case: Case) -> None:
    """Guard the guard: our `expect_*` values must come from the real file.

    Without this, a typo in the expectations could make the gate above pass
    against a number nobody ever shipped.
    """
    pads = _official_pads(case.footprint, case.library)
    # Perimeter lands on the x-axis rows: length along x, width along y.
    row = [
        p
        for p in pads
        if abs(p[2] - case.expect_length) < TOL_MM
        and abs(p[3] - case.expect_width) < TOL_MM
    ]
    assert row, (
        f"{case.footprint}: no pad in the shipped footprint has size "
        f"{case.expect_length} x {case.expect_width}; sizes present: "
        f"{sorted({(p[2], p[3]) for p in pads})}"
    )
    assert max(abs(p[0]) for p in row) == pytest.approx(case.expect_centre, abs=TOL_MM)


def test_shipped_sot23_is_stale_not_our_maths() -> None:
    """SOT-23 does not match its current size definition — and that is upstream.

    Feeding the *current* sot.yaml dimensions (lead_len 0.40..0.60) yields
    Gmin=0.35, but the shipped footprint implies Gmin=0.40. Feeding
    lead_len 0.40..0.55 reproduces the shipped file exactly, so the footprint
    was generated before that dimension was revised.

    This is kept as a test, not deleted, because it pins down an assumption the
    whole geometry gate rests on: a mismatch against a shipped footprint does
    NOT automatically mean our arithmetic is wrong. The official library is not
    guaranteed to be in sync with the size definitions it was generated from.
    If upstream ever regenerates SOT-23, this test fails and tells us to move it
    back into CASES.
    """
    cls = get_class(gullwing_class(0.95))
    common = dict(
        device_class=cls,
        lead_outside=Tol.span(2.10, 2.64),
        lead_width=Tol.span(0.30, 0.50),
        density="nominal",
    )

    current_yaml = land_pattern(lead_len=Tol.span(0.40, 0.60), **common)
    historical = land_pattern(lead_len=Tol.span(0.40, 0.55), **common)

    pads = _official_pads("SOT-23", "Package_TO_SOT_SMD.pretty")
    shipped_length = pads[0][2]
    shipped_centre = max(abs(p[0]) for p in pads)

    # The historical dimension reproduces the shipped file exactly...
    assert historical.length == pytest.approx(shipped_length, abs=TOL_MM)
    assert historical.centre == pytest.approx(shipped_centre, abs=TOL_MM)
    # ...and the current one does not, by exactly one 0.05 rounding step on Gmin.
    assert current_yaml.Zmax == pytest.approx(historical.Zmax, abs=TOL_MM)
    assert current_yaml.Gmin == pytest.approx(historical.Gmin - 0.05, abs=TOL_MM)


def test_tol_rms_propagation() -> None:
    """RMS combination must shrink the effective span, never the true one."""
    a = Tol.span(0.4, 1.27)
    doubled = a * 2
    assert doubled.minimum == pytest.approx(0.8)
    assert doubled.maximum == pytest.approx(2.54)
    # RMS tolerance is smaller than the linear sum, so the RMS extremes sit inside.
    assert doubled.ipc_tol_RMS < doubled.ipc_tol
    assert doubled.minimum_RMS > doubled.minimum
    assert doubled.maximum_RMS < doubled.maximum


def test_tol_exact_has_no_tolerance() -> None:
    exact = Tol.exact(9.0)
    assert exact.ipc_tol == 0
    assert exact.minimum_RMS == exact.maximum_RMS == 9.0


def test_tol_parse_kicad_syntax() -> None:
    assert Tol.parse("0.45 .. 0.75") == Tol.span(0.45, 0.75)
    assert Tol.parse("0.17 .. 0.22 .. 0.27") == Tol.span(0.17, 0.27)
    assert Tol.parse(9.0) == Tol.exact(9.0)
    assert Tol.parse("7.00") == Tol.exact(7.0)


def test_tol_rejects_reversed_range() -> None:
    with pytest.raises(ValueError, match="backwards"):
        Tol(minimum=2.0, maximum=1.0, ipc_tol_RMS=0.0)


def test_gullwing_class_splits_at_0_625() -> None:
    assert gullwing_class(0.5) == "ipc_spec_gw_small_pitch"
    assert gullwing_class(0.625) == "ipc_spec_gw_small_pitch"
    assert gullwing_class(0.65) == "ipc_spec_gw_large_pitch"
    assert gullwing_class(1.27) == "ipc_spec_gw_large_pitch"


def test_unknown_class_and_density_error_clearly() -> None:
    with pytest.raises(ValueError, match="unknown IPC device class"):
        get_class("ipc_spec_nonexistent")
    with pytest.raises(ValueError, match="unknown density"):
        get_class("ipc_spec_gw_large_pitch").for_density("enormous")
