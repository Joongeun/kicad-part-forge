#!/usr/bin/env bash
# Does the *artefact* work?
#
# Every test in this repo runs against src/ on sys.path. That configuration
# cannot detect the classic packaging failure: a data file the code loads at
# runtime is present in the checkout and absent from the wheel. It works here
# and dies on the first `uvx kifab` in a stranger's terminal.
#
# So: build the wheel, install it into an empty venv, cd somewhere with no
# source tree in sight, and run real commands.
set -euo pipefail

cd "$(dirname "$0")/.."
repo="$PWD"

rm -rf dist
uv build >/dev/null
wheel="$(ls dist/*.whl)"
echo "==> built $wheel"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
uv venv --quiet "$work/venv"
py="$work/venv/bin/python"
uv pip install --quiet --python "$py" "$wheel"

# Outside the repo: nothing on sys.path can come from src/.
cd "$work"

echo "==> kifab --version"
"$work/venv/bin/kifab" --version

echo "==> the data files the package loads at runtime are actually in it"
"$py" - "$repo" <<'PY'
import pathlib, sys
import kifab
installed = pathlib.Path(kifab.__file__).parent
assert "site-packages" in str(installed), f"not testing the installed copy: {installed}"

shipped = {p.relative_to(installed).as_posix()
           for p in installed.rglob("*") if p.is_file() and p.suffix != ".py"
           and "__pycache__" not in p.parts}
source = pathlib.Path(sys.argv[1], "src", "kifab")
wanted = {p.relative_to(source).as_posix()
          for p in source.rglob("*") if p.is_file() and p.suffix != ".py"
          and "__pycache__" not in p.parts}
missing = sorted(wanted - shipped)
if missing:
    raise SystemExit("wheel is missing package data: " + ", ".join(missing))
print(f"    {len(shipped)} data file(s) present: {', '.join(sorted(shipped))}")

# Loaded, not merely present.
from kifab.ipc.rules import load_rules
assert load_rules(), "IPC-7351B rules loaded empty from the installed package"
from kifab.pcm import find_schema
schema = find_schema()
assert schema is not None and schema.is_file(), "PCM schema unreachable when installed"
print("    IPC rules and the PCM schema both load from site-packages")
PY

echo "==> the core install is SDK-free"
"$py" -c '
import importlib.util
if importlib.util.find_spec("anthropic") is not None:
    raise SystemExit("a plain `pip install kifab` pulled in the Anthropic SDK")
import kifab.llm  # must import without it
print("    kifab.llm imports with no anthropic installed")
'

echo "==> T2 cannot see the local library, in the installed package too"
"$py" -c '
import sys, kifab.generate
leaked = sorted(m for m in sys.modules if m.startswith(("kifab.index", "kifab.resolve")))
if leaked:
    raise SystemExit("T2 imported the local-library layer: " + ", ".join(leaked))
print("    T2 import graph is clean")
'

echo "==> kifab build (a real corpus, from outside the repo)"
"$work/venv/bin/kifab" build "$repo/parts" -o "$work/out"
test -f "$work/out/kifab.kicad_sym" || { echo "no symbol library written"; exit 1; }
compgen -G "$work/out/kifab.pretty/*.kicad_mod" >/dev/null || { echo "no footprints written"; exit 1; }

echo "==> kifab check"
KICAD_CLI="${KICAD_CLI:-/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli}" \
  "$work/venv/bin/kifab" check "$repo/parts" "$work/out" --strict

echo "==> kifab pcm, validated against the schema the wheel shipped"
uv pip install --quiet --python "$py" jsonschema
"$work/venv/bin/kifab" pcm "$work/out" -o "$work/site" \
  --base-url https://example.github.io/kicad-part-forge \
  --schema "$("$py" -c 'import pathlib, kifab; print(pathlib.Path(kifab.__file__).parent / "pcm.v1.schema.json")')"
test -f "$work/site/repository.json"

printf '\n\033[32mwheel smoke test passed\033[0m\n'
