"""The schematic-symbol half of the IR."""

from __future__ import annotations

from collections import Counter

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .enums import Side
from .pin import Pin

# The 100 mil (2.54 mm) schematic grid. Everything in a KiCad schematic snaps
# to it; a pin that is off it cannot be wired to reliably.
SCHEMATIC_GRID = 2.54


class SymbolStyle(BaseModel):
    """House style for symbol drawing.

    Defaults reproduce the conventions of the official KiCad libraries, so a
    kifab symbol dropped next to an official one looks like it belongs.
    """

    model_config = ConfigDict(extra="forbid")

    grid: float = Field(
        default=SCHEMATIC_GRID, gt=0, description="Pin pitch and body-margin unit, mm."
    )
    pin_length: float = Field(default=2.54, gt=0, description="Default pin length, mm.")
    name_offset: float = Field(
        default=1.016,
        ge=0,
        description="Gap from the body edge to the start of the pin name, mm. "
        "1.016 is by far the most common value in the official libraries.",
    )
    text_size: float = Field(default=1.27, gt=0, description="Font size, mm.")
    body_stroke: float = Field(
        default=0.254, gt=0, description="Body outline width, mm."
    )
    fill: str = Field(
        default="background",
        pattern="^(none|outline|background)$",
        description="Body fill. 'background' is the official style for ICs.",
    )
    min_body_width: float = Field(default=5.08, gt=0)
    min_body_height: float = Field(default=5.08, gt=0)
    body_width: float | None = Field(
        default=None,
        gt=0,
        description="Force the body width in mm instead of computing it from "
        "pin names. Use when you want several related parts to draw "
        "identically. Escape hatch: the auto-sized value is always an even "
        "multiple of the grid, which is what keeps top and bottom pins on the "
        "grid — an override that is not puts them off it.",
    )
    body_height: float | None = Field(
        default=None,
        gt=0,
        description="Force the body height in mm. Same escape-hatch caveat as "
        "`body_width`, for left and right pins.",
    )
    char_width: float = Field(
        default=0.85,
        gt=0,
        description="Assumed advance per character at `text_size`, mm. KiCad's "
        "stroke font is narrower than this; the over-estimate is deliberate so "
        "auto-sized bodies never let opposing pin names collide.",
    )
    hide_pin_numbers: bool = False
    hide_pin_names: bool = False


class SymbolSpec(BaseModel):
    """Everything needed to draw the symbol, minus the part's identity fields.

    Identity (reference designator, value, datasheet, description) lives on
    `Part` because the footprint needs the same values; duplicating it here
    would let the two halves of a part disagree.
    """

    model_config = ConfigDict(extra="forbid")

    pins: list[Pin] = Field(min_length=1)
    style: SymbolStyle = Field(default_factory=SymbolStyle)
    in_bom: bool = True
    on_board: bool = True
    exclude_from_sim: bool = False
    keywords: str = Field(
        default="",
        description="Space-separated search keywords; emitted as KiCad's "
        "`ki_keywords` property. Drives symbol-chooser search.",
    )
    fp_filters: list[str] = Field(
        default_factory=list,
        description="Footprint-name wildcards KiCad offers when assigning a "
        "footprint to this symbol; emitted as `ki_fp_filters`.",
    )

    @property
    def units(self) -> list[int]:
        """Unit numbers present, ascending. `[1]` for an ordinary symbol."""
        return sorted({pin.unit for pin in self.pins})

    def pins_for(self, unit: int) -> list[Pin]:
        return [pin for pin in self.pins if pin.unit == unit]

    @model_validator(mode="after")
    def _check_pins(self) -> SymbolSpec:
        duplicates = [
            number
            for number, count in Counter(pin.number for pin in self.pins).items()
            if count > 1
        ]
        if duplicates:
            raise ValueError(
                f"duplicate pin numbers: {sorted(duplicates)} — each pad may be "
                "referenced by exactly one pin"
            )

        # Explicit slots must not collide within a (unit, side).
        seen: dict[tuple[int, Side], set[int]] = {}
        for pin in self.pins:
            if pin.slot is None:
                continue
            key = (pin.unit, pin.side)
            slots = seen.setdefault(key, set())
            if pin.slot in slots:
                raise ValueError(
                    f"unit {pin.unit} side {pin.side.value}: two pins both claim "
                    f"slot {pin.slot} (pin {pin.number!r} is the second)"
                )
            slots.add(pin.slot)

        # Units must be contiguous from 1; KiCad numbers units densely and a
        # gap silently produces an empty gate in the schematic editor.
        units = self.units
        if units != list(range(1, len(units) + 1)):
            raise ValueError(
                f"units must run 1..N with no gaps; got {units}"
            )
        return self
