"""Emit `.kicad_mod` (KiCad 9, format version 20241229).

The emitter is deliberately package-agnostic: it asks the package family for a
list of lands and then draws silk, fab and courtyard around whatever it gets.
Adding a package family never touches this file.

Canonical form was established in Phase 0 by round-tripping through
`kicad-cli fp upgrade --force` and diffing (see DECISIONS.md). What it requires:

* `(generator_version "9.0")` alongside `(generator ...)`;
* all four of Reference / Value / Datasheet / Description as properties, even
  when empty, with Datasheet and Description hidden on F.Fab;
* a `(uuid ...)` on every property, `fp_line`, `fp_poly`, `fp_text` and `pad` —
  derived, never random (see `kifab.uuids`);
* pad layers in the order `"F.Cu" "F.Mask" "F.Paste"`;
* `(embedded_fonts no)` immediately before the 3D model.

Silkscreen policy: draw the body outline, then **trim** away every stretch that
would come within `silk_pad_clearance` of a pad, rather than nudging the line.
Trimming is what the official generator does and it degrades gracefully — a
package whose pads swallow an entire edge simply loses that edge. Verified
against `LQFP-48_7x7mm_P0.5mm`, whose corner ticks this reproduces exactly.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from ..ir import MountType, Pad, PadType, Part
from ..uuids import UuidSource
from . import sexpr
from .sexpr import Node, fmt_num, quote

FOOTPRINT_VERSION = "20241229"
GENERATOR = "kifab"
GENERATOR_VERSION = "9.0"

# House rules, measured off the official libraries (see module docstring).
ROUNDRECT_RATIO = 0.25
MAX_CORNER_RADIUS = 0.25  # mm; caps the ratio on large pads
FAB_CHAMFER_MAX = 1.0  # mm; pin-1 chamfer on the F.Fab body outline
FAB_CHAMFER_FRACTION = 4.0  # chamfer = min(FAB_CHAMFER_MAX, body_min / this)
TEXT_THICKNESS_RATIO = 0.15
MIN_SILK_SEGMENT = 0.2  # mm; shorter fragments are dropped, not drawn
_EPS = 1e-6


def _round_half_up(value: float, places: int) -> float:
    """Round half away from zero.

    Python's built-in `round` rounds half to *even*, so `round(0.975, 2)` is
    0.97 — one micron under what the official generator writes for the same
    body. Text sizes are cosmetic, but a silent one-off from the reference
    output is exactly the kind of drift that makes a golden file untrustworthy.
    """
    scale = 10**places
    return math.floor(abs(value) * scale + 0.5) / scale * (1 if value >= 0 else -1)

_DEFAULT_LAYERS = {
    PadType.SMD: ["F.Cu", "F.Mask", "F.Paste"],
    PadType.THRU_HOLE: ["*.Cu", "*.Mask"],
    PadType.NP_THRU_HOLE: ["*.Cu", "*.Mask"],
}

_ATTR_TOKEN = {
    MountType.SMD: "smd",
    MountType.THROUGH_HOLE: "through_hole",
    MountType.OTHER: None,
}


@dataclass(frozen=True)
class Box:
    """An axis-aligned rectangle, in mm."""

    x0: float
    y0: float
    x1: float
    y1: float

    def grow(self, amount: float) -> Box:
        return Box(
            self.x0 - amount, self.y0 - amount, self.x1 + amount, self.y1 + amount
        )

    def round_out(self, grid: float) -> Box:
        return Box(
            math.floor(self.x0 / grid + 1e-9) * grid,
            math.floor(self.y0 / grid + 1e-9) * grid,
            math.ceil(self.x1 / grid - 1e-9) * grid,
            math.ceil(self.y1 / grid - 1e-9) * grid,
        )

    def union(self, other: Box) -> Box:
        return Box(
            min(self.x0, other.x0),
            min(self.y0, other.y0),
            max(self.x1, other.x1),
            max(self.y1, other.y1),
        )


def _pad_box(pad: Pad) -> Box:
    """Bounding box of a pad. Rotation is handled by taking the worst case."""
    half_x, half_y = pad.size[0] / 2, pad.size[1] / 2
    if pad.rotation % 180:
        # A rotated pad's bounding box is not the unrotated one. Rather than
        # compute the exact rotated hull, take the circumscribing square: it is
        # never too small, and silk clearance must never be under-estimated.
        half_x = half_y = math.hypot(half_x, half_y)
    return Box(
        pad.at[0] - half_x, pad.at[1] - half_y, pad.at[0] + half_x, pad.at[1] + half_y
    )


def _subtract(span: tuple[float, float], cuts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Remove `cuts` from the 1-D interval `span`, returning what survives."""
    pieces = [span]
    for lo, hi in cuts:
        survivors: list[tuple[float, float]] = []
        for a, b in pieces:
            if hi <= a or lo >= b:
                survivors.append((a, b))
                continue
            if a < lo:
                survivors.append((a, lo))
            if hi < b:
                survivors.append((hi, b))
        pieces = survivors
    return [(a, b) for a, b in pieces if b - a >= MIN_SILK_SEGMENT]


