"""Package geometry: how a physical package becomes copper.

A package is one of a small set of *families*. A family knows how to produce
its lands (`resolve_pads()`); the footprint emitter knows how to draw silk, fab
and courtyard around whatever lands it is given. Adding a family therefore
never touches the emitter.

Two kinds of family exist and both are first-class:

* **computed** (`dual_gullwing`, `quad_gullwing`) — the YAML states datasheet
  dimensions and IPC-7351B produces the lands. This is what you want whenever
  the datasheet mechanical drawing is available.
* **custom** — the YAML states the lands directly. This is the escape hatch,
  and it is the representation an importer (EasyEDA, a vendor-supplied
  footprint) lands in. Without it the IR could not express every footprint,
  and an IR that cannot represent its inputs is not a contract.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    PlainValidator,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from ..ipc import gullwing
from ..ipc.toleranced import Tol
from .designator import coerce_designator, require_non_empty
from .enums import Density, MountType, PadShape, PadType


def _to_tol(value: object) -> Tol:
    """Accept a datasheet dimension in any of the forms people write it."""
    if isinstance(value, Tol):
        return value
    if isinstance(value, bool):
        raise ValueError(f"cannot read dimension {value!r}")
    if isinstance(value, (int, float, str)):
        return Tol.parse(value)
    if isinstance(value, dict):
        if {"min", "max"} <= value.keys():
            return Tol.span(float(value["min"]), float(value["max"]))
        if {"nominal", "tolerance"} <= value.keys():
            return Tol.plus_minus(float(value["nominal"]), float(value["tolerance"]))
    raise ValueError(
        f"cannot read dimension {value!r}; write it as 3.9, '3.8 .. 4.0', "
        "{min: 3.8, max: 4.0} or {nominal: 3.9, tolerance: 0.1}"
    )


Dim = Annotated[
    Tol,
    PlainValidator(_to_tol),
    PlainSerializer(lambda t: f"{t.minimum} .. {t.maximum}", return_type=str),
    WithJsonSchema({"type": "string", "examples": ["3.9", "3.8 .. 4.0"]}),
]
"""A toleranced datasheet dimension in mm.

