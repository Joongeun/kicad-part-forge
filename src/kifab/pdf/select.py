"""Pick the pages of a datasheet that carry the pin table and the drawing.

Why this exists at all: a 40-page datasheet costs roughly 3,600 tokens per page
once a model renders each page as an image as well as text. Sending all of it
costs ~4x what sending the eight pages that matter costs, and the other 32
pages are noise that makes extraction *worse*, not better. Selection is free,
local and deterministic, so it happens first and it is tested on its own.

The method is deliberately dull — weighted keyword and shape evidence over the
text layer, with the reasons kept so a human can see why a page was chosen:

* **pin table** — the words a pin table uses ("pin function", "terminal
  configuration"), plus the *shape* evidence that beats keywords: many short
  lines that begin with a small integer or a BGA-style designator.
* **mechanical drawing** — package-outline vocabulary, dimension-table
  vocabulary, JEDEC/MO references, and the tell-tale run of single-letter
  dimension symbols (D, E, e, b, L, A1).

Nothing here tries to *read* the drawing. It only decides which pages are worth
paying to look at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .text import PageText

# --- evidence ------------------------------------------------------------

PIN_TABLE_TERMS: dict[str, float] = {
    "pin function": 3.0,
    "pin functions": 3.0,
    "pin description": 3.0,
    "pin descriptions": 3.0,
    "pin configuration": 2.5,
    "terminal configuration": 2.5,
    "terminal functions": 3.0,
    "pin assignment": 2.5,
    "pinout": 2.0,
    "pin name": 2.0,
    "pin no": 2.0,
    "pin number": 2.0,
    "signal name": 1.5,
    "i/o": 0.5,
}

MECHANICAL_TERMS: dict[str, float] = {
    "package outline": 3.0,
    "package drawing": 3.0,
    "package dimensions": 3.0,
    "package information": 2.0,
    "mechanical data": 3.0,
    "mechanical drawing": 3.0,
    "outline dimensions": 3.0,
    "recommended land pattern": 3.0,
    "land pattern": 2.5,
    "recommended pcb layout": 2.5,
    "solder pad layout": 2.5,
    "exposed pad": 1.5,
    "millimeters": 1.0,
    "dimensions are in millimeters": 2.0,
    "jedec": 2.0,
    "note: the dimension": 1.0,
}

#: JEDEC / JEITA outline references — very strong evidence of a drawing page.
_JEDEC = re.compile(r"\b(MO-\d{3}|MS-\d{3}|ED-\d{4}|VARIATION\s+[A-Z]{2,3})\b", re.I)

#: A drawing's dimension table lists single-letter symbols in a column.
_DIM_SYMBOL = re.compile(r"^\s*(A[0-9]?|b|c|D[0-9]?|E[0-9]?|e|L[0-9]?|k|θ|N)\s*[\s|:]")

#: "3.00 BSC", "0.45 REF", "2.90 2.95 3.00" — toleranced dimension rows.
_DIM_ROW = re.compile(
    r"\b\d\.\d{2,3}\b.*\b(BSC|REF|TYP|MAX|MIN|NOM)\b|"
    r"\b\d\.\d{2,3}\s+\d\.\d{2,3}\s+\d\.\d{2,3}\b",
    re.I,
)

#: A pin-table row: a small integer or a BGA designator, then a short name.
_PIN_ROW = re.compile(r"^\s*(\d{1,3}|[A-Z]{1,2}\d{1,2})[\s.):|-]+\S")


@dataclass(frozen=True)
class PageScore:
    """Why one page did or did not make the cut."""

    number: int
    pin_table: float
    mechanical: float
    reasons: tuple[str, ...] = ()

    @property
    def total(self) -> float:
        return self.pin_table + self.mechanical


@dataclass(frozen=True)
class Selection:
    """The pages to send, and the evidence for each."""

    pages: list[int]
    scores: list[PageScore]
    pin_table_pages: list[int] = field(default_factory=list)
    mechanical_pages: list[int] = field(default_factory=list)
    total_pages: int = 0

    @property
    def has_text_layer(self) -> bool:
        return any(s.total > 0 for s in self.scores)

    def reduction(self) -> float:
        """Fraction of the document we are *not* sending."""
        if not self.total_pages:
            return 0.0
        return 1 - len(self.pages) / self.total_pages

    def explain(self) -> str:
        lines = [
            f"pages {self.pages} of {self.total_pages} "
            f"({self.reduction() * 100:.0f}% of the document skipped)"
        ]
        by_number = {s.number: s for s in self.scores}
        for number in self.pages:
            score = by_number[number]
            kind = []
            if number in self.pin_table_pages:
                kind.append("pin table")
            if number in self.mechanical_pages:
                kind.append("mechanical")
            lines.append(
                f"  p{number}: {'+'.join(kind) or 'context'} "
                f"(pin {score.pin_table:.1f}, mech {score.mechanical:.1f})"
                + (f" — {', '.join(score.reasons)}" if score.reasons else "")
            )
        return "\n".join(lines)


def _score_page(page: PageText) -> PageScore:
    text = page.text
    lowered = text.lower()
    lines = [line for line in text.splitlines() if line.strip()]

    pin = 0.0
    mech = 0.0
    reasons: list[str] = []

    for term, weight in PIN_TABLE_TERMS.items():
        if term in lowered:
            pin += weight
            reasons.append(f"'{term}'")
    for term, weight in MECHANICAL_TERMS.items():
        if term in lowered:
            mech += weight
            reasons.append(f"'{term}'")

    pin_rows = sum(1 for line in lines if _PIN_ROW.match(line))
    if pin_rows >= 6:
        pin += min(4.0, pin_rows / 4)
        reasons.append(f"{pin_rows} numbered rows")

    dim_symbols = sum(1 for line in lines if _DIM_SYMBOL.match(line))
    if dim_symbols >= 4:
        mech += min(4.0, dim_symbols / 2)
        reasons.append(f"{dim_symbols} dimension symbols")

    dim_rows = sum(1 for line in lines if _DIM_ROW.search(line))
    if dim_rows >= 3:
        mech += min(3.0, dim_rows / 2)
        reasons.append(f"{dim_rows} toleranced rows")

    if _JEDEC.search(text):
        mech += 3.0
        reasons.append("JEDEC outline reference")

    return PageScore(
        number=page.number,
        pin_table=round(pin, 3),
        mechanical=round(mech, 3),
        reasons=tuple(reasons),
    )


#: A page needs at least this much evidence to be worth paying for.
THRESHOLD = 3.0

#: Hard ceiling on how many pages we will send, whatever the scores say.
MAX_PAGES = 8


def select_pages(
    pages: list[PageText],
    *,
    threshold: float = THRESHOLD,
    max_pages: int = MAX_PAGES,
) -> Selection:
    """Choose the pages worth sending to a model.

    Always keeps page 1 — the front page carries the part's identity, the
    package name and the ordering information, and it is the cheapest possible
    guard against extracting the wrong device from a multi-part datasheet.
    """
    scores = [_score_page(p) for p in pages]
    by_number = {s.number: s for s in scores}

    pin_pages = [s.number for s in scores if s.pin_table >= threshold]
    mech_pages = [s.number for s in scores if s.mechanical >= threshold]

    chosen: list[int] = []
    if pages:
        chosen.append(1)

    # Rank each category independently so a document heavy in one kind of page
    # cannot crowd the other out entirely — the two must both be present for
    # generation to be possible at all.
    ranked_pin = sorted(pin_pages, key=lambda n: -by_number[n].pin_table)
    ranked_mech = sorted(mech_pages, key=lambda n: -by_number[n].mechanical)

    budget = max_pages - len(chosen)
    half = max(1, budget // 2)
    for number in ranked_pin[:half] + ranked_mech[: budget - min(half, len(ranked_pin))]:
        if number not in chosen:
            chosen.append(number)

    # Anything still under budget goes to the next-best remaining page of
    # either kind, which is how a pin table split over three pages stays whole.
    for number in sorted(
        set(pin_pages) | set(mech_pages), key=lambda n: -by_number[n].total
    ):
        if len(chosen) >= max_pages:
            break
        if number not in chosen:
            chosen.append(number)

    return Selection(
        pages=sorted(chosen),
        scores=scores,
        pin_table_pages=sorted(pin_pages),
        mechanical_pages=sorted(mech_pages),
        total_pages=len(pages),
    )
