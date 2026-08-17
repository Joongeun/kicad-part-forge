"""Read a PDF's text layer, and cut a PDF down to a page subset.

Thin on purpose: `pypdf` does the parsing, this module owns the contract —
1-based page numbers everywhere, because that is what a datasheet's own page
footer says and what a human checking our work will type into a viewer.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from pypdf import PdfReader, PdfWriter


class PdfError(ValueError):
    """The supplied bytes are not a PDF we can read."""


@dataclass(frozen=True)
class PageText:
    """One page's extracted text layer."""

    number: int
    """1-based page number, as printed in a datasheet."""

    text: str

    @property
    def empty(self) -> bool:
        return not self.text.strip()


def extract_pages(data: bytes) -> list[PageText]:
    """Extract the text layer of every page.

    A scanned datasheet has no text layer and every page comes back empty;
    that is a real answer, not an error, and the caller decides what to do
    with it.
    """
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = list(reader.pages)
    except Exception as exc:  # pypdf raises a zoo of types
        raise PdfError(f"could not read the PDF: {exc}") from exc
    out: list[PageText] = []
    for i, page in enumerate(pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        out.append(PageText(number=i, text=text))
    return out


def page_count(data: bytes) -> int:
    try:
        return len(PdfReader(io.BytesIO(data)).pages)
    except Exception as exc:
        raise PdfError(f"could not read the PDF: {exc}") from exc


def slice_pdf(data: bytes, pages: list[int]) -> bytes:
    """Return a PDF containing only `pages` (1-based), in order.

    This is what actually reaches the model. It is also what makes the cost
    saving real rather than notional: the provider is handed a smaller
    document, not a full one with instructions to ignore most of it.
    """
    if not pages:
        raise ValueError("refusing to build an empty PDF slice")
    try:
        reader = PdfReader(io.BytesIO(data))
        total = len(reader.pages)
        writer = PdfWriter()
        for number in pages:
            if not 1 <= number <= total:
                raise ValueError(
                    f"page {number} is outside this {total}-page document"
                )
            writer.add_page(reader.pages[number - 1])
        buffer = io.BytesIO()
        writer.write(buffer)
    except ValueError:
        raise
    except Exception as exc:
        raise PdfError(f"could not slice the PDF: {exc}") from exc
    return buffer.getvalue()
