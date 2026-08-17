"""Package identity — what makes two land patterns the *same* package.

This module exists because of one specific, dangerous failure mode.

KiCad ships `Package_DFN_QFN.pretty/DFN-12-1EP_2x3mm_P0.45mm_EP0.64x2.4mm`,
described as *"DDB Package; 12-Lead Plastic DFN (3mm x 2mm), Linear DWG
05-08-1723"*.  The LTC5552IUDB uses the **UDB** package: *12-Lead Plastic QFN
(3mm x 2mm), LTC DWG # 05-08-1985*.  Same pin count, same body size, **different
package, different drawing, different pad geometry.**

A search-first resolver that ranks on body size will happily hand back the DDB
footprint for a UDB part.  It looks right in the library browser, it looks right
in the 3D viewer, and it scraps the board.  So:

    **A body-size match is NOT a package match.**

What this module does about it
------------------------------
It reduces a footprint (or a datasheet phrase, or a KiCad-style name) to a
`PackageIdentity`: a small set of attributes that, taken together, actually
pin down a package rather than merely describing its outline.

===========  ================================================================
attribute    why it discriminates
===========  ================================================================
family       DFN and QFN are different lead frames.  Never merged.
sides        *Measured from pad positions*, not from the name — a DFN has
             lands on 2 edges, a QFN on 4.  This is geometric proof, and it
             holds even for a badly named third-party library.
pins         perimeter land count, excluding exposed pads.
pitch        declared in the KiCad name when present, else measured.
body         the declared body envelope, orientation-independent.
exposed pad  presence *and* size.  An EP is not a detail; it is the thermal
             path and it is copper.
drawing      the manufacturer's mechanical drawing number when either side
             states one.  Two different drawings are two different packages,
             full stop.
===========  ================================================================

`compare()` returns per-attribute `Evidence`, and `PackageMatch` says whether
package identity was *established* — never merely "scored high".  An attribute
that is unknown blocks confidence exactly as hard as one that disagrees,
because "we could not tell" and "it matches" are not the same claim.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from enum import Enum

# --------------------------------------------------------------------------
# Family vocabulary
# --------------------------------------------------------------------------

# Canonical family bases, longest first: a token is canonicalised to the
# longest base it *ends with*, so modifier prefixes collapse (HVQFN -> QFN,
# UDFN -> DFN, LQFP -> QFP) while genuinely different lead frames never do.
# This is the single rule that keeps DFN and QFN apart no matter how the
# vendor decorates the name.
_FAMILY_BASES: tuple[str, ...] = (
    "TSSOP",
    "WLCSP",
    "SSOP",
    "QSOP",
    "MSOP",
    "TSOP",
    "SOIC",
    "PLCC",
    "QFN",
    "DFN",
    "QFP",
    "SON",
    "SOP",
    "SOT",
    "BGA",
    "LGA",
    "CSP",
    "DIP",
    "SIP",
    "TO",
    "SC",
    "SO",
)

# How many package edges carry perimeter lands.  `None` means "an area array or
# a family where edge count says nothing" — evidence stays UNKNOWN rather than
# inventing a discriminator.
FAMILY_EDGES: dict[str, int | None] = {
    "QFN": 4,
    "QFP": 4,
    "PLCC": 4,
    "DFN": 2,
    "SON": 2,
    "SOIC": 2,
    "SOP": 2,
    "SSOP": 2,
    "TSSOP": 2,
    "MSOP": 2,
    "QSOP": 2,
    "TSOP": 2,
    "SOT": 2,
    "SC": 2,
    "SO": 2,
    "DIP": 2,
    "SIP": 1,
    "BGA": None,
    "LGA": None,
    "CSP": None,
    "WLCSP": None,
    "TO": None,
}

#: Families whose land pattern has no meaningful lead pitch (two-terminal chips
#: and the like).  Pitch is not required to establish identity for these.
_PITCHLESS: frozenset[str] = frozenset({"TO"})


def canonical_family(token: str) -> str | None:
    """Normalise a package token, or return None if it is not a family name.

    >>> canonical_family("HVQFN")
    'QFN'
    >>> canonical_family("UDFN")
    'DFN'
    >>> canonical_family("LTC")     # not a package family
    """
    upper = token.upper().strip()
    if not upper or not upper.isalnum():
        upper = re.sub(r"[^A-Z0-9]", "", upper)
    if not upper:
        return None
    for base in _FAMILY_BASES:
        if upper.endswith(base):
            return base
    return None


# --------------------------------------------------------------------------
# Text extraction
# --------------------------------------------------------------------------

_RE_NAME_HEAD = re.compile(r"^([A-Za-z][A-Za-z0-9]*)(?:[-_](\d+))?")
_RE_PINS_LEAD = re.compile(r"\b(\d{1,4})\s*[- ]?\s*(?:lead|pin|leads|pins)\b", re.IGNORECASE)
_RE_PITCH_NAME = re.compile(r"[_-]P(\d+(?:\.\d+)?)mm(?![A-Za-z0-9.])", re.IGNORECASE)
_RE_PITCH_TEXT = re.compile(r"(\d+(?:\.\d+)?)\s*mm\s*pitch|pitch\s*(?:of\s*)?(\d+(?:\.\d+)?)\s*mm", re.IGNORECASE)
_RE_BODY_NAME = re.compile(r"[_-](\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)mm(?![A-Za-z0-9.])", re.IGNORECASE)
_RE_BODY_TEXT = re.compile(
    r"\(\s*(\d+(?:\.\d+)?)\s*mm\s*[x×*]\s*(\d+(?:\.\d+)?)\s*mm\s*\)", re.IGNORECASE
)
_RE_EP_NAME = re.compile(r"[_-]EP(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)mm(?![A-Za-z0-9.])", re.IGNORECASE)
_RE_EP_COUNT = re.compile(r"[-_](\d+)EP(?![A-Za-z0-9])", re.IGNORECASE)
_RE_DRAWING = re.compile(
    r"(?:DWG|DRAWING|DRAWING\s*NO\.?)\s*#?\s*:?\s*([0-9]{2,}(?:-[0-9]{2,}){1,3})"
    r"|\b(\d{2}-\d{2}-\d{4})\b",
    re.IGNORECASE,
)
_RE_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]{1,7}")


def _families_in(text: str) -> list[str]:
    """Every package family named anywhere in a blob of text, in order.

    Only fully upper-case tokens count. Without that rule the English word
    "to" canonicalises to the TO family and every description in the corpus
    claims a family it does not have — which would turn the family attribute,
    the strongest discriminator here, into noise.
    """
    out: list[str] = []
    for word in _RE_WORD.findall(text or ""):
        if word != word.upper():
            continue
        fam = canonical_family(word)
        if fam and fam not in out:
            out.append(fam)
    return out


_RE_SEGMENT = re.compile(r"^([A-Za-z][A-Za-z0-9]*)(?:-(\d+))?")


def _family_segment(text: str) -> tuple[str | None, int | None]:
    """Find the `FAMILY-<pins>` segment of a package name.

    Scanning every `_`-separated segment rather than only the first is what
    lets vendor-prefixed names work:
    `Texas_PHP0048E_HTQFP-48-1EP_7x7mm_P0.5mm` is an HTQFP-48, and reading only
    the head would have called it family "Texas" and refused to match anything.
    """
    for segment in text.split("_"):
        match = _RE_SEGMENT.match(segment.strip())
        if not match:
            continue
        family = canonical_family(match.group(1))
        if family:
            pins = int(match.group(2)) if match.group(2) else None
            return family, pins
    head = _RE_NAME_HEAD.match(text)
    if head and head.group(2):
        return None, int(head.group(2))
    return None, None


def _looks_like_kicad_name(text: str) -> bool:
    """True for `LQFP-48_7x7mm_P0.5mm`, false for a sentence from a datasheet."""
    return " " not in text and "mm" in text.lower() and bool(_RE_NAME_HEAD.match(text))


def _drawing_in(text: str) -> str | None:
    match = _RE_DRAWING.search(text or "")
    if not match:
        return None
    return match.group(1) or match.group(2)


# --------------------------------------------------------------------------
# The identity record
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PackageIdentity:
    """What we know about a package. Used for both sides of a comparison.

    A candidate's identity is measured from its file; a query's identity is
    parsed from whatever the caller stated.  Every field is optional because
    "unknown" is a real and important state — it is what stops the resolver
    from claiming a match it cannot justify.
    """

    primary_family: str | None = None
    """Family taken from the authoritative source: the leading token of a
    KiCad-style name, or the first family named in a datasheet phrase."""

    families: frozenset[str] = frozenset()
    """Every family named anywhere (name, description, tags). Supplementary."""

    pad_count: int | None = None
    """Perimeter lands, exposed pads excluded."""

    side_count: int | None = None
    """Package edges carrying lands. Measured from pad positions when
    available, else implied by the family."""

    pitch: float | None = None
    body: tuple[float, float] | None = None
    """Body envelope in mm, stored sorted so 2x3 and 3x2 compare equal."""

    exposed_pad: bool | None = None
    ep_size: tuple[float, float] | None = None
    drawing: str | None = None
    source: str = ""
    """Free text this identity was read from — for reporting, not matching."""

    @property
    def implied_sides(self) -> int | None:
        if self.side_count is not None:
            return self.side_count
        if self.primary_family:
            return FAMILY_EDGES.get(self.primary_family)
        return None

    @property
    def pitch_is_meaningful(self) -> bool:
        return self.primary_family not in _PITCHLESS

    @classmethod
    def parse(cls, text: str) -> PackageIdentity:
        """Read an identity out of a package *hint*.

        Handles both shapes people actually have to hand:

        * a KiCad-style name — ``QFN-12-1EP_2x3mm_P0.45mm_EP0.64x2.4mm``
        * a datasheet phrase — ``12-Lead Plastic QFN (3mm x 2mm), LTC DWG # 05-08-1985``
        """
        text = (text or "").strip()
        if not text:
            return cls()

        families = _families_in(text)
        primary, named_pins = _family_segment(text)
        if primary is None and families:
            primary = families[0]

        pad_count = named_pins
        if pad_count is None:
            lead = _RE_PINS_LEAD.search(text)
            if lead:
                pad_count = int(lead.group(1))

        pitch = None
        m = _RE_PITCH_NAME.search(text)
        if m:
            pitch = float(m.group(1))
        else:
            m = _RE_PITCH_TEXT.search(text)
            if m:
                pitch = float(m.group(1) or m.group(2))

        body = None
        m = _RE_BODY_NAME.search(text)
        if m:
            body = _sorted_pair(float(m.group(1)), float(m.group(2)))
        else:
            m = _RE_BODY_TEXT.search(text)
            if m:
                body = _sorted_pair(float(m.group(1)), float(m.group(2)))

        ep_size = None
        exposed = None
        m = _RE_EP_NAME.search(text)
        if m:
            ep_size = _sorted_pair(float(m.group(1)), float(m.group(2)))
            exposed = True
        elif _RE_EP_COUNT.search(text):
            exposed = True
        elif _looks_like_kicad_name(text):
            # A KiCad footprint name is a *complete* specification of the land
            # pattern: `LQFP-48_7x7mm_P0.5mm` says there is no exposed pad just
            # as loudly as `-1EP` says there is. Prose gets no such reading —
            # a datasheet sentence that omits the EP is simply silent about it.
            exposed = False

        return cls(
            primary_family=primary,
            families=frozenset(families),
            pad_count=pad_count,
            pitch=pitch,
            body=body,
            exposed_pad=exposed,
            ep_size=ep_size,
            drawing=_drawing_in(text),
            source=text,
        )


def _sorted_pair(a: float, b: float) -> tuple[float, float]:
    lo, hi = sorted((round(a, 4), round(b, 4)))
    return (lo, hi)


# --------------------------------------------------------------------------
# Measuring a footprint
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PadGeom:
    """Just enough of a land to reason about package identity."""

    number: str
    x: float
    y: float
    w: float
    h: float

    @property
    def area(self) -> float:
        return self.w * self.h


def merge_by_number(pads: list[PadGeom]) -> list[PadGeom]:
    """Collapse pads sharing a number into one land, by bounding box.

    KiCad's `*_ThermalVias` variants scatter a dozen small vias across the
    exposed pad, all carrying the exposed pad's own number. Left alone they
    read as extra perimeter lands on all four edges, so
    `TDFN-10-1EP_2x3mm_..._ThermalVias` measures as a 12-pin, four-sided
    package — i.e. it impersonates a QFN-12. Electrically they are one land,
    so that is what identity counts.
    """
    order: list[str] = []
    groups: dict[str, list[PadGeom]] = {}
    for pad in pads:
        if pad.number not in groups:
            order.append(pad.number)
            groups[pad.number] = []
        groups[pad.number].append(pad)

    merged: list[PadGeom] = []
    for number in order:
        members = groups[number]
        if len(members) == 1:
            merged.append(members[0])
            continue
        x0 = min(p.x - p.w / 2 for p in members)
        x1 = max(p.x + p.w / 2 for p in members)
        y0 = min(p.y - p.h / 2 for p in members)
        y1 = max(p.y + p.h / 2 for p in members)
        merged.append(
            PadGeom(
                number=number,
                x=round((x0 + x1) / 2, 4),
                y=round((y0 + y1) / 2, 4),
                w=round(x1 - x0, 4),
                h=round(y1 - y0, 4),
            )
        )
    return merged


def split_exposed(pads: list[PadGeom]) -> tuple[list[PadGeom], list[PadGeom]]:
    """Separate perimeter lands from exposed/thermal pads.

    Measured, not parsed: a pad counts as exposed when it sits well inside the
    land ring *and* is substantially larger than a typical perimeter land.
    Both conditions are needed — a small centre via is not an EP, and a big
    corner land is not one either.
    """
    if len(pads) < 3:
        return list(pads), []
    span_x = max(abs(p.x) for p in pads)
    span_y = max(abs(p.y) for p in pads)
    median_area = statistics.median(p.area for p in pads)
    perimeter: list[PadGeom] = []
    exposed: list[PadGeom] = []
    for pad in pads:
        central = (span_x < 1e-6 or abs(pad.x) <= 0.35 * span_x) and (
            span_y < 1e-6 or abs(pad.y) <= 0.35 * span_y
        )
        if central and pad.area > 1.5 * median_area:
            exposed.append(pad)
        else:
            perimeter.append(pad)
    return perimeter, exposed


def measure_sides(perimeter: list[PadGeom]) -> int | None:
    """How many package edges carry lands. The geometric family discriminator.

    A DFN's lands sit in two columns; a QFN's ring all four edges. This is
    read off the copper, so it is true even when the name lies.
    """
    if len(perimeter) < 2:
        return None
    span_x = max(abs(p.x) for p in perimeter)
    span_y = max(abs(p.y) for p in perimeter)
    if span_x < 1e-6 and span_y < 1e-6:
        return None
    sides: set[str] = set()
    for pad in perimeter:
        rx = abs(pad.x) / span_x if span_x > 1e-6 else 0.0
        ry = abs(pad.y) / span_y if span_y > 1e-6 else 0.0
        if rx >= ry:
            sides.add("L" if pad.x < 0 else "R")
        else:
            sides.add("T" if pad.y < 0 else "B")
    return len(sides)


def measure_pitch(perimeter: list[PadGeom]) -> float | None:
    """Median centre-to-centre spacing of neighbouring lands along each edge."""
    if len(perimeter) < 2:
        return None
    span_x = max(abs(p.x) for p in perimeter)
    span_y = max(abs(p.y) for p in perimeter)
    groups: dict[str, list[PadGeom]] = {}
    for pad in perimeter:
        rx = abs(pad.x) / span_x if span_x > 1e-6 else 0.0
        ry = abs(pad.y) / span_y if span_y > 1e-6 else 0.0
        if rx >= ry:
            groups.setdefault("L" if pad.x < 0 else "R", []).append(pad)
        else:
            groups.setdefault("T" if pad.y < 0 else "B", []).append(pad)
    gaps: list[float] = []
    for side, members in groups.items():
        if len(members) < 2:
            continue
        axis = (lambda p: p.y) if side in ("L", "R") else (lambda p: p.x)
        values = sorted(axis(p) for p in members)
        gaps.extend(
            round(b - a, 4) for a, b in zip(values, values[1:]) if b - a > 1e-6
        )
    if not gaps:
        return None
    return round(statistics.median(gaps), 4)


def identity_from_footprint(
    name: str,
    descr: str = "",
    tags: str = "",
    pads: list[PadGeom] | None = None,
    fab_bbox: tuple[float, float] | None = None,
) -> PackageIdentity:
    """Reduce a real footprint to its package identity.

    The KiCad name is treated as the authoritative declaration (it is generated
    from the mechanical drawing), and the copper is used to *measure*
    everything the name cannot be trusted to state — above all the edge count.
    """
    pads = merge_by_number(pads or [])
    declared = PackageIdentity.parse(name)
    text = " ".join(x for x in (name, descr, tags) if x)

    perimeter, exposed = split_exposed(pads)
    measured_pins = len(perimeter) or None
    measured_sides = measure_sides(perimeter)
    measured_pitch = measure_pitch(perimeter)

    ep_size = declared.ep_size
    if exposed:
        biggest = max(exposed, key=lambda p: p.area)
        ep_size = _sorted_pair(biggest.w, biggest.h)

    families = list(declared.families)
    for fam in _families_in(text):
        if fam not in families:
            families.append(fam)

    body = declared.body
    if body is None and fab_bbox is not None:
        body = _sorted_pair(round(fab_bbox[0], 2), round(fab_bbox[1], 2))

    return PackageIdentity(
        primary_family=declared.primary_family,
        families=frozenset(families),
        # Copper wins over the name for pin count: the name of a footprint with
        # a deliberately depopulated pin is not always updated.
        pad_count=measured_pins if measured_pins is not None else declared.pad_count,
        side_count=measured_sides,
        pitch=declared.pitch if declared.pitch is not None else measured_pitch,
        body=body,
        exposed_pad=bool(exposed) if pads else declared.exposed_pad,
        ep_size=ep_size,
        drawing=_drawing_in(text),
        source=name,
    )


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------


#: The attributes that make a wrong footprint *look* right in the library
#: browser. Agreement across these is what makes a candidate dangerous, which
#: is why the review list is ranked on them.
GEOMETRY_ATTRS: frozenset[str] = frozenset(
    {"pins", "pitch", "body", "exposed_pad", "ep_size"}
)


class Verdict(str, Enum):
    MATCH = "match"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Evidence:
    """One attribute's contribution to (or veto of) package identity."""

    attribute: str
    expected: str | None
    found: str | None
    verdict: Verdict
    decisive: bool
    """A MISMATCH here forbids a confident match. No score can outvote it."""

    required: bool
    """Must be MATCH for identity to be *established*; UNKNOWN blocks too."""

    note: str = ""

    def __str__(self) -> str:
        arrow = {"match": "==", "mismatch": "!=", "unknown": "??"}[self.verdict.value]
        return f"{self.attribute}: {self.expected} {arrow} {self.found}"


