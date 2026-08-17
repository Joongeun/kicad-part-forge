"""Package geometry: how a physical package becomes copper.

A package is one of a small set of *families*. A family knows how to produce
its lands (`resolve_pads()`); the footprint emitter knows how to draw silk, fab
and courtyard around whatever lands it is given. Adding a family therefore
never touches the emitter.

Two kinds of family exist and both are first-class:

* **computed** (`dual_gullwing`, `quad_gullwing`, `dual_no_lead`,
  `quad_no_lead`) — the YAML states datasheet dimensions and IPC-7351B produces
  the lands. This is what you want whenever the datasheet mechanical drawing is
  available. Gull-wing families need the **lead span** (the leads stick out past
  the body); no-lead families need the **body**, because the terminals are flush
  with it. Passing one where the other belongs is the mistake that moves every
  land by the lead length, so the field names say which.
* **custom** — the YAML states the lands directly. This is the escape hatch,
  and it is the representation an importer (EasyEDA, a vendor-supplied
  footprint) lands in. Without it the IR could not express every footprint,
  and an IR that cannot represent its inputs is not a contract.
"""

from __future__ import annotations

import math
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

from ..ipc import gullwing, no_lead
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
    aperture: bool = Field(
        default=False,
        description="This is a stencil aperture (a paste sub-pad under an "
        "exposed pad), not a land. It is emitted with an *empty* pad number, "
        "which is what KiCad's canonical form requires, and it carries no "
        "netlist connection — so it is exempt from the pin/pad bonding rule. "
        "`number` still names the land it belongs to, for review and for the "
        "derived UUID.",
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


class ExposedPadSpec(BaseModel):
    """The thermal pad under a no-lead package.

    Stated, never guessed: a package with an exposed pad the IR does not
    mention emits a footprint that will not solder down, and one with an
    invented size shorts to the perimeter lands.
    """

    model_config = ConfigDict(extra="forbid")

    size_x: float = Field(gt=0, description="Exposed-pad width, mm.")
    size_y: float = Field(gt=0, description="Exposed-pad height, mm.")
    number: str = Field(
        default="",
        description="Pad number. Empty means the next number after the "
        "perimeter lands, which is what KiCad's own no-lead footprints do.",
    )
    paste_pads: tuple[int, int] | None = Field(
        default=None,
        description="Subdivide the solder-paste aperture into this many "
        "columns x rows. Unset means one aperture covering the whole pad, "
        "which floats large exposed pads — state a grid for anything much "
        "over 2 mm, as KiCad's own no-lead footprints do (KLC F5.2).",
    )
    paste_coverage: float = Field(
        default=0.65,
        gt=0,
        le=1,
        description="Fraction of the exposed pad's area covered by paste. "
        "Only used when `paste_pads` is set. 0.65 is the usual default; the "
        "datasheet overrides it whenever it states one.",
    )

    @field_validator("number", mode="before")
    @classmethod
    def _coerce_number(cls, value: object) -> object:
        return coerce_designator(value)

    @field_validator("paste_pads")
    @classmethod
    def _positive_grid(cls, value: tuple[int, int] | None) -> tuple[int, int] | None:
        if value is not None and (value[0] < 1 or value[1] < 1):
            raise ValueError(f"paste_pads must be at least 1x1, got {value}")
        return value

    def paste_apertures(self, number: str) -> list[Pad]:
        """The paste sub-apertures, as pads sharing the copper pad's number."""
        if self.paste_pads is None:
            return []
        nx, ny = self.paste_pads
        # Area scales with the square of the linear shrink, so a coverage
        # fraction becomes a linear factor of sqrt(coverage).
        factor = math.sqrt(self.paste_coverage)
        cell_x, cell_y = self.size_x / nx, self.size_y / ny
        size = (round(cell_x * factor, 6), round(cell_y * factor, 6))
        first_x = -(nx - 1) * cell_x / 2
        first_y = -(ny - 1) * cell_y / 2
        return [
            Pad(
                number=number,
                at=(round(first_x + i * cell_x, 6), round(first_y + j * cell_y, 6)),
                size=size,
                shape=PadShape.ROUNDRECT,
                type=PadType.SMD,
                layers=["F.Paste"],
                aperture=True,
            )
            for j in range(ny)
            for i in range(nx)
        ]


class _NoLead(_PackageBase):
    """Shared shape of DFN/QFN: terminals flush with (or pulled back from) the body.

    `body` is the *nominal* body, which draws silk, fab and courtyard.
    `body_tolerance` is what IPC actually needs — the land pattern is computed
    from tolerance extremes, and a package whose tolerance is left at 0 is
    treated as exact. State it whenever the datasheet gives it.
    """

    pitch: float = Field(gt=0, description="Terminal-to-terminal pitch, mm.")
    lead_width: Dim = Field(description="Terminal width (datasheet 'b'), mm.")
    lead_length: Dim = Field(
        description="Terminal length (datasheet 'L') — how far the land "
        "extends in from the body edge."
    )
    body_tolerance: float = Field(
        default=0.0,
        ge=0,
        description="Plus/minus tolerance on both body dimensions, mm. "
        "Applied to `body.x` and `body.y` to make the toleranced lead span.",
    )
    pull_back: Dim | None = Field(
        default=None,
        description="Distance the terminals are set back from the body edge, "
        "mm. Set it only when the drawing shows a pull-back; it switches the "
        "IPC fillet goals to table 3-18.",
    )
    exposed_pad: ExposedPadSpec | None = None
    density: Density = Density.NOMINAL

    def _body_dim(self, value: float) -> Tol:
        if self.body_tolerance:
            return Tol.plus_minus(value, self.body_tolerance)
        return Tol.exact(value)

    def _ep(self) -> no_lead.ExposedPad | None:
        if self.exposed_pad is None:
            return None
        return no_lead.ExposedPad(
            size_x=self.exposed_pad.size_x,
            size_y=self.exposed_pad.size_y,
            number=self.exposed_pad.number,
        )

    def _land(self) -> no_lead.Land:  # pragma: no cover - abstract
        raise NotImplementedError

    def courtyard_excess(self) -> float:
        return self._land().courtyard_excess

    def resolve_pads(self) -> list[Pad]:
        pads = [_from_land(p) for p in self._land().pads]
        if self.exposed_pad is None or self.exposed_pad.paste_pads is None:
            return pads
        # The copper pad keeps mask but hands its paste to the sub-apertures.
        copper = pads[-1]
        pads[-1] = copper.model_copy(update={"layers": ["F.Cu", "F.Mask"]})
        return pads + self.exposed_pad.paste_apertures(copper.number)

    @model_validator(mode="after")
    def _exposed_pad_clears_the_lands(self) -> _NoLead:
        if self.exposed_pad is None:
            return self
        land = self._land()
        number = land.pads[-1].number
        gap = no_lead.check_ep_clearance(land.pads, number)
        if gap is not None and gap < no_lead.MIN_EP_TO_PAD_CLEARANCE - 1e-9:
            raise ValueError(
                f"exposed pad {self.exposed_pad.size_x}x{self.exposed_pad.size_y} mm "
                f"leaves only {gap:.3f} mm to the nearest land; IPC's floor is "
                f"{no_lead.MIN_EP_TO_PAD_CLEARANCE} mm. Check the exposed-pad "
                "dimensions and the terminal length against the drawing — "
                "kifab does not silently shrink lands to make a part fit."
            )
        return self


class DualNoLead(_NoLead):
    """DFN / SON / WSON: two land columns, left and right."""

    family: Literal["dual_no_lead"] = "dual_no_lead"
    pin_count: int = Field(gt=0)

    @model_validator(mode="after")
    def _even_pin_count(self) -> DualNoLead:
        if self.pin_count % 2:
            raise ValueError(
                f"a dual-row package needs an even pin count, got {self.pin_count}"
            )
        return self

    def _land(self) -> no_lead.Land:
        return no_lead.dual(
            pin_count=self.pin_count,
            pitch=self.pitch,
            body_x=self._body_dim(self.body.x),
            lead_width=self.lead_width,
            lead_length=self.lead_length,
            pull_back=self.pull_back,
            exposed_pad=self._ep(),
            density=self.density.value,
        )


class QuadNoLead(_NoLead):
    """QFN / VQFN / UQFN: four land rows, sides sized independently.

    `pins_x` and `pins_y` are stated separately rather than derived from a
    total, because a rectangular QFN (a 12-lead 2x3 mm part has 3 lands on the
    short sides and 3 on the long ones, or 2 and 4 — the drawing decides) is
    exactly the case a "pin_count // 4" generator gets silently wrong.
    """

    family: Literal["quad_no_lead"] = "quad_no_lead"
    pins_x: int = Field(
        gt=0, description="Lands on each of the top and bottom rows."
    )
    pins_y: int = Field(
        gt=0, description="Lands on each of the left and right columns."
    )

    @property
    def pin_count(self) -> int:
        return 2 * (self.pins_x + self.pins_y)

    def _land(self) -> no_lead.Land:
        return no_lead.quad(
            pins_x=self.pins_x,
            pins_y=self.pins_y,
            pitch=self.pitch,
            body_x=self._body_dim(self.body.x),
            body_y=self._body_dim(self.body.y),
            lead_width=self.lead_width,
            lead_length=self.lead_length,
            pull_back=self.pull_back,
            exposed_pad=self._ep(),
            density=self.density.value,
        )


def _from_land(land: gullwing.LandPad) -> Pad:
    return Pad(
        number=land.number,
        at=(land.x, land.y),
        size=(land.size_x, land.size_y),
        shape=PadShape.ROUNDRECT,
        type=PadType.SMD,
    )


Package = Annotated[
    CustomPackage | DualGullwing | QuadGullwing | DualNoLead | QuadNoLead,
    Field(discriminator="family"),
]
