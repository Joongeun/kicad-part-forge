"""Tier T2 — generate a part from its datasheet.

**Read this before changing anything in here.** This module is constructed with
`(datasheet PDF bytes, MPN string)` and nothing else. It does not import
`kifab.index` or `kifab.resolve`; the local-library corpus is owned by T0 and is
simply not in scope. `tests/test_generate.py` asserts that as a property of the
import graph, so the constraint fails a test rather than eroding quietly.

Why that matters: the point of a generated part is that it was *derived from the
drawing*. A generator that can see the 15,179 footprints already on disk may
produce a perfect result by copying one, and there is no way afterwards to tell
which happened. Isolation is the only thing that makes the output evidence.

The sequence, in order, and each step is deterministic until step 4:

1. read the datasheet's text layer                     (free, local)
2. score and select the pin-table + drawing pages      (free, local, tested)
3. slice the PDF down to those pages                   (free, local)
4. hand the slice to the provider                      (the only model call)
5. parse the reply into the Part IR, strictly          (free, local)
6. build, validate and render it into `runs/<mpn>/`    (free, local)

Nothing here writes to `parts/` or to any library. Step 6 lands in a proposal
directory; `kifab.review.accept` is the only code that promotes it, and it is a
separate command a human has to run.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..build import build
from ..ir import Part
from ..llm import ExtractionRequest, LLMProvider
from ..pdf import Selection, extract_pages, select_pages, slice_pdf
from ..validate import Conformance, check_part
from ..validate.report import Report, Severity
from .prompt import build_instructions

PROPOSAL_DIRNAME = "proposal"


class GenerationError(RuntimeError):
    """Generation could not produce a proposal. Never silently recovered from."""


@dataclass(frozen=True)
class GenerationRequest:
    """The complete input to T2. Note that this is the whole list."""

    mpn: str
    datasheet: bytes

    def __post_init__(self) -> None:
        if not self.mpn.strip():
            raise ValueError("generation needs an MPN")
        if not self.datasheet:
            raise ValueError(
                "generation needs the datasheet PDF; there is no other source "
                "of geometry in this tier"
            )


@dataclass
class Proposal:
    """A generated part, staged for review. Not in anyone's library yet."""

    mpn: str
    run_dir: Path
    yaml_path: Path
    part: Part | None = None
    report: Report = field(default_factory=Report)
    selection: Selection | None = None
    provider: str = ""
    svg_dir: Path | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def proposal_dir(self) -> Path:
        return self.run_dir / PROPOSAL_DIRNAME

    def accepted_ready(self) -> bool:
        return self.part is not None and self.report.ok()

    def summary(self) -> str:
        lines = [f"proposal: {self.yaml_path}"]
        if self.selection is not None:
            lines.append(self.selection.explain())
        lines.append(f"provider: {self.provider}")
        lines.append(f"kifab check: {self.report.summary()}")
        if self.svg_dir is not None:
            lines.append(f"preview: {self.svg_dir}")
        lines += [f"note: {n}" for n in self.notes]
        return "\n".join(lines)


def _parse(yaml_text: str, mpn: str) -> Part:
    try:
        data: Any = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise GenerationError(f"the model did not return valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise GenerationError(
            "the model's reply is not a YAML mapping; got "
            f"{type(data).__name__}"
        )
    # The MPN is ours, not the model's: it is the run's identity, it keys every
    # derived UUID, and a model that renamed the part would silently produce a
    # file nobody asked for.
    stated = str(data.get("mpn", "") or "")
    if stated and stated != mpn:
        data["mpn"] = mpn
    elif not stated:
        data["mpn"] = mpn
    try:
        return Part.model_validate(data)
    except Exception as exc:
        raise GenerationError(
            f"the model's YAML is not a valid Part IR document: {exc}"
        ) from exc