IPC works from tolerance extremes, not nominals, so a dimension given without a
tolerance (`3.9`) is treated as *exact*. That is correct for JEDEC **basic**
dimensions and optimistic for anything else — state the range whenever the
datasheet gives one.
"""


class Pad(BaseModel):
    """One land, positioned relative to the package centre.

    KiCad's footprint frame has +x right and **+y down**, which is the opposite
    of the schematic frame. Pad 1 of a dual package is therefore at negative y.
    """

    model_config = ConfigDict(extra="forbid")

    number: str = Field(
        description="Pad number. A string, because KiCad pad numbers are not "
        "all integers (BGA 'A1', exposed pad 'EP') — but a bare YAML integer "
        "is accepted and coerced, exactly as `Pin.number` is."
    )
    at: tuple[float, float] = Field(description="Centre (x, y) in mm.")
    size: tuple[float, float] = Field(description="(width, height) in mm.")
    shape: PadShape = PadShape.ROUNDRECT
    type: PadType = PadType.SMD
    rotation: float = 0.0
    drill: float | None = Field(
        default=None, gt=0, description="Hole diameter, mm. Through-hole pads only."
    )
    layers: list[str] | None = Field(
        default=None,
        description="Override the layer set. Leave unset — the default is the "
        "canonical set for the pad type, in the order kicad-cli canonicalises to.",
    )
    roundrect_ratio: float | None = Field(
        default=None,
        gt=0,
        le=0.5,
        description="Corner radius as a fraction of the shorter side. Unset "
        "means the house rule: 25%, capped at a 0.25 mm radius.",
    )

    @field_validator("number", mode="before")
    @classmethod
    def _coerce_number(cls, value: object) -> object:
        """Allow bare integers in YAML — `number: 3` rather than `number: "3"`."""
        return coerce_designator(value)

    @field_validator("number")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        return require_non_empty(value, "pad number")

    @model_validator(mode="after")
    def _check_drill(self) -> Pad:
        needs_drill = self.type in (PadType.THRU_HOLE, PadType.NP_THRU_HOLE)
        if needs_drill and self.drill is None:
            raise ValueError(
                f"pad {self.number!r} is {self.type.value} but has no drill"
            )
        if not needs_drill and self.drill is not None:
            raise ValueError(
                f"pad {self.number!r} is {self.type.value} but has a drill"
            )
        return self


class Body(BaseModel):
    """The plastic body outline, used for silkscreen, fab layer and courtyard.

    This is the body, not the lead span — for a gull-wing package the leads
    stick out past it, and the courtyard is sized from whichever is larger.
    """

    model_config = ConfigDict(extra="forbid")

    x: float = Field(gt=0, description="Body width (across the columns), mm.")
    y: float = Field(gt=0, description="Body height (along the columns), mm.")


class Span(BaseModel):
    """Toleranced lead spans in both axes, for four-sided packages."""

    model_config = ConfigDict(extra="forbid")

    x: Dim = Field(description="Outside-to-outside across the left/right columns.")
    y: Dim = Field(description="Outside-to-outside across the top/bottom rows.")


class _PackageBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: Body

    def mount(self) -> MountType:
        return MountType.SMD

    def courtyard_excess(self) -> float:  # pragma: no cover - abstract
        raise NotImplementedError

    def resolve_pads(self) -> list[Pad]:  # pragma: no cover - abstract
        raise NotImplementedError


class CustomPackage(_PackageBase):
    """Lands stated directly. The escape hatch and the importer target."""

    family: Literal["custom"] = "custom"
    pads: list[Pad] = Field(min_length=1)
    courtyard: float = Field(
        default=0.25,
        ge=0,
        description="Courtyard excess beyond the pad/body extent, mm. 0.25 is "
        "IPC nominal density.",
    )
    mount_type: MountType = Field(
        default=MountType.SMD,
        description="What KiCad's `(attr ...)` says. Set `through_hole` when "
        "the pads are drilled.",
    )

    def mount(self) -> MountType:
        return self.mount_type

    def courtyard_excess(self) -> float:
        return self.courtyard

    def resolve_pads(self) -> list[Pad]:
        return list(self.pads)


class _Gullwing(_PackageBase):
    pin_count: int = Field(gt=0)
    pitch: float = Field(gt=0, description="Lead-to-lead pitch, mm.")
    lead_width: Dim = Field(description="Lead width (datasheet 'b'), mm.")
    lead_length: Dim = Field(
        description="Lead foot length (datasheet 'L') — the flat part that "
        "touches the board, not the total lead length."
    )
    density: Density = Density.NOMINAL


class DualGullwing(_Gullwing):
    """SOIC / SOP / TSSOP / SOT-23-N: two land columns, left and right."""

    family: Literal["dual_gullwing"] = "dual_gullwing"
    lead_span: Dim = Field(
        description="Outside-to-outside across the two lead rows (datasheet "
        "'E'), mm. Not the body width."
    )

    @model_validator(mode="after")
    def _even_pin_count(self) -> DualGullwing:
        if self.pin_count % 2:
            raise ValueError(
                f"a dual-row package needs an even pin count, got {self.pin_count}"
            )
        return self

    def _land(self) -> gullwing.Land:
        return gullwing.dual(
            pin_count=self.pin_count,
            pitch=self.pitch,
            lead_span=self.lead_span,
            lead_width=self.lead_width,
            lead_length=self.lead_length,
            density=self.density.value,
        )

    def courtyard_excess(self) -> float:
        return self._land().courtyard_excess

    def resolve_pads(self) -> list[Pad]:
        return [_from_land(p) for p in self._land().pads]


class QuadGullwing(_Gullwing):
    """QFP / LQFP / TQFP: four land rows."""

    family: Literal["quad_gullwing"] = "quad_gullwing"
    lead_span: Span

    @model_validator(mode="after")
    def _four_equal_sides(self) -> QuadGullwing:
        if self.pin_count % 4:
            raise ValueError(
                "a four-sided package needs a pin count divisible by 4, got "
                f"{self.pin_count}"
            )
        return self

    def _land(self) -> gullwing.Land:
        return gullwing.quad(
            pin_count=self.pin_count,
            pitch=self.pitch,
            lead_span_x=self.lead_span.x,
            lead_span_y=self.lead_span.y,
            lead_width=self.lead_width,
            lead_length=self.lead_length,
            density=self.density.value,
        )

    def courtyard_excess(self) -> float:
        return self._land().courtyard_excess

    def resolve_pads(self) -> list[Pad]:
        return [_from_land(p) for p in self._land().pads]


def _from_land(land: gullwing.LandPad) -> Pad:
    return Pad(
        number=land.number,
        at=(land.x, land.y),
        size=(land.size_x, land.size_y),
        shape=PadShape.ROUNDRECT,
        type=PadType.SMD,
    )


Package = Annotated[
    CustomPackage | DualGullwing | QuadGullwing,
    Field(discriminator="family"),
]
