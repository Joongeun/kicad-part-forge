"""T0 resolution, and the discrimination it exists to perform.

The headline case is the LTC5552. KiCad ships a DFN-12 with the *same* pin
count, the *same* 2x3 mm body, the *same* 0.45 mm pitch and the *same*
0.64 x 2.4 mm exposed pad as the part's UDB package — and it is a different
land pattern from a different mechanical drawing. Handing it back as a match
ships a wrong footprint that looks right.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import KICAD_SHARED, requires_kicad

from kifab.index import Index, LibraryRoot
from kifab.resolve import Basis, Confidence, search

DDB = "DFN-12-1EP_2x3mm_P0.45mm_EP0.64x2.4mm"
UDB = "QFN-12-1EP_2x3mm_P0.45mm_EP0.64x2.4mm"
DDB_ID = f"Package_DFN_QFN:{DDB}"
UDB_ID = f"Package_DFN_QFN:{UDB}"
SOIC_ID = "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"

#: How the LTC5552 datasheet states the UDB package.
UDB_DATASHEET = "12-Lead Plastic QFN (3mm x 2mm), LTC DWG # 05-08-1985"


@pytest.fixture
def index(tmp_path: Path, corpus: Path) -> Index:
    db = Index(tmp_path / "index.sqlite3")
    db.refresh([LibraryRoot(corpus, "user")])
    return db


# --------------------------------------------------------------------------
# The discrimination requirement
# --------------------------------------------------------------------------


def test_ltc5552_udb_never_confidently_returns_the_ddb_footprint(index: Index) -> None:
    """The blind-holdout trap, asserted directly.

    Searching for the LTC5552's UDB package must not produce the DDB footprint
    as a confident match. Producing it as a *reviewable near miss* is correct.
    """
    result = search(index, "LTC5552", UDB_DATASHEET, limit=20)

    confident = result.footprints.names(Confidence.CONFIDENT)
    assert DDB_ID not in confident, (
        "the DDB (DFN) footprint was returned as a confident match for a UDB "
        "(QFN) part — same body size, different package"
    )

    review = result.footprints.names(Confidence.REVIEW)
    assert DDB_ID in review, "the DDB footprint should still be offered for review"

    ddb = next(c for c in result.footprints.review if c.name == DDB)
    assert ddb.confidence is Confidence.REVIEW
    blocking = {
        e.attribute for e in ddb.evidence if e.decisive and e.verdict.value == "mismatch"
    }
    assert "family" in blocking
    assert "sides" in blocking


def test_the_distinction_is_in_the_data_structure_not_the_prose(index: Index) -> None:
    """Confident and review are separate lists; there is no combined accessor.

    A caller cannot take `results[0]` and get a near miss by accident. Reaching
    one requires naming `.review`.
    """
    result = search(index, "LTC5552", UDB_DATASHEET, limit=20)
    assert result.footprints.has_confident_match is False
    assert result.footprints.best is None
    assert result.footprints.review  # near misses are still delivered
    assert not hasattr(result.footprints, "results")
    assert result.resolved is False


def test_identical_geometry_with_a_wrong_frame_is_still_only_review(
    index: Index,
) -> None:
    """The sharpest form of the trap.

    Ask with a *fully specified* package whose pins, pitch, body, exposed pad
    and EP size all equal the DDB footprint's. Only the lead frame differs.
    """
    result = search(index, "LTC5552", UDB, limit=20)
    assert DDB_ID not in result.footprints.names(Confidence.CONFIDENT)
    ddb = next(c for c in result.footprints.review if c.name == DDB)
    agreed = {e.attribute for e in ddb.evidence if e.verdict.value == "match"}
    assert {"pins", "pitch", "body", "exposed_pad", "ep_size"} <= agreed
    assert ddb.agreements == 5


def test_the_real_udb_package_does_resolve_confidently(index: Index) -> None:
    """Discrimination must not degrade into refusing everything."""
    result = search(index, "LTC5552", UDB, limit=20)
    confident = result.footprints.names(Confidence.CONFIDENT)
    assert UDB_ID in confident
    winner = next(c for c in result.footprints.confident if c.name == UDB)
    assert winner.basis in (Basis.EXACT_NAME, Basis.PACKAGE_IDENTITY)


def test_the_udb_package_resolves_from_the_datasheet_phrase_alone(
    index: Index,
) -> None:
    """No KiCad-style name needed — the mechanical drawing number carries it."""
    result = search(index, "LTC5552", UDB_DATASHEET, limit=20)
    review = result.footprints.names(Confidence.REVIEW)
    confident = result.footprints.names(Confidence.CONFIDENT)
    # Pitch and exposed pad are simply not stated in the phrase, so identity
    # cannot be *established* — but the right part must at least be offered,
    # ranked above the wrong-family look-alike it shares a body size with.
    assert UDB_ID in confident or UDB_ID in review
    if UDB_ID in review:
        order = review.index(UDB_ID), review.index(DDB_ID)
        assert order[0] < order[1], "the true package must outrank the near miss"


# --------------------------------------------------------------------------
# Ordinary resolution
# --------------------------------------------------------------------------


def test_no_package_hint_means_no_confident_footprint_by_geometry(
    index: Index,
) -> None:
    """Without a stated package, geometry can confirm nothing. Say so."""
    result = search(index, "SOIC-8", limit=10)
    for candidate in result.footprints.confident:
        assert candidate.basis is not Basis.PACKAGE_IDENTITY


def test_a_symbols_own_footprint_property_is_a_confident_basis(index: Index) -> None:
    """KiCad's librarians already paired these; that pairing is evidence."""
    result = search(index, "TESTCHIP8Tx", limit=10)
    assert result.symbols.has_confident_match
    names = result.footprints.names(Confidence.CONFIDENT)
    assert SOIC_ID in names
    adopted = next(
        c for c in result.footprints.confident if c.name == "SOIC-8_3.9x4.9mm_P1.27mm"
    )
    assert adopted.basis is Basis.SYMBOL_PROVENANCE
    assert result.resolved


