"""The local-corpus index: SQLite + FTS5, incremental by file stamp.

Why an index at all
-------------------
The KiCad install on this machine ships **22,387 symbols and 15,179
footprints**, and the user's own projects add more. Parsing that corpus takes
tens of seconds; doing it per query would make search-first resolution too slow
to actually use, and a resolver nobody waits for is a resolver that gets
skipped in favour of generating a duplicate part.

Why incremental
---------------
Refreshing is keyed on `(mtime, size)` per file, so a rebuild after a KiCad
update re-parses only what changed. `refresh()` is safe to call on every
search; the steady-state cost is one `stat()` per file.

Why the package identity is stored in columns
---------------------------------------------
Family, edge count, pin count, pitch, body and exposed-pad size are extracted
once at index time and stored as *columns*, not recomputed from the name at
query time. That is what lets the resolver reject a near miss on measured
geometry instead of on a string.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .package_id import PackageIdentity
from .read import FootprintRecord, SymbolRecord, read_footprint, read_symbol_library

SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS footprint (
    id            INTEGER PRIMARY KEY,
    library       TEXT NOT NULL,
    name          TEXT NOT NULL,
    path          TEXT NOT NULL,
    lib_path      TEXT NOT NULL DEFAULT '',
    origin        TEXT NOT NULL,
    mtime         REAL NOT NULL,
    size          INTEGER NOT NULL,
    descr         TEXT NOT NULL DEFAULT '',
    tags          TEXT NOT NULL DEFAULT '',
    mount         TEXT NOT NULL DEFAULT '',
    primary_family TEXT,
    families      TEXT NOT NULL DEFAULT '',
    pad_count     INTEGER,
    side_count    INTEGER,
    pitch         REAL,
    body_x        REAL,
    body_y        REAL,
    exposed_pad   INTEGER,
    ep_x          REAL,
    ep_y          REAL,
    drawing       TEXT,
    UNIQUE(path)
);
CREATE INDEX IF NOT EXISTS footprint_name ON footprint(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS footprint_lib_path ON footprint(lib_path);
CREATE INDEX IF NOT EXISTS footprint_family ON footprint(primary_family, pad_count);

CREATE TABLE IF NOT EXISTS symbol (
    id          INTEGER PRIMARY KEY,
    library     TEXT NOT NULL,
    name        TEXT NOT NULL,
    path        TEXT NOT NULL,
    origin      TEXT NOT NULL,
    mtime       REAL NOT NULL,
    size        INTEGER NOT NULL,
    extends     TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    keywords    TEXT NOT NULL DEFAULT '',
    fp_filters  TEXT NOT NULL DEFAULT '',
    footprint   TEXT NOT NULL DEFAULT '',
    datasheet   TEXT NOT NULL DEFAULT '',
    reference   TEXT NOT NULL DEFAULT '',
    pin_count   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(path, name)
);
CREATE INDEX IF NOT EXISTS symbol_name ON symbol(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS symbol_path ON symbol(path);

CREATE VIRTUAL TABLE IF NOT EXISTS footprint_fts USING fts5(
    name, descr, tags, tokenize = "unicode61 tokenchars '-_.'"
);
CREATE VIRTUAL TABLE IF NOT EXISTS symbol_fts USING fts5(
    name, description, keywords, tokenize = "unicode61 tokenchars '-_.'"
);
"""


# --------------------------------------------------------------------------
# Where the libraries are
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LibraryRoot:
    """A directory to scan. `origin` distinguishes shipped libs from the user's."""

    path: Path
    origin: str  # "kicad" | "user"


