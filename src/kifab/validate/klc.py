"""KiCad Library Convention checks — the subset that catches real defects.

**Layer: the file.** KLC is about what is on which layer, at what width, with
what text — none of which exists until the emitter has drawn it.

This is deliberately **not** all of KLC. The rules implemented are the ones
where a violation is a defect rather than a matter of taste, and where the
check is decidable from the file alone. What was skipped, and why, is listed in
`SKIPPED` at the bottom of this module so the omission is a recorded decision
rather than an oversight.

Rule identifiers follow KLC's own numbering (`KLC-F5.1`) so a finding can be
looked up in the published convention.
"""

from __future__ import annotations

import re

from .parse import ParsedFootprint, ParsedSymbol
from .report import LAYER_FOOTPRINT, LAYER_SYMBOL, Report, Severity

TOL = 1e-6

# --- KLC's stated dimensions ---------------------------------------------
SILK_WIDTH = 0.12  # KLC F5.1
FAB_WIDTH = 0.1  # KLC F5.2
COURTYARD_WIDTH = 0.05  # KLC F5.3
REF_TEXT_SIZE = 1.0  # KLC F5.2 — silkscreen reference
REF_TEXT_THICKNESS = 0.15
SYMBOL_TEXT_SIZE = 1.27  # KLC S4.3 — pin name and number
MIN_PIN_LENGTH = 2.54  # KLC S4.2
PIN_LENGTH_STEP = 1.27

#: A pin-1 indicator is required on F.Fab and expected on F.SilkS (KLC F6.2).
#: Only meaningful once a package has more than this many pads — a two-terminal
#: chip has no pin 1 to indicate.
PIN1_MIN_PADS = 2

_ILLEGAL_IN_NAME = re.compile(r"[:/\\\s]")
_MODEL_VARIABLE = re.compile(r"^\$\{[A-Za-z0-9_]+\}")
_MODEL_SUFFIXES = (".step", ".stp", ".wrl", ".STEP", ".WRL")

_REQUIRED_FOOTPRINT_PROPERTIES = ("Reference", "Value", "Datasheet", "Description")
_REQUIRED_SYMBOL_PROPERTIES = (
    "Reference",
    "Value",
    "Footprint",
    "Datasheet",
    "Description",
)


def _width_rule(
    report: Report,
    rule: str,
    layer_name: str,
    expected: float,
    fp: ParsedFootprint,
) -> None:
    for graphic in fp.on_layer(layer_name):
        if graphic.width and abs(graphic.width - expected) > TOL:
            report.add(
                rule,
                Severity.WARNING,
                f"line width {graphic.width:g} mm on {layer_name}; KLC "
                f"specifies {expected} mm",
                where=f"{graphic.kind} on {layer_name}",
                layer=LAYER_FOOTPRINT,
                at=(graphic.points[0] if graphic.points else graphic.centre),
            )
            return  # one finding per layer; a width error is uniform in practice


def check_footprint_name(fp: ParsedFootprint) -> Report:
    """KLC-F3.1 — the name is a library item name, not a sentence."""
    report = Report()
    if not fp.name.strip():
        report.add(
            "KLC-F3.1",
            Severity.ERROR,
            "the footprint has no name",
            where="footprint",
            layer=LAYER_FOOTPRINT,
        )
    elif _ILLEGAL_IN_NAME.search(fp.name):
        report.add(
            "KLC-F3.1",
            Severity.ERROR,
            f"name {fp.name!r} contains whitespace or one of ':', '/', '\\', "
            "which KiCad forbids in a library item name",
            where="footprint",
            layer=LAYER_FOOTPRINT,
        )
    return report


def check_layer_widths(fp: ParsedFootprint) -> Report:
    """KLC-F5.1 / F5.2 / F5.3 — the stated line widths per layer."""
    report = Report()
    _width_rule(report, "KLC-F5.1", "F.SilkS", SILK_WIDTH, fp)
    _width_rule(report, "KLC-F5.2", "F.Fab", FAB_WIDTH, fp)
    _width_rule(report, "KLC-F5.3", "F.CrtYd", COURTYARD_WIDTH, fp)
    return report


