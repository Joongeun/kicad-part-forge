"""The LTC5552 blind-holdout harness.

This file is the *apparatus*, not the experiment. Nothing here knows any
LTC5552 geometry, and that is the single most important property it has: a
harness that contains the answer cannot test anything.

Two layers, per the plan:

1. **Structural** — the preconditions that make a blind run meaningful. Asserted
   offline, on every `pytest` run, so the harness cannot silently rot between
   the day it was written and the day it is used.
2. **Audited** — the run itself, marked `live` and deselected by default. It
   needs a datasheet PDF and a working provider, neither of which belongs in a
   test suite that must pass with no network and no LLM.

Run the real thing with:

    KIFAB_LTC5552_DATASHEET=/path/to/5552f.pdf uv run pytest -m live \\
        tests/test_blind_holdout.py

or, end to end with the audit and the grading checklist, `scripts/blind_holdout.sh`.
"""

from __future__ import annotations

import inspect
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from kifab.audit import audit_run
from kifab.generate import GenerationRequest, generate
from kifab.llm import EXTRACTION_TOOLS, make_provider

ROOT = Path(__file__).resolve().parent.parent
MPN = "LTC5552"

#: The near miss the test is built around. KiCad ships a 12-lead DFN with the
#: same 2x3 mm body (Linear DWG 05-08-1723); the LTC5552 is the **UDB** QFN
#: (LTC DWG 05-08-1985). Same body, different lead frame, different lands.
TRAP_FOOTPRINT = "DFN-12-1EP_2x3mm_P0.45mm_EP0.64x2.4mm"


# ==========================================================================
# Layer 1 — the preconditions, checked offline on every run
# ==========================================================================


def test_the_answer_is_not_committed_to_this_repository() -> None:
    """A blind test whose answer is in the repo is not a blind test.

    Checked mechanically rather than promised in a comment: if anyone ever
    lands an LTC5552 part file or golden footprint, this fails and the
    holdout has to be retired and replaced.
    """
    offenders = sorted(
        p.relative_to(ROOT)
        for p in list((ROOT / "parts").glob(f"*{MPN}*"))
        + list((ROOT / "tests" / "golden").rglob(f"*{MPN}*"))
        + list(ROOT.glob(f"{MPN}*.kicad_mod"))
    )
    assert not offenders, (
        f"the LTC5552 answer is committed at {offenders}; the blind holdout "
        "is void until it is removed"
    )


def test_no_source_file_states_ltc5552_geometry() -> None:
    """No pad size, pitch or exposed-pad dimension keyed to the MPN, anywhere.

    Two source files name the part in prose — they explain *why* package
    identity beats body size, which is the Phase 2 lock this holdout exists to
    exercise. Prose is fine; a number is not. So the assertion is narrower and
    sharper than "never mention it": no line that names the MPN may also carry
    a millimetre-shaped figure, and no source file may state a dimension within
    three lines of naming it.
    """
    dimension = re.compile(r"(?<![\w.-])\d+\.\d+(?![\w.-])")
    offenders: list[str] = []
    for path in (ROOT / "src").rglob("*.py"):
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines):
            if MPN.lower() not in line.lower():
                continue
            window = lines[max(0, number - 3) : number + 4]
            for near in window:
                if dimension.search(near):
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{number + 1}: {near.strip()}"
                    )
    assert not offenders, (
        f"a source file states a dimension next to {MPN}: {offenders}"
    )


