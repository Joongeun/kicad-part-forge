"""A flat, checkable view of an emitted `.kicad_mod` / `.kicad_sym`.

Why this exists at all, given the IR: **some defects only exist in one
representation.** The IR has no silkscreen, no courtyard and no layer names —
those are produced by the emitter, so a silk line lying on a pad is a defect
that is simply not expressible upstream of the file. Equally, a file that came
from somewhere else (an adopted KiCad part, an EasyEDA import) has no IR at
all. So every geometry and convention rule is written against *this* view, and
an IR part is checked by rendering it and reading the result back.

Reuses `kifab.emit.sexpr`, which Phase 0 proved lossless across the whole
shipped corpus. Two traps found in Phase 2 are handled here rather than in each
check, because forgetting either one makes a DFN read as a QFN:

* **unnumbered pads** are paste-relief apertures and mechanical features, not
  lands. They are kept separately (`aperture_pads`) and excluded from anything
  that reasons about nets.
* **`*_ThermalVias` variants** carry vias numbered as the exposed pad. They
  land in `pads` like any other, so every clearance rule compares pads *of
  different numbers only* — same-number copper is one net and is allowed, by
  design, to touch.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from ..emit import sexpr
from ..index.read import unquote

#: How finely an arc is sampled when measuring clearance to it. 16 chords over
#: a full circle keeps the sagitta under 2 µm for a 1 mm radius, which is an
#: order of magnitude below any clearance rule here.
ARC_SAMPLES = 16


class ParseError(ValueError):
    """The file is not the kind of KiCad file it was read as."""


def _atoms(node: sexpr.Node) -> list[str]:
    return [c for c in node if isinstance(c, str)]


def _str_arg(node: sexpr.Node | None, index: int = 1) -> str:
    if node is None:
        return ""
    atoms = _atoms(node)
    return unquote(atoms[index]) if len(atoms) > index else ""


def _nums(node: sexpr.Node | None) -> list[float]:
    if node is None:
        return []
    out: list[float] = []
    for atom in _atoms(node)[1:]:
        try:
            out.append(float(atom))
        except ValueError:
            break
    return out


def _layers(node: sexpr.Node) -> list[str]:
    layer = sexpr.find(node, "layer")
    if layer is None:
        layer = sexpr.find(node, "layers")
    if layer is None:
        return []
    return [unquote(a) for a in _atoms(layer)[1:]]


def _stroke_width(node: sexpr.Node) -> float:
    stroke = sexpr.find(node, "stroke")
    if stroke is not None:
        values = _nums(sexpr.find(stroke, "width"))
        if values:
            return values[0]
    values = _nums(sexpr.find(node, "width"))  # pre-v7 spelling
    return values[0] if values else 0.0


def _filled(node: sexpr.Node) -> bool:
    fill = sexpr.find(node, "fill")
    if fill is None:
        return False
    atoms = _atoms(fill)[1:]
    if atoms:
        return atoms[0] in ("yes", "true", "solid")
    kind = sexpr.find(fill, "type")
    return _str_arg(kind) not in ("", "none")


def _font_of(node: sexpr.Node) -> tuple[float, float]:
    """(text size, thickness) in mm; (0, 0) when the node states neither."""
    effects = sexpr.find(node, "effects")
    if effects is None:
        return (0.0, 0.0)
    font = sexpr.find(effects, "font")
    if font is None:
        return (0.0, 0.0)
    size = _nums(sexpr.find(font, "size"))
    thickness = _nums(sexpr.find(font, "thickness"))
    return (size[0] if size else 0.0, thickness[0] if thickness else 0.0)


def _hidden(node: sexpr.Node) -> bool:
    hide = sexpr.find(node, "hide")
    if hide is not None:
        return _atoms(hide)[1:] != ["no"]
    effects = sexpr.find(node, "effects")
    if effects is not None and sexpr.find(effects, "hide") is not None:
        return True
    return False


# --------------------------------------------------------------------------
# Footprints
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedPad:
    number: str
    x: float
    y: float
    w: float
    h: float
    rotation: float = 0.0
    type: str = "smd"
    shape: str = "roundrect"
    layers: tuple[str, ...] = ()
    drill: float | None = None

    @property
    def at(self) -> tuple[float, float]:
        return (self.x, self.y)

    def copper_layers(self) -> frozenset[str]:
        """Which copper this pad is actually on.

        Two pads can only short each other if this intersects: a `*_ThermalVias`
        variant puts a B.Cu land under the exposed pad that overlaps every
        perimeter land in plan view and touches none of them in reality.
        `*.Cu` (through-hole) reaches every layer, so it intersects everything.
        """
        out: set[str] = set()
        for layer in self.layers:
            if layer == "*.Cu":
                return frozenset({"F.Cu", "B.Cu", "In.Cu"})
            if layer.endswith(".Cu"):
                out.add(layer)
        return frozenset(out) or frozenset({"F.Cu"})

    def corners(self) -> list[tuple[float, float]]:
        """The four corners of the placed pad, rotation included."""
        hx, hy = self.w / 2, self.h / 2
        theta = math.radians(self.rotation)
        cos, sin = math.cos(theta), math.sin(theta)
        out = []
        for dx, dy in ((-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)):
            # KiCad's pad rotation is counter-clockwise on screen, and screen y
            # runs downwards; the sign convention below is the one that
            # reproduces the corner positions kicad-cli renders.
            out.append((self.x + dx * cos + dy * sin, self.y - dx * sin + dy * cos))
        return out


@dataclass(frozen=True)
class ParsedGraphic:
    """One drawn item, reduced to a polyline plus (for circles) a radius."""

    kind: str
    layer: str
    width: float
    filled: bool
    points: tuple[tuple[float, float], ...] = ()
    centre: tuple[float, float] | None = None
    radius: float = 0.0
    closed: bool = False


@dataclass(frozen=True)
class ParsedText:
    kind: str
    text: str
    layer: str
    at: tuple[float, float]
    size: float
    thickness: float
    hidden: bool


@dataclass
class ParsedFootprint:
    name: str
    source: str = ""
    version: str = ""
    generator: str = ""
    descr: str = ""
    tags: str = ""
    attrs: tuple[str, ...] = ()
    pads: list[ParsedPad] = field(default_factory=list)
    aperture_pads: list[ParsedPad] = field(default_factory=list)
    graphics: list[ParsedGraphic] = field(default_factory=list)
    texts: list[ParsedText] = field(default_factory=list)
    properties: dict[str, ParsedText] = field(default_factory=dict)
    model: str = ""

    @property
    def pad_numbers(self) -> set[str]:
        return {p.number for p in self.pads}

    def on_layer(self, layer: str) -> list[ParsedGraphic]:
        return [g for g in self.graphics if g.layer == layer]

    def via_hosts(self) -> dict[int, frozenset[str]]:
        """Thermal vias, mapped to the numbers of the lands they stitch.

        This is the `*_ThermalVias` trap from Phase 2. A via driven through an
        exposed pad is not a component lead: it must not make an SMD footprint
        read as through-hole, its coincidence with the pad it stitches is not a
        duplicate, and its contact with that pad is not a short.

        A drilled pad counts as a via when it shares its number with an SMD
        land, **or** when it lies entirely inside one — Texas Instruments'
        `Texas_RGY_R-PVQFN-N16` numbers its via net separately from the exposed
        pad it sits in, so the number test alone is not enough.
        """
        smd = [p for p in self.pads if p.type == "smd"]
        by_number = {p.number for p in smd}
        out: dict[int, frozenset[str]] = {}
        for pad in self.pads:
            if pad.type == "smd":
                continue
            hosts = {pad.number} if pad.number in by_number else set()
            for host in smd:
                if _contains(host, pad):
                    hosts.add(host.number)
            if hosts:
                out[id(pad)] = frozenset(hosts)
        return out

    def via_pads(self) -> list[ParsedPad]:
        hosts = self.via_hosts()
        return [p for p in self.pads if id(p) in hosts]

    def leads(self) -> list[ParsedPad]:
        """Numbered pads that are component leads rather than thermal vias."""
        hosts = self.via_hosts()
        return [p for p in self.pads if id(p) not in hosts]

    def measurable_pads(self) -> list[ParsedPad]:
        """Pads whose copper outline is known from `(size ...)` alone.

        A `custom` pad's real outline lives in its `(primitives ...)` and its
        `size` describes only the base shape, so measuring clearance from it
        would invent overlaps that are not there. Those pads are reported as
        unmeasured rather than measured wrongly.
        """
        return [p for p in self.pads if p.shape != "custom"]

    def unmeasurable_pads(self) -> list[ParsedPad]:
        return [p for p in self.pads if p.shape == "custom"]

    def exposed_pad_numbers(self) -> set[str]:
        """Pads that are thermal/exposed rather than perimeter lands.

        Crisp rule, no scoring: a pad is exposed if its number is the highest
        numeric one *and* its area is at least `_EP_AREA_FACTOR` times the
        median perimeter land. Non-numeric names KiCad uses for the same thing
        (`EP`, `PAD`, `TH`) count too.
        """
        named = {p.number for p in self.pads if p.number.upper() in ("EP", "PAD", "TH")}
        numeric = [p for p in self.pads if p.number.isdigit()]
        if len(numeric) >= 3:
            areas = sorted(p.w * p.h for p in numeric)
            median = areas[len(areas) // 2]
            top = max(int(p.number) for p in numeric)
            for pad in numeric:
                if int(pad.number) == top and pad.w * pad.h >= _EP_AREA_FACTOR * median:
                    named.add(pad.number)
        return named

    @classmethod
    def from_text(cls, text: str, source: str = "") -> ParsedFootprint:
        try:
            root = sexpr.parse(text)
        except sexpr.SexprError as exc:
            raise ParseError(f"{source or 'input'}: {exc}") from exc
        head = root[0] if root and isinstance(root[0], str) else ""
        if head not in ("footprint", "module"):
            raise ParseError(f"{source or 'input'}: not a footprint file")
        return _read_footprint(root, source)

    @classmethod
    def from_path(cls, path: Path) -> ParsedFootprint:
        return cls.from_text(Path(path).read_text(encoding="utf-8"), str(path))


def _contains(outer: ParsedPad, inner: ParsedPad) -> bool:
    """True when `inner`'s copper lies wholly inside `outer`'s, in plan view."""
    ox, oy = outer.w / 2, outer.h / 2
    ix, iy = inner.w / 2, inner.h / 2
    return (
        inner.x - ix >= outer.x - ox - 1e-9
        and inner.x + ix <= outer.x + ox + 1e-9
        and inner.y - iy >= outer.y - oy - 1e-9
        and inner.y + iy <= outer.y + oy + 1e-9
    )


#: A pad this many times larger in area than the median land is a thermal pad,
#: not a land. 4x is far above the spread seen between perimeter pads of one
#: package and far below any real exposed pad, so nothing sits near the line.
_EP_AREA_FACTOR = 4.0

_GRAPHIC_TOKENS = ("fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly")


def _arc_points(
    start: tuple[float, float], mid: tuple[float, float], end: tuple[float, float]
) -> list[tuple[float, float]]:
    """Sample the circular arc through three points."""
    ax, ay = start
    bx, by = mid
    cx, cy = end
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:  # collinear — a straight line is the honest reading
        return [start, mid, end]
    ux = (
        (ax**2 + ay**2) * (by - cy)
        + (bx**2 + by**2) * (cy - ay)
        + (cx**2 + cy**2) * (ay - by)
    ) / d
    uy = (
        (ax**2 + ay**2) * (cx - bx)
        + (bx**2 + by**2) * (ax - cx)
        + (cx**2 + cy**2) * (bx - ax)
    ) / d
    radius = math.hypot(ax - ux, ay - uy)
    a0 = math.atan2(ay - uy, ax - ux)
    a1 = math.atan2(by - uy, bx - ux)
    a2 = math.atan2(cy - uy, cx - ux)
    # Walk start -> mid -> end the short way round each leg.
    def _sweep(f: float, t: float) -> float:
        delta = (t - f) % (2 * math.pi)
        return delta if delta <= math.pi else delta - 2 * math.pi

    legs = [(a0, _sweep(a0, a1)), (a1, _sweep(a1, a2))]
    points: list[tuple[float, float]] = []
    for base, sweep in legs:
        steps = max(2, ARC_SAMPLES // 2)
        for i in range(steps + 1):
            angle = base + sweep * i / steps
            points.append((ux + radius * math.cos(angle), uy + radius * math.sin(angle)))
    return points


def _read_graphic(node: sexpr.Node) -> ParsedGraphic | None:
    kind = node[0] if isinstance(node[0], str) else ""
    layers = _layers(node)
    layer = layers[0] if layers else ""
    width = _stroke_width(node)
    filled = _filled(node)

    if kind == "fp_line":
        start, end = _nums(sexpr.find(node, "start")), _nums(sexpr.find(node, "end"))
        if len(start) < 2 or len(end) < 2:
            return None
        return ParsedGraphic(
            kind, layer, width, filled, ((start[0], start[1]), (end[0], end[1]))
        )
    if kind == "fp_rect":
        start, end = _nums(sexpr.find(node, "start")), _nums(sexpr.find(node, "end"))
        if len(start) < 2 or len(end) < 2:
            return None
        x0, y0, x1, y1 = start[0], start[1], end[0], end[1]
        return ParsedGraphic(
            kind,
            layer,
            width,
            filled,
            ((x0, y0), (x1, y0), (x1, y1), (x0, y1)),
            closed=True,
        )
    if kind == "fp_circle":
        centre, edge = _nums(sexpr.find(node, "center")), _nums(sexpr.find(node, "end"))
        if len(centre) < 2 or len(edge) < 2:
            return None
        radius = math.hypot(edge[0] - centre[0], edge[1] - centre[1])
        return ParsedGraphic(
            kind,
            layer,
            width,
            filled,
            (),
            centre=(centre[0], centre[1]),
            radius=radius,
        )
    if kind == "fp_arc":
        a = _nums(sexpr.find(node, "start"))
        b = _nums(sexpr.find(node, "mid"))
        c = _nums(sexpr.find(node, "end"))
        if len(a) < 2 or len(b) < 2 or len(c) < 2:
            return None
        pts = _arc_points((a[0], a[1]), (b[0], b[1]), (c[0], c[1]))
        return ParsedGraphic(kind, layer, width, filled, tuple(pts))
    if kind == "fp_poly":
        pts_node = sexpr.find(node, "pts")
        if pts_node is None:
            return None
        points = []
        for xy in sexpr.find_all(pts_node, "xy"):
            values = _nums(xy)
            if len(values) >= 2:
                points.append((values[0], values[1]))
        if len(points) < 2:
            return None
        return ParsedGraphic(kind, layer, width, filled, tuple(points), closed=True)
    return None


def _read_footprint(root: sexpr.Node, source: str) -> ParsedFootprint:
    attr = sexpr.find(root, "attr")
    fp = ParsedFootprint(
        name=_str_arg(root, 1),
        source=source,
        version=_str_arg(sexpr.find(root, "version")),
        generator=_str_arg(sexpr.find(root, "generator")),
        descr=_str_arg(sexpr.find(root, "descr")),
        tags=_str_arg(sexpr.find(root, "tags")),
        attrs=tuple(unquote(a) for a in _atoms(attr)[1:]) if attr is not None else (),
        model=_str_arg(sexpr.find(root, "model")),
    )

    for pad in sexpr.find_all(root, "pad"):
        atoms = _atoms(pad)
        number = unquote(atoms[1]) if len(atoms) > 1 else ""
        at = _nums(sexpr.find(pad, "at"))
        size = _nums(sexpr.find(pad, "size"))
        if len(at) < 2 or len(size) < 2:
            continue
        drill = _nums(sexpr.find(pad, "drill"))
        parsed = ParsedPad(
            number=number,
            x=at[0],
            y=at[1],
            w=size[0],
            h=size[1],
            rotation=at[2] if len(at) > 2 else 0.0,
            type=atoms[2] if len(atoms) > 2 else "smd",
            shape=atoms[3] if len(atoms) > 3 else "",
            layers=tuple(_layers(pad)),
            drill=drill[0] if drill else None,
        )
        if number.strip():
            fp.pads.append(parsed)
        else:
            fp.aperture_pads.append(parsed)

    for child in root:
        if not isinstance(child, list):
            continue
        head = child[0] if child and isinstance(child[0], str) else ""
        if head in _GRAPHIC_TOKENS:
            graphic = _read_graphic(child)
            if graphic is not None:
                fp.graphics.append(graphic)
        elif head == "fp_text":
            atoms = _atoms(child)
            at = _nums(sexpr.find(child, "at"))
            size, thickness = _font_of(child)
            layers = _layers(child)
            fp.texts.append(
                ParsedText(
                    kind=atoms[1] if len(atoms) > 1 else "",
                    text=unquote(atoms[2]) if len(atoms) > 2 else "",
                    layer=layers[0] if layers else "",
                    at=(at[0], at[1]) if len(at) >= 2 else (0.0, 0.0),
                    size=size,
                    thickness=thickness,
                    hidden=_hidden(child),
                )
            )
        elif head == "property":
            atoms = _atoms(child)
            if len(atoms) < 3:
                continue
            at = _nums(sexpr.find(child, "at"))
            size, thickness = _font_of(child)
            layers = _layers(child)
            fp.properties[unquote(atoms[1])] = ParsedText(
                kind="property",
                text=unquote(atoms[2]),
                layer=layers[0] if layers else "",
                at=(at[0], at[1]) if len(at) >= 2 else (0.0, 0.0),
                size=size,
                thickness=thickness,
                hidden=_hidden(child),
            )
    return fp


# --------------------------------------------------------------------------
# Symbols
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedPin:
    number: str
    name: str
    x: float
    y: float
    angle: float
    length: float
    type: str
    shape: str
    hidden: bool
    unit: int
    name_size: float
    number_size: float

    @property
    def at(self) -> tuple[float, float]:
        return (self.x, self.y)

    @property
    def end(self) -> tuple[float, float]:
        """The far end of the pin — where the body edge is."""
        theta = math.radians(self.angle)
        return (
            self.x + self.length * math.cos(theta),
            self.y + self.length * math.sin(theta),
        )


@dataclass
class ParsedSymbol:
    name: str
    source: str = ""
    extends: str = ""
    properties: dict[str, str] = field(default_factory=dict)
    property_nodes: dict[str, ParsedText] = field(default_factory=dict)
    pins: list[ParsedPin] = field(default_factory=list)
    units: tuple[int, ...] = ()

    @property
    def pin_numbers(self) -> set[str]:
        return {p.number for p in self.pins}


@dataclass
class ParsedSymbolLib:
    source: str = ""
    version: str = ""
    generator: str = ""
    symbols: list[ParsedSymbol] = field(default_factory=list)

    @classmethod
    def from_text(cls, text: str, source: str = "") -> ParsedSymbolLib:
        try:
            root = sexpr.parse(text)
        except sexpr.SexprError as exc:
            raise ParseError(f"{source or 'input'}: {exc}") from exc
        head = root[0] if root and isinstance(root[0], str) else ""
        if head != "kicad_symbol_lib":
            raise ParseError(f"{source or 'input'}: not a symbol library")
        lib = cls(
            source=source,
            version=_str_arg(sexpr.find(root, "version")),
            generator=_str_arg(sexpr.find(root, "generator")),
        )
        for node in sexpr.find_all(root, "symbol"):
            lib.symbols.append(_read_symbol(node, source))
        return lib

    @classmethod
    def from_path(cls, path: Path) -> ParsedSymbolLib:
        return cls.from_text(Path(path).read_text(encoding="utf-8"), str(path))


def _read_pin(node: sexpr.Node, unit: int) -> ParsedPin | None:
    atoms = _atoms(node)
    at = _nums(sexpr.find(node, "at"))
    if len(at) < 2:
        return None
    length = _nums(sexpr.find(node, "length"))
    name_node = sexpr.find(node, "name")
    number_node = sexpr.find(node, "number")
    return ParsedPin(
        number=_str_arg(number_node) if number_node is not None else "",
        name=_str_arg(name_node) if name_node is not None else "",
        x=at[0],
        y=at[1],
        angle=at[2] if len(at) > 2 else 0.0,
        length=length[0] if length else 0.0,
        type=atoms[1] if len(atoms) > 1 else "",
        shape=atoms[2] if len(atoms) > 2 else "",
        hidden=_hidden(node),
        unit=unit,
        name_size=_font_of(name_node)[0] if name_node is not None else 0.0,
        number_size=_font_of(number_node)[0] if number_node is not None else 0.0,
    )


def _read_symbol(node: sexpr.Node, source: str) -> ParsedSymbol:
    name = _str_arg(node, 1)
    symbol = ParsedSymbol(
        name=name, source=source, extends=_str_arg(sexpr.find(node, "extends"))
    )
    for prop in sexpr.find_all(node, "property"):
        atoms = _atoms(prop)
        if len(atoms) < 3:
            continue
        key = unquote(atoms[1])
        size, thickness = _font_of(prop)
        at = _nums(sexpr.find(prop, "at"))
        symbol.properties[key] = unquote(atoms[2])
        symbol.property_nodes[key] = ParsedText(
            kind="property",
            text=unquote(atoms[2]),
            layer="",
            at=(at[0], at[1]) if len(at) >= 2 else (0.0, 0.0),
            size=size,
            thickness=thickness,
            hidden=_hidden(prop),
        )

    units: set[int] = set()
    for sub in sexpr.find_all(node, "symbol"):
        sub_name = _str_arg(sub, 1)
        unit, style = 1, 1
        tail = sub_name.rsplit("_", 2)
        if len(tail) == 3 and tail[1].isdigit() and tail[2].isdigit():
            unit, style = int(tail[1]), int(tail[2])
        # Style 2 is the De Morgan alternate: the same pins drawn differently.
        # Counting it would double every gate.
        if style not in (0, 1):
            continue
        for pin_node in sexpr.find_all(sub, "pin"):
            pin = _read_pin(pin_node, unit)
            if pin is not None:
                symbol.pins.append(pin)
                if unit:
                    units.add(unit)
    symbol.units = tuple(sorted(units))
    return symbol
