"""Shared pytest configuration."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

#: KiCad's shipped libraries. Tests that read them skip cleanly when absent.
KICAD_SHARED = Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport")

requires_kicad = pytest.mark.skipif(
    not KICAD_SHARED.is_dir(), reason="KiCad installation not found on this machine"
)


# --------------------------------------------------------------------------
# A tiny synthetic KiCad corpus
# --------------------------------------------------------------------------
#
# The index and resolver tests run against files they write themselves rather
# than the 37k-item install: it keeps the suite fast, it works on a machine
# with no KiCad, and it lets a test state the exact corpus its assertion needs.
# The tests that must prove behaviour on the *real* corpus are marked
# `requires_kicad` and index one library directory, not all 153.


def write_footprint(
    directory: Path,
    name: str,
    pads: list[tuple[str, float, float, float, float]],
    descr: str = "",
    tags: str = "",
    body: tuple[float, float] | None = None,
) -> Path:
    """Write a minimal but structurally real `.kicad_mod`."""
    directory.mkdir(parents=True, exist_ok=True)
    lines = [
        f'(footprint "{name}"',
        "\t(version 20241229)",
        '\t(generator "kifab-test")',
        '\t(generator_version "9.0")',
        '\t(layer "F.Cu")',
        f'\t(descr "{descr}")',
        f'\t(tags "{tags}")',
        "\t(attr smd)",
    ]
    if body is not None:
        bx, by = body[0] / 2, body[1] / 2
        lines.append(
            f"\t(fp_rect (start {-bx} {-by}) (end {bx} {by}) "
            '(stroke (width 0.1) (type default)) (fill none) (layer "F.Fab"))'
        )
    for number, x, y, w, h in pads:
        lines.append(
            f'\t(pad "{number}" smd roundrect (at {x} {y}) (size {w} {h}) '
            '(layers "F.Cu" "F.Mask" "F.Paste") (roundrect_rratio 0.25))'
        )
    lines.append(")")
    path = directory / f"{name}.kicad_mod"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_symbol_library(
    path: Path,
    symbols: dict[str, dict],
) -> Path:
    """Write a minimal `.kicad_sym`. Each entry: {pins, footprint, description}."""
    path.parent.mkdir(parents=True, exist_ok=True)
    out = ["(kicad_symbol_lib", "\t(version 20241209)", '\t(generator "kifab-test")']
    for name, spec in symbols.items():
        out.append(f'\t(symbol "{name}"')
        if spec.get("extends"):
            out.append(f'\t\t(extends "{spec["extends"]}")')
        for key, value in (
            ("Reference", spec.get("reference", "U")),
            ("Value", name),
            ("Footprint", spec.get("footprint", "")),
            ("Datasheet", spec.get("datasheet", "")),
            ("Description", spec.get("description", "")),
            ("ki_keywords", spec.get("keywords", "")),
        ):
            out.append(f'\t\t(property "{key}" "{value}" (at 0 0 0))')
        pins = spec.get("pins") or []
        if pins:
            out.append(f'\t\t(symbol "{name}_1_1"')
            for number, pin_name, etype, x, y, angle in pins:
                out.append(
                    f"\t\t\t(pin {etype} line (at {x} {y} {angle}) (length 2.54) "
                    f'(name "{pin_name}" (effects (font (size 1.27 1.27)))) '
                    f'(number "{number}" (effects (font (size 1.27 1.27)))))'
                )
            out.append("\t\t)")
        out.append("\t)")
    out.append(")")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


def dual_row_pads(
    count: int, pitch: float, x: float, w: float = 0.7, h: float = 0.25
) -> list[tuple[str, float, float, float, float]]:
    """Lands in two columns, numbered anticlockwise from the top left."""
    half = count // 2
    top = -(half - 1) * pitch / 2
    pads = [(str(i + 1), -x, round(top + i * pitch, 4), w, h) for i in range(half)]
    pads += [(str(count - i), x, round(top + i * pitch, 4), w, h) for i in range(half)]
    return pads


def quad_pads(
    per_side: int, pitch: float, r: float, w: float = 0.7, h: float = 0.25
) -> list[tuple[str, float, float, float, float]]:
    """Lands on all four edges, numbered anticlockwise from the top left."""
    start = -(per_side - 1) * pitch / 2
    pads: list[tuple[str, float, float, float, float]] = []
    n = 1
    for i in range(per_side):
        pads.append((str(n), -r, round(start + i * pitch, 4), w, h))
        n += 1
    for i in range(per_side):
        pads.append((str(n), round(start + i * pitch, 4), r, h, w))
        n += 1
    for i in range(per_side):
        pads.append((str(n), r, round(-(start + i * pitch), 4), w, h))
        n += 1
    for i in range(per_side):
        pads.append((str(n), round(-(start + i * pitch), 4), -r, h, w))
        n += 1
    return pads


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """A miniature library tree: `libs/Package_DFN_QFN.pretty` + a symbol lib."""
    root = tmp_path / "libs"
    pretty = root / "Package_DFN_QFN.pretty"

    # KiCad's real DDB part: a 12-lead DFN, 2x3 mm body, 0.45 mm pitch,
    # 0.64 x 2.4 mm exposed pad. Linear drawing 05-08-1723.
    write_footprint(
        pretty,
        "DFN-12-1EP_2x3mm_P0.45mm_EP0.64x2.4mm",
        dual_row_pads(12, 0.45, 0.925) + [("13", 0.0, 0.0, 0.64, 2.4)],
        descr=(
            "DDB Package; 12-Lead Plastic DFN (3mm x 2mm) "
            "(see Linear Technology DFN_12_05-08-1723.pdf)"
        ),
        tags="DFN 0.45",
        body=(2.0, 3.0),
    )
    # A genuine QFN-12 of the same body size, to prove the resolver can still
    # say yes when the package really does match.
    write_footprint(
        pretty,
        "QFN-12-1EP_2x3mm_P0.45mm_EP0.64x2.4mm",
        quad_pads(3, 0.45, 0.925) + [("13", 0.0, 0.0, 0.64, 2.4)],
        descr="UDB Package; 12-Lead Plastic QFN (3mm x 2mm), LTC DWG # 05-08-1985",
        tags="QFN 0.45",
        body=(2.0, 3.0),
    )
    write_footprint(
        pretty,
        "QFN-12-1EP_3x3mm_P0.5mm_EP1.6x1.6mm",
        quad_pads(3, 0.5, 1.4) + [("13", 0.0, 0.0, 1.6, 1.6)],
        descr="QFN, 12 Pin (3mm x 3mm)",
        tags="QFN 0.5",
        body=(3.0, 3.0),
    )
    write_footprint(
        root / "Package_SO.pretty",
        "SOIC-8_3.9x4.9mm_P1.27mm",
        dual_row_pads(8, 1.27, 2.475, w=1.95, h=0.6),
        descr="SOIC, 8 Pin (JEDEC MS-012AA)",
        tags="SOIC 1.27",
        body=(3.9, 4.9),
    )

    write_symbol_library(
        root / "MCU_Test.kicad_sym",
        {
            "LTC5552": {
                "description": "3 GHz to 20 GHz microwave mixer",
                "keywords": "mixer RF",
                "footprint": "",
                "pins": [
                    (str(i), f"P{i}", "passive", -7.62, 5.08 - 2.54 * i, 0)
                    for i in range(1, 7)
                ]
                + [
                    (str(i), f"P{i}", "passive", 7.62, 5.08 - 2.54 * (i - 6), 180)
                    for i in range(7, 14)
                ],
            },
            "TESTCHIP8Tx": {
                "description": "eight-pin test device",
                "keywords": "test",
                "footprint": "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
                "pins": [
                    (str(i), f"IO{i}", "bidirectional", -7.62, 5.08 - 2.54 * (i - 1), 0)
                    for i in range(1, 5)
                ]
                + [
                    (str(i), f"IO{i}", "bidirectional", 7.62, 5.08 - 2.54 * (i - 5), 180)
                    for i in range(5, 9)
                ],
            },
        },
    )
    return root


def pytest_addoption(parser) -> None:
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="rewrite tests/golden/ from the current emitters, then run the "
        "suite. Always read `git diff tests/golden/` afterwards — a golden "
        "file regenerated without being read is not a test.",
    )


def pytest_configure(config) -> None:
    if not config.getoption("--update-golden"):
        return
    # Golden cases are built at collection time, so the rewrite has to happen
    # before collection starts.
    spec = importlib.util.spec_from_file_location(
        "_golden_updater", HERE / "test_golden.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.update()
