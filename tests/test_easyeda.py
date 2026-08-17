"""T1 — the LCSC / EasyEDA ingester.

Everything here runs **offline**, against component payloads recorded verbatim
from the real API into `tests/fixtures/easyeda/`. The one test that touches the
network is marked `live` and is deselected by default (see `pyproject.toml`);
run it with `pytest -m live`.

The five fixtures were chosen because each carries a different failure mode:

| fixture | part | what it exercises |
|---|---|---|
| `C2040`  | RP2040, LQFN-56 + exposed pad | 28 pads stated at 90/270 deg; a numbered thermal pad; a 3D model |
| `C25804` | 0603 resistor | two-terminal chip — geometry that is *not derivable*, so `custom` lands; no body size in the package name |
| `C5446`  | XC6206, SOT-23-3 | EasyEDA's pin-number field disagreeing with the drawn pin number |
| `C6961`  | TL072, SOIC-8 | a multi-gate symbol, whose pins live in `subparts` and not in `dataStr` |
| `C2457`  | 1N4007, DO-41 | through-hole pads with drills |
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from kifab.build import build
from kifab.emit.footprint import render_footprint
from kifab.ir import Part, load_part
from kifab.ir.enums import PadType
from kifab.resolve import easyeda
from kifab.resolve.easyeda import (
    Candidate,
    EasyEdaClient,
    EasyEdaError,
    fetch_part,
    import_component,
    resolve_code,
    to_yaml,
)
from kifab.validate import check_part

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "easyeda"
CODES = ["C2040", "C25804", "C5446", "C6961", "C2457"]
KICAD_CLI = Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")


def payload(code: str) -> dict:
    data = json.loads((FIXTURES / f"{code}.json").read_text(encoding="utf-8"))
    return data["result"]


def imported(code: str, **kwargs):
    return import_component(payload(code), **kwargs)


class RecordedClient(EasyEdaClient):
    """An `EasyEdaClient` wired to the fixtures instead of the network."""

    def __init__(self, code: str, model: bytes | None = None) -> None:
        super().__init__(fetch=self._never)
        self.code = code
        self.model = model
        self.model_requests: list[str] = []

    @staticmethod
    def _never(url: str, body: bytes | None) -> bytes:  # pragma: no cover
        raise AssertionError(f"a test reached the network: {url}")

    def component(self, code: str) -> dict:
        assert code == self.code
        return payload(code)

    def search(self, query: str, limit: int = 8) -> list[Candidate]:
        return [Candidate(self.code, query, "PKG", "ACME", 1)]

    def model_step(self, uuid: str) -> bytes:
        self.model_requests.append(uuid)
        if self.model is None:
            raise EasyEdaError("no model")
        return self.model


# --------------------------------------------------------------------------
# The headline requirement: an import that cannot pass the linter is not done
# --------------------------------------------------------------------------


@pytest.mark.parametrize("code", CODES)
def test_every_fixture_imports_into_a_valid_part(code: str) -> None:
    result = imported(code)
    assert isinstance(result.part, Part)
    assert result.lcsc == code
    assert result.part.symbol.pins, "an imported symbol with no pins is not a part"


@pytest.mark.parametrize("code", CODES)
def test_every_imported_part_passes_kifab_check(code: str) -> None:
    """`check_part` renders the part and reads the file back — Phase 3's gate.

    Warnings are allowed and are the point: EasyEDA's electrical types are
    unreliable and the linter says so out loud. An *error* would mean we had
    imported something geometrically wrong.
    """
    report = check_part(imported(code).part)
    assert report.ok(), report.format(verbose=True)


@pytest.mark.parametrize("code", CODES)
def test_the_written_yaml_reloads_identically(code: str, tmp_path: Path) -> None:
    result = imported(code)
    target = tmp_path / f"{result.part.mpn}.yaml"
    target.write_text(to_yaml(result), encoding="utf-8")
    assert load_part(target).model_dump() == result.part.model_dump()


@pytest.mark.skipif(not KICAD_CLI.exists(), reason="kicad-cli not found")
@pytest.mark.parametrize("code", CODES)
def test_kicad_accepts_and_does_not_rewrite_an_imported_footprint(
    code: str, tmp_path: Path
) -> None:
    """The strongest gate we have, applied to imported parts too.

    Not merely "KiCad parses it": `fp upgrade` must write back what we wrote,
    apart from the generator token. This is the test that caught the pad-order
    defect below.
    """
    result = build([imported(code).part], tmp_path)
    pretty = tmp_path / "kifab.pretty"
    before = {p: p.read_text(encoding="utf-8") for p in pretty.glob("*.kicad_mod")}
    assert before
    subprocess.run(
        [str(KICAD_CLI), "fp", "upgrade", "--force", str(pretty)],
        check=True,
        capture_output=True,
    )
    for path, original in before.items():
        after = path.read_text(encoding="utf-8")
        differing = [
            (a, b)
            for a, b in zip(original.splitlines(), after.splitlines())
            if a != b and not a.lstrip().startswith("(generator ")
        ]
        assert not differing, f"{path.name}: kicad-cli rewrote {differing[:3]}"
    assert result.footprints


# --------------------------------------------------------------------------
# Normalisations that must be provably value-preserving
# --------------------------------------------------------------------------


def _stated_pad_boxes(code: str) -> dict[str, tuple[float, float, float, float]]:
    """Pad outlines exactly as EasyEDA drew them, in mm about the origin.

    EasyEDA states a *rendered polygon* alongside every pad's size/rotation
    pair, so it is its own oracle: whatever we do to size and rotation, the
    copper must land where the polygon says.
    """
    footprint = payload(code)["packageDetail"]["dataStr"]
    ox = float(footprint["head"]["x"])
    oy = float(footprint["head"]["y"])
    boxes: dict[str, tuple[float, float, float, float]] = {}
    for shape in footprint["shape"]:
        if not shape.startswith("PAD~"):
            continue
        fields = shape.split("~")
        points = easyeda._points(fields[10])
        # An OVAL pad states only the two endpoints of its long axis, which is
        # not an outline; only real polygons are usable as an oracle here.
        if len(points) < 3:
            continue
        xs = [(x - ox) * easyeda.TENMIL_MM for x, _ in points]
        ys = [(y - oy) * easyeda.TENMIL_MM for _, y in points]
        boxes[fields[8].strip()] = (min(xs), min(ys), max(xs), max(ys))
    return boxes


@pytest.mark.parametrize("code", CODES)
def test_folding_pad_rotation_into_the_size_preserves_the_copper(code: str) -> None:
    stated = _stated_pad_boxes(code)
    if not stated:
        pytest.skip(f"{code} states no pad outlines to check against")
    for pad in imported(code).part.footprint.package.resolve_pads():
        if pad.number not in stated or pad.rotation:
            continue
        x0, y0, x1, y1 = stated[pad.number]
        assert pad.at[0] == pytest.approx((x0 + x1) / 2, abs=1e-3)
        assert pad.at[1] == pytest.approx((y0 + y1) / 2, abs=1e-3)
        assert pad.size[0] == pytest.approx(x1 - x0, abs=1e-3)
        assert pad.size[1] == pytest.approx(y1 - y0, abs=1e-3)


def test_a_quarter_turn_pad_is_imported_as_a_size_swap_at_zero_degrees() -> None:
    """RP2040: 28 of its 57 lands are stated at 90 or 270 degrees."""
    result = imported("C2040")
    pads = {p.number: p for p in result.part.footprint.package.resolve_pads()}
    assert all(p.rotation == 0 for p in pads.values())
    # Pad 1 is on the left column (0 deg as stated), pad 56 on the top row
    # (270 deg as stated). Same land, turned a quarter.
    assert pads["1"].size == pytest.approx((0.85, 0.2))
    assert pads["56"].size == pytest.approx((0.2, 0.85))
    assert any("90/270" in note for note in result.notes)


def test_the_drawn_pin_number_wins_over_easyedas_sequence_field() -> None:
    """C5446's header field runs 1,2,3 while the drawn numbers are 1,3,2.

    Reading the header would put Vout on pad 3 and Vin on pad 2 — a regulator
    wired backwards, which is exactly the wrong-but-plausible import this tier
    exists to prevent.
    """
    result = imported("C5446")
    by_name = {pin.name: pin.number for pin in result.part.symbol.pins}
    assert by_name == {"GND": "1", "Vout": "2", "Vin": "3"}
    assert any("sequence index" in note for note in result.notes)


def test_a_multi_gate_symbol_is_read_from_its_subparts() -> None:
    """TL072: `dataStr.shape` is empty and both gates live in `subparts`."""
    result = imported("C6961")
    pins = result.part.symbol.pins
    assert len(pins) == 8
    assert {pin.unit for pin in pins} == {1, 2}
    assert {p.number for p in pins if p.unit == 2} == {"5", "6", "7"}
    assert any("sub-parts" in note for note in result.notes)


def test_through_hole_pads_carry_the_stated_drill() -> None:
    pads = imported("C2457").part.footprint.package.resolve_pads()
    assert [p.type for p in pads] == [PadType.THRU_HOLE, PadType.THRU_HOLE]
    assert all(p.drill == pytest.approx(1.0, abs=1e-3) for p in pads)
    # 8.70 mm lead pitch, per the package name DO-41_…-P8.70-…
    assert abs(pads[0].at[0] - pads[1].at[0]) == pytest.approx(8.7, abs=1e-3)
    assert imported("C2457").part.footprint.mount_type().value == "through_hole"


def test_the_body_comes_from_the_package_name_on_the_right_axis() -> None:
    """SOT-23 states L2.9-W1.6 but is drawn with its rows running vertically.

    The pair is orientation-independent; only the drawing can say which
    dimension is which axis. Getting it backwards would draw silkscreen across
    the lands.
    """
    body = imported("C5446").part.footprint.package.body
    assert (body.x, body.y) == (1.6, 2.9)
    # SOIC-8 is drawn the other way round, from the same kind of name.
    body = imported("C6961").part.footprint.package.body
    assert (body.x, body.y) == (5.0, 4.0)


def test_a_missing_body_size_is_flagged_not_invented() -> None:
    """R0603's package name states no dimensions, and its silk is not a body."""
    result = imported("C25804")
    assert any("check it against the datasheet" in n for n in result.notes)


