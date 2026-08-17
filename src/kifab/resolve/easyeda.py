"""T1 — LCSC / EasyEDA, as an *ingester*, never as a generator.

The rule this module exists to enforce: EasyEDA never writes to the user's
library. Its data is fetched, normalised into the Part IR, and re-emitted by
*our* emitters in house style, so an imported part is linted, restyled and
correctable exactly like a hand-written one. That is what lets us fix EasyEDA's
inconsistencies instead of inheriting them.

Why we talk to the API ourselves rather than depending on `easyeda2kicad`
----------------------------------------------------------------------------
`easyeda2kicad`'s stable, supported surface is its CLI, and that surface emits
the one artefact we have decided not to use: KiCad files. The only parts of it
we would actually consume are its *internals* — a thin two-URL HTTP wrapper and
a KiCad-shaped intermediate model — from a single-maintainer project that makes
no compatibility promise about them. Reading the raw component JSON directly is
about 300 lines, brings **zero new runtime dependencies** (stdlib `urllib`),
and puts the normalisation decisions where they have to live anyway: at the
point where the shape strings are parsed. Measured, not assumed: the upstream
CDN returns **403 to the `easyeda2kicad/…` User-Agent** and 200 to a browser
one, so even the transport is something we would have had to own.

What "normalise" means here, concretely
----------------------------------------------------------------------------
Only transformations that are *provably* value-preserving are applied silently.
Everything else is surfaced as a note on the written YAML, because a
wrong-but-plausible import is worse than a flagged one. See `NORMALISATIONS`
and `NOT_NORMALISED` at the bottom of this module — they are data, and a test
asserts they are documented.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..ir import Part
from ..ir.enums import ElectricalType, PadShape, PadType, Side
from .adopt import render_part_yaml

# --------------------------------------------------------------------------
# Units and vocabularies
# --------------------------------------------------------------------------

TENMIL_MM = 0.254
"""EasyEDA stores every coordinate in units of 10 mil. 10 mil = 0.254 mm."""

_LCSC_CODE = re.compile(r"^[Cc]\d+$")

#: EasyEDA copper layer ids. 1 = top, 2 = bottom, 11 = all (through-hole).
_LAYER_TOP, _LAYER_BOTTOM, _LAYER_MULTI = 1, 2, 11

#: EasyEDA graphic layers we read a body outline from, best first.
_LAYER_ASSEMBLY, _LAYER_SILK, _LAYER_DOC = 13, 3, 12

_ELECTRIC = {
    0: ElectricalType.UNSPECIFIED,
    1: ElectricalType.INPUT,
    2: ElectricalType.OUTPUT,
    3: ElectricalType.BIDIRECTIONAL,
    4: ElectricalType.POWER_IN,
}
"""EasyEDA's five-value pin electrical vocabulary, mapped to KiCad's twelve.

The mapping is total but lossy in one direction: EasyEDA cannot express
`power_out`, `passive`, `open_collector` or `no_connect`, so an LCSC symbol
types a resistor's two terminals as `input`. We map faithfully and let
`kifab check` say so — see `NOT_NORMALISED`.
"""

_ROTATION_TO_SIDE = {0: Side.RIGHT, 90: Side.TOP, 180: Side.LEFT, 270: Side.BOTTOM}
"""EasyEDA pin rotation -> which edge of the body the pin sits on.

