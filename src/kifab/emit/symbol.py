"""Emit `.kicad_sym` (KiCad 9, format version 20241209).

Layout policy lives here, not in the IR. A pin says "left side, slot 2"; this
module turns that into millimetres using the house grid. Changing the house
style therefore re-lays-out every part in the corpus from one place, which is
the whole reason the IR stores sides and slots instead of coordinates.

Canonical form was established by feeding a minimal hand-written library to
`kicad-cli sym upgrade --force` and diffing what came back. What it requires:

* `(generator_version "9.0")` alongside `(generator ...)`;
* per symbol, `(exclude_from_sim ...) (in_bom ...) (on_board ...)` before the
  properties, and all five of Reference / Value / Footprint / Datasheet /
  Description present even when empty;
* `(effects (font (size ...)))` on every pin name and number;
* `(embedded_fonts no)` closing each symbol.

Symbols carry **no** UUIDs — unlike footprints. That is KiCad's rule, not an
omission; `sym upgrade` adds none.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..ir import Part, Pin, Side, SymbolSpec, SymbolStyle
from . import sexpr
from .sexpr import Node, fmt_num, quote

SYMBOL_VERSION = "20241209"
GENERATOR = "kifab"
GENERATOR_VERSION = "9.0"

# Angle of the pin *body* measured from its connection point: a pin on the left
# edge extends rightwards into the symbol, so it is drawn at 0 degrees. Verified
# against the shipped Regulator_Linear and Memory_EEPROM libraries.
_SIDE_ANGLE = {Side.LEFT: 0, Side.RIGHT: 180, Side.TOP: 270, Side.BOTTOM: 90}


@dataclass(frozen=True)
class Placed:
    """A pin with its computed connection point, in mm."""

    pin: Pin
    x: float
    y: float
    angle: int


@dataclass(frozen=True)
class UnitLayout:
    width: float
    height: float
    pins: list[Placed]

    @property
    def outer_top(self) -> float:
        return max([self.height / 2] + [p.y for p in self.pins])

    @property
    def outer_bottom(self) -> float:
        return min([-self.height / 2] + [p.y for p in self.pins])


def _round_up(value: float, step: float) -> float:
    """Smallest multiple of `step` that is >= value (tolerant of float noise)."""
    return math.ceil(value / step - 1e-9) * step


def _assign_slots(pins: list[Pin]) -> dict[str, int]:
    """Resolve every pin's slot on its side.

    Pins with an explicit slot keep it; the rest fill the lowest free slots in
    declaration order. That is what lets a YAML author leave a gap between
    functional groups by pinning just one pin's slot.
    """
    resolved: dict[str, int] = {}
    for side in Side:
        on_side = [p for p in pins if p.side is side]
        taken = {p.slot for p in on_side if p.slot is not None}
        cursor = 0
        for pin in on_side:
            if pin.slot is not None:
                resolved[pin.number] = pin.slot
                continue
            while cursor in taken:
                cursor += 1
            resolved[pin.number] = cursor
            taken.add(cursor)
    return resolved


def _name_extent(pins: list[Pin], style: SymbolStyle) -> float:
    """Width the longest visible pin name needs, in mm."""
    if style.hide_pin_names:
        return 0.0
    lengths = [len(p.name) for p in pins if p.name not in ("", "~")]
    return max(lengths, default=0) * style.char_width


def layout_unit(pins: list[Pin], style: SymbolStyle) -> UnitLayout:
    """Place one unit's pins on the house grid and size its body.

    Two invariants drive every number here:

    1. **Every pin sits on a whole grid step from the origin.** KLC requires it
       and wiring depends on it. That is why the body's *half* size is snapped
       to the grid rather than the full size, and why a side with an even
       number of pins ends up a half-step off-centre rather than off-grid — a
       cosmetic asymmetry is worth an unwirable pin every time.
    2. **The body is wide enough that opposing pin names cannot collide**, using
       a deliberately generous per-character width.
    """
    slots = _assign_slots(pins)
    grid = style.grid

    by_side = {side: [p for p in pins if p.side is side] for side in Side}
    count = {
        side: max((slots[p.number] for p in ps), default=-1) + 1
        for side, ps in by_side.items()
    }

    rows = max(count[Side.LEFT], count[Side.RIGHT])
    cols = max(count[Side.TOP], count[Side.BOTTOM])

    def y_of(slot: int) -> float:
        """Rows run top to bottom, centred on the origin and snapped to grid."""
        return round(((rows - 1) // 2) * grid - slot * grid, 6)

    def x_of(slot: int) -> float:
        return round(-((cols - 1) // 2) * grid + slot * grid, 6)

    # Half-extent the pin rows themselves demand, plus one grid of margin so a
    # pin never starts exactly on a corner.
    span_y = max((abs(y_of(s)) for s in range(rows)), default=0.0) + grid
    span_x = max((abs(x_of(s)) for s in range(cols)), default=0.0) + grid

    # Opposing pin names must not meet in the middle. `char_width` is a
    # deliberate over-estimate of KiCad's stroke font, so this errs wide.
    name_width = (
        _name_extent(by_side[Side.LEFT], style)
        + _name_extent(by_side[Side.RIGHT], style)
        + 2 * style.name_offset
        + grid
        if rows > 0
        else 0.0
    )
    name_height = (
        _name_extent(by_side[Side.TOP], style)
        + _name_extent(by_side[Side.BOTTOM], style)
        + 2 * style.name_offset
        + grid
        if cols > 0
        else 0.0
    )

    half_w = _round_up(max(style.min_body_width / 2, span_x, name_width / 2), grid)
    half_h = _round_up(max(style.min_body_height / 2, span_y, name_height / 2), grid)
    width = style.body_width or 2 * half_w
    height = style.body_height or 2 * half_h

    placed: list[Placed] = []
    for pin in pins:
        slot = slots[pin.number]
        length = pin.length if pin.length is not None else style.pin_length
        if pin.side is Side.LEFT:
            point = (round(-(width / 2 + length), 6), y_of(slot))
        elif pin.side is Side.RIGHT:
            point = (round(width / 2 + length, 6), y_of(slot))
        elif pin.side is Side.TOP:
            point = (x_of(slot), round(height / 2 + length, 6))
        else:
            point = (x_of(slot), round(-(height / 2 + length), 6))
        placed.append(Placed(pin, point[0], point[1], _SIDE_ANGLE[pin.side]))

    return UnitLayout(width=round(width, 6), height=round(height, 6), pins=placed)


# ---------------------------------------------------------------------------
# S-expression construction
# ---------------------------------------------------------------------------


def _font(size: float) -> Node:
    return ["font", ["size", fmt_num(size), fmt_num(size)]]


def _effects(size: float, *, hide: bool = False, justify: str | None = None) -> Node:
    node: Node = ["effects", _font(size)]
    if justify:
        node.append(["justify", justify])
    if hide:
        node.append(["hide", "yes"])
    return node


def _property(
    name: str,
    value: str,
    *,
    at: tuple[float, float, float],
    size: float,
    hide: bool,
) -> Node:
    return [
        "property",
        quote(name),
        quote(value),
        ["at", fmt_num(at[0]), fmt_num(at[1]), fmt_num(at[2])],
        _effects(size, hide=hide),
    ]


def _pin(placed: Placed, style: SymbolStyle) -> Node:
    pin = placed.pin
    length = pin.length if pin.length is not None else style.pin_length
    node: Node = [
        "pin",
        pin.type.value,
        pin.shape.value,
        ["at", fmt_num(placed.x), fmt_num(placed.y), fmt_num(placed.angle)],
        ["length", fmt_num(length)],
    ]
    if pin.hidden:
        node.append(["hide", "yes"])
    node.append(["name", quote(pin.name), _effects(style.text_size)])
    node.append(["number", quote(pin.number), _effects(style.text_size)])
    return node


def _pin_order(pins: list[Placed]) -> list[Placed]:
    """Left to right, then top to bottom.

    This is the order `kicad-cli sym upgrade` writes pins back in. Emitting it
    directly makes the upgrade a no-op on our output, so a symbol edited in
    KiCad and a symbol regenerated from the IR diff only where they really
    differ. It also means declaration order in the YAML is free to follow the
    datasheet's pin table without changing a byte of output.
    """
    return sorted(pins, key=lambda p: (p.x, -p.y))


def _body(layout: UnitLayout, style: SymbolStyle) -> Node:
    half_w, half_h = layout.width / 2, layout.height / 2
    return [
        "rectangle",
        ["start", fmt_num(-half_w), fmt_num(half_h)],
        ["end", fmt_num(half_w), fmt_num(-half_h)],
        ["stroke", ["width", fmt_num(style.body_stroke)], ["type", "default"]],
        ["fill", ["type", style.fill]],
    ]


def _yesno(flag: bool) -> str:
    return "yes" if flag else "no"


def symbol_node(part: Part) -> Node:
    """Build the `(symbol ...)` node for one part."""
    spec: SymbolSpec = part.symbol
    style = spec.style
    name = part.symbol_name

    layouts = {unit: layout_unit(spec.pins_for(unit), style) for unit in spec.units}
    top = max(l.outer_top for l in layouts.values())
    bottom = min(l.outer_bottom for l in layouts.values())

    node: Node = ["symbol", quote(name)]
    if style.hide_pin_numbers:
        node.append(["pin_numbers", ["hide", "yes"]])
    node.append(
        ["pin_names", ["offset", fmt_num(style.name_offset)]]
        + ([["hide", "yes"]] if style.hide_pin_names else [])
    )
    node.append(["exclude_from_sim", _yesno(spec.exclude_from_sim)])
    node.append(["in_bom", _yesno(spec.in_bom)])
    node.append(["on_board", _yesno(spec.on_board)])

    text = style.text_size
    node.append(
        _property(
            "Reference",
            part.reference,
            at=(0, round(top + text, 6), 0),
            size=text,
            hide=False,
        )
    )
    node.append(
        _property(
            "Value",
            part.display_value,
            at=(0, round(bottom - text, 6), 0),
            size=text,
            hide=False,
        )
    )
    for prop_name, prop_value in (
        ("Footprint", part.footprint_id),
        ("Datasheet", part.datasheet),
        ("Description", part.description),
    ):
        node.append(
            _property(prop_name, prop_value, at=(0, 0, 0), size=text, hide=True)
        )
    # User properties before KiCad's `ki_*` ones — the order `sym upgrade`
    # writes them back in, so a round-trip through KiCad is a no-op here.
    if part.manufacturer:
        node.append(
            _property(
                "Manufacturer", part.manufacturer, at=(0, 0, 0), size=text, hide=True
            )
        )
    node.append(_property("MPN", part.mpn, at=(0, 0, 0), size=text, hide=True))
    if spec.keywords:
        node.append(
            _property("ki_keywords", spec.keywords, at=(0, 0, 0), size=text, hide=True)
        )
    if spec.fp_filters:
        node.append(
            _property(
                "ki_fp_filters",
                " ".join(spec.fp_filters),
                at=(0, 0, 0),
                size=text,
                hide=True,
            )
        )

    if len(spec.units) == 1:
        # `_0_1` is the "common to every unit" sub-symbol. With one unit the
        # official libraries still put the body there, so we match them.
        layout = layouts[1]
        node.append(["symbol", quote(f"{name}_0_1"), _body(layout, style)])
        unit_node: Node = ["symbol", quote(f"{name}_1_1")]
        unit_node += [_pin(p, style) for p in _pin_order(layout.pins)]
        node.append(unit_node)
    else:
        for unit, layout in layouts.items():
            unit_node = ["symbol", quote(f"{name}_{unit}_1"), _body(layout, style)]
            unit_node += [_pin(p, style) for p in _pin_order(layout.pins)]
            node.append(unit_node)

    node.append(["embedded_fonts", "no"])
    return node


def library_node(parts: list[Part]) -> Node:
    """Build a whole `(kicad_symbol_lib ...)` node."""
    node: Node = [
        "kicad_symbol_lib",
        ["version", SYMBOL_VERSION],
        ["generator", quote(GENERATOR)],
        ["generator_version", quote(GENERATOR_VERSION)],
    ]
    # Sorted by symbol name so the file is a pure function of the part set, not
    # of the order the CLI happened to read the YAML files in.
    node += [symbol_node(part) for part in sorted(parts, key=lambda p: p.symbol_name)]
    return node


def render_library(parts: list[Part]) -> str:
    """Render a `.kicad_sym` library containing every given part."""
    return sexpr.dumps(library_node(parts))
