"""The local-corpus index: what it extracts, and that refreshing is incremental."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import (
    KICAD_SHARED,
    dual_row_pads,
    quad_pads,
    requires_kicad,
    write_footprint,
)

from kifab.index import Index, LibraryRoot, identity_of_row, read_footprint


def _index(tmp_path: Path, corpus: Path) -> Index:
    index = Index(tmp_path / "index.sqlite3")
    index.refresh([LibraryRoot(corpus, "user")])
    return index


def test_indexes_footprints_and_symbols(tmp_path: Path, corpus: Path) -> None:
    index = _index(tmp_path, corpus)
    counts = index.counts()
    assert counts["footprints"] == 4
    assert counts["symbols"] == 2
    assert counts["footprint_libraries"] == 2


def test_package_identity_is_stored_as_columns(tmp_path: Path, corpus: Path) -> None:
    """Identity is measured once at index time, not re-derived per query."""
    index = _index(tmp_path, corpus)
    row = index.db.execute(
        "SELECT * FROM footprint WHERE name = ?",
        ("DFN-12-1EP_2x3mm_P0.45mm_EP0.64x2.4mm",),
    ).fetchone()
    assert row["primary_family"] == "DFN"
    assert row["pad_count"] == 12
    assert row["side_count"] == 2
    assert row["pitch"] == pytest.approx(0.45)
    assert (row["body_x"], row["body_y"]) == (2.0, 3.0)
    assert row["exposed_pad"] == 1
    assert (row["ep_x"], row["ep_y"]) == (0.64, 2.4)

    identity = identity_of_row(row)
    assert identity.primary_family == "DFN"
    assert identity.implied_sides == 2


def test_symbol_metadata_and_pin_count(tmp_path: Path, corpus: Path) -> None:
    index = _index(tmp_path, corpus)
    row = index.db.execute(
        "SELECT * FROM symbol WHERE name = ?", ("TESTCHIP8Tx",)
    ).fetchone()
    assert row["pin_count"] == 8
    assert row["footprint"] == "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"
    assert "eight-pin" in row["description"]


# --------------------------------------------------------------------------
# Incremental refresh — the property that makes a 37k-item index usable
# --------------------------------------------------------------------------


def test_refresh_reparses_nothing_when_nothing_changed(
    tmp_path: Path, corpus: Path
) -> None:
    index = _index(tmp_path, corpus)
    stats = index.refresh([LibraryRoot(corpus, "user")])
    assert stats.changed is False
    assert stats.footprints_added == 0
    assert stats.symbols_added == 0
    assert stats.unchanged_files == 5  # 4 footprints + 1 symbol library


def test_refresh_picks_up_an_added_and_a_deleted_footprint(
    tmp_path: Path, corpus: Path
) -> None:
    index = _index(tmp_path, corpus)
    pretty = corpus / "Package_DFN_QFN.pretty"
    write_footprint(
        pretty, "QFN-16-1EP_3x3mm_P0.5mm", quad_pads(4, 0.5, 1.4), body=(3.0, 3.0)
    )
    stats = index.refresh([LibraryRoot(corpus, "user")])
    assert stats.footprints_added == 1
    assert index.counts()["footprints"] == 5

    (pretty / "QFN-16-1EP_3x3mm_P0.5mm.kicad_mod").unlink()
    stats = index.refresh([LibraryRoot(corpus, "user")])
    assert stats.footprints_removed == 1
    assert index.counts()["footprints"] == 4


def test_two_libraries_with_the_same_nickname_do_not_evict_each_other(
    tmp_path: Path, corpus: Path
) -> None:
    """A project library called `Package_SO.pretty` next to KiCad's own.

    Keying refresh on the nickname made each pass delete the other directory's
    rows and re-add its own, so the index never converged and every search paid
    a full re-parse.
    """
    other = tmp_path / "project" / "Package_SO.pretty"
    write_footprint(
        other,
        "SOIC-8_MyVariant",
        dual_row_pads(8, 1.27, 2.475, w=1.95, h=0.6),
        body=(3.9, 4.9),
    )
    roots = [LibraryRoot(corpus, "user"), LibraryRoot(tmp_path / "project", "user")]
    index = Index(tmp_path / "index.sqlite3")
    index.refresh(roots)
    assert index.counts()["footprints"] == 5

    stats = index.refresh(roots)
    assert stats.changed is False
    assert index.counts()["footprints"] == 5


def test_a_changed_file_is_reparsed(tmp_path: Path, corpus: Path) -> None:
    index = _index(tmp_path, corpus)
    target = corpus / "Package_DFN_QFN.pretty" / "QFN-12-1EP_3x3mm_P0.5mm_EP1.6x1.6mm.kicad_mod"
    write_footprint(
        target.parent,
        "QFN-12-1EP_3x3mm_P0.5mm_EP1.6x1.6mm",
        quad_pads(3, 0.5, 1.4) + [("13", 0.0, 0.0, 2.0, 2.0)],
        descr="edited",
        body=(3.0, 3.0),
    )
    import os

    os.utime(target, (0, 0))  # force a stamp change even on a fast filesystem
    stats = index.refresh([LibraryRoot(corpus, "user")])
    assert stats.footprints_added == 1
    row = index.db.execute(
        "SELECT ep_x, ep_y FROM footprint WHERE name = ?",
        ("QFN-12-1EP_3x3mm_P0.5mm_EP1.6x1.6mm",),
    ).fetchone()
    assert (row["ep_x"], row["ep_y"]) == (2.0, 2.0)


def test_backup_directories_are_not_indexed(tmp_path: Path, corpus: Path) -> None:
    """A project's `-backups` folder holds stale copies of the user's own parts."""
    write_footprint(
        corpus / "proj-backups" / "Old.pretty",
        "STALE-8_3.9x4.9mm_P1.27mm",
        dual_row_pads(8, 1.27, 2.475),
    )
    index = _index(tmp_path, corpus)
    assert index.counts()["footprints"] == 4


# --------------------------------------------------------------------------
# Against the real install
# --------------------------------------------------------------------------


@requires_kicad
def test_reads_the_real_ddb_footprint_correctly() -> None:
    """The exact file that makes this whole tier dangerous, measured.

    Note the two unnumbered paste apertures over the exposed pad: counted as
    lands they make this 12-pin, two-sided DFN measure as a 14-pin, four-sided
    package — which is to say, as a QFN.
    """
    path = (
        KICAD_SHARED
        / "footprints/Package_DFN_QFN.pretty"
        / "DFN-12-1EP_2x3mm_P0.45mm_EP0.64x2.4mm.kicad_mod"
    )
    record = read_footprint(path, "Package_DFN_QFN", "kicad")
    assert record is not None
    identity = record.identity
    assert identity.primary_family == "DFN"
    assert identity.pad_count == 12
    assert identity.side_count == 2
    assert identity.pitch == pytest.approx(0.45)
    assert identity.body == (2.0, 3.0)
    assert identity.exposed_pad is True
    assert identity.ep_size == (0.64, 2.4)
    assert "DDB" in record.descr


@requires_kicad
def test_real_thermal_via_variants_keep_their_true_pin_count() -> None:
    path = (
        KICAD_SHARED
        / "footprints/Package_DFN_QFN.pretty"
        / "TDFN-10-1EP_2x3mm_P0.5mm_EP0.9x2mm_ThermalVias.kicad_mod"
    )
    if not path.is_file():  # pragma: no cover - library contents can change
        pytest.skip(f"{path.name} is not in this KiCad version")
    record = read_footprint(path, "Package_DFN_QFN", "kicad")
    assert record is not None
    assert record.identity.pad_count == 10
    assert record.identity.side_count == 2