def check_fab_layer(fp: ParsedFootprint) -> Report:
    """KLC-F5.2 — F.Fab carries a body outline and a `${REFERENCE}` text."""
    report = Report()
    if not fp.on_layer("F.Fab") and not fp.on_layer("B.Fab"):
        report.add(
            "KLC-F5.2",
            Severity.WARNING,
            "nothing on F.Fab: assembly drawings have no body outline to place "
            "against",
            where="F.Fab",
            layer=LAYER_FOOTPRINT,
        )
    has_ref_text = any(
        "${REFERENCE}" in text.text and text.layer.endswith("Fab") for text in fp.texts
    )
    if not has_ref_text:
        report.add(
            "KLC-F5.2",
            Severity.WARNING,
            "no ${REFERENCE} text on F.Fab, so the assembly drawing cannot "
            "label this part",
            where="F.Fab",
            layer=LAYER_FOOTPRINT,
        )
    return report


def check_reference_text(fp: ParsedFootprint) -> Report:
    """KLC-F5.2 — the silkscreen reference is 1.0 mm at 0.15 mm thickness."""
    report = Report()
    reference = fp.properties.get("Reference")
    if reference is None:
        return report
    if not reference.layer.endswith("SilkS"):
        report.add(
            "KLC-F5.2",
            Severity.WARNING,
            f"the Reference property is on {reference.layer or '(no layer)'}; "
            "KLC puts it on F.SilkS",
            where="Reference",
            layer=LAYER_FOOTPRINT,
        )
    if reference.size and abs(reference.size - REF_TEXT_SIZE) > TOL:
        report.add(
            "KLC-F5.2",
            Severity.WARNING,
            f"Reference text is {reference.size:g} mm; KLC specifies "
            f"{REF_TEXT_SIZE} mm",
            where="Reference",
            layer=LAYER_FOOTPRINT,
        )
    if reference.thickness and abs(reference.thickness - REF_TEXT_THICKNESS) > TOL:
        report.add(
            "KLC-F5.2",
            Severity.WARNING,
            f"Reference text thickness is {reference.thickness:g} mm; KLC "
            f"specifies {REF_TEXT_THICKNESS} mm",
            where="Reference",
            layer=LAYER_FOOTPRINT,
        )
    return report


def check_required_properties(fp: ParsedFootprint) -> Report:
    """KLC-F5.2 — KiCad 9 canonical form carries all four properties."""
    report = Report()
    missing = [p for p in _REQUIRED_FOOTPRINT_PROPERTIES if p not in fp.properties]
    if missing:
        report.add(
            "KLC-F5.2",
            Severity.WARNING,
            f"missing footprint properties {missing}; KiCad writes all four "
            "back the first time this file is opened, so the file will drift",
            where="properties",
            layer=LAYER_FOOTPRINT,
        )
    return report


def check_pin1_marker(fp: ParsedFootprint) -> Report:
    """KLC-F6.2 — pin 1 must be identifiable from the drawing.

    Detection rule, stated so it can be argued with: an indicator counts as
    present if F.Fab carries a closed outline with more than four vertices (the
    chamfered-corner convention), or if F.SilkS carries any polygon or circle
    (the triangle or dot convention). A footprint that indicates pin 1 some
    other way will warn — which is why this is a warning.
    """
    report = Report()
    if len(fp.pads) <= PIN1_MIN_PADS:
        return report
    chamfered = any(
        g.closed and len(g.points) > 4 for g in fp.on_layer("F.Fab")
    )
    silk_marker = any(
        g.kind in ("fp_poly", "fp_circle") for g in fp.on_layer("F.SilkS")
    )
    if not chamfered and not silk_marker:
        report.add(
            "KLC-F6.2",
            Severity.WARNING,
            "no pin-1 indicator found (expected a chamfered F.Fab outline or a "
            "silkscreen triangle/dot); a part fitted 180 degrees out is the "
            "most expensive assembly error there is",
            where="F.Fab / F.SilkS",
            layer=LAYER_FOOTPRINT,
        )
    return report


