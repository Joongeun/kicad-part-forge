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

step "KiCad format conformance"
if [[ -x "$KICAD_CLI" ]]; then
  # `upgrade` rewrites in place, so run it on a copy: the gate must not mutate
  # the artefacts it is judging.
  scratch="$(mktemp -d)"
  trap 'rm -rf "$scratch"' EXIT
  cp -R build/. "$scratch/" 2>/dev/null || true

  shopt -s nullglob
  pretty_dirs=("$scratch"/*.pretty)
  if (( ${#pretty_dirs[@]} == 0 )); then
    warn "no generated libraries in build/ yet"
  else
    for dir in "${pretty_dirs[@]}"; do
      echo "    $(basename "$dir")"
      "$KICAD_CLI" fp upgrade --force "$dir" || fail=1
    done
  fi

  sym_libs=("$scratch"/*.kicad_sym)
  for lib in ${sym_libs[@]+"${sym_libs[@]}"}; do
    echo "    $(basename "$lib")"
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