The pin's stated (x, y) is its *connection* end and the stub is drawn back
toward the body, so rotation 0 (stub drawn to the left) is a pin on the body's
right-hand edge. Verified against the RP2040 fixture, which has pins at 0
(right of centre), 180 (left of centre) and 270 (below centre).
"""

_ILLEGAL_IN_NAME = re.compile(r"[:/\\\s]+")


class EasyEdaError(ValueError):
    """The component cannot be turned into a valid, trustworthy part."""


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
"""Measured, not decorative: easyeda.com sits behind a CDN that answers 403 to
a tool-shaped User-Agent (`easyeda2kicad v0.8.0` included) and 200 to this one.
"""

COMPONENT_URL = "https://easyeda.com/api/products/{code}/components?version=6.4.19.5"
MODEL_STEP_URL = "https://modules.easyeda.com/qAxj6KHrDKw4blvCG8QJPs7Y/{uuid}"
SEARCH_URL = (
    "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/"
    "selectSmtComponentList"
)


@dataclass(frozen=True)
class Candidate:
    """One MPN search hit. Deliberately not auto-selected — see `resolve_code`."""

    code: str
    mpn: str
    package: str
    manufacturer: str
    stock: int = 0

    def __str__(self) -> str:
        return (
            f"{self.code:>10}  {self.mpn}  [{self.package}]  {self.manufacturer}"
            f"  stock {self.stock}"
        )


class EasyEdaClient:
    """The whole network surface of T1: three URLs, stdlib only.

    `fetch` is injectable so every test in the suite runs offline against
    recorded fixtures; nothing below this class knows the network exists.
    """

    def __init__(
        self,
        fetch: Callable[[str, bytes | None], bytes] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._fetch = fetch or self._urlopen
        self.timeout = timeout

    def _urlopen(self, url: str, body: bytes | None = None) -> bytes:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.URLError as exc:
            raise EasyEdaError(f"could not reach {url}: {exc}") from exc

    def component(self, code: str) -> dict:
        """The raw component payload for an LCSC code, unmodified."""
        raw = self._fetch(COMPONENT_URL.format(code=code.upper()), None)
        return _payload(json.loads(raw), code)

    def search(self, query: str, limit: int = 8) -> list[Candidate]:
        """MPN -> LCSC codes. Returns *candidates*; it never picks one."""
        body = json.dumps(
            {"currentPage": 1, "pageSize": limit, "keyword": query}
        ).encode()
        try:
            data = json.loads(self._fetch(SEARCH_URL, body))
        except (json.JSONDecodeError, KeyError) as exc:
            raise EasyEdaError(f"the parts search returned no usable data: {exc}") from exc
        rows = (data.get("data") or {}).get("componentPageInfo") or {}
        return [
            Candidate(
                code=row.get("componentCode", ""),
                mpn=row.get("componentModelEn", ""),
                package=row.get("componentSpecificationEn", ""),
                manufacturer=row.get("componentBrandEn", ""),
                stock=int(row.get("stockCount") or 0),
            )
            for row in rows.get("list") or []
            if row.get("componentCode")
        ]

    def model_step(self, uuid: str) -> bytes:
        data = self._fetch(MODEL_STEP_URL.format(uuid=uuid), None)
        if not data.lstrip().startswith(b"ISO-10303-21"):
            raise EasyEdaError(f"3D model {uuid} did not come back as a STEP file")
        return data


def _payload(data: Any, code: str) -> dict:
    if not isinstance(data, dict) or not data.get("success"):
        message = (data or {}).get("message", "component not found")
        raise EasyEdaError(f"LCSC {code}: {message}")
    result = data.get("result")
    if not isinstance(result, dict):
        raise EasyEdaError(f"LCSC {code}: the response carried no component")
    return result


def resolve_code(client: EasyEdaClient, query: str) -> tuple[str, list[Candidate]]:
    """Turn a user query into an LCSC code, or into candidates to choose from.

    An LCSC code is used as given. An MPN is resolved only when exactly one hit
    matches it exactly, case-insensitively — the same discipline `kifab search`
    applies: a near miss is handed back for a human, never auto-selected.
    """
    query = query.strip()
    if _LCSC_CODE.match(query):
        return query.upper(), []
    candidates = client.search(query)
    exact = [c for c in candidates if c.mpn.strip().lower() == query.lower()]
    if len(exact) == 1:
        return exact[0].code, candidates
    return "", candidates


# --------------------------------------------------------------------------
# Shape-string parsing
# --------------------------------------------------------------------------


def _num(text: str, default: float = 0.0) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def _fields(shape: str) -> list[str]:
    return shape.split("~")


def _points(text: str) -> list[tuple[float, float]]:
    values = [_num(v) for v in text.replace(",", " ").split() if v]
    return [(values[i], values[i + 1]) for i in range(0, len(values) - 1, 2)]


@dataclass
class _RawPad:
    number: str
    x: float
    y: float
    width: float
    height: float
    layer: int
    hole_radius: float
    rotation: float
    shape: str
    hole_length: float


def _parse_pads(shapes: list[str]) -> tuple[list[_RawPad], int]:
    """`PAD~shape~x~y~w~h~layer~net~number~holeR~points~rot~id~lock~holeLen~…`"""
    pads: list[_RawPad] = []
    unnumbered = 0
    for shape in shapes:
        if not shape.startswith("PAD~"):
            continue
        f = _fields(shape)
        if len(f) < 12:
            continue
        number = f[8].strip()
        if not number:
            unnumbered += 1
            continue
        pads.append(
            _RawPad(
                number=number,
                x=_num(f[2]),
                y=_num(f[3]),
                width=_num(f[4]),
                height=_num(f[5]),
                layer=int(_num(f[6], 1)),
                hole_radius=_num(f[9]),
                rotation=_num(f[11]) % 360,
                shape=f[1].strip().upper(),
                hole_length=_num(f[14]) if len(f) > 14 else 0.0,
            )
        )
    return pads, unnumbered


def _graphic_extent(shapes: list[str], layers: set[int]) -> tuple[float, float] | None:
    """Bounding box (width, height) of the TRACK/ARC/CIRCLE/RECT on `layers`."""
    xs: list[float] = []
    ys: list[float] = []
    for shape in shapes:
        f = _fields(shape)
        kind = f[0]
        if kind == "TRACK" and len(f) > 4 and int(_num(f[2], -1)) in layers:
            for x, y in _points(f[4]):
                xs.append(x)
                ys.append(y)
        elif kind == "CIRCLE" and len(f) > 5 and int(_num(f[5], -1)) in layers:
            cx, cy, r = _num(f[1]), _num(f[2]), _num(f[3])
            xs += [cx - r, cx + r]
            ys += [cy - r, cy + r]
        elif kind == "RECT" and len(f) > 6 and int(_num(f[6], -1)) in layers:
            x, y, w, h = _num(f[1]), _num(f[2]), _num(f[3]), _num(f[4])
            xs += [x, x + w]
            ys += [y, y + h]
        elif kind == "ARC" and len(f) > 4 and int(_num(f[2], -1)) in layers:
            # An arc is an SVG path. Its control numbers over-state the extent
            # slightly and its endpoints under-state it; taking every number is
            # the conservative reading, and the body is cross-checked against
            # the package name anyway.
            numbers = [float(v) for v in re.findall(r"-?\d+\.\d+|-?\d+", f[4])]
            for i in range(0, len(numbers) - 1, 2):
                xs.append(numbers[i])
                ys.append(numbers[i + 1])
    if len(xs) < 2 or len(ys) < 2:
        return None
    return (max(xs) - min(xs), max(ys) - min(ys))


@dataclass
class _RawPin:
    number: str
    sequence: str
    name: str
    electric: int
    x: float
    y: float
    rotation: int


def _parse_pins(shapes: list[str]) -> list[_RawPin]:
    """`P~show~electric~seq~x~y~rot~id~lock` + `^^`-joined sub-records."""
    pins: list[_RawPin] = []
    for shape in shapes:
        if not shape.startswith("P~"):
            continue
        segments = shape.split("^^")
        head = _fields(segments[0])
        if len(head) < 7:
            continue
        # Sub-records, in order: dot, path, name, number, dot-flag, clock.
        name_seg = _fields(segments[3]) if len(segments) > 3 else []
        number_seg = _fields(segments[4]) if len(segments) > 4 else []
        name = name_seg[4].strip() if len(name_seg) > 4 else ""
        number = number_seg[4].strip() if len(number_seg) > 4 else ""
        pins.append(
            _RawPin(
                number=number,
                sequence=head[3].strip(),
                name=name or "~",
                electric=int(_num(head[2], 0)),
                x=_num(head[4]),
                y=_num(head[5]),
                rotation=int(_num(head[6]) % 360),
            )
        )
    return pins


def _svgnode_model(shapes: list[str]) -> dict:
    for shape in shapes:
        if not shape.startswith("SVGNODE~"):
            continue
        try:
            node = json.loads(shape.split("~", 1)[1])
        except json.JSONDecodeError:
            continue
        attrs = node.get("attrs") or {}
        if attrs.get("uuid"):
            return attrs
    return {}


# --------------------------------------------------------------------------
# Normalisation into the IR
# --------------------------------------------------------------------------


def _pad_dict(raw: _RawPad, origin: tuple[float, float], notes: "_Notes") -> dict:
    width, height, rotation = raw.width, raw.height, raw.rotation

    shape = {
        "RECT": PadShape.RECT,
        "OVAL": PadShape.OVAL,
        "ELLIPSE": PadShape.CIRCLE,
    }.get(raw.shape)
    if shape is None:
        raise EasyEdaError(
            f"pad {raw.number!r} is an EasyEDA {raw.shape or 'unknown'} pad, which "
            "the IR cannot represent without approximating its copper. Import it "
            "by hand, or use tier T2."
        )
    if shape is PadShape.CIRCLE and abs(width - height) > 1e-9:
        shape = PadShape.OVAL

    # Provable: a rectangle, oval or circle has 180-degree symmetry, so a
    # rotation of 180 is a no-op and one of 90/270 is exactly a size swap.
    # Normalising them away is value-preserving and removes the single most
    # common cosmetic defect in EasyEDA footprints (see NORMALISATIONS).
    if abs(rotation % 180.0) < 1e-6:
        rotation = 0.0
    elif abs(rotation % 180.0 - 90.0) < 1e-6:
        width, height = height, width
        rotation = 0.0
        notes.count("rotated_pads")
    else:
        rotation = rotation % 180.0
        notes.count("oblique_pads")

    through = raw.hole_radius > 0 or raw.layer == _LAYER_MULTI
    entry: dict = {
        "number": raw.number,
        "at": [
            round((raw.x - origin[0]) * TENMIL_MM, 4),
            round((raw.y - origin[1]) * TENMIL_MM, 4),
        ],
        "size": [round(width * TENMIL_MM, 4), round(height * TENMIL_MM, 4)],
        "shape": shape.value,
        "type": (PadType.THRU_HOLE if through else PadType.SMD).value,
    }
    if rotation:
        entry["rotation"] = round(rotation, 4)
    if through:
        if raw.hole_radius <= 0:
            raise EasyEdaError(
                f"pad {raw.number!r} is on EasyEDA's multi-layer but states no "
                "hole size; refusing to guess a drill diameter"
            )
        entry["drill"] = round(raw.hole_radius * 2 * TENMIL_MM, 4)
        if raw.hole_length > 0:
            notes.add(
                f"pad {raw.number!r} has a slotted hole "
                f"({raw.hole_length * TENMIL_MM:.2f} mm long). The IR stores a "
                "round drill only, so the slot was imported as a round hole of "
                "the stated diameter — check it against the datasheet."
            )
    elif raw.layer == _LAYER_BOTTOM:
        entry["layers"] = ["B.Cu", "B.Mask", "B.Paste"]
        notes.add(
            f"pad {raw.number!r} is on the bottom copper layer; it was imported "
            "as a bottom-side SMD land"
        )
    elif raw.layer != _LAYER_TOP:
        notes.add(
            f"pad {raw.number!r} is on EasyEDA layer {raw.layer}, which is not a "
            "copper layer; it was imported as a top-side SMD land"
        )
    return entry


def _body(
    package_name: str,
    shapes: list[str],
    pads: list[dict],
    notes: "_Notes",
) -> dict:
    """Body size in mm, from the best available evidence, and say which.

    EasyEDA's silkscreen is not a body outline: on a gull-wing package it is
    drawn only *between* the pad rows (so it understates one axis) and on a
    chip it is drawn *around* the pads (so it overstates both). The package
    name, however, usually states the datasheet body as `L<len>-W<wid>` — an
    orientation-independent pair. So: take the pair from the name, and let the
    drawing decide which axis is which.
    """
    drawn = None
    for layers, source in (
        ({_LAYER_ASSEMBLY}, "assembly layer"),
        ({_LAYER_SILK}, "silkscreen"),
        ({_LAYER_DOC}, "document layer"),
    ):
        drawn = _graphic_extent(shapes, layers)
        if drawn:
            drawn_source = source
            break

    drawn_mm = (drawn[0] * TENMIL_MM, drawn[1] * TENMIL_MM) if drawn else None

    stated = re.search(r"[-_]L(\d+\.?\d*)-W(\d+\.?\d*)", package_name)
    if stated:
        a, b = float(stated.group(1)), float(stated.group(2))
        if drawn_mm and abs(drawn_mm[0] - b) + abs(drawn_mm[1] - a) < abs(
            drawn_mm[0] - a
        ) + abs(drawn_mm[1] - b):
            a, b = b, a
        notes.add(
            f"body {a} x {b} mm taken from the package name {package_name!r}; "
            f"the {drawn_source if drawn else 'drawing'} was used only to decide "
            "which dimension is which axis"
            if drawn
            else f"body {a} x {b} mm taken from the package name {package_name!r}"
        )
        return {"x": round(a, 3), "y": round(b, 3)}

    if drawn_mm:
        notes.add(
            f"the package name states no body size, so the body was measured "
            f"from the {drawn_source} ({drawn_mm[0]:.2f} x {drawn_mm[1]:.2f} mm). "
            "EasyEDA silkscreen is drawn around the pads on chip packages, so "
            "this can be larger than the real body — check it against the "
            "datasheet."
        )
        return {
            "x": round(max(drawn_mm[0], 0.01), 3),
            "y": round(max(drawn_mm[1], 0.01), 3),
        }

    xs = [abs(p["at"][0]) + p["size"][0] / 2 for p in pads]
    ys = [abs(p["at"][1]) + p["size"][1] / 2 for p in pads]
    notes.add(
        "no body outline of any kind was drawn; the body was taken from the pad "
        "extent, which is certainly wrong — set it from the datasheet."
    )
    return {"x": round(max(max(xs) * 2, 0.01), 3), "y": round(max(max(ys) * 2, 0.01), 3)}


def _pins(units: list[list[_RawPin]], notes: "_Notes") -> list[dict]:
    """EasyEDA pins -> IR pins (side + slot + unit, never coordinates)."""
    raw_pins = [pin for unit in units for pin in unit]
    if not raw_pins:
        raise EasyEdaError("the EasyEDA symbol has no pins")

    disagreeing = [p for p in raw_pins if p.number and p.number != p.sequence]
    if disagreeing:
        # Measured on C5446 (XC6206P332MR): the header field runs 1,2,3 while
        # the drawn pin numbers are 1,3,2 — the header is a *sequence index*,
        # not a pad number. Reading it would silently swap Vout and Vin.
        notes.add(
            f"{len(disagreeing)} pin(s) have an EasyEDA sequence index that "
            "differs from the drawn pin number "
            + ", ".join(f"#{p.sequence}->{p.number}" for p in disagreeing[:6])
            + ". The drawn number is the pad number and is what was imported."
        )
    for pin in raw_pins:
        if not pin.number:
            raise EasyEdaError(
                f"pin {pin.name!r} has no drawn pin number, so it cannot be bonded "
                "to a pad; refusing to guess one from its position"
            )

    if len(units) > 1:
        notes.add(
            f"EasyEDA states {len(units)} sub-parts; they were imported as "
            "KiCad units 1.."
            f"{len(units)}, each laid out independently in house style"
        )

    out: list[dict] = []
    for index, unit_pins in enumerate(units, start=1):
        groups: dict[Side, list[_RawPin]] = {}
        for pin in unit_pins:
            side = _ROTATION_TO_SIDE.get(pin.rotation)
            if side is None:
                notes.add(
                    f"pin {pin.number!r} is drawn at {pin.rotation} degrees, which "
                    "is not axis-aligned; it was placed on the left edge"
                )
                side = Side.LEFT
            groups.setdefault(side, []).append(pin)

        for side in (Side.LEFT, Side.RIGHT, Side.TOP, Side.BOTTOM):
            members = groups.get(side, [])
            if side in (Side.LEFT, Side.RIGHT):
                members.sort(key=lambda p: p.y)  # EasyEDA y grows downward
            else:
                members.sort(key=lambda p: p.x)
            for slot, pin in enumerate(members):
                entry = {
                    "number": pin.number,
                    "name": pin.name,
                    "type": _ELECTRIC.get(
                        pin.electric, ElectricalType.UNSPECIFIED
                    ).value,
                    "side": side.value,
                    "slot": slot,
                }
                if len(units) > 1:
                    entry["unit"] = index
                out.append(entry)
    return out


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


class _Notes:
    """Notes a reviewer must see, plus counters for the bulk normalisations."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.counters: dict[str, int] = {}

    def add(self, message: str) -> None:
        if message not in self.messages:
            self.messages.append(message)

    def count(self, key: str) -> None:
        self.counters[key] = self.counters.get(key, 0) + 1

    def finish(self) -> list[str]:
        head: list[str] = []
        if self.counters.get("rotated_pads"):
            head.append(
                f"{self.counters['rotated_pads']} pad(s) were stated at 90/270 "
                "degrees; the rotation was folded into the pad size, which is "
                "exactly equivalent and keeps the file readable"
            )
        if self.counters.get("oblique_pads"):
            head.append(
                f"{self.counters['oblique_pads']} pad(s) sit at an oblique angle; "
                "the rotation was kept as stated"
            )
        return head + self.messages


