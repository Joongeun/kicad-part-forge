"""Deterministic datasheet handling: read the text layer, pick the pages.

Everything in this package runs locally, for free, with no model and no
network. It exists so that the expensive step — the one that costs tokens and
can be wrong — is handed the two or three pages that matter instead of forty.

Order matters: page selection happens **before** any provider is constructed.
A run that cannot find a pin table or a mechanical drawing says so and stops,
rather than paying to have a model discover the same thing.
"""

from .select import PageScore, Selection, select_pages
from .text import PageText, extract_pages, slice_pdf

__all__ = [
    "PageScore",
    "PageText",
    "Selection",
    "extract_pages",
    "select_pages",
    "slice_pdf",
]