def generate(
    request: GenerationRequest,
    *,
    provider: LLMProvider,
    run_dir: Path,
    conformance: Conformance | None = None,
    max_pages: int | None = None,
) -> Proposal:
    """Run tier T2 for one part. Raises `GenerationError`; never guesses."""
    run_dir = Path(run_dir)
    proposal_dir = run_dir / PROPOSAL_DIRNAME
    proposal_dir.mkdir(parents=True, exist_ok=True)

    # 1-3: deterministic, free, and done before a provider is ever called.
    pages = extract_pages(request.datasheet)
    selection = (
        select_pages(pages, max_pages=max_pages)
        if max_pages
        else select_pages(pages)
    )
    if not selection.has_text_layer:
        raise GenerationError(
            f"{request.mpn}: this PDF has no usable text layer, so the "
            "pin-table and mechanical-drawing pages cannot be identified "
            "locally. Sending the whole document would cost several times as "
            "much and extract worse. Supply a text-layer PDF, or write "
            f"parts/{request.mpn}.yaml by hand (tier T3)."
        )
    if not selection.pin_table_pages:
        raise GenerationError(
            f"{request.mpn}: no page in this document looks like a pin table. "
            "Check that this is the right datasheet."
        )
    if not selection.mechanical_pages:
        raise GenerationError(
            f"{request.mpn}: no page in this document looks like a package "
            "mechanical drawing. Without it there is nothing to compute pad "
            "geometry from, and kifab will not guess it."
        )

    sliced = slice_pdf(request.datasheet, selection.pages)
    (run_dir / "pages.txt").write_text(selection.explain() + "\n", encoding="utf-8")

    # 4: the only model call in the pipeline.
    result = provider.extract(
        ExtractionRequest(
            mpn=request.mpn,
            pdf=sliced,
            pages=selection.pages,
            instructions=build_instructions(
                mpn=request.mpn,
                pages=selection.pages,
                total_pages=selection.total_pages,
                source=provider.source_clause(request.mpn),
            ),
        )
    )

    # 5-6: back to deterministic. The reply is written before it is parsed, so
    # a reply that fails to parse is still on disk to be read.
    yaml_path = proposal_dir / f"{request.mpn}.yaml"
    yaml_path.write_text(result.yaml_text, encoding="utf-8")

    proposal = Proposal(
        mpn=request.mpn,
        run_dir=run_dir,
        yaml_path=yaml_path,
        selection=selection,
        provider=result.provider,
    )

    part = _parse(result.yaml_text, request.mpn)
    proposal.part = part

    built = build([part], proposal_dir / "build")
    proposal.report = check_part(part, conformance=conformance)
    proposal.notes = [
        line.strip()
        for line in result.yaml_text.splitlines()
        if line.strip().startswith("# NOTE:")
    ]
    if conformance is not None and conformance.available:
        svg = _render_preview(built, proposal_dir, conformance)
        proposal.svg_dir = svg
    return proposal


def _render_preview(built, proposal_dir: Path, conformance: Conformance) -> Path | None:
    """Export SVGs so a human reviews a picture, not an s-expression.

    The review gate is only meaningful if reviewing is easy. A pin table in
    YAML plus a rendered land pattern is something a designer can actually
    judge in ten seconds; a `.kicad_mod` is not.
    """
    target = proposal_dir / "preview"
    target.mkdir(parents=True, exist_ok=True)
    ok = False
    seen: set[Path] = set()
    for path in built.footprints.values():
        if path.parent in seen:
            continue
        seen.add(path.parent)
        run = subprocess.run(
            [str(conformance.cli), "fp", "export", "svg", "-o", str(target), str(path.parent)],
            capture_output=True,
            text=True,
            check=False,
        )
        ok = ok or run.returncode == 0
    for path in built.symbol_libraries.values():
        run = subprocess.run(
            [str(conformance.cli), "sym", "export", "svg", "-o", str(target), str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        ok = ok or run.returncode == 0
    return target if ok and any(target.iterdir()) else None


def report_severity_counts(report: Report) -> dict[str, int]:
    return {
        "error": len(report.of(Severity.ERROR)),
        "warning": len(report.of(Severity.WARNING)),
        "info": len(report.of(Severity.INFO)),
    }


__all__ = [
    "GenerationError",
    "GenerationRequest",
    "PROPOSAL_DIRNAME",
    "Proposal",
    "generate",
]