def _shared_support() -> list[Path]:
    """Candidate KiCad shared-library locations, most specific first."""
    out: list[Path] = []
    for var in ("KICAD9_FOOTPRINT_DIR", "KICAD9_SYMBOL_DIR"):
        value = os.environ.get(var)
        if value:
            out.append(Path(value))
    if sys.platform == "darwin":
        out.append(Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport"))
    out += [
        Path("/usr/share/kicad"),
        Path("/usr/local/share/kicad"),
        Path("C:/Program Files/KiCad/9.0/share/kicad"),
    ]
    return out


def default_roots() -> list[LibraryRoot]:
    """KiCad's shared libraries plus the user's own project libraries."""
    roots: list[LibraryRoot] = []
    seen: set[Path] = set()

    def add(path: Path, origin: str) -> None:
        if path.is_dir() and path not in seen:
            seen.add(path)
            roots.append(LibraryRoot(path, origin))

    for base in _shared_support():
        add(base / "footprints", "kicad")
        add(base / "symbols", "kicad")
        # KICAD9_*_DIR point straight at the library directory.
        if base.name in ("footprints", "symbols"):
            add(base, "kicad")

    for candidate in (
        Path.home() / "Documents" / "KiCad",
        Path.home() / "KiCad",
        Path(os.environ.get("KIFAB_USER_LIBS", "")) if os.environ.get("KIFAB_USER_LIBS") else None,
    ):
        if candidate is not None:
            add(candidate, "user")
    return roots


_SKIP_DIRS = {".git", "__pycache__", "node_modules"}


def _walk_libraries(root: LibraryRoot) -> Iterator[tuple[str, Path]]:
    """Yield ("footprint_lib" | "symbol_lib", path) under a root.

    A `.pretty` directory is a footprint library; a `.kicad_sym` file is a
    symbol library. Backup directories are skipped: indexing them would offer
    stale duplicates of the user's own parts as search results.
    """
    # A root may *be* a library rather than contain one — pointing straight at
    # a `.pretty` directory is the natural way to index one library.
    if root.path.suffix == ".pretty" and root.path.is_dir():
        yield ("footprint_lib", root.path)
        return
    if root.path.suffix == ".kicad_sym" and root.path.is_file():
        yield ("symbol_lib", root.path)
        return

    stack = [root.path]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(".") or entry.name in _SKIP_DIRS:
                continue
            if entry.is_dir():
                if entry.name.endswith("-backups"):
                    continue
                if entry.suffix == ".pretty":
                    yield ("footprint_lib", entry)
                else:
                    stack.append(entry)
            elif entry.suffix == ".kicad_sym":
                yield ("symbol_lib", entry)


# --------------------------------------------------------------------------
# The index
# --------------------------------------------------------------------------


def default_db_path() -> Path:
    override = os.environ.get("KIFAB_INDEX")
    if override:
        return Path(override)
    cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(cache) if cache else Path.home() / ".cache"
    return base / "kifab" / "index.sqlite3"


@dataclass
class RefreshStats:
    """What a refresh actually did — printed so a slow run is explicable."""

    libraries: int = 0
    footprints_added: int = 0
    footprints_removed: int = 0
    symbols_added: int = 0
    symbols_removed: int = 0
    unchanged_files: int = 0

    @property
    def changed(self) -> bool:
        return bool(
            self.footprints_added
            or self.footprints_removed
            or self.symbols_added
            or self.symbols_removed
        )


class Index:
    """Read/write handle on the local-corpus index."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else default_db_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        # A rebuild writes ~37k rows plus two FTS indexes. The index is a
        # derived cache of files that still exist on disk, so trading crash
        # durability for build speed costs nothing we cannot regenerate.
        self.db.execute("PRAGMA synchronous = OFF")
        self.db.execute("PRAGMA journal_mode = MEMORY")
        self.db.executescript(_SCHEMA)
        stored = self.get_meta("schema_version")
        if stored is None:
            self.set_meta("schema_version", SCHEMA_VERSION)
        elif stored != SCHEMA_VERSION:
            self.reset()
        self.db.commit()

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Index:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def reset(self) -> None:
        """Drop everything. Used on schema change and by `--rebuild`."""
        for table in (
            "footprint",
            "symbol",
            "footprint_fts",
            "symbol_fts",
            "meta",
        ):
            self.db.execute(f"DELETE FROM {table}")
        self.set_meta("schema_version", SCHEMA_VERSION)
        self.db.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def counts(self) -> dict[str, int]:
        return {
            "footprints": self.db.execute(
                "SELECT count(*) c FROM footprint"
            ).fetchone()["c"],
            "symbols": self.db.execute("SELECT count(*) c FROM symbol").fetchone()["c"],
            "footprint_libraries": self.db.execute(
                "SELECT count(DISTINCT library) c FROM footprint"
            ).fetchone()["c"],
            "symbol_libraries": self.db.execute(
                "SELECT count(DISTINCT library) c FROM symbol"
            ).fetchone()["c"],
        }

    # -- refresh ---------------------------------------------------------

    def refresh(
        self, roots: Iterable[LibraryRoot] | None = None, *, rebuild: bool = False
    ) -> RefreshStats:
        """Bring the index in line with disk. Cheap when nothing changed."""
        if rebuild:
            self.reset()
        roots = list(roots) if roots is not None else default_roots()
        stats = RefreshStats()
        seen_libraries: set[tuple[str, str]] = set()

        for root in roots:
            for kind, lib_path in _walk_libraries(root):
                stats.libraries += 1
                library = lib_path.stem  # the nickname KiCad would show
                seen_libraries.add((kind, library))
                if kind == "footprint_lib":
                    self._refresh_footprint_library(lib_path, library, root.origin, stats)
                else:
                    self._refresh_symbol_library(lib_path, library, root.origin, stats)

        self.set_meta("roots", os.pathsep.join(str(r.path) for r in roots))
        self.db.commit()
        return stats

    def _refresh_footprint_library(
        self, lib_path: Path, library: str, origin: str, stats: RefreshStats
    ) -> None:
        on_disk: dict[str, os.stat_result] = {}
        try:
            for file in lib_path.iterdir():
                if file.suffix == ".kicad_mod":
                    try:
                        on_disk[file.name] = file.stat()
                    except OSError:
                        continue
        except OSError:
            return

        # Scoped by directory, not by nickname: two different `.pretty` dirs
        # can share a stem (a project library called `Package_SO.pretty` next
        # to KiCad's own), and keying on the nickname made each refresh delete
        # the other one's rows and re-add its own, forever.
        known = {
            row["path"]: (row["id"], row["mtime"], row["size"])
            for row in self.db.execute(
                "SELECT id, path, mtime, size FROM footprint WHERE lib_path = ?",
                (str(lib_path),),
            )
        }
        live_paths: set[str] = set()
        for filename, st in on_disk.items():
            path = lib_path / filename
            key = str(path)
            live_paths.add(key)
            existing = known.get(key)
            if existing and existing[1] == st.st_mtime and existing[2] == st.st_size:
                stats.unchanged_files += 1
                continue
            record = read_footprint(path, library, origin)
            if record is None:
                continue
            if existing:
                self._delete_footprint(existing[0])
            self._insert_footprint(record, str(lib_path), st.st_mtime, st.st_size)
            stats.footprints_added += 1

        for key, (row_id, _, _) in known.items():
            if key not in live_paths:
                self._delete_footprint(row_id)
                stats.footprints_removed += 1

    def _refresh_symbol_library(
        self, lib_path: Path, library: str, origin: str, stats: RefreshStats
    ) -> None:
        try:
            st = lib_path.stat()
        except OSError:
            return
        rows = self.db.execute(
            "SELECT id, mtime, size FROM symbol WHERE path = ?", (str(lib_path),)
        ).fetchall()
        if rows and rows[0]["mtime"] == st.st_mtime and rows[0]["size"] == st.st_size:
            stats.unchanged_files += 1
            return
        for row in rows:
            self._delete_symbol(row["id"])
            stats.symbols_removed += 1
        for record in read_symbol_library(lib_path, library, origin):
            self._insert_symbol(record, st.st_mtime, st.st_size)
            stats.symbols_added += 1

    # -- row writes ------------------------------------------------------

    def _insert_footprint(
        self, rec: FootprintRecord, lib_path: str, mtime: float, size: int
    ) -> None:
        i = rec.identity
        cur = self.db.execute(
            """INSERT OR REPLACE INTO footprint
               (library, name, path, lib_path, origin, mtime, size, descr, tags, mount,
                primary_family, families, pad_count, side_count, pitch,
                body_x, body_y, exposed_pad, ep_x, ep_y, drawing)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rec.library,
                rec.name,
                rec.path,
                lib_path,
                rec.origin,
                mtime,
                size,
                rec.descr,
                rec.tags,
                rec.mount,
                i.primary_family,
                " ".join(sorted(i.families)),
                i.pad_count,
                i.side_count,
                i.pitch,
                i.body[0] if i.body else None,
                i.body[1] if i.body else None,
                None if i.exposed_pad is None else int(i.exposed_pad),
                i.ep_size[0] if i.ep_size else None,
                i.ep_size[1] if i.ep_size else None,
                i.drawing,
            ),
        )
        self.db.execute(
            "INSERT INTO footprint_fts(rowid, name, descr, tags) VALUES (?,?,?,?)",
            (cur.lastrowid, rec.name, rec.descr, rec.tags),
        )

    def _delete_footprint(self, row_id: int) -> None:
        self.db.execute("DELETE FROM footprint_fts WHERE rowid = ?", (row_id,))
        self.db.execute("DELETE FROM footprint WHERE id = ?", (row_id,))

    def _insert_symbol(self, rec: SymbolRecord, mtime: float, size: int) -> None:
        cur = self.db.execute(
            """INSERT OR REPLACE INTO symbol
               (library, name, path, origin, mtime, size, extends, description,
                keywords, fp_filters, footprint, datasheet, reference, pin_count)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rec.library,
                rec.name,
                rec.path,
                rec.origin,
                mtime,
                size,
                rec.extends,
                rec.description,
                rec.keywords,
                rec.fp_filters,
                rec.footprint,
                rec.datasheet,
                rec.reference,
                rec.pin_count,
            ),
        )
        self.db.execute(
            "INSERT INTO symbol_fts(rowid, name, description, keywords) VALUES (?,?,?,?)",
            (cur.lastrowid, rec.name, rec.description, rec.keywords),
        )

    def _delete_symbol(self, row_id: int) -> None:
        self.db.execute("DELETE FROM symbol_fts WHERE rowid = ?", (row_id,))
        self.db.execute("DELETE FROM symbol WHERE id = ?", (row_id,))


def identity_of_row(row: sqlite3.Row) -> PackageIdentity:
    """Rebuild a `PackageIdentity` from an indexed footprint row."""
    body = None
    if row["body_x"] is not None and row["body_y"] is not None:
        body = (row["body_x"], row["body_y"])
    ep = None
    if row["ep_x"] is not None and row["ep_y"] is not None:
        ep = (row["ep_x"], row["ep_y"])
    families = row["families"].split() if row["families"] else []
    return PackageIdentity(
        primary_family=row["primary_family"],
        families=frozenset(families),
        pad_count=row["pad_count"],
        side_count=row["side_count"],
        pitch=row["pitch"],
        body=body,
        exposed_pad=None if row["exposed_pad"] is None else bool(row["exposed_pad"]),
        ep_size=ep,
        drawing=row["drawing"],
        source=row["name"],
    )
