"""Land-pattern layout for flat no-lead packages (DFN, QFN, and pull-back
variants such as many WSON/UQFN parts).

The arithmetic is the *same* three IPC-7351B equations `rules.py` already owns;
what changes is where the inputs come from. A no-lead terminal is flush with
the body, so there is no lead span to measure:

    lead_outside = body size              (pull-back packages: body - 2*pull_back)
    lead_inside  = lead_outside - 2*L     (L = the terminal length, datasheet 'L')

That is exactly what kilibs' `ipc_body_edge_inside` / `_pull_back` do, and it is
why `Package_DFN_QFN` geometry falls out of the tables we already vendored while
two-terminal chip geometry does not (see DECISIONS.md).

Numbering follows the same JEDEC/KiCad convention as `gullwing.py`: pin 1 is the
top of the left column and numbering runs counter-clockwise viewed from the top.
The exposed pad, when present, is the highest number.
"""

from __future__ import annotations

from dataclasses import dataclass

from .gullwing import Land, LandPad, _row_offsets
from .rules import Density, get_class, land_pattern
from .toleranced import Tol

#: `ipc_generic_rules.min_ep_to_pad_clearance` from the vendored tables.
MIN_EP_TO_PAD_CLEARANCE = 0.2


@dataclass(frozen=True)
class ExposedPad:
    """The thermal/ground pad under the body."""

    size_x: float
    size_y: float
    number: str = ""


def no_lead_class(pull_back: Tol | None) -> str:
    """Pull-back terminals get their own IPC fillet goals (table 3-18)."""
    return (
        "ipc_spec_flat_no_lead_pull_back"
        if pull_back is not None
        else "ipc_spec_flat_no_lead"
    )


def _outside(body: Tol, pull_back: Tol | None) -> Tol:
    return body if pull_back is None else body - pull_back * 2


def _pattern(
    *,
    body: Tol,
    pull_back: Tol | None,
    lead_width: Tol,
    lead_length: Tol,
    density: Density,
):
    device = get_class(no_lead_class(pull_back))
    outside = _outside(body, pull_back)
    return device, land_pattern(
        device_class=device,
        lead_outside=outside,
        lead_width=lead_width,
        lead_len=lead_length,
        density=density,
    )


def _exposed(ep: ExposedPad | None, next_number: int) -> list[LandPad]:
    if ep is None:
        return []
    number = ep.number or str(next_number)
    return [LandPad(number, 0.0, 0.0, ep.size_x, ep.size_y)]


def check_ep_clearance(pads: list[LandPad], ep_number: str) -> float | None:
    """Smallest gap from the exposed pad's edge to any perimeter land.

    Returns None when there is no exposed pad. IPC's generic rules put the
    floor at 0.2 mm; below that the real generator shrinks the lands (heel
    reduction) rather than shipping the overlap, so refusing is the honest
    answer until that is implemented.
    """
    ep = next((p for p in pads if p.number == ep_number), None)
    if ep is None:
        return None
    gap = float("inf")
    for pad in pads:
        if pad.number == ep_number:
            continue
        dx = abs(pad.x - ep.x) - (pad.size_x + ep.size_x) / 2
        dy = abs(pad.y - ep.y) - (pad.size_y + ep.size_y) / 2
        # Boxes are axis-aligned: the separation is the larger of the two
        # axis gaps (negative on both axes means they overlap).
        gap = min(gap, max(dx, dy))
    return gap


def dual(
    *,
    pin_count: int,
    pitch: float,
    body_x: Tol,
    lead_width: Tol,
    lead_length: Tol,
    pull_back: Tol | None = None,
    exposed_pad: ExposedPad | None = None,
    density: Density = "nominal",
) -> Land:
    """Two land columns, left and right (DFN, SON, WSON).

    `body_x` is the body dimension **across** the two terminal columns; the
    terminals are flush with it, which is what makes it the lead span.
    """
    if pin_count < 2 or pin_count % 2:
        raise ValueError(f"dual no-lead needs an even pin count >= 2, got {pin_count}")

    device, lp = _pattern(
        body=body_x,
        pull_back=pull_back,
        lead_width=lead_width,
        lead_length=lead_length,
        density=density,
    )

    per_side = pin_count // 2
    ys = _row_offsets(per_side, pitch)
    pads = [
        LandPad(str(i + 1), -lp.centre, ys[i], lp.length, lp.Xmax)
        for i in range(per_side)
    ]
    pads += [
        LandPad(
            str(per_side + i + 1),
            lp.centre,
            ys[per_side - 1 - i],
            lp.length,
            lp.Xmax,
        )
        for i in range(per_side)
    ]
    pads += _exposed(exposed_pad, pin_count + 1)
    return Land(pads=pads, courtyard_excess=device.for_density(density).courtyard)


def quad(
    *,
    pins_x: int,
    pins_y: int,
    pitch: float,
    body_x: Tol,
    body_y: Tol,
    lead_width: Tol,
    lead_length: Tol,
    pull_back: Tol | None = None,
    exposed_pad: ExposedPad | None = None,
    density: Density = "nominal",
) -> Land:
    """Four land rows (QFN, VQFN, UQFN).

    `pins_y` is the count in each of the left/right **columns**, `pins_x` the
    count in each of the top/bottom **rows**. They differ for a rectangular
    package, which is precisely the case a square-only generator gets wrong.
    """
    if pins_x < 1 or pins_y < 1:
        raise ValueError(
            f"quad no-lead needs at least one land per side, got "
            f"pins_x={pins_x}, pins_y={pins_y}"
        )

    common = dict(
        pull_back=pull_back,
        lead_width=lead_width,
        lead_length=lead_length,
        density=density,
    )
    device, lp_x = _pattern(body=body_x, **common)
    _, lp_y = _pattern(body=body_y, **common)

    ys = _row_offsets(pins_y, pitch)
    xs = _row_offsets(pins_x, pitch)

    pads: list[LandPad] = []
    number = 1
    # Left column, top to bottom.
    for i in range(pins_y):
        pads.append(LandPad(str(number), -lp_x.centre, ys[i], lp_x.length, lp_x.Xmax))
        number += 1
    # Bottom row, left to right. +y is *down* in KiCad's footprint frame.
    for i in range(pins_x):
        pads.append(LandPad(str(number), xs[i], lp_y.centre, lp_y.Xmax, lp_y.length))
        number += 1
    # Right column, bottom to top.
    for i in range(pins_y):
        pads.append(
            LandPad(
                str(number), lp_x.centre, ys[pins_y - 1 - i], lp_x.length, lp_x.Xmax
            )
        )
        number += 1
    # Top row, right to left.
    for i in range(pins_x):
        pads.append(
            LandPad(
                str(number), xs[pins_x - 1 - i], -lp_y.centre, lp_y.Xmax, lp_y.length
            )
        )
        number += 1

    pads += _exposed(exposed_pad, number)
    return Land(pads=pads, courtyard_excess=device.for_density(density).courtyard)
