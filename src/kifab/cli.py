"""`kifab` command line.

Thin on purpose: parse arguments, call `kifab.build`, print what happened. Any
logic that belongs to the pipeline belongs in a module, so it can be tested
without spawning a process.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .build import build, discover
from .ir import load_part

DEFAULT_PARTS = Path("parts")
DEFAULT_OUT = Path("build")


def _build_command(args: argparse.Namespace) -> int:
    sources = [Path(p) for p in args.parts] or [DEFAULT_PARTS]
    try:
        files = discover(sources)
    except FileNotFoundError as exc:
        print(f"kifab: {exc}", file=sys.stderr)
        return 2
    if not files:
        print(f"kifab: no part files found in {[str(s) for s in sources]}", file=sys.stderr)
        return 2

    parts = []
    failed = False
    for path in files:
        try:
            parts.append(load_part(path))
        except ValueError as exc:
            print(f"kifab: {exc}", file=sys.stderr)
            failed = True
    if failed:
        return 1

    try:
        result = build(parts, Path(args.out))
    except ValueError as exc:
        print(f"kifab: {exc}", file=sys.stderr)
        return 1

    for path in result.paths:
        print(path)
    print(
        f"kifab: built {len(parts)} part(s) into {args.out}",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kifab",
        description="Generate KiCad 9 symbols and footprints from part IR YAML.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build_parser = sub.add_parser(
        "build", help="emit .kicad_sym and .kicad_mod from part YAML"
    )
    build_parser.add_argument(
        "parts",
        nargs="*",
        help=f"part YAML files or directories (default: {DEFAULT_PARTS}/)",
    )
    build_parser.add_argument(
        "-o",
        "--out",
        default=str(DEFAULT_OUT),
        help=f"output directory (default: {DEFAULT_OUT}/)",
    )
    build_parser.set_defaults(func=_build_command)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
