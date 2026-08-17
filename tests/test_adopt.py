"""Adoption: a found part becomes a normal, correctable part of this project."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import KICAD_SHARED, requires_kicad

from kifab.build import build
from kifab.ir import CustomPackage, Side, load_part
from kifab.resolve.adopt import AdoptionError, adopt, to_yaml

DDB_NAME = "DFN-12-1EP_2x3mm_P0.45mm_EP0.64x2.4mm"


def _adopt(corpus: Path, **kwargs):
    return adopt(
        mpn="LTC5552TEST",
        footprint_path=corpus / "Package_DFN_QFN.pretty" / f"{DDB_NAME}.kicad_mod",
        symbol_path=corpus / "MCU_Test.kicad_sym",
        symbol_name="LTC5552",
        **kwargs,
    )


def test_adoption_produces_a_valid_part(corpus: Path) -> None:
    adoption = _adopt(corpus)
    part = adoption.part
    assert part.mpn == "LTC5552TEST"
    assert part.footprint.name == DDB_NAME
    assert len(part.symbol.pins) == 13
    assert isinstance(part.footprint.package, CustomPackage)
    assert len(part.footprint.package.pads) == 13


def test_pads_are_lifted_verbatim(corpus: Path) -> None:
    """Reuse means reuse: the copper is the donor's, to the micron."""
    pads = {p.number: p for p in _adopt(corpus).part.footprint.package.pads}
    assert pads["13"].size == (0.64, 2.4)
    assert pads["13"].at == (0.0, 0.0)
    assert pads["1"].size == (0.7, 0.25)


def test_the_symbol_is_restyled_not_copied(corpus: Path) -> None:
    """The IR stores a side and a slot; coordinates are the emitter's job."""
    adoption = _adopt(corpus)
    left = [p for p in adoption.part.symbol.pins if p.side is Side.LEFT]
    right = [p for p in adoption.part.symbol.pins if p.side is Side.RIGHT]
    assert [p.number for p in left] == ["1", "2", "3", "4", "5", "6"]
    assert [p.slot for p in left] == [0, 1, 2, 3, 4, 5]
    assert [p.number for p in right] == ["7", "8", "9", "10", "11", "12", "13"]
    assert any("house style" in note for note in adoption.notes)


def test_the_written_yaml_reloads_and_builds(corpus: Path, tmp_path: Path) -> None:
    """The point of adopting into the IR: it is buildable like anything else.

    Regression: pad numbers are typed `str` and pydantic will not coerce an
    int, so an unquoted `number: 1` produced a file that could not be reloaded.
    """
    adoption = _adopt(corpus)
    target = tmp_path / "parts" / "LTC5552TEST.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(to_yaml(adoption), encoding="utf-8")

    reloaded = load_part(target)
    assert reloaded.mpn == "LTC5552TEST"
    assert {p.number for p in reloaded.footprint.package.resolve_pads()} == {
        str(i) for i in range(1, 14)
    }

    result = build([reloaded], tmp_path / "out")
    assert (tmp_path / "out" / "kifab.kicad_sym").is_file()
    assert (tmp_path / "out" / "kifab.pretty" / f"{DDB_NAME}.kicad_mod").is_file()
    assert result.paths


def test_the_yaml_records_where_it_came_from(corpus: Path) -> None:
    """A reused part must not lose the fact that it was reused."""
    text = to_yaml(_adopt(corpus))
    assert "# footprint:" in text
    assert "# symbol:" in text
    assert DDB_NAME in text


def test_adoption_refuses_a_symbol_that_disagrees_with_the_footprint(
    corpus: Path,
) -> None:
    """The IR's pin/pad cross-check is the guarantee; adoption is not a hole in it."""
    with pytest.raises(AdoptionError, match="do not form a valid part"):
        adopt(
            mpn="MISMATCH",
            footprint_path=corpus
            / "Package_SO.pretty"
            / "SOIC-8_3.9x4.9mm_P1.27mm.kicad_mod",
            symbol_path=corpus / "MCU_Test.kicad_sym",
            symbol_name="LTC5552",  # 13 pins onto an 8-pad footprint
        )


def test_adoption_without_a_symbol_must_be_asked_for_explicitly(corpus: Path) -> None:
    footprint = corpus / "Package_SO.pretty" / "SOIC-8_3.9x4.9mm_P1.27mm.kicad_mod"
    with pytest.raises(AdoptionError, match="no symbol given"):
        adopt(mpn="X", footprint_path=footprint)

    adoption = adopt(mpn="X", footprint_path=footprint, pins_from_pads=True)
    assert len(adoption.part.symbol.pins) == 8
    assert all(p.type.value == "unspecified" for p in adoption.part.symbol.pins)
    assert any("stub" in note for note in adoption.notes)


# --------------------------------------------------------------------------
# Against the real install
# --------------------------------------------------------------------------


@requires_kicad
def test_adopting_a_real_kicad_part_round_trips(tmp_path: Path) -> None:
    adoption = adopt(
        mpn="24LC256ADOPTED",
        footprint_path=KICAD_SHARED
        / "footprints/Package_SO.pretty/SOIC-8_3.9x4.9mm_P1.27mm.kicad_mod",
        symbol_path=KICAD_SHARED / "symbols/Memory_EEPROM.kicad_sym",
        symbol_name="24LC256",
    )
    # `24LC256` is a derived symbol; its pins live on its base.
    assert any("extends" in note for note in adoption.notes)
    assert len(adoption.part.symbol.pins) == 8

    target = tmp_path / "24LC256ADOPTED.yaml"
    target.write_text(to_yaml(adoption), encoding="utf-8")
    reloaded = load_part(target)
    build([reloaded], tmp_path / "out")
    assert (tmp_path / "out" / "kifab.kicad_sym").is_file()
