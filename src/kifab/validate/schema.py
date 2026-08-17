"""Schema lint — semantic contradictions the type system cannot see.

**Layer: the IR.** Everything here reads a `Part` and nothing else. Pydantic
already guarantees shapes, ranges and the pin/pad cross-check; this module
catches statements that are individually well-typed and jointly wrong — a lead
span narrower than the body it wraps, a pin called `GND` typed as an output, a
footprint whose *name* claims a pitch its geometry does not have.

The rules that need a threshold say so, name the constant, and are warnings.
The rules that are pass/fail on a stated dimension are errors.
"""

from __future__ import annotations

import re

from ..ir import DualGullwing, ElectricalType, Part, QuadGullwing
from .report import LAYER_IR, Report, Severity

#: Pin-name prefixes that mean "this pin is a supply rail". Matched after
#: stripping KiCad's overbar markers and any trailing index (`VDD_2`, `GND3`).
#: A heuristic by construction — hence a warning, never an error — but the list
#: is closed and explicit rather than a fuzzy score.
POWER_PIN_PREFIXES = (
    "VCC",
    "VDD",
    "VSS",
    "VEE",
    "VBAT",
    "VBUS",
    "VIN",
    "VOUT",
    "VREF",
    "VPP",
    "AVDD",
    "AVSS",
    "AVCC",
    "DVDD",
    "DVSS",
    "GND",
    "AGND",
    "DGND",
    "PGND",
    "VDDA",
    "VSSA",
)

#: Electrical types that are legitimate for a supply pin. `power_out` covers a
#: regulator output; `passive` covers a pin the designer deliberately wants ERC
#: to ignore, which is common enough that flagging it would be noise.
POWER_PIN_TYPES = (
    ElectricalType.POWER_IN,
    ElectricalType.POWER_OUT,
    ElectricalType.PASSIVE,
)

_INDEX_SUFFIX = re.compile(r"[_\-]?\d+$")
_OVERBAR = re.compile(r"[~{}]")

#: `..._P0.5mm...` in a footprint name states the pitch in mm.
_NAME_PITCH = re.compile(r"_P(\d+(?:\.\d+)?)mm")
#: `LQFP-48_...`, `SOIC-8_...` — the pin count stated in the name.
_NAME_PIN_COUNT = re.compile(r"^[A-Za-z]+-(\d+)(?:[-_]|$)")

#: Reference designators are a letter prefix only: KiCad appends the number.
_REFERENCE = re.compile(r"^[A-Za-z]+$")


def _clean_pin_name(name: str) -> str:
    return _INDEX_SUFFIX.sub("", _OVERBAR.sub("", name).strip()).upper()


def check_reference(part: Part) -> Report:
    """SCH001 — the reference designator is a prefix, not an instance."""
    report = Report()
    if not _REFERENCE.match(part.reference):
        report.add(
            "SCH001",
            Severity.ERROR,
            f"reference {part.reference!r} is not a bare letter prefix; KiCad "
            f"appends the instance number, so this annotates as "
            f"{part.reference!r}1",
            where="reference",
            layer=LAYER_IR,
        )
    return report


def check_power_pins(part: Part) -> Report:
    """SCH002 — a supply pin must be typed as one, or ERC is meaningless."""
    report = Report()
    for pin in part.symbol.pins:
        cleaned = _clean_pin_name(pin.name)
        if not cleaned.startswith(POWER_PIN_PREFIXES):
            continue
        if pin.type in POWER_PIN_TYPES:
            continue
        report.add(
            "SCH002",
            Severity.WARNING,
            f"looks like a supply pin but is typed {pin.type.value!r}; ERC will "
            "not flag an unpowered rail unless it is power_in / power_out",
            where=f'pin "{pin.number}" ({pin.name})',
            layer=LAYER_IR,
        )
    return report


def check_no_connect_pins(part: Part) -> Report:
    """SCH003 — a pin named NC must be typed no_connect."""
    report = Report()
    for pin in part.symbol.pins:
        named_nc = _clean_pin_name(pin.name) in ("NC", "DNC", "NOCONNECT")
        typed_nc = pin.type is ElectricalType.NO_CONNECT
        if named_nc and not typed_nc:
            report.add(
                "SCH003",
                Severity.WARNING,
                f"is named NC but typed {pin.type.value!r}; type it no_connect "
                "so ERC objects when something is wired to it",
                where=f'pin "{pin.number}" ({pin.name})',
                layer=LAYER_IR,
            )
        elif typed_nc and not named_nc and pin.name not in ("", "~"):
            report.add(
                "SCH003",
                Severity.WARNING,
                "is typed no_connect but carries a functional name; one of the "
                "two is wrong",
                where=f'pin "{pin.number}" ({pin.name})',
                layer=LAYER_IR,
            )
    return report


