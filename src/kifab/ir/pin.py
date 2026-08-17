"""A single schematic pin."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .enums import ElectricalType, PinShape, Side


class Pin(BaseModel):
    """One pin of the symbol.

    Note what is *not* here: an (x, y) coordinate. A pin declares which side of
    the body it lives on and, optionally, which slot along that side. The
    emitter turns that into coordinates using the house grid, so:

    * every pin is on the grid by construction — off-grid is inexpressible;
    * changing the house style (pin length, grid, body margins) is a one-line
      change in exactly one place rather than a re-layout of every part;
    * the YAML stays reviewable — a pin table, not a coordinate dump.
    """

    model_config = ConfigDict(extra="forbid")

    number: str = Field(
        description="Pad number this pin bonds to. A string, because KiCad pad "
        "numbers are not always integers (BGA 'A1', exposed pad 'EP')."
    )
    name: str = Field(
        default="~",
        description="Displayed pin name. '~' is KiCad's 'no name' marker and is "
        "the correct value for a symbol whose pins are unnamed (a resistor).",
    )
    type: ElectricalType = Field(
        default=ElectricalType.PASSIVE,
        description="Electrical type — what ERC lets this pin connect to.",
    )
    shape: PinShape = Field(
        default=PinShape.LINE, description="Graphic style drawn at the body edge."
    )
    side: Side = Field(
        default=Side.LEFT, description="Which edge of the body the pin sits on."
    )
    slot: int | None = Field(
        default=None,
        ge=0,
        description="0-based position along that side, counting top-to-bottom "
        "for left/right and left-to-right for top/bottom. Leave unset to have "
        "pins fill consecutively in declaration order; set it to leave a "
        "deliberate gap between functional groups.",
    )
    unit: int = Field(
        default=1,
        ge=1,
        description="Which gate/unit of a multi-unit symbol this pin belongs to "
        "(a dual op-amp has units 1 and 2). Units are laid out independently.",
    )
    length: float | None = Field(
        default=None,
        gt=0,
        description="Pin length in mm, overriding the house default. Only needed "
        "for pins that must reach past a longer neighbour's name.",
    )
    hidden: bool = Field(
        default=False,
        description="Emit the pin but hide it. Reserve this for pins that are "
        "genuinely invisible in the schematic; hidden power pins are a legacy "
        "practice KLC discourages.",
    )

    @field_validator("number", mode="before")
    @classmethod
    def _coerce_number(cls, value: object) -> object:
        """Allow bare integers in YAML — `number: 3` rather than `number: "3"`."""
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(int(value))
        return value

    @field_validator("number")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("pin number must not be empty")
        return value
