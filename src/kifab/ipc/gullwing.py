"""Land-pattern layout for gull-wing packages (SOIC/SOP/SOT, QFP).

`rules.py` answers "how big is one land and how far from centre"; this module
answers "where does each numbered land go". They are split because the first is
IPC arithmetic that the Phase 0b gate holds to the official libraries, while
the second is pure numbering convention.

Numbering follows the JEDEC/KiCad convention, verified against the shipped
`SOIC-8_3.9x4.9mm_P1.27mm` and `LQFP-48_7x7mm_P0.5mm`: pin 1 is the top of the
left column and numbering runs counter-clockwise when viewed from the top.
"""

from __future__ import annotations

from dataclasses import dataclass

from .rules import Density, get_class, gullwing_class, land_pattern
from .toleranced import Tol


@dataclass(frozen=True)
class LandPad:
    """One computed land: centre and size in mm, origin at package centre."""

    number: str
    x: float
    y: float
    size_x: float
    size_y: float


@dataclass(frozen=True)
class Land:
    """The computed lands of a package plus the courtyard excess IPC asks for."""

    pads: list[LandPad]
    courtyard_excess: float


def _row_offsets(count: int, pitch: float) -> list[float]:
    """Centred positions of `count` lands at `pitch`, ascending."""
    first = -(count - 1) * pitch / 2
    return [first + i * pitch for i in range(count)]


def dual(
    *,
    pin_count: int,
    pitch: float,
    lead_span: Tol,
    lead_width: Tol,
    lead_length: Tol,
    density: Density = "nominal",
) -> Land:
    """Two land columns, left and right (SOIC, SOP, TSSOP, SOT-23-N).

    `lead_span` is the outside-to-outside dimension across the two lead rows —
    the dimension datasheets call E or "lead span", not the body width.
    """
    if pin_count < 2 or pin_count % 2:
        raise ValueError(f"dual gullwing needs an even pin count >= 2, got {pin_count}")

    device = get_class(gullwing_class(pitch))
    lp = land_pattern(
        device_class=device,
        lead_outside=lead_span,
        lead_width=lead_width,
        lead_len=lead_length,
        density=density,
    )

    per_side = pin_count // 2
    ys = _row_offsets(per_side, pitch)
    pads = [
        LandPad(str(i + 1), -lp.centre, ys[i], lp.length, lp.Xmax)
        for i in range(per_side)
    ]
    pads += [
        LandPad(str(per_side + i + 1), lp.centre, ys[per_side - 1 - i], lp.length, lp.Xmax)
        for i in range(per_side)
    ]
    return Land(pads=pads, courtyard_excess=device.for_density(density).courtyard)


def quad(
    *,
    pin_count: int,
    pitch: float,
    lead_span_x: Tol,
    lead_span_y: Tol,
    lead_width: Tol,
    lead_length: Tol,
    density: Density = "nominal",
) -> Land:
    """Four land rows (QFP, LQFP, TQFP).

    `lead_span_x` spans the left/right columns; `lead_span_y` spans the
    top/bottom rows. They differ only for rectangular packages.
    """
    if pin_count < 4 or pin_count % 4:
        raise ValueError(
            f"quad gullwing needs a pin count divisible by 4, got {pin_count}"
        )

    device = get_class(gullwing_class(pitch))
    common = {
        "device_class": device,
        "lead_width": lead_width,
        "lead_len": lead_length,
        "density": density,
    }
    lp_x = land_pattern(lead_outside=lead_span_x, **common)
    lp_y = land_pattern(lead_outside=lead_span_y, **common)

    per_side = pin_count // 4
    ys = _row_offsets(per_side, pitch)
    xs = _row_offsets(per_side, pitch)

    pads: list[LandPad] = []
    number = 1

    # Left column, top to bottom.
    for i in range(per_side):
        pads.append(LandPad(str(number), -lp_x.centre, ys[i], lp_x.length, lp_x.Xmax))
        number += 1
    # Bottom row, left to right. Note +y is *down* in KiCad's footprint frame.
    for i in range(per_side):
        pads.append(LandPad(str(number), xs[i], lp_y.centre, lp_y.Xmax, lp_y.length))
        number += 1
    # Right column, bottom to top.
    for i in range(per_side):
        pads.append(
            LandPad(str(number), lp_x.centre, ys[per_side - 1 - i], lp_x.length, lp_x.Xmax)
        )
        number += 1
    # Top row, right to left.
    for i in range(per_side):
        pads.append(
            LandPad(str(number), xs[per_side - 1 - i], -lp_y.centre, lp_y.Xmax, lp_y.length)
        )
        number += 1

    return Land(pads=pads, courtyard_excess=device.for_density(density).courtyard)
