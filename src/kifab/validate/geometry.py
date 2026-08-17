"""Geometry sanity — the checks that catch the errors which scrap boards.

**Layer: the file, not the IR.** Silkscreen, courtyard and layer names do not
exist in the IR; they are produced by the emitter. So every rule here reads a
`ParsedFootprint` / `ParsedSymbol`, and an IR part is checked by rendering it
first (see `kifab.validate.check_part`). The one cross-representation rule,
symbol/footprint pin-set agreement, takes both.

Every threshold is a named constant with its rationale, and every rule is
pass/fail on a measurement — none of them score.
"""

from __future__ import annotations

import math

from .parse import ParsedFootprint, ParsedGraphic, ParsedPad, ParsedPin, ParsedSymbol
from .report import LAYER_FOOTPRINT, LAYER_SYMBOL, Report, Severity

# --- thresholds -----------------------------------------------------------

#: Float noise guard, in mm. 0.1 µm is three orders of magnitude below the
#: smallest real clearance rule and below KiCad's own 1 nm internal unit
#: rounding, so it can never hide a defect but always absorbs a `0.2 - 0.2`
#: that lands at -1e-16.
TOL = 1e-4

#: Minimum copper gap between lands of *different* numbers, in mm. Calibrated
#: against the shipped corpus rather than guessed: the tightest gap the official
#: KiCad gull-wing libraries produce is **0.14 mm** (every 0.4 mm-pitch QFP, of
#: which there are 252 pad pairs in Package_QFP alone), so a limit of 0.15 would
#: warn on standard fine-pitch land patterns. 0.10 mm sits below every land
#: pattern in the corpus and at the floor of what a cheap fab will image.
#: Overlap (gap <= 0) is an error at any density — it is a short.
MIN_PAD_GAP = 0.1

#: Minimum gap from an exposed/thermal pad to a perimeter land, in mm. Same
#: physical rule as MIN_PAD_GAP, called out separately because the failure is
#: different in kind: an exposed pad that reaches a signal land shorts every
#: net that touches it to the thermal plane.
MIN_EXPOSED_PAD_GAP = 0.1

#: Minimum clearance from a pad edge to the *edge* of a silkscreen line, in mm.
#: KLC F5.1. Silk printed onto exposed copper wicks into the solder joint.
SILK_PAD_CLEARANCE = 0.2

#: KLC F5.3: courtyard coordinates are on a 0.01 mm grid.
COURTYARD_GRID = 0.01

#: Minimum distance from the courtyard to the copper it encloses, in mm.
#: A courtyard drawn on the pad edge lets a neighbouring part's courtyard abut
#: it with zero real clearance, which is what the courtyard exists to prevent.
#: Deliberately far below the IPC nominal excess (0.25) so that a legitimately
#: dense footprint does not warn.
COURTYARD_MIN_EXCESS = 0.05

#: KLC S4.1: symbol pins sit on a 100 mil (2.54 mm) grid. A pin off it cannot
#: be wired to reliably, because the schematic editor snaps the wire and not
#: the pin.
PIN_GRID = 2.54


# --- 2-D primitives -------------------------------------------------------

Point = tuple[float, float]
Poly = list[Point]


def _point_segment_distance(p: Point, a: Point, b: Point) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 <= 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _segments_cross(a: Point, b: Point, c: Point, d: Point) -> bool:
    def orient(p: Point, q: Point, r: Point) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    o1, o2 = orient(a, b, c), orient(a, b, d)
    o3, o4 = orient(c, d, a), orient(c, d, b)
    return (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0)


def segment_distance(a: Point, b: Point, c: Point, d: Point) -> float:
    if _segments_cross(a, b, c, d):
        return 0.0
    return min(
        _point_segment_distance(a, c, d),
        _point_segment_distance(b, c, d),
        _point_segment_distance(c, a, b),
        _point_segment_distance(d, a, b),
    )


def point_in_polygon(p: Point, poly: Poly) -> bool:
    """Ray casting. Exact enough: boundary cases are handled by the distance."""
    x, y = p
    inside = False
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        if (y0 > y) != (y1 > y):
            cross = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
            if cross > x:
                inside = not inside
    return inside


