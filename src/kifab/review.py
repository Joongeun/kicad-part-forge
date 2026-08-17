"""The human review gate — structural, not advisory.

The rule: **nothing a model proposed reaches the user's library until a human
confirms it.** That is enforced by the shape of the code, not by a warning
printed at the end of a run:

* `kifab.generate.generate()` takes a `run_dir` and has no parameter naming a
  library or a parts directory. There is no argument you could pass it that
  would make it write one.
* the only function that writes into `parts/` is `accept()`, below, and it is
  reachable only from a *separate command a human types* — `kifab accept`.
* `accept()` re-runs the full validator on the proposal, and refuses on any
  error. A reviewer who is not looking carefully still cannot promote a part
  that fails `kifab check`.
* `accept()` also re-runs the transcript audit, and refuses a run whose
  isolation cannot be proved. "Looks right" is not a reason to adopt a part
  whose provenance is unknown.

Nothing here is clever. It is a gate, and a gate's whole value is that it does
not have a bypass.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .audit import audit_run
from .generate import PROPOSAL_DIRNAME
from .ir import load_part
from .validate import Conformance, check_part
from .validate.report import Report


class ReviewError(RuntimeError):
    """The proposal was not accepted, and the reason is in the message."""


@dataclass(frozen=True)
class Acceptance:
    """What acceptance wrote, and what it checked first."""

    mpn: str
    source: Path
    target: Path
    check: Report
    audit: Report


def proposal_yaml(run_dir: Path) -> Path:
    """Find the single proposal YAML in a run directory."""
    run_dir = Path(run_dir)
    proposal_dir = run_dir / PROPOSAL_DIRNAME
    candidates = sorted(proposal_dir.glob("*.yaml"))
    if not candidates:
        raise ReviewError(
            f"no proposal in {proposal_dir}. Run `kifab generate` first — "
            "acceptance promotes an existing proposal, it never creates one."
        )
    if len(candidates) > 1:
        raise ReviewError(
            f"{proposal_dir} holds {len(candidates)} proposals "
            f"({', '.join(p.name for p in candidates)}); a run directory is "
            "one part, so this one is inconsistent."
        )
    return candidates[0]


def accept(
    run_dir: Path,
    *,
    parts_dir: Path,
    conformance: Conformance | None = None,
    force: bool = False,
    allow_unaudited: bool = False,
) -> Acceptance:
    """Promote a reviewed proposal into `parts/`. The only writer there.

    `force` overwrites an existing part file. `allow_unaudited` skips the
    provenance check, and exists only so that a hand-edited proposal from a run
    whose transcript was lost is not permanently unusable — it is not a way to
    ignore a *failed* audit, because the failure is printed either way.
    """
    run_dir = Path(run_dir)
    source = proposal_yaml(run_dir)

    audit = audit_run(run_dir)
    if not audit.ok() and not allow_unaudited:
        raise ReviewError(
            f"the audit of {run_dir} failed, so this part's provenance is not "
            "established:\n"
            + audit.format()
            + "\nA part that may have been copied from an existing footprint "
            "is not a generated part. Fix the run, or re-run generation."
        )

    try:
        part = load_part(source)
    except ValueError as exc:
        raise ReviewError(
            f"{source} is not a valid Part IR document, so it will not be "
            f"promoted:\n  {exc}"
        ) from exc
    check = check_part(part, conformance=conformance)
    if not check.ok():
        raise ReviewError(
            f"{source} does not pass `kifab check`, so it will not be "
            "promoted:\n" + check.format()
        )

    parts_dir = Path(parts_dir)
    parts_dir.mkdir(parents=True, exist_ok=True)
    target = parts_dir / f"{part.mpn}.yaml"
    if target.exists() and not force:
        raise ReviewError(
            f"{target} already exists. Diff it against {source} and re-run "
            "with --force if the new one is better."
        )
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return Acceptance(
        mpn=part.mpn, source=source, target=target, check=check, audit=audit
    )
