"""Adopt an existing KiCad symbol/footprint into the project as IR YAML.

The point of T0 is reuse, but reuse must not be a dead end. If `kifab search`
finds the right part and we simply told the user "it's over there", the result
would sit outside everything this project is for: no house style, no
validators, no correction path, no Part-DB registration.

So adoption lands in the same place every other tier lands — `parts/<MPN>.yaml`.
The adopted part is then a normal citizen: it rebuilds with `kifab build`, it
passes the same conformance gate, and a wrong pin is a one-line YAML edit.

What adoption is *not*
----------------------
It is not a byte copy. Pads are lifted verbatim into a `custom` package (the
IR's declared escape hatch, so the reuse is visible in review), but the symbol
is **re-laid-out in house style**: the IR stores a pin's side and slot, never
its coordinates. A donor symbol whose pins were grouped by function comes back
as a rectangle with pins in positional order. That is the documented trade —
consistency over inherited layout — and `adopt()` reports it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..emit import sexpr
from ..index.read import _atoms, _floats, _str_arg, unquote
from ..ir import Part
from ..ir.enums import ElectricalType, PadShape, PadType, PinShape, Side

_ANGLE_TO_SIDE = {0: Side.LEFT, 180: Side.RIGHT, 270: Side.TOP, 90: Side.BOTTOM}
"""KiCad pin angles point *from the connection end toward the body*, so a pin
drawn at 0 deg sits on the body's left edge."""

_SHAPE_TOKENS = {s.value for s in PadShape}
_PAD_TYPE_TOKENS = {t.value for t in PadType}


class AdoptionError(ValueError):
    """Raised when the donor files cannot be turned into a valid part."""


@dataclass
class Adoption:
    """The adopted part plus everything a reviewer needs to know about it."""

    part: Part
    notes: list[str] = field(default_factory=list)
    symbol_source: str = ""
    footprint_source: str = ""


# --------------------------------------------------------------------------
# Footprint -> CustomPackage
# --------------------------------------------------------------------------


def _bbox(root: sexpr.Node, layer: str) -> tuple[float, float, float, float] | None:
    xs: list[float] = []
    ys: list[float] = []
    for child in root:
        if not isinstance(child, list) or not child:
            continue
        head = child[0]
        if not isinstance(head, str) or not head.startswith("fp_"):
            continue
        node_layer = sexpr.find(child, "layer")
        if node_layer is None or layer not in {
            unquote(a) for a in _atoms(node_layer)[1:]
        }:
            continue
        for token in ("start", "end", "center", "mid"):
            point = _floats(sexpr.find(child, token), 2)
            if point:
                xs.append(point[0])
                ys.append(point[1])
        pts = sexpr.find(child, "pts")
        if pts is not None:
            for xy in sexpr.find_all(pts, "xy"):
                point = _floats(xy, 2)
                if point:
                    xs.append(point[0])
                    ys.append(point[1])
    if len(xs) < 2 or len(ys) < 2:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def footprint_package(path: Path) -> tuple[dict, list[str]]:
    """Read a `.kicad_mod` into the IR's `custom` package dict."""
    root = sexpr.parse(Path(path).read_text(encoding="utf-8"))
    head = root[0] if root and isinstance(root[0], str) else ""
    if head not in ("footprint", "module"):
        raise AdoptionError(f"{path}: not a KiCad footprint file")

    notes: list[str] = []
    pads: list[dict] = []
    dropped = 0
    for pad in sexpr.find_all(root, "pad"):
        atoms = _atoms(pad)
        number = unquote(atoms[1]) if len(atoms) > 1 else ""
        if not number.strip():
            dropped += 1
            continue
        at = _floats(sexpr.find(pad, "at"), 2)
        size = _floats(sexpr.find(pad, "size"), 2)
        if at is None or size is None:
            raise AdoptionError(f"{path}: pad {number!r} has no position or size")
        rot = _floats(sexpr.find(pad, "at"), 3)
        tokens = [unquote(a) for a in atoms[2:5]]
        pad_type = next((t for t in tokens if t in _PAD_TYPE_TOKENS), "smd")
        shape = next((t for t in tokens if t in _SHAPE_TOKENS), "rect")
        entry: dict = {
            "number": number,
            "at": [round(at[0], 4), round(at[1], 4)],
            "size": [round(size[0], 4), round(size[1], 4)],
            "shape": shape,
            "type": pad_type,
        }
        if rot is not None and abs(rot[2]) > 1e-9:
            entry["rotation"] = round(rot[2], 4)
        drill = sexpr.find(pad, "drill")
        if drill is not None:
            values = _floats(drill, 1)
            if values:
                entry["drill"] = round(values[0], 4)
        ratio = sexpr.find(pad, "roundrect_rratio")
        if ratio is not None and shape == "roundrect":
            values = _floats(ratio, 1)
            if values and values[0] > 0:
                entry["roundrect_ratio"] = round(values[0], 4)
        pads.append(entry)

    if not pads:
        raise AdoptionError(f"{path}: no numbered pads to adopt")
    if dropped:
        notes.append(
            f"{dropped} unnumbered pad(s) (paste apertures / mechanical features) "
            "were dropped — the IR bonds every pad to a symbol pin"
        )

    fab = _bbox(root, "F.Fab")
    if fab is None:
        xs = [p["at"][0] for p in pads]
        ys = [p["at"][1] for p in pads]
        fab = (min(xs), min(ys), max(xs), max(ys))
        notes.append("no F.Fab outline; body taken from the pad extent")
    body_x = round(max(fab[2] - fab[0], 0.01), 3)
    body_y = round(max(fab[3] - fab[1], 0.01), 3)

    pad_half_x = max(abs(p["at"][0]) + p["size"][0] / 2 for p in pads)
    pad_half_y = max(abs(p["at"][1]) + p["size"][1] / 2 for p in pads)
    courtyard = 0.25
    crtyd = _bbox(root, "F.CrtYd")
    if crtyd is not None:
        excess_x = max(abs(crtyd[0]), abs(crtyd[2])) - max(pad_half_x, body_x / 2)
        excess_y = max(abs(crtyd[1]), abs(crtyd[3])) - max(pad_half_y, body_y / 2)
        courtyard = round(max(0.0, min(1.0, max(excess_x, excess_y))), 3)

    attr = sexpr.find(root, "attr")
    mount = "smd"
    if attr is not None:
        flags = {unquote(a) for a in _atoms(attr)[1:]}
        if "through_hole" in flags:
            mount = "through_hole"
    if any(p["type"] in ("thru_hole", "np_thru_hole") for p in pads):
        mount = "through_hole"

    package = {
        "family": "custom",
        "body": {"x": body_x, "y": body_y},
        "courtyard": courtyard,
        "mount_type": mount,
        "pads": pads,
    }
    return package, notes