def check_hidden_pins(part: Part) -> Report:
    """SCH004 — hidden pins, and especially hidden power pins."""
    report = Report()
    for pin in part.symbol.pins:
        if not pin.hidden:
            continue
        if pin.type in (ElectricalType.POWER_IN, ElectricalType.POWER_OUT):
            report.add(
                "SCH004",
                Severity.WARNING,
                "is a hidden power pin — a legacy practice KLC discourages, "
                "because the rail it connects to is invisible in the schematic",
                where=f'pin "{pin.number}" ({pin.name})',
                layer=LAYER_IR,
            )
        else:
            report.add(
                "SCH004",
                Severity.WARNING,
                "is hidden; a pin nobody can see is a pin nobody wires",
                where=f'pin "{pin.number}" ({pin.name})',
                layer=LAYER_IR,
            )
    return report


def check_metadata(part: Part) -> Report:
    """SCH005 — the fields a human needs when they meet this part later."""
    report = Report()
    if not part.datasheet.strip():
        report.add(
            "SCH005",
            Severity.WARNING,
            "no datasheet URL; the part cannot be re-verified against its "
            "source later",
            where="datasheet",
            layer=LAYER_IR,
        )
    if not part.description.strip() and not part.footprint.description.strip():
        report.add(
            "SCH005",
            Severity.WARNING,
            "no description on either half; the symbol chooser will show a "
            "blank line",
            where="description",
            layer=LAYER_IR,
        )
    if not part.symbol.keywords.strip():
        report.add(
            "SCH005",
            Severity.INFO,
            "no keywords, so the part is findable only by its exact name",
            where="symbol.keywords",
            layer=LAYER_IR,
        )
    return report


def check_bom_agreement(part: Part) -> Report:
    """SCH006 — the two halves must agree about whether this is a BOM line."""
    report = Report()
    if part.symbol.in_bom and part.footprint.exclude_from_bom:
        report.add(
            "SCH006",
            Severity.WARNING,
            "the symbol is in the BOM but the footprint is excluded from it; "
            "the schematic and the board will disagree about this part",
            where="in_bom / exclude_from_bom",
            layer=LAYER_IR,
        )
    return report


def check_package_dimensions(part: Part) -> Report:
    """SCH007/SCH008 — dimensions that contradict each other.

    The failure this catches is a transposed pair in the YAML: writing the body
    width where the lead span belongs puts every land *under* the plastic.
    """
    report = Report()
    package = part.footprint.package
    body = package.body

    def _span(label: str, span_min: float, body_dim: float, axis: str) -> None:
        if span_min < body_dim - 1e-9:
            report.add(
                "SCH007",
                Severity.ERROR,
                f"{label} minimum {span_min:g} mm is smaller than the body "
                f"{axis} of {body_dim:g} mm — the leads cannot be inside the "
                "plastic; the two dimensions are probably transposed",
                where=f"package.{label}",
                layer=LAYER_IR,
            )

    def _row(pins_per_side: int, pitch: float, body_dim: float, axis: str) -> None:
        length = (pins_per_side - 1) * pitch
        if length > body_dim + 1e-9:
            report.add(
                "SCH008",
                Severity.WARNING,
                f"{pins_per_side} leads at {pitch:g} mm pitch span {length:g} mm, "
                f"which is longer than the body {axis} of {body_dim:g} mm",
                where="package.pitch",
                layer=LAYER_IR,
            )

    if isinstance(package, DualGullwing):
        _span("lead_span", package.lead_span.minimum, body.x, "x")
        _row(package.pin_count // 2, package.pitch, body.y, "y")
    elif isinstance(package, QuadGullwing):
        _span("lead_span.x", package.lead_span.x.minimum, body.x, "x")
        _span("lead_span.y", package.lead_span.y.minimum, body.y, "y")
        _row(package.pin_count // 4, package.pitch, body.y, "y")
        _row(package.pin_count // 4, package.pitch, body.x, "x")
    return report


def check_footprint_name(part: Part) -> Report:
    """SCH009 — a footprint name that states a dimension must state it truly.

    A name is what a human and the T0 resolver both match on. A name that has
    drifted from its geometry is how the wrong land pattern gets chosen while
    looking right in every list.
    """
    report = Report()
    package = part.footprint.package
    name = part.footprint.name
    pitch = getattr(package, "pitch", None)
    pin_count = getattr(package, "pin_count", None)

    stated_pitch = _NAME_PITCH.search(name)
    if stated_pitch and pitch is not None:
        value = float(stated_pitch.group(1))
        if abs(value - pitch) > 1e-6:
            report.add(
                "SCH009",
                Severity.ERROR,
                f"name states a {value:g} mm pitch but the package is "
                f"{pitch:g} mm",
                where=f"footprint.name {name!r}",
                layer=LAYER_IR,
            )

    stated_count = _NAME_PIN_COUNT.match(name)
    if stated_count and pin_count is not None:
        value = int(stated_count.group(1))
        if value != pin_count:
            report.add(
                "SCH009",
                Severity.ERROR,
                f"name states {value} pins but the package has {pin_count}",
                where=f"footprint.name {name!r}",
                layer=LAYER_IR,
            )
    return report


IR_CHECKS = (
    check_reference,
    check_power_pins,
    check_no_connect_pins,
    check_hidden_pins,
    check_metadata,
    check_bom_agreement,
    check_package_dimensions,
    check_footprint_name,
)


def check_schema(part: Part) -> Report:
    """Every IR-layer rule, in one report."""
    report = Report()
    for check in IR_CHECKS:
        report.extend(check(part))
    return report
