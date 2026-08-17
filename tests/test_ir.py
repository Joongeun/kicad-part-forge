"""The IR is the contract, so its guarantees get tested first.

Every check here exists because breaking it produces a *plausible-looking* part
that is wrong on the board — the failure mode the whole project exists to
prevent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from kifab.ipc.toleranced import Tol
from kifab.ir import (
    CustomPackage,
    DualGullwing,
    ElectricalType,
    MountType,
    Pad,
    PadType,
    Part,
    Pin,
    QuadGullwing,
    Side,
    SymbolSpec,
    load_part,
)
from kifab.ir.package import _to_tol

PARTS_DIR = Path(__file__).resolve().parent.parent / "parts"


def _minimal_part(**overrides) -> dict:
    data = {
        "mpn": "TEST1",
        "symbol": {
            "pins": [
                {"number": 1, "name": "A", "side": "left"},
                {"number": 2, "name": "B", "side": "right"},
            ]
        },
        "footprint": {
            "name": "TEST_FP",
            "package": {
                "family": "custom",
                "body": {"x": 2.0, "y": 1.0},
                "pads": [
                    {"number": "1", "at": [-1, 0], "size": [0.8, 0.8]},
                    {"number": "2", "at": [1, 0], "size": [0.8, 0.8]},
                ],
            },
        },
    }
    data.update(overrides)
    return data


# --------------------------------------------------------------------------
# The shipped corpus must always validate.
# --------------------------------------------------------------------------


def test_every_shipped_part_validates() -> None:
    files = sorted(PARTS_DIR.glob("*.yaml"))
    assert files, "parts/ is empty — the corpus is the point of the IR"
    for path in files:
        load_part(path)  # raises on any problem


def test_shipped_parts_cover_both_kinds_of_package_family() -> None:
    """A corpus of only computed or only custom packages tests half the IR."""
    families = {
        load_part(p).footprint.package.family for p in PARTS_DIR.glob("*.yaml")
    }
    assert "custom" in families
    assert families & {"dual_gullwing", "quad_gullwing"}


# --------------------------------------------------------------------------
# Cross-checks between the two halves of a part.
# --------------------------------------------------------------------------


def test_pin_without_a_pad_is_rejected() -> None:
    data = _minimal_part()
    data["symbol"]["pins"].append({"number": 3, "name": "C", "side": "left"})
    with pytest.raises(ValidationError, match="have no matching pad"):
        Part.model_validate(data)


def test_pad_without_a_pin_is_rejected() -> None:
    data = _minimal_part()
    data["footprint"]["package"]["pads"].append(
        {"number": "3", "at": [0, 0], "size": [0.8, 0.8]}
    )
    with pytest.raises(ValidationError, match="have no matching symbol pin"):
        Part.model_validate(data)


def test_pin_numbers_must_be_unique() -> None:
    data = _minimal_part()
    data["symbol"]["pins"][1]["number"] = 1
    with pytest.raises(ValidationError, match="duplicate pin numbers"):
        Part.model_validate(data)


@pytest.mark.parametrize(
    "field,value",
    [("mpn", "PART/A"), ("library", "my lib"), ("footprint_name", "SOIC:8")],
)
def test_names_kicad_forbids_are_rejected(field: str, value: str) -> None:
    data = _minimal_part()
    if field == "footprint_name":
        data["footprint"]["name"] = value
    else:
        data[field] = value
    with pytest.raises(ValidationError, match="forbids"):
        Part.model_validate(data)


def test_slot_collision_on_the_same_side_is_rejected() -> None:
    with pytest.raises(ValidationError, match="claim slot"):
        SymbolSpec.model_validate(
            {
                "pins": [
                    {"number": 1, "side": "left", "slot": 0},
                    {"number": 2, "side": "left", "slot": 0},
                ]
            }
        )


def test_the_same_slot_on_different_sides_is_fine() -> None:
    spec = SymbolSpec.model_validate(
        {
            "pins": [
                {"number": 1, "side": "left", "slot": 0},
                {"number": 2, "side": "right", "slot": 0},
            ]
        }
    )
    assert len(spec.pins) == 2


def test_unit_numbers_must_be_contiguous() -> None:
    with pytest.raises(ValidationError, match="units must run"):
        SymbolSpec.model_validate(
            {
                "pins": [
                    {"number": 1, "side": "left", "unit": 1},
                    {"number": 2, "side": "left", "unit": 3},
                ]
            }
        )


def test_unknown_field_is_an_error_not_a_shrug() -> None:
    """A typo'd key must fail loudly; silently ignoring it ships a wrong part."""
    data = _minimal_part()
    data["symbol"]["pins"][0]["typ"] = "input"
    with pytest.raises(ValidationError):
        Part.model_validate(data)


# --------------------------------------------------------------------------
# Field-level semantics.
# --------------------------------------------------------------------------


def test_pin_number_accepts_yaml_integers_and_keeps_them_strings() -> None:
    assert Pin(number=7).number == "7"
    assert Pin(number="EP").number == "EP"


def test_pin_defaults_are_the_conservative_ones() -> None:
    pin = Pin(number="1")
    assert pin.type is ElectricalType.PASSIVE
    assert pin.side is Side.LEFT
    assert pin.name == "~"
    assert pin.slot is None


