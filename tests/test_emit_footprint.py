"""The `.kicad_mod` emitter: KiCad 9 canonical form and land geometry.

The geometry tests here are the ones that matter most: they compare what kifab
computes from datasheet dimensions against the footprints KiCad itself ships.
A 0.1 mm pad error is invisible in review and scraps a board.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kifab.emit import sexpr
from kifab.emit.footprint import (
    FOOTPRINT_VERSION,
    Box,
    _silk_outline,
    _subtract,
    footprint_node,
    render_footprint,
)
from kifab.ir import Pad, Part, load_part

PARTS_DIR = Path(__file__).resolve().parent.parent / "parts"
SHARED = Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints")
TOL_MM = 1e-4


@pytest.fixture(scope="module")
def eeprom() -> Part:
    return load_part(PARTS_DIR / "24LC256.yaml")


@pytest.fixture(scope="module")
def mcu() -> Part:
    return load_part(PARTS_DIR / "STM32F103C8T6.yaml")


@pytest.fixture(scope="module")
def bead() -> Part:
    return load_part(PARTS_DIR / "BLM31PG601SN1L.yaml")


def _pads(text: str) -> dict[str, tuple[float, float, float, float]]:
    """(x, y, size_x, size_y) per pad number, from .kicad_mod source."""
    found = {}
    for m in re.finditer(
        r'\(pad "([^"]+)"[^\n]*\n\s*\(at ([-\d.]+) ([-\d.]+)[^\n]*\)\n\s*'
        r"\(size ([\d.]+) ([\d.]+)\)",
        text,
    ):
        found[m.group(1)] = tuple(float(m.group(i)) for i in range(2, 6))
    return found


# --------------------------------------------------------------------------
# Geometry against KiCad's own libraries — the check that catches board errors.
# --------------------------------------------------------------------------


@pytest.mark.skipif(not SHARED.is_dir(), reason="KiCad installation not found")
@pytest.mark.parametrize(
    "fixture,library",
    [("eeprom", "Package_SO.pretty"), ("mcu", "Package_QFP.pretty")],
)
def test_computed_lands_match_the_shipped_footprint(
    fixture: str, library: str, request: pytest.FixtureRequest
) -> None:
    part: Part = request.getfixturevalue(fixture)
    official = SHARED / library / f"{part.footprint.name}.kicad_mod"
    assert official.exists(), f"reference footprint missing: {official}"

    ours = _pads(render_footprint(part))
    theirs = _pads(official.read_text(encoding="utf-8"))

    assert set(ours) == set(theirs), "pad numbering differs from the official part"
    for number, mine in ours.items():
        for axis, (a, b) in enumerate(zip(mine, theirs[number])):
            assert a == pytest.approx(b, abs=TOL_MM), (
                f"{part.footprint.name} pad {number}: component {axis} "
                f"{a} != official {b}"
            )


# --------------------------------------------------------------------------
# Canonical KiCad 9 form (established by diffing `kicad-cli fp upgrade` output)
# --------------------------------------------------------------------------


def test_header_pins_the_format_version_and_generator_version(eeprom: Part) -> None:
    node = footprint_node(eeprom)
    assert sexpr.find(node, "version") == ["version", FOOTPRINT_VERSION]
    assert sexpr.find(node, "generator_version") == ["generator_version", '"9.0"']


def test_all_four_mandatory_properties_are_present(eeprom: Part) -> None:
    node = footprint_node(eeprom)
    names = [p[1].strip('"') for p in sexpr.find_all(node, "property")]
    assert names[:4] == ["Reference", "Value", "Datasheet", "Description"]


def test_pad_layer_order_is_cu_mask_paste(eeprom: Part) -> None:
    """Not Cu/Paste/Mask — kicad-cli canonicalises to this order."""
    for pad in sexpr.find_all(footprint_node(eeprom), "pad"):
        layers = sexpr.find(pad, "layers")
        assert layers[1:] == ['"F.Cu"', '"F.Mask"', '"F.Paste"']


@pytest.mark.parametrize("token", ["property", "fp_line", "fp_poly", "fp_text", "pad"])
def test_every_element_that_needs_a_uuid_has_one(eeprom: Part, token: str) -> None:
    elements = sexpr.find_all(footprint_node(eeprom), token)
    assert elements, f"no {token} in the generated footprint"
    for element in elements:
        assert sexpr.find(element, "uuid") is not None, element[:2]


def test_uuids_are_unique_within_a_footprint(mcu: Part) -> None:
    uuids = re.findall(r'\(uuid "([^"]+)"\)', render_footprint(mcu))
    assert len(uuids) == len(set(uuids)), "a derived UUID collided"


def test_uuids_differ_between_footprints(eeprom: Part, mcu: Part) -> None:
    a = set(re.findall(r'\(uuid "([^"]+)"\)', render_footprint(eeprom)))
    b = set(re.findall(r'\(uuid "([^"]+)"\)', render_footprint(mcu)))
    assert not (a & b)


def test_embedded_fonts_trailer_sits_before_the_3d_model(eeprom: Part) -> None:
    node = footprint_node(eeprom)
    tokens = [c[0] for c in node if isinstance(c, list)]
    assert tokens[-2:] == ["embedded_fonts", "model"]


def test_footprint_declares_its_mounting_technology(eeprom: Part, bead: Part) -> None:
    for part in (eeprom, bead):
        assert sexpr.find(footprint_node(part), "attr") == ["attr", "smd"]


def test_reparses_and_reprints_identically(mcu: Part) -> None:
    text = render_footprint(mcu)
    assert sexpr.dumps(sexpr.parse(text)) == text


def test_output_is_a_pure_function_of_the_ir(mcu: Part) -> None:
    assert render_footprint(mcu) == render_footprint(mcu)


# --------------------------------------------------------------------------
# Drawing rules
# --------------------------------------------------------------------------


def test_roundrect_ratio_caps_the_corner_radius_at_a_quarter_millimetre() -> None:
    part = Part.model_validate(
        {
            "mpn": "BIGPAD",
            "symbol": {"pins": [{"number": 1}, {"number": 2}]},
            "footprint": {
                "name": "BIGPAD_FP",
                "package": {
                    "family": "custom",
                    "body": {"x": 4, "y": 4},
                    "pads": [
                        {"number": "1", "at": [-2, 0], "size": [0.6, 0.6]},
                        {"number": "2", "at": [2, 0], "size": [2.0, 2.0]},
                    ],
                },
            },
        }
    )
    ratios = {
        pad[1]: float(sexpr.find(pad, "roundrect_rratio")[1])
        for pad in sexpr.find_all(footprint_node(part), "pad")
    }
    assert ratios['"1"'] == pytest.approx(0.25), "small pad keeps the 25% rule"
    assert ratios['"2"'] == pytest.approx(0.125), "large pad is capped at 0.25 mm"


def test_silk_is_trimmed_clear_of_pads_not_moved() -> None:
    """The trimmed stubs must keep the KLC clearance from every pad edge."""
    body = Box(-3.5, -3.5, 3.5, 3.5)
    pads = [
        Pad(number="1", at=(-4.0, 0.0), size=(1.5, 6.0)),
        Pad(number="2", at=(4.0, 0.0), size=(1.5, 6.0)),
    ]
    segments = _silk_outline(body, pads, offset=0.11, clearance=0.2, width=0.12)
    assert segments, "expected some silkscreen to survive"
    for (x0, y0), (x1, y1) in segments:
        for pad in pads:
            px0 = pad.at[0] - pad.size[0] / 2 - 0.2 - 0.06
            px1 = pad.at[0] + pad.size[0] / 2 + 0.2 + 0.06
            py0 = pad.at[1] - pad.size[1] / 2 - 0.2 - 0.06
            py1 = pad.at[1] + pad.size[1] / 2 + 0.2 + 0.06
            overlaps_x = min(x0, x1) < px1 - 1e-9 and px0 < max(x0, x1) - 1e-9
            overlaps_y = min(y0, y1) < py1 - 1e-9 and py0 < max(y0, y1) - 1e-9
            assert not (overlaps_x and overlaps_y), (
                f"silk {(x0, y0)}-{(x1, y1)} intrudes on pad {pad.number}"
            )


def test_an_edge_entirely_covered_by_pads_loses_its_silk() -> None:
    body = Box(-1.0, -1.0, 1.0, 1.0)
    pads = [Pad(number="1", at=(0.0, 0.0), size=(10.0, 10.0))]
    assert _silk_outline(body, pads, offset=0.11, clearance=0.2, width=0.12) == []


def test_subtract_keeps_only_usefully_long_fragments() -> None:
    assert _subtract((0.0, 10.0), [(2.0, 8.0)]) == [(0.0, 2.0), (8.0, 10.0)]
    assert _subtract((0.0, 10.0), [(0.05, 9.95)]) == []  # both stubs too short


def test_courtyard_encloses_every_pad_and_the_body(mcu: Part) -> None:
    node = footprint_node(mcu)
    lines = [
        line
        for line in sexpr.find_all(node, "fp_line")
        if sexpr.find(line, "layer") == ["layer", '"F.CrtYd"']
    ]
    assert len(lines) == 4
    xs, ys = [], []
    for line in lines:
        for token in ("start", "end"):
            point = sexpr.find(line, token)
            xs.append(float(point[1]))
            ys.append(float(point[2]))

    for pad in mcu.footprint.package.resolve_pads():
        assert pad.at[0] - pad.size[0] / 2 >= min(xs)
        assert pad.at[0] + pad.size[0] / 2 <= max(xs)
        assert pad.at[1] - pad.size[1] / 2 >= min(ys)
        assert pad.at[1] + pad.size[1] / 2 <= max(ys)


def test_fab_outline_chamfers_the_pin_one_corner(eeprom: Part) -> None:
    node = footprint_node(eeprom)
    fab = next(
        p
        for p in sexpr.find_all(node, "fp_poly")
        if sexpr.find(p, "layer") == ["layer", '"F.Fab"']
    )
    points = [
        (float(pt[1]), float(pt[2])) for pt in sexpr.find(fab, "pts")[1:]
    ]
    assert len(points) == 5, "a chamfered rectangle has five corners"
    # SOIC-8: body 3.9 x 4.9, so the chamfer is min(1.0, 3.9/4) = 0.975.
    assert (-1.95, -1.475) in points and (-0.975, -2.45) in points


def _silk_polys(part: Part) -> list:
    return [
        p
        for p in sexpr.find_all(footprint_node(part), "fp_poly")
        if sexpr.find(p, "layer") == ["layer", '"F.SilkS"']
    ]


def test_pin_one_marker_is_drawn_for_a_normal_package(eeprom: Part) -> None:
    marker = _silk_polys(eeprom)
    assert len(marker) == 1
    points = [(float(pt[1]), float(pt[2])) for pt in sexpr.find(marker[0], "pts")[1:]]
    # Apex nearest pad 1, base further out — it points at the pad.
    assert len(points) == 3
    assert max(p[1] for p in points) == pytest.approx(-2.465)
    assert sexpr.find(marker[0], "fill") == ["fill", "yes"]


def test_pin_one_marker_is_dropped_rather_than_drawn_over_a_pad() -> None:
    """A marker that cannot clear the pads must vanish, not violate clearance.

    The F.Fab chamfer is the pin-1 indicator that is always present, so losing
    the silk triangle is a downgrade, not a defect.
    """
    part = Part.model_validate(
        {
            "mpn": "CROWDED",
            "symbol": {"pins": [{"number": 1}, {"number": 2}]},
            "footprint": {
                "name": "CROWDED_FP",
                "package": {
                    "family": "custom",
                    "body": {"x": 2, "y": 4},
                    "pads": [
                        {"number": "1", "at": [0, 0.5], "size": [0.4, 0.4]},
                        {"number": "2", "at": [0, 1.5], "size": [0.4, 0.4]},
                    ],
                },
            },
        }
    )
    assert _silk_polys(part) == []


def test_reference_and_value_sit_outside_the_courtyard(eeprom: Part) -> None:
    node = footprint_node(eeprom)
    props = {p[1]: p for p in sexpr.find_all(node, "property")}
    ref_y = float(sexpr.find(props['"Reference"'], "at")[2])
    value_y = float(sexpr.find(props['"Value"'], "at")[2])
    assert ref_y < 0 < value_y
    assert sexpr.find(props['"Reference"'], "layer") == ["layer", '"F.SilkS"']
    assert sexpr.find(props['"Value"'], "layer") == ["layer", '"F.Fab"']


def test_through_hole_pads_get_a_drill_and_all_copper_layers() -> None:
    part = Part.model_validate(
        {
            "mpn": "THT2",
            "symbol": {"pins": [{"number": 1}, {"number": 2}]},
            "footprint": {
                "name": "THT2_FP",
                "package": {
                    "family": "custom",
                    "body": {"x": 5, "y": 2},
                    "mount_type": "through_hole",
                    "pads": [
                        {
                            "number": str(n),
                            "at": [x, 0],
                            "size": [1.6, 1.6],
                            "type": "thru_hole",
                            "shape": "circle",
                            "drill": 0.8,
                        }
                        for n, x in ((1, -2.54), (2, 2.54))
                    ],
                },
            },
        }
    )
    node = footprint_node(part)
    assert sexpr.find(node, "attr") == ["attr", "through_hole"]
    for pad in sexpr.find_all(node, "pad"):
        assert sexpr.find(pad, "drill") == ["drill", "0.8"]
        assert sexpr.find(pad, "layers")[1:] == ['"*.Cu"', '"*.Mask"']
