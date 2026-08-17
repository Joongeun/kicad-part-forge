"""The `.kicad_sym` emitter: layout policy and KiCad 9 canonical form."""

from __future__ import annotations

from pathlib import Path

import pytest

from kifab.emit import sexpr
from kifab.emit.symbol import (
    SYMBOL_VERSION,
    _assign_slots,
    layout_unit,
    library_node,
    render_library,
    symbol_node,
)
from kifab.ir import Part, Pin, Side, SymbolStyle, load_part

PARTS_DIR = Path(__file__).resolve().parent.parent / "parts"
GRID = 2.54


@pytest.fixture(scope="module")
def eeprom() -> Part:
    return load_part(PARTS_DIR / "24LC256.yaml")


@pytest.fixture(scope="module")
def mcu() -> Part:
    return load_part(PARTS_DIR / "STM32F103C8T6.yaml")


def _find_all_deep(node, token):
    """Every node with this head token, at any depth."""
    found = []
    for child in node:
        if isinstance(child, list):
            if child and child[0] == token:
                found.append(child)
            found += _find_all_deep(child, token)
    return found


# --------------------------------------------------------------------------
# Layout policy
# --------------------------------------------------------------------------


def test_slots_fill_consecutively_when_unset() -> None:
    pins = [Pin(number=str(i), side=Side.LEFT) for i in range(1, 4)]
    assert _assign_slots(pins) == {"1": 0, "2": 1, "3": 2}


def test_explicit_slots_leave_a_gap_for_the_rest_to_flow_around() -> None:
    pins = [
        Pin(number="1", side=Side.LEFT),
        Pin(number="2", side=Side.LEFT, slot=0),
        Pin(number="3", side=Side.LEFT),
    ]
    # "2" holds slot 0, so "1" takes 1 and "3" takes 2.
    assert _assign_slots(pins) == {"2": 0, "1": 1, "3": 2}


def test_every_pin_lands_on_the_schematic_grid(mcu: Part) -> None:
    """Off-grid pins cannot be wired reliably; the IR makes them inexpressible."""
    layout = layout_unit(mcu.symbol.pins_for(1), mcu.symbol.style)
    for placed in layout.pins:
        assert abs(placed.x / GRID - round(placed.x / GRID)) < 1e-9, placed
        assert abs(placed.y / GRID - round(placed.y / GRID)) < 1e-9, placed


def test_pin_angles_point_into_the_body() -> None:
    """KiCad measures a pin's angle from its connection point inwards."""
    pins = [
        Pin(number="1", side=Side.LEFT),
        Pin(number="2", side=Side.RIGHT),
        Pin(number="3", side=Side.TOP),
        Pin(number="4", side=Side.BOTTOM),
    ]
    placed = {p.pin.number: p for p in layout_unit(pins, SymbolStyle()).pins}
    assert (placed["1"].angle, placed["1"].x < 0) == (0, True)
    assert (placed["2"].angle, placed["2"].x > 0) == (180, True)
    assert (placed["3"].angle, placed["3"].y > 0) == (270, True)
    assert (placed["4"].angle, placed["4"].y < 0) == (90, True)


def test_body_grows_to_keep_opposing_names_apart() -> None:
    short = layout_unit(
        [Pin(number="1", name="A", side=Side.LEFT),
         Pin(number="2", name="B", side=Side.RIGHT)],
        SymbolStyle(),
    )
    long = layout_unit(
        [Pin(number="1", name="A_VERY_LONG_NAME", side=Side.LEFT),
         Pin(number="2", name="ANOTHER_LONG_NAME", side=Side.RIGHT)],
        SymbolStyle(),
    )
    assert long.width > short.width


@pytest.mark.parametrize("count", [1, 2, 3, 4, 7, 12, 26])
def test_body_half_size_is_on_grid_so_top_and_bottom_pins_are_too(count: int) -> None:
    """The body's *half* size is snapped, not its full size.

    A top pin's connection point is `height/2 + pin_length`, so if half the
    height were off-grid every top and bottom pin would be too.
    """
    pins = [Pin(number=str(i), side=Side.LEFT) for i in range(count)]
    pins += [Pin(number=f"t{i}", side=Side.TOP) for i in range(count)]
    layout = layout_unit(pins, SymbolStyle())
    for half in (layout.width / 2, layout.height / 2):
        assert abs(half / GRID - round(half / GRID)) < 1e-9, half


@pytest.mark.parametrize("count", [1, 2, 3, 4, 7, 12, 26])
def test_body_encloses_every_pin_with_a_grid_of_margin(count: int) -> None:
    pins = [Pin(number=str(i), side=Side.LEFT) for i in range(count)]
    layout = layout_unit(pins, SymbolStyle())
    for placed in layout.pins:
        assert abs(placed.y) + GRID <= layout.height / 2 + 1e-9


def test_left_and_right_slot_n_share_a_row() -> None:
    pins = [
        Pin(number="1", side=Side.LEFT, slot=0),
        Pin(number="2", side=Side.LEFT, slot=1),
        Pin(number="3", side=Side.RIGHT, slot=1),
    ]
    placed = {p.pin.number: p for p in layout_unit(pins, SymbolStyle()).pins}
    assert placed["2"].y == pytest.approx(placed["3"].y)


