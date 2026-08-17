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

step "Test suite (IR, emitters, golden files, round-trip, IPC geometry, T2 isolation)"
uv run pytest -q || fail=1

step "Tier T2 must not be able to see the local library"
# Structural isolation, asserted here as well as in the suite because it is the
# single property that makes a generated part evidence rather than a guess. A
# fresh interpreter, so an earlier import cannot mask it.
uv run python -c '
import sys, kifab.generate
leaked = sorted(m for m in sys.modules if m.startswith(("kifab.index", "kifab.resolve")))
if leaked:
    raise SystemExit("T2 imported the local-library layer: " + ", ".join(leaked))
print("    T2 import graph is clean")
' || fail=1

step "Negative control: with no LLM, generation must refuse, not improvise"
# Uses a real, synthetic datasheet so the refusal comes from the *provider*,
# not from the PDF reader — a control that fails for the wrong reason proves
# nothing. Exit 3 is `kifab generate`'s "no model configured".
NEG="$(mktemp -d)"
PYTHONPATH=tests uv run python -c "
import pathlib, sys
from pdfs import datasheet
pathlib.Path(sys.argv[1], 'ds.pdf').write_bytes(datasheet())
" "$NEG"
set +e
uv run kifab generate NEGATIVE-CONTROL --datasheet "$NEG/ds.pdf" --provider none \
  --force-tier=generate --isolated --runs "$NEG/runs" >/dev/null 2>"$NEG/err"
neg=$?
set -e
if (( neg != 3 )); then
  echo "    FAILED: --provider none exited $neg, expected 3 (refusal)"
  fail=1
elif compgen -G "$NEG/runs/NEGATIVE-CONTROL/proposal/*.yaml" >/dev/null; then
  echo "    FAILED: --provider none wrote a part file anyway"
  fail=1
elif ! grep -q "will not guess pad geometry" "$NEG/err"; then
  echo "    FAILED: the refusal did not say why"
  fail=1
else
  echo "    refused loudly, wrote nothing, and said what to do instead"
fi
rm -rf "$NEG"

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

step "The built wheel, installed into an empty venv and actually run"
# Everything above this line runs with src/ on sys.path, which structurally
# cannot catch a data file that is present in the checkout and missing from the
# wheel. Set KIFAB_SKIP_WHEEL=1 to skip it in a tight edit loop; CI never does.
if [[ "${KIFAB_SKIP_WHEEL:-}" == "1" ]]; then
  warn "KIFAB_SKIP_WHEEL=1 — the packaged artefact was NOT tested"
else
  KICAD_CLI="$KICAD_CLI" ./scripts/wheel_smoke.sh >/dev/null || { ./scripts/wheel_smoke.sh; fail=1; }
  echo "    wheel builds, installs clean, and runs from outside the repo"
fi

if (( fail )); then
  printf '\n\033[31mFAILED\033[0m\n'
  exit 1
fi
printf '\n\033[32mAll checks passed\033[0m\n'