@dataclass
class Model3D:
    """A fetched 3D model and where the footprint should point at it."""

    uuid: str
    title: str
    data: bytes
    suffix: str = ".step"


@dataclass
class Import:
    """An imported part plus everything a reviewer needs to know about it."""

    part: Part
    lcsc: str
    notes: list[str] = field(default_factory=list)
    package: str = ""
    model_uuid: str = ""
    model_title: str = ""
    model_needs_placement: bool = False


def _sanitise(name: str, label: str, notes: _Notes) -> str:
    cleaned = _ILLEGAL_IN_NAME.sub("_", name.strip())
    if cleaned != name.strip():
        notes.add(
            f"the {label} {name.strip()!r} contains characters KiCad forbids in a "
            f"library item name; it was imported as {cleaned!r}"
        )
    if not cleaned:
        raise EasyEdaError(f"the component states no usable {label}")
    return cleaned


def import_component(
    payload: dict,
    *,
    library: str = "kifab",
    model_path: str | None = None,
    bond_extra_pads: bool = False,
) -> Import:
    """Normalise one raw EasyEDA component payload into a validated `Part`.

    Pure: no network, no filesystem. Everything the network layer produces is
    an argument, which is what makes the whole tier testable against recorded
    fixtures.
    """
    notes = _Notes()
    symbol_data = payload.get("dataStr") or {}
    package_detail = payload.get("packageDetail") or {}
    footprint_data = package_detail.get("dataStr") or {}

    # A multi-gate part (a dual op-amp) carries an *empty* top-level shape list
    # and one `subparts` entry per gate. Reading only `dataStr.shape` silently
    # yields a symbol with no pins, so the units are the source of truth
    # whenever they exist.
    unit_shapes: list[list[str]] = []
    for subpart in payload.get("subparts") or []:
        shapes = ((subpart or {}).get("dataStr") or {}).get("shape") or []
        if shapes:
            unit_shapes.append(shapes)
    if not unit_shapes and symbol_data.get("shape"):
        unit_shapes = [symbol_data["shape"]]
    if not unit_shapes:
        raise EasyEdaError("the component payload carries no schematic symbol")
    if not footprint_data.get("shape"):
        raise EasyEdaError("the component payload carries no PCB footprint")

    sym_para = (symbol_data.get("head") or {}).get("c_para") or {}
    fp_head = footprint_data.get("head") or {}
    fp_para = fp_head.get("c_para") or {}
    lcsc = (payload.get("lcsc") or {}).get("number", "") or sym_para.get(
        "Supplier Part", ""
    )

    mpn = _sanitise(
        sym_para.get("Manufacturer Part") or payload.get("title") or lcsc,
        "part number",
        notes,
    )
    package_name = (
        package_detail.get("title") or fp_para.get("package") or "EasyEDA_import"
    )
    footprint_name = _sanitise(package_name, "package name", notes)

    # --- footprint ------------------------------------------------------
    shapes = footprint_data["shape"]
    raw_pads, unnumbered = _parse_pads(shapes)
    if not raw_pads:
        raise EasyEdaError("the EasyEDA footprint has no numbered pads")
    if unnumbered:
        notes.add(
            f"{unnumbered} unnumbered pad(s) (paste apertures / mechanical "
            "features) were dropped — the IR bonds every pad to a symbol pin"
        )
    origin = (_num(str(fp_head.get("x", 0))), _num(str(fp_head.get("y", 0))))
    pads = [_pad_dict(raw, origin, notes) for raw in raw_pads]
    mount = (
        "through_hole"
        if any(p["type"] in ("thru_hole", "np_thru_hole") for p in pads)
        else "smd"
    )
    package = {
        "family": "custom",
        "body": _body(package_name, shapes, pads, notes),
        "mount_type": mount,
        "pads": pads,
    }
    notes.add(
        "lands were lifted verbatim into a `custom` package: EasyEDA states pad "
        "geometry, not the datasheet dimensions IPC-7351B needs, so nothing here "
        "is computed. Two-terminal chip geometry is not derivable at all (see "
        "DECISIONS.md), so chip imports are always `custom`."
    )

    # --- symbol ---------------------------------------------------------
    pins = _pins([_parse_pins(shapes) for shapes in unit_shapes], notes)
    pad_numbers = {p["number"] for p in pads}
    pin_numbers = {p["number"] for p in pins}
    if pin_numbers - pad_numbers:
        raise EasyEdaError(
            f"EasyEDA's symbol and footprint disagree: symbol pin(s) "
            f"{sorted(pin_numbers - pad_numbers)} have no pad "
            f"(pads: {sorted(pad_numbers)}). This component's data is wrong at "
            "the source; fix it by hand or use another tier."
        )
    unbonded = sorted(pad_numbers - pin_numbers)
    if unbonded:
        if not bond_extra_pads:
            raise EasyEdaError(
                f"pad(s) {unbonded} have no symbol pin, so the netlist could not "
                "reach them. Re-run with bond_extra_pads=True (CLI: "
                "--bond-extra-pads) to add explicitly-unverified pins for them."
            )
        for number in unbonded:
            pins.append(
                {
                    "number": number,
                    "name": "~",
                    "type": ElectricalType.UNSPECIFIED.value,
                    "side": Side.BOTTOM.value,
                    "slot": unbonded.index(number),
                }
            )
        notes.add(
            f"pad(s) {unbonded} had no symbol pin. Unverified pins typed "
            "'unspecified' and with no name were synthesised so the netlist can "
            "reach them — name and type them from the datasheet before use."
        )

    # --- identity -------------------------------------------------------
    link = fp_para.get("link", "") or sym_para.get("link", "")
    datasheet = (
        link
        if link.lower().endswith(".pdf")
        else (f"https://www.lcsc.com/product-detail/{lcsc}.html" if lcsc else "")
    )
    description = payload.get("description") or ", ".join(payload.get("tags") or [])

    data: dict = {
        "mpn": mpn,
        "manufacturer": sym_para.get("Manufacturer", ""),
        "library": library,
        "reference": (sym_para.get("pre") or fp_para.get("pre") or "U").rstrip("?")
        or "U",
        "datasheet": datasheet,
        "description": description,
        "symbol": {"pins": pins},
        "footprint": {
            "name": footprint_name,
            "description": f"{package_name}, imported from LCSC {lcsc}".strip(", "),
            "tags": " ".join(t for t in (package_name, lcsc) if t),
            "package": package,
        },
    }
    value = sym_para.get("Value") or sym_para.get(sym_para.get("nameAlias", ""), "")
    if value and value != mpn:
        data["value"] = value
    if model_path:
        data["footprint"]["model"] = model_path

    try:
        part = Part.model_validate(data)
    except Exception as exc:  # pydantic ValidationError
        raise EasyEdaError(f"the imported component is not a valid part: {exc}") from exc

    attrs = _svgnode_model(shapes)
    model_uuid = attrs.get("uuid") or fp_head.get("uuid_3d") or ""
    origin_3d = _points(attrs.get("c_origin", "")) if attrs else []
    needs_placement = bool(attrs) and (
        attrs.get("c_rotation", "0,0,0") not in ("0,0,0", "")
        or _num(attrs.get("z", "0")) != 0.0
        or (
            origin_3d
            and (
                abs(origin_3d[0][0] - origin[0]) > 1e-3
                or abs(origin_3d[0][1] - origin[1]) > 1e-3
            )
        )
    )
    if needs_placement:
        notes.add(
            "the EasyEDA 3D model states a non-zero offset or rotation. The IR "
            "emits models at the footprint origin with no rotation, so check the "
            "model's placement in the 3D viewer."
        )

    return Import(
        part=part,
        lcsc=lcsc,
        notes=notes.finish(),
        package=package_name,
        model_uuid=model_uuid,
        model_title=attrs.get("title", "") or package_name,
        model_needs_placement=needs_placement,
    )


