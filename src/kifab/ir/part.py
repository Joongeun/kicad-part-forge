"""The Part IR — the contract the whole project rests on.

One validated shape that every source fills and every emitter reads. The design
rules it follows, and why:

* **Identity lives once.** Reference designator, value, datasheet, description
  and MPN are on `Part`, not duplicated into the symbol and footprint halves,
  because KiCad puts the same strings in both files and two copies can disagree.
* **No coordinates where a convention will do.** Pins declare a side and a
  slot; packages declare datasheet dimensions. Coordinates are computed. That
  is what makes house style a single-point change and keeps the YAML diffable
  as a pin table rather than a drawing.
* **Every escape hatch is explicit.** `CustomPackage`, `SymbolStyle.body_width`
  and `Pin.length` exist so a stubborn part is still representable — but they
  are named so their use is visible in review.
* **Extra keys are an error.** `extra="forbid"` everywhere: a typo'd field
  silently ignored is how a part ships with the wrong value.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .footprint import FootprintSpec
from .symbol import SymbolSpec

# KiCad forbids these in library item names; they are the LIB_ID separator and
# the wildcard characters used by the symbol chooser.
_ILLEGAL_IN_NAME = re.compile(r"[:/\\\s]")


class Part(BaseModel):
    """A component: identity + symbol + footprint.

    Serialised as YAML in `parts/<name>.yaml`. That file is the reviewable
    artefact — the generated `.kicad_sym` / `.kicad_mod` are derived and
    disposable.
    """

    model_config = ConfigDict(extra="forbid")

    mpn: str = Field(
        min_length=1,
        description="Manufacturer part number. The part's identity: it keys the "
        "derived UUIDs and names the symbol.",
    )
    manufacturer: str = ""
    library: str = Field(
        default="kifab",
        min_length=1,
        description="Output library name. Produces `<library>.kicad_sym` and "
        "`<library>.pretty/`, and is the left half of the LIB_ID KiCad stores "
        "in a schematic.",
    )
    reference: str = Field(
        default="U",
        min_length=1,
        description="Reference designator prefix — 'U', 'R', 'D'. KiCad appends "
        "the number, so do not write 'U?'.",
    )
    value: str | None = Field(
        default=None,
        description="Schematic value. Defaults to the MPN; override for generic "
        "parts where the value is a quantity ('10k') rather than a part number.",
    )
    datasheet: str = ""
    description: str = ""

    symbol: SymbolSpec
    footprint: FootprintSpec

    @property
    def symbol_name(self) -> str:
        """The symbol's name inside the library. Equal to the MPN."""
        return self.mpn

    @property
    def display_value(self) -> str:
        return self.value if self.value is not None else self.mpn

    @property
    def footprint_id(self) -> str:
        """The LIB_ID the symbol's Footprint property points at."""
        return f"{self.library}:{self.footprint.name}"

    @model_validator(mode="after")
    def _check(self) -> Part:
        for label, name in (
            ("mpn", self.mpn),
            ("library", self.library),
            ("footprint name", self.footprint.name),
        ):
            if _ILLEGAL_IN_NAME.search(name):
                raise ValueError(
                    f"{label} {name!r} contains a character KiCad forbids in a "
                    "library item name (whitespace, ':', '/' or '\\')"
                )

        # Every pin must bond to a pad that exists, and vice versa. This is the
        # single most valuable cross-check in the IR: a symbol/footprint pair
        # that disagrees about pin numbers produces a board that cannot be
        # routed correctly, and nothing downstream will notice.
        # Stencil apertures carry a land's number for traceability but are not
        # copper and never appear in a netlist, so they are not bondable.
        pad_numbers = {
            pad.number
            for pad in self.footprint.package.resolve_pads()
            if not pad.aperture
        }
        pin_numbers = {pin.number for pin in self.symbol.pins}
        missing = sorted(pin_numbers - pad_numbers)
        # One exception, and only one: an exposed pad the drawing did not
        # dimension. The symbol keeps its thermal pin so the netlist is right
        # and the gap stays visible; FP008 blocks the part until somebody
        # supplies the size. Widening this to any missing pad would delete the
        # most valuable cross-check in the IR.
        if missing:
            missing = [n for n in missing if n not in self._undimensioned_ep_pins()]
        if missing:
            raise ValueError(
                f"symbol pins {missing} have no matching pad in footprint "
                f"{self.footprint.name!r} (pads: {sorted(pad_numbers)})"
            )
        unbonded = sorted(pad_numbers - pin_numbers)
        if unbonded:
            raise ValueError(
                f"footprint pads {unbonded} have no matching symbol pin; add a "
                "pin (mechanical pads are usually type 'passive' or "
                "'unspecified') so the netlist can reach them"
            )
        return self

    def _undimensioned_ep_pins(self) -> set[str]:
        """The one pin number an undimensioned exposed pad is allowed to owe.

        Empty unless the package actually declares the gap, so this can only
        ever excuse a pad somebody wrote `undimensioned: true` for.
        """
        spec = getattr(self.footprint.package, "exposed_pad", None)
        if spec is None or not spec.undimensioned:
            return set()
        if spec.number:
            return {spec.number}
        # The default the emitter would have used: one past the perimeter.
        lands = len([p for p in self.footprint.package.resolve_pads() if not p.aperture])
        return {str(lands + 1)}


def load_part(path: str | Path) -> Part:
    """Read and validate one `parts/*.yaml`."""
    path = Path(path)
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping at the top level")
    try:
        return Part.model_validate(data)
    except Exception as exc:
        raise ValueError(f"{path}: {exc}") from exc


def load_parts(paths: list[Path]) -> list[Part]:
    """Load several part files, keeping the caller's order."""
    return [load_part(p) for p in paths]