def test_provenance_loses_to_a_contradicting_package_hint(index: Index) -> None:
    """The caller said which package this part is, and it is not that one."""
    result = search(index, "TESTCHIP8Tx", UDB, limit=10)
    assert SOIC_ID not in result.footprints.names(Confidence.CONFIDENT)
    demoted = next(
        (c for c in result.footprints.review if c.name == "SOIC-8_3.9x4.9mm_P1.27mm"),
        None,
    )
    assert demoted is not None
    assert demoted.basis is Basis.SYMBOL_PROVENANCE
    assert "contradicts" in demoted.reason


def test_kicad_variant_wildcards_match_a_full_mpn(index: Index) -> None:
    result = search(index, "TESTCHIP8T6", limit=10)
    assert "MCU_Test:TESTCHIP8Tx" in result.symbols.names(Confidence.CONFIDENT)
    winner = result.symbols.best
    assert winner is not None and winner.basis is Basis.WILDCARD_NAME


def test_an_unrelated_query_resolves_nothing(index: Index) -> None:
    result = search(index, "NOT_A_REAL_PART_XYZ", limit=10)
    assert not result.symbols.confident
    assert not result.footprints.confident
    assert result.resolved is False


# --------------------------------------------------------------------------
# The same assertion, against the shipped library
# --------------------------------------------------------------------------


@requires_kicad
def test_ddb_discrimination_holds_on_the_real_kicad_library(tmp_path: Path) -> None:
    """Same claim, but over KiCad's own `Package_DFN_QFN.pretty` (729 items)."""
    index = Index(tmp_path / "index.sqlite3")
    index.refresh(
        [LibraryRoot(KICAD_SHARED / "footprints" / "Package_DFN_QFN.pretty", "kicad")]
    )
    assert index.counts()["footprints"] > 100

    result = search(index, "LTC5552", UDB_DATASHEET, limit=50)
    assert result.footprints.confident == []
    review = result.footprints.names(Confidence.REVIEW)
    assert DDB_ID in review

    ddb = next(c for c in result.footprints.review if c.name == DDB)
    blocking = {
        e.attribute for e in ddb.evidence if e.decisive and e.verdict.value == "mismatch"
    }
    assert {"family", "sides"} <= blocking
    # And it is ranked as the most confusable thing in the library, which is
    # exactly where a human needs to see it.
    assert result.footprints.review[0].name == DDB