DEFAULT_MODEL_VAR = "KIFAB_3DMODEL_DIR"


def model_reference(library: str, footprint_name: str, variable: str) -> str:
    """The `${VAR}`-relative path KLC-F9.1 requires a model reference to use."""
    return f"${{{variable}}}/{library}.3dshapes/{footprint_name}.step"


def model_destination(root: Path, library: str, footprint_name: str) -> Path:
    return Path(root) / f"{library}.3dshapes" / f"{footprint_name}.step"


def fetch_part(
    query: str,
    *,
    client: EasyEdaClient | None = None,
    library: str = "kifab",
    models_dir: Path | None = None,
    model_variable: str = DEFAULT_MODEL_VAR,
    bond_extra_pads: bool = False,
) -> tuple[Import, Model3D | None]:
    """The whole T1 tier: query -> validated `Part` (+ the 3D model, if any)."""
    client = client or EasyEdaClient()
    code, candidates = resolve_code(client, query)
    if not code:
        listing = "\n".join(f"    {c}" for c in candidates[:8])
        raise EasyEdaError(
            f"{query!r} did not resolve to exactly one LCSC part.\n"
            + (
                f"Candidates:\n{listing}\nRe-run with the LCSC code you want."
                if candidates
                else "Nothing matched. Try the LCSC code (e.g. C2040)."
            )
        )

    payload = client.component(code)
    imported = import_component(
        payload, library=library, bond_extra_pads=bond_extra_pads
    )

    model: Model3D | None = None
    if models_dir is not None and imported.model_uuid:
        try:
            model = Model3D(
                uuid=imported.model_uuid,
                title=imported.model_title,
                data=client.model_step(imported.model_uuid),
            )
        except EasyEdaError as exc:
            imported.notes.append(
                f"the 3D model could not be fetched ({exc}); the footprint was "
                "written without a model reference"
            )
    elif models_dir is not None:
        imported.notes.append(
            "EasyEDA lists no 3D model for this package; the footprint was "
            "written without a model reference"
        )

    if model is not None:
        # Re-validate with the model reference attached rather than mutating the
        # Part: the IR is the contract, and it should be built once, valid.
        imported = import_component(
            payload,
            library=library,
            model_path=model_reference(
                library, imported.part.footprint.name, model_variable
            ),
            bond_extra_pads=bond_extra_pads,
        )
    return imported, model


