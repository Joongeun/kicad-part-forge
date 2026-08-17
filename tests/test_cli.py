"""`kifab build` — the end-to-end path a user actually runs."""

from __future__ import annotations

from pathlib import Path

import pytest

from kifab.build import BuildResult, build, discover
from kifab.cli import main
from kifab.ir import load_part

ROOT = Path(__file__).resolve().parent.parent
PARTS_DIR = ROOT / "parts"


def _run(tmp_path: Path, *args: str) -> int:
    return main(["build", *args, "-o", str(tmp_path)])


def test_build_writes_a_library_and_a_pretty_directory(tmp_path: Path) -> None:
    assert _run(tmp_path, str(PARTS_DIR)) == 0
    assert (tmp_path / "kifab.kicad_sym").is_file()
    pretty = tmp_path / "kifab.pretty"
    assert pretty.is_dir()
    assert {p.name for p in pretty.glob("*.kicad_mod")} == {
        "SOIC-8_3.9x4.9mm_P1.27mm.kicad_mod",
        "LQFP-48_7x7mm_P0.5mm.kicad_mod",
        "L_1206_3216Metric.kicad_mod",
    }


def test_building_a_single_file_works(tmp_path: Path) -> None:
    assert _run(tmp_path, str(PARTS_DIR / "24LC256.yaml")) == 0
    text = (tmp_path / "kifab.kicad_sym").read_text(encoding="utf-8")
    assert '(symbol "24LC256"' in text
    assert '(symbol "STM32F103C8T6"' not in text


def test_rebuilding_is_byte_stable(tmp_path: Path) -> None:
    """Regenerating an unchanged part must be a no-op in version control."""
    _run(tmp_path, str(PARTS_DIR))
    first = (tmp_path / "kifab.kicad_sym").read_bytes()
    _run(tmp_path, str(PARTS_DIR))
    assert (tmp_path / "kifab.kicad_sym").read_bytes() == first


def test_missing_file_reports_cleanly(tmp_path: Path, capsys) -> None:
    assert _run(tmp_path, str(tmp_path / "nope.yaml")) == 2
    assert "no such part file" in capsys.readouterr().err


def test_invalid_part_fails_with_the_path_in_the_message(
    tmp_path: Path, capsys
) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("mpn: X\nsymbol: {pins: []}\n", encoding="utf-8")
    assert _run(tmp_path, str(bad)) == 1
    assert "bad.yaml" in capsys.readouterr().err


