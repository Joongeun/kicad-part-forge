"""Closed vocabularies used across the IR.

Every value here is a token KiCad itself writes, spelled exactly as it appears
on disk, so the emitters never have to translate. Where a name is ours rather
than KiCad's (`Side`, `MountType`) it is called out in the docstring.
"""

from __future__ import annotations

from enum import Enum


class ElectricalType(str, Enum):
    """KiCad's pin electrical type — what ERC will allow this pin to connect to.

    Getting this wrong is the most common cause of a symbol that "works" but
    produces meaningless ERC output, so it is a required field on every pin.
    """

    INPUT = "input"
    OUTPUT = "output"
    BIDIRECTIONAL = "bidirectional"
    TRI_STATE = "tri_state"
    PASSIVE = "passive"
    FREE = "free"
    UNSPECIFIED = "unspecified"
    POWER_IN = "power_in"
    POWER_OUT = "power_out"
    OPEN_COLLECTOR = "open_collector"
    OPEN_EMITTER = "open_emitter"
    NO_CONNECT = "no_connect"


class PinShape(str, Enum):
    """KiCad's pin graphic style (the decoration drawn at the body edge)."""

    LINE = "line"
    INVERTED = "inverted"
    CLOCK = "clock"
    INVERTED_CLOCK = "inverted_clock"
    INPUT_LOW = "input_low"
    CLOCK_LOW = "clock_low"
    OUTPUT_LOW = "output_low"
    EDGE_CLOCK_HIGH = "edge_clock_high"
    NON_LOGIC = "non_logic"


class Side(str, Enum):
    """Which edge of the symbol body a pin sits on. Ours, not KiCad's.

    KiCad stores an absolute (x, y, rotation); we store the side and let the
    emitter compute the coordinates. That is what keeps every pin on the grid
    by construction — an off-grid pin is not expressible in this IR.
    """

    LEFT = "left"
    RIGHT = "right"
    TOP = "top"
    BOTTOM = "bottom"


class PadShape(str, Enum):
    RECT = "rect"
    ROUNDRECT = "roundrect"
    CIRCLE = "circle"
    OVAL = "oval"


class PadType(str, Enum):
    SMD = "smd"
    THRU_HOLE = "thru_hole"
    NP_THRU_HOLE = "np_thru_hole"


class MountType(str, Enum):
    """Maps to KiCad's footprint `(attr ...)` token. Ours, not KiCad's.

    `OTHER` covers the KiCad "unspecified / other" case, e.g. mechanical
    footprints with no electrical pads.
    """

    SMD = "smd"
    THROUGH_HOLE = "through_hole"
    OTHER = "other"


class Density(str, Enum):
    """IPC-7351B density level.

    `NOMINAL` (IPC level B) is the general-purpose default and is what the
    official KiCad libraries are generated at. `LEAST` (level C) is the
    high-density / space-constrained variant; `MOST` (level A) is the
    hand-solder / high-reliability variant with larger fillets.
    """

    LEAST = "least"
    NOMINAL = "nominal"
    MOST = "most"