def test_value_falls_back_to_mpn() -> None:
    part = Part.model_validate(_minimal_part())
    assert part.display_value == "TEST1"
    part = Part.model_validate(_minimal_part(value="10k"))
    assert part.display_value == "10k"


def test_footprint_id_is_the_lib_id_kicad_stores() -> None:
    part = Part.model_validate(_minimal_part(library="house"))
    assert part.footprint_id == "house:TEST_FP"


@pytest.mark.parametrize(
    "written,expected",
    [
        (3.9, Tol.exact(3.9)),
        ("3.9", Tol.exact(3.9)),
        ("3.8 .. 4.0", Tol.span(3.8, 4.0)),
        ("3.8 .. 3.9 .. 4.0", Tol.span(3.8, 4.0)),
        ({"min": 3.8, "max": 4.0}, Tol.span(3.8, 4.0)),
        ({"nominal": 3.9, "tolerance": 0.1}, Tol.span(3.8, 4.0)),
    ],
)
def test_dimension_accepts_every_form_a_datasheet_uses(written, expected) -> None:
    assert _to_tol(written) == expected


def test_unreadable_dimension_says_what_to_write_instead() -> None:
    with pytest.raises(ValueError, match="write it as"):
        _to_tol(["3.8", "4.0"])


def test_through_hole_pad_must_have_a_drill() -> None:
    with pytest.raises(ValidationError, match="has no drill"):
        Pad(number="1", at=(0, 0), size=(1.6, 1.6), type=PadType.THRU_HOLE)


def test_smd_pad_must_not_have_a_drill() -> None:
    with pytest.raises(ValidationError, match="but has a drill"):
        Pad(number="1", at=(0, 0), size=(1.6, 1.6), type=PadType.SMD, drill=0.8)


def test_custom_package_can_declare_through_hole_mounting() -> None:
    package = CustomPackage.model_validate(
        {
            "body": {"x": 2.0, "y": 2.0},
            "mount_type": "through_hole",
            "pads": [
                {
                    "number": "1",
                    "at": [0, 0],
                    "size": [1.6, 1.6],
                    "type": "thru_hole",
                    "drill": 0.8,
                }
            ],
        }
    )
    assert package.mount() is MountType.THROUGH_HOLE


# --------------------------------------------------------------------------
# Package families produce the numbering convention KiCad expects.
# --------------------------------------------------------------------------


def test_dual_gullwing_numbers_counter_clockwise_from_top_left() -> None:
    package = DualGullwing.model_validate(
        {
            "body": {"x": 3.9, "y": 4.9},
            "pin_count": 8,
            "pitch": 1.27,
            "lead_span": {"nominal": 6.0, "tolerance": 0.2},
            "lead_width": "0.31 .. 0.51",
            "lead_length": "0.40 .. 1.27",
        }
    )
    pads = {p.number: p.at for p in package.resolve_pads()}
    assert pads["1"][0] < 0 and pads["1"][1] < 0, "pin 1 is top-left"
    assert pads["4"][0] < 0 and pads["4"][1] > 0, "pin 4 is bottom-left"
    assert pads["5"][0] > 0 and pads["5"][1] > 0, "pin 5 is bottom-right"
    assert pads["8"][0] > 0 and pads["8"][1] < 0, "pin 8 is top-right"


def test_quad_gullwing_walks_left_bottom_right_top() -> None:
    package = QuadGullwing.model_validate(
        {
            "body": {"x": 7.0, "y": 7.0},
            "pin_count": 48,
            "pitch": 0.5,
            "lead_span": {"x": 9.0, "y": 9.0},
            "lead_width": "0.17 .. 0.27",
            "lead_length": "0.45 .. 0.75",
        }
    )
    pads = {p.number: p for p in package.resolve_pads()}
    assert len(pads) == 48
    assert pads["1"].at[0] < 0 and pads["1"].at[1] < 0
    assert pads["13"].at[1] > 0 and pads["13"].at[0] < 0
    assert pads["25"].at[0] > 0 and pads["25"].at[1] > 0
    assert pads["37"].at[1] < 0 and pads["37"].at[0] > 0
    # Side pads are long in x, top/bottom pads long in y.
    assert pads["1"].size[0] > pads["1"].size[1]
    assert pads["13"].size[1] > pads["13"].size[0]


@pytest.mark.parametrize("count", [0, 3, 7])
def test_dual_gullwing_rejects_odd_pin_counts(count: int) -> None:
    """Rejected at validation, not at emission — the IR is where this belongs."""
    with pytest.raises(ValidationError, match="even pin count|greater than 0"):
        DualGullwing.model_validate(
            {
                "body": {"x": 1, "y": 1},
                "pin_count": count,
                "pitch": 1.27,
                "lead_span": 6.0,
                "lead_width": 0.4,
                "lead_length": 0.8,
            }
        )


def test_quad_gullwing_rejects_counts_not_divisible_by_four() -> None:
    with pytest.raises(ValidationError, match="divisible by 4"):
        QuadGullwing.model_validate(
            {
                "body": {"x": 7, "y": 7},
                "pin_count": 46,
                "pitch": 0.5,
                "lead_span": {"x": 9.0, "y": 9.0},
                "lead_width": 0.2,
                "lead_length": 0.6,
            }
        )