def _edges(poly: Poly, closed: bool = True) -> list[tuple[Point, Point]]:
    n = len(poly)
    if n < 2:
        return []
    pairs = [(poly[i], poly[i + 1]) for i in range(n - 1)]
    if closed and n > 2:
        pairs.append((poly[-1], poly[0]))
    return pairs


def polygon_distance(a: Poly, b: Poly) -> float:
    """Closest approach of two polygons; 0 when they touch or overlap.

    Exact for convex and non-convex outlines alike, which matters because a
    rotated pad is not an axis-aligned box and approximating it by its
    circumscribing square would invent clearance failures.
    """
    if any(point_in_polygon(p, b) for p in a) or any(point_in_polygon(p, a) for p in b):
        return 0.0
    best = math.inf
    for p0, p1 in _edges(a):
        for q0, q1 in _edges(b):
            best = min(best, segment_distance(p0, p1, q0, q1))
    return best if best is not math.inf else 0.0


def point_polygon_distance(p: Point, poly: Poly) -> float:
    if point_in_polygon(p, poly):
        return 0.0
    return min(_point_segment_distance(p, a, b) for a, b in _edges(poly))


def pad_polygon(pad: ParsedPad) -> Poly:
    return pad.corners()


def graphic_distance_to_pad(graphic: ParsedGraphic, pad: ParsedPad) -> float:
    """Distance from the drawn *edge* of a graphic to the pad edge, in mm.

    The stroke's half-width is subtracted, so the number returned is the gap a
    fabricator sees, not the gap between centrelines. Circles are treated as
    discs: conservative for an unfilled ring drawn around a pad, which is rare
    and errs towards reporting rather than missing.
    """
    poly = pad_polygon(pad)
    if graphic.centre is not None:
        gap = point_polygon_distance(graphic.centre, poly) - graphic.radius
        return gap - graphic.width / 2
    points = list(graphic.points)
    if not points:
        return math.inf
    if graphic.filled and graphic.closed and len(points) >= 3:
        return polygon_distance(points, poly) - graphic.width / 2
    best = math.inf
    for a, b in _edges(points, closed=graphic.closed):
        for c, d in _edges(poly):
            best = min(best, segment_distance(a, b, c, d))
    if len(points) == 1:
        best = point_polygon_distance(points[0], poly)
    return best - graphic.width / 2