def _round(value: float) -> float:
    return round(value, 6)


# ---------------------------------------------------------------------------
# Node builders
# ---------------------------------------------------------------------------


def _stroke(width: float, kind: str = "solid") -> Node:
    return ["stroke", ["width", fmt_num(width)], ["type", kind]]


def _font(size: float, thickness: float) -> Node:
    return [
        "font",
        ["size", fmt_num(size), fmt_num(size)],
        ["thickness", fmt_num(thickness)],
    ]


def _line(
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    width: float,
    layer: str,
    uuid: str,
) -> Node:
    return [
        "fp_line",
        ["start", fmt_num(_round(start[0])), fmt_num(_round(start[1]))],
        ["end", fmt_num(_round(end[0])), fmt_num(_round(end[1]))],
        _stroke(width),
        ["layer", quote(layer)],
        ["uuid", quote(uuid)],
    ]


def _poly(
    points: list[tuple[float, float]],
    *,
    width: float,
    fill: bool,
    layer: str,
    uuid: str,
) -> Node:
    pts: Node = ["pts"]
    pts += [["xy", fmt_num(_round(x)), fmt_num(_round(y))] for x, y in points]
    return [
        "fp_poly",
        pts,
        _stroke(width),
        ["fill", "yes" if fill else "no"],
        ["layer", quote(layer)],
        ["uuid", quote(uuid)],
    ]


def _rect_lines(
    box: Box, *, width: float, layer: str, uuids: UuidSource, kind: str
) -> list[Node]:
    # Sorted by start point, which is the order `kicad-cli fp upgrade` writes
    # elements back in. Matching it makes the upgrade a no-op on our output, so
    # a file that has been through KiCad diffs cleanly against a regenerated one.
    corners = sorted(
        [
            ((box.x0, box.y0), (box.x1, box.y0)),
            ((box.x1, box.y0), (box.x1, box.y1)),
            ((box.x1, box.y1), (box.x0, box.y1)),
            ((box.x0, box.y1), (box.x0, box.y0)),
        ]
    )
    return [
        _line(a, b, width=width, layer=layer, uuid=uuids.next(kind)) for a, b in corners
    ]


def _property_node(
    name: str,
    value: str,
    *,
    at: tuple[float, float],
    layer: str,
    hide: bool,
    size: float,
    thickness: float,
    uuid: str,
) -> Node:
    node: Node = [
        "property",
        quote(name),
        quote(value),
        ["at", fmt_num(_round(at[0])), fmt_num(_round(at[1])), "0"],
        ["layer", quote(layer)],
    ]
    if hide:
        node.append(["hide", "yes"])
    node.append(["uuid", quote(uuid)])
    node.append(["effects", _font(size, thickness)])
    return node


def _pad_order(number: str) -> tuple:
    """Natural sort key for a pad number: `2` before `10`, `A1` before `B1`.

    Pad numbers are strings because they are not all integers (BGA `A1`, an
    exposed pad called `EP`), so a plain sort would put pad 10 before pad 2.
    """
    return tuple(
        (1, int(chunk), "") if chunk.isdigit() else (0, 0, chunk)
        for chunk in re.findall(r"\d+|\D+", number)
    ) or ((0, 0, ""),)


def _pad_node(pad: Pad, uuid: str) -> Node:
    at: Node = ["at", fmt_num(_round(pad.at[0])), fmt_num(_round(pad.at[1]))]
    if pad.rotation:
        at.append(fmt_num(_round(pad.rotation)))

    node: Node = [
        "pad",
        quote(pad.number),
        pad.type.value,
        pad.shape.value,
        at,
        ["size", fmt_num(_round(pad.size[0])), fmt_num(_round(pad.size[1]))],
    ]
    if pad.drill is not None:
        node.append(["drill", fmt_num(_round(pad.drill))])

    layers = pad.layers if pad.layers is not None else _DEFAULT_LAYERS[pad.type]
    node.append(["layers", *[quote(layer) for layer in layers]])

    if pad.type in (PadType.THRU_HOLE, PadType.NP_THRU_HOLE):
        # Canonical form for a through-hole pad, measured the same way as the
        # rest: `kicad-cli fp upgrade` inserts this and the official
        # `Connector_PinHeader_2.54mm` footprints carry it. Only Phase 4's
        # first through-hole part (a DO-41 diode from LCSC) reached it — the
        # corpus had been all-SMD until then.
        node.append(["remove_unused_layers", "no"])

    if pad.shape.value == "roundrect":
        ratio = pad.roundrect_ratio
        if ratio is None:
            shorter = min(pad.size)
            ratio = min(ROUNDRECT_RATIO, MAX_CORNER_RADIUS / shorter) if shorter else 0
        node.append(["roundrect_rratio", fmt_num(round(ratio, 6))])

    node.append(["uuid", quote(uuid)])
    return node


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------