def write_model(model: Model3D, root: Path, library: str, footprint_name: str) -> Path:
    path = model_destination(root, library, footprint_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(model.data)
    return path


# --------------------------------------------------------------------------
# YAML rendering
# --------------------------------------------------------------------------


def to_yaml(imported: Import) -> str:
    header = [
        f"# Imported from LCSC {imported.lcsc} via EasyEDA by `kifab lcsc` (T1).",
        f"# package: {imported.package}",
        "#",
        "# EasyEDA is an INGESTER here, not a generator: this YAML is the",
        "# artefact, and the .kicad_sym / .kicad_mod are re-emitted from it in",
        "# house style by kifab's own emitters. Correct this file, not the",
        "# generated output.",
        "#",
    ]
    for note in imported.notes:
        header.append(f"# NOTE: {note}")
    return render_part_yaml(imported.part, header)


# --------------------------------------------------------------------------
# The normalisation ledger — data, so it can be reviewed and tested
# --------------------------------------------------------------------------

NORMALISATIONS: dict[str, str] = {
    "pad rotation": "A rect/oval/circle land stated at 90 or 270 degrees is "
    "rewritten as a size swap at 0 degrees, and 180 degrees is dropped. Exactly "
    "equivalent copper (these shapes have 180-degree symmetry) and it makes the "
    "pad table readable.",
    "pin numbers": "The pad number is read from the *drawn* pin-number text, not "
    "from EasyEDA's header field, which is a sequence index. Measured on C5446, "
    "where the two disagree and the header would swap two pins.",
    "pin layout": "Pins keep the side EasyEDA drew them on and their order along "
    "that side, but are re-laid-out on the house grid by the symbol emitter — "
    "the IR stores a side and a slot, never coordinates.",
    "units": "EasyEDA's 10-mil grid is converted to mm exactly (x 0.254) and "
    "rounded to 0.1 um, below any fabricable tolerance.",
    "body size": "Taken from the `L…-W…` in the package name (the datasheet body) "
    "with the drawn outline used only to decide which dimension is which axis. "
    "EasyEDA's silkscreen is not a body outline and is never used as one when the "
    "name states the size.",
    "silk / courtyard / fab": "Discarded entirely and redrawn by our emitter from "
    "the body and the lands, which is what makes an imported part look like every "
    "other kifab part and pass the same KLC rules.",
}

NOT_NORMALISED: dict[str, str] = {
    "electrical types": "EasyEDA has five pin types (unspecified/input/output/"
    "bidirectional/power) against KiCad's twelve, and LCSC's own symbols use them "
    "loosely — a resistor arrives with two `input` pins. Retyping from the pin "
    "name would be guessing, so the mapping is faithful and `kifab check` reports "
    "the contradictions it can prove (SCH002 power-pin naming).",
    "pin names": "Imported verbatim. EasyEDA has no overbar convention we can "
    "trust to translate, so an active-low name arrives as written.",
    "chip land geometry": "Not derivable. IPC's two-terminal maths needs a lead "
    "separation the size tables do not state (DECISIONS.md, Phase 1), so chip "
    "parts stay in the `custom` family with EasyEDA's lands, not computed ones.",
    "polygon pads": "Refused rather than approximated by a bounding rectangle — "
    "an approximated land is the silent wrongness this project exists to prevent.",
    "slotted holes": "The IR stores a round drill. A slotted hole is imported as "
    "a round one of the stated diameter and flagged, never silently squared off.",
    "3D model placement": "Offset and rotation are reported, not applied: the IR "
    "emits models at the footprint origin.",
}