def test_a_chip_part_lands_in_the_custom_family() -> None:
    """Two-terminal IPC geometry is not derivable (DECISIONS.md, Phase 1)."""
    package = imported("C25804").part.footprint.package
    assert package.family == "custom"
    assert len(package.pads) == 2


def test_the_ten_mil_grid_converts_exactly() -> None:
    pads = {p.number: p for p in imported("C2040").part.footprint.package.resolve_pads()}
    assert abs(pads["2"].at[1] - pads["1"].at[1]) == pytest.approx(0.4, abs=1e-4)
    assert pads["57"].size == pytest.approx((3.1, 3.1))
    assert pads["57"].at == pytest.approx((0.0, 0.0))


# --------------------------------------------------------------------------
# Things we refuse to guess at
# --------------------------------------------------------------------------


def test_a_polygon_pad_is_refused_rather_than_approximated() -> None:
    data = payload("C25804")
    shapes = data["packageDetail"]["dataStr"]["shape"]
    data["packageDetail"]["dataStr"]["shape"] = [
        s.replace("PAD~RECT~", "PAD~POLYGON~") for s in shapes
    ]
    with pytest.raises(EasyEdaError, match="cannot represent"):
        import_component(data)


def test_a_pad_with_no_symbol_pin_is_refused_unless_asked_for() -> None:
    data = payload("C25804")
    shapes = data["dataStr"]["shape"]
    # Drop the pin numbered 2, keeping its pad — the "footprint has a land the
    # symbol forgot" case, which is common on exposed pads.
    data["dataStr"]["shape"] = [
        s for s in shapes if not (s.startswith("P~") and "~0~2~end~" in s)
    ]
    assert len([s for s in data["dataStr"]["shape"] if s.startswith("P~")]) == 1
    with pytest.raises(EasyEdaError, match="--bond-extra-pads"):
        import_component(data)

    result = import_component(data, bond_extra_pads=True)
    assert {p.number for p in result.part.symbol.pins} == {"1", "2"}
    stub = next(p for p in result.part.symbol.pins if p.number == "2")
    assert stub.type.value == "unspecified"
    assert any("Unverified pins" in note for note in result.notes)


