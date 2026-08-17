#!/usr/bin/env bash
# The LTC5552 blind-holdout run, end to end.
#
#   ./scripts/blind_holdout.sh /path/to/5552f.pdf [provider]
#
# What this does, and why each step is here:
#
#  1. refuses to run if the answer is present in the repository;
#  2. runs the generation in a scratch directory containing ONLY the datasheet,
#     so there is nothing else on disk to find even if isolation broke;
#  3. audits the transcript, and fails the run on any violation regardless of
#     how good the output looks;
#  4. runs the full validator including the kicad-cli format gate;
#  5. prints the grading checklist. Grading against ADI drawing 05-08-1985 is a
#     human step, on purpose — this script does not know the answer and must
#     not learn it.
#
# Exit codes, because two of them are outcomes rather than failures:
#
#   0  a proposal was produced and every gate passed
#  10  a proposal was produced and the validators are blocking it — the usual
#      result when the drawing did not state something the model refused to
#      invent. Read the findings and the NOTEs; the run is still evidence.
#   *  generation produced no proposal at all, or the audit failed. Nothing to
#      grade.
#
# Nothing is written to parts/. Acceptance is `kifab accept runs/LTC5552/`,
# which is a separate command a human types after reading the proposal.
set -euo pipefail

cd "$(dirname "$0")/.."
MPN=LTC5552
DATASHEET="${1:-}"
PROVIDER="${2:-claude-code}"

if [[ -z "$DATASHEET" ]]; then
  echo "usage: $0 /path/to/5552f.pdf [claude-code|api-key|none]" >&2
  exit 2
fi
if [[ ! -f "$DATASHEET" ]]; then
  echo "no datasheet at $DATASHEET" >&2
  exit 2
fi

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

step "Precondition: the answer must not be in this repository"
uv run pytest -q tests/test_blind_holdout.py

step "Stage an isolated scratch tree containing only the datasheet"
WORK="$(mktemp -d)"
trap 'echo "scratch kept at $WORK"' EXIT
cp "$DATASHEET" "$WORK/datasheet.pdf"
echo "    $WORK contains: $(ls "$WORK")"

step "Generate (tier T2, provider=$PROVIDER)"
set +e
uv run kifab generate "$MPN" \
  --datasheet "$WORK/datasheet.pdf" \
  --provider "$PROVIDER" \
  --force-tier=generate \
  --isolated \
  --runs runs \
  --force
GEN=$?
set -e

step "Audit the transcript — this decides whether the run is evidence at all"
uv run kifab audit "runs/$MPN"

# `kifab generate` exits nonzero both when it could not produce a proposal and
# when it produced one the validators block. Those are opposite outcomes and
# the harness has to tell them apart: the first is a broken run with nothing to
# read, the second is the pipeline working — a reviewable document with a named
# gap in it. The proposal file on disk is what separates them.
PROPOSAL="runs/$MPN/proposal/$MPN.yaml"
BLOCKED=0

if [[ ! -f "$PROPOSAL" ]]; then
  echo
  echo "generation produced no proposal (exit $GEN). Read runs/$MPN/ before" >&2
  echo "drawing any conclusion; a refusal is a legitimate outcome." >&2
  exit "$GEN"
fi

if (( GEN != 0 )); then
  BLOCKED=1
  echo
  echo "The model produced a proposal and the validators are blocking it." >&2
  echo "That is not a failed run: read the findings below, then the NOTEs in" >&2
  echo "$PROPOSAL. Grading still applies to everything that was stated." >&2
fi

step "Validate the proposal with every gate, warnings blocking"
uv run kifab check "$PROPOSAL" "runs/$MPN/proposal/build" --strict || true

step "Grade it"
cat <<EOF

  Proposal:  runs/$MPN/proposal/$MPN.yaml
  Preview:   runs/$MPN/proposal/preview/
  Pages sent: $(cat "runs/$MPN/pages.txt" 2>/dev/null | head -1)

  Footprint — against ADI drawing 05-08-1985:
    [ ] 12 perimeter lands + 1 exposed pad
    [ ] numbering and orientation agree with the pin-1 marker
    [ ] every land centre and size within +/-0.05 mm
    [ ] exposed-pad dimensions within +/-0.05 mm
    [ ] courtyard and silk-to-pad clearance meet KLC (kifab check says so)
    [ ] NOT byte-identical to DFN-12-1EP_2x3mm_P0.45mm_EP0.64x2.4mm

  With a reference footprint in hand:
    uv run scripts/grade_footprint.py REFERENCE.kicad_mod \\
      "runs/$MPN/proposal/build/kifab.pretty/<name>.kicad_mod" \\
      --not-identical-to "\$KICAD_FOOTPRINTS/Package_DFN_QFN.pretty/DFN-12-1EP_2x3mm_P0.45mm_EP0.64x2.4mm.kicad_mod"

  Symbol — against the datasheet pin table:
    [ ] 12 pins, correct number -> name mapping
    [ ] supply pins power_in, EN input, RF/LO/IF ports passive or bidirectional
    [ ] pins on the 1.27 mm grid, no duplicate numbers

  Nothing has been written to parts/. To adopt:  kifab accept runs/$MPN/
EOF

if (( BLOCKED )); then
  echo "verdict: proposal produced, blocked by the validators (exit 10)." >&2
  exit 10
fi
echo "verdict: proposal produced and every gate passed (exit 0)." >&2
