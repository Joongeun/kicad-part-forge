"""Tier T2 end to end, with a canned provider: pipeline, gate, and isolation.

Nothing here touches a model or a network. What it does assert is the property
that makes a generated part *evidence*: the T2 code path cannot see an existing
symbol or footprint, by construction rather than by instruction.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fakes import GOOD_YAML, CannedProvider
from pdfs import FILLER_PAGE, FRONT_PAGE, MECHANICAL_PAGE, PIN_TABLE_PAGE, datasheet, make_pdf

from kifab.generate import (
    GenerationError,
    GenerationRequest,
    Proposal,
    generate,
)
from kifab.llm import LLMUnavailable, NullProvider, Sandbox, Transcript
from kifab.review import ReviewError, accept
from kifab.validate import Conformance

KICAD_CLI = Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")


def _provider(cls, run_dir: Path, **kwargs):
    sandbox = Sandbox(
        root=run_dir / "scratch", transcript=Transcript(run_dir / "transcript.jsonl")
    )
    return cls(sandbox, **kwargs)


def _run(tmp_path: Path, *, provider_cls=CannedProvider, pdf=None, **kwargs) -> Proposal:
    run_dir = tmp_path / "runs" / "XYZ1234"
    run_dir.mkdir(parents=True)
    provider = _provider(provider_cls, run_dir, **kwargs)
    return generate(
        GenerationRequest(mpn="XYZ1234", datasheet=pdf or datasheet()),
        provider=provider,
        run_dir=run_dir,
    )


# -- structural isolation -------------------------------------------------


def test_the_t2_input_is_exactly_the_mpn_and_the_datasheet() -> None:
    """The whole anti-cheating design in one assertion."""
    assert set(GenerationRequest.__dataclass_fields__) == {"mpn", "datasheet"}


def test_generating_without_a_datasheet_is_refused() -> None:
    with pytest.raises(ValueError, match="needs the datasheet PDF"):
        GenerationRequest(mpn="LTC5552", datasheet=b"")


def test_the_library_index_is_not_in_the_t2_import_graph() -> None:
    """T0 owns the corpus; T2 must not even be able to name it.

    Asserted on a fresh interpreter rather than on this one, because the rest
    of the suite has already imported the index and `sys.modules` would lie.
    """
    code = (
        "import sys, kifab.generate; "
        "print(','.join(sorted(m for m in sys.modules "
        "if m.startswith('kifab.index') or m.startswith('kifab.resolve'))))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "", (
        "tier T2 imported the local-library layer: "
        f"{out.stdout.strip()}. A generator that can reach the 15,179 "
        "footprints on disk may have copied one, and no grading of the output "
        "can tell you which happened."
    )


def test_generate_has_no_parameter_that_could_name_a_library() -> None:
    import inspect

    params = set(inspect.signature(generate).parameters)
    assert params == {
        "request",
        "provider",
        "run_dir",
        "conformance",
        "max_pages",
    }


# -- the happy path -------------------------------------------------------


def test_generate_produces_a_proposal_and_writes_nothing_else(tmp_path: Path) -> None:
    proposal = _run(tmp_path)
    assert proposal.part is not None
    assert proposal.part.mpn == "XYZ1234"
    assert proposal.yaml_path.exists()
    assert proposal.yaml_path.parent.name == "proposal"
    assert proposal.report.ok()

    # Everything the run wrote lives under the run directory.
    written = {p for p in (tmp_path / "runs").rglob("*") if p.is_file()}
    assert written, "the run wrote nothing at all"
    assert all(str(p).startswith(str(tmp_path / "runs")) for p in written)
    assert not (tmp_path / "parts").exists()


def test_only_the_selected_pages_reach_the_provider(tmp_path: Path) -> None:
    """The cost reduction has to be real: a smaller PDF, not an instruction."""
    run_dir = tmp_path / "runs" / "XYZ1234"
    run_dir.mkdir(parents=True)
    provider = _provider(CannedProvider, run_dir)
    full = datasheet(pin_page=3, mech_page=6, total=40)
    generate(
        GenerationRequest(mpn="XYZ1234", datasheet=full),
        provider=provider,
        run_dir=run_dir,
    )
    request = provider.requests[0]
    assert request.pages == [1, 3, 6]
    assert len(request.pdf) < len(full)
    from kifab.pdf.text import page_count

    assert page_count(request.pdf) == 3


def test_the_mpn_is_ours_not_the_models(tmp_path: Path) -> None:
    """A model that renames the part must not produce a file nobody asked for."""
    proposal = _run(tmp_path, reply=GOOD_YAML.replace("mpn: XYZ1234", "mpn: SOMEOTHER"))
    assert proposal.part is not None
    assert proposal.part.mpn == "XYZ1234"


def test_model_notes_are_surfaced_not_buried(tmp_path: Path) -> None:
    reply = "# NOTE: the drawing does not state the exposed-pad tolerance\n" + GOOD_YAML
    proposal = _run(tmp_path, reply=reply)
    assert any("exposed-pad tolerance" in n for n in proposal.notes)


# -- the refusals ---------------------------------------------------------


def test_a_pdf_with_no_text_layer_stops_before_paying_for_it(tmp_path: Path) -> None:
    with pytest.raises(GenerationError, match="no usable text layer"):
        _run(tmp_path, pdf=make_pdf(["", "", ""]))


def test_a_datasheet_with_no_drawing_is_refused(tmp_path: Path) -> None:
    pdf = make_pdf([FRONT_PAGE, PIN_TABLE_PAGE, FILLER_PAGE])
    with pytest.raises(GenerationError, match="mechanical drawing"):
        _run(tmp_path, pdf=pdf)


def test_a_datasheet_with_no_pin_table_is_refused(tmp_path: Path) -> None:
    pdf = make_pdf([FRONT_PAGE, MECHANICAL_PAGE, FILLER_PAGE])
    with pytest.raises(GenerationError, match="pin table"):
        _run(tmp_path, pdf=pdf)


def test_unparseable_model_output_fails_and_is_kept_on_disk(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "XYZ1234"
    run_dir.mkdir(parents=True)
    provider = _provider(CannedProvider, run_dir, reply="I could not read the drawing.")
    with pytest.raises(GenerationError, match="not a YAML mapping"):
        generate(
            GenerationRequest(mpn="XYZ1234", datasheet=datasheet()),
            provider=provider,
            run_dir=run_dir,
        )
    kept = run_dir / "proposal" / "XYZ1234.yaml"
    assert kept.exists(), "the failing reply must stay on disk to be read"
    assert "could not read" in kept.read_text(encoding="utf-8")


def test_a_part_that_fails_validation_is_still_only_a_proposal(tmp_path: Path) -> None:
    """A bad extraction produces a rejected proposal, never a written part."""
    reply = GOOD_YAML.replace("- { number: 9, name: EP, type: passive, side: bottom }", "")
    run_dir = tmp_path / "runs" / "XYZ1234"
    run_dir.mkdir(parents=True)
    provider = _provider(CannedProvider, run_dir, reply=reply)
    with pytest.raises(GenerationError, match="not a valid Part IR"):
        generate(
            GenerationRequest(mpn="XYZ1234", datasheet=datasheet()),
            provider=provider,
            run_dir=run_dir,
        )


# -- the negative control -------------------------------------------------


def test_null_provider_refuses_rather_than_emitting_something_plausible(
    tmp_path: Path,
) -> None:
    """THE negative control. With no LLM, T2 must fail loudly and write nothing."""
    run_dir = tmp_path / "runs" / "LTC5552"
    run_dir.mkdir(parents=True)
    provider = _provider(NullProvider, run_dir)

    with pytest.raises(LLMUnavailable):
        generate(
            GenerationRequest(mpn="LTC5552", datasheet=datasheet()),
            provider=provider,
            run_dir=run_dir,
        )

    assert not list((run_dir / "proposal").glob("*.yaml")), (
        "the null provider produced a part file; a tool that emits plausible "
        "geometry with no way to read the datasheet is worse than useless"
    )
    assert not (tmp_path / "parts").exists()


# -- the review gate ------------------------------------------------------


def test_nothing_reaches_parts_until_accept_is_run(tmp_path: Path) -> None:
    proposal = _run(tmp_path)
    parts = tmp_path / "parts"
    assert not parts.exists()

    acceptance = accept(proposal.run_dir, parts_dir=parts)
    assert acceptance.target == parts / "XYZ1234.yaml"
    assert acceptance.target.read_text(encoding="utf-8") == proposal.yaml_path.read_text(
        encoding="utf-8"
    )


def test_accept_refuses_a_proposal_that_fails_the_validator(tmp_path: Path) -> None:
    proposal = _run(tmp_path)
    # Break the pin/pad bond after generation, the way a careless hand-edit would.
    text = proposal.yaml_path.read_text(encoding="utf-8").replace(
        "number: 9, name: EP", "number: 99, name: EP"
    )
    proposal.yaml_path.write_text(text, encoding="utf-8")
    with pytest.raises(ReviewError):
        accept(proposal.run_dir, parts_dir=tmp_path / "parts")
    assert not (tmp_path / "parts" / "XYZ1234.yaml").exists()


def test_accept_refuses_a_run_whose_audit_failed(tmp_path: Path) -> None:
    proposal = _run(tmp_path)
    transcript = proposal.run_dir / "transcript.jsonl"
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(
            '{"event": "tool_call", "tool": "read_pdf", '
            '"path": "/somewhere/DFN-12-1EP_2x3mm.kicad_mod"}\n'
        )
    with pytest.raises(ReviewError, match="provenance is not established"):
        accept(proposal.run_dir, parts_dir=tmp_path / "parts")
    assert not (tmp_path / "parts").exists()


def test_accept_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    proposal = _run(tmp_path)
    parts = tmp_path / "parts"
    accept(proposal.run_dir, parts_dir=parts)
    with pytest.raises(ReviewError, match="already exists"):
        accept(proposal.run_dir, parts_dir=parts)
    accept(proposal.run_dir, parts_dir=parts, force=True)


def test_accept_needs_a_proposal_to_exist(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "NOTHING"
    (run_dir / "proposal").mkdir(parents=True)
    with pytest.raises(ReviewError, match="no proposal"):
        accept(run_dir, parts_dir=tmp_path / "parts")


# -- the preview ----------------------------------------------------------


@pytest.mark.skipif(not KICAD_CLI.exists(), reason="kicad-cli not installed")
def test_the_proposal_renders_a_preview_for_the_human(tmp_path: Path) -> None:
    """A gate nobody can see through is a rubber stamp."""
    run_dir = tmp_path / "runs" / "XYZ1234"
    run_dir.mkdir(parents=True)
    proposal = generate(
        GenerationRequest(mpn="XYZ1234", datasheet=datasheet()),
        provider=_provider(CannedProvider, run_dir),
        run_dir=run_dir,
        conformance=Conformance.discover(str(KICAD_CLI)),
    )
    assert proposal.svg_dir is not None
    assert list(proposal.svg_dir.glob("*.svg"))
    assert proposal.report.ok()