def _bbox(points: list[Point]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def graphic_points(graphic: ParsedGraphic) -> list[Point]:
    if graphic.centre is not None:
        cx, cy = graphic.centre
        r = graphic.radius
        return [(cx - r, cy - r), (cx + r, cy + r)]
    return list(graphic.points)


# --- checks: footprint ----------------------------------------------------


def _pad_label(pad: ParsedPad) -> str:
    return f'pad "{pad.number}"'


def check_pad_clearance(fp: ParsedFootprint) -> Report:
    """GEO001/GEO002 — copper of different nets must not touch.

    Pads that share a number are one net (`*_ThermalVias` variants number their
    vias as the exposed pad, and split exposed pads are drawn as several
    same-numbered rectangles), so they are never compared with each other.
    Unnumbered pads are paste apertures and carry no net at all.
    """
    report = Report()
    exposed = fp.exposed_pad_numbers()
    hosts = fp.via_hosts()
    pads = fp.measurable_pads()
    for pad in fp.unmeasurable_pads():
        report.add(
            "GEO001",
            Severity.INFO,
            "pad is a `custom` shape, so its copper outline is not measurable "
            "from `(size ...)`; clearance to it was not checked",
            where=_pad_label(pad),
            layer=LAYER_FOOTPRINT,
            at=pad.at,
        )
    # Sweep in x so a 2000-ball BGA costs O(n log n + k) rather than O(n^2):
    # exact polygon distance is only computed for pairs whose bounding boxes
    # are already within the limit.
    limit = max(MIN_PAD_GAP, MIN_EXPOSED_PAD_GAP)
    boxes = {id(p): _bbox(pad_polygon(p)) for p in pads}
    pads = sorted(pads, key=lambda p: boxes[id(p)][0])
    for i, a in enumerate(pads):
        _, ay0, ax1, ay1 = boxes[id(a)]
        for b in pads[i + 1 :]:
            bx0, by0, _, by1 = boxes[id(b)]
            if bx0 > ax1 + limit:
                break  # and so is every pad after it
            if by0 > ay1 + limit or ay0 > by1 + limit:
                continue
            if a.number == b.number:
                continue
            if not (a.copper_layers() & b.copper_layers()):
                continue
            # A thermal via touching the land it stitches is the point of it.
            if b.number in hosts.get(id(a), ()) or a.number in hosts.get(id(b), ()):
                continue
            gap = polygon_distance(pad_polygon(a), pad_polygon(b))
            involves_ep = a.number in exposed or b.number in exposed
            check = "GEO002" if involves_ep else "GEO001"
            limit = MIN_EXPOSED_PAD_GAP if involves_ep else MIN_PAD_GAP
            where = f"{_pad_label(a)} <-> {_pad_label(b)}"
            midpoint = ((a.x + b.x) / 2, (a.y + b.y) / 2)
            if gap <= TOL:
                report.add(
                    check,
                    Severity.ERROR,
                    "copper of two different pad numbers overlaps or touches "
                    f"(gap {gap:.3f} mm) — this is a short",
                    where=where,
                    layer=LAYER_FOOTPRINT,
                    at=midpoint,
                )
            elif gap < limit - TOL:
                report.add(
                    check,
                    Severity.WARNING,
                    f"copper gap {gap:.3f} mm is below the {limit} mm minimum "
                    "conductor spacing",
                    where=where,
                    layer=LAYER_FOOTPRINT,
                    at=midpoint,
                )
    return report


def check_courtyard(fp: ParsedFootprint) -> Report:
    """GEO003 — a courtyard exists, is on grid, and contains the whole part."""
    report = Report()
    items = fp.on_layer("F.CrtYd") + fp.on_layer("B.CrtYd")
    if not items:
        report.add(
            "GEO003",
            Severity.ERROR,
            "no courtyard: nothing on F.CrtYd, so KiCad's DRC cannot detect a "
            "collision with a neighbouring part",
            where="footprint",
            layer=LAYER_FOOTPRINT,
        )
        return report

    points: list[Point] = []
    off_grid: list[tuple[str, float, Point]] = []
    for graphic in items:
        points += graphic_points(graphic)
        for x, y in graphic_points(graphic):
            for value, axis in ((x, "x"), (y, "y")):
                if abs(value / COURTYARD_GRID - round(value / COURTYARD_GRID)) > 1e-6:
                    off_grid.append((axis, value, (x, y)))
    if off_grid:
        # One finding per footprint: an off-grid courtyard is a single mistake
        # repeated at every corner, and reporting each corner buries the rest
        # of the report.
        axis, value, at = off_grid[0]
        more = f" (and {len(off_grid) - 1} more coordinate(s))" if len(off_grid) > 1 else ""
        report.add(
            "GEO003",
            Severity.WARNING,
            f"courtyard {axis}={value:g} is not on the {COURTYARD_GRID} mm "
            f"grid (KLC F5.3){more}",
            where="courtyard",
            layer=LAYER_FOOTPRINT,
            at=at,
        )
    if not points:
        return report

    x0, y0, x1, y1 = _bbox(points)
    escaped: list[ParsedPad] = []
    tight: list[tuple[float, ParsedPad]] = []
    for pad in fp.measurable_pads() + fp.aperture_pads:
        corners = pad_polygon(pad)
        outside = [
            c
            for c in corners
            if not (x0 - TOL <= c[0] <= x1 + TOL and y0 - TOL <= c[1] <= y1 + TOL)
        ]
        if outside:
            escaped.append(pad)
            continue
        excess = min(
            min(abs(c[0] - x0), abs(c[0] - x1), abs(c[1] - y0), abs(c[1] - y1))
            for c in corners
        )
        if excess < COURTYARD_MIN_EXCESS - TOL:
            tight.append((excess, pad))

    # A courtyard that is too small is one mistake, not one per pad: report it
    # once, naming a pad the reader can go and look at.
    if escaped:
        more = f" (and {len(escaped) - 1} other pad(s))" if len(escaped) > 1 else ""
        report.add(
            "GEO003",
            Severity.ERROR,
            f"pad lies outside the courtyard [{x0:g} {y0:g}] .. [{x1:g} {y1:g}]"
            f"{more} — placement DRC will not see this copper",
            where=_pad_label(escaped[0]),
            layer=LAYER_FOOTPRINT,
            at=escaped[0].at,
        )
    if tight:
        excess, pad = min(tight, key=lambda item: item[0])
        more = f" (and {len(tight) - 1} other pad(s))" if len(tight) > 1 else ""
        report.add(
            "GEO003",
            Severity.WARNING,
            f"courtyard clears this pad by only {excess:.3f} mm (want at least "
            f"{COURTYARD_MIN_EXCESS} mm){more}",
            where=_pad_label(pad),
            layer=LAYER_FOOTPRINT,
            at=pad.at,
        )

    for graphic in fp.on_layer("F.Fab") + fp.on_layer("B.Fab"):
        for x, y in graphic_points(graphic):
            if not (x0 - TOL <= x <= x1 + TOL and y0 - TOL <= y <= y1 + TOL):
                report.add(
                    "GEO003",
                    Severity.ERROR,
                    "the fabrication body outline extends outside the courtyard",
                    where="F.Fab outline",
                    layer=LAYER_FOOTPRINT,
                    at=(x, y),
                )
                break
    return report


def check_silk_clearance(fp: ParsedFootprint) -> Report:
    """GEO004 — silkscreen must not be printed onto or beside exposed copper."""
    report = Report()
    for silk_layer, copper in (("F.SilkS", "F.Cu"), ("B.SilkS", "B.Cu")):
        silk = fp.on_layer(silk_layer)
        if not silk:
            continue
        for pad in fp.measurable_pads():
            if copper not in pad.copper_layers():
                continue
            # One finding per pad, naming its closest offender: a silk outline
            # that runs past a pad is one mistake, not one per line segment.
            worst = min(
                ((graphic_distance_to_pad(g, pad), g) for g in silk),
                key=lambda item: item[0],
            )
            gap, graphic = worst
            if gap > SILK_PAD_CLEARANCE - TOL:
                continue
            points = graphic_points(graphic)
            where = f"{silk_layer} {graphic.kind} vs {_pad_label(pad)}"
            at = points[0] if points else pad.at
            if gap <= TOL:
                report.add(
                    "GEO004",
                    Severity.ERROR,
                    "silkscreen overlaps pad copper — the ink wicks into the "
                    "solder joint",
                    where=where,
                    layer=LAYER_FOOTPRINT,
                    at=at,
                )
            else:
                report.add(
                    "GEO004",
                    Severity.WARNING,
                    f"silk-to-pad clearance {gap:.3f} mm is below the "
                    f"{SILK_PAD_CLEARANCE} mm KLC F5.1 minimum",
                    where=where,
                    layer=LAYER_FOOTPRINT,
                    at=at,
                )
    return report


def check_pad_numbering(fp: ParsedFootprint) -> Report:
    """GEO005 — two pads with the same number at the same place is a typo."""
    report = Report()
    seen: dict[tuple, ParsedPad] = {}
    for pad in fp.pads:
        # Type and layers are part of the key, because two of the three shapes
        # that legitimately stack on one spot differ only in those: a thermal
        # via sits on the exposed pad it stitches, and a `*_ThermalVias`
        # variant puts a matching land on B.Cu directly underneath. Only pads
        # identical in every one of these is a genuine duplicate.
        key = (
            pad.number,
            pad.type,
            tuple(pad.layers),
            round(pad.x, 4),
            round(pad.y, 4),
            round(pad.w, 4),
            round(pad.h, 4),
        )
        if key in seen:
            report.add(
                "GEO005",
                Severity.ERROR,
                "two pads share this number *and* this position — one of them "
                "is a duplicate, and KiCad will silently keep both",
                where=_pad_label(pad),
                layer=LAYER_FOOTPRINT,
                at=pad.at,
            )
        seen[key] = pad
    return report


def check_pad_geometry(fp: ParsedFootprint) -> Report:
    """GEO006 — a pad must have area, and a drilled pad must have an annulus."""
    report = Report()
    for pad in fp.pads + fp.aperture_pads:
        if pad.w <= 0 or pad.h <= 0:
            report.add(
                "GEO006",
                Severity.ERROR,
                f"pad size ({pad.w:g} x {pad.h:g} mm) has no area",
                where=_pad_label(pad),
                layer=LAYER_FOOTPRINT,
                at=pad.at,
            )
            continue
        # Only *plated* holes need an annulus. A `np_thru_hole` whose pad size
        # equals its drill is how KiCad spells a plain mechanical hole.
        if (
            pad.type == "thru_hole"
            and pad.drill is not None
            and pad.drill >= min(pad.w, pad.h) - TOL
        ):
            report.add(
                "GEO006",
                Severity.ERROR,
                f"drill {pad.drill:g} mm is not smaller than the pad "
                f"({pad.w:g} x {pad.h:g} mm): there is no annular ring left to "
                "solder to",
                where=_pad_label(pad),
                layer=LAYER_FOOTPRINT,
                at=pad.at,
            )
    return report


FOOTPRINT_CHECKS = (
    check_pad_clearance,
    check_courtyard,
    check_silk_clearance,
    check_pad_numbering,
    check_pad_geometry,
)


def check_footprint_geometry(fp: ParsedFootprint) -> Report:
    report = Report()
    for check in FOOTPRINT_CHECKS:
        report.extend(check(fp))
    return report


# --- checks: symbol -------------------------------------------------------


def check_pin_grid(symbol: ParsedSymbol) -> Report:
    """GEO007 — every pin's connection point is on the 100 mil grid."""
    report = Report()
    for pin in symbol.pins:
        off = [
            f"{axis}={value:g}"
            for axis, value in (("x", pin.x), ("y", pin.y))
            if abs(value / PIN_GRID - round(value / PIN_GRID)) > 1e-6
        ]
        if off:
            report.add(
                "GEO007",
                Severity.ERROR,
                f"pin is off the {PIN_GRID} mm schematic grid ({', '.join(off)}) "
                "— a wire cannot reliably attach to it",
                where=f'pin "{pin.number}" ({pin.name})',
                layer=LAYER_SYMBOL,
                at=pin.at,
            )
    return report


def check_duplicate_pins(symbol: ParsedSymbol) -> Report:
    """GEO008 — one pad number, one pin."""
    report = Report()
    seen: dict[tuple[str, int], ParsedPin] = {}
    for pin in symbol.pins:
        key = (pin.number, pin.unit)
        if key in seen:
            report.add(
                "GEO008",
                Severity.ERROR,
                f'pin number "{pin.number}" appears more than once in unit '
                f"{pin.unit}; each pad may be referenced by exactly one pin",
                where=f'pin "{pin.number}" ({pin.name})',
                layer=LAYER_SYMBOL,
                at=pin.at,
            )
        seen[key] = pin
    return report


SYMBOL_CHECKS = (check_pin_grid, check_duplicate_pins)


def check_symbol_geometry(symbol: ParsedSymbol) -> Report:
    report = Report()
    for check in SYMBOL_CHECKS:
        report.extend(check(symbol))
    return report


# --- checks: both halves together -----------------------------------------


def check_pin_sets(symbol: ParsedSymbol, fp: ParsedFootprint) -> Report:
    """GEO009 — the symbol and the footprint must agree about pin numbers.

    The one rule that needs both files. A disagreement produces a board that
    cannot be routed correctly and that nothing downstream notices: the netlist
    simply drops the nets whose pads do not exist.
    """
    report = Report()
    pins = symbol.pin_numbers
    pads = fp.pad_numbers
    where = f"{symbol.name} <-> {fp.name}"
    missing = sorted(pins - pads)
    if missing:
        report.add(
            "GEO009",
            Severity.ERROR,
            f"symbol pins {missing} have no pad in the footprint "
            f"(pads: {sorted(pads)})",
            where=where,
            layer=LAYER_SYMBOL,
        )
    unbonded = sorted(pads - pins)
    if unbonded:
        report.add(
            "GEO009",
            Severity.ERROR,
            f"footprint pads {unbonded} have no symbol pin, so no net can ever "
            "reach them",
            where=where,
            layer=LAYER_FOOTPRINT,
        )
    return report