def check_attributes(fp: ParsedFootprint) -> Report:
    """KLC-F7 — `(attr ...)` must match the pads that are actually there.

    Not cosmetic: the attribute is what decides whether the part appears in the
    position file and whether KiCad's DRC applies through-hole rules to it.
    """
    report = Report()
    # Thermal vias are drilled but are not component leads, so they must not
    # make an SMD part read as through-hole (the Phase 2 `*_ThermalVias` trap).
    pads = fp.leads()
    if not pads:
        return report
    types = {p.type for p in pads}
    has_tht = "thru_hole" in types
    all_smd = types <= {"smd"}
    if has_tht and "through_hole" not in fp.attrs:
        report.add(
            "KLC-F7.1",
            Severity.ERROR,
            f"has plated through-hole pads but `(attr {' '.join(fp.attrs) or ''})` "
            "does not say through_hole",
            where="attr",
            layer=LAYER_FOOTPRINT,
        )
    elif all_smd and "smd" not in fp.attrs:
        report.add(
            "KLC-F7.1",
            Severity.ERROR,
            "every pad is SMD but `(attr smd)` is missing, so the part will be "
            "left out of the pick-and-place position file",
            where="attr",
            layer=LAYER_FOOTPRINT,
        )
    if "smd" in fp.attrs and has_tht:
        report.add(
            "KLC-F7.1",
            Severity.ERROR,
            "declared `smd` but contains through-hole pads",
            where="attr",
            layer=LAYER_FOOTPRINT,
        )
    return report


def check_model_path(fp: ParsedFootprint) -> Report:
    """KLC-F9.1 — a 3D model reference must survive leaving this machine."""
    report = Report()
    if not fp.model:
        return report
    if not _MODEL_VARIABLE.match(fp.model):
        report.add(
            "KLC-F9.1",
            Severity.WARNING,
            f"3D model path {fp.model!r} is not relative to a KiCad path "
            "variable (${KICAD9_3DMODEL_DIR}/...), so it will not resolve on "
            "another machine",
            where="model",
            layer=LAYER_FOOTPRINT,
        )
    if not fp.model.endswith(_MODEL_SUFFIXES):
        report.add(
            "KLC-F9.1",
            Severity.WARNING,
            f"3D model path {fp.model!r} does not end in .step or .wrl",
            where="model",
            layer=LAYER_FOOTPRINT,
        )
    return report


FOOTPRINT_CHECKS = (
    check_footprint_name,
    check_layer_widths,
    check_fab_layer,
    check_reference_text,
    check_required_properties,
    check_pin1_marker,
    check_attributes,
    check_model_path,
)


def check_footprint_klc(fp: ParsedFootprint) -> Report:
    report = Report()
    for check in FOOTPRINT_CHECKS:
        report.extend(check(fp))
    return report


# --- symbols --------------------------------------------------------------


def check_symbol_name(symbol: ParsedSymbol) -> Report:
    """KLC-S3.1 — symbol names are library item names."""
    report = Report()
    if _ILLEGAL_IN_NAME.search(symbol.name):
        report.add(
            "KLC-S3.1",
            Severity.ERROR,
            f"name {symbol.name!r} contains whitespace or one of ':', '/', "
            "'\\', which KiCad forbids in a library item name",
            where="symbol",
            layer=LAYER_SYMBOL,
        )
    return report