def test_a_symbol_pin_with_no_pad_is_refused() -> None:
    data = payload("C25804")
    data["dataStr"]["shape"] = data["dataStr"]["shape"] + [
        "P~show~0~9~80~0~0~x~0^^80~0^^M 80 0 h-10~#000^^0~65~3~0~EXTRA~end~~~#000"
        "^^0~75~-1~0~9~start~~~#000^^0~73~0^^0~M 70 -3 L 67 0 L 70 3"
    ]
    with pytest.raises(EasyEdaError, match="disagree"):
        import_component(data)


def test_a_missing_component_is_an_error_not_an_empty_part() -> None:
    raw = json.loads((FIXTURES / "C0000000-missing.json").read_text(encoding="utf-8"))
    client = EasyEdaClient(fetch=lambda url, body: json.dumps(raw).encode())
    with pytest.raises(EasyEdaError, match="not found"):
        client.component("C0000000")


def test_electrical_types_are_mapped_faithfully_and_the_linter_reports_them() -> None:
    """EasyEDA has five pin types; guessing better ones from names is not a
    normalisation, it is a guess. So we map faithfully and let SCH002 speak."""
    result = imported("C5446")
    assert [p.type.value for p in result.part.symbol.pins] == ["unspecified"] * 3
    report = check_part(result.part)
    assert report.ok(), "still a valid part"
    assert not report.ok(strict=True), "but the linter must not be silent about it"
    assert any(f.check == "SCH002" for f in report.findings)