def footprint_meta(path: Path) -> dict:
    root = sexpr.parse(Path(path).read_text(encoding="utf-8"))
    return {
        "name": _str_arg(root, 1) or Path(path).stem,
        "description": _str_arg(sexpr.find(root, "descr")),
        "tags": _str_arg(sexpr.find(root, "tags")),
    }


# --------------------------------------------------------------------------
# Symbol -> pins
# --------------------------------------------------------------------------


def _find_symbol(root: sexpr.Node, name: str) -> sexpr.Node:
    for node in sexpr.find_all(root, "symbol"):
        if _str_arg(node, 1) == name:
            return node
    raise AdoptionError(f"symbol {name!r} not found in library")


def symbol_pins(path: Path, name: str) -> tuple[list[dict], dict, list[str]]:
    """Read one symbol's pins, following `extends`, into IR pin dicts."""
    root = sexpr.parse(Path(path).read_text(encoding="utf-8"))
    if (root[0] if root and isinstance(root[0], str) else "") != "kicad_symbol_lib":
        raise AdoptionError(f"{path}: not a KiCad symbol library")

    notes: list[str] = []
    node = _find_symbol(root, name)
    props: dict[str, str] = {}
    chain = [node]
    seen = {name}
    parent = _str_arg(sexpr.find(node, "extends"))
    while parent and parent not in seen:
        seen.add(parent)
        parent_node = _find_symbol(root, parent)
        chain.append(parent_node)
        parent = _str_arg(sexpr.find(parent_node, "extends"))
    if len(chain) > 1:
        notes.append(
            f"{name!r} extends {'/'.join(_str_arg(n, 1) for n in chain[1:])}; "
            "pins were taken from the base symbol"
        )
    for source in chain:
        for prop in sexpr.find_all(source, "property"):
            atoms = _atoms(prop)
            if len(atoms) >= 3:
                props.setdefault(unquote(atoms[1]), unquote(atoms[2]))

    raw: list[dict] = []
    for source in chain:
        for sub in sexpr.find_all(source, "symbol"):
            sub_name = _str_arg(sub, 1)
            tail = sub_name.rsplit("_", 2)[-2:] if "_" in sub_name else []
            unit, style = 1, 1
            if len(tail) == 2 and tail[0].isdigit() and tail[1].isdigit():
                unit, style = int(tail[0]), int(tail[1])
            if style not in (0, 1):
                continue
            for pin in sexpr.find_all(sub, "pin"):
                atoms = _atoms(pin)
                etype = unquote(atoms[1]) if len(atoms) > 1 else "unspecified"
                shape = unquote(atoms[2]) if len(atoms) > 2 else "line"
                at = _floats(sexpr.find(pin, "at"), 3)
                if at is None:
                    continue
                number_node = sexpr.find(pin, "number")
                name_node = sexpr.find(pin, "name")
                raw.append(
                    {
                        "number": _str_arg(number_node) if number_node else "",
                        "name": _str_arg(name_node) if name_node else "~",
                        "type": etype,
                        "shape": shape,
                        "unit": max(1, unit),
                        "x": at[0],
                        "y": at[1],
                        "angle": round(at[2]) % 360,
                    }
                )
        if raw:
            break

    if not raw:
        raise AdoptionError(f"symbol {name!r} has no pins")

    valid_types = {t.value for t in ElectricalType}
    valid_shapes = {s.value for s in PinShape}
    for entry in raw:
        if entry["type"] not in valid_types:
            notes.append(
                f"pin {entry['number']}: unknown electrical type "
                f"{entry['type']!r} -> unspecified"
            )
            entry["type"] = "unspecified"
        if entry["shape"] not in valid_shapes:
            entry["shape"] = "line"

    # Side + slot replace the donor's coordinates: the IR stores conventions,
    # not geometry, which is what keeps every adopted pin on the house grid.
    pins: list[dict] = []
    groups: dict[tuple[int, Side], list[dict]] = {}
    for entry in raw:
        side = _ANGLE_TO_SIDE.get(entry["angle"])
        if side is None:
            notes.append(
                f"pin {entry['number']}: angle {entry['angle']} is not axis-aligned "
                "-> placed on the left"
            )
            side = Side.LEFT
        groups.setdefault((entry["unit"], side), []).append(entry)

    for (unit, side), members in sorted(
        groups.items(), key=lambda kv: (kv[0][0], kv[0][1].value)
    ):
        if side in (Side.LEFT, Side.RIGHT):
            members.sort(key=lambda e: -e["y"])  # top of the sheet first
        else:
            members.sort(key=lambda e: e["x"])
        for slot, entry in enumerate(members):
            pin: dict = {
                "number": entry["number"],
                "name": entry["name"],
                "type": entry["type"],
                "side": side.value,
                "slot": slot,
            }
            if entry["shape"] != "line":
                pin["shape"] = entry["shape"]
            if unit != 1:
                pin["unit"] = unit
            pins.append(pin)

    if len(chain) == 1 and any(e["unit"] > 1 for e in raw):
        notes.append("multi-unit symbol: each unit is laid out independently")
    notes.append(
        "the symbol was re-laid-out in house style; the donor's functional pin "
        "grouping is not preserved"
    )
    return pins, props, notes


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def adopt(
    *,
    mpn: str,
    footprint_path: Path | str,
    symbol_path: Path | str | None = None,
    symbol_name: str | None = None,
    library: str = "kifab",
    reference: str | None = None,
    manufacturer: str = "",
    pins_from_pads: bool = False,
) -> Adoption:
    """Build a validated `Part` from an existing footprint (+ optional symbol).

    Raises `AdoptionError` if the two halves disagree about pin numbers — that
    cross-check is the IR's most valuable guarantee and adoption must not be
    the hole in it.
    """
    footprint_path = Path(footprint_path)
    package, notes = footprint_package(footprint_path)
    meta = footprint_meta(footprint_path)

    props: dict[str, str] = {}
    if symbol_path and symbol_name:
        pins, props, sym_notes = symbol_pins(Path(symbol_path), symbol_name)
        notes += sym_notes
    elif pins_from_pads:
        pads = package["pads"]
        half = (len(pads) + 1) // 2
        pins = [
            {
                "number": pad["number"],
                "name": "~",
                "type": "unspecified",
                "side": "left" if i < half else "right",
                "slot": i if i < half else i - half,
            }
            for i, pad in enumerate(pads)
        ]
        notes.append(
            "no donor symbol: pins were synthesised from the pad numbers with "
            "type 'unspecified' and no names. This is a stub — fill in the pin "
            "table from the datasheet before using the part."
        )
    else:
        raise AdoptionError(
            "no symbol given. Pass a symbol to adopt, or --pins-from-pads to "
            "generate an explicitly-unverified pin stub from the pad numbers."
        )

    data: dict = {
        "mpn": mpn,
        "manufacturer": manufacturer,
        "library": library,
        "reference": reference or (props.get("Reference") or "U").rstrip("?") or "U",
        "datasheet": props.get("Datasheet", ""),
        "description": props.get("Description", ""),
        "symbol": {"pins": pins},
        "footprint": {
            "name": meta["name"],
            "description": meta["description"],
            "tags": meta["tags"],
            "package": package,
        },
    }
    keywords = props.get("ki_keywords", "")
    if keywords:
        data["symbol"]["keywords"] = keywords
    filters = props.get("ki_fp_filters", "")
    if filters:
        data["symbol"]["fp_filters"] = filters.split()

    try:
        part = Part.model_validate(data)
    except Exception as exc:  # pydantic ValidationError
        raise AdoptionError(
            f"the adopted symbol and footprint do not form a valid part: {exc}"
        ) from exc

    return Adoption(
        part=part,
        notes=notes,
        symbol_source=f"{symbol_path}#{symbol_name}" if symbol_name else "",
        footprint_source=str(footprint_path),
    )


