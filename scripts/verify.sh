#!/usr/bin/env bash
# The one command. Everything must pass before any work is called done.
#
# Usage: ./scripts/verify.sh
set -euo pipefail

cd "$(dirname "$0")/.."

KICAD_CLI="${KICAD_CLI:-/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli}"

fail=0
step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
warn() { printf '\033[33m    skip: %s\033[0m\n' "$1"; }

step "Build the part corpus"
uv run kifab build parts/ -o build || fail=1

step "Test suite (IR, emitters, golden files, S-expression round-trip, IPC geometry)"
uv run pytest -q || fail=1

step "Validators: schema lint, geometry sanity, KLC, KiCad format conformance"
# `kifab check` owns the kicad-cli round-trip gate now (src/kifab/validate/
# roundtrip.py), so the same code path runs here, in the tests and per part
# from the CLI. It works on a copy internally: `upgrade` rewrites in place and
# the gate must never mutate the artefacts it is judging.
if [[ ! -x "$KICAD_CLI" ]]; then
  warn "kicad-cli not found at $KICAD_CLI (set KICAD_CLI to override); the"
  warn "format conformance gate will report itself as skipped, not as passed"
fi
# --strict: for our own corpus a warning is a defect too. Third-party files
# checked ad hoc are the case --strict is *not* for.
KICAD_CLI="$KICAD_CLI" uv run kifab check parts/ build/ --strict || fail=1

if (( fail )); then
  printf '\n\033[31mFAILED\033[0m\n'
  exit 1
fi
printf '\n\033[32mAll checks passed\033[0m\n'
