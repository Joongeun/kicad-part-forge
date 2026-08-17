"""IPC-7351B land-pattern calculation.

The three governing equations (IPC-7351B section 3):

    Zmax = Lmin + 2*JT + sqrt(CL^2 + F^2 + P^2)     outer span of the land pair
    Gmin = Smax - 2*JH - sqrt(CS^2 + F^2 + P^2)     inner span of the land pair
    Xmax = Wmin + 2*JS + sqrt(CW^2 + F^2 + P^2)     land width

where JT/JH/JS are the toe/heel/side fillet goals for the chosen density level,
C* are the RMS tolerances of the corresponding dimensions, F is the fabrication
allowance and P the placement allowance.

From those, each land is:
    length = (Zmax - Gmin) / 2
    centre = (Zmax + Gmin) / 4      (distance from package centre)

F and P were not stated in any config file in the upstream repo. They were
recovered by solving against the shipped LQFP-48_7x7mm_P0.5mm footprint and
then confirmed against every other package in tests/test_ipc.py — see
FABRICATION_ALLOWANCE / PLACEMENT_ALLOWANCE below.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from .toleranced import Tol

# Recovered empirically (see module docstring). Any drift here shows up
# immediately as a geometry mismatch in the test suite.
FABRICATION_ALLOWANCE = 0.10
PLACEMENT_ALLOWANCE = 0.05

RULES_PATH = Path(__file__).with_name("ipc_7351b.yaml")

Density = str  # "least" | "nominal" | "most"


@dataclass(frozen=True)
class Offsets:
    """Fillet goals and courtyard excess for one density level, in mm."""

    toe: float
    heel: float
    side: float
    courtyard: float


@dataclass(frozen=True)
class Roundoff:
    """Grid each computed dimension is rounded to, in mm."""

    toe: float
    heel: float
    side: float


@dataclass(frozen=True)
class DeviceClass:
    name: str
    offsets: dict[Density, Offsets]
    roundoff: Roundoff

    def for_density(self, density: Density = "nominal") -> Offsets:
        try:
            return self.offsets[density]
        except KeyError:
            raise ValueError(
                f"unknown density {density!r} for class {self.name!r}; "
                f"expected one of {sorted(self.offsets)}"
            ) from None


@dataclass(frozen=True)
class LandPattern:
    """Result of an IPC land-pattern calculation."""

    Gmin: float
    Zmax: float
    Xmax: float

    @property
    def length(self) -> float:
        """Land length along the lead axis."""
        return (self.Zmax - self.Gmin) / 2

    @property
    def centre(self) -> float:
        """Distance from package centre to land centre."""
        return (self.Zmax + self.Gmin) / 4


@lru_cache(maxsize=1)
def load_rules(path: Path | None = None) -> dict[str, DeviceClass]:
    """Load the vendored IPC-7351B tables."""
    data = yaml.safe_load((path or RULES_PATH).read_text(encoding="utf-8"))
    classes: dict[str, DeviceClass] = {}
    for name, body in data.items():
        if not isinstance(body, dict) or "round_base" not in body:
            continue  # e.g. ipc_generic_rules
        offsets = {
            density: Offsets(**body[density])
            for density in ("least", "nominal", "most")
            if density in body
        }
        classes[name] = DeviceClass(
            name=name,
            offsets=offsets,
            roundoff=Roundoff(**body["round_base"]),
        )
    return classes


def get_class(name: str) -> DeviceClass:
    classes = load_rules()
    try:
        return classes[name]
    except KeyError:
        raise ValueError(
            f"unknown IPC device class {name!r}; known: {sorted(classes)}"
        ) from None


def round_to_base(value: float, base: float) -> float:
    """Round to the nearest multiple of `base`, as kilibs does."""
    if base <= 0:
        return value
    return round(value / base) * base


def gullwing_class(pitch: float) -> str:
    """IPC splits gullwing fillet goals at a pitch of 0.625 mm."""
    return "ipc_spec_gw_large_pitch" if pitch > 0.625 else "ipc_spec_gw_small_pitch"


def land_pattern(
    *,
    device_class: DeviceClass,
    lead_outside: Tol,
    lead_width: Tol,
    lead_len: Tol | None = None,
    lead_inside: Tol | None = None,
    density: Density = "nominal",
    heel_reduction: float = 0.0,
) -> LandPattern:
    """Compute Gmin/Zmax/Xmax for a two-sided lead row.

    Provide either `lead_len` (the foot length, from which the inner span is
    derived) or `lead_inside` (the inner span directly). Datasheets state one
    or the other; deriving from `lead_len` costs an extra RMS combination,
    which is why IPC prefers the inner span when it is available.
    """
    if lead_inside is not None:
        inner = lead_inside
    elif lead_len is not None:
        inner = lead_outside - lead_len * 2
    else:
        raise ValueError("one of lead_inside or lead_len must be given")

    offsets = device_class.for_density(density)
    f, p = FABRICATION_ALLOWANCE, PLACEMENT_ALLOWANCE

    def rms(tol_rms: float) -> float:
        return math.sqrt(tol_rms**2 + f**2 + p**2)

    Gmin = (
        inner.maximum_RMS
        - 2 * offsets.heel
        + 2 * heel_reduction
        - rms(inner.ipc_tol_RMS)
    )
    Zmax = lead_outside.minimum_RMS + 2 * offsets.toe + rms(lead_outside.ipc_tol_RMS)
    Xmax = lead_width.minimum_RMS + 2 * offsets.side + rms(lead_width.ipc_tol_RMS)

    return LandPattern(
        Gmin=round_to_base(Gmin, device_class.roundoff.heel),
        Zmax=round_to_base(Zmax, device_class.roundoff.toe),
        Xmax=round_to_base(Xmax, device_class.roundoff.side),
    )
