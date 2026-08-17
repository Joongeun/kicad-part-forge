"""Package identity — the unit tests behind the "not a body-size match" rule.

Pure arithmetic and string handling, so these run everywhere, with no KiCad
installation and no index.
"""

from __future__ import annotations

import pytest

from kifab.index.package_id import (
    GEOMETRY_ATTRS,
    PackageIdentity,
    PadGeom,
    Verdict,
    canonical_family,
    compare,
    identity_from_footprint,
    measure_pitch,
    measure_sides,
    merge_by_number,
    split_exposed,
)

# --------------------------------------------------------------------------
# Family normalisation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token, expected",
    [
        ("QFN", "QFN"),
        ("VQFN", "QFN"),
        ("HVQFN", "QFN"),
        ("DFN", "DFN"),
        ("UDFN", "DFN"),
        ("TDFN", "DFN"),
        ("LQFP", "QFP"),
        ("TQFP", "QFP"),
        ("SOIC", "SOIC"),
        ("TSSOP", "TSSOP"),
        ("LTC", None),
        ("Texas", None),
        ("PHP0048E", None),
    ],
)
def test_family_canonicalisation(token: str, expected: str | None) -> None:
    assert canonical_family(token) == expected


def test_dfn_and_qfn_never_collapse_however_decorated() -> None:
    """The whole module exists for this one assertion."""
    dfn = {canonical_family(t) for t in ("DFN", "UDFN", "TDFN", "WDFN", "VDFN")}
    qfn = {canonical_family(t) for t in ("QFN", "VQFN", "WQFN", "HVQFN", "UQFN")}
    assert dfn == {"DFN"}
    assert qfn == {"QFN"}
    assert dfn.isdisjoint(qfn)


def test_lowercase_prose_does_not_name_a_family() -> None:
    """"...generated with kicad-footprint-generator" must not claim family TO."""
    identity = PackageIdentity.parse(
        "Resistor SMD 0805, square end terminal, generated with a script"
    )
    assert identity.families == frozenset()
    assert identity.primary_family is None


# --------------------------------------------------------------------------
# Parsing hints
# --------------------------------------------------------------------------


def test_parses_a_datasheet_phrase() -> None:
    identity = PackageIdentity.parse(
        "12-Lead Plastic QFN (3mm x 2mm), LTC DWG # 05-08-1985"
    )
    assert identity.primary_family == "QFN"
    assert identity.pad_count == 12
    assert identity.body == (2.0, 3.0)
    assert identity.drawing == "05-08-1985"
    assert identity.implied_sides == 4
    # Prose that says nothing about a thermal pad must stay silent about it.
    assert identity.exposed_pad is None


def test_parses_a_kicad_footprint_name() -> None:
    identity = PackageIdentity.parse("QFN-12-1EP_2x3mm_P0.45mm_EP0.64x2.4mm")
    assert identity.primary_family == "QFN"
    assert identity.pad_count == 12
    assert identity.pitch == 0.45
    assert identity.body == (2.0, 3.0)
    assert identity.exposed_pad is True
    assert identity.ep_size == (0.64, 2.4)


def test_a_kicad_name_without_ep_means_there_is_no_ep() -> None:
    """A generated name is a complete spec; a datasheet sentence is not."""
    assert PackageIdentity.parse("LQFP-48_7x7mm_P0.5mm").exposed_pad is False
    assert PackageIdentity.parse("48-Lead LQFP (7mm x 7mm)").exposed_pad is None


def test_vendor_prefixed_names_still_yield_a_family() -> None:
    identity = PackageIdentity.parse(
        "Texas_PHP0048E_HTQFP-48-1EP_7x7mm_P0.5mm_EP6.5x6.5mm"
    )
    assert identity.primary_family == "QFP"
    assert identity.pad_count == 48


# --------------------------------------------------------------------------
# Measuring copper
# --------------------------------------------------------------------------


def _two_column(count: int, pitch: float = 0.5, x: float = 1.0) -> list[PadGeom]:
    half = count // 2
    top = -(half - 1) * pitch / 2
    pads = []
    for i in range(half):
        pads.append(PadGeom(str(i + 1), -x, top + i * pitch, 0.7, 0.25))
    for i in range(half):
        pads.append(PadGeom(str(count - i), x, top + i * pitch, 0.7, 0.25))
    return pads


def _four_sided(per_side: int, pitch: float = 0.5, r: float = 1.0) -> list[PadGeom]:
    start = -(per_side - 1) * pitch / 2
    pads: list[PadGeom] = []
    n = 1
    for i in range(per_side):  # left column
        pads.append(PadGeom(str(n), -r, start + i * pitch, 0.7, 0.25))
        n += 1
    for i in range(per_side):  # bottom row
        pads.append(PadGeom(str(n), start + i * pitch, r, 0.25, 0.7))
        n += 1
    for i in range(per_side):  # right column
        pads.append(PadGeom(str(n), r, -(start + i * pitch), 0.7, 0.25))
        n += 1
    for i in range(per_side):  # top row
        pads.append(PadGeom(str(n), -(start + i * pitch), -r, 0.25, 0.7))
        n += 1
    return pads


def test_edge_count_is_measured_from_the_copper() -> None:
    assert measure_sides(_two_column(12)) == 2
    assert measure_sides(_four_sided(3)) == 4


def test_pitch_is_measured_from_neighbouring_lands() -> None:
    assert measure_pitch(_two_column(12, pitch=0.45)) == pytest.approx(0.45)


