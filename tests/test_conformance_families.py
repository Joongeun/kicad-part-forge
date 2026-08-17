"""Byte-level KiCad conformance for the package families added in Phase 5.

Phase 4's finding, restated because it cost real time: `kifab check` reports
canonical-form drift as a *warning*, which is easy to read past. The gate that
actually catches it is `kicad-cli fp upgrade --force` followed by a byte diff.
Every new family and every new emitter field therefore goes through this file
on the day it is written, not once a real part happens to use it.

`parts/` only carries families that existed before Phase 5, so the exemplars
here are built in-test: one per new family, plus the `model_offset` /
`model_rotate` fields.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from kifab.build import build
from kifab.ir import (
    Body,
    DualNoLead,
    ExposedPadSpec,
    FootprintSpec,
    Part,
    Pin,
    QuadNoLead,
    SymbolSpec,
)
from kifab.ir.enums import ElectricalType, Side

KICAD_CLI = Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")

pytestmark = pytest.mark.skipif(
    not KICAD_CLI.exists(), reason="kicad-cli not found on this machine"
)

_GENERATOR = "\t(generator "


def _pins(numbers: list[str]) -> list[Pin]:
    half = (len(numbers) + 1) // 2
    return [
        Pin(
            number=n,
            name=f"P{n}",
            type=ElectricalType.PASSIVE,
            side=Side.LEFT if i < half else Side.RIGHT,
        )
        for i, n in enumerate(numbers)
    ]


def _part(name: str, package, **footprint_kwargs) -> Part:
    numbers = sorted({p.number for p in package.resolve_pads()}, key=int)
    return Part(
        mpn=name,
        library="kifab",
        reference="U",
        description=f"Phase 5 conformance exemplar: {name}",
        symbol=SymbolSpec(pins=_pins(numbers)),
        footprint=FootprintSpec(
            name=name,
            description=f"Phase 5 conformance exemplar: {name}",
            package=package,
            **footprint_kwargs,
        ),
    )


def exemplars() -> list[Part]:
    return [
        _part(
            "EXEMPLAR-DFN-8",
            DualNoLead(
                body=Body(x=3.0, y=3.0),
                body_tolerance=0.1,
                pin_count=8,
                pitch=0.5,
                lead_width="0.20 .. 0.30",
                lead_length="0.30 .. 0.50",
            ),
        ),
        _part(
            "EXEMPLAR-QFN-32-1EP",
            QuadNoLead(
                body=Body(x=5.0, y=5.0),
                body_tolerance=0.1,
                pins_x=8,
                pins_y=8,
                pitch=0.5,
                lead_width="0.20 .. 0.30",
                lead_length="0.30 .. 0.50",
                exposed_pad=ExposedPadSpec(
                    size_x=3.45, size_y=3.45, paste_pads=(3, 3)
                ),
            ),
        ),
        _part(
            "EXEMPLAR-QFN-RECT-1EP",
            QuadNoLead(
                body=Body(x=4.0, y=6.0),
                body_tolerance=0.1,
                pins_x=6,
                pins_y=10,
                pitch=0.5,
                lead_width="0.20 .. 0.30",
                lead_length="0.30 .. 0.50",
                exposed_pad=ExposedPadSpec(size_x=2.4, size_y=4.4),
            ),
            # The authorised IR addition: a vendor STEP that is 90 deg out and
            # sits above the board plane.
            model="${KICAD9_3DMODEL_DIR}/Package_DFN_QFN.3dshapes/QFN.step",
            model_offset=(0.0, 0.0, -3.5),
            model_rotate=(0.0, 0.0, 90.0),
        ),
    ]


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory):
    return build(exemplars(), tmp_path_factory.mktemp("families"))


def _upgrade(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(KICAD_CLI), *args], capture_output=True, text=True, check=False
    )


def test_new_families_are_a_fixed_point_of_fp_upgrade(built, tmp_path: Path) -> None:
    for path in built.footprints.values():
        pretty = tmp_path / f"{path.stem}.pretty"
        pretty.mkdir()
        copied = pretty / path.name
        shutil.copy(path, copied)
        before = copied.read_text(encoding="utf-8")

        run = _upgrade("fp", "upgrade", "--force", str(pretty))
        assert run.returncode == 0, f"{path.name} rejected by KiCad:\n{run.stderr}"

        after = copied.read_text(encoding="utf-8")
        a, b = before.splitlines(), after.splitlines()
        assert len(a) == len(b), (
            f"{path.name}: kicad-cli changed the line count {len(a)} -> {len(b)}"
        )
        changed = [(x, y) for x, y in zip(a, b) if x != y]
        assert all(x.startswith(_GENERATOR) for x, _ in changed), (
            f"{path.name} is not in canonical form; kicad-cli rewrote:\n"
            + "\n".join(f"  {x!r} -> {y!r}" for x, y in changed[:10])
        )


def test_new_families_render_to_svg(built, tmp_path: Path) -> None:
    for name, path in built.footprints.items():
        pretty = tmp_path / "svg" / f"{path.stem}.pretty"
        pretty.mkdir(parents=True)
        shutil.copy(path, pretty / path.name)
        target = tmp_path / "svg" / path.stem
        run = _upgrade("fp", "export", "svg", "-o", str(target), str(pretty))
        assert run.returncode == 0, f"{name} would not render:\n{run.stderr}"
        assert list(target.glob("*.svg")), f"{name} produced no SVG"


def test_model_placement_reaches_the_file(built) -> None:
    """The authorised IR fields must actually change the emitted model block."""
    path = built.footprints["kifab:EXEMPLAR-QFN-RECT-1EP"]
    text = path.read_text(encoding="utf-8")
    assert "(xyz 0 0 -3.5)" in text
    assert "(xyz 0 0 90)" in text


def test_model_placement_without_a_model_is_refused() -> None:
    with pytest.raises(ValueError, match="nothing to place"):
        FootprintSpec(
            name="NOPE",
            package=DualNoLead(
                body=Body(x=3.0, y=3.0),
                pin_count=8,
                pitch=0.5,
                lead_width="0.20 .. 0.30",
                lead_length="0.30 .. 0.50",
            ),
            model_rotate=(0.0, 0.0, 90.0),
        )
