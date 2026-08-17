"""T0 — "this part already exists". The reuse-before-generate tier.

The local install ships 22,387 symbols and 15,179 footprints, and they are
KLC-clean. For a large fraction of parts the correct answer is to adopt one of
them: zero cost, zero geometry risk, better quality than anything we could
generate. That is what this module finds.

The one thing it must never do
------------------------------
Return a *wrong* part confidently. A footprint that is nearly right is worse
than no footprint at all, because it passes every eyeball check and fails on
the bench. The canonical example is the LTC5552: KiCad's
`DFN-12-1EP_2x3mm_P0.45mm_EP0.64x2.4mm` (Linear DWG 05-08-1723) has the same
pin count and the same body size as the part's UDB package (LTC DWG
05-08-1985) — and is a completely different land pattern.

So the result type has **two separate lists**, not one ranked list:

    resolution.footprints.confident   # safe to use
    resolution.footprints.review      # near misses, for a human to judge

There is deliberately no combined accessor. A caller cannot take `results[0]`
and get a near miss by accident; reaching a near miss requires naming
`.review`, which is the whole point.

How a footprint earns `confident`
---------------------------------
One of exactly three bases, and never a score threshold:

`EXACT_NAME`
    The caller named the footprint and it exists.
`SYMBOL_PROVENANCE`
    A confidently matched symbol's own `Footprint` property points at it —
    KiCad's librarians already paired them. Demoted to review if a stated
    package hint contradicts it.
`PACKAGE_IDENTITY`
    Every identity attribute (family, pin count, pitch, body, exposed pad,
    plus edge count and drawing number where known) was stated *and* agreed.
    See `kifab.index.package_id`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum

from ..index.package_id import Evidence, PackageIdentity, PackageMatch, compare
from ..index.store import Index, identity_of_row

#: Below this score a candidate is not even a near miss; it is dropped rather
#: than padding the review list with noise a human would have to wade through.
REVIEW_FLOOR = 1.0

#: ...but a candidate that agrees on this many identity attributes is a near
#: miss *however badly it scores otherwise*. The DFN-12 that shares the UDB
#: part's pin count and body size scores below zero once its wrong lead frame
#: is penalised, and it is precisely the result a human must be shown.
NEAR_MISS_AGREEMENTS = 2


class Confidence(str, Enum):
    """The two states a result can be in. There is no third."""

    CONFIDENT = "confident"
    REVIEW = "review"


class Basis(str, Enum):
    """*Why* a candidate is where it is. Reported, never inferred by the caller."""

    EXACT_NAME = "exact_name"
    WILDCARD_NAME = "wildcard_name"
    PACKAGE_IDENTITY = "package_identity"
    SYMBOL_PROVENANCE = "symbol_provenance"
    TEXT = "text"


@dataclass
class Candidate:
    """One indexed item, graded."""

    kind: str  # "symbol" | "footprint"
    library: str
    name: str
    path: str
    origin: str
    confidence: Confidence
    basis: Basis
    score: float
    reason: str
    description: str = ""
    evidence: tuple[Evidence, ...] = ()
    agreements: int = 0
    """Identity attributes that positively agree — how confusable this is."""

    pin_count: int | None = None
    footprint: str = ""
    datasheet: str = ""

    @property
    def lib_id(self) -> str:
        return f"{self.library}:{self.name}"

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "lib_id": self.lib_id,
            "library": self.library,
            "name": self.name,
            "path": self.path,
            "origin": self.origin,
            "confidence": self.confidence.value,
            "basis": self.basis.value,
            "score": round(self.score, 3),
            "agreements": self.agreements,
            "reason": self.reason,
            "description": self.description,
            "pin_count": self.pin_count,
            "footprint": self.footprint,
            "evidence": [
                {
                    "attribute": e.attribute,
                    "expected": e.expected,
                    "found": e.found,
                    "verdict": e.verdict.value,
                    "decisive": e.decisive,
                    "required": e.required,
                    "note": e.note,
                }
                for e in self.evidence
            ],
        }


@dataclass
class MatchSet:
    """Confident matches and near misses, kept apart on purpose."""

    confident: list[Candidate] = field(default_factory=list)
    review: list[Candidate] = field(default_factory=list)

    @property
    def has_confident_match(self) -> bool:
        return bool(self.confident)

    @property
    def best(self) -> Candidate | None:
        """The one safe answer, or None. Never falls through to a near miss."""
        return self.confident[0] if self.confident else None

    def names(self, confidence: Confidence) -> list[str]:
        source = self.confident if confidence is Confidence.CONFIDENT else self.review
        return [c.lib_id for c in source]

    def to_dict(self) -> dict:
        return {
            "confident": [c.to_dict() for c in self.confident],
            "review": [c.to_dict() for c in self.review],
        }


@dataclass
class LocalResolution:
    """What T0 found for one query."""

    query: str
    package_hint: str = ""
    package_query: PackageIdentity = field(default_factory=PackageIdentity)
    symbols: MatchSet = field(default_factory=MatchSet)
    footprints: MatchSet = field(default_factory=MatchSet)

    @property
    def resolved(self) -> bool:
        """True only when both halves of a part are confidently in hand."""
        return self.symbols.has_confident_match and self.footprints.has_confident_match

    def to_dict(self) -> dict:
        pq = self.package_query
        return {
            "query": self.query,
            "package_hint": self.package_hint,
            "package_query": {
                "family": pq.primary_family,
                "pins": pq.pad_count,
                "sides": pq.implied_sides,
                "pitch": pq.pitch,
                "body": list(pq.body) if pq.body else None,
                "exposed_pad": pq.exposed_pad,
                "ep_size": list(pq.ep_size) if pq.ep_size else None,
                "drawing": pq.drawing,
            },
            "symbols": self.symbols.to_dict(),
            "footprints": self.footprints.to_dict(),
            "resolved": self.resolved,
        }


# --------------------------------------------------------------------------
# Name matching
# --------------------------------------------------------------------------


def wildcard_equal(query: str, name: str) -> bool:
    """KiCad's own convention: `x` in a symbol name is a variant placeholder.

    `STM32F103C8Tx` covers `STM32F103C8T6` and `STM32F103C8T7`. This is a
    documented library convention, not a guess, so it earns a confident match.
    """
    if len(query) != len(name):
        return False
    for q, n in zip(query.upper(), name.upper()):
        if n == "X" or n == q:
            continue
        return False
    return True


def _fts_query(text: str) -> str:
    """Turn free text into an FTS5 prefix query, quoting every term."""
    terms = [t for t in "".join(c if c.isalnum() else " " for c in text).split() if t]
    return " OR ".join(f'"{t}"*' for t in terms)


# --------------------------------------------------------------------------
# The tier
# --------------------------------------------------------------------------


def search(
    index: Index,
    query: str,
    package_hint: str = "",
    *,
    limit: int = 10,
) -> LocalResolution:
    """Rank the local corpus for one MPN (and optional package hint)."""
    query = query.strip()
    package_query = PackageIdentity.parse(package_hint) if package_hint else PackageIdentity()
    result = LocalResolution(
        query=query, package_hint=package_hint, package_query=package_query
    )
    result.symbols = _search_symbols(index, query, limit)
    result.footprints = _search_footprints(
        index, query, package_query, result.symbols, limit
    )
    return result


def _search_symbols(index: Index, query: str, limit: int) -> MatchSet:
    out = MatchSet()
    seen: set[int] = set()
    rows: list[tuple[sqlite3.Row, Basis, float, str]] = []

    for row in index.db.execute(
        "SELECT * FROM symbol WHERE name = ? COLLATE NOCASE", (query,)
    ):
        rows.append((row, Basis.EXACT_NAME, 100.0, "symbol name equals the query"))

    if query:
        for row in index.db.execute(
            "SELECT * FROM symbol WHERE length(name) = ? AND name LIKE ?",
            (len(query), query[:1] + "%"),
        ):
            if row["id"] in {r[0]["id"] for r in rows}:
                continue
            if wildcard_equal(query, row["name"]):
                rows.append(
                    (
                        row,
                        Basis.WILDCARD_NAME,
                        90.0,
                        f"{row['name']!r} is KiCad's variant-wildcard form of the query",
                    )
                )

    fts = _fts_query(query)
    if fts:
        for row in index.db.execute(
            "SELECT s.*, bm25(symbol_fts) AS rank FROM symbol_fts "
            "JOIN symbol s ON s.id = symbol_fts.rowid "
            "WHERE symbol_fts MATCH ? ORDER BY rank LIMIT ?",
            (fts, limit * 5),
        ):
            rows.append((row, Basis.TEXT, max(0.0, 10.0 - abs(row["rank"])), "text match"))

    for row, basis, score, reason in rows:
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        confident = basis in (Basis.EXACT_NAME, Basis.WILDCARD_NAME)
        candidate = Candidate(
            kind="symbol",
            library=row["library"],
            name=row["name"],
            path=row["path"],
            origin=row["origin"],
            confidence=Confidence.CONFIDENT if confident else Confidence.REVIEW,
            basis=basis,
            score=score,
            reason=reason,
            description=row["description"],
            pin_count=row["pin_count"],
            footprint=row["footprint"],
            datasheet=row["datasheet"],
        )
        (out.confident if confident else out.review).append(candidate)

    out.confident.sort(key=lambda c: (-c.score, c.lib_id))
    out.review.sort(key=lambda c: (-c.score, c.lib_id))
    del out.review[limit:]
    return out


def _footprint_candidate_rows(
    index: Index, query: str, pq: PackageIdentity, provenance: Iterable[str]
) -> dict[int, sqlite3.Row]:
    """Pull everything worth grading.

    Deliberately loose: near misses are a *deliverable*, so the candidate set
    is generated on weak signals (same pin count, text hit, symbol provenance)
    and narrowed by `compare()`, not by SQL.
    """
    rows: dict[int, sqlite3.Row] = {}

    def add(iterable: Iterable[sqlite3.Row]) -> None:
        for row in iterable:
            rows[row["id"]] = row

    if pq.pad_count is not None:
        add(
            index.db.execute(
                "SELECT * FROM footprint WHERE pad_count = ?", (pq.pad_count,)
            )
        )
    if pq.source:
        add(
            index.db.execute(
                "SELECT * FROM footprint WHERE name = ? COLLATE NOCASE", (pq.source,)
            )
        )
        fts = _fts_query(pq.source)
        if fts:
            add(
                index.db.execute(
                    "SELECT f.* FROM footprint_fts JOIN footprint f "
                    "ON f.id = footprint_fts.rowid WHERE footprint_fts MATCH ? "
                    "ORDER BY bm25(footprint_fts) LIMIT 200",
                    (fts,),
                )
            )
    for lib_id in provenance:
        library, _, name = lib_id.partition(":")
        if not name:
            continue
        add(
            index.db.execute(
                "SELECT * FROM footprint WHERE library = ? AND name = ?",
                (library, name),
            )
        )
    if not rows and query:
        fts = _fts_query(query)
        if fts:
            add(
                index.db.execute(
                    "SELECT f.* FROM footprint_fts JOIN footprint f "
                    "ON f.id = footprint_fts.rowid WHERE footprint_fts MATCH ? "
                    "ORDER BY bm25(footprint_fts) LIMIT 50",
                    (fts,),
                )
            )
    return rows


def _search_footprints(
    index: Index,
    query: str,
    pq: PackageIdentity,
    symbols: MatchSet,
    limit: int,
) -> MatchSet:
    out = MatchSet()
    provenance = {
        c.footprint for c in symbols.confident if c.footprint and ":" in c.footprint
    }
    rows = _footprint_candidate_rows(index, query, pq, provenance)
    stated = pq.source != ""

    for row in rows.values():
        lib_id = f"{row['library']}:{row['name']}"
        identity = identity_of_row(row)
        match: PackageMatch | None = compare(pq, identity) if stated else None

        basis: Basis
        confident: bool
        reason: str

        if stated and pq.source.casefold() == row["name"].casefold():
            basis, confident = Basis.EXACT_NAME, True
            reason = "footprint name equals the stated package"
            score = 100.0
        elif lib_id in provenance:
            basis = Basis.SYMBOL_PROVENANCE
            score = 80.0
            if match is not None and match.blocking:
                # The librarian-curated pairing loses to a contradicting hint:
                # the caller told us which package this part is, and it is not
                # this one.
                confident = False
                reason = (
                    "named by a matched symbol, but contradicts the stated "
                    f"package — {match.reason()}"
                )
                score = 40.0 + match.score
            else:
                confident = True
                reason = "named by the Footprint property of a matched symbol"
        elif match is not None:
            basis = Basis.PACKAGE_IDENTITY
            confident = match.established
            reason = match.reason()
            score = match.score
        else:
            basis, confident = Basis.TEXT, False
            reason = "text match only — package identity was never established"
            score = 1.0

        agreements = match.agreements if match else 0
        if (
            not confident
            and score < REVIEW_FLOOR
            and agreements < NEAR_MISS_AGREEMENTS
        ):
            continue

        candidate = Candidate(
            kind="footprint",
            library=row["library"],
            name=row["name"],
            path=row["path"],
            origin=row["origin"],
            confidence=Confidence.CONFIDENT if confident else Confidence.REVIEW,
            basis=basis,
            score=score,
            reason=reason,
            description=row["descr"],
            evidence=match.evidence if match else (),
            agreements=agreements,
            pin_count=row["pad_count"],
        )
        (out.confident if confident else out.review).append(candidate)

    out.confident.sort(key=lambda c: (-c.score, c.lib_id))
    # Review is ranked by how *confusable* a candidate is, not by how well it
    # scores: the point of the list is to put the dangerous look-alikes in
    # front of a human, and penalties would bury exactly those.
    out.review.sort(key=lambda c: (-c.agreements, -c.score, c.lib_id))
    del out.review[limit:]
    return out