def test_the_t2_path_cannot_reach_the_local_library() -> None:
    """Structural isolation, restated here because this test is its reason."""
    code = (
        "import sys, kifab.generate; "
        "print(','.join(sorted(m for m in sys.modules "
        "if m.startswith('kifab.index') or m.startswith('kifab.resolve'))))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == ""


def test_the_extraction_tool_allowlist_is_exactly_two_tools() -> None:
    assert set(EXTRACTION_TOOLS) == {"read_pdf", "emit_ir"}


def test_generation_is_constructed_from_the_datasheet_and_the_mpn_alone() -> None:
    assert set(GenerationRequest.__dataclass_fields__) == {"mpn", "datasheet"}
    assert "index" not in inspect.signature(generate).parameters


def test_t0_rejects_the_trap_footprint_on_package_identity(corpus: Path) -> None:
    """The Phase 2 gate, re-asserted here because it is part of *this* test.

    Silently returning the DDB DFN for a UDB QFN part is the single most likely
    real-world failure of a search-first design, and it ships a wrong footprint
    that looks right in the 3D viewer.
    """
    # Imported inside the test, not at module scope: importing the index at
    # the top of this file would put it in the process before the isolation
    # test above ran, and that test's whole job is to notice it.
    from kifab.index import Index, LibraryRoot
    from kifab.resolve import search

    index = Index(corpus / "index.db")
    index.refresh([LibraryRoot(corpus, "user")])
    result = search(
        index, MPN, "12-Lead Plastic QFN (3mm x 2mm), LTC DWG # 05-08-1985"
    )
    confident = [c.name for c in result.footprints.confident]
    assert TRAP_FOOTPRINT not in confident, (
        "T0 returned the DFN (DDB) footprint as a confident match for a QFN "
        "(UDB) part. Body size is not package identity."
    )


# ==========================================================================
# Layer 2 — the run itself
# ==========================================================================

DATASHEET_ENV = "KIFAB_LTC5552_DATASHEET"
PROVIDER_ENV = "KIFAB_PROVIDER"


def _datasheet() -> Path:
    value = os.environ.get(DATASHEET_ENV, "")
    if not value:
        pytest.skip(f"set {DATASHEET_ENV} to the LTC5552 datasheet PDF")
    path = Path(value)
    if not path.is_file():
        pytest.skip(f"{DATASHEET_ENV}={value} is not a file")
    return path


@pytest.mark.live
def test_blind_generation_of_the_holdout_part(tmp_path: Path) -> None:
    """Generate LTC5552 from its datasheet alone, then audit that it was blind.

    Deliberately asserts nothing about the *values* produced — grading is a
    human step against ADI drawing 05-08-1985, and a threshold baked in here
    would be a number this file is not allowed to know. What it does assert is
    that the run was isolated, that it produced a proposal, and that the
    proposal is not a copy of the trap footprint.
    """
    run_dir = tmp_path / "runs" / MPN
    run_dir.mkdir(parents=True)
    provider = make_provider(os.environ.get(PROVIDER_ENV, "claude-code"), run_dir=run_dir)

    proposal = generate(
        GenerationRequest(mpn=MPN, datasheet=_datasheet().read_bytes()),
        provider=provider,
        run_dir=run_dir,
    )

    report = audit_run(run_dir)
    assert report.ok(), (
        "the run is not usable as evidence:\n" + report.format(verbose=True)
    )

    assert proposal.part is not None
    footprint = proposal.part.footprint
    assert footprint.name != TRAP_FOOTPRINT, (
        "the generated footprint is named after the trap part; a body-size "
        "match is not a package match"
    )
    # A UDB QFN has lands on all four sides. A DFN has two columns. If the
    # model produced a two-sided package, the trap was hit in substance even
    # if not in name.
    pads = footprint.package.resolve_pads()
    xs = {round(p.at[0], 3) for p in pads if not p.aperture}
    ys = {round(p.at[1], 3) for p in pads if not p.aperture}
    assert len(xs) > 2 and len(ys) > 2, (
        f"the generated package has lands on {len(xs)} x-positions and "
        f"{len(ys)} y-positions, which is a dual-row package, not a quad one"
    )

    print("\n" + proposal.summary())
    print(f"\nGrade this against ADI drawing 05-08-1985:\n  {proposal.yaml_path}")


@pytest.mark.live
def test_the_negative_control_on_the_real_datasheet(tmp_path: Path) -> None:
    """Same command, no provider. It must refuse, not improvise."""
    from kifab.llm import LLMUnavailable

    run_dir = tmp_path / "runs" / MPN
    run_dir.mkdir(parents=True)
    with pytest.raises(LLMUnavailable):
        generate(
            GenerationRequest(mpn=MPN, datasheet=_datasheet().read_bytes()),
            provider=make_provider("none", run_dir=run_dir),
            run_dir=run_dir,
        )
    assert not list((run_dir / "proposal").glob("*.yaml"))


# ==========================================================================
# The harness's own guard
# ==========================================================================


def test_this_file_states_no_millimetre_dimension() -> None:
    """Guard the guard: if the harness ever learns the answer, fail.

    Any decimal literal in this file would be a geometry value someone typed
    in, which is precisely how a blind test stops being blind. Package
    drawing numbers (05-08-1985) and the trap footprint's *name* are text, not
    dimensions, and are excluded explicitly.
    """
    text = Path(__file__).read_text(encoding="utf-8")
    text = text.replace(TRAP_FOOTPRINT, "").replace("05-08-1985", "")
    text = text.replace("05-08-1723", "").replace("2x3 mm", "")
    numbers = re.findall(r"(?<![\w.-])\d+\.\d+(?![\w.-])", text)
    assert not numbers, (
        f"this harness states dimension-shaped literals {numbers}; it must "
        "not know any LTC5552 geometry"
    )