# --------------------------------------------------------------------------
# YAML rendering
# --------------------------------------------------------------------------


def _looks_numeric(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def _flow(mapping: dict) -> str:
    parts = []
    for key, value in mapping.items():
        if isinstance(value, str):
            # Pad numbers must stay strings: `Pad.number` is typed `str` (BGA
            # "A1", exposed pad "EP"), and pydantic will not coerce an int to
            # one, so an unquoted `number: 1` makes the file we just wrote fail
            # to load.
            needs_quotes = value == "" or " " in value or _looks_numeric(value)
            rendered = f'"{value}"' if needs_quotes else value
        elif isinstance(value, list):
            rendered = "[" + ", ".join(f"{v:g}" for v in value) + "]"
        elif isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, float):
            rendered = f"{value:g}"
        else:
            rendered = str(value)
        parts.append(f"{key}: {rendered}")
    return "{ " + ", ".join(parts) + " }"


def to_yaml(adoption: Adoption) -> str:
    """Render the adopted part as reviewable YAML, in the house layout.

    Hand-rolled rather than `yaml.safe_dump` for one reason: the pin table and
    the pad table are what a human actually reviews, and they are only
    reviewable one-record-per-line.
    """
    part = adoption.part
    lines = [
        "# Adopted from the local KiCad corpus by `kifab adopt` (T0 reuse).",
        f"# footprint: {adoption.footprint_source}",
    ]
    if adoption.symbol_source:
        lines.append(f"# symbol:    {adoption.symbol_source}")
    lines.append("#")
    for note in adoption.notes:
        lines.append(f"# NOTE: {note}")
    lines.append("")

    dumped = part.model_dump(mode="json", exclude_defaults=True)
    for key in ("mpn", "manufacturer", "library", "reference", "value"):
        if dumped.get(key):
            lines.append(f"{key}: {dumped[key]}")
    for key in ("datasheet", "description"):
        if dumped.get(key):
            lines.append(f"{key}: {_scalar(dumped[key])}")

    lines.append("")
    lines.append("symbol:")
    symbol = dumped.get("symbol", {})
    if symbol.get("keywords"):
        lines.append(f"  keywords: {_scalar(symbol['keywords'])}")
    if symbol.get("fp_filters"):
        lines.append("  fp_filters:")
        lines += [f"    - {f}" for f in symbol["fp_filters"]]
    lines.append("  pins:")
    for pin in symbol.get("pins", []):
        lines.append(f"    - {_flow(pin)}")

    lines.append("")
    lines.append("footprint:")
    footprint = dumped.get("footprint", {})
    lines.append(f"  name: {footprint['name']}")
    for key in ("description", "tags"):
        if footprint.get(key):
            lines.append(f"  {key}: {_scalar(footprint[key])}")
    package = footprint["package"]
    lines.append("  package:")
    lines.append("    family: custom")
    lines.append(f"    body: {_flow(package['body'])}")
    if "courtyard" in package:
        lines.append(f"    courtyard: {package['courtyard']:g}")
    if package.get("mount_type"):
        lines.append(f"    mount_type: {package['mount_type']}")
    lines.append("    pads:")
    for pad in package["pads"]:
        lines.append(f"      - {_flow(pad)}")
    return "\n".join(lines) + "\n"


def _scalar(text: str) -> str:
    if any(ch in text for ch in ':#"\'') or text.strip() != text:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text
