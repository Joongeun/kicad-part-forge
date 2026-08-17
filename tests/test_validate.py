"""Every validator, proved by a part that deliberately violates it.

The rule this file follows: **a check that has never caught anything is not
known to work.** So each test constructs a part or a file that breaks exactly
one rule, asserts that rule fires at the stated severity, and — where the
distinction matters — asserts that a correct part does *not* fire it.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from kifab.ir import Part
from kifab.validate import (
    Conformance,
    Report,
    Severity,
    check_footprint,
    check_part,
    check_path,
    check_symbol,
    geometry,
    klc,
    schema,
)
from kifab.validate.parse import ParsedFootprint, ParsedSymbolLib, ParseError

PARTS_DIR = Path(__file__).resolve().parent.parent / "parts"


# --------------------------------------------------------------------------
# Builders: the smallest thing that is a real KiCad file
# --------------------------------------------------------------------------


def base_part(**overrides) -> dict:
    """A clean two-terminal part that fires no check at all."""
    data = {
        "mpn": "TEST1",
        "reference": "U",
        "datasheet": "https://example.invalid/ds.pdf",
        "description": "test device",
        "symbol": {
            "keywords": "test",
            "pins": [
                {"number": 1, "name": "A", "side": "left"},
                {"number": 2, "name": "B", "side": "right"},
            ],
        },
        "footprint": {
            "name": "TEST_FP",
            "description": "test footprint",
            "package": {
                "family": "custom",
                "body": {"x": 2.0, "y": 1.0},
                "pads": [
                    {"number": 1, "at": [-1.2, 0], "size": [0.8, 0.8]},
                    {"number": 2, "at": [1.2, 0], "size": [0.8, 0.8]},
                ],
            },
        },
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            merged = copy.deepcopy(data[key])
            merged.update(value)
            data[key] = merged
        else:
            data[key] = value
    return data


def soic_part(**overrides) -> dict:
    """A computed dual-gullwing part — the case the dimension rules apply to."""
    data = base_part(**overrides)
    data.setdefault("mpn", "TEST8")
    data["symbol"] = {
        "keywords": "test",
        "pins": [
            {"number": n, "name": f"P{n}", "side": "left" if n <= 4 else "right"}
            for n in range(1, 9)
        ],
    }
    data["footprint"] = {
        "name": "SOIC-8_3.9x4.9mm_P1.27mm",
        "description": "test",
        "package": {
            "family": "dual_gullwing",
            "pin_count": 8,
            "pitch": 1.27,
            "body": {"x": 3.9, "y": 4.9},
            "lead_span": {"nominal": 6.0, "tolerance": 0.2},
            "lead_width": "0.31 .. 0.51",
            "lead_length": "0.40 .. 1.27",
        },
    }
    for key, value in overrides.items():
        if key in ("symbol", "footprint"):
            data[key].update(value)
    return data


def footprint_text(
    *,
    name: str = "TEST_FP",
    pads: str = "",
    graphics: str = "",
    attr: str = "(attr smd)",
    properties: bool = True,
    model: str = "",
) -> str:
    props = ""
    if properties:
        props = "\n".join(
            f'\t(property "{key}" "{value}"\n'
            f"\t\t(at 0 0 0)\n"
            f'\t\t(layer "{layer}")\n'
            "\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1 1)\n"
            "\t\t\t\t(thickness 0.15)\n\t\t\t)\n\t\t)\n"
            "\t)"
            for key, value, layer in (
                ("Reference", "REF**", "F.SilkS"),
                ("Value", name, "F.Fab"),
                ("Datasheet", "", "F.Fab"),
                ("Description", "", "F.Fab"),
            )
        )
    return "\n".join(
        [
            f'(footprint "{name}"',
            "\t(version 20241229)",
            '\t(generator "kifab-test")',
            '\t(layer "F.Cu")',
            props,
            attr,
            graphics,
            pads,
            f'\t(model "{model}")' if model else "",
            ")",
        ]
    )


def pad(
    number: str,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    kind: str = "smd",
    shape: str = "rect",
    layers: str = '"F.Cu" "F.Mask" "F.Paste"',
    drill: float | None = None,
) -> str:
    drill_line = f"\t\t(drill {drill})\n" if drill is not None else ""
    return (
        f'\t(pad "{number}" {kind} {shape}\n'
        f"\t\t(at {x} {y})\n"
        f"\t\t(size {w} {h})\n"
        f"{drill_line}"
        f"\t\t(layers {layers})\n"
        "\t)"
    )


def line(x0: float, y0: float, x1: float, y1: float, layer: str, width: float) -> str:
    return (
        "\t(fp_line\n"
        f"\t\t(start {x0} {y0})\n"
        f"\t\t(end {x1} {y1})\n"
        f"\t\t(stroke\n\t\t\t(width {width})\n\t\t\t(type solid)\n\t\t)\n"
        f'\t\t(layer "{layer}")\n'
        "\t)"
    )


def rect_outline(half_x: float, half_y: float, layer: str, width: float) -> str:
    return "\n".join(
        [
            line(-half_x, -half_y, half_x, -half_y, layer, width),
            line(half_x, -half_y, half_x, half_y, layer, width),
            line(half_x, half_y, -half_x, half_y, layer, width),
            line(-half_x, half_y, -half_x, -half_y, layer, width),
        ]
    )


def fab_body(half_x: float, half_y: float, chamfer: float = 0.3) -> str:
    """A chamfered F.Fab outline — the pin-1 indicator KLC F6.2 looks for."""
    points = [
        (-half_x, -half_y + chamfer),
        (-half_x, half_y),
        (half_x, half_y),
        (half_x, -half_y),
        (-half_x + chamfer, -half_y),
    ]
    xy = "\n".join(f"\t\t\t(xy {x} {y})" for x, y in points)
    return (
        "\t(fp_poly\n\t\t(pts\n"
        + xy
        + "\n\t\t)\n\t\t(stroke\n\t\t\t(width 0.1)\n\t\t\t(type solid)\n\t\t)\n"
        '\t\t(fill no)\n\t\t(layer "F.Fab")\n\t)\n'
        '\t(fp_text user "${REFERENCE}"\n\t\t(at 0 0 0)\n\t\t(layer "F.Fab")\n'
        "\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1 1)\n"
        "\t\t\t\t(thickness 0.15)\n\t\t\t)\n\t\t)\n\t)"
    )


def clean_footprint(**overrides) -> ParsedFootprint:
    """Two 0.8 mm pads 2.4 mm apart, correctly drawn. Fires nothing."""
    defaults = dict(
        pads="\n".join([pad("1", -1.2, 0, 0.8, 0.8), pad("2", 1.2, 0, 0.8, 0.8)]),
        graphics="\n".join(
            [
                rect_outline(1.85, 0.75, "F.CrtYd", 0.05),
                fab_body(1.0, 0.5),
            ]
        ),
    )
    defaults.update(overrides)
    return ParsedFootprint.from_text(footprint_text(**defaults))


def checks_of(report: Report, severity: Severity | None = None) -> set[str]:
    return {
        f.check
        for f in report
        if severity is None or f.severity is severity
    }


# --------------------------------------------------------------------------
# The report model — severity has to mean something
# --------------------------------------------------------------------------


def test_an_error_blocks_and_a_warning_does_not() -> None:
    report = Report()
    report.add("X", Severity.WARNING, "questionable")
    report.add("Y", Severity.INFO, "just so you know")
    assert report.ok() is True
    assert report.ok(strict=True) is False, "--strict must promote warnings"

    report.add("Z", Severity.ERROR, "wrong")
    assert report.ok() is False
    assert report.ok(strict=True) is False


def test_info_never_blocks_even_under_strict() -> None:
    report = Report()
    report.add("X", Severity.INFO, "kicad-cli missing")
    assert report.ok(strict=True) is True


def test_findings_are_machine_readable() -> None:
    report = Report()
    report.add(
        "GEO001",
        Severity.ERROR,
        "shorted",
        subject="part.yaml",
        where='pad "1"',
        layer="footprint",
        at=(1.0, -2.0),
    )
    payload = report.to_dict()
    assert payload["ok"] is False
    assert payload["counts"] == {"error": 1, "warning": 0, "info": 0}
    finding = payload["findings"][0]
    assert finding["check"] == "GEO001"
    assert finding["at"] == [1.0, -2.0]
    assert finding["where"] == 'pad "1"'
    assert finding["layer"] == "footprint"


# --------------------------------------------------------------------------
# Schema — the IR layer
# --------------------------------------------------------------------------


def test_a_clean_part_fires_no_schema_check() -> None:
    assert list(schema.check_schema(Part.model_validate(base_part()))) == []


def test_reference_with_a_question_mark_is_an_error() -> None:
    part = Part.model_validate(base_part(reference="U?"))
    report = schema.check_reference(part)
    assert checks_of(report, Severity.ERROR) == {"SCH001"}


def test_supply_pin_typed_as_a_signal_warns() -> None:
    data = base_part()
    data["symbol"]["pins"][0].update({"name": "VCC", "type": "output"})
    report = schema.check_power_pins(Part.model_validate(data))
    assert checks_of(report, Severity.WARNING) == {"SCH002"}
    assert 'pin "1"' in report.findings[0].where


def test_supply_pin_typed_power_in_is_silent() -> None:
    data = base_part()
    data["symbol"]["pins"][0].update({"name": "GND_2", "type": "power_in"})
    assert list(schema.check_power_pins(Part.model_validate(data))) == []


def test_nc_pin_typed_as_an_input_warns() -> None:
    data = base_part()
    data["symbol"]["pins"][0].update({"name": "NC", "type": "input"})
    assert checks_of(schema.check_no_connect_pins(Part.model_validate(data))) == {
        "SCH003"
    }


def test_hidden_power_pin_warns() -> None:
    data = base_part()
    data["symbol"]["pins"][0].update({"name": "VDD", "type": "power_in", "hidden": True})
    report = schema.check_hidden_pins(Part.model_validate(data))
    assert checks_of(report, Severity.WARNING) == {"SCH004"}
    assert "hidden power pin" in report.findings[0].message


def test_missing_datasheet_warns() -> None:
    part = Part.model_validate(base_part(datasheet=""))
    report = schema.check_metadata(part)
    assert checks_of(report, Severity.WARNING) == {"SCH005"}


def test_the_two_halves_must_agree_about_the_bom() -> None:
    data = base_part()
    data["footprint"]["exclude_from_bom"] = True
    report = schema.check_bom_agreement(Part.model_validate(data))
    assert checks_of(report, Severity.WARNING) == {"SCH006"}


def test_a_lead_span_narrower_than_the_body_is_an_error() -> None:
    """The transposed-dimension typo: it puts every land under the plastic."""
    data = soic_part()
    data["footprint"]["package"]["lead_span"] = {"nominal": 3.0, "tolerance": 0.1}
    report = schema.check_package_dimensions(Part.model_validate(data))
    assert checks_of(report, Severity.ERROR) == {"SCH007"}


def test_a_lead_row_longer_than_the_body_warns() -> None:
    data = soic_part()
    data["footprint"]["package"]["pitch"] = 2.54
    data["footprint"]["name"] = "SOIC-8_3.9x4.9mm"
    report = schema.check_package_dimensions(Part.model_validate(data))
    assert checks_of(report, Severity.WARNING) == {"SCH008"}


def test_a_footprint_name_that_lies_about_its_pitch_is_an_error() -> None:
    data = soic_part()
    data["footprint"]["name"] = "SOIC-8_3.9x4.9mm_P0.65mm"
    report = schema.check_footprint_name(Part.model_validate(data))
    assert checks_of(report, Severity.ERROR) == {"SCH009"}
    assert "0.65" in report.findings[0].message


def test_a_footprint_name_that_lies_about_its_pin_count_is_an_error() -> None:
    data = soic_part()
    data["footprint"]["name"] = "SOIC-16_3.9x4.9mm_P1.27mm"
    report = schema.check_footprint_name(Part.model_validate(data))
    assert checks_of(report, Severity.ERROR) == {"SCH009"}


def test_the_shipped_corpus_passes_every_schema_check() -> None:
    for path in sorted(PARTS_DIR.glob("*.yaml")):
        from kifab.ir import load_part

        report = schema.check_schema(load_part(path))
        assert report.ok(strict=True), f"{path.name}: {report.format()}"


# --------------------------------------------------------------------------
# Geometry — the file layer
# --------------------------------------------------------------------------


def test_a_clean_footprint_fires_no_geometry_check() -> None:
    report = geometry.check_footprint_geometry(clean_footprint())
    assert list(report) == [], report.format()


def test_overlapping_pads_of_different_numbers_are_an_error() -> None:
    fp = clean_footprint(
        pads="\n".join([pad("1", -0.2, 0, 0.8, 0.8), pad("2", 0.2, 0, 0.8, 0.8)])
    )
    report = geometry.check_pad_clearance(fp)
    assert checks_of(report, Severity.ERROR) == {"GEO001"}
    assert "short" in report.findings[0].message


def test_pads_closer_than_the_minimum_gap_warn_without_touching() -> None:
    # Edges at -0.025 and +0.025: a 0.05 mm gap, half the stated minimum.
    fp = clean_footprint(
        pads="\n".join([pad("1", -0.425, 0, 0.8, 0.8), pad("2", 0.425, 0, 0.8, 0.8)])
    )
    report = geometry.check_pad_clearance(fp)
    assert checks_of(report, Severity.WARNING) == {"GEO001"}
    assert report.errors == 0


def test_an_exposed_pad_touching_a_land_is_reported_as_such() -> None:
    pads = [pad(str(n), -2.0, -1.5 + n * 0.5, 1.0, 0.3) for n in range(1, 5)]
    pads.append(pad("5", 0.0, 0.0, 3.0, 2.6))  # exposed pad, reaching pad 1..4
    fp = clean_footprint(pads="\n".join(pads))
    report = geometry.check_pad_clearance(fp)
    assert "GEO002" in checks_of(report, Severity.ERROR)


def test_a_pad_outside_the_courtyard_is_an_error() -> None:
    fp = clean_footprint(
        graphics="\n".join([rect_outline(0.5, 0.5, "F.CrtYd", 0.05), fab_body(1.0, 0.5)])
    )
    report = geometry.check_courtyard(fp)
    assert checks_of(report, Severity.ERROR) == {"GEO003"}
    assert "outside the courtyard" in report.findings[0].message


def test_a_missing_courtyard_is_an_error() -> None:
    fp = clean_footprint(graphics=fab_body(1.0, 0.5))
    report = geometry.check_courtyard(fp)
    assert checks_of(report, Severity.ERROR) == {"GEO003"}
    assert "no courtyard" in report.findings[0].message


def test_an_off_grid_courtyard_warns_once() -> None:
    fp = clean_footprint(
        graphics="\n".join(
            [rect_outline(1.8512, 0.7512, "F.CrtYd", 0.05), fab_body(1.0, 0.5)]
        )
    )
    report = geometry.check_courtyard(fp)
    warnings = [f for f in report if f.severity is Severity.WARNING]
    assert [f.check for f in warnings] == ["GEO003"], "one finding, not one per corner"
    assert "0.01 mm" in warnings[0].message


def test_silk_printed_over_a_pad_is_an_error() -> None:
    fp = clean_footprint(
        graphics="\n".join(
            [
                rect_outline(1.85, 0.75, "F.CrtYd", 0.05),
                fab_body(1.0, 0.5),
                line(-1.2, -0.5, -1.2, 0.5, "F.SilkS", 0.12),
            ]
        )
    )
    report = geometry.check_silk_clearance(fp)
    assert checks_of(report, Severity.ERROR) == {"GEO004"}


def test_silk_too_close_to_a_pad_warns() -> None:
    # Pad 1 spans x -1.6..-0.8; a line at x = -0.7 leaves 0.1 - 0.06 = 0.04 mm.
    fp = clean_footprint(
        graphics="\n".join(
            [
                rect_outline(1.85, 0.75, "F.CrtYd", 0.05),
                fab_body(1.0, 0.5),
                line(-0.7, -0.5, -0.7, 0.5, "F.SilkS", 0.12),
            ]
        )
    )
    report = geometry.check_silk_clearance(fp)
    assert checks_of(report, Severity.WARNING) == {"GEO004"}
    assert report.errors == 0


def test_silk_at_exactly_the_clearance_limit_is_silent() -> None:
    """The emitter places silk exactly on the boundary; float noise must not
    turn that into a finding."""
    fp = clean_footprint(
        graphics="\n".join(
            [
                rect_outline(1.85, 0.75, "F.CrtYd", 0.05),
                fab_body(1.0, 0.5),
                line(-0.54, -0.5, -0.54, 0.5, "F.SilkS", 0.12),
            ]
        )
    )
    assert list(geometry.check_silk_clearance(fp)) == []


def test_a_duplicated_pad_is_an_error() -> None:
    fp = clean_footprint(
        pads="\n".join(
            [
                pad("1", -1.2, 0, 0.8, 0.8),
                pad("1", -1.2, 0, 0.8, 0.8),
                pad("2", 1.2, 0, 0.8, 0.8),
            ]
        )
    )
    assert checks_of(geometry.check_pad_numbering(fp), Severity.ERROR) == {"GEO005"}


def test_a_zero_area_pad_is_an_error() -> None:
    fp = clean_footprint(
        pads="\n".join([pad("1", -1.2, 0, 0.8, 0), pad("2", 1.2, 0, 0.8, 0.8)])
    )
    assert checks_of(geometry.check_pad_geometry(fp), Severity.ERROR) == {"GEO006"}


def test_a_drill_as_wide_as_its_pad_is_an_error() -> None:
    fp = clean_footprint(
        pads="\n".join(
            [
                pad(
                    "1",
                    -1.2,
                    0,
                    0.8,
                    0.8,
                    kind="thru_hole",
                    shape="circle",
                    layers='"*.Cu" "*.Mask"',
                    drill=0.9,
                ),
                pad("2", 1.2, 0, 0.8, 0.8),
            ]
        )
    )
    report = geometry.check_pad_geometry(fp)
    assert checks_of(report, Severity.ERROR) == {"GEO006"}
    assert "annular ring" in report.findings[0].message


def test_a_plain_mechanical_hole_is_not_a_missing_annulus() -> None:
    """`np_thru_hole` with pad size == drill is how KiCad spells a plain hole."""
    fp = clean_footprint(
        pads="\n".join(
            [
                pad("1", -1.2, 0, 0.8, 0.8),
                pad("2", 1.2, 0, 0.8, 0.8),
                pad(
                    "",
                    0,
                    0,
                    3.2,
                    3.2,
                    kind="np_thru_hole",
                    shape="circle",
                    layers='"F&B.Cu" "*.Mask"',
                    drill=3.2,
                ),
            ]
        )
    )
    assert list(geometry.check_pad_geometry(fp)) == []


def test_a_pin_off_the_schematic_grid_is_an_error() -> None:
    lib = ParsedSymbolLib.from_text(symbol_text(pin_x=-7.0))
    report = geometry.check_pin_grid(lib.symbols[0])
    assert checks_of(report, Severity.ERROR) == {"GEO007"}


def test_a_pin_on_the_grid_is_silent() -> None:
    lib = ParsedSymbolLib.from_text(symbol_text())
    assert list(geometry.check_pin_grid(lib.symbols[0])) == []


def test_a_duplicated_pin_number_is_an_error() -> None:
    lib = ParsedSymbolLib.from_text(symbol_text(second_number="1"))
    assert checks_of(geometry.check_duplicate_pins(lib.symbols[0])) == {"GEO008"}


def test_symbol_and_footprint_must_agree_about_pin_numbers() -> None:
    lib = ParsedSymbolLib.from_text(symbol_text(second_number="3"))
    report = geometry.check_pin_sets(lib.symbols[0], clean_footprint())
    assert checks_of(report, Severity.ERROR) == {"GEO009"}
    messages = " ".join(f.message for f in report)
    assert "['3']" in messages and "['2']" in messages


def test_matching_pin_sets_are_silent() -> None:
    lib = ParsedSymbolLib.from_text(symbol_text())
    assert list(geometry.check_pin_sets(lib.symbols[0], clean_footprint())) == []


def symbol_text(
    *,
    name: str = "TEST1",
    pin_x: float = -7.62,
    second_number: str = "2",
    pin_length: float = 2.54,
    text_size: float = 1.27,
    properties: bool = True,
) -> str:
    props = ""
    if properties:
        props = "\n".join(
            f'\t\t(property "{key}" "{value}"\n\t\t\t(at 0 0 0)\n\t\t)'
            for key, value in (
                ("Reference", "U"),
                ("Value", name),
                ("Footprint", "kifab:TEST_FP"),
                ("Datasheet", ""),
                ("Description", ""),
            )
        )
    pins = "\n".join(
        f"\t\t\t(pin passive line\n"
        f"\t\t\t\t(at {x} {y} {angle})\n"
        f"\t\t\t\t(length {pin_length})\n"
        f'\t\t\t\t(name "{pname}"\n\t\t\t\t\t(effects\n\t\t\t\t\t\t(font\n'
        f"\t\t\t\t\t\t\t(size {text_size} {text_size})\n\t\t\t\t\t\t)\n\t\t\t\t\t)\n\t\t\t\t)\n"
        f'\t\t\t\t(number "{number}"\n\t\t\t\t\t(effects\n\t\t\t\t\t\t(font\n'
        f"\t\t\t\t\t\t\t(size {text_size} {text_size})\n\t\t\t\t\t\t)\n\t\t\t\t\t)\n\t\t\t\t)\n"
        f"\t\t\t)"
        for number, pname, x, y, angle in (
            ("1", "A", pin_x, 0, 0),
            (second_number, "B", -pin_x, 0, 180),
        )
    )
    return "\n".join(
        [
            "(kicad_symbol_lib",
            "\t(version 20241209)",
            '\t(generator "kifab-test")',
            f'\t(symbol "{name}"',
            props,
            f'\t\t(symbol "{name}_1_1"',
            pins,
            "\t\t)",
            "\t)",
            ")",
        ]
    )


# --------------------------------------------------------------------------
# The two Phase 2 traps must not read as defects
# --------------------------------------------------------------------------


def thermal_vias_footprint() -> ParsedFootprint:
    """An SMD footprint with an exposed pad stitched by through-hole vias.

    Modelled on the shipped `*_ThermalVias` variants: vias numbered as the
    exposed pad, an unnumbered paste aperture over it, and a matching land on
    B.Cu that overlaps every perimeter pad in plan view.
    """
    pads = [pad(str(n), -2.0, -1.5 + n * 0.5, 1.0, 0.3) for n in range(1, 5)]
    pads.append(pad("5", 0.0, 0.0, 2.0, 2.0))
    pads.append(pad("5", 0.0, 0.0, 5.0, 3.0, layers='"B.Cu"'))
    pads += [
        pad(
            "5",
            x,
            y,
            0.6,
            0.6,
            kind="thru_hole",
            shape="circle",
            layers='"*.Cu"',
            drill=0.3,
        )
        for x, y in ((-0.5, -0.5), (0.5, 0.5))
    ]
    pads.append(pad("", 0.0, 0.0, 0.9, 0.9))  # unnumbered paste aperture
    return clean_footprint(
        pads="\n".join(pads),
        graphics="\n".join(
            [rect_outline(2.9, 1.9, "F.CrtYd", 0.05), fab_body(2.5, 1.5)]
        ),
    )


def test_thermal_vias_are_not_read_as_duplicates_shorts_or_through_hole_leads() -> None:
    fp = thermal_vias_footprint()
    assert fp.pad_numbers == {"1", "2", "3", "4", "5"}
    assert len(fp.aperture_pads) == 1, "the unnumbered aperture is not a land"
    assert len(fp.via_pads()) == 2

    report = check_footprint(fp)
    assert report.errors == 0, report.format()


def test_a_back_side_land_cannot_short_a_front_side_pad() -> None:
    fp = ParsedFootprint.from_text(
        footprint_text(
            pads="\n".join(
                [
                    pad("1", -1.2, 0, 0.8, 0.8),
                    pad("2", 0.0, 0.0, 5.0, 5.0, layers='"B.Cu"'),
                ]
            )
        )
    )
    assert list(geometry.check_pad_clearance(fp)) == []


def test_a_custom_shaped_pad_is_reported_as_unmeasured_not_as_a_short() -> None:
    fp = ParsedFootprint.from_text(
        footprint_text(
            pads="\n".join(
                [
                    pad("1", -1.2, 0, 0.8, 0.8),
                    pad("2", 0.0, 0.0, 5.0, 5.0, shape="custom"),
                ]
            )
        )
    )
    report = geometry.check_pad_clearance(fp)
    assert report.errors == 0
    assert checks_of(report, Severity.INFO) == {"GEO001"}


# --------------------------------------------------------------------------
# KLC
# --------------------------------------------------------------------------


def test_a_clean_footprint_fires_no_klc_check() -> None:
    report = klc.check_footprint_klc(clean_footprint())
    assert list(report) == [], report.format()


def test_a_footprint_name_with_a_space_is_an_error() -> None:
    fp = clean_footprint(name="TEST FP")
    assert checks_of(klc.check_footprint_name(fp), Severity.ERROR) == {"KLC-F3.1"}


def test_wrong_silk_width_warns() -> None:
    fp = clean_footprint(
        graphics="\n".join(
            [
                rect_outline(1.85, 0.75, "F.CrtYd", 0.05),
                fab_body(1.0, 0.5),
                line(-1.85, -0.75, 1.85, -0.75, "F.SilkS", 0.2),
            ]
        )
    )
    assert checks_of(klc.check_layer_widths(fp), Severity.WARNING) == {"KLC-F5.1"}


def test_a_missing_fab_reference_warns() -> None:
    fp = clean_footprint(graphics=rect_outline(1.85, 0.75, "F.CrtYd", 0.05))
    assert checks_of(klc.check_fab_layer(fp), Severity.WARNING) == {"KLC-F5.2"}


def test_missing_footprint_properties_warn() -> None:
    fp = clean_footprint(properties=False)
    report = klc.check_required_properties(fp)
    assert checks_of(report, Severity.WARNING) == {"KLC-F5.2"}


def test_a_footprint_with_no_pin_one_indicator_warns() -> None:
    pads = "\n".join(pad(str(n), -1.5 + n * 0.7, 0, 0.5, 0.5) for n in range(1, 5))
    fp = clean_footprint(
        pads=pads,
        graphics="\n".join(
            [
                rect_outline(2.0, 1.0, "F.CrtYd", 0.05),
                rect_outline(1.5, 0.5, "F.Fab", 0.1),
                '\t(fp_text user "${REFERENCE}"\n\t\t(at 0 0 0)\n\t\t(layer "F.Fab")\n\t)',
            ]
        ),
    )
    assert checks_of(klc.check_pin1_marker(fp), Severity.WARNING) == {"KLC-F6.2"}


def test_smd_pads_without_the_smd_attribute_is_an_error() -> None:
    fp = clean_footprint(attr="")
    report = klc.check_attributes(fp)
    assert checks_of(report, Severity.ERROR) == {"KLC-F7.1"}
    assert "position file" in report.findings[0].message


def test_through_hole_pads_declared_smd_is_an_error() -> None:
    fp = clean_footprint(
        pads="\n".join(
            [
                pad(
                    "1",
                    -1.2,
                    0,
                    1.6,
                    1.6,
                    kind="thru_hole",
                    shape="circle",
                    layers='"*.Cu" "*.Mask"',
                    drill=0.8,
                ),
                pad("2", 1.2, 0, 0.8, 0.8),
            ]
        )
    )
    assert checks_of(klc.check_attributes(fp), Severity.ERROR) == {"KLC-F7.1"}


def test_an_absolute_3d_model_path_warns() -> None:
    fp = clean_footprint(model="/Users/someone/models/TEST.step")
    assert checks_of(klc.check_model_path(fp), Severity.WARNING) == {"KLC-F9.1"}


def test_a_model_path_using_a_kicad_variable_is_silent() -> None:
    fp = clean_footprint(model="${KICAD9_3DMODEL_DIR}/Package_SO.3dshapes/TEST.step")
    assert list(klc.check_model_path(fp)) == []


def test_a_symbol_name_with_a_space_is_an_error() -> None:
    lib = ParsedSymbolLib.from_text(symbol_text(name="TEST 1"))
    assert checks_of(klc.check_symbol_name(lib.symbols[0]), Severity.ERROR) == {
        "KLC-S3.1"
    }


def test_missing_symbol_properties_warn() -> None:
    lib = ParsedSymbolLib.from_text(symbol_text(properties=False))
    assert checks_of(klc.check_symbol_properties(lib.symbols[0])) == {"KLC-S6.3"}


def test_a_short_pin_warns() -> None:
    lib = ParsedSymbolLib.from_text(symbol_text(pin_length=1.0))
    assert checks_of(klc.check_pin_style(lib.symbols[0])) == {"KLC-S4.2"}


def test_oversized_pin_text_warns() -> None:
    lib = ParsedSymbolLib.from_text(symbol_text(text_size=2.0))
    assert checks_of(klc.check_pin_style(lib.symbols[0])) == {"KLC-S4.3"}


def test_a_clean_symbol_fires_no_check() -> None:
    lib = ParsedSymbolLib.from_text(symbol_text())
    report = check_symbol(lib.symbols[0])
    assert list(report) == [], report.format()


def test_the_skipped_klc_rules_are_recorded_with_a_reason() -> None:
    assert klc.SKIPPED, "the omissions must be documented, not implicit"
    assert all(len(reason) > 30 for reason in klc.SKIPPED.values())


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_a_non_footprint_file_is_rejected_clearly() -> None:
    with pytest.raises(ParseError):
        ParsedFootprint.from_text("(kicad_symbol_lib)", "x")


def test_malformed_source_is_rejected_clearly() -> None:
    with pytest.raises(ParseError):
        ParsedFootprint.from_text("(footprint", "x")


def test_de_morgan_bodies_do_not_double_the_pin_count() -> None:
    text = symbol_text().replace('(symbol "TEST1_1_1"', '(symbol "TEST1_1_2"', 1)
    lib = ParsedSymbolLib.from_text(text)
    assert lib.symbols[0].pins == []


# --------------------------------------------------------------------------
# End to end: whole parts, and the corpus
# --------------------------------------------------------------------------


def test_the_shipped_corpus_passes_every_check_including_strict() -> None:
    report = check_path(PARTS_DIR)
    assert report.ok(strict=True), report.format(verbose=True)


def test_a_part_is_checked_at_both_layers() -> None:
    """A defect that only exists after rendering must still be caught."""
    data = base_part(reference="U?")
    data["footprint"]["package"]["pads"] = [
        {"number": 1, "at": [-0.2, 0], "size": [0.8, 0.8]},
        {"number": 2, "at": [0.2, 0], "size": [0.8, 0.8]},
    ]
    report = check_part(Part.model_validate(data))
    fired = checks_of(report, Severity.ERROR)
    assert "SCH001" in fired, "the IR-layer defect"
    assert "GEO001" in fired, "the defect that only exists in the emitted file"


def test_check_path_reports_a_part_that_will_not_load() -> None:
    report = check_path(PARTS_DIR / "does-not-exist.yaml")
    assert report.errors == 1
    assert report.findings[0].check == "PATH"


def test_check_path_reads_an_emitted_footprint(tmp_path: Path) -> None:
    path = tmp_path / "TEST_FP.kicad_mod"
    path.write_text(
        footprint_text(
            pads="\n".join([pad("1", -0.2, 0, 0.8, 0.8), pad("2", 0.2, 0, 0.8, 0.8)]),
            graphics=rect_outline(1.85, 0.75, "F.CrtYd", 0.05),
        ),
        encoding="utf-8",
    )
    report = check_path(path)
    assert "GEO001" in checks_of(report, Severity.ERROR)


# --------------------------------------------------------------------------
# The kicad-cli conformance gate
# --------------------------------------------------------------------------


def test_the_conformance_gate_reports_a_skip_rather_than_a_pass() -> None:
    """An unrunnable check must never look like a passing one."""
    absent = Conformance(cli=None)
    assert absent.available is False
    report = absent.check_footprint_text(footprint_text(), "TEST_FP")
    assert [f.severity for f in report] == [Severity.INFO]
    assert report.ok(strict=True) is True
    assert "did not run" in report.findings[0].message


kicad = Conformance.discover()
requires_kicad_cli = pytest.mark.skipif(
    not kicad.available, reason="kicad-cli not found on this machine"
)


@requires_kicad_cli
def test_kicad_cli_accepts_and_canonicalises_the_corpus() -> None:
    from kifab.ir import load_part

    parts = [(str(p), load_part(p)) for p in sorted(PARTS_DIR.glob("*.yaml"))]
    from kifab.validate import check_parts

    report = check_parts(parts, conformance=kicad)
    assert report.ok(strict=True), report.format(verbose=True)


@requires_kicad_cli
def test_a_file_kicad_rejects_is_an_error() -> None:
    broken = footprint_text(pads='\t(pad "1" smd rect (at 0 0) (size nonsense 1))')
    report = kicad.check_footprint_text(broken, "BROKEN")
    assert checks_of(report, Severity.ERROR) == {"CLI001"}


@requires_kicad_cli
def test_a_non_canonical_file_is_a_warning_not_an_error() -> None:
    """Merely-acceptable form is usable; it just is not what we emit."""
    sloppy = footprint_text(
        pads=pad("1", -1.2, 0, 0.8, 0.8) + "\n" + pad("2", 1.2, 0, 0.8, 0.8)
    )
    report = kicad.check_footprint_text(sloppy, "TEST_FP")
    assert report.errors == 0
    assert checks_of(report, Severity.WARNING) <= {"CLI002"}
