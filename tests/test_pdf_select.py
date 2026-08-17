"""Deterministic page selection: the free step that must work before any model.

These tests never touch a model, a network or a key. That is the point of
building this layer separately — the ~4x cost reduction it buys is a property
of code that can be tested for nothing.
"""

from __future__ import annotations

import pytest
from pdfs import (
    FILLER_PAGE,
    FRONT_PAGE,
    MECHANICAL_PAGE,
    PIN_TABLE_PAGE,
    datasheet,
    make_pdf,
)

from kifab.pdf import extract_pages, select_pages, slice_pdf
from kifab.pdf.select import MAX_PAGES
from kifab.pdf.text import PdfError, page_count


def _select(pdf: bytes, **kwargs):
    return select_pages(extract_pages(pdf), **kwargs)


def test_finds_the_pin_table_and_the_drawing() -> None:
    selection = _select(datasheet(pin_page=3, mech_page=6, total=8))
    assert selection.pin_table_pages == [3]
    assert selection.mechanical_pages == [6]
    assert selection.pages == [1, 3, 6]


def test_page_one_is_always_kept() -> None:
    """The front page is the cheapest guard against extracting the wrong device."""
    selection = _select(datasheet(pin_page=4, mech_page=7, total=9))
    assert selection.pages[0] == 1


def test_filler_pages_are_left_behind() -> None:
    selection = _select(datasheet(pin_page=2, mech_page=3, total=40))
    assert selection.pages == [1, 2, 3]
    assert selection.reduction() > 0.9


def test_selection_is_deterministic() -> None:
    pdf = datasheet(pin_page=3, mech_page=6, total=12)
    first = _select(pdf)
    second = _select(pdf)
    assert first.pages == second.pages
    assert [s.total for s in first.scores] == [s.total for s in second.scores]


def test_never_sends_more_than_the_ceiling() -> None:
    """Even a datasheet that looks relevant everywhere has a cost ceiling."""
    pdf = make_pdf([PIN_TABLE_PAGE, MECHANICAL_PAGE] * 20)
    selection = _select(pdf)
    assert len(selection.pages) <= MAX_PAGES
    # Both kinds must survive the cap; sending eight pin tables and no drawing
    # would be a cheaper way to fail.
    assert any(p in selection.pin_table_pages for p in selection.pages)
    assert any(p in selection.mechanical_pages for p in selection.pages)


def test_a_scanned_datasheet_reports_no_text_layer() -> None:
    """No text layer is a real answer, and the caller must be able to see it."""
    selection = _select(make_pdf(["", "", ""]))
    assert not selection.has_text_layer
    assert selection.pin_table_pages == []
    assert selection.mechanical_pages == []


def test_a_datasheet_with_no_drawing_is_visible_as_such() -> None:
    selection = _select(make_pdf([FRONT_PAGE, PIN_TABLE_PAGE, FILLER_PAGE]))
    assert selection.pin_table_pages == [2]
    assert selection.mechanical_pages == []


def test_slice_contains_exactly_the_chosen_pages() -> None:
    pdf = datasheet(pin_page=3, mech_page=6, total=8)
    selection = _select(pdf)
    sliced = slice_pdf(pdf, selection.pages)

    assert page_count(sliced) == len(selection.pages)
    text = [p.text for p in extract_pages(sliced)]
    assert any("PIN FUNCTIONS" in t for t in text)
    assert any("PACKAGE OUTLINE" in t for t in text)
    assert not any("ELECTRICAL CHARACTERISTICS" in t for t in text)
    # The saving has to be real, not notional: the slice is much smaller.
    assert len(sliced) < len(pdf)


def test_slice_rejects_pages_that_do_not_exist() -> None:
    pdf = make_pdf([FRONT_PAGE, PIN_TABLE_PAGE])
    with pytest.raises(ValueError, match="outside this 2-page document"):
        slice_pdf(pdf, [1, 5])
    with pytest.raises(ValueError, match="empty PDF slice"):
        slice_pdf(pdf, [])


def test_a_non_pdf_is_an_error_not_an_empty_result() -> None:
    with pytest.raises(PdfError):
        extract_pages(b"this is not a PDF")


def test_explain_names_the_evidence() -> None:
    """A human must be able to see *why* a page was chosen, not just that it was."""
    selection = _select(datasheet())
    text = selection.explain()
    assert "pin table" in text and "mechanical" in text
    assert "JEDEC outline reference" in text
