"""Phase 5 gate: does the no-lead (DFN/QFN) family reproduce official geometry?

Same shape as the Phase 0b gullwing gate in `test_ipc.py`: feed in the *same*
input dimensions the KiCad library team used — taken from the vendored
`vendor/ipc/qfn3x.yaml` size definitions, cited per case — and assert that every
numbered land lands where the shipped `.kicad_mod` puts it.

Deliberately **not** used as a case here: `DFN-12-1EP_2x3mm_P0.45mm_EP0.64x2.4mm`
or any other 2x3 mm 12-lead part. That is the LTC5552 blind-holdout trap, and a
test that pins its geometry would leak the answer into the repository.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from kifab.emit import sexpr
from kifab.ipc import no_lead
from kifab.ipc.toleranced import Tol
from kifab.ir import Body, DualNoLead, ExposedPadSpec, QuadNoLead

SHARED = Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints")

pytestmark = pytest.mark.skipif(
    not SHARED.is_dir(), reason="KiCad installation not found on this machine"
)

TOL_MM = 1e-4


@dataclass(frozen=True)
class Case:
    footprint: str
    kwargs: dict = field(default_factory=dict)
    source: str = ""


CASES = [
    Case(
        # vendor/ipc/qfn3x.yaml :: QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm
        footprint="QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm",
        source="Analog QFN_32_05-08-1693",
        kwargs=dict(
            body=Body(x=5.0, y=5.0),
            body_tolerance=0.1,
            pins_x=8,
            pins_y=8,
            pitch=0.5,
            lead_width=Tol.plus_minus(0.25, 0.05),
            lead_length=Tol.plus_minus(0.40, 0.10),
            exposed_pad=ExposedPadSpec(size_x=3.45, size_y=3.45),
        ),
    ),
    Case(
        # vendor/ipc/qfn3x.yaml :: QFN-36-1EP_5x6mm_P0.5mm_EP3.6x4.1mm.
        # Rectangular, and the sides carry different land counts — the case a
        # `pin_count // 4` generator gets silently wrong.
        footprint="QFN-36-1EP_5x6mm_P0.5mm_EP3.6x4.1mm",
        source="Trinamic TMC2100 datasheet p43",
        kwargs=dict(
            body=Body(x=5.0, y=6.0),
            body_tolerance=0.1,
            pins_x=8,
            pins_y=10,
            pitch=0.5,
            lead_width=Tol.span(0.20, 0.30),
            lead_length=Tol.span(0.35, 0.45),
            exposed_pad=ExposedPadSpec(size_x=3.6, size_y=4.1),
        ),
    ),
    Case(
        # vendor/ipc/qfn3x.yaml :: QFN-32-1EP_7x7mm_P0.65mm_EP4.65x4.65mm.
        # A larger pitch and an exact (zero-tolerance) body, which exercises
        # the Tol.exact path through the same equations.
        footprint="QFN-32-1EP_7x7mm_P0.65mm_EP4.65x4.65mm",
        source="Atmel ATmega32M1 datasheet p426",
        kwargs=dict(
            body=Body(x=7.0, y=7.0),
            body_tolerance=0.0,
            pins_x=8,
            pins_y=8,
            pitch=0.65,
            lead_width=Tol.span(0.25, 0.37),
            lead_length=Tol.span(0.50, 0.70),
            exposed_pad=ExposedPadSpec(size_x=4.65, size_y=4.65),
        ),
    ),
]


def _official_copper_pads(footprint: str) -> dict[str, tuple[float, float, float, float]]:
    """Numbered copper pads of a shipped footprint, keyed by pad number.

    Anonymous pads (KiCad's paste sub-apertures carry an empty number) are
    skipped: the paste plan is a separate concern from land geometry.
    """
    path = SHARED / "Package_DFN_QFN.pretty" / f"{footprint}.kicad_mod"
    assert path.exists(), f"missing reference footprint: {path}"
    tree = sexpr.parse(path.read_text(encoding="utf-8"))
    pads: dict[str, tuple[float, float, float, float]] = {}
    for pad in sexpr.find_all(tree, "pad"):
        number = pad[1].strip('"')
        if not number:
            continue
        at, size = sexpr.find(pad, "at"), sexpr.find(pad, "size")
        assert at and size
        pads[number] = (float(at[1]), float(at[2]), float(size[1]), float(size[2]))
    return pads


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.footprint)
def test_quad_no_lead_matches_official(case: Case) -> None:
    """Every computed land must equal the shipped one, position and size."""
    package = QuadNoLead(**case.kwargs)
    official = _official_copper_pads(case.footprint)
    computed = {p.number: (p.at[0], p.at[1], p.size[0], p.size[1]) for p in package.resolve_pads()}

    assert set(computed) == set(official), (
        f"{case.footprint}: pad numbers differ — "
        f"only computed {sorted(set(computed) - set(official))}, "
        f"only official {sorted(set(official) - set(computed))}"
    )
    for number, got in sorted(computed.items(), key=lambda kv: int(kv[0])):
        want = official[number]
        assert got == pytest.approx(want, abs=TOL_MM), (
            f"{case.footprint} pad {number}: computed {got} != official {want} "
            f"(inputs from {case.source})"
        )


def test_dual_no_lead_numbering_and_symmetry() -> None:
    """DFN shares the arithmetic; what is its own is the numbering."""
    package = DualNoLead(
        body=Body(x=3.0, y=3.0),
        body_tolerance=0.1,
        pin_count=8,
        pitch=0.5,
        lead_width=Tol.span(0.20, 0.30),
        lead_length=Tol.span(0.30, 0.50),
    )
    pads = {p.number: p for p in package.resolve_pads()}
    assert sorted(pads, key=int) == [str(i) for i in range(1, 9)]
    # Pin 1 is top of the left column: negative x, most negative y.
    assert pads["1"].at[0] < 0
    assert pads["1"].at[1] == min(p.at[1] for p in pads.values())
    # Column 2 runs bottom-to-top, so pin 8 faces pin 1 across the package.
    assert pads["8"].at[0] == pytest.approx(-pads["1"].at[0])
    assert pads["8"].at[1] == pytest.approx(pads["1"].at[1])
    # Every land is the same size and the two columns mirror.
    assert {p.size for p in pads.values()} == {pads["1"].size}


def test_dual_no_lead_shares_the_gullwing_arithmetic() -> None:
    """A DFN column must equal the equivalent QFN column, land for land."""
    common = dict(
        body=Body(x=4.0, y=4.0),
        body_tolerance=0.1,
        pitch=0.5,
        lead_width=Tol.span(0.20, 0.30),
        lead_length=Tol.span(0.30, 0.50),
    )
    dual = DualNoLead(pin_count=12, **common)
    quad = QuadNoLead(pins_x=6, pins_y=6, **common)
    dual_left = [(p.at[0], p.at[1], p.size) for p in dual.resolve_pads()[:6]]
    quad_left = [(p.at[0], p.at[1], p.size) for p in quad.resolve_pads()[:6]]
    assert dual_left == quad_left


def test_exposed_pad_too_close_is_refused_not_shrunk() -> None:
    """An EP that crowds the lands is an error, never a silent heel reduction."""
    with pytest.raises(ValueError, match="IPC's floor is 0.2"):
        QuadNoLead(
            body=Body(x=5.0, y=5.0),
            body_tolerance=0.1,
            pins_x=8,
            pins_y=8,
            pitch=0.5,
            lead_width=Tol.plus_minus(0.25, 0.05),
            lead_length=Tol.plus_minus(0.40, 0.10),
            exposed_pad=ExposedPadSpec(size_x=4.5, size_y=4.5),
        )


def test_pull_back_selects_the_other_ipc_table() -> None:
    """Pull-back terminals use IPC-7351B table 3-18, not 3-15."""
    assert no_lead.no_lead_class(None) == "ipc_spec_flat_no_lead"
    assert (
        no_lead.no_lead_class(Tol.exact(0.1)) == "ipc_spec_flat_no_lead_pull_back"
    )
    common = dict(
        body=Body(x=4.0, y=4.0),
        body_tolerance=0.1,
        pins_x=4,
        pins_y=4,
        pitch=0.5,
        lead_width=Tol.span(0.20, 0.30),
        lead_length=Tol.span(0.30, 0.50),
    )
    flush = QuadNoLead(**common)
    pulled = QuadNoLead(pull_back=Tol.exact(0.15), **common)
    # A pulled-back terminal starts further in, so its land must too.
    assert abs(pulled.resolve_pads()[0].at[0]) < abs(flush.resolve_pads()[0].at[0])


def test_paste_apertures_share_the_pad_number_and_shrink_by_area() -> None:
    package = QuadNoLead(
        body=Body(x=5.0, y=5.0),
        body_tolerance=0.1,
        pins_x=8,
        pins_y=8,
        pitch=0.5,
        lead_width=Tol.plus_minus(0.25, 0.05),
        lead_length=Tol.plus_minus(0.40, 0.10),
        exposed_pad=ExposedPadSpec(
            size_x=3.45, size_y=3.45, paste_pads=(3, 3), paste_coverage=0.81
        ),
    )
    pads = package.resolve_pads()
    copper = [p for p in pads if p.number == "33" and p.layers == ["F.Cu", "F.Mask"]]
    paste = [p for p in pads if p.aperture]
    assert len(copper) == 1 and len(paste) == 9
    assert all(p.number == "33" and p.layers == ["F.Paste"] for p in paste)
    # coverage 0.81 -> linear factor 0.9 of the 1.15 mm cell.
    assert paste[0].size == pytest.approx((1.035, 1.035), abs=1e-6)
    total = sum(p.size[0] * p.size[1] for p in paste)
    assert total == pytest.approx(0.81 * 3.45 * 3.45, rel=1e-6)


def test_no_lead_family_never_needs_a_lead_span() -> None:
    """The terminals are flush with the body, which is the whole point.

    Stated as a test because the single most likely misuse of this family is
    passing a gull-wing lead span where the body size belongs, which would
    push every land outward by the lead length.
    """
    with pytest.raises(Exception):
        QuadNoLead(
            body=Body(x=5.0, y=5.0),
            pins_x=8,
            pins_y=8,
            pitch=0.5,
            lead_span=Tol.exact(7.0),  # type: ignore[call-arg]
            lead_width=Tol.span(0.2, 0.3),
            lead_length=Tol.span(0.3, 0.5),
        )
