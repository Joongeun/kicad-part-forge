"""The PCB-footprint half of the IR."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .enums import MountType
from .package import Package


class FootprintStyle(BaseModel):
    """House style for footprint drawing.

    Defaults reproduce the official KiCad footprint conventions, so a kifab
    footprint sits next to an official one without looking foreign.
    """

    model_config = ConfigDict(extra="forbid")

    silk_width: float = Field(default=0.12, gt=0, description="F.SilkS line width, mm.")
    fab_width: float = Field(default=0.1, gt=0, description="F.Fab line width, mm.")
    courtyard_width: float = Field(
        default=0.05, gt=0, description="F.CrtYd line width, mm."
    )
    silk_body_offset: float = Field(
        default=0.11,
        ge=0,
        description="Gap from the body outline out to the silk centreline, mm. "
        "0.11 = half the silk width plus 0.05, which is what the official "
        "generator uses.",
    )
    silk_pad_clearance: float = Field(
        default=0.2,
        ge=0,
        description="Minimum clearance from a pad edge to silkscreen copper, "
        "mm. KLC F5.3. Silk that would come closer is trimmed away, not moved.",
    )
    courtyard_grid: float = Field(
        default=0.01,
        gt=0,
        description="Courtyard coordinates are rounded outward to this grid, "
        "mm. KLC F5.3 requires 0.01 mm.",
    )
    text_size: float = Field(default=1.0, gt=0)
    text_thickness: float = Field(default=0.15, gt=0)
    fab_reference_size: float = Field(
        default=1.0,
        gt=0,
        description="Size of the ${REFERENCE} text on F.Fab. Shrunk "
        "automatically when the body is too small to hold it.",
    )
    pin1_marker: float = Field(
        default=0.5,
        gt=0,
        description="Side length of the silkscreen pin-1 triangle, mm. The "
        "triangle is dropped entirely if it cannot clear every pad; the F.Fab "
        "corner chamfer is the pin-1 indicator that is always present.",
    )


class FootprintSpec(BaseModel):
    """Everything needed to draw the footprint, minus the part's identity.

    `name` is the footprint's own name and is deliberately *not* the MPN: many
    parts share `SOIC-8_3.9x4.9mm_P1.27mm`, and naming it after one of them is
    how libraries end up with fifty copies of the same land pattern.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        description="Footprint name, by convention describing the package and "
        "its critical dimensions, e.g. 'SOIC-8_3.9x4.9mm_P1.27mm'.",
    )
    description: str = ""
    tags: str = Field(
        default="", description="Space-separated search keywords for KiCad."
    )
    package: Package
    mount: MountType | None = Field(
        default=None,
        description="Override KiCad's `(attr ...)`. Unset means the package "
        "family decides, which is nearly always right.",
    )
    exclude_from_bom: bool = False
    exclude_from_pos_files: bool = False
    model: str | None = Field(
        default=None,
        description="3D model path, normally using a KiCad path variable, e.g. "
        "'${KICAD9_3DMODEL_DIR}/Package_SO.3dshapes/SOIC-8.step'.",
    )
    style: FootprintStyle = Field(default_factory=FootprintStyle)

    def mount_type(self) -> MountType:
        return self.mount if self.mount is not None else self.package.mount()