@dataclass(frozen=True)
class PackageMatch:
    """The result of comparing a query identity with a candidate's.

    `established` is the only thing a caller should gate on. It is deliberately
    not derived from `score`: a score is an ordering device, and no amount of
    it can overrule a decisive mismatch.
    """

    established: bool
    score: float
    evidence: tuple[Evidence, ...]

    @property
    def blocking(self) -> tuple[Evidence, ...]:
        """Attributes that positively disagree — the reason to reject."""
        return tuple(
            e for e in self.evidence if e.decisive and e.verdict is Verdict.MISMATCH
        )

    @property
    def missing(self) -> tuple[Evidence, ...]:
        """Attributes identity needs but nobody stated."""
        return tuple(
            e for e in self.evidence if e.required and e.verdict is Verdict.UNKNOWN
        )

    @property
    def agreements(self) -> int:
        """How *confusable* this candidate is: agreeing visible geometry.

        Deliberately counts only `GEOMETRY_ATTRS` — the attributes a person
        checks by eye. Family and edge count are excluded precisely because
        they are the attributes that a dangerous near miss gets *wrong*;
        counting them would rank the look-alike below unrelated parts.

        `score` cannot do this job either: it subtracts for disagreement, so
        the DFN that shares the UDB part's pin count and body size sinks to
        the bottom exactly when a human most needs to see it.
        """
        return sum(
            1
            for e in self.evidence
            if e.attribute in GEOMETRY_ATTRS and e.verdict is Verdict.MATCH
        )

    def reason(self) -> str:
        if self.blocking:
            return "; ".join(str(e) for e in self.blocking)
        if self.missing:
            return "unverified: " + ", ".join(e.attribute for e in self.missing)
        return "package identity established"