def _silk_outline(
    body: Box, pads: list[Pad], *, offset: float, clearance: float, width: float
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Body outline on F.SilkS, trimmed clear of every pad."""
    box = body.grow(offset)
    keepout = clearance + width / 2
    grown = [_pad_box(p).grow(keepout) for p in pads]

    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []

    for y in (box.y0, box.y1):
        cuts = [(g.x0, g.x1) for g in grown if g.y0 <= y <= g.y1]
        segments += [
            ((a, y), (b, y)) for a, b in _subtract((box.x0, box.x1), cuts)
        ]
    for x in (box.x0, box.x1):
        cuts = [(g.y0, g.y1) for g in grown if g.x0 <= x <= g.x1]
        segments += [
            ((x, a), (x, b)) for a, b in _subtract((box.y0, box.y1), cuts)
        ]
    # Sorted for the same reason as the courtyard rectangle: it is the order
    # `kicad-cli fp upgrade` writes lines back in.
    return sorted(segments)


def _pin1_marker(
    pads: list[Pad], *, size: float, clearance: float, width: float
) -> list[tuple[float, float]] | None:
    """A filled silk triangle pointing at pad 1, or None if there is no room.

    It sits outside pad 1 along the axis the pad row runs in — for a gull-wing
    package that is "further from the middle of the column", which is where the
    official libraries put it. If the triangle would come too close to any pad,
    we draw nothing: the F.Fab chamfer is the KLC-required pin-1 indicator and
    it is always present.
    """
    first = next((p for p in pads if p.number == "1"), None)
    if first is None:
        return None

    # The pad's long axis points outwards from the body, so the row runs along
    # the short axis; step outwards along that.
    along_y = first.size[0] >= first.size[1]
    if along_y:
        direction = -1.0 if first.at[1] <= 0 else 1.0
        apex = (first.at[0], first.at[1] + direction * (first.size[1] / 2 + clearance + width / 2))
        base_y = apex[1] + direction * size
        points = [apex, (apex[0] - size / 2, base_y), (apex[0] + size / 2, base_y)]
    else:
        direction = -1.0 if first.at[0] <= 0 else 1.0
        apex = (first.at[0] + direction * (first.size[0] / 2 + clearance + width / 2), first.at[1])
        base_x = apex[0] + direction * size
        points = [apex, (base_x, apex[1] - size / 2), (base_x, apex[1] + size / 2)]

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    marker = Box(min(xs), min(ys), max(xs), max(ys)).grow(width / 2)
    for pad in pads:
        blocked = _pad_box(pad).grow(clearance)
        # `_EPS` keeps a marker that merely *touches* the clearance boundary —
        # the common case, since it is placed exactly at that boundary — from
        # being discarded by floating-point noise.
        if (
            marker.x0 < blocked.x1 - _EPS
            and blocked.x0 < marker.x1 - _EPS
            and marker.y0 < blocked.y1 - _EPS
            and blocked.y0 < marker.y1 - _EPS
        ):
            return None
    return points


def _fab_outline(body: Box, chamfer: float) -> list[tuple[float, float]]:
    """Body outline with the pin-1 corner cut away."""
    return [
        (body.x0, body.y0 + chamfer),
        (body.x0, body.y1),
        (body.x1, body.y1),
        (body.x1, body.y0),
        (body.x0 + chamfer, body.y0),
    ]


# ---------------------------------------------------------------------------
# Top level
# ---------------------------------------------------------------------------


def footprint_node(part: Part) -> Node:
    spec = part.footprint
    style = spec.style
    pads = spec.package.resolve_pads()
    uuids = UuidSource(f"{part.library}:{spec.name}")

    body = Box(
        -spec.package.body.x / 2,
        -spec.package.body.y / 2,
        spec.package.body.x / 2,
        spec.package.body.y / 2,
    )
    extent = body
    for pad in pads:
        extent = extent.union(_pad_box(pad))
    courtyard = extent.grow(spec.package.courtyard_excess()).round_out(
        style.courtyard_grid
    )

    node: Node = [
        "footprint",
        quote(spec.name),
        ["version", FOOTPRINT_VERSION],
        ["generator", quote(GENERATOR)],
        ["generator_version", quote(GENERATOR_VERSION)],
        ["layer", quote("F.Cu")],
    ]
    if spec.description:
        node.append(["descr", quote(spec.description)])
    if spec.tags:
        node.append(["tags", quote(spec.tags)])

    text, thick = style.text_size, style.text_thickness
    ref_y = courtyard.y0 - text
    value_y = courtyard.y1 + text
    node.append(
        _property_node(
            "Reference",
            "REF**",
            at=(0, ref_y),
            layer="F.SilkS",
            hide=False,
            size=text,
            thickness=thick,
            uuid=uuids.named("property", "Reference"),
        )
    )
    node.append(
        _property_node(
            "Value",
            spec.name,
            at=(0, value_y),
            layer="F.Fab",
            hide=False,
            size=text,
            thickness=thick,
            uuid=uuids.named("property", "Value"),
        )
    )
    node.append(
        _property_node(
            "Datasheet",
            part.datasheet,
            at=(0, 0),
            layer="F.Fab",
            hide=True,
            size=1.27,
            thickness=thick,
            uuid=uuids.named("property", "Datasheet"),
        )
    )
    node.append(
        _property_node(
            "Description",
            spec.description or part.description,
            at=(0, 0),
            layer="F.Fab",
            hide=True,
            size=1.27,
            thickness=thick,
            uuid=uuids.named("property", "Description"),
        )
    )

    attr = _ATTR_TOKEN[spec.mount_type()]
    attr_flags = [attr] if attr else []
    if spec.exclude_from_pos_files:
        attr_flags.append("exclude_from_pos_files")
    if spec.exclude_from_bom:
        attr_flags.append("exclude_from_bom")
    if attr_flags:
        node.append(["attr", *attr_flags])

    # --- silkscreen -------------------------------------------------------
    for start, end in _silk_outline(
        body,
        pads,
        offset=style.silk_body_offset,
        clearance=style.silk_pad_clearance,
        width=style.silk_width,
    ):
        node.append(
            _line(
                start,
                end,
                width=style.silk_width,
                layer="F.SilkS",
                uuid=uuids.next("silk"),
            )
        )
    marker = _pin1_marker(
        pads,
        size=style.pin1_marker,
        clearance=style.silk_pad_clearance,
        width=style.silk_width,
    )
    if marker:
        node.append(
            _poly(
                marker,
                width=style.silk_width,
                fill=True,
                layer="F.SilkS",
                uuid=uuids.next("silk_poly"),
            )
        )

    # --- courtyard --------------------------------------------------------
    node += _rect_lines(
        courtyard,
        width=style.courtyard_width,
        layer="F.CrtYd",
        uuids=uuids,
        kind="courtyard",
    )

    # --- fabrication ------------------------------------------------------
    chamfer = min(
        FAB_CHAMFER_MAX,
        min(spec.package.body.x, spec.package.body.y) / FAB_CHAMFER_FRACTION,
    )
    node.append(
        _poly(
            _fab_outline(body, chamfer),
            width=style.fab_width,
            fill=False,
            layer="F.Fab",
            uuid=uuids.next("fab"),
        )
    )
    # Reference text is scaled to a quarter of the smaller body dimension and
    # capped, which is the rule the official footprints follow.
    ref_size = max(
        0.25,
        min(
            style.fab_reference_size,
            _round_half_up(min(spec.package.body.x, spec.package.body.y) / 4, 2),
        ),
    )
    node.append(
        [
            "fp_text",
            "user",
            quote("${REFERENCE}"),
            ["at", "0", "0", "0"],
            ["layer", quote("F.Fab")],
            ["uuid", quote(uuids.next("fab_text"))],
            [
                "effects",
                _font(ref_size, _round_half_up(ref_size * TEXT_THICKNESS_RATIO, 2)),
            ],
        ]
    )

    # --- copper -----------------------------------------------------------
    # Pads are written in ascending pad-number order, which is another of
    # kicad-cli's undocumented canonical-form rules (measured: feed it a
    # footprint whose pads are in any other order and `fp upgrade` rewrites
    # every one of them). Every part before Phase 4 happened to declare its
    # pads in order already, so this only surfaced with an EasyEDA import,
    # whose exposed pad is stated first. The sort is stable, so pads sharing a
    # number (a thermal-via variant) keep their relative order.
    for pad in sorted(pads, key=lambda p: _pad_order(p.number)):
        node.append(_pad_node(pad, uuids.named("pad", pad.number)))

    node.append(["embedded_fonts", "no"])

    if spec.model:
        node.append(
            [
                "model",
                quote(spec.model),
                ["offset", ["xyz", "0", "0", "0"]],
                ["scale", ["xyz", "1", "1", "1"]],
                ["rotate", ["xyz", "0", "0", "0"]],
            ]
        )
    return node


def render_footprint(part: Part) -> str:
    """Render one `.kicad_mod` file."""
    return sexpr.dumps(footprint_node(part))
