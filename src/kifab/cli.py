"""`kifab` command line.

Thin on purpose: parse arguments, call `kifab.build`, print what happened. Any
logic that belongs to the pipeline belongs in a module, so it can be tested
without spawning a process.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .build import build, discover
from .index import Index, LibraryRoot, default_db_path, default_roots
from .ir import load_part
from .resolve import MatchSet, search
from .resolve.adopt import AdoptionError, adopt, to_yaml

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


def _open_index(args: argparse.Namespace) -> Index:
    return Index(Path(args.db) if getattr(args, "db", None) else None)


def _roots(args: argparse.Namespace) -> list[LibraryRoot] | None:
    if not getattr(args, "root", None):
        return None
    return [LibraryRoot(Path(p), "user") for p in args.root]


def _index_command(args: argparse.Namespace) -> int:
    index = _open_index(args)
    if args.status:
        counts = index.counts()
        print(f"index: {index.path}")
        print(f"  footprints: {counts['footprints']} in {counts['footprint_libraries']} libraries")
        print(f"  symbols:    {counts['symbols']} in {counts['symbol_libraries']} libraries")
        roots = index.get_meta("roots")
        if roots:
            for root in roots.split(":"):
                print(f"  root: {root}")
        return 0

    roots = _roots(args) or default_roots()
    if not roots:
        print(
            "kifab: no KiCad libraries found. Pass --root, or set "
            "KICAD9_FOOTPRINT_DIR / KICAD9_SYMBOL_DIR.",
            file=sys.stderr,
        )
        return 2
    for root in roots:
        print(f"    scanning {root.path} ({root.origin})", file=sys.stderr)
    stats = index.refresh(roots, rebuild=args.rebuild)
    counts = index.counts()
    print(
        f"kifab: {counts['footprints']} footprints + {counts['symbols']} symbols "
        f"indexed ({stats.footprints_added + stats.symbols_added} parsed, "
        f"{stats.unchanged_files} unchanged) -> {index.path}",
        file=sys.stderr,
    )
    return 0


def _ensure_populated(index: Index, args: argparse.Namespace) -> None:
    """Build on first use; otherwise just re-stat, which is sub-second."""
    counts = index.counts()
    if counts["footprints"] == 0 and counts["symbols"] == 0:
        print(
            "kifab: index is empty; building it now (one-off, ~1 minute)...",
            file=sys.stderr,
        )
        index.refresh(_roots(args))
    elif not getattr(args, "no_refresh", False):
        index.refresh(_roots(args))


def _print_matches(label: str, matches: MatchSet) -> None:
    print(f"\n{label}")
    if matches.confident:
        print("  CONFIDENT — safe to adopt:")
        for c in matches.confident:
            print(f"    {c.lib_id}  [{c.basis.value}] {c.reason}")
            if c.description:
                print(f"        {c.description[:100]}")
    else:
        print("  CONFIDENT — none. Package identity was not established.")
    if matches.review:
        print("  REVIEW — near misses, NOT verified; a human must judge these:")
        for c in matches.review:
            print(f"    {c.lib_id}  [{c.basis.value}] {c.reason}")
            for e in c.evidence:
                if e.verdict.value == "mismatch" and e.decisive:
                    print(f"        ! {e}  ({e.note or 'decisive'})")


def _search_command(args: argparse.Namespace) -> int:
    index = _open_index(args)
    _ensure_populated(index, args)
    result = search(index, args.query, args.package or "", limit=args.limit)
    if args.json:
        json.dump(result.to_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0 if (result.symbols.confident or result.footprints.confident) else 1

    print(f"query: {result.query!r}" + (f"  package: {args.package!r}" if args.package else ""))
    _print_matches("symbols", result.symbols)
    _print_matches("footprints", result.footprints)
    if not args.package:
        print(
            "\nnote: no --package given, so no footprint can be confirmed by "
            "package identity.\n      Pass the package from the datasheet, e.g. "
            "--package '12-Lead Plastic QFN (3mm x 2mm)'.",
        )
    if result.resolved:
        print("\nT0 resolved this part. Adopt it with `kifab adopt`.")
        return 0
    print("\nT0 did not resolve this part confidently.")
    return 1


def _lookup(index: Index, table: str, lib_id: str) -> tuple[str, str, str] | None:
    library, _, name = lib_id.partition(":")
    if not name:
        rows = index.db.execute(
            f"SELECT library, name, path FROM {table} WHERE name = ? COLLATE NOCASE",
            (lib_id,),
        ).fetchall()
    else:
        rows = index.db.execute(
            f"SELECT library, name, path FROM {table} "
            "WHERE library = ? AND name = ? COLLATE NOCASE",
            (library, name),
        ).fetchall()
    if not rows:
        return None
    return (rows[0]["library"], rows[0]["name"], rows[0]["path"])


def _adopt_command(args: argparse.Namespace) -> int:
    index = _open_index(args)
    _ensure_populated(index, args)

    footprint = _lookup(index, "footprint", args.footprint)
    if footprint is None:
        print(f"kifab: no indexed footprint {args.footprint!r}", file=sys.stderr)
        return 2
    symbol_path = symbol_name = None
    if args.symbol:
        found = _lookup(index, "symbol", args.symbol)
        if found is None:
            print(f"kifab: no indexed symbol {args.symbol!r}", file=sys.stderr)
            return 2
        _, symbol_name, symbol_path = found

    try:
        adoption = adopt(
            mpn=args.mpn or (symbol_name or footprint[1]),
            footprint_path=footprint[2],
            symbol_path=symbol_path,
            symbol_name=symbol_name,
            library=args.library,
            reference=args.reference,
            manufacturer=args.manufacturer,
            pins_from_pads=args.pins_from_pads,
        )
    except AdoptionError as exc:
        print(f"kifab: {exc}", file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{adoption.part.mpn}.yaml"
    if target.exists() and not args.force:
        print(f"kifab: {target} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    target.write_text(to_yaml(adoption), encoding="utf-8")
    print(target)
    for note in adoption.notes:
        print(f"    note: {note}", file=sys.stderr)
    print(
        f"kifab: adopted into {target}. Review it, then `kifab build`.",
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

    # -- T0: the local corpus ------------------------------------------
    index_parser = sub.add_parser(
        "index", help="build or refresh the index over the local KiCad libraries"
    )
    index_parser.add_argument(
        "--root",
        action="append",
        help="extra library root to scan (repeatable). Defaults to KiCad's "
        "shared libraries plus ~/Documents/KiCad.",
    )
    index_parser.add_argument(
        "--rebuild", action="store_true", help="discard the index and re-parse everything"
    )
    index_parser.add_argument(
        "--status", action="store_true", help="report what is indexed and exit"
    )
    index_parser.add_argument("--db", help=f"index location (default: {default_db_path()})")
    index_parser.set_defaults(func=_index_command)

    search_parser = sub.add_parser(
        "search",
        help="find an existing symbol/footprint in the local libraries (tier T0)",
        description="Results come back in two separate groups: CONFIDENT "
        "matches, where package identity was established, and REVIEW near "
        "misses, which are never safe to use unchecked.",
    )
    search_parser.add_argument("query", help="MPN or free text")
    search_parser.add_argument(
        "--package",
        help="package as the datasheet states it — 'QFN-12-1EP_2x3mm_P0.45mm' "
        "or '12-Lead Plastic QFN (3mm x 2mm), DWG 05-08-1985'. Without this no "
        "footprint can be confirmed by package identity.",
    )
    search_parser.add_argument("--limit", type=int, default=10)
    search_parser.add_argument("--json", action="store_true")
    search_parser.add_argument("--root", action="append", help=argparse.SUPPRESS)
    search_parser.add_argument(
        "--no-refresh", action="store_true", help="skip the on-disk freshness check"
    )
    search_parser.add_argument("--db", help=argparse.SUPPRESS)
    search_parser.set_defaults(func=_search_command)

    adopt_parser = sub.add_parser(
        "adopt",
        help="copy an existing part into this project as IR YAML",
        description="Writes parts/<MPN>.yaml so the reused part is correctable, "
        "buildable and validatable like any other. The symbol is re-laid-out in "
        "house style; pads are lifted verbatim into a `custom` package.",
    )
    adopt_parser.add_argument(
        "--footprint", required=True, help="LIBRARY:NAME of the footprint to adopt"
    )
    adopt_parser.add_argument("--symbol", help="LIBRARY:NAME of the symbol to adopt")
    adopt_parser.add_argument("--mpn", help="part number (default: the symbol's name)")
    adopt_parser.add_argument("--library", default="kifab")
    adopt_parser.add_argument("--reference", help="reference designator prefix")
    adopt_parser.add_argument("--manufacturer", default="")
    adopt_parser.add_argument(
        "--pins-from-pads",
        action="store_true",
        help="with no symbol, synthesise an explicitly-unverified pin stub from "
        "the pad numbers",
    )
    adopt_parser.add_argument("-o", "--out", default=str(DEFAULT_PARTS))
    adopt_parser.add_argument("--force", action="store_true")
    adopt_parser.add_argument("--no-refresh", action="store_true")
    adopt_parser.add_argument("--root", action="append", help=argparse.SUPPRESS)
    adopt_parser.add_argument("--db", help=argparse.SUPPRESS)
    adopt_parser.set_defaults(func=_adopt_command)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
