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

step "Test suite (S-expression round-trip, IPC geometry)"
uv run pytest -q || fail=1

step "KiCad format conformance"
if [[ -x "$KICAD_CLI" ]]; then
  # Every generated footprint must survive KiCad's own parser. `fp upgrade`
  # exits non-zero on a parse error, which is the conformance gate.
  shopt -s nullglob
  pretty_dirs=(build/*.pretty)
  if (( ${#pretty_dirs[@]} == 0 )); then
    warn "no generated libraries in build/ yet"
  else
    for dir in "${pretty_dirs[@]}"; do
      echo "    $dir"
      "$KICAD_CLI" fp upgrade --force "$dir" || fail=1
    done
  fi

  sym_libs=(build/*.kicad_sym)
  for lib in ${sym_libs[@]+"${sym_libs[@]}"}; do
    echo "    $lib"
    "$KICAD_CLI" sym upgrade --force "$lib" || fail=1
  done
else
  warn "kicad-cli not found at $KICAD_CLI (set KICAD_CLI to override)"
fi

if (( fail )); then
  printf '\n\033[31mFAILED\033[0m\n'
  exit 1
fi
printf '\n\033[32mAll checks passed\033[0m\n'