def test_exposed_pad_is_found_by_size_and_position() -> None:
    pads = _two_column(12) + [PadGeom("13", 0.0, 0.0, 0.64, 2.4)]
    perimeter, exposed = split_exposed(pads)
    assert len(perimeter) == 12
    assert [p.number for p in exposed] == ["13"]


def test_a_two_terminal_chip_has_no_exposed_pad() -> None:
    pads = [PadGeom("1", -1.5, 0, 1.05, 1.9), PadGeom("2", 1.5, 0, 1.05, 1.9)]
    perimeter, exposed = split_exposed(pads)
    assert exposed == []
    assert len(perimeter) == 2


def test_thermal_vias_do_not_turn_a_dfn_into_a_qfn() -> None:
    """A regression: `*_ThermalVias` numbers its vias after the exposed pad."""
    pads = _two_column(10) + [PadGeom("11", 0.0, 0.0, 0.9, 2.0)]
    pads += [
        PadGeom("11", dx, dy, 0.3, 0.3)
        for dx in (-0.3, 0.0, 0.3)
        for dy in (-0.6, 0.0, 0.6)
    ]
    identity = identity_from_footprint("TDFN-10-1EP_2x3mm_P0.5mm_ThermalVias", pads=pads)
    assert identity.pad_count == 10
    assert identity.side_count == 2
    assert merge_by_number(pads)[-1].number == "11"


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------


def _udb_query() -> PackageIdentity:
    return PackageIdentity.parse("QFN-12-1EP_2x3mm_P0.45mm_EP0.64x2.4mm")


def _ddb_like() -> PackageIdentity:
    """The shape of KiCad's DFN-12-1EP_2x3mm — identical geometry, wrong frame."""
    pads = _two_column(12, pitch=0.45) + [PadGeom("13", 0.0, 0.0, 0.64, 2.4)]
    return identity_from_footprint(
        "DFN-12-1EP_2x3mm_P0.45mm_EP0.64x2.4mm",
        descr="DDB Package; 12-Lead Plastic DFN (3mm x 2mm)",
        pads=pads,
    )


def test_identical_geometry_is_still_not_a_match_when_the_family_differs() -> None:
    """The single most important assertion in the package layer.

    Pins, pitch, body, exposed pad and EP size ALL agree. Identity must still
    not be established, because the lead frame — and the measured edge count —
    disagree.
    """
    match = compare(_udb_query(), _ddb_like())
    assert match.established is False
    verdicts = {e.attribute: e.verdict for e in match.evidence}
    assert verdicts["pins"] is Verdict.MATCH
    assert verdicts["pitch"] is Verdict.MATCH
    assert verdicts["body"] is Verdict.MATCH
    assert verdicts["exposed_pad"] is Verdict.MATCH
    assert verdicts["ep_size"] is Verdict.MATCH
    assert verdicts["family"] is Verdict.MISMATCH
    assert verdicts["sides"] is Verdict.MISMATCH
    assert {e.attribute for e in match.blocking} == {"family", "sides"}


def test_a_true_match_is_established() -> None:
    pads = _two_column(12, pitch=0.45)
    pads = _four_sided(3, pitch=0.45) + [PadGeom("13", 0.0, 0.0, 0.64, 2.4)]
    candidate = identity_from_footprint(
        "QFN-12-1EP_2x3mm_P0.45mm_EP0.64x2.4mm", pads=pads
    )
    match = compare(_udb_query(), candidate)
    assert match.established is True
    assert match.blocking == ()
    assert match.missing == ()


def test_unknown_blocks_confidence_exactly_as_hard_as_disagreement() -> None:
    """"We could not tell" is not "it matches"."""
    query = PackageIdentity.parse("12-Lead Plastic QFN (3mm x 2mm)")
    pads = _four_sided(3, pitch=0.45) + [PadGeom("13", 0.0, 0.0, 0.64, 2.4)]
    candidate = identity_from_footprint(
        "QFN-12-1EP_2x3mm_P0.45mm_EP0.64x2.4mm", pads=pads
    )
    match = compare(query, candidate)
    assert match.blocking == ()
    assert {e.attribute for e in match.missing} == {"pitch", "exposed_pad"}
    assert match.established is False


def test_a_different_drawing_number_blocks_confidence() -> None:
    query = PackageIdentity.parse(
        "12-Lead Plastic QFN (3mm x 2mm), LTC DWG # 05-08-1985"
    )
    pads = _four_sided(3, pitch=0.45) + [PadGeom("13", 0.0, 0.0, 0.64, 2.4)]
    candidate = identity_from_footprint(
        "QFN-12-1EP_2x3mm_P0.45mm_EP0.64x2.4mm",
        descr="12-Lead Plastic QFN (3mm x 2mm) LTC DWG 05-08-1723",
        pads=pads,
    )
    match = compare(query, candidate)
    assert "drawing" in {e.attribute for e in match.blocking}
    assert match.established is False


def test_confusability_counts_only_the_attributes_that_look_right() -> None:
    match = compare(_udb_query(), _ddb_like())
    assert match.agreements == len(GEOMETRY_ATTRS)


def test_no_score_can_outvote_a_decisive_mismatch() -> None:
    """Identity is established by evidence, never by clearing a threshold.

    The DDB look-alike scores *positively* — five of eight attributes agree,
    and two of the disagreements are worth less than the agreements. If
    confidence were a threshold on `score`, this is the case that would ship a
    wrong footprint.
    """
    match = compare(_udb_query(), _ddb_like())
    assert match.score > 0
    assert match.established is False
