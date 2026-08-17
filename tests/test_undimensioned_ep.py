"""`exposed_pad: {undimensioned: true}` — the recorded gap.

Why this exists: the LTC5552 blind holdout produced a footprint whose every
stated dimension matched ADI DWG 05-08-1985, and which correctly declined to
dimension the exposed pad because the drawing does not attach a dimension to it
unambiguously. The IR had no way to say that, so the most honest answer the
pipeline could produce was rejected outright and nothing reached disk.

The flag makes that answer expressible. What it must never become is a way to
ship a part without a thermal pad, so every test here is really about the
blocking half.
"""

from __future__ import annotations

import pytest

from kifab.ir import Part
from kifab.validate import Severity, schema

# The real holdout shape: 12-lead QFN, 3x2 mm, 0.5 mm pitch, 3 lands a side,
# thermal pin 13 in the symbol. Dimensions from the drawing; EP deliberately
# not stated, which is the whole point.
LTC5552 = {
    "mpn": "LTC5552",
    "reference": "U",
    "description": "microwave mixer",
    "datasheet": "https://example.invalid/5552f.pdf",
    "symbol": {
        "keywords": "mixer",
        "pins": [
            {"number": 1, "name": "GND", "type": "power_in", "side": "right"},
            {"number": 2, "name": "IF+", "type": "passive", "side": "right"},
            {"number": 3, "name": "IF-", "type": "passive", "side": "right"},
            {"number": 4, "name": "GND", "type": "power_in", "side": "right"},
            {"number": 5, "name": "RF", "type": "passive", "side": "left"},
            {"number": 6, "name": "GND", "type": "power_in", "side": "right"},
            {"number": 7, "name": "EN", "type": "input", "side": "left"},
            {"number": 8, "name": "GND", "type": "power_in", "side": "right"},
            {"number": 9, "name": "VCC", "type": "power_in", "side": "left"},
            {"number": 10, "name": "GND", "type": "power_in", "side": "right"},
            {"number": 11, "name": "LO", "type": "passive", "side": "left"},
            {"number": 12, "name": "GND", "type": "power_in", "side": "right"},
            {"number": 13, "name": "EP", "type": "passive", "side": "right"},
        ],
    },
    "footprint": {
        "name": "QFN-12-1EP_3x2mm_P0.5mm",
        "description": "12-lead QFN 3x2mm, DWG 05-08-1985",
        "package": {
            "family": "quad_no_lead",
            "body": {"x": 3.0, "y": 2.0},
            "body_tolerance": 0.10,
            "pins_x": 3,
            "pins_y": 3,
            "pitch": 0.5,
            "lead_width": "0.20 .. 0.30",
            "lead_length": "0.30 .. 0.50",
            "exposed_pad": {"undimensioned": True},
        },
    },
}


def _part(**package_overrides) -> Part:
    data = {**LTC5552}
    package = {**LTC5552["footprint"]["package"], **package_overrides}
    data["footprint"] = {**LTC5552["footprint"], "package": package}
    return Part.model_validate(data)


# -- the gap is expressible -------------------------------------------------


def test_the_honest_answer_now_parses() -> None:
    """Before this flag existed, this document was simply unrepresentable."""
    part = _part()
    assert part.footprint.package.exposed_pad.undimensioned


def test_no_thermal_copper_is_emitted() -> None:
    """An undimensioned pad is exactly as much copper as no pad at all."""
    numbers = {pad.number for pad in _part().footprint.package.resolve_pads()}
    assert numbers == {str(n) for n in range(1, 13)}
    assert "13" not in numbers


def test_the_symbol_keeps_its_thermal_pin() -> None:
    """The netlist still has to be able to reach ground through pin 13."""
    assert "13" in {pin.number for pin in _part().symbol.pins}


# -- and it blocks ----------------------------------------------------------


def test_it_is_an_error_not_a_warning() -> None:
    """A footprint with no thermal pad will not solder down. It blocks."""
    report = schema.check_undimensioned_exposed_pad(_part())
    findings = [f for f in report.findings if f.check == "SCH010"]
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR


def test_strict_is_not_what_stops_it() -> None:
    """It has to fail a default `kifab check`, not only `--strict`."""
    report = schema.check_schema(_part())
    assert not report.ok()


def test_a_dimensioned_pad_fires_nothing() -> None:
    part = _part(exposed_pad={"size_x": 0.77, "size_y": 0.25})
    assert schema.check_undimensioned_exposed_pad(part).findings == []
    assert "13" in {p.number for p in part.footprint.package.resolve_pads()}


# -- the flag cannot be abused ---------------------------------------------


def test_cannot_claim_the_gap_and_state_a_size() -> None:
    with pytest.raises(ValueError, match="also states"):
        _part(exposed_pad={"undimensioned": True, "size_x": 3.0, "size_y": 1.0})


def test_an_exposed_pad_with_neither_size_nor_flag_is_refused() -> None:
    with pytest.raises(ValueError, match="undimensioned"):
        _part(exposed_pad={"number": "13"})


def test_the_exemption_covers_only_the_exposed_pad_pin() -> None:
    """A second unbonded pin is still the hard error it always was."""
    data = {**LTC5552}
    data["symbol"] = {
        **LTC5552["symbol"],
        "pins": [
            *LTC5552["symbol"]["pins"],
            {"number": 14, "name": "MECH", "type": "passive", "side": "right"},
        ],
    }
    with pytest.raises(ValueError, match="no matching pad"):
        Part.model_validate(data)


def test_a_dimensioned_part_still_cross_checks_pins_against_pads() -> None:
    """The exemption must not leak into the normal case."""
    data = {**LTC5552}
    package = {
        **LTC5552["footprint"]["package"],
        "exposed_pad": {"size_x": 0.77, "size_y": 0.25},
    }
    data["footprint"] = {**LTC5552["footprint"], "package": package}
    data["symbol"] = {
        **LTC5552["symbol"],
        "pins": [
            *LTC5552["symbol"]["pins"],
            {"number": 14, "name": "MECH", "type": "passive", "side": "right"},
        ],
    }
    with pytest.raises(ValueError, match="no matching pad"):
        Part.model_validate(data)