# --------------------------------------------------------------------------
# Resolution, 3D models and the CLI
# --------------------------------------------------------------------------


def test_an_lcsc_code_is_used_as_given_without_a_search() -> None:
    client = EasyEdaClient(fetch=lambda url, body: pytest.fail("searched for a code"))
    assert resolve_code(client, "c2040") == ("C2040", [])


def test_an_ambiguous_mpn_returns_candidates_rather_than_picking_one() -> None:
    class Ambiguous(EasyEdaClient):
        def search(self, query: str, limit: int = 8) -> list[Candidate]:
            return [
                Candidate("C2040", "RP2040", "LQFN-56", "RPi", 60000),
                Candidate("C2961140", "RP2040", "QFN-56-EP", "RPi", 0),
            ]

    code, candidates = resolve_code(Ambiguous(fetch=lambda u, b: b""), "RP2040")
    assert code == ""
    assert len(candidates) == 2


def test_exactly_one_exact_mpn_match_resolves() -> None:
    class One(EasyEdaClient):
        def search(self, query: str, limit: int = 8) -> list[Candidate]:
            return [
                Candidate("C6961", "TL072CDT", "SO-8", "ST", 1),
                Candidate("C999", "TL072CDTR(XBLW)", "SOP-8", "XBLW", 1),
            ]

    code, _ = resolve_code(One(fetch=lambda u, b: b""), "tl072cdt")
    assert code == "C6961"


def test_a_fetched_3d_model_is_wired_up_with_a_kicad_path_variable(
    tmp_path: Path,
) -> None:
    client = RecordedClient("C2040", model=b"ISO-10303-21;\nHEADER;\n")
    result, model = fetch_part(
        "C2040", client=client, models_dir=tmp_path, model_variable="MY_MODELS"
    )
    assert client.model_requests == [result.model_uuid]
    assert model is not None
    path = easyeda.write_model(model, tmp_path, "kifab", result.part.footprint.name)
    assert path.read_bytes() == model.data
    assert result.part.footprint.model == (
        "${MY_MODELS}/kifab.3dshapes/LQFN-56_L7.0-W7.0-P0.4-EP.step"
    )
    # KLC-F9.1: the reference must survive leaving this machine.
    assert check_part(result.part).ok()