def check_symbol_properties(symbol: ParsedSymbol) -> Report:
    """KLC-S6.3 — the five properties KiCad 9 canonical form always carries."""
    report = Report()
    if symbol.extends:
        return report  # a derived symbol inherits what it does not override
    missing = [p for p in _REQUIRED_SYMBOL_PROPERTIES if p not in symbol.properties]
    if missing:
        report.add(
            "KLC-S6.3",
            Severity.WARNING,
            f"missing properties {missing}; KiCad writes them back on save, so "
            "the file will drift from what we generated",
            where="properties",
            layer=LAYER_SYMBOL,
        )
    return report


def check_pin_style(symbol: ParsedSymbol) -> Report:
    """KLC-S4.2 / S4.3 — pin length and text size."""
    report = Report()
    reported_length = False
    reported_text = False
    for pin in symbol.pins:
        where = f'pin "{pin.number}" ({pin.name})'
        if not reported_length and pin.length:
            too_short = pin.length < MIN_PIN_LENGTH - TOL
            off_step = (
                abs(pin.length / PIN_LENGTH_STEP - round(pin.length / PIN_LENGTH_STEP))
                > 1e-6
            )
            if too_short or off_step:
                report.add(
                    "KLC-S4.2",
                    Severity.WARNING,
                    f"pin length {pin.length:g} mm; KLC wants at least "
                    f"{MIN_PIN_LENGTH} mm in steps of {PIN_LENGTH_STEP} mm",
                    where=where,
                    layer=LAYER_SYMBOL,
                    at=pin.at,
                )
                reported_length = True
        if not reported_text:
            sizes = [s for s in (pin.name_size, pin.number_size) if s]
            if any(abs(s - SYMBOL_TEXT_SIZE) > TOL for s in sizes):
                report.add(
                    "KLC-S4.3",
                    Severity.WARNING,
                    f"pin name/number text is {sizes[0]:g} mm; KLC specifies "
                    f"{SYMBOL_TEXT_SIZE} mm",
                    where=where,
                    layer=LAYER_SYMBOL,
                    at=pin.at,
                )
                reported_text = True
    return report


SYMBOL_CHECKS = (check_symbol_name, check_symbol_properties, check_pin_style)


def check_symbol_klc(symbol: ParsedSymbol) -> Report:
    report = Report()
    for check in SYMBOL_CHECKS:
        report.extend(check(symbol))
    return report


#: KLC rules deliberately **not** implemented, and the reason. Recorded here so
#: the gap is a decision that can be revisited rather than an unknown.
SKIPPED: dict[str, str] = {
    "F2.x (3D model correctness)": "requires opening the STEP file and judging "
    "its alignment and scale against the footprint — no oracle available "
    "without a mesh library.",
    "F3.x (family naming conventions)": "the full naming grammar per package "
    "family is a taste rule with hundreds of special cases; the part of it "
    "that catches defects — a name whose stated pitch or pin count contradicts "
    "the geometry — is implemented as SCH009 instead.",
    "F4.x (per-family land geometry)": "this is what the IPC-7351B module "
    "computes and what tests/test_ipc.py diffs against the official corpus; "
    "restating it as a lint rule would test the same arithmetic twice.",
    "F7.2 (through-hole pad shape: pad 1 rectangular)": "kifab does not "
    "generate through-hole packages yet (Phase 5); the rule would only ever "
    "fire on adopted third-party parts.",
    "F8.x (paste / thermal relief on exposed pads)": "exposed-pad packages are "
    "deferred with the QFN/DFN family; there is nothing to check until they "
    "are generatable.",
    "S2.x / S6.x (symbol body proportions and graphic style)": "aesthetic. The "
    "emitter owns the house style and tests/test_emit_symbol.py holds it; a "
    "lint rule would only restate the emitter's own constants.",
    "S5.x (Value property must equal the symbol name)": "false on generic "
    "parts by design — parts/BLM31PG601SN1L.yaml sets a value of "
    "'600R@100MHz', which is correct. A rule that fires on correct parts "
    "trains people to ignore the linter.",
    "Library organisation rules": "which library a part belongs in is a "
    "project decision, not a property of the file.",
}
