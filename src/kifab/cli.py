"""`kifab` command line.

Thin on purpose: parse arguments, call `kifab.build`, print what happened. Any
logic that belongs to the pipeline belongs in a module, so it can be tested
without spawning a process.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .audit import audit_run, trace
from .build import build, discover
from .generate import GenerationError, GenerationRequest, generate
from .index import Index, LibraryRoot, default_db_path, default_roots
from .ir import load_part
from .partdb import (
    PartDbClient,
    PartDbError,
    SyncState,
    apply_plan,
    httplib_document,
    plan_sync,
    write_httplib,
)
from .partdb.sync import DEFAULT_CATEGORY, DEFAULT_STATE_PATH
from .llm import DEFAULT_PROVIDER, LLMUnavailable, ProviderError, make_provider
from .pdf.text import PdfError
from .review import ReviewError, accept
from .resolve import MatchSet, search
from .resolve.adopt import AdoptionError, adopt, to_yaml
from .resolve.easyeda import (
    DEFAULT_MODEL_VAR,
    EasyEdaClient,
    EasyEdaError,
    fetch_part,
    import_component,
    write_model,
)
from .resolve.easyeda import to_yaml as import_to_yaml
from .validate import Conformance, check_part, check_paths

DEFAULT_PARTS = Path("parts")
DEFAULT_OUT = Path("build")
DEFAULT_MODELS = Path("models")
DEFAULT_RUNS = Path("runs")


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


def _lcsc_command(args: argparse.Namespace) -> int:
    """T1 — import an LCSC/EasyEDA part into the IR.

    EasyEDA is an ingester: nothing it returns reaches the user's library
    directly. It lands in `parts/<MPN>.yaml`, from where our own emitters
    rebuild it in house style.
    """
    client = EasyEdaClient()
    models_dir = None if args.no_model else Path(args.models)

    try:
        if args.list:
            candidates = client.search(args.query, limit=args.limit)
            if not candidates:
                print(f"kifab: nothing on LCSC matched {args.query!r}", file=sys.stderr)
                return 1
            for candidate in candidates:
                print(candidate)
            return 0

        if args.payload:
            payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
            payload = payload.get("result", payload)
            imported = import_component(
                payload,
                library=args.library,
                bond_extra_pads=args.bond_extra_pads,
            )
            model = None
        else:
            imported, model = fetch_part(
                args.query,
                client=client,
                library=args.library,
                models_dir=models_dir,
                model_variable=args.model_var,
                bond_extra_pads=args.bond_extra_pads,
            )
    except EasyEdaError as exc:
        print(f"kifab: {exc}", file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{imported.part.mpn}.yaml"
    if target.exists() and not args.force:
        print(
            f"kifab: {target} already exists (use --force to overwrite)",
            file=sys.stderr,
        )
        return 1

    if model is not None and models_dir is not None:
        path = write_model(
            model, models_dir, args.library, imported.part.footprint.name
        )
        print(path)
        print(
            f"kifab: 3D model written to {path}. Point KiCad's "
            f"{args.model_var} path variable at {models_dir}/ "
            "(Preferences > Configure Paths).",
            file=sys.stderr,
        )

    target.write_text(import_to_yaml(imported), encoding="utf-8")
    print(target)
    for note in imported.notes:
        print(f"    note: {note}", file=sys.stderr)

    # An import that cannot pass the linter is not done. Run the same gate the
    # build runs, on the part we just wrote, and say so out loud.
    report = check_part(imported.part)
    text = report.format()
    if text:
        print(text, file=sys.stderr)
    print(
        f"kifab: imported LCSC {imported.lcsc} into {target} — "
        f"kifab check: {'OK' if report.ok() else 'FAILED'}, {report.summary()}. "
        "Review it, then `kifab build`.",
        file=sys.stderr,
    )
    return 0 if report.ok() else 1


def _generate_command(args: argparse.Namespace) -> int:
    """T2 — generate a part from its datasheet, into a review directory.

    This command cannot write to `parts/`. It stages a proposal under
    `runs/<mpn>/`; `kifab accept` is the separate, human-typed step that
    promotes it. See `kifab/review.py` for why that split is structural.
    """
    if args.force_tier not in (None, "generate"):
        print(
            f"kifab: --force-tier={args.force_tier} is not implemented; "
            "`kifab generate` is tier T2 and only understands 'generate'",
            file=sys.stderr,
        )
        return 2

    datasheet = Path(args.datasheet)
    if not datasheet.is_file():
        print(f"kifab: no datasheet at {datasheet}", file=sys.stderr)
        return 2

    run_dir = Path(args.runs) / args.mpn
    if run_dir.exists() and not args.force:
        print(
            f"kifab: {run_dir} already exists. Read it, or pass --force to "
            "start the run again.",
            file=sys.stderr,
        )
        return 2
    run_dir.mkdir(parents=True, exist_ok=True)

    conformance = None
    if not args.no_kicad_cli:
        conformance = Conformance.discover(args.kicad_cli)

    provider = make_provider(args.provider, run_dir=run_dir)

    # The whole input to T2. There is deliberately no third argument.
    request = GenerationRequest(mpn=args.mpn, datasheet=datasheet.read_bytes())

    try:
        proposal = generate(
            request,
            provider=provider,
            run_dir=run_dir,
            conformance=conformance,
            max_pages=args.max_pages,
        )
    except LLMUnavailable as exc:
        print(f"\nkifab: GENERATION REFUSED\n{exc}", file=sys.stderr)
        return 3
    except (GenerationError, ProviderError, PdfError) as exc:
        print(f"\nkifab: GENERATION FAILED\n{exc}", file=sys.stderr)
        return 1

    print(proposal.yaml_path)
    print(proposal.summary(), file=sys.stderr)
    text = proposal.report.format()
    if text:
        print(text, file=sys.stderr)

    audit = audit_run(run_dir)
    print(f"kifab audit: {'OK' if audit.ok() else 'FAILED'}", file=sys.stderr)
    if not audit.ok():
        print(audit.format(), file=sys.stderr)

    ok = proposal.report.ok() and audit.ok()
    print(
        "\nkifab: NOTHING HAS BEEN WRITTEN TO YOUR LIBRARY.\n"
        f"  review  {proposal.yaml_path}\n"
        + (f"  preview {proposal.svg_dir}\n" if proposal.svg_dir else "")
        + f"  audit   kifab audit {run_dir}\n"
        + f"  accept  kifab accept {run_dir}",
        file=sys.stderr,
    )
    return 0 if ok else 1


def _accept_command(args: argparse.Namespace) -> int:
    conformance = None if args.no_kicad_cli else Conformance.discover(args.kicad_cli)
    try:
        acceptance = accept(
            Path(args.run),
            parts_dir=Path(args.out),
            conformance=conformance,
            force=args.force,
            allow_unaudited=args.allow_unaudited,
        )
    except ReviewError as exc:
        print(f"kifab: NOT ACCEPTED\n{exc}", file=sys.stderr)
        return 1
    print(acceptance.target)
    print(
        f"kifab: accepted {acceptance.mpn} into {acceptance.target} "
        f"({acceptance.check.summary()}). Now `kifab build`.",
        file=sys.stderr,
    )
    return 0


def _audit_command(args: argparse.Namespace) -> int:
    run_dir = Path(args.run)
    report = audit_run(run_dir)
    if args.json:
        json.dump(report.to_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0 if report.ok() else 1

    print(f"transcript trace for {run_dir}:")
    print(trace(run_dir))
    text = report.format(verbose=True)
    if text:
        print(text)
    verdict = "OK" if report.ok() else "FAILED"
    print(
        f"kifab audit: {verdict} — {report.summary()}",
        file=sys.stderr,
    )
    return 0 if report.ok() else 1


# -- Part-DB: registration, and the file KiCad reads back --------------
#
# Direction of travel: `kifab sync` writes to Part-DB's REST API; KiCad reads
# from Part-DB's separate, read-only KiCad HTTP library API. Part-DB never
# supplies geometry, so nothing here can affect what a part *is* — only where
# the inventory says its symbol and footprint live.

ENV_URL = "PARTDB_URL"
ENV_TOKEN = "PARTDB_TOKEN"
ENV_TOKEN_FILE = "PARTDB_TOKEN_FILE"


def _partdb_url(args: argparse.Namespace) -> str:
    url = args.url or os.environ.get(ENV_URL, "")
    if not url:
        raise PartDbError(
            f"no Part-DB URL. Pass --url https://host, or set ${ENV_URL}."
        )
    return url


def _partdb_token(args: argparse.Namespace) -> str:
    """Token from the flag, a file, or the environment — in that order.

    A token is a credential, so `--token-file` and the environment exist to
    keep it out of shell history and out of `ps`.
    """
    if getattr(args, "token", None):
        return str(args.token)
    path = getattr(args, "token_file", None) or os.environ.get(ENV_TOKEN_FILE, "")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise PartDbError(f"could not read the token file {path}: {exc}") from exc
    token = os.environ.get(ENV_TOKEN, "")
    if not token:
        raise PartDbError(
            f"no Part-DB API token. Pass --token, --token-file, or set ${ENV_TOKEN}.\n"
            "  Create one in Part-DB: User Settings > API tokens > Create. The "
            "token needs the 'Edit' scope, and the user it belongs to needs the "
            "'Access the API' permission (Permissions > API)."
        )
    return token


def _sync_command(args: argparse.Namespace) -> int:
    """Reconcile `parts/` with the Part-DB inventory.

    Reads `parts/`, never `runs/`: a proposal that has not been through the
    review gate is not a part, and registering one would put an unreviewed
    symbol id in front of every user of the inventory.
    """
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
    for path in files:
        try:
            parts.append(load_part(path))
        except ValueError as exc:
            print(f"kifab: {exc}", file=sys.stderr)
            return 1

    try:
        client = PartDbClient(_partdb_url(args), _partdb_token(args))
        state = SyncState.load(args.state)
        plan = plan_sync(client, parts, state, force=args.force)
        if not args.dry_run:
            apply_plan(client, plan, state, category=args.category)
    except PartDbError as exc:
        print(f"kifab: {exc}", file=sys.stderr)
        return 1

    if args.json:
        json.dump(plan.to_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0 if plan.ok() else 1

    text = plan.format()
    if text:
        print(text)
    verdict = "OK" if plan.ok() else "BLOCKED"
    print(
        f"kifab sync: {verdict} — {plan.summary()}"
        + (" (dry run: nothing was written)" if args.dry_run else ""),
        file=sys.stderr,
    )
    if not args.dry_run and plan.writes:
        print(
            f"    recorded what was written in {state.path} — commit it, so "
            "another machine reconciles instead of duplicating.",
            file=sys.stderr,
        )
    return 0 if plan.ok() else 1


def _httplib_command(args: argparse.Namespace) -> int:
    """Write the `.kicad_httplib` KiCad needs to read Part-DB."""
    try:
        document = httplib_document(
            _partdb_url(args),
            _partdb_token(args),
            name=args.name,
            description=args.description,
            locale=args.locale,
        )
    except (PartDbError, ValueError) as exc:
        print(f"kifab: {exc}", file=sys.stderr)
        return 2

    if args.stdout:
        json.dump(document, sys.stdout, indent=4)
        sys.stdout.write("\n")
        return 0

    path = write_httplib(args.out, document)
    print(path)
    print(
        f"kifab: wrote {path} (mode 0600 — it contains a live API token; do "
        "not commit it).\n"
        "  In KiCad: Preferences > Manage Symbol Libraries > Add (folder icon) "
        f"> pick {path.name}.\n"
        f"  It serves {document['source']['root_url']}"
        f"{document['source']['api_version']}/ — parts appear there only once "
        "`kifab sync` has given them a KiCad symbol.",
        file=sys.stderr,
    )
    return 0


def _check_command(args: argparse.Namespace) -> int:
    targets = [Path(p) for p in args.targets] or [DEFAULT_PARTS]
    conformance = None
    if not args.no_kicad_cli:
        conformance = Conformance.discover(args.kicad_cli)
        if not conformance.available:
            print(
                "kifab: kicad-cli not found; the format conformance gate will "
                "be reported as skipped (set KICAD_CLI to override)",
                file=sys.stderr,
            )

    report = check_paths(targets, conformance=conformance)

    if args.json:
        json.dump(report.to_dict(strict=args.strict), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0 if report.ok(strict=args.strict) else 1

    text = report.format(verbose=args.verbose)
    if text:
        print(text)
    verdict = "OK" if report.ok(strict=args.strict) else "FAILED"
    print(
        f"kifab check: {verdict} — {report.summary()}"
        + (" (strict: warnings block)" if args.strict else ""),
        file=sys.stderr,
    )
    return 0 if report.ok(strict=args.strict) else 1


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

    check_parser = sub.add_parser(
        "check",
        help="validate parts, libraries or footprints",
        description="Runs every validator: IR schema lint, geometry sanity, "
        "KLC conventions and the kicad-cli format gate. Accepts part YAML, a "
        ".kicad_sym, a .kicad_mod, a .pretty directory or a tree containing "
        "any of them. Errors block (exit 1); warnings do not, unless --strict.",
    )
    check_parser.add_argument(
        "targets",
        nargs="*",
        help=f"what to check (default: {DEFAULT_PARTS}/)",
    )
    check_parser.add_argument(
        "--strict",
        action="store_true",
        help="treat warnings as blocking too",
    )
    check_parser.add_argument(
        "--json", action="store_true", help="machine-readable output for CI"
    )
    check_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="also print informational findings, including skipped checks",
    )
    check_parser.add_argument(
        "--kicad-cli", help="path to kicad-cli (default: $KICAD_CLI, then PATH)"
    )
    check_parser.add_argument(
        "--no-kicad-cli",
        action="store_true",
        help="skip the format conformance gate entirely",
    )
    check_parser.set_defaults(func=_check_command)

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

    # -- T1: LCSC / EasyEDA --------------------------------------------
    lcsc_parser = sub.add_parser(
        "lcsc",
        help="import a part from LCSC/EasyEDA into this project as IR YAML (tier T1)",
        description="EasyEDA is an ingester, not a generator: its data is "
        "normalised into the Part IR and re-emitted by kifab's own emitters in "
        "house style, so an imported part is linted, restyled and correctable "
        "like any other. Writes parts/<MPN>.yaml and, when EasyEDA has one, a "
        "STEP model. Anything that could not be normalised provably is written "
        "into the file as a NOTE rather than guessed at.",
    )
    lcsc_parser.add_argument(
        "query", help="an LCSC code (C2040) or an exact MPN (RP2040)"
    )
    lcsc_parser.add_argument(
        "--list",
        action="store_true",
        help="list the LCSC candidates for this query and stop",
    )
    lcsc_parser.add_argument("--limit", type=int, default=8)
    lcsc_parser.add_argument("--library", default="kifab")
    lcsc_parser.add_argument("-o", "--out", default=str(DEFAULT_PARTS))
    lcsc_parser.add_argument("--force", action="store_true")
    lcsc_parser.add_argument(
        "--models",
        default=str(DEFAULT_MODELS),
        help=f"where to write fetched 3D models (default: {DEFAULT_MODELS}/)",
    )
    lcsc_parser.add_argument(
        "--model-var",
        default=DEFAULT_MODEL_VAR,
        help="KiCad path variable the model reference is written relative to "
        f"(default: {DEFAULT_MODEL_VAR})",
    )
    lcsc_parser.add_argument(
        "--no-model", action="store_true", help="do not fetch a 3D model"
    )
    lcsc_parser.add_argument(
        "--bond-extra-pads",
        action="store_true",
        help="when EasyEDA's footprint has pads its symbol has no pin for, "
        "synthesise explicitly-unverified pins so the netlist can reach them",
    )
    lcsc_parser.add_argument(
        "--payload",
        help="import from a saved EasyEDA component JSON instead of the network",
    )
    lcsc_parser.set_defaults(func=_lcsc_command)

    # -- T2: generate from the datasheet, behind a review gate -----------
    gen_parser = sub.add_parser(
        "generate",
        help="generate a part from its datasheet PDF (tier T2, needs an LLM)",
        description="Selects the pin-table and mechanical-drawing pages "
        "locally (free, deterministic), sends only those to the configured "
        "provider, and stages the result under runs/<MPN>/ as a proposal. "
        "It CANNOT write to your library: `kifab accept` is a separate step. "
        "With --provider none this command fails loudly rather than emitting "
        "something plausible.",
    )
    gen_parser.add_argument("mpn", help="manufacturer part number")
    gen_parser.add_argument(
        "--datasheet", required=True, help="path to the datasheet PDF"
    )
    gen_parser.add_argument(
        "--provider",
        default=DEFAULT_PROVIDER,
        choices=["claude-code", "api-key", "none"],
        help=f"which LLM provider to use (default: {DEFAULT_PROVIDER})",
    )
    gen_parser.add_argument(
        "--force-tier",
        choices=["generate"],
        help="pin the resolver to this tier. `generate` is the only value, "
        "and it is what the blind-holdout test uses: no local search, no "
        "LCSC, datasheet only.",
    )
    gen_parser.add_argument(
        "--isolated",
        action="store_true",
        help="documentation of intent; isolation is structural and always on "
        "(the T2 code path has no access to the library index).",
    )
    gen_parser.add_argument(
        "--runs", default=str(DEFAULT_RUNS), help=f"run directory (default: {DEFAULT_RUNS}/)"
    )
    gen_parser.add_argument("--max-pages", type=int, help="cap on pages sent")
    gen_parser.add_argument("--force", action="store_true", help="reuse an existing run dir")
    gen_parser.add_argument("--kicad-cli", help=argparse.SUPPRESS)
    gen_parser.add_argument("--no-kicad-cli", action="store_true")
    gen_parser.set_defaults(func=_generate_command)

    accept_parser = sub.add_parser(
        "accept",
        help="promote a reviewed proposal into parts/ (the review gate)",
        description="The only command that writes a generated part into your "
        "parts directory. It re-runs every validator and the transcript "
        "audit first, and refuses on any error.",
    )
    accept_parser.add_argument("run", help="the runs/<MPN>/ directory to accept")
    accept_parser.add_argument("-o", "--out", default=str(DEFAULT_PARTS))
    accept_parser.add_argument("--force", action="store_true")
    accept_parser.add_argument(
        "--allow-unaudited",
        action="store_true",
        help="accept a run whose transcript is missing. Never use this to "
        "wave through an audit that actually failed.",
    )
    accept_parser.add_argument("--kicad-cli", help=argparse.SUPPRESS)
    accept_parser.add_argument("--no-kicad-cli", action="store_true")
    accept_parser.set_defaults(func=_accept_command)

    audit_parser = sub.add_parser(
        "audit",
        help="prove a generation run never looked at an existing part",
        description="Reads runs/<MPN>/transcript.jsonl and asserts it contains "
        "no read of a .kicad_mod/.kicad_sym, no fetch from a library "
        "aggregator, and no successful access outside the run's own scratch "
        "directory. Prints the tool-call trace so you can read what the model "
        "did rather than what it says it did.",
    )
    audit_parser.add_argument("run", help="the runs/<MPN>/ directory to audit")
    audit_parser.add_argument("--json", action="store_true")
    audit_parser.set_defaults(func=_audit_command)

    # -- Part-DB: registration downstream of the library -----------------
    sync_parser = sub.add_parser(
        "sync",
        help="register parts/ in Part-DB so KiCad's HTTP library can see them",
        description="Reconciles parts/ against a Part-DB inventory over its "
        "REST API. Idempotent: a second run of an unchanged library issues no "
        "writes at all. Parts are identified by MPN, so a row somebody already "
        "created by hand is updated rather than duplicated. kifab writes only "
        "the four EDA fields (KiCad symbol, KiCad footprint, reference prefix, "
        "value); stock, storage, price and supplier data are never touched. If "
        "somebody changed one of those four in Part-DB, that is reported as a "
        "conflict and left alone.",
    )
    sync_parser.add_argument(
        "parts",
        nargs="*",
        help=f"part YAML files or directories (default: {DEFAULT_PARTS}/). "
        "Never runs/ — an unreviewed proposal is not a part.",
    )
    sync_parser.add_argument("--url", help=f"Part-DB base URL (default: ${ENV_URL})")
    sync_parser.add_argument("--token", help=f"API token (default: ${ENV_TOKEN})")
    sync_parser.add_argument(
        "--token-file", help=f"read the API token from a file (default: ${ENV_TOKEN_FILE})"
    )
    sync_parser.add_argument(
        "--state",
        default=str(DEFAULT_STATE_PATH),
        help="record of what kifab last wrote, used to tell our own changes "
        f"from somebody else's (default: {DEFAULT_STATE_PATH}). Commit it.",
    )
    sync_parser.add_argument(
        "--category",
        default=DEFAULT_CATEGORY,
        help="Part-DB category for parts kifab creates; made if absent "
        f"(default: {DEFAULT_CATEGORY!r})",
    )
    sync_parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="print the plan and perform no writes",
    )
    sync_parser.add_argument(
        "--force",
        action="store_true",
        help="resolve conflicts in favour of parts/, overwriting what somebody "
        "set in Part-DB. Read the dry run first.",
    )
    sync_parser.add_argument("--json", action="store_true")
    sync_parser.set_defaults(func=_sync_command)

    httplib_parser = sub.add_parser(
        "httplib",
        help="write the .kicad_httplib that points KiCad at Part-DB",
        description="Generates KiCad's HTTP library descriptor. This is the "
        "read side: KiCad fetches symbol ids and field values from Part-DB "
        "through it. It contains a live API token, so the file is written "
        "0600 and must not be committed.",
    )
    httplib_parser.add_argument("--url", help=f"Part-DB base URL (default: ${ENV_URL})")
    httplib_parser.add_argument("--token", help=f"API token (default: ${ENV_TOKEN})")
    httplib_parser.add_argument("--token-file", help=f"(default: ${ENV_TOKEN_FILE})")
    httplib_parser.add_argument(
        "-o", "--out", default="partdb.kicad_httplib", help="where to write it"
    )
    httplib_parser.add_argument("--name", default="Part-DB")
    httplib_parser.add_argument(
        "--description", default="Parts registered by kifab, served from Part-DB"
    )
    httplib_parser.add_argument(
        "--locale",
        default="en",
        help="Part-DB serves the KiCad API under a locale prefix (default: en)",
    )
    httplib_parser.add_argument(
        "--stdout", action="store_true", help="print the JSON instead of writing it"
    )
    httplib_parser.set_defaults(func=_httplib_command)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
