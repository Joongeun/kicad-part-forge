"""The real gate: KiCad's own parser judging our output.

`kicad-cli sym|fp upgrade --force` re-reads a file and writes it back in KiCad
9's canonical form. Two things are asserted here, in increasing strength:

1. It exits 0 — the file parses.
2. What it writes back is **byte-identical to what we wrote**, apart from the
   `(generator ...)` token it stamps with its own name. That is the strongest
   available statement that we emit canonical form rather than merely
   acceptable form, and it means a part edited in KiCad and a part regenerated
   from the IR diff only where they genuinely differ.

Skips cleanly when KiCad is not installed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from kifab.build import build
from kifab.ir import load_part

ROOT = Path(__file__).resolve().parent.parent
PARTS_DIR = ROOT / "parts"
KICAD_CLI = Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")

pytestmark = pytest.mark.skipif(
    not KICAD_CLI.exists(), reason="kicad-cli not found on this machine"
)

# The only line kicad-cli is expected to change: it stamps its own name.
_GENERATOR = "\t(generator "


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory):
    out = tmp_path_factory.mktemp("build")
    parts = [load_part(p) for p in sorted(PARTS_DIR.glob("*.yaml"))]
    assert parts, "parts/ is empty"
    return build(parts, out)


def _upgrade(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(KICAD_CLI), *args], capture_output=True, text=True, check=False
    )


def _diff_lines(before: str, after: str) -> list[tuple[str, str]]:
    a, b = before.splitlines(), after.splitlines()
    assert len(a) == len(b), (
        f"kicad-cli changed the line count: {len(a)} -> {len(b)}"
    )
    return [(x, y) for x, y in zip(a, b) if x != y]


def test_footprints_are_a_fixed_point_of_fp_upgrade(built, tmp_path: Path) -> None:
    result = built
    for path in result.footprints.values():
        pretty = tmp_path / f"{path.stem}.pretty"
        pretty.mkdir()
        copied = pretty / path.name
        shutil.copy(path, copied)
        before = copied.read_text(encoding="utf-8")

        run = _upgrade("fp", "upgrade", "--force", str(pretty))
        assert run.returncode == 0, f"{path.name} rejected by KiCad:\n{run.stderr}"

        changed = _diff_lines(before, copied.read_text(encoding="utf-8"))
        assert all(x.startswith(_GENERATOR) for x, _ in changed), (
            f"{path.name} is not in canonical form; kicad-cli rewrote:\n"
            + "\n".join(f"  {x!r} -> {y!r}" for x, y in changed[:10])
        )


def test_symbol_library_is_a_fixed_point_of_sym_upgrade(built, tmp_path: Path) -> None:
    result = built
    for path in result.symbol_libraries.values():
        copied = tmp_path / path.name
        shutil.copy(path, copied)
        before = copied.read_text(encoding="utf-8")

        run = _upgrade("sym", "upgrade", "--force", str(copied))
        assert run.returncode == 0, f"{path.name} rejected by KiCad:\n{run.stderr}"

        changed = _diff_lines(before, copied.read_text(encoding="utf-8"))
        assert all(x.startswith(_GENERATOR) for x, _ in changed), (
            f"{path.name} is not in canonical form; kicad-cli rewrote:\n"
            + "\n".join(f"  {x!r} -> {y!r}" for x, y in changed[:10])
        )


def test_footprints_render_to_svg(built, tmp_path: Path) -> None:
    """A file can parse and still be undrawable; make KiCad actually draw it."""
    result = built
    for name, path in result.footprints.items():
        pretty = tmp_path / "svg" / f"{path.stem}.pretty"
        pretty.mkdir(parents=True)
        shutil.copy(path, pretty / path.name)
        target = tmp_path / "svg" / path.stem
        run = _upgrade("fp", "export", "svg", "-o", str(target), str(pretty))
        assert run.returncode == 0, f"{name} would not render:\n{run.stderr}"
        assert list(target.glob("*.svg")), f"{name} produced no SVG"