def test_explicit_body_size_overrides_the_computed_one() -> None:
    layout = layout_unit(
        [Pin(number="1", side=Side.LEFT)],
        SymbolStyle(body_width=20.32, body_height=7.62),
    )
    assert (layout.width, layout.height) == (20.32, 7.62)


def test_pin_length_override_moves_only_that_pin() -> None:
    pins = [
        Pin(number="1", side=Side.LEFT),
        Pin(number="2", side=Side.LEFT, length=5.08),
    ]
    placed = {p.pin.number: p for p in layout_unit(pins, SymbolStyle()).pins}
    assert placed["2"].x < placed["1"].x


# --------------------------------------------------------------------------
# Canonical KiCad 9 form
# --------------------------------------------------------------------------


def test_library_header_pins_the_format_version(eeprom: Part) -> None:
    node = library_node([eeprom])
    assert sexpr.find(node, "version") == ["version", SYMBOL_VERSION]
    assert sexpr.find(node, "generator_version") == ["generator_version", '"9.0"']


def test_every_mandatory_property_is_present(eeprom: Part) -> None:
    """kicad-cli adds these if they are missing, so emitting them is canonical."""
    node = symbol_node(eeprom)
    names = [p[1].strip('"') for p in sexpr.find_all(node, "property")]
    for required in ("Reference", "Value", "Footprint", "Datasheet", "Description"):
        assert required in names, f"{required} missing from {names}"


def test_symbol_closes_with_the_embedded_fonts_trailer(eeprom: Part) -> None:
    node = symbol_node(eeprom)
    assert node[-1] == ["embedded_fonts", "no"]


def test_symbols_carry_no_uuids(eeprom: Part) -> None:
    """Unlike footprints. `sym upgrade` adds none, so neither do we."""
    assert "uuid" not in render_library([eeprom])


def test_footprint_property_points_at_the_generated_footprint(eeprom: Part) -> None:
    node = symbol_node(eeprom)
    prop = next(
        p for p in sexpr.find_all(node, "property") if p[1] == '"Footprint"'
    )
    assert prop[2] == '"kifab:SOIC-8_3.9x4.9mm_P1.27mm"'


def test_single_unit_symbol_uses_the_official_0_1_body_convention(eeprom: Part) -> None:
    node = symbol_node(eeprom)
    sub = [s[1].strip('"') for s in sexpr.find_all(node, "symbol")]
    assert sub == ["24LC256_0_1", "24LC256_1_1"]
    body_block = sexpr.find_all(node, "symbol")[0]
    assert sexpr.find(body_block, "rectangle") is not None


def test_multi_unit_symbol_gets_a_body_per_unit() -> None:
    part = Part.model_validate(
        {
            "mpn": "DUALGATE",
            "symbol": {
                "pins": [
                    {"number": 1, "side": "left", "unit": 1},
                    {"number": 2, "side": "right", "unit": 1},
                    {"number": 3, "side": "left", "unit": 2},
                    {"number": 4, "side": "right", "unit": 2},
                ]
            },
            "footprint": {
                "name": "FP4",
                "package": {
                    "family": "custom",
                    "body": {"x": 2, "y": 2},
                    "pads": [
                        {"number": str(n), "at": [n, 0], "size": [0.5, 0.5]}
                        for n in range(1, 5)
                    ],
                },
            },
        }
    )
    node = symbol_node(part)
    sub = sexpr.find_all(node, "symbol")
    assert [s[1].strip('"') for s in sub] == ["DUALGATE_1_1", "DUALGATE_2_1"]
    for unit in sub:
        assert sexpr.find(unit, "rectangle") is not None
        assert len(sexpr.find_all(unit, "pin")) == 2


def test_pin_names_and_numbers_carry_font_effects(eeprom: Part) -> None:
    node = symbol_node(eeprom)
    for pin in _find_all_deep(node, "pin"):
        for field in ("name", "number"):
            block = sexpr.find(pin, field)
            assert block is not None and sexpr.find(block, "effects") is not None


def test_all_pins_survive_into_the_output(mcu: Part) -> None:
    node = symbol_node(mcu)
    numbers = {
        sexpr.find(p, "number")[1].strip('"') for p in _find_all_deep(node, "pin")
    }
    assert numbers == {str(n) for n in range(1, 49)}


def test_output_is_a_pure_function_of_the_ir(eeprom: Part) -> None:
    assert render_library([eeprom]) == render_library([eeprom])


def test_library_order_does_not_depend_on_input_order(eeprom: Part, mcu: Part) -> None:
    assert render_library([eeprom, mcu]) == render_library([mcu, eeprom])


def test_emitted_library_reparses(eeprom: Part, mcu: Part) -> None:
    text = render_library([eeprom, mcu])
    tree = sexpr.parse(text)
    assert sexpr.dumps(tree) == text, "emit -> parse -> emit must be stable"