def test_empty_directory_is_an_error_not_a_silent_success(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _run(tmp_path, str(empty)) == 2


def test_discover_expands_directories_and_sorts(tmp_path: Path) -> None:
    found = discover([PARTS_DIR])
    assert found == sorted(found)
    assert all(p.suffix in (".yaml", ".yml") for p in found)


def test_parts_sharing_a_footprint_are_allowed_when_identical(tmp_path: Path) -> None:
    base = load_part(PARTS_DIR / "24LC256.yaml")
    twin = base.model_copy(update={"mpn": "24AA256"})
    result = build([base, twin], tmp_path)
    assert isinstance(result, BuildResult)
    assert len(result.footprints) == 1, "one footprint, shared by both parts"
    text = (tmp_path / "kifab.kicad_sym").read_text(encoding="utf-8")
    assert '(symbol "24AA256"' in text and '(symbol "24LC256"' in text


def test_parts_disagreeing_about_a_footprint_are_rejected(tmp_path: Path) -> None:
    """Silently letting one overwrite the other ships the wrong land pattern."""
    base = load_part(PARTS_DIR / "24LC256.yaml")
    conflicting = load_part(PARTS_DIR / "STM32F103C8T6.yaml").model_copy(
        update={"footprint": base.footprint.model_copy(update={"tags": "different"})}
    )
    with pytest.raises(ValueError, match="different geometry"):
        build([base, conflicting], tmp_path)


def test_two_parts_with_the_same_mpn_are_rejected(tmp_path: Path) -> None:
    base = load_part(PARTS_DIR / "24LC256.yaml")
    with pytest.raises(ValueError, match="both define symbol"):
        build([base, base], tmp_path)


# --------------------------------------------------------------------------
# T0 — `kifab index`, `kifab search`, `kifab adopt`
# --------------------------------------------------------------------------


def _t0(tmp_path: Path, corpus: Path, *args: str) -> list[str]:
    """Run a T0 command against an index confined to the test corpus."""
    return [*args, "--db", str(tmp_path / "index.sqlite3"), "--root", str(corpus)]


def test_index_reports_what_it_holds(tmp_path: Path, corpus: Path, capsys) -> None:
    assert main(_t0(tmp_path, corpus, "index")) == 0
    assert main(_t0(tmp_path, corpus, "index", "--status")) == 0
    out = capsys.readouterr().out
    assert "footprints: 4" in out
    assert "symbols:    2" in out


def test_search_separates_confident_from_review(
    tmp_path: Path, corpus: Path, capsys
) -> None:
    """The CLI must not let a near miss read as an answer."""
    main(_t0(tmp_path, corpus, "index"))
    code = main(
        _t0(
            tmp_path,
            corpus,
            "search",
            "LTC5552",
            "--package",
            "12-Lead Plastic QFN (3mm x 2mm), LTC DWG # 05-08-1985",
        )
    )
    out = capsys.readouterr().out
    assert code == 1  # nothing confident: a non-zero exit is the honest answer
    assert "CONFIDENT — none" in out
    assert "REVIEW — near misses" in out
    assert "DFN-12-1EP_2x3mm_P0.45mm_EP0.64x2.4mm" in out
    assert "family: QFN != DFN" in out


def test_search_json_exposes_the_confidence_split(
    tmp_path: Path, corpus: Path, capsys
) -> None:
    import json

    main(_t0(tmp_path, corpus, "index"))
    main(_t0(tmp_path, corpus, "search", "LTC5552", "--package", "QFN-12-1EP_2x3mm_P0.45mm_EP0.64x2.4mm", "--json"))
    payload = json.loads(capsys.readouterr().out)
    assert payload["footprints"]["confident"]
    assert all(
        c["name"] != "DFN-12-1EP_2x3mm_P0.45mm_EP0.64x2.4mm"
        for c in payload["footprints"]["confident"]
    )
    near = next(
        c
        for c in payload["footprints"]["review"]
        if c["name"] == "DFN-12-1EP_2x3mm_P0.45mm_EP0.64x2.4mm"
    )
    assert near["confidence"] == "review"
    assert any(
        e["attribute"] == "family" and e["verdict"] == "mismatch"
        for e in near["evidence"]
    )


def test_adopt_writes_a_buildable_part(tmp_path: Path, corpus: Path) -> None:
    main(_t0(tmp_path, corpus, "index"))
    out_dir = tmp_path / "parts"
    code = main(
        _t0(
            tmp_path,
            corpus,
            "adopt",
            "--footprint",
            "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
            "--symbol",
            "MCU_Test:TESTCHIP8Tx",
            "--mpn",
            "TESTCHIP8T6",
            "-o",
            str(out_dir),
        )
    )
    assert code == 0
    written = out_dir / "TESTCHIP8T6.yaml"
    assert written.is_file()
    assert load_part(written).mpn == "TESTCHIP8T6"
    assert main(["build", str(out_dir), "-o", str(tmp_path / "built")]) == 0


def test_adopt_refuses_to_overwrite_without_force(tmp_path: Path, corpus: Path) -> None:
    main(_t0(tmp_path, corpus, "index"))
    args = _t0(
        tmp_path,
        corpus,
        "adopt",
        "--footprint",
        "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
        "--symbol",
        "MCU_Test:TESTCHIP8Tx",
        "--mpn",
        "TESTCHIP8T6",
        "-o",
        str(tmp_path / "parts"),
    )
    assert main(args) == 0
    assert main(args) == 1
    assert main([*args, "--force"]) == 0


def test_adopt_reports_an_unknown_library_item(tmp_path: Path, corpus: Path) -> None:
    main(_t0(tmp_path, corpus, "index"))
    assert (
        main(_t0(tmp_path, corpus, "adopt", "--footprint", "Nope:Nothing"))
        == 2
    )