def _num(value: float | None, digits: int = 3) -> str | None:
    return None if value is None else f"{value:.{digits}g}"


def _pair(value: tuple[float, float] | None) -> str | None:
    return None if value is None else f"{value[0]:g}x{value[1]:g}"


def _cmp_optional(
    attribute: str,
    expected: object | None,
    found: object | None,
    *,
    equal: bool,
    decisive: bool,
    required: bool,
    note: str = "",
) -> Evidence:
    if expected is None or found is None:
        verdict = Verdict.UNKNOWN
    else:
        verdict = Verdict.MATCH if equal else Verdict.MISMATCH
    return Evidence(
        attribute=attribute,
        expected=None if expected is None else str(expected),
        found=None if found is None else str(found),
        verdict=verdict,
        decisive=decisive,
        required=required,
        note=note,
    )


#: Relative tolerance for body dimensions, mm. A body stated as 3.0 and drawn
#: as 2.95 is the same body; one drawn as 3.3 is not.
BODY_TOL = 0.15
#: Exposed pads are copper; they get a tight tolerance.
EP_TOL = 0.05
PITCH_TOL = 0.005


def compare(query: PackageIdentity, candidate: PackageIdentity) -> PackageMatch:
    """Grade one candidate package against what the caller asked for.

    Nothing here is a heuristic vote. Each attribute either agrees, disagrees,
    or is unknown, and both of the latter block a confident answer.
    """
    ev: list[Evidence] = []

    # --- family ---------------------------------------------------------
    # The DDB/UDB trap lives here. Primary families are compared directly when
    # both sides declare one; the wider set is only a fallback.
    q_fam, c_fam = query.primary_family, candidate.primary_family
    if q_fam and c_fam:
        ev.append(
            _cmp_optional(
                "family",
                q_fam,
                c_fam,
                equal=q_fam == c_fam,
                decisive=True,
                required=True,
                note="lead frame family; a body-size match is not a package match",
            )
        )
    elif query.families and candidate.families:
        shared = query.families & candidate.families
        ev.append(
            Evidence(
                "family",
                "|".join(sorted(query.families)),
                "|".join(sorted(candidate.families)),
                Verdict.MATCH if shared else Verdict.MISMATCH,
                decisive=True,
                required=True,
                note="matched on families named in text, not on a package name",
            )
        )
    else:
        ev.append(
            Evidence("family", q_fam, c_fam, Verdict.UNKNOWN, True, True)
        )

    # --- edge count (geometric) ----------------------------------------
    q_sides, c_sides = query.implied_sides, candidate.implied_sides
    ev.append(
        _cmp_optional(
            "sides",
            q_sides,
            c_sides,
            equal=q_sides == c_sides,
            decisive=True,
            required=False,
            note="package edges carrying lands, measured from pad positions",
        )
    )

    # --- perimeter land count ------------------------------------------
    ev.append(
        _cmp_optional(
            "pins",
            query.pad_count,
            candidate.pad_count,
            equal=query.pad_count == candidate.pad_count,
            decisive=True,
            required=True,
        )
    )

    # --- pitch -----------------------------------------------------------
    pitch_required = query.pitch_is_meaningful and candidate.pitch_is_meaningful
    ev.append(
        _cmp_optional(
            "pitch",
            _num(query.pitch),
            _num(candidate.pitch),
            equal=(
                query.pitch is not None
                and candidate.pitch is not None
                and abs(query.pitch - candidate.pitch) <= PITCH_TOL
            ),
            decisive=True,
            required=pitch_required,
        )
    )

    # --- body ------------------------------------------------------------
    body_equal = (
        query.body is not None
        and candidate.body is not None
        and all(abs(a - b) <= BODY_TOL for a, b in zip(query.body, candidate.body))
    )
    ev.append(
        _cmp_optional(
            "body",
            _pair(query.body),
            _pair(candidate.body),
            equal=body_equal,
            decisive=True,
            required=True,
            note="necessary, never sufficient",
        )
    )

    # --- exposed pad -----------------------------------------------------
    ev.append(
        _cmp_optional(
            "exposed_pad",
            query.exposed_pad,
            candidate.exposed_pad,
            equal=query.exposed_pad == candidate.exposed_pad,
            decisive=True,
            required=True,
        )
    )
    ep_equal = (
        query.ep_size is not None
        and candidate.ep_size is not None
        and all(abs(a - b) <= EP_TOL for a, b in zip(query.ep_size, candidate.ep_size))
    )
    ev.append(
        _cmp_optional(
            "ep_size",
            _pair(query.ep_size),
            _pair(candidate.ep_size),
            equal=ep_equal,
            decisive=True,
            required=False,
        )
    )

    # --- mechanical drawing ---------------------------------------------
    # Two different drawing numbers are two different packages, whatever the
    # rest of the attributes say.
    ev.append(
        _cmp_optional(
            "drawing",
            query.drawing,
            candidate.drawing,
            equal=query.drawing == candidate.drawing,
            decisive=True,
            required=False,
            note="manufacturer mechanical drawing number",
        )
    )

    blocking = [e for e in ev if e.decisive and e.verdict is Verdict.MISMATCH]
    missing = [e for e in ev if e.required and e.verdict is Verdict.UNKNOWN]
    established = not blocking and not missing

    score = 0.0
    for e in ev:
        if e.verdict is Verdict.MATCH:
            score += 2.0 if e.required else 1.0
        elif e.verdict is Verdict.MISMATCH:
            score -= 4.0 if e.required else 2.0
    return PackageMatch(established=established, score=score, evidence=tuple(ev))