def test_a_failed_3d_model_fetch_degrades_to_a_note_not_a_failure(
    tmp_path: Path,
) -> None:
    client = RecordedClient("C2040", model=None)
    result, model = fetch_part("C2040", client=client, models_dir=tmp_path)
    assert model is None
    assert result.part.footprint.model is None
    assert any("could not be fetched" in note for note in result.notes)


def test_a_3d_model_needing_placement_is_reported_not_silently_misplaced() -> None:
    """C5446's model states a 90 degree Z rotation the IR cannot express."""
    result = imported("C5446")
    assert result.model_needs_placement
    assert any("3D viewer" in note for note in result.notes)


def test_the_cli_imports_from_a_saved_payload_without_touching_the_network(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from kifab.cli import main

    code = main(
        [
            "lcsc",
            "C5446",
            "--payload",
            str(FIXTURES / "C5446.json"),
            "-o",
            str(tmp_path),
        ]
    )
    assert code == 0
    written = tmp_path / "XC6206P332MR-G.yaml"
    assert written.exists()
    assert "kifab check: OK" in capsys.readouterr().err
    assert load_part(written).mpn == "XC6206P332MR-G"


def test_the_cli_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    from kifab.cli import main

    args = ["lcsc", "C5446", "--payload", str(FIXTURES / "C5446.json"), "-o", str(tmp_path)]
    assert main(args) == 0
    assert main(args) == 1
    assert main(args + ["--force"]) == 0


# --------------------------------------------------------------------------
# The ledger, and the emitter defect this tier surfaced
# --------------------------------------------------------------------------


def test_the_normalisation_ledger_is_written_down() -> None:
    """`NORMALISATIONS` / `NOT_NORMALISED` are the reviewable statement of what
    this ingester does and refuses to do. An empty entry is a lie."""
    for ledger in (easyeda.NORMALISATIONS, easyeda.NOT_NORMALISED):
        assert ledger
        for key, reason in ledger.items():
            assert len(reason) > 40, f"{key} has no stated reason"


def test_pads_are_emitted_in_ascending_pad_number_order() -> None:
    """Regression for a Phase 1 emitter defect that only Phase 4 could reach.

    `kicad-cli fp upgrade` sorts pads by number. Every part before this tier
    happened to declare them in order already, so the emitter never had to; an
    EasyEDA import states its exposed pad first, and the conformance gate then
    reported 222 rewritten lines.
    """
    part = Part.model_validate(
        {
            "mpn": "ORDER",
            "symbol": {
                "pins": [
                    {"number": n, "name": "~"} for n in ("EP", "10", "2", "1", "A1")
                ]
            },
            "footprint": {
                "name": "ORDER",
                "package": {
                    "family": "custom",
                    "body": {"x": 4, "y": 4},
                    "pads": [
                        {"number": "EP", "at": [0, 0], "size": [1, 1]},
                        {"number": "10", "at": [1.5, 0], "size": [0.4, 0.4]},
                        {"number": "2", "at": [-1.5, 0], "size": [0.4, 0.4]},
                        {"number": "1", "at": [-1.5, 1], "size": [0.4, 0.4]},
                        {"number": "A1", "at": [1.5, 1], "size": [0.4, 0.4]},
                    ],
                },
            },
        }
    )
    order = [
        line.split('"')[1]
        for line in render_footprint(part).splitlines()
        if line.lstrip().startswith("(pad ")
    ]
    assert order == ["A1", "EP", "1", "2", "10"]


# --------------------------------------------------------------------------
# The one test that needs the internet
# --------------------------------------------------------------------------


@pytest.mark.live
def test_live_import_from_lcsc(tmp_path: Path) -> None:
    """Deselected by default. `pytest -m live` to run it.

    It exists so a change in EasyEDA's API surfaces as a failing test rather
    than as a mystery at the CLI, and so the recorded fixtures can be checked
    for drift.
    """
    result, model = fetch_part("C2040", models_dir=tmp_path)
    assert result.part.mpn == "RP2040"
    assert len(result.part.footprint.package.pads) == 57
    assert model is not None and model.data.startswith(b"ISO-10303-21")
    assert check_part(result.part).ok()
